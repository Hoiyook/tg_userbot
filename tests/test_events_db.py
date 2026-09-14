"""任务事件流 SQLite 持久化（runtime_db 的 download_events 表）单元测试。

任务书：docs/plan/Userbot_任务事件SQLite化_Phase2任务书.md §10 测试 1-4、12。
stats/reporter 接线与一次性导入在 tests/test_events_wiring.py（测试 5-11）。

    .venv/bin/python -m unittest tests.test_events_db -v
"""
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录（config import 期
# 有真实副作用，见 test_config 的基座说明）。
_TMP = tempfile.mkdtemp(prefix="tg_userbot_events_db_test_")
import atexit  # noqa: E402
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402

FMT = "%Y-%m-%d %H:%M:%S"


def _epoch(ts_str):
    """本地时间串 → epoch（与 runtime_db 的导入解析同一套换算）。"""
    return int(time.mktime(time.strptime(ts_str, FMT)))


def _rec(ts_str="2026-09-13 21:20:48", ev="SUCCESS", task_id=None, **extra):
    """构造一条与 JSONL rec 同形状的事件（ts 为本地串）。"""
    rec = {"ts": ts_str, "ev": ev}
    if task_id:
        rec["id"] = task_id
    rec.update(extra)
    return rec


def _insert_rec(rec):
    """按 emit_event 的拆解方式把 rec 落库（ts 转epoch、payload 装其余）。"""
    payload = {k: v for k, v in rec.items() if k not in ("ts", "ev", "id")}
    runtime_db.download_event_insert(
        rec["ev"], task_id=rec.get("id"), payload=payload or None,
        ts=_epoch(rec["ts"]))


