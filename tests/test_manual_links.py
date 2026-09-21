"""手动外链台账（manual_links.py）测试：移动端自己找到的网盘链接，
发进来记录（默认未处理）→ 处理完手动标记 → 再次发送查重（台账历史 +
Pawchive 已完成帖子的外链）。

    .venv/bin/python -m unittest tests.test_manual_links -v
"""
import asyncio
import os
import shutil
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录（config import 期
# 有真实副作用，见 test_config 的基座说明）。
_TMP = tempfile.mkdtemp(prefix="tg_userbot_mlinks_test_")
import atexit  # noqa: E402
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import manual_links  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402


class _MlinksDbBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mlinks_", dir=_TMP)
        self.path = os.path.join(self.dir, "tg_userbot.db")
        self._p = mock.patch.object(config, "RUNTIME_DB_FILE", self.path)
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())


class TestExtractUrls(unittest.TestCase):
    def test_multi_and_trim(self):
        text = "处理好了 https://mega.nz/file/a#K1。还有 https://krakenfiles.com/b）"
        urls = manual_links.extract_urls(text)
        self.assertEqual(urls, ["https://mega.nz/file/a#K1",
                                "https://krakenfiles.com/b"])

    def test_no_url(self):
        self.assertEqual(manual_links.extract_urls("没有链接的普通文本"), [])
        self.assertEqual(manual_links.extract_urls(""), [])

    def test_dedup_same_text(self):
        urls = manual_links.extract_urls(
            "https://mega.nz/f/a#K https://mega.nz/f/a#K")
        self.assertEqual(urls, ["https://mega.nz/f/a#K"])


class TestObserveAndDone(_MlinksDbBase):
    """发送 → 记录/查重；标记完成 → 再发送报已处理。"""

    def test_new_link_recorded_pending(self):
        reply, buttons = manual_links.observe("https://mega.nz/file/a#K1")
        self.assertIn("🔗 已记录", reply)
        self.assertIn("未处理", reply)
        self.assertTrue(buttons)                     # ✅ 标记按钮
        row = runtime_db.list_manual_links()[0]
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["host"], "mega.nz")

    def test_resend_pending_is_notice(self):
        manual_links.observe("https://mega.nz/file/a#K1")
        reply, buttons = manual_links.observe("https://mega.nz/file/a#K1")
        self.assertIn("已在记录中", reply)
        self.assertIn("未处理", reply)

    def test_mark_done_then_resend_warns(self):
        reply, _ = manual_links.observe("https://mega.nz/file/a#K1")
        link_id = runtime_db.list_manual_links()[0]["id"]
        self.assertTrue(runtime_db.manual_link_done(link_id))
        reply, _ = manual_links.observe("https://mega.nz/file/a#K1")
        self.assertIn("⚠️ 已处理过", reply)
        self.assertIn("标记完成", reply)

    def test_double_mark_done_idempotent(self):
        manual_links.observe("https://mega.nz/file/a#K1")
        link_id = runtime_db.list_manual_links()[0]["id"]
        self.assertTrue(runtime_db.manual_link_done(link_id))
        self.assertFalse(runtime_db.manual_link_done(link_id))

    def test_two_links_one_message(self):
        reply, _ = manual_links.observe(
            "https://mega.nz/file/a#K1 和 https://krakenfiles.com/b")
        self.assertIn("2 条", reply)
        self.assertEqual(len(runtime_db.list_manual_links()), 2)

    def test_view_lists_pending_with_buttons(self):
        manual_links.observe("https://mega.nz/file/a#K1")
        manual_links.observe("https://krakenfiles.com/b")
        mega_id = next(r["id"] for r in runtime_db.list_manual_links()
                       if "mega.nz" in r["url"])
        runtime_db.manual_link_done(mega_id)
        text, buttons = manual_links.links_view()
        self.assertIn("外链台账", text)
        self.assertIn("krakenfiles.com", text)       # 未处理的还在列
        self.assertNotIn("mega.nz/file/a#K1", text)  # 已完成的不在待办列表
        flat = [b for row in buttons for b in row]
        self.assertEqual(len(flat), 1)               # 只剩一条 ✅ 按钮

    async def test_done_reply_edit_shape(self):
        """面板 ✅ 点击 → 标记 + 刷新视图（原地 edit 语义）。"""
        manual_links.observe("https://mega.nz/file/a#K1")
        link_id = runtime_db.list_manual_links()[0]["id"]
        text, buttons = await manual_links.done_reply(str(link_id))
        self.assertIn("✅ 已标记完成", text)
        self.assertIn("链接台账", text)


