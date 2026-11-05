"""B站视频下载（bili_dl.py）的单元测试。subprocess/网络全 mock 不联网。
运行方式（项目根目录）：
    .venv/bin/python -m unittest tests.test_bili_dl -v
"""
import asyncio
import atexit
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_bili_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import bili_dl  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402


class _DbTestCase(unittest.TestCase):
    """bili DB 队列语义测试基类：临时 runtime_db（v21 bili_tasks）。"""

    def setUp(self):
        self._td = tempfile.mkdtemp(dir=_TMP)
        self._saved_conn = runtime_db._CONN
        runtime_db._CONN = None
        self.dbpath = os.path.join(self._td, "tg.db")
        runtime_db.init_db(self.dbpath)
        bili_dl._WORKER._stop = False
        bili_dl._WORKER._stop_flag = False
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            runtime_db.close_db()
        except Exception:
            pass
        runtime_db._CONN = self._saved_conn
        shutil.rmtree(self._td, ignore_errors=True)


class ParseInputTest(unittest.TestCase):

    def test_bv_id_and_url_normalized(self):
        with mock.patch.object(bili_dl, "probe_season", return_value=None):
            url, kind, err = bili_dl.parse_input("BV1YK4y1C7CU")
        self.assertIsNone(err)
        self.assertEqual(kind, "video")
        self.assertEqual(url, "https://www.bilibili.com/video/BV1YK4y1C7CU")
        with mock.patch.object(bili_dl, "probe_season", return_value=None):
            url2, kind2, _ = bili_dl.parse_input(
                "https://www.bilibili.com/video/BV1YK4y1C7CU?p=2&spm_id_from=x")
        self.assertEqual((url2, kind2),
                         ("https://www.bilibili.com/video/BV1YK4y1C7CU",
                          "video"))
        # BV 从任意文本里抠出来（用户常整段粘贴分享文案）
        with mock.patch.object(bili_dl, "probe_season", return_value=None):
            url3, _, _ = bili_dl.parse_input(
                "快看这个 里提到的 BV1xx411c7mD 真不错")
        self.assertEqual(url3, "https://www.bilibili.com/video/BV1xx411c7mD")

    def test_b23_short_resolved(self):
        with mock.patch.object(bili_dl, "resolve_short",
                               return_value="https://www.bilibili.com/"
                                            "video/BV1AbCdEfGhH?share_source=x"):
            url, kind, err = bili_dl.parse_input("https://b23.tv/xyz123")
        self.assertIsNone(err)
        self.assertEqual(kind, "video")
        self.assertEqual(url, "https://www.bilibili.com/video/BV1AbCdEfGhH")

    def test_b23_short_link_extracted_from_share_text(self):
        # 分享直达带标题前缀：必须先抠出 b23 本体再展开——整段字符串喂
        # urllib 曾炸 unknown url type（2026-11-01 app 分享直达实测）
        with mock.patch.object(
                bili_dl, "resolve_short",
                return_value="https://www.bilibili.com/"
                             "video/BV1AbCdEfGhH") as rs, \
             mock.patch.object(bili_dl, "probe_season", return_value=None):
            url, kind, err = bili_dl.parse_input(
                "【鸣潮】【布料/锁暝】笑纳 https://b23.tv/m91lXr4")
        self.assertIsNone(err)
        self.assertEqual(kind, "video")
        self.assertEqual(url, "https://www.bilibili.com/video/BV1AbCdEfGhH")
        rs.assert_called_once_with("https://b23.tv/m91lXr4")

    def test_b23_trailing_punctuation_stripped(self):
        with mock.patch.object(
                bili_dl, "resolve_short",
                return_value="https://www.bilibili.com/"
                             "video/BV1AbCdEfGhH") as rs, \
             mock.patch.object(bili_dl, "probe_season", return_value=None):
            _, _, err = bili_dl.parse_input("笑纳 https://b23.tv/m91lXr4。")
        self.assertIsNone(err)
        rs.assert_called_once_with("https://b23.tv/m91lXr4")

    def test_collection_and_series_urls(self):
        # 合集：规范重建（丢噪声参数、保留 sid），kind=合集
        url, kind, err = bili_dl.parse_input(
            "https://space.bilibili.com/546195/channel/collectiondetail"
            "?sid=1096034&share_medium=android")
        self.assertIsNone(err)
        self.assertEqual(kind, "合集")
        self.assertEqual(url, "https://space.bilibili.com/546195/channel/"
                              "collectiondetail?sid=1096034")
        # 系列
        url2, kind2, _ = bili_dl.parse_input(
            "https://space.bilibili.com/546195/channel/seriesdetail?sid=77")
        self.assertEqual((url2, kind2),
                         ("https://space.bilibili.com/546195/channel/"
                          "seriesdetail?sid=77", "系列"))
        # b23 短链指向合集 → resolve 后进同一分支
        with mock.patch.object(
                bili_dl, "resolve_short",
                return_value="https://space.bilibili.com/1/channel/"
                             "collectiondetail?sid=9"):
            url3, kind3, err3 = bili_dl.parse_input("https://b23.tv/short9")
        self.assertIsNone(err3)
        self.assertEqual((url3, kind3),
                         ("https://space.bilibili.com/1/channel/"
                          "collectiondetail?sid=9", "合集"))

    def test_bv_with_season_converts_to_collection(self):
        with mock.patch.object(bili_dl, "probe_season",
                               return_value=(6616068, 45678, "MMD短视频")):
            url, kind, err = bili_dl.parse_input("BV1aTsGzYEbv")
        self.assertIsNone(err)
        self.assertEqual(kind, "合集")
        self.assertEqual(url, "https://space.bilibili.com/45678/channel/"
                              "collectiondetail?sid=6616068")

    def test_bv_season_probe_failure_falls_back_to_video(self):
        # probe_season 内部吞异常返回 None（真实失败路径），这里模拟之
        with mock.patch.object(bili_dl, "resolve_short",
                               return_value="https://www.bilibili.com/"
                                            "video/BV1aTsGzYEbv"), \
             mock.patch.object(bili_dl, "probe_season", return_value=None):
            url, kind, err = bili_dl.parse_input("https://b23.tv/abc")
        self.assertIsNone(err)
        self.assertEqual(kind, "video")
        self.assertEqual(url, "https://www.bilibili.com/video/BV1aTsGzYEbv")

    def test_b23_season_share_link_becomes_collection(self):
        """真场景：b23 短链 → 视频页（share_from=season）→ 探到合集 → 转合集。"""
        with mock.patch.object(
                bili_dl, "resolve_short",
                return_value="https://www.bilibili.com/video/"
                             "BV1aTsGzYEbv/?share_from=season&plat_id=411"), \
             mock.patch.object(bili_dl, "probe_season",
                               return_value=(6616068, 45678, "MMD短视频")):
            url, kind, err = bili_dl.parse_input("https://b23.tv/uH8wkSt")
        self.assertIsNone(err)
        self.assertEqual(kind, "合集")
        self.assertEqual(url, "https://space.bilibili.com/45678/channel/"
                              "collectiondetail?sid=6616068")

    def test_bangumi_passthrough(self):
        ep = "https://www.bilibili.com/bangumi/play/ep123456"
        url, kind, err = bili_dl.parse_input(ep)
        self.assertIsNone(err)
        self.assertEqual((url, kind), (ep, "video"))

    def test_rejects_garbage_and_favlist(self):
        _, _, err = bili_dl.parse_input("")
        self.assertIn("用法", err)
        _, _, err2 = bili_dl.parse_input("随便打的字没有链接")
        self.assertIn("未识别到 BV 号", err2)
        # 收藏夹明确不支持（混杂他人内容，用户定档不做）
        _, _, err3 = bili_dl.parse_input(
            "https://space.bilibili.com/123/favlist?fid=456")
        self.assertIn("未识别到 BV 号", err3)


