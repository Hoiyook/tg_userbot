"""本地 API（api_server）的单元测试（2026-11-01）。

覆盖五块：
- 鉴权：无 token 全放行（仅 loopback 部署语义）；配 token 后 Bearer /
  X-Token / ?token= 三路都认、缺失与错值一律 401；
- /api/status：state 缺席安全降级 + 队列/在途/台账字段的如实映射 +
  rebuild_stats 异常回落 collect_stats；
- /api/enqueue：分流顺序（抖音/IG → 种子 → B站 → 外链台账）逐路打到
  既有入口（全部 mock，绝不真解析/真下载），空文本 400；
- /api/action：白名单外 404，白名单内各自打到对应服务函数；
- /api/history 与 /（看板页面存在性）。

不联网：aiohttp TestServer 只绑本机随机端口；Telegram / DB 全部注入替身。
"""
import asyncio
import atexit
import json
import atexit as _atexit
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_api_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config as _cfg  # noqa: E402

# 本机启用本地 API 后真实 tg_secrets.json 带 api.token——不抹掉的话，
# 未显式 patch token 的用例全撞 401。只动 API 相关配置（进程内），
# 不动 TG_SECRETS_FILE：cd2/ops_report 等测试要读真实 secrets 的 cd2 段。
# 泄漏修复（2026-11-05 审计）：保存原值，atexit 恢复——此前模块级直接
# 覆盖永不恢复，按字母序污染后续测试文件（config 全局被改）。
_cfg.API_TOKEN = ""
_cfg.API_ENABLED = False
_ORIG_API_TOKEN = _cfg.API_TOKEN
_ORIG_API_ENABLED = _cfg.API_ENABLED


@_atexit.register
def _restore_api_config():
    _cfg.API_TOKEN = _ORIG_API_TOKEN
    _cfg.API_ENABLED = _ORIG_API_ENABLED

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from tg_userbot import api_server  # noqa: E402
from tg_userbot import config  # noqa: E402
from tg_userbot import state  # noqa: E402
from tg_userbot import chrome_agent  # noqa: E402
from tg_userbot import pawchive  # noqa: E402
from tg_userbot import runtime_db as runtime_db_mod  # noqa: E402


def make_client() -> TestClient:
    return TestClient(TestServer(api_server.make_app()))


def patch_token(value):
    return mock.patch.object(config, "API_TOKEN", value)


class AuthTest(unittest.IsolatedAsyncioTestCase):
    """鉴权三通道 + 未配置 token 的放行语义。"""

    async def test_open_access_when_token_empty(self):
        # token 为空 = 仅 loopback 部署（start_api 已挡非 loopback），全放行
        with patch_token(""), mock.patch.object(
                api_server, "build_status", return_value={"ok": True}):
            client = make_client()
            await client.start_server()
            try:
                resp = await client.get("/api/status")
                self.assertEqual(resp.status, 200)
            finally:
                await client.close()

    async def test_missing_token_rejected_when_configured(self):
        with patch_token("sekret"), mock.patch.object(
                api_server, "build_status", return_value={"ok": True}):
            client = make_client()
            await client.start_server()
            try:
                resp = await client.get("/api/status")
                self.assertEqual(resp.status, 401)
                data = await resp.json()
                self.assertFalse(data["ok"])
            finally:
                await client.close()

    async def test_wrong_token_rejected(self):
        with patch_token("sekret"), mock.patch.object(
                api_server, "build_status", return_value={"ok": True}):
            client = make_client()
            await client.start_server()
            try:
                for headers, params in (
                        ({"Authorization": "Bearer wrong"}, {}),
                        ({"X-Token": "wrong"}, {}),
                        ({}, {"token": "wrong"})):
                    resp = await client.get("/api/status", headers=headers,
                                            params=params)
                    self.assertEqual(resp.status, 401)
            finally:
                await client.close()

    async def test_three_token_channels_accepted(self):
        with patch_token("sekret"), mock.patch.object(
                api_server, "build_status", return_value={"ok": True}):
            client = make_client()
            await client.start_server()
            try:
                r1 = await client.get(
                    "/api/status",
                    headers={"Authorization": "Bearer sekret"})
                r2 = await client.get("/api/status",
                                      headers={"X-Token": "sekret"})
                r3 = await client.get("/api/status", params={"token": "sekret"})
                self.assertEqual([r1.status, r2.status, r3.status],
                                 [200, 200, 200])
            finally:
                await client.close()


class StatusTest(unittest.IsolatedAsyncioTestCase):
    """/api/status 的字段映射与降级。"""

    async def test_maps_queue_and_inflight_and_stats(self):
        fake_state = {
            "QUEUE": {
                "tasks": [{"id": "abcdef1234567890",
                           "final_name": "26-11-01 测试 - file.mp4",
                           "kind": "url", "platform": "douyin"}],
                "retry": [{"id": "ffffffffffffffff",
                           "url": "https://x.com/a", "attempts": 2,
                           "kind": "url", "platform": "x"}],
            },
            "ACTIVE_DOWNLOADS": {
                1: {"filename": "大文件.zip", "percent": 42,
                    "downloaded": 1000, "total": 2400},
            },
            "RUNTIME_DB_READY": True,
            "DOWNLOAD_CONCURRENCY": 5,
        }
        fake_stats = {"success": 7, "success_bytes": 123456789,
                      "failed_final": 1, "dedup_hit": 3, "active": 1}
        with mock.patch.object(state, "QUEUE", fake_state["QUEUE"]), \
             mock.patch.object(state, "ACTIVE_DOWNLOADS",
                               fake_state["ACTIVE_DOWNLOADS"]), \
             mock.patch.object(state, "RUNTIME_DB_READY", True), \
             mock.patch.object(state, "DOWNLOAD_CONCURRENCY", 5), \
             mock.patch.object(api_server.stats, "rebuild_stats",
                               return_value=fake_stats):
            client = make_client()
            await client.start_server()
            try:
                data = await (await client.get("/api/status")).json()
            finally:
                await client.close()
        self.assertTrue(data["ok"])
        self.assertEqual(data["stats"]["in_flight"], 1)
        self.assertEqual(data["stats"]["queued"], 1)
        self.assertEqual(data["stats"]["retry"], 1)
        self.assertEqual(data["stats"]["done_today"], 7)
        self.assertEqual(data["stats"]["bytes_today"], 123456789)
        self.assertEqual(data["in_flight"][0]["percent"], 42)
        self.assertEqual(data["queue"][0]["id"], "abcdef12")
        self.assertEqual(data["queue"][0]["state"], "排队")
        self.assertEqual(data["retry"][0]["state"], "待重试")
        self.assertEqual(data["retry"][0]["attempts"], 2)
        self.assertEqual(data["concurrency"], 5)

    async def test_state_absent_degrades_safely(self):
        # main() 未跑（state.* 全是 None/空占位）也不炸——套壳 app 的
        # 可用性不该依赖调用时序
        with mock.patch.object(state, "QUEUE", None), \
             mock.patch.object(state, "ACTIVE_DOWNLOADS", None), \
             mock.patch.object(api_server.stats, "rebuild_stats",
                               side_effect=RuntimeError("db down")), \
             mock.patch.object(api_server.stats, "collect_stats",
                               return_value={"success_count": 4}):
            client = make_client()
            await client.start_server()
            try:
                data = await (await client.get("/api/status")).json()
            finally:
                await client.close()
        self.assertTrue(data["ok"])
        self.assertEqual(data["stats"]["in_flight"], 0)
        self.assertEqual(data["stats"]["done_today"], 4)  # collect_stats 回落


