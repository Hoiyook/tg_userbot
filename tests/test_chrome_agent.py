"""Chrome Agent（chrome_agent.py）的单元测试：基础纯函数、任务持久化、
状态机推进、恢复规则、重试/超时、CDP 事件归因（WS 消息注入 mock）。

不联网：CDP 真连接不做单测；Chrome 真实拉起不做单测（真机验收）。
运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import asyncio
import atexit
import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_chrome_test_")
# 退出时回收临时目录（测试跑完就地删，别让 /var/folders 越堆越多）
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import chrome_agent  # noqa: E402
from tg_userbot import log as logmod  # noqa: E402


class ChromeConfigTest(unittest.TestCase):
    """Chrome Agent 的配置常量与目录计算。"""

    def test_download_dir_under_save_folder(self):
        """下载目录必须是 <SAVE_FOLDER>/TG Chrome Download（规格 21），
        TG_SAVE_FOLDER 变化时自动跟随。"""
        self.assertEqual(
            config.CHROME_DOWNLOAD_DIR,
            os.path.join(config.SAVE_FOLDER, "TG Chrome Download"),
        )

    def test_cdp_binds_local_only(self):
        """CDP 只允许监听 127.0.0.1（规格 6），禁 0.0.0.0。"""
        self.assertEqual(config.CHROME_CDP_HOST, "127.0.0.1")
        self.assertNotEqual(config.CHROME_CDP_HOST, "0.0.0.0")
        self.assertEqual(config.CHROME_CDP_PORT, 9222)

    def test_retry_and_timeout_defaults(self):
        """最大重试 3 次、单次下载超时 1800 秒（规格 29/30）。"""
        self.assertEqual(config.CHROME_DOWNLOAD_RETRIES, 3)
        self.assertEqual(config.CHROME_DOWNLOAD_TIMEOUT, 1800)

    def test_profile_dir_is_dedicated(self):
        """专用 Profile 必须在用户主目录的独立目录（2026-09-09 用户允许
        新建 Profile），绝不指向正常 Chrome 的 User Data。"""
        self.assertTrue(config.CHROME_PROFILE_DIR.endswith("tg_chrome_agent_profile"))
        self.assertNotIn("Google/Chrome", config.CHROME_PROFILE_DIR)


class ValidateUrlTest(unittest.TestCase):
    """URL 验证（规格 16）：只收 http/https，拒绝空/非法。"""

    def test_accepts_http_https(self):
        self.assertTrue(chrome_agent.validate_chrome_url(
            "https://example.com/test.zip"))
        self.assertTrue(chrome_agent.validate_chrome_url(
            "http://example.com/a.bin"))

    def test_rejects_bad_urls(self):
        self.assertFalse(chrome_agent.validate_chrome_url(""))
        self.assertFalse(chrome_agent.validate_chrome_url(None))
        self.assertFalse(chrome_agent.validate_chrome_url("abc"))
        self.assertFalse(chrome_agent.validate_chrome_url("ftp://x/a"))
        self.assertFalse(chrome_agent.validate_chrome_url("file:///etc/passwd"))
        self.assertFalse(chrome_agent.validate_chrome_url("https://"))


class TaskIdTest(unittest.TestCase):
    """task_id 唯一性（规格 37）。"""

    def test_task_ids_unique(self):
        ids = {chrome_agent.new_task_id() for _ in range(1000)}
        self.assertEqual(len(ids), 1000)


class CreateTaskTest(unittest.TestCase):
    """create_task：完整字段 + PENDING 起点（规格 25）。"""

    def test_full_record_shape(self):
        task = chrome_agent.create_task(
            "https://example.com/test.zip", "task-1")
        self.assertEqual(task["task_id"], "task-1")
        self.assertEqual(task["url"], "https://example.com/test.zip")
        self.assertEqual(task["status"], "PENDING")
        self.assertEqual(task["attempts"], 0)
        self.assertIsNone(task["filename"])
        self.assertIsNone(task["size_bytes"])
        self.assertIsNone(task["error"])
        for key in ("created_at", "updated_at"):
            self.assertIn(key, task)
        self.assertIsNone(task["started_at"])
        self.assertIsNone(task["finished_at"])


class FindChromeBinaryTest(unittest.TestCase):
    """Chrome 可执行文件探测（规格 5：默认路径 + 自动检测）。"""

    def test_default_path_on_this_mac(self):
        # 本机装了标准位置 Chrome（--version 已验证），探测应命中
        path = chrome_agent.find_chrome_binary()
        self.assertIsNotNone(path)
        self.assertIn("Google Chrome", path)

    def test_returns_none_when_absent(self):
        with mock.patch.object(os.path, "exists", return_value=False):
            self.assertIsNone(chrome_agent.find_chrome_binary())


class TaskStoreTest(unittest.TestCase):
    """chrome_tasks.json 持久化：原子写、坏文件兜底（规格 24）。"""

    def setUp(self):
        self.path = os.path.join(_TMP, "chrome_tasks_store_test.json")
        if os.path.exists(self.path):
            os.remove(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_save_and_load_roundtrip(self):
        tasks = [
            chrome_agent.create_task("https://a.com/1.zip", "t1"),
            chrome_agent.create_task("https://a.com/2.zip", "t2"),
        ]
        chrome_agent.save_tasks(tasks, self.path)
        loaded = chrome_agent.load_tasks(self.path)
        self.assertEqual([t["task_id"] for t in loaded], ["t1", "t2"])

    def test_load_missing_or_corrupt_returns_empty(self):
        self.assertEqual(chrome_agent.load_tasks(self.path), [])
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{broken json")
        self.assertEqual(chrome_agent.load_tasks(self.path), [])
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('["not", "a", "dict", "list"]')
        self.assertEqual(chrome_agent.load_tasks(self.path), [])

    def test_load_skips_entries_without_task_id(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"tasks": [{"status": "PENDING"}, '
                    '{"task_id": "ok", "status": "PENDING"}]}')
        loaded = chrome_agent.load_tasks(self.path)
        self.assertEqual([t["task_id"] for t in loaded], ["ok"])


def _task(tid="t1"):
    return chrome_agent.create_task("https://example.com/a.zip", tid)


class StateMachineTest(unittest.TestCase):
    """状态机推进（规格 26）：PENDING→RUNNING→SUCCESS/RETRY_WAIT/FAILED。"""

    def test_start_attempt_increments_and_enters_running(self):
        task = _task()
        chrome_agent.start_attempt(task)
        self.assertEqual(task["status"], "RUNNING")
        self.assertEqual(task["attempts"], 1)
        self.assertIsNotNone(task["started_at"])
        # 重新进入（重试）继续累计
        chrome_agent.start_attempt(task)
        self.assertEqual(task["attempts"], 2)

    def test_finish_success_records_file_info(self):
        task = _task()
        chrome_agent.start_attempt(task)
        chrome_agent.finish_success(task, "a.zip", 123456789)
        self.assertEqual(task["status"], "SUCCESS")
        self.assertEqual(task["filename"], "a.zip")
        self.assertEqual(task["size_bytes"], 123456789)  # 原始 bytes（规格 31）
        self.assertIsNotNone(task["finished_at"])

    def test_fail_under_limit_enters_retry_wait(self):
        task = _task()
        chrome_agent.start_attempt(task)  # attempts=1
        now = datetime(2026, 9, 9, 10, 0, 0)
        chrome_agent.fail_attempt(task, "下载超时", retries=3,
                                  wait_seconds=30, now=now)
        self.assertEqual(task["status"], "RETRY_WAIT")
        self.assertEqual(task["error"], "下载超时")
        self.assertIn("next_retry_at", task)

    def test_fail_at_limit_is_final(self):
        task = _task()
        for _ in range(3):
            chrome_agent.start_attempt(task)
        chrome_agent.fail_attempt(task, "下载超时", retries=3, wait_seconds=30)
        self.assertEqual(task["status"], "FAILED")
        self.assertEqual(task["attempts"], 3)  # 最终失败保留 attempts（规格 29）
        self.assertIsNotNone(task["finished_at"])


class ClaimNextTest(unittest.TestCase):
    """FIFO 认领：PENDING 优先、到期 RETRY_WAIT 可领、其余跳过（规格 23）。"""

    def test_fifo_pending_order(self):
        t1, t2, t3 = _task("t1"), _task("t2"), _task("t3")
        chrome_agent.start_attempt(t1)  # RUNNING 不领
        tasks = [t1, t2, t3]
        self.assertEqual(chrome_agent.claim_next(tasks)["task_id"], "t2")

    def test_retry_wait_claimed_only_when_due(self):
        due = _task("due")
        due.update({"status": "RETRY_WAIT", "attempts": 1,
                    "next_retry_at": "2026-09-09 10:00:00"})
        not_due = _task("not_due")
        not_due.update({"status": "RETRY_WAIT", "attempts": 1,
                        "next_retry_at": "2026-09-09 23:59:59"})
        now = datetime(2026, 9, 9, 10, 0, 30)
        self.assertIsNone(chrome_agent.claim_next([not_due], now=now))
        self.assertEqual(
            chrome_agent.claim_next([due], now=now)["task_id"], "due")

    def test_terminals_never_claimed(self):
        done = _task("ok")
        done["status"] = "SUCCESS"
        failed = _task("bad")
        failed["status"] = "FAILED"
        self.assertIsNone(chrome_agent.claim_next([done, failed]))


class RecoveryTest(unittest.TestCase):
    """Agent 重启恢复（规格 27/28）。"""

    def setUp(self):
        self.dl_dir = os.path.join(_TMP, "chrome_dl_recovery")
        os.makedirs(self.dl_dir, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dl_dir, ignore_errors=True)

    def test_running_becomes_pending(self):
        t = _task()
        chrome_agent.start_attempt(t)
        chrome_agent.recover_tasks([t], self.dl_dir)
        self.assertEqual(t["status"], "PENDING")

    def test_running_with_completed_file_restores_success(self):
        """completed 事件后、写 SUCCESS 前崩溃：成品已在下载目录 → 直接
        恢复 SUCCESS，绝不重复下载（规格 28）。"""
        t = _task()
        chrome_agent.start_attempt(t)
        t["filename"] = "done.zip"
        with open(os.path.join(self.dl_dir, "done.zip"), "wb") as f:
            f.write(b"x" * 100)
        chrome_agent.recover_tasks([t], self.dl_dir)
        self.assertEqual(t["status"], "SUCCESS")
        self.assertEqual(t["size_bytes"], 100)

    def test_running_with_crdownload_still_pending(self):
        t = _task()
        chrome_agent.start_attempt(t)
        t["filename"] = "partial.zip"
        with open(os.path.join(self.dl_dir, "partial.zip.crdownload"),
                  "wb") as f:
            f.write(b"partial")
        chrome_agent.recover_tasks([t], self.dl_dir)
        self.assertEqual(t["status"], "PENDING")  # 无法可靠判断 → 重下

    def test_terminals_and_retry_wait_untouched(self):
        succ, failed = _task("s"), _task("f")
        succ["status"] = "SUCCESS"
        failed.update({"status": "FAILED", "attempts": 3, "error": "x"})
        wait = _task("w")
        wait.update({"status": "RETRY_WAIT", "attempts": 1,
                     "next_retry_at": "2026-09-01 00:00:00"})
        pending = _task("p")
        chrome_agent.recover_tasks([succ, failed, wait, pending], self.dl_dir)
        self.assertEqual(succ["status"], "SUCCESS")   # 绝不重复下载
        self.assertEqual(failed["status"], "FAILED")  # 达上限不自动重跑
        self.assertEqual(wait["status"], "RETRY_WAIT")
        self.assertEqual(pending["status"], "PENDING")


class RetryFlowTest(unittest.TestCase):
    """重试流（规格 29）：失败→重试→成功；三连败→FAILED。"""

    def _drive(self, outcomes):
        task = _task()
        for outcome in outcomes:
            chrome_agent.start_attempt(task)
            if outcome == "ok":
                chrome_agent.finish_success(task, "a.zip", 1)
            else:
                chrome_agent.fail_attempt(task, outcome, retries=3,
                                          wait_seconds=0)
        return task

    def test_two_failures_then_success(self):
        task = self._drive(["net error", "net error", "ok"])
        self.assertEqual(task["status"], "SUCCESS")
        self.assertEqual(task["attempts"], 3)

    def test_three_failures_is_failed(self):
        task = self._drive(["e1", "e2", "e3"])
        self.assertEqual(task["status"], "FAILED")
        self.assertEqual(task["attempts"], 3)
        self.assertEqual(task["error"], "e3")


# ------------------------------------------------------------
# CDP 事件归因（WS 消息流注入 FakeCDP，不联网）
# ------------------------------------------------------------

class FakeCDP:
    """脚本化 CDP：记录命令、按脚本吐事件（downloadWillBegin/Progress）。"""

    def __init__(self, events, hang=False):
        self.commands = []
        self._events = list(events)
        self._hang = hang

    async def setup_download(self, download_path):
        self.commands.append(("setup_download", download_path))

    async def open_tab(self, url):
        self.commands.append(("open_tab", url))
        return "target-1"

    async def close_tab(self, target_id):
        self.commands.append(("close_tab", target_id))

    async def next_event(self, timeout):
        import asyncio
        if self._events:
            return self._events.pop(0)
        if self._hang:
            # 模拟「永远等不到事件」：睡满调用方给的窗口后返回 None
            await asyncio.sleep(max(0.0, timeout))
        return None

    async def command(self, method, params=None, timeout=30):
        """CDP command 入口（真客户端发 WS；这里只记账）。

        取消功能要发 Browser.cancelDownload，所以假客户端也得有这一格。
        """
        self.commands.append((method, params))
        return {}

    def is_alive(self):
        """连接判活（真客户端按 WS/接收循环判定）；假客户端默认活着。"""
        return True


class CDPEventAttributionTest(unittest.IsolatedAsyncioTestCase):
    """run_download_attempt：依据 CDP 下载事件判定 completed/canceled/超时
    （规格 22：不许 sleep 后假设成功）。"""

    def setUp(self):
        self.dl_dir = os.path.join(_TMP, "chrome_dl_cdp")
        os.makedirs(self.dl_dir, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dl_dir, ignore_errors=True)

    async def test_completed_event_is_success_with_exact_size(self):
        final = os.path.join(self.dl_dir, "a.zip")
        with open(final, "wb") as f:
            f.write(b"x" * 555)
        cdp = FakeCDP([
            {"method": "Browser.downloadWillBegin",
             "params": {"guid": "g1", "suggestedFilename": "a.zip"}},
            {"method": "Browser.downloadProgress",
             "params": {"guid": "g1", "state": "inProgress"}},
            {"method": "Browser.downloadProgress",
             "params": {"guid": "g1", "state": "completed"}},
        ])
        ok, filename, size, error = await chrome_agent.run_download_attempt(
            cdp, "https://example.com/a.zip", self.dl_dir, timeout=5)
        self.assertTrue(ok)
        self.assertEqual(filename, "a.zip")
        self.assertEqual(size, 555)  # 精确 bytes
        self.assertIsNone(error)
        self.assertIn(("setup_download", self.dl_dir), cdp.commands)
        self.assertIn(("open_tab", "https://example.com/a.zip"), cdp.commands)
        self.assertIn(("close_tab", "target-1"), cdp.commands)

    async def test_canceled_event_is_failure(self):
        cdp = FakeCDP([
            {"method": "Browser.downloadWillBegin",
             "params": {"guid": "g2", "suggestedFilename": "b.zip"}},
            {"method": "Browser.downloadProgress",
             "params": {"guid": "g2", "state": "canceled"}},
        ])
        ok, filename, size, error = await chrome_agent.run_download_attempt(
            cdp, "https://example.com/b.zip", self.dl_dir, timeout=5)
        self.assertFalse(ok)
        self.assertIn("取消", error)

    async def test_timeout_without_terminal_event(self):
        cdp = FakeCDP([
            {"method": "Browser.downloadWillBegin",
             "params": {"guid": "g3", "suggestedFilename": "c.zip"}},
        ], hang=True)
        ok, filename, size, error = await chrome_agent.run_download_attempt(
            cdp, "https://example.com/c.zip", self.dl_dir, timeout=0.2)
        self.assertFalse(ok)
        self.assertIn("超时", error)

    async def test_completed_but_file_missing_is_failure(self):
        cdp = FakeCDP([
            {"method": "Browser.downloadWillBegin",
             "params": {"guid": "g4", "suggestedFilename": "ghost.zip"}},
            {"method": "Browser.downloadProgress",
             "params": {"guid": "g4", "state": "completed"}},
        ])
        ok, filename, size, error = await chrome_agent.run_download_attempt(
            cdp, "https://example.com/ghost.zip", self.dl_dir, timeout=5)
        self.assertFalse(ok)
        self.assertIn("缺失", error)

    async def test_no_download_begins_is_timeout_failure(self):
        """网页类 URL 不触发下载：无 willBegin 也按超时判失败。"""
        cdp = FakeCDP([], hang=True)
        ok, filename, size, error = await chrome_agent.run_download_attempt(
            cdp, "https://example.com/page", self.dl_dir, timeout=0.2)
        self.assertFalse(ok)
        self.assertIn("超时", error)


class LaunchArgsTest(unittest.TestCase):
    """专用 Chrome 启动参数：user-data-dir + 127.0.0.1 + 端口（无 0.0.0.0）。"""

    def test_launch_args_shape(self):
        with mock.patch.object(chrome_agent.subprocess,
                               "Popen") as popen:
            chrome_agent.launch_chrome_detached(
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Users/x/tg_chrome_agent_profile", "127.0.0.1", 9222)
        args = popen.call_args[0][0]
        joined = " ".join(args)
        self.assertIn("--user-data-dir=/Users/x/tg_chrome_agent_profile",
                      joined)
        self.assertIn("--remote-debugging-port=9222", joined)
        self.assertIn("--remote-debugging-address=127.0.0.1", joined)
        self.assertNotIn("0.0.0.0", joined)
        self.assertNotIn("--proxy-server", joined)  # 缺省不带代理参数

    def test_launch_args_with_proxy(self):
        with mock.patch.object(chrome_agent.subprocess,
                               "Popen") as popen:
            chrome_agent.launch_chrome_detached(
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Users/x/tg_chrome_agent_profile", "127.0.0.1", 9222,
                proxy_server="socks5://127.0.0.1:12334")
        joined = " ".join(popen.call_args[0][0])
        self.assertIn("--proxy-server=socks5://127.0.0.1:12334", joined)


class WaitForCDPTest(unittest.IsolatedAsyncioTestCase):
    """CDP 端点轮询：webSocketDebuggerUrl 提取、超时返回 None。"""

    async def test_extracts_ws_url(self):
        async def fake_to_thread(fn, *a, **k):
            return {"Browser": "Chrome/152",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/x"}
        with mock.patch.object(chrome_agent.asyncio, "to_thread",
                               fake_to_thread):
            ws = await chrome_agent.wait_for_cdp("127.0.0.1", 9222, 5)
        self.assertEqual(ws, "ws://127.0.0.1:9222/x")

    async def test_timeout_returns_none(self):
        async def fake_to_thread(fn, *a, **k):
            raise OSError("connection refused")
        with mock.patch.object(chrome_agent.asyncio, "to_thread",
                               fake_to_thread):
            ws = await chrome_agent.wait_for_cdp("127.0.0.1", 9222, 0.3)
        self.assertIsNone(ws)


class ClaimRequestsTest(unittest.TestCase):
    """从 chrome_requests.json 认领新任务（按 task_id 去重）。"""

    def setUp(self):
        self.req_path = os.path.join(_TMP, "chrome_requests_claim.json")
        if os.path.exists(self.req_path):
            os.remove(self.req_path)

    def tearDown(self):
        if os.path.exists(self.req_path):
            os.remove(self.req_path)

    def _write_requests(self, reqs):
        import json
        with open(self.req_path, "w", encoding="utf-8") as f:
            json.dump({"requests": reqs}, f, ensure_ascii=False)

    def test_new_requests_merged(self):
        tasks = []
        self._write_requests([
            {"task_id": "r1", "url": "https://a.com/1.zip"},
            {"task_id": "r2", "url": "https://a.com/2.zip"},
        ])
        claimed = chrome_agent.claim_new_requests(tasks, self.req_path)
        self.assertEqual([t["task_id"] for t in claimed], ["r1", "r2"])
        self.assertEqual([t["task_id"] for t in tasks], ["r1", "r2"])
        self.assertTrue(all(t["status"] == "PENDING" for t in tasks))

    def test_known_requests_not_remerged(self):
        tasks = [chrome_agent.create_task("https://a.com/1.zip", "r1")]
        self._write_requests([
            {"task_id": "r1", "url": "https://a.com/1.zip"},
            {"task_id": "r3", "url": "https://a.com/3.zip"},
        ])
        claimed = chrome_agent.claim_new_requests(tasks, self.req_path)
        self.assertEqual([t["task_id"] for t in claimed], ["r3"])
        self.assertEqual([t["task_id"] for t in tasks], ["r1", "r3"])

    def test_malformed_request_entries_skipped(self):
        self._write_requests([
            {"url": "https://a.com/no-id.zip"},                # 缺 task_id
            {"task_id": "r4", "url": "not-a-valid-url"},       # 非法 URL
            {"task_id": "r5", "url": "https://a.com/ok.zip"},
        ])
        tasks = []
        claimed = chrome_agent.claim_new_requests(tasks, self.req_path)
        self.assertEqual([t["task_id"] for t in claimed], ["r5"])


class ProcessPendingTest(unittest.IsolatedAsyncioTestCase):
    """process_pending_tasks：FIFO 单并发执行 + 状态落盘（规格 23/26）。"""

    def setUp(self):
        self.dl_dir = os.path.join(_TMP, "chrome_dl_process")
        os.makedirs(self.dl_dir, exist_ok=True)
        self.tasks_path = os.path.join(_TMP, "chrome_tasks_process.json")
        if os.path.exists(self.tasks_path):
            os.remove(self.tasks_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dl_dir, ignore_errors=True)
        if os.path.exists(self.tasks_path):
            os.remove(self.tasks_path)

    async def test_success_flow_persists(self):
        final = os.path.join(self.dl_dir, "ok.zip")
        with open(final, "wb") as f:
            f.write(b"z" * 42)
        cdp = FakeCDP([
            {"method": "Browser.downloadWillBegin",
             "params": {"guid": "g", "suggestedFilename": "ok.zip"}},
            {"method": "Browser.downloadProgress",
             "params": {"guid": "g", "state": "completed"}},
        ])
        task = chrome_agent.create_task("https://a.com/ok.zip", "p1")
        n = await chrome_agent.process_pending_tasks(
            cdp, [task], self.tasks_path, self.dl_dir,
            timeout=5, retries=3, wait_seconds=0)
        self.assertEqual(n, 1)
        self.assertEqual(task["status"], "SUCCESS")
        self.assertEqual(task["size_bytes"], 42)
        # 落盘验证
        loaded = chrome_agent.load_tasks(self.tasks_path)
        self.assertEqual(loaded[0]["status"], "SUCCESS")

    async def test_failure_persists_retry_wait(self):
        cdp = FakeCDP([
            {"method": "Browser.downloadWillBegin",
             "params": {"guid": "g", "suggestedFilename": "x.zip"}},
            {"method": "Browser.downloadProgress",
             "params": {"guid": "g", "state": "canceled"}},
        ])
        task = chrome_agent.create_task("https://a.com/x.zip", "p2")
        n = await chrome_agent.process_pending_tasks(
            cdp, [task], self.tasks_path, self.dl_dir,
            timeout=5, retries=3, wait_seconds=30)
        self.assertEqual(n, 1)
        self.assertEqual(task["status"], "RETRY_WAIT")
        self.assertEqual(task["attempts"], 1)
        self.assertIsNotNone(task["next_retry_at"])

    async def test_exhausted_retries_persists_failed(self):
        cdp = FakeCDP([
            {"method": "Browser.downloadWillBegin",
             "params": {"guid": "g", "suggestedFilename": "y.zip"}},
            {"method": "Browser.downloadProgress",
             "params": {"guid": "g", "state": "canceled"}},
        ])
        task = chrome_agent.create_task("https://a.com/y.zip", "p3")
        n = await chrome_agent.process_pending_tasks(
            cdp, [task], self.tasks_path, self.dl_dir,
            timeout=0.2, retries=3, wait_seconds=0)
        self.assertEqual(n, 1)  # 一个任务重试多次仍是一个任务
        self.assertEqual(task["status"], "FAILED")
        self.assertEqual(task["attempts"], 3)

    async def test_fifo_one_at_a_time(self):
        final = os.path.join(self.dl_dir, "ok.zip")
        with open(final, "wb") as f:
            f.write(b"z")
        cdp = FakeCDP([
            {"method": "Browser.downloadWillBegin",
             "params": {"guid": "g", "suggestedFilename": "ok.zip"}},
            {"method": "Browser.downloadProgress",
             "params": {"guid": "g", "state": "completed"}},
        ])
        t1 = chrome_agent.create_task("https://a.com/1.zip", "q1")
        t2 = chrome_agent.create_task("https://a.com/2.zip", "q2")
        await chrome_agent.process_pending_tasks(
            cdp, [t1, t2], self.tasks_path, self.dl_dir,
            timeout=0.2, retries=1, wait_seconds=0)
        # t1 成功消费了脚本；t2 无事件可吃 → 超时失败（retries=1 → FAILED）
        self.assertEqual(t1["status"], "SUCCESS")
        self.assertEqual(t2["status"], "FAILED")
        # open_tab 恰好 2 次（串行，一次一个）
        opens = [c for c in cdp.commands if c[0] == "open_tab"]
        self.assertEqual(len(opens), 2)


class ChromeLabelRenameTest(unittest.IsolatedAsyncioTestCase):
    """Agent 侧：成功后把文件重命名为「#标注 原文件名」（磁盘+记录同步）。"""

    def setUp(self):
        self.dl_dir = os.path.join(_TMP, "chrome_dl_label")
        os.makedirs(self.dl_dir, exist_ok=True)
        self.tasks_path = os.path.join(_TMP, "chrome_tasks_label.json")
        if os.path.exists(self.tasks_path):
            os.remove(self.tasks_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dl_dir, ignore_errors=True)
        if os.path.exists(self.tasks_path):
            os.remove(self.tasks_path)

    async def test_success_renames_with_label(self):
        final = os.path.join(self.dl_dir, "a.zip")
        with open(final, "wb") as f:
            f.write(b"z" * 7)
        cdp = FakeCDP([
            {"method": "Browser.downloadWillBegin",
             "params": {"guid": "g", "suggestedFilename": "a.zip"}},
            {"method": "Browser.downloadProgress",
             "params": {"guid": "g", "state": "completed"}},
        ])
        task = chrome_agent.create_task("https://a.com/a.zip", "l1")
        task["label"] = "#你好#260908"
        await chrome_agent.process_pending_tasks(
            cdp, [task], self.tasks_path, self.dl_dir,
            timeout=5, retries=1, wait_seconds=0)
        # 磁盘上是「#标注 原名」，原名不复存在
        self.assertFalse(os.path.exists(final))
        renamed = os.path.join(self.dl_dir, "#你好#260908 a.zip")
        self.assertTrue(os.path.isfile(renamed))
        self.assertEqual(task["filename"], "#你好#260908 a.zip")
        self.assertEqual(task["size_bytes"], 7)

    async def test_no_label_keeps_original_name(self):
        final = os.path.join(self.dl_dir, "b.zip")
        with open(final, "wb") as f:
            f.write(b"z")
        cdp = FakeCDP([
            {"method": "Browser.downloadWillBegin",
             "params": {"guid": "g", "suggestedFilename": "b.zip"}},
            {"method": "Browser.downloadProgress",
             "params": {"guid": "g", "state": "completed"}},
        ])
        task = chrome_agent.create_task("https://a.com/b.zip", "l2")
        await chrome_agent.process_pending_tasks(
            cdp, [task], self.tasks_path, self.dl_dir,
            timeout=5, retries=1, wait_seconds=0)
        self.assertEqual(task["filename"], "b.zip")

    async def test_label_collision_gets_unique_suffix(self):
        with open(os.path.join(self.dl_dir, "#标 a.zip"), "wb") as f:
            f.write(b"old")
        final = os.path.join(self.dl_dir, "a.zip")
        with open(final, "wb") as f:
            f.write(b"new")
        cdp = FakeCDP([
            {"method": "Browser.downloadWillBegin",
             "params": {"guid": "g", "suggestedFilename": "a.zip"}},
            {"method": "Browser.downloadProgress",
             "params": {"guid": "g", "state": "completed"}},
        ])
        task = chrome_agent.create_task("https://a.com/a.zip", "l3")
        task["label"] = "#标"
        await chrome_agent.process_pending_tasks(
            cdp, [task], self.tasks_path, self.dl_dir,
            timeout=5, retries=1, wait_seconds=0)
        self.assertEqual(task["filename"], "#标 a (1).zip")
        self.assertTrue(os.path.isfile(
            os.path.join(self.dl_dir, "#标 a (1).zip")))
        # 旧文件不被覆盖
        with open(os.path.join(self.dl_dir, "#标 a.zip"), "rb") as f:
            self.assertEqual(f.read(), b"old")


