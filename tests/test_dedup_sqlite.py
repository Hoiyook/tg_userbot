"""去重索引 SQLite 持久化（runtime_db 的 dedup_index 表）测试。

Phase 4（schema v7）。纪律与队列/事件/历史一致：内存 dict
（state.DEDUP_INDEX）是唯一工作副本与读取面，DB 只换「怎么存」；
remember 的 DB 写失败仅告警、dict 照常更新（与文件时代逐字同构）。

    .venv/bin/python -m unittest tests.test_dedup_sqlite -v
"""
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录（config import 期
# 有真实副作用，见 test_config 的基座说明）。
_TMP = tempfile.mkdtemp(prefix="tg_userbot_dedup_sqlite_test_")
import atexit  # noqa: E402
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import dedup  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402
from tg_userbot import state  # noqa: E402


class DedupDbTestBase(unittest.TestCase):
    """DB 模式基座：临时库 + 临时索引文件路径 + 干净 dict。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dedup_db_", dir=_TMP)
        self.db_path = os.path.join(self.dir, "tg_userbot.db")
        self.idx_file = os.path.join(self.dir, "dedup_index.txt")
        self._patches = [
            mock.patch.object(config, "RUNTIME_DB_FILE", self.db_path),
            mock.patch.object(dedup, "DEDUP_INDEX_FILE", self.idx_file),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        self._old_index = state.DEDUP_INDEX
        state.DEDUP_INDEX = {}
        self.addCleanup(setattr, state, "DEDUP_INDEX", self._old_index)

    def _write_index_file(self, lines):
        with open(self.idx_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    @staticmethod
    def _file_line(key, ts="26-09-13 21:02", filename="视频.mp4"):
        return f"{key}\t{ts}\t{filename}"


class TestRememberAndLoad(DedupDbTestBase):
    """remember 落表 + dict 更新；load_index 重建 dict（重复键后写胜）。"""

    def test_multi_key_remember(self):
        dedup.remember(["tg:AAA", "f:a.mp4:123", "c:sha256xxx"],
                       "26-09-13 标题.mp4")
        self.assertEqual(runtime_db.dedup_index_count(), 3)
        self.assertEqual(state.DEDUP_INDEX["tg:AAA"]["filename"],
                         "26-09-13 标题.mp4")
        # 重建（模拟重启）
        state.DEDUP_INDEX = {}
        loaded = dedup.load_index()
        self.assertEqual(loaded, 3)
        self.assertEqual(state.DEDUP_INDEX["c:sha256xxx"]["date"],
                         state.DEDUP_INDEX["tg:AAA"]["date"])

    def test_single_key_str_compat(self):
        """抖音路径的单键字符串入参兼容。"""
        dedup.remember("dyc:12345", "抖音视频.mp4")
        self.assertIn("dyc:12345", state.DEDUP_INDEX)
        state.DEDUP_INDEX = {}
        dedup.load_index()
        self.assertIn("dyc:12345", state.DEDUP_INDEX)

    def test_duplicate_key_last_wins(self):
        """同键重复记（不应发生但文件时代如此容忍）：dict 取最后一条。"""
        dedup.remember("tg:AAA", "第一条.mp4")
        dedup.remember("tg:AAA", "第二条.mp4")
        self.assertEqual(runtime_db.dedup_index_count(), 2)
        state.DEDUP_INDEX = {}
        dedup.load_index()
        self.assertEqual(state.DEDUP_INDEX["tg:AAA"]["filename"],
                         "第二条.mp4")

    def test_remember_db_failure_dict_still_updated(self):
        """写失败仅告警、dict 照常更新——与文件时代逐字同构的语义。"""
        with mock.patch.object(runtime_db, "dedup_index_append",
                               side_effect=runtime_db.DbUnavailable("炸了")), \
                self.assertLogs("tg_userbot", level="WARNING"):
            dedup.remember("tg:AAA", "x.mp4")     # 不抛
        self.assertIn("tg:AAA", state.DEDUP_INDEX)
        self.assertEqual(runtime_db.dedup_index_count(), 0)

    def test_none_and_empty_keys_skipped(self):
        dedup.remember([None, "", "tg:OK"], "x.mp4")
        self.assertEqual(runtime_db.dedup_index_count(), 1)

    def test_should_skip_hits_after_remember(self):
        """集成冒烟：判重主链路读 dict，行为与文件时代一致。"""
        dedup.remember("tg:AAA", "x.mp4")
        skip, _ = dedup.should_skip(["tg:AAA"])
        self.assertTrue(skip)


class TestTrim(DedupDbTestBase):
    """启动裁剪：超上限保尾部（DB DELETE，dict 只装保留的键）。"""

    def test_trim_keeps_tail(self):
        for i in range(12):
            dedup.remember(f"tg:K{i:02d}", f"视频{i}.mp4")
        state.DEDUP_INDEX = {}
        with mock.patch.object(dedup, "DEDUP_MAX_ENTRIES", 10):
            loaded = dedup.load_index()
        self.assertEqual(loaded, 10)
        self.assertNotIn("tg:K00", state.DEDUP_INDEX)   # 最旧被裁
        self.assertIn("tg:K11", state.DEDUP_INDEX)      # 最新保留
        self.assertEqual(runtime_db.dedup_index_count(), 10)


class TestFileFallback(DedupDbTestBase):
    """无 DB 连接 → 文件旧路径（既有行为，含启动原子重写裁剪）。"""

    def test_file_mode_remember_and_load(self):
        runtime_db.close_db()
        dedup.remember("tg:AAA", "x.mp4")
        self.assertTrue(os.path.exists(self.idx_file))
        state.DEDUP_INDEX = {}
        dedup.load_index()
        self.assertIn("tg:AAA", state.DEDUP_INDEX)


class TestStartupImport(DedupDbTestBase):
    """旧 dedup_index.txt 一次性导入（tab 三段，filename 含 tab 容错）。"""

    def test_one_time_import(self):
        self._write_index_file([
            self._file_line("tg:AAA", "26-09-08 08:39", "长离.mp4"),
            self._file_line("dyc:123", "26-09-08 08:44", "抖音.mp4"),
        ])
        loaded = dedup.load_index()
        self.assertEqual(loaded, 2)
        self.assertEqual(runtime_db.dedup_index_count(), 2)
        self.assertEqual(state.DEDUP_INDEX["tg:AAA"]["filename"], "长离.mp4")
        self.assertFalse(os.path.exists(self.idx_file))
        self.assertTrue(os.path.exists(self.idx_file + ".imported"))
        # 幂等
        state.DEDUP_INDEX = {}
        dedup.load_index()
        self.assertEqual(runtime_db.dedup_index_count(), 2)

    def test_import_filename_with_tab_joined(self):
        """load_index 对 parts[2:] 的 \t join 语义在导入侧保持。"""
        self._write_index_file([self._file_line("tg:AAA", "26-09-08",
                                                "含\ttab.mp4")])
        dedup.load_index()
        state.DEDUP_INDEX = {}
        dedup.load_index()
        self.assertEqual(state.DEDUP_INDEX["tg:AAA"]["filename"],
                         "含\ttab.mp4")

    def test_bad_lines_skipped(self):
        self._write_index_file([
            "没有tab分隔的行",                       # 坏行：跳过
            "\t\t",                                  # 空键：跳过
            self._file_line("tg:BBB", "26-09-08", "好行.mp4"),
        ])
        dedup.load_index()
        self.assertEqual(runtime_db.dedup_index_count(), 1)
        self.assertIn("tg:BBB", state.DEDUP_INDEX)

    def test_import_conflict_db_wins(self):
        dedup.remember("tg:DB", "db里的.mp4")
        self._write_index_file([self._file_line("tg:FILE", "26-09-08",
                                                "文件里的.mp4")])
        with self.assertLogs("tg_userbot", level="WARNING"):
            dedup.load_index()
        self.assertEqual(runtime_db.dedup_index_count(), 1)
        self.assertIn("tg:DB", state.DEDUP_INDEX)
        self.assertTrue(os.path.exists(self.idx_file + ".imported"))

    def test_import_failure_keeps_file(self):
        self._write_index_file([self._file_line("tg:AAA")])
        with mock.patch.object(runtime_db, "dedup_index_append",
                               side_effect=runtime_db.DbUnavailable("炸了")), \
                self.assertLogs("tg_userbot", level="WARNING"):
            loaded = dedup.load_index()
        self.assertEqual(loaded, 0)
        self.assertTrue(os.path.exists(self.idx_file))    # 原样保留

    def test_missing_file_noop(self):
        self.assertEqual(dedup.load_index(), 0)


class TestSchemaV6ToV7(DedupDbTestBase):
    def setUp(self):
        super().setUp()
        runtime_db.close_db()
        conn = sqlite3.connect(self.db_path)
        conn.execute("DROP TABLE dedup_index")
        conn.execute("UPDATE schema_meta SET value='6' "
                     "WHERE key='schema_version'")
        conn.commit()
        conn.close()

    def test_v6_migrates_to_v7(self):
        self.assertTrue(runtime_db.init_db(self.db_path))
        self.assertEqual(runtime_db.get_schema_version(), 7)
        self.assertEqual(runtime_db.dedup_index_count(), 0)
        # 前序表原样
        self.assertEqual(runtime_db.history_count(), 0)
        self.assertEqual(runtime_db.queue_count(), {"queued": 0, "retry": 0})

    def test_migration_idempotent(self):
        self.assertTrue(runtime_db.init_db(self.db_path))
        self.assertTrue(runtime_db.init_db(self.db_path))
        self.assertEqual(runtime_db.get_schema_version(), 7)


if __name__ == "__main__":
    unittest.main()