class CookieFileTest(unittest.TestCase):

    def setUp(self):
        base = tempfile.mkdtemp(dir=_TMP)
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.rt = os.path.join(base, "runtime")
        os.makedirs(self.rt, exist_ok=True)
        p = mock.patch.object(config, "RUNTIME_DIR", self.rt)
        p.start()
        self.addCleanup(p.stop)

    def test_unconfigured_returns_false(self):
        with mock.patch.object(config, "BILIBILI_COOKIE", ""):
            ok, msg = bili_dl.build_cookie_file()
        self.assertFalse(ok)
        self.assertIn("未配置", msg)

    def test_netscape_format_with_forced_quality(self):
        cookie = ("SESSDATA=abc123; buvid3=BUV; DedeUserID=42; "
                  "CURRENT_QUALITY=16")
        with mock.patch.object(config, "BILIBILI_COOKIE", cookie):
            ok, msg = bili_dl.build_cookie_file()
        self.assertTrue(ok, msg)
        body = open(bili_dl._cookie_path(), encoding="utf-8").read()
        self.assertIn("# Netscape HTTP Cookie File", body)
        self.assertIn(".bilibili.com\tTRUE\t/\tFALSE\t", body)
        self.assertIn("SESSDATA\tabc123", body)
        self.assertIn("buvid3\tBUV", body)
        # issue #12492 workaround：画质档位 cookie 强制 4K/fnval 全开，
        # 且覆盖 cookie 串里已有的低档值（16）
        self.assertIn("CURRENT_QUALITY\t120", body)
        self.assertIn("CURRENT_FNVAL\t4048", body)
        self.assertNotIn("CURRENT_QUALITY\t16", body)


