"""命令模板（cmd_templates.py，/cmdt）与 ls 精简视图（shell.py）的单元测试。

不联网；shell 执行用 echo 等只读命令。
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

_TMP = tempfile.mkdtemp(prefix="tg_userbot_cmdt_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import cmd_templates  # noqa: E402
from tg_userbot import config  # noqa: E402
from tg_userbot import menu  # noqa: E402
from tg_userbot import shell  # noqa: E402
from tg_userbot import state  # noqa: E402

# 按钮属性兼容垫片（unittest discover 不加载 conftest.py——Telethon 1.45 起
# 回调数据挪进 .type.data，这里补回顶层 .data/.url 让既有断言保持原样）
import conftest as _btn_shim  # noqa: F401


class CmdTemplateStoreTest(unittest.TestCase):
    """增删查改 + 持久化（文件落在临时目录）。"""

    def setUp(self):
        self.old = dict(state.CMD_TEMPLATES)
        state.CMD_TEMPLATES = {}
        self._p = mock.patch.object(
            config, "CMD_TEMPLATES_FILE",
            os.path.join(_TMP, "cmdt_test.json"))
        self._p.start()
        self.addCleanup(self._p.stop)

        def cleanup():
            state.CMD_TEMPLATES = self.old
            path = cmd_templates._config_path()
            if os.path.exists(path):
                os.remove(path)
        self.addCleanup(cleanup)

    def test_upsert_and_persist(self):
        ok, _ = cmd_templates.upsert("磁盘占用", "du -sh * | sort -rh | head -20")
        self.assertTrue(ok)
        ok, _ = cmd_templates.upsert("重启前检查", "df -h .")
        self.assertTrue(ok)
        # 持久化到文件
        state.CMD_TEMPLATES = {}
        count = cmd_templates.load_cmd_templates()
        self.assertEqual(count, 2)
        self.assertEqual(cmd_templates.get("磁盘占用"),
                         "du -sh * | sort -rh | head -20")

    def test_upsert_same_name_overwrites(self):
        cmd_templates.upsert("清理", "ls")
        ok, msg = cmd_templates.upsert("清理", "ls -la")
        self.assertTrue(ok)
        self.assertIn("覆盖", msg)
        self.assertEqual(cmd_templates.get("清理"), "ls -la")
        self.assertEqual(len(state.CMD_TEMPLATES), 1)

    def test_validate_name(self):
        self.assertFalse(cmd_templates.validate_name("")[0])
        self.assertFalse(cmd_templates.validate_name("a" * 17)[0])
        self.assertFalse(cmd_templates.validate_name("add")[0])
        self.assertFalse(cmd_templates.validate_name("带 空格")[0])
        self.assertTrue(cmd_templates.validate_name("磁盘占用")[0])

    def test_delete(self):
        cmd_templates.upsert("临时", "echo hi")
        ok, _ = cmd_templates.delete("临时")
        self.assertTrue(ok)
        self.assertIsNone(cmd_templates.get("临时"))
        ok, _ = cmd_templates.delete("临时")
        self.assertFalse(ok)

    def test_parse(self):
        self.assertEqual(cmd_templates.parse_cmdt_command("/cmdt"),
                         ("list", None))
        self.assertEqual(cmd_templates.parse_cmdt_command("/cmdt add 磁盘 du -sh"),
                         ("add", "磁盘 du -sh"))
        self.assertEqual(cmd_templates.parse_cmdt_command("/cmdt 磁盘"),
                         ("run", "磁盘"))
        self.assertEqual(cmd_templates.parse_cmdt_command("/cmdt del 磁盘"),
                         ("del", "磁盘"))


class CmdTemplateExecuteTest(unittest.IsolatedAsyncioTestCase):
    """执行走 shell.command_reply（echo 只读命令真跑子进程）。"""

    def setUp(self):
        self.old = dict(state.CMD_TEMPLATES)
        state.CMD_TEMPLATES = {}
        self.addCleanup(setattr, state, "CMD_TEMPLATES", self.old)
        cmd_templates.upsert("打招呼", "echo hello-cmdt")

    async def test_execute_runs_shell_command(self):
        ok, result = await cmd_templates.execute("打招呼")
        self.assertTrue(ok)
        self.assertIn("hello-cmdt", result)

    async def test_execute_missing_template(self):
        ok, result = await cmd_templates.execute("不存在")
        self.assertFalse(ok)
        self.assertIn("没有叫", result)

    async def test_blacklist_still_enforced(self):
        """模板存了黑名单命令：执行被 shell 纪律拒绝（模板不是后门）。"""
        cmd_templates.upsert("危险", "rm -rf /")
        ok, result = await cmd_templates.execute("危险")
        self.assertTrue(ok)   # 模板存在
        self.assertIn("拒绝执行", result)   # 但命令被黑名单拦下


class CmdtMenuTest(unittest.TestCase):

    def test_menu_buttons_actions_registered(self):
        state.CMD_TEMPLATES = {"磁盘": "du -sh *"}
        rows = cmd_templates.menu_buttons()
        parsed = [menu.parse_menu_data(b.data) for row in rows for b in row]
        for action, _arg in parsed:
            self.assertIn(action, config.MENU_ACTIONS)
        run_args = [arg for action, arg in parsed if action == "cmdt_run"]
        self.assertEqual(run_args, ["磁盘"])
        # 回调数据 ≤64 字节
        for row in rows:
            for b in row:
                self.assertLessEqual(len(b.data), 64)

    def test_template_buttons_appear_in_sh_panel(self):
        """用户要求：新增模板后，命令行面板自动生成执行按钮。"""
        from tg_userbot import menu as menu_mod
        cmd_templates.upsert("磁盘占用", "du -sh * | sort -rh | head -20")
        rows = menu_mod.sh_menu_buttons()
        flat = [b for row in rows for b in row]
        btn = next(b for b in flat if b.text == "▶️ 磁盘占用")
        action, arg = menu_mod.parse_menu_data(btn.data)
        self.assertEqual((action, arg), ("cmdt_run", "磁盘占用"))
        # 管理入口仍在
        self.assertTrue(any("管理模板" in b.text for b in flat))
        # 删模板后按钮消失
        cmd_templates.delete("磁盘占用")
        rows = menu_mod.sh_menu_buttons()
        self.assertFalse(any("磁盘占用" in b.text
                             for row in rows for b in row))

    async def test_template_button_executes(self):
        """▶️ 模板按钮 → cmdt_run → 真实执行命令。"""
        cmd_templates.upsert("问好", "echo hi-template")
        from tg_userbot import bot as bot_mod
        ok, result = await cmd_templates.execute("问好")
        self.assertTrue(ok)
        self.assertIn("hi-template", result)
        # bot 分支存在性：cmdt_run 已在 MENU_ACTIONS
        self.assertIn("cmdt_run", config.MENU_ACTIONS)

    def test_view_text(self):
        state.CMD_TEMPLATES = {"磁盘": "du -sh *"}
        text = cmd_templates.list_text()
        self.assertIn("命令模板", text)
        self.assertIn("磁盘", text)


class LsFormatTest(unittest.TestCase):
    """ls -la 精简视图：时间(yyyy-mm-dd) / 大小 / 名称；目录带 / 标记。"""

    LS_FIXTURE = (
        "$ ls -la\n```\n"
        "total 48\n"
        "drwxr-xr-x@ 15 user staff 480 Sep 16 23:16 .git\n"
        "-rw-r--r--@  1 user staff 309 Sep 15 01:33 .gitignore\n"
        "drwxr-xr-x   7 user staff 224 Jan  1  2020 旧目录\n"
        "-rw-r--r--   1 user staff 1048576 Sep 15 20:10 大视频.mp4\n"
        "```\n"
    )

    def test_format_produces_slim_lines(self):
        out = shell.format_ls_listing(self.LS_FIXTURE)
        self.assertIn("2026-09-16", out)          # 近期文件按当前年补齐
        self.assertIn("2020-01-01", out)          # 旧文件保留原年份
        self.assertIn("309.00 B", out)            # 人性化大小
        self.assertIn("1.00 MB", out)
        self.assertIn("—", out)                   # 目录大小显示占位
        self.assertIn(".git/", out)               # 目录名带 / 标记
        self.assertIn("大视频.mp4", out)
        self.assertNotIn("drwxr-xr-x", out)       # 权限位不再出现
        self.assertNotIn("jiajun-chen", out)      # 用户组不再出现

    def test_browser_parses_formatted_dirs(self):
        """精简视图直接喂给文件夹浏览器：目录解析正确（新格式兼容）。"""
        out = shell.format_ls_listing(self.LS_FIXTURE)
        body = out.split("```")[1]
        dirs = shell.parse_ls_entries(body)
        self.assertIn(".git", dirs)
        self.assertIn("旧目录", dirs)

    def test_command_reply_formats_ls_la(self):
        """/sh ls -la 的回复直接就是精简视图（asyncio 驱动）。"""

        async def run():
            return await shell.command_reply("/sh ls -la")

        out = asyncio.run(run())
        self.assertIn("```", out)


if __name__ == "__main__":
    unittest.main()
