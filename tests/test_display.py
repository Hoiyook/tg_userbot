"""列表展示增强（最终文件名 + 来源链接）的单元测试。

运行方式（在项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_display_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import state  # noqa: E402
from tg_userbot import config, naming, sources, queue, text  # noqa: E402
from tg_userbot import download as download_mod  # noqa: E402


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
            naming.compute_final_filename(message), "标题 - a.mp4"
        )

    def test_meaningless_uuid_name_with_caption(self):
        message = fake_message(
            "c80d0ff8-4fdb-4762-8b97-800612217e4c.mp4", caption="标题"
        )
        self.assertEqual(
            naming.compute_final_filename(message), "标题.mp4"
        )

    def test_meaningless_name_without_caption_falls_back(self):
        message = fake_message("c80d0ff8-4fdb-4762-8b97-800612217e4c.mp4")
        result = naming.compute_final_filename(message)
        self.assertRegex(result, r"^file_\d{8}_\d{6}\.mp4$")

    def test_plain_name_without_caption_kept(self):
        message = fake_message("葉瞬光_Handjob_Uncensored_CC.mp4")
        self.assertEqual(
            naming.compute_final_filename(message),
            "葉瞬光_Handjob_Uncensored_CC.mp4",
        )

    def test_missing_extension_inferred_from_video(self):
        message = fake_message("video_no_ext")
        message.video = True
        message.file.mime_type = "video/mp4"
        result = naming.compute_final_filename(message)
        self.assertEqual(result, "video_no_ext.mp4")


class DatePrefixNamingTest(unittest.TestCase):
    """compute_final_filename：文件名前缀原消息日期（'YY-MM-DD '）。"""

    D = datetime(2026, 9, 5, 14, 30, 25)

    def _msg(self, filename, caption="", **media_flags):
        m = fake_message(filename, caption=caption, **media_flags)
        m.date = self.D
        return m

    def test_date_prefix_on_caption_name(self):
        message = self._msg("a.mp4", caption="标题")
        self.assertEqual(
            naming.compute_final_filename(message), "26-09-05 标题 - a.mp4"
        )

    def test_date_prefix_on_plain_name(self):
        message = self._msg("葉瞬光_Handjob_Uncensored_CC.mp4")
        self.assertEqual(
            naming.compute_final_filename(message),
            "26-09-05 葉瞬光_Handjob_Uncensored_CC.mp4",
        )

    def test_date_prefix_on_uuid_with_caption(self):
        message = self._msg("c80d0ff8-4fdb-4762-8b97-800612217e4c.mp4", caption="标题")
        self.assertEqual(
            naming.compute_final_filename(message), "26-09-05 标题.mp4"
        )

    def test_fallback_timestamp_not_prefixed(self):
        # 兜底名 媒体类型_时间戳 已含日期，不再前缀，避免 '26-09-05 video_2026...'
        message = self._msg("c80d0ff8-4fdb-4762-8b97-800612217e4c.mp4")
        result = naming.compute_final_filename(message)
        self.assertRegex(result, r"^file_\d{8}_\d{6}\.mp4$")
        self.assertNotIn("26-09-05", result)

    def test_no_date_means_no_prefix(self):
        message = fake_message("a.mp4", caption="标题")  # date=None
        self.assertEqual(naming.compute_final_filename(message), "标题 - a.mp4")


class ForwardedOriginalDateTest(unittest.TestCase):
    """date_prefix：转发副本的日期前缀取转发来源（fwd_from.date）的原始日期。

    message.date 对转发副本是转发当天；用户要的是内容最初发布的日期，它只在
    fwd_from.date 里。fwd_from 缺失/缺日期时退回 message.date（现状）。电报
    日期是 UTC 感知时间，按本地时区渲染（= 客户端里看到的日期）。
    """

    def _msg(self, filename, caption="", msg_date=None, fwd_date=None):
        m = fake_message(filename, caption=caption)
        if msg_date is not None:
            m.date = msg_date
        if fwd_date is not None:
            m.fwd_from = mock.Mock(date=fwd_date)
        return m

    def test_forwarded_copy_uses_original_date(self):
        # 转发当天 09-06，原帖 09-01（UTC 感知时间）→ 前缀应为原帖本地日期
        fwd_date = datetime(
            2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc
        )
        message = self._msg(
            "a.mp4", caption="标题",
            msg_date=datetime(2026, 9, 6, 12, 0, 0),
            fwd_date=fwd_date,
        )
        expected_prefix = fwd_date.astimezone().strftime("%y-%m-%d ")
        self.assertEqual(
            naming.compute_final_filename(message),
            f"{expected_prefix}标题 - a.mp4",
        )

    def test_forward_without_fwd_from_keeps_message_date(self):
        # 无转发头（如收藏夹自发送）→ 仍用消息自身日期
        message = self._msg(
            "a.mp4", caption="标题",
            msg_date=datetime(2026, 9, 6, 12, 0, 0),
        )
        self.assertEqual(
            naming.compute_final_filename(message), "26-09-06 标题 - a.mp4"
        )

    def test_aware_message_date_rendered_in_local_tz(self):
        # UTC 感知的消息日期按本地时区渲染（+8 时区下 20:00 UTC 属次日）
        message = self._msg(
            "a.mp4", caption="标题",
            msg_date=datetime(2026, 9, 5, 20, 0, 0, tzinfo=timezone.utc),
        )
        expected = datetime(
            2026, 9, 5, 20, 0, 0, tzinfo=timezone.utc
        ).astimezone().strftime("%y-%m-%d ")
        self.assertEqual(
            naming.compute_final_filename(message),
            f"{expected}标题 - a.mp4",
        )


class CaptionOverrideNamingTest(unittest.TestCase):
    """compute_final_filename(message, caption=...)：相册继承说明用于命名。

    转发副本补不了 caption，调用方把从源 chat 继承的同组说明（album_caption）
    作为 caption 参数传入，无文字图片沿用相册标题而非 photo_时间戳 兜底。
    """

    D = datetime(2026, 9, 5, 14, 30, 25)

    def test_captionless_photo_with_meaningless_name_uses_override(self):
        # 相册无文字图片，原名是无意义 UUID → 用继承的说明（+ 推断 .jpg）
        m = fake_message("c80d0ff8-4fdb-4762-8b97-800612217e4c.jpg")
        m.photo = True
        m.date = self.D
        result = naming.compute_final_filename(m, caption="作者：#Furatto 绝区零")
        self.assertEqual(result, "26-09-05 作者：#Furatto 绝区零.jpg")

    def test_captionless_photo_with_real_name_keeps_name(self):
        # 无文字但原名有意义（如 IMG_1234.jpg）→ 说明拼在原名前
        m = fake_message("IMG_1234.jpg")
        m.photo = True
        m.date = self.D
        result = naming.compute_final_filename(m, caption="作者：#Furatto")
        self.assertEqual(result, "26-09-05 作者：#Furatto - IMG_1234.jpg")

    def test_no_caption_no_override_falls_back_to_timestamp(self):
        # 继承说明读不到、消息也无文字 → 仍回 媒体类型_时间戳 兜底
        m = fake_message("c80d0ff8-4fdb-4762-8b97-800612217e4c.jpg")
        m.photo = True
        m.date = self.D
        result = naming.compute_final_filename(m, caption="")
        self.assertRegex(result, r"^photo_\d{8}_\d{6}\.jpg$")

    def test_caption_none_defaults_to_message_text(self):
        # caption=None（缺省）＝取消息自带文字，行为不变
        m = fake_message("a.mp4", caption="标题")
        m.date = self.D
        self.assertEqual(
            naming.compute_final_filename(m, caption=None),
            "26-09-05 标题 - a.mp4",
        )


class LabelNamingTest(unittest.TestCase):
    """compute_final_filename(..., label=)：手工转发评论作 #标注拼到命名最前。"""

    D = datetime(2026, 9, 6, 11, 30, 25)

    def _msg(self, filename, caption="", **media_flags):
        m = fake_message(filename, caption=caption, **media_flags)
        m.date = self.D
        return m

    def test_label_prepends_hash_before_caption(self):
        # 用户实测格式：<date> #自存 原caption - 原名
        m = self._msg("a.mp4", caption="标题")
        self.assertEqual(
            naming.compute_final_filename(m, label="自存"),
            "26-09-06 #自存 标题 - a.mp4",
        )

    def test_label_alone_with_no_caption(self):
        m = self._msg("a.mp4")
        self.assertEqual(
            naming.compute_final_filename(m, label="自存"),
            "26-09-06 #自存 - a.mp4",
        )

    def test_label_on_meaningless_uuid_caption(self):
        # 抖音视频原名无意义(UUID)：原 caption 拼在 #标注后、不挂 UUID
        m = self._msg("c80d0ff8-4fdb-4762-8b97-800612217e4c.mp4", caption="标题")
        self.assertEqual(
            naming.compute_final_filename(m, label="自存"),
            "26-09-06 #自存 标题.mp4",
        )

    def test_label_rescues_meaningless_name_from_fallback(self):
        # 无意义原名 + 无 caption + 有标注 → 用 #标注 兜底，不再落 媒体类型_时间戳
        m = self._msg("c80d0ff8-4fdb-4762-8b97-800612217e4c.mp4")
        self.assertEqual(
            naming.compute_final_filename(m, label="自存"),
            "26-09-06 #自存.mp4",
        )

    def test_label_user_pretyped_hash_not_doubled(self):
        # 用户自己已打 '#' → 代码不再重复加，避免 '##自存'
        m = self._msg("a.mp4", caption="标题")
        self.assertEqual(
            naming.compute_final_filename(m, label="#自存"),
            "26-09-06 #自存 标题 - a.mp4",
        )

    def test_label_only_hash_is_ignored(self):
        m = self._msg("a.mp4", caption="标题")
        self.assertEqual(
            naming.compute_final_filename(m, label="#"),
            "26-09-06 标题 - a.mp4",
        )

    def test_label_none_unchanged(self):
        # 无标注 = 缺省旧行为（caption 直接拼）
        m = self._msg("a.mp4", caption="标题")
        self.assertEqual(
            naming.compute_final_filename(m, caption=None),
            "26-09-06 标题 - a.mp4",
        )


