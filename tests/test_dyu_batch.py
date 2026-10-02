"""/dyu 抖音作者批量（douyin_batch）测试。

覆盖：命令解析、作品→任务记录形状（serial/resolve_first/dedup_key/subdir）、
download_url_media 的子目录落盘与执行期解析（direct_url 空 + resolve_first）、
queue 的 serial 串行门（一次一条，不并发）。

    .venv/bin/python -m unittest tests.test_dyu_batch -v
"""
import asyncio
import atexit
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_dyu_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

import httpx  # noqa: E402  download_url_media 测试必须真有 httpx

from tg_userbot import state, config  # noqa: E402
from tg_userbot import download  # noqa: E402
from tg_userbot import douyin_batch  # noqa: E402
from tg_userbot import queue as queue_mod  # noqa: E402


class SinceTest(unittest.TestCase):
    """since 日期参数：解析 + 过滤 + 整页早停判定。"""

    def test_parse_since(self):
        url, sub, since = douyin_batch.parse_dyu_command(
            "/dyu https://v.douyin.com/abc since 2025-10-01 我的目录")
        self.assertEqual(url, "https://v.douyin.com/abc")
        self.assertEqual(sub, "我的目录")
        self.assertEqual(since.strftime("%Y-%m-%d"), "2025-10-01")

    def test_parse_since_no_subdir(self):
        url, sub, since = douyin_batch.parse_dyu_command(
            "/dyu https://v.douyin.com/abc since 2025-10-01")
        self.assertEqual(sub, "")
        self.assertIsNotNone(since)

    def test_parse_bad_since_rejected(self):
        self.assertEqual(
            douyin_batch.parse_dyu_command(
                "/dyu https://v.douyin.com/abc since 昨天"),
            (None, None, None))

    def test_parse_without_since(self):
        url, sub, since = douyin_batch.parse_dyu_command(
            "/dyu https://v.douyin.com/abc 目录")
        self.assertIsNone(since)
        self.assertEqual(sub, "目录")

    def test_filter_and_page_all_older(self):
        from datetime import datetime
        since = datetime(2025, 10, 1)
        awemes = [
            {"aweme_id": "1", "create_time": 1759363200},   # 2025-10-02 ✓
            {"aweme_id": "2", "create_time": 1759276800},   # 2025-10-01 ✓
            {"aweme_id": "3", "create_time": 1759190400},   # 2025-09-30 ✗
            {"aweme_id": "4", "create_time": None},         # 无时间 → 剔除
        ]
        kept, page_all_older = douyin_batch.filter_awemes_since(awemes, since)
        self.assertEqual([a["aweme_id"] for a in kept], ["1", "2"])
        self.assertFalse(page_all_older)   # 页内有新于 since 的
        # 整页全早于 → 停止翻页信号
        old_page = [{"aweme_id": "9", "create_time": 1700000000}]
        kept2, stop = douyin_batch.filter_awemes_since(old_page, since)
        self.assertEqual(kept2, [])
        self.assertTrue(stop)
        # since=None 不过滤
        kept3, stop3 = douyin_batch.filter_awemes_since(awemes, None)
        self.assertEqual(len(kept3), 4)
        self.assertFalse(stop3)


class ParseCommandTest(unittest.TestCase):
    def test_parse_ok(self):
        url, sub, since = douyin_batch.parse_dyu_command(
            "/dyu https://v.douyin.com/abc123/ 我的子目录")
        self.assertEqual(url, "https://v.douyin.com/abc123/")
        self.assertEqual(sub, "我的子目录")
        self.assertIsNone(since)

    def test_parse_no_subdir(self):
        url, sub, since = douyin_batch.parse_dyu_command(
            "/dyu https://v.douyin.com/x")
        self.assertEqual(url, "https://v.douyin.com/x")
        self.assertEqual(sub, "")
        self.assertIsNone(since)

    def test_parse_rejects(self):
        # 裸命令 / 非抖音域 / since 关键词后非日期
        self.assertEqual(douyin_batch.parse_dyu_command("/dyu"),
                         (None, None, None))
        self.assertEqual(
            douyin_batch.parse_dyu_command("/dyu https://youtube.com/x"),
            (None, None, None))
        url, sub, since = douyin_batch.parse_dyu_command(
            "/dyu https://www.douyin.com/user/MS4abc 我 的 目 录")
        self.assertEqual(sub, "我 的 目 录")


