"""Pixiv 画师下载（pixiv_dl.py）的单元测试。subprocess/网络全 mock 不联网。
运行方式（项目根目录）：
    .venv/bin/python -m unittest tests.test_pixiv_dl -v
"""
import asyncio
import atexit
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_pixiv_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import pixiv_dl  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402


class _DbTestCase(unittest.TestCase):
    """pixiv DB 队列语义测试基类：临时 runtime_db（v22 xi_tasks）。"""

    def setUp(self):
        self._td = tempfile.mkdtemp(dir=_TMP)
        self._saved_conn = runtime_db._CONN
        runtime_db._CONN = None
        self.dbpath = os.path.join(self._td, "tg.db")
        runtime_db.init_db(self.dbpath)
        pixiv_dl._WORKER._stop = False
        pixiv_dl._WORKER._stop_flag = False
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
        cmd = pixiv_dl._gallery_dl_cmd("93028519", "/tmp/d", "/tmp/a.txt",
                                       "TOKEN")
        self.assertTrue(cmd[0].endswith("gallery-dl"))
        self.assertEqual(cmd[cmd.index("--directory") + 1], "/tmp/d")
        self.assertEqual(cmd[cmd.index("--download-archive") + 1],
                         "/tmp/a.txt")
        # App API 认证（refresh-token 经 -o 注入，token 不进 URL）
        self.assertIn("extractor.pixiv.refresh-token=TOKEN", cmd)
        # 限速最安全档（与 /x 同档）
        self.assertEqual(cmd[cmd.index("--sleep") + 1], "2")
        self.assertEqual(cmd[cmd.index("--sleep-request") + 1], "1")
        # 只认数字 ID 的 users URL
        self.assertTrue(cmd[-1].endswith("/users/93028519"))
        # 目录规约：作者名_作者ID（2026-10-25 用户定）；必须是 JSON
        # 数组——gallery-dl 的 directory 逐项迭代，裸串会逐字符炸
        self.assertIn('directory=["{user[name]}_{user[id]}"]', cmd)
        # 文件名规约：日期_标题_作品ID_p第几张
        fname = next(v for v in cmd if v.startswith("filename="))
        self.assertEqual(
            fname, "filename={date:%y-%m-%d}_{title}_{id}_p{num}.{extension}")


class ResolveTest(unittest.TestCase):

    def test_digits_pass_through(self):
        uid, note = pixiv_dl.resolve_user_id("93028519")
        self.assertEqual(uid, "93028519")
        self.assertEqual(note, "")

    def test_username_via_pixiv_me_redirect(self):
        resp = type("R", (), {"status_code": 301,
                              "headers": {"Location":
                                          "https://www.pixiv.net/users/101238283"}})
        import requests as _rq
        with mock.patch.object(_rq, "head", return_value=resp):
            uid, note = pixiv_dl.resolve_user_id("dramjou")
        self.assertEqual(uid, "101238283")
        self.assertEqual(note, "")

    def test_username_404(self):
        resp = type("R", (), {"status_code": 404, "headers": {}})
        import requests as _rq
        with mock.patch.object(_rq, "head", return_value=resp):
            uid, note = pixiv_dl.resolve_user_id("nobody")
        self.assertIsNone(uid)
        self.assertIn("不存在", note)

    def test_empty_target(self):
        uid, note = pixiv_dl.resolve_user_id("")
        self.assertIsNone(uid)
        self.assertIn("空", note)