class AccountInfoTest(unittest.TestCase):

    def _fake_open(self, payload):
        body = json.dumps(payload).encode()

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                return body

        opener = mock.Mock()
        opener.open.return_value = _Resp()
        return opener

    def test_vip_login_and_guest(self):
        opener = self._fake_open({"code": 0, "data": {
            "isLogin": True, "vipStatus": 1, "uname": "某人"}})
        with mock.patch.object(bili_dl, "_direct_opener",
                               return_value=opener), \
             mock.patch.object(config, "BILIBILI_COOKIE", "SESSDATA=x"):
            self.assertEqual(bili_dl.account_info(), (True, 1, "某人"))
        # 未配置 cookie → 直接游客态，不发请求
        fresh = mock.Mock()
        with mock.patch.object(bili_dl, "_direct_opener",
                               return_value=fresh), \
             mock.patch.object(config, "BILIBILI_COOKIE", ""):
            self.assertEqual(bili_dl.account_info(), (False, 0, ""))
        fresh.open.assert_not_called()


class YtdlpCmdTest(unittest.TestCase):

    def setUp(self):
        base = tempfile.mkdtemp(dir=_TMP)
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.dl = os.path.join(base, "downloads")
        self.rt = os.path.join(base, "runtime")
        os.makedirs(self.rt, exist_ok=True)
        for p in (mock.patch.object(config, "DOWNLOAD_DIR", self.dl),
                  mock.patch.object(config, "RUNTIME_DIR", self.rt)):
            p.start()
            self.addCleanup(p.stop)

    def test_command_shape_proxy_and_no_playlist(self):
        with mock.patch.object(bili_dl, "build_cookie_file",
                               return_value=(False, "未配置")), \
             mock.patch.object(bili_dl.shutil, "which",
                               return_value="/opt/homebrew/bin/ffmpeg"):
            cmd = bili_dl._ytdlp_cmd("https://www.bilibili.com/video/BV1YK4y1C7CU")
        self.assertTrue(cmd[0].endswith("yt-dlp"))
        # 硬性要求：--proxy ""（macOS 系统代理会让 B站 412，实测）
        self.assertEqual(cmd[cmd.index("--proxy") + 1], "")
        self.assertIn("--yes-playlist", cmd)    # 多P视频全收（2026-10-30 定档）
        self.assertIn("--sleep-requests", cmd)  # 限流纪律
        self.assertIn("--merge-output-format", cmd)
        self.assertEqual(cmd[cmd.index("--ffmpeg-location") + 1],
                         "/opt/homebrew/bin/ffmpeg")
        # 命名模板：日期_BV_标题，落在 B站/<UP主>/ 两级目录
        tpl = cmd[cmd.index("-o") + 1]
        self.assertIn(os.path.join("B站", "%(uploader|未知UP主)s"), tpl)
        self.assertIn("%(upload_date>%Y-%m-%d)s_%(id)s_%(title).50B", tpl)
        # 未配置 cookie → 不带 --cookies（游客直连）
        self.assertNotIn("--cookies", cmd)

    def test_cookies_included_when_configured(self):
        with mock.patch.object(bili_dl, "build_cookie_file",
                               return_value=(True, "3 条")):
            cmd = bili_dl._ytdlp_cmd("https://www.bilibili.com/video/BV1YK4y1C7CU")
        self.assertEqual(cmd[cmd.index("--cookies") + 1],
                         bili_dl._cookie_path())

    def test_playlist_command_shape(self):
        """合集/系列模式：--yes-playlist + archive 增量 + 逐视频限速 +
        模板多一层 <合集名> 目录；不再带 --no-playlist。"""
        with mock.patch.object(bili_dl, "build_cookie_file",
                               return_value=(False, "未配置")), \
             mock.patch.object(bili_dl.shutil, "which", return_value=None):
            cmd = bili_dl._ytdlp_cmd(
                "https://space.bilibili.com/1/channel/collectiondetail?sid=9",
                playlist=True)
        self.assertIn("--yes-playlist", cmd)
        self.assertNotIn("--no-playlist", cmd)
        self.assertEqual(cmd[cmd.index("--download-archive") + 1],
                         bili_dl._archive_path())
        self.assertEqual(cmd[cmd.index("--sleep-interval") + 1], "2")
        tpl = cmd[cmd.index("-o") + 1]
        self.assertIn("%(playlist_title|合集)s", tpl)
        # 普通视频也全量（2026-10-30 定档：多P全收），模板不含合集层
        with mock.patch.object(bili_dl, "build_cookie_file",
                               return_value=(False, "未配置")):
            single = bili_dl._ytdlp_cmd("https://www.bilibili.com/video/BV1x")
        self.assertIn("--yes-playlist", single)
        self.assertNotIn("--no-playlist", single)
        self.assertEqual(single[single.index("--download-archive") + 1],
                         bili_dl._archive_path())
        self.assertNotIn("%(playlist_title", single[single.index("-o") + 1])
        # 番剧 ep URL 例外：只下指向的一集（不扩整季）
        with mock.patch.object(bili_dl, "build_cookie_file",
                               return_value=(False, "未配置")):
            bangumi = bili_dl._ytdlp_cmd(
                "https://www.bilibili.com/bangumi/play/ep123456")
        self.assertIn("--no-playlist", bangumi)
        self.assertNotIn("--yes-playlist", bangumi)


