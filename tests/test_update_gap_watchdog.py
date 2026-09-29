"""app._heal_update_gap 更新缺口自愈测试 + 启动补拉接线检查。

背景（2026-09-29）：owner 在进程停机/登录重试窗口发的消息（.torrent 直链）
静默丢失——_main_serve 的 catch_up 只覆盖运行中断线重连，覆盖不到启动；且
「连接活着但更新流卡死」无人检测。修复：启动补拉 + 保活 ping 顺手对比服务端/
本地 pts，落后两轮 catch_up、四轮断开重连。

    .venv/bin/python -m unittest tests.test_update_gap_watchdog -v
"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录（config import 期
# 有真实副作用，见 test_queue_wiring 的基座说明）。
_TMP = tempfile.mkdtemp(prefix="tg_userbot_update_gap_test_")
import atexit  # noqa: E402
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import app  # noqa: E402
from tg_userbot import state  # noqa: E402


class _ServerState:
    """GetStateRequest 的返回替身（只用到 pts）。"""

    def __init__(self, pts):
        self.pts = pts


class _FakeMessageBox:
    def __init__(self, pts):
        self._pts = pts

    @property
    def session_state(self):
        return {"pts": self._pts}, {}


class _FakeClient:
    def __init__(self, local_pts):
        self._message_box = _FakeMessageBox(local_pts)
        self.catch_up = mock.AsyncMock()
        self.disconnect = mock.AsyncMock()


class TestHealUpdateGap(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = _FakeClient(local_pts=100)
        patcher = mock.patch.object(state, "client", self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_no_gap_returns_zero(self):
        # 服务端不领先 → 清零，无任何动作
        rounds = await app._heal_update_gap(_ServerState(100), 3)
        self.assertEqual(rounds, 0)
        self.client.catch_up.assert_not_awaited()
        self.client.disconnect.assert_not_awaited()

    async def test_first_behind_round_only_counts(self):
        rounds = await app._heal_update_gap(_ServerState(120), 0)
        self.assertEqual(rounds, 1)
        self.client.catch_up.assert_not_awaited()

    async def test_second_round_triggers_catch_up(self):
        rounds = await app._heal_update_gap(_ServerState(120), 1)
        self.assertEqual(rounds, 2)
        self.client.catch_up.assert_awaited_once()
        self.client.disconnect.assert_not_awaited()

    async def test_four_rounds_disconnects_for_fresh_loop(self):
        # 两次 catch_up（第 2、3 轮）无效 → 第 4 轮断开重连、计数清零
        rounds = 1
        for _ in range(3):
            rounds = await app._heal_update_gap(_ServerState(120), rounds)
        self.assertEqual(rounds, 0)
        self.assertEqual(self.client.catch_up.await_count, 2)
        self.client.disconnect.assert_awaited_once()

    async def test_unreadable_state_is_ignored(self):
        # 读不到本地状态（结构变化等）不猜、不动作、清零
        self.client._message_box = object()
        rounds = await app._heal_update_gap(_ServerState(120), 2)
        self.assertEqual(rounds, 0)
        self.client.catch_up.assert_not_awaited()


class TestStartupCatchUpWiring(unittest.TestCase):
    """启动补拉必须存在于 main() 登录之后（停机窗口消息不静默丢失）。"""

    def test_main_calls_catch_up_after_login(self):
        import inspect
        src = inspect.getsource(app.main)
        self.assertIn("catch_up", src)
        # 补拉点必须在 start_with_retry(state.client)（登录）之后
        self.assertLess(src.index("start_with_retry(state.client)"),
                        src.rindex("catch_up"))


if __name__ == "__main__":
    unittest.main()
