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
from tg_userbot import chrome_agent  # noqa: E402
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


class ChromeCollectorTest(unittest.TestCase):
    """_collect_chrome：每链接一行成败台账（2026-10-31 owner 定档）。"""

    def test_chrome_ledger_counts_and_sections(self):
        tasks = [
            {"task_id": "a" * 32, "url": "https://x.com/1", "status": "SUCCESS",
             "attempts": 2, "filename": "a.mp4", "error": "",
             "created_at": "2026-10-31 09:00:00",
             "finished_at": "2026-10-31 09:05:00"},
            {"task_id": "b" * 32, "url": "https://x.com/2", "status": "FAILED",
             "attempts": 10, "filename": "", "error": "下载超时（超时内无终态事件）",
             "created_at": "2026-10-31 08:00:00",
             "finished_at": "2026-10-31 09:40:00",
             "updated_at": "2026-10-31 09:40:00"},
            {"task_id": "c" * 32, "url": "https://x.com/3", "status": "RETRY_WAIT",
             "attempts": 6, "filename": "", "error": "CDP 连接已断开",
             "created_at": "2026-10-31 10:00:00",
             "next_retry_at": "2026-10-31 10:02:00"},
        ]
        with mock.patch.object(chrome_agent, "load_tasks", return_value=tasks):
            text, csv_text = ops_report._collect_chrome()
        self.assertIn("共 3 条", text)
        self.assertIn("✅成功 1", text)
        self.assertIn("❌失败 1", text)
        self.assertIn("【❌ 失败明细】", text)
        self.assertIn("第10次耗尽", text)
        self.assertIn("下载超时", text)
        self.assertIn("【✅ 最近成功（前 30 条）】", text)
        self.assertIn("a.mp4", text)
        self.assertIn("【⏳ 在途/排队/等重试】", text)
        self.assertIn("下次 2026-10-31 10:02", text)
        # CSV 每行一个链接（表头 + 3）
        self.assertEqual(len(csv_text.strip().splitlines()), 4)
        self.assertIn("https://x.com/1", csv_text)


class PruneOldReportsTest(unittest.TestCase):
    """同类报告只留最新（2026-10-31 owner 定档）：跨日期全局比新旧、
    删除走回收站、上传成功才调用、无时间戳的报告永不清理。"""

    def test_prune_keeps_latest_per_type(self):
        listing = {
            "/115open/userBot报告": [
                ("2026-10-30", 0, True), ("2026-10-31", 0, True),
                ("说明.txt", 10, False)],
            "/115open/userBot报告/2026-10-30": [
                ("chrome_20261030-1200.txt", 100, False),
                ("failed_20261030-1200.txt", 100, False)],
            "/115open/userBot报告/2026-10-31": [
                ("chrome_20261031-0800.txt", 100, False),
                ("chrome_20261031-2200.csv", 200, False),
                ("chrome_20261031-2200.txt", 100, False),
                ("failed_20261031-2200.txt", 100, False)],
        }
        deleted = []

        def fake_list(path, limit=200):
            return listing.get(path)

        def fake_delete(paths):
            deleted.extend(paths)
            return [(p, True, "") for p in paths]

        with mock.patch.object(cd2_api, "list_remote_dir",
                               side_effect=fake_list), \
             mock.patch.object(cd2_api, "delete_remotes",
                               side_effect=fake_delete):
            n, kept = ops_report.prune_old_reports()
        # chrome 3 个单元留最新 + failed 2 个单元留最新 → 共删 3 个旧文件
        # （chrome 10-30 txt、chrome 10-31 0800 txt、failed 10-30 txt）；
        # 最新的 2200 单元 txt+csv 成对保留
        self.assertEqual(n, 3)
        self.assertNotIn("/115open/userBot报告/2026-10-31/"
                         "chrome_20261031-2200.csv", deleted)
        self.assertIn("/115open/userBot报告/2026-10-30/chrome_20261030-1200.txt",
                      deleted)
        self.assertIn("/115open/userBot报告/2026-10-31/"
                      "chrome_20261031-0800.txt", deleted)
        self.assertNotIn("/115open/userBot报告/2026-10-31/"
                         "chrome_20261031-2200.txt", deleted)
        # failed 两份留最新
        self.assertIn("/115open/userBot报告/2026-10-30/"
                      "failed_20261030-1200.txt", deleted)
        self.assertEqual(kept.get("chrome"), 1)
        self.assertEqual(kept.get("failed"), 1)

    def test_untimestamped_reports_never_deleted(self):
        listing = {
            "/115open/userBot报告": [("说明.txt", 10, False),
                                     ("说明 (1).txt", 10, False)],
        }
        with mock.patch.object(cd2_api, "list_remote_dir",
                               return_value=listing["/115open/userBot报告"]), \
             mock.patch.object(cd2_api, "delete_remotes") as dele:
            n, _ = ops_report.prune_old_reports()
        self.assertEqual(n, 0)
        dele.assert_not_called()


    def test_fresh_units_never_deleted(self):
        """宽限窗（30 分钟）：刚上传的报告即使排 keep 之后也绝不删——
        2026-10-31 实查：最新 csv 消失，疑为列表可见性竞态。"""
        import time as _t
        now_stamp = _t.strftime("%Y%m%d-%H%M")
        prev = _t.strftime("%Y%m%d-%H%M", _t.localtime(_t.time() - 3600))
        listing = [f"chrome_{prev}.txt", f"chrome_{prev}.csv",
                   f"chrome_{now_stamp}.txt"]
        deleted = []
        with mock.patch.object(cd2_api, "list_remote_dir",
                               return_value=[(n, 10, False)
                                             for n in listing]), \
             mock.patch.object(cd2_api, "delete_remotes",
                               side_effect=lambda ps: deleted.extend(ps)
                               or [(p, True, "") for p in ps]):
            n, _ = ops_report.prune_old_reports()
        # now_stamp 单元在宽限窗内：即使它是 keep 之后第一个也不删；
        # prev 单元（1 小时前，超出宽限窗）整单元删除（txt+csv 成对）
        self.assertEqual(sorted(deleted),
                         sorted([f"/115open/userBot报告/chrome_{prev}.txt",
                                 f"/115open/userBot报告/chrome_{prev}.csv"]))
        self.assertNotIn(f"/115open/userBot报告/chrome_{now_stamp}.txt",
                         deleted)


