"""下载队列 SQLite 持久化（runtime_db 的 download_tasks 表）单元测试。

任务书：docs/plan/Userbot_下载队列SQLite化_DeepSeek任务书.md §11。
本文件覆盖测试 1-4、9、10（表 + 函数层）；queue.py 接线与启动迁移导入在
test_queue_wiring.py（测试 5-8）。

    .venv/bin/python -m unittest tests.test_queue_db -v
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
_TMP = tempfile.mkdtemp(prefix="tg_userbot_queue_db_test_")
import atexit  # noqa: E402
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402


def _record(**extra):
    """构造一条典型队列记录（开放 dict，字段来自 queue.py/app.py 实测全集）。"""
    rec = {
        "id": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
        "kind": "media",
        "label": "26-09-13 标题_ #o泡o泡 0_09.mp4",
        "final_name": "26-09-13 标题_ #o泡o泡 0_09.mp4",
        "chat_id": -1001234567890,
        "msg_id": 42,
        "source_override": None,
        "source_link": "https://t.me/c/1234567890/42",
        "album_caption": "相册说明 📷 emoji",
        "parent_caption": None,
        "parent_date": "2026-09-13T21:37:45+00:00",
        "user_label": "#手工标注",
        "dedup_keys": ["tg:BAADBQAD6yAAAtWmOFWj_ZMSmaY9TwI",
                       "f:0_09.mp4:1116688"],
    }
    rec.update(extra)
    return rec


class QueueDbTestBase(unittest.TestCase):
    """每个用例一个临时库，测完关闭。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="queuedb_", dir=_TMP)
        self.path = os.path.join(self.dir, "tg_userbot.db")
        self._p = mock.patch.object(config, "RUNTIME_DB_FILE", self.path)
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db(), "init_db 应成功")


class TestRecordRoundTrip(QueueDbTestBase):
    """测试 1：insert → load 逐字段无损（含 unicode/未知字段）。"""

    def test_full_field_roundtrip(self):
        rec = _record()
        runtime_db.queue_insert(rec, "QUEUED", now=1000)
        rows = runtime_db.queue_load_all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["__state"], "QUEUED")
        for key, value in rec.items():
            self.assertEqual(row[key], value, f"字段 {key} round-trip 失损")

    def test_unknown_fields_survive(self):
        """未来新增字段（payload 白名单外）也必须保住。"""
        rec = _record(future_field={"nested": ["x", 1]}, another="值")
        runtime_db.queue_insert(rec, "RETRY", now=1000)
        row = runtime_db.queue_load_all()[0]
        self.assertEqual(row["future_field"], {"nested": ["x", 1]})
        self.assertEqual(row["another"], "值")

    def test_url_task_kind(self):
        rec = _record(kind="url", url="https://v.douyin.com/xxx/",
                      title="标题", chat_id=None, msg_id=None,
                      source_link=None)
        runtime_db.queue_insert(rec, "QUEUED", now=1000)
        row = runtime_db.queue_load_all()[0]
        self.assertEqual(row["kind"], "url")
        self.assertEqual(row["url"], "https://v.douyin.com/xxx/")
        self.assertIsNone(row["chat_id"])


class TestSeqOrdering(QueueDbTestBase):
    """测试 2/3：两态装载 + seq 序号语义。"""

    def test_states_load_into_two_lists_by_seq(self):
        for i in range(3):
            runtime_db.queue_insert(_record(id=f"q{i}"), "QUEUED", now=i)
        for i in range(2):
            runtime_db.queue_insert(_record(id=f"r{i}"), "RETRY", now=i)
        rows = runtime_db.queue_load_all()
        queued = [r["id"] for r in rows if r["__state"] == "QUEUED"]
        retry = [r["id"] for r in rows if r["__state"] == "RETRY"]
        self.assertEqual(queued, ["q0", "q1", "q2"])   # 入队顺序保持
        self.assertEqual(retry, ["r0", "r1"])

    def test_delete_middle_keeps_order(self):
        for i in range(3):
            runtime_db.queue_insert(_record(id=f"q{i}"), "QUEUED", now=i)
        runtime_db.queue_delete("q1")
        queued = [r["id"] for r in runtime_db.queue_load_all()
                  if r["__state"] == "QUEUED"]
        self.assertEqual(queued, ["q0", "q2"])

    def test_move_to_retry_appends_to_retry_end(self):
        runtime_db.queue_insert(_record(id="r0"), "RETRY", now=0)
        runtime_db.queue_insert(_record(id="q1"), "QUEUED", now=1)
        runtime_db.queue_insert(_record(id="q2"), "QUEUED", now=2)
        runtime_db.queue_move_to_retry("q1", attempts=3, next_retry_at=99.5)
        retry = [r["id"] for r in runtime_db.queue_load_all()
                 if r["__state"] == "RETRY"]
        self.assertEqual(retry, ["r0", "q1"])          # 追加到 retry 尾部

    def test_update_retry_keeps_position(self):
        """retry_failed 原位累加：state/seq 不动，只更新次数与到期时间。"""
        runtime_db.queue_insert(_record(id="r0"), "RETRY", now=0)
        runtime_db.queue_insert(_record(id="r1"), "RETRY", now=1)
        before = {r["id"]: r for r in runtime_db.queue_load_all()}
        runtime_db.queue_update_retry("r0", attempts=5, next_retry_at=123.0)
        after = {r["id"]: r for r in runtime_db.queue_load_all()}
        self.assertEqual(after["r0"]["attempts"], 5)
        self.assertEqual(after["r0"]["next_retry_at"], 123.0)
        self.assertEqual(after["r0"]["__seq"], before["r0"]["__seq"])
        self.assertEqual(after["r1"]["__seq"], before["r1"]["__seq"])

    def test_attempts_and_next_retry_persisted(self):
        runtime_db.queue_insert(_record(id="q0"), "QUEUED", now=0)
        runtime_db.queue_move_to_retry("q0", attempts=2, next_retry_at=77.5)
        row = runtime_db.queue_load_all()[0]
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["next_retry_at"], 77.5)
        self.assertEqual(row["__state"], "RETRY")

    def test_delete_is_idempotent(self):
        runtime_db.queue_insert(_record(id="q0"), "QUEUED", now=0)
        runtime_db.queue_delete("q0")
        runtime_db.queue_delete("q0")                  # 不存在不报错
        self.assertEqual(runtime_db.queue_load_all(), [])


