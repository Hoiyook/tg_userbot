"""download.py 断点续传 + 终结性死媒体检测测试。

背景（2026-09-30）：代理每 3-5 分钟掐断一次长传输，而重试循环原来每次都删
临时文件从 0 重下——GB 级大文件（NIMA 2.53GB）永远到不了终点；Evanescia 则
是媒体在服务器端已失效（刷新引用后零字节），队列无限重投反复打扰。

修复后行为：
1. document/photo 走 iter_download 断点续传泵，.download 半成品保留为锚点；
2. 本次执行有字节推进 → 重试上限放宽到 DOWNLOAD_RETRIES + RESUME_EXTRA_RETRIES；
3. 刷新引用后仍零字节 + 引用失效类错误 → 登记终结性失败，队列确认一次后移除。

    .venv/bin/python -m unittest tests.test_download_resume -v
"""
import asyncio
import os
import shutil
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录（config import 期
# 有真实副作用，见 test_queue_wiring 的基座说明）。
_TMP = tempfile.mkdtemp(prefix="tg_userbot_download_resume_test_")
import atexit  # noqa: E402
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from telethon.errors.rpcerrorlist import LocationInvalidError  # noqa: E402

from tg_userbot import download  # noqa: E402
from tg_userbot import state  # noqa: E402


class _FakeFile:
    def __init__(self, size):
        self.size = size


class _FakeMessage:
    def __init__(self, msg_id=100, chat_id=-1001234, size=4096, has_doc=True):
        self.id = msg_id
        self.chat_id = chat_id
        self.file = _FakeFile(size)
        if has_doc:
            self.document = object()   # 走 iter_download 泵；位置解析会被 patch


