"""SerialTaskWorker 框架的单元测试——重点：取消时先 release 再上抛
（E43485 bug 类的唯一防线，8 处复制样板的替代者）。"""
import asyncio
import unittest
from unittest import mock

from tg_userbot import task_worker


class _FakeWorker(task_worker.SerialTaskWorker):
    def __init__(self):
        super().__init__()
        self.claims = []           # 依次弹出的任务
        self.processed = []
        self.released = []
        self.recovered = 0
        self.extras_started = 0
        self.pause_flag = False
        self.gate = None      # 用例显式设为 Event() 才阻塞处理

    def claim(self):
        return self.claims.pop(0) if self.claims else None

    async def process(self, task):
        self.processed.append(task)
        if self.gate is not None:
            await self.gate.wait()     # 卡在处理中（模拟长任务）

    def release(self, task):
        self.released.append(task)
        return True

    def recover_startup(self):
        self.recovered += 1

    def extras_start(self):
        self.extras_started += 1

    def paused(self):
        return self.pause_flag


class FrameworkTest(unittest.IsolatedAsyncioTestCase):

    async def test_lifecycle_and_pacing(self):
        w = _FakeWorker()
        w.claims = [{"id": 1}, {"id": 2}]
        w.idle_sleep = 0.01
        w.poll_seconds = 0.01
        with mock.patch.object(task_worker.logger, "info"):
            runner = asyncio.ensure_future(w.run())
            await asyncio.sleep(0.15)
        self.assertEqual(w.processed, [{"id": 1}, {"id": 2}])
        self.assertEqual(w.recovered, 1, "启动恢复恰好一次")
        self.assertEqual(w.extras_started, 1)
        runner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await runner
        self.assertEqual(w.released, [], "空转取消无在途，不误释放")

    async def test_cancel_during_process_releases_then_raises(self):
        """核心用例：处理中取消 → 先 release（再上抛），顺序不可反。"""
        w = _FakeWorker()
        w.claims = [{"id": 7}]
        w.gate = asyncio.Event()
        order = []
        w.release = lambda task: order.append("release") or True

        async def slow(task):
            order.append("process-start")
            await w.gate.wait()

        w.process = slow
        with mock.patch.object(task_worker.logger, "info"):
            runner = asyncio.ensure_future(w.run())
            await asyncio.sleep(0.05)
            self.assertIsNotNone(w._current)
            runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runner
        self.assertEqual(order, ["process-start", "release"],
                         "取消必须先放回再上抛")

    async def test_process_exception_contained_loop_survives(self):
        w = _FakeWorker()
        w.error_sleep = 0.01
        w.poll_seconds = 0.01
        w.claims = [{"id": 1}, {"id": 2}]

        calls = {"n": 0}

        async def flaky(task):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("炸一轮")
            w.processed.append(task)

        w.process = flaky
        with mock.patch.object(task_worker.logger, "exception"):
            runner = asyncio.ensure_future(w.run())
            await asyncio.sleep(0.15)
        self.assertEqual(w.processed, [{"id": 2}], "第一轮异常不退出循环")
        runner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await runner
        self.assertEqual(w.released, [], "业务异常不触发释放（域内自理）")

    async def test_pause_blocks_claim(self):
        w = _FakeWorker()
        w.poll_seconds = 0.01
        w.pause_flag = True
        with mock.patch.object(task_worker.logger, "info"):
            runner = asyncio.ensure_future(w.run())
            await asyncio.sleep(0.08)
        runner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await runner
        self.assertEqual(w.processed, [], "暂停期不领取")
        self.assertEqual(w.claims, [])

    async def test_release_failure_does_not_mask_cancel(self):
        """release 抛错（DB 忙）不得吞掉取消——转警告后照常上抛。"""
        w = _FakeWorker()
        w.claims = [{"id": 9}]
        w.gate = asyncio.Event()

        def bad_release(task):
            raise RuntimeError("db busy")

        w.release = bad_release
        with mock.patch.object(task_worker.logger, "warning"):
            runner = asyncio.ensure_future(w.run())
            await asyncio.sleep(0.05)
            runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runner

    async def test_release_inflight_fallback_idempotent(self):
        w = _FakeWorker()
        w._current = {"id": 3}
        self.assertTrue(w.release_inflight())
        w._current = None
        self.assertFalse(w.release_inflight())