class EnqueueTest(unittest.IsolatedAsyncioTestCase):
    """/api/enqueue 的平台分流：全部 mock 既有入口，验证「打到同一路」。"""

    async def test_douyin_text_routes_to_platform_relay(self):
        with mock.patch.object(
                api_server.platform, "extract_douyin_urls",
                return_value=["https://v.douyin.com/abc/"]), \
             mock.patch.object(api_server.platform,
                               "extract_instagram_urls", return_value=[]), \
             mock.patch.object(api_server.platform, "relay_platform_links",
                               new=mock.AsyncMock()) as relay:
            client = make_client()
            await client.start_server()
            try:
                data = await _post_json(client, "/api/enqueue",
                                        {"text": "自存 https://v.douyin.com/abc/"})
            finally:
                await client.close()
        self.assertTrue(data["ok"])
        self.assertEqual(data["routed"], "douyin")
        shim, douyin, instagram = relay.await_args.args
        self.assertEqual(douyin, ["https://v.douyin.com/abc/"])
        self.assertEqual(instagram, [])
        self.assertEqual(shim.message, "自存 https://v.douyin.com/abc/")
        self.assertGreaterEqual(shim.id, 10 ** 12)  # 不与真实消息 id 空间重叠

    async def test_instagram_text_routes_to_relay(self):
        with mock.patch.object(api_server.platform,
                               "extract_douyin_urls", return_value=[]), \
             mock.patch.object(api_server.platform,
                               "extract_instagram_urls",
                               return_value=["https://instagram.com/x"]), \
             mock.patch.object(api_server.platform, "relay_platform_links",
                               new=mock.AsyncMock()):
            client = make_client()
            await client.start_server()
            try:
                data = await _post_json(client, "/api/enqueue",
                                        {"text": "https://instagram.com/x"})
            finally:
                await client.close()
        self.assertEqual(data["routed"], "instagram")

    async def test_bili_text_routes_to_bili(self):
        from tg_userbot import bili_dl
        with mock.patch.object(api_server.platform,
                               "extract_douyin_urls", return_value=[]), \
             mock.patch.object(api_server.platform,
                               "extract_instagram_urls", return_value=[]), \
             mock.patch.object(bili_dl, "looks_like_bili", return_value=True), \
             mock.patch.object(bili_dl, "auto_reply",
                               new=mock.AsyncMock(return_value="B站已受理")):
            client = make_client()
            await client.start_server()
            try:
                data = await _post_json(
                    client, "/api/enqueue",
                    {"text": "https://b23.tv/abc"})
            finally:
                await client.close()
        self.assertTrue(data["ok"])
        self.assertEqual(data["routed"], "bili")
        self.assertEqual(data["message"], "B站已受理")

    async def test_torrent_text_routes_to_offline(self):
        from tg_userbot import manual_links, torrent_offline
        with mock.patch.object(api_server.platform,
                               "extract_douyin_urls", return_value=[]), \
             mock.patch.object(api_server.platform,
                               "extract_instagram_urls", return_value=[]), \
             mock.patch.object(manual_links, "extract_urls",
                               return_value=["https://host/a.torrent"]), \
             mock.patch.object(torrent_offline, "is_torrent_url",
                               return_value=True), \
             mock.patch.object(torrent_offline, "trailing_author",
                               return_value=None), \
             mock.patch.object(torrent_offline, "handle_torrent_url",
                               new=mock.AsyncMock(return_value="🧲 已提交离线")):
            client = make_client()
            await client.start_server()
            try:
                data = await _post_json(
                    client, "/api/enqueue",
                    {"text": "https://host/a.torrent"})
            finally:
                await client.close()
        self.assertEqual(data["routed"], "torrent")
        self.assertEqual(data["message"], "🧲 已提交离线")

    async def test_plain_url_falls_to_ledger(self):
        # example.com 不命中抖音/B站/种子的任何识别 → 外链台账（与收藏夹
        # 发这条消息的行为一致）
        from tg_userbot import manual_links
        with mock.patch.object(manual_links, "observe",
                               return_value=("🔗 已登记外链台账", None)) as obs:
            client = make_client()
            await client.start_server()
            try:
                data = await _post_json(
                    client, "/api/enqueue",
                    {"text": "看看这个 https://example.com/page"})
            finally:
                await client.close()
        self.assertEqual(data["routed"], "ledger")
        self.assertEqual(data["message"], "🔗 已登记外链台账")
        self.assertIn("https://example.com/page", obs.call_args[0][0])

    async def test_text_without_any_url_rejected(self):
        client = make_client()
        await client.start_server()
        try:
            data = await _post_json(client, "/api/enqueue",
                                    {"text": "只是一句话"})
        finally:
            await client.close()
        self.assertFalse(data["ok"])

    async def test_missing_text_rejected(self):
        client = make_client()
        await client.start_server()
        try:
            resp = await client.post("/api/enqueue", data=json.dumps({}))
            self.assertEqual(resp.status, 400)
        finally:
            await client.close()


class ActionTest(unittest.IsolatedAsyncioTestCase):
    """/api/action 的白名单语义。"""

    async def test_unknown_action_404(self):
        client = make_client()
        await client.start_server()
        try:
            resp = await client.post("/api/action/format_disk")
            self.assertEqual(resp.status, 404)
        finally:
            await client.close()

    async def test_clean_action_calls_cleanup(self):
        from tg_userbot import cleanup
        with mock.patch.object(cleanup, "clean_temp_files", return_value=3):
            client = make_client()
            await client.start_server()
            try:
                data = await _post_json(client, "/api/action/clean", {})
            finally:
                await client.close()
        self.assertTrue(data["ok"])
        self.assertIn("3", data["message"])

    async def test_retry_all_action(self):
        from tg_userbot import queue
        with mock.patch.object(queue, "retry_all", return_value=(5, 1)):
            client = make_client()
            await client.start_server()
            try:
                data = await _post_json(client, "/api/action/retry_all", {})
            finally:
                await client.close()
        self.assertIn("5", data["message"])
        self.assertIn("1", data["message"])

    async def test_wl_scan_action(self):
        from tg_userbot import wl_scan
        with mock.patch.object(wl_scan, "scan_all",
                               new=mock.AsyncMock(return_value={"n": 1})), \
             mock.patch.object(wl_scan, "summary_text",
                               return_value="扫描完成"):
            client = make_client()
            await client.start_server()
            try:
                data = await _post_json(client, "/api/action/wl_scan", {})
            finally:
                await client.close()
        self.assertEqual(data["message"], "扫描完成")