class AgentLogIsolationTest(unittest.TestCase):
    """Agent 进程必须写自己的日志文件，绝不与主 userbot 共用 download.log。

    两个进程各持一个 TimedRotatingFileHandler 写同一文件时，午夜各自轮转，
    POSIX rename 静默替换 → 后轮转者覆盖先归档者的内容，且先轮转者的句柄仍
    绑在已被改名的 inode 上、此后持续写进归档名文件。2026-09-10 实测：
    9/9 全天日志被覆盖丢失，主进程日志此后全灌进 download.log.2026-09-09
    （见 issues/001）。此测试钉死「Agent 用自己的文件」这一隔离契约。
    """

    def setUp(self):
        self._probe = "🤖 agent 日志隔离探针"
        # download.log 在 import config 时已被 handler 创建，取其当下内容作基线
        self._main_before = self._read(config.LOG_FILE)

    def tearDown(self):
        # 本测试改了共享 logger 的 handler（会干扰同进程其它测试模块），恢复之
        for h in list(logmod.logger.handlers):
            logmod.logger.removeHandler(h)
            h.close()
        logmod.configure(config.LOG_FILE, config.LOG_RETENTION_DAYS)

    @staticmethod
    def _read(path):
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()

    def _log_probe_and_flush(self):
        logmod.logger.info(self._probe)
        for h in logmod.logger.handlers:
            h.flush()

    def test_agent_log_file_is_distinct_from_main_log(self):
        """两个路径必须不同，且 Agent 日志同样落在 runtime/ 目录里。"""
        self.assertNotEqual(config.CHROME_AGENT_LOG_FILE, config.LOG_FILE)
        self.assertEqual(
            os.path.dirname(config.CHROME_AGENT_LOG_FILE), config.RUNTIME_DIR)

    def test_configure_agent_logging_points_away_from_download_log(self):
        chrome_agent.configure_agent_logging()
        self.assertEqual(
            logmod.current_log_path(), config.CHROME_AGENT_LOG_FILE)

    def test_agent_holds_no_handler_on_download_log(self):
        """重定向后不能还握着 download.log 的句柄（旧 handler 必须被关掉）。"""
        chrome_agent.configure_agent_logging()
        targets = {getattr(h, "baseFilename", None)
                   for h in logmod.logger.handlers}
        self.assertNotIn(config.LOG_FILE, targets)
        self.assertIn(config.CHROME_AGENT_LOG_FILE, targets)

    def test_agent_records_land_in_own_file_not_download_log(self):
        """探针日志必须只进 Agent 文件，一条都不能落进 download.log。"""
        chrome_agent.configure_agent_logging()
        self._log_probe_and_flush()
        self.assertIn(self._probe, self._read(config.CHROME_AGENT_LOG_FILE))
        self.assertNotIn(self._probe, self._read(config.LOG_FILE))
        self.assertEqual(self._read(config.LOG_FILE), self._main_before)

    def test_agent_main_entry_wires_its_own_log(self):
        """入口 agent_main 必须真的调 configure——否则函数在但没人用。"""
        with mock.patch.object(
                chrome_agent, "find_chrome_binary", return_value=None):
            rc = asyncio.run(chrome_agent.agent_main())
        self.assertEqual(rc, 1)  # 无 Chrome 可执行文件 → 快速退出
        self.assertEqual(
            logmod.current_log_path(), config.CHROME_AGENT_LOG_FILE)


