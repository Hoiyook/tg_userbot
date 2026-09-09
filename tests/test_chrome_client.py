"""Chrome Client（chrome_client.py，User Bot 侧）的单元测试：命令解析、
URL 验证、进程管理（幂等/过期 PID）、requests 映射持久化、结果通知路由。

不联网、不真拉 Chrome/Agent（进程管理用真 sleep 进程或 mock）。
运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_chrome_client_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import chrome_agent  # noqa: E402
from tg_userbot import chrome_client  # noqa: E402
from tg_userbot import config  # noqa: E402


class ChromeCommandParseTest(unittest.TestCase):
    """命令识别（规格 37）：四条合法命令 + 非法形态。"""

    def test_valid_commands(self):
        self.assertTrue(chrome_client.is_chrome_command("/chrome_start"))
        self.assertTrue(chrome_client.is_chrome_command("/chrome_stop"))
        self.assertTrue(chrome_client.is_chrome_command("/chrome_status"))
        self.assertTrue(chrome_client.is_chrome_command(
            "/chrome https://example.com/a.zip"))

    def test_invalid_commands(self):
        self.assertFalse(chrome_client.is_chrome_command("/chrome"))
        self.assertFalse(chrome_client.is_chrome_command("/chrome abc"))
        self.assertFalse(chrome_client.is_chrome_command("/chromestatus"))
        self.assertFalse(chrome_client.is_chrome_command("/status"))
        self.assertFalse(chrome_client.is_chrome_command(""))


class ChromeDownloadDirTest(unittest.TestCase):
    """下载目录（规格 21）：<SAVE_FOLDER>/TG Chrome Download。"""

    def test_dir_matches_save_folder(self):
        self.assertEqual(
            chrome_client.download_dir(),
            os.path.join(config.SAVE_FOLDER, "TG Chrome Download"),
        )


class ChromeRequestsStoreTest(unittest.TestCase):
    """chrome_requests.json：task_id → 用户映射持久化（规格 32）。"""

    def setUp(self):
        self.path = os.path.join(_TMP, "chrome_requests_store.json")
        if os.path.exists(self.path):
            os.remove(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_add_and_get_roundtrip(self):
        chrome_client.add_request(
            "t1", "https://a.com/1.zip",
            user_id=545, chat_id=545, message_id=7, path=self.path)
        chrome_client.add_request(
            "t2", "https://a.com/2.zip",
            user_id=545, chat_id=999, message_id=8, path=self.path)
        rec = chrome_client.get_request("t1", path=self.path)
        self.assertEqual(rec["url"], "https://a.com/1.zip")
        self.assertEqual(rec["chat_id"], 545)
        self.assertEqual(rec["message_id"], 7)
        self.assertNotIn("notified_at", rec)
        self.assertEqual(len(chrome_client.load_requests(self.path)), 2)

    def test_missing_file_returns_none(self):
        self.assertIsNone(chrome_client.get_request("nope", path=self.path))

    def test_mark_notified_persists(self):
        chrome_client.add_request(
            "t1", "https://a.com/1.zip",
            user_id=1, chat_id=1, message_id=1, path=self.path)
        chrome_client.mark_notified("t1", path=self.path)
        rec = chrome_client.get_request("t1", path=self.path)
        self.assertIn("notified_at", rec)


class AgentProcessTest(unittest.TestCase):
    """Agent 进程管理（规格 33）：PID 文件 + 实际进程检测 + 幂等。"""

    def setUp(self):
        self.pid_file = os.path.join(_TMP, "chrome_agent_test.pid")
        if os.path.exists(self.pid_file):
            os.remove(self.pid_file)

    def tearDown(self):
        if os.path.exists(self.pid_file):
            os.remove(self.pid_file)

    def test_no_pid_file_means_stopped(self):
        with mock.patch.object(chrome_client, "CHROME_AGENT_PID_FILE",
                               self.pid_file):
            self.assertIsNone(chrome_client.agent_pid())
            self.assertFalse(chrome_client.agent_running())

    def test_stale_pid_file_means_stopped(self):
        """PID 文件存在但进程早已死亡 → 必须判定为停止（规格 33）。"""
        with open(self.pid_file, "w") as f:
            f.write("999999999")
        with mock.patch.object(chrome_client, "CHROME_AGENT_PID_FILE",
                               self.pid_file):
            self.assertFalse(chrome_client.agent_running())

    def test_living_unrelated_pid_rejected(self):
        """PID 存活但不是 chrome_agent 进程（PID 复用）→ 判定停止。"""
        sleeper = subprocess.Popen(["/bin/sleep", "5"])
        try:
            with open(self.pid_file, "w") as f:
                f.write(str(sleeper.pid))
            with mock.patch.object(chrome_client, "CHROME_AGENT_PID_FILE",
                                   self.pid_file):
                self.assertFalse(chrome_client.agent_running())
        finally:
            sleeper.kill()
            sleeper.wait()

    def test_real_agent_process_detected(self):
        """真 chrome_agent 模块进程（sleep 顶名检测不可靠，直接用命令行
        含 chrome_agent 的真进程验证识别逻辑）。"""
        proc = subprocess.Popen(
            ["/bin/sh", "-c", "exec -a tg_userbot.chrome_agent /bin/sleep 5"])
        try:
            with open(self.pid_file, "w") as f:
                f.write(str(proc.pid))
            with mock.patch.object(chrome_client, "CHROME_AGENT_PID_FILE",
                                   self.pid_file):
                self.assertTrue(chrome_client.agent_running())
        finally:
            proc.kill()
            proc.wait()

    def test_stop_agent_terminates_process(self):
        with mock.patch.object(chrome_client, "CHROME_AGENT_PID_FILE",
                               self.pid_file):
            # 造一个「活着的 agent」：直接写 pid 文件 + 检测用 mock 打通
            sleeper = subprocess.Popen(["/bin/sleep", "30"])
            with open(self.pid_file, "w") as f:
                f.write(str(sleeper.pid))
            with mock.patch.object(chrome_client, "_pid_command",
                                   return_value="tg_userbot.chrome_agent"):
                self.assertTrue(chrome_client.agent_running())
                stopped = chrome_client.stop_agent()
            self.assertTrue(stopped)
            self.assertIsNone(chrome_client.agent_pid())
            sleeper.wait(timeout=5)  # SIGTERM 后进程应已终止
            self.assertIsNotNone(sleeper.poll())

    def test_stop_when_not_running_returns_false(self):
        with mock.patch.object(chrome_client, "CHROME_AGENT_PID_FILE",
                               self.pid_file):
            self.assertFalse(chrome_client.stop_agent())


class SpawnAgentTest(unittest.TestCase):
    """spawn_agent：同一解释器 -m 模块启动、脱离进程组。"""

    def test_spawn_command_shape(self):
        with mock.patch.object(chrome_client.subprocess,
                               "Popen") as popen:
            chrome_client.spawn_agent()
        args = popen.call_args[0][0]
        self.assertEqual(args[:3],
                         [chrome_client.sys.executable, "-m",
                          "tg_userbot.chrome_agent"])
        self.assertTrue(popen.call_args[1].get("start_new_session"))


class OwnerCheckTest(unittest.TestCase):
    """权限（规格 35）：只有 Owner 能执行 chrome 命令。"""

    def test_owner_id_prefers_config_override(self):
        with mock.patch.object(config, "CHROME_AGENT_OWNER_ID", 777):
            self.assertEqual(chrome_client.resolve_owner_id(my_id=111), 777)

    def test_owner_id_falls_back_to_my_id(self):
        with mock.patch.object(config, "CHROME_AGENT_OWNER_ID", None):
            self.assertEqual(chrome_client.resolve_owner_id(my_id=111), 111)


class StatusTextStatsTest(unittest.TestCase):
    """/chrome_status 任务统计（验收反馈：任务成功后状态里什么都看不到）。

    统计必须覆盖终态任务（成功/失败），不能只看 RUNNING 与排队。"""

    def test_status_includes_terminal_statistics(self):
        from tg_userbot import state as _state
        tasks = [
            chrome_agent.create_task("https://a.com/1.zip", "s1"),
        ]
        chrome_agent.start_attempt(tasks[0])
        chrome_agent.finish_success(tasks[0], "1.zip", 4096)
        failed = chrome_agent.create_task("https://a.com/2.zip", "f1")
        for _ in range(3):
            chrome_agent.start_attempt(failed)
            chrome_agent.fail_attempt(failed, "超时", retries=3, wait_seconds=0)
        pending = chrome_agent.create_task("https://a.com/3.zip", "p1")
        tasks += [failed, pending]

        text = chrome_client.status_text(
            agent_up=True, chrome_running=True, cdp_ok=True,
            tasks=tasks, dl_dir="/dl")

        self.assertIn("任务统计", text)
        self.assertIn("累计 3", text)
        self.assertIn("成功 1", text)
        self.assertIn("失败 1", text)
        self.assertIn("排队 1", text)
        self.assertIn("1.zip", text)  # 最近完成的任务可见


class ChromeFlowsTest(unittest.IsolatedAsyncioTestCase):
    """/chrome_start /chrome_stop /chrome_status /chrome 的命令流。"""

    def setUp(self):
        self.requests_path = os.path.join(_TMP, "chrome_requests_flow.json")
        self.pid_file = os.path.join(_TMP, "chrome_agent_flow.pid")
        for p in (self.requests_path, self.pid_file):
            if os.path.exists(p):
                os.remove(p)
        self.replies = []

        class _Event:
            sender_id = 545

            async def reply(self, text, **kw):
                self_out.replies.append(text)

        self_out = self
        self.event = _Event()

    def tearDown(self):
        for p in (self.requests_path, self.pid_file):
            if os.path.exists(p):
                os.remove(p)

    def _patch(self, running):
        return (
            mock.patch.object(chrome_client, "CHROME_REQUESTS_FILE",
                              self.requests_path),
            mock.patch.object(chrome_client, "CHROME_AGENT_PID_FILE",
                              self.pid_file),
            mock.patch.object(chrome_client, "agent_running",
                              lambda: running),
        )

    async def test_submit_creates_request_and_replies_task_id(self):
        patches = self._patch(running=True)
        with patches[0], patches[1], patches[2]:
            handled = await chrome_client.handle_chrome_command(
                self.event, "/chrome https://example.com/a.zip",
                owner_id=545)
        self.assertTrue(handled)
        self.assertEqual(len(self.replies), 1)
        self.assertIn("任务 ID", self.replies[0])
        reqs = chrome_client.load_requests(self.requests_path)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]["chat_id"], 545)

    def _noop_patches(self, running):
        return self._patch(running=running)

    async def test_submit_when_agent_stopped_prompts_start(self):
        patches = self._patch(running=False)
        with patches[0], patches[1], patches[2]:
            handled = await chrome_client.handle_chrome_command(
                self.event, "/chrome https://example.com/a.zip",
                owner_id=545)
        self.assertTrue(handled)
        self.assertIn("/chrome_start", self.replies[0])
        # 不自动启动（规格 15），也不产生任务请求
        self.assertEqual(chrome_client.load_requests(self.requests_path), [])

    async def test_submit_rejects_invalid_url(self):
        patches = self._patch(running=True)
        with patches[0], patches[1], patches[2]:
            handled = await chrome_client.handle_chrome_command(
                self.event, "/chrome abc", owner_id=545)
        self.assertTrue(handled)
        self.assertIn("URL", self.replies[0])
        self.assertEqual(chrome_client.load_requests(self.requests_path), [])

    async def test_wrong_owner_is_rejected(self):
        patches = self._patch(running=True)
        with patches[0], patches[1], patches[2]:
            handled = await chrome_client.handle_chrome_command(
                self.event, "/chrome https://example.com/a.zip",
                owner_id=545, sender_id=999)
        self.assertTrue(handled)  # 命令被识别并拦截
        self.assertIn("无权", self.replies[0])
        self.assertEqual(chrome_client.load_requests(self.requests_path), [])

    async def test_start_when_already_running_is_idempotent(self):
        patches = self._patch(running=True)
        with patches[0], patches[1], patches[2]:
            with mock.patch.object(chrome_client, "spawn_agent") as spawn:
                handled = await chrome_client.handle_chrome_command(
                    self.event, "/chrome_start", owner_id=545)
            spawn.assert_not_called()
        self.assertTrue(handled)
        self.assertIn("已经在运行", self.replies[0])

    async def test_start_spawns_when_stopped(self):
        patches = self._patch(running=False)

        async def fake_wait(*a, **k):
            return True

        with patches[0], patches[1], patches[2]:
            with mock.patch.object(chrome_client, "spawn_agent") as spawn, \
                    mock.patch.object(chrome_client, "wait_agent_up",
                                      side_effect=fake_wait):
                handled = await chrome_client.handle_chrome_command(
                    self.event, "/chrome_start", owner_id=545)
            spawn.assert_called_once()
        self.assertTrue(handled)
        self.assertIn("启动成功", self.replies[0])

    async def test_stop_when_running(self):
        patches = self._patch(running=True)
        with patches[0], patches[1], patches[2]:
            with mock.patch.object(chrome_client, "stop_agent",
                                   return_value=True):
                handled = await chrome_client.handle_chrome_command(
                    self.event, "/chrome_stop", owner_id=545)
        self.assertTrue(handled)
        self.assertIn("已停止", self.replies[0])
        self.assertIn("Chrome", self.replies[0])  # 明示 Chrome 不受影响

    async def test_status_when_stopped_prompts_start(self):
        patches = self._patch(running=False)

        async def fake_cdp():
            return False

        with patches[0], patches[1], patches[2], \
                mock.patch.object(chrome_client, "cdp_available",
                                  side_effect=fake_cdp), \
                mock.patch.object(chrome_client, "chrome_app_running",
                                  return_value=True):
            handled = await chrome_client.handle_chrome_command(
                self.event, "/chrome_status", owner_id=545)
        self.assertTrue(handled)
        self.assertIn("Stopped", self.replies[0])
        self.assertIn("/chrome_start", self.replies[0])


class NotifyScanTest(unittest.IsolatedAsyncioTestCase):
    """结果通知：终态任务按映射路由给用户，通知后打标记不重发（规格 32）。"""

    def setUp(self):
        self.tasks_path = os.path.join(_TMP, "chrome_tasks_notify.json")
        self.requests_path = os.path.join(_TMP, "chrome_requests_notify.json")
        for p in (self.tasks_path, self.requests_path):
            if os.path.exists(p):
                os.remove(p)

    def tearDown(self):
        for p in (self.tasks_path, self.requests_path):
            if os.path.exists(p):
                os.remove(p)

    def _seed(self):
        task = chrome_agent.create_task("https://a.com/1.zip", "t1")
        chrome_agent.finish_success(task, "1.zip", 4096)
        chrome_agent.save_tasks([task], self.tasks_path)
        chrome_client.add_request("t1", "https://a.com/1.zip",
                                  user_id=545, chat_id=545, message_id=9,
                                  path=self.requests_path)

    async def test_terminal_task_notifies_once(self):
        self._seed()
        sent = []

        async def fake_send(chat_id, text):
            sent.append((chat_id, text))

        with mock.patch.object(chrome_client, "CHROME_TASKS_FILE",
                               self.tasks_path), \
                mock.patch.object(chrome_client, "CHROME_REQUESTS_FILE",
                                  self.requests_path), \
                mock.patch.object(chrome_client, "send_owner_message",
                                  fake_send):
            await chrome_client.notify_pending_results()
            await chrome_client.notify_pending_results()  # 第二轮不重发
        self.assertEqual(len(sent), 1)
        chat_id, text = sent[0]
        self.assertEqual(chat_id, 545)
        self.assertIn("SUCCESS", text)
        self.assertIn("4096", text)  # 精确 bytes 展示
        rec = chrome_client.get_request("t1", path=self.requests_path)
        self.assertIn("notified_at", rec)

    async def test_failed_task_notifies_with_error(self):
        task = chrome_agent.create_task("https://a.com/2.zip", "t2")
        for _ in range(3):  # 三次尝试全失败 → FAILED（attempts=3）
            chrome_agent.start_attempt(task)
            chrome_agent.fail_attempt(task, "下载超时", retries=3,
                                      wait_seconds=0)
        chrome_agent.save_tasks([task], self.tasks_path)
        chrome_client.add_request("t2", "https://a.com/2.zip",
                                  user_id=545, chat_id=545, message_id=10,
                                  path=self.requests_path)
        sent = []

        async def fake_send(chat_id, text):
            sent.append((chat_id, text))

        with mock.patch.object(chrome_client, "CHROME_TASKS_FILE",
                               self.tasks_path), \
                mock.patch.object(chrome_client, "CHROME_REQUESTS_FILE",
                                  self.requests_path), \
                mock.patch.object(chrome_client, "send_owner_message",
                                  fake_send):
            await chrome_client.notify_pending_results()
        self.assertEqual(len(sent), 1)
        self.assertIn("FAILED", sent[0][1])
        self.assertIn("下载超时", sent[0][1])


if __name__ == "__main__":
    unittest.main()


class ChromeLabelParseTest(unittest.IsolatedAsyncioTestCase):
    """/chrome <#标注> <URL>：标注拼在原文件名前（空格分隔）。"""

    def setUp(self):
        self.requests_path = os.path.join(_TMP, "chrome_requests_label.json")
        if os.path.exists(self.requests_path):
            os.remove(self.requests_path)
        self.replies = []

        class _Event:
            sender_id = 545

            async def reply(self, text, **kw):
                self_out.replies.append(text)

        self_out = self
        self.event = _Event()

    def tearDown(self):
        if os.path.exists(self.requests_path):
            os.remove(self.requests_path)

    def _run(self, cmd_text):
        return chrome_client.handle_chrome_command(
            self.event, cmd_text, owner_id=545)

    async def test_label_and_url_parsed(self):
        with mock.patch.object(chrome_client, "CHROME_REQUESTS_FILE",
                               self.requests_path), \
                mock.patch.object(chrome_client, "agent_running",
                                  lambda: True):
            handled = await self._run(
                "/chrome #你好#260908 https://example.com/a.zip")
        self.assertTrue(handled)
        reqs = chrome_client.load_requests(self.requests_path)
        self.assertEqual(reqs[0].get("label"), "#你好#260908")
        self.assertEqual(reqs[0]["url"], "https://example.com/a.zip")

    async def test_plain_url_has_no_label(self):
        with mock.patch.object(chrome_client, "CHROME_REQUESTS_FILE",
                               self.requests_path), \
                mock.patch.object(chrome_client, "agent_running",
                                  lambda: True):
            await self._run("/chrome https://example.com/a.zip")
        reqs = chrome_client.load_requests(self.requests_path)
        self.assertIsNone(reqs[0].get("label"))

    async def test_label_with_invalid_url_rejected(self):
        with mock.patch.object(chrome_client, "CHROME_REQUESTS_FILE",
                               self.requests_path), \
                mock.patch.object(chrome_client, "agent_running",
                                  lambda: True):
            await self._run("/chrome #你好#260908 not-a-url")
        self.assertTrue(any("URL" in r for r in self.replies))
        self.assertEqual(chrome_client.load_requests(self.requests_path), [])