class HistoryAndDashboardTest(unittest.IsolatedAsyncioTestCase):
    async def test_history_lines(self):
        from tg_userbot import history
        with mock.patch.object(history, "get_history_lines",
                               return_value=["a | b"]) as gh:
            client = make_client()
            await client.start_server()
            try:
                data = await (await client.get(
                    "/api/history?n=5")).json()
            finally:
                await client.close()
        self.assertEqual(data["lines"], ["a | b"])
        gh.assert_called_once_with(5, kind=None)
        # kind 过滤透传（渠道分类，2026-11-05）
        with mock.patch.object(history, "get_history_lines",
                               return_value=["x | B站"]) as gh2:
            client = make_client()
            await client.start_server()
            try:
                data = await (await client.get(
                    "/api/history?n=5&kind=B%E7%AB%99")).json()
            finally:
                await client.close()
        self.assertEqual(data["lines"], ["x | B站"])
        gh2.assert_called_once_with(5, kind="B站")

    async def test_dashboard_html_served(self):
        client = make_client()
        await client.start_server()
        try:
            resp = await client.get("/")
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])
            body = await resp.text()
            self.assertIn("TG Userbot", body)
        finally:
            await client.close()

    async def test_paw_schedule_text(self):
        """回填执行计划：薄封装 pawchive.backfill_schedule_text（同源）。"""
        from tg_userbot import pawchive
        with mock.patch.object(pawchive, "backfill_schedule_text",
                               return_value="🐾 排期…") as bs:
            client = make_client()
            await client.start_server()
            try:
                data = await (await client.get("/api/paw_schedule")).json()
            finally:
                await client.close()
        self.assertTrue(data["ok"])
        self.assertEqual(data["text"], "🐾 排期…")
        bs.assert_called_once_with()

    async def test_paw_schedule_error_500(self):
        """渲染异常如实回 500 + 错误文案，不静默吞掉。"""
        from tg_userbot import pawchive
        with mock.patch.object(pawchive, "backfill_schedule_text",
                               side_effect=RuntimeError("db炸了")):
            client = make_client()
            await client.start_server()
            try:
                resp = await client.get("/api/paw_schedule")
                self.assertEqual(resp.status, 500)
                data = await resp.json()
            finally:
                await client.close()
        self.assertFalse(data["ok"])
        self.assertIn("db炸了", data["error"])

    async def test_pastes_iwara_dedup_fallback(self):
        """iwara 粘贴状态兜底：任务查不到 → 按视频 ID 查既往成功（重复拦截）。"""
        from tg_userbot import iwara, api_server
        path = os.path.join(_TMP, "paste_log_test.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": 0, "text": "https://www.iwara.tv/video/wogdabc",
                "url": "https://www.iwara.tv/video/wogdabc",
                "routed": "iwara", "ref": None}) + "\n")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with mock.patch.object(api_server, "_PASTE_LOG_FILE", path), \
             mock.patch.object(iwara, "find_previous_success",
                               return_value={"filename": "xx.mp4",
                                             "notified_at": "2026-11-02 09:42"}):
            client = make_client()
            await client.start_server()
            try:
                data = await (await client.get("/api/pastes?n=10")).json()
            finally:
                await client.close()
        self.assertTrue(data["ok"])
        row = next(x for x in data["items"] if x.get("routed") == "iwara")
        self.assertIn("重复拦截", row["status"])
        self.assertIn("xx.mp4", row["status"])
        self.assertEqual(row["kind"], "done")


class StartApiGuardTest(unittest.IsolatedAsyncioTestCase):
    """start_api 的裸暴露防线：非 loopback + 无 token 必须拒绝启动。"""

    async def test_refuses_non_loopback_without_token(self):
        with mock.patch.object(api_server.web, "TCPSite") as site, \
             patch_token(""):
            await asyncio.wait_for(
                api_server.start_api(bind="0.0.0.0", port=8765), 5)
        site.assert_not_called()

    async def test_disabled_flag_short_circuits(self):
        with mock.patch.object(api_server.web, "TCPSite") as site, \
             mock.patch.object(config, "API_ENABLED", False):
            await asyncio.wait_for(
                api_server.start_api(bind="127.0.0.1", port=8765), 5)
        site.assert_not_called()


class AppRelayTest(unittest.IsolatedAsyncioTestCase):
    """Telegram 中转通道（套壳 app 分享）：🛰 标记 → dispatch_link_text。"""

    async def test_marker_never_collides_with_notification_prefixes(self):
        # 回归钉：中转标记若撞上通知前缀，bot 的通知会被 app 中转分支
        # 误当分享处理（app.py 在 classify 之前抢先 return）
        from tg_userbot import cleanup
        self.assertNotIn(api_server.APP_RELAY_MARKER,
                         cleanup.CLEAN_NOTIFICATION_PREFIXES)

    async def test_relay_success_dispatches_and_notifies(self):
        with mock.patch.object(api_server, "dispatch_link_text",
                               new=mock.AsyncMock(return_value={
                                   "ok": True, "routed": "ledger",
                                   "message": "已登记"})), \
             mock.patch.object(api_server.notify, "notify_user",
                               new=mock.AsyncMock()) as nfy:
            await asyncio.wait_for(
                api_server.handle_app_relay("https://example.com/x"), 5)
        nfy.assert_awaited_once()
        self.assertIn("已受理", nfy.await_args.args[0])

    async def test_relay_failure_notifies_and_never_raises(self):
        with mock.patch.object(api_server, "dispatch_link_text",
                               new=mock.AsyncMock(
                                   side_effect=RuntimeError("炸了"))), \
             mock.patch.object(api_server.notify, "notify_user",
                               new=mock.AsyncMock()) as nfy:
            # 不抛：后台任务绝不让异常外泄
            await asyncio.wait_for(
                api_server.handle_app_relay("https://example.com/x"), 5)
        nfy.assert_awaited_once()
        self.assertIn("失败", nfy.await_args.args[0])
        self.assertIn("炸了", nfy.await_args.args[0])

    async def test_relay_rejected_result_notifies(self):
        with mock.patch.object(api_server, "dispatch_link_text",
                               new=mock.AsyncMock(return_value={
                                   "ok": False, "routed": None,
                                   "message": "没有识别到可处理的链接"})), \
             mock.patch.object(api_server.notify, "notify_user",
                               new=mock.AsyncMock()) as nfy:
            await asyncio.wait_for(
                api_server.handle_app_relay("只是一句话"), 5)
        nfy.assert_awaited_once()
        self.assertIn("未受理", nfy.await_args.args[0])


