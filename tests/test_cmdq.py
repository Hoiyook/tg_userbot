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
    def __init__(self, mid, text, date=None):
        self.id = mid
        self.message = text
        self.date = date


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
        # 拦截通知（2026-11-27）必须可测且不触网
        from tg_userbot import notify as _notify
        self._ntf = mock.patch.object(
            _notify, "notify_user", new=mock.AsyncMock())
        self._notify_mock = self._ntf.start()
        self.addCleanup(self._ntf.stop)

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
        """回放执行幂等安全命令（查询类）；写操作命令转 CONFIRM 待人工
        （2026-11-22 事故：历史 /pd /retry 被回放重放——pixeldrain 重下、
        wnacg 重投）。"""
        msgs = [_Msg(10, "/stats"), _Msg(11, "闲聊"),
                _Msg(12, "/paw_plan 作者"), _Msg(13, "/wnacg 123 作者")]
        cli = _Cli(msgs)
        state.client = cli
        state.bot_client = None
        state.MY_ID = None
        self.addCleanup(setattr, state, "client", None)

        async def run():
            return await cmdq.replay_chat("me")
        res = asyncio.run(run())
        self.assertEqual(res["found"], 3)
        self.assertEqual(res["executed"], 1, "只有幂等安全的 /stats 自动执行")
        self.assertEqual(self.executed, ["/stats"])
        # 写操作命令转 CONFIRM（待人工 /cmdq confirm）
        conf = [r for r in runtime_db.list_cmd_ledger(
                    status=runtime_db.CMD_CONFIRM, limit=10)]
        self.assertEqual(len(conf), 2, "写操作命令应转 CONFIRM")
        # 水位线推进到最后一条（含非指令）
        self.assertEqual(runtime_db.get_cmd_cursor(_chat_key("me")), 13)
        # 再跑一遍：水位线后无新消息，CONFIRM 不再重放
        res2 = asyncio.run(run())
        self.assertEqual(res2["found"], 0)
        self.assertEqual(len(self.executed), 1, "恰好一次，不重复")

    def test_replay_stale_skipped(self):
        runtime_db.record_cmd("me", 5, "/旧指令", now=time.time() - 8 * 86400)
        row = runtime_db.get_cmd_status("me", 5)
        st = asyncio.run(cmdq.execute_ledger_row(row))
        self.assertEqual(st, runtime_db.CMD_STALE)
        self.assertEqual(runtime_db.get_cmd_status("me", 5)["status"],
                         runtime_db.CMD_STALE)
        self.assertEqual(self.executed, [], "过期指令不执行")

    def test_fresh_write_command_executes_via_replay(self):
        """重启/断连间隙漏掉的现发指令（≤15 分钟）：写操作补执行不拦
        （2026-11-27 owner 实测：09:25 发 /paw_plan，09:26 回放静默拦成
        ⏸——"没跑也找不到"）；更老的写指令仍拦，但必须发 TG 通知。"""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        msgs = [_Msg(20, "/paw_plan 作者",
                     date=now - timedelta(seconds=120)),
                _Msg(21, "/pd https://pixeldrain.com/l/陈年旧链",
                     date=now - timedelta(days=2))]
        cli = _Cli(msgs)
        state.client = cli
        state.bot_client = None
        state.MY_ID = None
        self.addCleanup(setattr, state, "client", None)

        async def run():
            return await cmdq.replay_chat("me")
        res = asyncio.run(run())
        self.assertEqual(res["executed"], 1, "刚发的指令直接补执行")
        self.assertEqual(res["parked"], 1, "两天前的写指令仍然拦")
        self.assertEqual(self.executed, ["/paw_plan 作者"])
        # 拦截不再是静默 ⏸：一条 TG 通知列出被拦命令与放行方式
        self.assertEqual(self._notify_mock.call_count, 1)
        notified = self._notify_mock.call_args[0][0]
        self.assertIn("/cmdq confirm", notified)
        self.assertIn("/pd", notified)

    def test_confirm_cmd_actually_executes(self):
        """/cmdq confirm 真正放行（2026-11-27 修复：confirm 再执行时再次
        撞上写操作拦截，状态原地打回 CONFIRM——闸门只进不出，人工确认
        永远放不了行，今早 4 条 /paw_plan 因此一直卡死）。"""
        runtime_db.record_cmd("me", 30, "/paw_plan 作者")
        row = runtime_db.get_cmd_status("me", 30)
        st = asyncio.run(cmdq.execute_ledger_row(row))
        self.assertEqual(st, runtime_db.CMD_CONFIRM, "回放先拦下来")
        self.assertEqual(self.executed, [], "未确认前不执行")
        parked = runtime_db.list_cmd_ledger(
            status=runtime_db.CMD_CONFIRM, limit=10)
        self.assertEqual(len(parked), 1)
        tid = parked[0]["id"]
        reply = asyncio.run(cmdq.confirm_cmd(str(tid)))
        self.assertIn("✅", reply)
        self.assertEqual(self.executed, ["/paw_plan 作者"],
                         "确认后必须真执行（而非原地再拦一次）")
        self.assertEqual(
            runtime_db.get_cmd_status("me", 30)["status"],
            runtime_db.CMD_SUCCESS)

    def test_msg_age_unknown_parks_safely(self):
        """无日期消息按陈旧处理（安全默认）；带年龄且新鲜 → 执行。"""
        runtime_db.record_cmd("me", 40, "/wnacg 123 作者")
        row = runtime_db.get_cmd_status("me", 40)
        st = asyncio.run(cmdq.execute_ledger_row(row, msg_age=None))
        self.assertEqual(st, runtime_db.CMD_CONFIRM, "未知年龄 = 拦截")
        runtime_db.record_cmd("me", 41, "/wnacg 124 作者")
        row = runtime_db.get_cmd_status("me", 41)
        st = asyncio.run(cmdq.execute_ledger_row(row, msg_age=30))
        self.assertEqual(st, runtime_db.CMD_SUCCESS, "30 秒前 = 现发指令")
        self.assertEqual(runtime_db.get_cmd_status("me", 40)["status"],
                         runtime_db.CMD_CONFIRM)


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
