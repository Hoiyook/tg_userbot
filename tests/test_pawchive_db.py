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
from tg_userbot import pawchive  # noqa: E402
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
        requeued, skipped = runtime_db.retry_pawchive_posts()
        self.assertEqual((requeued, skipped), (1, 0))
        self.assertEqual(
            runtime_db.get_pawchive_post(p111["id"])["status"],
            runtime_db.PAW_POST_PENDING)
        after = runtime_db.list_pawchive_files(p111["id"])[0]
        self.assertEqual(after["status"], runtime_db.PAW_FILE_PENDING)
        self.assertIsNone(after["chrome_task_id"])


class RetrySkipsDeadLinksTest(_PawDbTestCase):
    """用户要求（2026-09-15）：/paw retry 不重投已确认死链，只重投可恢复数据。

    死链 = FAILED 且 error 以「站点缺文件(404)」开头。规则：
    - 帖子重投时只把可恢复文件放回 PENDING，死链文件保持 FAILED；
    - 纯死链帖（没有任何可恢复文件）整帖跳过、维持 FAILED；
    - 重投帖子的备注写明跳过的死链数（/paw status 可见）。
    """

    def _seed_failed(self, files):
        """入库 + 打成 FAILED 终态，返回帖子行。"""
        runtime_db.enqueue_pawchive_posts(
            "patreon", "42", "C", [{
                "post_id": "99", "title": "T", "published": "2026-01-01",
                "post_url": "u", "subdir": "Pawchive/C/99",
                "files": files, "ext_links": [],
            }])
        row = runtime_db.find_pawchive_posts_by_post_id("99")[0]
        runtime_db.claim_next_pawchive_post(now=1000)
        seed_by_url = {f["url"]: f.get("seed_error") or "网络超时"
                       for f in files}
        for f in runtime_db.list_pawchive_files(row["id"]):
            runtime_db.mark_pawchive_file_failed(
                f["id"], error=seed_by_url.get(f["url"], "网络超时"))
        runtime_db.finalize_pawchive_post(
            row["id"], runtime_db.PAW_POST_FAILED, error="下载失败")
        return row

    def test_dead_file_stays_failed_on_retry(self):
        row = self._seed_failed([
            {"url": "https://x/dead.jpg", "filename": "dead.jpg",
             "seed_error": runtime_db.PAW_DEAD_LINK_MARK + " HTTP 404"},
            {"url": "https://x/live.mp4", "filename": "live.mp4",
             "seed_error": "下载超时"},
        ])
        requeued, skipped = runtime_db.retry_pawchive_posts()
        self.assertEqual((requeued, skipped), (1, 0))
        self.assertEqual(
            runtime_db.get_pawchive_post(row["id"])["status"],
            runtime_db.PAW_POST_PENDING)
        by_url = {f["url"]: f for f in runtime_db.list_pawchive_files(row["id"])}
        # 死链保持 FAILED（不进预检队列空转）；可恢复的放回 PENDING
        self.assertEqual(by_url["https://x/dead.jpg"]["status"],
                         runtime_db.PAW_FILE_FAILED)
        self.assertIn("站点缺文件", by_url["https://x/dead.jpg"]["error"])
        self.assertEqual(by_url["https://x/live.mp4"]["status"],
                         runtime_db.PAW_FILE_PENDING)
        # 备注写明跳过的死链数
        self.assertIn("跳过 1 个已确认死链",
                      runtime_db.get_pawchive_post(row["id"])["last_error"])

    def test_all_dead_post_skipped_entirely(self):
        row = self._seed_failed([
            {"url": "https://x/d1.jpg", "filename": "d1.jpg",
             "seed_error": runtime_db.PAW_DEAD_LINK_MARK + " HTTP 404"},
            {"url": "https://x/d2.jpg", "filename": "d2.jpg",
             "seed_error": runtime_db.PAW_DEAD_LINK_MARK + " HTTP 410"},
        ])
        requeued, skipped = runtime_db.retry_pawchive_posts()
        self.assertEqual((requeued, skipped), (0, 1))
        self.assertEqual(
            runtime_db.get_pawchive_post(row["id"])["status"],
            runtime_db.PAW_POST_FAILED)   # 维持失败，不空转
        self.assertTrue(all(
            f["status"] == runtime_db.PAW_FILE_FAILED
            for f in runtime_db.list_pawchive_files(row["id"])))


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