class StatsFindChromeTest(unittest.IsolatedAsyncioTestCase):
    """文本视图端点：stats / find / chrome（全部 mock 底层渲染）。"""

    async def test_stats_days_and_text(self):
        with mock.patch.object(api_server.stats, "stats_text",
                               return_value="📊 台账正文") as st:
            client = make_client()
            await client.start_server()
            try:
                data = await (await client.get(
                    "/api/stats?days=3")).json()
            finally:
                await client.close()
        self.assertTrue(data["ok"])
        self.assertEqual(data["days"], 3)
        self.assertEqual(data["text"], "📊 台账正文")
        st.assert_called_once_with(3)

    async def test_stats_days_clamped_to_1_7(self):
        client = make_client()
        await client.start_server()
        try:
            with mock.patch.object(api_server.stats, "stats_text",
                                   return_value="x") as st:
                await (await client.get("/api/stats?days=99")).json()
                st.assert_called_once_with(7)
                st.reset_mock()
                await (await client.get("/api/stats?days=0")).json()
                st.assert_called_once_with(1)
        finally:
            await client.close()

    async def test_find_requires_two_chars(self):
        client = make_client()
        await client.start_server()
        try:
            resp = await client.get("/api/find?q=a")
            self.assertEqual(resp.status, 400)
        finally:
            await client.close()

    async def test_find_returns_text(self):
        from tg_userbot import finder
        with mock.patch.object(finder, "find_media",
                               return_value="🔍 查询结果 3 条"):
            client = make_client()
            await client.start_server()
            try:
                data = await (await client.get(
                    "/api/find?q=笑纳")).json()
            finally:
                await client.close()
        self.assertTrue(data["ok"])
        self.assertEqual(data["text"], "🔍 查询结果 3 条")

    async def test_chrome_view(self):
        from tg_userbot import chrome_client
        with mock.patch.object(chrome_client, "status_view",
                               new=mock.AsyncMock(return_value="🤖 视图")):
            client = make_client()
            await client.start_server()
            try:
                data = await (await client.get("/api/chrome")).json()
            finally:
                await client.close()
        self.assertEqual(data["text"], "🤖 视图")


class ActionV2Test(unittest.IsolatedAsyncioTestCase):
    """v2 扩充动作：开关/带参/慢操作，全部 mock 既有服务函数。"""

    async def test_dedup_on_off(self):
        from tg_userbot import dedup
        with mock.patch.object(dedup, "set_enabled",
                               return_value="🛡 去重已开启") as se:
            client = make_client()
            await client.start_server()
            try:
                data = await _post_json(client, "/api/action/dedup_on", {})
            finally:
                await client.close()
        self.assertTrue(data["ok"])
        se.assert_called_once_with(True)

    async def test_listen_off(self):
        from tg_userbot import listener
        with mock.patch.object(listener, "set_enabled") as se:
            client = make_client()
            await client.start_server()
            try:
                data = await _post_json(client, "/api/action/listen_off", {})
            finally:
                await client.close()
        se.assert_called_once_with(False)
        self.assertIn("关闭", data["message"])

    async def test_thread_with_n(self):
        from tg_userbot import thread
        with mock.patch.object(thread, "apply_thread_limit",
                               return_value=(True, "并发已设为 5")) as tl:
            client = make_client()
            await client.start_server()
            try:
                data = await _post_json(client, "/api/action/thread",
                                        {"n": 5})
            finally:
                await client.close()
        tl.assert_called_once_with("5")
        self.assertTrue(data["ok"])

    async def test_chrome_cancel_requires_id(self):
        client = make_client()
        await client.start_server()
        try:
            resp = await client.post("/api/action/chrome_cancel",
                                     data=json.dumps({}))
            self.assertEqual(resp.status, 400)
        finally:
            await client.close()

    async def test_chrome_cancel_with_id(self):
        from tg_userbot import chrome_client
        with mock.patch.object(chrome_client, "request_cancel",
                               return_value=(True, "🛑 已取消")) as rc:
            client = make_client()
            await client.start_server()
            try:
                data = await _post_json(client, "/api/action/chrome_cancel",
                                        {"id": "abc123"})
            finally:
                await client.close()
        rc.assert_called_once_with("abc123")
        self.assertEqual(data["message"], "🛑 已取消")

    async def test_paw_pause_and_cd2_start(self):
        from tg_userbot import cd2, pawchive
        with mock.patch.object(pawchive, "pause_reply",
                               return_value="⏸ 已暂停"), \
             mock.patch.object(cd2, "cd2_start_or_status",
                               new=mock.AsyncMock(return_value="✅ CD2 已启动")):
            client = make_client()
            await client.start_server()
            try:
                d1 = await _post_json(client, "/api/action/paw_pause", {})
                d2 = await _post_json(client, "/api/action/cd2_start", {})
            finally:
                await client.close()
        self.assertIn("已暂停", d1["message"])
        self.assertIn("CD2", d2["message"])

    async def test_health_and_now(self):
        from tg_userbot import health
        with mock.patch.object(health, "render_health",
                               return_value="🟢 正常"), \
             mock.patch.object(health, "render_now",
                               return_value="ℹ️ 现在空闲"):
            client = make_client()
            await client.start_server()
            try:
                d1 = await _post_json(client, "/api/action/health", {})
                d2 = await _post_json(client, "/api/action/now", {})
            finally:
                await client.close()
        self.assertEqual(d1["message"], "🟢 正常")
        self.assertEqual(d2["message"], "ℹ️ 现在空闲")

    async def test_action_exception_returned_as_500_json(self):
        from tg_userbot import dedup
        with mock.patch.object(dedup, "set_enabled",
                               side_effect=RuntimeError("炸了")):
            client = make_client()
            await client.start_server()
            try:
                resp = await client.post("/api/action/dedup_on",
                                         data=json.dumps({}))
                self.assertEqual(resp.status, 500)
                data = await resp.json()
            finally:
                await client.close()
        self.assertFalse(data["ok"])
        # 2026-11-05 审计修复：内部异常细节不再外泄（原断言 assertIn("炸了")）
        self.assertIn("详见服务端日志", data["error"])
        self.assertNotIn("炸了", data["error"])


class RateLimitTest(unittest.TestCase):
    """非 loopback 限流（120/min）+ 回环豁免。"""

    def setUp(self):
        api_server._rate_hits.clear()

    def test_loopback_always_allowed(self):
        for i in range(1000):
            self.assertTrue(api_server._rate_allow("127.0.0.1", now=i * 0.1))

    def test_window_limit_and_slide(self):
        t0 = 1000.0
        for i in range(120):
            self.assertTrue(api_server._rate_allow("1.2.3.4", now=t0 + i * 0.1))
        self.assertFalse(api_server._rate_allow("1.2.3.4", now=t0 + 12))
        # 61s 后窗口整体滑出 → 恢复放行
        self.assertTrue(api_server._rate_allow("1.2.3.4", now=t0 + 61))