class ChromeMergeTest(unittest.TestCase):
    """P0（2026-11-01）：chrome 台账增量合并——被 200 条窗口裁掉的行
    由上一份 CSV 保住，不再随快照去旧而丢。"""

    def test_merge_preserves_trimmed_rows(self):
        import csv as _csv
        prev_rows = [
            {"任务ID": "old-trimmed", "状态": "SUCCESS", "尝试次数": "1",
             "完成时间": "2026-10-30 08:00:00", "文件名": "旧文件.mp4",
             "链接": "https://x.com/old", "错误": "",
             "更新时间": "2026-10-30 08:00:00"},
            {"任务ID": "t1", "状态": "RETRY_WAIT", "尝试次数": "2",
             "完成时间": "", "文件名": "", "链接": "https://x.com/1",
             "错误": "断连", "更新时间": "2026-10-30 09:00:00"},
        ]
        tasks = [
            {"task_id": "t1", "status": "SUCCESS", "attempts": 3,
             "finished_at": "2026-10-31 10:00:00", "filename": "新文件.mp4",
             "url": "https://x.com/1", "error": "",
             "updated_at": "2026-10-31 10:00:00"},
            {"task_id": "t2", "status": "FAILED", "attempts": 10,
             "finished_at": "2026-10-31 10:30:00", "filename": "",
             "url": "https://x.com/2", "error": "超时",
             "updated_at": "2026-10-31 10:30:00"},
        ]
        base = tempfile.mkdtemp(dir=_TMP)
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        day_dir = os.path.join(base, "userbot_reports", "2026-10-31")
        os.makedirs(day_dir)
        prev_path = os.path.join(day_dir, "chrome_20261031-0900.csv")
        with open(prev_path, "w", encoding="utf-8-sig", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=list(prev_rows[0]))
            w.writeheader()
            w.writerows(prev_rows)

        rt = mock.patch.object(config, "RUNTIME_DIR", base)
        lp = mock.patch.object(ops_report, "_latest_local_chrome_csv",
                               return_value=prev_path)
        with rt, lp:
            rows = ops_report._merged_chrome_rows(tasks)
        ids = [r.get("任务ID") or r.get("task_id") for r in rows]
        self.assertIn("old-trimmed", ids, "被窗口裁掉的行必须保住")
        # 同 id：当前台账（活状态）覆盖旧快照
        t1 = next(r for r in rows
                  if (r.get("任务ID") or r.get("task_id")) == "t1")
        self.assertEqual(t1.get("status"), "SUCCESS")
        self.assertEqual(t1.get("filename"), "新文件.mp4")
        self.assertEqual(len(rows), 3)

    def test_prune_keep_by_type(self):
        listing = [f"系统备份_2026100{i}-1200.zip" for i in range(1, 9)]
        deleted = []
        with mock.patch.object(cd2_api, "list_remote_dir",
                               return_value=[(n, 10, False)
                                             for n in listing]), \
             mock.patch.object(cd2_api, "delete_remotes",
                               side_effect=lambda ps: deleted.extend(ps)
                               or [(p, True, "") for p in ps]):
            ops_report.prune_old_reports(keep_by_type={"系统备份": 7})
        self.assertEqual(len(deleted), 1)   # 8 份保 7，删最旧 1 份


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
        # 8 TXT + 3 CSV（failed/retry 附 CSV；chrome 2026-10-31 起入全套
        # 并附 CSV；classify 空也出提示文件）
        self.assertEqual(len(res["files"]), 11)
        import time as _t
        day = _t.strftime("%Y-%m-%d")
        # 115 侧与本地侧都按日期分目录；内容带 BOM（Excel 双击不乱码）
        mount_out = os.listdir(
            os.path.join(self.mount_base, "userBot报告", day))
        self.assertEqual(len(mount_out), 11)
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