class LabelBudgetTrimTest(unittest.TestCase):
    """超长名：先裁原 caption、再动 #标注、最后才截文件名（download 用 max_bytes）。"""

    D = datetime(2026, 9, 6, 11, 30, 25)

    def test_trims_caption_first_keeps_filename(self):
        # caption 240 字节超限：应裁 caption 保 原名 与 #标注，总体 ≤ 上限
        m = fake_message("葉瞬光_Handjob_Uncensored_CC.mp4", caption="汉" * 80)
        m.date = self.D
        result = naming.compute_final_filename(
            m, label="自存", max_bytes=config.MAX_FILENAME_BYTES
        )
        self.assertLessEqual(len(result.encode("utf-8")), config.MAX_FILENAME_BYTES)
        self.assertTrue(result.endswith("葉瞬光_Handjob_Uncensored_CC.mp4"))
        self.assertIn("#自存", result)  # 标注被保留
        self.assertNotIn("汉" * 80, result)  # 原 caption 被裁短
        result.encode("utf-8")  # 合法 UTF-8

    def test_keeps_caption_and_label_when_within_budget(self):
        m = fake_message("a.mp4", caption="标题")
        m.date = self.D
        result = naming.compute_final_filename(
            m, label="自存", max_bytes=config.MAX_FILENAME_BYTES
        )
        self.assertEqual(result, "26-09-06 #自存 标题 - a.mp4")

    def test_trims_caption_even_before_truncating_label_only_when_forced(self):
        # 标注 + 极长 caption：标注整条保留，caption 被裁到放不下为止
        m = fake_message("a.mp4", caption="汉" * 80)
        m.date = self.D
        result = naming.compute_final_filename(
            m, label="自存", max_bytes=config.MAX_FILENAME_BYTES
        )
        self.assertLessEqual(len(result.encode("utf-8")), config.MAX_FILENAME_BYTES)
        self.assertTrue(result.endswith("- a.mp4"))
        self.assertTrue(result.startswith("26-09-06 #自存 "))
        self.assertNotEqual(result, "26-09-06 #自存 " + "汉" * 80 + " - a.mp4")

    def test_filename_truncated_only_as_last_resort(self):
        # 原名本身就超限（预算小到连 prefix+原名 都放不下）→ 最后手段才截文件名
        m = fake_message("汉" * 40 + ".mkv", caption="标题")
        m.date = self.D
        result = naming.compute_final_filename(
            m, label="自存", max_bytes=100
        )
        self.assertLessEqual(len(result.encode("utf-8")), 100)
        self.assertTrue(result.endswith(".mkv"))  # 扩展名保住
        # caption/label 一点预算都分不到时整段被弃，只保留被截的原名部分
        self.assertNotIn("标题", result)
        self.assertNotIn("#自存", result)

    def test_budget_none_keeps_full_uncut(self):
        # max_bytes=None（队列展示/单测默认）不裁剪
        m = fake_message("a.mp4", caption="汉" * 80)
        m.date = self.D
        result = naming.compute_final_filename(m, label="自存", max_bytes=None)
        self.assertIn("汉" * 80, result)
        self.assertTrue(result.startswith("26-09-06 #自存 "))

    def test_label_piece_helper(self):
        self.assertEqual(naming._label_piece(None), "")
        self.assertEqual(naming._label_piece(""), "")
        self.assertEqual(naming._label_piece("  "), "")
        self.assertEqual(naming._label_piece("自存"), "#自存")
        self.assertEqual(naming._label_piece("#自存"), "#自存")
        self.assertEqual(naming._label_piece("#"), "")


