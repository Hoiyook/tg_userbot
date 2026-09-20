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

    def test_bare_url_in_plain_text(self):
        """正文里裸写（无 <a> 包裹）的 URL 也要识别出来。"""
        content = ("<p>备份：https://mega.nz/folder/abc#key123 备用。</p>")
        links = pawchive.extract_links({"content": content, "embed": {}})
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["kind"], "text")
        self.assertEqual(links[0]["domain"], "mega.nz")
        self.assertEqual(links[0]["url"],
                         "https://mega.nz/folder/abc#key123")

    def test_bare_url_trailing_punctuation_trimmed(self):
        """URL 后跟中文标点是常态，必须剪掉否则链接打不开。"""
        content = "<p>https://mega.nz/file/x#k。备用：https://krakenfiles.com/a）</p>"
        links = pawchive.extract_links({"content": content, "embed": {}})
        urls = [l["url"] for l in links]
        self.assertIn("https://mega.nz/file/x#k", urls)
        self.assertIn("https://krakenfiles.com/a", urls)

    def test_bare_url_dedup_with_anchor(self):
        """<a> 的锚点文本若是同一 URL，剥标签后的裸扫按 URL 去重不重复计。"""
        content = ('<p><a href="https://mega.nz/file/x#k">MEGA</a></p>')
        links = pawchive.extract_links({"content": content, "embed": {}})
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["kind"], "link")

    def test_bare_url_entity_unescaped(self):
        content = "<p>https://example.com/f?a=1&amp;b=2 直接看</p>"
        links = pawchive.extract_links({"content": content, "embed": {}})
        self.assertEqual(links[0]["url"], "https://example.com/f?a=1&b=2")

    def test_bare_pawchive_url_excluded(self):
        content = "<p>原帖：https://pawchive.pw/patreon/user/1/post/2 看这里</p>"
        self.assertEqual(
            pawchive.extract_links({"content": content, "embed": {}}), [])

    def test_bare_url_balanced_paren_kept(self):
        """URL 内含成对括号（wiki 式）不能误剪。"""
        content = "<p>见 https://example.com/a(b)c。</p>"
        links = pawchive.extract_links({"content": content, "embed": {}})
        self.assertEqual(links[0]["url"], "https://example.com/a(b)c")

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


class EarlyStopTest(unittest.TestCase):
    """增量扫描：**连续两整页**均已入库 → 提前停止（防单页假象漏数据）；
    混入新帖/首扫 → 继续；前提是绝不漏数据。"""

    def _posts(self, ids):
        return [{"id": str(i), "title": f"T{i}", "published": "2026-01-01",
                 "attachments": [], "content": "", "embed": {}}
                for i in ids]

    def _run(self, pages, known):
        """pages: 每页返回的帖子 id 列表；known: 预置已入库集合。"""
        calls = []

        def fake_get_json(url, cookie=None, timeout=30, retries=4):
            offset = int(url.split("o=")[1])
            calls.append(offset)
            idx = offset // 50
            return self._posts(pages[idx]) if idx < len(pages) else []

        old_page, old_get = pawchive._PAGE, pawchive._http_get_json
        pawchive._PAGE = 50
        pawchive._http_get_json = fake_get_json
        try:
            posts = pawchive.fetch_creator_posts(
                "patreon", "42", None, None, known_ids=known)
        finally:
            pawchive._PAGE, pawchive._http_get_json = old_page, old_get
        return posts, calls

    def test_two_consecutive_known_pages_stop(self):
        # 连续两整页全已知 → 第 2 页后停止（2 个请求）
        pages = [list(range(200, 250)), list(range(150, 200))]
        posts, calls = self._run(pages,
                                 known={str(i) for i in range(150, 250)})
        self.assertEqual(len(posts), 100)
        self.assertEqual(calls, [0, 50])

    def test_single_known_page_not_enough(self):
        """只有一页全已知 → 不停（防排序抖动漏数据），继续拉到短页。"""
        pages = [list(range(200, 250)), list(range(150, 200))]
        known = {str(i) for i in range(200, 250)}   # 仅第 1 页已知
        posts, calls = self._run(pages, known=known)
        self.assertEqual(len(posts), 100)   # 第2页含未知帖必须继续
        self.assertEqual(calls, [0, 50, 100])  # 第3页为空页才自然停止

    def test_page_with_new_post_continues(self):
        # 第 1 页混入 1 个新帖 → 不能算全已知页，继续拉第 2 页
        pages = [[300] + list(range(201, 250)), list(range(151, 201)), []]
        known = {str(i) for i in range(151, 250)}
        posts, calls = self._run(pages, known=known)
        self.assertEqual(len(posts), 100)
        self.assertEqual(calls, [0, 50, 100])

    def test_first_scan_full_pagination(self):
        # 首扫（known=None）：拉到短页为止
        pages = [list(range(100, 150)), list(range(90, 100))]
        posts, calls = self._run(pages, known=None)
        self.assertEqual(len(posts), 60)
        self.assertEqual(calls, [0, 50])


