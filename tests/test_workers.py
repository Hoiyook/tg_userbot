"""多 worker 下载池（workers.py）与 download_file worker 路由的单元测试。

守护点：
1. 池未启用（DOWNLOAD_WORKER_QUEUE=None）时 borrow() 返回 None → download_file
   照旧走消息自带客户端（test_queue 死锁回归的路径不受影响）。
2. spawn_pool / sync_pool_to_target 的扩缩容簿记：只摘空闲断开、在途不打断、
   live 恒不低于 target（borrow 不饿死）。
3. download_file 借到 worker 时把字节走 worker.download_media(message, ...)，
   而不是 message.download_media —— 这是「多 socket 并行」的关键分流。
4. borrow 借出前验活：空闲期被网络抖动杀死的 worker 就地重连再借出（重连
   失败仍借出，交下载重试兜底）；健康连接不多发起 connect。

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
    """假 worker：记录 download_media/connect 调用；disconnect 即失联。

    is_connected/connect 与真实 TelegramClient 同接口（borrow 借出前验活、
    重连都要走它们）。
    """

    def __init__(self, name, calls=None):
        self.name = name
        self.calls = calls if calls is not None else []
        self.disconnected = False
        self.connected = True
        self.connect_calls = 0
        self.session = types.SimpleNamespace(server_address="fake-host")

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connect_calls += 1
        self.connected = True
        return True

    async def disconnect(self):
        self.disconnected = True
        self.connected = False

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


class BorrowLivenessTest(unittest.IsolatedAsyncioTestCase):
    """borrow 借出前验活：空闲期被网络抖动杀死的 worker 就地重连再借出。

    背景（2026-09-07 线上）：04:00 代理断连把全部 20 条 worker 杀死在池中，
    池无守护、借出不验活 → 每个任务第 1 次尝试 0 秒败在「Cannot send
    requests while disconnected」，白烧 1 次重试预算；3 个任务再被网络杀两次
    就耗尽重试、永久进 retry 列表。修复 = 借出前 is_connected() + 就地重连
    （带超时）；重连失败仍借出，交给下载自身的重试兜底，不劣于旧版。
    """

    async def asyncSetUp(self):
        self.old = (state.DOWNLOAD_WORKERS, state.DOWNLOAD_WORKER_QUEUE,
                    state.DOWNLOAD_WORKER_TARGET)
        _disabled_pool()
        self.worker = _FakeWorker("w")
        state.DOWNLOAD_WORKERS = [self.worker]
        state.DOWNLOAD_WORKER_QUEUE = asyncio.Queue()
        state.DOWNLOAD_WORKER_QUEUE.put_nowait(self.worker)
        state.DOWNLOAD_WORKER_TARGET = 1

    async def asyncTearDown(self):
        state.DOWNLOAD_WORKERS, state.DOWNLOAD_WORKER_QUEUE, \
            state.DOWNLOAD_WORKER_TARGET = self.old

    async def test_borrow_revives_dead_worker(self):
        self.worker.connected = False  # 空闲期被网络抖动杀死
        got = await workers.borrow()
        self.assertIs(got, self.worker)
        self.assertEqual(self.worker.connect_calls, 1)
        self.assertTrue(self.worker.is_connected())

    async def test_borrow_returns_dead_worker_when_reconnect_fails(self):
        """网络仍断（重连失败）时也要照常借出：borrow 若换走/扣下 worker 会
        把池抽干、借方饿死；借出后由下载重试路径（_sleep_and_reconnect）
        继续尝试复活，与旧行为兼容。"""
        self.worker.connected = False

        async def dead_network():
            raise OSError("网络不可达")

        self.worker.connect = dead_network
        got = await workers.borrow()
        self.assertIs(got, self.worker)

    async def test_borrow_does_not_touch_healthy_worker(self):
        """守卫：健康连接借出时不发起多余 connect。"""
        got = await workers.borrow()
        self.assertIs(got, self.worker)
        self.assertEqual(self.worker.connect_calls, 0)


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

    async def test_success_remembers_dedup_key(self):
        """成功落盘后把 tg:<file_unique_id> 记入去重索引（文件 + 内存）——
        同一媒体再次转发/重发时由入队前判重拦截。"""
        from tg_userbot import dedup

        self.fake_message.file.id = "TG-UID-9"
        old_index = state.DEDUP_INDEX
        state.DEDUP_INDEX = {}
        idx_file = os.path.join(_TMP, "dedup_index_workerstest.txt")
        if os.path.exists(idx_file):
            os.remove(idx_file)
        try:
            with mock.patch.object(dedup, "DEDUP_INDEX_FILE", idx_file):
                ok = await asyncio.wait_for(
                    download.download_file(self.fake_message, "测试来源"),
                    timeout=10,
                )
                self.assertTrue(ok)
                # mock 消息算出的最终名不可控，只断言键与落盘行为
                self.assertIn("tg:TG-UID-9", state.DEDUP_INDEX)
                with open(idx_file, "r", encoding="utf-8") as f:
                    self.assertIn("tg:TG-UID-9", f.read())
        finally:
            state.DEDUP_INDEX = old_index
            if os.path.exists(idx_file):
                os.remove(idx_file)

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


class ContentDedupHitTest(unittest.IsolatedAsyncioTestCase):
    """download_file 落盘前内容级判重（download.py 的内容检查挂钩）。

    下载完成、os.replace 之前读回 .download 临时文件算 sha256 查索引：
    命中 → 丢弃临时文件不落盘（.download 扩展名不在 CD2 备份白名单，
    重复内容进不了 115）、通知、把 tg:/f: 元数据键补记进索引（下次连
    元数据层都能拦）；未命中 → 照常落盘并把 c: 键一并 remember。
    """

    CONTENT = b"DUPLICATE-BYTES"

    async def asyncSetUp(self):
        import hashlib
        from tg_userbot import dedup
        self.dedup = dedup
        self.digest = hashlib.sha256(self.CONTENT).hexdigest()

        self.old = {
            "sem": state.DOWNLOAD_SEMAPHORE,
            "client": state.client,
            "ww": state.DOWNLOAD_WORKERS,
            "wq": state.DOWNLOAD_WORKER_QUEUE,
            "wt": state.DOWNLOAD_WORKER_TARGET,
            "idx": state.DEDUP_INDEX,
            "enabled": state.DEDUP_ENABLED,
        }
        state.DOWNLOAD_SEMAPHORE = config.AdjustableSemaphore(1)
        _disabled_pool()
        state.DEDUP_INDEX = {}
        state.DEDUP_ENABLED = True
        self.idx_file = os.path.join(_TMP, "dedup_index_content_hit.txt")
        if os.path.exists(self.idx_file):
            os.remove(self.idx_file)

        # 一个 worker，写已知字节（内容级判重的判定依据）
        self.notified = []

        async def write_known(message, *, file=None, progress_callback=None):
            with open(file, "wb") as f:
                f.write(self.CONTENT)
            return file

        self.worker = _FakeWorker("w")
        self.worker.download_media = write_known
        state.DOWNLOAD_WORKERS = [self.worker]
        state.DOWNLOAD_WORKER_QUEUE = asyncio.Queue()
        state.DOWNLOAD_WORKER_QUEUE.put_nowait(self.worker)
        state.DOWNLOAD_WORKER_TARGET = 1

        fake_client = mock.MagicMock()
        fake_client.is_connected = lambda: True

        async def fake_send_message(*args, **kwargs):
            if len(args) > 1:
                self.notified.append(args[1])

        fake_client.send_message = fake_send_message
        state.client = fake_client

        self.fake_message = mock.MagicMock()
        fake_file = mock.MagicMock()
        fake_file.name = "video.mp4"
        fake_file.size = 2048
        fake_file.id = "TG-C1"
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

        self.source_dir = os.path.join(config.SAVE_FOLDER, "内容判重来源")
        # 目录级隔离：三个测试共用落盘目录，先清掉上一个测试的落盘文件。
        # 路径必须用 config.SAVE_FOLDER（进程级共享、首个 import 的测试模块
        # 定下的那个临时目录），不能用本模块的 _TMP 拼——全量跑时两者不同。
        import shutil
        if os.path.isdir(self.source_dir):
            shutil.rmtree(self.source_dir)

    async def asyncTearDown(self):
        for w in list(state.DOWNLOAD_WORKERS):
            await w.disconnect()
        for key in ("sem", "client", "ww", "wq", "wt", "idx", "enabled"):
            setattr(state, {"sem": "DOWNLOAD_SEMAPHORE", "client": "client",
                            "ww": "DOWNLOAD_WORKERS",
                            "wq": "DOWNLOAD_WORKER_QUEUE",
                            "wt": "DOWNLOAD_WORKER_TARGET",
                            "idx": "DEDUP_INDEX",
                            "enabled": "DEDUP_ENABLED"}[key], self.old[key])
        if os.path.exists(self.idx_file):
            os.remove(self.idx_file)

    def _landed(self):
        return (os.listdir(self.source_dir)
                if os.path.isdir(self.source_dir) else [])

    async def _run(self):
        with mock.patch.object(self.dedup, "DEDUP_INDEX_FILE", self.idx_file):
            return await asyncio.wait_for(
                download.download_file(self.fake_message, "内容判重来源"),
                timeout=10,
            )

    async def test_content_hit_blocks_before_land(self):
        """索引里已有同内容 c: 键：临时文件被丢弃、不落盘、元数据键补记。"""
        state.DEDUP_INDEX[self.dedup.content_key(self.digest)] = {
            "date": "26-09-08 10:00", "filename": "原文件.mp4",
        }

        ok = await self._run()

        self.assertTrue(ok)  # 队列按成功移除（重复内容不需要重试）
        self.assertEqual(self._landed(), [])  # 没有任何文件落盘
        # 元数据键补记：下次同媒体连 tg:/f: 层都能拦
        self.assertIn("tg:TG-C1", state.DEDUP_INDEX)
        self.assertIn("f:video.mp4:2048", state.DEDUP_INDEX)
        self.assertTrue(any("内容重复" in n for n in self.notified))
        # worker 归还空闲队列（finally 链路照常收尾）
        self.assertEqual(state.DOWNLOAD_WORKER_QUEUE.qsize(), 1)

    async def test_content_miss_lands_and_remembers(self):
        """新内容：照常落盘，c: 键随 tg:/f: 一并入索引。"""
        ok = await self._run()

        self.assertTrue(ok)
        self.assertEqual(len(self._landed()), 1)
        self.assertIn("tg:TG-C1", state.DEDUP_INDEX)
        self.assertIn("f:video.mp4:2048", state.DEDUP_INDEX)
        self.assertIn(self.dedup.content_key(self.digest), state.DEDUP_INDEX)

    async def test_content_check_disabled_still_lands(self):
        """/dedup off：内容命中不拦截照常落盘；下载的文件仍记入索引
        （off 期间下载的内容也该挡住未来的重复）。"""
        state.DEDUP_INDEX[self.dedup.content_key(self.digest)] = {
            "date": "26-09-08 10:00", "filename": "原文件.mp4",
        }
        state.DEDUP_ENABLED = False

        ok = await self._run()

        self.assertTrue(ok)
        self.assertEqual(len(self._landed()), 1)  # 命中但没拦
        self.assertFalse(any("内容重复" in n for n in self.notified))
        self.assertIn(self.dedup.content_key(self.digest), state.DEDUP_INDEX)

    async def test_success_emits_event_with_exact_bytes(self):
        """下载层在成功点发 SUCCESS 事件，bytes 为精确字节数（台账容量
        不再靠 format_size 反解）；带 task_id 时才发（默认 None 不发）。"""
        from tg_userbot import stats as stats_mod
        ev_file = os.path.join(_TMP, "task_events_dl_success.jsonl")
        if os.path.exists(ev_file):
            os.remove(ev_file)
        try:
            with mock.patch.object(stats_mod, "TASK_EVENTS_FILE", ev_file):
                ok = await asyncio.wait_for(
                    download.download_file(
                        self.fake_message, "内容判重来源",
                        task_id="d" * 32,
                    ),
                    timeout=10,
                )
            self.assertTrue(ok)
            events = stats_mod.load_events(ev_file)
            success = [e for e in events if e["ev"] == "SUCCESS"]
            self.assertEqual(len(success), 1)
            self.assertEqual(success[0]["id"], "d" * 32)
            # 精确字节：worker 写入的已知内容长度，不经 format_size 反解
            self.assertEqual(success[0]["bytes"], len(self.CONTENT))
        finally:
            if os.path.exists(ev_file):
                os.remove(ev_file)

    async def test_content_hit_emits_dedup_hit_not_success(self):
        """内容级拦截发 DEDUP_HIT 终态（不是 SUCCESS——没落盘不算成功，
        台账对账才有独立出口桶）。"""
        from tg_userbot import stats as stats_mod
        state.DEDUP_INDEX[self.dedup.content_key(self.digest)] = {
            "date": "26-09-08 10:00", "filename": "原文件.mp4",
        }
        ev_file = os.path.join(_TMP, "task_events_dl_hit.jsonl")
        if os.path.exists(ev_file):
            os.remove(ev_file)
        try:
            with mock.patch.object(stats_mod, "TASK_EVENTS_FILE", ev_file):
                ok = await asyncio.wait_for(
                    download.download_file(
                        self.fake_message, "内容判重来源",
                        task_id="e" * 32,
                    ),
                    timeout=10,
                )
            self.assertTrue(ok)
            names = [e["ev"] for e in stats_mod.load_events(ev_file)]
            self.assertEqual(names.count("DEDUP_HIT"), 1)
            self.assertEqual(names.count("SUCCESS"), 0)
        finally:
            if os.path.exists(ev_file):
                os.remove(ev_file)


if __name__ == "__main__":
    unittest.main()
