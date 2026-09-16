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
        reply, buttons = manual_links.observe(["https://mega.nz/file/a#K1"])
        self.assertIn("🔗 已记录", reply)
        self.assertIn("未处理", reply)
        self.assertTrue(buttons)                     # ✅ 标记按钮
        row = runtime_db.list_manual_links()[0]
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["host"], "mega.nz")

    def test_resend_pending_is_notice(self):
        manual_links.observe(["https://mega.nz/file/a#K1"])
        reply, buttons = manual_links.observe(["https://mega.nz/file/a#K1"])
        self.assertIn("已在记录中", reply)
        self.assertIn("未处理", reply)

    def test_mark_done_then_resend_warns(self):
        reply, _ = manual_links.observe(["https://mega.nz/file/a#K1"])
        link_id = runtime_db.list_manual_links()[0]["id"]
        self.assertTrue(runtime_db.manual_link_done(link_id))
        reply, _ = manual_links.observe(["https://mega.nz/file/a#K1"])
        self.assertIn("⚠️ 已处理过", reply)
        self.assertIn("标记完成", reply)

    def test_double_mark_done_idempotent(self):
        manual_links.observe(["https://mega.nz/file/a#K1"])
        link_id = runtime_db.list_manual_links()[0]["id"]
        self.assertTrue(runtime_db.manual_link_done(link_id))
        self.assertFalse(runtime_db.manual_link_done(link_id))

    def test_two_links_one_message(self):
        reply, _ = manual_links.observe(
            ["https://mega.nz/file/a#K1", "https://krakenfiles.com/b"])
        self.assertIn("2 条", reply)
        self.assertEqual(len(runtime_db.list_manual_links()), 2)

    def test_view_lists_pending_with_buttons(self):
        manual_links.observe(["https://mega.nz/file/a#K1"])
        manual_links.observe(["https://krakenfiles.com/b"])
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
        manual_links.observe(["https://mega.nz/file/a#K1"])
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
        reply, _ = manual_links.observe(["https://mega.nz/file/zz#K9"])
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
