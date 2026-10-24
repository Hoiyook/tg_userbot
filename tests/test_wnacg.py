"""wnacg 模块测试：地址解析 / 下载页解析 / DB 生命周期 / 命令 / worker。

跑法：
    .venv/bin/python -m unittest tests.test_wnacg -v
"""
import atexit
import os
import shutil
import tempfile
import unittest
from unittest import mock

import sys  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest as _btn_shim  # noqa: F401,E402

_TMP = tempfile.mkdtemp(prefix="tg_userbot_wnacg_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ.setdefault("TG_SAVE_FOLDER", _TMP)

from tg_userbot import config  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402
from tg_userbot import wnacg  # noqa: E402


PAGE = """<html><body>
<h1 class="text-xl font-bold">目白阿尔丹去按摩(真按摩)[卖瓜老板个人翻译].zip</h1>
<a href="//dl1.wn01.download/down/3984/8fbe3bea4181951625e6aab6e00d9884.zip?n=xxx">點擊下載</a>
</body></html>"""


class ParseTest(unittest.TestCase):
    def test_ref_forms(self):
        self.assertEqual(
            wnacg.parse_wnacg_ref(
                "https://www.wnacg.com/photos-index-aid-398337.html"), 398337)
        self.assertEqual(
            wnacg.parse_wnacg_ref(
                "https://www.wnacg.com/download-index-aid-42.html"), 42)
        self.assertEqual(wnacg.parse_wnacg_ref("398337"), 398337)
        self.assertIsNone(wnacg.parse_wnacg_ref("https://example.com/x"))
        self.assertIsNone(wnacg.parse_wnacg_ref(""))
        self.assertIsNone(wnacg.parse_wnacg_ref("wnacg 首页"))

    def test_download_page_parse(self):
        title, url = wnacg.parse_download_page(PAGE)
        self.assertEqual(title, "目白阿尔丹去按摩(真按摩)[卖瓜老板个人翻译]")
        self.assertEqual(
            url, "https://dl1.wn01.download/down/3984/"
                 "8fbe3bea4181951625e6aab6e00d9884.zip")

    def test_title_fallback_from_title_tag(self):
        title, url = wnacg.parse_download_page(
            "<title>某本子.zip 下載 - 紳士漫畫</title>"
            '<a href="//dl1.x/down/1/2.zip">d</a>')
        self.assertEqual(title, "某本子")

    def test_download_page_garbage(self):
        self.assertEqual(wnacg.parse_download_page("<html>404</html>"),
                         (None, None))
        self.assertEqual(wnacg.parse_download_page(None), (None, None))


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wnacg_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())


class DbLifecycleTest(_DbCase):
    def test_enqueue_claim_complete(self):
        self.assertTrue(runtime_db.enqueue_wnacg_task(100, "标题A"))
        self.assertFalse(runtime_db.enqueue_wnacg_task(100, "重复"),
                         "aid 主键判重")
        t = runtime_db.claim_next_wnacg_task()
        self.assertEqual(t["aid"], 100)
        self.assertEqual(t["status"], runtime_db.WNACG_PROCESSING)
        self.assertIsNone(runtime_db.claim_next_wnacg_task(),
                          "没有第二条可领")
        runtime_db.complete_wnacg_task(100, size_bytes=12345)
        done = runtime_db.get_wnacg_task(100)
        self.assertEqual(done["status"], runtime_db.WNACG_DONE)
        self.assertEqual(done["size_bytes"], 12345)

    def test_fail_backoff_and_cap_then_retry(self):
        runtime_db.enqueue_wnacg_task(200)
        with mock.patch.object(config, "WNACG_MAX_ATTEMPTS", 2):
            runtime_db.claim_next_wnacg_task()
            st = runtime_db.fail_wnacg_task(200, "HTTP 500", max_attempts=2)
            self.assertEqual(st, runtime_db.WNACG_PENDING)   # 1 < 2 退避
            t = runtime_db.claim_next_wnacg_task(now=10 ** 12)  # 过了退避
            self.assertEqual(t["aid"], 200)
            st = runtime_db.fail_wnacg_task(200, "又挂", max_attempts=2)
            self.assertEqual(st, runtime_db.WNACG_FAILED)    # 到上限
        self.assertEqual(runtime_db.retry_wnacg_task(200), 1)
        self.assertEqual(runtime_db.get_wnacg_task(200)["status"],
                         runtime_db.WNACG_PENDING)

    def test_lease_recovery(self):
        runtime_db.enqueue_wnacg_task(300)
        runtime_db.claim_next_wnacg_task(now=1000, lease_seconds=60)
        self.assertEqual(
            runtime_db.recover_expired_wnacg_tasks(now=2000), 1)
        self.assertEqual(runtime_db.get_wnacg_task(300)["status"],
                         runtime_db.WNACG_PENDING)


