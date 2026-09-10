"""Chrome Client（chrome_client.py，User Bot 侧）的单元测试：命令解析、
URL 验证、进程管理（幂等/过期 PID）、requests 映射持久化、结果通知路由。

不联网、不真拉 Chrome/Agent（进程管理用真 sleep 进程或 mock）。
运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import asyncio
import atexit
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_chrome_client_test_")
# 退出时回收临时目录（测试跑完就地删，别让 /var/folders 越堆越多）
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import chrome_agent  # noqa: E402
from tg_userbot import chrome_client  # noqa: E402
from tg_userbot import config  # noqa: E402


class ChromeCommandParseTest(unittest.TestCase):
    """命令分发（规格 37）：`is_chrome_dispatch` 是唯一入口。

    刻意"宽"——非法形态（/chrome 无参、/chrome abc 非法 URL）也要落进
    handle_chrome_command 里回一句人话，而不是掉进普通下载逻辑里悄无声息。
    历史注：曾有一个更严格的 `is_chrome_command`（还要求 URL 合法），但没有任何
    生产代码调用它、只有测试在喂它，2026-09-10 删掉并把这些断言改成测真谓词。
    """

    def test_all_chrome_forms_are_dispatched(self):
        for text in ("/chrome_start", "/chrome_stop", "/chrome_status",
                     "/chrome_tasks", "/chrome_cancel", "/chrome_cancel 2",
                     "/chrome https://example.com/a.zip",
                     "/chrome #标 https://a.com/t.zip",
                     "/chrome A/B/#标 https://a.com/t.zip",
                     "/chrome", "/chrome abc"):
            with self.subTest(text=text):
                self.assertTrue(chrome_client.is_chrome_dispatch(text))

    def test_non_chrome_commands_not_dispatched(self):
        for text in ("/chromestatus", "/status", "/chrome2", "chrome_start",
                     "", "   ", None):
            with self.subTest(text=text):
                self.assertFalse(chrome_client.is_chrome_dispatch(text))


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

    async def test_cancelled_task_notifies_once(self):
        """§21 测试 8：CANCELLED 也发通知，并按 notified_at 幂等。"""
        task = chrome_agent.create_task("https://a.com/3.zip", "t3")
        chrome_agent.mark_cancelled(task)
        chrome_agent.save_tasks([task], self.tasks_path)
        chrome_client.add_request("t3", "https://a.com/3.zip",
                                  user_id=545, chat_id=545, message_id=11,
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
            await chrome_client.notify_pending_results()  # 第二轮不重发
        self.assertEqual(len(sent), 1)
        self.assertIn("CANCELLED", sent[0][1])
        self.assertIn("已取消", sent[0][1])
        self.assertIn("用户主动取消", sent[0][1])
        rec = chrome_client.get_request("t3", path=self.requests_path)
        self.assertIn("notified_at", rec)


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


class ChromeSubdirParseTest(unittest.TestCase):
    """`/chrome [目录/][#标注] URL` 的解析规则。

    核心规则（任务书 §1）：**最后一个 `/` 之后的 `#xxx` 是文件名标注，
    前面的全部内容才是目录路径**。
    """

    def _parse(self, text):
        m = chrome_client._CHROME_CMD_RE.fullmatch(text.strip())
        self.assertIsNotNone(m, f"现有正则应当能接受：{text}")
        return chrome_client.parse_chrome_submit(m)

    def test_url_only(self):
        self.assertEqual(self._parse("/chrome https://a.com/t.zip"),
                         ("https://a.com/t.zip", None, None))

    def test_label_only(self):
        self.assertEqual(self._parse("/chrome #标 https://a.com/t.zip"),
                         ("https://a.com/t.zip", "#标", None))

    def test_one_level_subdir(self):
        self.assertEqual(self._parse("/chrome A/#标 https://a.com/t.zip"),
                         ("https://a.com/t.zip", "#标", "A"))

    def test_two_level_subdir(self):
        self.assertEqual(self._parse("/chrome A/B/#标 https://a.com/t.zip"),
                         ("https://a.com/t.zip", "#标", "A/B"))

    def test_three_level_subdir(self):
        self.assertEqual(self._parse("/chrome A/B/C/#标 https://a.com/t.zip"),
                         ("https://a.com/t.zip", "#标", "A/B/C"))

    def test_single_level_dir_without_label(self):
        """「只有目录、没有 #标注」：头 token 是目录，URL 仍是最后一个 token。"""
        self.assertEqual(self._parse("/chrome A https://a.com/t.zip"),
                         ("https://a.com/t.zip", None, "A"))

    def test_two_level_dir_without_label(self):
        """2026-09-10 实测回归：`/chrome Hyuk/250630 <URL>` 曾被当成
        「URL 无效：Hyuk/250630」——目录形态不要求最后一段带 #。"""
        self.assertEqual(
            self._parse("/chrome Hyuk/250630 https://a.com/t.zip"),
            ("https://a.com/t.zip", None, "Hyuk/250630"))

    def test_head_token_that_is_itself_a_url_keeps_old_behaviour(self):
        """头 token 本身就是合法 http(s) URL 时仍按 URL 解析（旧行为），
        绝不把它降级成目录——否则会凭空建出名叫 `https:` 的目录。"""
        url, label, subdir = self._parse(
            "/chrome https://a.com/1.zip https://b.com/2.zip")
        self.assertEqual(url, "https://a.com/1.zip")
        self.assertIsNone(label)
        self.assertIsNone(subdir)

    def test_subdir_forms_reach_the_handler(self):
        """分发谓词必须放过带子目录/标注的命令——否则它们会被判成「不是
        chrome 命令」，掉进普通下载逻辑里连回执都没有。"""
        for text in ("/chrome A/B/#标 https://a.com/t.zip",
                     "/chrome A/B https://a.com/t.zip",
                     "/chrome https://a.com/t.zip",
                     "/chrome A/B/#标 not-a-url"):
            with self.subTest(text=text):
                self.assertTrue(chrome_client.is_chrome_dispatch(text))
        # URL 合不合法由 handler 判定（非法就回执 URL 无效），解析规则两边一致
        self.assertTrue(chrome_client.is_chrome_dispatch(
            "/chrome A/B not-a-url"))