class TestPawCompletedCheck(_MlinksDbBase):
    """查重第二数据源：Pawchive 已完成帖子的外链。"""

    def test_completed_paw_ext_link_warns(self):
        from tg_userbot import pawchive
        post = {"post_id": "77", "title": "帖", "published": "2026-09-15",
                "post_url": "https://pawchive.pw/x/1",
                "subdir": "s", "files": [],
                "ext_links": [{"kind": "link", "domain": "mega.nz",
                               "url": "https://mega.nz/file/zz#K9",
                               "text": ""}]}
        runtime_db.enqueue_pawchive_posts("patreon", "9", "作者A", [post])
        rows = runtime_db.list_pawchive_posts(limit=10)
        claimed = runtime_db.claim_next_pawchive_post()   # finalize 仅收 PROCESSING
        assert claimed and claimed["id"] == rows[0]["id"]
        runtime_db.finalize_pawchive_post(
            rows[0]["id"], runtime_db.PAW_POST_COMPLETED)
        self.assertIsNotNone(
            pawchive.find_completed_ext_link("https://mega.nz/file/zz#K9"))
        self.assertIsNone(
            pawchive.find_completed_ext_link("https://mega.nz/file/other#K"))

    def test_observe_warns_on_completed_paw_link(self):
        from tg_userbot import pawchive
        post = {"post_id": "77", "title": "帖", "published": "2026-09-15",
                "post_url": "https://pawchive.pw/x/1",
                "subdir": "s", "files": [],
                "ext_links": [{"kind": "link", "domain": "mega.nz",
                               "url": "https://mega.nz/file/zz#K9",
                               "text": ""}]}
        runtime_db.enqueue_pawchive_posts("patreon", "9", "作者A", [post])
        rows = runtime_db.list_pawchive_posts(limit=10)
        claimed = runtime_db.claim_next_pawchive_post()
        assert claimed and claimed["id"] == rows[0]["id"]
        runtime_db.finalize_pawchive_post(
            rows[0]["id"], runtime_db.PAW_POST_COMPLETED)
        reply, _ = manual_links.observe("https://mega.nz/file/zz#K9")
        self.assertIn("Pawchive 外链已完成", reply)
        self.assertIn("作者A", reply)
        # 台账里不重复记（paw 已完成即视为已处理）
        self.assertEqual(len(runtime_db.list_manual_links()), 0)


if __name__ == "__main__":
    unittest.main()


class PanelViewTest(_MlinksDbBase):
    """#4：工具面板的 🔗 外链台账入口（mlink_view 动作）。"""

    def test_tools_panel_has_ledger_button(self):
        from tg_userbot import menu
        rows = menu.tools_menu_buttons([])
        flat = [b for row in rows for b in row]
        led = [b for b in flat if "外链台账" in b.text]
        self.assertEqual(len(led), 1)
        self.assertLessEqual(len(led[0].data), 64)

    async def test_mlink_view_action(self):
        from tg_userbot import bot
        manual_links.observe(["https://mega.nz/file/a#K1"])
        text, buttons = await bot.handle_menu_action("mlink_view", None)
        self.assertIn("外链台账", text)
        self.assertIn("mega.nz", text)
        # 带返回导航
        flat = [b for row in buttons for b in row]
        self.assertTrue(any("返回" in b.text for b in flat))


class NoteSubmissionTest(_MlinksDbBase):
    """链接 + 备注：登记/更新/展示（2026-09-16 用户需求）。"""

    def test_link_with_note_recorded(self):
        reply, _ = manual_links.observe(
            "https://mega.nz/file/a#K1 我的备份 4K版")
        self.assertIn("📝 我的备份 4K版", reply)
        row = runtime_db.list_manual_links()[0]
        self.assertEqual(row["note"], "我的备份 4K版")

    def test_note_before_url_also_captured(self):
        reply, _ = manual_links.observe("备份用 https://mega.nz/file/a#K1")
        row = runtime_db.list_manual_links()[0]
        self.assertEqual(row["note"], "备份用")

    def test_note_whitespace_collapsed(self):
        manual_links.observe("https://mega.nz/file/a#K1   多段   备注\t这里")
        row = runtime_db.list_manual_links()[0]
        self.assertEqual(row["note"], "多段 备注 这里")

    def test_no_note_is_none(self):
        manual_links.observe("https://mega.nz/file/a#K1")
        self.assertIsNone(runtime_db.list_manual_links()[0]["note"])

    def test_resend_with_new_note_updates(self):
        """同链接再发带新备注 → 备注更新，状态不变。"""
        manual_links.observe("https://mega.nz/file/a#K1 旧备注")
        reply, _ = manual_links.observe("https://mega.nz/file/a#K1 新备注")
        self.assertIn("🔁 备注已更新", reply)
        row = runtime_db.list_manual_links()[0]
        self.assertEqual(row["note"], "新备注")
        self.assertEqual(row["status"], "PENDING")   # 状态不受备注更新影响

    def test_resend_without_note_keeps_note(self):
        """同链接无备注重发 → 查重提示，原备注保留。"""
        manual_links.observe("https://mega.nz/file/a#K1 原备注")
        reply, _ = manual_links.observe("https://mega.nz/file/a#K1")
        self.assertIn("已在记录中", reply)
        self.assertEqual(runtime_db.list_manual_links()[0]["note"], "原备注")

    def test_resend_note_after_done_updates_but_reports_done(self):
        """已完成的链接再发带备注：报告已处理过，备注仍更新（留档）。"""
        manual_links.observe("https://mega.nz/file/a#K1 原备注")
        link_id = runtime_db.list_manual_links()[0]["id"]
        runtime_db.manual_link_done(link_id)
        reply, _ = manual_links.observe("https://mega.nz/file/a#K1 新备注")
        self.assertIn("⚠️ 已处理过", reply)
        self.assertEqual(runtime_db.list_manual_links()[0]["note"], "新备注")

    def test_multi_url_no_note(self):
        """多 URL 消息：备注归属有歧义 → 全部无备注（原行为）。"""
        reply, _ = manual_links.observe(
            "https://mega.nz/file/a#K1 和 https://krakenfiles.com/b 都是")
        self.assertEqual(len(runtime_db.list_manual_links()), 2)
        for r in runtime_db.list_manual_links():
            self.assertIsNone(r["note"])

    def test_view_shows_note(self):
        manual_links.observe("https://mega.nz/file/a#K1 我的 4K 备份")
        text, _ = manual_links.links_view()
        self.assertIn("📝 我的 4K 备份", text)

    def test_extract_urls_still_works(self):
        """纯 URL 提取保留（多 URL 场景/外部使用）。"""
        self.assertEqual(
            manual_links.extract_urls("a https://x/f/1 b"),
            ["https://x/f/1"])