class BuildRecordTest(unittest.TestCase):
    def test_record_shape(self):
        aweme = {"aweme_id": "7123456789", "desc": "测试作品 #舞蹈",
                 "create_time": 1759000000}
        rec = douyin_batch.build_aweme_record(aweme, "作者甲", "子目录A")
        self.assertEqual(rec["kind"], "url")
        self.assertEqual(rec["url"],
                         "https://www.douyin.com/video/7123456789")
        self.assertIsNone(rec["direct_url"])
        self.assertTrue(rec["resolve_first"])   # 执行期解析（规避 3h 过期）
        self.assertTrue(rec["serial"])          # 串行门
        self.assertEqual(rec["subdir"], "子目录A")
        self.assertEqual(rec["dedup_key"], "dyc:7123456789")
        self.assertEqual(rec["author"], "作者甲")
        self.assertTrue(rec["final_name"].endswith(".mp4"))
        # 文件名日期前缀用作品发布时间（2025-09-…），不是入队时间
        self.assertIn("25-09-", rec["final_name"])

    def test_work_url_shape(self):
        self.assertEqual(
            douyin_batch._work_url("42"),
            "https://www.douyin.com/video/42")


class CookieRefreshTest(unittest.IsolatedAsyncioTestCase):
    """浏览器 cookie 保鲜：成功持久化；失败/关闭时静默沿用现值。"""

    async def test_success_persists(self):
        saved = {}
        with mock.patch(
                "tg_userbot.browser_cookies.load_browser_cookie_string",
                return_value=("msToken=fresh; sessionid=x", None)), \
             mock.patch.object(config, "save_douyin_cookie",
                               side_effect=lambda v: saved.update(v=v)
                               or None):
            ok = await douyin_batch.refresh_cookie_from_browser()
        self.assertTrue(ok)
        self.assertEqual(saved["v"], "msToken=fresh; sessionid=x")

    async def test_failure_keeps_current(self):
        with mock.patch(
                "tg_userbot.browser_cookies.load_browser_cookie_string",
                return_value=("", "读取 chrome cookie 失败：模拟")), \
             mock.patch.object(config, "save_douyin_cookie") as save:
            ok = await douyin_batch.refresh_cookie_from_browser()
        self.assertFalse(ok)
        save.assert_not_called()

    async def test_disabled_via_config(self):
        with mock.patch.object(config, "DYU_BROWSER_COOKIE", "off"), \
             mock.patch("tg_userbot.browser_cookies"
                        ".load_browser_cookie_string") as loader:
            self.assertFalse(await douyin_batch.refresh_cookie_from_browser())
        loader.assert_not_called()


class BotStampTest(unittest.TestCase):
    """在途目录戳：一盖一取一次性，过期作废。"""

    def test_stamp_and_pop_once(self):
        douyin_batch._BOT_STAMP.update(subdir=None, aweme_id=None, expires=0)
        douyin_batch.stamp_next_bot_video("作者X", "42")
        self.assertEqual(douyin_batch.pop_bot_stamp(), ("作者X", "42"))
        self.assertIsNone(douyin_batch.pop_bot_stamp())   # 一次性

    def test_expired_stamp_returns_none(self):
        douyin_batch._BOT_STAMP.update(subdir=None, aweme_id=None, expires=0)
        douyin_batch.stamp_next_bot_video("作者X", "42", ttl=-1)   # 已过期
        self.assertIsNone(douyin_batch.pop_bot_stamp())

    def test_empty_subdir_not_stamped(self):
        douyin_batch._BOT_STAMP.update(subdir=None, aweme_id=None, expires=0)
        douyin_batch.stamp_next_bot_video("", "42")
        self.assertIsNone(douyin_batch.pop_bot_stamp())