class ClassifyNoteTest(unittest.TestCase):

    def test_premium_missing_and_412_and_clean(self):
        out = ("[BiliBili] Format(s) 4K 超高清 are missing; you have to "
               "become a premium member to download them")
        self.assertIn("大会员", bili_dl._classify_note(out))
        self.assertIn("412", bili_dl._classify_note("HTTP Error 412"))
        self.assertIsNone(bili_dl._classify_note("all good"))


class RunBiliTest(unittest.TestCase):

    def setUp(self):
        base = tempfile.mkdtemp(dir=_TMP)
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.dl = os.path.join(base, "downloads")
        self.rt = os.path.join(base, "runtime")
        os.makedirs(self.rt, exist_ok=True)
        for p in (mock.patch.object(config, "DOWNLOAD_DIR", self.dl),
                  mock.patch.object(config, "RUNTIME_DIR", self.rt),
                  mock.patch.object(config, "BILIBILI_COOKIE", "")):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(setattr, bili_dl, "_BILI_RUNNING", {})

    def _fake_run(self, code=0, stdout=""):
        def fake(cmd, on_line=None, timeout=None, tag=None):
            for ln in stdout.splitlines():
                if on_line:
                    on_line(ln)
            return code, stdout[-1500:], stdout
        return fake

    def test_success_with_progress_parsing(self):
        out = ("[download]  12.3% of ~10MiB\n"
               "[download]  87.9% of ~10MiB\n"
               "[Merger] Merging formats into "
               "/dl/B站/UP主/2026-10-29_BV1x_test.mp4\n")
        with mock.patch.object(bili_dl, "_run_proc",
                               side_effect=self._fake_run(0, out)), \
             mock.patch.object(bili_dl, "account_info",
                               return_value=(True, 1, "UP主")):
            ok, note = bili_dl.run_bili("https://www.bilibili.com/video/BV1x")
        self.assertTrue(ok)
        self.assertIn("大会员", note)
        self.assertIn("UP主", note)
        self.assertGreaterEqual(bili_dl._BILI_RUNNING.get("pct", 0), 87.9)
        self.assertIn("elapsed", bili_dl._BILI_RUNNING)

    def test_failure_carries_tail(self):
        with mock.patch.object(bili_dl, "_run_proc",
                               side_effect=self._fake_run(1, "boom error")), \
             mock.patch.object(bili_dl, "account_info", return_value=None):
            ok, note = bili_dl.run_bili("https://www.bilibili.com/video/BV1x")
        self.assertFalse(ok)
        self.assertIn("退出码 1", note)
        self.assertIn("boom error", note)

    def test_expired_cookie_warns_but_downloads(self):
        with mock.patch.object(bili_dl, "_run_proc",
                               side_effect=self._fake_run(0, "")), \
             mock.patch.object(bili_dl, "account_info",
                               return_value=(False, 0, "")):
            ok, note = bili_dl.run_bili("https://www.bilibili.com/video/BV1x")
        self.assertTrue(ok)
        self.assertIn("480P", note)

    def test_single_dedup_hit_skips_with_hint(self):
        """单视频 archive 命中：零新增 → note 带跳过说明与找回指引。"""
        with open(bili_dl._cookie_path(), "w") as f:   # 防无关报错
            f.write("")
        with open(bili_dl._archive_path(), "w") as f:
            f.write("bilibili BV1x\n")
        with mock.patch.object(bili_dl, "_run_proc",
                               side_effect=self._fake_run(
                                   0, "[download] BV1x: has already been "
                                      "recorded in the archive")), \
             mock.patch.object(bili_dl, "account_info", return_value=None):
            ok, note = bili_dl.run_bili("https://www.bilibili.com/video/BV1x")
        self.assertTrue(ok)
        self.assertIn("已下载过", note)
        self.assertIn("bili_archive.txt", note)

    def test_single_new_download_no_dedup_note(self):
        """单视频真下载（archive +1）→ 不出现跳过说明。"""
        def fake(cmd, on_line=None, timeout=None, tag=None):
            with open(bili_dl._archive_path(), "a") as f:
                f.write("bilibili BV1x\n")
            return 0, "", ""
        with mock.patch.object(bili_dl, "_run_proc", side_effect=fake), \
             mock.patch.object(bili_dl, "account_info", return_value=None):
            ok, note = bili_dl.run_bili("https://www.bilibili.com/video/BV1x")
        self.assertTrue(ok)
        self.assertNotIn("已下载过", note)

    def test_multip_new_parts_note(self):
        """多P全收：archive 一次 +N（N>1）→ note 报分P新增数。"""
        def fake(cmd, on_line=None, timeout=None, tag=None):
            with open(bili_dl._archive_path(), "a") as f:
                f.write("bilibili BV1x_p1\nbilibili BV1x_p2\n")
            return 0, "", ""
        with mock.patch.object(bili_dl, "_run_proc", side_effect=fake), \
             mock.patch.object(bili_dl, "account_info", return_value=None):
            ok, note = bili_dl.run_bili("https://www.bilibili.com/video/BV1x")
        self.assertTrue(ok)
        self.assertIn("多P视频：本次新增 2 个分P", note)


