"""ehentai 模块测试：URL 解析 / 种子页解析 / 成本页解析 / 命令形态。
跑法：.venv/bin/python -m unittest tests.test_ehentai -v
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest as _btn_shim  # noqa: F401,E402

from tg_userbot import config, ehentai  # noqa: E402


TORRENT_PAGE = """
<table><tr>
<td>Posted: 2026-10-24 09:00</td><td>Size: 49.79 MiB</td>
<td>Seeds: 16</td><td>Peers: 5</td>
<td><a href="https://ehtracker.org/get/4277638/x/f24ad3a354e466e7095a9b29a96324018d76a0b8.torrent">dl</a></td>
<td>[ぼるしち] どりっぷこーひー.zip</td>
</tr><tr>
<td>Posted: 2026-01-01</td><td>Seeds: 2</td>
<td><a href="https://ehtracker.org/get/4277638/y/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.torrent">dl</a></td>
<td>旧版.zip</td>
</tr></table>
"""

ARCHIVER_PAGE = """
<body>Current Funds: 8,443,868 GP
Download Cost:  1,045 GP
Estimated Size:  49.78 MiB
<input type="hidden" name="dltype" value="org">
<input type="submit" name="dlcheck" value="Download Original Archive">
</body>"""

HATH_PAGE = """
<body>The file was successfully prepared
<a href="https://x.hath.network/archive/4277638/abc/12q/0">Click Here To Start Downloadin</a>
</body>"""


class ParseTest(unittest.TestCase):
    def test_gallery_ref(self):
        self.assertEqual(
            ehentai.parse_gallery_ref("https://e-hentai.org/g/4277638/baedd2e998/"),
            (4277638, "baedd2e998"))
        self.assertEqual(
            ehentai.parse_gallery_ref("https://exhentai.org/g/12/abcd1234/"),
            (12, "abcd1234"))
        self.assertIsNone(ehentai.parse_gallery_ref("https://wnacg.com/x"))
        self.assertIsNone(ehentai.parse_gallery_ref("/ehentai"))

    def test_torrent_page_ranking(self):
        torrents = ehentai.parse_torrent_page(TORRENT_PAGE)
        self.assertEqual(len(torrents), 2)
        seeds, name, url = torrents[0]
        self.assertEqual(seeds, 16, "按 seeds 降序取最优")
        self.assertIn("ぼるしち", name)
        self.assertIn("f24ad3a354e466e7095a9b29a96324018d76a0b8", url)

    def test_cost_parse(self):
        m = ehentai._COST_RE.search(ARCHIVER_PAGE)
        self.assertEqual(int(m.group(1).replace(",", "")), 1045)

    def test_hath_link_parse(self):
        m = ehentai._HATH_LINK_RE.search(HATH_PAGE)
        self.assertIn("/archive/", m.group(1))


class CookieTest(unittest.TestCase):
    def test_save_and_read(self):
        import tempfile, json, shutil
        d = tempfile.mkdtemp(prefix="ehck_")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        sec = os.path.join(d, "secrets.json")
        with open(sec, "w") as f:
            json.dump({"tg_proxy": "socks5://x"}, f)
        with mock.patch.object(config, "SECRETS_FILE", sec):
            err = config.save_ehentai_cookie(
                "ipb_member_id=1; ipb_pass_hash=abc", path=sec)
        self.assertIsNone(err)
        data = json.load(open(sec))
        self.assertEqual(data["ehentai_cookie"], "ipb_member_id=1; ipb_pass_hash=abc")
        self.assertEqual(data["tg_proxy"], "socks5://x", "其它字段保留")
        self.assertEqual(config.EHENTAI_COOKIE, "ipb_member_id=1; ipb_pass_hash=abc")


class CommandTest(unittest.TestCase):
    def test_usage_and_bad_url(self):
        async def run():
            return (await ehentai.command_reply("/ehentai"),
                    await ehentai.command_reply("/ehentai https://x.com/y"))
        usage, bad = asyncio.run(run())
        self.assertIn("用法", usage)
        self.assertIn("认不出", bad)

    def test_cookie_command(self):
        async def run():
            return await ehentai.command_reply("/ehentai cookie abc=1")
        with mock.patch.object(config, "save_ehentai_cookie",
                               return_value=None) as sv:
            out = asyncio.run(run())
        sv.assert_called_once_with("abc=1")
        self.assertIn("已保存", out)

    def test_no_torrent_no_cookie_reply(self):
        async def run():
            with mock.patch.object(
                    ehentai, "fetch_torrents_blocking",
                    return_value=[]), \
                  mock.patch.object(
                    ehentai, "fetch_archiver_cost_blocking",
                    side_effect=RuntimeError("未配置 e-hentai cookie")):
                return await ehentai.command_reply(
                    "/ehentai https://e-hentai.org/g/4277638/baedd2e998/ 作者")
        out = asyncio.run(run())
        self.assertIn("无种子", out)
        self.assertIn("cookie", out)

    def test_torrent_route_submits_magnet(self):
        torrents = [(16, "ぼるしち.zip",
                     "https://ehtracker.org/get/1/x/"
                     "f24ad3a354e466e7095a9b29a96324018d76a0b8.torrent")]
        add = mock.MagicMock(return_value=(True, None))

        async def run():
            with mock.patch.object(
                    ehentai, "fetch_torrents_blocking",
                    return_value=torrents), \
                  mock.patch(
                    "tg_userbot.cd2_api.add_offline_download", add), \
                  mock.patch("tg_userbot.torrent_offline.spawn_watch"):
                return await ehentai.command_reply(
                    "/ehentai https://e-hentai.org/g/4277638/baedd2e998/ 作者")
        out = asyncio.run(run())
        self.assertIn("已走种子", out)
        self.assertIn("16 seeds", out)
        # 磁链提交到了 漫画/作者
        magnet, folder = add.call_args[0][0], add.call_args[0][1]
        self.assertIn("btih:f24ad3a354e466e7095a9b29a96324018d76a0b8", magnet)
        self.assertEqual(folder, "/115open/漫画/作者")
        self.assertIn("漫画/作者", out)


if __name__ == "__main__":
    unittest.main()


class TorrentFolderTest(unittest.TestCase):
    """种子/磁链目标目录路由（owner 2026-10-25：尾随作者 → 漫画/作者/）。"""

    def test_resolve_to_folder(self):
        from tg_userbot import torrent_offline as to
        self.assertEqual(to.resolve_to_folder(None), "/115open/云下载")
        self.assertEqual(to.resolve_to_folder("/115open/漫画/直路径"),
                         "/115open/漫画/直路径")
        self.assertEqual(to.resolve_to_folder("某作者/测试"),
                         "/115open/漫画/某作者_测试")

    def test_trailing_author(self):
        from tg_userbot import torrent_offline as to
        url = "https://ehtracker.org/get/1/x/abc.torrent"
        self.assertEqual(
            to.trailing_author(f"{url} 某作者", url), "某作者")
        self.assertIsNone(to.trailing_author(url, url))
        self.assertIsNone(to.trailing_author(f"{url} /cmd", url),
                          "斜杠开头不认作者")

    def test_handle_torrent_url_routes_folder(self):
        import asyncio
        from tg_userbot import torrent_offline as to
        url = ("https://ehtracker.org/get/1/x/"
               "f24ad3a354e466e7095a9b29a96324018d76a0b8.torrent")
        submit = mock.MagicMock(return_value=(True, None))
        watch = mock.MagicMock()

        async def run():
            with mock.patch.object(to, "_submit", submit), \
                    mock.patch.object(to, "spawn_watch", watch):
                return await to.handle_torrent_url(
                    url, to_folder="/115open/漫画/作者甲")
        out = asyncio.run(run())
        self.assertIn("作者甲", out)
        magnet, folder = submit.call_args[0][0], submit.call_args[0][1]
        self.assertEqual(folder, "/115open/漫画/作者甲")
        self.assertEqual(watch.call_args[0][0], "/115open/漫画/作者甲")

    def test_paw_offline_author_routes(self):
        import asyncio
        from tg_userbot import pawchive
        add = mock.MagicMock(return_value=(True, None))
        from tg_userbot import torrent_offline as to
        watch = mock.MagicMock()

        async def run():
            with mock.patch("tg_userbot.cd2_api.add_offline_download", add), \
                    mock.patch.object(to, "spawn_watch", watch), \
                    mock.patch.object(to, "ensure_remote_dir"):
                return await pawchive.offline_reply(
                    "magnet:?xt=urn:btih:aaa\n某作者乙")
        out = asyncio.run(run())
        self.assertIn("漫画/某作者乙", out)
        joined, folder = add.call_args[0][0], add.call_args[0][1]
        self.assertEqual(folder, "/115open/漫画/某作者乙")
        self.assertIn("btih:aaa", joined)
