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
from tg_userbot import msg_history as msg_history_mod  # noqa: E402
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
# 2) /cmdhis 消息历史（2026-09-25 需求重定义：记录发给 bot 的消息，
#    不是 sh 历史命令——sh 历史机制已整体撤除）
# ============================================================
class MsgHistoryStoreTest(unittest.TestCase):
    """msg_history 存储：新→旧、去相邻重复、截断、损坏容错、多行保留。"""

    def setUp(self):
        self.path = os.path.join(_TMP, f"msghist_{id(self)}.json")
        self.addCleanup(lambda: os.path.exists(self.path)
                        and os.remove(self.path))

    def test_record_order_and_cap(self):
        from tg_userbot import msg_history
        for i in range(60):
            msg_history.record_message(f"消息{i}", path=self.path)
        items = msg_history.load_messages(self.path)
        self.assertEqual(len(items), msg_history.MAX_MESSAGES)
        self.assertEqual(items[0], "消息59")
        self.assertEqual(items[-1], "消息10")

    def test_adjacent_dedup_and_multiline(self):
        from tg_userbot import msg_history
        msg_history.record_message("/paw plan MofuMochii", path=self.path)
        msg_history.record_message("/paw plan MofuMochii", path=self.path)
        msg_history.record_message("第一行\n第二行", path=self.path)
        items = msg_history.load_messages(self.path)
        self.assertEqual(items, ["第一行\n第二行", "/paw plan MofuMochii"])

    def test_blank_ignored_and_corrupt_tolerated(self):
        from tg_userbot import msg_history
        msg_history.record_message("   ", path=self.path)
        self.assertEqual(msg_history.load_messages(self.path), [])
        with open(self.path, "w") as f:
            f.write("{坏 json")
        self.assertEqual(msg_history.load_messages(self.path), [])
        msg_history.record_message("ok", path=self.path)
        self.assertEqual(msg_history.load_messages(self.path), ["ok"])


class CmdhisCommandTest(unittest.TestCase):
    """/cmdhis：读消息历史渲染；代码块化、新→旧、空态；N 参数。"""

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_empty(self):
        ev = mock.MagicMock()
        ev.reply = mock.AsyncMock()
        with mock.patch.object(msg_history_mod, "load_messages",
                               return_value=[]):
            self._run(commands.handle_command(ev, "/cmdhis"))
        self.assertIn("还没有发给 bot", ev.reply.await_args.args[0])

    def test_lists_messages_newest_first_in_code_block(self):
        ev = mock.MagicMock()
        ev.reply = mock.AsyncMock()
        with mock.patch.object(msg_history_mod, "load_messages",
                               return_value=["/paw plan MofuMochii", "你好"]):
            self._run(commands.handle_command(ev, "/cmdhis"))
        text = ev.reply.await_args.args[0]
        self.assertIn("2/2 条", text)
        self.assertLess(text.index("/paw plan MofuMochii"), text.index("你好"))
        self.assertIn("```", text)   # with_code_block：整块可复制

    def test_limit_param(self):
        ev = mock.MagicMock()
        ev.reply = mock.AsyncMock()
        msgs = [f"m{i}" for i in range(20)]
        with mock.patch.object(msg_history_mod, "load_messages",
                               return_value=msgs):
            self._run(commands.handle_command(ev, "/cmdhis 5"))
        self.assertIn("5/20 条", ev.reply.await_args.args[0])

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


class BotHandlerRecordsHistoryTest(unittest.IsolatedAsyncioTestCase):
    """bot_message_handler 接线：普通/命令/链接消息入史；输入窗口的
    敏感文本与程序面板**绝不入史**。"""

    def setUp(self):
        self.path = os.path.join(_TMP, f"msghook_{id(self)}.json")
        self.addCleanup(lambda: os.path.exists(self.path)
                        and os.remove(self.path))
        self._saved = (state.MY_ID, state.FIND_INPUT_UNTIL)
        state.MY_ID = 5452449426
        state.FIND_INPUT_UNTIL = 0.0

    def tearDown(self):
        state.MY_ID, state.FIND_INPUT_UNTIL = self._saved

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
                               mock.AsyncMock(return_value=True)), \
                mock.patch.object(bot.msg_history, "_history_path",
                                  return_value=self.path), \
                mock.patch.object(bot.logger, "info"):
            await bot.bot_message_handler(ev)

    async def test_command_and_text_recorded(self):
        await self._message("/paw plan MofuMochii")
        await self._message("随便一句话")
        items = msg_history_mod.load_messages(self.path)
        self.assertEqual(items[0], "随便一句话")          # 新→旧
        self.assertIn("/paw plan MofuMochii", items)

    async def test_input_window_text_not_recorded(self):
        """find 输入窗口的文本被窗口消费，不能落进历史。"""
        state.FIND_INPUT_UNTIL = 9e9
        with mock.patch.object(bot, "_handle_find_input",
                               mock.AsyncMock()):
            await self._message("这是查询关键词不该入史")
        self.assertEqual(msg_history_mod.load_messages(self.path), [])

    async def test_panel_prefix_not_recorded(self):
        from tg_userbot.config import REPORT_STATUS_PREFIX
        await self._message(REPORT_STATUS_PREFIX + " 面板内容")
        self.assertEqual(msg_history_mod.load_messages(self.path), [])
