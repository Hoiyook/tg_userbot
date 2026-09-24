"""快捷指令 + /cmdhis 命令行历史 + 面板补缺按钮的单元测试（2026-09-24）。

1. 快捷指令（COMMAND_SHORTCUTS）：文本精确命中映射 → 当作命令执行；
   输入窗口等待期窗口优先（快捷指令不抢输入）。
2. /cmdhis：shell 命令历史 JSON 落盘（新→旧、去相邻重复、截 50 条），
   /sh 真执行才记录、菜单目录导航（record=False）不污染。
3. 面板补缺：🖥 SQL 控制台 / 🧾 解析账本 / 🗑 清程序消息 / 🔎 搜外链备注
   的动作注册与输入窗口互斥。
"""
import asyncio
import atexit
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_sc_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import bot, commands, config, shell, state  # noqa: E402
from tg_userbot import menu, sql_templates  # noqa: E402


# ============================================================
# 1) 快捷指令
# ============================================================
class ResolveShortcutTest(unittest.TestCase):
    """resolve_shortcut 纯函数守卫。"""

    def test_hit(self):
        self.assertEqual(commands.resolve_shortcut("1"), "/cmdhis")
        self.assertEqual(commands.resolve_shortcut(" 1 "), "/cmdhis")

    def test_slash_never_shortcut(self):
        """以 / 开头的一律走正常命令，不进映射。"""
        self.assertIsNone(commands.resolve_shortcut("/1"))
        self.assertIsNone(commands.resolve_shortcut("/cmdhis"))

    def test_miss_and_guards(self):
        self.assertIsNone(commands.resolve_shortcut("2"))
        self.assertIsNone(commands.resolve_shortcut(""))
        self.assertIsNone(commands.resolve_shortcut(None))
        self.assertIsNone(commands.resolve_shortcut("123456789"))  # >8 字符


class ShortcutRoutingBotChatTest(unittest.IsolatedAsyncioTestCase):
    """bot 对话里发「1」应执行 /cmdhis；输入窗口期内窗口优先。"""

    def setUp(self):
        self.saved = (state.MY_ID, state.FIND_INPUT_UNTIL,
                      state.SHELL_INPUT_UNTIL)
        state.MY_ID = 5452449426
        state.FIND_INPUT_UNTIL = 0.0
        state.SHELL_INPUT_UNTIL = 0.0

    def tearDown(self):
        (state.MY_ID, state.FIND_INPUT_UNTIL,
         state.SHELL_INPUT_UNTIL) = self.saved

    async def _message(self, text):
        ev = mock.MagicMock()
        ev.chat_id = state.MY_ID
        ev.out = False
        ev.message = mock.MagicMock()
        ev.message.message = text
        ev.message.fwd_from = None
        ev.delete = mock.AsyncMock()
        state.bot_client = mock.MagicMock()
        state.bot_client.send_message = mock.AsyncMock()
        with mock.patch.object(bot.commands, "handle_command",
                               mock.AsyncMock(return_value=True)) as hc, \
                mock.patch.object(bot.logger, "info"):
            await bot.bot_message_handler(ev)
        return hc

    async def test_shortcut_triggers_command(self):
        hc = await self._message("1")
        hc.assert_awaited_once()
        self.assertEqual(hc.await_args.args[1], "/cmdhis")

    async def test_plain_text_still_falls_to_menu(self):
        hc = await self._message("随便说句话")
        hc.assert_not_awaited()
        self.assertTrue(state.bot_client.send_message.await_count >= 1)

    async def test_input_window_beats_shortcut(self):
        """find 输入窗口开着时，「1」是查询关键字，不是快捷指令。"""
        state.FIND_INPUT_UNTIL = time.monotonic() + 60
        with mock.patch.object(bot, "_handle_find_input",
                               mock.AsyncMock()) as find_input, \
                mock.patch.object(bot.commands, "handle_command",
                                  mock.AsyncMock(return_value=True)) as hc, \
                mock.patch.object(bot.logger, "info"):
            await self._message("1")
        hc.assert_not_awaited()
        find_input.assert_awaited_once()