class CommandCenterTest(unittest.IsolatedAsyncioTestCase):
    """指令中心：注册表分组 + 白名单无头执行（危险指令排除）。"""

    async def test_registry_groups_and_excludes_danger(self):
        client = make_client()
        await client.start_server()
        try:
            data = await (await client.get("/api/commands")).json()
        finally:
            await client.close()
        self.assertTrue(data["ok"])
        names = {i["cmd"] for g in data["groups"] for i in g["items"]}
        self.assertIn("/stats", names)
        self.assertIn("/paw_plan", names)
        self.assertIn("/listen_add", names)
        self.assertNotIn("/sh", names)
        self.assertNotIn("/sql", names)
        self.assertNotIn("/restart", names)
        for bad in ("sh", "sql", "sqlt", "restart", "up", "paw_cookie"):
            self.assertIn(bad, data["excluded"])
        # 组顺序按 _GROUP_ORDER（有内容的组）
        self.assertEqual(data["groups"][0]["name"], "📊 监控与查询")

    async def test_cmd_rejects_dangerous(self):
        client = make_client()
        await client.start_server()
        try:
            resp = await client.post("/api/cmd",
                                     data=json.dumps({"cmd": "/sql select 1"}))
            self.assertEqual(resp.status, 403)
            data = await resp.json()
        finally:
            await client.close()
        self.assertIn("不开放", data["error"])

    async def test_cmd_rejects_unknown(self):
        client = make_client()
        await client.start_server()
        try:
            resp = await client.post("/api/cmd",
                                     data=json.dumps({"cmd": "/nope"}))
            self.assertEqual(resp.status, 404)
        finally:
            await client.close()

    async def test_cmd_requires_slash(self):
        client = make_client()
        await client.start_server()
        try:
            resp = await client.post("/api/cmd",
                                     data=json.dumps({"cmd": "stats"}))
            self.assertEqual(resp.status, 400)
        finally:
            await client.close()

    async def test_cmd_executes_via_handle_command(self):
        from tg_userbot import commands

        async def fake_handle(ev, text):
            ev.replies.append("📊 台账结果")
            return True

        with mock.patch.object(commands, "handle_command",
                               new=mock.AsyncMock(side_effect=fake_handle)) as h:
            client = make_client()
            await client.start_server()
            try:
                data = await _post_json(client, "/api/cmd",
                                        {"cmd": "/stats"})
            finally:
                await client.close()
        self.assertTrue(data["ok"])
        self.assertEqual(data["text"], "📊 台账结果")
        h.assert_awaited_once()
        ev, text = h.await_args.args
        self.assertEqual(text, "/stats")
        self.assertEqual(ev.replies, ["📊 台账结果"])
        self.assertIsNone(ev.chat_id)  # cmdq 台账自动跳过

    async def test_cmd_not_handled_returns_400(self):
        from tg_userbot import commands
        with mock.patch.object(commands, "handle_command",
                               new=mock.AsyncMock(return_value=False)):
            client = make_client()
            await client.start_server()
            try:
                resp = await client.post("/api/cmd",
                                         data=json.dumps({"cmd": "/stats"}))
                self.assertEqual(resp.status, 400)
            finally:
                await client.close()


class BackupViewTest(unittest.IsolatedAsyncioTestCase):
    """/api/backup：CD2 实时上传视图（gRPC 下放线程，全 mock）。"""

    async def test_backup_rows_and_counts(self):
        from tg_userbot import cd2_api
        files = [{"name": "a.mp4", "pct": 42, "status": "Uploading",
                  "dest": "/115open/Nekogram/a.mp4"},
                 {"name": "b.zip", "pct": 0, "status": "Error",
                  "dest": "/115open/Nekogram/b.zip"}]
        with mock.patch.object(cd2_api, "upload_files_summary",
                               return_value=(2, files)), \
             mock.patch.object(cd2_api, "tasks_count",
                               return_value={"download": 1, "upload": 2,
                                             "copy": 0}):
            client = make_client()
            await client.start_server()
            try:
                data = await (await client.get("/api/backup")).json()
            finally:
                await client.close()
        self.assertTrue(data["ok"])
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["counts"]["upload"], 2)
        self.assertEqual(len(data["tasks"]), 2)
        self.assertEqual(data["tasks"][0]["pct"], 42)

    async def test_backup_cd2_down_returns_friendly_error(self):
        from tg_userbot import cd2_api
        with mock.patch.object(cd2_api, "upload_files_summary",
                               side_effect=RuntimeError("连接被拒")):
            client = make_client()
            await client.start_server()
            try:
                data = await (await client.get("/api/backup")).json()
            finally:
                await client.close()
        self.assertFalse(data["ok"])
        self.assertIn("CD2 不可达", data["error"])


async def _post_json(client, path, body):
    resp = await client.post(path, data=json.dumps(body))
    return await resp.json()


if __name__ == "__main__":
    unittest.main()


async def _fake_chrome_cmd(ev, cmd_text, owner_id=None, sender_id=None):
    ev.replies.append("🤖 Chrome 下载任务已提交")
    return True


