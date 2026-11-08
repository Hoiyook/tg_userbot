"""B站 cookie 功能的单元测试：browser_cookies 泛化 + save_bilibili_cookie
+ 菜单状态视图。浏览器 Cookie 库全 mock 不触碰真库。
运行方式（项目根目录）：
    .venv/bin/python -m unittest tests.test_bili_cookie -v
"""
import atexit
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_bilicookie_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import browser_cookies  # noqa: E402
from tg_userbot import menu  # noqa: E402


def _fake_cookie(name, value):
    return type("C", (), {"name": name, "value": value, "domain": ".t.com",
                          "path": "/", "expires": 0, "secure": False})


class BrowserCookiesDomainTest(unittest.TestCase):
    """泛化回归：默认 douyin.com 不变，B站传 bilibili.com。"""

    def test_default_domain_is_douyin(self):
        with mock.patch("browser_cookie3.chrome") as bc:
            bc.return_value = [_fake_cookie("sessionid", "s1")]
            cookie, err = browser_cookies.load_browser_cookie_string("chrome")
        self.assertIsNone(err)
        self.assertEqual(cookie, "sessionid=s1")
        bc.assert_called_once_with(domain_name="douyin.com")

    def test_bilibili_domain_passed_through(self):
        with mock.patch("browser_cookie3.chrome") as bc:
            bc.return_value = [_fake_cookie("SESSDATA", "SE"),
                               _fake_cookie("buvid3", "B3")]
            cookie, err = browser_cookies.load_browser_cookie_string(
                "chrome", domain="bilibili.com")
        self.assertIsNone(err)
        self.assertEqual(cookie, "SESSDATA=SE; buvid3=B3")
        bc.assert_called_once_with(domain_name="bilibili.com")

    def test_empty_jar_error_mentions_domain(self):
        with mock.patch("browser_cookie3.chrome") as bc:
            bc.return_value = []
            cookie, err = browser_cookies.load_browser_cookie_string(
                "chrome", domain="bilibili.com")
        self.assertIn("bilibili.com", err)
        self.assertEqual(cookie, "")

    def test_keychain_permission_error_hint(self):
        with mock.patch("browser_cookie3.chrome",
                        side_effect=PermissionError("denied")):
            cookie, err = browser_cookies.load_browser_cookie_string(
                "chrome", domain="bilibili.com")
        self.assertIn("钥匙串", err)

    def test_unsupported_browser(self):
        cookie, err = browser_cookies.load_browser_cookie_string(
            "safari", domain="bilibili.com")
        self.assertIn("不支持", err)
        self.assertEqual(cookie, "")


class SaveBilibiliCookieTest(unittest.TestCase):
    """save_bilibili_cookie 纪律：原子替换 + 保留其它字段 + 内存实时生效。"""

    def setUp(self):
        base = tempfile.mkdtemp(dir=_TMP)
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.secrets = os.path.join(base, "tg_secrets.json")
        with open(self.secrets, "w", encoding="utf-8") as f:
            json.dump({"douyin_cookie": "keep_me"}, f)
        self._old = config.BILIBILI_COOKIE
        self.addCleanup(setattr, config, "BILIBILI_COOKIE", self._old)

    def test_save_updates_file_and_memory(self):
        err = config.save_bilibili_cookie("SESSDATA=xyz",
                                          path=self.secrets)
        self.assertIsNone(err)
        self.assertEqual(config.BILIBILI_COOKIE, "SESSDATA=xyz")
        with open(self.secrets, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["bilibili_cookie"], "SESSDATA=xyz")
        self.assertEqual(data["douyin_cookie"], "keep_me", "其它字段必须保留")

    def test_clear_writes_empty(self):
        config.save_bilibili_cookie("SESSDATA=xyz", path=self.secrets)
        err = config.save_bilibili_cookie("", path=self.secrets)
        self.assertIsNone(err)
        self.assertEqual(config.BILIBILI_COOKIE, "")
        with open(self.secrets, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["bilibili_cookie"], "")

    def test_corrupt_secrets_rejected(self):
        with open(self.secrets, "w", encoding="utf-8") as f:
            f.write("not json {")
        err = config.save_bilibili_cookie("x", path=self.secrets)
        self.assertIn("解析失败", err)
        self.assertEqual(config.BILIBILI_COOKIE, self._old, "内存值不动")


class BiliCookieMenuTest(unittest.TestCase):
    """设置页「🌐 B站Cookie」状态视图（SESSDATA 检测）。"""

    def _old(self):
        return config.BILIBILI_COOKIE

    def setUp(self):
        self._old_value = config.BILIBILI_COOKIE
        self.addCleanup(setattr, config, "BILIBILI_COOKIE", self._old_value)

    def test_unconfigured_view(self):
        with mock.patch.object(config, "BILIBILI_COOKIE", ""):
            text = menu.bili_cookie_status_text()
        self.assertIn("未配置", text)
        self.assertIn("480P", text)

    def test_configured_with_sessdata(self):
        with mock.patch.object(config, "BILIBILI_COOKIE",
                               "buvid3=x; SESSDATA=abc; DedeUserID=1"):
            text = menu.bili_cookie_status_text()
        self.assertIn("SESSDATA ✅", text)
        self.assertIn(config.mask_douyin_cookie(
            "buvid3=x; SESSDATA=abc; DedeUserID=1"), text)

    def test_configured_without_sessdata_warns(self):
        with mock.patch.object(config, "BILIBILI_COOKIE", "buvid3=x"):
            text = menu.bili_cookie_status_text()
        self.assertIn("未检测到 SESSDATA", text)

    def test_buttons_wired(self):
        rows = menu.bili_cookie_menu_buttons()
        flat = [b.data.decode() if isinstance(b.data, bytes) else str(b.data)
                for row in rows for b in row]
        joined = "".join(flat)
        self.assertIn("bili_cookie_set", joined)
        self.assertIn("bili_cookie_clear", joined)
        self.assertIn("bili_cookie_imp", joined)

    def test_settings_row_has_entry(self):
        rows = menu.settings_menu_buttons()
        flat = "".join(str(b.data) for row in rows for b in row)
        self.assertIn("bili_cookie", flat)


if __name__ == "__main__":
    unittest.main()
