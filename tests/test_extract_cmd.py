"""/115x 命令层测试（extract_worker.command_reply）。

覆盖：命令识别（/115xfoo 不算）、路径归一（三种写法同义 + .. 拒绝）、
扫描入队回执（含幂等跳过）、stop/start、retry/del 短 id 前缀匹配。

    .venv/bin/python -m unittest tests.test_extract_cmd -v
"""
import asyncio
import atexit
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest as _btn_shim  # noqa: F401,E402

_TMP = tempfile.mkdtemp(prefix="tg_userbot_extract_cmd_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import extract_worker as ew  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402


class CmdBase(unittest.TestCase):
    def setUp(self):
        self.db_dir = tempfile.mkdtemp(prefix="rtdb_", dir=_TMP)
        self.mount = tempfile.mkdtemp(prefix="mount_", dir=_TMP)
        patches = [
            mock.patch.object(config, "RUNTIME_DB_FILE",
                              os.path.join(self.db_dir, "tg_userbot.db")),
            mock.patch.object(config, "CLOUD_MOUNT_BASE", self.mount),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        ew._PAUSED["paused"] = False
        self.addCleanup(ew._PAUSED.update, {"paused": False})

    def _put_zip(self, rel_dir, name, members):
        d = os.path.join(self.mount, rel_dir)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with zipfile.ZipFile(path, "w") as z:
            for n, data in members.items():
                z.writestr(n, data)
        return os.path.getsize(path)

    def _fake_listing(self):
        def fake_list(remote_path, limit=500):
            p = os.path.join(
                self.mount, str(remote_path)[len("/115open/"):].strip("/"))
            if not os.path.isdir(p):
                return None
            out = []
            for n in sorted(os.listdir(p)):
                fp = os.path.join(p, n)
                if os.path.isdir(fp):
                    out.append((n, 0, True))
                else:
                    out.append((n, os.path.getsize(fp), False))
            return out
        return fake_list


class IdentifyTest(CmdBase):
    def test_is_extract_command(self):
        self.assertTrue(ew.is_extract_command("/115x"))
        self.assertTrue(ew.is_extract_command("/115x 云下载"))
        self.assertTrue(ew.is_extract_command("/115x_stop"))
        self.assertFalse(ew.is_extract_command("/115xy"))
        self.assertFalse(ew.is_extract_command("/115"))

    def test_normalize_remote_dir(self):
        self.assertEqual(ew.normalize_remote_dir("云下载"),
                         "/115open/云下载")
        self.assertEqual(ew.normalize_remote_dir("/云下载/画集"),
                         "/115open/云下载/画集")
        self.assertEqual(ew.normalize_remote_dir("/115open/云下载/"),
                         "/115open/云下载")
        self.assertIsNone(ew.normalize_remote_dir("../etc"))
        self.assertIsNone(ew.normalize_remote_dir(""))
        self.assertIsNone(ew.normalize_remote_dir("/115open/../x"))


class ScanEnqueueTest(CmdBase):
    def test_scan_and_enqueue(self):
        size = self._put_zip("云下载", "画集Z.zip", {"a.txt": b"x"})
        self._put_zip("云下载", "已入队.zip", {"b.txt": b"y"})
        runtime_db.enqueue_extract_tasks("/115open/云下载",
                                         [("已入队.zip", os.path.getsize(
                                             os.path.join(
                                                 self.mount, "云下载",
                                                 "已入队.zip")))])
        with mock.patch("tg_userbot.cd2_api.list_remote_dir",
                        side_effect=self._fake_listing()):
            out = asyncio.run(ew.command_reply("/115x 云下载"))
        self.assertIn("发现 2 个压缩包", out)
        self.assertIn("新入队 1", out)
        self.assertIn("跳过 1", out)
        tasks = runtime_db.list_extract_tasks()
        names = sorted(t["archive_name"] for t in tasks)
        self.assertEqual(names, ["已入队.zip", "画集Z.zip"])
        # 远端尺寸对齐入队
        z = [t for t in tasks if t["archive_name"] == "画集Z.zip"][0]
        self.assertEqual(z["archive_size"], size)

    def test_no_archives(self):
        os.makedirs(os.path.join(self.mount, "空目录"), exist_ok=True)
        with open(os.path.join(self.mount, "空目录", "x.mp4"), "wb") as f:
            f.write(b"v")
        with mock.patch("tg_userbot.cd2_api.list_remote_dir",
                        side_effect=self._fake_listing()):
            out = asyncio.run(ew.command_reply("/115x 空目录"))
        self.assertIn("没有发现压缩包", out)

    def test_zip_named_dirs_hint(self):
        # 115 云解压会留下 .zip 命名的目录——应提示「无包可拉」而非静默
        d = os.path.join(self.mount, "云解压目录")
        os.makedirs(os.path.join(d, "画集A.zip"), exist_ok=True)
        with open(os.path.join(d, "画集A.zip", "内页.jpg"), "wb") as f:
            f.write(b"j")
        with mock.patch("tg_userbot.cd2_api.list_remote_dir",
                        side_effect=self._fake_listing()):
            out = asyncio.run(ew.command_reply("/115x 云解压目录"))
        self.assertIn("没有发现压缩包文件", out)
        self.assertIn("命名的**目录**", out)
        self.assertIn("画集A.zip", out)

    def test_unreadable_dir(self):
        with mock.patch("tg_userbot.cd2_api.list_remote_dir",
                        return_value=None):
            out = asyncio.run(ew.command_reply("/115x 幽灵目录"))
        self.assertIn("目录不可读", out)

    def test_traversal_rejected(self):
        out = asyncio.run(ew.command_reply("/115x ../etc"))
        self.assertIn("路径无效", out)


class ControlTest(CmdBase):
    def test_stop_start(self):
        out = asyncio.run(ew.command_reply("/115x_stop"))
        self.assertIn("已暂停", out)
        self.assertTrue(ew._PAUSED["paused"])
        out = asyncio.run(ew.command_reply("/115x start"))
        self.assertIn("已恢复", out)
        self.assertFalse(ew._PAUSED["paused"])

    def test_status_and_retry_del(self):
        ins, _ = runtime_db.enqueue_extract_tasks(
            "/115open/云下载", [("包R.zip", 10)])
        tid = runtime_db.list_extract_tasks()[0]["id"]
        runtime_db.claim_next_extract_task()
        runtime_db.terminate_extract_task(tid, "需密码")
        # 裸命令 = 状态视图（含终结任务与错误）
        out = asyncio.run(ew.command_reply("/115x"))
        self.assertIn("终结 1", out)
        self.assertIn("需密码", out)
        # 短 id 前缀重投
        out = asyncio.run(ew.command_reply(f"/115x retry {str(tid)[:3]}"))
        self.assertIn("已重投", out)
        self.assertEqual(
            runtime_db.get_extract_task(tid)["status"],
            runtime_db.EXTRACT_PENDING)
        # 删除（短 id 前缀）
        out = asyncio.run(ew.command_reply(f"/115x del {str(tid)[:3]}"))
        self.assertIn("已移除", out)
        self.assertIsNone(runtime_db.get_extract_task(tid))

    def test_retry_no_match(self):
        out = asyncio.run(ew.command_reply("/115x retry 999"))
        self.assertIn("没有匹配", out)


if __name__ == "__main__":
    unittest.main()