class IwaraRoutingTest(unittest.IsolatedAsyncioTestCase):
    """iwara 链接路由：enqueue/cmd 直达 Chrome Agent 下载（bot 对话同语义）。"""

    def setUp(self):
        # iwara 判重与 Chrome 提交全 mock（不联网、不动 Chrome）
        patches = [
            mock.patch("tg_userbot.iwara.extract_iwara_video_id",
                       return_value="vid123"),
            mock.patch("tg_userbot.iwara.find_previous_success",
                       return_value=None),
            mock.patch("tg_userbot.chrome_client.handle_chrome_command",
                       new=mock.AsyncMock(side_effect=_fake_chrome_cmd)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # dispatch_link_text 内部 lazy import iwara/chrome_client——
        # patch 的是模块属性，lazy import 拿到同一模块对象 ✓

    async def test_enqueue_iwara_url_routes_to_chrome(self):
        client = make_client()
        await client.start_server()
        try:
            data = await _post_json(client, "/api/enqueue",
                                    {"text": "看这个 https://iwara.tv/video/abc123"})
        finally:
            await client.close()
        self.assertEqual(data["routed"], "iwara")
        from tg_userbot import chrome_client
        handle = chrome_client.handle_chrome_command
        handle.assert_awaited_once()
        ev, cmd = handle.await_args.args
        self.assertTrue(cmd.startswith("/chrome https://iwara.tv/video/abc123"))
        self.assertIn("已提交", data["message"])

    async def test_cmd_bare_url_dispatches(self):
        # 万能输入直接贴链接（非 / 开头）→ 链接分流而非 400
        client = make_client()
        await client.start_server()
        try:
            data = await _post_json(client, "/api/cmd",
                                    {"cmd": "https://iwara.tv/video/xyz"})
        finally:
            await client.close()
        self.assertEqual(data["routed"], "iwara")

    async def test_cmd_text_without_link_or_slash_still_400(self):
        client = make_client()
        await client.start_server()
        try:
            resp = await client.post("/api/cmd",
                                     data=json.dumps({"cmd": "随便说说"}))
            self.assertEqual(resp.status, 400)
        finally:
            await client.close()

    async def test_iwara_duplicate_intercepted(self):
        from tg_userbot import iwara
        with mock.patch.object(iwara, "find_previous_success",
                               return_value={"notified_at": "2026-10-01 10:00"}), \
             mock.patch.object(iwara, "duplicate_text",
                               return_value="⏭️ 已成功过，不重复下载"):
            client = make_client()
            await client.start_server()
            try:
                data = await _post_json(client, "/api/enqueue",
                                        {"text": "https://iwara.tv/video/old"})
            finally:
                await client.close()
        self.assertEqual(data["routed"], "iwara_dup")
        self.assertIn("不重复", data["message"])

    async def test_status_carries_chrome_progress(self):
        from tg_userbot import chrome_client
        with mock.patch.object(chrome_client, "read_progress",
                               return_value={"updated_at": 1,
                                             "items": [{"received": 10,
                                                        "total": 100}]}):
            client = make_client()
            await client.start_server()
            try:
                data = await (await client.get("/api/status")).json()
            finally:
                await client.close()
        self.assertEqual(data["chrome_progress"]["items"][0]["received"], 10)


class PasteHistoryTest(unittest.IsolatedAsyncioTestCase):
    """粘贴历史 + 状态回查（事件流/chrome/台账三真相源，全 mock）。"""

    def setUp(self):
        try:
            os.remove(api_server._PASTE_LOG_FILE)
        except OSError:
            pass

    def tearDown(self):
        try:
            os.remove(api_server._PASTE_LOG_FILE)
        except OSError:
            pass

    def test_log_and_read_newest_first(self):
        api_server._log_paste("t1 https://a.com/1", "https://a.com/1", "ledger", None)
        api_server._log_paste("t2 https://b.com/2", "https://b.com/2", "iwara", "task123")
        items = api_server._read_paste_log(10)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["routed"], "iwara")   # 新→旧
        self.assertEqual(items[0]["ref"], "task123")

    async def test_pastes_endpoint_resolves_statuses(self):
        from tg_userbot import chrome_client, stats
        api_server._log_paste("iwara 文本", "https://iwara.tv/video/1", "iwara", "TASK1")
        api_server._log_paste("douyin 文本", "https://v.douyin.com/1", "douyin", "DOUYIN1")
        api_server._log_paste("台账 文本", "https://example.com/p1", "ledger", None)
        with mock.patch.object(chrome_agent, "load_tasks",
                               return_value=[{"task_id": "TASK1",
                                              "status": "RUNNING",
                                              "filename": "x.mp4"}]), \
             mock.patch.object(stats, "load_events",
                               return_value=[{"task_id": "DOUYIN1",
                                              "ev": "RUNNING"}]), \
             mock.patch.object(runtime_db_mod, "search_manual_links",
                               return_value=[{"id": 7, "status": "PENDING",
                                              "url": "https://example.com/p1",
                                              "host": "example.com", "note": "n"}]):
            client = make_client()
            await client.start_server()
            try:
                data = await (await client.get("/api/pastes?n=10")).json()
            finally:
                await client.close()
        by_routed = {i["routed"]: i for i in data["items"]}
        self.assertIn("下载中", by_routed["iwara"]["status"])
        self.assertEqual(by_routed["iwara"]["kind"], "run")
        self.assertIn("下载中", by_routed["douyin"]["status"])
        self.assertEqual(by_routed["ledger"]["status"], "👤 待人工处理")
        self.assertEqual(by_routed["ledger"]["kind"], "pending")

    async def test_ledger_branch_logs_paste(self):
        # enqueue 台账分支 → 粘贴日志落一条 routed=ledger
        # （密闭：pawchive 查重与台账写入全 mock——机器高负载下 SQLite
        #   busy 会让真库路径抖动，本用例只验证分支接了 _log_paste）
        from tg_userbot import pawchive, runtime_db
        with mock.patch.object(pawchive, "find_completed_ext_link",
                               return_value=None), \
             mock.patch.object(pawchive, "find_manual_ext_link",
                               return_value=None), \
             mock.patch.object(runtime_db, "manual_link_add",
                               return_value=("pending", {
                                   "id": 5, "url": "https://example.com/log1",
                                   "host": "example.com", "note": "备注",
                                   "status": "PENDING"})):
            client = make_client()
            await client.start_server()
            try:
                data = await _post_json(client, "/api/enqueue",
                                        {"text": "备注 https://example.com/log1"})
            finally:
                await client.close()
        self.assertEqual(data["routed"], "ledger")
        items = api_server._read_paste_log(10)
        self.assertTrue(any(i["routed"] == "ledger" and
                            i["url"] == "https://example.com/log1"
                            for i in items))


class LinksEndpointsTest(unittest.IsolatedAsyncioTestCase):
    """/api/links（待人工清单）+ /api/links_done（幂等标记完成）。"""

    async def test_links_lists_pending(self):
        from tg_userbot import runtime_db
        with mock.patch.object(runtime_db, "list_manual_links",
                               return_value=[{"id": 3, "url": "https://x.com/a",
                                              "host": "x.com", "note": "备注"}]), \
             mock.patch.object(runtime_db, "count_manual_links",
                               return_value=57):
            client = make_client()
            await client.start_server()
            try:
                data = await (await client.get("/api/links")).json()
            finally:
                await client.close()
        self.assertEqual(data["done_count"], 57)
        self.assertEqual(data["pending"][0]["id"], 3)
        self.assertEqual(data["pending"][0]["note"], "备注")

    async def test_links_done_calls_runtime_db(self):
        from tg_userbot import runtime_db
        with mock.patch.object(runtime_db, "manual_link_done",
                               return_value=True) as md:
            client = make_client()
            await client.start_server()
            try:
                data = await _post_json(client, "/api/links_done", {"id": 9})
            finally:
                await client.close()
        md.assert_called_once_with(9)
        self.assertTrue(data["ok"])

    async def test_links_done_requires_numeric_id(self):
        client = make_client()
        await client.start_server()
        try:
            resp = await client.post("/api/links_done",
                                     data=json.dumps({"id": "abc"}))
            self.assertEqual(resp.status, 400)
        finally:
            await client.close()


class PwaAssetsTest(unittest.IsolatedAsyncioTestCase):
    """PWA 资产路由：manifest/SW/图标/分享页——免鉴权可取（安装与分享面板不带令牌）。"""

    async def test_manifest_served_without_auth(self):
        client = make_client()
        await client.start_server()
        try:
            resp = await client.get("/manifest.json")
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["name"], "TG Userbot 控制台")
            self.assertIn("share_target", data)
            self.assertEqual(data["share_target"]["action"], "/share")
        finally:
            await client.close()

    async def test_sw_and_icons_served(self):
        client = make_client()
        await client.start_server()
        try:
            sw = await client.get("/sw.js")
            self.assertEqual(sw.status, 200)
            self.assertIn("javascript", sw.headers["Content-Type"])
            icon = await client.get("/icons/icon-192.png")
            self.assertEqual(icon.status, 200)
            self.assertEqual(icon.headers["Content-Type"], "image/png")
            body = await icon.read()
            self.assertEqual(body[:4], b"\x89PNG")
        finally:
            await client.close()

    async def test_share_page_renders(self):
        client = make_client()
        await client.start_server()
        try:
            resp = await client.get("/share?text=hello&url=https://x.com/a")
            self.assertEqual(resp.status, 200)
            body = await resp.text()
            self.assertIn("分享到下载器", body)
            self.assertIn("加入下载队列", body)
        finally:
            await client.close()