class PickGroupCaptionTextTest(unittest.TestCase):
    """相册 caption 继承：从同 grouped_id 兄弟里挑带文字的成员文本。"""

    @staticmethod
    def _member(gid, text="", msg_id=0):
        m = mock.MagicMock()
        m.id = msg_id
        m.grouped_id = gid
        m.message = text
        return m

    def test_picks_text_from_caption_member(self):
        # 相册里视频带文字、两张图片无文字 → 返回视频的文字
        gid = 123456
        siblings = [
            self._member(gid, "作者：#Furatto", 36701),  # 视频（带 caption）
            self._member(gid, "", 36702),                # 图片 1
            self._member(gid, "", 36703),                # 图片 2
        ]
        self.assertEqual(
            naming.pick_group_caption_text(siblings, gid), "作者：#Furatto"
        )

    def test_ignores_other_grouped_id(self):
        siblings = [
            self._member(999, "别的相册文字", 1),
            self._member(123, "本相册文字", 2),
        ]
        self.assertEqual(
            naming.pick_group_caption_text(siblings, 123), "本相册文字"
        )

    def test_no_text_returns_empty(self):
        gid = 123456
        siblings = [self._member(gid, ""), self._member(gid, "  ")]
        self.assertEqual(naming.pick_group_caption_text(siblings, gid), "")

    def test_none_grouped_or_empty_list(self):
        self.assertEqual(naming.pick_group_caption_text([], 123), "")
        self.assertEqual(naming.pick_group_caption_text(None, 123), "")
        self.assertEqual(
            naming.pick_group_caption_text([self._member(123, "x")], None), ""
        )

    def test_handles_bad_objects(self):
        # 兄弟里混入异常对象不抛错、跳过继续
        gid = 5
        bad = object()
        good = self._member(gid, "标题")
        self.assertEqual(naming.pick_group_caption_text([bad, good], gid), "标题")