class ParsePostRefTest(unittest.TestCase):

    def test_full_url(self):
        ref = pawchive.parse_post_ref(
            "https://pawchive.pw/patreon/user/152819670/post/169389311")
        self.assertEqual(ref, ("patreon", "152819670", "169389311"))

    def test_bare_id(self):
        self.assertEqual(pawchive.parse_post_ref("169389311"),
                         (None, None, "169389311"))

    def test_garbage(self):
        self.assertIsNone(pawchive.parse_post_ref("随便说说"))
        self.assertIsNone(pawchive.parse_post_ref(""))
        # 非 pawchive 站 URL 不算帖子引用
        self.assertIsNone(pawchive.parse_post_ref(
            "https://example.com/patreon/user/1/post/2"))


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


class SinglePostTest(_DbTestCase):

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_post_status_reply_failed_requeues(self):
        """失败的帖子：_post_status_reply 自动重投并告知。"""
        runtime_db.enqueue_pawchive_posts("patreon", "42", "C", [{
            "post_id": "9", "title": "T", "published": "2026-01-01",
            "post_url": "u", "subdir": "Pawchive/C/x",
            "files": [{"url": "https://x/a.mp4", "filename": "a.mp4"}],
            "ext_links": [],
        }])
        row = runtime_db.find_pawchive_posts_by_post_id("9")[0]
        runtime_db.claim_next_pawchive_post(now=1000)
        runtime_db.finalize_pawchive_post(
            row["id"], runtime_db.PAW_POST_FAILED, error="下载超时")
        row = runtime_db.find_pawchive_posts_by_post_id("9")[0]
        msg = pawchive._post_status_reply(row)
        self.assertIn("已重投", msg)
        self.assertEqual(
            runtime_db.get_pawchive_post(row["id"])["status"],
            runtime_db.PAW_POST_PENDING)

    def test_post_reply_url_enqueues_single(self):
        """URL 输入：拉详情 → 单帖入队 PENDING，附件与外链齐全。"""
        detail = {
            "id": "777", "title": "单帖标题", "published": "2026-09-01T00:00:00",
            "attachments": [{"name": "v.mp4", "path": "/vv/v.mp4"}],
            "content": '<a href="https://mega.nz/x#k">M</a>', "embed": {},
        }
        profile = {"name": "SomeCreator"}
        with mock.patch.object(pawchive, "fetch_post_detail",
                               return_value=detail), \
                mock.patch.object(pawchive, "fetch_creator_profile",
                                  return_value=profile):
            msg = self._run(pawchive.post_reply_text(
                "https://pawchive.pw/fanbox/user/55/post/777"))
        self.assertIn("已入队", msg)
        row = runtime_db.find_pawchive_posts_by_post_id("777")[0]
        self.assertEqual(row["status"], runtime_db.PAW_POST_PENDING)
        self.assertEqual(row["creator_name"], "SomeCreator")
        files = runtime_db.list_pawchive_files(row["id"])
        self.assertEqual(len(files), 1)
        self.assertIn("v.mp4", files[0]["url"])
        self.assertEqual(len(row["ext_links"]), 1)

    def test_post_reply_existing_completed(self):
        """已完成的帖子：不重复入队，告知文件位置。"""
        self._seed()
        row = runtime_db.find_pawchive_posts_by_post_id("1")[0]
        runtime_db.claim_next_pawchive_post(now=1000)
        runtime_db.finalize_pawchive_post(
            row["id"], runtime_db.PAW_POST_COMPLETED)
        row = runtime_db.find_pawchive_posts_by_post_id("1")[0]
        msg = pawchive._post_status_reply(row)
        self.assertIn("已下载完成", msg)

    def test_post_reply_bare_id_not_in_db(self):
        msg = self._run(pawchive.post_reply_text("424242"))
        self.assertIn("完整帖子 URL", msg)


