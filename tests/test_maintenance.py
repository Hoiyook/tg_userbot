"""可靠性维护任务测试：R1 每日 DB 备份 / R2 事件流周期裁剪 / R4 retry_all 上限 / R5 回滚防护。

    .venv/bin/python -m unittest tests.test_maintenance -v
"""
import asyncio
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录（config import 期
# 有真实副作用，见 test_config 的基座说明）。
_TMP = tempfile.mkdtemp(prefix="tg_userbot_maint_test_")
import atexit  # noqa: E402
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import maintenance  # noqa: E402
from tg_userbot import queue as queue_mod  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402
from tg_userbot import state  # noqa: E402


class _DbBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="maint_", dir=_TMP)
        self.db_path = os.path.join(self.dir, "tg_userbot.db")
        self._p = mock.patch.object(config, "RUNTIME_DB_FILE", self.db_path)
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())


class BackupTest(_DbBase):
    """R1：在线备份 + 滚动保留。"""

    def test_backup_creates_copy_with_tables(self):
        out = os.path.join(self.dir, "bak")
        path = maintenance.backup_db(out)
        self.assertTrue(os.path.exists(path))
        # 备份副本可独立打开且含业务表
        conn = sqlite3.connect(path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertIn("download_events", tables)
        self.assertIn("manual_links", tables)

    def test_rolling_keep_7(self):
        out = os.path.join(self.dir, "bak")
        os.makedirs(out)
        for i in range(8):
            old = os.path.join(out, f"tg_userbot.{i}.db")
            open(old, "w").close()
        maintenance.backup_db(out, keep=7)
        remaining = sorted(os.listdir(out))
        self.assertLessEqual(len(remaining), 7)
        # 最旧的被清
        self.assertNotIn("tg_userbot.0.db", remaining)

    def test_backup_no_connection_needed(self):
        """备份用 sqlite .backup API 独立开源库，不依赖进程内连接。"""
        runtime_db.close_db()
        path = maintenance.backup_db(os.path.join(self.dir, "bak2"))
        self.assertTrue(os.path.exists(path))


class TrimPeriodicTest(_DbBase):
    """R2：事件流周期裁剪——maintenance 每日入口直写独立连接（线程安全）。"""

    def test_daily_maintenance_trims(self):
        import time as _t
        for i in range(12):
            runtime_db.download_event_insert("RUNNING", task_id="a" * 32,
                                             ts=int(_t.time()) + i)
        with mock.patch.object(maintenance, "EVENTS_TRIM_MAX", 4):
            maintenance.daily_maintenance()   # 含备份+裁剪，失败只告警
        self.assertEqual(runtime_db.download_events_count(), 4)


class RetryAllCapTest(unittest.IsolatedAsyncioTestCase):
    """2026-09-20 用户决策：取消次数上限——全部重放（over 恒 0）。"""

    def setUp(self):
        self._old = (state.QUEUE, state.QUEUE_LOCK, state.EXECUTING)
        state.QUEUE = {"tasks": [], "retry": []}
        state.QUEUE_LOCK = asyncio.Lock()
        state.EXECUTING = set()
        self.addCleanup(self._restore)

    def _restore(self):
        state.QUEUE, state.QUEUE_LOCK, state.EXECUTING = self._old

    async def test_retry_all_replays_all(self):
        """2026-09-20 用户决策：取消次数上限——全部重放（over 恒 0）。"""
        self._old = (state.QUEUE, state.QUEUE_LOCK, state.EXECUTING)
        state.QUEUE = {"tasks": [], "retry": [
            {"id": "ok1", "attempts": 2, "label": "正常"},
            {"id": "dead1", "attempts": 99, "label": "死任务"},
        ]}
        state.QUEUE_LOCK = asyncio.Lock()
        state.EXECUTING = set()
        self.addCleanup(self._restore)
        spawned = []
        with mock.patch.object(queue_mod, "spawn_execute",
                               side_effect=lambda r: spawned.append(r["id"])):
            n, over = queue_mod.retry_all()
        self.assertEqual(spawned, ["ok1", "dead1"])
        self.assertEqual((n, over), (2, 0))


class RollbackGuardTest(_DbBase):
    """R5：切回 sqlite 时 json 比 DB 新 → 警告不导入。"""

    def test_import_conflict_warns(self):
        import json as _json
        from tg_userbot import queue as q
        # 表里先有任务（DB 权威）
        runtime_db.queue_insert({"id": "a" * 32, "kind": "media",
                                 "label": "DB任务"}, "QUEUED", now=1000)
        state.RUNTIME_DB_READY = True
        self.addCleanup(setattr, state, "RUNTIME_DB_READY", False)
        queue_file = os.path.join(self.dir, "download_queue.json")
        with open(queue_file, "w", encoding="utf-8") as f:
            _json.dump({"tasks": [{"id": "b" * 32, "kind": "media",
                                   "label": "json任务"}], "retry": []}, f)
        with mock.patch.object(config, "QUEUE_STORE", "sqlite"), \
                mock.patch.object(config, "QUEUE_FILE", queue_file), \
                mock.patch.object(q, "QUEUE_FILE", queue_file):
            loaded = q.load_queue_any()
        # DB 任务在，json 任务不混入（已归档）
        ids = {r["id"] for r in loaded["tasks"]}
        self.assertIn("a" * 32, ids)
        self.assertNotIn("b" * 32, ids)
        self.assertTrue(os.path.exists(queue_file + ".imported"))
        # 归档文件保留（数据不丢，人工可收）
        with open(queue_file + ".imported", encoding="utf-8") as f:
            archived = _json.load(f)
        self.assertEqual(archived["tasks"][0]["id"], "b" * 32)


class MaintenanceLoopFirstIterationTest(unittest.IsolatedAsyncioTestCase):
    """_maintenance_loop 首轮冒烟：备份→Cookie 体检整段跑通、失效会提醒。

    该循环一天只执行一轮且启动即跑，首轮里的名字级错误（如 2026-09-24 的
    config NameError）只有真正执行首轮才会暴露——此测试钉住它。"""

    async def test_first_iteration_completes_and_notifies_on_invalid(self):
        from tg_userbot import app as app_mod
        from tg_userbot import notify, pawchive

        async def fake_daily():
            return None

        async def fake_check(force=False):
            self.assertTrue(force, "每日体检应绕过缓存现打")
            return False, "已失效（HTTP 401）"

        sent = []

        async def fake_notify(text):
            sent.append(text)

        async def fake_sleep(seconds):
            raise asyncio.CancelledError   # 首轮跑完即停

        with mock.patch.object(maintenance, "daily_maintenance", fake_daily), \
             mock.patch.object(pawchive, "cookie_check_cached", fake_check), \
             mock.patch.object(notify, "notify_user", fake_notify), \
             mock.patch.object(config, "PAWCHIVE_COOKIE", "sessionid=X"), \
             mock.patch.object(app_mod.asyncio, "sleep", fake_sleep):
            task = asyncio.ensure_future(app_mod._maintenance_loop())
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(sent), 1)
        self.assertIn("Cookie", sent[0])
        self.assertIn("已失效", sent[0])