class AutoDetectTest(_DbTestCase):
    """直接发 B站链接（无 /bili 前缀）自动识别（2026-10-31 owner 定档）。

    必须在外链台账之前拦——否则 B站链接会登记成待处理外链。"""

    def test_looks_like_bili_matches_all_forms(self):
        for text in (
            "https://www.bilibili.com/video/BV1YK4y1C7CU",
            "【合集】https://b23.tv/uH8wkSt",
            "https://space.bilibili.com/1/channel/collectiondetail?sid=9",
            "https://space.bilibili.com/1/favlist?fid=2",   # 不支持的形态也拦
            "https://www.bilibili.com/bangumi/play/ep1",
        ):
            self.assertTrue(bili_dl.looks_like_bili(text), text)

    def test_looks_like_bili_rejects_non_bili(self):
        for text in ("BV1YK4y1C7CU",                      # 裸 BV 不拦（保守）
                     "https://www.douyin.com/video/1",
                     "https://mega.nz/file/x",
                     "随便聊聊没有链接"):
            self.assertFalse(bili_dl.looks_like_bili(text), text)

    def test_auto_reply_enqueues(self):
        """自动识别 → 入 DB 队列（worker 串行领取，不再直接 spawn）。"""
        out = asyncio.run(bili_dl.auto_reply(
            "https://www.bilibili.com/video/BV1YK4y1C7CU"))
        self.assertIn("已自动识别", out)
        self.assertIn("已入队", out)
        row = runtime_db.BILI_TASKS.get(
            "https://www.bilibili.com/video/BV1YK4y1C7CU")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "PENDING")

    def test_auto_reply_duplicate_queues_once(self):
        """同一链接重复提交 → 单队列判重（任何状态）。"""
        asyncio.run(bili_dl.auto_reply("https://b23.tv/x"))
        out = asyncio.run(bili_dl.auto_reply("https://b23.tv/x"))
        self.assertIn("已在队列", out)
        self.assertEqual(runtime_db.BILI_TASKS.count(), 1)

    def test_auto_reply_unsupported_form_gives_friendly_error(self):
        """拦进来的不支持形态（如收藏夹）给友好报错而非静默入队。

        b23 短链需网络解析故延迟到领取时；其余形态 _submit 本地正则
        预检（2026-11-05 迁移语义）。"""
        out = asyncio.run(bili_dl.auto_reply(
            "https://space.bilibili.com/1/favlist?fid=2"))
        self.assertIn("未识别到", out)