class RunUserTest(unittest.TestCase):
    """run_user 全链路（resolve/token/proc/state）——mock 不联网。"""

    def setUp(self):
        base = tempfile.mkdtemp(dir=_TMP)   # 每用例独立，state 不串
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        # run_user 会写 _PIX_RUNNING——每用例后归位防串扰
        self.addCleanup(setattr, pixiv_dl, "_PIX_RUNNING", {})
        self.dl = os.path.join(base, "downloads")
        self.rt = os.path.join(base, "runtime")
        os.makedirs(self.dl, exist_ok=True)
        os.makedirs(self.rt, exist_ok=True)
        patches = [
            mock.patch.object(config, "DOWNLOAD_DIR", self.dl),
            mock.patch.object(config, "RUNTIME_DIR", self.rt),
            mock.patch.object(pixiv_dl, "_load_token",
                              return_value="TOKEN"),
            mock.patch.object(pixiv_dl, "resolve_user_id",
                              return_value=("93028519", "")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _fake_run(self, code=0, stdout=""):
        def fake(cmd, on_line=None, timeout=None, tag="x"):
            for ln in stdout.splitlines():
                if on_line:
                    on_line(ln)
            return code, stdout[-1500:], stdout
        return fake

    def test_run_user_counts_new_and_persists_state(self):
        out = ("./Pixiv/作者A_93028519/26-10-25_题1_151274187_p0.png\n"
               "./Pixiv/作者A_93028519/26-10-25_题2_151147399_p0.png\n")
        with mock.patch.object(pixiv_dl, "_run_proc",
                               side_effect=self._fake_run(0, out)):
            n, note = pixiv_dl.run_user("93028519")
        self.assertEqual(n, 2)
        self.assertEqual(note, "")
        st = pixiv_dl._load_state()
        self.assertEqual(st["93028519"]["total"], 2, st)
        # 目录名（作者名_ID）从落盘路径学出
        self.assertEqual(st["93028519"]["dir"], "作者A_93028519")
        # 增量：再跑一次新增 1
        with mock.patch.object(
                pixiv_dl, "_run_proc",
                side_effect=self._fake_run(
                    0,
                    "./Pixiv/作者A_93028519/26-10-25_题3_151300000_p0.png\n")):
            n2, _ = pixiv_dl.run_user("93028519")
        self.assertEqual(n2, 1)
        self.assertEqual(pixiv_dl._load_state()["93028519"]["total"], 3)

    def test_username_target_resolved_to_id_dir_and_state(self):
        """用户名入口：resolve 成 ID → URL/state 以 ID 为键；叶子目录
        由 gallery-dl 建（run_user 只建 Pixiv 根），目录名从输出学出。"""
        out = "./Pixiv/dramjou_101238283/26-10-25_题_100_p0.png\n"
        with mock.patch.object(
                pixiv_dl, "resolve_user_id",
                return_value=("101238283", "")), \
             mock.patch.object(pixiv_dl, "_run_proc",
                               side_effect=self._fake_run(0, out)) as rp:
            n, _ = pixiv_dl.run_user("dramjou")
        self.assertEqual(n, 1)
        self.assertIn("/users/101238283", rp.call_args[0][0][-1])
        st = pixiv_dl._load_state()
        self.assertIn("101238283", st)
        self.assertEqual(st["101238283"]["target"], "dramjou")
        self.assertEqual(st["101238283"]["dir"], "dramjou_101238283")
        self.assertTrue(os.path.isdir(os.path.join(self.dl, "Pixiv")))

    def test_progress_updates_running_state_via_on_line(self):
        out = "./Pixiv/93028519/a_p0.png\n./Pixiv/93028519/b_p0.png\n"
        pixiv_dl._PIX_RUNNING = {"target": "93028519",
                                 "started": time.time(), "done": 0}
        try:
            with mock.patch.object(pixiv_dl, "_run_proc",
                                   side_effect=self._fake_run(0, out)):
                pixiv_dl.run_user("93028519")
            self.assertGreaterEqual(pixiv_dl._PIX_RUNNING.get("done", 0), 2)
            self.assertEqual(pixiv_dl._PIX_RUNNING.get("user_id"),
                             "93028519")
        finally:
            pixiv_dl._PIX_RUNNING = {}

    def test_auth_failure_reauths_and_retries_once(self):
        calls = {"n": 0}

        def flaky(cmd, on_line=None, timeout=None, tag="x"):
            calls["n"] += 1
            if calls["n"] == 1:
                return 4, "Login failed: invalid_grant 'refresh-token'", ""
            for ln in "./Pixiv/93028519/x_p0.png\n".splitlines():
                if on_line:
                    on_line(ln)
            return 0, "./Pixiv/93028519/x_p0.png", "./Pixiv/93028519/x_p0.png"

        with mock.patch.object(pixiv_dl, "_run_proc", side_effect=flaky), \
             mock.patch.object(pixiv_dl, "_try_reauth",
                               return_value=(True, "ok")) as ra:
            n, note = pixiv_dl.run_user("93028519")
        self.assertEqual(calls["n"], 2)      # reauth 后重试了一次
        ra.assert_called_once()
        self.assertEqual(n, 1)
        self.assertEqual(note, "")

    def test_auth_failure_without_reauth_reports(self):
        def bad(cmd, on_line=None, timeout=None, tag="x"):
            return 4, "Login failed: invalid_grant", ""

        with mock.patch.object(pixiv_dl, "_run_proc", side_effect=bad), \
             mock.patch.object(pixiv_dl, "_try_reauth",
                               return_value=(False, "CDP 没连上")):
            n, note = pixiv_dl.run_user("93028519")
        self.assertEqual(n, 0)
        self.assertIn("reauth 失败", note)

    def test_resolve_failure_short_circuits(self):
        with mock.patch.object(
                pixiv_dl, "resolve_user_id",
                return_value=(None, "用户名「x」不存在")), \
             mock.patch.object(pixiv_dl, "_run_proc") as rp:
            n, note = pixiv_dl.run_user("x")
        rp.assert_not_called()
        self.assertEqual(n, 0)
        self.assertIn("不存在", note)

    def test_no_token_triggers_reauth(self):
        def ok_run(cmd, on_line=None, timeout=None, tag="x"):
            return 0, "", ""
        with mock.patch.object(pixiv_dl, "_load_token",
                               return_value=None), \
             mock.patch.object(pixiv_dl, "_try_reauth",
                               return_value=(True, "ok")) as ra, \
             mock.patch.object(pixiv_dl, "_run_proc", side_effect=ok_run):
            n, _ = pixiv_dl.run_user("93028519")
        ra.assert_called_once()
        self.assertEqual(n, 0)   # reauth 拿新 token 后空跑（无新文件）


class StatusTest(_DbTestCase):

    def test_status_progress(self):
        pixiv_dl._PIX_RUNNING = {"target": "dramjou", "user_id": "101238283",
                                 "started": time.time() - 95,
                                 "done": 7, "last": "26-10-25 1512_p0.png"}
        try:
            text = pixiv_dl.status_text()
            self.assertIn("101238283", text)
            self.assertIn("已收 7 个", text)
            self.assertIn("1512_p0.png", text)
        finally:
            pixiv_dl._PIX_RUNNING = {}

    def test_status_idle_list(self):
        with mock.patch.object(
                pixiv_dl, "_load_state",
                return_value={"93028519": {"total": 12, "last_run": 1790000000,
                                           "target": "93028519",
                                           "dir": "作者A_93028519"}}):
            text = pixiv_dl.status_text()
        self.assertIn("已跟画师", text)
        # 有学到的目录名（作者名_ID）优先展示
        self.assertIn("作者A_93028519：累计 12 个", text)


class CommandTest(_DbTestCase):

    def test_parse_and_bare_status(self):
        self.assertTrue(pixiv_dl.is_pixiv_command("/pixiv"))
        self.assertTrue(pixiv_dl.is_pixiv_command("/pixiv 93028519"))
        self.assertFalse(pixiv_dl.is_pixiv_command("/pixivx"))
        self.assertIn("/pixiv", asyncio.run(pixiv_dl.command_reply("/pixiv")))

    def test_enqueue_and_duplicate(self):
        """提交 → 入队；重复提交 → 单队列判重（迁移语义）。"""
        out = asyncio.run(pixiv_dl.command_reply("/pixiv 93028519"))
        self.assertIn("已入队", out)
        out2 = asyncio.run(pixiv_dl.command_reply("/pixiv 93028519"))
        self.assertIn("已在队列", out2)
        self.assertEqual(runtime_db.XI_TASKS.count(), 1)

    def test_notify_on_completion(self):
        """worker.process 完成链：DONE + 完成通知 + 进度视图清空。"""
        notes = []

        async def fake_notify(text):
            notes.append(text)

        runtime_db.XI_TASKS.enqueue("93028519")
        task = runtime_db.XI_TASKS.claim_next()
        with mock.patch.object(pixiv_dl, "run_user", return_value=(5, "")), \
             mock.patch("tg_userbot.notify.notify_user", fake_notify):
            asyncio.run(pixiv_dl._WORKER.process(task))
        self.assertEqual(runtime_db.XI_TASKS.get("93028519")["status"], "DONE")
        self.assertEqual(len(notes), 1)
        self.assertIn("新增 5 个", notes[0])
        self.assertEqual(pixiv_dl._PIX_RUNNING, {})


class RestartRecoveryTest(_DbTestCase):
    """停机/租约路径（DB 队列语义；RUNNING_MARK 已随迁移移除）。"""

    def test_shutdown_cancel_requeues(self):
        """停机取消 worker → 在途回 PENDING（框架 release），重启自续。"""
        runtime_db.XI_TASKS.enqueue("u2")

        async def go():
            loop_task = asyncio.ensure_future(pixiv_dl._WORKER.run())
            await asyncio.sleep(0.25)   # 进入 process（run_user 睡 0.4）
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

        with mock.patch.object(pixiv_dl, "run_user",
                               side_effect=lambda *a: time.sleep(0.4)):
            asyncio.run(go())
        self.assertEqual(runtime_db.XI_TASKS.get("u2")["status"], "PENDING")

    def test_stop_marks_failed_terminal(self):
        """/pixiv stop → 在途标 FAILED（不回队列不打架），队列继续。"""
        runtime_db.XI_TASKS.enqueue("u2")
        runtime_db.XI_TASKS.enqueue("u4")

        async def go():
            loop_task = asyncio.ensure_future(pixiv_dl._WORKER.run())
            await asyncio.sleep(0.25)
            pixiv_dl.pixiv_stop()
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

        with mock.patch.object(pixiv_dl, "run_user",
                               side_effect=lambda *a: time.sleep(0.4)):
            asyncio.run(go())
        self.assertEqual(runtime_db.XI_TASKS.get("u2")["status"], "FAILED")
        self.assertIn(runtime_db.XI_TASKS.get("u4")["status"],
                      ("PENDING", "PROCESSING"))


class AuthFalsePositiveTest(unittest.TestCase):
    """评审收紧回归：正常输出（Refreshing access token）/ 文件名里的
    数字子串不得误判认证失效——否则任何网络抖动都触发 3 分钟 oauth。"""

    def setUp(self):
        # 本类断言「不触发重试」——关掉自动重跑档
        _rp = mock.patch.object(config, 'GALLERY_DL_AUTO_RETRY_MAX', 0)
        _rp.start()
        self.addCleanup(_rp.stop)
        # run_user 会写 _PIX_RUNNING——用例后归位防串扰后续类
        self.addCleanup(setattr, pixiv_dl, "_PIX_RUNNING", {})
        base = tempfile.mkdtemp(dir=_TMP)
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.dl = os.path.join(base, "downloads")
        self.rt = os.path.join(base, "runtime")
        os.makedirs(self.dl, exist_ok=True)
        os.makedirs(self.rt, exist_ok=True)
        for p in (mock.patch.object(config, "DOWNLOAD_DIR", self.dl),
                  mock.patch.object(config, "RUNTIME_DIR", self.rt),
                  mock.patch.object(pixiv_dl, "_load_token",
                                    return_value="TOKEN"),
                  mock.patch.object(pixiv_dl, "resolve_user_id",
                                    return_value=("93028519", ""))):
            p.start()
            self.addCleanup(p.stop)

    def test_normal_refresh_log_and_id_substring_do_not_reauth(self):
        calls = {"n": 0}

        def flaky(cmd, on_line=None, timeout=None, tag="x"):
            calls["n"] += 1
            # 正常信息行 + 含 401 子串的作品ID + 真实失败码
            return 4, ("[pixiv][info] Refreshing access token\n"
                       "./Pixiv/x_9/26-10-25_题_174012345_p0.png\n"
                       "urllib3 HTTPError"), ""

        with mock.patch.object(pixiv_dl, "_run_proc", side_effect=flaky), \
             mock.patch.object(pixiv_dl, "_try_reauth") as ra:
            n, note = pixiv_dl.run_user("93028519")
        self.assertEqual(calls["n"], 1, "非强信号失败不得触发重试")
        ra.assert_not_called()
        self.assertIn("退出码 4", note)

    def test_strong_signal_still_reauths(self):
        calls = {"n": 0}

        def bad(cmd, on_line=None, timeout=None, tag="x"):
            calls["n"] += 1
            if calls["n"] == 1:
                return 4, "Login failed: invalid_grant", ""
            return 0, "", ""

        with mock.patch.object(pixiv_dl, "_run_proc", side_effect=bad), \
             mock.patch.object(pixiv_dl, "_try_reauth",
                               return_value=(True, "ok")):
            n, _ = pixiv_dl.run_user("93028519")
        self.assertEqual(calls["n"], 2)


class AutoRetryTest(unittest.TestCase):
    """失败自动重跑（与 /x 同款定档）：重试只发生在下载环内，
    解析失败不重试。"""

    def setUp(self):
        base = tempfile.mkdtemp(dir=_TMP)
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.dl = os.path.join(base, "downloads")
        self.rt = os.path.join(base, "runtime")
        os.makedirs(self.dl, exist_ok=True)
        os.makedirs(self.rt, exist_ok=True)
        for p in (mock.patch.object(config, "DOWNLOAD_DIR", self.dl),
                  mock.patch.object(config, "RUNTIME_DIR", self.rt),
                  mock.patch.object(pixiv_dl, "_load_token",
                                    return_value="TOKEN"),
                  mock.patch.object(pixiv_dl, "resolve_user_id",
                                    return_value=("93028519", "")),
                  mock.patch.object(config, "GALLERY_DL_AUTO_RETRY_MAX", 2),
                  mock.patch.object(config, "GALLERY_DL_AUTO_RETRY_BASE_DELAY",
                                    0.01),
                  mock.patch.object(config, "GALLERY_DL_AUTO_RETRY_MAX_DELAY",
                                    0.02)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(setattr, pixiv_dl, "_PIX_RUNNING", {})

    def _fake_seq(self, results):
        seq = list(results)

        def fake(cmd, on_line=None, timeout=None, tag="x"):
            code, out = seq.pop(0)
            for ln in (out or "").splitlines():
                if on_line:
                    on_line(ln)
            return code, (out or "")[-1500:], out or ""
        return fake

    def test_fail_then_success_accumulates(self):
        results = [(4, "./Pixiv/x_9/a_p0.png"), (0, "./Pixiv/x_9/b_p0.png")]
        with mock.patch.object(pixiv_dl, "_run_proc",
                               side_effect=self._fake_seq(results)) as rp:
            n, note = pixiv_dl.run_user("93028519")
        self.assertEqual(rp.call_count, 2)
        self.assertEqual(n, 2)
        self.assertEqual(note, "")
        st = pixiv_dl._load_state()
        self.assertEqual(st["93028519"]["total"], 2)

    def test_resolve_failure_not_retried(self):
        with mock.patch.object(
                pixiv_dl, "resolve_user_id",
                return_value=(None, "用户名「x」不存在")), \
             mock.patch.object(pixiv_dl, "_run_proc") as rp:
            n, note = pixiv_dl.run_user("x")
        rp.assert_not_called()
        self.assertIn("不存在", note)

    def test_exhaust_reports_retry_count(self):
        with mock.patch.object(
                pixiv_dl, "_run_proc",
                side_effect=self._fake_seq([(4, "net"), (4, ""),
                                            (4, "")])) as rp:
            n, note = pixiv_dl.run_user("93028519")
        self.assertEqual(rp.call_count, 3)
        self.assertIn("已自动重跑 2 次仍失败", note)
