"""下载白名单功能的单元测试。

运行方式（在项目根目录）：
    .venv/bin/python -m unittest test_whitelist -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

# 必须在导入 tg_userbot_final 之前把保存目录指到临时目录：
# 该模块在 import 时会创建 SAVE_FOLDER 并打开日志文件。
_TMP = tempfile.mkdtemp(prefix="tg_userbot_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tg_userbot_final as tg  # noqa: E402


class ClassifyMessageChatTest(unittest.TestCase):
    """classify_message_chat：决定一条消息属于 me / 白名单 chat / 忽略。"""

    def test_my_id_returns_me(self):
        result = tg.classify_message_chat(chat_id=100, my_id=100, whitelist={})
        self.assertEqual(result, ("me", None))

    def test_whitelisted_chat_returns_chat_with_title(self):
        result = tg.classify_message_chat(
            chat_id=777, my_id=100, whitelist={777: "解析机器人"}
        )
        self.assertEqual(result, ("chat", "解析机器人"))

    def test_unknown_chat_is_ignored(self):
        result = tg.classify_message_chat(
            chat_id=999, my_id=100, whitelist={777: "解析机器人"}
        )
        self.assertEqual(result, (None, None))

    def test_my_id_wins_even_if_whitelisted(self):
        result = tg.classify_message_chat(
            chat_id=100, my_id=100, whitelist={100: "自己"}
        )
        self.assertEqual(result, ("me", None))


class WhitelistPersistenceTest(unittest.TestCase):
    """save_whitelist / load_whitelist：JSON 持久化往返。"""

    def setUp(self):
        self.path = os.path.join(_TMP, "test_whitelist.json")
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_roundtrip(self):
        chats = {123: "机器人A", -100456: "频道B"}
        tg.save_whitelist(chats, path=self.path)
        self.assertEqual(tg.load_whitelist(path=self.path), chats)

    def test_stored_format_is_id_title_pairs(self):
        tg.save_whitelist({123: "机器人A"}, path=self.path)
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data, {"chats": [[123, "机器人A"]]})

    def test_missing_file_returns_empty(self):
        self.assertEqual(tg.load_whitelist(path=self.path), {})

    def test_corrupt_file_returns_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("这不是 JSON")
        self.assertEqual(tg.load_whitelist(path=self.path), {})


class ParseWlCommandTest(unittest.TestCase):
    """parse_wl_command / is_wl_command：/wl 命令文本解析。"""

    def test_plain_wl_is_list(self):
        self.assertEqual(tg.parse_wl_command("/wl"), ("list", None))

    def test_wl_list_is_list(self):
        self.assertEqual(tg.parse_wl_command("/wl list"), ("list", None))

    def test_add_with_target(self):
        self.assertEqual(
            tg.parse_wl_command("/wl add @DouYintg_bot"),
            ("add", "@DouYintg_bot"),
        )

    def test_add_with_numeric_id(self):
        self.assertEqual(
            tg.parse_wl_command("/wl add -100123"), ("add", "-100123")
        )

    def test_add_without_target_returns_empty(self):
        self.assertEqual(tg.parse_wl_command("/wl add"), ("add", ""))

    def test_del_with_key(self):
        self.assertEqual(tg.parse_wl_command("/wl del 123"), ("del", "123"))

    def test_del_with_negative_id(self):
        self.assertEqual(
            tg.parse_wl_command("/wl del -100123"), ("del", "-100123")
        )

    def test_unknown_subcommand_is_invalid(self):
        self.assertEqual(tg.parse_wl_command("/wl foo"), ("invalid", None))

    def test_non_wl_command_returns_none(self):
        self.assertIsNone(tg.parse_wl_command("/status"))

    def test_is_wl_command_matches(self):
        for text in ("/wl", "/wl list", "/wl add @x", "/wl del -100123"):
            self.assertTrue(tg.is_wl_command(text), text)

    def test_is_wl_command_rejects_others(self):
        for text in ("/done", "/thread 5", "/wadd", "wl"):
            self.assertFalse(tg.is_wl_command(text), text)


class ResolveWlDelKeyTest(unittest.TestCase):
    """resolve_wl_del_key：/wl del 参数优先按 ID、其次按列表序号解析。"""

    def setUp(self):
        self.whitelist = {123: "A", -100456: "B"}

    def test_id_match_wins(self):
        self.assertEqual(tg.resolve_wl_del_key("123", self.whitelist), 123)

    def test_negative_id_match(self):
        self.assertEqual(
            tg.resolve_wl_del_key("-100456", self.whitelist), -100456
        )

    def test_index_falls_back_to_sorted_order(self):
        # 排序后 [-100456, 123]，序号 2 → 123
        self.assertEqual(tg.resolve_wl_del_key("2", self.whitelist), 123)

    def test_index_out_of_range_returns_none(self):
        self.assertIsNone(tg.resolve_wl_del_key("5", self.whitelist))

    def test_non_numeric_returns_none(self):
        self.assertIsNone(tg.resolve_wl_del_key("abc", self.whitelist))


class EntityDisplayNameTest(unittest.TestCase):
    """entity_display_name：实体 → 可读名称。"""

    def test_channel_uses_title(self):
        entity = mock.Mock(title="某频道", first_name=None, last_name=None, username=None)
        self.assertEqual(tg.entity_display_name(entity), "某频道")

    def test_user_uses_full_name(self):
        entity = mock.Mock(
            title=None, first_name="张", last_name="三", username="zhangsan"
        )
        self.assertEqual(tg.entity_display_name(entity), "张 三")

    def test_user_without_name_uses_username(self):
        entity = mock.Mock(
            title=None, first_name=None, last_name=None, username="zhangsan"
        )
        self.assertEqual(tg.entity_display_name(entity), "zhangsan")


class ResolveDownloadSourceTest(unittest.IsolatedAsyncioTestCase):
    """resolve_download_source：白名单 chat 用标题覆盖转发来源。"""

    async def test_override_wins_without_forward_lookup(self):
        fake_message = mock.Mock()
        with mock.patch.object(
            tg, "get_forward_source", side_effect=AssertionError("不该查询转发来源")
        ):
            source = await tg.resolve_download_source(fake_message, "解析机器人")
        self.assertEqual(source, "解析机器人")

    async def test_no_override_falls_back_to_forward_source(self):
        fake_message = mock.Mock()
        with mock.patch.object(
            tg, "get_forward_source", return_value="转发频道"
        ) as m:
            source = await tg.resolve_download_source(fake_message, None)
        m.assert_awaited_once_with(fake_message)
        self.assertEqual(source, "转发频道")


if __name__ == "__main__":
    unittest.main()