class SearchByNoteTest(_MlinksDbBase):
    """按备注关键词查外链（/links <关键词>，2026-09-17 用户需求）。"""

    def _seed(self):
        manual_links.observe("https://mega.nz/file/one#K1 鸣潮 4K 合集")
        manual_links.observe("https://krakenfiles.com/two 日常备份")
        manual_links.observe("https://mega.nz/file/three#K3 原神截图")
        return {r["url"]: r for r in runtime_db.list_manual_links()}

    def test_search_hits_note_substring(self):
        self._seed()
        text, buttons = manual_links.links_view(keyword="鸣潮")
        self.assertIn("mega.nz/file/one", text)
        self.assertIn("📝 鸣潮 4K 合集", text)
        self.assertNotIn("krakenfiles.com/two", text)
        self.assertNotIn("file/three", text)

    def test_search_case_insensitive(self):
        self._seed()
        text, _ = manual_links.links_view(keyword="Mega")
        # 无备注命中的 URL 大小写不敏感兜底（host 含 Mega → 命中两条 mega）
        self.assertIn("file/one", text)
        self.assertIn("file/three", text)

    def test_search_also_matches_url(self):
        """关键词同时匹配 URL 本身（不止备注）。"""
        self._seed()
        text, _ = manual_links.links_view(keyword="krakenfiles")
        self.assertIn("krakenfiles.com/two", text)
        self.assertNotIn("file/one", text)

    def test_search_covers_done_entries(self):
        """已完成条目也参与搜索（找回历史）。"""
        self._seed()
        one_id = next(r["id"] for r in runtime_db.list_manual_links()
                      if "one" in r["url"])
        runtime_db.manual_link_done(one_id)
        text, _ = manual_links.links_view(keyword="鸣潮")
        self.assertIn("已处理", text)          # 搜索视图显示终态标记
        self.assertIn("file/one", text)

    def test_search_no_hit(self):
        self._seed()
        text, _ = manual_links.links_view(keyword="不存在的词")
        self.assertIn("无匹配", text)

    def test_no_keyword_still_pending_only(self):
        """无参数 = 原行为：未处理清单。"""
        self._seed()
        text, buttons = manual_links.links_view()
        self.assertIn("未处理", text)
        self.assertNotIn("krakenfiles.com/two 备份", text)  # 无备注行

    def test_command_passes_keyword(self):
        """/links <词> 分发：commands.py 把参数传进 links_view。"""
        from tg_userbot import commands
        import inspect
        src = inspect.getsource(commands.handle_command)
        self.assertIn('startswith("/links ")', src)
        self.assertIn("keyword=keyword", src)


class PendingResendButtonTest(_MlinksDbBase):
    """待办重复发送也挂 ✅ 按钮（2026-09-17 用户要求）。"""

    def test_resend_pending_has_button(self):
        manual_links.observe("https://example.com/f.zip")   # 非云盘：仅 ✅
        reply, buttons = manual_links.observe("https://example.com/f.zip")
        self.assertIn("已在记录中", reply)
        flat = [b for row in buttons for b in row]
        self.assertEqual(len(flat), 1)           # ✅ 可点
        self.assertIn("✅", flat[0].text)


class CloudOpenButtonTest(_MlinksDbBase):
    """网盘链接挂 🌐 在Chrome打开 按钮（mlink_open → 可见打开）。"""

    def test_cloud_link_gets_open_button(self):
        manual_links.observe("https://mega.nz/file/a#K1 我的备份")
        reply, buttons = manual_links.observe("https://mega.nz/file/a#K1")
        flat = [b for row in buttons for b in row]
        open_btns = [b for b in flat if "🌐" in b.text]
        self.assertEqual(len(open_btns), 1)
        self.assertTrue(open_btns[0].data.startswith(b"m:mlink_open:"))

    def test_non_cloud_no_open_button(self):
        manual_links.observe("https://example.com/file.zip 普通文件")
        reply, buttons = manual_links.observe("https://example.com/file.zip")
        flat = [b for row in buttons for b in row]
        self.assertFalse(any("🌐" in b.text for b in flat))

    async def test_mlink_open_action(self):
        from tg_userbot import bot
        import subprocess
        with mock.patch.object(subprocess, "run") as run:
            text, buttons = await bot.handle_menu_action("mlink_open",
                "https://mega.nz/file/a#K1")
        args = run.call_args[0][0]
        self.assertEqual(args[0], "open")
        self.assertIn("已在 Chrome 打开", text)