class ChromeSubdirCommandFlowTest(unittest.IsolatedAsyncioTestCase):
    """/chrome 带子目录时的提交链路：request 落盘 + 回执文案。"""

    def setUp(self):
        self.requests_path = os.path.join(_TMP, "chrome_requests_subdir.json")
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

    async def test_subdir_and_label_stored_in_request(self):
        with mock.patch.object(chrome_client, "CHROME_REQUESTS_FILE",
                               self.requests_path), \
                mock.patch.object(chrome_client, "agent_running",
                                  lambda: True):
            await self._run("/chrome A/B/#标 https://example.com/a.zip")
        reqs = chrome_client.load_requests(self.requests_path)
        self.assertEqual(reqs[0]["url"], "https://example.com/a.zip")
        self.assertEqual(reqs[0]["label"], "#标")
        self.assertEqual(reqs[0]["download_subdir"], "A/B")

    async def test_plain_url_has_no_subdir_key(self):
        with mock.patch.object(chrome_client, "CHROME_REQUESTS_FILE",
                               self.requests_path), \
                mock.patch.object(chrome_client, "agent_running",
                                  lambda: True):
            await self._run("/chrome https://example.com/a.zip")
        reqs = chrome_client.load_requests(self.requests_path)
        self.assertIsNone(reqs[0].get("download_subdir"))

    async def test_label_only_form_unchanged(self):
        with mock.patch.object(chrome_client, "CHROME_REQUESTS_FILE",
                               self.requests_path), \
                mock.patch.object(chrome_client, "agent_running",
                                  lambda: True):
            await self._run("/chrome #标 https://example.com/a.zip")
        reqs = chrome_client.load_requests(self.requests_path)
        self.assertEqual(reqs[0]["label"], "#标")
        self.assertIsNone(reqs[0].get("download_subdir"))

    async def test_dir_only_stored_without_label(self):
        """只有目录时 request 也要带 download_subdir（label 为空）。"""
        with mock.patch.object(chrome_client, "CHROME_REQUESTS_FILE",
                               self.requests_path), \
                mock.patch.object(chrome_client, "agent_running",
                                  lambda: True):
            await self._run("/chrome Hyuk/250630 https://example.com/a.zip")
        reqs = chrome_client.load_requests(self.requests_path)
        self.assertEqual(reqs[0]["url"], "https://example.com/a.zip")
        self.assertEqual(reqs[0]["download_subdir"], "Hyuk/250630")
        self.assertIsNone(reqs[0].get("label"))

    async def test_submit_reply_shows_target_dir(self):
        with mock.patch.object(chrome_client, "CHROME_REQUESTS_FILE",
                               self.requests_path), \
                mock.patch.object(chrome_client, "agent_running",
                                  lambda: True):
            await self._run("/chrome A/B/#标 https://example.com/a.zip")
        self.assertTrue(any("A/B" in r for r in self.replies),
                        "回执应显示实际下载目录")

    def test_result_text_shows_task_dir_not_global(self):
        """成功通知的目录必须是**该任务的**目录（带子目录时不再是根目录）。"""
        task = {
            "task_id": "abcdef1234567890", "status": "SUCCESS",
            "url": "https://a.com/x.zip", "filename": "#标 x.zip",
            "size_bytes": 100, "download_subdir": "A/B",
        }
        body = chrome_client.result_text(task)
        self.assertIn("A/B", body)
        self.assertNotIn(f"\n{chrome_client.download_dir()}\n", body)


