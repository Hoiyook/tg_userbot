"""Pawchive v8 表（runtime_db.py 的 pawchive_* 助手）的单元测试。

契约（pawchive_posts/pawchive_files，schema v8）：

1. UNIQUE(service, creator_id, post_id)：重复 plan 幂等——已存在帖子整体
   跳过，状态与文件进度原样保留；只有新帖子才进 PENDING。
2. claim 短事务 + 乐观锁（UPDATE…WHERE status=?），attempts 口径 =
   被领取执行的次数；lease_until 落库。
3. 终态流转只允许 PROCESSING → COMPLETED/MANUAL/FAILED（非法值拒绝、
   非 PROCESSING 行拒绝）。
4. 租约过期自愈：PROCESSING + lease_until<now → PENDING。
5. /paw retry：FAILED → PENDING 且文件级 FAILED 一并重投；PENDING 文件不动。
6. 文件级状态机：PENDING → SUBMITTED（带 chrome_task_id）→ DONE/FAILED；
   mark_pawchive_file_pending 只吃 SUBMITTED/FAILED。

不联网；DB 落在进程级临时目录，退出时回收（含 -wal/-shm）。
运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import atexit
import os
import shutil
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_paw_db_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402


def _post(post_id="111", files=None, ext_links=None, title="标题"):
    return {
        "post_id": post_id,
        "title": title,
        "published": "2026-09-13T04:57:26",
        "post_url": f"https://pawchive.pw/patreon/user/1/post/{post_id}",
        "subdir": f"Pawchive/A/2026-09-13_{post_id}_t",
        "files": files if files is not None else [
            {"url": "https://file.pawchive.pw/data/aa.mp4", "filename": "a.mp4"}],
        "ext_links": ext_links or [],
    }


class _PawDbTestCase(unittest.TestCase):
    """每个用例一个全新的 DB 文件（含 -wal/-shm），用完即删。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pawdb_", dir=_TMP)
        self.path = os.path.join(self.dir, "tg_userbot.db")
        self._p = mock.patch.object(config, "RUNTIME_DB_FILE", self.path)
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db(), "init_db 应成功")
        self.assertEqual(
            int(runtime_db.get_schema_meta("schema_version") or 0),
            config.RUNTIME_DB_SCHEMA_VERSION)

    def enqueue_two(self):
        """预置两条帖子（111 带 1 文件；222 带 2 文件 + 1 外链）。"""
        posts = [
            _post("111"),
            _post("222",
                  files=[{"url": "https://x/1.mp4", "filename": "1.mp4"},
                         {"url": "https://x/2.mp4", "filename": "2.mp4"}],
                  ext_links=[{"kind": "link", "domain": "mega.nz",
                              "url": "https://mega.nz/file/x#key",
                              "text": "MEGA"}]),
        ]
        created, skipped = runtime_db.enqueue_pawchive_posts(
            "patreon", "42", "TestCreator", posts)
        return created, skipped


class EnqueueIdempotencyTest(_PawDbTestCase):

    def test_enqueue_creates_posts_and_files(self):
        created, skipped = self.enqueue_two()
        self.assertEqual((created, skipped), (2, 0))
        files_222 = runtime_db.list_pawchive_files(
            runtime_db.list_pawchive_posts(status="PENDING")[1]["id"])
        self.assertEqual(len(files_222), 2)
        self.assertTrue(all(
            f["status"] == runtime_db.PAW_FILE_PENDING for f in files_222))

    def test_duplicate_plan_skips_existing_wholesale(self):
        """重复扫描：已存在帖子跳过——即使文件列表变了也不动它。"""
        self.enqueue_two()
        # 第二次：同一 (service, creator_id, post_id)，但文件多了一个
        again = [_post("111", files=[
            {"url": "https://file.pawchive.pw/data/aa.mp4", "filename": "a.mp4"},
            {"url": "https://file.pawchive.pw/data/bb.mp4", "filename": "b.mp4"}]),
            _post("333")]
        created, skipped = runtime_db.enqueue_pawchive_posts(
            "patreon", "42", "TestCreator", again)
        # 333 是新帖（created=1），111 已存在跳过（skipped=1）
        self.assertEqual((created, skipped), (1, 1))
        # 111 的文件还是 1 个（原有进度不动）
        p111 = next(p for p in runtime_db.list_pawchive_posts()
                    if p["post_id"] == "111")
        self.assertEqual(len(runtime_db.list_pawchive_files(p111["id"])), 1)

    def test_ext_links_json_roundtrip(self):
        self.enqueue_two()
        p222 = next(p for p in runtime_db.list_pawchive_posts()
                    if p["post_id"] == "222")
        self.assertEqual(p222["ext_count"], 1)
        self.assertEqual(p222["ext_links"][0]["domain"], "mega.nz")