class TestQueueCount(QueueDbTestBase):
    def test_queue_count(self):
        runtime_db.queue_insert(_record(id="q0"), "QUEUED", now=0)
        runtime_db.queue_insert(_record(id="q1"), "QUEUED", now=1)
        runtime_db.queue_insert(_record(id="r0"), "RETRY", now=2)
        self.assertEqual(runtime_db.queue_count(),
                         {"queued": 2, "retry": 1})


class TestBadPayloadRow(QueueDbTestBase):
    """测试 9：坏 payload 行跳过 + WARNING，其余照常装载。"""

    def test_corrupt_row_skipped(self):
        runtime_db.queue_insert(_record(id="good0"), "QUEUED", now=0)
        runtime_db.queue_insert(_record(id="good1"), "QUEUED", now=1)
        # 直接往表里塞一行坏 payload（绕过 queue_insert）
        runtime_db._write(
            lambda c: runtime_db._execute(
                c, "INSERT INTO download_tasks(id, kind, state, seq, "
                   "attempts, next_retry_at, enqueued_at, payload) "
                   "VALUES('bad', 'media', 'QUEUED', 99, 0, NULL, 0, "
                   "'{ 不是 JSON')"),
            "塞坏行")
        rows = runtime_db.queue_load_all()
        self.assertEqual([r["id"] for r in rows], ["good0", "good1"])


class TestSchemaV3ToV4(QueueDbTestBase):
    """测试 10：v3 旧库 → v4（download_tasks 出现，listener 数据原样）。"""

    def setUp(self):
        super().setUp()
        # 把刚 init 出来的 v4 库降回 v3 形态：删掉 download_tasks、版本写回 3
        runtime_db.close_db()
        conn = sqlite3.connect(self.path)
        conn.execute("DROP TABLE download_tasks")
        conn.execute("UPDATE schema_meta SET value='3' "
                     "WHERE key='schema_version'")
        conn.commit()
        conn.close()
        # 放一行 listener 数据，验证升级不动 listener 表
        self.assertTrue(runtime_db.init_db(self.path))
        runtime_db.enqueue_listener_tasks(
            -1001234567890, [{"message_id": 7, "target_type": "saved_messages",
                              "target_chat_id": None, "download": 0}])
        runtime_db.close_db()
        conn = sqlite3.connect(self.path)
        conn.execute("DROP TABLE download_tasks")      # 回到真实 v3 形态
        conn.execute("UPDATE schema_meta SET value='3' "
                     "WHERE key='schema_version'")
        conn.commit()
        conn.close()

    def test_v3_migrates_to_v4(self):
        self.assertTrue(runtime_db.init_db(self.path))
        self.assertEqual(runtime_db.get_schema_version(), 5)
        self.assertEqual(runtime_db.queue_count(),
                         {"queued": 0, "retry": 0})    # 空表可用
        # listener 数据在升级中原样保留
        tasks = runtime_db.list_listener_tasks(limit=10)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["message_id"], 7)

    def test_migration_idempotent(self):
        self.assertTrue(runtime_db.init_db(self.path))
        self.assertTrue(runtime_db.init_db(self.path))
        self.assertEqual(runtime_db.get_schema_version(), 5)


class TestNoConnectionContract(QueueDbTestBase):
    """DB 未初始化 → DbUnavailable（降级决策在 queue.py，不在本层）。"""

    def test_ops_raise_when_no_connection(self):
        runtime_db.close_db()
        with self.assertRaises(runtime_db.DbUnavailable):
            runtime_db.queue_load_all()
        with self.assertRaises(runtime_db.DbUnavailable):
            runtime_db.queue_insert(_record(), "QUEUED")
        with self.assertRaises(runtime_db.DbUnavailable):
            runtime_db.queue_delete("x")
        with self.assertRaises(runtime_db.DbUnavailable):
            runtime_db.queue_count()


if __name__ == "__main__":
    unittest.main()