class ChromeTasksListTest(unittest.TestCase):
    """`/chrome_tasks` 列表：只列可取消的，顺序 进行中→排队→等待重试（§4）。"""

    def _task(self, tid, status, **extra):
        task = chrome_agent.create_task(f"https://a.com/{tid}.zip", tid)
        task["status"] = status
        task.update(extra)
        return task

    def test_empty_list(self):
        self.assertIn("没有可取消的任务", chrome_client.tasks_text([]))

    def test_order_skips_terminal(self):
        tasks = [self._task("p1", "PENDING"),
                 self._task("r1", "RUNNING"),
                 self._task("w1", "RETRY_WAIT"),
                 self._task("s1", "SUCCESS"),
                 self._task("c1", "CANCELLED")]
        text = chrome_client.tasks_text(tasks)
        self.assertLess(text.index("进行中"), text.index("排队中"))
        self.assertLess(text.index("排队中"), text.index("等待重试"))
        self.assertNotIn("s1", text)   # 成功的不列
        self.assertNotIn("c1", text)   # 已取消的不列
        self.assertIn("可取消：1、2、3", text)
        # 序号 1 就是列表里的第一个（进行中）——两处用的是同一个排序
        self.assertEqual(
            [t["task_id"] for t in chrome_client.cancelable_tasks(tasks)],
            ["r1", "p1", "w1"])

    def test_shows_label_and_subdir(self):
        text = chrome_client.tasks_text([
            self._task("l1", "PENDING", label="#标", download_subdir="A/B")])
        self.assertIn("l1", text)      # 短 task_id
        self.assertIn("#标", text)
        self.assertIn("A/B", text)


