"""`tg_userbot/config.py` 的单元测试：按当前真实接口断言配置契约。

历史说明（2026-09-10 重写）：本文件最初由 Chrome Agent V2 会话生成，断言的
是一套**从未落地**的 API——`config.load_secrets()`、读 `TERMUX`/`MINGW`
环境变量的 `is_termux()`、`PLATFORM_LINKS[...]["bot_username"]`、
`config.TASK_EVENT_TYPES`、`config.LIST_PAGE_SIZE`、
`config.FIND_INPUT_UNTIL`——那份设计稿的影子一直以 3 失败 7 错误挂在
suite 上（见提交 e3fb56b 保存的 V2 遗留）。现按 config.py 的真身重写：
不存在的概念直接删掉，不留假断言；真实契约补上针对性用例。

    .venv/bin/python -m unittest tests.test_config -v
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tg_userbot import config


class TestPlatformDetection(unittest.TestCase):
    """平台分支只有 Termux / 桌面两条，判据是 TERMUX_VERSION。"""

    def test_is_termux_reads_termux_version(self):
        with patch.dict(os.environ, {"TERMUX_VERSION": "0.118.0"},
                        clear=False):
            self.assertTrue(config.is_termux())

    def test_is_termux_false_without_marker(self):
        """没有 TERMUX_VERSION 且数据目录不存在 → 桌面分支。"""
        with patch.dict(os.environ, {}, clear=True), \
                patch("os.path.isdir", return_value=False):
            self.assertFalse(config.is_termux())

    def test_is_termux_detects_termux_data_dir(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("os.path.isdir", return_value=True):
            self.assertTrue(config.is_termux())

    def test_is_termux_ignores_bare_termux_var(self):
        """`TERMUX=1` 不是判据——这正是旧测试的错处（它断言 is_termux 为真）。"""
        with patch.dict(os.environ, {"TERMUX": "1"}, clear=True), \
                patch("os.path.isdir", return_value=False):
            self.assertFalse(config.is_termux())

    def test_import_time_flag_is_bool(self):
        self.assertIsInstance(config.IS_TERMUX, bool)
        self.assertEqual(config.IS_TERMUX, config.is_termux())

    def test_no_mingw_counterpart(self):
        """没有 is_mingw：Windows/MinGW 从来不是本项目的一个分支。"""
        self.assertFalse(hasattr(config, "is_mingw"))


class TestSecretLoading(unittest.TestCase):
    """`load_secret_config()` 是唯一的读取入口，任何异常都退化成 {}。"""

    def _load_with_file(self, content):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "secrets.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            with patch.object(config, "SECRETS_FILE", path):
                return config.load_secret_config()

    def test_roundtrip(self):
        secrets = {
            "api_id": 12345,
            "api_hash": "test_hash",
            "bot_token": "test:token",
            "bot_username": "test_bot",
            "tg_proxy": "socks5://127.0.0.1:7890",
            "douyin_cookie": "test_cookie",
        }
        self.assertEqual(self._load_with_file(json.dumps(secrets)), secrets)

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(config, "SECRETS_FILE",
                              os.path.join(tmp, "nope.json")):
                self.assertEqual(config.load_secret_config(), {})

    def test_corrupt_file_returns_empty(self):
        self.assertEqual(self._load_with_file("{ 这不是合法 JSON"), {})

    def test_non_dict_json_returns_empty(self):
        """JSON 合法但不是对象时同样退化成 {}，绝不能炸启动。"""
        self.assertEqual(self._load_with_file("[1, 2, 3]"), {})

    def test_partial_file_keeps_only_present_keys(self):
        self.assertEqual(
            self._load_with_file('{"api_id": 12345, "bot_token": "t:token"}'),
            {"api_id": 12345, "bot_token": "t:token"})

    def test_secrets_file_resolution(self):
        """REPO_ROOT = 包目录的父目录。

        单文件时代默认路径是「脚本所在目录」= 仓库根；代码搬进 tg_userbot/
        后若还用 __file__ 直推，查找点会悄然挪进包内、读不到 tg_secrets.json。
        """
        self.assertEqual(config.PACKAGE_DIR,
                         os.path.dirname(os.path.abspath(config.__file__)))
        self.assertEqual(config.REPO_ROOT,
                         os.path.dirname(config.PACKAGE_DIR))
        self.assertEqual(
            config.SECRETS_FILE,
            os.environ.get("TG_SECRETS_FILE")
            or os.path.join(config.REPO_ROOT, "tg_secrets.json"))


class TestPlatformLinks(unittest.TestCase):
    def test_shape(self):
        """键是 'bot' / 'label'（旧测试断言的 'bot_username'/'log_label' 不存在）。"""
        for kind in ("douyin", "instagram"):
            with self.subTest(kind=kind):
                link = config.PLATFORM_LINKS[kind]
                self.assertIn("bot", link)
                self.assertIn("label", link)
                self.assertTrue(link["bot"].startswith("@"))
                self.assertTrue(link["label"])
                self.assertNotIn("bot_username", link)


class TestEventAndWindowConstants(unittest.TestCase):
    def test_task_events_max_events(self):
        self.assertIsInstance(config.TASK_EVENTS_MAX_EVENTS, int)
        self.assertGreater(config.TASK_EVENTS_MAX_EVENTS, 0)

    def test_find_input_window(self):
        """事件类型表（TASK_EVENT_TYPES）不在 config —— 事件类型字符串直接
        写在 stats.py 里，所以这里只断言窗口常量。"""
        self.assertIsInstance(config.FIND_INPUT_WINDOW_SECONDS, int)
        self.assertGreater(config.FIND_INPUT_WINDOW_SECONDS, 0)
        self.assertFalse(hasattr(config, "TASK_EVENT_TYPES"))

    def test_list_page_size_and_input_until_live_elsewhere(self):
        """LIST_PAGE_SIZE 在 queue.py、FIND_INPUT_UNTIL 在 state.py——
        config 只放不可变常量，运行时可变的窗口戳不进 config。"""
        self.assertFalse(hasattr(config, "LIST_PAGE_SIZE"))
        self.assertFalse(hasattr(config, "FIND_INPUT_UNTIL"))


class TestChromeConstants(unittest.TestCase):
    """Chrome Agent（V1，已落地）的常量。"""

    def test_agent_constants(self):
        self.assertIsInstance(config.CHROME_AGENT_PID_FILE, str)
        self.assertTrue(config.CHROME_AGENT_PID_FILE.endswith(".pid"))

        for name in ("CHROME_CDP_CONNECT_TIMEOUT", "CHROME_DOWNLOAD_RETRIES",
                     "CHROME_DOWNLOAD_TIMEOUT"):
            with self.subTest(name=name):
                self.assertIsInstance(getattr(config, name), int)
                self.assertGreater(getattr(config, name), 0)

        self.assertIsInstance(config.CHROME_POLL_SECONDS, float)
        self.assertGreater(config.CHROME_POLL_SECONDS, 0)

    def test_download_dir_under_save_folder(self):
        self.assertEqual(
            config.CHROME_DOWNLOAD_DIR,
            os.path.join(config.SAVE_FOLDER, "TG Chrome Download"))

    def test_persistence_files_under_runtime_dir(self):
        """两进程靠 RUNTIME_DIR 下的 JSON 通信（规格 24），搬错地方会互相看不见。"""
        for name in ("CHROME_TASKS_FILE", "CHROME_REQUESTS_FILE",
                     "CHROME_AGENT_PID_FILE"):
            with self.subTest(name=name):
                path = getattr(config, name)
                self.assertTrue(
                    path.startswith(config.RUNTIME_DIR + os.sep), path)

    def test_chrome_v2_legacy_constants(self):
        """Chrome Agent V2 预留常量：随提交 e3fb56b 一起保存、目前无人引用
        （V2 未落地）。断言它们仍在，是为了让「这批常量还在不在」有据可查，
        而不是暗示 V2 已实现。"""
        for name in ("CHROME_HEALTH_CHECK_INTERVAL", "CHROME_RECOVERY_TIMEOUT",
                     "CHROME_GUID_VALIDITY_SECONDS", "CHROME_MAX_GUID_AGE",
                     "CHROME_MIN_PROGRESS_INTERVAL"):
            with self.subTest(name=name):
                self.assertIsInstance(getattr(config, name), (int, float))
                self.assertGreater(getattr(config, name), 0)
        self.assertIsInstance(config.CHROME_BACKUP_COUNT, int)
        self.assertGreater(config.CHROME_BACKUP_COUNT, 0)


if __name__ == '__main__':
    unittest.main()