class PawBackfillEndpointTest(unittest.IsolatedAsyncioTestCase):
    """/api/paw_backfill：三段式数据源（当前/下一批/历史）全 mock。"""

    async def test_overview_shape(self):
        from tg_userbot import pawchive
        overview = {
            "current": {"cursor": 1, "total_creators": 38,
                        "loop_started": "11-02 00:00", "full_round_h": 72,
                        "per_round": 3, "wake_hours": 6},
            "next_batch": [{"name": "作者A", "posts": 20,
                            "eta": "11-03 00:00", "overdue": True}],
            "history": [{"ts": 1793800000, "author_count": 3,
                         "summary": "🐾 本轮 3 作者", "cursor": 1}],
        }
        with mock.patch.object(pawchive, "backfill_status_overview",
                               return_value=overview):
            client = make_client()
            await client.start_server()
            try:
                data = await (await client.get("/api/paw_backfill")).json()
            finally:
                await client.close()
        self.assertTrue(data["ok"])
        self.assertEqual(data["current"]["total_creators"], 38)
        self.assertEqual(data["next_batch"][0]["name"], "作者A")
        self.assertEqual(len(data["history"]), 1)

    async def test_overview_error_500(self):
        from tg_userbot import pawchive
        with mock.patch.object(pawchive, "backfill_status_overview",
                               side_effect=RuntimeError("炸了")):
            client = make_client()
            await client.start_server()
            try:
                resp = await client.get("/api/paw_backfill")
                self.assertEqual(resp.status, 500)
                data = await resp.json()
            finally:
                await client.close()
        self.assertIn("炸了", data["error"])

    def test_history_log_and_read(self):
        d = tempfile.mkdtemp()
        orig = pawchive.PAWB_HISTORY_FILE
        pawchive.PAWB_HISTORY_FILE = os.path.join(d, "h.jsonl")
        try:
            pawchive.log_backfill_round(3, "本轮 3 作者", 4)
            pawchive.log_backfill_round(1, "让位人工扫描", 4)
            items = pawchive.read_backfill_history(10)
            self.assertEqual(len(items), 2)
            self.assertEqual(items[0]["summary"], "让位人工扫描")  # 新→旧
            self.assertEqual(items[1]["author_count"], 3)
        finally:
            pawchive.PAWB_HISTORY_FILE = orig
            shutil.rmtree(d, ignore_errors=True)


class PawBackfillDeferredTest(unittest.TestCase):
    """顺延判定与重启快醒：状态概览的 last_run_at/deferred 语义。"""

    def _overview(self, history, overdue_first=True):
        d = tempfile.mkdtemp(dir=_TMP)
        orig_file = pawchive.PAWB_HISTORY_FILE
        pawchive.PAWB_HISTORY_FILE = os.path.join(d, "h.jsonl")
        try:
            for ts, summary in history:
                pawchive.log_backfill_round(1, summary, 0)
                # 覆写 ts 为指定值（log 用当前时间）
                lines = open(pawchive.PAWB_HISTORY_FILE,
                             encoding="utf-8").readlines()
                e = json.loads(lines[-1])
                e["ts"] = ts
                lines[-1] = json.dumps(e, ensure_ascii=False) + "\n"
                open(pawchive.PAWB_HISTORY_FILE, "w",
                     encoding="utf-8").writelines(lines)
            with mock.patch.object(pawchive, "list_scanned_creators",
                                   return_value=[{"creator_name": "A",
                                                  "posts": 5}]), \
                 mock.patch.object(
                     pawchive, "_load_pawb_state",
                     return_value={"cursor": 0,
                                   # loop started 13h ago: first wake
                                   # (6h) already overdue
                                   "loop_started": 1793900000.0
                                   - 13 * 3600}), \
                 mock.patch.object(pawchive.time, "time",
                                   return_value=1793900000.0):
                return pawchive.backfill_status_overview()
        finally:
            pawchive.PAWB_HISTORY_FILE = orig_file
            shutil.rmtree(d, ignore_errors=True)

    def test_deferred_when_overdue_and_no_recent_run(self):
        # 首批逾期 7h，且历史里最近执行也在 7h 前 → deferred=True
        now = 1793900000.0
        ov = self._overview([(now - 7 * 3600, "旧一轮")])
        self.assertTrue(ov["current"]["deferred"])
        self.assertTrue(ov["current"]["last_run_at"])

    def test_not_deferred_when_recently_ran(self):
        # 6h 内有实际执行 → 不算顺延（正常节奏内）
        now = 1793900000.0
        ov = self._overview([(now - 2 * 3600, "刚跑过一轮")])
        self.assertFalse(ov["current"]["deferred"])

    def test_not_deferred_when_not_overdue(self):
        # 预计时间在未来（next_batch overdue=False）→ 不算顺延
        now = 1793900000.0
        ov = self._overview([(now - 30 * 60, "刚跑过一轮")])
        # _load_pawb_state loop_started=0 → started 兜底 now（被 mock 为
        # 固定值）→ eta = now+6h，未逾期
        self.assertFalse(ov["current"]["deferred"])