"""手动外链台账（manual_links.py）测试：移动端自己找到的网盘链接，
发进来记录（默认未处理）→ 处理完手动标记 → 再次发送查重（台账历史 +
Pawchive 已完成帖子的外链）。

    .venv/bin/python -m unittest tests.test_manual_links -v
"""
import asyncio
import os
import shutil
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录（config import 期
# 有真实副作用，见 test_config 的基座说明）。
_TMP = tempfile.mkdtemp(prefix="tg_userbot_mlinks_test_")
import atexit  # noqa: E402
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import manual_links  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402


class _MlinksDbBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mlinks_", dir=_TMP)
        self.path = os.path.join(self.dir, "tg_userbot.db")
        self._p = mock.patch.object(config, "RUNTIME_DB_FILE", self.path)
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())


class TestExtractUrls(unittest.TestCase):
    def test_multi_and_trim(self):
        text = "处理好了 https://mega.nz/file/a#K1。还有 https://krakenfiles.com/b）"
        urls = manual_links.extract_urls(text)
        self.assertEqual(urls, ["https://mega.nz/file/a#K1",
                                "https://krakenfiles.com/b"])

    def test_no_url(self):
        self.assertEqual(manual_links.extract_urls("没有链接的普通文本"), [])
        self.assertEqual(manual_links.extract_urls(""), [])

    def test_dedup_same_text(self):
        urls = manual_links.extract_urls(
            "https://mega.nz/f/a#K https://mega.nz/f/a#K")
        self.assertEqual(urls, ["https://mega.nz/f/a#K"])


class TestObserveAndDone(_MlinksDbBase):
    """发送 → 记录/查重；标记完成 → 再发送报已处理。"""

    def test_new_link_recorded_pending(self):
        reply, buttons = manual_links.observe("https://mega.nz/file/a#K1")
        self.assertIn("🔗 已记录", reply)
        self.assertIn("未处理", reply)
        self.assertTrue(buttons)                     # ✅ 标记按钮
        row = runtime_db.list_manual_links()[0]
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["host"], "mega.nz")

    def test_resend_pending_is_notice(self):
        manual_links.observe("https://mega.nz/file/a#K1")
        reply, buttons = manual_links.observe("https://mega.nz/file/a#K1")
        self.assertIn("已在记录中", reply)
        self.assertIn("未处理", reply)

    def test_mark_done_then_resend_warns(self):
        reply, _ = manual_links.observe("https://mega.nz/file/a#K1")
        link_id = runtime_db.list_manual_links()[0]["id"]
        self.assertTrue(runtime_db.manual_link_done(link_id))
        reply, _ = manual_links.observe("https://mega.nz/file/a#K1")
        self.assertIn("⚠️ 已处理过", reply)
        self.assertIn("标记完成", reply)

    def test_double_mark_done_idempotent(self):
        manual_links.observe("https://mega.nz/file/a#K1")
        link_id = runtime_db.list_manual_links()[0]["id"]
        self.assertTrue(runtime_db.manual_link_done(link_id))
        self.assertFalse(runtime_db.manual_link_done(link_id))

    def test_two_links_one_message(self):
        reply, _ = manual_links.observe(
            "https://mega.nz/file/a#K1 和 https://krakenfiles.com/b")
        self.assertIn("2 条", reply)
        self.assertEqual(len(runtime_db.list_manual_links()), 2)

    def test_view_lists_pending_with_buttons(self):
        manual_links.observe("https://mega.nz/file/a#K1")
        manual_links.observe("https://krakenfiles.com/b")
        mega_id = next(r["id"] for r in runtime_db.list_manual_links()
                       if "mega.nz" in r["url"])
        runtime_db.manual_link_done(mega_id)
        text, buttons = manual_links.links_view()
        self.assertIn("外链台账", text)
        self.assertIn("krakenfiles.com", text)       # 未处理的还在列
        self.assertNotIn("mega.nz/file/a#K1", text)  # 已完成的不在待办列表
        flat = [b for row in buttons for b in row]
        self.assertEqual(len(flat), 1)               # 只剩一条 ✅ 按钮

    async def test_done_reply_edit_shape(self):
        """面板 ✅ 点击 → 标记 + 刷新视图（原地 edit 语义）。"""
        manual_links.observe("https://mega.nz/file/a#K1")
        link_id = runtime_db.list_manual_links()[0]["id"]
        text, buttons = await manual_links.done_reply(str(link_id))
        self.assertIn("✅ 已标记完成", text)
        self.assertIn("链接台账", text)


