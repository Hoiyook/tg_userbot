"""t.me 帖子链接直下（tme 模块）的单元测试（2026-11-02）。
覆盖四块：
- parse_tme_post：公开用户名 / c/ 私有 id / s/ 预览前缀 / ?single 尾巴 /
  裸频道链接（非帖子）/ 非 t.me 链接；
- extract_tme_post_urls：混排文本保序去重，非帖子形态不计；
- handle_tme_url：单媒体 / 相册窗口展开（跨组与 None 过滤）/ 纯文本帖 /
  拉取异常 / 帖子不存在——fetcher 与 enqueue 全部注入替身，完全离线；
- backfill_pending：PENDING 里的 t.me 条目逐条处理并销账，非 t.me 不动。
不联网：Telegram 客户端不 import，state.client 缺席路径不触发。
"""
import asyncio
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_tme_test_")
import atexit
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import tme  # noqa: E402


class FakeMsg:
    """Telethon Message 的最小替身：is_downloadable 只看属性面。"""

    def __init__(self, id, media=True, grouped_id=None, title=None):
        self.id = id
        self.file = object() if media else None
        self.photo = None
        self.document = object() if media else None
        self.grouped_id = grouped_id
        self.chat = types.SimpleNamespace(title=title) if title else None


class ParseTest(unittest.TestCase):
    def test_public_username(self):
        self.assertEqual(tme.parse_tme_post("https://t.me/zhangyuyingshi18/9862"),
                         ("zhangyuyingshi18", 9862))

    def test_private_internal_id(self):
        self.assertEqual(tme.parse_tme_post("https://t.me/c/1719225045/19541"),
                         (-1001719225045, 19541))

    def test_s_preview_and_query_tail(self):
        self.assertEqual(tme.parse_tme_post("https://t.me/s/talu3D/69553"),
                         ("talu3D", 69553))
        self.assertEqual(
            tme.parse_tme_post("https://t.me/zhangyuyingshi18/9862?single"),
            ("zhangyuyingshi18", 9862))

    def test_bare_channel_is_not_post(self):
        self.assertIsNone(tme.parse_tme_post("https://t.me/zhangyuyingshi18"))

    def test_non_tme(self):
        self.assertIsNone(tme.parse_tme_post("https://example.com/x/1"))
        self.assertIsNone(tme.parse_tme_post("看看这个 https://t.me/a/1"))


class ExtractTest(unittest.TestCase):
    def test_mixed_text_ordered_dedup(self):
        text = ("https://t.me/talu3D/69553 爱弥斯 #3D "
                "https://www.iwara.tv/video/abc "
                "https://t.me/talu3D/69553 https://t.me/somechannel")
        self.assertEqual(tme.extract_tme_post_urls(text),
                         ["https://t.me/talu3D/69553"])

    def test_two_posts_kept(self):
        text = "https://t.me/aaachan/1 和 https://t.me/c/123456/789"
        self.assertEqual(len(tme.extract_tme_post_urls(text)), 2)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class HandleTest(unittest.TestCase):
    def _fetcher(self, by_id, window=None):
        async def f(ref, ids):
            if isinstance(ids, int):
                return [by_id.get(ids)]
            return [window.get(i) if window else by_id.get(i) for i in ids]
        return f

    def test_single_media(self):
        msg = FakeMsg(9862, title="某频道")
        enqueued = []

        async def enqueue(m):
            enqueued.append(m.id)

        out = run(tme.handle_tme_url(
            "https://t.me/demochan/9862",
            fetcher=self._fetcher({9862: msg}), enqueue=enqueue))
        self.assertIn("1 个文件", out)
        self.assertIn("某频道", out)
        self.assertEqual(enqueued, [9862])

    def test_album_expansion(self):
        gid = 777
        target = FakeMsg(101, grouped_id=gid)
        same = [FakeMsg(100, grouped_id=gid), target, FakeMsg(102, grouped_id=gid)]
        other = FakeMsg(99, grouped_id=999)
        by_id = {m.id: m for m in same + [other]}
        # 窗口拉取里混入 None 与异组成员，都应被过滤
        window = dict(by_id)
        window[103] = None
        order = []

        async def enqueue(m):
            order.append(m.id)

        out = run(tme.handle_tme_url(
            "https://t.me/demochan/101", fetcher=self._fetcher(by_id, window),
            enqueue=enqueue))
        self.assertIn("相册 3 个文件", out)
        self.assertEqual(order, [100, 101, 102])   # 按 id 升序整组入队

    def test_text_only_post(self):
        msg = FakeMsg(5, media=False, title="纯文本频道")
        out = run(tme.handle_tme_url(
            "https://t.me/demochan/5",
            fetcher=self._fetcher({5: msg}), enqueue=self._noop()))
        self.assertIn("无媒体", out)

    def test_fetch_error(self):
        async def bad(ref, ids):
            raise RuntimeError("no access")

        out = run(tme.handle_tme_url("https://t.me/demochan/1",
                                     fetcher=bad, enqueue=self._noop()))
        self.assertIn("拉取帖子失败", out)

    def test_post_missing(self):
        out = run(tme.handle_tme_url(
            "https://t.me/demochan/9",
            fetcher=self._fetcher({}), enqueue=self._noop()))
        self.assertIn("帖子不存在", out)

    @staticmethod
    def _noop():
        async def enqueue(m):
            pass
        return enqueue


