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
import atexit
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_dedup_test_")
# 退出时回收临时目录（测试跑完就地删，别让 /var/folders 越堆越多）
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
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


class FileKeyTest(unittest.TestCase):
    """文件级键 f:<原始文件名>:<字节大小>：判「bot 重传」——解析 bot 每次
    重新上传 file.id 都变（tg: 键判不了），但生成的文件名与字节大小不变。
    拿不全/原名无意义 → None（放行，判重不确定性永不拦下载）。"""

    def test_file_key_uses_name_and_size(self):
        msg = SimpleNamespace(
            file=SimpleNamespace(id="A", name="video.mp4", size=1024)
        )
        self.assertEqual(dedup.file_key(msg), "f:video.mp4:1024")

    def test_file_key_none_when_incomplete(self):
        self.assertIsNone(dedup.file_key(SimpleNamespace(file=None)))
        self.assertIsNone(dedup.file_key(SimpleNamespace(
            file=SimpleNamespace(id="A", name="v.mp4", size=None))))
        self.assertIsNone(dedup.file_key(SimpleNamespace(
            file=SimpleNamespace(id="A", name="", size=5))))

    def test_file_key_none_when_name_meaningless(self):
        """无意义原名（未命名/UUID）不参与文件级判重：不同文件可能恰好
        同大小，键会误伤（相册照片就没有文件名）。"""
        self.assertIsNone(dedup.file_key(SimpleNamespace(
            file=SimpleNamespace(id="A", name="未命名文件", size=5))))
        self.assertIsNone(dedup.file_key(SimpleNamespace(
            file=SimpleNamespace(
                id="A", name="3f2a" * 8 + ".mp4", size=5))))

    def test_file_key_none_on_error(self):
        class _Boom:
            @property
            def size(self):
                raise RuntimeError("boom")
        self.assertIsNone(dedup.file_key(SimpleNamespace(file=_Boom())))

    def test_media_keys_tg_then_file(self):
        self.assertEqual(
            dedup.media_keys(_media_message()),
            ["tg:VID-123", "f:a.mp4:123"],
        )

    def test_media_keys_drops_missing_keys(self):
        msg = SimpleNamespace(file=SimpleNamespace(id="A", name=None, size=None))
        self.assertEqual(dedup.media_keys(msg), ["tg:A"])
        self.assertEqual(dedup.media_keys(SimpleNamespace(file=None)), [])


class MultiKeyShouldSkipTest(unittest.IsolatedAsyncioTestCase):
    """should_skip 多键版：列表任一键命中即拦；单字符串仍兼容（抖音路径）；
    队列在途匹配新 dedup_keys 列表字段 + 旧 dedup_key 单字段。"""

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

    async def test_any_key_index_hit_blocks(self):
        state.DEDUP_INDEX["f:a.mp4:123"] = {
            "date": "26-09-08 10:00", "filename": "旧文件.mp4",
        }
        skip, notice = dedup.should_skip(["tg:BRAND-NEW", "f:a.mp4:123"])
        self.assertTrue(skip)
        self.assertIn("旧文件.mp4", notice)

    async def test_file_layer_hit_notice_says_same_file(self):
        state.DEDUP_INDEX["f:a.mp4:123"] = {
            "date": "26-09-08 10:00", "filename": "旧文件.mp4",
        }
        _, notice = dedup.should_skip(["tg:BRAND-NEW", "f:a.mp4:123"])
        self.assertIn("相同文件", notice)

    async def test_single_string_key_still_accepted(self):
        """抖音 dyc 路径传单键字符串：签名兼容，行为不变。"""
        state.DEDUP_INDEX["dyc:9"] = {
            "date": "26-09-08", "filename": "旧视频.mp4",
        }
        skip, _ = dedup.should_skip("dyc:9")
        self.assertTrue(skip)

    async def test_queue_matches_dedup_keys_list_field(self):
        state.QUEUE["tasks"] = [
            {"dedup_keys": ["tg:X", "f:a.mp4:123"], "label": "x"},
        ]
        skip, notice = dedup.should_skip(["tg:Y", "f:a.mp4:123"])
        self.assertTrue(skip)
        self.assertIn("队列", notice)

    async def test_queue_old_single_field_still_matches(self):
        """旧队列 JSON 里的 dedup_key 单字段（升级前的存量记录）照样判。"""
        state.QUEUE["retry"] = [{"dedup_key": "tg:abc"}]
        skip, _ = dedup.should_skip(["tg:abc"])
        self.assertTrue(skip)

    async def test_empty_list_passes(self):
        self.assertEqual(dedup.should_skip([]), (False, None))