class TruncateFilenameTest(unittest.TestCase):
    """truncate_filename：把超长文件名按 UTF-8 字节整字截短（防 Errno 63）。"""

    def test_short_name_unchanged(self):
        name = "普通视频.mp4"
        self.assertEqual(naming.truncate_filename(name), name)

    def test_ascii_under_limit_unchanged(self):
        name = "a" * 150 + ".mp4"
        self.assertEqual(naming.truncate_filename(name), name)

    def test_long_chinese_truncated_within_bytes(self):
        # 复现现场：超长标题整段拼进文件名
        long_caption = "标题_" + "AI漫剧重点不在漫剧在AI" * 20
        name = f"{long_caption}_21_14.mp4"
        result = naming.truncate_filename(name)
        self.assertLessEqual(len(result.encode("utf-8")), config.MAX_FILENAME_BYTES)
        self.assertTrue(result.endswith(".mp4"))
        result.encode("utf-8")  # 必须是合法 UTF-8（不抛异常）

    def test_truncation_keeps_extension_and_prefix(self):
        stem = "汉" * 80  # 240 字节，超出默认上限
        original = stem + ".mkv"
        result = naming.truncate_filename(original)
        self.assertTrue(result.endswith(".mkv"))
        self.assertLessEqual(len(result.encode("utf-8")), config.MAX_FILENAME_BYTES)
        self.assertNotEqual(result, original)
        # 扩展名被保留，截断只发生在主体尾部（去扩展名后应是原主体的前缀）
        self.assertTrue(original.split(".")[0].startswith(result.split(".")[0]))
        self.assertLess(len(result.split(".")[0]), len(original.split(".")[0]))

    def test_explicit_smaller_limit(self):
        original = "汉" * 40 + ".mp4"  # 120+4 字节
        result = naming.truncate_filename(original, max_bytes=50)
        self.assertLessEqual(len(result.encode("utf-8")), 50)
        self.assertTrue(result.endswith(".mp4"))

    def test_empty_and_whitespace_names(self):
        self.assertEqual(naming.truncate_filename(""), "")
        self.assertEqual(naming.truncate_filename(None), "")

    def test_no_partial_utf8_char(self):
        # 每个汉字 3 字节；主体预算 100 字节 → 33 个整字（99 字节），第 34 字被整字丢弃
        result = naming.truncate_filename("汉" * 100, max_bytes=100)
        self.assertEqual(result, "汉" * 33)
        self.assertEqual(len(result.encode("utf-8")), 99)
        self.assertLessEqual(len(result.encode("utf-8")), 100)