class ChromeSubdirTest(unittest.TestCase):
    """下载子目录：safe_subdir / get_task_download_dir / 字段透传。"""

    def test_safe_subdir_normalizes(self):
        f = chrome_agent.safe_subdir
        self.assertEqual(f("A"), "A")
        self.assertEqual(f("A/B"), "A/B")
        self.assertEqual(f("A//B"), "A/B")
        self.assertEqual(f(" A / B "), "A/B")
        self.assertEqual(f("A/B/"), "A/B")
        self.assertIsNone(f(None))
        self.assertIsNone(f(""))
        self.assertIsNone(f("   "))

    def test_safe_subdir_rejects_traversal(self):
        """子目录直接来自用户输入，必须挡住上跳/绝对/反斜杠——
        否则 `A/../../..` 会把文件写到 TG Chrome Download 之外。"""
        f = chrome_agent.safe_subdir
        self.assertIsNone(f(".."))
        self.assertIsNone(f("A/../B"))
        self.assertIsNone(f("A/.."))
        self.assertIsNone(f("../../etc"))
        self.assertIsNone(f("A/./B"))
        self.assertIsNone(f("a\\b"))

    def test_get_task_download_dir(self):
        root = "/root/TG Chrome Download"
        self.assertEqual(
            chrome_agent.get_task_download_dir(root, {}), root)
        self.assertEqual(
            chrome_agent.get_task_download_dir(root, {"download_subdir": None}),
            root)
        self.assertEqual(
            chrome_agent.get_task_download_dir(
                root, {"download_subdir": "A/B"}),
            os.path.join(root, "A", "B"))

    def test_get_task_download_dir_falls_back_on_illegal(self):
        """非法子目录退回根目录（不报错、不越界），绝不写出根之外。"""
        root = "/root/TG Chrome Download"
        got = chrome_agent.get_task_download_dir(
            root, {"download_subdir": "../outside"})
        self.assertEqual(got, root)

    def test_create_task_stores_subdir_only_when_present(self):
        with_sub = chrome_agent.create_task(
            "https://a.com/1.zip", "t1", download_subdir="A/B")
        self.assertEqual(with_sub["download_subdir"], "A/B")
        without = chrome_agent.create_task("https://a.com/2.zip", "t2")
        self.assertNotIn("download_subdir", without)

    def test_claim_new_requests_propagates_subdir(self):
        path = os.path.join(_TMP, "chrome_req_subdir.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"requests": [{
                "task_id": "t1", "url": "https://a.com/1.zip",
                "label": "#标", "download_subdir": "A/B",
            }]}, f)
        tasks = []
        claimed = chrome_agent.claim_new_requests(tasks, path)
        os.remove(path)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["download_subdir"], "A/B")
        self.assertEqual(claimed[0]["label"], "#标")


