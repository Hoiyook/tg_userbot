"""stats/reporter 的事件流双模式接线与一次性导入测试。

任务书：docs/plan/Userbot_任务事件SQLite化_Phase2任务书.md §10 测试 5-11。
表/函数层在 tests/test_events_db.py。

    .venv/bin/python -m unittest tests.test_events_wiring -v
"""
import json
import os
import shutil
import tempfile
import unittest
from datetime import date
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录（config import 期
# 有真实副作用，见 test_config 的基座说明）。
_TMP = tempfile.mkdtemp(prefix="tg_userbot_events_wiring_test_")
import atexit  # noqa: E402
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402
from tg_userbot import reporter  # noqa: E402
from tg_userbot import stats  # noqa: E402
from tg_userbot import state  # noqa: E402


def _rec(ts_str="2026-09-13 21:20:48", ev="SUCCESS", task_id=None, **extra):
    rec = {"ts": ts_str, "ev": ev}
    if task_id:
        rec["id"] = task_id
    rec.update(extra)
    return rec


def _fixture_events():
    """golden parity 共用事件集：全词表取样 + 跨天 + 无 id 事件。"""
    return [
        _rec("2026-09-12 08:00:01", "RECEIVED", "a" * 32,
             label="12日的视频.mp4", src="me"),
        _rec("2026-09-12 08:00:02", "QUEUED", "a" * 32, kind="media"),
        _rec("2026-09-12 08:00:03", "RUNNING", "a" * 32),
        _rec("2026-09-12 08:01:00", "SUCCESS", "a" * 32,
             label="12日的视频.mp4", bytes=1234567),
        _rec("2026-09-13 09:00:00", "DEDUP_SKIPPED"),
        _rec("2026-09-13 09:00:10", "LISTEN_SCAN", scanned=5, matched=1,
             created=1, duplicate=0),
        _rec("2026-09-13 10:00:00", "RECEIVED", "b" * 32, src="wl",
             label="13日的视频.mp4"),
        _rec("2026-09-13 10:00:01", "QUEUED", "b" * 32, kind="media"),
        _rec("2026-09-13 10:00:02", "RUNNING", "b" * 32),
        _rec("2026-09-13 10:00:30", "RETRY", "b" * 32, attempts=1),
        _rec("2026-09-13 10:00:31", "FAILED", "b" * 32),
        _rec("2026-09-13 10:05:00", "AUTO_REPLAY", "b" * 32, attempts=2),
        _rec("2026-09-13 10:05:10", "RUNNING", "b" * 32),
        _rec("2026-09-13 10:05:40", "SUCCESS", "b" * 32,
             label="13日的视频.mp4", bytes=999),
        _rec("2026-09-13 11:00:00", "REMOVED", "c" * 32, why="manual"),
        _rec("2026-09-13 11:01:00", "CANCELLED", "d" * 32),
        _rec("2026-09-13 11:02:00", "DEDUP_HIT", "e" * 32),
    ]


