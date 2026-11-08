"""转发扩展名过滤（2026-10-27 定档，@talu3D 专用 .jpg）的单元测试。"""
import atexit
import os
import shutil
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_extf_", dir=tempfile.gettempdir())
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config, listener, state  # noqa: E402


class _M:
    """假媒体消息：可指定原始文件名 / 是否 photo。"""

    def __init__(self, mid, name=None, photo=False):
        self.id = mid
        if name is not None:
            f = mock.MagicMock()
            f.name = name
            self.file = f
        else:
            self.file = None
        self.photo = True if photo else None


class MediaExtTest(unittest.TestCase):

    def test_member_ext_document_photo_and_none(self):
        self.assertEqual(listener.media_member_ext(_M(1, "a.JPG")), ".jpg")
        self.assertEqual(listener.media_member_ext(_M(2, photo=True)), ".jpg")
        self.assertEqual(listener.media_member_ext(_M(3, "noext")), "")
        self.assertEqual(listener.media_member_ext(_M(4)), "")

    def test_filter_drops_and_keeps(self):
        media = [_M(1, "cover.jpg"), _M(2, "v.mp4"), _M(3, photo=True),
                 _M(4, "w.png")]
        kept, dropped = listener.filter_media_by_ext(media, [".jpg"])
        self.assertEqual([m.id for m in kept], [2, 4])
        self.assertEqual(dropped, 2)

    def test_filter_empty_list_noop(self):
        media = [_M(1, "a.jpg")]
        kept, dropped = listener.filter_media_by_ext(media, [])
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 0)


class NormalizeRuleTest(unittest.TestCase):

    def test_exclude_ext_normalized(self):
        r = listener.normalize_rule({
            "source_chat_id": -100, "tag": "#a", "download": True,
            "targets": [{"type": "saved_messages"}],
            "exclude_ext": ["JPG", ".png", " ", ".png"]})
        self.assertEqual(r["exclude_ext"], [".jpg", ".png"])

    def test_absent_keeps_rule_clean(self):
        r = listener.normalize_rule({
            "source_chat_id": -100, "tag": "#a", "download": True,
            "targets": [{"type": "saved_messages"}]})
        self.assertNotIn("exclude_ext", r)


class SetExtCommandTest(unittest.TestCase):

    def setUp(self):
        state.LISTEN_RULES = [
            {"source_chat_id": -100, "tag": "#a", "download": True,
             "targets": [{"type": "saved_messages"}]}]
        self.addCleanup(setattr, state, "LISTEN_RULES", [])

    def test_set_and_clear(self):
        with mock.patch.object(listener, "save_listen_config",
                               return_value=True) as sv:
            ok, msg = listener.set_rule_exclude_ext("1 .jpg,.PNG")
        self.assertTrue(ok)
        self.assertEqual(state.LISTEN_RULES[0]["exclude_ext"],
                         [".jpg", ".png"])
        sv.assert_called_once()
        with mock.patch.object(listener, "save_listen_config",
                               return_value=True):
            ok2, msg2 = listener.set_rule_exclude_ext("1 clear")
        self.assertTrue(ok2)
        self.assertNotIn("exclude_ext", state.LISTEN_RULES[0])

    def test_bad_index_and_usage(self):
        self.assertFalse(listener.set_rule_exclude_ext("9 .jpg")[0])
        self.assertFalse(listener.set_rule_exclude_ext("1")[0])
        self.assertFalse(listener.set_rule_exclude_ext("1 ,," )[0])

    def test_parse_listen_ext(self):
        self.assertEqual(listener.parse_listen_command("/listen ext 1 .jpg"),
                         ("ext", "1 .jpg"))
