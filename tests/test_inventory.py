"""115 全量清单（inventory.py）+ 分类体检（ops_report._collect_classify）
的单元测试。CD2 层全 mock，不联网不碰真实网盘。
运行方式（项目根目录）：
    .venv/bin/python -m unittest tests.test_inventory -v
"""
import asyncio
import atexit
import os
import shutil
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_inv_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config      # noqa: E402
from tg_userbot import cd2_api     # noqa: E402
from tg_userbot import inventory   # noqa: E402
from tg_userbot import ops_report  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402


class _DbTestCase(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="invdb_", dir=_TMP)
        p = mock.patch.object(config, "RUNTIME_DB_FILE",
                              os.path.join(self.dir, "t.db"))
        p.start()
        self.addCleanup(p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.assertTrue(runtime_db.init_db())

    def _fake_scan_map(self, mapping, aborted_tops=()):
        """top → (files, empty_dirs)；指定 top 返回 aborted=True。"""

        def fake_scan(root, progress=None, deadline_s=None):
            top = root.rsplit("/", 1)[-1]
            files, empty = mapping.get(top, ([], []))
            aborted = top in aborted_tops
            stats = {"dirs": 1, "files": len(files), "no_hash": 0,
                     "errors": [], "errors_total": 0, "empty_dirs": empty,
                     "aborted": aborted, "elapsed_s": 0.1}
            return files, stats

        return fake_scan


class InventoryScanTest(_DbTestCase):

    def test_segments_progress_and_done_tops(self):
        mapping = {
            "B站": ([("/115open/B站/a.mp4", 100, "AA", 10),
                     ("/115open/B站/b.jpg", 50, "", 20)], []),
            "画集": ([("/115open/画集/c.zip", 10, "BB", 30)],
                    ["/115open/画集/空壳"]),
        }
        with mock.patch.object(inventory, "list_top_dirs",
                               return_value=["B站", "画集", "temp"]), \
                mock.patch.object(cd2_api, "scan_remote",
                                  side_effect=self._fake_scan_map(
                                      {"B站": mapping["B站"],
                                       "画集": mapping["画集"],
                                       "temp": ([], [])})):
            res = inventory.inventory_scan(5)
        self.assertEqual(sorted(res["done"]), ["B站", "temp", "画集"])
        self.assertEqual(res["files"], 3)
        by = runtime_db.inv_by_top()
        self.assertEqual(by[0]["top"], "B站")     # 按大小降序
        self.assertEqual(runtime_db.inv_empty_dirs(),
                         ["/115open/画集/空壳"])
        # 再跑一段：全部 done → todo 空
        with mock.patch.object(inventory, "list_top_dirs",
                               return_value=["B站", "画集", "temp"]), \
                mock.patch.object(cd2_api, "scan_remote",
                                  side_effect=self._fake_scan_map({})):
            res2 = inventory.inventory_scan(5)
        self.assertEqual(res2["todo"], [])

    def test_aborted_top_not_marked_done_and_rerun_replaces(self):
        mapping = {"B站": ([("/115open/B站/a.mp4", 100, "AA", 10)], [])}
        with mock.patch.object(inventory, "list_top_dirs",
                               return_value=["B站"]), \
                mock.patch.object(cd2_api, "scan_remote",
                                  side_effect=self._fake_scan_map(
                                      mapping, aborted_tops=("B站",))):
            res = inventory.inventory_scan(5)
        # 时限中断：不记完成，但已入库的半截行允许存在（下次整段重扫覆盖）
        self.assertEqual(res["done"], [])
        self.assertEqual(runtime_db.inv_counts()[0], 1)
        # 重扫完整 → done
        with mock.patch.object(inventory, "list_top_dirs",
                               return_value=["B站"]), \
                mock.patch.object(cd2_api, "scan_remote",
                                  side_effect=self._fake_scan_map(
                                      {"B站": ([
                                          ("/115open/B站/a.mp4", 100, "AA", 10),
                                          ("/115open/B站/b.mp4", 90, "CC", 20)],
                                          [])})):
            res2 = inventory.inventory_scan(5)
        self.assertEqual(res2["done"], ["B站"])
        self.assertEqual(runtime_db.inv_counts()[0], 2)   # 旧半截行被替换

    def test_ext_extracted_lowercase(self):
        with mock.patch.object(inventory, "list_top_dirs",
                               return_value=["X"]), \
                mock.patch.object(cd2_api, "scan_remote",
                                  side_effect=self._fake_scan_map(
                                      {"X": ([("/115open/X/v.MP4", 1, "", 1)],
                                             [])})):
            inventory.inventory_scan(5)
        rows = runtime_db.inv_by_top_ext()
        self.assertEqual(rows[0]["ext"], "mp4")


class InvCommandTest(_DbTestCase):

    def test_parse_forms_and_reentry(self):
        with mock.patch.object(asyncio, "create_task") as ct:
            out = asyncio.run(inventory.command_reply("/115inv"))
        ct.assert_not_called()
        self.assertIn("还没有清单", out)

        spawned = {}

        class _FakeTask:
            def __init__(self, coro):
                spawned["minutes"] = coro.cr_frame.f_locals.get("minutes")
                coro.close()

            def add_done_callback(self, cb):
                pass

        try:
            with mock.patch.object(asyncio, "create_task", _FakeTask):
                out = asyncio.run(inventory.command_reply("/115inv scan 30"))
            self.assertIn("已启动", out)
            self.assertEqual(spawned["minutes"], 30)
            self.assertEqual(inventory._INV_RUNNING, {"minutes": 30})
            # 防重入
            with mock.patch.object(asyncio, "create_task") as ct:
                out = asyncio.run(
                    inventory.command_reply("/115inv_scan 5"))
            ct.assert_not_called()
            self.assertIn("已在进行中", out)
        finally:
            inventory._INV_RUNNING = {}

    def test_reset_clears_everything(self):
        runtime_db.inv_set_state("done_tops", '["a"]')
        runtime_db.inv_replace_top("a", [{"path": "/115open/a/x", "ext": "",
                                          "size": 1, "sha1": "",
                                          "write_time": 0}], [])
        out = asyncio.run(inventory.command_reply("/115inv reset"))
        self.assertIn("已清空", out)
        self.assertEqual(runtime_db.inv_counts(), (0, 0, 0))
        self.assertIsNone(runtime_db.inv_get_state("done_tops"))


class ClassifyCollectorTest(_DbTestCase):

    def _seed(self):
        runtime_db.inv_set_state("all_tops", '["B站","temp","视频"]')
        runtime_db.inv_set_state("done_tops", '["B站","temp"]')
        runtime_db.inv_replace_top(
            "B站",
            [{"path": "/115open/B站/a.mp4", "ext": "mp4", "size": 900,
              "sha1": "", "write_time": 1790000000},
             {"path": "/115open/B站/b.jpg", "ext": "jpg", "size": 100,
              "sha1": "", "write_time": 1790000000}], [])
        runtime_db.inv_replace_top(
            "temp",
            [{"path": "/115open/temp/old.mp4", "ext": "mp4", "size": 500,
              "sha1": "", "write_time": 1700000000}],   # 2023 年 → 滞留
            ["/115open/temp/空壳"])

    def test_classify_renders_full_picture(self):
        self._seed()
        text, csv = ops_report._collect_classify()
        self.assertIsNone(csv)
        self.assertIn("清单不完整（2/3", text)          # 视频 未扫
        self.assertIn("B站｜", text)
        self.assertIn("视频 90%", text)                 # 类型构成
        self.assertIn("全盘类型分布", text)
        self.assertIn("收件箱滞留", text)
        self.assertIn("temp：1 个", text)
        self.assertIn("空目录", text)
        self.assertIn("/115open/temp/空壳", text)

    def test_classify_without_inventory(self):
        text, _csv = ops_report._collect_classify()
        self.assertIn("还没有清单", text)

    def test_classify_with_dup_summary(self):
        self._seed()
        runtime_db.dup_start_run("/115open")
        runtime_db.dup_finish_run(
            1, {"total_files": 3, "dup_groups": 2, "dup_files": 4,
                "wasted_bytes": 1024},
            [{"sha1": "AA", "size": 512, "files": [("/a", 1), ("/b", 2)]}])
        text, _csv = ops_report._collect_classify()
        self.assertIn("重复概况", text)
        self.assertIn("2 组", text)


if __name__ == "__main__":
    unittest.main()
