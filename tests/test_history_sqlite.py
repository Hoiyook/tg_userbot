"""下载历史 SQLite 持久化（runtime_db 的 download_history 表）测试。

任务书延续：docs/plan/Userbot_任务事件SQLite化_Phase2任务书.md 的双模式/
导入纪律；本表为 Phase 3（schema v6）。

    .venv/bin/python -m unittest tests.test_history_sqlite -v
"""
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录（config import 期
# 有真实副作用，见 test_config 的基座说明）。
_TMP = tempfile.mkdtemp(prefix="tg_userbot_history_sqlite_test_")
import atexit  # noqa: E402
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import finder  # noqa: E402
from tg_userbot import history  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402
from tg_userbot import stats  # noqa: E402
from tg_userbot import text  # noqa: E402


def _line(ts="2026-09-13 21:03:06", kind="普通", name="26-09-03 视频.mp4",
          size="485.51 MB", source="祂录（3D区）"):
    return f"{ts} | {kind} | {name} | {size} | 来源：{source}"


# parity/导入共用夹具：3 行（含跨天 + 旧类型 抖音）
FIXTURE = [
    _line("2026-09-12 08:00:00", "普通", "12日的.mp4", "1.00 MB", "频道A"),
    _line("2026-09-13 09:00:00", "抖音", "13日的.mp4", "2.00 MB", "本地解析"),
    _line("2026-09-13 10:00:00", "普通", "另一条 13日.mp4", "3.00 MB",
          "频道B"),
]


