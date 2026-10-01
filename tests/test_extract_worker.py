"""extract_worker 115 解压回传 worker 测试。

mock 掉挂载（临时目录充当 CLOUD_MOUNT_BASE）与 cd2_api（list_remote_dir
从"假挂载"读——写入假挂载的文件对账自然可见），全链路离线跑通：
COPY 幂等 / 解压 / UPLOAD 逐文件 / VERIFY 通过与失败 / 密码终结。

    .venv/bin/python -m unittest tests.test_extract_worker -v
"""
import asyncio
import atexit
import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest as _btn_shim  # noqa: F401,E402

_TMP = tempfile.mkdtemp(prefix="tg_userbot_extract_worker_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import extract_worker as ew  # noqa: E402
from tg_userbot import notify  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402
from tg_userbot import state  # noqa: E402


class _Env(unittest.IsolatedAsyncioTestCase):
    """假 115 挂载 + 真 DB（临时文件）+ 假 cd2_api + 收口的 notify。"""

    def setUp(self):
        self.mount = tempfile.mkdtemp(prefix="mount_", dir=_TMP)
        self.db_dir = tempfile.mkdtemp(prefix="rtdb_", dir=_TMP)
        self.staging = tempfile.mkdtemp(prefix="staging_", dir=_TMP)
        self._patches = [
            mock.patch.object(config, "RUNTIME_DB_FILE",
                              os.path.join(self.db_dir, "tg_userbot.db")),
            mock.patch.object(config, "CLOUD_MOUNT_BASE", self.mount),
            mock.patch.object(config, "EXTRACT_STAGING_ROOT", self.staging),
            mock.patch.object(config, "EXTRACT_VERIFY_TRIES", 2),
            mock.patch.object(config, "EXTRACT_VERIFY_GAP_SECONDS", 0.0),
            mock.patch.object(config, "PAWCHIVE_MIN_FREE_GB", 0.001),
            mock.patch.object(state, "RUNTIME_DB_READY", True),
            mock.patch.object(ew.notify, "notify_user",
                              new=mock.AsyncMock()),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())

        # cd2_api 对账：list_remote_dir 直接读假挂载；tasks_reply 报 0 在途
        def fake_list(remote_path, limit=500):
            mount_path = os.path.join(
                self.mount, str(remote_path)[len("/115open/"):].strip("/"))
            if not os.path.isdir(mount_path):
                return None
            out = []
            for n in sorted(os.listdir(mount_path)):
                p = os.path.join(mount_path, n)
                if os.path.isdir(p):
                    out.append((n, 0, True))
                else:
                    out.append((n, os.path.getsize(p), False))
            return out

        self._list = mock.patch.object(
            ew, "__dict__")  # 占位，实际在 _patch_cd2 里逐个 patch
        self._cd2_patches = [
            mock.patch("tg_userbot.cd2_api.list_remote_dir",
                       side_effect=fake_list),
            mock.patch("tg_userbot.cd2_api.tasks_reply",
                       new=mock.AsyncMock(return_value=(
                           "☁ CD2 上传任务\n"
                           "计数：⬇️下载 0 | ⬆️上传 0 | 复制 0\n"
                           "（没有在途/失败的上传任务）"))),
        ]
        for p in self._cd2_patches:
            p.start()
            self.addCleanup(p.stop)
        ew._STOP["requested"] = False
        self.addCleanup(ew._STOP.update, {"requested": False})

    # ---- 假 115 造数 ----
    def _put_remote_zip(self, rel_dir, name, members):
        d = os.path.join(self.mount, rel_dir)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with zipfile.ZipFile(path, "w") as z:
            for n, data in members.items():
                z.writestr(n, data)
        return os.path.getsize(path)

    def _enqueue_one(self, rel_dir, name, size):
        ins, _skip = runtime_db.enqueue_extract_tasks(
            f"/115open/{rel_dir}", [(name, size)])
        self.assertEqual(ins, 1)
        return runtime_db.list_extract_tasks()[0]

    async def _run_one_tick(self):
        await ew._tick()

    def _notify_calls(self):
        return ew.notify.notify_user.await_args_list


class PathMappingTest(_Env):
    def test_mount_of_roundtrip(self):
        self.assertEqual(ew.mount_of("/115open/云下载/画集"),
                         os.path.join(self.mount, "云下载/画集"))
        self.assertIsNone(ew.mount_of("/other/x"))
        self.assertIsNone(ew.mount_of("/115open/"))
        self.assertEqual(
            ew.remote_of(os.path.join(self.mount, "云下载", "a.zip")),
            "/115open/云下载/a.zip")

    def test_is_archive_name(self):
        self.assertTrue(ew.is_archive_name("a.ZIP"))
        self.assertTrue(ew.is_archive_name("b.tar.gz"))
        self.assertFalse(ew.is_archive_name("c.mp4"))
        self.assertFalse(ew.is_archive_name(""))


class HappyPathTest(_Env):
    async def test_full_pipeline(self):
        size = self._put_remote_zip("云下载", "画集A.zip", {
            "图1.png": b"0" * 1000, "子/图2.png": b"1" * 500})
        task = self._enqueue_one("云下载", "画集A.zip", size)

        await self._run_one_tick()
        after = runtime_db.get_extract_task(task["id"])
        self.assertEqual(after["status"], runtime_db.EXTRACT_COMPLETED)

        # 远端产物：<原目录>/<包名>/ 树完整
        out_root = os.path.join(self.mount, "云下载", "画集A")
        self.assertTrue(os.path.isfile(os.path.join(out_root, "图1.png")))
        self.assertTrue(os.path.isfile(
            os.path.join(out_root, "子", "图2.png")))
        # 原压缩包保留
        self.assertTrue(os.path.isfile(
            os.path.join(self.mount, "云下载", "画集A.zip")))
        # 本地 staging 已清理
        self.assertEqual(os.listdir(self.staging), [])
        # 完成通知已发
        text = self._notify_calls()[-1].args[0]
        self.assertIn("解压回传完成", text)

    async def test_copy_idempotent_reuse(self):
        size = self._put_remote_zip("云下载", "包B.zip", {"a.txt": b"x"})
        task = self._enqueue_one("云下载", "包B.zip", size)
        staging = os.path.join(
            self.staging, f"{task['id']}_包B.zip")
        os.makedirs(staging, exist_ok=True)
        # 预置一份正确副本 → COPY 应跳过（不再读"远端"）
        local_arch = os.path.join(staging, "archive.zip")
        with open(local_arch, "wb") as f:
            f.write(b"x" * 0)
        import zipfile as _zf
        with _zf.ZipFile(local_arch, "w") as z:
            z.writestr("a.txt", b"x")
        self.assertEqual(os.path.getsize(local_arch), size)
        # 断言没有再读远端：改坏远端文件内容（尺寸不变很难造，直接改
        # 内容同尺寸）——若 worker 重拷，产物内容会变
        remote = os.path.join(self.mount, "云下载", "包B.zip")
        with _zf.ZipFile(remote, "w") as z:
            z.writestr("a.txt", b"y")   # 同尺寸不同内容
        await self._run_one_tick()
        after = runtime_db.get_extract_task(task["id"])
        self.assertEqual(after["status"], runtime_db.EXTRACT_COMPLETED)
        with open(os.path.join(self.mount, "云下载", "包B", "a.txt"),
                  "rb") as f:
            self.assertEqual(f.read(), b"x")   # 用的是预置副本


class FailurePathTest(_Env):
    async def test_password_rar_terminates(self):
        d = os.path.join(self.mount, "云下载")
        os.makedirs(d, exist_ok=True)
        bad = os.path.join(d, "加密包.rar")
        with open(bad, "wb") as f:
            f.write(b"Rar! not really, garbage that bsdtar cannot list")
        task = self._enqueue_one("云下载", "加密包.rar", os.path.getsize(bad))
        await self._run_one_tick()
        after = runtime_db.get_extract_task(task["id"])
        self.assertEqual(after["status"], runtime_db.EXTRACT_TERMINAL)
        self.assertIn("需密码", after["error"])
        text = self._notify_calls()[-1].args[0]
        self.assertIn("需密码", text)

    def _break_verify(self, marker):
        """让对账对包含 marker 的远端目录报尺寸漂移（其余照常）。"""
        from tg_userbot import cd2_api
        orig = cd2_api.list_remote_dir

        def broken_list(remote_path, limit=500):
            if marker in str(remote_path):
                return [("漂移.txt", 999, False)]
            return orig(remote_path, limit=limit)

        return mock.patch.object(cd2_api, "list_remote_dir",
                                 side_effect=broken_list)

    async def test_verify_mismatch_fails(self):
        size = self._put_remote_zip("云下载", "包C.zip", {"a.txt": b"x" * 10})
        task = self._enqueue_one("云下载", "包C.zip", size)
        # 失败流：postpone → PENDING + error（同一行退避重投，不新增）
        with self._break_verify("包C"):
            await self._run_one_tick()
        after = runtime_db.get_extract_task(task["id"])
        self.assertEqual(after["status"], runtime_db.EXTRACT_PENDING)
        self.assertIn("对账不一致", after["error"])
        self.assertGreater(after["attempts"], 0)

    async def test_retry_terminates_after_max_attempts(self):
        size = self._put_remote_zip("云下载", "包D.zip", {"a.txt": b"x"})
        task = self._enqueue_one("云下载", "包D.zip", size)
        with mock.patch.object(config, "EXTRACT_MAX_ATTEMPTS", 1), \
                self._break_verify("包D"):
            await self._run_one_tick()
        after = runtime_db.get_extract_task(task["id"])
        self.assertEqual(after["status"], runtime_db.EXTRACT_TERMINAL)
        # 手动重投后再失败 → 再次终结（仍 TERMINAL）
        self.assertTrue(runtime_db.retry_extract_task(task["id"]))
        with mock.patch.object(config, "EXTRACT_MAX_ATTEMPTS", 1), \
                self._break_verify("包D"):
            await self._run_one_tick()
        after = runtime_db.get_extract_task(task["id"])
        self.assertEqual(after["status"], runtime_db.EXTRACT_TERMINAL)

    async def test_remote_missing_terminates(self):
        ins, _ = runtime_db.enqueue_extract_tasks(
            "/115open/云下载", [("幽灵.zip", 123)])
        task = runtime_db.list_extract_tasks()[0]
        await self._run_one_tick()
        after = runtime_db.get_extract_task(task["id"])
        self.assertEqual(after["status"], runtime_db.EXTRACT_TERMINAL)
        self.assertIn("已不存在", after["error"])


class PreconditionTest(_Env):
    def test_mount_missing_blocks(self):
        with mock.patch.object(config, "CLOUD_MOUNT_BASE",
                               "/nonexistent_mount"):
            self.assertFalse(ew._preconditions_ok())


if __name__ == "__main__":
    unittest.main()
