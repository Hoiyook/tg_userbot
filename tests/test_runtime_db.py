"""Runtime DB（runtime_db.py）的单元测试。

契约（任务书 docs/Telegram标签监听_Producer-Consumer_SQLite架构调整_DeepSeek开发任务书.md）：

1. 数据分层：**配置 JSON / 业务状态 SQLite / 技术日志文件**。本模块是 Listener
   第一阶段唯一的 SQL 出口（§33），其他模块不许散落 SQL。
2. `PRAGMA journal_mode=WAL` + `foreign_keys=ON` + `busy_timeout`。WAL 探测必须
   **读回实际生效的模式**：Termux 的 /storage/emulated/0 是 FUSE 外部存储，
   WAL 依赖 mmap 共享内存可能不可用——那时回落 DELETE 并告警，不许假装生效。
3. `schema_meta` 记 `schema_version`，migration 幂等、可重复执行、中途失败可重跑。
4. **checkpoint 与任务的原子性（§13）**：INSERT tasks + INSERT events +
   UPDATE checkpoint 必须在**同一个事务**里；只有 COMMIT 成功才算数，失败
   ROLLBACK 且 checkpoint 不动（Case A/B）。Telegram API 绝不在事务内。
5. UNIQUE(source_chat_id, message_id, target_type, target_chat_id) 保证重复扫描
   不产生重复任务。相册的 message_id 是**单元锚点**（组内最小成员 id），
   整组成员另存 payload——这样 §8 的唯一索引与 §16 的整组转发同时成立。
6. Lease（§24）：claim 落 PROCESSING + lease_until；崩溃遗留的过期 PROCESSING
   可恢复成 PENDING 并记 LEASE_EXPIRED。
7. 重试状态机（§25/§26）：PROCESSING → PENDING + next_retry_at 用同一行，
   不新增一行；attempts 累计。永久错误 → FAILED，不无限重试。
8. SQLITE_BUSY/LOCKED 有限重试（§39），绝不因此崩掉主进程、绝不无限循环。

不联网；DB 落在进程级临时目录，退出时回收（含 -wal/-shm）。
运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import atexit
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_runtime_db_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402

SRC = -1001234567890
CHAT_A = -1009876543210
CHAT_B = -1005555555555


def _task(message_id=101, target_type="saved_messages", target_chat_id=None,
          grouped_id=None, download=False, payload=None):
    t = {
        "message_id": message_id,
        "grouped_id": grouped_id,
        "target_type": target_type,
        "target_chat_id": target_chat_id,
        "download": download,
    }
    if payload is not None:
        t["payload"] = payload
    return t


class ImportPurityTest(unittest.TestCase):
    """import 本模块绝不建连接。

    这不是洁癖：``chrome_agent`` 进程（独立进程）也 import 本包去用
    ``chrome_client`` 的纯函数；若 import 即开库，那个进程就成了 Runtime DB 的
    第二个写者，WAL 的单写者前提当场破坏、``BEGIN IMMEDIATE`` 会互相等锁。
    连接只能由 ``main()`` 显式 ``init_db()`` 建立。

    本类**不**在 setUp 里 init_db——它验证的正是「没人调用 init_db 时没连接」
    （前一个用例的连接由其 cleanup 关闭）。
    """

    def test_no_connection_without_init(self):
        runtime_db.close_db()
        self.assertFalse(runtime_db.has_connection(),
                         "没人调 init_db 就不该有连接")

    def test_module_has_no_connect_at_import_scope(self):
        """源码层面兜底：模块顶层不许出现 sqlite3.connect 调用。"""
        import inspect
        src = inspect.getsource(runtime_db)
        head = src.split("def init_db")[0]
        self.assertNotIn("sqlite3.connect", head,
                         "模块顶层不该建连接，只允许在 init_db 里")
        self.assertIsNone(runtime_db.journal_mode())


class _DbTestCase(unittest.TestCase):
    """每个用例一个全新的 DB 文件（含 -wal/-shm），用完即删。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rtdb_", dir=_TMP)
        self.path = os.path.join(self.dir, "tg_userbot.db")
        self._p = mock.patch.object(config, "RUNTIME_DB_FILE", self.path)
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db(), "init_db 应成功")

    def tearDown(self):
        runtime_db.close_db()
        shutil.rmtree(self.dir, ignore_errors=True)


