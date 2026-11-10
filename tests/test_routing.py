"""链接分流单点（routing.py）测试。

背景：链接进入系统有三个文本入口（收藏夹 / bot 对话 / 看板粘贴框），
分流顺序与参数构造此前各写一份，已实际漏过（iwara 只挂了 bot 与 API，
收藏夹掉进外链台账；X 只挂收藏夹，看板掉台账）。本文件锁住单一实现：
``classify`` 负责「识别 + 优先级 + 每路参数」，``execute`` 负责「调哪
个服务入口」。各入口只保留自己的交付方式（回帖 / 面板发送 / JSON）。
"""
import atexit
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_routing_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import routing  # noqa: E402


DOUYIN = "https://v.douyin.com/abc123/"
INSTAGRAM = "https://www.instagram.com/p/ABC123/"
TORRENT = "https://example.com/file.torrent"
IWARA = "https://www.iwara.tv/video/abcd1234"
BILI = "https://www.bilibili.com/video/BV1xx411c7mD"
X_URL = "https://x.com/someone/status/1234567890"
TME = "https://t.me/somechannel/12345"
MEGA = "https://mega.nz/file/abcdef"


class ClassifyTest(unittest.TestCase):
    def test_douyin(self):
        d = routing.classify(DOUYIN + " 复制打开抖音", allow=routing.SURFACE_ME)
        self.assertEqual(d.kind, "douyin")
        self.assertEqual(d.urls, (DOUYIN,))

    def test_instagram(self):
        d = routing.classify(INSTAGRAM, allow=routing.SURFACE_ME)
        self.assertEqual(d.kind, "instagram")
        self.assertEqual(d.urls, (INSTAGRAM,))

    def test_torrent_carries_folder_and_url(self):
        with mock.patch.object(routing.torrent_offline, "resolve_to_folder",
                               return_value="漫画/作者"):
            d = routing.classify(TORRENT + " 作者名",
                                 allow=routing.SURFACE_ME)
        self.assertEqual(d.kind, "torrent")
        self.assertEqual(d.urls, (TORRENT,))
        self.assertEqual(d.extra["to_folder"], "漫画/作者")

    def test_torrent_without_author_has_no_folder(self):
        d = routing.classify(TORRENT, allow=routing.SURFACE_ME)
        self.assertEqual(d.kind, "torrent")
        self.assertIsNone(d.extra["to_folder"])

    def test_iwara_routes_on_every_surface(self):
        """三个入口都接管 iwara（2026-11-08 统一，此前收藏夹掉外链台账）。"""
        for allow in (routing.SURFACE_ME, routing.SURFACE_BOT,
                      routing.SURFACE_API):
            self.assertEqual(routing.classify(IWARA, allow=allow).kind,
                             "iwara")

    def test_bili_wins_over_generic_link(self):
        d = routing.classify(f"{BILI} {MEGA}", allow=routing.SURFACE_ME)
        self.assertEqual(d.kind, "bili")

    def test_x(self):
        d = routing.classify(X_URL, allow=routing.SURFACE_ME)
        self.assertEqual(d.kind, "x")

    def test_tme_carries_stripped_label(self):
        d = routing.classify(f"{TME} 我的标注", allow=routing.SURFACE_ME)
        self.assertEqual(d.kind, "tme")
        self.assertEqual(d.urls, (TME,))
        self.assertEqual(d.extra["label"], "我的标注")

    def test_tme_routes_on_bot_surface(self):
        """bot 对话也接管 t.me（此前会掉进外链台账）。"""
        d = routing.classify(TME, allow=routing.SURFACE_BOT)
        self.assertEqual(d.kind, "tme")

    def test_douyin_and_instagram_route_on_bot_surface(self):
        """bot 对话也接管抖音/IG（此前会掉进外链台账）。"""
        self.assertEqual(routing.classify(
            DOUYIN, allow=routing.SURFACE_BOT).kind, "douyin")
        self.assertEqual(routing.classify(
            INSTAGRAM, allow=routing.SURFACE_BOT).kind, "instagram")

    def test_ledger_for_generic_link(self):
        d = routing.classify(MEGA, allow=routing.SURFACE_ME)
        self.assertEqual(d.kind, "ledger")
        self.assertEqual(d.urls, (MEGA,))

    def test_plain_text_is_none(self):
        d = routing.classify("今天天气不错", allow=routing.SURFACE_ME)
        self.assertEqual(d.kind, "none")
        self.assertFalse(d.handled)

    def test_me_surface_skips_ledger_for_forwarded_message(self):
        class Msg:
            fwd_from = object()

        d = routing.classify(MEGA, allow=routing.SURFACE_ME,
                             message=Msg(), skip_ledger_for_forwarded=True)
        self.assertEqual(d.kind, "none")
        self.assertEqual(d.extra.get("ledger_skipped"), "forwarded")

    def test_entities_supply_douyin_url(self):
        class Entity:
            url = DOUYIN

        class Msg:
            fwd_from = None

        # 文本里没有链接，只在 entity 里（Telegram WebPage 预览形态）
        d = routing.classify("看看这个", allow=routing.SURFACE_ME,
                             message=Msg(), entities=[Entity()])
        self.assertEqual(d.kind, "douyin")
        self.assertEqual(d.urls, (DOUYIN,))

    def test_autroute_off_falls_back_to_ledger(self):
        with mock.patch.object(routing.config, "AUTOROUTE_BILI", False):
            d = routing.classify(BILI, allow=routing.SURFACE_ME)
        self.assertEqual(d.kind, "ledger")

    def test_android_disables_desktop_routes(self):
        """Android 版没有 yt-dlp/gallery-dl/Chrome/CD2：桌面路由落台账。"""
        with mock.patch.object(routing.config, "IS_ANDROID", True):
            for url in (IWARA, TORRENT, BILI, X_URL):
                d = routing.classify(url, allow=routing.SURFACE_ME)
                self.assertEqual(d.kind, "ledger", url)

    def test_priority_douyin_before_torrent(self):
        d = routing.classify(f"{DOUYIN} {TORRENT}",
                             allow=routing.SURFACE_ME)
        self.assertEqual(d.kind, "douyin")

    def test_surface_policies_are_identical(self):
        """三个入口共用同一套完整策略（2026-11-08 统一）。

        此前收藏夹缺 iwara、bot 对话缺抖音/IG 与 t.me，链接会掉进外链台账。
        新增链接类型时只改 classify + 策略表一处，不要再让入口各持一份。
        """
        expected = {"douyin", "instagram", "torrent", "iwara", "bili", "x",
                    "tme", "wnacg", "asmr", "ehentai", "ledger"}
        for name, allow in (("SURFACE_ME", routing.SURFACE_ME),
                            ("SURFACE_BOT", routing.SURFACE_BOT),
                            ("SURFACE_API", routing.SURFACE_API)):
            self.assertEqual(set(allow), expected, name)


