"""Pawchive 模块（pawchive.py）的单元测试：命令解析 / 外链提取 / 扫描记录 / CSV。

不联网：API 层不测（真实站点结构已在独立 CLI 脚本里验证过）；这里测纯函数
与落库/导出的边界。外链样例取自 SillyTeshii 帖子的真实 HTML 形态（MEGA 带
#key、站内链接、纯文本 Key 不在 <a> 里）。
运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import atexit
import os
import shutil
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_paw_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402
from tg_userbot import pawchive  # noqa: E402


class CommandParseTest(unittest.TestCase):

    def test_is_paw_command(self):
        self.assertTrue(pawchive.is_paw_command("/paw"))
        self.assertTrue(pawchive.is_paw_command("/paw plan X"))
        self.assertTrue(pawchive.is_paw_command("/PAW"))
        self.assertFalse(pawchive.is_paw_command("/pawfoo"))
        self.assertFalse(pawchive.is_paw_command("/progress"))
        self.assertFalse(pawchive.is_paw_command(""))

    def test_parse(self):
        self.assertEqual(pawchive.parse_paw_command("/paw"), ("status", None))
        self.assertEqual(pawchive.parse_paw_command("/paw help"), ("help", None))
        self.assertEqual(
            pawchive.parse_paw_command("/paw plan SillyTeshii"),
            ("plan", "SillyTeshii"))
        self.assertEqual(
            pawchive.parse_paw_command("/paw plan Foo all"), ("plan", "Foo all"))
        self.assertEqual(
            pawchive.parse_paw_command("/paw retry 12"), ("retry", "12"))
        self.assertEqual(
            pawchive.parse_paw_command("/paw unknown x"), ("help", None))


class ExtractLinksTest(unittest.TestCase):
    # 真实帖子 content 的典型形态
    CONTENT = (
        '<p>ANIMATION LINK: <a href="https://mega.nz/file/6nQWmBpL'
        '#_8s1ZWe4Ao3RchulauWbFIuejvsTOEDcAvvXR5_A0vM">https://mega.nz/file/6nQWmBpL</a></p>'
        '<p>KEY: YAcHs6MBCtCYWozn8nP-vVTtTd4HnCf3bTyqwJamBWI</p>'
        '<p><a href="/patreon/user/1/post/2">站内链接</a>'
        '<a href="https://drive.google.com/file/d/17W9/view?usp=sharing">GD</a>'
        '<a href="https://mega.nz/file/6nQWmBpL'
        '#_8s1ZWe4Ao3RchulauWbFIuejvsTOEDcAvvXR5_A0vM">重复的 MEGA</a></p>'
    )

    def test_extracts_external_dedupes_skips_internal(self):
        links = pawchive.extract_links({"content": self.CONTENT, "embed": {}})
        domains = [l["domain"] for l in links]
        self.assertEqual(domains, ["mega.nz", "drive.google.com"])
        # MEGA 的 #key 原样保留
        self.assertIn("#_8s1ZWe4Ao3RchulauWbFIuejvsTOEDcAvvXR5_A0vM",
                      links[0]["url"])
        # 站内链接不入清单；纯文本 KEY 不算链接（它不在 <a> 里）
        self.assertTrue(all("pawchive.pw" not in l["url"] for l in links))

    def test_embed_url_included(self):
        links = pawchive.extract_links({
            "content": "", "embed": {"url": "https://www.youtube.com/watch?v=x",
                                     "subject": "preview"}})
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["kind"], "embed")


class BuildScanRecordsTest(unittest.TestCase):

    def _post(self, pid, attachments, content="", embed=None, published=None):
        return {"id": pid, "title": f"T{pid}", "published": published,
                "attachments": attachments, "content": content,
                "embed": embed or {}}

    def _creator(self):
        return {"service": "patreon", "id": "42", "name": "TestCreator"}

    def test_faved_filtered_and_files_mapped(self):
        posts = [
            self._post("1", [{"name": "a.mp4",
                              "path": "/aa/a.mp4"}]),
            self._post("2", [{"name": "b.mp4",
                              "path": "/bb/b.mp4"}],
                       content='<a href="https://mega.nz/x#k">M</a>'),
            # 无 path 的附件 + 无外链 → 整帖跳过
            self._post("3", [{"name": "gone.mp4"}]),
        ]
        records = pawchive.build_scan_records(
            self._creator(), posts, faved_ids={"2"}, scope="notfaved")
        # 2 已收藏被排除；3 无直链无外链被排除；剩 1
        self.assertEqual([r["post_id"] for r in records], ["1"])
        self.assertEqual(records[0]["files"][0]["url"],
                         f"{config.PAWCHIVE_FILE_BASE}/data/aa/a.mp4?f=a.mp4")
        self.assertEqual(records[0]["files"][0]["filename"], "a.mp4")

    def test_ext_only_post_kept_for_manual(self):
        """没直链但有外链的帖子要进生命周期（worker 会直接判 MANUAL）。"""
        posts = [self._post("9", [], content='<a href="https://mega.nz/x#k">M</a>')]
        records = pawchive.build_scan_records(self._creator(), posts,
                                              faved_ids=None, scope="notfaved")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["files"], [])
        self.assertEqual(records[0]["ext_links"][0]["domain"], "mega.nz")

    def test_subdir_uses_date_and_postid(self):
        posts = [self._post("7", [{"name": "a.mp4", "path": "/a.mp4"}],
                            published="2026-09-13T04:57:26")]
        records = pawchive.build_scan_records(self._creator(), posts,
                                              faved_ids=None)
        self.assertIn("2026-09-13_7_T7", records[0]["subdir"])
        self.assertTrue(records[0]["subdir"].startswith("Pawchive/TestCreator/"))


class _DbTestCase(unittest.TestCase):
    """需要真实 DB 的用例（CSV 导出 / 命令回复）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pawmod_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        self._dl = mock.patch.object(config, "DOWNLOAD_DIR", self.dir)
        self._dl.start()
        self.addCleanup(self._dl.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())

    def _seed(self):
        posts = [{
            "post_id": "1", "title": "标题", "published": "2026-09-13T04:57:26",
            "post_url": "https://pawchive.pw/patreon/user/42/post/1",
            "subdir": "Pawchive/C/2026-09-13_1_t",
            "files": [{"url": "https://file.pawchive.pw/data/a.mp4",
                       "filename": "a.mp4"}],
            "ext_links": [{"kind": "link", "domain": "mega.nz",
                           "url": "https://mega.nz/x#k", "text": "M"}],
        }]
        runtime_db.enqueue_pawchive_posts("patreon", "42", "Creator", posts)