class _DbModeBase(unittest.TestCase):
    """DB 模式基座：临时库 + 临时 TASK_EVENTS_FILE。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="events_wiring_", dir=_TMP)
        self.db_path = os.path.join(self.dir, "tg_userbot.db")
        self.events_file = os.path.join(self.dir, "task_events.jsonl")
        self._patches = [
            mock.patch.object(config, "RUNTIME_DB_FILE", self.db_path),
            mock.patch.object(stats, "TASK_EVENTS_FILE", self.events_file),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())

    def _write_jsonl(self, recs):
        with open(self.events_file, "w", encoding="utf-8") as f:
            for rec in recs:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class TestEmitEventDbMode(_DbModeBase):
    """测试 5：emit_event DB 模式落表；失败仅告警不抛。"""

    def test_emit_lands_in_db(self):
        stats.emit_event("SUCCESS", task_id="a" * 32,
                         label="测试.mp4", bytes=123)
        events = runtime_db.download_events_all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["ev"], "SUCCESS")
        self.assertEqual(events[0]["id"], "a" * 32)
        self.assertEqual(events[0]["bytes"], 123)
        # label 清洗与截断逻辑原样
        stats.emit_event("RECEIVED", task_id="b" * 32, label="a\tb\nc" * 40)
        rec = runtime_db.download_events_all()[1]
        self.assertNotIn("\t", rec["label"])
        self.assertNotIn("\n", rec["label"])
        self.assertLessEqual(len(rec["label"]), 80)
        # None 值 extra 不落
        stats.emit_event("RUNNING", task_id="c" * 32, label=None, src=None)
        self.assertNotIn("label", runtime_db.download_events_all()[2])
        self.assertNotIn("src", runtime_db.download_events_all()[2])

    def test_emit_failure_warns_only(self):
        with mock.patch.object(runtime_db, "download_event_insert",
                               side_effect=runtime_db.DbUnavailable("炸了")), \
                self.assertLogs("tg_userbot", level="WARNING"):
            stats.emit_event("RUNNING", task_id="a" * 32)   # 不抛
        self.assertEqual(runtime_db.download_events_count(), 0)


class TestLoadEventsParity(_DbModeBase):
    """测试 7/8：双模式 load_events 形状 parity + rebuild_stats golden。"""

    def _seed_both(self):
        """同一事件集分别落 JSONL 文件与 DB，返回文件路径读出的事件。"""
        events = _fixture_events()
        self._write_jsonl(events)
        # 文件模式读出（此时是唯一真相，读完再灌 DB）
        with mock.patch.object(runtime_db, "has_connection", return_value=False):
            from_file = stats.load_events()
        for rec in events:
            payload = {k: v for k, v in rec.items()
                       if k not in ("ts", "ev", "id")}
            runtime_db.download_event_insert(
                rec["ev"], task_id=rec.get("id"), payload=payload or None,
                ts=runtime_db._event_epoch_from_str(rec["ts"]))
        return events, from_file

    def test_load_events_shape_parity(self):
        events, from_file = self._seed_both()
        from_db = stats.load_events()          # DB 模式
        self.assertEqual(from_db, from_file)   # 逐条逐键相等

    def test_rebuild_stats_golden_parity(self):
        events, from_file = self._seed_both()
        today = date(2026, 9, 13)
        built_db = stats.rebuild_stats(stats.load_events(), days=2,
                                       today=today)
        built_file = stats.rebuild_stats(from_file, days=2, today=today)
        self.assertEqual(built_db, built_file)  # 逐字符相等

    def test_stats_text_parity(self):
        """端到端：stats_text（含 _legacy_text 回落判定）双模式一致。"""
        self._seed_both()
        today = date(2026, 9, 13)
        log_a = os.path.join(self.dir, "a.log")
        log_b = os.path.join(self.dir, "b.log")
        for p in (log_a, log_b):
            open(p, "w").close()
        text_db = stats.stats_text(days=2, today=today, log_path=log_a,
                                   history_path=log_a)
        with mock.patch.object(runtime_db, "has_connection",
                               return_value=False), \
                mock.patch.object(stats, "TASK_EVENTS_FILE",
                                  self.events_file):
            text_file = stats.stats_text(days=2, today=today,
                                         log_path=log_b,
                                         history_path=log_b)
        self.assertEqual(text_db, text_file)


class TestJsonFallback(_DbModeBase):
    """测试 6：无 DB 连接 → emit/load/trim 走 JSONL 旧路径。"""

    def test_fallback_when_not_connected(self):
        runtime_db.close_db()
        stats.emit_event("RUNNING", task_id="a" * 32, label="x.mp4")
        self.assertTrue(os.path.exists(self.events_file))   # 写的是文件
        events = stats.load_events()
        self.assertEqual([e["ev"] for e in events], ["RUNNING"])
        # trim 走文件（造 5 条裁到 2）
        for i in range(4):
            stats.emit_event("QUEUED", task_id="a" * 32, n=i)
        stats.trim_event_file(max_events=2)
        self.assertEqual(len(stats.load_events()), 2)

    def test_trim_db_mode(self):
        for i in range(10):
            stats.emit_event("QUEUED", task_id="a" * 32, n=i)
        stats.trim_event_file(max_events=4)     # DB 在连 → 裁表
        self.assertEqual(runtime_db.download_events_count(), 4)
        self.assertFalse(os.path.exists(self.events_file))  # 不碰文件


class TestReporterCursor(_DbModeBase):
    """测试 11：reporter 游标 = MAX(id)；只通知新增；裁剪不丢不重。"""

    def _make_reporter(self):
        rep = reporter.Reporter.__new__(reporter.Reporter)
        rep._event_cursor = None
        rep._stats_cache = None
        rep._stats_dirty = False
        rep._error_keys = set()
        rep.dispatched = []
        rep._touch_activity = lambda: None

        async def fake_dispatch(events):
            rep.dispatched.extend(events)

        rep.dispatch_events = fake_dispatch
        return rep

    def test_mark_cursor_skips_history(self):
        for i in range(3):
            stats.emit_event("QUEUED", task_id="a" * 32, n=i)
        rep = self._make_reporter()
        rep.mark_event_cursor()
        self.assertEqual(rep._event_cursor,
                         runtime_db.download_events_max_id())
        import asyncio
        asyncio.run(rep.poll_events())
        self.assertEqual(rep.dispatched, [])    # 历史不重放

    def test_poll_returns_only_new_events(self):
        for i in range(3):
            stats.emit_event("QUEUED", task_id="a" * 32, n=i)
        rep = self._make_reporter()
        rep.mark_event_cursor()
        stats.emit_event("AUTO_REPLAY", task_id="a" * 32, n=99)
        import asyncio
        asyncio.run(rep.poll_events())
        self.assertEqual([e["n"] for e in rep.dispatched], [99])

    def test_trim_does_not_break_cursor(self):
        for i in range(6):
            stats.emit_event("QUEUED", task_id="a" * 32, n=i)
        rep = self._make_reporter()
        rep.mark_event_cursor()
        stats.trim_event_file(max_events=3)     # 裁掉旧行
        stats.emit_event("SUCCESS", task_id="a" * 32, n=99)
        import asyncio
        asyncio.run(rep.poll_events())
        self.assertEqual([e["n"] for e in rep.dispatched], [99])  # 不丢不重


class TestStartupImport(_DbModeBase):
    """测试 9/10：一次性导入 → .imported 归档；冲突 DB 赢。"""

    def test_one_time_import(self):
        self._write_jsonl(_fixture_events())
        stats.migrate_and_trim_events()
        self.assertEqual(runtime_db.download_events_count(),
                         len(_fixture_events()))
        # rec 形状 parity（抽查含无 id 事件与 unicode）
        events = runtime_db.download_events_all()
        self.assertEqual(events[0], _fixture_events()[0])
        self.assertNotIn("id", events[4])       # DEDUP_SKIPPED
        self.assertFalse(os.path.exists(self.events_file))
        self.assertTrue(os.path.exists(self.events_file + ".imported"))

    def test_import_idempotent(self):
        self._write_jsonl(_fixture_events())
        stats.migrate_and_trim_events()
        stats.migrate_and_trim_events()          # 第二次：表有行 → 无动作
        self.assertEqual(runtime_db.download_events_count(),
                         len(_fixture_events()))

    def test_db_wins_on_conflict(self):
        stats.emit_event("RUNNING", task_id="f" * 32)   # 表里先有 1 条
        self._write_jsonl(_fixture_events())
        with self.assertLogs("tg_userbot", level="WARNING"):
            stats.migrate_and_trim_events()
        self.assertEqual(runtime_db.download_events_count(), 1)
        self.assertTrue(os.path.exists(self.events_file + ".imported"))

    def test_bad_lines_skipped(self):
        with open(self.events_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(_rec(task_id="a" * 32)) + "\n")
            f.write("{ 不是 JSON\n")
            f.write(json.dumps(_rec(ts="不是时间", task_id="a" * 32)) + "\n")
            f.write("\n")
        stats.migrate_and_trim_events()
        self.assertEqual(runtime_db.download_events_count(), 1)
        self.assertTrue(os.path.exists(self.events_file + ".imported"))

    def test_empty_file_no_import(self):
        self._write_jsonl([])
        stats.migrate_and_trim_events()
        self.assertEqual(runtime_db.download_events_count(), 0)
        self.assertFalse(os.path.exists(self.events_file + ".imported"))

    def test_import_failure_keeps_file(self):
        self._write_jsonl(_fixture_events())
        with mock.patch.object(runtime_db, "download_event_insert",
                               side_effect=runtime_db.DbUnavailable("炸了")), \
                self.assertLogs("tg_userbot", level="WARNING"):
            stats.migrate_and_trim_events()
        self.assertTrue(os.path.exists(self.events_file))   # 原样保留


if __name__ == "__main__":
    unittest.main()