class ChromeCancelFlowTest(unittest.IsolatedAsyncioTestCase):
    """`/chrome_cancel <序号>`：解析、文案、请求落盘（任务书 §5/§9）。"""

    def setUp(self):
        self.tasks_path = os.path.join(_TMP, "chrome_tasks_cancel_cmd.json")
        self.cancel_path = os.path.join(_TMP, "chrome_cancel_cmd.json")
        for p in (self.tasks_path, self.cancel_path):
            if os.path.exists(p):
                os.remove(p)
        self.replies = []
        out = self

        class _Event:
            sender_id = 545

            async def reply(self, text, **kw):
                out.replies.append(text)

        self.event = _Event()

    def tearDown(self):
        for p in (self.tasks_path, self.cancel_path):
            if os.path.exists(p):
                os.remove(p)

    def _seed(self, *tasks):
        chrome_agent.save_tasks(list(tasks), self.tasks_path)

    def _patch(self, stack):
        stack.enter_context(mock.patch.object(
            chrome_client, "CHROME_TASKS_FILE", self.tasks_path))
        stack.enter_context(mock.patch.object(
            chrome_client, "CHROME_CANCEL_REQUESTS_FILE", self.cancel_path))
        stack.enter_context(mock.patch.object(
            chrome_client, "agent_running", lambda: True))

    def _run(self, cmd):
        return chrome_client.handle_chrome_command(
            self.event, cmd, owner_id=545)

    def _task(self, tid, status):
        task = chrome_agent.create_task(f"https://a.com/{tid}.zip", tid)
        task["status"] = status
        return task

    async def test_cancel_running_task_writes_request(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            self._patch(stack)
            self._seed(self._task("p2", "PENDING"),
                       self._task("r1", "RUNNING"))
            await self._run("/chrome_cancel 1")   # 1 = 进行中的那个
        reqs = chrome_client.load_cancellations(self.cancel_path)
        self.assertEqual([r["task_id"] for r in reqs], ["r1"])
        self.assertTrue(any("取消请求已提交" in r for r in self.replies))

    async def test_cancel_queued_task_writes_request(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            self._patch(stack)
            self._seed(self._task("r1", "RUNNING"),
                       self._task("p2", "PENDING"))
            await self._run("/chrome_cancel 2")
        reqs = chrome_client.load_cancellations(self.cancel_path)
        self.assertEqual([r["task_id"] for r in reqs], ["p2"])

    async def test_usage_without_index(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            self._patch(stack)
            await self._run("/chrome_cancel")
        self.assertIn("用法：/chrome_cancel <序号>", self.replies[0])
        self.assertFalse(os.path.exists(self.cancel_path))

    async def test_non_numeric_index(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            self._patch(stack)
            await self._run("/chrome_cancel abc")
        self.assertIn("序号必须是数字", self.replies[0])

    async def test_out_of_range_index(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            self._patch(stack)
            self._seed(self._task("p1", "PENDING"))
            await self._run("/chrome_cancel 9")
        self.assertIn("任务序号无效", self.replies[0])
        self.assertFalse(os.path.exists(self.cancel_path))

    async def test_terminal_race_reported_not_cancelled(self):
        """看到列表→按下取消之间任务可能已经跑完：如实回话，不写取消请求。"""
        import contextlib
        pending = self._task("q1", "PENDING")
        done = self._task("q1", "SUCCESS")
        with contextlib.ExitStack() as stack:
            self._patch(stack)
            stack.enter_context(mock.patch.object(
                chrome_agent, "load_tasks", side_effect=[[pending], [done]]))
            await self._run("/chrome_cancel 1")
        self.assertIn("已经完成，无法取消", self.replies[0])
        self.assertFalse(os.path.exists(self.cancel_path))

    async def test_cancelled_task_race_message(self):
        import contextlib
        pending = self._task("q2", "PENDING")
        already = self._task("q2", "CANCELLED")
        with contextlib.ExitStack() as stack:
            self._patch(stack)
            stack.enter_context(mock.patch.object(
                chrome_agent, "load_tasks", side_effect=[[pending], [already]]))
            await self._run("/chrome_cancel 1")
        self.assertIn("已经取消", self.replies[0])

    async def test_cancel_by_task_id_prefix(self):
        """按 task_id 寻址：不受「列表前移」影响（序号漂移的根治手段）。"""
        import contextlib
        with contextlib.ExitStack() as stack:
            self._patch(stack)
            self._seed(self._task("aaaa1111", "PENDING"),
                       self._task("bbbb2222", "PENDING"))
            await self._run("/chrome_cancel bbbb22")
        reqs = chrome_client.load_cancellations(self.cancel_path)
        self.assertEqual([r["task_id"] for r in reqs], ["bbbb2222"])

    async def test_cancel_by_id_ignores_list_shift(self):
        """列表前移的现场：先取列表再按 ID 取消，仍落在原任务上。"""
        import contextlib
        with contextlib.ExitStack() as stack:
            self._patch(stack)
            first = self._task("cccc3333", "PENDING")
            second = self._task("dddd4444", "PENDING")
            self._seed(first, second)
            # 列表此时是 [cccc3333, dddd4444]；先完成第一个（列表前移）
            first["status"] = "SUCCESS"
            chrome_agent.save_tasks([first, second], self.tasks_path)
            await self._run("/chrome_cancel dddd4444")
        reqs = chrome_client.load_cancellations(self.cancel_path)
        self.assertEqual([r["task_id"] for r in reqs], ["dddd4444"])

    async def test_unknown_id_reports_not_found(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            self._patch(stack)
            self._seed(self._task("eeee5555", "PENDING"))
            await self._run("/chrome_cancel ffff99")
        self.assertIn("没找到这个任务 ID", self.replies[0])
        self.assertFalse(os.path.exists(self.cancel_path))

    async def test_ambiguous_id_prefix_rejected(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            self._patch(stack)
            self._seed(self._task("abcd1111", "PENDING"),
                       self._task("abcd1122", "RUNNING"))
            await self._run("/chrome_cancel abcd11")   # 6 位，两个都命中
        self.assertIn("匹配到多个任务", self.replies[0])
        self.assertFalse(os.path.exists(self.cancel_path))

    async def test_write_failure_is_reported_not_hidden(self):
        """写盘失败必须如实回执——取消是单向通道，谎报等于什么都没做。"""
        import contextlib
        with contextlib.ExitStack() as stack:
            self._patch(stack)
            self._seed(self._task("gggg6666", "PENDING"))
            stack.enter_context(mock.patch.object(
                chrome_client, "save_cancellations", lambda *a, **k: False))
            await self._run("/chrome_cancel 1")
        self.assertIn("取消请求写入失败", self.replies[0])

    async def test_unknown_task_id_reports_gone(self):
        """菜单按钮可能带着一个已被裁掉的旧 task_id（历史终态会裁剪）。"""
        import contextlib
        with contextlib.ExitStack() as stack:
            self._patch(stack)
            self._seed(self._task("hhhh7777", "PENDING"))
            ok, msg = chrome_client.request_cancel("nonexistent")
        self.assertFalse(ok)
        self.assertIn("任务已不存在", msg)

    async def test_agent_down_note(self):
        """Agent 没跑时照样登记请求，但如实告知何时生效。"""
        import contextlib
        with contextlib.ExitStack() as stack:
            self._patch(stack)
            stack.enter_context(mock.patch.object(
                chrome_client, "agent_running", lambda: False))
            self._seed(self._task("p1", "PENDING"))
            await self._run("/chrome_cancel 1")
        self.assertIn("Agent 当前未运行", self.replies[0])
        self.assertEqual(
            len(chrome_client.load_cancellations(self.cancel_path)), 1)