class ChromeSubdirRecoveryTest(unittest.TestCase):
    """Recovery 必须按任务自己的目录找成品（否则重启后会重下已完成的）。"""

    def setUp(self):
        self.root = os.path.join(_TMP, "chrome_subdir_recover")
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_recover_finds_file_in_subdir(self):
        sub = os.path.join(self.root, "A", "B")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, "done.zip"), "wb") as f:
            f.write(b"z" * 10)
        task = chrome_agent.create_task(
            "https://a.com/done.zip", "r1", download_subdir="A/B")
        chrome_agent.start_attempt(task)
        task["filename"] = "done.zip"

        chrome_agent.recover_tasks([task], self.root)
        self.assertEqual(task["status"], "SUCCESS",
                         "子目录里的成品没被认出来 → 会重复下载")

    def test_recover_still_pending_when_file_absent(self):
        task = chrome_agent.create_task(
            "https://a.com/miss.zip", "r2", download_subdir="A/B")
        chrome_agent.start_attempt(task)
        task["filename"] = "miss.zip"
        chrome_agent.recover_tasks([task], self.root)
        self.assertEqual(task["status"], "PENDING")


class _WritingCDP(FakeCDP):
    """在 setup_download 时把成品写进「浏览器将要下载到的」那个目录。

    既证明传给 CDP 的目录确实变了（记录在 commands 里），又满足 completed
    之后 run_download_attempt 对成品文件的校验。
    """

    def __init__(self, events, filename):
        super().__init__(events)
        self.filename = filename

    async def setup_download(self, download_path):
        await super().setup_download(download_path)
        os.makedirs(download_path, exist_ok=True)
        with open(os.path.join(download_path, self.filename), "wb") as f:
            f.write(b"z" * 7)