# ============================================================
# 2) /cmdhis 命令行历史
# ============================================================
class ShellHistoryTest(unittest.TestCase):
    """历史存储：新→旧、去相邻重复、截断、损坏容错。"""

    def setUp(self):
        self.path = os.path.join(_TMP, f"shellhist_{id(self)}.json")
        self.addCleanup(lambda: os.path.exists(self.path)
                        and os.remove(self.path))

    def test_record_order_and_cap(self):
        for i in range(60):
            shell.record_command_history(f"cmd{i}", path=self.path)
        items = shell.load_command_history(self.path)
        self.assertEqual(len(items), shell.SHELL_HISTORY_MAX)
        self.assertEqual(items[0], "cmd59")       # 最新在前
        self.assertEqual(items[-1], "cmd10")      # 最旧被截掉

    def test_adjacent_dedup(self):
        shell.record_command_history("ls -la", path=self.path)
        shell.record_command_history("ls -la", path=self.path)
        shell.record_command_history("df -h", path=self.path)
        shell.record_command_history("df -h", path=self.path)
        self.assertEqual(shell.load_command_history(self.path),
                         ["df -h", "ls -la"])

    def test_corrupt_file_tolerated(self):
        with open(self.path, "w") as f:
            f.write("{坏 json")
        self.assertEqual(shell.load_command_history(self.path), [])
        shell.record_command_history("ok", path=self.path)
        self.assertEqual(shell.load_command_history(self.path), ["ok"])

    def test_command_reply_records_but_navigation_skips(self):
        """真执行记录历史；record=False（目录导航）不记。"""
        async def run():
            with mock.patch.object(shell, "_reply_for",
                                   mock.AsyncMock(return_value="out")) as rf:
                await shell.command_reply("/sh echo hi", path_guard=None) \
                    if False else None
                with mock.patch.object(shell, "record_command_history") as rec:
                    await shell.command_reply("/sh echo a")
                rec.assert_called_once_with("echo a")
                with mock.patch.object(shell, "record_command_history") as rec2:
                    await shell.command_reply("/sh ls -la", record=False)
                rec2.assert_not_called()
        asyncio.new_event_loop().run_until_complete(run())


class CmdhisCommandTest(unittest.TestCase):
    """/cmdhis 输出：代码块化、新→旧、空历史提示。"""

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_empty_history(self):
        ev = mock.MagicMock()
        ev.reply = mock.AsyncMock()
        with mock.patch.object(shell, "load_command_history",
                               return_value=[]):
            self._run(commands.handle_command(ev, "/cmdhis"))
        self.assertIn("还没有执行过", ev.reply.await_args.args[0])

    def test_lists_commands_newest_first(self):
        ev = mock.MagicMock()
        ev.reply = mock.AsyncMock()
        with mock.patch.object(shell, "load_command_history",
                               return_value=["df -h", "ls -la"]):
            self._run(commands.handle_command(ev, "/cmdhis"))
        text = ev.reply.await_args.args[0]
        self.assertIn("2/2 条", text)
        self.assertLess(text.index("df -h"), text.index("ls -la"))
        # with_code_block 生效：首行留外、正文进围栏（方便整块复制）
        self.assertIn("```", text)

    def test_registered_and_panel_listed(self):
        self.assertIn("cmdhis", config.REGISTERED_COMMAND_NAMES)
        names = [n for n, _ in bot.BOT_COMMANDS]
        self.assertIn("cmdhis", names)


