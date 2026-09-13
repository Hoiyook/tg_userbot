"""/sh 命令行执行器（shell.py + handle_command 接线）的单元测试。

覆盖：命令识别、黑名单拦截、cd 切换与持久化、子进程执行
（stdout/stderr 合并、退出码、超时）、输出截断、启动恢复。

运行方式（在项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import asyncio
import atexit
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_shell_test_")
# 退出时回收临时目录（测试跑完就地删，别让 /var/folders 越堆越多）
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import commands  # noqa: E402
from tg_userbot import config  # noqa: E402
from tg_userbot import shell  # noqa: E402
from tg_userbot import state  # noqa: E402


def _make_event():
    class FakeEvent:
        """仅实现 handle_command 用到的 reply：捕获回复文本，不碰网络。"""

        def __init__(self):
            self.replies = []

        async def reply(self, text, **kwargs):
            self.replies.append(text)

    return FakeEvent()


def _run(cmd):
    ev = _make_event()
    ok = asyncio.run(commands.handle_command(ev, cmd))
    return ok, ev.replies


class ShellCommandTest(unittest.TestCase):
    def setUp(self):
        # 每个用例从干净状态出发：工作目录=默认，状态文件不存在
        self._old_cwd = state.SHELL_CWD
        state.SHELL_CWD = config.REPO_ROOT
        if os.path.exists(config.SHELL_STATE_FILE):
            os.remove(config.SHELL_STATE_FILE)
        self.subdir = os.path.join(_TMP, "shell_sub")
        os.makedirs(self.subdir, exist_ok=True)

    def tearDown(self):
        state.SHELL_CWD = self._old_cwd
        if os.path.exists(config.SHELL_STATE_FILE):
            os.remove(config.SHELL_STATE_FILE)

    # ---------- 命令识别 ----------

    def test_is_shell_command(self):
        self.assertTrue(shell.is_shell_command("/sh"))
        self.assertTrue(shell.is_shell_command("/sh ls"))
        self.assertFalse(shell.is_shell_command("/shx"))
        self.assertFalse(shell.is_shell_command("/shell"))
        self.assertFalse(shell.is_shell_command("/shfoo bar"))
        self.assertFalse(shell.is_shell_command("/status"))

    # ---------- 用法 ----------

    def test_usage_when_no_args(self):
        ok, replies = _run("/sh")
        self.assertTrue(ok)
        self.assertEqual(len(replies), 1)
        self.assertIn("用法", replies[0])

    # ---------- 执行 ----------

    def test_exec_simple(self):
        ok, replies = _run("/sh echo shell-test-ok")
        self.assertTrue(ok)
        self.assertEqual(len(replies), 1)
        self.assertIn("shell-test-ok", replies[0])
        self.assertIn("$ echo shell-test-ok", replies[0])

    def test_exec_output_wrapped_in_code_block(self):
        ok, replies = _run("/sh echo fence-check")
        self.assertTrue(ok)
        self.assertIn("```\nfence-check\n```", replies[0])

    def test_exec_in_persistent_cwd(self):
        state.SHELL_CWD = _TMP
        ok, replies = _run("/sh pwd")
        self.assertTrue(ok)
        self.assertIn(_TMP, replies[0])

    def test_stderr_merged_and_exit_code_reported(self):
        ok, replies = _run("/sh ls /nonexistent_shell_test_path_xyz")
        self.assertTrue(ok)
        self.assertIn("退出码", replies[0])
        # stderr 与 stdout 合并捕获（No such file 是 stderr 内容）
        self.assertIn("No such file", replies[0])

    def test_empty_output_noted(self):
        ok, replies = _run("/sh true")
        self.assertTrue(ok)
        self.assertIn("无输出", replies[0])

    def test_timeout_kills_process(self):
        # 把超时阈值压到 1 秒，避免测试真等默认 30 秒
        with mock.patch.object(config, "SHELL_TIMEOUT_SECONDS", 1):
            ok, replies = _run("/sh sleep 5")
        self.assertTrue(ok)
        self.assertIn("超时", replies[0])

    # ---------- 黑名单 ----------

    def test_blacklist_rejects_dangerous_commands(self):
        for cmd in [
            "/sh sudo ls",
            "/sh rm -rf /tmp/x",
            "/sh dd if=/a of=/b",
            "/sh kill 123",
            "/sh ls && shutdown -h now",
        ]:
            with self.subTest(cmd=cmd):
                ok, replies = _run(cmd)
                self.assertTrue(ok)
                self.assertEqual(len(replies), 1)
                self.assertIn("拒绝", replies[0])

    def test_blacklist_rejects_dev_redirect(self):
        for cmd in ["/sh echo x > /dev/sda", "/sh echo x >/dev/sda"]:
            with self.subTest(cmd=cmd):
                ok, replies = _run(cmd)
                self.assertTrue(ok)
                self.assertIn("拒绝", replies[0])

    def test_blacklist_allows_safe_command(self):
        ok, replies = _run("/sh echo blacklist-pass")
        self.assertTrue(ok)
        self.assertIn("blacklist-pass", replies[0])
        self.assertNotIn("拒绝", replies[0])

    # ---------- cd ----------

    def test_cd_success_updates_state_and_persists(self):
        ok, replies = _run(f"/sh cd {self.subdir}")
        self.assertTrue(ok)
        self.assertEqual(state.SHELL_CWD, os.path.realpath(self.subdir))
        self.assertIn(os.path.realpath(self.subdir), replies[0])
        with open(config.SHELL_STATE_FILE, "r", encoding="utf-8") as f:
            self.assertEqual(
                json.load(f).get("cwd"), os.path.realpath(self.subdir)
            )

    def test_cd_relative_path_resolves_against_current_cwd(self):
        state.SHELL_CWD = _TMP
        ok, replies = _run("/sh cd shell_sub")
        self.assertTrue(ok)
        self.assertEqual(state.SHELL_CWD, os.path.realpath(self.subdir))

    def test_cd_nonexistent_rejected_state_unchanged(self):
        ok, replies = _run("/sh cd /nonexistent_shell_test_dir_xyz")
        self.assertTrue(ok)
        self.assertIn("❌", replies[0])
        self.assertEqual(state.SHELL_CWD, config.REPO_ROOT)

    def test_cd_no_arg_shows_current(self):
        state.SHELL_CWD = self.subdir
        ok, replies = _run("/sh cd")
        self.assertTrue(ok)
        self.assertIn(self.subdir, replies[0])

    # ---------- 输出截断 ----------

    def test_truncate_output_long_head_tail(self):
        out = "H" * 2000 + "MIDDLE" + "T" * 2000
        result = shell.truncate_output(out)
        self.assertLess(len(result), 3400)
        self.assertIn("省略", result)
        self.assertTrue(result.startswith("H"))
        self.assertTrue(result.endswith("T"))

    def test_truncate_output_short_unchanged(self):
        self.assertEqual(shell.truncate_output("short"), "short")

    # ---------- 启动恢复 ----------

    def test_load_shell_cwd_restores_from_file(self):
        with open(config.SHELL_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"cwd": self.subdir}, f)
        shell.load_shell_cwd()
        self.assertEqual(state.SHELL_CWD, self.subdir)

    def test_load_shell_cwd_fallback_on_missing_dir(self):
        with open(config.SHELL_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"cwd": "/nonexistent_shell_test_dir_xyz"}, f)
        shell.load_shell_cwd()
        self.assertEqual(state.SHELL_CWD, config.REPO_ROOT)

    def test_load_shell_cwd_no_file_defaults_repo_root(self):
        shell.load_shell_cwd()
        self.assertEqual(state.SHELL_CWD, config.REPO_ROOT)

    def test_load_shell_cwd_corrupt_file_defaults_repo_root(self):
        with open(config.SHELL_STATE_FILE, "w", encoding="utf-8") as f:
            f.write("not json{")
        shell.load_shell_cwd()
        self.assertEqual(state.SHELL_CWD, config.REPO_ROOT)


if __name__ == "__main__":
    unittest.main()
