"""CD2 API 备份对账（cd2_api.py）的单元测试。

gRPC 层全部 mock（_stub / find_remote / get_space_info / iter_local_files /
backup_tasks），测路径映射 / 对账分类 / 人话报告。不联网、不碰真实 CD2。
运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import asyncio
import atexit
import os
import shutil
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_cd2_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import cd2_api  # noqa: E402


class LocalToRemoteTest(unittest.TestCase):

    def test_maps_download_dir_to_backup_dest(self):
        with mock.patch.object(cd2_api, "backup_tasks", return_value=[
                {"source": "/V1/downloads",
                 "destination": "/115open/Nekogram",
                 "enabled": True}]):
            remote, local = cd2_api.local_to_remote_root()
        self.assertEqual(remote, "/115open/Nekogram")
        self.assertEqual(local, config.DOWNLOAD_DIR)

    def test_no_tasks_returns_none(self):
        with mock.patch.object(cd2_api, "backup_tasks", return_value=[]):
            remote, local = cd2_api.local_to_remote_root()
        self.assertIsNone(remote)

    def test_local_to_remote_relpath(self):
        rp = cd2_api.local_to_remote(
            os.path.join("/V1/downloads", "作者", "视频.mp4"),
            "/115open/Nekogram", "/V1/downloads")
        self.assertEqual(rp, "/115open/Nekogram/作者/视频.mp4")


class _ReconcileBase(unittest.IsolatedAsyncioTestCase):
    """对账分类：已备份 / 缺失 / 大小不符 / 过新跳过。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cd2recon_", dir=_TMP)
        self._root = mock.patch.object(config, "DOWNLOAD_DIR", self.dir)
        self._root.start()
        self.addCleanup(self._root.stop)
        # 本地三个文件：两个旧（≥10min）、一个刚完成（过新）
        self.old1 = self._mkfile("作者A/已备份.mp4", b"x" * 100, old=True)
        self.old2 = self._mkfile("作者A/缺失.jpeg", b"y" * 200, old=True)
        self.old3 = self._mkfile("作者B/截断.zip", b"z" * 50, old=True)
        self.new1 = self._mkfile("作者B/刚完成.mp4", b"n" * 10, old=False)
        # 远端：已备份.mp4 在（大小一致）、截断.zip 大小不符、缺失.jpeg 无
        remote = {
            "/115open/Nekogram/作者A/已备份.mp4":
                {"exists": True, "size": 100},
            "/115open/Nekogram/作者B/截断.zip":
                {"exists": True, "size": 40},   # 远端被截断
        }

        def fake_find(remote_path, force_refresh=False):
            info = remote.get(remote_path)
            if not info or not info["exists"]:
                return None

            class F:
                size = info["size"]
            return F()

        patches = [
            mock.patch.object(cd2_api, "backup_tasks", return_value=[
                {"source": "/V1/downloads",
                 "destination": "/115open/Nekogram",
                 "enabled": True}]),
            mock.patch.object(cd2_api, "iter_local_files",
                              return_value=[self.old1, self.old2,
                                            self.old3, self.new1]),
            mock.patch.object(cd2_api, "find_remote",
                              side_effect=fake_find),
            mock.patch.object(cd2_api, "get_space_info",
                              return_value=(40e12, 12.8e12, 27e12)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _mkfile(self, rel, content, old):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
        if old:
            old_ts = __import__("time").time() - 3600
            os.utime(path, (old_ts, old_ts))
        return path

    async def test_classify_and_report(self):
        r = await cd2_api.reconcile_reply(limit=10)
        self.assertIn("✅已备份 1", r)
        self.assertIn("⚠️远端缺失 1", r)
        self.assertIn("大小不符 1", r)
        self.assertIn("过新跳过 1", r)
        self.assertIn(".jpeg", r)                      # 扩展名诊断
        self.assertIn("115 容量", r)




class ReconcileTest(_ReconcileBase):
    """对账用例（共享 _ReconcileBase 夹具）。"""

    async def test_classify_counts(self):
        """过新的文件跳过不计入 checked；其余按远端状态分类。"""
        with mock.patch.object(cd2_api, "find_remote",
                               side_effect=lambda p, **k: None), \
                mock.patch.object(cd2_api, "get_space_info",
                                  return_value=None):
            r = cd2_api.reconcile(limit=10)
        self.assertEqual(r["checked"], 3)   # 过新的不计入
        self.assertEqual(len(r["missing"]), 3)
        self.assertEqual(r["too_new"], 1)


class TasksAndOfflineTest(unittest.TestCase):
    """上传任务摘要（排序/过滤完成项）与离线下载提交。"""

    def _fake_upload_list(self, files):
        class Resp:
            uploadFiles = files
        return Resp

    def test_summary_filters_finished_and_sorts(self):
        from google.protobuf import empty_pb2

        def fake(status, pct_done, size):
            f = mock.MagicMock()
            f.status = status
            f.size = size
            f.transferedBytes = pct_done
            f.key = f"/src/{status}_{pct_done}.mp4"
            f.destPath = "/115open/Nekogram/x"
            return f

        files = [
            fake("Finish", 100, 100),        # 完成项应被过滤
            fake("Error", 0, 100),           # 失败排最前
            fake("Transfer", 500, 1000),     # 进行中 50%
        ]
        stub = mock.MagicMock()
        stub.GetUploadFileList.return_value = type("R", (), {"uploadFiles": files})()

        with mock.patch.object(cd2_api, "_stub", return_value=(stub, "tk")), \
                mock.patch.object(cd2_api, "_md", return_value=(stub, ()),):
            # _md 返回 (stub, md)——这里 stub 复用即可
            total, out = cd2_api.upload_files_summary()
        self.assertEqual(total, 2)               # Finish 被过滤
        self.assertEqual(out[0]["status"], "Error")
        self.assertEqual(out[1]["pct"], 50)

    def test_offline_download_ok(self):
        from google.protobuf import wrappers_pb2
        op = mock.MagicMock()
        op.success = True
        op.errorMessage = ""
        stub = mock.MagicMock()
        stub.AddOfflineFiles.return_value = op
        with mock.patch.object(cd2_api, "_stub", return_value=(stub, "tk")), \
                mock.patch.object(cd2_api, "_md", return_value=(stub, ())):
            ok, err = cd2_api.add_offline_download("magnet:?xt=1")
        self.assertTrue(ok)
        # toFolder 应指向云下载目录
        req = stub.AddOfflineFiles.call_args[0][0]
        self.assertEqual(req.toFolder, "/115open/云下载")
        self.assertIn("magnet:", req.urls)

    def test_offline_download_error(self):
        op = mock.MagicMock()
        op.success = False
        op.errorMessage = "链接无效"
        stub = mock.MagicMock()
        stub.AddOfflineFiles.return_value = op
        with mock.patch.object(cd2_api, "_stub", return_value=(stub, "tk")), \
                mock.patch.object(cd2_api, "_md", return_value=(stub, ())):
            ok, err = cd2_api.add_offline_download("magnet:?x")
        self.assertFalse(ok)
        self.assertIn("链接无效", err)


if __name__ == "__main__":
    unittest.main()