class EventsDbTestBase(unittest.TestCase):
    """每个用例一个临时库，测完关闭。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="eventsdb_", dir=_TMP)
        self.path = os.path.join(self.dir, "tg_userbot.db")
        self._p = mock.patch.object(config, "RUNTIME_DB_FILE", self.path)
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db(), "init_db 应成功")


class TestRecShapeParity(EventsDbTestBase):
    """测试 1/2：行 → rec dict 与 JSONL rec 逐字段兼容。"""

    def test_full_field_roundtrip(self):
        rec = _rec(ev="SUCCESS", task_id="a" * 32, label="26-09-06 测试.mp4",
                   bytes=200000, src="me")
        _insert_rec(rec)
        events = runtime_db.download_events_all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], rec)       # 逐键逐值（含 ts 串格式）

    def test_label_unicode_and_special(self):
        rec = _rec(task_id="b" * 32,
                   label="26-09-13 标题_ #o泡o泡 0_09.mp4")
        _insert_rec(rec)
        self.assertEqual(runtime_db.download_events_all()[0]["label"],
                         rec["label"])

    def test_input_side_event_has_no_id_key(self):
        """DEDUP_SKIPPED/LISTEN_SCAN 无 task_id → rec 无 `id` 键（JSONL 对齐）。"""
        rec = _rec(ev="DEDUP_SKIPPED", label=None)
        _insert_rec(rec)
        events = runtime_db.download_events_all()
        self.assertEqual(events[0]["ev"], "DEDUP_SKIPPED")
        self.assertNotIn("id", events[0])
        rec2 = _rec(ev="LISTEN_SCAN", scanned=3, matched=1, created=1)
        _insert_rec(rec2)
        self.assertNotIn("id", runtime_db.download_events_all()[1])
        self.assertEqual(runtime_db.download_events_all()[1]["matched"], 1)

    def test_multiple_events_keep_insertion_order(self):
        for i, ev in enumerate(("RECEIVED", "QUEUED", "RUNNING",
                                "RETRY", "FAILED", "SUCCESS")):
            _insert_rec(_rec(ev=ev, task_id="c" * 32, attempts=i))
        events = runtime_db.download_events_all()
        self.assertEqual([e["ev"] for e in events],
                         ["RECEIVED", "QUEUED", "RUNNING",
                          "RETRY", "FAILED", "SUCCESS"])


class TestSinceCursor(EventsDbTestBase):
    """测试 3：reporter 增量游标语义。"""

    def setUp(self):
        super().setUp()
        for i in range(5):
            _insert_rec(_rec(ev="RUNNING", task_id="d" * 32, n=i))

    def test_since_returns_only_newer_in_order(self):
        events = runtime_db.download_events_since(2)
        self.assertEqual([e["n"] for e in events], [2, 3, 4])
        events = runtime_db.download_events_since(0)
        self.assertEqual(len(events), 5)               # 从头全量

    def test_since_max_returns_empty(self):
        max_id = runtime_db.download_events_max_id()
        self.assertEqual(runtime_db.download_events_since(max_id), [])

    def test_new_inserts_after_cursor_are_visible(self):
        max_id = runtime_db.download_events_max_id()
        _insert_rec(_rec(ev="AUTO_REPLAY", task_id="d" * 32))
        events = runtime_db.download_events_since(max_id)
        self.assertEqual([e["ev"] for e in events], ["AUTO_REPLAY"])

    def test_limit_caps_batch(self):
        self.assertEqual(len(runtime_db.download_events_since(0, limit=3)), 3)


class TestTrim(EventsDbTestBase):
    """测试 4：保尾裁剪 + rowid 单调性（reporter 游标不破）。"""

    def test_trim_keeps_tail(self):
        for i in range(10):
            _insert_rec(_rec(ev="QUEUED", task_id="e" * 32, n=i))
        removed = runtime_db.download_events_trim(4)
        self.assertEqual(removed, 6)
        events = runtime_db.download_events_all()
        self.assertEqual([e["n"] for e in events], [6, 7, 8, 9])

    def test_rowid_stays_monotonic_after_trim(self):
        for i in range(10):
            _insert_rec(_rec(ev="QUEUED", task_id="e" * 32, n=i))
        runtime_db.download_events_trim(4)
        _insert_rec(_rec(ev="SUCCESS", task_id="e" * 32, n=10))
        events = runtime_db.download_events_all()
        # 新事件排在最后（rowid 单调 → 读出序即插入序）
        self.assertEqual([e["n"] for e in events], [6, 7, 8, 9, 10])
        # 裁剪前指向已删行的旧游标：不会错拿已删数据，新事件照常可见
        events_since = runtime_db.download_events_since(5)
        self.assertEqual([e["n"] for e in events_since], [6, 7, 8, 9, 10])

    def test_trim_idempotent(self):
        for i in range(3):
            _insert_rec(_rec(ev="QUEUED", task_id="f" * 32))
        self.assertEqual(runtime_db.download_events_trim(10), 0)  # 不足不动
        self.assertEqual(runtime_db.download_events_count(), 3)


class TestCountAndMaxId(EventsDbTestBase):
    def test_empty_db(self):
        self.assertEqual(runtime_db.download_events_count(), 0)
        self.assertEqual(runtime_db.download_events_max_id(), 0)


class TestNoConnectionContract(EventsDbTestBase):
    """DB 未初始化 → DbUnavailable（降级决策在 stats/reporter）。"""

    def test_ops_raise_when_no_connection(self):
        runtime_db.close_db()
        with self.assertRaises(runtime_db.DbUnavailable):
            runtime_db.download_events_all()
        with self.assertRaises(runtime_db.DbUnavailable):
            runtime_db.download_event_insert("RUNNING")
        with self.assertRaises(runtime_db.DbUnavailable):
            runtime_db.download_events_since(0)
        with self.assertRaises(runtime_db.DbUnavailable):
            runtime_db.download_events_trim(10)
        with self.assertRaises(runtime_db.DbUnavailable):
            runtime_db.download_events_count()
        with self.assertRaises(runtime_db.DbUnavailable):
            runtime_db.download_events_max_id()


class TestSchemaV4ToV5(EventsDbTestBase):
    """测试 12：v4 旧库 → v5（download_events 出现，其余原样）。"""

    def setUp(self):
        super().setUp()
        runtime_db.close_db()
        conn = sqlite3.connect(self.path)
        conn.execute("DROP TABLE download_events")
        conn.execute("UPDATE schema_meta SET value='4' "
                     "WHERE key='schema_version'")
        conn.commit()
        conn.close()
        # 放一行队列数据，验证升级不动 Phase 1 的表
        self.assertTrue(runtime_db.init_db(self.path))
        runtime_db.queue_insert({"id": "9" * 32, "kind": "media",
                                 "label": "x.mp4"}, "QUEUED", now=1000)
        runtime_db.close_db()
        conn = sqlite3.connect(self.path)
        conn.execute("DROP TABLE download_events")     # 回到真实 v4 形态
        conn.execute("UPDATE schema_meta SET value='4' "
                     "WHERE key='schema_version'")
        conn.commit()
        conn.close()

    def test_v4_migrates_to_v5(self):
        self.assertTrue(runtime_db.init_db(self.path))
        self.assertEqual(runtime_db.get_schema_version(), config.RUNTIME_DB_SCHEMA_VERSION)
        self.assertEqual(runtime_db.download_events_count(), 0)
        # Phase 1 的 download_tasks 原样保留
        self.assertEqual(runtime_db.queue_count(), {"queued": 1, "retry": 0})

    def test_migration_idempotent(self):
        self.assertTrue(runtime_db.init_db(self.path))
        self.assertTrue(runtime_db.init_db(self.path))
        self.assertEqual(runtime_db.get_schema_version(), config.RUNTIME_DB_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
