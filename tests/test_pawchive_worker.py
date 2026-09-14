"""Pawchive Worker（pawchive_worker.py）的单元测试。

Chrome Agent 层全部 monkeypatch（agent_running / wait_agent_up / add_request /
mark_notified / chrome_agent.load_tasks / new_task_id），只测 worker 自己的
纪律：对账吸收终态、失联重投、终态流转（COMPLETED/MANUAL/FAILED）、磁盘
保护、暂停开关、通知粒度（per-file 屏蔽、per-post 通知）。
运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import asyncio
import atexit
import os
import shutil
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_paw_worker_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import chrome_agent  # noqa: E402
from tg_userbot import chrome_client  # noqa: E402
from tg_userbot import config  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402
from tg_userbot import state  # noqa: E402
from tg_userbot import pawchive_worker as worker  # noqa: E402


def _post(post_id="1", files=None, ext_links=None):
    return {
        "post_id": post_id,
        "title": f"T{post_id}",
        "published": "2026-09-13T04:57:26",
        "post_url": f"https://pawchive.pw/patreon/user/42/post/{post_id}",
        "subdir": f"Pawchive/C/2026-09-13_{post_id}_t",
        "files": files if files is not None else [
            {"url": "https://file.pawchive.pw/data/a.mp4",
             "filename": "a.mp4"}],
        "ext_links": ext_links or [],
    }


def _task(task_id, status, size=None, error=None):
    return {"task_id": task_id, "status": status, "size_bytes": size,
            "error": error, "filename": "x.mp4"}


class _WorkerDbTestCase(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pawwk_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        # worker 全局态复位
        worker.resume()
        worker.release_inflight()
        self.addCleanup(worker.release_inflight)
        # Chrome 层打桩
        self._task_seq = [0]

        def _new_task_id():
            self._task_seq[0] += 1
            return f"task-{self._task_seq[0]:03d}"

        self._submitted = []          # (task_id, url, subdir)
        self._notified = []           # task_id（屏蔽逐文件通知的标记）
        self._notifies = []           # worker 发出的帖子级通知
        self._cancels = []            # request_cancel 捕获
        self._tasks_fixture = []      # chrome_tasks.json 的替身

        patches = [
            mock.patch.object(chrome_client, "agent_running", return_value=True),
            mock.patch.object(chrome_client, "spawn_agent"),
            mock.patch.object(chrome_client, "add_request", side_effect=(
                lambda tid, url, **kw: self._submitted.append(
                    (tid, url, kw.get("download_subdir"))))),
            mock.patch.object(chrome_client, "mark_notified",
                              side_effect=lambda tid: self._notified.append(tid)),
            mock.patch.object(chrome_client, "request_cancel",
                              side_effect=lambda tid: (
                                  self._cancels.append(tid), (True, "ok"))[1]),
            mock.patch.object(chrome_client, "wait_agent_up",
                              side_effect=self._wait_agent_up),
            mock.patch.object(chrome_agent, "new_task_id",
                              side_effect=_new_task_id),
            mock.patch.object(chrome_agent, "load_tasks",
                              side_effect=lambda path=None: list(
                                  self._tasks_fixture)),
            mock.patch.object(worker, "_disk_free_gb", return_value=100.0),
            mock.patch.object(worker, "_head_status", return_value=200),
            mock.patch.object(worker.notify, "notify_user",
                              side_effect=self._capture_notify),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # notify_user / wait_agent_up 是协程
        self._notify_calls = self._notifies
        state.MY_ID = 12345
        self.addCleanup(setattr, state, "MY_ID", None)

    async def _wait_agent_up(self, timeout=15):
        return True

    async def _capture_notify(self, text):
        self._notifies.append(text)

    def _seed_and_claim(self, post=None):
        """入库 + 领取，返回 (post_row, files)。"""
        created, _ = runtime_db.enqueue_pawchive_posts(
            "patreon", "42", "Creator", [post or _post()])
        claimed = runtime_db.claim_next_pawchive_post(now=1000)
        self.assertIsNotNone(claimed)
        files = runtime_db.list_pawchive_files(claimed["id"])
        return claimed, files


class ReconcileSubmitTest(_WorkerDbTestCase):

    async def test_submit_pending_with_subdir_and_notify_suppressed(self):
        post, files = self._seed_and_claim()
        submitted = worker._submit_pending(post, files)
        self.assertEqual(submitted, 1)
        tid, url, subdir = self._submitted[0]
        self.assertEqual(url, "https://file.pawchive.pw/data/a.mp4")
        self.assertEqual(subdir, post["subdir"])
        # 逐文件结果通知必须被屏蔽（per-post 通知是 worker 的职责）
        self.assertEqual(self._notified, [tid])
        row = runtime_db.list_pawchive_files(post["id"])[0]
        self.assertEqual(row["status"], runtime_db.PAW_FILE_SUBMITTED)
        self.assertEqual(row["chrome_task_id"], tid)

    async def test_absorb_terminal_and_requeue_missing(self):
        post, files = self._seed_and_claim(_post("1", files=[
            {"url": "https://x/1.mp4", "filename": "1.mp4"},
            {"url": "https://x/2.mp4", "filename": "2.mp4"},
            {"url": "https://x/3.mp4", "filename": "3.mp4"},
        ]))
        # 预置：1 已 SUBMITTED+终态 SUCCESS；2 已 SUBMITTED+task 失联；3 PENDING
        runtime_db.mark_pawchive_file_submitted(files[0]["id"], "task-done")
        runtime_db.mark_pawchive_file_submitted(files[1]["id"], "task-gone")
        self._tasks_fixture = [_task("task-done", "SUCCESS", size=123)]
        # 重新读 DB 快照（生产路径 process_post 就是刚读的；本地旧 dict 的
        # status 还是 PENDING，会让对账分支走错）
        files = runtime_db.list_pawchive_files(post["id"])
        worker._reconcile_submitted(post, files)
        submitted = worker._submit_pending(post, files)
        rows = runtime_db.list_pawchive_files(post["id"])
        by_url = {r["url"]: r for r in rows}
        # 终态就地吸收
        self.assertEqual(by_url["https://x/1.mp4"]["status"],
                         runtime_db.PAW_FILE_DONE)
        self.assertEqual(by_url["https://x/1.mp4"]["size_bytes"], 123)
        # 失联重投 → 本轮重新提交
        self.assertEqual(by_url["https://x/2.mp4"]["status"],
                         runtime_db.PAW_FILE_SUBMITTED)
        # 3 PENDING → 本轮提交
        self.assertEqual(submitted, 2)


class WaitForTerminalTest(_WorkerDbTestCase):

    async def test_wait_absorbs_and_renews_lease(self):
        post, files = self._seed_and_claim()
        worker._submit_pending(post, files)
        tid = files[0]["chrome_task_id"]
        # 首轮轮询后任务才终态：用 side_effect 模拟「第一次没有、第二次有」
        states = [[], [_task(tid, "SUCCESS", size=999)]]

        async def _fake_to_thread(fn, path=None):
            return list(states.pop(0) if states else
                        [_task(tid, "SUCCESS", size=999)])

        with mock.patch.object(worker.asyncio, "sleep", new=_noop_sleep), \
                mock.patch.object(worker.asyncio, "to_thread",
                                  side_effect=_fake_to_thread):
            await worker._wait_files_terminal(post, files)
        row = runtime_db.list_pawchive_files(post["id"])[0]
        self.assertEqual(row["status"], runtime_db.PAW_FILE_DONE)
        # 租约被续过（至少一次 renew 调用成功）
        self.assertGreater(
            runtime_db.get_pawchive_post(post["id"])["lease_until"], 1000)


async def _noop_sleep(_):
    return None


class FinalizeTest(_WorkerDbTestCase):

    async def test_completed_no_notify(self):
        post, files = self._seed_and_claim()
        for f in files:
            f["status"] = runtime_db.PAW_FILE_DONE
        await worker._finalize(post, files)
        self.assertEqual(
            runtime_db.get_pawchive_post(post["id"])["status"],
            runtime_db.PAW_POST_COMPLETED)
        self.assertEqual(self._notifies, [])

    async def test_manual_with_ext_links_notifies(self):
        post, files = self._seed_and_claim(_post(
            "2", ext_links=[{"kind": "link", "domain": "mega.nz",
                             "url": "https://mega.nz/x#k", "text": "M"}]))
        for f in files:
            f["status"] = runtime_db.PAW_FILE_DONE
        await worker._finalize(post, files)
        self.assertEqual(
            runtime_db.get_pawchive_post(post["id"])["status"],
            runtime_db.PAW_POST_MANUAL)
        self.assertEqual(len(self._notifies), 1)
        self.assertIn("https://mega.nz/x#k", self._notifies[0])
        self.assertIn(post["post_url"], self._notifies[0])

    async def test_failed_notifies_with_retry_hint(self):
        post, files = self._seed_and_claim()
        for f in files:
            f["status"] = runtime_db.PAW_FILE_FAILED
            f["error"] = "下载超时"
        await worker._finalize(post, files)
        self.assertEqual(
            runtime_db.get_pawchive_post(post["id"])["status"],
            runtime_db.PAW_POST_FAILED)
        self.assertIn(f"/paw retry {post['id']}", self._notifies[0])


class ProcessPostTest(_WorkerDbTestCase):

    async def test_full_flow_manual(self):
        post, _ = self._seed_and_claim(_post(
            "5", ext_links=[{"kind": "link", "domain": "mega.nz",
                             "url": "https://mega.nz/x#k", "text": "M"}]))
        self._tasks_fixture = []   # 提交后 load_tasks 不含终态 → 先等
        # 直接把终态放进去：提交的 task id 是 task-001
        self._tasks_fixture = [_task("task-001", "SUCCESS", size=1)]
        with mock.patch.object(worker.asyncio, "sleep", new=_noop_sleep):
            ok = await worker.process_post(post)
        self.assertTrue(ok)
        self.assertEqual(
            runtime_db.get_pawchive_post(post["id"])["status"],
            runtime_db.PAW_POST_MANUAL)
        self.assertEqual(len(self._submitted), 1)

    async def test_disk_guard_pauses_and_releases(self):
        post, _ = self._seed_and_claim()
        with mock.patch.object(worker, "_disk_free_gb", return_value=0.5), \
                mock.patch.object(worker.asyncio, "sleep", new=_noop_sleep):
            ok = await worker.process_post(post)
        self.assertFalse(ok)
        self.assertTrue(worker.paused())
        self.assertIn("磁盘", worker.worker_state_text())
        self.assertEqual(
            runtime_db.get_pawchive_post(post["id"])["status"],
            runtime_db.PAW_POST_PENDING)
        self.assertEqual(self._submitted, [])
        # resume 恢复
        worker.resume()
        self.assertFalse(worker.paused())

    async def test_agent_down_postpones(self):
        post, _ = self._seed_and_claim()
        with mock.patch.object(chrome_client, "agent_running",
                               return_value=False), \
                mock.patch.object(chrome_client, "wait_agent_up",
                                  return_value=False), \
                mock.patch.object(worker.asyncio, "sleep", new=_noop_sleep):
            ok = await worker.process_post(post)
        self.assertFalse(ok)
        self.assertEqual(
            runtime_db.get_pawchive_post(post["id"])["status"],
            runtime_db.PAW_POST_PENDING)
        self.assertGreater(
            runtime_db.get_pawchive_post(post["id"])["next_retry_at"] or 0, 0)
        self.assertEqual(self._submitted, [])


class PrecheckTest(_WorkerDbTestCase):

    async def test_dead_link_failed_without_chrome(self):
        """404 死链：预检直接标 FAILED，不进 Chrome；活的照常提交。"""
        post, _ = self._seed_and_claim(_post(
            "11", files=[
                {"url": "https://x/dead.jpg", "filename": "dead.jpg"},
                {"url": "https://x/alive.mp4", "filename": "alive.mp4"},
            ]))
        self._tasks_fixture = [_task("task-001", "SUCCESS", size=9)]
        codes = {"https://x/dead.jpg": 404, "https://x/alive.mp4": 200}
        with mock.patch.object(worker, "_head_status",
                               side_effect=lambda u: codes[u]), \
                mock.patch.object(worker.asyncio, "sleep", new=_noop_sleep):
            ok = await worker.process_post(post)
        self.assertTrue(ok)
        rows = {r["url"]: r for r in runtime_db.list_pawchive_files(post["id"])}
        self.assertEqual(rows["https://x/dead.jpg"]["status"],
                         runtime_db.PAW_FILE_FAILED)
        self.assertIn("站点缺文件", rows["https://x/dead.jpg"]["error"])
        self.assertIsNone(rows["https://x/dead.jpg"]["chrome_task_id"])
        self.assertEqual(rows["https://x/alive.mp4"]["status"],
                         runtime_db.PAW_FILE_DONE)
        # 死链+活链混合 → 帖子 FAILED 且通知
        self.assertEqual(
            runtime_db.get_pawchive_post(post["id"])["status"],
            runtime_db.PAW_POST_FAILED)
        self.assertEqual(len(self._notifies), 1)
        self.assertEqual(len(self._submitted), 1)

    async def test_head_error_treated_alive(self):
        """HEAD 网络异常不预判——交给 Chrome 正常走。"""
        f = {"id": 1, "url": "https://x/a.mp4",
             "status": runtime_db.PAW_FILE_PENDING}
        with mock.patch.object(worker, "_head_status", return_value=None):
            dead = await asyncio.to_thread(worker._head_dead_ids, [(f, f["url"])])
        self.assertEqual(dead, set())

    async def test_all_dead_finalize_silent(self):
        """全部死链：帖子 FAILED 且不发通知（重试也一样 404，不可行动）。"""
        post, files = self._seed_and_claim()
        for f in files:
            f["status"] = runtime_db.PAW_FILE_FAILED
            f["error"] = worker._MISSING_MARK + " HTTP 404"
        await worker._finalize(post, files)
        self.assertEqual(
            runtime_db.get_pawchive_post(post["id"])["status"],
            runtime_db.PAW_POST_FAILED)
        self.assertEqual(self._notifies, [])

    async def test_mixed_dead_still_notifies(self):
        """部分死链部分成功：帖子 FAILED 且通知（用户该知道丢了内容）。"""
        post, _ = self._seed_and_claim(_post("12", files=[
            {"url": "https://x/1.mp4", "filename": "1.mp4"},
            {"url": "https://x/2.mp4", "filename": "2.mp4"},
        ]))
        files = runtime_db.list_pawchive_files(post["id"])
        files[0]["status"] = runtime_db.PAW_FILE_DONE
        files[1]["status"] = runtime_db.PAW_FILE_FAILED
        files[1]["error"] = worker._MISSING_MARK + " HTTP 404"
        await worker._finalize(post, files)
        self.assertEqual(
            runtime_db.get_pawchive_post(post["id"])["status"],
            runtime_db.PAW_POST_FAILED)
        self.assertEqual(len(self._notifies), 1)

    async def test_queued_dead_link_cancelled(self):
        """已提交但还在 Agent 队列里没开下的死链：预检取消并标失败。"""
        post, _ = self._seed_and_claim()
        runtime_db.mark_pawchive_file_submitted(
            runtime_db.list_pawchive_files(post["id"])[0]["id"], "task-q1")
        self._tasks_fixture = [_task("task-q1", "PENDING")]
        with mock.patch.object(worker, "_head_status", return_value=404), \
                mock.patch.object(worker.asyncio, "sleep", new=_noop_sleep):
            ok = await worker.process_post(post)
        self.assertTrue(ok)
        self.assertEqual(self._cancels, ["task-q1"])
        row = runtime_db.list_pawchive_files(post["id"])[0]
        self.assertEqual(row["status"], runtime_db.PAW_FILE_FAILED)
        self.assertIn("站点缺文件", row["error"])
        self.assertEqual(
            runtime_db.get_pawchive_post(post["id"])["status"],
            runtime_db.PAW_POST_FAILED)


class ExtOnlyPostTest(_WorkerDbTestCase):

    async def test_ext_only_post_goes_manual_without_chrome(self):
        post, _ = self._seed_and_claim(_post(
            "9", files=[], ext_links=[{"kind": "link", "domain": "mega.nz",
                                       "url": "https://mega.nz/y#k",
                                       "text": "M"}]))
        with mock.patch.object(worker.asyncio, "sleep", new=_noop_sleep):
            ok = await worker.process_post(post)
        self.assertTrue(ok)
        self.assertEqual(
            runtime_db.get_pawchive_post(post["id"])["status"],
            runtime_db.PAW_POST_MANUAL)
        self.assertEqual(self._submitted, [])


if __name__ == "__main__":
    unittest.main()