# ============================================================
# Phase 1：连接 / schema / 迁移
# ============================================================
class InitDbTest(_DbTestCase):
    def test_creates_file_and_tables(self):
        self.assertTrue(os.path.exists(self.path))
        conn = runtime_db._conn()
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("schema_meta", "listener_checkpoints",
                      "listener_tasks", "task_events"):
            self.assertIn(table, names)

    def test_foreign_keys_on(self):
        self.assertEqual(
            runtime_db._conn().execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_journal_mode_reported_honestly(self):
        """读回实际生效的模式；本机 tmp 支持 WAL，就必须是 wal。"""
        mode = runtime_db._conn().execute(
            "PRAGMA journal_mode").fetchone()[0]
        self.assertIn(mode.lower(), ("wal", "delete"))
        self.assertEqual(mode.lower(), runtime_db.journal_mode())

    def test_synchronous_applied(self):
        got = runtime_db._conn().execute("PRAGMA synchronous").fetchone()[0]
        # 0=OFF 1=NORMAL 2=FULL 3=EXTRA
        expected = {"OFF": 0, "NORMAL": 1, "FULL": 2, "EXTRA": 3}[
            config.RUNTIME_DB_SYNCHRONOUS]
        self.assertEqual(got, expected)

    def test_busy_timeout_applied(self):
        got = runtime_db._conn().execute(
            "PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(got, config.RUNTIME_DB_BUSY_TIMEOUT_MS)

    def test_schema_version_recorded(self):
        self.assertEqual(runtime_db.get_schema_version(),
                         config.RUNTIME_DB_SCHEMA_VERSION)

    def test_migrate_is_idempotent(self):
        """重复执行不报错、不破坏数据、版本不变（§6）。"""
        runtime_db.set_listener_checkpoint(SRC, 500)
        for _ in range(3):
            self.assertEqual(runtime_db.migrate(),
                             config.RUNTIME_DB_SCHEMA_VERSION)
        self.assertEqual(runtime_db.get_listener_checkpoint(SRC), 500)

    def test_init_db_is_idempotent(self):
        self.assertTrue(runtime_db.init_db())
        self.assertTrue(runtime_db.init_db())
        self.assertEqual(runtime_db.get_schema_version(),
                         config.RUNTIME_DB_SCHEMA_VERSION)

    def test_close_clears_state(self):
        self.assertTrue(runtime_db.has_connection())
        runtime_db.close_db()
        self.assertFalse(runtime_db.has_connection())
        runtime_db.close_db()          # 幂等
        self.assertFalse(runtime_db.has_connection())

    def test_db_path_follows_config(self):
        self.assertEqual(runtime_db.db_path(), self.path)

    def test_schema_version_survives_reopen(self):
        runtime_db.set_listener_checkpoint(SRC, 42)
        runtime_db.close_db()
        runtime_db.init_db()
        self.assertEqual(runtime_db.get_schema_version(),
                         config.RUNTIME_DB_SCHEMA_VERSION)
        self.assertEqual(runtime_db.get_listener_checkpoint(SRC), 42)


class SqlErrorHandlingTest(_DbTestCase):
    """§39：SQLITE_BUSY/LOCKED 有限重试，不崩主进程、不无限循环。"""

    def test_busy_is_retried_then_succeeds(self):
        calls = []
        real = runtime_db._execute

        def flaky(conn, sql, params=()):
            calls.append(sql)
            if len(calls) <= 2:
                raise sqlite3.OperationalError("database is locked")
            return real(conn, sql, params)

        with mock.patch.object(runtime_db, "_execute", flaky), \
                mock.patch.object(config, "RUNTIME_DB_BUSY_RETRY_DELAY_SECONDS",
                                  0):
            self.assertTrue(runtime_db.set_listener_checkpoint(SRC, 1))
        self.assertGreaterEqual(len(calls), 3)

    def test_busy_gives_up_after_limit(self):
        def always_busy(conn, sql, params=()):
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(runtime_db, "_execute", always_busy), \
                mock.patch.object(runtime_db, "_retry_delay", lambda: None):
            with self.assertRaises(runtime_db.DbUnavailable):
                runtime_db.set_listener_checkpoint(SRC, 1)
        # 失败只影响本次调用，连接仍在、后续调用照常
        self.assertTrue(runtime_db.set_listener_checkpoint(SRC, 7))
        self.assertEqual(runtime_db.get_listener_checkpoint(SRC), 7)

    def test_non_busy_error_is_not_retried(self):
        """非 BUSY 类错误不重试，但仍统一收成 DbUnavailable（保留 __cause__）。"""
        calls = []

        def boom(conn, sql, params=()):
            calls.append(sql)
            raise sqlite3.OperationalError("no such table: nope")

        with mock.patch.object(runtime_db, "_execute", boom):
            with self.assertRaises(runtime_db.DbUnavailable) as ctx:
                runtime_db.set_listener_checkpoint(SRC, 1)
        self.assertEqual(len(calls), 1, "非 BUSY 类错误不重试")
        self.assertIsInstance(ctx.exception.__cause__,
                              sqlite3.OperationalError)


# ============================================================
# Phase 2：checkpoint
# ============================================================
class CheckpointTest(_DbTestCase):
    def test_missing_checkpoint_is_none(self):
        self.assertIsNone(runtime_db.get_listener_checkpoint(SRC))

    def test_set_then_get(self):
        self.assertTrue(runtime_db.set_listener_checkpoint(SRC, 500))
        self.assertEqual(runtime_db.get_listener_checkpoint(SRC), 500)

    def test_set_is_upsert(self):
        runtime_db.set_listener_checkpoint(SRC, 500)
        runtime_db.set_listener_checkpoint(SRC, 900)
        self.assertEqual(runtime_db.get_listener_checkpoint(SRC), 900)

    def test_checkpoint_is_monotonic_by_caller_contract(self):
        """模块本身允许写回较小值（回滚场景），单调性由 Scanner 保证。"""
        runtime_db.set_listener_checkpoint(SRC, 500)
        runtime_db.set_listener_checkpoint(SRC, 400)
        self.assertEqual(runtime_db.get_listener_checkpoint(SRC), 400)

    def test_per_chat_isolated(self):
        runtime_db.set_listener_checkpoint(SRC, 500)
        runtime_db.set_listener_checkpoint(CHAT_A, 700)
        self.assertEqual(runtime_db.get_listener_checkpoint(SRC), 500)
        self.assertEqual(runtime_db.get_listener_checkpoint(CHAT_A), 700)

    def test_updated_at_is_written(self):
        runtime_db.set_listener_checkpoint(SRC, 500, now=1_700_000_000)
        row = runtime_db._conn().execute(
            "SELECT updated_at FROM listener_checkpoints "
            "WHERE source_chat_id=?", (SRC,)).fetchone()
        self.assertEqual(row[0], 1_700_000_000)


# ============================================================
# Phase 2：任务入队 / 唯一约束 / 事务原子性
# ============================================================
class EnqueueTasksTest(_DbTestCase):
    def test_insert_returns_ids(self):
        ids = runtime_db.enqueue_listener_tasks(SRC, [
            _task(101, "saved_messages", None, download=True),
            _task(101, "chat", CHAT_A),
        ], checkpoint=101)
        self.assertEqual(len(ids), 2)
        self.assertTrue(all(isinstance(i, int) for i in ids))
        self.assertEqual(runtime_db.get_listener_checkpoint(SRC), 101)

    def test_edge_creates_events_and_checkpoint_atomically(self):
        ids = runtime_db.enqueue_listener_tasks(SRC, [
            _task(101, "saved_messages", None, download=True),
        ], checkpoint=101)
        events = runtime_db.get_task_events(ids[0])
        self.assertEqual([e["event_type"] for e in events], ["RECEIVED"])
        self.assertEqual(runtime_db.get_listener_checkpoint(SRC), 101)

    def test_duplicate_insert_is_ignored(self):
        """同一 source+message+target 重复建任务 → 被 UNIQUE 吃掉，不报错。"""
        first = runtime_db.enqueue_listener_tasks(
            SRC, [_task(101, "chat", CHAT_A)], checkpoint=101)
        second = runtime_db.enqueue_listener_tasks(
            SRC, [_task(101, "chat", CHAT_A)], checkpoint=101)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [None], "重复任务应返回 None 而不是新 id")
        self.assertEqual(len(runtime_db.list_listener_tasks()), 1)

    def test_duplicate_saved_messages_task_is_ignored(self):
        """收藏夹目标的 target_chat_id 是 NULL，而 **SQLite 的 UNIQUE 里 NULL
        彼此不相等**——按任务书字面建索引（(…, target_chat_id)）这条唯一约束
        对收藏夹目标**完全失效**，同一消息能插出两条收藏夹任务、转发两次。
        索引必须建在 COALESCE(target_chat_id, 0) 上。"""
        first = runtime_db.enqueue_listener_tasks(
            SRC, [_task(101, "saved_messages", None)], checkpoint=101)
        second = runtime_db.enqueue_listener_tasks(
            SRC, [_task(101, "saved_messages", None)], checkpoint=101)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [None])
        self.assertEqual(len(runtime_db.list_listener_tasks()), 1)

    def test_duplicate_detection_survives_reopen(self):
        """去重靠数据库约束，不靠内存——重启后仍拦得住。"""
        runtime_db.enqueue_listener_tasks(
            SRC, [_task(101, "saved_messages", None)], checkpoint=101)
        runtime_db.close_db()
        runtime_db.init_db()
        ids = runtime_db.enqueue_listener_tasks(
            SRC, [_task(101, "saved_messages", None)], checkpoint=101)
        self.assertEqual(ids, [None])
        self.assertEqual(len(runtime_db.list_listener_tasks()), 1)

    def test_duplicate_does_not_block_other_targets(self):
        runtime_db.enqueue_listener_tasks(
            SRC, [_task(101, "chat", CHAT_A)], checkpoint=101)
        ids = runtime_db.enqueue_listener_tasks(SRC, [
            _task(101, "chat", CHAT_A),      # 重复
            _task(101, "chat", CHAT_B),      # 新目标
        ], checkpoint=101)
        self.assertEqual(ids[0], None)
        self.assertIsInstance(ids[1], int)
        self.assertEqual(len(runtime_db.list_listener_tasks()), 2)

    def test_same_message_different_source_is_allowed(self):
        runtime_db.enqueue_listener_tasks(
            SRC, [_task(101, "chat", CHAT_A)], checkpoint=101)
        ids = runtime_db.enqueue_listener_tasks(
            CHAT_A, [_task(101, "chat", CHAT_A)], checkpoint=101)
        self.assertIsInstance(ids[0], int)

    def test_album_anchor_and_members(self):
        """相册：message_id 是单元锚点，整组成员在 payload 里。"""
        ids = runtime_db.enqueue_listener_tasks(SRC, [
            _task(101, "saved_messages", None, grouped_id=77, download=True,
                  payload={"member_ids": [101, 102, 103],
                           "caption": "#a 相册说明"}),
        ], checkpoint=103)
        rec = runtime_db.get_listener_task(ids[0])
        self.assertEqual(rec["message_id"], 101)
        self.assertEqual(rec["grouped_id"], 77)
        self.assertEqual(rec["payload"]["member_ids"], [101, 102, 103])
        self.assertEqual(rec["payload"]["caption"], "#a 相册说明")
        self.assertEqual(runtime_db.get_listener_checkpoint(SRC), 103)

    def test_empty_task_list_still_advances_checkpoint(self):
        """没有匹配消息时也要推进 checkpoint（这些消息已经检查过了）。"""
        self.assertEqual(runtime_db.enqueue_listener_tasks(
            SRC, [], checkpoint=105), [])
        self.assertEqual(runtime_db.get_listener_checkpoint(SRC), 105)

    def test_status_defaults_to_pending(self):
        ids = runtime_db.enqueue_listener_tasks(
            SRC, [_task(101)], checkpoint=101)
        rec = runtime_db.get_listener_task(ids[0])
        self.assertEqual(rec["status"], "PENDING")
        self.assertEqual(rec["attempts"], 0)
        self.assertIsNone(rec["next_retry_at"])
        self.assertIsNone(rec["started_at"])
        self.assertIsNone(rec["lease_until"])
        self.assertIsNone(rec["last_error"])

    def test_checkpoint_failure_rolls_back_tasks(self):
        """Case A：INSERT tasks 成功但 UPDATE checkpoint 失败 → 整事务回滚。

        checkpoint 与任务必须同生共死：漏了任务=丢消息，漏了回滚=重复任务。
        """
        with mock.patch.object(runtime_db, "_write_checkpoint",
                               side_effect=sqlite3.OperationalError("boom")):
            with self.assertRaises(runtime_db.DbUnavailable):
                runtime_db.enqueue_listener_tasks(
                    SRC, [_task(101, "chat", CHAT_A)], checkpoint=101)
        self.assertEqual(len(runtime_db.list_listener_tasks()), 0,
                         "任务必须随事务一起回滚")
        self.assertIsNone(runtime_db.get_listener_checkpoint(SRC),
                          "checkpoint 不能推进")

    def test_rollback_leaves_db_usable(self):
        with mock.patch.object(runtime_db, "_write_checkpoint",
                               side_effect=sqlite3.OperationalError("boom")):
            with self.assertRaises(runtime_db.DbUnavailable):
                runtime_db.enqueue_listener_tasks(
                    SRC, [_task(101, "chat", CHAT_A)], checkpoint=101)
        # 回滚后重扫：同一批任务能正常建出来（Case B）
        ids = runtime_db.enqueue_listener_tasks(
            SRC, [_task(101, "chat", CHAT_A)], checkpoint=101)
        self.assertIsInstance(ids[0], int)
        self.assertEqual(runtime_db.get_listener_checkpoint(SRC), 101)