class CommandTest(_DbCase):
    def test_enqueue_by_url(self):
        text = wnacg.command_reply(
            "/wnacg https://www.wnacg.com/photos-index-aid-398337.html")
        self.assertIn("已入队 #398337", text)
        # 重复发：回已有
        again = wnacg.command_reply(
            "/wnacg https://www.wnacg.com/photos-index-aid-398337.html")
        self.assertIn("已在队列", again)

    def test_status_and_retry_usage(self):
        runtime_db.enqueue_wnacg_task(7, "标题")
        text = wnacg.command_reply("/wnacg")
        self.assertIn("PENDING=1", text)
        self.assertIn("#7", text)
        self.assertIn("用法", wnacg.command_reply("/wnacg retry"))

    def test_bad_input(self):
        self.assertIn("认不出", wnacg.command_reply("/wnacg https://x.com/y"))


class WorkerFlowTest(_DbCase):
    """worker 全流程 mock：解析 → 下载 → 落 115 → 解压入队。"""

    def setUp(self):
        super().setUp()
        self.staging = tempfile.mkdtemp(prefix="wnacg_st_", dir=_TMP)
        self._st = mock.patch.object(config, "RUNTIME_DIR", self.staging)
        self._st.start()
        self.addCleanup(self._st.stop)
        self._ntf = mock.patch("tg_userbot.notify.notify_user",
                               new=mock.AsyncMock())
        self._ntf.start()
        self.addCleanup(self._ntf.stop)

    def _patch_net(self, size=100):
        local_zip = os.path.join(self.staging, "x.zip")
        with open(local_zip, "wb") as f:
            f.write(b"z" * size)
        info = {"title": "T", "zip_url": "https://dl/x.zip",
                "filename": "x.zip"}
        remote = {"x.zip": size}
        copy_calls = []

        def fake_download(url, dest, expected=None):
            shutil.copyfile(local_zip, dest)
            return size

        def fake_copy(src, dst):
            copy_calls.append(dst)   # 模拟落挂载

        def fake_list(path=None, limit=500):
            if copy_calls:
                return [(n, s, False) for n, s in remote.items()]
            return []

        def fake_mount(remote_dir):
            return "/tmp/fake_mount"
        p1 = mock.patch.object(wnacg, "fetch_album_info_blocking",
                               return_value=info)
        p2 = mock.patch.object(wnacg, "download_zip_blocking",
                               side_effect=fake_download)
        p3 = mock.patch.object(wnacg, "_copy_with_progress",
                               side_effect=fake_copy)
        p4 = mock.patch.object(wnacg, "_remote_size_blocking",
                               side_effect=lambda name: remote.get(name)
                               if copy_calls else None)
        p5 = mock.patch.object(wnacg, "_mount_dir",
                               side_effect=fake_mount)
        for p in (p1, p2, p3, p4, p5):
            p.start()
            self.addCleanup(p.stop)

    async def test_full_flow(self):
        self._patch_net(size=2048)
        runtime_db.enqueue_wnacg_task(500)
        task = runtime_db.claim_next_wnacg_task()
        await wnacg._run_one(task)
        row = runtime_db.get_wnacg_task(500)
        self.assertEqual(row["status"], runtime_db.WNACG_DONE)
        self.assertEqual(row["size_bytes"], 2048)
        # 解压任务已入队（进 /115x 管线）
        extracts = runtime_db.list_extract_tasks(limit=5)
        self.assertTrue(any(r["archive_name"] == "x.zip"
                            for r in extracts))

    async def test_fail_marks_retryable(self):
        with mock.patch.object(
                wnacg, "fetch_album_info_blocking",
                side_effect=RuntimeError("下载页请求失败：x")):
            runtime_db.enqueue_wnacg_task(600)
            task = runtime_db.claim_next_wnacg_task()
            await wnacg._run_one(task)
        row = runtime_db.get_wnacg_task(600)
        self.assertEqual(row["status"], runtime_db.WNACG_PENDING)
        self.assertIn("下载页请求失败", row["error"])


if __name__ == "__main__":
    unittest.main()