class MessageLinkTest(unittest.TestCase):
    """message_link：Telegram 消息链接生成。"""

    def test_channel_id_returns_tme_link(self):
        self.assertEqual(
            sources.message_link(-1001234567890, 25965),
            "https://t.me/c/1234567890/25965",
        )

    def test_private_chat_returns_none(self):
        # Saved Messages / 私聊（正 id）没有链接格式
        self.assertIsNone(sources.message_link(987654321, 25965))

    def test_legacy_group_returns_none(self):
        self.assertIsNone(sources.message_link(-12345, 10))

    def test_none_returns_none(self):
        self.assertIsNone(sources.message_link(None, 5))
        self.assertIsNone(sources.message_link(-100123, None))


class QueueDisplayTest(unittest.TestCase):
    """队列/待重试列表展示最终文件名与来源。"""

    def test_queue_line_shows_final_name(self):
        q = {"tasks": [], "retry": []}
        queue.queue_enqueue(q, {
            "kind": "media", "chat_id": 987654321, "msg_id": 25965,
            "final_name": "标题 - 葉瞬光.mp4", "label": "葉瞬光.mp4",
        })
        text = queue.format_queue_text(q)
        self.assertIn("标题 - 葉瞬光.mp4", text)
        self.assertIn("#25965", text)  # 私聊无链接，显示 #消息ID

    def test_queue_line_shows_channel_link(self):
        q = {"tasks": [], "retry": []}
        queue.queue_enqueue(q, {
            "kind": "media", "chat_id": -1001234567890, "msg_id": 25965,
            "final_name": "a.mp4", "label": "a.mp4",
        })
        text = queue.format_queue_text(q)
        self.assertIn("https://t.me/c/1234567890/25965", text)

    def test_retry_line_shows_final_name_and_attempts(self):
        q = {"tasks": [], "retry": []}
        rec = queue.queue_enqueue(q, {
            "kind": "media", "chat_id": 987654321, "msg_id": 1,
            "final_name": "a.mp4", "label": "a.mp4",
        })
        queue.queue_fail_to_retry(q, rec)
        text = queue.format_retry_text(q)
        self.assertIn("a.mp4", text)
        self.assertIn("已尝试 1 次", text)

    def test_link_task_without_final_name_shows_url(self):
        q = {"tasks": [], "retry": []}
        queue.queue_enqueue(q, {
            "kind": "douyin", "url": "https://v.douyin.com/abc/",
            "label": "https://v.douyin.com/abc/",
        })
        text = queue.format_queue_text(q)
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
            sources.message_source_link(message, fallback_chat_id=987654321),
            "https://t.me/c/1234567890/42",
        )

    def test_forward_without_channel_post_falls_back(self):
        message = mock.MagicMock()
        message.id = 7
        message.fwd_from.from_id = self.PeerChannel(123)
        message.fwd_from.channel_post = None
        self.assertEqual(
            sources.message_source_link(message, fallback_chat_id=-1001234567890),
            "https://t.me/c/1234567890/7",
        )

    def test_not_forwarded_private_chat_returns_none(self):
        message = mock.MagicMock()
        message.id = 7
        message.fwd_from = None
        self.assertIsNone(
            sources.message_source_link(message, fallback_chat_id=987654321)
        )

    def test_queue_display_uses_stored_source_link(self):
        q = {"tasks": [], "retry": []}
        queue.queue_enqueue(q, {
            "kind": "media", "chat_id": 987654321, "msg_id": 26005,
            "final_name": "a.mp4", "label": "a.mp4",
            "source_link": "https://t.me/c/1234567890/42",
        })
        text = queue.format_queue_text(q)
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
        chat_id, name = sources.forward_source_info(fwd)
        self.assertEqual(chat_id, -1001234567890)
        self.assertEqual(name, "私密频道")

    def test_without_name_returns_empty(self):
        fwd = mock.Mock()
        fwd.from_id = self.PeerChannel(123)
        fwd.from_name = None
        chat_id, name = sources.forward_source_info(fwd)
        self.assertEqual(chat_id, -1000000000123)
        self.assertEqual(name, "")

    def test_no_from_id_returns_none(self):
        fwd = mock.Mock()
        fwd.from_id = None
        self.assertEqual(sources.forward_source_info(fwd), (None, None))