# ============================================================
# 3) 面板补缺按钮
# ============================================================
class PanelGapActionsTest(unittest.TestCase):
    """新动作全部注册 + 按钮确实挂在对应面板上。"""

    def test_actions_registered(self):
        for action in ("sql_console", "mlink_search", "origin", "clearmsg"):
            self.assertIn(action, config.MENU_ACTIONS, action)

    def test_sqlt_panel_has_console_button(self):
        row = sql_templates.menu_buttons()[0]
        texts = [b.text for b in row]
        self.assertIn("🖥 SQL控制台", texts)

    def test_tools_panel_has_origin_and_clearmsg(self):
        texts = [b.text for row in menu.tools_menu_buttons([])
                 for b in row]
        self.assertIn("🧾 解析账本", texts)
        self.assertIn("🗑 清程序消息", texts)

    def test_new_input_windows_open_and_mutex(self):
        saved = (state.SQL_CONSOLE_INPUT_UNTIL, state.SQLT_INPUT_UNTIL,
                 state.MLINK_SEARCH_INPUT_UNTIL)
        state.SQL_CONSOLE_INPUT_UNTIL = 9e9
        state.SQLT_INPUT_UNTIL = 9e9
        try:
            bot.open_input_window("mlink_search")
            self.assertEqual(state.SQL_CONSOLE_INPUT_UNTIL, 0.0,
                             "开新窗口必须关掉旧的（互斥）")
            self.assertEqual(state.SQLT_INPUT_UNTIL, 0.0)
            self.assertGreater(state.MLINK_SEARCH_INPUT_UNTIL, 0)
        finally:
            (state.SQL_CONSOLE_INPUT_UNTIL, state.SQLT_INPUT_UNTIL,
             state.MLINK_SEARCH_INPUT_UNTIL) = saved

    def test_clear_input_states_covers_new_windows(self):
        state.SQL_CONSOLE_INPUT_UNTIL = 9e9
        state.MLINK_SEARCH_INPUT_UNTIL = 9e9
        bot._clear_input_states()
        self.assertEqual(state.SQL_CONSOLE_INPUT_UNTIL, 0.0)
        self.assertEqual(state.MLINK_SEARCH_INPUT_UNTIL, 0.0)


class NewActionsFlowTest(unittest.IsolatedAsyncioTestCase):
    """新动作的行为：SQL 控制台执行输入、origin 出文本、clearmsg 走命令。"""

    async def test_sql_console_executes_input(self):
        saved_me, saved_bc = state.MY_ID, state.bot_client
        state.MY_ID = 5452449426
        state.bot_client = mock.MagicMock()
        state.bot_client.send_message = mock.AsyncMock()
        ev = mock.MagicMock()
        try:
            with mock.patch.object(
                    bot.runtime_db, "execute_user_sql",
                    return_value={"kind": "done", "rowcount": 1}):
                await bot._handle_sql_console_input(
                    ev, "UPDATE x SET y=1")
            text = state.bot_client.send_message.await_args.args[1]
            self.assertIn("已执行", text)
        finally:
            state.MY_ID, state.bot_client = saved_me, saved_bc

    async def test_mlink_search_executes_input(self):
        saved_me, saved_bc = state.MY_ID, state.bot_client
        state.MY_ID = 5452449426
        state.bot_client = mock.MagicMock()
        state.bot_client.send_message = mock.AsyncMock()
        ev = mock.MagicMock()
        try:
            with mock.patch.object(
                    bot.manual_links, "links_view",
                    return_value=("📋 结果", [])):
                await bot._handle_mlink_search_input(ev, "mega")
            call = state.bot_client.send_message.await_args
            self.assertEqual(call.args[1], "📋 结果")   # 视图正文
            # handler 追加了「返回主菜单」行，按钮应有且仅一行
            self.assertEqual(len(call.kwargs.get("buttons")), 1)
        finally:
            state.MY_ID, state.bot_client = saved_me, saved_bc

    async def test_origin_action_returns_text(self):
        from tg_userbot import sources
        reply, buttons = await bot.handle_menu_action(
            "origin", None, mock.MagicMock())
        self.assertIsInstance(reply, str)
        self.assertTrue(buttons)

    async def test_clearmsg_action_dispatches_command(self):
        with mock.patch.object(bot.commands, "handle_command",
                               mock.AsyncMock(return_value=True)) as hc:
            reply, _ = await bot.handle_menu_action(
                "clearmsg", None, mock.MagicMock())
            await asyncio.sleep(0.05)   # create_task 需要让出一步
        self.assertIn("已开始", reply)
        hc.assert_awaited_once()
        self.assertEqual(hc.await_args.args[1], "/clearmsg")


if __name__ == "__main__":
    unittest.main()
