"""白名单扫描生产者（wl_scan.py）的单元测试。

契约（规格书 docs/plan/下载白名单双通道扫描制改造_设计规格.md §5/§8）：
1. 按 wl 游标（chain='wl'）分页扫描白名单聊天，命中「可下载媒体」即建任务
   （origin='wl'，目标=收藏夹 + download），任务与游标同事务推进。
2. 相册按 grouped_id 整组一个单元；**不跳过镜像帖**（白名单契约是一切媒体）。
3. dedup 前置：整组全部成员命中已下载/在途索引才跳过（游标照推）；单元内
   部分命中照建任务（下载侧 per-copy 拦截兜底）。
4. 背压：待执行 ≥ LISTEN_MAX_PENDING_TASKS 停扫、游标不动。
5. 分页上限：每轮最多 WHITELIST_SCAN_PAGES_PER_ROUND 页、页间睡。
6. 首启无游标 → 以当前最新 id 初始化（不扫历史）。
7. 死聊天降噪：连续失败只报第一次 ERROR。

不联网：FakeScanClient 记录调用；每个用例一个全新 DB（沿 test_runtime_db
的 _DbTestCase 隔离思路——claim/扫描用例都会在任务表里留存量，共享库互相
污染计数）。需要历史语义的用例显式播种 wl 游标（0 = 从头扫）。
"""
import asyncio
import atexit
import os
import shutil
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_wl_scan_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import dedup  # noqa: E402
from tg_userbot import listener  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402
from tg_userbot import state  # noqa: E402
from tg_userbot import wl_scan  # noqa: E402

SRC = -1001234567890
OTHER = -1005555555555


class FakeFile:
    def __init__(self, fid="file-1", size=100, name="v.mp4"):
        self.id = fid
        self.size = size
        self.name = name
        self.mime_type = "video/mp4"


class FakeMsg:
    def __init__(self, mid, text="", grouped_id=None, chat_id=SRC, name=None,
                 media=True):
        self.id = mid
        self.message = text
        self.grouped_id = grouped_id
        self.chat_id = chat_id
        self.date = None
        self.fwd_from = None
        self.file = FakeFile(name=name or f"f{mid}.mp4") if media else None
        self.document = object() if media else None
        self.video = object() if media else None
        self.photo = None


class FakeScanClient:
    """get_messages 内存实现：min_id 过滤 + reverse 语义，支持分页。"""

    def __init__(self, messages=()):
        self.store = {m.id: m for m in messages}
        self.calls = []

    async def get_messages(self, chat_id, limit=100, min_id=0,
                           reverse=False, ids=None, **kw):
        self.calls.append(dict(limit=limit, min_id=min_id, ids=ids))
        if ids:
            return [self.store[i] for i in ids if i in self.store]
        got = sorted((m for m in self.store.values()
                      if m.id > (min_id or 0)), key=lambda m: m.id)
        if reverse:
            return got[:limit]
        return list(reversed(got))[:limit]


def _nap_record(sleeps):
    async def nap(seconds):
        sleeps.append(seconds)
    return nap