# ============================================================
# Phase 2：claim / complete / retry / fail / cancel / lease
# ============================================================
class ClaimTest(_DbTestCase):
    def _seed(self, n=1, **kw):
        return runtime_db.enqueue_listener_tasks(
            SRC, [_task(100 + i, "chat", CHAT_A, **kw) for i in range(n)],
            checkpoint=100 + n)

    def test_claim_marks_processing_with_lease(self):
        self._seed()
        rec = runtime_db.claim_listener_task(now=1000, lease_seconds=600)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "PROCESSING")
        self.assertEqual(rec["started_at"], 1000)
        self.assertEqual(rec["lease_until"], 1600)
        self.assertEqual(rec["attempts"], 1)

    def test_claim_records_running_event(self):
        self._seed()
        rec = runtime_db.claim_listener_task(now=1000)
        kinds = [e["event_type"] for e in runtime_db.get_task_events(rec["id"])]
        self.assertEqual(kinds, ["RECEIVED", "RUNNING"])

    def test_claim_returns_none_when_nothing_pending(self):
        self.assertIsNone(runtime_db.claim_listener_task(now=1000))

    def test_claimed_task_is_not_handed_out_twice(self):
        self._seed(1)
        first = runtime_db.claim_listener_task(now=1000, lease_seconds=600)
        second = runtime_db.claim_listener_task(now=1000, lease_seconds=600)
        self.assertIsNotNone(first)
        self.assertIsNone(second, "租约内不允许第二条再领同一任务")

    def test_claim_respects_next_retry_at(self):
        ids = self._seed(1)
        runtime_db.claim_listener_task(now=1000)
        runtime_db.retry_listener_task(ids[0], next_retry_at=2000)
        self.assertIsNone(runtime_db.claim_listener_task(now=1500),
                          "退避未到期不该被领走")
        rec = runtime_db.claim_listener_task(now=2500)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["attempts"], 2)

    def test_claim_is_fifo(self):
        ids = self._seed(3)
        first = runtime_db.claim_listener_task(now=1000)
        self.assertEqual(first["id"], ids[0])

    def test_success_path(self):
        ids = self._seed(1)
        runtime_db.claim_listener_task(now=1000)
        self.assertTrue(runtime_db.complete_listener_task(ids[0], now=1010))
        rec = runtime_db.get_listener_task(ids[0])
        self.assertEqual(rec["status"], "SUCCESS")
        self.assertEqual(rec["completed_at"], 1010)
        self.assertIsNone(rec["lease_until"])
        kinds = [e["event_type"] for e in runtime_db.get_task_events(ids[0])]
        self.assertEqual(kinds, ["RECEIVED", "RUNNING", "SUCCESS"])
        self.assertIsNone(runtime_db.claim_listener_task(now=2000))

    def test_retry_returns_to_pending_same_row(self):
        ids = self._seed(1)
        runtime_db.claim_listener_task(now=1000)
        self.assertTrue(runtime_db.retry_listener_task(
            ids[0], next_retry_at=1300, error="TimeoutError: x"))
        rec = runtime_db.get_listener_task(ids[0])
        self.assertEqual(rec["status"], "PENDING")
        self.assertEqual(rec["next_retry_at"], 1300)
        self.assertIsNone(rec["lease_until"], "重试要释放租约")
        self.assertIn("TimeoutError", rec["last_error"])
        self.assertEqual(len(runtime_db.list_listener_tasks()), 1,
                         "重试不新增行（§25）")

    def test_fail_is_terminal(self):
        ids = self._seed(1)
        runtime_db.claim_listener_task(now=1000)
        runtime_db.fail_listener_task(ids[0], error="ChatWriteForbidden")
        rec = runtime_db.get_listener_task(ids[0])
        self.assertEqual(rec["status"], "FAILED")
        self.assertEqual(rec["last_error"], "ChatWriteForbidden")
        self.assertIsNone(runtime_db.claim_listener_task(now=99999),
                          "FAILED 不再被领取")

    def test_cancel_from_pending_and_processing(self):
        ids = self._seed(2)
        self.assertTrue(runtime_db.cancel_listener_task(ids[0]))
        runtime_db.claim_listener_task(now=1000)
        self.assertTrue(runtime_db.cancel_listener_task(ids[1]))
        self.assertEqual(runtime_db.get_listener_task(ids[0])["status"],
                         "CANCELLED")
        self.assertEqual(runtime_db.get_listener_task(ids[1])["status"],
                         "CANCELLED")
        self.assertIsNone(runtime_db.claim_listener_task(now=2000))

    def test_cancel_unknown_task_is_false(self):
        self.assertFalse(runtime_db.cancel_listener_task(999999))

    def test_release_returns_task_to_pending(self):
        """优雅停机：把手上的任务放回 PENDING，不等租约到期。"""
        ids = self._seed(1)
        runtime_db.claim_listener_task(now=1000)
        self.assertTrue(runtime_db.release_listener_task(ids[0]))
        rec = runtime_db.get_listener_task(ids[0])
        self.assertEqual(rec["status"], "PENDING")
        self.assertIsNone(rec["lease_until"])
        self.assertIsNotNone(runtime_db.claim_listener_task(now=1100))


