"""bot 按钮菜单功能的单元测试。

运行方式（在项目根目录）：
    .venv/bin/python -m unittest test_menu -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

# 必须在导入 tg_userbot_final 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_menu_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tg_userbot_final as tg  # noqa: E402


class MenuDataCodecTest(unittest.TestCase):
    """encode_menu_data / parse_menu_data：回调数据编解码。"""

    def test_encode_basic(self):
        self.assertEqual(tg.encode_menu_data("home"), b"m:home")
        self.assertEqual(tg.encode_menu_data("thread", "5"), b"m:thread:5")
        self.assertEqual(
            tg.encode_menu_data("wl_del", "-100123"), b"m:wl_del:-100123"
        )

    def test_parse_roundtrip(self):
        for action, arg in (
            ("home", None),
            ("progress", None),
            ("thread", "5"),
            ("wl_del", "-100123"),
            ("wl_add", "-1001234567890"),
            ("back", "home"),
            ("cd2", None),
            ("cd2_stop", None),
        ):
            data = tg.encode_menu_data(action, arg)
            self.assertEqual(
                tg.parse_menu_data(data), (action, arg), data
            )

    def test_payload_within_64_bytes(self):
        # 最长的实际载荷：wl_add/wl_del 带 15 位频道 id
        data = tg.encode_menu_data("wl_add", "-1001234567890")
        self.assertLessEqual(len(data), 64)

    def test_parse_unknown_returns_unknown(self):
        self.assertEqual(tg.parse_menu_data(b"garbage"), ("unknown", None))
        self.assertEqual(tg.parse_menu_data(b"m:a:b:c"), ("unknown", None))
        self.assertEqual(tg.parse_menu_data(b""), ("unknown", None))


class MenuTextTest(unittest.TestCase):
    """菜单/信息文本构建。"""

    def test_main_menu_buttons_contain_all_entries(self):
        texts = [b.text for row in tg.main_menu_buttons() for b in row]
        for label in ("📊 状态", "📈 进度", "📜 下载记录",
                      "📋 白名单", "🧵 并发", "🧹 清理",
                      "🖥 启动CD2", "🛑 停止CD2"):
            self.assertIn(label, texts)

    def test_main_menu_text_non_empty(self):
        text = tg.build_main_menu_text()
        self.assertTrue(text.strip())
        self.assertIn("菜单", text)

    def test_status_text_contains_folder(self):
        text = tg.status_text()
        self.assertIn("TG Userbot", text)
        self.assertIn(tg.SAVE_FOLDER, text)

    def test_wl_list_text_empty(self):
        self.assertIn("空", tg.wl_list_text({}))

    def test_wl_list_text_entries(self):
        text = tg.wl_list_text({-100123: "频道A", 456: "机器人B"})
        self.assertIn("频道A", text)
        self.assertIn("-100123", text)
        self.assertIn("机器人B", text)
        self.assertIn("456", text)

    def test_progress_text_empty(self):
        tg.ACTIVE_DOWNLOADS.clear()
        self.assertIn("没有进行中", tg.progress_text())

    def test_progress_text_with_entries(self):
        tg.ACTIVE_DOWNLOADS.clear()
        tg.ACTIVE_DOWNLOADS[1] = {
            "label": "普通", "filename": "a.mp4",
            "percent": 42.0, "downloaded": 1024, "total": 2048,
        }
        text = tg.progress_text()
        self.assertIn("a.mp4", text)
        self.assertIn("42.0%", text)
        tg.ACTIVE_DOWNLOADS.clear()


class ThreadLimitTest(unittest.TestCase):
    """apply_thread_limit：并发数设置。"""

    def tearDown(self):
        tg.DOWNLOAD_CONCURRENCY = 3
        tg.DOWNLOAD_SEMAPHORE = None

    def test_valid_value(self):
        ok, msg = tg.apply_thread_limit("5")
        self.assertTrue(ok)
        self.assertEqual(tg.DOWNLOAD_CONCURRENCY, 5)

    def test_invalid_number(self):
        ok, msg = tg.apply_thread_limit("99")
        self.assertFalse(ok)
        self.assertEqual(tg.DOWNLOAD_CONCURRENCY, 3)

    def test_non_numeric(self):
        ok, msg = tg.apply_thread_limit("abc")
        self.assertFalse(ok)
        self.assertEqual(tg.DOWNLOAD_CONCURRENCY, 3)


class CleanTempFilesTest(unittest.TestCase):
    """clean_temp_files：清理 .download 临时文件。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tg_clean_test_")
        for name in ("a.mp4.download", "b.jpg.download", "keep.mp4"):
            with open(os.path.join(self.dir, name), "w") as f:
                f.write("x")

    def test_removes_download_files_only(self):
        count = tg.clean_temp_files(self.dir)
        self.assertEqual(count, 2)
        remaining = sorted(os.listdir(self.dir))
        self.assertEqual(remaining, ["keep.mp4"])