class ExecuteTest(unittest.IsolatedAsyncioTestCase):
    async def test_douyin_calls_relay_with_message(self):
        sentinel = object()
        d = routing.Decision(
            kind="douyin", urls=(DOUYIN,), message=sentinel,
            extra={"douyin_urls": [DOUYIN], "instagram_urls": []})
        with mock.patch.object(routing.platform, "relay_platform_links",
                               new=mock.AsyncMock()) as relay:
            res = await routing.execute(d)
        relay.assert_awaited_once_with(sentinel, [DOUYIN], [])
        self.assertTrue(res.handled)
        self.assertEqual(res.kind, "douyin")

    async def test_douyin_result_has_confirmation_message(self):
        """bot 对话把结果直接发到面板：空消息会让 send_message 报错。"""
        sentinel = object()
        d = routing.Decision(
            kind="douyin", urls=(DOUYIN,), message=sentinel,
            extra={"douyin_urls": [DOUYIN], "instagram_urls": []})
        with mock.patch.object(routing.platform, "relay_platform_links",
                               new=mock.AsyncMock()):
            res = await routing.execute(d)
        self.assertTrue(res.message)

    async def test_douyin_reports_queue_ref(self):
        """看板粘贴历史靠 ref 回查任务状态（原来在 api_server 里手写）。"""
        sentinel = object()
        d = routing.Decision(
            kind="douyin", urls=(DOUYIN,), message=sentinel,
            extra={"douyin_urls": [DOUYIN], "instagram_urls": []})
        with mock.patch.object(routing.platform, "relay_platform_links",
                               new=mock.AsyncMock()), \
                mock.patch.object(
                    routing.state, "QUEUE",
                    {"tasks": [{"url": DOUYIN, "id": "task-1"}],
                     "retry": []}):
            res = await routing.execute(d)
        self.assertEqual(res.ref, "task-1")

    async def test_torrent_passes_folder(self):
        d = routing.Decision(kind="torrent", urls=(TORRENT,),
                             extra={"to_folder": "漫画/作者"})
        with mock.patch.object(
                routing.torrent_offline, "handle_torrent_url",
                new=mock.AsyncMock(return_value="✅ 已提交")) as handler:
            res = await routing.execute(d)
            handler.assert_awaited_once_with(
                TORRENT, to_folder="漫画/作者")
        self.assertEqual(res.message, "✅ 已提交")

    async def test_bili_returns_reply(self):
        d = routing.Decision(kind="bili", text=f"标题 {BILI}",
                             urls=(BILI,))
        with mock.patch.object(
                routing.bili_dl, "auto_reply",
                new=mock.AsyncMock(return_value="📺 已入队")) as reply:
            res = await routing.execute(d)
            reply.assert_awaited_once_with(f"标题 {BILI}")
        self.assertEqual(res.message, "📺 已入队")

    async def test_x_returns_reply(self):
        d = routing.Decision(kind="x", text=X_URL, urls=(X_URL,))
        with mock.patch.object(routing.x_dl, "auto_reply",
                               new=mock.AsyncMock(return_value="🐦 已入队")):
            res = await routing.execute(d)
        self.assertEqual(res.kind, "x")
        self.assertEqual(res.message, "🐦 已入队")

    async def test_tme_returns_reply(self):
        d = routing.Decision(kind="tme", urls=(TME,),
                             extra={"label": "标注"})
        with mock.patch.object(
                routing.tme, "handle_tme_url",
                new=mock.AsyncMock(return_value="📥 已入队")) as handler:
            res = await routing.execute(d)
            handler.assert_awaited_once()
            self.assertEqual(handler.await_args.args[0], TME)
            self.assertTrue(callable(
                handler.await_args.kwargs.get("on_enqueued")))
        self.assertEqual(res.message, "📥 已入队")

    async def test_ledger_returns_buttons(self):
        d = routing.Decision(kind="ledger", text=MEGA, urls=(MEGA,))
        with mock.patch.object(routing.manual_links, "observe",
                               return_value=("📋 已登记", [[object()]])):
            res = await routing.execute(d)
        self.assertEqual(res.message, "📋 已登记")
        self.assertEqual(len(res.buttons), 1)

    async def test_iwara_duplicate_short_circuits(self):
        d = routing.Decision(kind="iwara", urls=(IWARA,),
                             extra={"video_id": "abcd1234"})
        with mock.patch.object(routing.iwara, "find_previous_success",
                               return_value={"notified_at": "x"}), \
                mock.patch.object(routing.iwara, "duplicate_text",
                                  return_value="⏭️ 已下过"), \
                mock.patch.object(routing.chrome_client,
                                  "handle_chrome_command",
                                  new=mock.AsyncMock()) as submit:
            res = await routing.execute(d, event=object())
        submit.assert_not_awaited()
        self.assertEqual(res.kind, "iwara_dup")
        self.assertEqual(res.message, "⏭️ 已下过")

    async def test_iwara_submits_and_reports_ref(self):
        d = routing.Decision(kind="iwara", urls=(IWARA,),
                             extra={"video_id": "abcd1234"})
        event = object()
        with mock.patch.object(routing.iwara, "find_previous_success",
                               return_value=None), \
                mock.patch.object(routing.chrome_client, "resolve_owner_id",
                                  return_value=42), \
                mock.patch.object(routing.chrome_client,
                                  "handle_chrome_command",
                                  new=mock.AsyncMock(return_value="ok")) as submit, \
                mock.patch.object(routing, "_lookup_chrome_ref",
                                  return_value="task123"):
            res = await routing.execute(d, event=event)
            submit.assert_awaited_once_with(
                event, "/chrome " + IWARA, owner_id=42)
        self.assertEqual(res.kind, "iwara")
        self.assertEqual(res.ref, "task123")
        self.assertEqual(res.message, "ok")

    async def test_none_is_unhandled(self):
        res = await routing.execute(routing.Decision(kind="none"))
        self.assertFalse(res.handled)
        self.assertEqual(res.message, "")