class SettleTest(unittest.IsolatedAsyncioTestCase):
    """销账：bot 送回作品 → 对应 url 重试任务出榜（write-through 删库）。"""

    async def test_settle_removes_matching_task(self):
        import asyncio as _aio
        from tg_userbot import state, queue as queue_mod
        old_q = state.QUEUE
        state.QUEUE = {"tasks": [], "retry": [
            {"id": "t1", "kind": "url",
             "url": "https://www.douyin.com/video/42",
             "final_name": "f.mp4"},
            {"id": "t2", "kind": "url",
             "url": "https://www.douyin.com/video/43",
             "final_name": "g.mp4"},
        ]}
        saved = []
        try:
            with mock.patch.object(queue_mod, "_save_after_mutation",
                                   side_effect=lambda r, op:
                                       saved.append((r["id"], op))):
                from tg_userbot import app as app_mod
                await _aio.wait_for(
                    app_mod._settle_dyu_task("42"), timeout=5)
            self.assertEqual([r["id"] for r in state.QUEUE["retry"]], ["t2"])
            self.assertEqual(saved, [("t1", "delete")])
        finally:
            state.QUEUE = old_q

    async def test_settle_no_match_is_noop(self):
        import asyncio as _aio
        from tg_userbot import state, queue as queue_mod
        old_q = state.QUEUE
        state.QUEUE = {"tasks": [], "retry": []}
        try:
            from tg_userbot import app as app_mod
            await _aio.wait_for(app_mod._settle_dyu_task("999"), timeout=5)
            self.assertEqual(state.QUEUE["retry"], [])
        finally:
            state.QUEUE = old_q


class HarvestHelpersTest(unittest.TestCase):
    """Chrome DOM 收割的纯函数：href 解析 / 停止判定 / cookie 合并。"""

    def test_parse_video_hrefs(self):
        hrefs = [
            "https://www.douyin.com/video/111?a=1",
            "https://www.douyin.com/video/111",       # 重复 → 去重
            "https://www.douyin.com/video/222/",
            "https://www.douyin.com/user/xxx",        # 非视频 → 忽略
            "", None,
        ]
        out = douyin_batch.parse_video_hrefs(hrefs)
        self.assertEqual([a["aweme_id"] for a in out], ["111", "222"])
        self.assertEqual(out[0]["desc"], "")

    def test_scroll_should_stop(self):
        # 连续 stable_rounds 轮无增长才停
        self.assertFalse(douyin_batch.scroll_should_stop([10, 10]))
        self.assertTrue(douyin_batch.scroll_should_stop([10, 12, 12, 12]))
        self.assertFalse(douyin_batch.scroll_should_stop(
            [10, 12, 12, 14], stable_rounds=2))

    def test_merge_cookie_fragments(self):
        base = "sessionid=abc; msToken=old; ttwid=old2; foo=1"
        out = douyin_batch.merge_cookie_fragments(
            base, {"msToken": "new", "ttwid": "new2"})
        parts = {p.split("=", 1)[0]: p.split("=", 1)[1]
                 for p in out.split("; ")}
        self.assertEqual(parts["sessionid"], "abc")
        self.assertEqual(parts["msToken"], "new")
        self.assertEqual(parts["ttwid"], "new2")
        self.assertEqual(parts["foo"], "1")


class _MockTransport:
    """httpx MockTransport 工厂：{url: (status, bytes)}。"""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def handler(self, request):
        self.requests.append(request.url)
        status, body = self.routes.get(str(request.url), (404, b""))
        return httpx.Response(status, content=body)

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


