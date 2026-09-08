"""重复媒体去重（dedup.py）的单元测试。

背景：同一频道帖子转发两次 / 同一抖音视频换口令重发，都会原样再下载一遍
（几个 GB 的白吃）。去重靠两个稳定元数据键：Telegram 媒体 file_unique_id
（跨聊天/跨转发稳定）与抖音 aweme_id（不同分享口令指到同一视频）。

守护点：
1. 键构造：tg:<file_unique_id> / dyc:<aweme_id>；拿不到 → None（放行，
   判重不确定性永不拦下载）。
2. 索引文件 append-only：remember 只追加一行 + 更新内存 dict（与 history
   同款「事件循环内单行 append 原子」纪律，永不全量重写）。
3. load_index：启动载入内存；超 DEDUP_MAX_ENTRIES 只在启动时裁一次
   （保尾部、原子重写）；坏行跳过；文件缺失返回空。
4. should_skip 两级判重：索引已下载过 → 拦；队列 tasks/retry 在途 → 拦；
   开关关 / 键为 None → 永远放行。
5. 开关持久化（dedup_config.json，仿 thread_config.json）。

不联网；文件全部落在进程级临时 SAVE_FOLDER。
运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_dedup_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import state  # noqa: E402
from tg_userbot import config  # noqa: E402
from tg_userbot import dedup  # noqa: E402
from tg_userbot import queue as queue_mod  # noqa: E402
from tg_userbot import resolver  # noqa: E402
from tg_userbot import app  # noqa: E402
from tg_userbot import platform  # noqa: E402


def _media_message(unique_id="VID-123"):
    """能走通 _build_media_record 全链路的假媒体消息（配方同 test_queue）。"""
    msg = mock.MagicMock()
    msg.id = 1
    msg.message = ""
    msg.fwd_from = None
    msg.media = None
    msg.photo = None
    msg.video = None
    msg.audio = None
    msg.voice = None
    msg.document = None
    # 对齐真实 telethon File：有 id/name/size，没有 unique_id
    msg.file = SimpleNamespace(id=unique_id, name="a.mp4", size=123)
    return msg


class DedupKeyTest(unittest.TestCase):
    """键构造：拿得到稳定元数据才给键，拿不到一律 None（放行）。"""

    def test_media_key_uses_file_unique_id(self):
        self.assertEqual(dedup.media_key(_media_message()), "tg:VID-123")

    def test_media_key_uses_file_id_not_unique_id(self):
        """回归：telethon File 没有 unique_id 属性（取它恒 AttributeError），
        判重键必须取 file.id——否则去重对真实消息静默失效。"""
        # 真实 File 形状：只有 id，没有 unique_id
        real_shaped = SimpleNamespace(
            file=SimpleNamespace(id="DOC-777", name="v.mp4")
        )
        self.assertEqual(dedup.media_key(real_shaped), "tg:DOC-777")

    def test_media_key_none_when_no_file(self):
        self.assertIsNone(dedup.media_key(SimpleNamespace(id=1, file=None)))
        self.assertIsNone(
            dedup.media_key(SimpleNamespace(id=1, file=SimpleNamespace(
                unique_id="")))
        )
        # 异常防御：message.file 访问炸了也只放行，不拦下载
        class _Boom:
            @property
            def unique_id(self):
                raise RuntimeError("boom")
        self.assertIsNone(
            dedup.media_key(SimpleNamespace(id=1, file=_Boom()))
        )

    def test_douyin_key(self):
        self.assertEqual(dedup.douyin_key("7123456"), "dyc:7123456")
        self.assertIsNone(dedup.douyin_key(""))
        self.assertIsNone(dedup.douyin_key(None))


class DedupIndexTest(unittest.TestCase):
    """索引 append / load / 裁剪 / 查询（文件操作全在临时目录）。"""

    def setUp(self):
        self.old_index = state.DEDUP_INDEX
        state.DEDUP_INDEX = {}
        self._patch_file = mock.patch.object(
            dedup, "DEDUP_INDEX_FILE",
            os.path.join(_TMP, "dedup_index_test.txt"),
        )
        self._patch_file.start()
        self.path = dedup.DEDUP_INDEX_FILE
        if os.path.exists(self.path):
            os.remove(self.path)

    def tearDown(self):
        self._patch_file.stop()
        state.DEDUP_INDEX = self.old_index
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_remember_appends_line_and_updates_memory(self):
        dedup.remember("tg:abc", "视频.mp4", 12345)
        dedup.remember("dyc:777", "抖音视频.mp4", 999)
        with open(self.path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("tg:abc\t"))
        self.assertIn("视频.mp4", lines[0])
        self.assertEqual(state.DEDUP_INDEX["tg:abc"]["filename"], "视频.mp4")
        self.assertIsNotNone(state.DEDUP_INDEX["tg:abc"]["date"])

    def test_remember_empty_key_is_noop(self):
        dedup.remember(None, "x.mp4")
        self.assertEqual(state.DEDUP_INDEX, {})
        self.assertFalse(os.path.exists(self.path))

    def test_remember_sanitizes_tabs_and_newlines_in_filename(self):
        dedup.remember("tg:x", "坏\t名字\n.mp4")
        with open(self.path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)  # 文件名里的换行不会拆行
        self.assertNotIn("\t坏", lines[0].split("\t")[-1])

    def test_load_index_missing_file_returns_empty(self):
        self.assertEqual(dedup.load_index(), 0)
        self.assertEqual(state.DEDUP_INDEX, {})

    def test_load_index_parses_lines_and_skips_corrupt(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("tg:a\t26-09-07 10:00\ta.mp4\n")
            f.write("这不是一行合法记录\n")
            f.write("dyc:b\t26-09-07 11:00\tb.mp4\n")
        n = dedup.load_index()
        self.assertEqual(n, 2)
        self.assertEqual(state.DEDUP_INDEX["tg:a"]["filename"], "a.mp4")
        self.assertEqual(state.DEDUP_INDEX["dyc:b"]["date"], "26-09-07 11:00")

    def test_load_index_trims_to_cap_keeping_tail(self):
        with mock.patch.object(dedup, "DEDUP_MAX_ENTRIES", 3):
            with open(self.path, "w", encoding="utf-8") as f:
                for i in range(5):
                    f.write(f"tg:k{i}\t26-09-07 0{i}:00\t文件{i}.mp4\n")
            n = dedup.load_index()
        self.assertEqual(n, 3)
        self.assertNotIn("tg:k0", state.DEDUP_INDEX)
        self.assertNotIn("tg:k1", state.DEDUP_INDEX)
        self.assertIn("tg:k4", state.DEDUP_INDEX)
        with open(self.path, "r", encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), 3)  # 文件也裁到上限


class ShouldSkipTest(unittest.IsolatedAsyncioTestCase):
    """入队前两级判重：索引已下载 → 拦；队列在途 → 拦；开关关/键空 → 放行。"""

    async def asyncSetUp(self):
        self.old = (state.DEDUP_INDEX, state.DEDUP_ENABLED,
                    state.QUEUE, state.QUEUE_LOCK)
        state.DEDUP_INDEX = {}
        state.DEDUP_ENABLED = True
        state.QUEUE = {"tasks": [], "retry": []}
        state.QUEUE_LOCK = None

    async def asyncTearDown(self):
        state.DEDUP_INDEX, state.DEDUP_ENABLED, state.QUEUE, \
            state.QUEUE_LOCK = self.old

    async def test_index_hit_blocks(self):
        state.DEDUP_INDEX["tg:abc"] = {
            "date": "26-09-01 10:00", "filename": "旧视频.mp4",
        }
        skip, notice = dedup.should_skip("tg:abc")
        self.assertTrue(skip)
        self.assertIn("旧视频.mp4", notice)
        self.assertIn("26-09-01", notice)
        self.assertIn("/dedup off", notice)  # 提示怎么临时关掉

    async def test_queue_inflight_hit_blocks(self):
        state.QUEUE["tasks"] = [{"dedup_key": "tg:abc", "label": "x"}]
        skip, notice = dedup.should_skip("tg:abc")
        self.assertTrue(skip)
        self.assertIn("队列", notice)

        state.QUEUE["tasks"] = []
        state.QUEUE["retry"] = [{"dedup_key": "tg:abc"}]
        skip, _ = dedup.should_skip("tg:abc")
        self.assertTrue(skip)  # retry 列表里的也算在途

    async def test_disabled_or_empty_key_always_passes(self):
        state.DEDUP_INDEX["tg:abc"] = {
            "date": "26-09-01", "filename": "旧.mp4",
        }
        state.QUEUE["tasks"] = [{"dedup_key": "tg:abc"}]
        # 开关关 → 索引和在途命中都放行
        state.DEDUP_ENABLED = False
        self.assertEqual(dedup.should_skip("tg:abc"), (False, None))
        # 键拿不到 → 放行（判重不确定性永不拦下载）
        state.DEDUP_ENABLED = True
        self.assertEqual(dedup.should_skip(None), (False, None))
        # 未见过的键 → 放行
        self.assertEqual(dedup.should_skip("tg:other"), (False, None))


class DedupToggleTest(unittest.TestCase):
    """/dedup 开关持久化（dedup_config.json，仿 thread_config.json）。"""

    def setUp(self):
        self.old_enabled = state.DEDUP_ENABLED
        state.DEDUP_ENABLED = True
        self._patch = mock.patch.object(
            dedup, "DEDUP_CONFIG_FILE",
            os.path.join(_TMP, "dedup_config_test.json"),
        )
        self._patch.start()
        if os.path.exists(dedup.DEDUP_CONFIG_FILE):
            os.remove(dedup.DEDUP_CONFIG_FILE)

    def tearDown(self):
        self._patch.stop()
        state.DEDUP_ENABLED = self.old_enabled
        if os.path.exists(dedup.DEDUP_CONFIG_FILE):
            os.remove(dedup.DEDUP_CONFIG_FILE)

    def test_set_enabled_persists_and_flips(self):
        text = dedup.set_enabled(False)
        self.assertIn("关闭", text)
        self.assertFalse(state.DEDUP_ENABLED)
        # 重新读盘应还原为关
        state.DEDUP_ENABLED = True
        dedup.load_dedup_config()
        self.assertFalse(state.DEDUP_ENABLED)

        dedup.set_enabled(True)
        self.assertTrue(state.DEDUP_ENABLED)

    def test_load_missing_config_defaults_on(self):
        dedup.load_dedup_config()
        self.assertTrue(state.DEDUP_ENABLED)

    def test_status_text_reflects_state(self):
        state.DEDUP_ENABLED = True
        self.assertIn("开启", dedup.status_text())
        state.DEDUP_ENABLED = False
        self.assertIn("关闭", dedup.status_text())

    def test_is_dedup_command(self):
        self.assertTrue(dedup.is_dedup_command("/dedup"))
        self.assertTrue(dedup.is_dedup_command("/dedup off"))
        self.assertTrue(dedup.is_dedup_command("/dedup ON"))
        self.assertFalse(dedup.is_dedup_command("/dedup now"))
        self.assertFalse(dedup.is_dedup_command("/queue"))

if __name__ == "__main__":
    unittest.main()