class ChromeRefLookupTest(unittest.TestCase):
    """iwara 粘贴历史的 ref：提交瞬间任务还在 requests 里，Agent 尚未认领。"""

    def test_prefers_latest_request_before_agent_claims(self):
        with mock.patch("tg_userbot.chrome_agent.load_tasks",
                        return_value=[]), \
                mock.patch("tg_userbot.chrome_client.load_requests",
                           return_value=[
                               {"task_id": "old", "url": IWARA,
                                "notified_at": "x"},
                               {"task_id": "new", "url": IWARA},
                           ]):
            self.assertEqual(routing._lookup_chrome_ref(IWARA), "new")

    def test_falls_back_to_claimed_task(self):
        with mock.patch("tg_userbot.chrome_agent.load_tasks",
                        return_value=[{"task_id": "task1", "url": IWARA}]), \
                mock.patch("tg_userbot.chrome_client.load_requests",
                           return_value=[]):
            self.assertEqual(routing._lookup_chrome_ref(IWARA), "task1")


class AppSurfaceIwaraDeliveryTest(unittest.IsolatedAsyncioTestCase):
    """收藏夹入口：iwara 必须交给 Chrome 命令入口。

    未接 iwara 分支时，分类结果会一路落到「纯文本 = 转发评论标注」，
    把一条下载链接记成下个文件的 #标注（2026-11-08 统一策略时补的回归）。
    """

    async def asyncSetUp(self):
        from tg_userbot import app, state
        self.app = app
        self.state = state
        self._old = (state.MY_ID, state.WHITELIST_CHATS)
        state.MY_ID = 1001
        state.WHITELIST_CHATS = {}

        msg = mock.MagicMock()
        msg.message = IWARA
        msg.id = 501
        msg.file = None
        msg.media = None
        msg.photo = None
        msg.document = None
        msg.video = None
        msg.audio = None
        msg.grouped_id = None
        msg.entities = []
        msg.fwd_from = None
        self.event = mock.MagicMock()
        self.event.message = msg
        self.event.chat_id = state.MY_ID
        self.event.reply = mock.AsyncMock()

    async def asyncTearDown(self):
        self.state.MY_ID, self.state.WHITELIST_CHATS = self._old

    async def _run(self, result):
        with mock.patch.object(
                routing, "execute",
                new=mock.AsyncMock(return_value=result)) as execute, \
                mock.patch.object(
                    self.app.cleanup, "is_cleanup_message",
                    return_value=False), \
                mock.patch.object(self.app, "_record_me_label") as label:
            await self.app.new_message_handler(self.event)
        return execute, label

    async def test_iwara_submitted_not_recorded_as_label(self):
        execute, label = await self._run(
            routing.RouteResult(kind="iwara", message=""))
        execute.assert_awaited_once()
        self.assertIs(execute.await_args.kwargs.get("event"), self.event)
        label.assert_not_called()

    async def test_iwara_duplicate_replies(self):
        execute, label = await self._run(
            routing.RouteResult(kind="iwara_dup", message="⏭️ 已下过"))
        execute.assert_awaited_once()
        self.event.reply.assert_awaited_once_with(
            "⏭️ 已下过", link_preview=False)
        label.assert_not_called()

    async def test_media_message_does_not_hijack_iwara_caption(self):
        """消息自身带媒体时，caption 里的 iwara/t.me 链接不抢附件下载。"""
        self.event.message.file = object()   # is_downloadable=True
        captured = {}

        def spy(text, **kwargs):
            captured.update(kwargs)
            return routing.Decision(kind="none")

        with mock.patch.object(routing, "classify", side_effect=spy), \
                mock.patch.object(routing, "execute",
                                  new=mock.AsyncMock()) as execute, \
                mock.patch.object(self.app, "spawn") as spawn, \
                mock.patch.object(self.app, "_enqueue_me",
                                  new=mock.MagicMock(
                                      return_value=object())):
            await self.app.new_message_handler(self.event)
        self.assertNotIn("iwara", captured["allow"])
        self.assertNotIn("tme", captured["allow"])
        execute.assert_not_awaited()
        spawn.assert_called_once()


