"""X 作者下载（x_dl.py）的单元测试。subprocess/cookie 全 mock 不联网。
运行方式（项目根目录）：
    .venv/bin/python -m unittest tests.test_x_dl -v
"""
import asyncio
import atexit
import subprocess
import time
import os
import shutil
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_x_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import x_dl    # noqa: E402
from tg_userbot import runtime_db  # noqa: E402


class _DbTestCase(unittest.TestCase):
    """X DB 队列语义测试基类：临时 runtime_db（v22 xn_tasks）。"""

    def setUp(self):
        self._td = tempfile.mkdtemp(dir=_TMP)
        self._saved_conn = runtime_db._CONN
        runtime_db._CONN = None
        self.dbpath = os.path.join(self._td, "tg.db")
        runtime_db.init_db(self.dbpath)
        x_dl._WORKER._stop = False
        x_dl._WORKER._stop_flag = False
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            runtime_db.close_db()
        except Exception:
            pass
        runtime_db._CONN = self._saved_conn
        shutil.rmtree(self._td, ignore_errors=True)


class CmdBuildTest(unittest.TestCase):

    def test_gallery_dl_command_shape(self):
        with mock.patch.object(x_dl, "_cookie_path",
                               return_value="/tmp/ck.txt"):
            cmd = x_dl._gallery_dl_cmd("@Au", "/tmp/d", "/tmp/a.txt")
        self.assertTrue(cmd[0].endswith("gallery-dl"))
        self.assertIn("--cookies", cmd)
        self.assertEqual(cmd[cmd.index("--directory") + 1], "/tmp/d")
        self.assertEqual(cmd[cmd.index("--download-archive") + 1], "/tmp/a.txt")
        self.assertIn("--sleep", cmd)          # 限速防风控
        self.assertTrue(cmd[-1].endswith("/Au"))
        self.assertNotIn("@", cmd[-1])         # @ 剥离
        # 命名模板带日期前缀（项目命名规约）
        fname = cmd[cmd.index("-o") + 1]
        self.assertIn("{date:%y-%m-%d}", fname)

    def test_proxy_socks5h_dns_via_proxy(self):
        with mock.patch.dict(os.environ, {"TG_PROXY":
                                          "socks5://127.0.0.1:12334"}):
            self.assertEqual(x_dl._proxy_arg(),
                             "socks5h://127.0.0.1:12334")
        with mock.patch.dict(os.environ, {"TG_PROXY": ""}, clear=False):
            # CHROME_PROXY 是 import 期常量——绕过：直接 unset TG_PROXY 且
            # CHROME_PROXY 也为空的环境在 CI/默认即成立；否则非 None
            val = x_dl._proxy_arg()
            self.assertTrue(val is None or val.startswith(("socks", "http")))


class CookieTest(unittest.TestCase):

    def test_refresh_requires_auth_and_ct0(self):
        fake = [("gt", "v", ".x.com", "/", 0, True),
                ("guest_id", "v", ".x.com", "/", 0, True)]
        prof = os.path.join(_TMP, "agentprof", "Default")
        os.makedirs(prof, exist_ok=True)
        open(os.path.join(prof, "Cookies"), "w").write("")
        with mock.patch("shutil.copy"), \
             mock.patch("browser_cookie3.chrome") as bc, \
             mock.patch.object(config, "CHROME_PROFILE_DIR",
                               os.path.join(_TMP, "agentprof")):
            bc.return_value = [type("C", (), {"name": n, "value": v,
                                              "domain": d, "path": p,
                                              "expires": e, "secure": s})
                               for n, v, d, p, e, s in fake]
            ok, msg = x_dl.refresh_cookies()
        self.assertFalse(ok)
        self.assertIn("未登录", msg)

    def test_refresh_writes_netscape_file(self):
        fake = [("auth_token", "AUTH", ".x.com", "/", 1790000000, True),
                ("ct0", "CT", ".x.com", "/", 0, False)]
        out = os.path.join(_TMP, "ck.txt")
        prof = os.path.join(_TMP, "agentprof2", "Default")
        os.makedirs(prof, exist_ok=True)
        open(os.path.join(prof, "Cookies"), "w").write("")
        with mock.patch("shutil.copy"), \
             mock.patch("browser_cookie3.chrome") as bc, \
             mock.patch.object(x_dl, "_cookie_path", return_value=out), \
             mock.patch.object(config, "CHROME_PROFILE_DIR",
                               os.path.join(_TMP, "agentprof2")):
            bc.return_value = [type("C", (), {"name": n, "value": v,
                                              "domain": d, "path": p,
                                              "expires": e, "secure": s})
                               for n, v, d, p, e, s in fake]
            ok, msg = x_dl.refresh_cookies()
        self.assertTrue(ok, msg)
        body = open(out).read()
        self.assertIn("auth_token\tAUTH", body)
        self.assertIn(".x.com\tTRUE\t/\tTRUE\t1790000000\tauth_token", body)