class _FreshDb(unittest.IsolatedAsyncioTestCase):
    """每个用例一个全新的 DB 文件（含 -wal/-shm），用完即删。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wlscan_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE",
            os.path.join(self.dir, "tg_userbot.db"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)


class WlScanTest(_FreshDb):

    def setUp(self):
        super().setUp()
        self._patches = [
            mock.patch.object(state, "WHITELIST_CHATS", {SRC: "测试频道"}),
            mock.patch.object(state, "RUNTIME_DB_READY", True),
            mock.patch.object(dedup, "should_skip",
                              lambda keys: (False, "")),
            # 不让 FakeMsg（无 reply_to/fwd_from）走真实评论继承解析
            mock.patch.object(wl_scan, "resolve_origin_snapshot",
                              mock.AsyncMock(return_value=None)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = FakeScanClient()
        state.client = self.client
        self.addCleanup(setattr, state, "client", None)

    def _seed(self, msg_id=0):
        """播种 wl 游标：0 = 从头扫（无游标时扫描会初始化为最新、不扫历史）。"""
        runtime_db.set_listener_checkpoint(SRC, msg_id, chain="wl")

    # -- 基础：全媒体建任务 + 游标推进 -------------------------------------
    async def test_media_builds_task_and_advances_checkpoint(self):
        self._seed(0)
        self.client.store = {
            m.id: m for m in (FakeMsg(10, text="#a"), FakeMsg(11),
                              FakeMsg(12, text="纯文本", media=False))}
        r = await wl_scan.scan_wl_chat(SRC)
        self.assertEqual(r["created"], 2)      # 10、11 是媒体；12 纯文本跳过
        tasks = [t for t in runtime_db.list_listener_tasks()
                 if t["origin"] == "wl"]
        self.assertEqual(len(tasks), 2)
        t = tasks[0]
        self.assertEqual(t["target_type"], "saved_messages")
        self.assertEqual(t["download"], 1)
        self.assertEqual(
            runtime_db.get_listener_checkpoint(SRC, chain="wl"), 12)

    async def test_first_scan_initializes_checkpoint_no_history(self):
        # 无游标时初始化为当前最新（12），**不**建历史任务
        self.client.store = {m.id: m for m in (FakeMsg(10), FakeMsg(12))}
        r = await wl_scan.scan_wl_chat(SRC)
        self.assertEqual(r["created"], 0)
        self.assertEqual(
            runtime_db.get_listener_checkpoint(SRC, chain="wl"), 12)

    # -- 相册 --------------------------------------------------------------
    async def test_album_becomes_one_task_with_members(self):
        self._seed(0)
        album = [FakeMsg(20, grouped_id=77),
                 FakeMsg(21, grouped_id=77, text="相册说明"),
                 FakeMsg(22, grouped_id=77)]
        self.client.store = {m.id: m for m in album}
        r = await wl_scan.scan_wl_chat(SRC)
        self.assertEqual(r["created"], 1)
        t = [x for x in runtime_db.list_listener_tasks()
             if x["origin"] == "wl"][0]
        self.assertEqual(t["message_id"], 20)  # 锚点 = 组内最小 id
        self.assertEqual(t["payload"]["member_ids"], [20, 21, 22])
        self.assertEqual(t["payload"]["caption"], "相册说明")

    # -- dedup 前置 --------------------------------------------------------
    async def test_all_members_dedup_hit_skips_unit(self):
        self._seed(0)
        self.client.store = {m.id: m for m in (FakeMsg(30), FakeMsg(31))}
        with mock.patch.object(wl_scan.dedup, "should_skip",
                               lambda keys: (True, "")):
            r = await wl_scan.scan_wl_chat(SRC)
        self.assertEqual(r["created"], 0)
        # 游标照推（内容已下载过，刻意放行）
        self.assertEqual(
            runtime_db.get_listener_checkpoint(SRC, chain="wl"), 31)

    async def test_partial_dedup_hit_within_unit_still_builds(self):
        self._seed(0)
        album = [FakeMsg(30, grouped_id=88), FakeMsg(31, grouped_id=88)]
        self.client.store = {m.id: m for m in album}
        calls = {"n": 0}

        def flaky(keys):
            calls["n"] += 1
            return (calls["n"] == 1, "")   # 组内第一个成员命中、第二个不命中
        with mock.patch.object(wl_scan.dedup, "should_skip", flaky):
            r = await wl_scan.scan_wl_chat(SRC)
        # 部分命中不跳过：整组照常建一条任务（下载侧 per-copy 拦截兜底）
        self.assertEqual(r["created"], 1)
        t = [x for x in runtime_db.list_listener_tasks()
             if x["origin"] == "wl"][0]
        self.assertEqual(t["payload"]["member_ids"], [30, 31])

    # -- 背压与分页 --------------------------------------------------------
    async def test_backpressure_stops_scan_and_keeps_cursor(self):
        runtime_db.set_listener_checkpoint(SRC, 100, chain="wl")
        with mock.patch.object(
                runtime_db, "count_pending_listener_tasks",
                return_value=int(config.LISTEN_MAX_PENDING_TASKS)):
            r = await wl_scan.scan_wl_chat(SRC)
        self.assertTrue(r["capped"])
        self.assertEqual(r["scanned"], 0)
        # 游标原地不动（没入队的消息绝不能被跳过）
        self.assertEqual(
            runtime_db.get_listener_checkpoint(SRC, chain="wl"), 100)

    async def test_pagination_respects_pages_per_round(self):
        self._seed(0)
        msgs = [FakeMsg(i) for i in range(1, 451)]   # 450 条 = 3 页
        self.client.store = {m.id: m for m in msgs}
        sleeps = []
        with mock.patch.object(config, "WHITELIST_SCAN_PAGES_PER_ROUND", 2):
            r = await wl_scan.scan_wl_chat(SRC, nap=_nap_record(sleeps))
        self.assertEqual(r["scanned"], 400)          # 只扫 2 页
        self.assertEqual(
            runtime_db.get_listener_checkpoint(SRC, chain="wl"), 400)
        self.assertEqual(sleeps,
                         [config.WHITELIST_SCAN_PAGE_SLEEP_SECONDS])

    # -- 失败降噪 / 多聊天 -------------------------------------------------
    async def test_failure_marked_once(self):
        async def dead(chat_id, checkpoint):
            return None
        with mock.patch.object(listener, "fetch_new_messages", dead):
            await wl_scan.scan_wl_chat(SRC)
            self.assertIn(SRC, wl_scan._FAILING_CHATS)
        # 第二轮失败不再打 ERROR（降噪），成功后清除
        self.client.store = {m.id: m for m in (FakeMsg(50),)}
        await wl_scan.scan_wl_chat(SRC)
        self.assertNotIn(SRC, wl_scan._FAILING_CHATS)

    async def test_scan_all_skips_non_whitelist_and_reports(self):
        state.WHITELIST_CHATS.clear()   # setUp patch 的同一 dict
        r = await wl_scan.scan_all()
        self.assertTrue(r.get("empty_chats"))


class SinceCheckpointTest(_FreshDb):

    def setUp(self):
        super().setUp()
        p = mock.patch.object(state, "WHITELIST_CHATS", {SRC: "测试频道"})
        p.start()
        self.addCleanup(p.stop)

    async def test_since_rejects_unknown_chat(self):
        ok, msg = await wl_scan.since_checkpoint(None, "@nope", "100")
        self.assertFalse(ok)

    async def test_since_writes_wl_cursor(self):
        ok, msg = await wl_scan.since_checkpoint(None, str(SRC), "88000")
        self.assertTrue(ok)
        self.assertEqual(
            runtime_db.get_listener_checkpoint(SRC, chain="wl"), 88000)

    async def test_since_rejects_bad_msgid(self):
        ok, _ = await wl_scan.since_checkpoint(None, str(SRC), "abc")
        self.assertFalse(ok)
        ok, _ = await wl_scan.since_checkpoint(None, str(SRC), "0")
        self.assertFalse(ok)


class BuildTaskHelperTest(unittest.TestCase):

    def test_source_name_falls_back_to_chat_title(self):
        p = mock.patch.object(state, "WHITELIST_CHATS", {SRC: "测试频道"})
        p.start()
        self.addCleanup(p.stop)
        records = listener.build_saved_messages_task(SRC, [FakeMsg(5)], "")
        self.assertEqual(records[0]["payload"]["source_name"], "测试频道")


if __name__ == "__main__":
    unittest.main()