class JournalRecordTest(unittest.IsolatedAsyncioTestCase):
    """三入口统一日志：routing.execute 是唯一记录点。"""

    async def test_execute_records_surface_kind_url_and_ref(self):
        d = routing.Decision(kind="bili", text=f"标题 {BILI}", urls=(BILI,))
        with mock.patch.object(routing.bili_dl, "auto_reply",
                               new=mock.AsyncMock(return_value="ok")), \
                mock.patch.object(routing.link_journal, "record") as rec:
            await routing.execute(d, surface="bot")
        rec.assert_called_once()
        args, kwargs = rec.call_args
        self.assertEqual(args[1], BILI)
        self.assertEqual(args[2], "bili")
        self.assertEqual(kwargs["surface"], "bot")

    async def test_none_does_not_record(self):
        with mock.patch.object(routing.link_journal, "record") as rec:
            await routing.execute(routing.Decision(kind="none"),
                                  surface="me")
        rec.assert_not_called()

    async def test_tme_collects_enqueued_pairs_as_ref(self):
        class Msg:
            chat_id = -1001
            id = 42

        async def fake(url, on_enqueued=None):
            on_enqueued(Msg())
            return "📥 已入队"

        d = routing.Decision(kind="tme", urls=(TME,), extra={"label": ""})
        with mock.patch.object(routing.tme, "handle_tme_url", new=fake), \
                mock.patch.object(routing.link_journal, "record") as rec:
            res = await routing.execute(d, surface="api")
        self.assertEqual(json.loads(res.ref), [[-1001, 42]])
        self.assertEqual(rec.call_args.kwargs["ref"], res.ref)

    async def test_exception_still_records(self):
        """失败也要留痕：日志里能看到 ❌，而不是整条提交消失。"""
        d = routing.Decision(kind="iwara", urls=(IWARA,), extra={})
        with mock.patch.object(
                routing.chrome_client, "handle_chrome_command",
                new=mock.AsyncMock(side_effect=RuntimeError("boom"))), \
                mock.patch.object(routing.link_journal, "record") as rec:
            with self.assertRaises(RuntimeError):
                await routing.execute(d, event=object(), surface="me")
        rec.assert_called_once()
        self.assertIn("❌", rec.call_args.kwargs["message"])