class ManualDoneTest(_PawDbTestCase):
    """外链人工处理闭环：MANUAL → COMPLETED 标记（幂等、防误标）。"""

    def _enqueue_manual(self):
        """预置一帖：1 直链 + 2 外链，按 worker 真实路径推到 MANUAL。

        finalize 只接受 PROCESSING（终态流转守卫），先 claim 再 finalize。"""
        created, _ = self.enqueue_two()          # 111/222 两条 PENDING
        rows = runtime_db.list_pawchive_posts(limit=10)
        target = next(r for r in rows if r["post_id"] == "222")
        first = runtime_db.claim_next_pawchive_post()   # id 序先领 111
        assert first and first["id"] != target["id"]
        claimed = runtime_db.claim_next_pawchive_post()  # 再领 222
        assert claimed and claimed["id"] == target["id"]
        runtime_db.finalize_pawchive_post(
            target["id"], runtime_db.PAW_POST_MANUAL)
        return target

    def test_complete_manual_post(self):
        target = self._enqueue_manual()
        self.assertTrue(
            runtime_db.complete_pawchive_manual_post(target["id"]))
        row = runtime_db.find_pawchive_posts_by_post_id("222")[0]
        self.assertEqual(row["status"], runtime_db.PAW_POST_COMPLETED)
        self.assertIsNotNone(row["completed_at"])

    def test_double_complete_is_noop(self):
        """重复标记：第二次返回 False（幂等友好）。"""
        target = self._enqueue_manual()
        self.assertTrue(runtime_db.complete_pawchive_manual_post(target["id"]))
        self.assertFalse(
            runtime_db.complete_pawchive_manual_post(target["id"]))

    def test_non_manual_rejected(self):
        """PENDING/PROCESSING 帖子不可直接标完成（防误触跳过下载）。"""
        created, _ = self.enqueue_two()
        rows = runtime_db.list_pawchive_posts(limit=10)
        self.assertFalse(
            runtime_db.complete_pawchive_manual_post(rows[0]["id"]))

    def test_manual_view_has_buttons_and_date(self):
        """manual_view：日期/外链进正文；每帖 ✅ + 🔗 按钮（≤64 字节）。"""
        from tg_userbot import pawchive
        target = self._enqueue_manual()
        text, buttons = pawchive.manual_view()
        self.assertIn("TestCreator", text)
        self.assertIn("2026-09-13", text)        # 日期
        self.assertIn("https://mega.nz/file/x#key", text)
        flat = [b for row in buttons for b in row]
        done = [b for b in flat if "✅" in b.text]
        self.assertEqual(len(done), 1)           # 一帖一个完成按钮
        self.assertLessEqual(len(done[0].data), 64)
        self.assertTrue(any(getattr(b, "url", None) for b in flat))  # 🔗 原帖

    def test_manual_view_empty(self):
        from tg_userbot import pawchive
        text, buttons = pawchive.manual_view()
        self.assertIn("没有待人工处理", text)
        self.assertEqual(buttons, [])

    def test_mark_manual_done_by_arg(self):
        """pawchive.mark_manual_done：#id/裸 id 均可；已完成的给幂等提示。"""
        from tg_userbot import pawchive
        target = self._enqueue_manual()
        self.assertIn("✅", pawchive.mark_manual_done(f"#{target['id']}"))
        self.assertIn("已经", pawchive.mark_manual_done(f"{target['id']}"))
        self.assertIn("❌", pawchive.mark_manual_done("999999"))


