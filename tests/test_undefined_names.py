"""未定义名哨兵 + 离线完成守望调度测试。

背景（2026-09-29）：同一类「漏 import → 回复/通知静默死亡」的缺陷一次抓出
12 处（bot.py 的 encode_menu_data/cd2_api/text、commands.py 的 Button、
listener_worker.py 的 except 变量、torrent_offline.py 的 config/notify——
导致 .torrent 直链提交成功却永远没有回执、离线完成守望从未运行）。本文件
两道防线：

1. pyflakes 哨兵：任何未定义名出现即测试失败（这是「发了没反应」的根类）。
2. spawn_watch 功能测试：守望必须真的被调度运行（此前返回的协程无人 await）。

    .venv/bin/python -m unittest tests.test_undefined_names -v
"""
import asyncio
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录（config import 期
# 有真实副作用，见 test_queue_wiring 的基座说明）。
_TMP = tempfile.mkdtemp(prefix="tg_userbot_undefined_names_test_")
import atexit  # noqa: E402
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "tg_userbot")

from tg_userbot import torrent_offline  # noqa: E402


class TestNoUndefinedNames(unittest.TestCase):
    """pyflakes 哨兵：包内不允许任何 undefined name（漏 import 的根类）。"""

    def test_no_undefined_names(self):
        if importlib.util.find_spec("pyflakes") is None:
            self.skipTest("pyflakes 未安装（.venv/bin/pip install pyflakes）")
        proc = subprocess.run(
            [sys.executable, "-m", "pyflakes",
             *[os.path.join(PKG, f) for f in sorted(os.listdir(PKG))
               if f.endswith(".py")]],
            capture_output=True, text=True, timeout=120)
        findings = [ln for ln in proc.stdout.splitlines()
                    if "undefined name" in ln]
        self.assertEqual(
            findings, [],
            f"包内存在未定义名（运行时 NameError → 回复/通知静默死亡）：\n"
            + "\n".join(findings))


class TestSpawnWatch(unittest.IsolatedAsyncioTestCase):
    """spawn_watch 必须把守望真正挂上事件循环（此前协程无人 await）。"""

    async def test_watch_scheduled_and_runs(self):
        calls = []

        async def fake_watcher(to_folder, known, label=""):
            calls.append((to_folder, set(known or []), label))

        def fake_dir_names(path):
            return {"旧文件.bin": 100}

        with mock.patch.object(torrent_offline, "_dir_names",
                               fake_dir_names), \
             mock.patch.object(torrent_offline, "spawn_completion_watcher",
                               fake_watcher):
            t = torrent_offline.spawn_watch("/115open/云下载", label="测试种")
            self.assertIsNotNone(t)
            # 任务在事件循环里跑完（快照走线程池，让一步）
            await asyncio.wait_for(t, timeout=5)

        self.assertEqual(calls, [("/115open/云下载", {"旧文件.bin"}, "测试种")])
        # 任务结束后强引用集合应自动清理
        self.assertNotIn(t, torrent_offline._OFFLINE_WATCH_TASKS)

    async def test_known_names_bypasses_snapshot(self):
        calls = []

        async def fake_watcher(to_folder, known, label=""):
            calls.append(set(known or []))

        with mock.patch.object(torrent_offline, "_dir_names",
                               unittest.mock.Mock(side_effect=AssertionError(
                                   "传了 known_names 就不该再抓快照"))), \
             mock.patch.object(torrent_offline, "spawn_completion_watcher",
                               fake_watcher):
            t = torrent_offline.spawn_watch(
                "/115open/云下载", known_names={"已快照.bin"})
            await asyncio.wait_for(t, timeout=5)

        self.assertEqual(calls, [{"已快照.bin"}])


if __name__ == "__main__":
    unittest.main()
