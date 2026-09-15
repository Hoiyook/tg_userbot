"""Pawchive Worker（pawchive_worker.py，内置 httpx 下载器）的单元测试。

打桩点：`worker._download_one`（并发执行体）与 `worker._head_status`（死链
预检）；`_download_one` 本体用 httpx.MockTransport 真跑 HTTP 层（直连/续传/
404 判死/大小校验），不联网。
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

import httpx

_TMP = tempfile.mkdtemp(prefix="tg_userbot_paw_worker_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

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


class _WorkerDbTestCase(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pawwk_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        self._dl = mock.patch.object(config, "DOWNLOAD_DIR", self.dir)
        self._dl.start()
        self.addCleanup(self._dl.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        worker.resume()
        worker.release_inflight()
        self.addCleanup(worker.release_inflight)
        # 里程碑计数复位（模块级全局，跨用例隔离）
        for k in worker._MILESTONE:
            worker._MILESTONE[k] = 0
        self._notifies = []      # worker 发出的帖子级通知
        self._downloads = []     # _download_one 收到的 (post, file)
        patches = [
            mock.patch.object(worker, "_disk_free_gb", return_value=100.0),
            mock.patch.object(worker, "_head_status", return_value=200),
            mock.patch.object(worker.notify, "notify_user",
                              side_effect=self._capture_notify),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        state.MY_ID = 12345
        self.addCleanup(setattr, state, "MY_ID", None)

    async def _capture_notify(self, text):
        self._notifies.append(text)

    def _patch_downloader(self, result=None):
        """替换内置下载执行体：普通函数（side_effect 对协程返回值不会
        自动 await，会得到 coroutine 对象）；调用侧用 to_thread 包它，
        所以这里直接同步返回三元组即可。"""
        def fake(post, f, progress):
            self._downloads.append((post, f))
            return result or ("done", 123, None)
        return mock.patch.object(worker, "_download_one", side_effect=fake)

    def _seed_and_claim(self, post=None):
        """入库 + 领取，返回 (post_row, files)。"""
        runtime_db.enqueue_pawchive_posts(
            "patreon", "42", "Creator", [post or _post()])
        claimed = runtime_db.claim_next_pawchive_post(now=1000)
        self.assertIsNotNone(claimed)
        return claimed, runtime_db.list_pawchive_files(claimed["id"])


class PrecheckTest(_WorkerDbTestCase):

    async def test_dead_link_failed_without_download(self):
        """404 死链：预检直接标 FAILED，不进下载器；活的照常下载。"""
        post, _ = self._seed_and_claim(_post(
            "11", files=[
                {"url": "https://x/dead.jpg", "filename": "dead.jpg"},
                {"url": "https://x/alive.mp4", "filename": "alive.mp4"},
            ]))
        codes = {"https://x/dead.jpg": 404, "https://x/alive.mp4": 200}
        with mock.patch.object(worker, "_head_status",
                               side_effect=lambda u: codes[u]), \
                self._patch_downloader(("done", 5, None)):
            ok = await worker.process_post(post)
        self.assertTrue(ok)
        rows = {r["url"]: r for r in runtime_db.list_pawchive_files(post["id"])}
        self.assertEqual(rows["https://x/dead.jpg"]["status"],
                         runtime_db.PAW_FILE_FAILED)
        self.assertIn("站点缺文件", rows["https://x/dead.jpg"]["error"])
        self.assertEqual(rows["https://x/alive.mp4"]["status"],
                         runtime_db.PAW_FILE_DONE)
        # 死链+活链混合 → 帖子 FAILED 且通知
        self.assertEqual(
            runtime_db.get_pawchive_post(post["id"])["status"],
            runtime_db.PAW_POST_FAILED)
        self.assertEqual(len(self._notifies), 1)
        self.assertEqual([f["url"] for _, f in self._downloads],
                         ["https://x/alive.mp4"])

    async def test_head_error_treated_alive(self):
        """HEAD 网络异常不预判——交给下载重试。"""
        f = {"id": 1, "url": "https://x/a.mp4",
             "status": runtime_db.PAW_FILE_PENDING}
        with mock.patch.object(worker, "_head_status", return_value=None):
            dead = await asyncio.to_thread(worker._head_dead_ids,
                                           [(f, f["url"])])
        self.assertEqual(dead, set())


class LegacyRequeueTest(_WorkerDbTestCase):

    async def test_submitted_requeued_for_builtin(self):
        """执行体切换迁移：旧 Chrome 时代的 SUBMITTED → PENDING，清 task id。"""
        post, files = self._seed_and_claim()
        runtime_db.mark_pawchive_file_submitted(files[0]["id"], "task-old")
        files = runtime_db.list_pawchive_files(post["id"])
        worker._requeue_legacy_submitted(files)
        row = runtime_db.list_pawchive_files(post["id"])[0]
        self.assertEqual(row["status"], runtime_db.PAW_FILE_PENDING)
        self.assertIsNone(row["chrome_task_id"])


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


class ProcessPostTest(_WorkerDbTestCase):

    async def test_full_flow_completed(self):
        post, _ = self._seed_and_claim()
        with self._patch_downloader(("done", 4096, None)):
            ok = await worker.process_post(post)
        self.assertTrue(ok)
        self.assertEqual(
            runtime_db.get_pawchive_post(post["id"])["status"],
            runtime_db.PAW_POST_COMPLETED)
        row = runtime_db.list_pawchive_files(post["id"])[0]
        self.assertEqual(row["status"], runtime_db.PAW_FILE_DONE)
        self.assertEqual(row["size_bytes"], 4096)

    async def test_disk_guard_pauses_and_releases(self):
        post, _ = self._seed_and_claim()
        with mock.patch.object(worker, "_disk_free_gb", return_value=0.5):
            ok = await worker.process_post(post)
        self.assertFalse(ok)
        self.assertTrue(worker.paused())
        self.assertIn("磁盘", worker.worker_state_text())
        self.assertEqual(
            runtime_db.get_pawchive_post(post["id"])["status"],
            runtime_db.PAW_POST_PENDING)
        self.assertEqual(self._downloads, [])
        worker.resume()

    async def test_ext_only_post_goes_manual_without_download(self):
        post, _ = self._seed_and_claim(_post(
            "9", files=[], ext_links=[{"kind": "link", "domain": "mega.nz",
                                       "url": "https://mega.nz/y#k",
                                       "text": "M"}]))
        ok = await worker.process_post(post)
        self.assertTrue(ok)
        self.assertEqual(
            runtime_db.get_pawchive_post(post["id"])["status"],
            runtime_db.PAW_POST_MANUAL)
        self.assertEqual(self._downloads, [])


class PanelTest(_WorkerDbTestCase):
    """进度面板：正文构建 / 原地编辑容错 / 里程碑汇总。"""

    async def test_build_progress_text_contains_counts_and_disk(self):
        self._seed_and_claim()
        text = worker.build_progress_text()
        self.assertIn("🔄1", text)
        self.assertIn("磁盘剩余", text)

    async def test_panel_edit_unchanged_and_invalid(self):
        global _PANEL_MSG_ID
        w = worker
        # 直接驱动 _refresh_panel：首次创建 → 编辑不变 → 失效重建
        sent, edited = [], []

        class FakePanelClient:
            async def send_message(self, target, text):
                sent.append(text)

                class M:
                    id = 777
                return M()

            async def edit_message(self, target, mid, text):
                edited.append((mid, text))
                raise Exception("MessageIdInvalidError: boom")

        old_client = state.client
        state.client = FakePanelClient()
        old_bot = state.BOT_ID
        state.BOT_ID = 999
        old_mid = w._PANEL_MSG_ID
        old_last = w._PANEL_LAST_TEXT
        try:
            w._PANEL_MSG_ID = None
            w._PANEL_LAST_TEXT = None
            self.assertTrue(await w._refresh_panel())
            self.assertEqual(len(sent), 1)
            self.assertEqual(w._PANEL_MSG_ID, 777)
            # 同内容 → 跳过编辑
            self.assertTrue(await w._refresh_panel())
            self.assertEqual(len(edited), 0)
            # 内容变化但消息失效 → 面板 id 重置（下轮重建）
            runtime_db.enqueue_pawchive_posts(
                "patreon", "42", "C", [_post("555")])
            self.assertFalse(await w._refresh_panel())
            self.assertIsNone(w._PANEL_MSG_ID)
        finally:
            state.client = old_client
            state.BOT_ID = old_bot
            w._PANEL_MSG_ID = old_mid
            w._PANEL_LAST_TEXT = old_last

    async def test_milestone_digest_and_reset(self):
        self._seed_and_claim()
        with mock.patch.object(config, "PAWCHIVE_MILESTONE_POSTS", 2):
            self.assertFalse(await worker._milestone_notify_if_due())
            worker._bump_milestone("completed")
            self.assertFalse(await worker._milestone_notify_if_due())
            worker._bump_milestone("manual")
            self.assertTrue(await worker._milestone_notify_if_due())
        self.assertEqual(len(self._notifies), 1)
        self.assertIn("✅完成 1", self._notifies[0])
        self.assertIn("👤待人工 1", self._notifies[0])
        # 计数已复位
        self.assertEqual(worker._MILESTONE,
                         {"completed": 0, "manual": 0, "failed": 0})


class PauseResumeTest(_WorkerDbTestCase):

    async def test_pause_blocks_claim(self):
        worker.pause("测试")
        self.assertIsNone(worker.claim_next_post())
        worker.resume()


class DownloadOneHttpTest(_WorkerDbTestCase):
    """_download_one 本体：httpx.MockTransport 真跑 HTTP 层。"""

    def _run_download(self, handler, filename="a.mp4", pre_exists=False,
                      pre_size=None):
        """构造一条文件记录并用 MockTransport 跑 _download_one（线程里）。"""
        post = _post()
        f = {"id": 1, "url": "https://file.pawchive.pw/data/a.mp4",
             "filename": filename, "status": runtime_db.PAW_FILE_PENDING}
        target = worker._target_path(post, filename)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if pre_exists:
            with open(target, "wb") as fh:
                fh.write(b"x" * (pre_size or 4))
        transport = httpx.MockTransport(handler)
        # 先捕获真实类：打桩后模块属性是 MagicMock，工厂要绕过它
        real_client = httpx.Client
        client_factory = lambda **kw: real_client(
            transport=transport, **kw)

        calls = {"n": 0}

        def progress(c, t):
            calls["n"] += 1

        with mock.patch.object(worker.httpx, "Client",
                               side_effect=client_factory), \
                mock.patch.object(worker.time, "sleep", new=_noop):
            result = asyncio.run(
                asyncio.to_thread(worker._download_one, post, f, progress))
        return result, target, calls["n"]

    def test_success_writes_target(self):
        def handler(request):
            return httpx.Response(200, content=b"hello-video")
        result, target, _ = self._run_download(handler)
        self.assertEqual(result[0], "done")
        self.assertEqual(result[1], 11)
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"hello-video")

    def test_404_marked_dead(self):
        def handler(request):
            return httpx.Response(404, text="nope")
        result, target, _ = self._run_download(handler)
        self.assertEqual(result[0], "dead")
        self.assertIn("404", result[2])
        self.assertFalse(os.path.exists(target))

    def test_resume_uses_range(self):
        """.part 已有前半段：请求带 Range，206 续写。"""
        seen_headers = []

        def handler(request):
            seen_headers.append(request.headers.get("range"))
            rng = request.headers.get("range")
            if rng == "bytes=5-":
                return httpx.Response(206, content=b"-world")
            return httpx.Response(200, content=b"hello-world")

        post = _post()
        f = {"id": 1, "url": "https://file.pawchive.pw/data/a.mp4",
             "filename": "a.mp4", "status": runtime_db.PAW_FILE_PENDING}
        target = worker._target_path(post, "a.mp4")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target + ".part", "wb") as fh:
            fh.write(b"hello")
        transport = httpx.MockTransport(handler)
        real_client = httpx.Client
        with mock.patch.object(worker.httpx, "Client",
                               side_effect=lambda **kw: real_client(
                                   transport=transport, **kw)), \
                mock.patch.object(worker.time, "sleep",
                                  new=(lambda s: None)):
            result = asyncio.run(
                asyncio.to_thread(worker._download_one, post, f,
                                  lambda c, t: None))
        self.assertEqual(result[0], "done")
        self.assertIn("bytes=5-", seen_headers[0])
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"hello-world")

    def test_size_mismatch_retries_then_fails(self):
        """Content-Length 与实际不符：校验失败计入额度 → 耗尽 → failed。

        撒谎的服务器不能靠无限续传迁就（可能永远差一截）。"""
        def handler(request):
            return httpx.Response(200, content=b"short",
                                  headers={"Content-Length": "100"})
        result, target, _ = self._run_download(handler)
        self.assertEqual(result[0], "failed")

    def test_progress_made_disconnects_do_not_burn_retry_budget(self):
        """任务书场景（2026-09-15 深夜 CDN 断流）：有进账的断流不消耗重试
        额度——每尝试 +3 字节，连断 4 次仍未到上限，第 5 次给全量 → done。"""
        state_n = {"n": 0}

        def handler(request):
            state_n["n"] += 1
            if state_n["n"] <= 4:
                # 断流：先给 300KB（要超过 iter_bytes 的 256KB 缓冲，字节才会
                # 真正落到 .part）再掐——模拟 CDN 传输中途断
                def stream():
                    yield b"abc" * 100000
                    raise httpx.RemoteProtocolError("peer closed")
                return httpx.Response(206, content=stream())
            return httpx.Response(200, content=b"abcdefghij")

        post = _post()
        f = {"id": 1, "url": "https://x/big.mp4", "filename": "big.mp4",
             "status": runtime_db.PAW_FILE_PENDING}
        transport = httpx.MockTransport(handler)
        real_client = httpx.Client
        with mock.patch.object(worker.httpx, "Client",
                               side_effect=lambda **kw: real_client(
                                   transport=transport, **kw)), \
                mock.patch.object(worker.time, "sleep",
                                  new=lambda s: None):
            result = asyncio.run(
                asyncio.to_thread(worker._download_one, post, f,
                                  lambda c, t: None))
        self.assertEqual(result[0], "done")
        self.assertGreaterEqual(state_n["n"], 5,
                                "断流有进账不应消耗 3 次重试额度")

    def test_no_progress_disconnects_exhaust_budget(self):
        """无进账断流：3 次额度耗尽 → failed（不无限空转）。"""
        state_n = {"n": 0}

        def handler(request):
            state_n["n"] += 1
            raise httpx.RemoteProtocolError("peer closed")

        post = _post()
        f = {"id": 1, "url": "https://x/x.mp4", "filename": "x.mp4",
             "status": runtime_db.PAW_FILE_PENDING}
        transport = httpx.MockTransport(handler)
        real_client = httpx.Client
        with mock.patch.object(worker.httpx, "Client",
                               side_effect=lambda **kw: real_client(
                                   transport=transport, **kw)), \
                mock.patch.object(worker.time, "sleep",
                                  new=lambda s: None):
            result = asyncio.run(
                asyncio.to_thread(worker._download_one, post, f,
                                  lambda c, t: None))
        self.assertEqual(result[0], "failed")
        self.assertEqual(state_n["n"], worker._DOWNLOAD_ATTEMPTS)

    def test_existing_target_wrong_size_redownloads(self):
        """任务书 §8-A 反例：目标存在但大小不符（截断/损坏）→ 重新下载。"""
        post = _post()
        f = {"id": 1, "url": "https://file.pawchive.pw/data/a.mp4",
             "filename": "a.mp4", "status": runtime_db.PAW_FILE_PENDING}
        target = worker._target_path(post, "a.mp4")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(b"truncated")   # 9 字节，远端是 11 字节

        def handler(request):
            return httpx.Response(200, content=b"hello-world")

        transport = httpx.MockTransport(handler)
        real_client = httpx.Client
        with mock.patch.object(worker, "_head_alive",
                               return_value=(200, 11)), \
                mock.patch.object(worker.httpx, "Client",
                                  side_effect=lambda **kw: real_client(
                                      transport=transport, **kw)), \
                mock.patch.object(worker.time, "sleep",
                                  new=lambda s: None):
            result = asyncio.run(
                asyncio.to_thread(worker._download_one, post, f,
                                  lambda c, t: None))
        self.assertEqual(result[0], "done")
        self.assertEqual(result[1], 11)
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"hello-world")

    def test_410_marked_dead(self):
        def handler(request):
            return httpx.Response(410, text="gone")
        post = _post()
        f = {"id": 1, "url": "https://x/gone.mp4", "filename": "gone.mp4",
             "status": runtime_db.PAW_FILE_PENDING}
        transport = httpx.MockTransport(handler)
        real_client = httpx.Client
        with mock.patch.object(worker.httpx, "Client",
                               side_effect=lambda **kw: real_client(
                                   transport=transport, **kw)):
            result = asyncio.run(
                asyncio.to_thread(worker._download_one, post, f,
                                  lambda c, t: None))
        self.assertEqual(result[0], "dead")
        self.assertIn("410", result[2])

    def test_500_retries_then_failed_not_dead(self):
        """任务书 §8-D：服务端错误可重试，耗尽后 failed（≠ 死链 dead）。"""
        def handler(request):
            return httpx.Response(500, text="boom")
        post = _post()
        f = {"id": 1, "url": "https://x/err.mp4", "filename": "err.mp4",
             "status": runtime_db.PAW_FILE_PENDING}
        transport = httpx.MockTransport(handler)
        real_client = httpx.Client
        with mock.patch.object(worker.httpx, "Client",
                               side_effect=lambda **kw: real_client(
                                   transport=transport, **kw)), \
                mock.patch.object(worker.time, "sleep",
                                  new=lambda s: None):
            result = asyncio.run(
                asyncio.to_thread(worker._download_one, post, f,
                                  lambda c, t: None))
        self.assertEqual(result[0], "failed")
        self.assertIn("500", result[2])

    def test_existing_target_same_size_skips(self):
        """目标已存在且远端大小一致 → 直接算完成，不发起下载。"""
        post = _post()
        f = {"id": 1, "url": "https://file.pawchive.pw/data/a.mp4",
             "filename": "a.mp4", "status": runtime_db.PAW_FILE_PENDING}
        target = worker._target_path(post, "a.mp4")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(b"x" * 7)
        with mock.patch.object(worker, "_head_alive",
                               return_value=(200, 7)), \
                mock.patch.object(worker.httpx, "Client") as client_cls:
            result = asyncio.run(
                asyncio.to_thread(worker._download_one, post, f,
                                  lambda c, t: None))
        self.assertEqual(result, ("done", 7, None))
        client_cls.assert_not_called()   # 完全没发起 HTTP


async def _noop(_):
    return None


if __name__ == "__main__":
    unittest.main()