class ManualPanelFlowTest(_PawDbTestCase):
    """Pawchive 面板的待人工流程：paw_manual/paw_done 分支的（文本, 按钮）。"""

    def _enqueue_manual(self):
        created, _ = self.enqueue_two()
        rows = runtime_db.list_pawchive_posts(limit=10)
        target = next(r for r in rows if r["post_id"] == "222")
        first = runtime_db.claim_next_pawchive_post()
        assert first and first["id"] != target["id"]
        claimed = runtime_db.claim_next_pawchive_post()
        assert claimed and claimed["id"] == target["id"]
        runtime_db.finalize_pawchive_post(
            target["id"], runtime_db.PAW_POST_MANUAL)
        return target

    def test_manual_view_full_has_menu_buttons(self):
        from tg_userbot import pawchive
        self._enqueue_manual()
        text, buttons = pawchive.manual_view_full()
        self.assertIn("TestCreator", text)
        flat = [b for row in buttons for b in row]
        self.assertTrue(any("✅" in b.text for b in flat))
        # 底部保留 Pawchive 面板按钮（返回面板/扫描作者等导航）
        self.assertGreater(len(buttons), 2)

    def test_manual_done_reply_edit_in_place(self):
        """面板 ✅ 点击 → 标记完成 + 返回刷新后的视图（原地 edit 语义）。"""
        from tg_userbot import pawchive
        target = self._enqueue_manual()
        text, buttons = pawchive.manual_done_reply(str(target["id"]))
        self.assertIn("✅", text)
        self.assertIn("没有待人工处理", text)     # 唯一的 MANUAL 帖已标完
        # 空态也保留面板按钮（用户还能点回其他视图）
        self.assertTrue(buttons)


class ArchiveFailedTest(_PawDbTestCase):
    """#3：FAILED 死链归档 —— /paw archive failed 的存储层。

    2026-09-17 语义收紧：**只归档纯死链帖**——含可恢复文件（PENDING，或
    FAILED 且非死链）的帖子保持 FAILED，归档不得埋掉数据（用户决策）。
    resurrect_archived_recoverable 是对历史误归档的一次性补救。
    """

    def _seed_mixed(self):
        """111→FAILED（死链），222→PENDING；archive 只归 111。"""
        created, _ = self.enqueue_two()
        rows = runtime_db.list_pawchive_posts(limit=10)
        target = next(r for r in rows if r["post_id"] == "111")
        first = runtime_db.claim_next_pawchive_post()
        assert first and first["id"] == target["id"]
        files = runtime_db.list_pawchive_files(target["id"])
        runtime_db.mark_pawchive_file_failed(
            files[0]["id"], error=runtime_db.PAW_DEAD_LINK_MARK + " HTTP 404")
        runtime_db.finalize_pawchive_post(
            target["id"], runtime_db.PAW_POST_FAILED, error="死链")
        return rows

    def test_archive_moves_only_failed(self):
        rows = self._seed_mixed()
        target = next(r for r in rows if r["post_id"] == "111")
        other = next(r for r in rows if r["post_id"] == "222")
        n, kept = runtime_db.archive_pawchive_failed()
        self.assertEqual((n, kept), (1, 0))
        statuses = {r["id"]: r["status"] for r in
                    runtime_db.list_pawchive_posts(limit=10)}
        self.assertEqual(statuses[target["id"]], "ARCHIVED")
        self.assertEqual(statuses[other["id"]], "PENDING")

    def test_archive_idempotent(self):
        self._seed_mixed()
        self.assertEqual(runtime_db.archive_pawchive_failed(), (1, 0))
        self.assertEqual(runtime_db.archive_pawchive_failed(), (0, 0))

    def test_archived_excluded_from_status_counts_of_active(self):
        """ARCHIVED 是独立状态：status_counts 自然分组，面板标签需覆盖。"""
        from tg_userbot import pawchive_worker
        self.assertIn(runtime_db.PAW_POST_ARCHIVED, pawchive_worker._STATUS_LABELS)
        from tg_userbot import pawchive
        self.assertIn(runtime_db.PAW_POST_ARCHIVED, pawchive._STATUS_LABELS)

    def test_mixed_post_kept_from_archive(self):
        """帖内含可恢复文件（FAILED 非死链）→ 整帖保持 FAILED 不归档。"""
        runtime_db.enqueue_pawchive_posts("patreon", "42", "C", [{
            "post_id": "99", "title": "混帖", "published": "2026-01-01",
            "post_url": "u", "subdir": "Pawchive/C/99",
            "files": [
                {"url": "https://x/dead.jpg", "filename": "dead.jpg"},
                {"url": "https://x/live.mp4", "filename": "live.mp4"},
            ],
            "ext_links": [],
        }])
        row = runtime_db.find_pawchive_posts_by_post_id("99")[0]
        runtime_db.claim_next_pawchive_post(now=1000)
        for f in runtime_db.list_pawchive_files(row["id"]):
            err = (runtime_db.PAW_DEAD_LINK_MARK + " HTTP 404"
                   if "dead" in f["url"] else "网络超时")
            runtime_db.mark_pawchive_file_failed(f["id"], error=err)
        runtime_db.finalize_pawchive_post(
            row["id"], runtime_db.PAW_POST_FAILED, error="混帖失败")
        n, kept = runtime_db.archive_pawchive_failed()
        self.assertEqual(kept, 1)
        self.assertEqual(
            runtime_db.get_pawchive_post(row["id"])["status"],
            runtime_db.PAW_POST_FAILED)
        # 重投可恢复文件：只有 live.mp4 回 PENDING，死链不动
        requeued_files = runtime_db.retry_pawchive_posts(row_ids=[row["id"]])
        self.assertEqual(requeued_files, (1, 0))
        by_url = {f["url"]: f["status"]
                  for f in runtime_db.list_pawchive_files(row["id"])}
        self.assertEqual(by_url["https://x/dead.jpg"],
                         runtime_db.PAW_FILE_FAILED)
        self.assertEqual(by_url["https://x/live.mp4"],
                         runtime_db.PAW_FILE_PENDING)


