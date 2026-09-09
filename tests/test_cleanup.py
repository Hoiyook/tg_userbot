"""清理谓词 is_cleanup_message 的单元测试（统一下载链路下的媒体副本保护）。

运行方式（在项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import os
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_cleanup_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import cleanup  # noqa: E402


def make_msg(text="", document=None, video=None, photo=None, media=None):
    """构造消息：默认纯文本（无媒体）；document/video/photo 传入 truthy 值即带媒体。

    MagicMock 的属性会自动生成 truthy Mock（file/document/video/photo/audio），
    会被 is_downloadable 误判成可下载媒体，故显式全部置 None；真实媒体场景
    用 document/video/photo 传入 truthy 值表达。
    """
    m = mock.MagicMock()
    m.id = 1
    m.message = text
    m.file = None
    m.document = document
    m.video = video
    m.photo = photo
    m.audio = None
    m.voice = None
    m.media = media
    return m


DOUYIN_URL = "https://v.douyin.com/AbCdEfG/"


class CleanupMediaCopyGuardTest(unittest.TestCase):
    """转发进收藏夹的媒体副本（含 caption 带链接/通知前缀）不得被清理。"""

    def test_media_with_douyin_caption_kept(self):
        # 解析 bot 回复视频经白名单转发进收藏夹，caption 可能带抖音来源 URL
        msg = make_msg(text=f"标题：可爱的小狐狸 {DOUYIN_URL}", document=mock.MagicMock())
        self.assertFalse(cleanup.is_cleanup_message(msg))

    def test_media_with_notification_prefix_kept(self):
        msg = make_msg(text="✅ 下载完成\n\n收藏的副本", video=mock.MagicMock())
        self.assertFalse(cleanup.is_cleanup_message(msg))

    def test_media_with_command_like_text_kept(self):
        # 即使正文恰好像命令，只要是真媒体就保留
        msg = make_msg(text="/status", photo=mock.MagicMock())
        self.assertFalse(cleanup.is_cleanup_message(msg))


class CleanupTextMessageTest(unittest.TestCase):
    """纯文本程序消息（命令/链接指令/通知前缀）仍照旧清理。"""

    def test_plain_command_cleaned(self):
        self.assertTrue(cleanup.is_cleanup_message(make_msg(text="/status")))

    def test_plain_douyin_link_cleaned(self):
        # 用户自发的抖音链接指令消息（带 WebPage 预览但无真实媒体）
        msg = make_msg(text=DOUYIN_URL, media=mock.MagicMock())
        self.assertTrue(cleanup.is_cleanup_message(msg))

    def test_plain_notification_prefix_cleaned(self):
        msg = make_msg(text="🎬 抖音视频下载完成\n\n文件：x.mp4")
        self.assertTrue(cleanup.is_cleanup_message(msg))

    def test_resolver_notification_prefixes_cleaned(self):
        # 本地解析链的两条新通知也要纳入自动清理（2026-09-06 线上漏清理）
        self.assertTrue(cleanup.is_cleanup_message(
            make_msg(text="🛠 本地解析成功，已入队下载\n\n文件：x.mp4\n链接：…")
        ))
        self.assertTrue(cleanup.is_cleanup_message(
            make_msg(text="❌ 直链下载失败\n\n文件：x.mp4\n请查看 download.log")
        ))

    def test_setcleartime_command_cleaned(self):
        self.assertTrue(cleanup.is_cleanup_message(make_msg(text="/setcleartime 1m")))

    def test_plain_user_content_kept(self):
        # 普通收藏内容既不是命令/链接指令也不是程序通知 → 不清理
        self.assertFalse(cleanup.is_cleanup_message(make_msg(text="今天天气不错")))


if __name__ == "__main__":
    unittest.main()


class CleanupFindTest(unittest.TestCase):
    """/find 命令与「🔍 查询」回复纳入自动清理白名单（2026-09-08）。"""

    def test_find_command_cleaned(self):
        self.assertTrue(cleanup.is_cleanup_message(make_msg(text="/find bl44")))

    def test_find_reply_prefix_cleaned(self):
        msg = make_msg(text="🔍 查询「bl44」匹配 3 处\n\n1. 🔁 待重试 | …")
        self.assertTrue(cleanup.is_cleanup_message(msg))


class ChromeResultNotificationTest(unittest.TestCase):
    """Chrome 下载结果通知持久保留（2026-09-09 验收反馈：通知 85 秒后就被
    自动清理删掉，用户没看到）。结果类通知豁免自动清理（include_persistent
    默认 False 不命中），/clearmsg 显式带 include_persistent=True 才清理。"""

    def test_success_result_not_auto_cleaned(self):
        msg = make_msg(text="✅ Chrome 下载完成\n\n任务 ID：abc\n状态：SUCCESS")
        self.assertFalse(cleanup.is_cleanup_message(msg))

    def test_failed_result_not_auto_cleaned(self):
        msg = make_msg(text="❌ Chrome 下载失败\n\n任务 ID：abc\n状态：FAILED")
        self.assertFalse(cleanup.is_cleanup_message(msg))

    def test_results_cleaned_by_clearmsg_mode(self):
        """/clearmsg 的 include_persistent=True 模式：结果通知仍可批量清理。"""
        msg = make_msg(text="✅ Chrome 下载完成\n\n任务 ID：abc")
        self.assertTrue(cleanup.is_cleanup_message(msg,
                                                   include_persistent=True))

    def test_transient_chrome_replies_still_auto_cleaned(self):
        """瞬态回复（🤖 Chrome 开头的提交/状态报告）照旧自动清理。"""
        msg = make_msg(text="🤖 Chrome 下载任务已提交\n\n任务 ID：abc")
        self.assertTrue(cleanup.is_cleanup_message(msg))
        self.assertTrue(cleanup.is_cleanup_message(msg,
                                                   include_persistent=True))
