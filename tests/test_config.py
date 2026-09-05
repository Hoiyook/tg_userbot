"""运行时文件归集（RUNTIME_DIR）与历史文件迁移的单元测试。

只测纯函数 _migrate_runtime_files（显式传临时目录），不碰模块全局目录：
import 包时 config 已用某个测试临时目录跑过一次真实迁移，这里不再依赖它。
"""
import os
import tempfile
import unittest

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_config_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402


RUNTIME_BASENAMES = (
    "download.log", "download_history.txt", "thread_config.json",
    "whitelist_config.json", "download_queue.json", "clear_time.json",
    "cd2_launch.log",
)


class RuntimeDirConfigTest(unittest.TestCase):
    """常量落点：LOG_FILE / 各运行时 JSON / 历史文件都指向 RUNTIME_DIR。"""

    def test_runtime_dir_under_save_folder(self):
        self.assertTrue(config.RUNTIME_DIR.startswith(config.SAVE_FOLDER))
        self.assertEqual(
            config.RUNTIME_DIR, os.path.join(config.SAVE_FOLDER, "runtime")
        )

    def test_runtime_files_all_under_runtime_dir(self):
        # LOG_FILE 已在临时目录的 runtime/ 下（真实 import 迁移过）
        self.assertTrue(
            config.LOG_FILE.endswith(os.path.join("runtime", "download.log"))
        )
        for f in (
            config.DOWNLOAD_HISTORY_FILE,
            config.THREAD_CONFIG_FILE,
            config.WHITELIST_FILE,
            config.QUEUE_FILE,
            config.CLEAR_TIME_CONFIG_FILE,
            config.CD2_LAUNCH_LOG,
        ):
            self.assertTrue(
                os.path.dirname(f) == config.RUNTIME_DIR,
                f"{f} 不在 RUNTIME_DIR 下",
            )

    def test_log_retention_default_seven(self):
        self.assertEqual(config.LOG_RETENTION_DAYS, 7)


class RuntimeFilesMigrationTest(unittest.TestCase):
    """_migrate_runtime_files：显式传临时目录，验证搬移/幂等/不误伤。"""

    def _make_dirs(self):
        base = tempfile.mkdtemp(prefix="tg_migrate_test_")
        runtime = os.path.join(base, "runtime")
        os.makedirs(runtime, exist_ok=True)
        return base, runtime

    def test_moves_every_present_basename(self):
        base, runtime = self._make_dirs()
        # 只在根目录放 3 个旧文件（模拟历史版本残留）
        for name in RUNTIME_BASENAMES[:3]:
            with open(os.path.join(base, name), "w", encoding="utf-8") as f:
                f.write("old")
        moved = config._migrate_runtime_files(base, runtime)
        self.assertEqual(sorted(moved), sorted(RUNTIME_BASENAMES[:3]))
        for name in RUNTIME_BASENAMES[:3]:
            self.assertFalse(os.path.exists(os.path.join(base, name)))
            self.assertTrue(os.path.exists(os.path.join(runtime, name)))

    def test_idempotent_when_already_migrated(self):
        base, runtime = self._make_dirs()
        with open(os.path.join(base, "download.log"), "w", encoding="utf-8") as f:
            f.write("old")
        config._migrate_runtime_files(base, runtime)
        second = config._migrate_runtime_files(base, runtime)
        self.assertEqual(second, [])  # 无待迁项，no-op
        with open(os.path.join(runtime, "download.log"), "r",
                  encoding="utf-8") as f:
            self.assertEqual(f.read(), "old")

    def test_does_not_overwrite_existing_runtime_file(self):
        base, runtime = self._make_dirs()
        # 根目录与 runtime 都有同名文件（如降级后又升级）→ 保留 runtime 新版
        with open(os.path.join(base, "download.log"), "w", encoding="utf-8") as f:
            f.write("根目录旧")
        with open(os.path.join(runtime, "download.log"), "w", encoding="utf-8") as f:
            f.write("runtime 新")
        moved = config._migrate_runtime_files(base, runtime)
        self.assertEqual(moved, [])
        with open(os.path.join(runtime, "download.log"), "r",
                  encoding="utf-8") as f:
            self.assertEqual(f.read(), "runtime 新")

    def test_never_touches_unrelated_files(self):
        base, runtime = self._make_dirs()
        stray = os.path.join(base, "我的视频.mp4")
        with open(stray, "w", encoding="utf-8") as f:
            f.write("媒体")
        config._migrate_runtime_files(base, runtime)
        self.assertTrue(os.path.exists(stray))  # 媒体/无关文件绝不搬


if __name__ == "__main__":
    unittest.main()
