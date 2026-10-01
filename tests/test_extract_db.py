"""extract_tasks 表（115 解压回传，schema v12）单元测试。

契约（任务书 docs/plan/Userbot_115解压回传任务书.md §3）：
1. 唯一键 (remote_dir, archive_name, archive_size) 幂等——重复 /115x 不重复入队。
2. claim 乐观锁：并发领取只有一个赢家；租约过期可恢复。
3. FAILED 退避重试（同一行回 PENDING，不新增）；TERMINAL 是不可恢复终态。
4. 手动重投可把 FAILED/TERMINAL 拉回 PENDING。

    .venv/bin/python -m unittest tests.test_extract_db -v
"""
import atexit
import os
import shutil
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_extract_db_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402

DIR_A = "/115open/云下载"


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

    def _enqueue(self, archives=None, remote_dir=DIR_A):
        ins, _skip = runtime_db.enqueue_extract_tasks(
            remote_dir, archives or [("pack.zip", 1024)])
        self.assertEqual(ins, 1)
        return runtime_db.list_extract_tasks()[0]


class EnqueueTest(_DbTestCase):
    def test_enqueue_and_idempotent(self):
        ins, skip = runtime_db.enqueue_extract_tasks(
            DIR_A, [("a.zip", 100), ("b.rar", 200)])
        self.assertEqual((ins, skip), (2, 0))
        # 同目录同名同尺寸重复 → 跳过
        ins, skip = runtime_db.enqueue_extract_tasks(
            DIR_A, [("a.zip", 100), ("b.rar", 200)])
        self.assertEqual((ins, skip), (0, 2))
        # 同名不同尺寸 = 不同文件 → 新任务
        ins, skip = runtime_db.enqueue_extract_tasks(DIR_A, [("a.zip", 999)])
        self.assertEqual(ins, 1)
        counts = runtime_db.extract_status_counts()
        self.assertEqual(counts.get(runtime_db.EXTRACT_PENDING), 3)

    def test_same_name_different_dir_is_separate(self):
        runtime_db.enqueue_extract_tasks(DIR_A, [("a.zip", 100)])
        runtime_db.enqueue_extract_tasks("/115open/其他", [("a.zip", 100)])
        self.assertEqual(len(runtime_db.list_extract_tasks()), 2)


class ClaimLeaseTest(_DbTestCase):
    def test_claim_optimistic_lock(self):
        task = self._enqueue()
        claimed = runtime_db.claim_next_extract_task(lease_seconds=600)
        self.assertEqual(claimed["id"], task["id"])
        self.assertEqual(claimed["status"], runtime_db.EXTRACT_PROCESSING)
        self.assertEqual(claimed["attempts"], 1)
        self.assertIsNotNone(claimed["lease_until"])
        # 已无 PENDING → 第二次领取拿不到
        self.assertIsNone(runtime_db.claim_next_extract_task())

    def test_expired_lease_recovers(self):
        task = self._enqueue()
        runtime_db.claim_next_extract_task(now=1000, lease_seconds=600)
        # 租约 1000+600，未到期 → 不恢复
        self.assertEqual(runtime_db.recover_expired_extract_tasks(now=1500), 0)
        # 到期 → 恢复 PENDING 且 attempts 保留
        self.assertEqual(runtime_db.recover_expired_extract_tasks(now=2000), 1)
        again = runtime_db.claim_next_extract_task(now=2001)
        self.assertEqual(again["id"], task["id"])
        self.assertEqual(again["attempts"], 2)

    def test_retry_at_gates_pickup(self):
        self._enqueue()
        claimed = runtime_db.claim_next_extract_task(now=1000)
        runtime_db.postpone_extract_task(
            claimed["id"], next_retry_at=9999, error="暂退")
        self.assertIsNone(runtime_db.claim_next_extract_task(now=2000))
        due = runtime_db.claim_next_extract_task(now=10000)
        self.assertIsNotNone(due)


class TerminalFlowTest(_DbTestCase):
    def test_complete(self):
        task = self._enqueue()
        claimed = runtime_db.claim_next_extract_task()
        runtime_db.set_extract_staging(claimed["id"], "/tmp/stage/a")
        runtime_db.set_extract_progress(claimed["id"], files=5, bytes_done=1024)
        self.assertTrue(runtime_db.complete_extract_task(claimed["id"]))
        after = runtime_db.get_extract_task(task["id"])
        self.assertEqual(after["status"], runtime_db.EXTRACT_COMPLETED)
        self.assertEqual(after["uploaded_files"], 5)
        self.assertEqual(after["uploaded_bytes"], 1024)

    def test_postpone_then_terminal_by_attempts(self):
        task = self._enqueue()
        with mock.patch.object(config, "EXTRACT_MAX_ATTEMPTS", 2):
            claimed = runtime_db.claim_next_extract_task(now=1000)
            self.assertTrue(runtime_db.postpone_extract_task(
                claimed["id"], next_retry_at=1001, error="网络抖动"))
            second = runtime_db.claim_next_extract_task(now=1002)
            self.assertEqual(second["attempts"], 2)
            # 已到上限 → 终结
            self.assertTrue(runtime_db.terminate_extract_task(
                second["id"], "需密码"))
            after = runtime_db.get_extract_task(task["id"])
            self.assertEqual(after["status"], runtime_db.EXTRACT_TERMINAL)

    def test_manual_retry_pulls_back_terminal(self):
        task = self._enqueue()
        claimed = runtime_db.claim_next_extract_task()
        runtime_db.terminate_extract_task(claimed["id"], "需密码")
        self.assertTrue(runtime_db.retry_extract_task(claimed["id"]))
        after = runtime_db.get_extract_task(task["id"])
        self.assertEqual(after["status"], runtime_db.EXTRACT_PENDING)
        self.assertEqual(after["attempts"], 1)   # attempts 不清零（口径=领取次数）

    def test_release_and_delete(self):
        task = self._enqueue()
        claimed = runtime_db.claim_next_extract_task()
        self.assertTrue(runtime_db.release_extract_task(claimed["id"]))
        self.assertEqual(
            runtime_db.get_extract_task(task["id"])["status"],
            runtime_db.EXTRACT_PENDING)
        self.assertTrue(runtime_db.delete_extract_task(claimed["id"]))
        self.assertIsNone(runtime_db.get_extract_task(task["id"]))


if __name__ == "__main__":
    unittest.main()