class ChromeSubdirDownloadTest(unittest.IsolatedAsyncioTestCase):
    """带 download_subdir 的任务真的落到子目录（建目录 + CDP 落点 + 改名）。"""

    def setUp(self):
        self.root = os.path.join(_TMP, "chrome_subdir_dl")
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)
        self.tasks_path = os.path.join(_TMP, "chrome_tasks_subdir.json")
        if os.path.exists(self.tasks_path):
            os.remove(self.tasks_path)

    def _events(self):
        return [
            {"method": "Browser.downloadWillBegin",
             "params": {"guid": "g", "suggestedFilename": "x.zip"}},
            {"method": "Browser.downloadProgress",
             "params": {"guid": "g", "state": "completed"}},
        ]

    async def test_download_lands_in_subdir(self):
        cdp = _WritingCDP(self._events(), "x.zip")
        task = chrome_agent.create_task(
            "https://a.com/x.zip", "s1", download_subdir="A/B")
        await chrome_agent.process_pending_tasks(
            cdp, [task], self.tasks_path, self.root,
            timeout=5, retries=3, wait_seconds=0)
        self.assertEqual(task["status"], "SUCCESS")
        # CDP 的下载落点必须是子目录，不是根目录
        self.assertIn(
            ("setup_download", os.path.join(self.root, "A", "B")),
            cdp.commands)
        self.assertTrue(
            os.path.isfile(os.path.join(self.root, "A", "B", "x.zip")))

    async def test_label_rename_happens_inside_subdir(self):
        cdp = _WritingCDP(self._events(), "x.zip")
        task = chrome_agent.create_task(
            "https://a.com/x.zip", "s2", download_subdir="A/B")
        task["label"] = "#标"
        await chrome_agent.process_pending_tasks(
            cdp, [task], self.tasks_path, self.root,
            timeout=5, retries=3, wait_seconds=0)
        self.assertEqual(task["status"], "SUCCESS")
        self.assertEqual(task["filename"], "#标 x.zip")
        self.assertTrue(
            os.path.isfile(os.path.join(self.root, "A", "B", "#标 x.zip")))
        # 根目录不该出现任何东西
        self.assertFalse(os.path.exists(os.path.join(self.root, "x.zip")))

    async def test_no_subdir_still_lands_in_root(self):
        """回归：不带子目录的任务行为不变（还是落在根目录）。"""
        cdp = _WritingCDP(self._events(), "x.zip")
        task = chrome_agent.create_task("https://a.com/x.zip", "s3")
        await chrome_agent.process_pending_tasks(
            cdp, [task], self.tasks_path, self.root,
            timeout=5, retries=3, wait_seconds=0)
        self.assertIn(("setup_download", self.root), cdp.commands)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "x.zip")))


# ------------------------------------------------------------
# 任务取消（任务书 §3/§7/§8/§11/§14/§15/§16）
# ------------------------------------------------------------

class CancelStateTest(unittest.TestCase):
    """CANCELLED 终态：保留记录、不加 attempts、认领逻辑永不认领（§3）。"""

    def test_pending_cancelled_is_not_claimed(self):
        task = _task("c1")
        chrome_agent.mark_cancelled(task)
        self.assertEqual(task["status"], "CANCELLED")
        self.assertEqual(task["error"], chrome_agent.CANCEL_REASON)
        self.assertIsNotNone(task["finished_at"])
        self.assertIsNotNone(task["updated_at"])
        self.assertEqual(task["attempts"], 0)  # 取消不算一次尝试（§3）
        self.assertIsNone(chrome_agent.claim_next([task]))

    def test_retry_wait_cancelled_is_not_claimed_even_when_due(self):
        """§21 测试 2+4：RETRY_WAIT → CANCELLED 后，到期也绝不认领（错误 E）。"""
        task = _task("c2")
        chrome_agent.start_attempt(task)
        chrome_agent.fail_attempt(task, "网络断", retries=3, wait_seconds=0,
                                  now=datetime(2026, 9, 10, 10, 0, 0))
        self.assertEqual(task["status"], "RETRY_WAIT")
        chrome_agent.mark_cancelled(task)
        after_due = datetime(2026, 9, 10, 12, 0, 0)
        self.assertIsNone(chrome_agent.claim_next([task], now=after_due))

    def test_cancelled_survives_recovery(self):
        """§24 场景 D：重启恢复后仍是 CANCELLED，不得回落成 PENDING。"""
        task = _task("c3")
        chrome_agent.mark_cancelled(task)
        chrome_agent.recover_tasks([task], _TMP)
        self.assertEqual(task["status"], "CANCELLED")


