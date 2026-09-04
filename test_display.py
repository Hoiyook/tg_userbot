"""列表展示增强（最终文件名 + 来源链接）的单元测试。

运行方式（在项目根目录）：
    .venv/bin/python -m unittest test_display -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

# 必须在导入 tg_userbot_final 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_display_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tg_userbot_final as tg  # noqa: E402


def fake_message(filename, caption="", **media_flags):
    """构造一个足够真实的假消息（MagicMock 兜底未设置属性）。"""
    message = mock.MagicMock()
    message.id = 1
    message.file.name = filename
    message.file.size = 100
    message.fwd_from = None
    message.message = caption
    message.date = None
    for attr in ("photo", "video", "audio", "voice", "document", "media"):
        setattr(message, attr, media_flags.get(attr, None))
    return message


class ComputeFinalFilenameTest(unittest.TestCase):
    """compute_final_filename：从消息计算最终落盘文件名。"""

    def test_caption_prepended_to_name(self):
        message = fake_message("a.mp4", caption="标题")
        self.assertEqual(
            tg.compute_final_filename(message), "标题 - a.mp4"
        )

    def test_meaningless_uuid_name_with_caption(self):
        message = fake_message(
            "c80d0ff8-4fdb-4762-8b97-800612217e4c.mp4", caption="标题"
        )
        self.assertEqual(
            tg.compute_final_filename(message), "标题.mp4"
        )

    def test_meaningless_name_without_caption_falls_back(self):
        message = fake_message("c80d0ff8-4fdb-4762-8b97-800612217e4c.mp4")
        result = tg.compute_final_filename(message)
        self.assertRegex(result, r"^file_\d{8}_\d{6}\.mp4$")

    def test_plain_name_without_caption_kept(self):
        message = fake_message("葉瞬光_Handjob_Uncensored_CC.mp4")
        self.assertEqual(
            tg.compute_final_filename(message),
            "葉瞬光_Handjob_Uncensored_CC.mp4",
        )

    def test_missing_extension_inferred_from_video(self):
        message = fake_message("video_no_ext")
        message.video = True
        message.file.mime_type = "video/mp4"
        result = tg.compute_final_filename(message)
        self.assertEqual(result, "video_no_ext.mp4")


class MessageLinkTest(unittest.TestCase):
    """message_link：Telegram 消息链接生成。"""

    def test_channel_id_returns_tme_link(self):
        self.assertEqual(
            tg.message_link(-1001234567890, 25965),
            "https://t.me/c/1234567890/25965",
        )

    def test_private_chat_returns_none(self):
        # Saved Messages / 私聊（正 id）没有链接格式
        self.assertIsNone(tg.message_link(987654321, 25965))

    def test_legacy_group_returns_none(self):
        self.assertIsNone(tg.message_link(-12345, 10))

    def test_none_returns_none(self):
        self.assertIsNone(tg.message_link(None, 5))
        self.assertIsNone(tg.message_link(-100123, None))


class QueueDisplayTest(unittest.TestCase):
    """队列/待重试列表展示最终文件名与来源。"""

    def test_queue_line_shows_final_name(self):
        queue = {"tasks": [], "retry": []}
        tg.queue_enqueue(queue, {
            "kind": "media", "chat_id": 987654321, "msg_id": 25965,
            "final_name": "标题 - 葉瞬光.mp4", "label": "葉瞬光.mp4",
        })
        text = tg.format_queue_text(queue)
        self.assertIn("标题 - 葉瞬光.mp4", text)
        self.assertIn("#25965", text)  # 私聊无链接，显示 #消息ID

    def test_queue_line_shows_channel_link(self):
        queue = {"tasks": [], "retry": []}
        tg.queue_enqueue(queue, {
            "kind": "media", "chat_id": -1001234567890, "msg_id": 25965,
            "final_name": "a.mp4", "label": "a.mp4",
        })
        text = tg.format_queue_text(queue)
        self.assertIn("https://t.me/c/1234567890/25965", text)

    def test_retry_line_shows_final_name_and_attempts(self):
        queue = {"tasks": [], "retry": []}
        rec = tg.queue_enqueue(queue, {
            "kind": "media", "chat_id": 987654321, "msg_id": 1,
            "final_name": "a.mp4", "label": "a.mp4",
        })
        tg.queue_fail_to_retry(queue, rec)
        text = tg.format_retry_text(queue)
        self.assertIn("a.mp4", text)
        self.assertIn("已尝试 1 次", text)

    def test_link_task_without_final_name_shows_url(self):
        queue = {"tasks": [], "retry": []}
        tg.queue_enqueue(queue, {
            "kind": "douyin", "url": "https://v.douyin.com/abc/",
            "label": "https://v.douyin.com/abc/",
        })
        text = tg.format_queue_text(queue)
        self.assertIn("https://v.douyin.com/abc/", text)


class MessageSourceLinkTest(unittest.TestCase):
    """message_source_link：转发消息链到原频道消息，否则用消息自身 chat。"""

    def setUp(self):
        from telethon.tl.types import PeerChannel
        self.PeerChannel = PeerChannel

    def test_forwarded_channel_message_links_to_original(self):
        message = mock.MagicMock()
        message.id = 1
        message.fwd_from.from_id = self.PeerChannel(1234567890)
        message.fwd_from.channel_post = 42
        self.assertEqual(
            tg.message_source_link(message, fallback_chat_id=987654321),
            "https://t.me/c/1234567890/42",
        )

    def test_forward_without_channel_post_falls_back(self):
        message = mock.MagicMock()
        message.id = 7
        message.fwd_from.from_id = self.PeerChannel(123)
        message.fwd_from.channel_post = None
        self.assertEqual(
            tg.message_source_link(message, fallback_chat_id=-1001234567890),
            "https://t.me/c/1234567890/7",
        )

    def test_not_forwarded_private_chat_returns_none(self):
        message = mock.MagicMock()
        message.id = 7
        message.fwd_from = None
        self.assertIsNone(
            tg.message_source_link(message, fallback_chat_id=987654321)
        )

    def test_queue_display_uses_stored_source_link(self):
        queue = {"tasks": [], "retry": []}
        tg.queue_enqueue(queue, {
            "kind": "media", "chat_id": 987654321, "msg_id": 26005,
            "final_name": "a.mp4", "label": "a.mp4",
            "source_link": "https://t.me/c/1234567890/42",
        })
        text = tg.format_queue_text(queue)
        self.assertIn("https://t.me/c/1234567890/42", text)


class ForwardSourceInfoTest(unittest.TestCase):
    """forward_source_info：从转发头提取来源（不依赖 get_entity）。"""

    def setUp(self):
        from telethon.tl.types import PeerChannel
        self.PeerChannel = PeerChannel

    def test_private_channel_with_name(self):
        fwd = mock.Mock()
        fwd.from_id = self.PeerChannel(1234567890)
        fwd.from_name = "私密频道"
        chat_id, name = tg.forward_source_info(fwd)
        self.assertEqual(chat_id, -1001234567890)
        self.assertEqual(name, "私密频道")

    def test_without_name_returns_empty(self):
        fwd = mock.Mock()
        fwd.from_id = self.PeerChannel(123)
        fwd.from_name = None
        chat_id, name = tg.forward_source_info(fwd)
        self.assertEqual(chat_id, -1000000000123)
        self.assertEqual(name, "")

    def test_no_from_id_returns_none(self):
        fwd = mock.Mock()
        fwd.from_id = None
        self.assertEqual(tg.forward_source_info(fwd), (None, None))


class ProgressLinkTest(unittest.TestCase):
    """progress_text：进行中下载展示来源链接。"""

    def setUp(self):
        tg.ACTIVE_DOWNLOADS.clear()

    def tearDown(self):
        tg.ACTIVE_DOWNLOADS.clear()

    def test_progress_shows_channel_link(self):
        tg.ACTIVE_DOWNLOADS[1] = {
            "label": "普通", "filename": "a.mp4",
            "percent": 10.0, "downloaded": 100, "total": 1000,
            "link": "https://t.me/c/1234567890/25965",
        }
        text = tg.progress_text()
        self.assertIn("a.mp4", text)
        self.assertIn("https://t.me/c/1234567890/25965", text)

    def test_progress_without_link(self):
        tg.ACTIVE_DOWNLOADS[1] = {
            "label": "普通", "filename": "a.mp4",
            "percent": 10.0, "downloaded": 100, "total": 1000,
            "link": None,
        }
        text = tg.progress_text()
        self.assertIn("a.mp4", text)
        self.assertNotIn("t.me", text)


if __name__ == "__main__":
    unittest.main()