class TestPawCompletedCheck(_MlinksDbBase):
    """查重第二数据源：Pawchive 已完成帖子的外链。"""

    def test_completed_paw_ext_link_warns(self):
        from tg_userbot import pawchive
        post = {"post_id": "77", "title": "帖", "published": "2026-09-15",
                "post_url": "https://pawchive.pw/x/1",
                "subdir": "s", "files": [],
                "ext_links": [{"kind": "link", "domain": "mega.nz",
                               "url": "https://mega.nz/file/zz#K9",
                               "text": ""}]}
        runtime_db.enqueue_pawchive_posts("patreon", "9", "作者A", [post])
        rows = runtime_db.list_pawchive_posts(limit=10)
        claimed = runtime_db.claim_next_pawchive_post()   # finalize 仅收 PROCESSING
        assert claimed and claimed["id"] == rows[0]["id"]
        runtime_db.finalize_pawchive_post(
            rows[0]["id"], runtime_db.PAW_POST_COMPLETED)
        self.assertIsNotNone(
            pawchive.find_completed_ext_link("https://mega.nz/file/zz#K9"))
        self.assertIsNone(
            pawchive.find_completed_ext_link("https://mega.nz/file/other#K"))

    def test_observe_warns_on_completed_paw_link(self):
        from tg_userbot import pawchive
        post = {"post_id": "77", "title": "帖", "published": "2026-09-15",
                "post_url": "https://pawchive.pw/x/1",
                "subdir": "s", "files": [],
                "ext_links": [{"kind": "link", "domain": "mega.nz",
                               "url": "https://mega.nz/file/zz#K9",
                               "text": ""}]}
        runtime_db.enqueue_pawchive_posts("patreon", "9", "作者A", [post])
        rows = runtime_db.list_pawchive_posts(limit=10)
        claimed = runtime_db.claim_next_pawchive_post()
        assert claimed and claimed["id"] == rows[0]["id"]
        runtime_db.finalize_pawchive_post(
            rows[0]["id"], runtime_db.PAW_POST_COMPLETED)
        reply, _ = manual_links.observe("https://mega.nz/file/zz#K9")
        self.assertIn("Pawchive 外链已完成", reply)
        self.assertIn("作者A", reply)
        # 台账里不重复记（paw 已完成即视为已处理）
        self.assertEqual(len(runtime_db.list_manual_links()), 0)


if __name__ == "__main__":
    unittest.main()


class PanelViewTest(_MlinksDbBase):
    """#4：工具面板的 🔗 外链台账入口（mlink_view 动作）。"""

    def test_tools_panel_has_ledger_button(self):
        from tg_userbot import menu
        rows = menu.tools_menu_buttons([])
        flat = [b for row in rows for b in row]
        led = [b for b in flat if "外链台账" in b.text]
        self.assertEqual(len(led), 1)
        self.assertLessEqual(len(led[0].data), 64)

    async def test_mlink_view_action(self):
        from tg_userbot import bot
        manual_links.observe(["https://mega.nz/file/a#K1"])
        text, buttons = await bot.handle_menu_action("mlink_view", None)
        self.assertIn("外链台账", text)
        self.assertIn("mega.nz", text)
        # 带返回导航
        flat = [b for row in buttons for b in row]
        self.assertTrue(any("返回" in b.text for b in flat))


class NoteSubmissionTest(_MlinksDbBase):
    """链接 + 备注：登记/更新/展示（2026-09-16 用户需求）。"""

    def test_link_with_note_recorded(self):
        reply, _ = manual_links.observe(
            "https://mega.nz/file/a#K1 我的备份 4K版")
        self.assertIn("📝 我的备份 4K版", reply)
        row = runtime_db.list_manual_links()[0]
        self.assertEqual(row["note"], "我的备份 4K版")

    def test_note_before_url_also_captured(self):
        reply, _ = manual_links.observe("备份用 https://mega.nz/file/a#K1")
        row = runtime_db.list_manual_links()[0]
        self.assertEqual(row["note"], "备份用")

    def test_note_whitespace_collapsed(self):
        manual_links.observe("https://mega.nz/file/a#K1   多段   备注\t这里")
        row = runtime_db.list_manual_links()[0]
        self.assertEqual(row["note"], "多段 备注 这里")

    def test_no_note_is_none(self):
        manual_links.observe("https://mega.nz/file/a#K1")
        self.assertIsNone(runtime_db.list_manual_links()[0]["note"])

    def test_resend_with_new_note_updates(self):
        """同链接再发带新备注 → 备注更新，状态不变。"""
        manual_links.observe("https://mega.nz/file/a#K1 旧备注")
        reply, _ = manual_links.observe("https://mega.nz/file/a#K1 新备注")
        self.assertIn("🔁 备注已更新", reply)
        row = runtime_db.list_manual_links()[0]
        self.assertEqual(row["note"], "新备注")
        self.assertEqual(row["status"], "PENDING")   # 状态不受备注更新影响

    def test_resend_without_note_keeps_note(self):
        """同链接无备注重发 → 查重提示，原备注保留。"""
        manual_links.observe("https://mega.nz/file/a#K1 原备注")
        reply, _ = manual_links.observe("https://mega.nz/file/a#K1")
        self.assertIn("已在记录中", reply)
        self.assertEqual(runtime_db.list_manual_links()[0]["note"], "原备注")

    def test_resend_note_after_done_updates_but_reports_done(self):
        """已完成的链接再发带备注：报告已处理过，备注仍更新（留档）。"""
        manual_links.observe("https://mega.nz/file/a#K1 原备注")
        link_id = runtime_db.list_manual_links()[0]["id"]
        runtime_db.manual_link_done(link_id)
        reply, _ = manual_links.observe("https://mega.nz/file/a#K1 新备注")
        self.assertIn("⚠️ 已处理过", reply)
        self.assertEqual(runtime_db.list_manual_links()[0]["note"], "新备注")

    def test_multi_url_no_note(self):
        """多 URL 消息：备注归属有歧义 → 全部无备注（原行为）。"""
        reply, _ = manual_links.observe(
            "https://mega.nz/file/a#K1 和 https://krakenfiles.com/b 都是")
        self.assertEqual(len(runtime_db.list_manual_links()), 2)
        for r in runtime_db.list_manual_links():
            self.assertIsNone(r["note"])

    def test_view_shows_note(self):
        manual_links.observe("https://mega.nz/file/a#K1 我的 4K 备份")
        text, _ = manual_links.links_view()
        self.assertIn("📝 我的 4K 备份", text)

    def test_extract_urls_still_works(self):
        """纯 URL 提取保留（多 URL 场景/外部使用）。"""
        self.assertEqual(
            manual_links.extract_urls("a https://x/f/1 b"),
            ["https://x/f/1"])


