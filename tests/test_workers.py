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

    async def test_network_cancel_retried_not_propagated(self):
        """回归：download_media 抛网络层 CancelledError（telethon 断线时对 pending
        请求 future 调 cancel()）时应走重试重下，而不是把 CancelledError 一路打出
        download_file —— 旧代码它逃逸到队列层、except Exception 拦不住（py3.8+
        CancelledError 是 BaseException），任务静默死亡、记录永远卡在队列。"""
        # 关池 → download_file 走消息自带客户端路径（message.download_media）
        state.DOWNLOAD_WORKER_QUEUE = None
        state.DOWNLOAD_WORKERS = []
        state.DOWNLOAD_WORKER_TARGET = 0

        calls = []

        async def flaky_download_media(file=None, progress_callback=None):
            calls.append(file)
            if len(calls) == 1:
                raise asyncio.CancelledError("网络层 future.cancel()")
            with open(file, "wb") as f:
                f.write(b"x")
            return file

        self.fake_message.download_media = flaky_download_media

        async def _instant_sleep(*args, **kwargs):
            return None

        with mock.patch.object(asyncio, "sleep", _instant_sleep):
            ok = await asyncio.wait_for(
                download.download_file(self.fake_message, "测试来源"),
                timeout=10,
            )

        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)  # 首次被取消后重试并成功，不再向上抛

    async def _retry_exhaustion(self, err, retries, extra):
        """让 worker.download_media 每次抛同一错误，测重试耗尽时的尝试次数。"""
        calls = []

        async def failing_download_media(message, *, file=None,
                                         progress_callback=None):
            calls.append(file)
            raise err

        self.worker.download_media = failing_download_media

        async def _instant_sleep(*args, **kwargs):
            return None

        with mock.patch.object(asyncio, "sleep", _instant_sleep), \
                mock.patch.object(download, "DOWNLOAD_RETRIES", retries), \
                mock.patch.object(download, "EXPORT_RACE_EXTRA_RETRIES", extra):
            ok = await asyncio.wait_for(
                download.download_file(self.fake_message, "测试来源"),
                timeout=10,
            )
        return ok, calls

    async def test_authbytes_invalid_gets_extended_retries(self):
        """回归：跨 DC 授权导出竞态（AuthBytesInvalidError）应放宽重试上限 ——
        该错误秒级失败在首字节前、且竞争随成功者退出而消散，落败者多试几次才可能
        赢；上限 = DOWNLOAD_RETRIES + EXPORT_RACE_EXTRA_RETRIES。"""
        from telethon.errors import AuthBytesInvalidError

        ok, calls = await self._retry_exhaustion(
            AuthBytesInvalidError(None), retries=1, extra=3
        )
        self.assertFalse(ok)
        self.assertEqual(len(calls), 4)  # 上限 1 + 3，而非 1 次就放弃

    async def test_other_rpc_error_respects_plain_retry_cap(self):
        """对照：普通网络错误（非导出竞态）仍按 DOWNLOAD_RETRIES 封顶，
        不因特例被放大 —— 防止导出竞态宽松被误用到一切失败上。"""
        ok, calls = await self._retry_exhaustion(
            ConnectionError("连接被重置"), retries=2, extra=6
        )
        self.assertFalse(ok)
        self.assertEqual(len(calls), 2)  # 正好 DOWNLOAD_RETRIES 次，不加量

    async def test_authbytes_then_success_converges(self):
        """导出竞态的本质是「谁先落地谁赢」：几次失败后成功（竞争消散）应成功，
        验证放宽上限确实给了落败者继续尝试的机会。"""
        from telethon.errors import AuthBytesInvalidError

        calls = []

        async def converges_download_media(message, *, file=None,
                                           progress_callback=None):
            calls.append(file)
            if len(calls) < 3:
                raise AuthBytesInvalidError(None)
            with open(file, "wb") as f:
                f.write(b"w")
            return file

        self.worker.download_media = converges_download_media

        async def _instant_sleep(*args, **kwargs):
            return None

        with mock.patch.object(asyncio, "sleep", _instant_sleep), \
                mock.patch.object(download, "DOWNLOAD_RETRIES", 1), \
                mock.patch.object(download, "EXPORT_RACE_EXTRA_RETRIES", 6):
            ok = await asyncio.wait_for(
                download.download_file(self.fake_message, "测试来源"),
                timeout=10,
            )
        self.assertTrue(ok)
        self.assertEqual(len(calls), 3)  # 前 2 次竞态失败，第 3 次落地成功

    async def test_idle_timeout_fails_cleanly_and_cleans_temp(self):
        """回归：无进度看门狗 —— 连接僵死（既不报错也不出数据）时，应在超时后取消
        本次尝试（抛 TimeoutError）失败收场，并保证 .download 半成品被 finally
        清理、不残留孤儿临时文件（曾被异常退出留下的死文件）。"""
        state.DOWNLOAD_WORKER_QUEUE = None
        state.DOWNLOAD_WORKERS = []
        state.DOWNLOAD_WORKER_TARGET = 0

        async def hangy_download_media(file=None, progress_callback=None):
            with open(file, "wb") as f:
                f.write(b"partial")  # 先落点数据 → .download 存在，验证被清
            await asyncio.sleep(3600)  # 永不回调进度、永不结束

        self.fake_message.download_media = hangy_download_media

        with mock.patch.object(download, "DOWNLOAD_IDLE_TIMEOUT", 0.5), \
                mock.patch.object(download, "DOWNLOAD_RETRIES", 1):
            ok = await asyncio.wait_for(
                download.download_file(self.fake_message, "测试来源"),
                timeout=15,
            )

        self.assertFalse(ok)
        leftovers = [
            os.path.join(root, f)
            for root, _dirs, files in os.walk(_TMP)
            for f in files
            if f.endswith(".download")
        ]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
