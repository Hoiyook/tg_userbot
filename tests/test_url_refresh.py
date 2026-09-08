"""download_url_media 直链过期防护（刷新 + 降级解析 bot）的单元测试。

背景：douyinvod 直链签名带 ~3 小时过期戳（实测 = 解析时刻 +3h），本地解析链
入队的 kind=url 任务在深队列里排队太久会拿着过期直链开下——旧代码会白烧
DOWNLOAD_RETRIES 次重试后转 retry，而 /retry 重放的仍是同一条死链，任务救不
回来。守护点：
1. 直链签名过期戳解码（/<md5>/<hex>/ 段）+ 临近过期判定（含安全边际）。
2. 开下前解码出「临近过期/已过期」→ 先用记录里的原始分享链接重新解析刷新，
   旧直链不再尝试。
3. 直链被 CDN 拒（403/410）→ 短路重试循环：补一次刷新；刷新无效则把原始
   链接转交解析 bot 兜底（返回 "delegated"，队列按成功移除），不白烧重试。
4. 其它 HTTP 错误不触发降级，仍按 DOWNLOAD_RETRIES 普通重试。

不联网：HTTP 用 httpx.MockTransport 顶替，resolver/platform 用 mock 顶替。
运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import asyncio
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_url_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

import httpx  # noqa: E402  f2 的依赖，download_url_media 的测试必须真有它

from tg_userbot import state, config  # noqa: E402
from tg_userbot.config import SAVE_FOLDER  # noqa: E402  进程级真实保存目录
from tg_userbot import download  # noqa: E402
from tg_userbot import resolver  # noqa: E402
from tg_userbot import platform  # noqa: E402


_MD5_SEG = "d6221ae4be8b0383f57a025f91a94574"
_SHARE_URL = "https://v.douyin.com/abc123/"


def _make_direct_url(expiry_ts):
    """按真实 douyinvod 签名格式造一条直链：/<md5>/<hex过期戳>/video/...。"""
    return (
        f"https://v11-weba.douyinvod.com/{_MD5_SEG}/"
        f"{format(expiry_ts, 'x')}/video/tos/cn/tos-cn-ve-15/fake/"
    )


def _record_with_name(direct_url, name):
    return {
        "kind": "url",
        "url": _SHARE_URL,
        "direct_url": direct_url,
        "title": "测试视频",
        "final_name": name,
    }


class _TransportRecorder:
    """MockTransport 后端：按 URL 前缀返回 403/200/500，并记录收到的请求。"""

    def __init__(self, routes):
        # routes: {url 前缀: 状态码}；未命中前缀按 default_status
        self.routes = routes
        self.default_status = 200
        self.requested = []

    def handler(self, request):
        self.requested.append(str(request.url))
        for prefix, status in self.routes.items():
            if str(request.url).startswith(prefix):
                return httpx.Response(status, content=b"video-bytes")
        return httpx.Response(self.default_status, content=b"video-bytes")

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


class DirectUrlExpiryTest(unittest.TestCase):
    """直链签名过期戳解码 + 临近过期判定（纯函数）。"""

    def test_expiry_decoded_from_hex_segment(self):
        url = _make_direct_url(0x6A9EC977)
        self.assertEqual(download.direct_url_expiry(url), float(0x6A9EC977))

    def test_expiry_none_for_non_signature_url(self):
        # 分享短链 / 无 hex 段的普通 URL 都解码不出过期戳
        self.assertIsNone(download.direct_url_expiry(_SHARE_URL))
        self.assertIsNone(download.direct_url_expiry("https://example.com/a/b/"))
        self.assertIsNone(download.direct_url_expiry(""))
        self.assertIsNone(download.direct_url_expiry(None))

    def test_needs_refresh_within_margin_or_expired(self):
        expiry = int(time.time()) + 60 * 60  # 1 小时后过期
        url = _make_direct_url(expiry)
        # 剩余 > 15 分钟边际 → 不刷新
        self.assertFalse(
            download.direct_url_needs_refresh(url, now=expiry - 16 * 60)
        )
        # 恰到边际（剩余 15 分钟）→ 刷新
        self.assertTrue(
            download.direct_url_needs_refresh(url, now=expiry - 15 * 60)
        )
        # 已过期 → 刷新
        self.assertTrue(
            download.direct_url_needs_refresh(url, now=expiry + 1)
        )

    def test_needs_refresh_false_when_signature_unrecognized(self):
        # 解码不出过期戳就不盲刷（等 403 分支兜底）
        self.assertFalse(download.direct_url_needs_refresh(_SHARE_URL))


class DownloadUrlMediaTest(unittest.IsolatedAsyncioTestCase):
    """download_url_media 的刷新/降级行为（HTTP 全部走 MockTransport）。"""

    async def asyncSetUp(self):
        self.old_sem = state.DOWNLOAD_SEMAPHORE
        self.old_client = state.client
        state.DOWNLOAD_SEMAPHORE = config.AdjustableSemaphore(1)

        fake_client = mock.MagicMock()

        async def fake_send_message(*args, **kwargs):
            return None

        fake_client.send_message = fake_send_message
        state.client = fake_client

        self.transport = None

        async def _instant_sleep(*args, **kwargs):
            return None

        self._p_sleep = mock.patch.object(asyncio, "sleep", _instant_sleep)
        # 成功路径会写 download_history.txt；本模块的假下载不许污染真实历史
        #（整包 unittest 同进程共享 SAVE_FOLDER，test_menu 的「今日 0 个」
        # 断言依赖历史为空——不靠模块名字母序碰运气）
        self._p_history = mock.patch.object(download, "append_history")
        self._p_sleep.start()
        self._p_history.start()

    async def asyncTearDown(self):
        self._p_sleep.stop()
        self._p_history.stop()
        state.DOWNLOAD_SEMAPHORE = self.old_sem
        state.client = self.old_client

    def _use_transport(self, routes):
        self.transport = _TransportRecorder(routes)
        p = mock.patch.object(
            download, "_make_http_client",
            side_effect=lambda timeout: self.transport.client(),
        )
        p.start()
        self._patches = [p]
        return self.transport

    def _patch_resolver(self, result):
        async def fake_resolve(url):
            self.resolver_urls.append(url)
            return result

        self.resolver_urls = []
        p = mock.patch.object(resolver, "resolve_douyin", fake_resolve)
        p.start()
        self._patches.append(p)

    def _patch_relay(self, exc=None):
        self.relayed = []

        async def fake_relay(kind, urls):
            self.relayed.append((kind, list(urls)))
            if exc is not None:
                raise exc

        p = mock.patch.object(platform, "_relay_kind_links", fake_relay)
        p.start()
        self._patches.append(p)

    async def _run(self, record):
        try:
            return await asyncio.wait_for(
                download.download_url_media(record), timeout=10
            )
        finally:
            for p in getattr(self, "_patches", []):
                p.stop()

    async def test_fresh_url_downloads_without_refresh(self):
        """直链离过期还远：直接下载成功，完全不碰 resolver。"""
        fresh = _make_direct_url(int(time.time()) + 2 * 60 * 60)
        transport = self._use_transport({fresh: 200})
        self._patch_resolver(None)  # 若被调用会让 resolve 返回 None，不该发生
        record = _record_with_name(fresh, "新直链.mp4")

        ok = await self._run(record)

        self.assertTrue(ok)
        self.assertEqual(self.resolver_urls, [])
        self.assertEqual(len(transport.requested), 1)
        self.assertTrue(transport.requested[0].startswith(fresh))
        self.assertTrue(os.path.exists(os.path.join(SAVE_FOLDER, "抖音", "新直链.mp4")))

    async def test_stale_url_refreshed_before_first_attempt(self):
        """直链已过期：开下前重新解析刷新，旧直链根本不被尝试。"""
        stale = _make_direct_url(int(time.time()) - 60)
        new = _make_direct_url(int(time.time()) + 2 * 60 * 60)
        transport = self._use_transport({stale: 403, new: 200})
        self._patch_resolver(
            SimpleNamespace(direct_url=new, title="测试视频", author="作者")
        )
        record = _record_with_name(stale, "刷新后下载.mp4")

        ok = await self._run(record)

        self.assertTrue(ok)
        self.assertEqual(self.resolver_urls, [_SHARE_URL])
        # 旧直链一次都不该被请求
        self.assertEqual(
            [u for u in transport.requested if u.startswith(stale)], []
        )
        self.assertTrue(transport.requested[0].startswith(new))
        # 新直链写回记录（任务收尾时随队列持久化）
        self.assertEqual(record["direct_url"], new)
        self.assertTrue(
            os.path.exists(os.path.join(SAVE_FOLDER, "抖音", "刷新后下载.mp4"))
        )

    async def test_403_on_unexpired_url_triggers_refresh_then_downloads(self):
        """守卫路径：签名看着没过期却 403（格式变化/时钟漂移）→ 403 短路补刷新。"""
        url = _make_direct_url(int(time.time()) + 2 * 60 * 60)
        new = _make_direct_url(int(time.time()) + 3 * 60 * 60)
        transport = self._use_transport({url: 403, new: 200})
        self._patch_resolver(
            SimpleNamespace(direct_url=new, title="测试视频", author="作者")
        )
        record = _record_with_name(url, "403刷新.mp4")

        ok = await self._run(record)

        self.assertTrue(ok)
        self.assertEqual(self.resolver_urls, [_SHARE_URL])
        self.assertTrue(transport.requested[-1].startswith(new))

    async def test_403_refresh_failed_delegates_to_parse_bot(self):
        """刷新也拿不到新直链（cookie 失效等）→ 转交解析 bot 兜底，返回
        "delegated"（truthy → 队列按成功移除，不再重放死链）。"""
        stale = _make_direct_url(int(time.time()) - 60)
        transport = self._use_transport({stale: 403})
        self._patch_resolver(None)  # cookie 失效：解析不到
        self._patch_relay()
        record = _record_with_name(stale, "降级bot.mp4")

        result = await self._run(record)

        self.assertEqual(result, "delegated")
        self.assertEqual(self.relayed, [("douyin", [_SHARE_URL])])
        # 降级路径不该留下半成品
        self.assertFalse(
            os.path.exists(os.path.join(SAVE_FOLDER, "抖音", "降级bot.mp4"))
        )

    async def test_relay_failure_returns_false_for_retry(self):
        """转交解析 bot 本身失败（网络断等）→ 返回 False 转 retry，不丢链接。"""
        stale = _make_direct_url(int(time.time()) - 60)
        self._use_transport({stale: 403})
        self._patch_resolver(None)
        self._patch_relay(exc=OSError("网络不可达"))
        record = _record_with_name(stale, "转交失败.mp4")

        with mock.patch.object(download, "DOWNLOAD_RETRIES", 1):
            result = await self._run(record)

        self.assertFalse(result)

    async def test_other_http_error_keeps_plain_retry_no_delegate(self):
        """非 403/410 的 HTTP 错误（如 500）按普通失败重试，不刷新不降级。"""
        url = _make_direct_url(int(time.time()) + 2 * 60 * 60)
        transport = self._use_transport({url: 500})
        self._patch_resolver(None)
        self._patch_relay()
        record = _record_with_name(url, "500错误.mp4")

        with mock.patch.object(download, "DOWNLOAD_RETRIES", 2):
            result = await self._run(record)

        self.assertFalse(result)
        self.assertEqual(self.resolver_urls, [])
        self.assertEqual(self.relayed, [])
        # 500 仍按 DOWNLOAD_RETRIES 烧满普通重试
        self.assertEqual(len(transport.requested), 2)

    async def test_success_remembers_dedup_key(self):
        """成功落盘后把记录的 dedup_key 记入去重索引（文件 + 内存）。"""
        from tg_userbot import state as _state

        url = _make_direct_url(int(time.time()) + 2 * 60 * 60)
        self._use_transport({url: 200})
        record = _record_with_name(url, "入索引.mp4")
        record["dedup_key"] = "dyc:999"

        old_index = _state.DEDUP_INDEX
        temp_index = {}
        _state.DEDUP_INDEX = temp_index
        idx_file = os.path.join(_TMP, "dedup_index_urltest.txt")
        if os.path.exists(idx_file):
            os.remove(idx_file)
        try:
            with mock.patch.object(download.dedup, "DEDUP_INDEX_FILE",
                                   idx_file):
                ok = await self._run(record)
        finally:
            _state.DEDUP_INDEX = old_index

        self.assertTrue(ok)
        self.assertIn("dyc:999", temp_index)
        self.assertEqual(temp_index["dyc:999"]["filename"], "入索引.mp4")
        with open(idx_file, "r", encoding="utf-8") as f:
            self.assertIn("dyc:999", f.read())

    async def test_failure_does_not_remember(self):
        """失败/降级路径绝不记索引——没下成的文件下次还要能下。"""
        from tg_userbot import state as _state

        url = _make_direct_url(int(time.time()) - 60)
        self._use_transport({url: 403})
        record = _record_with_name(url, "不入索引.mp4")
        record["dedup_key"] = "dyc:fail"

        old_index = _state.DEDUP_INDEX
        _state.DEDUP_INDEX = {}
        try:
            with mock.patch.object(download.dedup, "DEDUP_INDEX_FILE",
                                   os.path.join(_TMP, "never.bin")), \
                    mock.patch.object(download, "DOWNLOAD_RETRIES", 1):
                await self._run(record)
        finally:
            _state.DEDUP_INDEX = old_index

        self.assertNotIn("dyc:fail", _state.DEDUP_INDEX)


if __name__ == "__main__":
    unittest.main()