class TmePasteStatusTest(unittest.TestCase):
    """t.me 粘贴历史状态回查：ref 定位队列任务 + 无 ref 兼容。"""

    def test_resolve_via_queue_record(self):
        import json as _json
        from tg_userbot import stats
        pairs = [[-1001234, 500]]
        ref = _json.dumps(pairs)
        queue = {"tasks": [], "retry": [{
            "id": "abc123", "origin_chat_id": -1001234,
            "origin_msg_id": 500, "final_name": "24-11-03 帖子视频.mp4"}]}
        events = [{"task_id": "abc123", "ev": "RUNNING"}]
        with mock.patch.object(state, "QUEUE", queue), \
             mock.patch.object(api_server.stats, "load_events",
                               return_value=events):
            text, kind = api_server._paste_status(
                {"routed": "tme", "ref": ref, "url": "https://t.me/x/500"})
        self.assertEqual(kind, "run")
        self.assertIn("下载中", text)
        self.assertIn("帖子视频", text)

    def test_completed_task_reports_done(self):
        import json as _json
        ref = _json.dumps([[-1001234, 500]])
        queue = {"tasks": [], "retry": []}   # 已完成出队
        events = [{"task_id": "abc", "ev": "SUCCESS", "label": "视频.mp4"}]
        with mock.patch.object(state, "QUEUE", queue), \
             mock.patch.object(api_server.stats, "load_events",
                               return_value=events):
            text, kind = api_server._paste_status(
                {"routed": "tme", "ref": ref, "url": "https://t.me/x/500"})
        self.assertEqual(kind, "done")

    def test_no_ref_static_fallback(self):
        text, kind = api_server._paste_status(
            {"routed": "tme", "ref": None, "url": "https://t.me/x/1"})
        self.assertEqual(kind, "run")
        self.assertIn("已入队", text)

    def test_enqueue_callback_collects_pairs(self):
        # tme 分支：入队回调把 (chat_id, msg_id) 收进 ref（JSON）
        import asyncio
        from tg_userbot import tme as tme_mod
        collected = []

        async def fake_enqueue(msg):
            collected.append(msg)

        async def fake_fetcher(ref, ids=None):
            class M:
                id = 500
                chat_id = -1001234
                grouped_id = None
            return M()

        # fetcher 签名是 (ref, ids=...)——handle_tme_url 用 ids=mid 调用；
        # 上面定义已匹配。失败原因：无 grouped_id 时走单条路，ok。

        # is_downloadable 需要 True——mock 掉
        with mock.patch("tg_userbot.sources.is_downloadable",
                        return_value=True), \
             mock.patch.object(tme_mod, "_channel_title",
                               return_value="测试频道"):
            out = asyncio.run(tme_mod.handle_tme_url(
                "https://t.me/channame/500", fetcher=fake_fetcher,
                enqueue=fake_enqueue, on_enqueued=collected.append))
        self.assertIn("已入队", out)
        # enqueue 与 on_enqueued 各触发一次（同一条 msg）→ collected 2 条
        self.assertEqual(len(collected), 2)
        self.assertEqual(collected[0].chat_id, -1001234)


class QueuesAggregateTest(unittest.IsolatedAsyncioTestCase):
    """/api/queues 聚合：各域摘要 + 单域降级不影响其它。"""

    async def test_aggregate_shape_and_degradation(self):
        client = make_client()
        await client.start_server()
        try:
            resp = await client.get("/api/queues")
            data = await resp.json()
            self.assertEqual(resp.status, 200)
            self.assertTrue(data["ok"])
            names = [q["name"] for q in data["queues"]]
            self.assertIn("媒体队列", names)
            self.assertIn("B站", names)
            self.assertIn("wnacg 漫画", names)
            # 每个域要么带计数要么带 error（降级不崩）
            for q in data["queues"]:
                self.assertTrue("error" in q or "pending" in q)
        finally:
            await client.close()


class EnqueueXRouteTest(unittest.IsolatedAsyncioTestCase):
    """/api/enqueue 的 X 分流（2026-11-06 补——看板粘贴框此前漏 X
    落外链台账；与收藏夹/bot 路由同语义）。"""

    async def test_x_link_routes_to_x_queue(self):
        from tg_userbot import runtime_db
        self._td = tempfile.mkdtemp(dir=_TMP)
        self._saved_conn = runtime_db._CONN
        runtime_db._CONN = None
        runtime_db.init_db(os.path.join(self._td, "tg.db"))
        self.addCleanup(lambda: (
            runtime_db.close_db(),
            setattr(runtime_db, "_CONN", self._saved_conn),
            shutil.rmtree(self._td, ignore_errors=True),
        ))
        client = make_client()
        await client.start_server()
        try:
            resp = await client.post(
                "/api/enqueue",
                data=json.dumps({"text": "https://x.com/DaymanDraws/status/2118073958474158364"}),
            )
            data = await resp.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["routed"], "x")
            self.assertIn("已入队", data["message"])
            row = runtime_db.XN_TASKS.get(
                "https://x.com/DaymanDraws/status/2118073958474158364")
            self.assertIsNotNone(row)
        finally:
            await client.close()


class FileManagerTest(unittest.IsolatedAsyncioTestCase):
    """/api/fs/*：数据盘护栏 + 列表/重命名/粘贴。"""

    runtime_db = runtime_db_mod   # 类属性（setUp 里实例重绑也走它）

    def setUp(self):
        from tg_userbot import runtime_db as _rt
        self._td = tempfile.mkdtemp(dir=_TMP)
        self._saved_conn = _rt._CONN
        _rt._CONN = None
        _rt.init_db(os.path.join(self._td, "tg.db"))
        runtime_db = _rt
        self._old_root = config.DATA_ROOT
        config.DATA_ROOT = os.path.join(self._td, "v1")
        os.makedirs(config.DATA_ROOT)
        os.makedirs(os.path.join(config.DATA_ROOT, "sub"))
        with open(os.path.join(config.DATA_ROOT, "sub", "a.txt"), "w") as f:
            f.write("hello")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        rt = self.runtime_db
        try:
            rt.close_db()
        except Exception:
            pass
        rt._CONN = self._saved_conn
        config.DATA_ROOT = self._old_root
        shutil.rmtree(self._td, ignore_errors=True)

    async def test_list_and_rename(self):
        client = make_client()
        await client.start_server()
        try:
            r = await client.get("/api/fs/list?path=" +
                                 config.DATA_ROOT.replace("\\", "/"))
            d = await r.json()
            self.assertTrue(d["ok"])
            names = [x["name"] for x in d["items"]]
            self.assertIn("sub", names)
            # 重命名
            r2 = await client.post("/api/fs/rename", data=json.dumps(
                {"path": config.DATA_ROOT + "/sub/a.txt",
                 "new_name": "b.txt"}))
            self.assertTrue((await r2.json())["ok"])
            self.assertTrue(os.path.exists(
                os.path.join(config.DATA_ROOT, "sub", "b.txt")))
        finally:
            await client.close()

    async def test_traversal_rejected(self):
        client = make_client()
        await client.start_server()
        try:
            r = await client.get(
                "/api/fs/list?path=/etc")
            d = await r.json()
            self.assertFalse(d["ok"])
            r2 = await client.post("/api/fs/rename", data=json.dumps(
                {"path": "/etc/hosts", "new_name": "x"}))
            self.assertFalse((await r2.json())["ok"])
        finally:
            await client.close()

    async def test_paste_copy_in_disk(self):
        client = make_client()
        await client.start_server()
        try:
            r = await client.post("/api/fs/paste", data=json.dumps(
                {"dest": config.DATA_ROOT,
                 "srcs": [config.DATA_ROOT + "/sub/a.txt"],
                 "move": False}))
            d = await r.json()
            self.assertTrue(d["ok"], d)
            for _ in range(30):
                if not json.loads((await (
                        await client.get("/api/fs/op")).text()))["running"]:
                    break
                await asyncio.sleep(0.1)
            self.assertTrue(os.path.exists(
                os.path.join(config.DATA_ROOT, "a.txt")))
        finally:
            await client.close()
