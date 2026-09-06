"""本地解析链的纯函数与降级行为（全部离线，不 import f2、不联网）。

覆盖：bit_rate 最高档挑选、链接评论提取、url 任务命名、f2 import 失败
时的静默降级（import 副作用被 try/except 包住是 resolver 的关键契约）。
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_resolver_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import naming, resolver  # noqa: E402
from tg_userbot.platform import extract_link_comment  # noqa: E402


def _aweme(bit_rate, play_urls=None):
    play = {"url_list": play_urls if play_urls is not None else ["默认档"]}
    return {"video": {"play_addr": play, "bit_rate": bit_rate}}


class PickBestVideoUrlTest(unittest.TestCase):
    """pick_best_video_url：选最高码率档。"""

    def test_picks_highest_bitrate(self):
        detail = _aweme([
            {"bit_rate": 540, "play_addr": {"url_list": ["低档"]}},
            {"bit_rate": 1080, "play_addr": {"url_list": ["高档", "备选"]}},
            {"bit_rate": 720, "play_addr": {"url_list": ["中档"]}},
        ])
        self.assertEqual(resolver.pick_best_video_url(detail), "高档")

    def test_empty_bit_rate_falls_back_to_play_addr(self):
        detail = _aweme([], play_urls=["默认档"])
        self.assertEqual(resolver.pick_best_video_url(detail), "默认档")

    def test_bit_rate_items_without_urls_are_skipped(self):
        detail = _aweme([
            {"bit_rate": 1080, "play_addr": {"url_list": []}},
            {"bit_rate": 720, "play_addr": {"url_list": ["可用的档"]}},
        ])
        self.assertEqual(resolver.pick_best_video_url(detail), "可用的档")

    def test_nothing_available_returns_none(self):
        # play_addr 也没有任何 URL → 真的什么都拿不到
        self.assertIsNone(resolver.pick_best_video_url(_aweme([], play_urls=[])))
        self.assertIsNone(resolver.pick_best_video_url(None))


class ExtractLinkCommentTest(unittest.TestCase):
    """链接消息里的评论提取（本地解析任务的命名标注）。"""

    def test_comment_before_url(self):
        self.assertEqual(
            extract_link_comment("自存 https://v.douyin.com/x/", ["https://v.douyin.com/x/"]),
            "自存",
        )

    def test_pure_url_returns_none(self):
        self.assertIsNone(extract_link_comment("https://v.douyin.com/x/", ["https://v.douyin.com/x/"]))

    def test_command_returns_none(self):
        self.assertIsNone(extract_link_comment("/wl https://v.douyin.com/x/", ["https://v.douyin.com/x/"]))

    def test_empty_text_returns_none(self):
        self.assertIsNone(extract_link_comment("", ["x"]))


class ComputeUrlFilenameTest(unittest.TestCase):
    """compute_url_filename：日期前缀 + #标注 + 标题，与媒体流同一视觉。"""

    _T = datetime(2026, 9, 6, 14, 30, 5)

    def test_date_prefix_and_title(self):
        self.assertEqual(
            naming.compute_url_filename("一个标题", self._T),
            "26-09-06 一个标题.mp4",
        )

    def test_label_piece_goes_first(self):
        self.assertEqual(
            naming.compute_url_filename("标题", self._T, label="自存"),
            "26-09-06 #自存 标题.mp4",
        )

    def test_empty_title_falls_back_to_timestamp(self):
        name = naming.compute_url_filename("", self._T)
        self.assertEqual(name, "视频_20260906_143005.mp4")

    def test_byte_budget_trims_title_keeps_label(self):
        long_title = "很" * 200
        name = naming.compute_url_filename(long_title, self._T, label="标")
        self.assertLessEqual(len(name.encode("utf-8")), 200)
        self.assertTrue(name.startswith("26-09-06 #标 "))

    def test_illegal_chars_sanitized(self):
        name = naming.compute_url_filename("a/b:c*d", self._T)
        self.assertEqual(name, "26-09-06 a_b_c_d.mp4")


class ResolveDouyinDegradationTest(unittest.TestCase):
    """resolve_douyin 的降级契约：任何 f2 侧坏味道 → None，不抛异常。"""

    def setUp(self):
        # 清掉「只警告一次」的标记，让每个用例独立断言
        resolver._f2_unavailable_warned = False

    def test_import_failure_returns_none_async(self):
        import asyncio
        with mock.patch.dict(sys.modules, {
            "f2.apps.douyin.filter": None,
            "f2.apps.douyin.handler": None,
            "f2.apps.douyin.utils": None,
        }):
            result = asyncio.new_event_loop().run_until_complete(
                resolver.resolve_douyin("https://v.douyin.com/x/")
            )
        self.assertIsNone(result)