class ApplyCancellationsTest(unittest.TestCase):
    """取消请求文件 → 任务状态（§7：User Bot 独占写、Agent 只读）。"""

    def setUp(self):
        self.path = os.path.join(_TMP, "chrome_cancel_apply.json")
        if os.path.exists(self.path):
            os.remove(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _write(self, ids):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"cancellations": [
                {"task_id": i, "created_at": "2026-09-10 12:00:00"}
                for i in ids]}, f)

    def test_pending_and_retry_wait_are_cancelled(self):
        pending = _task("k1")
        retry = _task("k2")
        chrome_agent.start_attempt(retry)
        chrome_agent.fail_attempt(retry, "断", retries=3, wait_seconds=30)
        self._write(["k1", "k2"])
        cancelled = chrome_agent.apply_cancellations(
            [pending, retry], self.path)
        self.assertEqual(sorted(cancelled), ["k1", "k2"])
        self.assertEqual(pending["status"], "CANCELLED")
        self.assertEqual(retry["status"], "CANCELLED")

    def test_terminal_tasks_untouched(self):
        """§21 测试 7：SUCCESS / FAILED / 已取消的任务一律不动。"""
        ok = _task("k3")
        chrome_agent.finish_success(ok, "a.zip", 1)
        bad = _task("k4")
        for _ in range(3):
            chrome_agent.start_attempt(bad)
        chrome_agent.fail_attempt(bad, "x", retries=3, wait_seconds=0)
        done = _task("k5")
        chrome_agent.mark_cancelled(done)
        self._write(["k3", "k4", "k5"])
        self.assertEqual(
            chrome_agent.apply_cancellations([ok, bad, done], self.path), [])
        self.assertEqual([ok["status"], bad["status"], done["status"]],
                         ["SUCCESS", "FAILED", "CANCELLED"])

    def test_missing_file_is_noop(self):
        task = _task("k6")
        self.assertEqual(
            chrome_agent.apply_cancellations([task], self.path), [])
        self.assertEqual(task["status"], "PENDING")

    def test_repeat_apply_is_idempotent(self):
        """请求文件是 append-only，同一 id 会被反复读到——不能重复改写终态。"""
        task = _task("k7")
        self._write(["k7"])
        self.assertEqual(chrome_agent.apply_cancellations([task], self.path),
                         ["k7"])
        finished_at = task["finished_at"]
        self.assertEqual(chrome_agent.apply_cancellations([task], self.path),
                         [])
        self.assertEqual(task["finished_at"], finished_at)


