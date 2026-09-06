"""url 任务（本地解析链）的队列分发 / 路由降级 / HTTP 下载（全部离线）。

覆盖：queue 对 kind=url 的分发、platform 抖音链接的「本地解析优先 + bot
兜底」分流、build_url_record、download_url_media 的真实流式写盘（httpx
MockTransport 模拟 CDN）。
"""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_urlqueue_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

import httpx  # noqa: E402  f2 的依赖，装 f2 即有

from tg_userbot import download, platform, queue, state, config  # noqa: E402
from tg_userbot.resolver import ResolveResult  # noqa: E402


def _result():
    return ResolveResult(
        aweme_id="123", title="测试标题", author="作者", direct_url="http://cdn/video"
    )


class QueueUrlDispatchTest(unittest.TestCase):
    """kind=url 任务走 download_url_media，成功移除/失败转 retry。"""

    def setUp(self):
        # py3.9 的 Lock/Semaphore 构造时绑定「当前」事件循环；全量跑时
        # 前置模块可能清掉 current loop → 先建 loop 再造原语，全程同一个
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        state.QUEUE = {"tasks": [], "retry": []}
        state.QUEUE_LOCK = asyncio.Lock()
        state.DOWNLOAD_SEMAPHORE = asyncio.Semaphore(2)
        state.EXECUTING.clear()

    def tearDown(self):
        asyncio.set_event_loop(None)
        self.loop.close()

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def test_url_kind_dispatches_to_download_url_media(self):
        record = {"id": "abc", "kind": "url"}
        dl_mock = mock.AsyncMock(return_value=True)
        with mock.patch.object(download, "download_url_media", new=dl_mock):
            ok = self._run(queue._run_queued_task(record))
        self.assertTrue(ok)
        dl_mock.assert_awaited_once_with(record)

    def test_url_failure_moves_to_retry(self):
        record = {"id": "abc2", "kind": "url"}
        state.QUEUE["tasks"] = [record]
        with mock.patch.object(download, "download_url_media",
                               new=mock.AsyncMock(return_value=False)), \
             mock.patch.object(queue, "save_queue"):
            self._run(queue.execute_queued_task(record))
        self.assertEqual(state.QUEUE["tasks"], [])
        self.assertEqual(len(state.QUEUE["retry"]), 1)


class BuildUrlRecordTest(unittest.TestCase):
    def test_record_fields(self):
        rec = platform.build_url_record("douyin", "https://v.douyin.com/x/", _result(),
                                        user_label="自存")
        self.assertEqual(rec["kind"], "url")
        self.assertEqual(rec["source"], "抖音")
        self.assertEqual(rec["user_label"], "自存")
        self.assertTrue(rec["final_name"].endswith(".mp4"))
        self.assertIn("#自存", rec["final_name"])


class DouyinRoutingTest(unittest.TestCase):
    """本地解析成功 → 入队不走 bot；失败/未启用 → bot 兜底。"""

    @staticmethod
    def _douyin_relay_calls(relay_mock):
        # _relay_kind_links('instagram', []) 的空调用也计数，按 douyin 过滤
        return [c for c in relay_mock.await_args_list if c.args[0] == "douyin"]

    def _run_relay(self, resolve_return, enabled=True):
        message = mock.Mock()
        message.id = 1
        message.message = "https://v.douyin.com/x/"
        with mock.patch.object(config, "RESOLVER_ENABLED", enabled), \
                mock.patch("tg_userbot.resolver.resolve_douyin",
                       new=mock.AsyncMock(return_value=resolve_return)), \
             mock.patch.object(queue, "enqueue_and_start",
                               new=mock.AsyncMock()) as enq, \
             mock.patch.object(platform, "_relay_kind_links",
                               new=mock.AsyncMock()) as relay, \
             mock.patch.object(state, "client", new=mock.AsyncMock()):
            asyncio.new_event_loop().run_until_complete(
                platform.relay_platform_links(message, ["https://v.douyin.com/x/"], [])
            )
        return enq, relay

    def test_resolve_success_enqueues_without_bot(self):
        enq, relay = self._run_relay(_result())
        self.assertEqual(enq.await_count, 1)
        self.assertEqual(len(self._douyin_relay_calls(relay)), 0)

    def test_resolve_failure_falls_back_to_bot(self):
        enq, relay = self._run_relay(None)
        self.assertEqual(enq.await_count, 0)
        # instagram（空列表）也会调一次 _relay_kind_links，按 douyin 参数过滤
        self.assertEqual(len(self._douyin_relay_calls(relay)), 1)

    def test_disabled_goes_straight_to_bot(self):
        enq, relay = self._run_relay(_result(), enabled=False)
        self.assertEqual(enq.await_count, 0)
        self.assertEqual(len(self._douyin_relay_calls(relay)), 1)


class DownloadUrlMediaTest(unittest.TestCase):
    """download_url_media 用 httpx MockTransport 模拟 CDN 的真实流式写盘。"""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        state.DOWNLOAD_SEMAPHORE = asyncio.Semaphore(2)

    def tearDown(self):
        asyncio.set_event_loop(None)
        self.loop.close()

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def test_success_writes_and_replaces(self):
        record = {
            "id": "u1", "kind": "url", "direct_url": "http://cdn/v",
            "final_name": "26-09-06 测试.mp4",
        }
        payload = b"x" * 200_000  # 跨多个 64KB 块

        def handler(request):
            return httpx.Response(200, headers={"content-length": str(len(payload))},
                                  content=payload)

        real_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        with mock.patch.object(download, "_make_http_client",
                               return_value=real_client), \
             mock.patch.object(download, "SAVE_FOLDER", _TMP), \
             mock.patch.object(download.state, "client", new=mock.AsyncMock()):
            ok = self._run(download.download_url_media(record))

        self.assertTrue(ok)
        final_path = os.path.join(_TMP, "抖音", "26-09-06 测试.mp4")
        self.assertTrue(os.path.exists(final_path))
        with open(final_path, "rb") as f:
            self.assertEqual(f.read(), payload)
        self.assertFalse(os.path.exists(final_path + ".download"))