class SearchByNoteTest(_MlinksDbBase):
    """按备注关键词查外链（/links <关键词>，2026-09-17 用户需求）。"""

    def _seed(self):
        manual_links.observe("https://mega.nz/file/one#K1 鸣潮 4K 合集")
        manual_links.observe("https://krakenfiles.com/two 日常备份")
        manual_links.observe("https://mega.nz/file/three#K3 原神截图")
        return {r["url"]: r for r in runtime_db.list_manual_links()}

    def test_search_hits_note_substring(self):
        self._seed()
        text, buttons = manual_links.links_view(keyword="鸣潮")
        self.assertIn("mega.nz/file/one", text)
        self.assertIn("📝 鸣潮 4K 合集", text)
        self.assertNotIn("krakenfiles.com/two", text)
        self.assertNotIn("file/three", text)

    def test_search_case_insensitive(self):
        self._seed()
        text, _ = manual_links.links_view(keyword="Mega")
        # 无备注命中的 URL 大小写不敏感兜底（host 含 Mega → 命中两条 mega）
        self.assertIn("file/one", text)
        self.assertIn("file/three", text)

    def test_search_also_matches_url(self):
        """关键词同时匹配 URL 本身（不止备注）。"""
        self._seed()
        text, _ = manual_links.links_view(keyword="krakenfiles")
        self.assertIn("krakenfiles.com/two", text)
        self.assertNotIn("file/one", text)

    def test_search_covers_done_entries(self):
        """已完成条目也参与搜索（找回历史）。"""
        self._seed()
        one_id = next(r["id"] for r in runtime_db.list_manual_links()
                      if "one" in r["url"])
        runtime_db.manual_link_done(one_id)
        text, _ = manual_links.links_view(keyword="鸣潮")
        self.assertIn("已处理", text)          # 搜索视图显示终态标记
        self.assertIn("file/one", text)

    def test_search_no_hit(self):
        self._seed()
        text, _ = manual_links.links_view(keyword="不存在的词")
        self.assertIn("无匹配", text)

    def test_no_keyword_still_pending_only(self):
        """无参数 = 原行为：未处理清单。"""
        self._seed()
        text, buttons = manual_links.links_view()
        self.assertIn("未处理", text)
        self.assertNotIn("krakenfiles.com/two 备份", text)  # 无备注行

    def test_command_passes_keyword(self):
        """/links <词> 分发：commands.py 把参数传进 links_view。"""
        from tg_userbot import commands
        import inspect
        src = inspect.getsource(commands.handle_command)
        self.assertIn('startswith("/links ")', src)
        self.assertIn("keyword=keyword", src)


class PendingResendButtonTest(_MlinksDbBase):
    """待办重复发送也挂 ✅ 按钮（2026-09-17 用户要求）。"""

    def test_resend_pending_has_button(self):
        manual_links.observe("https://example.com/f.zip")   # 非云盘：仅 ✅
        reply, buttons = manual_links.observe("https://example.com/f.zip")
        self.assertIn("已在记录中", reply)
        flat = [b for row in buttons for b in row]
        self.assertEqual(len(flat), 1)           # ✅ 可点
        self.assertIn("✅", flat[0].text)


class CloudOpenButtonTest(_MlinksDbBase):
    """网盘链接挂 🌐 在Chrome打开 按钮（mlink_open → 可见打开）。"""

    def test_cloud_link_gets_open_button(self):
        manual_links.observe("https://mega.nz/file/a#K1 我的备份")
        reply, buttons = manual_links.observe("https://mega.nz/file/a#K1")
        flat = [b for row in buttons for b in row]
        open_btns = [b for b in flat if "🌐" in b.text]
        self.assertEqual(len(open_btns), 1)
        self.assertTrue(open_btns[0].data.startswith(b"m:mlink_open:"))

    def test_non_cloud_no_open_button(self):
        manual_links.observe("https://example.com/file.zip 普通文件")
        reply, buttons = manual_links.observe("https://example.com/file.zip")
        flat = [b for row in buttons for b in row]
        self.assertFalse(any("🌐" in b.text for b in flat))

    async def test_mlink_open_action(self):
        from tg_userbot import bot
        import subprocess
        with mock.patch.object(subprocess, "run") as run:
            text, buttons = await bot.handle_menu_action("mlink_open",
                "https://mega.nz/file/a#K1")
        args = run.call_args[0][0]
        self.assertEqual(args[0], "open")
        self.assertIn("已在 Chrome 打开", text)


class LongUrlButtonTest(_MlinksDbBase):
    """长网盘 URL 的 🌐 按钮回调数据 ≤64 字节（2026-09-18 事故回归：
    完整 URL 塞进回调数据超 64 字节上限，整条回复被拒、登记"没反应"）。"""

    LONG = ("https://mega.nz/file/a748abe234c44236bff7ccdb5c0d5e69"
            "#70DcJ1JqQlxQtq0CrjXyZw==")

    def test_button_data_within_64_bytes(self):
        reply, buttons = manual_links.observe(f"{self.LONG} 我的备份")
        flat = [b for row in buttons for b in row]
        open_btns = [b for b in flat if "🌐" in b.text]
        self.assertEqual(len(open_btns), 1)
        self.assertLessEqual(len(open_btns[0].data), 64)


