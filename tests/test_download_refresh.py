"""download.py 引用失效重试（_refresh_message）与失败通知可达性测试。

两个真实 bug（2026-09-29，SINEDAHH 任务三轮 9 连败引出）：

1. ``LocationInvalidError``/``FILE_REFERENCE_EXPIRED`` 后原样重试——消息内嵌
   的 file_reference 已被服务端作废，同一对象重试必然原样再败。修复：重试前
   经 ``_refresh_message`` 重取消息换新引用。
2. 失败通知里 ``netio.humanize_net_error`` 在未导入 netio 的模块里求值 →
   NameError 被 ``except Exception`` 吞掉，「失败必须汇报原因」的通知从未
   发出。修复：补 import；测试断言失败路径 notify_user 真被调用且带原因。

    .venv/bin/python -m unittest tests.test_download_refresh -v
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
_TMP = tempfile.mkdtemp(prefix="tg_userbot_download_refresh_test_")
import atexit  # noqa: E402
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from telethon.errors import FileReferenceExpiredError  # noqa: E402
from telethon.errors.rpcerrorlist import LocationInvalidError  # noqa: E402

from tg_userbot import download  # noqa: E402
from tg_userbot import state  # noqa: E402


class _FakeFile:
    def __init__(self, size):
        self.size = size


class _FakeMessage:
    """最小可下载消息：id/chat_id/file.size 三件套。"""

    def __init__(self, msg_id=100, chat_id=-1001234, size=4096):
        self.id = msg_id
        self.chat_id = chat_id
        self.file = _FakeFile(size)


class _FakeWorker:
    """borrow 出来的下载连接替身：按脚本逐次应答，记录收到的消息对象。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen_messages = []
        self.is_connected = lambda: True

    async def download_media(self, message, file=None, progress_callback=None):
        self.seen_messages.append(message)
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        with open(file, "wb") as f:
            f.write(b"x" * 32)
        return file


class _Base(unittest.IsolatedAsyncioTestCase):
    """把 download_file 的外部依赖（通知/来源/命名/去重/台账/池）收口。"""

    def _patches(self):
        return [
            mock.patch.object(download, "resolve_download_source",
                              new=mock.AsyncMock(return_value="测试源")),
            mock.patch.object(download, "compute_final_filename",
                              return_value="视频.mp4"),
            mock.patch.object(download, "get_original_filename",
                              return_value="视频.mp4"),
            mock.patch.object(download, "effective_caption",
                              return_value=""),
            mock.patch.object(download, "message_source_link",
                              return_value=None),
            mock.patch.object(download, "append_history"),
            mock.patch.object(download, "_sleep_and_reconnect",
                              new=mock.AsyncMock()),
            mock.patch.object(download.notify, "notify_user",
                              new=mock.AsyncMock()),
            mock.patch.object(download.stats, "emit_event"),
            mock.patch.object(download.dedup, "media_keys",
                              return_value=[]),
            mock.patch.object(download.dedup, "remember"),
            mock.patch.object(download.workers, "mark_healthy"),
            mock.patch.object(download.workers, "mark_unhealthy"),
        ]

    async def _run_download(self, worker, message):
        """在补丁全开下跑 download_file，返回 (结果, notify mock)。"""
        patches = self._patches()
        # 内容判重放行（成功路径会走到）
        patches.append(mock.patch.object(
            download, "_content_dedupe_check",
            new=mock.AsyncMock(return_value=(False, None))))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # 信号量在 main() 里才创建，测试里换普通 asyncio.Semaphore
        sem_patcher = mock.patch.object(download.state, "DOWNLOAD_SEMAPHORE",
                                        asyncio.Semaphore(1))
        sem_patcher.start()
        self.addCleanup(sem_patcher.stop)
        with mock.patch.object(download.workers, "borrow",
                               new=mock.AsyncMock(return_value=worker)):
            with mock.patch.object(download, "DOWNLOAD_DIR", _TMP):
                result = await download.download_file(message, task_id="t1")
        return result, download.notify.notify_user