class RunAuthorTest(unittest.TestCase):

    def setUp(self):
        import tempfile
        base = tempfile.mkdtemp(dir=_TMP)   # 每用例独立，state 不串
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.dl = os.path.join(base, "downloads")
        self.rt = os.path.join(base, "runtime")
        os.makedirs(self.dl, exist_ok=True)
        os.makedirs(self.rt, exist_ok=True)
        patches = [mock.patch.object(config, "DOWNLOAD_DIR", self.dl),
                   mock.patch.object(config, "RUNTIME_DIR", self.rt),
                   mock.patch.object(x_dl, "_cookie_path",
                                     return_value=os.path.join(self.rt,
                                                               "ck.txt"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # cookie 文件已存在 → 不触发刷新
        open(os.path.join(self.rt, "ck.txt"), "w").write("# ok")

    def _fake_run(self, code=0, stdout=""):
        def fake(cmd, on_line=None, timeout=None):
            for ln in stdout.splitlines():
                if on_line:
                    on_line(ln)
            return code, stdout[-1500:], stdout
        return fake

    def test_run_author_counts_new_and_persists_state(self):
        out = "./X/Au/26-10-25 123_1.jpg\n./X/Au/26-10-25 124_1.mp4\n"
        with mock.patch.object(x_dl, "_run_proc",
                               side_effect=self._fake_run(0, out)):
            n, note = x_dl.run_author("Au")
        self.assertEqual(n, 2)
        self.assertEqual(note, "")
        st = x_dl._load_state()
        self.assertEqual(st["Au"]["total"], 2, st)
        # 增量：再跑一次新增 1
        with mock.patch.object(x_dl, "_run_proc",
                               side_effect=self._fake_run(
                                   0, "./X/Au/26-10-25 125_1.jpg\n")):
            n2, _ = x_dl.run_author("@Au")
        self.assertEqual(n2, 1)
        self.assertEqual(x_dl._load_state()["Au"]["total"], 3)

    def test_progress_updates_running_state_via_on_line(self):
        """流式 on_line 实时更新 _X_RUNNING——/x 进度视图的数据源。"""
        out = "./X/Au/a_1.jpg\n./X/Au/b_1.mp4\n"
        x_dl._X_RUNNING = {"author": "Au", "started": time.time(), "done": 0}
        try:
            with mock.patch.object(x_dl, "_run_proc",
                                   side_effect=self._fake_run(0, out)):
                x_dl.run_author("Au")
            # 执行中 done 已被 on_line 累计（结束时仍保留在 dict 里，
            # run_x_and_notify 的 finally 才清）
            self.assertGreaterEqual(x_dl._X_RUNNING.get("done", 0), 2)
        finally:
            x_dl._X_RUNNING = {}

    def test_status_text_shows_progress(self):
        x_dl._X_RUNNING = {"author": "Au", "started": time.time() - 95,
                           "done": 7, "last": "26-10-25 123_1.jpg"}
        try:
            text = x_dl.status_text()
            self.assertIn("Au", text)
            self.assertIn("已收 7 个", text)
            self.assertIn("26-10-25 123_1.jpg", text)
            self.assertIn("1分", text)
        finally:
            x_dl._X_RUNNING = {}

    def test_auth_failure_refreshes_and_retries_once(self):
        calls = {"n": 0}

        def flaky(cmd, on_line=None, timeout=None):
            calls["n"] += 1
            code = 401 if calls["n"] == 1 else 0
            err = "Could not authenticate you"
            return code, err, ""

        with mock.patch.object(x_dl, "_run_proc", side_effect=flaky), \
             mock.patch.object(x_dl, "refresh_cookies",
                               return_value=(True, "ok")) as rc:
            n, note = x_dl.run_author("Au")
        self.assertEqual(calls["n"], 2)      # 重试了一次
        rc.assert_called_once()
        self.assertEqual(note, "")

    def test_auth_failure_persists_error_message(self):
        def bad(cmd, on_line=None, timeout=None):
            return 401, "Could not authenticate you", ""

        with mock.patch.object(x_dl, "_run_proc", side_effect=bad), \
             mock.patch.object(x_dl, "refresh_cookies",
                               return_value=(False, "Agent Chrome 未登录 X")):
            n, note = x_dl.run_author("Au")
        self.assertEqual(n, 0)
        self.assertIn("未登录", note)


class CommandTest(_DbTestCase):

    def test_parse_and_status(self):
        self.assertTrue(x_dl.is_x_command("/x"))
        self.assertTrue(x_dl.is_x_command("/x @Au"))
        self.assertFalse(x_dl.is_x_command("/xl"))
        self.assertIn("/x <", asyncio.run(x_dl.command_reply("/x")))

    def test_normalize(self):
        """目标归一：裸名/@名/作者页 → @name；推文 → 裁剪 URL。"""
        self.assertEqual(x_dl.normalize_target("@Au")[0], "@Au")
        self.assertEqual(x_dl.normalize_target("Au")[0], "@Au")
        self.assertEqual(
            x_dl.normalize_target("https://x.com/Au/status/9?s=20")[0],
            "https://x.com/Au/status/9")
        self.assertIsNone(
            x_dl.normalize_target("https://x.com/search?q=x")[0])

    def test_enqueue_and_duplicate(self):
        """提交 → 入队；重复提交 → 单队列判重（2026-11-05 迁移语义）。"""
        out = asyncio.run(x_dl.command_reply("/x @Shiro_AI11"))
        self.assertIn("已入队", out)
        out2 = asyncio.run(x_dl.command_reply("/x @Shiro_AI11"))
        self.assertIn("已在队列", out2)
        self.assertEqual(runtime_db.XN_TASKS.count(), 1)

    def test_notify_on_completion(self):
        """worker.process 完成链：DONE + 完成通知 + 进度视图清空。"""
        notes = []

        async def fake_notify(text):
            notes.append(text)

        runtime_db.XN_TASKS.enqueue("@Au")
        task = runtime_db.XN_TASKS.claim_next()
        with mock.patch.object(x_dl, "run_author", return_value=(5, "")), \
             mock.patch.object(x_dl, "normalize_target",
                               return_value=("@Au", "https://x.com/Au",
                                             "@Au", None)), \
             mock.patch("tg_userbot.notify.notify_user", fake_notify):
            asyncio.run(x_dl._WORKER.process(task))
        self.assertEqual(runtime_db.XN_TASKS.get("@Au")["status"], "DONE")
        self.assertEqual(len(notes), 1)
        self.assertIn("新增 5 个", notes[0])
        self.assertEqual(x_dl._X_RUNNING, {})


class AutoRouteTest(_DbTestCase):
    """分享链接自动识别（2026-11-05 owner：直接发 x.com 链接即下载）。"""

    def test_looks_like_x(self):
        self.assertTrue(x_dl.looks_like_x(
            "看这个 https://x.com/a/status/9 超强"))
        self.assertFalse(x_dl.looks_like_x("纯文本 https://b23.tv/x"))

    def test_auto_reply_extracts_url(self):
        """文本+链接混合形态：抽 URL 入队（键=归一 URL，不是整段文本）。"""
        out = asyncio.run(x_dl.auto_reply(
            "[渲染练习] 木偶大人的Gimme×Gimme https://x.com/foo/status/123"))
        self.assertIn("已自动识别", out)
        self.assertIn("已入队", out)
        self.assertIsNotNone(
            runtime_db.XN_TASKS.get("https://x.com/foo/status/123"))


class RestartRecoveryTest(_DbTestCase):
    """停机/租约路径（DB 队列语义；RUNNING_MARK 已随迁移移除）。"""

    def test_shutdown_cancel_requeues(self):
        """停机取消 worker → 在途回 PENDING（框架 release），重启自续。"""
        runtime_db.XN_TASKS.enqueue("@u2")

        async def go():
            loop_task = asyncio.ensure_future(x_dl._WORKER.run())
            await asyncio.sleep(0.25)   # 进入 process（run_author 睡 0.4）
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

        with mock.patch.object(x_dl, "run_author",
                               side_effect=lambda *a, **k: time.sleep(0.4)), \
             mock.patch.object(x_dl, "normalize_target",
                               return_value=("@u2", "https://x.com/u2",
                                             "@u2", None)):
            asyncio.run(go())
        self.assertEqual(runtime_db.XN_TASKS.get("@u2")["status"], "PENDING")

    def test_stop_marks_failed_terminal(self):
        """/x stop → 在途标 FAILED（不回队列不打架），队列继续。"""
        runtime_db.XN_TASKS.enqueue("@u2")
        runtime_db.XN_TASKS.enqueue("@u4")

        async def go():
            loop_task = asyncio.ensure_future(x_dl._WORKER.run())
            await asyncio.sleep(0.25)
            x_dl.x_stop()
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

        with mock.patch.object(x_dl, "run_author",
                               side_effect=lambda *a, **k: time.sleep(0.4)), \
             mock.patch.object(x_dl, "normalize_target",
                               return_value=("@uX", "https://x.com/uX",
                                             "@uX", None)):
            asyncio.run(go())
        self.assertEqual(runtime_db.XN_TASKS.get("@u2")["status"], "FAILED")
        self.assertIn(runtime_db.XN_TASKS.get("@u4")["status"],
                      ("PENDING", "PROCESSING"))


if __name__ == "__main__":
    unittest.main()


class AuthFalsePositiveTest(unittest.TestCase):
    """评审收紧回归：文件名/推文 ID 里的 "401" 子串不得误判认证失效。"""

    def setUp(self):
        # 本类断言「不触发重试」——关掉自动重跑档
        _rp = mock.patch.object(config, 'GALLERY_DL_AUTO_RETRY_MAX', 0)
        _rp.start()
        self.addCleanup(_rp.stop)
        base = tempfile.mkdtemp(dir=_TMP)
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.dl = os.path.join(base, "downloads")
        self.rt = os.path.join(base, "runtime")
        os.makedirs(self.dl, exist_ok=True)
        os.makedirs(self.rt, exist_ok=True)
        open(os.path.join(self.rt, "ck.txt"), "w").write("# ok")
        for p in (mock.patch.object(config, "DOWNLOAD_DIR", self.dl),
                  mock.patch.object(config, "RUNTIME_DIR", self.rt),
                  mock.patch.object(x_dl, "_cookie_path",
                                    return_value=os.path.join(self.rt,
                                                              "ck.txt"))):
            p.start()
            self.addCleanup(p.stop)

    def test_id_substring_401_does_not_refresh(self):
        calls = {"n": 0}

        def bad(cmd, on_line=None, timeout=None, tag="x"):
            calls["n"] += 1
            return 4, ("./X/Au/26-10-25 1740123456789_1.jpg\n"
                       "network error"), ""

        with mock.patch.object(x_dl, "_run_proc", side_effect=bad), \
             mock.patch.object(x_dl, "refresh_cookies") as rc:
            n, note = x_dl.run_author("Au")
        self.assertEqual(calls["n"], 1, "误报不得触发刷 cookie 重试")
        rc.assert_not_called()
        self.assertIn("退出码 4", note)

    def test_http_401_still_refreshes(self):
        calls = {"n": 0}

        def bad(cmd, on_line=None, timeout=None, tag="x"):
            calls["n"] += 1
            return 4, "HTTP 401 Unauthorized", ""

        with mock.patch.object(x_dl, "_run_proc", side_effect=bad), \
             mock.patch.object(x_dl, "refresh_cookies",
                               return_value=(True, "ok")):
            n, note = x_dl.run_author("Au")
        self.assertEqual(calls["n"], 2)


class AutoRetryTest(unittest.TestCase):
    """失败自动重跑（2026-10-26 定档）：非零退出按退避整跑重试，
    archive 幂等；新增数跨重跑累计；烧完上限才报失败。"""

    def setUp(self):
        base = tempfile.mkdtemp(dir=_TMP)
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.dl = os.path.join(base, "downloads")
        self.rt = os.path.join(base, "runtime")
        os.makedirs(self.dl, exist_ok=True)
        os.makedirs(self.rt, exist_ok=True)
        open(os.path.join(self.rt, "ck.txt"), "w").write("# ok")
        for p in (mock.patch.object(config, "DOWNLOAD_DIR", self.dl),
                  mock.patch.object(config, "RUNTIME_DIR", self.rt),
                  mock.patch.object(x_dl, "_cookie_path",
                                    return_value=os.path.join(self.rt,
                                                              "ck.txt")),
                  mock.patch.object(config, "GALLERY_DL_AUTO_RETRY_MAX", 2),
                  mock.patch.object(config, "GALLERY_DL_AUTO_RETRY_BASE_DELAY",
                                    0.01),
                  mock.patch.object(config, "GALLERY_DL_AUTO_RETRY_MAX_DELAY",
                                    0.02)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(setattr, x_dl, "_X_RUNNING", {})

    def _fake_seq(self, results):
        seq = list(results)

        def fake(cmd, on_line=None, timeout=None, tag="x"):
            code, out = seq.pop(0)
            for ln in (out or "").splitlines():
                if on_line:
                    on_line(ln)
            return code, (out or "")[-1500:], out or ""
        return fake

    def test_fail_twice_then_success(self):
        results = [(4, "./X/Au/a_1.jpg"),   # 首跑：挂但收到 1 个
                   (4, "./X/Au/b_1.jpg"),   # 重跑1：挂又收 1 个
                   (0, "./X/Au/c_1.jpg")]   # 重跑2：成
        with mock.patch.object(x_dl, "_run_proc",
                               side_effect=self._fake_seq(results)):
            n, note = x_dl.run_author("Au")
        self.assertEqual(n, 3, "新增数应跨重跑累计")
        self.assertEqual(note, "")

    def test_exhausts_max_then_reports(self):
        results = [(4, "./X/Au/a.jpg"), (4, ""), (4, ""), (4, "")]
        with mock.patch.object(x_dl, "_run_proc",
                               side_effect=self._fake_seq(results)) as rp:
            n, note = x_dl.run_author("Au")
        self.assertEqual(rp.call_count, 3, "1 + 重跑2次")
        self.assertIn("已自动重跑 2 次仍失败", note)
        self.assertEqual(n, 1)

    def test_success_first_try_no_retry(self):
        with mock.patch.object(x_dl, "_run_proc",
                               side_effect=self._fake_seq([(0, "./X/Au/a.jpg")])) as rp:
            n, note = x_dl.run_author("Au")
        self.assertEqual(rp.call_count, 1)
        self.assertEqual((n, note), (1, ""))

    def test_retry_wait_visible_in_status(self):
        x_dl._X_RUNNING = {"author": "Au", "started": time.time(),
                           "done": 1, "retry_wait": 300, "attempt": 1}
        try:
            text = x_dl.status_text()
            self.assertIn("300s 后自动重跑", text)
            self.assertIn("第 1 次", text)
        finally:
            x_dl._X_RUNNING = {}
