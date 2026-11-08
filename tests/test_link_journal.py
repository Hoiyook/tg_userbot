"""链接提交日志（link_journal）：单行 append、读取、向后兼容。"""
import atexit
import os
import shutil
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="tg_userbot_journal_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import link_journal  # noqa: E402


class LinkJournalTest(unittest.TestCase):
    def setUp(self):
        try:
            os.remove(link_journal.PATH)
        except OSError:
            pass

    def test_record_and_read_newest_first(self):
        link_journal.record("text1", "https://a/1", "bili", surface="bot")
        link_journal.record("text2", "https://a/2", "iwara", ref="T1",
                            surface="me")
        items = link_journal.read(10)
        self.assertEqual([p["url"] for p in items],
                         ["https://a/2", "https://a/1"])
        self.assertEqual(items[0]["routed"], "iwara")
        self.assertEqual(items[0]["ref"], "T1")
        self.assertEqual(items[0]["surface"], "me")

    def test_old_record_without_surface_is_readable(self):
        """旧 paste_log 行（无 surface）必须原样读出，不能因此丢历史。"""
        with open(link_journal.PATH, "a", encoding="utf-8") as f:
            f.write('{"ts": 1, "text": "old", "url": "https://a/old", '
                    '"routed": "ledger", "ref": null}\n')
        items = link_journal.read(10)
        self.assertEqual(items[0]["url"], "https://a/old")
        self.assertNotIn("surface", items[0])

    def test_read_caps_to_latest_500(self):
        for i in range(510):
            link_journal.record(f"t{i}", f"https://a/{i}", "bili")
        items = link_journal.read(1000)
        self.assertEqual(len(items), 500)
        self.assertEqual(items[0]["url"], "https://a/509")
        self.assertEqual(items[-1]["url"], "https://a/10")

    def test_message_and_ids_persisted(self):
        link_journal.record("t", "https://a/1", "tme", surface="api",
                            chat_id=-100, message_id=7,
                            message="❌ 拉取失败")
        p = link_journal.read(1)[0]
        self.assertEqual(p["chat_id"], -100)
        self.assertEqual(p["message_id"], 7)
        self.assertEqual(p["message"], "❌ 拉取失败")


if __name__ == "__main__":
    unittest.main()