class PlaylistRunTest(unittest.TestCase):
    """合集/系列下载：archive 增量计数、N/M 进度、中断续跑提示。"""

    def setUp(self):
        base = tempfile.mkdtemp(dir=_TMP)
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.dl = os.path.join(base, "downloads")
        self.rt = os.path.join(base, "runtime")
        os.makedirs(self.rt, exist_ok=True)
        for p in (mock.patch.object(config, "DOWNLOAD_DIR", self.dl),
                  mock.patch.object(config, "RUNTIME_DIR", self.rt),
                  mock.patch.object(config, "BILIBILI_COOKIE", "")):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(setattr, bili_dl, "_BILI_RUNNING", {})

    def _fake_run_appending(self, code, archive_lines_to_add, stdout=""):
        """yt-dlp 替身：边输出边往 archive 记 BV（模拟真实下载节奏）。"""
        def fake(cmd, on_line=None, timeout=None, tag=None):
            for ln in stdout.splitlines():
                if on_line:
                    on_line(ln)
            with open(bili_dl._archive_path(), "a", encoding="utf-8") as f:
                f.write("\n".join(archive_lines_to_add) + "\n")
            return code, stdout[-1500:], stdout
        return fake

    def test_new_count_from_archive_delta(self):
        url = "https://space.bilibili.com/1/channel/collectiondetail?sid=9"
        with open(bili_dl._archive_path(), "w") as f:
            f.write("youtube BV1old\n")
        out = ("[download] Downloading item 1 of 3\n"
               "[download] Downloading item 2 of 3\n")
        fake = self._fake_run_appending(
            0, ["youtube BV1new1\n", "youtube BV1new2\n"], out)
        with mock.patch.object(bili_dl, "_run_proc", side_effect=fake), \
             mock.patch.object(bili_dl, "account_info",
                               return_value=(True, 0, "UP主")):
            ok, note = bili_dl.run_bili(url, playlist=True)
        self.assertTrue(ok)
        self.assertIn("本次新增 2 个", note)
        self.assertIn("累计 3 个", note)
        self.assertIn("普通账号", note)
        self.assertEqual(bili_dl._BILI_RUNNING.get("item"), 2)
        self.assertEqual(bili_dl._BILI_RUNNING.get("item_count"), 3)

    def test_interrupted_reports_partial_and_resume_hint(self):
        url = "https://space.bilibili.com/1/channel/seriesdetail?sid=9"
        with open(bili_dl._archive_path(), "w") as f:
            f.write("youtube BV1a\n")
        fake = self._fake_run_appending(1, ["youtube BV1b\n"], "HTTP Error 429")
        with mock.patch.object(bili_dl, "_run_proc", side_effect=fake), \
             mock.patch.object(bili_dl, "account_info", return_value=None):
            ok, note = bili_dl.run_bili(url, playlist=True)
        self.assertFalse(ok)
        self.assertIn("退出码 1", note)
        self.assertIn("中断前已新增 1 个", note)
        self.assertIn("重跑同链接自动续", note)

    def test_status_shows_item_progress(self):
        bili_dl._BILI_RUNNING = {"label": "合集sid9", "kind": "合集",
                                 "started": time.time(), "pct": 55.0,
                                 "item": 3, "item_count": 57}
        try:
            text = bili_dl.status_text()
            self.assertIn("合集sid9", text)
            self.assertIn("第 3/57 个", text)
        finally:
            bili_dl._BILI_RUNNING = {}