class DownloadSubdirResolveTest(unittest.IsolatedAsyncioTestCase):
    """direct_url 空 + resolve_first：执行期解析 + 子目录落盘。"""

    async def asyncSetUp(self):
        self.old_sem = state.DOWNLOAD_SEMAPHORE
        self.old_client = state.client
        self.old_dedup = (state.DEDUP_INDEX, state.DEDUP_ENABLED)
        state.DEDUP_INDEX = {}
        state.DEDUP_ENABLED = True
        state.DOWNLOAD_SEMAPHORE = config.AdjustableSemaphore(1)
        fake_client = mock.MagicMock()
        fake_client.send_message = mock.AsyncMock(return_value=None)
        state.client = fake_client
        self._patches = [
            mock.patch.object(download, "append_history"),
            mock.patch.object(download.notify, "notify_user",
                              new=mock.AsyncMock()),
        ]

    async def asyncTearDown(self):
        state.DOWNLOAD_SEMAPHORE = self.old_sem
        state.client = self.old_client   # 不还原会泄漏到 test_reporter 的状态判定
        state.DEDUP_INDEX, state.DEDUP_ENABLED = self.old_dedup

    async def test_resolve_first_and_subdir(self):
        direct = "https://v.douyinvod.com/fake/video/tos/cn/x/"
        transport = _MockTransport({direct: (200, b"dyvideo-bytes")})
        self._patches.append(mock.patch.object(
            download, "_make_http_client",
            side_effect=lambda timeout: transport.client()))
        refreshed = mock.AsyncMock(return_value=direct)
        self._patches.append(mock.patch.object(download, "_refresh_direct_url",
                                               refreshed))
        # bot 转交失败（模拟第三方不可达）→ 落到 f2 直连路径
        from tg_userbot import platform
        self._patches.append(mock.patch.object(
            platform, "relay_links_to_parse_bot",
            new=mock.AsyncMock(side_effect=RuntimeError("bot 不可达"))))
        for p in self._patches:
            p.start()
        record = {
            "id": "dyu1", "kind": "url", "platform": "douyin",
            "url": "https://www.douyin.com/video/777",
            "direct_url": None, "resolve_first": True, "serial": True,
            "subdir": "作者合集", "title": "标题", "author": "作者",
            "final_name": "25-10-02 标题.mp4", "source": "抖音作者合集",
            "attempts": 2,   # 第 1 顺位是解析 bot；f2 直连在重放轮生效
        }
        ok = await download.download_url_media(record)
        for p in self._patches:
            p.stop()
        self.assertTrue(ok)
        refreshed.assert_awaited_once()      # 执行期解析确实发生
        # 落盘在 抖音/<子目录>/ 下
        expect = os.path.join(download._douyin_folder(), "作者合集",
                              "25-10-02 标题.mp4")
        self.assertTrue(os.path.isfile(expect), expect)
        with open(expect, "rb") as f:
            self.assertEqual(f.read(), b"dyvideo-bytes")


class ResolveFailureTest(unittest.IsolatedAsyncioTestCase):
    """执行期解析失败：退避重试（不落盘）；屡败转解析 bot。"""

    async def asyncSetUp(self):
        self.old_sem = state.DOWNLOAD_SEMAPHORE
        self.old_client = state.client
        self.old_dedup = (state.DEDUP_INDEX, state.DEDUP_ENABLED)
        state.DEDUP_INDEX = {}
        state.DEDUP_ENABLED = True
        state.DOWNLOAD_SEMAPHORE = config.AdjustableSemaphore(1)
        fake = mock.MagicMock()
        fake.send_message = mock.AsyncMock(return_value=None)
        state.client = fake
        self._patches = [
            mock.patch.object(download, "append_history"),
            mock.patch.object(download.notify, "notify_user",
                              new=mock.AsyncMock()),
        ]

    async def asyncTearDown(self):
        state.DOWNLOAD_SEMAPHORE = self.old_sem
        state.client = self.old_client
        state.DEDUP_INDEX, state.DEDUP_ENABLED = self.old_dedup

    def _record(self, attempts):
        return {
            "id": "dyu-f1", "kind": "url", "platform": "douyin",
            "url": "https://www.douyin.com/video/9",
            "direct_url": None, "resolve_first": True, "serial": True,
            "subdir": "X", "title": "t", "author": "a",
            "final_name": "f.mp4", "source": "抖音作者合集",
            "attempts": attempts,
        }

    async def test_first_attempt_relays_to_parse_bot_keeps_task(self):
        """owner 指令（2026-10-02）：第 1 顺位解析 bot——转交但不删任务。"""
        relayed = mock.AsyncMock()
        from tg_userbot import platform
        self._patches.append(mock.patch.object(
            platform, "relay_links_to_parse_bot", relayed))
        self._patches.append(mock.patch.object(asyncio, "sleep",
                                               new=mock.AsyncMock()))
        for p in self._patches:
            p.start()
        try:
            ok = await download.download_url_media(self._record(attempts=1))
        finally:
            for p in self._patches:
                p.stop()
        relayed.assert_awaited_once()   # 链接已发解析 bot
        self.assertFalse(ok)            # 但任务按失败退避（转交≠送达）

    async def test_second_attempt_f2_fail_returns_false_no_file(self):
        self._patches.append(mock.patch.object(
            download, "_refresh_direct_url",
            new=mock.AsyncMock(return_value=None)))
        self._patches.append(mock.patch.object(
            asyncio, "sleep", new=mock.AsyncMock()))
        for p in self._patches:
            p.start()
        try:
            ok = await download.download_url_media(self._record(attempts=2))
        finally:
            for p in self._patches:
                p.stop()
        self.assertFalse(ok)     # 退避重试，不是假成功
        self.assertFalse(os.path.exists(
            os.path.join(download._douyin_folder(), "X", "f.mp4")))

    async def test_no_attempt_cap_always_relays(self):
        """owner 指令（2026-10-02）：无次数上限——任意 attempts 都转 bot，
        绝不转交收尾。销账由目录戳消费方负责。"""
        relayed = mock.AsyncMock()
        from tg_userbot import platform
        self._patches.append(mock.patch.object(
            platform, "relay_links_to_parse_bot", relayed))
        self._patches.append(mock.patch.object(asyncio, "sleep",
                                               new=mock.AsyncMock()))
        for p in self._patches:
            p.start()
        try:
            for attempts in (8, 20, 99):
                relayed.reset_mock()
                ok = await download.download_url_media(
                    self._record(attempts=attempts))
                relayed.assert_awaited_once()   # 仍走 bot 优先
                self.assertFalse(ok)            # 留榜等销账
        finally:
            for p in self._patches:
                p.stop()

    async def test_html_response_rejected(self):
        """直链拿到 text/html：判失败绝不落盘（守卫）。"""
        import httpx as _hx

        class _T:
            def handler(self, request):
                return _hx.Response(200, content=b"<html>page</html>",
                                    headers={"content-type": "text/html"})
            def client(self):
                return _hx.AsyncClient(
                    transport=_hx.MockTransport(self.handler))
        self._patches.append(mock.patch.object(
            download, "_make_http_client",
            side_effect=lambda t: _T().client()))
        self._patches.append(mock.patch.object(
            download, "_refresh_direct_url",
            new=mock.AsyncMock(
                return_value="https://v.douyinvod.com/fake/x/")))
        for p in self._patches:
            p.start()
        try:
            ok = await download.download_url_media(self._record(attempts=1))
        finally:
            for p in self._patches:
                p.stop()
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(
            os.path.join(download._douyin_folder(), "X", "f.mp4")))


