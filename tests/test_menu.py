"""bot 按钮菜单功能的单元测试。

运行方式（在项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import os
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_menu_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import state, config  # noqa: E402
from tg_userbot import menu, text, thread, cleanup, cd2, whitelist, bot  # noqa: E402


class MenuDataCodecTest(unittest.TestCase):
    """encode_menu_data / parse_menu_data：回调数据编解码。"""

    def test_encode_basic(self):
        self.assertEqual(menu.encode_menu_data("home"), b"m:home")
        self.assertEqual(menu.encode_menu_data("thread", "5"), b"m:thread:5")
        self.assertEqual(
            menu.encode_menu_data("wl_del", "-100123"), b"m:wl_del:-100123"
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
            ("bak", None),
        ):
            data = menu.encode_menu_data(action, arg)
            self.assertEqual(
                menu.parse_menu_data(data), (action, arg), data
            )

    def test_payload_within_64_bytes(self):
        # 最长的实际载荷：wl_add/wl_del 带 15 位频道 id
        data = menu.encode_menu_data("wl_add", "-1001234567890")
        self.assertLessEqual(len(data), 64)

    def test_parse_unknown_returns_unknown(self):
        self.assertEqual(menu.parse_menu_data(b"garbage"), ("unknown", None))
        self.assertEqual(menu.parse_menu_data(b"m:a:b:c"), ("unknown", None))
        self.assertEqual(menu.parse_menu_data(b""), ("unknown", None))


class MenuTextTest(unittest.TestCase):
    """菜单/信息文本构建。"""

    def test_main_menu_buttons_contain_all_entries(self):
        texts = [b.text for row in menu.main_menu_buttons() for b in row]
        for label in ("📊 状态", "📈 进度", "📜 下载记录",
                      "📋 白名单", "🧵 并发", "🧹 清理",
                      "🖥 启动CD2", "🛑 停止CD2", "🗂 备份记录"):
            self.assertIn(label, texts)

    def test_main_menu_has_dedup_entry(self):
        texts = [b.text for row in menu.main_menu_buttons() for b in row]
        self.assertIn("🛡 去重", texts)

    def test_dedup_menu_buttons(self):
        rows = menu.dedup_menu_buttons()
        pairs = [menu.parse_menu_data(b.data) for row in rows for b in row]
        actions = [a for a, _arg in pairs]
        self.assertIn("dedup_toggle", actions)
        self.assertIn("home", actions)

    def test_dedup_menu_actions_registered(self):
        from tg_userbot import config
        for a in ("dedup", "dedup_toggle"):
            self.assertIn(a, config.MENU_ACTIONS)

    def test_retry_menu_buttons_paginated(self):
        from tg_userbot import state as _state
        old_q = _state.QUEUE
        _state.QUEUE = {"tasks": [], "retry": []}
        try:
            for i in range(12):
                _state.QUEUE["retry"].append({
                    "id": f"r{i}", "label": f"任务{i}.mp4", "attempts": 1,
                })
            rows = menu.retry_menu_buttons(1)
            flat = [(b.text, menu.parse_menu_data(b.data))
                    for row in rows for b in row]
            texts = [t for t, _ in flat]
            actions = [a for _, (a, _arg) in flat]
            run_items = [t for t, (a, _arg) in flat if a == "retry_run"]
            self.assertEqual(len(run_items), 10)  # 只渲染本页条目
            self.assertIn("▶️ 下一页", texts)
            self.assertNotIn("◀️ 上一页", texts)  # 第 1 页没有上一页
            self.assertIn(("▶️ 下一页", ("retry", "2")),
                          [(t, p) for t, p in flat])
            # 第 2 页：有上一页、无下一页，序号从 11 起
            rows2 = menu.retry_menu_buttons(2)
            texts2 = [b.text for row in rows2 for b in row]
            self.assertIn("◀️ 上一页", texts2)
            self.assertNotIn("▶️ 下一页", texts2)
            self.assertIn("▶️ 11", texts2)
            # 全部重放入口
            self.assertIn("♻️ 全部重放", texts)
            self.assertIn("retry_all", actions)
        finally:
            _state.QUEUE = old_q

    def test_main_menu_text_non_empty(self):
        text = menu.build_main_menu_text()
        self.assertTrue(text.strip())
        self.assertIn("菜单", text)

    def test_status_text_contains_folder(self):
        body = text.status_text()
        self.assertIn("TG Userbot", body)
        self.assertIn(config.SAVE_FOLDER, body)

    def test_wl_list_text_empty(self):
        self.assertIn("空", text.wl_list_text({}))

    def test_wl_list_text_entries(self):
        body = text.wl_list_text({-100123: "频道A", 456: "机器人B"})
        self.assertIn("频道A", body)
        self.assertIn("-100123", body)
        self.assertIn("机器人B", body)
        self.assertIn("456", body)

    def test_progress_text_empty(self):
        state.ACTIVE_DOWNLOADS.clear()
        self.assertIn("没有进行中", text.progress_text())

    def test_progress_text_with_entries(self):
        state.ACTIVE_DOWNLOADS.clear()
        state.ACTIVE_DOWNLOADS[1] = {
            "label": "普通", "filename": "a.mp4",
            "percent": 42.0, "downloaded": 1024, "total": 2048,
        }
        body = text.progress_text()
        self.assertIn("a.mp4", body)
        self.assertIn("42.0%", body)
        state.ACTIVE_DOWNLOADS.clear()


class LiveMenuHeaderTest(unittest.TestCase):
    """主菜单头部实时状态：打开菜单即见 在途/待处理/待重试/今日成功。"""

    def setUp(self):
        self.old = (state.ACTIVE_DOWNLOADS, state.QUEUE)
        state.ACTIVE_DOWNLOADS = {}
        state.QUEUE = {"tasks": [], "retry": []}

    def tearDown(self):
        state.ACTIVE_DOWNLOADS, state.QUEUE = self.old

    def test_idle_menu(self):
        """全空（且今日无成功）时显示空闲。"""
        self.assertIn("空闲", menu.build_main_menu_text())

    def test_busy_menu_shows_counts(self):
        state.ACTIVE_DOWNLOADS = {1: {}, 2: {}}
        state.QUEUE = {
            "tasks": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "retry": [{"id": "d"}],
        }
        t = menu.build_main_menu_text()
        self.assertIn("在途 2", t)
        self.assertIn("待处理 3", t)
        self.assertIn("待重试 1", t)
        # 测试环境的临时目录无日志/历史 → 今日成功为 0，仍应展示（可预期的格式）
        self.assertIn("今日 0 个", t)
        self.assertNotIn("空闲", t)


class RefreshButtonsTest(unittest.TestCase):
    """队列/待重试视图的「🔄 刷新」按钮：原地重渲染，免走主菜单。"""

    def test_queue_menu_has_refresh(self):
        state.QUEUE = {"tasks": [], "retry": []}
        rows = menu.queue_menu_buttons()
        actions = [
            menu.parse_menu_data(b.data)
            for row in rows for b in row
        ]
        self.assertIn(("queue", None), actions)

    def test_retry_menu_has_refresh(self):
        state.QUEUE = {"tasks": [], "retry": []}
        rows = menu.retry_menu_buttons()
        actions = [
            menu.parse_menu_data(b.data)
            for row in rows for b in row
        ]
        # 刷新按钮带当前页参（🔄 1/1 → m:retry:1）
        self.assertIn(("retry", "1"), actions)

    def test_main_menu_has_ledger_entry(self):
        texts = [b.text for row in menu.main_menu_buttons() for b in row]
        self.assertIn("📊 台账", texts)


class BotCommandsRegistryTest(unittest.IsolatedAsyncioTestCase):
    """bot 命令面板：BOT_COMMANDS 表合法，注册时发 SetBotCommandsRequest。"""

    def test_bot_commands_well_formed(self):
        for name, desc in bot.BOT_COMMANDS:
            self.assertRegex(name, r"^[a-z0-9_]{1,32}$")
            self.assertTrue(desc.strip())
        names = [n for n, _ in bot.BOT_COMMANDS]
        self.assertIn("stats", names)
        self.assertEqual(len(names), len(set(names)), "命令不应重复")

    async def test_register_sends_request(self):
        calls = []

        class FakeClient:
            async def __call__(self, request):
                calls.append(request)

        await bot.register_bot_commands(FakeClient())
        self.assertEqual(len(calls), 1)
        names = [c.command for c in calls[0].commands]
        self.assertIn("stats", names)
        self.assertEqual(calls[0].lang_code, "")


class ThreadLimitTest(unittest.TestCase):
    """apply_thread_limit：并发数设置。"""

    def tearDown(self):
        state.DOWNLOAD_CONCURRENCY = config.DOWNLOAD_CONCURRENCY
        state.DOWNLOAD_SEMAPHORE = None

    def test_valid_value(self):
        ok, msg = thread.apply_thread_limit("5")
        self.assertTrue(ok)
        self.assertEqual(state.DOWNLOAD_CONCURRENCY, 5)

    def test_invalid_number(self):
        ok, msg = thread.apply_thread_limit("99")
        self.assertFalse(ok)
        self.assertEqual(state.DOWNLOAD_CONCURRENCY, config.DOWNLOAD_CONCURRENCY)

    def test_non_numeric(self):
        ok, msg = thread.apply_thread_limit("abc")
        self.assertFalse(ok)
        self.assertEqual(state.DOWNLOAD_CONCURRENCY, config.DOWNLOAD_CONCURRENCY)


class CleanTempFilesTest(unittest.TestCase):
    """clean_temp_files：清理 .download 临时文件。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tg_clean_test_")
        for name in ("a.mp4.download", "b.jpg.download", "keep.mp4"):
            with open(os.path.join(self.dir, name), "w") as f:
                f.write("x")

    def test_removes_download_files_only(self):
        count = cleanup.clean_temp_files(self.dir)
        self.assertEqual(count, 2)
        remaining = sorted(os.listdir(self.dir))
        self.assertEqual(remaining, ["keep.mp4"])