class CleanPartialTest(unittest.TestCase):
    """§15：半成品清理必须绑定「任务自己的目录 + 自己的文件名」，绝不 glob。"""

    def setUp(self):
        self.root = os.path.join(_TMP, "chrome_dl_partial")
        os.makedirs(self.root, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def _touch(self, *parts):
        path = os.path.join(self.root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"half")
        return path

    def test_partial_of_own_task_is_deleted(self):
        """§21 测试 5：取消 RUNNING 后本任务的 .crdownload 被删除。"""
        task = _task("d1")
        partial = self._touch("a.zip.crdownload")
        chrome_agent.clean_partial_download(self.root, task, filename="a.zip")
        self.assertFalse(os.path.exists(partial))

    def test_other_tasks_partial_is_kept(self):
        """§21 测试 6：取消 A，A 的半成品删、B 的半成品留（错误 D）。"""
        task = _task("d2")
        mine = self._touch("a.zip.crdownload")
        other = self._touch("b.zip.crdownload")
        chrome_agent.clean_partial_download(self.root, task, filename="a.zip")
        self.assertFalse(os.path.exists(mine))
        self.assertTrue(os.path.exists(other))

    def test_subdir_isolation(self):
        """带 download_subdir 的任务只在**自己的**目录里删（§15 兼容项）。"""
        task = _task("d3")
        task["download_subdir"] = "A/B"
        mine = self._touch("A", "B", "x.zip.crdownload")
        same_name_elsewhere = self._touch("x.zip.crdownload")
        chrome_agent.clean_partial_download(self.root, task, filename="x.zip")
        self.assertFalse(os.path.exists(mine))
        self.assertTrue(os.path.exists(same_name_elsewhere))

    def test_unknown_filename_deletes_nothing(self):
        """文件名未知（downloadWillBegin 尚未到）时无从归属，宁可不动手。"""
        task = _task("d4")
        stray = self._touch("stray.zip.crdownload")
        chrome_agent.clean_partial_download(self.root, task, filename=None)
        self.assertTrue(os.path.exists(stray))


class _EndingWS:
    """WebSocket 替身：消息吐完（或一开始就空）→ 接收循环自然退出。

    这正是「Chrome 崩了 / 专用实例被关掉」时真实 WS 的样子。
    """

    def __init__(self, messages=()):
        self._messages = list(messages)
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        pass


class CDPLivenessTest(unittest.IsolatedAsyncioTestCase):
    """CDP 判活与「断线立刻交回」（旧实现断了之后整个进程生命周期都不再连）。"""

    def _client(self):
        return chrome_agent.ChromeCDPClient("ws://127.0.0.1:1/devtools/browser/x")

    async def test_not_connected_is_dead(self):
        self.assertFalse(self._client().is_alive())

    async def test_next_event_returns_promptly_when_dead(self):
        """断线时不能干等满 timeout——否则一次断线要白等 30 分钟才失败。"""
        start = time.monotonic()
        self.assertIsNone(await self._client().next_event(timeout=30))
        self.assertLess(time.monotonic() - start, 1.0)

    async def test_recv_loop_death_marks_client_dead(self):
        with mock.patch("websockets.connect",
                        mock.AsyncMock(return_value=_EndingWS())):
            client = self._client()
            await client.connect()
            self.assertTrue(client.is_alive())
            await asyncio.sleep(0)      # 让接收循环跑到结束（流已空）
            await asyncio.sleep(0)
            self.assertFalse(client.is_alive())
            # 断线后取事件同样立即返回，而不是等满 timeout
            start = time.monotonic()
            self.assertIsNone(await client.next_event(timeout=30))
            self.assertLess(time.monotonic() - start, 1.0)

    async def test_close_marks_dead(self):
        with mock.patch("websockets.connect",
                        mock.AsyncMock(return_value=_EndingWS())):
            client = self._client()
            await client.connect()
            await client.close()
            self.assertFalse(client.is_alive())


class _DeadCDP(FakeCDP):
    """连接已死：取事件立刻返回 None，判活为 False。"""

    def __init__(self):
        super().__init__([])

    def is_alive(self):
        return False

    async def next_event(self, timeout):
        return None


class CDPDeadFastFailTest(unittest.IsolatedAsyncioTestCase):
    """断线时下载当场失败转 RETRY_WAIT（好让主循环重连后接着跑）。"""

    def setUp(self):
        self.dl_dir = os.path.join(_TMP, "chrome_dl_dead")
        os.makedirs(self.dl_dir, exist_ok=True)
        self.tasks_path = os.path.join(_TMP, "chrome_tasks_dead.json")
        if os.path.exists(self.tasks_path):
            os.remove(self.tasks_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dl_dir, ignore_errors=True)
        if os.path.exists(self.tasks_path):
            os.remove(self.tasks_path)

    async def test_attempt_fails_fast_with_clear_error(self):
        start = time.monotonic()
        ok, filename, size, error = await chrome_agent.run_download_attempt(
            _DeadCDP(), "https://a.com/x.zip", self.dl_dir, timeout=30)
        self.assertFalse(ok)
        self.assertIn("CDP 连接已断开", error)
        self.assertLess(time.monotonic() - start, 1.0)   # 不是等满 30s

    async def test_dead_cdp_failure_enters_retry_wait_not_failed(self):
        """必须留在 RETRY_WAIT（不是 FAILED）：重连后这一轮要能接着跑。"""
        task = chrome_agent.create_task("https://a.com/x.zip", "dead1")
        await chrome_agent.process_pending_tasks(
            _DeadCDP(), [task], self.tasks_path, self.dl_dir,
            timeout=5, retries=3, wait_seconds=30)
        self.assertEqual(task["status"], "RETRY_WAIT")
        self.assertEqual(task["attempts"], 1)


class EnsureCdpClientTest(unittest.IsolatedAsyncioTestCase):
    """ensure_cdp_client：活着复用、断了重连、连不上返回 None（不退出进程）。"""

    def _args(self):
        return ("bin", "profile", "127.0.0.1", 9222, 1.0, None, _TMP)

    async def test_alive_client_is_reused(self):
        client = mock.MagicMock()
        client.is_alive.return_value = True
        out = await chrome_agent.ensure_cdp_client(client, *self._args())
        self.assertIs(out, client)

    async def test_dead_client_is_replaced(self):
        client = mock.MagicMock()
        client.is_alive.return_value = False
        client.close = mock.AsyncMock()
        fresh = mock.MagicMock()
        fresh.connect = mock.AsyncMock()
        fresh.setup_download = mock.AsyncMock()
        with mock.patch.object(chrome_agent, "ensure_chrome_cdp",
                               mock.AsyncMock(return_value="ws://x")), \
                mock.patch.object(chrome_agent, "ChromeCDPClient",
                                  return_value=fresh):
            out = await chrome_agent.ensure_cdp_client(client, *self._args())
        self.assertIs(out, fresh)
        client.close.assert_awaited()          # 旧的先关掉
        fresh.connect.assert_awaited()
        fresh.setup_download.assert_awaited()  # 下载目录行为要重新设

    async def test_reconnect_failure_returns_none(self):
        client = mock.MagicMock()
        client.is_alive.return_value = False
        client.close = mock.AsyncMock()
        with mock.patch.object(chrome_agent, "ensure_chrome_cdp",
                               mock.AsyncMock(return_value=None)):
            self.assertIsNone(
                await chrome_agent.ensure_cdp_client(client, *self._args()))

    async def test_connect_error_is_swallowed(self):
        """连上了 WS 但握手失败（Chrome 正在关）不能把 Agent 带崩。"""
        client = mock.MagicMock()
        client.is_alive.return_value = False
        client.close = mock.AsyncMock()
        failed = mock.MagicMock()
        failed.connect = mock.AsyncMock(side_effect=OSError("connection reset"))
        failed.close = mock.AsyncMock()
        with mock.patch.object(chrome_agent, "ensure_chrome_cdp",
                               mock.AsyncMock(return_value="ws://x")), \
                mock.patch.object(chrome_agent, "ChromeCDPClient",
                                  return_value=failed):
            self.assertIsNone(
                await chrome_agent.ensure_cdp_client(client, *self._args()))
        failed.close.assert_awaited()


class TrimTerminalTasksTest(unittest.TestCase):
    """历史终态任务裁剪：只裁最老的，非终态一条都不动。"""

    def _done(self, tid):
        task = _task(tid)
        chrome_agent.finish_success(task, f"{tid}.zip", 1)
        return task

    def test_under_limit_keeps_everything(self):
        tasks = [self._done("a"), self._done("b")]
        self.assertEqual(chrome_agent.trim_terminal_tasks(tasks, keep=5), [])
        self.assertEqual(len(tasks), 2)

    def test_oldest_terminal_dropped(self):
        tasks = [self._done("a"), self._done("b"), self._done("c")]
        removed = chrome_agent.trim_terminal_tasks(tasks, keep=2)
        self.assertEqual([t["task_id"] for t in removed], ["a"])
        self.assertEqual([t["task_id"] for t in tasks], ["b", "c"])

    def test_non_terminal_never_touched(self):
        """在跑/排队的任务一条都不能动（错误地裁掉 = 任务凭空消失）。"""
        running = _task("r1")
        running["status"] = "RUNNING"
        pending = _task("p1")
        tasks = [running, self._done("a"), pending, self._done("b")]
        chrome_agent.trim_terminal_tasks(tasks, keep=1)
        self.assertEqual([t["task_id"] for t in tasks], ["r1", "p1", "b"])

    def test_keeps_in_place_and_returns_dropped(self):
        """原地裁剪：调用方持有的列表对象不变（agent_main 全程用同一个）。"""
        tasks = [self._done("a"), self._done("b")]
        ref = tasks
        chrome_agent.trim_terminal_tasks(tasks, keep=1)
        self.assertIs(tasks, ref)
        self.assertEqual([t["task_id"] for t in tasks], ["b"])


class OrphanPartialSweepTest(unittest.TestCase):
    """启动清扫孤儿 .crdownload：只删「没人认领且久未动过」的（§15 漏网口）。"""

    def setUp(self):
        self.root = os.path.join(_TMP, "chrome_dl_sweep")
        os.makedirs(self.root, exist_ok=True)
        self.old = time.time() - chrome_agent.ORPHAN_PARTIAL_MIN_AGE_SECONDS - 60

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def _touch(self, *parts, age=None):
        path = os.path.join(self.root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"half")
        if age is not None:
            os.utime(path, (age, age))
        return path

    def test_old_unclaimed_partial_is_removed(self):
        orphan = self._touch("ghost.zip.crdownload", age=self.old)
        self.assertEqual(
            chrome_agent.sweep_orphan_partials(self.root, []), [orphan])
        self.assertFalse(os.path.exists(orphan))

    def test_live_task_partial_is_kept(self):
        """非终态任务（已认领、等待重试等）的半成品必须留着给它续跑。"""
        task = _task("w1")
        task["filename"] = "mine.zip"
        task["status"] = "RETRY_WAIT"
        kept = self._touch("mine.zip.crdownload", age=self.old)
        self.assertEqual(chrome_agent.sweep_orphan_partials(self.root, [task]),
                         [])
        self.assertTrue(os.path.exists(kept))

    def test_fresh_partial_is_kept(self):
        """刚写过的半成品可能是上一个 Agent 进程仍在写的（Chrome 还活着）。"""
        fresh = self._touch("busy.zip.crdownload", age=time.time())
        self.assertEqual(chrome_agent.sweep_orphan_partials(self.root, []), [])
        self.assertTrue(os.path.exists(fresh))

    def test_finished_files_are_never_touched(self):
        """成品与主下载链路的 .download 都不归它管（只碰 .crdownload）。"""
        completed = self._touch("done.zip", age=self.old)
        other = self._touch("x.download", age=self.old)
        chrome_agent.sweep_orphan_partials(self.root, [])
        self.assertTrue(os.path.exists(completed))
        self.assertTrue(os.path.exists(other))

    def test_subdir_task_partial_kept(self):
        """带子目录的任务：文件在它自己的目录里，同样按全路径认定归属。"""
        task = _task("w2")
        task["filename"] = "deep.zip"
        task["download_subdir"] = "A/B"
        task["status"] = "PENDING"
        kept = self._touch("A", "B", "deep.zip.crdownload", age=self.old)
        orphan = self._touch("A", "ghost.zip.crdownload", age=self.old)
        self.assertEqual(
            chrome_agent.sweep_orphan_partials(self.root, [task]), [orphan])
        self.assertTrue(os.path.exists(kept))


class _HangingCDP(FakeCDP):
    """吐完脚本事件后挂起（下载进行中的样子），直到被取消或超时。"""

    def __init__(self, guid, filename):
        super().__init__([
            {"method": "Browser.downloadWillBegin",
             "params": {"guid": guid, "suggestedFilename": filename}},
        ], hang=True)


class _DelayedBeginCDP(FakeCDP):
    """downloadWillBegin 迟到（模拟「先收到取消、guid 还没产生」，§11）。

    事件经真 asyncio 队列由后台任务延迟投递：取消掉一次 next_event **不会
    吞掉**在途事件——这正是真 ChromeCDPClient 的语义（wait_for(queue.get())
    被取消不消费队列），也是 §13 要求不能丢事件的那条线。
    """

    def __init__(self, guid, filename, delay):
        super().__init__([], hang=True)
        self._queue = asyncio.Queue()
        self._producer = None
        self._delay = delay
        self._begin = {"method": "Browser.downloadWillBegin",
                       "params": {"guid": guid,
                                  "suggestedFilename": filename}}

    async def next_event(self, timeout):
        if self._producer is None:
            self._producer = asyncio.ensure_future(self._produce())
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    async def _produce(self):
        await asyncio.sleep(self._delay)
        await self._queue.put(self._begin)


class _PerUrlCDP(FakeCDP):
    """按 URL 分脚本的 CDP：任务串行执行，B 的事件不会串进 A 的下载。

    平面事件表会让「A 还在跑」时把 B 的 downloadWillBegin 认成自己的
    （真实 FIFO 串行下不可能发生），所以这里按 open_tab 的 URL 取脚本。
    """

    def __init__(self, script):
        super().__init__([], hang=True)
        self._script = script      # {url: [event, ...]}
        self._current = []

    async def open_tab(self, url):
        self.commands.append(("open_tab", url))
        self._current = list(self._script.get(url, []))
        return "target-1"

    async def next_event(self, timeout):
        if self._current:
            return self._current.pop(0)
        if self._hang:
            await asyncio.sleep(max(0.0, timeout))
        return None


class _CompleteWithCancelCDP(FakeCDP):
    """返回 completed 事件的同时置位取消：模拟「完成与取消同时确认」（§14）。"""

    def __init__(self, events, cancel_event):
        super().__init__(events)
        self._cancel = cancel_event

    async def next_event(self, timeout):
        event = await super().next_event(timeout)
        if (event or {}).get("params", {}).get("state") == "completed":
            self._cancel.set()
        return event


class CancelRunningTest(unittest.IsolatedAsyncioTestCase):
    """§8/§11/§14：取消 RUNNING 必须真正中止 Chrome 下载，而不是只改 JSON。"""

    def setUp(self):
        self.dl_dir = os.path.join(_TMP, "chrome_dl_cancel")
        os.makedirs(self.dl_dir, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dl_dir, ignore_errors=True)

    async def test_cancel_with_guid_issues_cdp_cancel(self):
        cdp = _HangingCDP("g1", "a.zip")
        cancel = asyncio.Event()
        attempt = asyncio.ensure_future(chrome_agent.run_download_attempt(
            cdp, "https://a.com/a.zip", self.dl_dir, timeout=30,
            cancel_event=cancel))
        await asyncio.sleep(0.05)  # 让 downloadWillBegin 先被消费
        cancel.set()
        ok, filename, size, error = await asyncio.wait_for(attempt, 5)
        self.assertFalse(ok)
        self.assertEqual(filename, "a.zip")  # 供半成品清理定位
        self.assertIn(("Browser.cancelDownload", {"guid": "g1"}),
                      cdp.commands)
        # 收尾照旧：标签页不能因为取消而不关
        self.assertIn(("close_tab", "target-1"), cdp.commands)

    async def test_cancel_before_guid_still_cancels(self):
        """§11/错误 G：guid 尚未产生就取消，宽限窗口内等到 begin 立刻取消。"""
        cdp = _DelayedBeginCDP("g2", "b.zip", delay=0.15)
        cancel = asyncio.Event()
        attempt = asyncio.ensure_future(chrome_agent.run_download_attempt(
            cdp, "https://a.com/b.zip", self.dl_dir, timeout=30,
            cancel_event=cancel))
        await asyncio.sleep(0.02)
        cancel.set()
        ok, filename, size, error = await asyncio.wait_for(attempt, 5)
        self.assertFalse(ok)
        self.assertIn(("Browser.cancelDownload", {"guid": "g2"}),
                      cdp.commands)

    async def test_completed_event_not_lost_when_cancel_lands_together(self):
        """§14：完成与取消同时到达时不能丢掉 completed 事件（保留 SUCCESS）。"""
        final = os.path.join(self.dl_dir, "c.zip")
        with open(final, "wb") as f:
            f.write(b"z" * 7)
        cancel = asyncio.Event()
        cdp = _CompleteWithCancelCDP([
            {"method": "Browser.downloadWillBegin",
             "params": {"guid": "g3", "suggestedFilename": "c.zip"}},
            {"method": "Browser.downloadProgress",
             "params": {"guid": "g3", "state": "completed"}},
        ], cancel)
        ok, filename, size, error = await chrome_agent.run_download_attempt(
            cdp, "https://a.com/c.zip", self.dl_dir, timeout=5,
            cancel_event=cancel)
        self.assertTrue(ok)
        self.assertEqual(size, 7)
        self.assertNotIn(("Browser.cancelDownload", {"guid": "g3"}),
                         cdp.commands)

    async def test_no_cancel_event_keeps_old_behaviour(self):
        """回归：不传 cancel_event 时一切照旧（现存调用点与测试不受影响）。"""
        final = os.path.join(self.dl_dir, "d.zip")
        with open(final, "wb") as f:
            f.write(b"z" * 3)
        cdp = FakeCDP([
            {"method": "Browser.downloadWillBegin",
             "params": {"guid": "g4", "suggestedFilename": "d.zip"}},
            {"method": "Browser.downloadProgress",
             "params": {"guid": "g4", "state": "completed"}},
        ])
        ok, filename, size, error = await chrome_agent.run_download_attempt(
            cdp, "https://a.com/d.zip", self.dl_dir, timeout=5)
        self.assertTrue(ok)
        self.assertEqual(size, 3)


class CancelProcessFlowTest(unittest.IsolatedAsyncioTestCase):
    """process_pending_tasks 与取消请求文件的接线（§16：取消后继续跑下一个）。"""

    def setUp(self):
        self.dl_dir = os.path.join(_TMP, "chrome_dl_cancel_flow")
        os.makedirs(self.dl_dir, exist_ok=True)
        self.tasks_path = os.path.join(_TMP, "chrome_tasks_cancel.json")
        self.cancel_path = os.path.join(_TMP, "chrome_cancel_flow.json")
        for p in (self.tasks_path, self.cancel_path):
            if os.path.exists(p):
                os.remove(p)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dl_dir, ignore_errors=True)
        for p in (self.tasks_path, self.cancel_path):
            if os.path.exists(p):
                os.remove(p)

    def _write_cancel(self, task_id):
        with open(self.cancel_path, "w", encoding="utf-8") as f:
            json.dump({"cancellations": [{"task_id": task_id}]}, f)

    async def test_cancel_running_task_then_next_task_runs(self):
        """§24 场景 C：A 下载中被取消 → A CANCELLED，B 接着跑完成。"""
        with open(os.path.join(self.dl_dir, "b.zip"), "wb") as f:
            f.write(b"z" * 11)
        cdp = _PerUrlCDP({
            # A：只给 downloadWillBegin，此后一直挂着（正在下载）
            "https://a.com/a.zip": [
                {"method": "Browser.downloadWillBegin",
                 "params": {"guid": "ga", "suggestedFilename": "a.zip"}},
            ],
            # B：完整跑完
            "https://a.com/b.zip": [
                {"method": "Browser.downloadWillBegin",
                 "params": {"guid": "gb", "suggestedFilename": "b.zip"}},
                {"method": "Browser.downloadProgress",
                 "params": {"guid": "gb", "state": "completed"}},
            ],
        })
        a = chrome_agent.create_task("https://a.com/a.zip", "a1")
        b = chrome_agent.create_task("https://a.com/b.zip", "b2")

        original_open = cdp.open_tab

        async def open_tab_and_cancel(url):
            # A 的标签页一开就写下取消请求：模拟「用户在其下载中取消 A」
            self._write_cancel("a1")
            return await original_open(url)

        cdp.open_tab = open_tab_and_cancel

        await chrome_agent.process_pending_tasks(
            cdp, [a, b], self.tasks_path, self.dl_dir,
            timeout=1, retries=3, wait_seconds=0,
            cancel_path=self.cancel_path, cancel_poll=0.01)

        self.assertEqual(a["status"], "CANCELLED")
        self.assertEqual(b["status"], "SUCCESS")     # 错误 F：取消 A 后 B 照样执行
        # 取消本身不加 attempts：A 的 1 次来自它真的跑过（start_attempt）
        self.assertEqual(a["attempts"], 1)
        self.assertEqual(b["attempts"], 1)
        self.assertIn(("Browser.cancelDownload", {"guid": "ga"}), cdp.commands)

    async def test_cancel_pending_before_start_never_downloads(self):
        """排队任务取消：认领前就被拦下，绝不发起下载。"""
        cdp = FakeCDP([], hang=True)
        a = chrome_agent.create_task("https://a.com/a.zip", "z1")
        self._write_cancel("z1")
        await chrome_agent.process_pending_tasks(
            cdp, [a], self.tasks_path, self.dl_dir,
            timeout=1, retries=3, wait_seconds=0,
            cancel_path=self.cancel_path, cancel_poll=0.01)
        self.assertEqual(a["status"], "CANCELLED")
        self.assertEqual([c for c in cdp.commands if c[0] == "open_tab"], [])


if __name__ == "__main__":
    unittest.main()