class CommandTest(_DbTestCase):

    def tearDown(self):
        bili_dl._BILI_RUNNING = {}

    def test_command_detect_and_usage(self):
        self.assertTrue(bili_dl.is_bili_command("/bili"))
        self.assertTrue(bili_dl.is_bili_command("/bili BV1x"))
        self.assertFalse(bili_dl.is_bili_command("/bilibili"))
        self.assertFalse(bili_dl.is_bili_command("/bili2"))
        self.assertIn("/bili <", asyncio.run(bili_dl.command_reply("/bili")))

    def test_submit_enqueue_and_duplicate(self):
        """提交 → 入队；同链接重复提交 → 单队列判重（迁移语义）。"""
        out = asyncio.run(bili_dl.command_reply("/bili BV1YK4y1C7CU"))
        self.assertIn("已入队", out)
        out2 = asyncio.run(bili_dl.command_reply("/bili BV1YK4y1C7CU"))
        self.assertIn("已在队列", out2)
        self.assertEqual(runtime_db.BILI_TASKS.count(), 1)

    def test_playlist_enqueue(self):
        out = asyncio.run(bili_dl.command_reply(
            "/bili https://space.bilibili.com/1/channel/"
            "collectiondetail?sid=1096034"))
        self.assertIn("已入队", out)
        row = runtime_db.BILI_TASKS.get(
            "https://space.bilibili.com/1/channel/"
            "collectiondetail?sid=1096034")
        self.assertIsNotNone(row)

    def test_status_running_shows_pct(self):
        bili_dl._BILI_RUNNING = {"label": "BV1x", "started": time.time() - 65,
                                 "pct": 42.5, "last": "xxx.mp4"}
        try:
            text = bili_dl.status_text()
            self.assertIn("BV1x", text)
            self.assertIn("42%", text)
            self.assertIn("1分", text)
        finally:
            bili_dl._BILI_RUNNING = {}

    def test_notify_on_completion(self):
        """worker.process 完成链：DONE + 完成通知 + 进度视图清空。"""
        notes = []

        async def fake_notify(text):
            notes.append(text)

        runtime_db.BILI_TASKS.enqueue("BV1x")
        task = runtime_db.BILI_TASKS.claim_next()
        with mock.patch.object(bili_dl, "run_bili",
                               return_value=(True, "")), \
             mock.patch.object(bili_dl, "parse_input",
                               return_value=("BV1x", "video", None)), \
             mock.patch("tg_userbot.notify.notify_user", fake_notify):
            asyncio.run(bili_dl._WORKER.process(task))
        self.assertEqual(runtime_db.BILI_TASKS.get("BV1x")["status"], "DONE")
        self.assertEqual(len(notes), 1)
        self.assertIn("下载完成", notes[0])
        self.assertEqual(bili_dl._BILI_RUNNING, {})

    def test_invalid_url_marks_failed_terminal(self):
        """无法识别的链接 → FAILED 终态（不自动重试）。"""
        notes = []

        async def fake_notify(text):
            notes.append(text)

        runtime_db.BILI_TASKS.enqueue("垃圾输入")
        task = runtime_db.BILI_TASKS.claim_next()
        with mock.patch("tg_userbot.notify.notify_user", fake_notify):
            asyncio.run(bili_dl._WORKER.process(task))
        row = runtime_db.BILI_TASKS.get("垃圾输入")
        self.assertEqual(row["status"], "FAILED")
        self.assertEqual(len(notes), 1)