class BackfillTest(unittest.TestCase):
    def test_processes_tme_rows_and_marks_done(self):
        rows = [
            {"id": "11", "url": "https://t.me/aaa/1"},
            {"id": "12", "url": "https://t.me/c/123/4"},
            {"id": "13", "url": "https://mega.nz/file/xyz"},   # 非 t.me 不动
        ]
        done = []

        async def fake_handle(url):
            return "📥 t.me 帖已入队：x · 1 个文件"

        with mock.patch("tg_userbot.runtime_db.list_manual_links",
                        return_value=rows), \
             mock.patch("tg_userbot.runtime_db.manual_link_done",
                        side_effect=lambda i: done.append(i)), \
             mock.patch.object(tme, "handle_tme_url", fake_handle):
            out = run(tme.backfill_pending())
        self.assertIn("2 条", out)
        self.assertEqual(sorted(done), [11, 12])   # 非 t.me 条目未销账

    def test_no_rows(self):
        with mock.patch("tg_userbot.runtime_db.list_manual_links",
                        return_value=[]):
            out = run(tme.backfill_pending())
        self.assertIn("没有待处理", out)


class EnqueueCoordTest(unittest.TestCase):
    """频道原帖入队坐标：chat_id 必须透传频道 id（Worker 回拉媒体的坐标，
    缺省收藏夹坐标只会「原消息已被删除」——2026-11-03 真机踩坑回归钉）。"""

    def _run(self, msg, chat_id):
        from tg_userbot import app, state
        old_my = getattr(state, "MY_ID", None)
        state.MY_ID = 555
        self.addCleanup(setattr, state, "MY_ID", old_my)
        captured = {}

        async def noop(*a, **k):
            return None

        async def fake_enqueue_media(message, chat_id, folder, **k):
            captured["chat_id"] = chat_id

        with mock.patch.object(app, "_maybe_album_caption", noop), \
             mock.patch.object(app, "resolve_origin_snapshot", noop), \
             mock.patch.object(app, "enqueue_media", fake_enqueue_media):
            run(app._enqueue_me(msg, no_label_wait=True, chat_id=chat_id))
        return captured["chat_id"]

    def test_channel_id_passthrough(self):
        msg = FakeMsg(9862)
        msg.chat_id = -1001234567890
        self.assertEqual(self._run(msg, msg.chat_id), -1001234567890)

    def test_default_is_my_id(self):
        self.assertEqual(self._run(FakeMsg(1), None), 555)


if __name__ == "__main__":
    unittest.main()
