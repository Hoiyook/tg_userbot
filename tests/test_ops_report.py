"""运维报告（ops_report.py）的单元测试。

采集器全部只读、DB 不可用降级空数据；导出链路与 /115dup 共用
（cd2_api.upload_to_115，mock 挂载与 gRPC 层）。不联网。
运行方式（项目根目录）：
    .venv/bin/python -m unittest tests.test_ops_report -v
"""
import asyncio
import atexit
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_ops_report_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config      # noqa: E402
from tg_userbot import cd2_api     # noqa: E402
from tg_userbot import ops_report  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402
from tg_userbot import state       # noqa: E402


class _DbTestCase(unittest.TestCase):
    """全新 DB + 挂载/存档目录全落临时目录。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="opsdb_", dir=_TMP)
        self.path = os.path.join(self.dir, "t.db")
        self.mount_base = os.path.join(self.dir, "mount")
        self.report_dir = os.path.join(self.dir, "reports")
        os.makedirs(self.mount_base)
        patches = [
            mock.patch.object(config, "RUNTIME_DB_FILE", self.path),
            mock.patch.object(config, "CLOUD_MOUNT_BASE", self.mount_base),
            mock.patch.object(config, "RUNTIME_DIR", self.report_dir),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.assertTrue(runtime_db.init_db())

    def _seed_ops_data(self):
        """PENDING 外链 / 监听失败 / 解压缺密码 / Pawchive MANUAL+FAILED。"""
        runtime_db.manual_link_add("https://mega.nz/x1", host="mega")
        runtime_db.manual_link_add("https://mega.nz/done", host="mega")
        runtime_db._write(lambda c: runtime_db._execute(
            c, "UPDATE manual_links SET status='DONE' WHERE url_key=?",
            ("https://mega.nz/done",)), "完成一条")
        runtime_db.enqueue_listener_tasks(
            -1001, [{"message_id": 11, "target_type": "saved_messages",
                     "target_chat_id": None, "download": True}])
        runtime_db._write(lambda c: runtime_db._execute(
            c, "UPDATE listener_tasks SET status='FAILED', last_error=? "
               "WHERE message_id=?", ("源消息不可读（可能已删除）", 11)),
            "造失败")
        runtime_db._write(lambda c: runtime_db._execute(
            c, "INSERT INTO extract_tasks(remote_dir, archive_name, "
               "archive_size, status, error, created_at, updated_at) "
               "VALUES('/115open/画集', 'a.zip', 100, 'PASSWORD', '需密码', "
               "1, 1)"), "造解压")
        created, _skipped = runtime_db.enqueue_pawchive_posts(
            "pawchive", "c1", "作者甲",
            [{"post_id": "p1", "title": "待人工帖", "published": "2026-10-01",
              "post_url": "https://pawchive.pw/p1", "subdir": "作者甲",
              "files": [], "ext_links": [{"domain": "mega",
                                          "url": "https://mega.nz/p1"}]},
             {"post_id": "p2", "title": "失败帖", "published": "2026-10-02",
              "post_url": "https://pawchive.pw/p2", "subdir": "作者甲",
              "files": [], "ext_links": []}])
        assert created == 2
        runtime_db._write(lambda c: runtime_db._execute(
            c, "UPDATE pawchive_posts SET status=?, ext_links=? "
               "WHERE post_id='p1'",
            (runtime_db.PAW_POST_MANUAL,
             '[{"domain":"mega","url":"https://mega.nz/p1"}]')), "置 MANUAL")
        runtime_db._write(lambda c: runtime_db._execute(
            c, "UPDATE pawchive_posts SET status=?, last_error='Chrome 挂了' "
               "WHERE post_id='p2'", (runtime_db.PAW_POST_FAILED,)), "置 FAILED")


class CollectorTest(_DbTestCase):

    def setUp(self):
        super().setUp()
        self._seed_ops_data()

    def test_manual_collects_all_attention_sources(self):
        text, csv = ops_report._collect_manual()
        self.assertIsNone(csv)
        self.assertIn("Pawchive 待人工", text)
        self.assertIn("待人工帖", text)
        self.assertIn("https://mega.nz/p1", text)   # 外链清单
        self.assertIn("https://pawchive.pw/p1", text)
        self.assertIn("手动外链台账 PENDING", text)
        self.assertIn("https://mega.nz/x1", text)
        self.assertNotIn("mega.nz/done", text)      # DONE 不进待人工
        self.assertIn("缺密码", text)
        self.assertIn("a.zip", text)
        self.assertIn("监听失败", text)
        self.assertIn("源消息不可读", text)

    def test_failed_and_retry_render_with_origin_link(self):
        retry = [{"id": "t1", "final_name": "25-10-29 视频.mp4",
                  "attempts": 3, "next_retry_at": 1792800000,
                  "error": "TimeoutError: 超时",
                  "origin_chat_id": -1001719225045, "origin_msg_id": 90901}]
        with mock.patch.object(state, "QUEUE",
                               {"tasks": [], "retry": retry}):
            rows = ops_report._retry_rows()
            text, csv_text = ops_report._collect_retry()
            failed_text, failed_csv = ops_report._collect_failed()
        self.assertEqual(rows[0]["attempts"], 3)
        self.assertIn("已尝试 3 次", text)
        self.assertIn("https://t.me/c/1719225045/90901", text)  # 原帖跳转
        self.assertIn("no,name,attempts", csv_text)
        self.assertIn("Chrome 挂了", failed_text)   # Pawchive 失败
        self.assertIn("25-10-29 视频.mp4", failed_csv)

    def test_queue_snapshot(self):
        active = {1: {"label": "[T=x]", "filename": "大文件.mp4",
                      "percent": 42, "downloaded": 1, "total": 2, "link": ""}}
        with mock.patch.object(state, "ACTIVE_DOWNLOADS", active), \
                mock.patch.object(cd2_api, "get_space_info",
                                  return_value=(10 ** 12, 4 * 10 ** 11,
                                                6 * 10 ** 11)):
            text, _csv = ops_report._collect_queue()
        self.assertIn("在途下载", text)
        self.assertIn("42%", text)
        self.assertIn("大文件.mp4", text)
        self.assertIn("115 容量", text)
        self.assertIn("115 解压", text)

    def test_stats_collector(self):
        with mock.patch("tg_userbot.stats.stats_text",
                        return_value="📊 台账（示例）"):
            text, _csv = ops_report._collect_stats(3)
        self.assertEqual(text, "📊 台账（示例）")


class ExportAndCommandTest(_DbTestCase):

    def setUp(self):
        super().setUp()
        self._seed_ops_data()

    def _upload_patches(self):
        def fake_find(remote, force_refresh=False):
            # 远端路径尾两段 = 日期目录/文件名，镜像本地 userbot_reports 结构
            day, name = remote.rstrip("/").rsplit("/", 2)[-2:]
            lp = os.path.join(self.report_dir, "userbot_reports", day, name)
            return NS(size=os.path.getsize(lp)) if os.path.exists(lp) else None

        return [
            mock.patch.object(cd2_api, "find_remote", side_effect=fake_find),
            mock.patch.object(cd2_api, "upload_files_summary",
                              return_value=(0, [])),
        ]

    def test_export_all_types_writes_local_and_mount(self):
        with mock.patch.object(state, "QUEUE", {"tasks": [], "retry": []}), \
                mock.patch("tg_userbot.stats.stats_text",
                           return_value="📊 台账"):
            for p in self._upload_patches():
                p.start()
                self.addCleanup(p.stop)
            res = ops_report.export_reports(list(ops_report.REPORT_TYPES))
        self.assertTrue(res["ok"], res["note"])
        # 6 TXT + 2 CSV（failed/retry 各附；classify 需清单，空也出提示文件）
        self.assertEqual(len(res["files"]), 8)
        import time as _t
        day = _t.strftime("%Y-%m-%d")
        # 115 侧与本地侧都按日期分目录；内容带 BOM（Excel 双击不乱码）
        mount_out = os.listdir(
            os.path.join(self.mount_base, "userBot报告", day))
        self.assertEqual(len(mount_out), 8)
        self.assertTrue(any(f.startswith("manual_") for f in mount_out))
        local_txt = next(f for f in res["files"] if f.endswith(".txt"))
        with open(local_txt, "rb") as f:
            self.assertTrue(f.read(3) == b"\xef\xbb\xbf",
                            "TXT 须带 UTF-8 BOM")
        self.assertTrue(local_dir_dated := res["local_dir"].endswith(day),
                        res["local_dir"])

    def test_report_command_reply_ok_and_usage(self):
        self.assertTrue(ops_report.is_report_command("/report"))
        self.assertTrue(ops_report.is_report_command("/report manual"))
        self.assertFalse(ops_report.is_report_command("/reportx"))
        out = asyncio.run(ops_report.command_reply("/report 什么"))
        self.assertIn("/report", out)          # 非法类型 → USAGE
        with mock.patch.object(state, "QUEUE", {"tasks": [], "retry": []}), \
                mock.patch("tg_userbot.stats.stats_text",
                           return_value="📊 台账"):
            for p in self._upload_patches():
                p.start()
                self.addCleanup(p.stop)
            out = asyncio.run(ops_report.command_reply("/report retry"))
        self.assertIn("已传 115", out)
        self.assertIn("userBot报告", out)
        self.assertIn("retry_", out)

    def test_collectors_survive_db_unavailable(self):
        runtime_db.close_db()   # DB 掉线：采集降级空数据，报告照出
        for t in ops_report.REPORT_TYPES:
            text, _csv = ops_report._COLLECTORS[t]()
            self.assertTrue(text, f"{t} 采集不得返回空文本")


if __name__ == "__main__":
    unittest.main()