class UnifiedLedgerTest(_MlinksDbBase):
    """统一外链管理（2026-09-20 用户需求）：

    1. 发送链接时，Pawchive MANUAL（待人工）帖的外链也参与查重——
       命中提示「已在 Pawchive 待人工清单」并给出帖子上下文；
    2. 🔗 /links 视图合并两个来源：台账条目 + Pawchive MANUAL 帖的外链，
       每条带 ✅（标记完成）与 🔗 原帖按钮；
    3. ✅ 点 Pawchive 来源的条目 → 该帖转 COMPLETED（复用 /paw done 语义）。
    """

    def _seed_paw_manual(self, url="https://mega.nz/folder/paw#K9",
                         creator="作者丙"):
        import json as _json
        post = {"post_id": "77", "title": "网盘帖", "published": "2026-09-19",
                "post_url": "https://pawchive.pw/x/77", "subdir": "s",
                "files": [], "ext_links": [
                    {"kind": "link", "domain": "mega.nz",
                     "url": url, "text": ""}]}
        runtime_db.enqueue_pawchive_posts("patreon", "9", creator, [post])
        claimed = runtime_db.claim_next_pawchive_post()
        runtime_db.finalize_pawchive_post(
            claimed["id"], runtime_db.PAW_POST_MANUAL)
        return claimed

    def test_resend_of_paw_manual_link_warns_with_context(self):
        self._seed_paw_manual()
        reply, _ = manual_links.observe("https://mega.nz/folder/paw#K9")
        self.assertIn("Pawchive 待人工", reply)
        self.assertIn("作者丙", reply)
        # 不重复入台账（paw 帖子记录就是它的账）
        self.assertEqual(len(runtime_db.list_manual_links()), 0)

    def test_unified_view_shows_both_sources(self):
        manual_links.observe("https://mega.nz/file/manual#K1")
        self._seed_paw_manual()
        text, buttons = manual_links.unified_view()
        self.assertIn("manual#K1", text)            # 台账条目
        self.assertIn("paw#K9", text)               # paw MANUAL 外链
        self.assertIn("作者丙", text)
        flat = [b for row in buttons for b in row]
        done_btns = [b for b in flat if "✅" in b.text]
        # 1 个台账 ✅ + 1 个 paw 帖 ✅
        self.assertEqual(len(done_btns), 2)
        for b in flat:
            data = getattr(b, "data", None)
            if data:
                self.assertLessEqual(len(data), 64)

    def test_paw_done_button_click_completes_post(self):
        claimed = self._seed_paw_manual()
        text, buttons = manual_links.unified_view()
        flat = [b for row in buttons for b in row]
        paw_btn = [b for b in flat
                   if (getattr(b, "data", b"") or b"").startswith(b"m:paw_done_view:")][0]
        arg = paw_btn.data.decode().split(":", 2)[2]
        text, buttons = manual_links.done_paw_post(arg)
        self.assertIn("✅", text)
        st = runtime_db.get_pawchive_post_row(claimed["id"])["status"]
        self.assertEqual(st, runtime_db.PAW_POST_COMPLETED)

    def test_observe_cloud_link_gets_open_button(self):
        self._seed_paw_manual()
        reply, buttons = manual_links.observe("https://mega.nz/folder/paw#K9")
        self.assertIn("已在 Pawchive 清单", reply)


class NoiseUrlFilterTest(_MlinksDbBase):
    """YouTube 预览链接是噪音：不进台账、不在看板（2026-09-20 用户需求）。"""

    def test_is_noise_url(self):
        from tg_userbot import manual_links as ml
        for url in ("https://www.youtube.com/watch?v=x",
                    "https://youtu.be/abc",
                    "https://m.youtube.com/watch?v=x",
                    "https://music.youtube.com/watch?v=x"):
            self.assertTrue(ml.is_noise_url(url), url)
        for url in ("https://mega.nz/file/a", "https://krakenfiles.com/b",
                    "https://notyoutube.com/x"):
            self.assertFalse(ml.is_noise_url(url), url)

    def test_observe_skips_noise(self):
        reply, buttons = manual_links.observe(
            "https://www.youtube.com/watch?v=7hQEV1gh0cE 预览")
        self.assertIn("已忽略", reply)
        self.assertEqual(len(runtime_db.list_manual_links()), 0)

    def test_mixed_message_registers_only_real_links(self):
        reply, buttons = manual_links.observe(
            "https://www.youtube.com/watch?v=x 预览 "
            "https://mega.nz/file/real#K 真资源")
        self.assertIn("已忽略 1 条 YouTube", reply)
        self.assertIn("已记录", reply)
        hosts = [r["host"] for r in runtime_db.list_manual_links()]
        self.assertEqual(hosts, ["mega.nz"])

    def test_all_noise_message_brief_reply(self):
        reply, buttons = manual_links.observe(
            "https://youtu.be/abc https://www.youtube.com/watch?v=y")
        self.assertIn("YouTube", reply)
        self.assertEqual(len(runtime_db.list_manual_links()), 0)
        self.assertEqual(buttons, [])