class CD2ConfigTest(unittest.TestCase):
    """cd2_config：读取 tg_secrets.json 的 cd2 段（command/port）。"""

    def _patch_secrets(self, value):
        patcher = mock.patch.object(config, "_SECRET_CONFIG", value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_cd2_defaults_to_empty_command(self):
        self._patch_secrets({})
        command, port = cd2.cd2_config()
        self.assertEqual(command, "")
        self.assertEqual(port, 19798)

    def test_reads_command_and_port(self):
        self._patch_secrets({"cd2": {
            "command": "~/software/cd2/clouddrive", "port": 19798,
        }})
        command, port = cd2.cd2_config()
        self.assertEqual(command, "~/software/cd2/clouddrive")
        self.assertEqual(port, 19798)

    def test_non_numeric_port_falls_back(self):
        self._patch_secrets({"cd2": {"command": "cd2", "port": "abc"}})
        command, port = cd2.cd2_config()
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
        pids = cd2._cd2_pids_from_ps_output(
            self.PS, "/Users/u/software/cd/clouddrive"
        )
        self.assertEqual(pids, [56136, 56137])

    def test_no_match_when_not_running(self):
        pids = cd2._cd2_pids_from_ps_output(
            "  1 /sbin/launchd\n", "/Users/u/software/cd/clouddrive"
        )
        self.assertEqual(pids, [])

    def test_empty_output(self):
        self.assertEqual(cd2._cd2_pids_from_ps_output("", "/x/clouddrive"), [])


class BackupLogParseTest(unittest.TestCase):
    """parse_backup_log_lines：从 CD2 backup.<日期>.log 解析备份清理记录。"""

    LINE = (
        "2026-09-05 12:54:47.481  INFO cloudapi::backup_manager: "
        'handle_localfs_notify: delete file and remove from all dests "{}"'
    )
    A = "/u/Downloads/Nagram/Douyin/她头上为什么顶着两个妙脆角.mp4"
    B = "/u/Downloads/Nagram/抖音TikTok去水印bot/标题_ #绝区零.mp4"

    def _line(self, path, ts="2026-09-05 12:54:47.481", kind="localfs"):
        return (
            f"{ts}  INFO cloudapi::backup_manager: handle_{kind}_notify: "
            f'delete file and remove from all dests "{path}"'
        )

    def test_dedupes_pair_and_drops_non_media(self):
        lines = [
            self._line(self.A),                      # localfs
            self._line(self.A, kind="cloudfs"),      # 同文件另一通知
            self._line(self.B),
            self._line("/u/Downloads/Nagram/孤儿.mp4.download"),
            self._line("/u/Downloads/Nagram/download.log"),
            "2026-09-05 13:00:00.000 INFO cloudapi::backup_manager: "
            'now handle notify callback: "/s", [Delete("/r/a.mp4")]',
        ]
        items = cd2.parse_backup_log_lines(lines)
        paths = [p for _, p in items]
        self.assertEqual(sorted(paths), sorted([self.A, self.B]))
        self.assertEqual(len(items), 2)

    def test_sorted_newest_first(self):
        lines = [
            self._line(self.B, ts="2026-09-05 10:00:00.000"),
            self._line(self.A, ts="2026-09-05 12:00:00.000"),
        ]
        items = cd2.parse_backup_log_lines(lines)
        self.assertEqual([p for _, p in items], [self.A, self.B])

    def test_empty_lines(self):
        self.assertEqual(cd2.parse_backup_log_lines([]), [])


class CD2LogDirTest(unittest.TestCase):
    """cd2_log_dir：读取 tg_secrets.json 的 cd2.log_dir。"""

    def test_unconfigured_is_empty(self):
        with mock.patch.object(config, "_SECRET_CONFIG", {}):
            self.assertEqual(cd2.cd2_log_dir(), "")

    def test_reads_log_dir(self):
        with mock.patch.object(
            config, "_SECRET_CONFIG",
            {"cd2": {"log_dir": "~/Waytech/CloudDrive2/log"}},
        ):
            self.assertEqual(cd2.cd2_log_dir(), "~/Waytech/CloudDrive2/log")


class WhitelistCommitTest(unittest.TestCase):
    """add_to_whitelist / del_from_whitelist：白名单提交。"""

    def setUp(self):
        state.WHITELIST_CHATS.clear()
        self.saved = []
        patcher = mock.patch.object(
            whitelist, "save_whitelist", side_effect=self._fake_save
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_save(self, chats, path=None):
        self.saved.append(dict(chats))

    def test_add_new_chat(self):
        ok, msg = whitelist.add_to_whitelist(777, "机器人")
        self.assertTrue(ok)
        self.assertEqual(state.WHITELIST_CHATS, {777: "机器人"})
        self.assertEqual(self.saved[-1], {777: "机器人"})

    def test_add_duplicate_fails(self):
        whitelist.add_to_whitelist(777, "机器人")
        ok, msg = whitelist.add_to_whitelist(777, "机器人")
        self.assertFalse(ok)

    def test_del_by_id(self):
        whitelist.add_to_whitelist(777, "机器人")
        ok, msg = whitelist.del_from_whitelist("777")
        self.assertTrue(ok)
        self.assertEqual(state.WHITELIST_CHATS, {})

    def test_del_missing_fails(self):
        ok, msg = whitelist.del_from_whitelist("123")
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
        to_delete, to_keep = cleanup.plan_bot_chat_cleanup(messages, age_limit=1)
        self.assertEqual(to_keep, {2})
        self.assertEqual(set(to_delete), {1, 3})

    def test_deletes_only_old_messages(self):
        messages = [
            self._msg(1, age_minutes=0.5, has_buttons=False),
            self._msg(2, age_minutes=2, has_buttons=False),
        ]
        to_delete, to_keep = cleanup.plan_bot_chat_cleanup(messages, age_limit=1)
        self.assertEqual(to_delete, [2])
        self.assertEqual(to_keep, set())

    def test_no_messages(self):
        to_delete, to_keep = cleanup.plan_bot_chat_cleanup([], age_limit=1)
        self.assertEqual(to_delete, [])
        self.assertEqual(to_keep, set())


if __name__ == "__main__":
    unittest.main()


class FindMenuEntryTest(unittest.TestCase):
    """主菜单带【🔍 查询】按钮，动作注册为 find（2026-09-08 媒体查询）。"""

    def test_main_menu_has_find_entry(self):
        rows = menu.main_menu_buttons()
        texts = [b.text for row in rows for b in row]
        self.assertTrue(any("查询" in t for t in texts))
        actions = [menu.parse_menu_data(b.data)[0]
                   for row in rows for b in row]
        self.assertIn("find", actions)
