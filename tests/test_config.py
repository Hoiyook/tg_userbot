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
import atexit
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录：config 的 import
# 期有真实副作用（mkdir + 旧数据根迁移闸门），零配置裸 import 会以「桌面默认
# 部署」形态创建真实 /Volumes/V1 子目录、甚至迁移真实 ~/Downloads/Nagram。
_TMP = tempfile.mkdtemp(prefix="tg_userbot_config_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402


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

    def test_download_dir_under_download_dir(self):
        """路径解耦后 Chrome 媒体归属 DOWNLOAD_DIR（SAVE_FOLDER 只是别名）。"""
        self.assertEqual(
            config.CHROME_DOWNLOAD_DIR,
            os.path.join(config.DOWNLOAD_DIR, "TG Chrome Download"))

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


class TestPathResolution(unittest.TestCase):
    """`_resolve_paths` 纯函数：环境变量/平台 → 四个路径常量的解析契约。

    解析逻辑独立成纯函数是为了可测：整模块 reload 会在 import 期对真实
    文件系统做 makedirs/迁移，测试里既污染真实数据卷又互相踩。
    """

    @staticmethod
    def _resolve(env=None, is_termux=False):
        return config._resolve_paths(env or {}, is_termux)

    def test_desktop_defaults(self):
        """测试 1：桌面零配置 → /Volumes/V1 三件套。"""
        p = self._resolve({})
        self.assertEqual(p.data_root, "/Volumes/V1")
        self.assertEqual(p.download_dir, "/Volumes/V1/downloads")
        self.assertEqual(p.runtime_dir, "/Volumes/V1/runtime")
        self.assertEqual(p.runtime_db_file, "/Volumes/V1/runtime/tg_userbot.db")

    def test_termux_defaults_unchanged(self):
        """Termux 零配置 → 与旧版布局逐字节一致：媒体与数据根同目录，
        runtime/ 是其子目录（本次改造不为 Android 改任何行为）。"""
        nagram = "/storage/emulated/0/Download/Nagram"
        p = self._resolve({}, is_termux=True)
        self.assertEqual(p.data_root, nagram)
        self.assertEqual(p.download_dir, nagram)
        self.assertEqual(p.runtime_dir, os.path.join(nagram, "runtime"))

    def test_data_root_env(self):
        """测试 2：TG_DATA_ROOT 派生 downloads/ 与 runtime/。"""
        p = self._resolve({"TG_DATA_ROOT": "/tmp/test-data"})
        self.assertEqual(p.data_root, "/tmp/test-data")
        self.assertEqual(p.download_dir, "/tmp/test-data/downloads")
        self.assertEqual(p.runtime_dir, "/tmp/test-data/runtime")

    def test_download_dir_env(self):
        """测试 3：TG_DOWNLOAD_DIR 只动下载目录。"""
        p = self._resolve({"TG_DOWNLOAD_DIR": "/tmp/downloads"})
        self.assertEqual(p.download_dir, "/tmp/downloads")
        self.assertEqual(p.runtime_dir, "/Volumes/V1/runtime")

    def test_runtime_dir_env(self):
        """测试 4：TG_RUNTIME_DIR 只动 Runtime 目录。"""
        p = self._resolve({"TG_RUNTIME_DIR": "/tmp/runtime"})
        self.assertEqual(p.download_dir, "/Volumes/V1/downloads")
        self.assertEqual(p.runtime_dir, "/tmp/runtime")

    def test_download_and_runtime_fully_independent(self):
        """测试 5：两个目录各设各的，互不影响。"""
        p = self._resolve({"TG_DOWNLOAD_DIR": "/tmp/d",
                           "TG_RUNTIME_DIR": "/tmp/r"})
        self.assertEqual(p.download_dir, "/tmp/d")
        self.assertEqual(p.runtime_dir, "/tmp/r")

    def test_legacy_save_folder_compat(self):
        """测试 6：旧变量 TG_SAVE_FOLDER 仍是下载根的有效来源；
        且只设它（无 TG_DATA_ROOT）时 runtime 仍与它同目录——
        测试基座（34 个模块 import 前只设 TG_SAVE_FOLDER）与老部署都靠这条。"""
        p = self._resolve({"TG_SAVE_FOLDER": "/tmp/legacy"})
        self.assertEqual(p.download_dir, "/tmp/legacy")
        self.assertEqual(p.runtime_dir, "/tmp/legacy/runtime")

    def test_new_download_dir_beats_legacy(self):
        """测试 7：TG_DOWNLOAD_DIR 优先于旧 TG_SAVE_FOLDER。"""
        p = self._resolve({"TG_DOWNLOAD_DIR": "/tmp/new",
                           "TG_SAVE_FOLDER": "/tmp/legacy"})
        self.assertEqual(p.download_dir, "/tmp/new")

    def test_runtime_db_single_file_override(self):
        """测试 8：TG_RUNTIME_DB 单文件覆盖能力不丢。"""
        p = self._resolve({"TG_RUNTIME_DB": "/tmp/db/tg.db"})
        self.assertEqual(p.runtime_db_file, "/tmp/db/tg.db")

    def test_data_root_beats_legacy_for_runtime(self):
        """显式设了 TG_DATA_ROOT（新式配置意图）时，runtime 回到
        DATA_ROOT/runtime；TG_SAVE_FOLDER 只剩下载目录兼容语义。"""
        p = self._resolve({"TG_DATA_ROOT": "/tmp/newroot",
                           "TG_SAVE_FOLDER": "/tmp/legacy"})
        self.assertEqual(p.download_dir, "/tmp/legacy")
        self.assertEqual(p.runtime_dir, "/tmp/newroot/runtime")

    def test_import_time_constants_follow_resolution(self):
        """import 期常量满足解耦模型的派生关系，且把基座注入的
        TG_SAVE_FOLDER 喂回纯函数能复现同一组值。

        注意不能拿「测试运行时的 os.environ」对比：同进程 suite 里 config
        由最先 import 的测试模块按它自己的临时目录解析，之后各模块再改
        TG_SAVE_FOLDER 也不影响已 import 的常量。
        """
        self.assertEqual(config.SAVE_FOLDER, config.DOWNLOAD_DIR)
        self.assertEqual(config.LOG_FILE,
                         os.path.join(config.RUNTIME_DIR, "download.log"))
        self.assertEqual(config.RUNTIME_DB_FILE,
                         os.path.join(config.RUNTIME_DIR, "tg_userbot.db"))
        self.assertEqual(config.CHROME_DOWNLOAD_DIR,
                         os.path.join(config.DOWNLOAD_DIR,
                                      "TG Chrome Download"))
        p = config._resolve_paths(
            {"TG_SAVE_FOLDER": config.SAVE_FOLDER}, config.IS_TERMUX)
        self.assertEqual(p.download_dir, config.DOWNLOAD_DIR)
        self.assertEqual(p.runtime_dir, config.RUNTIME_DIR)
        self.assertEqual(p.runtime_db_file, config.RUNTIME_DB_FILE)


class TestLegacyMigration(unittest.TestCase):
    """`_migrate_legacy_data`：旧数据根 → 新 downloads/runtime 的一次性迁移。

    契约（任务书 §18-22）：先识别旧路径再切换；目标已存在 → 保留新文件
    不覆盖并记冲突；tg_userbot.db 冲突必须点名两个路径；绝不删除旧根。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="migrate_test_")
        self.old_root = os.path.join(self.tmp, "old_nagram")
        self.dl = os.path.join(self.tmp, "new_downloads")
        self.rt = os.path.join(self.tmp, "new_runtime")
        for d in (self.old_root, self.dl, self.rt):
            os.makedirs(d)
        # 旧 runtime/
        old_rt = os.path.join(self.old_root, "runtime")
        os.makedirs(old_rt)
        self._write(old_rt, "tg_userbot.db", b"OLD-DB")
        self._write(old_rt, "listen.json", b'{"rules": []}')
        self._write(old_rt, "task_events.jsonl", b'{"ev":"SUCCESS"}\n')
        # 旧根散落的运行时文件（历史版本布局）
        self._write(self.old_root, "download_history.txt", b"h1\n")
        self._write(self.old_root, "whitelist_config.json", b'{"chats": []}')
        # 媒体（含嵌套目录）
        self._write(self.old_root, "频道A/video.mp4", b"VIDEO")
        self._write(self.old_root, "抖音/nested/a.mp4", b"AUDIO")
        self._write(self.old_root, "TG Chrome Download/x/y.zip", b"ZIP")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _write(root, rel, data):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def _read(self, path):
        with open(path, "rb") as f:
            return f.read()

    def _run(self):
        return config._migrate_legacy_data(self.old_root, self.dl, self.rt)

    def test_runtime_files_move_first(self):
        summary = self._run()
        self.assertEqual(self._read(os.path.join(self.rt, "tg_userbot.db")),
                         b"OLD-DB")
        self.assertEqual(self._read(os.path.join(self.rt, "listen.json")),
                         b'{"rules": []}')
        self.assertEqual(
            self._read(os.path.join(self.rt, "task_events.jsonl")),
            b'{"ev":"SUCCESS"}\n')
        self.assertIn("tg_userbot.db", summary["runtime"])

    def test_scattered_root_files_go_to_runtime(self):
        self._run()
        self.assertEqual(
            self._read(os.path.join(self.rt, "download_history.txt")), b"h1\n")
        self.assertEqual(
            self._read(os.path.join(self.rt, "whitelist_config.json")),
            b'{"chats": []}')
        self.assertFalse(os.path.exists(
            os.path.join(self.old_root, "download_history.txt")))

    def test_media_goes_to_download_dir(self):
        self._run()
        self.assertEqual(
            self._read(os.path.join(self.dl, "频道A", "video.mp4")), b"VIDEO")
        self.assertEqual(
            self._read(os.path.join(self.dl, "抖音", "nested", "a.mp4")),
            b"AUDIO")
        self.assertEqual(
            self._read(os.path.join(self.dl, "TG Chrome Download",
                                    "x", "y.zip")), b"ZIP")
        # 源侧不再保留已迁走的媒体
        self.assertFalse(os.path.exists(
            os.path.join(self.old_root, "频道A")))

    def test_legacy_root_not_deleted(self):
        self._run()
        self.assertTrue(os.path.isdir(self.old_root))
        self.assertTrue(os.path.isdir(os.path.join(self.old_root, "runtime")))

    def test_target_conflict_keeps_new_file(self):
        """测试 11：目标已存在 → 保留新文件、不覆盖、源保留、记冲突。"""
        self._write(self.rt, "listen.json", b'{"new": true}')
        summary = self._run()
        self.assertEqual(self._read(os.path.join(self.rt, "listen.json")),
                         b'{"new": true}')
        self.assertEqual(
            self._read(os.path.join(self.old_root, "runtime",
                                    "listen.json")), b'{"rules": []}')
        self.assertTrue(any("listen.json" in c for c in summary["conflicts"]))

    def test_db_conflict_names_both_paths(self):
        """§21：旧 DB 与新 DB 同时存在 → 禁止覆盖，且提示必须点名两个路径。"""
        self._write(self.rt, "tg_userbot.db", b"NEW-DB")
        summary = self._run()
        self.assertEqual(self._read(os.path.join(self.rt, "tg_userbot.db")),
                         b"NEW-DB")
        conflicts = "；".join(summary["conflicts"])
        self.assertIn("tg_userbot.db", conflicts)
        self.assertIn(self.old_root, conflicts)
        self.assertIn(self.rt, conflicts)
        # 源 DB 原样保留（绝不丢旧数据）
        self.assertEqual(
            self._read(os.path.join(self.old_root, "runtime",
                                    "tg_userbot.db")), b"OLD-DB")

    def test_idempotent_second_run_is_noop(self):
        first = self._run()
        self.assertTrue(first["runtime"] or first["media"])
        second = self._run()
        self.assertEqual(second["runtime"], [])
        self.assertEqual(second["scattered"], [])
        self.assertEqual(second["media"], [])
        self.assertEqual(second["conflicts"], [])

    def test_stale_migrating_temp_is_retried(self):
        """崩溃残留的 .migrating 临时文件：重跑时清掉重拷，不会留下半个文件。"""
        stale = os.path.join(self.dl, "video.mp4.migrating")
        with open(stale, "wb") as f:
            f.write(b"PARTIAL")
        # 直接在下载根同名冲突形态下模拟：频道A/video.mp4 已有 .migrating
        os.makedirs(os.path.join(self.dl, "频道A"), exist_ok=True)
        self._run()
        self.assertFalse(os.path.exists(stale))
        self.assertEqual(
            self._read(os.path.join(self.dl, "频道A", "video.mp4")), b"VIDEO")


class TestLegacyMigrationGate(unittest.TestCase):
    """import 期自动迁移的触发闸门：只在「零配置桌面默认部署」生效。

    测试基座（34 个模块 import 前设 TG_SAVE_FOLDER）与任何自定义部署
    都必须被闸住，否则跑一次单测就会把真实 ~/Downloads/Nagram 搬走。
    """

    @staticmethod
    def _write(root, rel, data):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_fires_for_pure_default_desktop(self):
        with tempfile.TemporaryDirectory() as legacy:
            self._write(legacy, "频道A/video.mp4", b"V")
            self.assertTrue(config._legacy_migration_root(
                env={}, is_termux=False, legacy_root=legacy))

    def test_gated_when_legacy_root_is_empty(self):
        """空旧根（无 runtime/ 也无媒体）→ 无物可迁，静默跳过。"""
        with tempfile.TemporaryDirectory() as legacy:
            self.assertIsNone(config._legacy_migration_root(
                env={}, is_termux=False, legacy_root=legacy))

    def test_gated_by_any_path_override(self):
        with tempfile.TemporaryDirectory() as legacy:
            for key in ("TG_SAVE_FOLDER", "TG_DOWNLOAD_DIR", "TG_DATA_ROOT",
                        "TG_RUNTIME_DIR"):
                with self.subTest(key=key):
                    self.assertIsNone(config._legacy_migration_root(
                        env={key: "/tmp/x"}, is_termux=False,
                        legacy_root=legacy))

    def test_gated_on_termux(self):
        with tempfile.TemporaryDirectory() as legacy:
            self.assertIsNone(config._legacy_migration_root(
                env={}, is_termux=True, legacy_root=legacy))

    def test_gated_by_kill_switch(self):
        with tempfile.TemporaryDirectory() as legacy:
            self.assertIsNone(config._legacy_migration_root(
                env={"TG_MIGRATE_LEGACY": "0"}, is_termux=False,
                legacy_root=legacy))

    def test_gated_when_legacy_missing(self):
        missing = os.path.join(tempfile.gettempdir(), "no_such_legacy_dir_x32")
        self.assertIsNone(config._legacy_migration_root(
            env={}, is_termux=False, legacy_root=missing))

    def test_gated_when_only_skeleton_fully_migrated(self):
        """旧根只剩 runtime/ 骨架、且其中文件都已在新 RUNTIME_DIR 就位
        （上轮已迁完）→ 静默跳过，不每次启动都重报迁移冲突。"""
        with tempfile.TemporaryDirectory() as legacy, \
                tempfile.TemporaryDirectory() as new_rt:
            os.makedirs(os.path.join(legacy, "runtime"))
            self._write(legacy, "runtime/tg_userbot.db", b"OLD")
            self._write(new_rt, "tg_userbot.db", b"OLD")
            with patch.object(config, "RUNTIME_DIR", new_rt):
                self.assertIsNone(config._legacy_migration_root(
                    env={}, is_termux=False, legacy_root=legacy))

    def test_fires_when_skeleton_has_unmigrated_delta(self):
        """骨架里有目标缺着的增量文件（旧进程分裂写入的收尾场景）→ 仍要迁。"""
        with tempfile.TemporaryDirectory() as legacy, \
                tempfile.TemporaryDirectory() as new_rt:
            os.makedirs(os.path.join(legacy, "runtime"))
            self._write(legacy, "runtime/task_events.jsonl", b'{"ev":1}\n')
            self._write(new_rt, "tg_userbot.db", b"OLD")
            with patch.object(config, "RUNTIME_DIR", new_rt):
                self.assertEqual(
                    config._legacy_migration_root(
                        env={}, is_termux=False, legacy_root=legacy),
                    legacy)


if __name__ == '__main__':
    unittest.main()