class FindReplyTest(_DbTestCase):
    """/paw find：按名称查扫描记录 + /sh 当前目录下的文件。"""

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_find_hits_db_and_files(self):
        self._seed()
        # 建一个含关键词的文件放到当前工作目录
        from tg_userbot import state
        workdir = os.path.join(self.dir, "work")
        os.makedirs(workdir, exist_ok=True)
        with open(os.path.join(workdir, "标题文件.mp4"), "wb") as f:
            f.write(b"x")
        old_cwd = state.SHELL_CWD
        state.SHELL_CWD = workdir
        try:
            msg = self._run(pawchive.find_reply("标题"))
        finally:
            state.SHELL_CWD = old_cwd
        self.assertIn("扫描记录", msg)
        self.assertIn("Creator", msg)                  # DB 记录作者命中
        self.assertIn("标题文件.mp4", msg)              # 文件命中

    def test_find_no_match(self):
        self._seed()
        from tg_userbot import state
        old = state.SHELL_CWD
        state.SHELL_CWD = self.dir
        try:
            msg = self._run(pawchive.find_reply("不存在的词xyz"))
        finally:
            state.SHELL_CWD = old
        self.assertIn("没有匹配", msg)

    def test_parse_find(self):
        self.assertEqual(pawchive.parse_paw_command("/paw find abc"),
                         ("find", "abc"))


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
        self.assertIn("没有需要重投", ev.replies[0])

    def test_status_with_db(self):
        self._seed()
        ev = _FakeEvent()
        self._run(pawchive.command_reply(ev, "/paw status"))
        self.assertIn("⏳ 待处理 1", ev.replies[0])


if __name__ == "__main__":
    unittest.main()


class SinceDateTest(unittest.TestCase):
    """/paw plan <作者> since <日期>：只收该日期（含）之后的帖子。"""

    def _posts(self):
        def post(pid, pub, links=None):
            return {"id": pid, "title": f"帖{pid}", "published": pub,
                    "attachments": [], "embed": {},
                    "content": "".join(
                        f'<a href="{u}">l</a>' for u in (links or []))}
        return [
            post("1", "2026-08-01T00:00:00", ["https://mega.nz/file/old"]),
            post("2", "2026-09-01T00:00:00", ["https://mega.nz/file/mid"]),
            post("3", "2026-09-15T00:00:00", ["https://mega.nz/file/new"]),
        ]

    def test_since_filters_older_posts(self):
        from tg_userbot import pawchive
        creator = {"service": "patreon", "id": "1", "name": "C"}
        records = pawchive.build_scan_records(
            creator, self._posts(), faved_ids=None, scope="all",
            since="2026-09-01")
        ids = sorted(r["post_id"] for r in records)
        self.assertEqual(ids, ["2", "3"])      # 8 月旧帖被过滤

    def test_since_date_inclusive(self):
        from tg_userbot import pawchive
        creator = {"service": "patreon", "id": "1", "name": "C"}
        records = pawchive.build_scan_records(
            creator, self._posts(), faved_ids=None, scope="all",
            since="2026-09-01")
        self.assertIn("2", [r["post_id"] for r in records])   # 当天含

    def test_since_combined_with_scope(self):
        """since 与收藏范围可叠加（scope 照旧判定）。"""
        posts = self._posts()
        creator = {"service": "patreon", "id": "1", "name": "C"}
        records = pawchive.build_scan_records(
            creator, posts, faved_ids={"2"}, scope="notfaved",
            since="2026-09-01")
        ids = [r["post_id"] for r in records]
        self.assertEqual(ids, ["3"])           # 2 已收藏被 scope 排除

    def test_parse_plan_since(self):
        from tg_userbot import pawchive
        self.assertEqual(
            pawchive.parse_paw_command("/paw plan SillyTeshii since 2026-09-01"),
            ("plan", "SillyTeshii since 2026-09-01"))