class PawViewNoiseFilterTest(_MlinksDbBase):
    """Pawchive MANUAL 看板/导出/att：YouTube 预览外链不显示。"""

    def _seed_paw_manual_with_youtube(self):
        import json as _json
        post = {"post_id": "88", "title": "带油管预览的帖", "published": "2026-09-19",
                "post_url": "https://pawchive.pw/x/88", "subdir": "s",
                "files": [], "ext_links": [
                    {"kind": "link", "domain": "mega.nz",
                     "url": "https://mega.nz/file/work#K", "text": "真资源"},
                    {"kind": "link", "domain": "www.youtube.com",
                     "url": "https://www.youtube.com/watch?v=prev", "text": "预览"}]}
        runtime_db.enqueue_pawchive_posts("patreon", "9", "作者A", [post])
        claimed = runtime_db.claim_next_pawchive_post()
        runtime_db.finalize_pawchive_post(
            claimed["id"], runtime_db.PAW_POST_MANUAL)

    def test_manual_view_hides_youtube(self):
        self._seed_paw_manual_with_youtube()
        from tg_userbot import manual_links as ml
        text, buttons = ml.unified_view()
        self.assertIn("mega.nz/file/work#K", text)
        self.assertNotIn("youtube.com/watch?v=prev", text)

    def test_export_hides_youtube(self):
        self._seed_paw_manual_with_youtube()
        from tg_userbot import pawchive
        text = pawchive.manual_export_text()
        self.assertIn("mega.nz/file/work#K", text)
        self.assertNotIn("watch?v=prev", text)

    def test_att_hides_youtube(self):
        from tg_userbot import pawchive
        self._seed_paw_manual_with_youtube()
        rid = [r["id"] for r in runtime_db.list_pawchive_posts(limit=5)][0]
        text = pawchive.att_text(str(rid))
        self.assertIn("mega.nz/file/work#K", text)
        self.assertNotIn("watch?v=prev", text)

    def _pawchive_links_view(self):
        # 统一看板（unified_view）里的 paw 段
        from tg_userbot import manual_links
        text, buttons = manual_links.unified_view()
        return text, buttons


class PawHitActionButtonsTest(_MlinksDbBase):
    """Pawchive 待人工命中时：显示外链当前处理状态 + 三操作按钮
    （✅ 完成 / 🗑 删除该帖 / 🌐 打开 Chrome），点击即生效。"""

    def _seed_paw_manual(self, url="https://drive.google.com/file/d/1BWF"):
        post = {"post_id": "77", "title": "外链帖", "published": "2026-09-20",
                "post_url": "https://pawchive.pw/x/77", "subdir": "s",
                "files": [], "ext_links": [
                    {"kind": "link", "domain": "drive.google.com",
                     "url": url, "text": ""}]}
        runtime_db.enqueue_pawchive_posts("patreon", "9", "作者丙", [post])
        claimed = runtime_db.claim_next_pawchive_post()
        runtime_db.finalize_pawchive_post(
            claimed["id"], runtime_db.PAW_POST_MANUAL)
        return claimed

    def test_paw_hit_shows_status_and_buttons(self):
        claimed = self._seed_paw_manual(
            "https://drive.google.com/file/d/1BWFfgxG5ZNCb7D7x8OrcPSSNQNAkUyWm")
        reply, buttons = manual_links.observe(
            "https://drive.google.com/file/d/1BWFfgxG5ZNCb7D7x8OrcPSSNQNAkUyWm")
        self.assertIn("👤 未处理（Pawchive 待人工）", reply)
        flat = [b for row in buttons for b in row]
        texts = [b.text for b in flat]
        self.assertTrue(any("✅ 完成" in t for t in texts))
        self.assertTrue(any("🗑 删除" in t for t in texts))
        self.assertTrue(any("🌐 打开" in t for t in texts))
        for b in flat:
            data = getattr(b, "data", None)
            if data:
                self.assertLessEqual(len(data), 64)
            url = getattr(b, "url", None)
            if url:
                self.assertTrue(url.startswith("https://"))

    def test_done_button_completes_paw_post(self):
        url = "https://drive.google.com/file/d/1BWFfgxG5ZNCb7D7x8OrcPSSNQNAkUyWm"
        claimed = self._seed_paw_manual(url)
        reply, buttons = manual_links.observe(url)
        flat = [b for row in buttons for b in row]
        done_btn = [b for b in flat if "✅ 完成" in b.text][0]
        self.assertEqual(done_btn.data.decode().split(":")[1], "mlink_paw_done")
        st = runtime_db.get_pawchive_post_row(claimed["id"])["status"]
        self.assertEqual(st, runtime_db.PAW_POST_MANUAL)   # 未点击前不变
        manual_links.complete_paw_post(claimed["id"])
        self.assertEqual(
            runtime_db.get_pawchive_post_row(claimed["id"])["status"],
            runtime_db.PAW_POST_COMPLETED)

    def test_delete_button_removes_paw_post(self):
        url = "https://drive.google.com/file/d/1BWFfgxG5ZNCb7D7x8OrcPSSNQNAkUyWm"
        claimed = self._seed_paw_manual(url)
        reply, buttons = manual_links.observe(url)
        flat = [b for row in buttons for b in row]
        del_btn = [b for b in flat if "🗑" in b.text][0]
        self.assertTrue(del_btn.data.decode().startswith("m:mlink_paw_del:"))