class RestartRecoveryTest(_DbTestCase):
    """停机/停止路径（DB 队列语义；RUNNING_MARK 已随迁移移除）。"""

    def test_shutdown_cancel_requeues(self):
        """停机取消 worker → 在途回 PENDING（框架 release），重启自续。"""
        runtime_db.BILI_TASKS.enqueue("u2")   # 不手动 claim——run 自己领
        async def go():
            loop_task = asyncio.ensure_future(bili_dl._WORKER.run())
            await asyncio.sleep(0.25)   # 进入 process（run_bili 睡 0.5）
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass
        with mock.patch.object(bili_dl, "run_bili",
                               side_effect=lambda *a: time.sleep(0.5)), \
             mock.patch.object(bili_dl, "parse_input",
                               return_value=("u2", "video", None)):
            asyncio.run(go())
        self.assertEqual(runtime_db.BILI_TASKS.get("u2")["status"],
                         "PENDING")

    def test_stop_marks_failed_terminal(self):
        """/bili stop → 在途标 FAILED（不回队列不打架），队列继续。"""
        runtime_db.BILI_TASKS.enqueue("u2")
        runtime_db.BILI_TASKS.enqueue("u4")   # 不手动 claim——run 自己领
        async def go():
            loop_task = asyncio.ensure_future(bili_dl._WORKER.run())
            await asyncio.sleep(0.25)
            bili_dl.bili_stop()   # 内部置 _stop_flag + cancel inner
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass
        with mock.patch.object(bili_dl, "run_bili",
                               side_effect=lambda *a: time.sleep(0.5)), \
             mock.patch.object(bili_dl, "parse_input",
                               return_value=("uX", "video", None)):
            asyncio.run(go())
        self.assertEqual(runtime_db.BILI_TASKS.get("u2")["status"], "FAILED")
        # u4 未被领取（claim 只在 process 完成后发生；stop 时仍在 PENDING）
        self.assertIn(runtime_db.BILI_TASKS.get("u4")["status"],
                      ("PENDING", "PROCESSING"))


if __name__ == "__main__":
    unittest.main()


class QueuePersistenceTest(_DbTestCase):
    """队列持久化：进程内 DB 重开后任务仍在（重启不再丢排队）。"""

    def test_queue_survives_reopen(self):
        runtime_db.BILI_TASKS.enqueue("https://b23.tv/persist")
        runtime_db.close_db()
        runtime_db.init_db(self.dbpath)
        row = runtime_db.BILI_TASKS.get("https://b23.tv/persist")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "PENDING")


