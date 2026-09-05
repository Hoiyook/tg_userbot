"""平台链接提取（extract_douyin_urls / extract_instagram_urls）的单元测试。

统一下载链路后 platform 只保留“链接识别 + 转发给解析 bot”，这两个纯提取
函数被 cleanup（清理判定）与 app（链接分支）复用。仅测纯逻辑，不触网。

运行方式（在项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import os
import tempfile
import unittest

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_platform_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import platform  # noqa: E402


class ExtractDouyinUrlsTest(unittest.TestCase):
    def test_short_link(self):
        self.assertEqual(
            platform.extract_douyin_urls("看这个 https://v.douyin.com/AbC/ 不错"),
            ["https://v.douyin.com/AbC/"],
        )

    def test_www_and_domain_forms(self):
        text = (
            "a https://www.douyin.com/video/123 "
            "b https://douyin.com/video/456 "
            "c https://m.douyin.com/share/789"
        )
        self.assertEqual(
            platform.extract_douyin_urls(text),
            [
                "https://www.douyin.com/video/123",
                "https://douyin.com/video/456",
                "https://m.douyin.com/share/789",
            ],
        )

    def test_multiple_links_deduped(self):
        text = "https://v.douyin.com/A/ 和 https://v.douyin.com/A/ 与 https://v.douyin.com/B/"
        self.assertEqual(
            platform.extract_douyin_urls(text),
            ["https://v.douyin.com/A/", "https://v.douyin.com/B/"],
        )

    def test_trailing_cn_punctuation_stripped(self):
        # URL 后紧跟全角句号（无空格）也应被剥掉
        self.assertEqual(
            platform.extract_douyin_urls("点这里https://v.douyin.com/AbC/。"),
            ["https://v.douyin.com/AbC/"],
        )

    def test_empty_and_no_match(self):
        self.assertEqual(platform.extract_douyin_urls(""), [])
        self.assertEqual(platform.extract_douyin_urls("今天没链接"), [])
        self.assertEqual(platform.extract_douyin_urls(None), [])

    def test_instagram_link_not_douyin(self):
        self.assertEqual(
            platform.extract_douyin_urls("https://www.instagram.com/reel/xyz/"),
            [],
        )


class ExtractInstagramUrlsTest(unittest.TestCase):
    def test_full_instagram(self):
        self.assertEqual(
            platform.extract_instagram_urls(
                "图 https://www.instagram.com/p/CxYz123/"
            ),
            ["https://www.instagram.com/p/CxYz123/"],
        )

    def test_short_domain(self):
        self.assertEqual(
            platform.extract_instagram_urls("https://instagr.am/reel/xyz/"),
            ["https://instagr.am/reel/xyz/"],
        )

    def test_douyin_link_not_instagram(self):
        self.assertEqual(
            platform.extract_instagram_urls("https://v.douyin.com/AbC/"),
            [],
        )


class ExtractByPatternTest(unittest.TestCase):
    """extract_urls_by_pattern：通用抽取器行为。"""

    def test_none_or_empty(self):
        self.assertEqual(platform.extract_urls_by_pattern(None, platform.DOUYIN_URL_PATTERN), [])
        self.assertEqual(platform.extract_urls_by_pattern("", platform.DOUYIN_URL_PATTERN), [])

    def test_url_list_ordering_preserved(self):
        text = "x https://a.com/1 y https://a.com/2 z https://a.com/3"
        # 自定义只匹配 a.com 的 pattern（不依赖平台正则）
        import re

        pat = re.compile(r"https?://a\.com/[^\s<>\"]+", re.IGNORECASE)
        self.assertEqual(
            platform.extract_urls_by_pattern(text, pat),
            ["https://a.com/1", "https://a.com/2", "https://a.com/3"],
        )


if __name__ == "__main__":
    unittest.main()