class LeaseRecoveryTest(_DbTestCase):
    def test_expired_processing_recovered(self):
        """Case C：claim 后进程被 kill → 租约到期 → 恢复 PENDING 可重跑。"""
        ids = runtime_db.enqueue_listener_tasks(
            SRC, [_task(101, "chat", CHAT_A)], checkpoint=101)
        runtime_db.claim_listener_task(now=1000, lease_seconds=600)

        self.assertEqual(
            runtime_db.recover_expired_listener_tasks(now=1500), 0,
            "租约未到期不该动它")
        self.assertEqual(
            runtime_db.recover_expired_listener_tasks(now=1700), 1)
        rec = runtime_db.get_listener_task(ids[0])
        self.assertEqual(rec["status"], "PENDING")
        self.assertIsNone(rec["lease_until"])
        self.assertEqual(rec["last_error"], "lease expired")
        kinds = [e["event_type"] for e in runtime_db.get_task_events(ids[0])]
        self.assertEqual(kinds, ["RECEIVED", "RUNNING", "LEASE_EXPIRED"])
        self.assertIsNotNone(runtime_db.claim_listener_task(now=1800))

    def test_recovery_is_idempotent(self):
        runtime_db.enqueue_listener_tasks(
            SRC, [_task(101, "chat", CHAT_A)], checkpoint=101)
        runtime_db.claim_listener_task(now=1000, lease_seconds=600)
        self.assertEqual(
            runtime_db.recover_expired_listener_tasks(now=1700), 1)
        self.assertEqual(
            runtime_db.recover_expired_listener_tasks(now=1700), 0)
        self.assertEqual(
            len(runtime_db.get_task_events(1)), 3, "不重复记事件")

    def test_does_not_touch_terminal_states(self):
        ids = runtime_db.enqueue_listener_tasks(
            SRC, [_task(101, "chat", CHAT_A), _task(102, "chat", CHAT_A)],
            checkpoint=102)
        runtime_db.claim_listener_task(now=1000, lease_seconds=1)
        runtime_db.complete_listener_task(ids[0], now=1000)
        self.assertEqual(
            runtime_db.recover_expired_listener_tasks(now=9999), 0)


