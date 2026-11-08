"""离线种子终态探测（torrent_offline.offline_status，P2 2026-11-01）的
单元测试。gRPC/网络全 mock 不联网。
运行方式（项目根目录）：
    .venv/bin/python -m unittest tests.test_torrent_offline -v
"""
import atexit
import os
import shutil
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_torrent_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

import conftest as _btn_shim  # noqa: E402,F401  按钮兼容垫片
from tg_userbot import cd2_api  # noqa: E402
from tg_userbot import torrent_offline  # noqa: E402


def _fake_stub(pages):
    """ListAllOfflineFiles 替身：pages = {page: [ (infoHash, status, pct) ]}"""
    stub = mock.Mock()

    class _R:
        def __init__(self, items):
            self.offlineFiles = [
                mock.Mock(infoHash=ih, status=st, percendDone=pct)
                for ih, st, pct in items
            ]

    def list_all(req, metadata=None, timeout=None):
        return _R(pages.get(req.page, []))

    stub.ListAllOfflineFiles.side_effect = list_all
    return stub


class OfflineStatusTest(unittest.TestCase):

    def setUp(self):
        # find_remote 返回带账号身份的 id（{cloud-account} 序号）
        f = mock.Mock(id="{115open-103308103}0")
        self._fr = mock.patch.object(cd2_api,
                                     "find_remote", return_value=f)
        self._fr.start()
        self.addCleanup(self._fr.stop)
        self._md = mock.patch.object(
            cd2_api, "_md",
            return_value=(mock.Mock(), (("authorization", "Bearer x"),)))
        self._md.start()
        self.addCleanup(self._md.stop)

    def test_found_running(self):
        stub = _fake_stub({1: [("abc" * 8, 3, 55.0)]})
        with mock.patch.object(cd2_api, "_md",
                               return_value=(stub, None)):
            st = torrent_offline.offline_status("ABC" * 8)
        self.assertEqual(st, ("下载中", 55.0))   # infoHash 大小写不敏感

    def test_not_in_queue_means_rejected(self):
        stub = _fake_stub({1: [("zzz" * 8, 2, 100.0)]})
        with mock.patch.object(cd2_api, "_md",
                               return_value=(stub, None)):
            st = torrent_offline.offline_status("abc" * 8)
        self.assertIsNone(st, "不在队列 = 提交被拒/未入列的关键信号")

    def test_searches_second_page(self):
        stub = _fake_stub({1: [("x" * 40, 2, 100.0)] * 30,
                           2: [("abc" * 8, 1, 0.0)]})
        with mock.patch.object(cd2_api, "_md",
                               return_value=(stub, None)):
            st = torrent_offline.offline_status("abc" * 8)
        self.assertEqual(st, ("排队中", 0.0))

    def test_unresolvable_cloud_returns_none(self):
        with mock.patch.object(cd2_api, "find_remote",
                               return_value=mock.Mock(id="weird")):
            self.assertIsNone(torrent_offline.offline_status("abc" * 8))


if __name__ == "__main__":
    unittest.main()
