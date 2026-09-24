"""sh 文件夹浏览器（ls → 可点击目录按钮）与回复代码块化测试。

Part B：ls 输出解析 → 目录绝对路径注册表 → sh_ls 回调按钮（64 字节限制
用 hash8 键绕开）；Part A：with_code_block 统一指令回复代码块化。

    .venv/bin/python -m unittest tests.test_shell_browser tests.test_reply_format -v
"""
import asyncio
import os
import shutil
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录（config import 期
# 有真实副作用，见 test_config 的基座说明）。
_TMP = tempfile.mkdtemp(prefix="tg_userbot_shell_browser_test_")
import atexit  # noqa: E402
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import menu  # noqa: E402
from tg_userbot import shell  # noqa: E402
from tg_userbot import state  # noqa: E402

LS_FIXTURE = """total 48
drwxr-xr-x@  5 user  staff   160 Jan  1 10:00 频道A
drwxr-xr-x@  3 user  staff    96 Jan  1 10:00 TG Chrome Download
-rw-r--r--@  1 user  staff  1234 Jan  1 10:00 视频.mp4
lrwxr-xr-x   1 user  staff    12 Jan  1 10:00 链接 -> /tmp
drwx------   7 user  staff   224 Jan  1 10:00 带 空格 目录
-rw-r--r--   1 user  staff     0 Jan  1 10:00 .
..wtfline
"""


class TestParseLsEntries(unittest.TestCase):
    """ls -la 输出 → 目录名列表（保序；文件/链接/坏行排除）。"""

    def test_dirs_only_in_order(self):
        dirs = shell.parse_ls_entries(LS_FIXTURE)
        self.assertEqual(dirs, ["频道A", "TG Chrome Download", "带 空格 目录"])

    def test_dot_and_dotdot_excluded(self):
        out = ("total 0\ndrwxr-xr-x  2 u s 64 Jan 1 10:00 .\n"
               "drwxr-xr-x  3 u s 96 Jan 1 10:00 ..\n"
               "drwxr-xr-x  2 u s 64 Jan 1 10:00 real\n")
        self.assertEqual(shell.parse_ls_entries(out), ["real"])

    def test_empty_output(self):
        self.assertEqual(shell.parse_ls_entries(""), [])
        self.assertEqual(shell.parse_ls_entries("total 0\n"), [])

    def test_non_ls_output(self):
        self.assertEqual(shell.parse_ls_entries("Volume /\nName blah\n"),
                         [])


class TestExtractLsDirs(unittest.TestCase):
    """command_reply 的回复 + 命令 → 目录绝对路径（base 解析正确）。"""

    def _reply(self, listing):
        return f"$ ls -la\n```\n{listing}\n```"

    def test_simple_ls_uses_cwd(self):
        dirs = shell.extract_ls_dirs(
            "ls -la", self._reply(LS_FIXTURE), "/base/cwd")
        self.assertEqual(dirs, ["/base/cwd/频道A",
                                "/base/cwd/TG Chrome Download",
                                "/base/cwd/带 空格 目录"])

    def test_ls_with_abs_operand(self):
        dirs = shell.extract_ls_dirs(
            "ls -la /Volumes/V1", self._reply(LS_FIXTURE), "/elsewhere")
        self.assertEqual(dirs[0], "/Volumes/V1/频道A")

    def test_ls_with_relative_operand(self):
        dirs = shell.extract_ls_dirs(
            "ls downloads", self._reply(LS_FIXTURE), "/base")
        self.assertEqual(dirs[0], "/base/downloads/频道A")

    def test_non_ls_command(self):
        self.assertEqual(
            shell.extract_ls_dirs("df -h", self._reply(LS_FIXTURE), "/x"),
            [])

    def test_ls_with_flags_only_flags_ok(self):
        dirs = shell.extract_ls_dirs(
            "ls -la -A", self._reply(LS_FIXTURE), "/base")
        self.assertEqual(dirs[0], "/base/频道A")

    def test_missing_closing_fence(self):
        self.assertEqual(
            shell.extract_ls_dirs("ls", "$ ls\n```\ndrwx x", "/b"), [])


class TestPathRegistry(unittest.TestCase):
    """hash8 路径注册表：round-trip、未知 token、容量上限淘汰。"""

    def setUp(self):
        self._old = state.LS_PATHS
        state.LS_PATHS = {}
        self.addCleanup(setattr, state, "LS_PATHS", self._old)

    def test_roundtrip(self):
        real = tempfile.mkdtemp(dir=_TMP)
        token = shell.register_ls_paths([real, "/c d"])[0]
        self.assertEqual(shell.resolve_ls_dir(token), real)

    def test_unknown_token(self):
        self.assertIsNone(shell.resolve_ls_dir("deadbeef"))

    def test_bounded_eviction(self):
        for i in range(200):
            shell.register_ls_paths([f"/dir{i}"])
        self.assertLessEqual(len(state.LS_PATHS), 128)
        self.assertIsNone(shell.resolve_ls_dir(
            shell.register_ls_paths(["/probe"]) and "nope"))

    def test_resolve_validates_isdir(self):
        token = shell.register_ls_paths(["/no/such/dir"])[0]
        self.assertIsNone(shell.resolve_ls_dir(token))