class PendingCountTest(_DbTestCase):
    def test_counts_only_actionable(self):
        ids = runtime_db.enqueue_listener_tasks(SRC, [
            _task(101, "chat", CHAT_A), _task(102, "chat", CHAT_A),
            _task(103, "chat", CHAT_A)], checkpoint=103)
        self.assertEqual(runtime_db.count_pending_listener_tasks(), 3)
        runtime_db.claim_listener_task(now=1000)
        self.assertEqual(runtime_db.count_pending_listener_tasks(), 3,
                         "在途任务仍占队列额度")
        runtime_db.complete_listener_task(ids[0], now=1000)
        self.assertEqual(runtime_db.count_pending_listener_tasks(), 2)


# ============================================================
# 事件与统计
# ============================================================
class TaskEventTest(_DbTestCase):
    def test_event_payload_roundtrip(self):
        ids = runtime_db.enqueue_listener_tasks(
            SRC, [_task(101, "chat", CHAT_A)], checkpoint=101)
        runtime_db.record_task_event(ids[0], "CUSTOM", {"n": 1, "s": "x"})
        events = runtime_db.get_task_events(ids[0])
        self.assertEqual(events[-1]["payload"], {"n": 1, "s": "x"})

    def test_event_for_unknown_task_rejected(self):
        """外键约束：孤儿事件不该悄悄写进去（它会让统计对不上）。"""
        self.assertIsNone(runtime_db.record_task_event(999999, "SUCCESS"))
        self.assertEqual(len(runtime_db.get_task_events(999999)), 0)

    def test_foreign_key_enforced(self):
        conn = runtime_db._conn()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO task_events(task_id, event_type, created_at) "
                "VALUES(?,?,?)", (999999, "SUCCESS", 0))


