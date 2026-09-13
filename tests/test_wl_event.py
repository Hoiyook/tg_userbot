"""白名单事件生产者（app.record_whitelist_media）的单元测试。

契约（规格书 docs/plan/下载白名单双通道扫描制改造_设计规格.md §4）：
1. 单条媒体 → listener_tasks 一条任务（origin='wl'，目标收藏夹 + download），
   payload 带 source_name（目录兜底）与 caption。
2. 相册 → 攒批协调器把整组合成**一条**任务（member_ids 进 payload）。
3. dedup 前置：整组全命中 → 不建任务。
4. DB 不可用（DbUnavailable）→ 回退直下原消息（app.enqueue_media），媒体不丢。

不联网：FakeMsg 内存假件；DB 落临时目录（每用例独立）。
"""
import asyncio
import atexit
import os
import shutil
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_wl_event_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import app  # noqa: E402
from tg_userbot import config  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402
from tg_userbot import state  # noqa: E402
from tg_userbot import wl_scan  # noqa: E402
from tg_userbot.runtime_db import DbUnavailable  # noqa: E402

SRC = -1001234567890


class FakeFile:
    def __init__(self, fid="file-1", size=100, name="v.mp4"):
        self.id = fid
        self.size = size
        self.name = name
        self.mime_type = "video/mp4"


class FakeMsg:
    def __init__(self, mid, text="", grouped_id=None, chat_id=SRC):
        self.id = mid
        self.message = text
        self.grouped_id = grouped_id
        self.chat_id = chat_id
        self.date = None
        self.fwd_from = None
        self.file = FakeFile()
        self.document = object()
        self.video = object()
        self.photo = None


def _wl_tasks():
    return [t for t in runtime_db.list_listener_tasks()
            if t["origin"] == "wl"]


class RecordWhitelistMediaTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wlevent_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE",
            os.path.join(self.dir, "tg_userbot.db"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        p1 = mock.patch.object(state, "WHITELIST_CHATS", {SRC: "测试频道"})
        p1.start()
        self.addCleanup(p1.stop)
        p2 = mock.patch.object(wl_scan.dedup, "should_skip",
                               lambda keys: (False, ""))
        p2.start()
        self.addCleanup(p2.stop)
        # 事件生产者不做评论继承回源（单测不联网）：resolve 恒 None
        p3 = mock.patch.object(app, "resolve_origin_snapshot",
                               mock.AsyncMock(return_value=None))
        p3.start()
        self.addCleanup(p3.stop)

    async def test_single_media_becomes_task(self):
        msg = FakeMsg(10, text="标题")
        await app.record_whitelist_media(msg, SRC, "测试频道")
        tasks = _wl_tasks()
        self.assertEqual(len(tasks), 1)
        t = tasks[0]
        self.assertEqual(t["source_chat_id"], SRC)
        self.assertEqual(t["message_id"], 10)
        self.assertEqual(t["target_type"], "saved_messages")
        self.assertEqual(t["download"], 1)
        self.assertEqual(t["payload"]["source_name"], "测试频道")
        self.assertEqual(t["payload"]["caption"], "标题")

    async def test_dedup_hit_skips_task(self):
        msg = FakeMsg(11)
        p = mock.patch.object(wl_scan.dedup, "should_skip",
                              lambda keys: (True, ""))
        p.start()
        self.addCleanup(p.stop)
        await app.record_whitelist_media(msg, SRC, "测试频道")
        self.assertEqual(_wl_tasks(), [])

    async def test_db_unavailable_falls_back_direct_download(self):
        msg = FakeMsg(12)
        enqueued = []

        async def fake_enqueue(message, chat_id, source_override, **kw):
            enqueued.append((message.id, chat_id, source_override))

        def boom(*a, **kw):
            raise DbUnavailable("db down")
        with mock.patch.object(runtime_db, "enqueue_listener_tasks", boom), \
                mock.patch.object(app, "enqueue_media", fake_enqueue):
            await app.record_whitelist_media(msg, SRC, "测试频道")
        self.assertEqual(enqueued, [(12, SRC, "测试频道")])

    async def test_album_becomes_one_task(self):
        p1 = mock.patch.object(app, "_WL_ALBUM_DEBOUNCE_SECONDS", 0)
        p2 = mock.patch.object(app, "_WL_ALBUM_SETTLE_SECONDS", 0)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        members = [FakeMsg(20, grouped_id=77),
                   FakeMsg(21, grouped_id=77, text="相册说明")]
        fetched = mock.AsyncMock(return_value=members)
        p3 = mock.patch.object(app, "_fetch_album_members", fetched)
        p3.start()
        self.addCleanup(p3.stop)

        await app._record_album_member(members[0], SRC, "测试频道")
        await app._record_album_member(members[1], SRC, "测试频道")
        key = (SRC, 77)
        task = app._WL_ALBUM_TASKS[key]["task"]
        await task   # 等协调任务跑完

        tasks = _wl_tasks()
        self.assertEqual(len(tasks), 1)
        t = tasks[0]
        self.assertEqual(t["message_id"], 20)
        self.assertEqual(t["payload"]["member_ids"], [20, 21])
        self.assertEqual(t["payload"]["caption"], "相册说明")


if __name__ == "__main__":
    unittest.main()