class ProgressLinkTest(unittest.TestCase):
    """progress_text：进行中下载展示来源链接。"""

    def setUp(self):
        state.ACTIVE_DOWNLOADS.clear()

    def tearDown(self):
        state.ACTIVE_DOWNLOADS.clear()

    def test_progress_shows_channel_link(self):
        state.ACTIVE_DOWNLOADS[1] = {
            "label": "普通", "filename": "a.mp4",
            "percent": 10.0, "downloaded": 100, "total": 1000,
            "link": "https://t.me/c/1234567890/25965",
        }
        body = text.progress_text()
        self.assertIn("a.mp4", body)
        self.assertIn("https://t.me/c/1234567890/25965", body)

    def test_progress_without_link(self):
        state.ACTIVE_DOWNLOADS[1] = {
            "label": "普通", "filename": "a.mp4",
            "percent": 10.0, "downloaded": 100, "total": 1000,
            "link": None,
        }
        body = text.progress_text()
        self.assertIn("a.mp4", body)
        self.assertNotIn("t.me", body)


class ReserveFinalPathTest(unittest.TestCase):
    """_reserve_final_path：并发下载的最终路径占位（防同标题相册图片撞名）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tg_reserve_")
        download_mod._RESERVED_FINAL_PATHS.clear()

    def tearDown(self):
        download_mod._RESERVED_FINAL_PATHS.clear()

    def test_returns_plain_path_when_free(self):
        p = download_mod._reserve_final_path(self.dir, "a.jpg")
        self.assertEqual(p, os.path.join(self.dir, "a.jpg"))

    def test_bumps_on_existing_disk_file(self):
        open(os.path.join(self.dir, "a.jpg"), "w").close()
        p = download_mod._reserve_final_path(self.dir, "a.jpg")
        self.assertEqual(p, os.path.join(self.dir, "a (1).jpg"))

    def test_concurrent_reserves_are_distinct(self):
        # 两次并发占位（都还没落盘）不得选出同一条路径
        p1 = download_mod._reserve_final_path(self.dir, "同标题.jpg")
        p2 = download_mod._reserve_final_path(self.dir, "同标题.jpg")
        self.assertNotEqual(p1, p2)
        self.assertTrue(p1.endswith("同标题.jpg"))
        self.assertTrue(p2.endswith("同标题 (1).jpg"))

    def test_release_frees_reservation(self):
        p = download_mod._reserve_final_path(self.dir, "a.jpg")
        download_mod._RESERVED_FINAL_PATHS.discard(p)
        p2 = download_mod._reserve_final_path(self.dir, "a.jpg")
        self.assertEqual(p, p2)


if __name__ == "__main__":
    unittest.main()