class CsvTest(_DbTestCase):

    def test_write_csv_rows_and_encoding(self):
        self._seed()
        path, err = pawchive.write_csv("patreon", "42", "Creator")
        self.assertIsNone(err)
        self.assertTrue(os.path.isfile(path))
        import csv as csv_mod
        with open(path, encoding="utf-8-sig") as f:
            rows = list(csv_mod.reader(f))
        self.assertEqual(rows[0][:3], ["作者", "平台", "帖子ID"])
        kinds = [r[6] for r in rows[1:]]
        self.assertEqual(kinds, ["附件", "外链"])
        self.assertEqual(rows[1][8], "https://file.pawchive.pw/data/a.mp4")

    def test_write_csv_without_records(self):
        path, err = pawchive.write_csv("patreon", "99", "Nobody")
        self.assertIsNone(path)
        self.assertIn("没有", err)


class _FakeEvent:
    """最小 event 替身：capture reply。"""

    def __init__(self):
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)


class CommandReplyTest(_DbTestCase):

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_help_and_status(self):
        ev = _FakeEvent()
        self._run(pawchive.command_reply(ev, "/paw help"))
        self.assertIn("/paw plan", ev.replies[0])
        self._run(pawchive.command_reply(ev, "/paw"))
        self.assertIn(TEXT := "🐾 Pawchive", ev.replies[1])

    def test_retry_bad_id(self):
        ev = _FakeEvent()
        self._run(pawchive.command_reply(ev, "/paw retry abc"))
        self.assertIn("数字", ev.replies[0])

    def test_retry_all_empty(self):
        ev = _FakeEvent()
        self._run(pawchive.command_reply(ev, "/paw retry all"))
        self.assertIn("0", ev.replies[0])

    def test_status_with_db(self):
        self._seed()
        ev = _FakeEvent()
        self._run(pawchive.command_reply(ev, "/paw status"))
        self.assertIn("⏳ 待处理 1", ev.replies[0])


if __name__ == "__main__":
    unittest.main()