class CD2ConfigTest(unittest.TestCase):
    """cd2_config：读取 tg_secrets.json 的 cd2 段（command/port）。"""

    def _patch_secrets(self, value):
        patcher = mock.patch.object(tg, "_SECRET_CONFIG", value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_cd2_defaults_to_empty_command(self):
        self._patch_secrets({})
        command, port = tg.cd2_config()
        self.assertEqual(command, "")
        self.assertEqual(port, 19798)

    def test_reads_command_and_port(self):
        self._patch_secrets({"cd2": {
            "command": "~/software/cd2/clouddrive", "port": 19798,
        }})
        command, port = tg.cd2_config()
        self.assertEqual(command, "~/software/cd2/clouddrive")
        self.assertEqual(port, 19798)

    def test_non_numeric_port_falls_back(self):
        self._patch_secrets({"cd2": {"command": "cd2", "port": "abc"}})
        command, port = tg.cd2_config()
        self.assertEqual(command, "cd2")
        self.assertEqual(port, 19798)


class CD2PidParseTest(unittest.TestCase):
    """_cd2_pids_from_ps_output：从 ps 输出筛出 CD2 进程 PID（纯函数）。"""

    PS = (
        " 56136 /Users/u/software/cd/clouddrive\n"
        " 56137 /Users/u/software/cd/clouddrive Start-Service 56136\n"
        "   772 /System/Library/.../CloudDocs.iCloudDriveFileProvider\n"
        "   670 /System/Library/.../iCloudDriveCore/.../bird\n"
    )

    def test_matches_main_and_service_children(self):
        pids = tg._cd2_pids_from_ps_output(
            self.PS, "/Users/u/software/cd/clouddrive"
        )
        self.assertEqual(pids, [56136, 56137])

    def test_no_match_when_not_running(self):
        pids = tg._cd2_pids_from_ps_output(
            "  1 /sbin/launchd\n", "/Users/u/software/cd/clouddrive"
        )
        self.assertEqual(pids, [])

    def test_empty_output(self):
        self.assertEqual(tg._cd2_pids_from_ps_output("", "/x/clouddrive"), [])


class WhitelistCommitTest(unittest.TestCase):
    """add_to_whitelist / del_from_whitelist：白名单提交。"""

    def setUp(self):
        tg.WHITELIST_CHATS.clear()
        self.saved = []
        patcher = mock.patch.object(
            tg, "save_whitelist", side_effect=self._fake_save
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_save(self, chats, path=None):
        self.saved.append(dict(chats))

    def test_add_new_chat(self):
        ok, msg = tg.add_to_whitelist(777, "机器人")
        self.assertTrue(ok)
        self.assertEqual(tg.WHITELIST_CHATS, {777: "机器人"})
        self.assertEqual(self.saved[-1], {777: "机器人"})

    def test_add_duplicate_fails(self):
        tg.add_to_whitelist(777, "机器人")
        ok, msg = tg.add_to_whitelist(777, "机器人")
        self.assertFalse(ok)

    def test_del_by_id(self):
        tg.add_to_whitelist(777, "机器人")
        ok, msg = tg.del_from_whitelist("777")
        self.assertTrue(ok)
        self.assertEqual(tg.WHITELIST_CHATS, {})

    def test_del_missing_fails(self):
        ok, msg = tg.del_from_whitelist("123")
        self.assertFalse(ok)


class BotCleanupPlanTest(unittest.TestCase):
    """plan_bot_chat_cleanup：bot 对话清理决策（纯函数）。"""

    def _msg(self, mid, age_minutes, has_buttons):
        return {
            "id": mid,
            "age_minutes": age_minutes,
            "has_buttons": has_buttons,
        }

    def test_keeps_newest_menu_always(self):
        messages = [
            self._msg(1, age_minutes=10, has_buttons=False),
            self._msg(2, age_minutes=9, has_buttons=True),   # 最新菜单
            self._msg(3, age_minutes=5, has_buttons=False),
        ]
        to_delete, to_keep = tg.plan_bot_chat_cleanup(messages, age_limit=1)
        self.assertEqual(to_keep, {2})
        self.assertEqual(set(to_delete), {1, 3})

    def test_deletes_only_old_messages(self):
        messages = [
            self._msg(1, age_minutes=0.5, has_buttons=False),
            self._msg(2, age_minutes=2, has_buttons=False),
        ]
        to_delete, to_keep = tg.plan_bot_chat_cleanup(messages, age_limit=1)
        self.assertEqual(to_delete, [2])
        self.assertEqual(to_keep, set())

    def test_no_messages(self):
        to_delete, to_keep = tg.plan_bot_chat_cleanup([], age_limit=1)
        self.assertEqual(to_delete, [])
        self.assertEqual(to_keep, set())


if __name__ == "__main__":
    unittest.main()
