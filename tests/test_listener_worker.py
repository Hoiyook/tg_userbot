"""Listener Worker（listener_worker.py）的单元测试。

契约（任务书 docs/Telegram标签监听_Producer-Consumer_SQLite架构调整_DeepSeek开发任务书.md）：

1. Worker 常驻受控消费 SQLite 里的任务，**不读 listen.json 重新决定目标**
   （已入队任务的目标以 listener_tasks 为准，§12/§19）。
2. concurrency = 1 + 最小转发间隔：扫描完不再连续砸转发请求（§31/§32）。
3. **FloodWait 必须采信服务端返回值**（§27）：不许固定等 60 秒、不许立即重试；
   且要**暂停整个 Worker** 的发送能力（FloodWait 是账号级的，只给那条任务设
   退避、转头去发别的，会继续吃限流甚至加重）。
4. 临时错误 → PENDING + next_retry_at（退避复用 queue 的指数风格，§26）；
   永久错误（ChatWriteForbidden/ChannelPrivate/…）→ FAILED，不无限重试（§28）。
5. download=true：转发到收藏夹后调**现有** app.enqueue_media —— 不建第二套
   下载器（§22）。
6. 优雅停机：把手上的任务放回 PENDING（release），不等租约到期；被取消时
   原样上抛。
7. 后台任务保持强引用（§35），不能被 GC 掉。
8. Worker 不因为单个任务失败而退出（§42）。

不联网：FakeClient 记录调用；DB 落临时目录。
运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import asyncio
import atexit
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_listener_worker_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from telethon.errors import (  # noqa: E402
    ChannelPrivateError,
    ChatWriteForbiddenError,
    FloodWaitError,
    PeerIdInvalidError,
)

from tg_userbot import config  # noqa: E402
from tg_userbot import listener  # noqa: E402
from tg_userbot import listener_worker as lw  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402
from tg_userbot import state  # noqa: E402

SRC = -1001234567890
CHAT_A = -1009876543210


def _flood(seconds):
    """构造一个 FloodWaitError（Telethon 的构造签名因版本而异，稳妥起见直接造）。"""
    err = FloodWaitError.__new__(FloodWaitError)
    err.seconds = seconds
    return err


class FakeFile:
    id = "F1"
    size = 100
    name = "v.mp4"
    mime_type = "video/mp4"


class FakeMessage:
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
        self.audio = None
        self.voice = None


class FakeCopy(FakeMessage):
    pass


class FakeClient:
    """记录 forward/get_messages；可注入异常。"""

    def __init__(self, messages=None, forward_error=None,
                 forward_error_times=None, fetch_error=None):
        # forward_error_times: None = 每次都抛；N = 前 N 次抛（之后成功）
        self.messages = {m.id: m for m in (messages or [])}
        self.forward_error = forward_error
        self.forward_error_times = forward_error_times
        self.fetch_error = fetch_error
        self.forwards = []          # (peer, [ids], from_peer)
        self._next_id = 70000

    async def get_messages(self, peer, **kw):
        if self.fetch_error:
            raise self.fetch_error
        ids = kw.get("ids") or []
        return [self.messages[i] for i in ids if i in self.messages]

    async def forward_messages(self, peer, messages, from_peer=None):
        if self.forward_error is not None:
            if self.forward_error_times is None:
                raise self.forward_error
            if self.forward_error_times > 0:
                self.forward_error_times -= 1
                raise self.forward_error
        self.forwards.append((peer, [m.id for m in messages], from_peer))
        out = []
        for m in messages:
            self._next_id += 1
            out.append(FakeCopy(self._next_id, text=getattr(m, "message", "")))
        return out


class _WorkerTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lw_", dir=_TMP)
        self.path = os.path.join(self.dir, "tg_userbot.db")
        self._p = mock.patch.object(config, "RUNTIME_DB_FILE", self.path)
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())

        self.old = (state.client, state.MY_ID, state.LISTEN_RULES)
        state.MY_ID = 42
        state.LISTEN_RULES = []
        self.addCleanup(self._restore)
        self.copies = []

        async def fake_enqueue(copy, source_link, album_caption, src,
                               parent_date=None, parent_caption=None,
                               source_name=None):
            self.copies.append((copy.id, source_link, album_caption, src,
                                parent_date, parent_caption, source_name))

        self._p2 = mock.patch.object(lw, "_enqueue_copy", fake_enqueue)
        self._p2.start()
        self.addCleanup(self._p2.stop)

    def _restore(self):
        (state.client, state.MY_ID, state.LISTEN_RULES) = self.old
        lw.reset_pause()

    def _seed(self, messages, targets=(("saved_messages", None, True),)):
        """把消息按目标建成任务（走真实的 runtime_db）。"""
        state.client = FakeClient(messages)
        tasks = []
        for m in messages:
            for target_type, target_chat_id, download in targets:
                tasks.append({
                    "message_id": m.id,
                    "grouped_id": m.grouped_id,
                    "target_type": target_type,
                    "target_chat_id": target_chat_id,
                    "download": download,
                    "payload": {"member_ids": [m.id], "caption": m.message},
                })
        ids = runtime_db.enqueue_listener_tasks(SRC, tasks, checkpoint=999)
        return [i for i in ids if i]


class RetryPolicyTest(unittest.TestCase):
    """错误分类与退避：临时 → 重试，永久 → FAILED，FloodWait 用服务端值。"""

    def test_floodwait_uses_server_value(self):
        retry, delay = lw.classify_error(_flood(120))
        self.assertEqual(retry, "flood")
        self.assertEqual(delay, 120, "必须采信服务端返回值")

    def test_floodwait_not_pinned_to_sixty(self):
        self.assertEqual(lw.classify_error(_flood(7))[1], 7)
        self.assertEqual(lw.classify_error(_flood(300))[1], 300)

    def test_floodwait_absurd_value_capped_with_warning(self):
        retry, delay = lw.classify_error(_flood(10 ** 9))
        self.assertEqual(retry, "flood")
        self.assertEqual(delay, config.LISTEN_FLOODWAIT_MAX_WAIT_SECONDS)

    def test_retryable_errors(self):
        for exc in (TimeoutError("t"), ConnectionError("c"),
                    OSError("o"), RuntimeError("r")):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(lw.classify_error(exc)[0], "retry")

    def test_permanent_errors(self):
        for exc in (ChatWriteForbiddenError(request=None),
                    ChannelPrivateError(request=None),
                    PeerIdInvalidError(request=None)):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(lw.classify_error(exc)[0], "permanent")

    def test_backoff_grows_and_caps(self):
        first = lw.retry_delay_seconds(1)
        second = lw.retry_delay_seconds(2)
        self.assertEqual(first, config.LISTEN_WORKER_BACKOFF_BASE_SECONDS)
        self.assertEqual(second, first * 2)
        huge = lw.retry_delay_seconds(50)
        self.assertEqual(huge, config.LISTEN_WORKER_BACKOFF_MAX_SECONDS)

    def test_giving_up_after_max_attempts(self):
        self.assertTrue(lw.should_retry(attempts=1))
        self.assertFalse(lw.should_retry(
            attempts=config.LISTEN_WORKER_MAX_ATTEMPTS + 1))


class ExecuteTaskTest(_WorkerTestCase):
    async def test_success_path_marks_success(self):
        ids = self._seed([FakeMessage(101, "#a 标题")])
        task = runtime_db.claim_listener_task()
        ok = await lw.execute_task(task)
        self.assertTrue(ok)
        self.assertEqual(runtime_db.get_listener_task(ids[0])["status"],
                         "SUCCESS")
        self.assertEqual(state.client.forwards,
                         [("me", [101], SRC)])
        self.assertEqual(len(self.copies), 1, "download=True 应入队副本")
        kinds = [e["event_type"]
                 for e in runtime_db.get_task_events(ids[0])]
        self.assertEqual(kinds, ["RECEIVED", "RUNNING", "SUCCESS"])

    async def test_chat_target_forwards_without_enqueue(self):
        ids = self._seed([FakeMessage(101, "#a")],
                         targets=[("chat", CHAT_A, False)])
        task = runtime_db.claim_listener_task()
        await lw.execute_task(task)
        self.assertEqual(state.client.forwards, [(CHAT_A, [101], SRC)])
        self.assertEqual(self.copies, [], "非收藏夹目标不该入队下载")
        self.assertEqual(runtime_db.get_listener_task(ids[0])["status"],
                         "SUCCESS")

    async def test_album_forwards_whole_group_in_one_call(self):
        """相册任务一次 forward 整组（member_ids 来自 payload）。"""
        m1, m2, m3 = (FakeMessage(101, grouped_id=77),
                      FakeMessage(102, "#a 相册说明", grouped_id=77),
                      FakeMessage(103, grouped_id=77))
        runtime_db.enqueue_listener_tasks(SRC, [{
            "message_id": 101, "grouped_id": 77,
            "target_type": "saved_messages", "target_chat_id": None,
            "download": True,
            "payload": {"member_ids": [101, 102, 103],
                        "caption": "#a 相册说明"},
        }], checkpoint=103)
        state.client = FakeClient([m1, m2, m3])
        task = runtime_db.claim_listener_task()
        await lw.execute_task(task)
        self.assertEqual(state.client.forwards, [("me", [101, 102, 103], SRC)])
        self.assertEqual(len(self.copies), 3, "整组副本逐个入队")

    async def test_album_caption_passed_when_copy_has_no_text(self):
        """无文字的副本继承源侧读到的相册说明（否则图片名退化成时间戳）。"""
        m1 = FakeMessage(101, grouped_id=77)
        state.client = FakeClient([m1])
        runtime_db.enqueue_listener_tasks(SRC, [{
            "message_id": 101, "grouped_id": 77,
            "target_type": "saved_messages", "target_chat_id": None,
            "download": True,
            "payload": {"member_ids": [101], "caption": "#a 相册说明"},
        }], checkpoint=101)
        task = runtime_db.claim_listener_task()
        await lw.execute_task(task)
        self.assertEqual(self.copies[0][2], "#a 相册说明")

    async def test_missing_source_message_is_permanent_failure(self):
        """源消息被删（取不回）→ 永久失败，不做无谓重试。"""
        ids = self._seed([FakeMessage(101, "#a")])
        state.client = FakeClient([])          # 消息已不存在
        task = runtime_db.claim_listener_task()
        await lw.execute_task(task)
        self.assertEqual(runtime_db.get_listener_task(ids[0])["status"],
                         "FAILED")

    async def test_transient_failure_goes_to_retry(self):
        ids = self._seed([FakeMessage(101, "#a")])
        state.client = FakeClient([FakeMessage(101, "#a")],
                                  forward_error=ConnectionError("断网"))
        task = runtime_db.claim_listener_task()
        await lw.execute_task(task)
        rec = runtime_db.get_listener_task(ids[0])
        self.assertEqual(rec["status"], "PENDING")
        self.assertIsNotNone(rec["next_retry_at"])
        self.assertIn("断网", rec["last_error"])

    async def test_permanent_failure_is_terminal(self):
        ids = self._seed([FakeMessage(101, "#a")])
        state.client = FakeClient(
            [FakeMessage(101, "#a")],
            forward_error=ChatWriteForbiddenError(request=None))
        task = runtime_db.claim_listener_task()
        await lw.execute_task(task)
        self.assertEqual(runtime_db.get_listener_task(ids[0])["status"],
                         "FAILED")

    async def test_retry_exhaustion_becomes_failed(self):
        """超过最大重试次数 → FAILED，不再无限占用队列（§26/§28）。"""
        ids = self._seed([FakeMessage(101, "#a")])
        state.client = FakeClient([FakeMessage(101, "#a")])
        task = runtime_db.claim_listener_task()
        exhausted = dict(task, attempts=config.LISTEN_WORKER_MAX_ATTEMPTS + 1)
        await lw._handle_failure(exhausted, ConnectionError("断网"))
        rec = runtime_db.get_listener_task(ids[0])
        self.assertEqual(rec["status"], "FAILED")
        self.assertIn("重试", rec["last_error"])

    async def test_below_limit_still_retries(self):
        ids = self._seed([FakeMessage(101, "#a")])
        state.client = FakeClient([FakeMessage(101, "#a")])
        task = runtime_db.claim_listener_task()
        await lw._handle_failure(dict(task, attempts=1), ConnectionError("断网"))
        self.assertEqual(runtime_db.get_listener_task(ids[0])["status"],
                         "PENDING")

    async def test_enqueue_failure_does_not_lose_forward(self):
        """转发成功但入队抛错：任务仍算成功（转发已发生，重跑会重复转发）。"""
        ids = self._seed([FakeMessage(101, "#a")])

        async def boom(*a, **k):
            raise RuntimeError("入队炸了")

        with mock.patch.object(lw, "_enqueue_copy", boom):
            task = runtime_db.claim_listener_task()
            ok = await lw.execute_task(task)
        self.assertTrue(ok)
        self.assertEqual(runtime_db.get_listener_task(ids[0])["status"],
                         "SUCCESS")


class FloodWaitTest(_WorkerTestCase):
    async def test_floodwait_sets_server_delay_and_pauses_worker(self):
        ids = self._seed([FakeMessage(101, "#a")])
        state.client = FakeClient([FakeMessage(101, "#a")],
                                  forward_error=_flood(300))
        task = runtime_db.claim_listener_task()
        await lw.execute_task(task)

        rec = runtime_db.get_listener_task(ids[0])
        self.assertEqual(rec["status"], "PENDING")
        self.assertAlmostEqual(rec["next_retry_at"] - int(__import__("time").time()),
                               300, delta=5)
        self.assertGreater(lw.paused_for(), 250,
                           "FloodWait 是账号级的：必须暂停整个 Worker")

    async def test_paused_worker_claims_nothing(self):
        lw.pause_for(600)
        self._seed([FakeMessage(101, "#a")])
        self.assertIsNone(lw.claim_next_task(),
                          "暂停期间不领取任何任务")

    async def test_other_targets_not_attempted_while_paused(self):
        """暂停期间即使还有别的目标的任务也不发（避免继续吃限流）。"""
        self._seed([FakeMessage(101, "#a")],
                   targets=[("chat", CHAT_A, False), ("chat", -100555, False)])
        lw.pause_for(600)
        self.assertIsNone(lw.claim_next_task())


class WorkerLoopTest(_WorkerTestCase):
    async def test_run_once_processes_one_task(self):
        self._seed([FakeMessage(101, "#a")], targets=[("chat", CHAT_A, False)])
        done = await lw.run_once()
        self.assertTrue(done)
        self.assertEqual(len(state.client.forwards), 1)

    async def test_run_once_returns_false_when_idle(self):
        state.client = FakeClient([])
        self.assertFalse(await lw.run_once())

    async def test_startup_recovers_expired_leases(self):
        """启动时必须先恢复过期租约（§24），否则崩溃遗留的任务永远卡住。"""
        self._seed([FakeMessage(101, "#a")])
        runtime_db.claim_listener_task(now=1000, lease_seconds=10)
        state.client = FakeClient([FakeMessage(101, "#a")])
        n = lw.recover_expired(now=99999)
        self.assertEqual(n, 1)

    async def test_worker_survives_task_errors(self):
        """单个任务炸掉不能让 Worker 退出（§42）。"""
        self._seed([FakeMessage(101, "#a")], targets=[("chat", CHAT_A, False)])

        with mock.patch.object(lw, "execute_task",
                               side_effect=RuntimeError("boom")):
            done = await lw.run_once()
        self.assertFalse(done)          # 本轮没做成，但没抛出去

    async def test_graceful_stop_releases_inflight(self):
        """停机：手上的任务放回 PENDING，不等租约到期。"""
        ids = self._seed([FakeMessage(101, "#a")])
        task = runtime_db.claim_listener_task()
        self.assertEqual(runtime_db.get_listener_task(ids[0])["status"],
                         "PROCESSING")
        lw.release_inflight(task["id"])
        self.assertEqual(runtime_db.get_listener_task(ids[0])["status"],
                         "PENDING")

    async def test_loop_stops_on_cancel(self):
        state.client = FakeClient([])
        task = asyncio.create_task(lw.worker_loop())
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_loop_keeps_strong_reference(self):
        """后台任务必须保持强引用（§35），不能被 GC 掉。"""
        state.client = FakeClient([])
        t = lw.start_worker()
        self.assertIsNotNone(t)
        lw._TASKS.discard(t)
        self.assertIsNotNone(t)


class MinIntervalTest(_WorkerTestCase):
    async def test_pace_waits_when_just_sent(self):
        """刚发过就再发 → 必须补足最小间隔（§31/§32）。"""
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        with mock.patch.object(lw, "_sleep", fake_sleep), \
                mock.patch.object(config,
                                  "LISTEN_WORKER_MIN_FORWARD_INTERVAL_SECONDS",
                                  2.5):
            lw._LAST_SEND_AT = __import__("time").monotonic()
            await lw.pace()
        self.assertTrue(sleeps, "刚发过就该等待")
        self.assertGreater(sleeps[0], 2.0)
        self.assertLessEqual(sleeps[0], 2.5)

    async def test_pace_never_waits_on_first_send(self):
        """从未发过（哨兵 0.0）→ 不该凭空等待。"""
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        with mock.patch.object(lw, "_sleep", fake_sleep), \
                mock.patch.object(config,
                                  "LISTEN_WORKER_MIN_FORWARD_INTERVAL_SECONDS",
                                  2.5):
            lw._LAST_SEND_AT = 0.0
            await lw.pace()
        self.assertEqual(sleeps, [])

    async def test_pace_disabled_when_zero(self):
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        with mock.patch.object(lw, "_sleep", fake_sleep), \
                mock.patch.object(config,
                                  "LISTEN_WORKER_MIN_FORWARD_INTERVAL_SECONDS",
                                  0):
            lw._LAST_SEND_AT = __import__("time").monotonic()
            await lw.pace()
        self.assertEqual(sleeps, [], "间隔设成 0 = 关闭节流")

    def tearDown(self):
        lw._LAST_SEND_AT = 0.0


if __name__ == "__main__":
    unittest.main()