class DailyFailedPushTest(unittest.TestCase):
    """每日失败报告自动推送（2026-10-29 定档）：有新增失败才推；
    推送落 userBot报告/<今日>/；状态持久化防重推。"""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="failedpush_", dir=_TMP)
        self.rt = os.path.join(self.dir, "runtime")
        os.makedirs(self.rt, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for p in (mock.patch.object(config, "RUNTIME_DIR", self.rt),):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_fingerprint_counts_safely(self):
        # 空环境：retry 榜空、DB 不可用 → 指纹应为有限整数（Chrome 文件缺失=0）
        fp = ops_report.failed_fingerprint()
        self.assertIsInstance(fp, int)
        self.assertEqual(fp, 0)

    def test_push_once_per_fingerprint(self):
        import asyncio
        calls = []

        def fake_export(types, target_dir=None):
            calls.append(types)
            return {"ok": True, "files": [], "remote_dir":
                    "/115open/userBot报告/x", "local_dir": self.rt,
                    "note": ""}

        async def fake_notify(text):
            pass

        # 指纹钉 5（环境真实值 0 会走静默分支）——本用例测「同指纹不重推」
        with mock.patch.object(ops_report, "failed_fingerprint",
                               return_value=5), \
             mock.patch.object(ops_report, "export_reports",
                               side_effect=fake_export), \
             mock.patch("tg_userbot.notify.notify_user",
                        side_effect=fake_notify):
            r1 = asyncio.run(ops_report.daily_failed_push())
            self.assertIn("已推 115", r1)
            r2 = asyncio.run(ops_report.daily_failed_push())
            self.assertIn("无新增", r2)      # 同日同指纹不重推
            self.assertEqual(len(calls), 1)
            # 失败量变化 → 再推（环境真实指纹为 0，把已推指纹改成 -1
            # 模拟「指纹变化」）
            st = ops_report._load_push_state()
            st["fingerprint"] = -1
            ops_report._save_push_state(st)
            r3 = asyncio.run(ops_report.daily_failed_push())
            self.assertIn("已推 115", r3)
        self.assertEqual(len(calls), 2)

    def test_force_bypasses_fingerprint(self):
        import asyncio
        calls = []

        def fake_export(types, target_dir=None):
            calls.append(1)
            return {"ok": True, "files": [], "remote_dir": "/x",
                    "local_dir": self.rt, "note": ""}

        async def fake_notify(text):
            pass

        with mock.patch.object(ops_report, "failed_fingerprint",
                               return_value=5), \
             mock.patch.object(ops_report, "export_reports",
                               side_effect=fake_export), \
             mock.patch("tg_userbot.notify.notify_user",
                        side_effect=fake_notify):
            asyncio.run(ops_report.daily_failed_push())
            r = asyncio.run(ops_report.daily_failed_push(force=True))
        self.assertEqual(len(calls), 2)
        self.assertIn("已推 115", r)

    def test_export_failure_reports_local_dir(self):
        import asyncio

        def bad_export(types, target_dir=None):
            return {"ok": False, "files": [], "remote_dir": "/x",
                    "local_dir": "/local", "note": "挂载不可用"}

        async def fake_notify(text):
            pass

        with mock.patch.object(ops_report, "failed_fingerprint",
                               return_value=7), \
             mock.patch.object(ops_report, "export_reports",
                               side_effect=bad_export), \
             mock.patch("tg_userbot.notify.notify_user",
                        side_effect=fake_notify):
            r = asyncio.run(ops_report.daily_failed_push())
        self.assertIn("未完成", r)
        self.assertIn("本地留档", r)