if __name__ == "__main__":
    unittest.main()


class PeriodicRecoverTest(unittest.IsolatedAsyncioTestCase):
    """recover_seconds>0 时框架自带周期租约回收循环（asmr P2 的防线）。"""

    async def test_periodic_recover_fires(self):
        calls = []
        w = _FakeWorker()
        w.recover_seconds = 0.05
        w.recover_periodic = lambda: calls.append(1)
        w.recover_startup = lambda: None
        with mock.patch.object(task_worker.logger, "info"):
            runner = asyncio.ensure_future(w.run())
            await asyncio.sleep(0.18)
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass
        self.assertGreaterEqual(len(calls), 2, "周期回收应按间隔执行")


class CopyWatchdogTest(unittest.IsolatedAsyncioTestCase):
    """共享看门狗拷贝：冻结超限抛 TimeoutError；正常完成校验尺寸。"""

    async def test_frozen_copy_raises_timeout(self):
        import os
        import tempfile
        w = task_worker
        d = tempfile.mkdtemp()
        src = os.path.join(d, "src")
        open(src, "wb").write(b"x" * 10)

        def frozen(s, dd):
            import time as _t
            _t.sleep(1.0)      # dst 永不出现 → 无进展

        with self.assertRaises(TimeoutError):
            await w.copy_with_watchdog(src, os.path.join(d, "dst"), 10,
                                       check=0.05, stall=0.12,
                                       copy_fn=frozen, label="T")

    async def test_ok_copy_and_size_mismatch(self):
        import os
        import tempfile
        w = task_worker
        d = tempfile.mkdtemp()
        src = os.path.join(d, "src")
        open(src, "wb").write(b"abcdef")
        dst = os.path.join(d, "dst")
        await w.copy_with_watchdog(src, dst, 6, check=0.2, stall=1.0,
                                   copy_fn=None, label="T")
        self.assertEqual(os.path.getsize(dst), 6)
        with self.assertRaises(IOError):
            await w.copy_with_watchdog(src, dst, 99, check=0.2, stall=1.0,
                                       copy_fn=None, label="T")


class DownloadWatchdogTest(unittest.IsolatedAsyncioTestCase):
    """协程侧 .part 看门狗：同步层挂死（DNS/连接，零字节零超时）时
    弃线程快速返回 TimeoutError；正常下载（.part 增长）不误伤。"""

    async def test_hung_thread_abandoned(self):
        import os
        import tempfile
        import threading
        d = tempfile.mkdtemp()
        dest = os.path.join(d, "f.mp3")
        part = dest + ".part"
        open(part, "wb").write(b"x" * 100)     # 静止的 .part
        entered = threading.Event()

        def hang(url, dst, expected=None):
            entered.set()
            threading.Event().wait(5)          # 模拟 DNS 挂死：永不返回

        with self.assertRaises(TimeoutError):
            await task_worker.download_with_watchdog(
                "http://x", dest, 999, check=0.05, stall=0.12,
                download_fn=hang, label="T")
        self.assertTrue(entered.is_set())

    async def test_growing_part_completes(self):
        import os
        import tempfile
        import time as _t
        d = tempfile.mkdtemp()
        dest = os.path.join(d, "g.mp3")

        def slow_ok(url, dst, expected=None):
            part = dst + ".part"
            with open(part, "wb") as f:
                for _ in range(3):
                    f.write(b"z" * 512)
                    f.flush()
                    _t.sleep(0.08)             # 增长中——看门狗须放行
            os.replace(part, dst)
            return 1536

        n = await task_worker.download_with_watchdog(
            "http://x", dest, 1536, check=0.1, stall=1.5,
            download_fn=slow_ok, label="T")
        self.assertEqual(n, 1536)
        self.assertEqual(os.path.getsize(dest), 1536)

    async def test_cancel_propagates(self):
        import asyncio
        import os
        import tempfile
        d = tempfile.mkdtemp()
        dest = os.path.join(d, "c.mp3")

        async def drive():
            task = asyncio.ensure_future(
                task_worker.download_with_watchdog(
                    "http://x", dest, 1, check=0.05, stall=5.0,
                    download_fn=lambda u, dd, e=None: __import__(
                        "time").sleep(5),
                    label="T"))
            await asyncio.sleep(0.12)
            task.cancel()
            await task

        with self.assertRaises(asyncio.CancelledError):
            await drive()