class ContentDedupTest(unittest.TestCase):
    """内容级键 c:<sha256>：下载完成后、落盘前查——字节相同即拦，
    是元数据/文件两层全漏掉时的最终兜底。"""

    def setUp(self):
        self.old = (state.DEDUP_INDEX, state.DEDUP_ENABLED)
        state.DEDUP_INDEX = {}
        state.DEDUP_ENABLED = True
        self.idx_file = os.path.join(_TMP, "dedup_index_content_test.txt")
        if os.path.exists(self.idx_file):
            os.remove(self.idx_file)

    def tearDown(self):
        state.DEDUP_INDEX, state.DEDUP_ENABLED = self.old
        if os.path.exists(self.idx_file):
            os.remove(self.idx_file)

    def test_content_key_prefixes_digest(self):
        digest = "ab" * 32
        self.assertEqual(dedup.content_key(digest), f"c:{digest}")
        self.assertIsNone(dedup.content_key(None))
        self.assertIsNone(dedup.content_key(""))

    def test_content_seen_hit_miss_off_empty(self):
        state.DEDUP_INDEX[dedup.content_key("h1")] = {
            "date": "26-09-08 10:00", "filename": "原文件.mp4",
        }
        self.assertEqual(
            dedup.content_seen("h1"), state.DEDUP_INDEX["c:h1"]
        )
        self.assertIsNone(dedup.content_seen("no-such-digest"))
        state.DEDUP_ENABLED = False
        self.assertIsNone(dedup.content_seen("h1"))  # 开关关 → 全放行
        state.DEDUP_ENABLED = True
        self.assertIsNone(dedup.content_seen(None))  # 哈希拿不到 → 放行

    def test_remember_list_writes_one_line_per_key(self):
        with mock.patch.object(dedup, "DEDUP_INDEX_FILE", self.idx_file):
            dedup.remember(["tg:A", "c:" + "ab" * 32], "文件.mp4")
        self.assertIn("tg:A", state.DEDUP_INDEX)
        self.assertIn("c:" + "ab" * 32, state.DEDUP_INDEX)
        with open(self.idx_file, "r", encoding="utf-8") as f:
            self.assertEqual(len(f.read().strip().splitlines()), 2)

    def test_remember_list_drops_none_and_accepts_single_string(self):
        with mock.patch.object(dedup, "DEDUP_INDEX_FILE", self.idx_file):
            dedup.remember(["tg:A", None], "x")
            self.assertEqual(list(state.DEDUP_INDEX), ["tg:A"])
            dedup.remember("tg:B", "x")
        self.assertIn("tg:B", state.DEDUP_INDEX)


