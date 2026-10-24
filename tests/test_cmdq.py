"""cmdq 指令回放测试：恰好一次 / 水位线推进 / 过期跳过 / 回放执行 /
在线入账不重复 / 台账查询。跑法：
    .venv/bin/python -m unittest tests.test_cmdq -v
"""
import asyncio
import atexit
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest as _btn_shim  # noqa: F401,E402

_TMP = tempfile.mkdtemp(prefix="tg_userbot_cmdq_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ.setdefault("TG_SAVE_FOLDER", _TMP)

from tg_userbot import cmdq, config, runtime_db, state  # noqa: E402


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cmdq_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())


class LedgerDbTest(_DbCase):
    def test_record_once_and_finish(self):
        self.assertTrue(runtime_db.record_cmd(1, 100, "/paw"))
        self.assertFalse(runtime_db.record_cmd(1, 100, "/paw"),
                         "UNIQUE(chat,msg) 恰好一次")
        row = runtime_db.get_cmd_status(1, 100)
        self.assertEqual(row["status"], runtime_db.CMD_PENDING)
        runtime_db.finish_cmd(1, 100, runtime_db.CMD_SUCCESS)
        self.assertEqual(runtime_db.get_cmd_status(1, 100)["status"],
                         runtime_db.CMD_SUCCESS)

    def test_cursor_only_advances(self):
        runtime_db.advance_cmd_cursor(1, 50)
        runtime_db.advance_cmd_cursor(1, 30)     # 旧消息不回退
        self.assertEqual(runtime_db.get_cmd_cursor(1), 50)

    def test_counts_and_list(self):
        runtime_db.record_cmd(1, 1, "/a")
        runtime_db.record_cmd(1, 2, "/b")
        runtime_db.finish_cmd(1, 1, runtime_db.CMD_SUCCESS)
        self.assertEqual(runtime_db.cmd_ledger_counts(),
                         {"SUCCESS": 1, "PENDING": 1})
        self.assertEqual(len(runtime_db.list_cmd_ledger(
            status=runtime_db.CMD_PENDING)), 1)


class CommandTextTest(unittest.TestCase):
    def test_is_command_text(self):
        self.assertTrue(cmdq.is_command_text("/paw_plan x"))
        self.assertTrue(cmdq.is_command_text("/wnacg https://…"))
        self.assertTrue(cmdq.is_command_text("/115x /115open/云下载"))
        self.assertFalse(cmdq.is_command_text("普通文本"))
        self.assertFalse(cmdq.is_command_text("/不存在指令 abc"))


class _Msg:
    def __init__(self, mid, text):
        self.id = mid
        self.message = text


class _Cli:
    def __init__(self, msgs):
        self._msgs = msgs
        self.get_calls = []

    async def get_messages(self, chat, **kw):
        self.get_calls.append((chat, kw))
        return [m for m in self._msgs
                if m.id > (kw.get("min_id") or 0)][:kw.get("limit", 100)]


class ReplayFlowTest(_DbCase):
    """回放全流程：水位线后的指令入账并执行，非指令只推水位线。"""

    def setUp(self):
        super().setUp()
        self.executed = []
        self._mark = mock.patch.object(cmdq, "_mark", new=mock.AsyncMock())
        self._mark.start()
        self.addCleanup(self._mark.stop)

        async def fake_handle(event, cmd_text):
            self.executed.append(cmd_text)
            return True

        async def _setup_and_patch():
            from tg_userbot import commands as _c   # loop 存活期导入（Lock 需 loop）
            self._h = mock.patch.object(_c, "handle_command",
                                        side_effect=fake_handle)
            self._h.start()
        asyncio.run(_setup_and_patch())
        self.addCleanup(lambda: self._h.stop())

    def test_replay_executes_pending_commands(self):
        msgs = [_Msg(10, "/paw_plan 作者"), _Msg(11, "闲聊"),
                _Msg(12, "/wnacg 123 作者")]
        cli = _Cli(msgs)
        state.client = cli
        state.bot_client = None
        state.MY_ID = None
        self.addCleanup(setattr, state, "client", None)

        async def run():
            return await cmdq.replay_chat("me")
        res = asyncio.run(run())
        self.assertEqual(res["found"], 2)
        self.assertEqual(res["executed"], 2)
        self.assertEqual(sorted(self.executed),
                         ["/paw_plan 作者", "/wnacg 123 作者"])
        # 水位线推进到最后一条（含非指令）
        self.assertEqual(runtime_db.get_cmd_cursor(_chat_key("me")), 12)
        # 再跑一遍：水位线后无新消息，不重复执行
        res2 = asyncio.run(run())
        self.assertEqual(res2["found"], 0)
        self.assertEqual(len(self.executed), 2, "恰好一次，不重复")

    def test_replay_stale_skipped(self):
        runtime_db.record_cmd("me", 5, "/旧指令", now=time.time() - 8 * 86400)
        row = runtime_db.get_cmd_status("me", 5)
        st = asyncio.run(cmdq.execute_ledger_row(row))
        self.assertEqual(st, runtime_db.CMD_STALE)
        self.assertEqual(runtime_db.get_cmd_status("me", 5)["status"],
                         runtime_db.CMD_STALE)
        self.assertEqual(self.executed, [], "过期指令不执行")


def _chat_key(chat):
    # cursor 存库时 chat 键：replay_chat 用原样 chat（"me" 或 int）
    return chat if not isinstance(chat, int) else int(chat)


class StatusReplyTest(_DbCase):
    def test_reply_shapes(self):
        runtime_db.record_cmd(1, 9, "/ok")
        runtime_db.finish_cmd(1, 9, runtime_db.CMD_SUCCESS)
        runtime_db.record_cmd(1, 10, "/bad")
        runtime_db.finish_cmd(1, 10, runtime_db.CMD_FAILED, error="HTTP 500")
        text = cmdq.status_reply("")
        self.assertIn("✅ 成功1", text)
        self.assertIn("❌ 失败1", text)
        self.assertIn("/bad", text)
        only = cmdq.status_reply("失败")
        self.assertIn("/bad", only)
        self.assertNotIn("/ok", only)
        self.assertIn("用法", cmdq.status_reply("不明参数"))


class LiveRecordTest(_DbCase):
    """在线入账：record_live 落账 RUNNING；handle_command wrapper 落终态。"""

    def test_record_live_idempotent(self):
        ev = cmdq.ReplayEvent(77, 300, "/paw")

        async def run():
            await cmdq.record_live(ev, "/paw")
            await cmdq.record_live(ev, "/paw")   # 幂等
        asyncio.run(run())
        self.assertEqual(runtime_db.get_cmd_status(77, 300)["status"],
                         runtime_db.CMD_RUNNING)

    def test_recover_stale_running(self):
        runtime_db.record_cmd(1, 1, "/x")
        runtime_db.finish_cmd(1, 1, runtime_db.CMD_RUNNING)
        self.assertEqual(cmdq.recover_stale_running(), 1)
        self.assertEqual(runtime_db.get_cmd_status(1, 1)["status"],
                         runtime_db.CMD_PENDING)


if __name__ == "__main__":
    unittest.main()