class ResurrectArchivedTest(_PawDbTestCase):
    """resurrect_archived_recoverable：对历史误归档的一次性补救。"""

    def _seed_archived_mixed(self):
        """造一个已归档但埋了可恢复文件的帖子（旧 bug 的产物形态）。"""
        runtime_db.enqueue_pawchive_posts("patreon", "42", "C", [{
            "post_id": "77", "title": "误归档帖", "published": "2026-01-01",
            "post_url": "u", "subdir": "Pawchive/C/77",
            "files": [
                {"url": "https://x/dead.jpg", "filename": "dead.jpg"},
                {"url": "https://x/resume.zip", "filename": "resume.zip"},
            ],
            "ext_links": [],
        }])
        row = runtime_db.find_pawchive_posts_by_post_id("77")[0]
        runtime_db.claim_next_pawchive_post(now=1000)
        for f in runtime_db.list_pawchive_files(row["id"]):
            err = (runtime_db.PAW_DEAD_LINK_MARK + " HTTP 404"
                   if "dead" in f["url"] else "断流中断")
            runtime_db.mark_pawchive_file_failed(f["id"], error=err)
        runtime_db.finalize_pawchive_post(
            row["id"], runtime_db.PAW_POST_FAILED, error="失败")
        # 模拟旧 bug：整帖被归档（含可恢复文件）
        runtime_db._write(
            lambda conn: conn.execute(
                "UPDATE pawchive_posts SET status='ARCHIVED' WHERE id=?",
                (row["id"],)),
            "模拟旧 bug 归档")
        return row

    def test_resurrect_requeues_recoverable_only(self):
        row = self._seed_archived_mixed()
        posts, files = runtime_db.resurrect_archived_recoverable()
        self.assertEqual((posts, files), (1, 1))
        self.assertEqual(
            runtime_db.get_pawchive_post(row["id"])["status"],
            runtime_db.PAW_POST_PENDING)
        by_url = {f["url"]: f["status"]
                  for f in runtime_db.list_pawchive_files(row["id"])}
        self.assertEqual(by_url["https://x/resume.zip"],
                         runtime_db.PAW_FILE_PENDING)
        self.assertEqual(by_url["https://x/dead.jpg"],
                         runtime_db.PAW_FILE_FAILED)
        # 幂等：再跑一遍无事发生
        self.assertEqual(runtime_db.resurrect_archived_recoverable(), (0, 0))

    def test_pure_dead_archived_post_untouched(self):
        """纯死链的归档帖保持 ARCHIVED（不被误救）。"""
        runtime_db.enqueue_pawchive_posts("patreon", "42", "C", [{
            "post_id": "88", "title": "纯死链", "published": "2026-01-01",
            "post_url": "u", "subdir": "Pawchive/C/88",
            "files": [{"url": "https://x/d.mp4", "filename": "d.mp4"}],
            "ext_links": [],
        }])
        row = runtime_db.find_pawchive_posts_by_post_id("88")[0]
        runtime_db.claim_next_pawchive_post(now=1000)
        for f in runtime_db.list_pawchive_files(row["id"]):
            runtime_db.mark_pawchive_file_failed(
                f["id"], error=runtime_db.PAW_DEAD_LINK_MARK + " HTTP 404")
        runtime_db.finalize_pawchive_post(
            row["id"], runtime_db.PAW_POST_FAILED, error="死链")
        runtime_db.archive_pawchive_failed()
        self.assertEqual(
            runtime_db.get_pawchive_post(row["id"])["status"],
            runtime_db.PAW_POST_ARCHIVED)
        self.assertEqual(runtime_db.resurrect_archived_recoverable(), (0, 0))