class EnqueueMediaKeysTest(unittest.IsolatedAsyncioTestCase):
    """enqueue_media 接线：媒体任务入队记录带 dedup_keys 列表（tg: + f:），
    任一键命中（索引或在途）即不入队。"""

    async def asyncSetUp(self):
        self.old = (state.DEDUP_INDEX, state.DEDUP_ENABLED,
                    state.QUEUE, state.QUEUE_LOCK, state.client)
        state.DEDUP_INDEX = {}
        state.DEDUP_ENABLED = True
        state.QUEUE = {"tasks": [], "retry": []}
        state.QUEUE_LOCK = None
        state.client = mock.MagicMock()

    async def asyncTearDown(self):
        (state.DEDUP_INDEX, state.DEDUP_ENABLED,
         state.QUEUE, state.QUEUE_LOCK, state.client) = self.old

    async def test_record_carries_dedup_keys_list(self):
        captured = []

        async def fake_enqueue(record):
            captured.append(record)

        with mock.patch.object(app.queue, "enqueue_and_start", fake_enqueue):
            await app.enqueue_media(_media_message(), 111, "测试来源")

        self.assertEqual(len(captured), 1)
        self.assertEqual(
            captured[0]["dedup_keys"], ["tg:VID-123", "f:a.mp4:123"]
        )

    async def test_file_key_index_hit_skips_enqueue(self):
        """文件级键命中（bot 重传场景：file.id 变、名+大小不变）→ 不入队。"""
        state.DEDUP_INDEX["f:a.mp4:123"] = {
            "date": "26-09-08", "filename": "旧.mp4",
        }
        captured = []

        async def fake_enqueue(record):
            captured.append(record)

        async def fake_send(*a, **k):
            return None

        state.client.send_message = fake_send
        with mock.patch.object(app.queue, "enqueue_and_start", fake_enqueue):
            await app.enqueue_media(_media_message(), 111, "测试来源")

        self.assertEqual(captured, [])

    async def test_dedup_hit_emits_skipped_event_not_task(self):
        """去重跳过是「收到但未产生下载任务」：只发 DEDUP_SKIPPED 事件，
        绝无 RECEIVED/QUEUED（任务数不被污染）。"""
        from tg_userbot import stats as stats_mod

        state.DEDUP_INDEX["f:a.mp4:123"] = {
            "date": "26-09-08", "filename": "旧.mp4",
        }
        ev_file = os.path.join(_TMP, "task_events_dedup_skip.jsonl")
        if os.path.exists(ev_file):
            os.remove(ev_file)

        async def fake_enqueue(record):
            raise AssertionError("去重命中不应入队")

        async def fake_send(*a, **k):
            return None

        state.client.send_message = fake_send
        try:
            with mock.patch.object(app.queue, "enqueue_and_start",
                                   fake_enqueue), \
                    mock.patch.object(stats_mod, "TASK_EVENTS_FILE", ev_file):
                await app.enqueue_media(_media_message(), 111, "测试来源")
            events = stats_mod.load_events(ev_file)
            self.assertEqual([e["ev"] for e in events], ["DEDUP_SKIPPED"])
            self.assertNotIn("id", events[0])  # 没有任务，无 task_id
        finally:
            if os.path.exists(ev_file):
                os.remove(ev_file)

    async def test_douyin_url_dedup_hit_emits_skipped_event(self):
        """抖音 url 判重命中同样发 DEDUP_SKIPPED（输入侧事件，无任务）。"""
        from tg_userbot import stats as stats_mod

        ev_file = os.path.join(_TMP, "task_events_dyc_skip.jsonl")
        if os.path.exists(ev_file):
            os.remove(ev_file)
        message = mock.MagicMock()
        message.message = "看看这个 https://v.douyin.com/abc/"

        async def fake_send(*a, **k):
            return None

        state.client.send_message = fake_send

        async def fake_resolve(url):
            return SimpleNamespace(aweme_id="777", direct_url="https://x",
                                   title="t", author="a")

        try:
            with mock.patch.object(stats_mod, "TASK_EVENTS_FILE", ev_file), \
                    mock.patch.object(dedup, "should_skip",
                                      return_value=(True, "重复")), \
                    mock.patch.object(resolver, "resolve_douyin",
                                      side_effect=fake_resolve), \
                    mock.patch.object(queue_mod, "enqueue_and_start",
                                      side_effect=AssertionError("不应入队")):
                await platform._handle_douyin_urls(
                    message, ["https://v.douyin.com/abc/"]
                )
            events = stats_mod.load_events(ev_file)
            self.assertEqual([e["ev"] for e in events], ["DEDUP_SKIPPED"])
        finally:
            if os.path.exists(ev_file):
                os.remove(ev_file)


if __name__ == "__main__":
    unittest.main()