class TestChangeCwd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=_TMP)
        os.makedirs(os.path.join(self.tmp, "sub"))
        self._old_cwd = state.SHELL_CWD
        self.addCleanup(setattr, state, "SHELL_CWD", self._old_cwd)
        self._p = mock.patch.object(config, "SHELL_STATE_FILE",
                                    os.path.join(self.tmp, "shell_state.json"))
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_change_and_persist(self):
        target = os.path.realpath(os.path.join(self.tmp, "sub"))
        self.assertTrue(shell.change_cwd(target))
        self.assertEqual(state.SHELL_CWD, target)   # realpath 归一
        with open(config.SHELL_STATE_FILE, encoding="utf-8") as f:
            import json
            self.assertEqual(json.load(f)["cwd"], target)

    def test_invalid_dir_rejected(self):
        before = state.SHELL_CWD
        self.assertFalse(shell.change_cwd("/no/such/dir"))
        self.assertEqual(state.SHELL_CWD, before)


class TestShLsButtons(unittest.TestCase):
    """文件夹按钮网格：2 个/行、回调数据 ≤64 字节、长名截断。"""

    def test_rows_of_two_and_byte_limit(self):
        dirs = [(f"目录{i}", f"{i:08x}") for i in range(5)]
        rows = menu.sh_ls_buttons(dirs, up_token="abcdef01",
                                  home_token="12345678")
        # 3 行目录按钮（2+2+1）+ 1 行导航（⬆️ + 🏠）
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(rows[0]), 2)
        self.assertEqual(len(rows[2]), 1)
        for row in rows:
            for btn in row:
                self.assertLessEqual(len(btn.data), 64)

    def test_labels_trimmed(self):
        rows = menu.sh_ls_buttons([("很" * 40, "00000001")], None, None)
        self.assertLessEqual(len(rows[0][0].text), 30)

    def test_no_dirs_no_nav(self):
        rows = menu.sh_ls_buttons([], None, None)
        self.assertEqual(rows, [])

    def test_up_only_when_token(self):
        rows = menu.sh_ls_buttons([], "abcdef01", None)
        self.assertEqual(len(rows), 1)
        self.assertIn("⬆️", rows[0][0].text)


class TestShLsCallbackFlow(unittest.IsolatedAsyncioTestCase):
    """sh_ls 回调：解析 → cd → ls → 目录按钮（网络全 mock）。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(dir=_TMP)
        os.makedirs(os.path.join(self.tmp, "sub"))
        self._old = (state.SHELL_CWD, state.LS_PATHS)
        state.SHELL_CWD = self.tmp
        state.LS_PATHS = {}
        self.addCleanup(self._restore)

    def _restore(self):
        state.SHELL_CWD, state.LS_PATHS = self._old

    async def test_sh_ls_flow(self):
        from tg_userbot import bot
        sub = os.path.realpath(os.path.join(self.tmp, "sub"))
        token = shell.register_ls_paths([sub])[0]
        listing = ("total 0\ndrwxr-xr-x 2 u s 64 Jan 1 10:00 inner\n"
                   "-rw-r--r-- 1 u s 0 Jan 1 10:00 f.mp4\n")
        reply = f"$ ls -la\n```\n{listing}\n```"

        async def fake_command_reply(cmd, record=True):
            return reply

        with mock.patch.object(shell, "command_reply",
                               side_effect=fake_command_reply):
            text, buttons = await bot.handle_menu_action("sh_ls", token, None)
        self.assertEqual(state.SHELL_CWD, sub)
        self.assertIn("inner", text)       # ls 输出进了回复
        flat = [b for row in buttons for b in row]
        self.assertTrue(any("📁" in b.text for b in flat))   # 目录按钮
        self.assertTrue(any("⬆️" in b.text for b in flat))   # 上级导航
        # 按钮只显示条目名，不带全路径（2026-09-15 用户反馈）
        inner_btn = next(b for b in flat if "inner" in b.text)
        self.assertNotIn(self.tmp, inner_btn.text)
        self.assertTrue(any(b.text == "📁 inner" for b in flat))

    async def test_expired_token(self):
        from tg_userbot import bot
        text, buttons = await bot.handle_menu_action("sh_ls", "nope1234",
                                                     None)
        self.assertIn("失效", text)


if __name__ == "__main__":
    unittest.main()
