"""通知出口 notify.notify_user 的单元测试（2026-09-11）。

路由决策：程序主动通知一律发到 **bot 控制面板对话**，收藏夹只留媒体。
之所以由 **bot 账号**发而不是主账号发到同一对话——bot 自己的出站消息不会
回流成更新，因此不会被 `bot_message_handler` 当成 owner 指令、每发一条通知
就回一次主菜单（2026-09-11 实测刷屏的真实成因）。

不联网：全部是假的 telegram 客户端。
"""
import asyncio
import atexit
import os
import shutil
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_notify_test_")
# 退出时回收临时目录（测试跑完就地删，别让 /var/folders 越堆越多）
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from telethon.errors import RPCError  # noqa: E402

from tg_userbot import notify, state  # noqa: E402


class _FakeTG:
    """假 telegram 客户端：记录发往哪里。"""

    def __init__(self, connected=True):
        self._connected = connected
        self.sent = []
        self.error = None

    def is_connected(self):
        return self._connected

    async def send_message(self, target, text, link_preview=False):
        if self.error:
            raise self.error
        self.sent.append((target, text))
        return mock.MagicMock(id=1)


class NotifyUserTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._saved = (state.client, state.bot_client, state.MY_ID)
        self.main = _FakeTG()
        self.bot = _FakeTG()
        state.client = self.main
        state.bot_client = self.bot
        state.MY_ID = 42

    async def asyncTearDown(self):
        state.client, state.bot_client, state.MY_ID = self._saved

    async def test_goes_to_control_panel_via_bot_account(self):
        """有 bot 账号时：由 bot 发到 owner（控制面板对话），主账号不发。"""
        self.assertTrue(await notify.notify_user("✅ 下载完成"))
        self.assertEqual(self.bot.sent, [(42, "✅ 下载完成")])
        self.assertEqual(self.main.sent, [], "通知不该再落到收藏夹")

    async def test_falls_back_to_saved_messages_without_bot(self):
        state.bot_client = None
        self.assertTrue(await notify.notify_user("⏭️ 重复媒体已跳过"))
        self.assertEqual(self.main.sent, [("me", "⏭️ 重复媒体已跳过")])

    async def test_falls_back_when_bot_disconnected(self):
        self.bot._connected = False
        self.assertTrue(await notify.notify_user("❌ 下载失败"))
        self.assertEqual(self.main.sent, [("me", "❌ 下载失败")])

    async def test_falls_back_when_bot_send_fails(self):
        self.bot.error = RPCError(request=None, message="boom")
        self.assertTrue(await notify.notify_user("📥 开始下载"))
        self.assertEqual(self.main.sent, [("me", "📥 开始下载")])

    async def test_network_cancel_never_escapes(self):
        """网络层取消不得冒给调用方（否则会把下载任务误判成被真取消）。"""
        self.bot.error = asyncio.CancelledError()
        self.main.error = asyncio.CancelledError()
        self.assertFalse(await notify.notify_user("x"))    # 不抛

    async def test_total_failure_returns_false(self):
        self.bot.error = RPCError(request=None, message="boom")
        self.main.error = RPCError(request=None, message="boom")
        self.assertFalse(await notify.notify_user("x"))

    async def test_no_client_at_all_returns_false(self):
        state.bot_client = None
        state.client = None
        self.assertFalse(await notify.notify_user("x"))


if __name__ == "__main__":
    unittest.main()