class PawArchiveCommandTest(unittest.TestCase):
    """/paw archive 子命令解析。"""

    def test_parse_archive(self):
        from tg_userbot import pawchive
        self.assertEqual(pawchive.parse_paw_command("/paw archive"),
                         ("archive", None))
        self.assertEqual(pawchive.parse_paw_command("/paw archive failed"),
                         ("archive", "failed"))


class ManualExportTest(_PawDbTestCase):
    """A1：/paw manual export —— 待处理外链的批量工作清单。"""

    def _seed_two_authors(self):
        """两作者各一帖纯外链，全部推到 MANUAL。"""
        p1 = {"post_id": "11", "title": "帖A", "published": "2026-09-15",
              "post_url": "https://pawchive.pw/x/11", "subdir": "s1",
              "files": [], "ext_links": [
                  {"kind": "link", "domain": "mega.nz",
                   "url": "https://mega.nz/file/A1", "text": ""}]}
        p2 = {"post_id": "12", "title": "帖B", "published": "2026-09-16",
              "post_url": "https://pawchive.pw/x/12", "subdir": "s2",
              "files": [], "ext_links": [
                  {"kind": "link", "domain": "krakenfiles.com",
                   "url": "https://krakenfiles.com/B1", "text": ""},
                  {"kind": "link", "domain": "mega.nz",
                   "url": "https://mega.nz/file/B2", "text": ""}]}
        runtime_db.enqueue_pawchive_posts("patreon", "7", "作者甲", [p1])
        runtime_db.enqueue_pawchive_posts("patreon", "8", "作者乙", [p2])
        for _ in range(2):
            claimed = runtime_db.claim_next_pawchive_post()
            runtime_db.finalize_pawchive_post(
                claimed["id"], runtime_db.PAW_POST_MANUAL)

    def test_export_contains_all_links_grouped(self):
        from tg_userbot import pawchive
        self._seed_two_authors()
        text = pawchive.manual_export_text()
        self.assertIn("作者甲", text)
        self.assertIn("作者乙", text)
        self.assertIn("https://mega.nz/file/A1", text)
        self.assertIn("https://krakenfiles.com/B1", text)
        self.assertIn("#", text)                     # 帖子行 id（/paw done 用）
        self.assertIn("原帖", text)

    def test_export_empty(self):
        from tg_userbot import pawchive
        self.assertIn("没有待处理", pawchive.manual_export_text())

    def test_done_range_support(self):
        """A2：/paw done 105-120 区间批量标记。"""
        from tg_userbot import pawchive
        self._seed_two_authors()
        ids = sorted(r["id"] for r in runtime_db.list_pawchive_posts(
            status=runtime_db.PAW_POST_MANUAL))
        lo, hi = ids[0], ids[-1]
        reply = pawchive.mark_manual_done(f"{lo}-{hi}")
        self.assertIn("✅", reply)
        remaining = runtime_db.list_pawchive_posts(
            status=runtime_db.PAW_POST_MANUAL)
        self.assertEqual(len(remaining), 0)

    def test_done_by_author(self):
        """A2：/paw done @作者名 —— 该作者全部 MANUAL 帖批量标记。"""
        from tg_userbot import pawchive
        self._seed_two_authors()
        reply = pawchive.mark_manual_done("作者甲")
        self.assertIn("✅", reply)
        left = runtime_db.list_pawchive_posts(status=runtime_db.PAW_POST_MANUAL)
        self.assertEqual(len(left), 1)               # 只剩作者乙
        self.assertEqual(left[0]["creator_name"], "作者乙")