class HistoryDbTestBase(unittest.TestCase):
    """DB 模式基座：临时库 + 临时历史文件路径。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="history_db_", dir=_TMP)
        self.db_path = os.path.join(self.dir, "tg_userbot.db")
        self.hist_file = os.path.join(self.dir, "download_history.txt")
        self._patches = [
            mock.patch.object(config, "RUNTIME_DB_FILE", self.db_path),
            mock.patch.object(history, "DOWNLOAD_HISTORY_FILE",
                              self.hist_file),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())


class TestAppendAndRender(HistoryDbTestBase):
    """append → get 逐字符还原（rendered line 是消费方的唯一接口）。"""

    def test_structured_roundtrip(self):
        rec = _line()
        history.append_history(rec)
        self.assertEqual(history.get_history_lines(), [rec])
        # 结构化存储（非 raw）：kind/source 可查
        row = runtime_db._read(
            lambda c: runtime_db._execute(
                c, "SELECT ts, kind, filename, size_text, source, raw "
                   "FROM download_history").fetchone(), "查行")
        self.assertIsNone(row["raw"])
        self.assertEqual(row["kind"], "普通")
        self.assertEqual(row["source"], "来源：祂录（3D区）")

    def test_old_kinds_roundtrip(self):
        """历史遗留类型（统一链之前的 抖音/Instagram）同样逐字符还原。"""
        for kind in ("抖音", "Instagram"):
            rec = _line(kind=kind, name=f"{kind}视频.mp4",
                        source="本地解析" if kind == "抖音" else "Douyin")
            history.append_history(rec)
            self.assertIn(rec, history.get_history_lines())

    def test_unicode_and_emoji(self):
        rec = _line(name="26-09-13 标题_ #o泡o泡 🤑 0_09.mp4")
        history.append_history(rec)
        self.assertEqual(history.get_history_lines(), [rec])

    def test_malformed_line_stored_raw(self):
        """不合 5 段格式的行原样保留（零丢失），渲染原样吐出。"""
        rec = "这是一段没有竖线的怪记录"
        history.append_history(rec)
        self.assertEqual(history.get_history_lines(), [rec])
        row = runtime_db._read(
            lambda c: runtime_db._execute(
                c, "SELECT raw FROM download_history").fetchone(), "查行")
        self.assertEqual(row["raw"], rec)

    def test_tail_n(self):
        for i in range(5):
            history.append_history(_line(name=f"视频{i}.mp4"))
        lines = history.get_history_lines(2)
        self.assertIn("视频3", lines[0])
        self.assertIn("视频4", lines[1])     # 末 2 条且按时间正序

    def test_db_failure_warns_only(self):
        with mock.patch.object(runtime_db, "history_append_record",
                               side_effect=runtime_db.DbUnavailable("炸了")), \
                self.assertLogs("tg_userbot", level="WARNING"):
            history.append_history(_line())   # 不抛


class TestFileFallback(HistoryDbTestBase):
    """无 DB 连接 → 文件旧路径（既有行为）。"""

    def test_file_mode_roundtrip(self):
        runtime_db.close_db()
        history.append_history(_line())
        self.assertTrue(os.path.exists(self.hist_file))
        self.assertEqual(history.get_history_lines(), [_line()])

    def test_iter_explicit_path_reads_file(self):
        """显式 path（测试/诊断）永远读文件——stats/finder 的注入口径。"""
        history.append_history(_line())      # 进表
        fixture = os.path.join(self.dir, "fixture.txt")
        with open(fixture, "w", encoding="utf-8") as f:
            f.write(_line(name="fixture.mp4") + "\n")
        lines = history.iter_history_lines(fixture)
        self.assertEqual(lines, [_line(name="fixture.mp4")])


class TestConsumerParity(HistoryDbTestBase):
    """消费方 parity：同一数据走文件/DB 两条路径，输出一致。"""

    def _seed_both(self):
        self._write_file(FIXTURE)
        for rec in FIXTURE:
            runtime_db.history_append_record(rec)

    def _write_file(self, lines):
        with open(self.hist_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def test_get_history_lines_parity(self):
        self._seed_both()
        from_db = history.get_history_lines()
        with mock.patch.object(runtime_db, "has_connection",
                               return_value=False):
            from_file = history.get_history_lines()
        self.assertEqual(from_db, from_file)

    def test_collect_stats_parity(self):
        self._seed_both()
        today = date(2026, 9, 13)
        log_a = os.path.join(self.dir, "a.log")
        open(log_a, "w").close()
        # DB 模式（history_path=None）
        db_stats = stats.collect_stats(days=2, today=today,
                                       log_path=log_a, history_path=None)
        # 文件模式（显式 path）
        with mock.patch.object(runtime_db, "has_connection",
                               return_value=False):
            file_stats = stats.collect_stats(days=2, today=today,
                                             log_path=log_a,
                                             history_path=self.hist_file)
        self.assertEqual(db_stats["success_count"], 3)   # 两天全在窗口
        self.assertEqual(db_stats["success_count"],
                         file_stats["success_count"])
        self.assertEqual(db_stats["success_bytes"],
                         file_stats["success_bytes"])

    def test_done_reply_parity(self):
        self._seed_both()
        db_text = text.done_reply_text(10, keyword="13日")
        with mock.patch.object(runtime_db, "has_connection",
                               return_value=False):
            file_text = text.done_reply_text(10, keyword="13日")
        self.assertEqual(db_text, file_text)
        self.assertIn("13日的.mp4", db_text)

    def test_find_media_parity(self):
        self._seed_both()
        log_path = os.path.join(self.dir, "a.log")
        open(log_path, "w").close()
        db_text = finder.find_media("13日", today=date(2026, 9, 13),
                                    log_path=log_path, history_path=None)
        with mock.patch.object(runtime_db, "has_connection",
                               return_value=False):
            file_text = finder.find_media("13日", today=date(2026, 9, 13),
                                          log_path=log_path,
                                          history_path=self.hist_file)
        self.assertEqual(db_text, file_text)
        self.assertIn("✅ 已下载", db_text)


class TestStartupImport(HistoryDbTestBase):
    """旧 download_history.txt 一次性导入（raw 兜底零丢失）。"""

    def test_one_time_import(self):
        with open(self.hist_file, "w", encoding="utf-8") as f:
            f.write("\n".join(FIXTURE) + "\n")
        history.migrate_history_to_db()
        self.assertEqual(runtime_db.history_count(), 3)
        self.assertEqual(history.get_history_lines(), FIXTURE)
        self.assertFalse(os.path.exists(self.hist_file))
        self.assertTrue(os.path.exists(self.hist_file + ".imported"))
        # 幂等
        history.migrate_history_to_db()
        self.assertEqual(runtime_db.history_count(), 3)

    def test_import_conflict_db_wins(self):
        history.append_history(_line(name="DB里的.mp4"))
        with open(self.hist_file, "w", encoding="utf-8") as f:
            f.write(_line(name="旧文件里的.mp4") + "\n")
        with self.assertLogs("tg_userbot", level="WARNING"):
            history.migrate_history_to_db()
        self.assertEqual(runtime_db.history_count(), 1)
        self.assertIn("DB里的", history.get_history_lines()[0])
        self.assertTrue(os.path.exists(self.hist_file + ".imported"))

    def test_import_failure_keeps_file(self):
        with open(self.hist_file, "w", encoding="utf-8") as f:
            f.write(_line() + "\n")
        with mock.patch.object(runtime_db, "history_append_record",
                               side_effect=runtime_db.DbUnavailable("炸了")), \
                self.assertLogs("tg_userbot", level="WARNING"):
            history.migrate_history_to_db()
        self.assertTrue(os.path.exists(self.hist_file))   # 原样保留

    def test_missing_file_noop(self):
        history.migrate_history_to_db()
        self.assertEqual(runtime_db.history_count(), 0)


class TestSchemaV5ToV6(HistoryDbTestBase):
    def setUp(self):
        super().setUp()
        runtime_db.close_db()
        conn = sqlite3.connect(self.db_path)
        conn.execute("DROP TABLE download_history")
        conn.execute("UPDATE schema_meta SET value='5' "
                     "WHERE key='schema_version'")
        conn.commit()
        conn.close()

    def test_v5_migrates_to_v6(self):
        self.assertTrue(runtime_db.init_db(self.db_path))
        self.assertEqual(runtime_db.get_schema_version(), 7)
        self.assertEqual(runtime_db.history_count(), 0)
        # 前序表原样
        self.assertEqual(runtime_db.queue_count(), {"queued": 0, "retry": 0})
        self.assertEqual(runtime_db.download_events_count(), 0)

    def test_migration_idempotent(self):
        self.assertTrue(runtime_db.init_db(self.db_path))
        self.assertTrue(runtime_db.init_db(self.db_path))
        self.assertEqual(runtime_db.get_schema_version(), 7)


if __name__ == "__main__":
    unittest.main()
