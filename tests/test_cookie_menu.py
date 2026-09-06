"""bot 菜单【🍪 抖音Cookie】的保存/掩码/视图/动作（全部离线）。

覆盖：原子保存（保留其它字段、实时更新内存值）、掩码、状态视图、
三个菜单动作、等待窗口内的输入处理（原文立即删除）。
"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_cookie_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

import telethon  # noqa: E402

from tg_userbot import bot, config, menu, state  # noqa: E402


def _write_secrets(path, extra=None):
    data = {"api_id": 123, "api_hash": "h", "douyin_cookie": "old=1"}
    if extra:
        data.update(extra)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


class MaskDouyinCookieTest(unittest.TestCase):
    def test_long_cookie_masked(self):
        c = "a" * 12 + "MIDDLE" + "z" * 4
        self.assertEqual(config.mask_douyin_cookie(c), "aaaaaaaaaaaa…zzzz")

    def test_short_cookie(self):
        self.assertEqual(config.mask_douyin_cookie("abc"), "abc…")

    def test_empty(self):
        self.assertEqual(config.mask_douyin_cookie(""), "")


class SaveDouyinCookieTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(_TMP, f"secrets_{id(self)}.json")
        self._orig = config.DOUYIN_COOKIE

    def tearDown(self):
        config.DOUYIN_COOKIE = self._orig
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_save_preserves_other_fields_and_updates_memory(self):
        _write_secrets(self.path)
        err = config.save_douyin_cookie("ttwid=NEW; sessionid=XYZ", path=self.path)
        self.assertIsNone(err)
        self.assertEqual(config.DOUYIN_COOKIE, "ttwid=NEW; sessionid=XYZ")
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["api_id"], 123)          # 其它字段保留
        self.assertEqual(data["douyin_cookie"], "ttwid=NEW; sessionid=XYZ")
        self.assertFalse(os.path.exists(self.path + ".tmp"))  # 原子替换无残留

    def test_clear_writes_empty(self):
        _write_secrets(self.path)
        self.assertIsNone(config.save_douyin_cookie("", path=self.path))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["douyin_cookie"], "")

    def test_corrupt_file_rejected(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("不是JSON{{{")
        err = config.save_douyin_cookie("new=1", path=self.path)
        self.assertIn("拒绝覆盖", err)


class CookieMenuViewTest(unittest.TestCase):
    def setUp(self):
        self._orig = config.DOUYIN_COOKIE

    def tearDown(self):
        config.DOUYIN_COOKIE = self._orig

    def test_main_menu_contains_cookie_button(self):
        datas = [
            b.data for row in menu.main_menu_buttons() for b in row
        ]
        self.assertIn(b"m:cookie", datas)

    def test_status_text_configured(self):
        config.DOUYIN_COOKIE = "ttwid=1; sessionid=XYZ" + "x" * 30
        text = menu.cookie_status_text()
        self.assertIn("已配置", text)
        self.assertIn("sessionid ✅", text)

    def test_status_text_unconfigured(self):
        config.DOUYIN_COOKIE = ""
        self.assertIn("未配置", menu.cookie_status_text())


class CookieMenuActionsTest(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.path = os.path.join(_TMP, f"secrets_act_{id(self)}.json")
        self._orig_cookie = config.DOUYIN_COOKIE
        self._orig_until = state.COOKIE_INPUT_UNTIL

    def tearDown(self):
        config.DOUYIN_COOKIE = self._orig_cookie
        state.COOKIE_INPUT_UNTIL = self._orig_until
        asyncio.set_event_loop(None)
        self.loop.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def test_cookie_action_returns_status(self):
        text, buttons = self._run(bot.handle_menu_action("cookie", None, None))
        self.assertIn("抖音 Cookie", text)
        self.assertTrue(buttons)

    def test_cookie_set_opens_input_window(self):
        before = time.monotonic()
        text, _ = self._run(bot.handle_menu_action("cookie_set", None, None))
        self.assertIn("发送 cookie 内容", text)
        self.assertGreaterEqual(state.COOKIE_INPUT_UNTIL, before)

    def test_cookie_clear_persists(self):
        _write_secrets(self.path)
        config.DOUYIN_COOKIE = "old"
        with mock.patch.object(config, "SECRETS_FILE", self.path):
            text, _ = self._run(bot.handle_menu_action("cookie_clear", None, None))
        self.assertIn("已清除", text)
        self.assertEqual(config.DOUYIN_COOKIE, "")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["douyin_cookie"], "")


class CookieInputHandlingTest(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.path = os.path.join(_TMP, f"secrets_in_{id(self)}.json")
        _write_secrets(self.path)
        self._orig_cookie = config.DOUYIN_COOKIE

    def tearDown(self):
        config.DOUYIN_COOKIE = self._orig_cookie
        asyncio.set_event_loop(None)
        self.loop.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def test_input_saved_and_original_deleted(self):
        event = mock.Mock()
        event.delete = mock.AsyncMock()
        with mock.patch.object(config, "SECRETS_FILE", self.path), \
             mock.patch.object(bot.state, "bot_client", new=mock.Mock()) as bc:
            bc.send_message = mock.AsyncMock()
            self._run(bot._handle_cookie_input(event, "ttwid=NEW; sessionid=AB"))
        event.delete.assert_awaited_once()          # 原文立即删除
        bc.send_message.assert_awaited_once()
        self.assertIn("实时生效", bc.send_message.await_args.args[1])
        self.assertEqual(config.DOUYIN_COOKIE, "ttwid=NEW; sessionid=AB")

    def test_empty_input_cancelled(self):
        event = mock.Mock()
        event.delete = mock.AsyncMock()
        with mock.patch.object(bot.state, "bot_client", new=mock.Mock()) as bc:
            bc.send_message = mock.AsyncMock()
            self._run(bot._handle_cookie_input(event, ""))
        self.assertIn("内容为空", bc.send_message.await_args.args[1])