class TestRefreshMessage(unittest.IsolatedAsyncioTestCase):
    """_refresh_message 本体：拿到新引用就换，拿不到沿用原消息。"""

    def setUp(self):
        self._client = mock.AsyncMock()
        patcher = mock.patch.object(download.state, "client", self._client)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_returns_fresh_message(self):
        fresh = _FakeMessage(msg_id=100)
        self._client.get_messages = mock.AsyncMock(return_value=fresh)
        orig = _FakeMessage(msg_id=100)
        self.assertIs(await download._refresh_message(orig), fresh)

    async def test_get_messages_failure_returns_original(self):
        self._client.get_messages = mock.AsyncMock(
            side_effect=RuntimeError("网络抖动"))
        orig = _FakeMessage(msg_id=100)
        # netio.shielded 把任何异常收口成 None → 沿用原消息
        self.assertIs(await download._refresh_message(orig), orig)

    async def test_no_chat_id_returns_original_without_calling(self):
        self._client.get_messages = mock.AsyncMock()
        orig = _FakeMessage(msg_id=100)
        orig.chat_id = None
        self.assertIs(await download._refresh_message(orig), orig)
        self._client.get_messages.assert_not_called()


class TestLocationInvalidWiring(_Base):
    """引用失效错误触发重取消息；普通连接错误不触发。"""

    async def test_refresh_called_and_new_reference_used(self):
        orig = _FakeMessage(msg_id=100)
        fresh = _FakeMessage(msg_id=100)
        # 三次尝试全败（LocationInvalid）→ 上限 3 → 失败收尾。
        # 第 1 次用原消息；第 2/3 次应用重取后的新引用。
        worker = _FakeWorker([LocationInvalidError(request=""),
                              LocationInvalidError(request=""),
                              LocationInvalidError(request="")])
        with mock.patch.object(
            download, "_refresh_message",
            new=mock.AsyncMock(return_value=fresh),
        ) as refresh:
            result, notify_mock = await self._run_download(worker, orig)
        self.assertFalse(result)
        # 连续 2 轮零推进（中间刷新过一次引用）→ 终结，不再烧第 3 次
        self.assertEqual(refresh.await_count, 1)
        self.assertEqual(worker.seen_messages, [orig, fresh])
        # 失败通知真的发出且带原因（netio 缺 import 时代这行从未可达）。
        # 三次全败且零字节 + 引用失效 → 2026-09-30 起走终结性文案（媒体失效）。
        text = notify_mock.await_args.args[0]
        self.assertIn("❌ 文件无法下载", text)
        self.assertIn("原因：", text)
        self.assertIn("LocationInvalid", text)
        self.assertIn("无法恢复", text)

    async def test_file_reference_expired_also_refreshes(self):
        worker = _FakeWorker([FileReferenceExpiredError(request=""),
                              FileReferenceExpiredError(request="")])
        with mock.patch.object(
            download, "_refresh_message",
            new=mock.AsyncMock(return_value=_FakeMessage()),
        ) as refresh:
            await self._run_download(worker, _FakeMessage())
        # 第 1 轮失败刷新一次，第 2 轮仍零推进 → 终结
        self.assertEqual(refresh.await_count, 1)

    async def test_connection_error_does_not_refresh(self):
        worker = _FakeWorker([ConnectionError("断连"),
                              ConnectionError("断连"),
                              ConnectionError("断连")])
        with mock.patch.object(
            download, "_refresh_message",
            new=mock.AsyncMock(return_value=_FakeMessage()),
        ) as refresh:
            await self._run_download(worker, _FakeMessage())
        refresh.assert_not_awaited()

    async def test_retry_after_refresh_can_succeed(self):
        orig = _FakeMessage(msg_id=100)
        fresh = _FakeMessage(msg_id=100)
        # 第 1 次引用失效，刷新后第 2 次成功 → 任务整体成功
        worker = _FakeWorker([LocationInvalidError(request=""), None])
        with mock.patch.object(
            download, "_refresh_message",
            new=mock.AsyncMock(return_value=fresh),
        ):
            result, _ = await self._run_download(worker, orig)
        self.assertTrue(result)
        self.assertEqual(worker.seen_messages, [orig, fresh])
        self.assertTrue(os.path.exists(
            os.path.join(_TMP, "测试源", "视频.mp4")))


if __name__ == "__main__":
    unittest.main()