class ClaimLeaseTest(_PawDbTestCase):

    def test_claim_marks_processing_with_lease(self):
        self.enqueue_two()
        post = runtime_db.claim_next_pawchive_post(now=1000)
        self.assertIsNotNone(post)
        self.assertEqual(post["status"], runtime_db.PAW_POST_PROCESSING)
        self.assertEqual(post["attempts"], 1)
        self.assertEqual(post["lease_until"], 1000 + config.PAWCHIVE_LEASE_SECONDS)
        # FIFO：先领 id 小的
        self.assertEqual(post["post_id"], "111")

    def test_claim_respects_next_retry_at(self):
        self.enqueue_two()
        p = runtime_db.claim_next_pawchive_post(now=1000)
        runtime_db.postpone_pawchive_post(p["id"], 9999, error="等一会")
        # 到期前领不到它，只能领 222
        nxt = runtime_db.claim_next_pawchive_post(now=5000)
        self.assertEqual(nxt["post_id"], "222")
        # 到期后可领
        third = runtime_db.claim_next_pawchive_post(now=10000)
        self.assertEqual(third["post_id"], "111")

    def test_pause_blocks_claim(self):
        """worker 暂停是 worker 层的 _PAUSED 标志，DB 层不感知（这里验证
        claim 本身只看状态与 next_retry_at）。"""
        self.enqueue_two()
        for _ in range(2):
            runtime_db.claim_next_pawchive_post(now=1000)
        self.assertIsNone(runtime_db.claim_next_pawchive_post(now=1000))

    def test_expired_lease_recovery(self):
        self.enqueue_two()
        p = runtime_db.claim_next_pawchive_post(now=1000)
        # 租约已过（now 远超 lease_until）
        recovered = runtime_db.recover_expired_pawchive_posts(now=100000)
        self.assertEqual(recovered, 1)
        again = runtime_db.get_pawchive_post(p["id"])
        self.assertEqual(again["status"], runtime_db.PAW_POST_PENDING)

    def test_renew_lease(self):
        self.enqueue_two()
        p = runtime_db.claim_next_pawchive_post(now=1000)
        self.assertTrue(runtime_db.renew_pawchive_lease(p["id"], now=5000))
        self.assertEqual(
            runtime_db.get_pawchive_post(p["id"])["lease_until"],
            5000 + config.PAWCHIVE_LEASE_SECONDS)


class FinalizeTest(_PawDbTestCase):

    def _claim(self):
        self.enqueue_two()
        return runtime_db.claim_next_pawchive_post(now=1000)

    def test_finalize_completed(self):
        p = self._claim()
        self.assertTrue(runtime_db.finalize_pawchive_post(
            p["id"], runtime_db.PAW_POST_COMPLETED))
        self.assertEqual(
            runtime_db.get_pawchive_post(p["id"])["status"],
            runtime_db.PAW_POST_COMPLETED)

    def test_finalize_rejects_non_processing(self):
        self.enqueue_two()
        # PENDING 行（没人 claim）直接终态 → 拒绝
        p = runtime_db.list_pawchive_posts(status="PENDING")[0]
        self.assertFalse(runtime_db.finalize_pawchive_post(
            p["id"], runtime_db.PAW_POST_COMPLETED))

    def test_finalize_rejects_illegal_status(self):
        p = self._claim()
        with self.assertRaises(ValueError):
            runtime_db.finalize_pawchive_post(p["id"], "PENDING")


class FileLifecycleTest(_PawDbTestCase):

    def _claimed_post_with_files(self):
        self.enqueue_two()
        post = runtime_db.claim_next_pawchive_post(now=1000)
        return post, runtime_db.list_pawchive_files(post["id"])

    def test_submit_done_failed_flow(self):
        post, files = self._claimed_post_with_files()
        f = files[0]
        runtime_db.mark_pawchive_file_submitted(f["id"], "task-abc", now=2000)
        f2 = runtime_db.list_pawchive_files(post["id"])[0]
        self.assertEqual(f2["status"], runtime_db.PAW_FILE_SUBMITTED)
        self.assertEqual(f2["chrome_task_id"], "task-abc")
        self.assertEqual(f2["attempts"], 1)
        runtime_db.mark_pawchive_file_done(f["id"], 12345, now=3000)
        f3 = runtime_db.list_pawchive_files(post["id"])[0]
        self.assertEqual(f3["status"], runtime_db.PAW_FILE_DONE)
        self.assertEqual(f3["size_bytes"], 12345)

    def test_requeue_only_from_submitted_or_failed(self):
        post, files = self._claimed_post_with_files()
        f = files[0]
        # PENDING → 重投不吃
        self.assertFalse(runtime_db.mark_pawchive_file_pending(f["id"]))
        # SUBMITTED → 可重投
        runtime_db.mark_pawchive_file_submitted(f["id"], "task-abc")
        self.assertTrue(runtime_db.mark_pawchive_file_pending(f["id"]))
        self.assertEqual(
            runtime_db.list_pawchive_files(post["id"])[0]["status"],
            runtime_db.PAW_FILE_PENDING)

    def test_retry_posts_requeues_failed_files_only(self):
        self.enqueue_two()
        p111 = next(p for p in runtime_db.list_pawchive_posts()
                    if p["post_id"] == "111")
        # 造 FAILED 终态 + 一个 FAILED 文件、一个 DONE 文件
        runtime_db.claim_next_pawchive_post(now=1000)
        files = runtime_db.list_pawchive_files(p111["id"])
        runtime_db.mark_pawchive_file_submitted(files[0]["id"], "t1")
        runtime_db.mark_pawchive_file_failed(files[0]["id"], "boom")
        runtime_db.finalize_pawchive_post(
            p111["id"], runtime_db.PAW_POST_FAILED, error="1/1 失败")
        count = runtime_db.retry_pawchive_posts()
        self.assertEqual(count, 1)
        self.assertEqual(
            runtime_db.get_pawchive_post(p111["id"])["status"],
            runtime_db.PAW_POST_PENDING)
        after = runtime_db.list_pawchive_files(p111["id"])[0]
        self.assertEqual(after["status"], runtime_db.PAW_FILE_PENDING)
        self.assertIsNone(after["chrome_task_id"])


class StatusCountsTest(_PawDbTestCase):

    def test_counts_by_status(self):
        self.enqueue_two()
        p = runtime_db.claim_next_pawchive_post(now=1000)
        runtime_db.finalize_pawchive_post(p["id"], runtime_db.PAW_POST_COMPLETED)
        counts = runtime_db.pawchive_status_counts()
        self.assertEqual(counts.get("PENDING"), 1)
        self.assertEqual(counts.get("COMPLETED"), 1)


if __name__ == "__main__":
    unittest.main()