class _FakeStreamClient:
    """iter_download 替身：按脚本产出 (字节, 结局异常)，记录调用参数。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def iter_download(self, media, offset=0, file_size=None, dc_id=None):
        self.calls.append({"offset": offset, "file_size": file_size,
                           "dc_id": dc_id})
        item = self.script.pop(0)
        return self._gen(item)

    async def _gen(self, item):
        data, err = item
        if data:
            yield data
        if err is not None:
            raise err


class _Base(unittest.IsolatedAsyncioTestCase):
    """download_file 外部依赖收口（同 test_download_refresh 的基座）。"""

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
            mock.patch.object(download, "_content_dedupe_check",
                              new=mock.AsyncMock(return_value=(False, None))),
            # _pump_iter 内部 from telethon.utils import get_input_location
            mock.patch("telethon.utils.get_input_location",
                       return_value=(2, None)),
        ]

    async def _run_download(self, client_for_pump, message, config_overrides=None):
        """补丁全开下跑 download_file；borrow 返回 client_for_pump 作 worker。"""
        # 终结性登记是模块级字典：清掉其他测试文件（test_download_refresh
        # 同样用 task_id="t1"）留下的残留，保证断言只见本测试的效果
        download.TERMINAL_FAILURES.clear()
        self.addCleanup(download.TERMINAL_FAILURES.clear)
        patches = self._patches()
        for name, value in (config_overrides or {}).items():
            patches.append(mock.patch.object(download, name, value))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        sem_patcher = mock.patch.object(download.state, "DOWNLOAD_SEMAPHORE",
                                        asyncio.Semaphore(1))
        sem_patcher.start()
        self.addCleanup(sem_patcher.stop)
        with mock.patch.object(download.workers, "borrow",
                               new=mock.AsyncMock(return_value=client_for_pump)):
            with mock.patch.object(download, "DOWNLOAD_DIR", _TMP):
                result = await download.download_file(message, task_id="t1")
        return result, download.notify.notify_user


class TestPumpIter(unittest.IsolatedAsyncioTestCase):
    """_pump_iter 本体：offset 续传、绝对字节进度、短读转可重试。"""

    async def test_resume_from_part_offset(self):
        part = os.path.join(_TMP, "pump_a.download")
        with open(part, "wb") as f:
            f.write(b"AAAA")
        client = _FakeStreamClient([(b"BC", None)])
        progress = mock.Mock()
        with mock.patch("telethon.utils.get_input_location",
                        return_value=(2, None)):
            out = await download._pump_iter(client, object(), part, 6, 4,
                                            progress)
        self.assertEqual(out, part)
        with open(part, "rb") as f:
            self.assertEqual(f.read(), b"AAAABC")
        self.assertEqual(client.calls, [{"offset": 4, "file_size": 6,
                                         "dc_id": 2}])
        # 进度是绝对字节：基线 4 → 终点 6
        progress.assert_any_call(4, 6)
        progress.assert_any_call(6, 6)

    async def test_short_read_raises_connection_error(self):
        part = os.path.join(_TMP, "pump_b.download")
        client = _FakeStreamClient([(b"AB", None)])   # 声明 10 只给了 2
        with mock.patch("telethon.utils.get_input_location",
                        return_value=(2, None)), \
             self.assertRaises(ConnectionError):
            await download._pump_iter(client, object(), part, 10, 0,
                                      mock.Mock())
        # 短读的字节也要留在盘上（锚点），下次续传
        self.assertEqual(os.path.getsize(part), 2)

    async def test_fresh_start_uses_wb(self):
        part = os.path.join(_TMP, "pump_c.download")
        with open(part, "wb") as f:
            f.write(b"STALE")
        client = _FakeStreamClient([(b"ok", None)])
        with mock.patch("telethon.utils.get_input_location",
                        return_value=(2, None)):
            await download._pump_iter(client, object(), part, 2, 0, mock.Mock())
        with open(part, "rb") as f:
            self.assertEqual(f.read(), b"ok")   # wb 截断，不带旧残留


class TestResumeWiring(_Base):
    """download_file 接线：泵被选中、.part 保留、上限按进展放宽。"""

    async def test_progress_extends_cap_and_part_retained(self):
        msg = _FakeMessage(size=4096)
        # 每次尝试推进 4 字节后被掐（ConnectionError = 网络层取消的转换形态）
        client = _FakeStreamClient([
            (b"wxyz", ConnectionError("断")),
            (b"wxyz", ConnectionError("断")),
            (b"wxyz", ConnectionError("断")),
        ])
        # 压小上限便于断言：1 + 2（有进展）= 3 次尝试耗尽
        result, _ = await self._run_download(
            client, msg,
            {"DOWNLOAD_RETRIES": 1, "RESUME_EXTRA_RETRIES": 2})
        self.assertFalse(result)
        self.assertEqual(len(client.calls), 3)
        # 续传锚点逐次前移：0 → 4 → 8
        self.assertEqual([c["offset"] for c in client.calls], [0, 4, 8])
        # 失败收尾后 .part 保留（队列重试从这里继续）
        part = os.path.join(_TMP, "测试源", "视频.mp4.download")
        self.assertEqual(os.path.getsize(part), 12)
        # 有进展 → 不判终结
        self.assertEqual(download.TERMINAL_FAILURES, {})

    async def test_zero_progress_keeps_small_cap(self):
        msg = _FakeMessage(size=4096)
        client = _FakeStreamClient([
            (None, ConnectionError("断")),
            (None, ConnectionError("断")),
            (None, ConnectionError("断")),
            (None, ConnectionError("断")),
        ])
        result, _ = await self._run_download(
            client, msg,
            {"DOWNLOAD_RETRIES": 1, "RESUME_EXTRA_RETRIES": 2})
        self.assertFalse(result)
        self.assertEqual(len(client.calls), 1)   # 零进展不放宽：1 次即止


class TestTerminalDeadMedia(_Base):
    """刷新引用后零字节 + 引用失效 → 终结性登记与明确通知。"""

    async def test_terminal_registered_and_notified(self):
        msg = _FakeMessage(size=4096)
        client = _FakeStreamClient([
            (None, LocationInvalidError(request="")),
            (None, LocationInvalidError(request="")),
            (None, LocationInvalidError(request="")),
        ])
        with mock.patch.object(download, "_refresh_message",
                               new=mock.AsyncMock(return_value=msg)):
            result, notify_mock = await self._run_download(client, msg)
        self.assertFalse(result)
        reason = download.pop_terminal_failure("t1")
        self.assertIsNotNone(reason)
        self.assertIn("LocationInvalid", reason)
        self.assertIsNone(download.pop_terminal_failure("t1"))  # 取走即清
        text = notify_mock.await_args.args[0]
        self.assertIn("文件无法下载", text)
        self.assertIn("已失效", text)
        self.assertIn("无法恢复", text)

    async def test_progress_made_not_terminal(self):
        msg = _FakeMessage(size=4096)
        client = _FakeStreamClient([
            ((b"abcd", LocationInvalidError(request="")),),
            ((None, LocationInvalidError(request="")),),
            ((None, LocationInvalidError(request="")),),
        ])
        with mock.patch.object(download, "_refresh_message",
                               new=mock.AsyncMock(return_value=msg)), \
             mock.patch.object(download, "DOWNLOAD_RETRIES", 1):
            await self._run_download(client, msg)
        # 仅有 2 轮停滞（<3）→ 还不够判截断，不登记终结
        self.assertEqual(download.TERMINAL_FAILURES, {})

    async def test_truncated_media_terminal_after_3_stalls(self):
        """Evanescia 形态：开头能拿、续传点之后连续 3 次立即失效 → 终结。"""
        msg = _FakeMessage(size=4096)
        client = _FakeStreamClient([
            ((b"ABCDEFGH", LocationInvalidError(request=""))),  # 拿到 8B
            ((None, LocationInvalidError(request=""))),         # 停滞 1
            ((None, LocationInvalidError(request=""))),         # 停滞 2
            ((None, LocationInvalidError(request=""))),         # 停滞 3 → 终结
        ])
        with mock.patch.object(download, "_refresh_message",
                               new=mock.AsyncMock(return_value=msg)):
            result, notify_mock = await self._run_download(client, msg)
        self.assertFalse(result)
        self.assertEqual(len(client.calls), 4)   # 第 4 次停滞即 break（上限 15 用不满）
        reason = download.pop_terminal_failure("t1")
        self.assertIsNotNone(reason)
        self.assertIn("数据不完整", reason)
        self.assertIn("8.00 B", reason)
        text = notify_mock.await_args.args[0]
        self.assertIn("媒体数据不完整/已失效", text)
        # 残缺半成品被清掉（截断数据没有续传/落盘价值）
        part = os.path.join(_TMP, "测试源", "视频.mp4.download")
        self.assertFalse(os.path.exists(part))


class TestTerminalQueueRemoval(unittest.IsolatedAsyncioTestCase):
    """队列侧：终结性失败确认一次后移出队列（attempts ≥ 2）。"""

    def setUp(self):
        _TMPQ = tempfile.mkdtemp(prefix="tg_userbot_queue_terminal_test_")
        self.addCleanup(shutil.rmtree, _TMPQ, ignore_errors=True)
        self._tmpq = _TMPQ
        self._saved = (dict(state.QUEUE), set(state.EXECUTING))
        self.addCleanup(self._restore)

    def _restore(self):
        state.QUEUE.clear()
        state.QUEUE.update(self._saved[0])
        state.EXECUTING.clear()
        state.EXECUTING.update(self._saved[1])

    async def test_removed_after_second_confirmation(self):
        from tg_userbot import queue as queue_mod
        from tg_userbot import state

        record = {"id": "term-test-1", "kind": "media", "attempts": 1,
                  "chat_id": -1001234, "msg_id": 1, "label": "死媒体.mp4"}
        state.QUEUE["retry"] = [record]
        saved = {}
        def fake_save(rec, action):
            saved["action"] = action
        with mock.patch.object(queue_mod, "_save_after_mutation",
                               new=fake_save), \
             mock.patch.object(queue_mod.stats, "emit_event"), \
             mock.patch.object(queue_mod.download, "pop_terminal_failure",
                               return_value="媒体失效"), \
             mock.patch.object(queue_mod.notify, "notify_user",
                               new=mock.AsyncMock()):
            # 需要真实跑 execute_queued_task 的失败分支：
            # 直接构造（不经 _run_queued_task 的网络路径）
            import time as _time
            with mock.patch.object(queue_mod, "_record_fail_fast"), \
                 mock.patch.object(queue_mod.state, "QUEUE_LOCK",
                                   asyncio.Lock()):
                state.QUEUE["retry"] = [record]
                # 复刻 execute_queued_task 的失败处理关键段不可行（闭包大），
                # 这里直接调用完整函数并 mock _run_queued_task
                with mock.patch.object(
                        queue_mod, "_run_queued_task",
                        new=mock.AsyncMock(return_value=False)):
                    await queue_mod.execute_queued_task(record)
        self.assertEqual(saved.get("action"), "delete")
        self.assertEqual(state.QUEUE["retry"], [])   # 已移出
        self.assertEqual(state.QUEUE["tasks"], [])

    async def test_first_failure_still_retries(self):
        from tg_userbot import queue as queue_mod
        from tg_userbot import state

        record = {"id": "term-test-2", "kind": "media", "attempts": 0,
                  "chat_id": -1001234, "msg_id": 1, "label": "疑似死.mp4"}
        saved = {}
        def fake_save(rec, action):
            saved["action"] = action
        with mock.patch.object(queue_mod, "_save_after_mutation",
                               new=fake_save), \
             mock.patch.object(queue_mod.stats, "emit_event"), \
             mock.patch.object(queue_mod.download, "pop_terminal_failure",
                               return_value="媒体失效"), \
             mock.patch.object(queue_mod.notify, "notify_user",
                               new=mock.AsyncMock()), \
             mock.patch.object(queue_mod, "_record_fail_fast"), \
             mock.patch.object(queue_mod.state, "QUEUE_LOCK",
                               asyncio.Lock()), \
             mock.patch.object(queue_mod, "_run_queued_task",
                               new=mock.AsyncMock(return_value=False)), \
             mock.patch.object(queue_mod, "_backoff_delay", return_value=60):
            state.QUEUE["tasks"] = [record]
            state.QUEUE["retry"] = []
            await queue_mod.execute_queued_task(record)
        # 首败：转 retry 保留（再确认一次），不是 delete
        self.assertEqual(saved.get("action"), "to_retry")
        self.assertEqual(len(state.QUEUE["retry"]), 1)


if __name__ == "__main__":
    unittest.main()