if __name__ == "__main__":
    unittest.main()


class SiteCommandRoutingTest(unittest.TestCase):
    """wnacg / asmr / ehentai URL 自动分流（2026-11-10）。

    URL 形态 → 直提交站点任务；裸 aid 数字 / 裸 RJ 号 / 其它链接仍落
    外链台账（纯文本数字不劫持）。Android 端无 CD2/115 → 落台账。
    """

    WNACG = "https://www.wnacg.com/photos-index-aid-400952.html"
    ASMR = "https://www.asmr-300.com/work/RJ01207846"
    EHENTAI = "https://e-hentai.org/g/4277638/baedd2e998/"

    def test_wnacg_url_classifies(self):
        d = routing.classify(self.WNACG, allow=routing.SURFACE_ME)
        self.assertEqual(d.kind, "wnacg")
        self.assertEqual(d.urls, (self.WNACG,))

    def test_asmr_url_classifies(self):
        d = routing.classify(self.ASMR, allow=routing.SURFACE_API)
        self.assertEqual(d.kind, "asmr")
        self.assertEqual(d.urls, (self.ASMR,))

    def test_ehentai_url_classifies(self):
        d = routing.classify(self.EHENTAI, allow=routing.SURFACE_BOT)
        self.assertEqual(d.kind, "ehentai")

    def test_bare_aid_and_rj_do_not_hijack(self):
        """裸数字不是链接：manual_links.extract_urls 抽不到 URL →
        自然不进站点路（这里验证即便文本里带 aid 字样也不误判）。"""
        d = routing.classify("aid-400952 这个不错", allow=routing.SURFACE_ME)
        self.assertIn(d.kind, ("none", "ledger"))
        d = routing.classify("RJ01207846 纯文本", allow=routing.SURFACE_ME)
        self.assertIn(d.kind, ("none", "ledger"))

    def test_android_falls_to_ledger(self):
        with mock.patch.object(routing.config, "IS_ANDROID", True):
            d = routing.classify(self.WNACG, allow=routing.SURFACE_ME)
        self.assertEqual(d.kind, "ledger")

    def test_mixed_text_wins_earlier_kinds(self):
        """wnacg 链接 + MEGA 链接混合：wnacg 识别在 ledger 前 → wnacg 胜。"""
        d = routing.classify(f"{self.WNACG} {MEGA}", allow=routing.SURFACE_ME)
        self.assertEqual(d.kind, "wnacg")
