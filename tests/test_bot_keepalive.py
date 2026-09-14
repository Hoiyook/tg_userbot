"""bot 菜单连接守护（app._bot_keepalive）的存活语义测试。

根因（2026-09-14 实测）：代理抖动期间 start_with_retry 内部的 telethon
pending future 被网络层 cancel()，CancelledError 穿透 keepalive 的
`except Exception`（真取消显式放行），守护任务静默死亡——bot 账号此后
再无人重连，面板永远 🔴。

契约：CancelledError 到达 keepalive 时按 `state.STOP_EVENT` 结构性区分——
未停服 = telethon 网络层取消 → 吞掉并继续下轮探活；停服（main 真取消）→
原样上抛。存活以「start_with_retry 被调用的次数」判定：被一次取消打死
= 恒为 1；存活 = 继续调用。

    .venv/bin/python -m unittest tests.test_bot_keepalive -v
"""
import asyncio
import os
import shutil
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录（config import 期
# 有真实副作用，见 test_config 的基座说明）。
_TMP = tempfile.mkdtemp(prefix="tg_userbot_keepalive_test_")
import atexit  # noqa: E402
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import app  # noqa: E402
from tg_userbot import state  # noqa: E402


class BotKeepaliveAliveTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old = (state.bot_client, state.STOP_EVENT)
        bot = mock.MagicMock()
        bot.is_connected.return_value = False   # 每轮探活都触发重连
        state.bot_client = bot
        state.STOP_EVENT = asyncio.Event()
        self.addCleanup(self._restore)

    def _restore(self):
        state.bot_client, state.STOP_EVENT = self._old

    async def _run(self, stop=False, probe_count=3):
        """跑 probe_count 轮探活后外部取消，返回 (结局, 调用次数)。

        外部 task.cancel() 只在探活次数达标后发出——先于达标的
        CancelledError 结局只能是「守护自己死了」。"""
        calls = {"n": 0}

        async def fake_start(client, bot_token=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise asyncio.CancelledError()   # 网络层取消（首轮）
            if stop and calls["n"] == 2:
                # 真实停服序列：SIGTERM 先置 STOP_EVENT，main 再取消任务
                state.STOP_EVENT.set()
                raise asyncio.CancelledError()
            await asyncio.sleep(0)               # 成功重连

        with mock.patch.object(app, "BOT_KEEPALIVE_INTERVAL", 0.005), \
                mock.patch.object(app, "start_with_retry",
                                  side_effect=fake_start):
            task = asyncio.create_task(app._bot_keepalive())
            for _ in range(probe_count * 20):
                await asyncio.sleep(0.01)
                if calls["n"] >= probe_count:
                    break
            else:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                return "died_early", calls["n"]
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                return "cancelled", calls["n"]
            return "finished", calls["n"]

    async def test_network_cancel_does_not_kill_keepalive(self):
        outcome, calls = await self._run(stop=False, probe_count=3)
        self.assertGreaterEqual(calls, 3,
                                "一次网络层取消就把守护打死了")
        self.assertIn(outcome, ("cancelled", "finished"))

    async def test_regular_failure_does_not_kill_keepalive(self):
        """普通异常（登录失败等）照旧吞掉继续——既有语义。"""
        calls = {"n": 0}

        async def fake_start(client, bot_token=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("代理挂了")
            await asyncio.sleep(0)

        with mock.patch.object(app, "BOT_KEEPALIVE_INTERVAL", 0.005), \
                mock.patch.object(app, "start_with_retry",
                                  side_effect=fake_start):
            task = asyncio.create_task(app._bot_keepalive())
            for _ in range(60):
                await asyncio.sleep(0.01)
                if calls["n"] >= 2:
                    break
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.assertGreaterEqual(calls["n"], 2)

    async def test_stop_event_set_reraises(self):
        """停服（main 真取消，STOP_EVENT 已置位）→ CancelledError 上抛，
        不再继续探活（调用次数停在 2：首轮网络取消 + 停服取消）。"""
        outcome, calls = await self._run(stop=True, probe_count=2)
        self.assertEqual(outcome, "cancelled")
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
