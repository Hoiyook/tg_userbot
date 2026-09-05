"""多 worker 下载池（workers.py）与 download_file worker 路由的单元测试。

守护点：
1. 池未启用（DOWNLOAD_WORKER_QUEUE=None）时 borrow() 返回 None → download_file
   照旧走消息自带客户端（test_queue 死锁回归的路径不受影响）。
2. spawn_pool / sync_pool_to_target 的扩缩容簿记：只摘空闲断开、在途不打断、
   live 恒不低于 target（borrow 不饿死）。
3. download_file 借到 worker 时把字节走 worker.download_media(message, ...)，
   而不是 message.download_media —— 这是「多 socket 并行」的关键分流。

不联网：spawn 的建 client 用假 client 顶替；download 路径沿用
test_queue 死锁回归的假消息配方（真实 download_file 全链路，无网络）。
运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import asyncio
import os
import tempfile
import types
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_workers_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import state  # noqa: E402
from tg_userbot import config  # noqa: E402
from tg_userbot import workers  # noqa: E402
from tg_userbot import download  # noqa: E402


class _FakeWorker:
    """假 worker：记录 download_media 调用；disconnect 无操作。"""

    def __init__(self, name, calls=None):
        self.name = name
        self.calls = calls if calls is not None else []
        self.disconnected = False
        self.session = types.SimpleNamespace(server_address="fake-host")

    async def disconnect(self):
        self.disconnected = True

    async def download_media(self, message, *, file=None, progress_callback=None):
        self.calls.append(("worker", file))
        with open(file, "wb") as f:
            f.write(b"w")
        return file


def _disabled_pool():
    """把池状态清成禁用默认（borrow 返回 None 的基线）。"""
    state.DOWNLOAD_WORKER_QUEUE = None
    state.DOWNLOAD_WORKERS = []
    state.DOWNLOAD_WORKER_TARGET = 0


class BorrowWithoutPoolTest(unittest.IsolatedAsyncioTestCase):
    """池未启用时 borrow() 立即返回 None（download 回退主客户端单连接）。"""

    async def asyncSetUp(self):
        self.old = (state.DOWNLOAD_WORKERS, state.DOWNLOAD_WORKER_QUEUE,
                    state.DOWNLOAD_WORKER_TARGET)
        _disabled_pool()

    async def asyncTearDown(self):
        state.DOWNLOAD_WORKERS, state.DOWNLOAD_WORKER_QUEUE, \
            state.DOWNLOAD_WORKER_TARGET = self.old

    async def test_borrow_returns_none(self):
        self.assertIsNone(await workers.borrow())


class PoolLifecycleTest(unittest.IsolatedAsyncioTestCase):
    """spawn/borrow/release 的簿记；建 client 用假 spawner 顶替。"""

    async def asyncSetUp(self):
        self.old = (state.DOWNLOAD_WORKERS, state.DOWNLOAD_WORKER_QUEUE,
                    state.DOWNLOAD_WORKER_TARGET)
        _disabled_pool()
        self.created = []

        async def fake_spawn(snapshot):
            w = _FakeWorker(f"w{len(self.created) + 1}")
            self.created.append(w)
            return w

        self._p_snapshot = mock.patch.object(
            workers, "read_live_session",
            return_value={"dc_id": 2, "server_address": "1.2.3.4",
                          "port": 443, "auth_key": object()},
        )
        self._p_spawn = mock.patch.object(workers, "_spawn_one", fake_spawn)
        self._p_snapshot.start()
        self._p_spawn.start()

    async def asyncTearDown(self):
        # 断开测试内建出的 worker（队列里的对象也在 DOWNLOAD_WORKERS 里）
        for w in list(state.DOWNLOAD_WORKERS):
            await w.disconnect()
        self._p_snapshot.stop()
        self._p_spawn.stop()
        state.DOWNLOAD_WORKERS, state.DOWNLOAD_WORKER_QUEUE, \
            state.DOWNLOAD_WORKER_TARGET = self.old

    async def test_spawn_populates_and_cycles(self):
        n = await workers.spawn_pool(3)
        self.assertEqual(n, 3)
        self.assertEqual(len(state.DOWNLOAD_WORKERS), 3)
        self.assertEqual(state.DOWNLOAD_WORKER_QUEUE.qsize(), 3)
        self.assertEqual(state.DOWNLOAD_WORKER_TARGET, 3)

        borrowed = [await workers.borrow() for _ in range(3)]
        self.assertEqual(state.DOWNLOAD_WORKER_QUEUE.qsize(), 0)
        for w in borrowed:
            await workers.release(w)
        self.assertEqual(state.DOWNLOAD_WORKER_QUEUE.qsize(), 3)
        self.assertEqual(len(state.DOWNLOAD_WORKERS), 3)

    async def test_spawn_zero_on_all_failures(self):
        async def boom(snapshot):
            raise OSError("连不上")
        with mock.patch.object(workers, "_spawn_one", boom):
            n = await workers.spawn_pool(3)
        self.assertEqual(n, 0)
        self.assertIsNone(state.DOWNLOAD_WORKER_QUEUE)
        self.assertEqual(state.DOWNLOAD_WORKERS, [])
        self.assertEqual(state.DOWNLOAD_WORKER_TARGET, 0)

    async def test_sync_shrink_all_idle_disconnects_to_target(self):
        await workers.spawn_pool(3)
        state.DOWNLOAD_WORKER_TARGET = 1
        await workers.sync_pool_to_target()
        # 全空闲：摘到 live == target，超额的已断开
        self.assertEqual(len(state.DOWNLOAD_WORKERS), 1)
        self.assertEqual(state.DOWNLOAD_WORKER_QUEUE.qsize(), 1)
        disconnected = sum(1 for w in self.created if w.disconnected)
        self.assertEqual(disconnected, 2)

    async def test_sync_grow_spawns_to_target(self):
        await workers.spawn_pool(2)
        state.DOWNLOAD_WORKER_TARGET = 5
        await workers.sync_pool_to_target()
        self.assertEqual(len(state.DOWNLOAD_WORKERS), 5)
        self.assertEqual(state.DOWNLOAD_WORKER_QUEUE.qsize(), 5)
        self.assertEqual(state.DOWNLOAD_WORKER_TARGET, 5)


class DownloadWorkerRoutingTest(unittest.IsolatedAsyncioTestCase):
    """download_file 借到 worker 时字节走 worker.download_media（多 socket 分流）。

    假消息沿用 test_queue 死锁回归的配方（真实 download_file 全链路可跑）。
    """

    async def asyncSetUp(self):
        self.old = {
            "sem": state.DOWNLOAD_SEMAPHORE,
            "client": state.client,
            "ww": state.DOWNLOAD_WORKERS,
            "wq": state.DOWNLOAD_WORKER_QUEUE,
            "wt": state.DOWNLOAD_WORKER_TARGET,
        }
        state.DOWNLOAD_SEMAPHORE = config.AdjustableSemaphore(1)
        _disabled_pool()

        # 一个 worker 入池：download_file 应借到它、走它的 download_media
        self.calls = []
        self.worker = _FakeWorker("w", self.calls)
        state.DOWNLOAD_WORKERS = [self.worker]
        state.DOWNLOAD_WORKER_QUEUE = asyncio.Queue()
        state.DOWNLOAD_WORKER_QUEUE.put_nowait(self.worker)
        state.DOWNLOAD_WORKER_TARGET = 1

        self.fake_message = mock.MagicMock()
        fake_file = mock.MagicMock()
        fake_file.name = "a.mp4"
        fake_file.size = 123
        self.fake_message.id = 1
        self.fake_message.file = fake_file
        self.fake_message.fwd_from = None
        self.fake_message.message = ""
        self.fake_message.media = None
        self.fake_message.photo = None
        self.fake_message.video = None
        self.fake_message.audio = None
        self.fake_message.voice = None
        self.fake_message.document = None

        async def message_download_media_should_not_be_used(file=None,
                                                            progress_callback=None):
            raise AssertionError("字节应走 worker.download_media，而非消息自带客户端")

        self.fake_message.download_media = message_download_media_should_not_be_used

        fake_client = mock.MagicMock()
        fake_client.is_connected = lambda: True

        async def fake_send_message(*args, **kwargs):
            return None

        fake_client.send_message = fake_send_message
        state.client = fake_client

    async def asyncTearDown(self):
        # 队列里的对象也在 DOWNLOAD_WORKERS 里，断开列表成员即可
        for w in list(state.DOWNLOAD_WORKERS):
            await w.disconnect()
        state.DOWNLOAD_SEMAPHORE = self.old["sem"]
        state.client = self.old["client"]
        state.DOWNLOAD_WORKERS = self.old["ww"]
        state.DOWNLOAD_WORKER_QUEUE = self.old["wq"]
        state.DOWNLOAD_WORKER_TARGET = self.old["wt"]

    async def test_worker_path_used_and_released(self):
        ok = await asyncio.wait_for(
            download.download_file(self.fake_message, "测试来源"),
            timeout=10,
        )
        self.assertTrue(ok)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "worker")
        # 完成后 worker 归还空闲队列
        self.assertEqual(state.DOWNLOAD_WORKER_QUEUE.qsize(), 1)
        self.assertEqual(len(state.DOWNLOAD_WORKERS), 1)


if __name__ == "__main__":
    unittest.main()