class ListenerStatsTest(_DbTestCase):
    def test_stats_shape_and_counts(self):
        ids = runtime_db.enqueue_listener_tasks(SRC, [
            _task(101, "saved_messages", None, download=True),
            _task(101, "chat", CHAT_A),
            _task(102, "chat", CHAT_A),
        ], checkpoint=102)
        runtime_db.claim_listener_task(now=1000)
        runtime_db.complete_listener_task(ids[0], now=1010)
        runtime_db.claim_listener_task(now=1000)
        runtime_db.fail_listener_task(ids[1], error="boom", now=1010)

        s = runtime_db.get_listener_stats()
        self.assertEqual(s["pending"], 1)
        self.assertEqual(s["success"], 1)
        self.assertEqual(s["failed"], 1)
        self.assertEqual(s["total"], 3)

    def test_stats_window(self):
        runtime_db.enqueue_listener_tasks(
            SRC, [_task(101, "chat", CHAT_A)], checkpoint=101, now=1000)
        runtime_db.enqueue_listener_tasks(
            SRC, [_task(102, "chat", CHAT_A)], checkpoint=102, now=5000)
        self.assertEqual(runtime_db.get_listener_stats(since=4000)["total"], 1)


# ============================================================
# v3 迁移：checkpoint 加 chain、任务加 origin（白名单双通道，2026-09-13）
# ============================================================
class V3MigrationTest(unittest.TestCase):
    """v2 旧库 → v3：chain 列 + origin 列；旧行归 listen；幂等可重跑。"""

    def setUp(self):
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.path = os.path.join(_TMP, "v3_migrate.db")
        if os.path.exists(self.path):
            os.remove(self.path)
        conn = sqlite3.connect(self.path)
        conn.executescript("""
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE listener_checkpoints (
                source_chat_id INTEGER PRIMARY KEY,
                last_message_id INTEGER NOT NULL,
                updated_at INTEGER NOT NULL);
            CREATE TABLE listener_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                grouped_id INTEGER,
                target_type TEXT NOT NULL,
                target_chat_id INTEGER,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at INTEGER,
                created_at INTEGER NOT NULL,
                started_at INTEGER,
                completed_at INTEGER,
                lease_until INTEGER,
                last_error TEXT,
                download INTEGER NOT NULL DEFAULT 0,
                payload TEXT);
            INSERT INTO schema_meta VALUES('schema_version', '2');
            INSERT INTO listener_checkpoints VALUES(111, 500, 1000);
        """)
        conn.commit()
        conn.close()

    def test_v2_migrates_to_v3(self):
        self.assertTrue(runtime_db.init_db(self.path))
        self.assertEqual(runtime_db.get_schema_version(), 3)
        # 旧行归 listen 链；wl 链无游标
        self.assertEqual(
            runtime_db.get_listener_checkpoint(111, chain="listen"), 500)
        self.assertIsNone(runtime_db.get_listener_checkpoint(111, chain="wl"))
        # origin 列生效：新插入的行默认 listen
        ids = runtime_db.enqueue_listener_tasks(111, [_task(message_id=1)])
        self.assertTrue(ids[0])
        self.assertEqual(runtime_db.get_listener_task(ids[0])["origin"],
                         "listen")

    def test_migration_idempotent(self):
        self.assertTrue(runtime_db.init_db(self.path))
        self.assertTrue(runtime_db.init_db(self.path))
        self.assertEqual(runtime_db.get_schema_version(), 3)
        self.assertEqual(
            runtime_db.get_listener_checkpoint(111, chain="listen"), 500)


