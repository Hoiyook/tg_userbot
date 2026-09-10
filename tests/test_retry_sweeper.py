"""retry 榜自动重放的后台扫描任务（app._retry_sweeper）接线测试。

钉死两点：① 它确实每跳都调 queue.replay_due（防「函数写了但没人用」）；
② 单次扫描抛错不会把循环打死（否则一次异常就永久失去自动恢复能力）。
退避/上限/空闲 worker 预算的语义在 test_queue.py::AutoReplayDueTest 覆盖。
"""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_sweeper_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import app  # noqa: E402

_TICK = 0.01


class RetrySweeperTest(unittest.IsolatedAsyncioTestCase):
    async def _run_sweeper(self, replay_due, ticks=0.05):
        """跑一小段扫描循环后取消，返回 (调用次数, 是否以取消收尾)。"""
        calls = []

        def wrapper():
            calls.append(1)
            return replay_due()

        with mock.patch.object(app.queue, "replay_due", wrapper), \
                mock.patch.object(app, "AUTO_RETRY_SWEEP_SECONDS", _TICK):
            task = asyncio.create_task(app._retry_sweeper())
            await asyncio.sleep(ticks)
            cancelled = task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        return len(calls), cancelled

    async def test_each_tick_calls_replay_due(self):
        n, _ = await self._run_sweeper(lambda: 0)
        self.assertGreaterEqual(n, 2, "扫描循环没有按间隔调用 replay_due")

    async def test_scan_error_does_not_kill_loop(self):
        """replay_due 抛错 → 记日志继续下一跳，而不是任务结束。"""
        n, _ = await self._run_sweeper(
            lambda: (_ for _ in ()).throw(ValueError("boom")))
        self.assertGreaterEqual(
            n, 2, "一次扫描异常就把自动重放循环打死了")

    async def test_cancel_propagates(self):
        """停止信号（真取消）必须原样上抛，不被 except Exception 吞掉。"""
        _, cancelled = await self._run_sweeper(lambda: 0)
        self.assertTrue(cancelled)


if __name__ == "__main__":
    unittest.main()