class AttCommandTest(_PawDbTestCase):
    """/paw att <URL|帖子ID|行id>：查帖子的全部附件与外链及状态。"""

    def _seed_manual_with_files(self):
        """1 帖：2 附件（1 DONE 1 PENDING）+ 2 外链，推到 MANUAL。"""
        post = {"post_id": "169052124", "title": "小提丰摇",
                "published": "2026-09-09", "post_url": "https://pawchive.pw/x/1",
                "subdir": "s", "ext_links": [
                    {"kind": "link", "domain": "mega.nz",
                     "url": "https://mega.nz/folder/x#K", "text": ""},
                    {"kind": "embed", "domain": "youtube.com",
                     "url": "https://youtube.com/w?v=1", "text": ""}],
                "files": [
                    {"url": "https://file.pawchive.pw/data/1",
                     "filename": "主视频.mp4"},
                    {"url": "https://file.pawchive.pw/data/2",
                     "filename": "图.png"}]}
        runtime_db.enqueue_pawchive_posts("patreon", "42", "作者A", [post])
        claimed = runtime_db.claim_next_pawchive_post()
        runtime_db.finalize_pawchive_post(
            claimed["id"], runtime_db.PAW_POST_MANUAL)
        # 文件：一个标 DONE
        files = runtime_db.list_pawchive_files(claimed["id"])
        for f in files:
            if f["filename"] == "主视频.mp4":
                runtime_db.mark_pawchive_file_done(f["id"], size_bytes=123)
        return claimed

    def test_att_by_post_id(self):
        self._seed_manual_with_files()
        text = pawchive.att_text("169052124")
        self.assertIn("作者A", text)
        self.assertIn("MANUAL", text)
        self.assertIn("✅ 主视频.mp4", text)
        self.assertIn("⏳ 图.png", text)
        self.assertIn("mega.nz/folder/x#K", text)
        self.assertNotIn("youtube.com", text)   # YouTube 预览外链已被噪音过滤

    def test_att_by_url(self):
        self._seed_manual_with_files()
        text = pawchive.att_text("https://pawchive.pw/patreon/user/42/post/169052124")
        self.assertIn("作者A", text)

    def test_att_by_row_id(self):
        from tg_userbot import runtime_db as rd
        self._seed_manual_with_files()
        rid = rd.list_pawchive_posts(limit=5)[0]["id"]
        self.assertIn("作者A", pawchive.att_text(str(rid)))

    def test_att_not_found(self):
        self.assertIn("❌", pawchive.att_text("88888888"))

    def test_att_no_files_shows_ext_only(self):
        """纯外链帖：显示外链与「无附件」。"""
        post = {"post_id": "55", "title": "纯外链", "published": "2026-09-19",
                "post_url": "https://pawchive.pw/x/55", "subdir": "s",
                "files": [], "ext_links": [
                    {"kind": "link", "domain": "mega.nz",
                     "url": "https://mega.nz/folder/z#K", "text": ""}]}
        runtime_db.enqueue_pawchive_posts("patreon", "42", "作者B", [post])
        claimed = runtime_db.claim_next_pawchive_post()
        runtime_db.finalize_pawchive_post(
            claimed["id"], runtime_db.PAW_POST_MANUAL)
        text = pawchive.att_text("55")
        self.assertIn("无附件", text)
        self.assertIn("mega.nz/folder/z#K", text)