class SerialGateTest(unittest.IsolatedAsyncioTestCase):
    """serial=True 的 url 任务一次只跑一条（全局串行门）。"""

    async def test_no_overlap(self):
        queue_mod._URL_SERIAL_LOCK = None   # 洁净锁
        running = []
        order = []

        async def fake_dl(record):
            running.append(record["id"])
            order.append(("start", record["id"]))
            self.assertEqual(len(running), 1)   # 串行：无并发
            await asyncio.sleep(0.05)
            running.pop()
            order.append(("end", record["id"]))
            return True

        with mock.patch.object(queue_mod, "download_url_media",
                               side_effect=fake_dl) if False else \
             mock.patch.object(queue_mod.download, "download_url_media",
                               side_effect=fake_dl):
            r1 = {"id": "s1", "kind": "url", "serial": True}
            r2 = {"id": "s2", "kind": "url", "serial": True}
            await asyncio.gather(
                queue_mod._run_queued_task(r1),
                queue_mod._run_queued_task(r2))
        # 完全串行：s1 start/end 之后才是 s2 start/end
        self.assertEqual(order, [("start", "s1"), ("end", "s1"),
                                 ("start", "s2"), ("end", "s2")])
        queue_mod._URL_SERIAL_LOCK = None


class ManualCommandTest(unittest.TestCase):
    """/manual：文档存在且能被命令定位（发送走 state.client，另测替身）。"""

    def test_doc_exists_at_expected_path(self):
        import tg_userbot.commands as cmds
        root = os.path.dirname(os.path.dirname(
            os.path.abspath(cmds.__file__)))
        doc = os.path.join(root, "docs", "下载链路操作指引.md")
        self.assertTrue(os.path.isfile(doc), doc)
        content = open(doc, encoding="utf-8").read()
        self.assertIn("mermaid", content)          # 数据流图在
        self.assertIn("/dyu", content)             # 命令表在
        self.assertIn("更新记录", content)          # 长期维护表在


if __name__ == "__main__":
    unittest.main()