if __name__ == "__main__":
    unittest.main()

class StatusUnclaimedTest(unittest.TestCase):
    """/chrome_status 必须显示「已提交未认领」的请求（验收反馈：当前任务
    下载中时再提交的链接，提交回执说进了队列，状态里却看不见）。"""

    def _tasks_with_running(self):
        t = chrome_agent.create_task("https://a.com/1.zip", "run1")
        chrome_agent.start_attempt(t)
        return [t]

    def test_unclaimed_requests_shown(self):
        text = chrome_client.status_text(
            agent_up=True, chrome_running=True, cdp_ok=True,
            tasks=self._tasks_with_running(), dl_dir="/dl",
            unclaimed=[{"task_id": "u1", "url": "https://a.com/2.zip"}])
        self.assertIn("待入队", text)
        self.assertIn("2.zip", text)

    def test_no_unclaimed_no_noise(self):
        text = chrome_client.status_text(
            agent_up=True, chrome_running=True, cdp_ok=True,
            tasks=self._tasks_with_running(), dl_dir="/dl",
            unclaimed=[])
        self.assertNotIn("待入队", text)


class StatusUnclaimedFlowTest(unittest.IsolatedAsyncioTestCase):
    """/chrome_status 命令流：requests 里有 tasks 中不存在的请求 → 计入待入队。"""

    def setUp(self):
        self.tasks_path = os.path.join(_TMP, "chrome_tasks_unclaimed.json")
        self.requests_path = os.path.join(_TMP, "chrome_requests_unclaimed.json")
        for p in (self.tasks_path, self.requests_path):
            if os.path.exists(p):
                os.remove(p)
        self.replies = []

        class _Event:
            sender_id = 545

            async def reply(self, text, **kw):
                self_out.replies.append(text)

        self_out = self
        self.event = _Event()

    def tearDown(self):
        for p in (self.tasks_path, self.requests_path):
            if os.path.exists(p):
                os.remove(p)

    async def test_status_flow_counts_unclaimed_request(self):
        # 正在跑的任务在 tasks.json；第二个提交只在 requests.json
        running = chrome_agent.create_task("https://a.com/1.zip", "run1")
        chrome_agent.start_attempt(running)
        chrome_agent.save_tasks([running], self.tasks_path)
        chrome_client.add_request("run1", "https://a.com/1.zip",
                                  user_id=545, chat_id=545, message_id=1,
                                  path=self.requests_path)
        chrome_client.add_request("new2", "https://a.com/2.zip",
                                  user_id=545, chat_id=545, message_id=2,
                                  path=self.requests_path)

        async def fake_cdp():
            return True

        with mock.patch.object(chrome_client, "CHROME_TASKS_FILE",
                               self.tasks_path), \
                mock.patch.object(chrome_client, "CHROME_REQUESTS_FILE",
                                  self.requests_path), \
                mock.patch.object(chrome_client, "agent_running",
                                  lambda: True), \
                mock.patch.object(chrome_client, "chrome_app_running",
                                  return_value=True), \
                mock.patch.object(chrome_client, "cdp_available",
                                  side_effect=fake_cdp):
            handled = await chrome_client.handle_chrome_command(
                self.event, "/chrome_status", owner_id=545)
        self.assertTrue(handled)
        status = self.replies[0]
        self.assertIn("待入队", status)
        self.assertIn("2.zip", status)
        self.assertNotIn("1.zip", status.split("待入队")[1].split("\n")[0])