class ChainAndOriginTest(_DbTestCase):
    """chain 游标互相独立；claim listen 优先；stats 按 origin 过滤。

    继承 _DbTestCase（每用例独立 DB 文件）：claim/stats 用例会在任务表里
    留下 PENDING/PROCESSING 存量，共享库会互相污染计数。
    """

    def test_chain_isolation(self):
        runtime_db.set_listener_checkpoint(111, 500, chain="listen")
        runtime_db.set_listener_checkpoint(111, 900, chain="wl")
        self.assertEqual(
            runtime_db.get_listener_checkpoint(111, chain="listen"), 500)
        self.assertEqual(
            runtime_db.get_listener_checkpoint(111, chain="wl"), 900)
        # 回补只动 wl 游标，不波及 listen
        runtime_db.set_listener_checkpoint(111, 501, chain="listen")
        self.assertEqual(
            runtime_db.get_listener_checkpoint(111, chain="wl"), 900)

    def test_claim_prefers_listen_over_wl(self):
        ids_wl = runtime_db.enqueue_listener_tasks(
            SRC, [_task(message_id=1)], origin="wl")
        ids_listen = runtime_db.enqueue_listener_tasks(
            SRC, [_task(message_id=2)], origin="listen")
        task = runtime_db.claim_listener_task()
        self.assertEqual(task["id"], ids_listen[0],
                         "listen 任务优先，尽管 id 更大")
        runtime_db.complete_listener_task(task["id"])
        task2 = runtime_db.claim_listener_task()
        self.assertEqual(task2["id"], ids_wl[0])

    def test_stats_origin_filter(self):
        runtime_db.enqueue_listener_tasks(SRC, [_task(message_id=1)],
                                          origin="wl")
        runtime_db.enqueue_listener_tasks(SRC, [_task(message_id=2)])
        self.assertEqual(runtime_db.get_listener_stats(origin="wl")["total"], 1)
        self.assertEqual(
            runtime_db.get_listener_stats(origin="listen")["total"], 1)
        self.assertEqual(runtime_db.get_listener_stats()["total"], 2)

    def test_count_pending_for_chat(self):
        runtime_db.enqueue_listener_tasks(SRC, [_task(message_id=1)],
                                          origin="wl")
        runtime_db.enqueue_listener_tasks(CHAT_A, [_task(message_id=2)],
                                          origin="wl")
        runtime_db.enqueue_listener_tasks(SRC, [_task(message_id=3)])
        self.assertEqual(
            runtime_db.count_listener_tasks_for_chat(SRC, origin="wl"), 1)
        self.assertEqual(
            runtime_db.count_listener_tasks_for_chat(SRC), 2)
        self.assertEqual(
            runtime_db.count_listener_tasks_for_chat(CHAT_B, origin="wl"), 0)


if __name__ == "__main__":
    unittest.main()
