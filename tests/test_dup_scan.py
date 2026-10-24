"""115 全盘查重（dup_scan.py + cd2_api.scan_remote/delete_remotes）的单元测试。

查重链路＝CD2 gRPC 递归列举（mock stub，不联网不碰真实 CD2）→ SHA1
分组（纯函数）→ runtime_db v14 落库 → 命令文本。重点契约：

1. SHA1 相同 = 一组；空文件（size=0）与无哈希文件绝不进组，只计数。
2. 组序 = 可释放空间降序（组号即此序，list 页序 / del 组号建立在它上）。
3. replace-per-scan：新 run 落库即清旧 run，del 组号永远对着最近一次扫描。
4. 删除纪律：默认 dry-run；keep=oldest 保留最早写入（wtime=0 视为最早，
   宁可多留）；确认后走回收站删除并打删标。
5. 扫描容错：单目录失败记 error 继续走；deadline 软超时宁可半份结果。

运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import asyncio
import atexit
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_dup_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config   # noqa: E402
from tg_userbot import cd2_api  # noqa: E402
from tg_userbot import dup_scan  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402


# ============================================================
# cd2_api.scan_remote —— 递归列举 + SHA1 提取 + 容错
# ============================================================
def _file(name, path, size=10, sha1="AA", wtime=0):
    return NS(name=name, fullPathName=path, size=size, isDirectory=False,
              fileHashes={cd2_api.HASH_KEY_SHA1: sha1} if sha1 else {},
              writeTime=NS(seconds=wtime, nanos=0))


def _dir(name, path):
    return NS(name=name, fullPathName=path, size=0, isDirectory=True,
              fileHashes={}, writeTime=NS(seconds=0, nanos=0))


class _FakeStub:
    """path → 可迭代的 reply（reply.subFiles 为条目列表）；可注入失败。"""

    def __init__(self, tree, fail_paths=()):
        self.tree = tree
        self.fail_paths = set(fail_paths)
        self.calls = []

    def GetSubFiles(self, request, metadata=None, timeout=None):
        self.calls.append(request.path)
        if request.path in self.fail_paths:
            raise RuntimeError(" rpc err ")
        return iter([NS(subFiles=self.tree.get(request.path, []))])

    def DeleteFile(self, request, metadata=None, timeout=None):
        if "bad" in request.path:
            return NS(success=False, errorMessage="nope")
        return NS(success=True, errorMessage="")


def _with_stub(stub):
    return mock.patch.object(cd2_api, "_md", return_value=(stub, ()),)


class ScanRemoteTest(unittest.TestCase):

    def test_recursive_walk_collects_files_with_sha1(self):
        stub = _FakeStub({
            "/r": [_dir("d1", "/r/d1"), _file("a.mp4", "/r/a.mp4", 100, "AB")],
            "/r/d1": [_file("b.mp4", "/r/d1/b.mp4", 50, "CD", 1700000000)],
        })
        with _with_stub(stub):
            files, stats = cd2_api.scan_remote("/r")
        self.assertEqual(
            files,
            [("/r/a.mp4", 100, "AB", 0), ("/r/d1/b.mp4", 50, "CD", 1700000000)])
        self.assertEqual(stats["dirs"], 2)
        self.assertEqual(stats["files"], 2)
        self.assertFalse(stats["aborted"])

    def test_sha1_uppercased_and_missing_counted(self):
        stub = _FakeStub({
            "/r": [_file("a", "/r/a", 1, sha1="ab"),
                   _file("b", "/r/b", 2, sha1=None)],
        })
        with _with_stub(stub):
            files, stats = cd2_api.scan_remote("/r")
        self.assertEqual(files[0][2], "AB")
        self.assertEqual(files[1][2], "")
        self.assertEqual(stats["no_hash"], 1)

    def test_failed_dir_recorded_and_walk_continues(self):
        stub = _FakeStub({
            "/r": [_dir("bad", "/r/bad"), _dir("ok", "/r/ok")],
            "/r/bad": [],   # fail_paths 命中抛错
            "/r/ok": [_file("x", "/r/ok/x", 1, "AA")],
        }, fail_paths={"/r/bad"})
        with _with_stub(stub):
            files, stats = cd2_api.scan_remote("/r")
        self.assertEqual(len(files), 1)
        self.assertEqual(stats["dirs"], 3)
        self.assertEqual(stats["errors"][0][0], "/r/bad")
        self.assertEqual(stats["errors_total"], 1)   # 全量计数（审查 P3）

    def test_deadline_aborts_softly(self):
        stub = _FakeStub({
            f"/r/d{i}": [_file(f"f{i}", f"/r/d{i}/f{i}", 1, "AA")]
            for i in range(5)
        } | {"/r": [_dir(f"d{i}", f"/r/d{i}") for i in range(5)]})
        with _with_stub(stub):
            _files, stats = cd2_api.scan_remote("/r", deadline_s=-1)
        self.assertTrue(stats["aborted"])

    def test_delete_remotes_reports_per_path(self):
        stub = _FakeStub({})
        with _with_stub(stub):
            out = cd2_api.delete_remotes(["/r/ok.mp4", "/r/bad.mp4"])
        self.assertEqual(out[0], ("/r/ok.mp4", True, ""))
        self.assertFalse(out[1][1])
        self.assertIn("nope", out[1][2])


# ============================================================
# group_duplicates —— 分组纯函数
# ============================================================
class GroupDuplicatesTest(unittest.TestCase):

    def test_groups_by_sha1_and_orders_by_wasted(self):
        files = [
            ("/a/x.mp4", 100, "AA", 20),
            ("/a/y.mp4", 100, "AA", 10),
            ("/a/z.mp4", 100, "AA", 30),
            ("/a/w.rar", 500, "BB", 1),
            ("/a/v.rar", 500, "BB", 2),
        ]
        groups, stats = dup_scan.group_duplicates(files)
        # BB（可省 500）在 AA（可省 200）前
        self.assertEqual([g["sha1"] for g in groups], ["BB", "AA"])
        # 组内按写入时间升序（keep=oldest 取首个）
        self.assertEqual([p for p, _t in groups[1]["files"]],
                         ["/a/y.mp4", "/a/x.mp4", "/a/z.mp4"])
        self.assertEqual(stats["dup_groups"], 2)
        self.assertEqual(stats["dup_files"], 5)
        self.assertEqual(stats["wasted_bytes"], 700)

    def test_empty_and_no_hash_never_group(self):
        files = [
            ("/a/e1", 0, "AA", 1),      # 空文件：SHA1 无区分度
            ("/a/e2", 0, "AA", 2),
            ("/a/n1", 5, "", 3),        # 无哈希
            ("/a/n2", 5, "", 4),
            ("/a/ok", 5, "CC", 5),      # 唯一文件
        ]
        groups, stats = dup_scan.group_duplicates(files)
        self.assertEqual(groups, [])
        self.assertEqual(stats["empty"], 2)   # size=0 且带 sha1 → empty
        self.assertEqual(stats["no_hash"], 2)
        self.assertEqual(stats["dup_groups"], 0)


# ============================================================
# runtime_db v14 —— 落库 / 分页 / replace-per-scan
# ============================================================
class _DbTestCase(unittest.TestCase):
    """每个用例一个全新的 DB 文件（对齐 test_runtime_db 的基类）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dupdb_", dir=_TMP)
        self.path = os.path.join(self.dir, "tg_userbot.db")
        p = mock.patch.object(config, "RUNTIME_DB_FILE", self.path)
        p.start()
        self.addCleanup(p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)


class DupDbTest(_DbTestCase):

    def test_roundtrip_and_pagination(self):
        run_id = runtime_db.dup_start_run("/115open")
        groups = [
            {"sha1": "AA", "size": 100, "files": [("/a", 1), ("/b", 2)]},
            {"sha1": "BB", "size": 50, "files": [("/c", 3)]},
        ]
        runtime_db.dup_finish_run(run_id,
                                  {"total_files": 3, "dup_groups": 2,
                                   "dup_files": 3, "wasted_bytes": 150},
                                  groups)
        run = runtime_db.dup_latest_run()
        self.assertEqual(run["id"], run_id)
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["wasted_bytes"], 150)
        page = runtime_db.dup_group_page(run_id, 0, 1)
        self.assertEqual(page[0]["group_id"], 1)
        self.assertEqual(page[0]["n"], 2)
        members = runtime_db.dup_group_members(run_id, 1)
        self.assertEqual([m["path"] for m in members], ["/a", "/b"])
        self.assertEqual(members[0]["sha1"], "AA")

    def test_replace_per_scan_clears_old_run(self):
        r1 = runtime_db.dup_start_run("/115open")
        runtime_db.dup_finish_run(r1, {"dup_groups": 1, "dup_files": 2,
                                       "wasted_bytes": 10, "total_files": 2},
                                  [{"sha1": "AA", "size": 10,
                                    "files": [("/a", 1), ("/b", 2)]}])
        r2 = runtime_db.dup_start_run("/115open")
        runtime_db.dup_finish_run(r2, {"dup_groups": 1, "dup_files": 2,
                                       "wasted_bytes": 20, "total_files": 2},
                                  [{"sha1": "BB", "size": 20,
                                    "files": [("/c", 1), ("/d", 2)]}])
        self.assertEqual(runtime_db.dup_latest_run()["id"], r2)
        self.assertEqual(runtime_db.dup_group_members(r1, 1), [])
        self.assertEqual(
            [m["path"] for m in runtime_db.dup_group_members(r2, 1)],
            ["/c", "/d"])

    def test_mark_deleted_flags_members(self):
        run_id = runtime_db.dup_start_run("/r")
        runtime_db.dup_finish_run(run_id, {"dup_groups": 1},
                                  [{"sha1": "AA", "size": 1,
                                    "files": [("/a", 1), ("/b", 2)]}])
        runtime_db.dup_mark_deleted(run_id, 1, ["/b"])
        members = {m["path"]: m["deleted"]
                   for m in runtime_db.dup_group_members(run_id, 1)}
        self.assertEqual(members, {"/a": 0, "/b": 1})


# ============================================================
# 命令层 —— 解析 / dry-run / 确认删除 / keep 策略
# ============================================================
class ParseDupCommandTest(unittest.TestCase):

    def test_bare_and_underscore_forms(self):
        self.assertEqual(dup_scan.parse_dup_command("/115dup"),
                         ("list", {"page": 1}))
        self.assertEqual(dup_scan.parse_dup_command("/115dup scan /x"),
                         ("scan", {"root": "/x",
                                   "minutes": dup_scan.SCAN_DEADLINE_DEFAULT_MIN}))
        self.assertEqual(dup_scan.parse_dup_command("/115dup_scan"),
                         ("scan", {"root": "/115open",
                                   "minutes": dup_scan.SCAN_DEADLINE_DEFAULT_MIN}))
        self.assertEqual(dup_scan.parse_dup_command("/115dup_list 2"),
                         ("list", {"page": 2}))
        self.assertEqual(dup_scan.parse_dup_command("/115dup_show 3"),
                         ("show", {"gid": 3}))
        self.assertEqual(
            dup_scan.parse_dup_command("/115dup_del 5 keep=newest 确认"),
            ("del", {"gid": 5, "keep": "newest", "confirmed": True}))

    def test_scan_minutes_and_order_free_args(self):
        # 纯数字 token=分钟；非数字=路径；顺序不拘
        self.assertEqual(dup_scan.parse_dup_command("/115dup scan 30"),
                         ("scan", {"root": "/115open", "minutes": 30}))
        self.assertEqual(
            dup_scan.parse_dup_command("/115dup scan /115open/视频 20"),
            ("scan", {"root": "/115open/视频", "minutes": 20}))
        self.assertEqual(
            dup_scan.parse_dup_command("/115dup_scan 15 /115open/B站"),
            ("scan", {"root": "/115open/B站", "minutes": 15}))
        # 边界钳制：0 → 1，超大 → 上限
        self.assertEqual(dup_scan.parse_dup_command("/115dup scan 0"),
                         ("scan", {"root": "/115open", "minutes": 1}))
        self.assertEqual(
            dup_scan.parse_dup_command("/115dup scan 99999"),
            ("scan", {"root": "/115open",
                      "minutes": dup_scan.SCAN_DEADLINE_MAX_MIN}))

    def test_invalid_args_fall_to_usage(self):
        self.assertEqual(dup_scan.parse_dup_command("/115dup del abc"),
                         ("usage", {}))
        self.assertEqual(dup_scan.parse_dup_command("/115dup show"),
                         ("usage", {}))
        self.assertIsNone(dup_scan.parse_dup_command("/115x"))

    def test_is_dup_command(self):
        self.assertTrue(dup_scan.is_dup_command("/115dup list"))
        self.assertTrue(dup_scan.is_dup_command("/115dup_del 1"))
        self.assertFalse(dup_scan.is_dup_command("/115x"))


class _SeededDbTestCase(_DbTestCase):
    """预置一次扫描：组 1（AA ×3，可省 200）/ 组 2（BB ×2，可省 50）。"""

    def setUp(self):
        super().setUp()
        self.run_id = runtime_db.dup_start_run("/115open")
        runtime_db.dup_finish_run(
            self.run_id,
            {"total_files": 5, "dup_groups": 2, "dup_files": 5,
             "wasted_bytes": 250},
            [
                {"sha1": "AA", "size": 100,
                 "files": [("/o/老.mp4", 100), ("/o/中.mp4", 200),
                           ("/o/新.mp4", 300)]},
                {"sha1": "BB", "size": 50,
                 "files": [("/o/b1.rar", 10), ("/o/b2.rar", 20)]},
            ])


class DeleteFlowTest(_SeededDbTestCase):

    def test_plan_delete_oldest_keeps_earliest_and_skips_deleted(self):
        members = runtime_db.dup_group_members(self.run_id, 1)
        keep, dels = dup_scan._plan_delete(members, "oldest")
        self.assertEqual(keep["path"], "/o/老.mp4")
        self.assertEqual(len(dels), 2)
        # 已删成员不再参与决策
        runtime_db.dup_mark_deleted(self.run_id, 1, ["/o/老.mp4"])
        members = runtime_db.dup_group_members(self.run_id, 1)
        keep, dels = dup_scan._plan_delete(members, "oldest")
        self.assertEqual(keep["path"], "/o/中.mp4")
        self.assertEqual(len(dels), 1)

    def test_plan_delete_newest_keeps_latest(self):
        members = runtime_db.dup_group_members(self.run_id, 1)
        keep, _dels = dup_scan._plan_delete(members, "newest")
        self.assertEqual(keep["path"], "/o/新.mp4")

    def test_wtime_zero_treated_as_oldest(self):
        members = [{"path": "/a", "write_time": 0, "deleted": 0},
                   {"path": "/b", "write_time": 5, "deleted": 0}]
        keep, dels = dup_scan._plan_delete(members, "oldest")
        self.assertEqual(keep["path"], "/a")   # 未知时间 → 保留候选
        self.assertEqual([m["path"] for m in dels], ["/b"])

    def test_wtime_zero_also_preferred_under_newest(self):
        """未知写入时间在 newest 策略下同样优先保留（审查 P3：此前它恰是
        第一个被删的，与「宁多留一份」的 docstring 矛盾）。"""
        members = [{"path": "/a", "write_time": 0, "deleted": 0},
                   {"path": "/b", "write_time": 5, "deleted": 0}]
        keep, dels = dup_scan._plan_delete(members, "newest")
        self.assertEqual(keep["path"], "/a")
        self.assertEqual([m["path"] for m in dels], ["/b"])
        # 全组未知 → newest 取 max(path)（确定性即可），其余可删
        members = [{"path": "/x", "write_time": 0, "deleted": 0},
                   {"path": "/y", "write_time": 0, "deleted": 0}]
        keep, dels = dup_scan._plan_delete(members, "newest")
        self.assertEqual(keep["path"], "/y")
        self.assertEqual([m["path"] for m in dels], ["/x"])

    def test_dry_run_lists_plan_without_deleting(self):
        with mock.patch.object(cd2_api, "delete_remotes") as dr:
            out = dup_scan._del_text(1, "oldest", confirmed=False)
        dr.assert_not_called()
        self.assertIn("删除预览", out)
        self.assertIn("✅保留", out)
        self.assertIn("🗑将删", out)
        self.assertIn("/115dup del 1 keep=oldest 确认", out)

    def test_confirmed_delete_calls_cd2_and_marks_db(self):
        with mock.patch.object(
                cd2_api, "delete_remotes",
                return_value=[("/o/中.mp4", True, ""),
                              ("/o/新.mp4", True, "")]) as dr:
            out = dup_scan._del_text(1, "oldest", confirmed=True)
        self.assertEqual(dr.call_count, 1)
        self.assertIn("2 个进回收站", out)
        members = {m["path"]: m["deleted"]
                   for m in runtime_db.dup_group_members(self.run_id, 1)}
        self.assertEqual(members, {"/o/老.mp4": 0, "/o/中.mp4": 1,
                                   "/o/新.mp4": 1})

    def test_confirmed_delete_partial_failure_reported(self):
        with mock.patch.object(
                cd2_api, "delete_remotes",
                return_value=[("/o/中.mp4", True, ""),
                              ("/o/新.mp4", False, "boom")]):
            out = dup_scan._del_text(1, "oldest", confirmed=True)
        self.assertIn("失败 1 个", out)
        self.assertIn("boom", out)
        members = {m["path"]: m["deleted"]
                   for m in runtime_db.dup_group_members(self.run_id, 1)}
        self.assertEqual(members["/o/新.mp4"], 0)   # 失败不打删标


class ListShowTextTest(_SeededDbTestCase):

    def test_list_text_pages_and_marks_trimmed_groups(self):
        out = dup_scan._list_text(1)
        self.assertIn("第 1/1 页", out)
        self.assertIn("1. ", out)
        self.assertIn("2. ", out)
        runtime_db.dup_mark_deleted(self.run_id, 1,
                                    ["/o/中.mp4", "/o/新.mp4"])
        out = dup_scan._list_text(1)
        self.assertIn("✂️1.", out)   # 组 1 只剩 1 份 → ✂️ 标记

    def test_show_text_lists_name_dir_and_sha1(self):
        out = dup_scan._show_text(2)
        self.assertIn("SHA1 BB", out)
        self.assertIn("MB", out)
        self.assertIn("b1.rar", out)          # 文件名行
        self.assertIn("b2.rar", out)
        self.assertRegex(out, r"\n      /o")  # 目录行缩进
        self.assertRegex(out, r"｜ \d{4}-\d{2}-\d{2} \d{2}:\d{2}")
        self.assertIn("/115dup del 2", out)

    def test_running_run_not_misreported_as_no_duplicates(self):
        """扫描进行中/重启遗留的 running run（dup_groups=0）不得误报
        「没有重复文件」（审查 P3）。"""
        runtime_db.dup_start_run("/115open")   # 新 run 开档未收尾
        out = dup_scan._list_text(1)
        self.assertIn("尚未完成", out)
        self.assertNotIn("没有重复文件", out)

    def test_partial_failed_run_shows_warning_header(self):
        """时限到的 failed run：组表可查，但页头带未完整提示。"""
        runtime_db._write(lambda conn: runtime_db._execute(
            conn, "UPDATE dup_runs SET error=?, status='failed' WHERE id=?",
            ("扫描时限到（10 分钟），结果为已扫部分", self.run_id)), "改 run")
        out = dup_scan._list_text(1)
        self.assertIn("上次扫描未完整", out)
        self.assertIn("1. ", out)              # 组数据照常可查

    def test_show_missing_group(self):
        out = dup_scan._show_text(99)
        self.assertIn("不存在", out)


class ScanAndSaveTest(_DbTestCase):

    def test_scan_and_save_persists_and_reports(self):
        stub = _FakeStub({
            "/r": [_file("a", "/r/a", 100, "AA", 10),
                   _file("b", "/r/b", 100, "AA", 20)],
        })
        with _with_stub(stub):
            run_id, groups, stats, scan_stats = dup_scan.scan_and_save("/r")
        self.assertEqual(len(groups), 1)
        self.assertEqual(stats["dup_groups"], 1)
        self.assertEqual(stats["wasted_bytes"], 100)
        run = runtime_db.dup_latest_run()
        self.assertEqual(run["id"], run_id)
        self.assertEqual(run["status"], "done")

    def test_deadline_reaches_scan_and_timeout_marks_partial(self):
        captured = {}

        def fake_scan(root, progress=None, deadline_s=None):
            captured["deadline_s"] = deadline_s
            return ([("/r/a", 10, "AA", 1), ("/r/b", 10, "AA", 2)],
                    {"dirs": 1, "files": 2, "no_hash": 0, "errors": [],
                     "aborted": True, "elapsed_s": 0.5})

        with mock.patch.object(cd2_api, "scan_remote",
                               side_effect=fake_scan):
            run_id, groups, stats, _scan = dup_scan.scan_and_save("/r", 20 * 60)
        self.assertEqual(captured["deadline_s"], 1200)
        run = runtime_db.dup_latest_run()
        self.assertEqual(run["status"], "failed")   # 半份结果
        self.assertIn("已扫部分", run["error"])

        # 不传时限 → 默认 10 分钟
        with mock.patch.object(cd2_api, "scan_remote", side_effect=fake_scan):
            dup_scan.scan_and_save("/r")
        self.assertEqual(captured["deadline_s"],
                         dup_scan.SCAN_DEADLINE_DEFAULT_MIN * 60)

    def test_summary_text_without_runs(self):
        out = dup_scan.summary_text()
        self.assertIn("/115dup scan", out)


class CommandReplyTest(_SeededDbTestCase):

    def test_bare_command_lists_latest_run(self):
        out = asyncio.run(dup_scan.command_reply("/115dup"))
        self.assertIn("重复 2 组", out)

    def test_usage_on_bad_args(self):
        out = asyncio.run(dup_scan.command_reply("/115dup del x"))
        self.assertIn("/115dup scan", out)

    def test_non_command_returns_none(self):
        self.assertIsNone(asyncio.run(dup_scan.command_reply("/115x")))


# ============================================================
# 报告导出 —— 渲染 / 上传 / 降级
# ============================================================
class ParseReportTest(unittest.TestCase):

    def test_report_forms(self):
        self.assertEqual(dup_scan.parse_dup_command("/115dup report"),
                         ("report", {"dir": dup_scan.REPORT_DIR_DEFAULT}))
        self.assertEqual(
            dup_scan.parse_dup_command("/115dup_report /115open/X"),
            ("report", {"dir": "/115open/X"}))

    def test_is_dup_command_accepts_report(self):
        self.assertTrue(dup_scan.is_dup_command("/115dup_report"))


class _ExportBase(_SeededDbTestCase):
    """挂载点/本地存档/RUNTIME_DIR 全部落临时目录；gRPC 层 mock。"""

    def setUp(self):
        super().setUp()
        self.mount_base = os.path.join(self.dir, "mount")
        self.report_dir = os.path.join(self.dir, "reports")
        os.makedirs(self.mount_base)
        patches = [
            mock.patch.object(config, "CLOUD_MOUNT_BASE", self.mount_base),
            mock.patch.object(config, "RUNTIME_DIR", self.report_dir),
            mock.patch.object(dup_scan, "REPORT_VERIFY_GAP_S", 0.0),
            mock.patch.object(dup_scan, "REPORT_VERIFY_TRIES", 2),
            mock.patch.object(dup_scan, "REPORT_UPLOAD_WAIT_GAP_S", 0.0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _local_sizes(self, res):
        return {os.path.basename(p): os.path.getsize(p) for p in res["files"]}


class ReportRenderTest(_ExportBase):

    def test_text_report_marks_dates_mb_and_split_paths(self):
        rows = dup_scan._rows_from_db(self.run_id)
        text = dup_scan._render_report_text("/115open", {"total_files": 5,
                                                         "dup_groups": 2,
                                                         "dup_files": 5,
                                                         "wasted_bytes": 250},
                                            rows)
        self.assertIn("115 查重报告", text)
        self.assertIn("可释放", text)
        self.assertIn("MB", text)          # 尺寸统一 MB
        self.assertIn("── 组 1（", text)
        self.assertIn("✅", text)   # 保留建议（最早）
        self.assertIn("🗑", text)
        # 目录与文件名分行：文件名行在前、目录行缩进在后
        self.assertRegex(text, r"✅ 老\.mp4 ｜ \d{4}-\d{2}-\d{2} \d{2}:\d{2}")
        self.assertRegex(text, r"\n     /o\n")
        runtime_db.dup_mark_deleted(self.run_id, 2, ["/o/b2.rar"])
        rows = dup_scan._rows_from_db(self.run_id)
        text = dup_scan._render_report_text("/115open", {}, rows)
        self.assertIn("[已删]", text)

    def test_csv_report_columns(self):
        import csv
        import io
        rows = dup_scan._rows_from_db(self.run_id)
        table = list(csv.reader(io.StringIO(dup_scan._render_report_csv(rows))))
        self.assertEqual(table[0],
                         ["group_id", "sha1", "size_mb", "directory",
                          "filename", "write_time", "deleted"])
        # 3（组1）+ 2（组2）+ 表头 = 6 行
        self.assertEqual(len(table), 6)
        # 目录与文件名分列；时间格式化；size 为 MB 数值
        self.assertEqual(table[1][3], "/o")
        self.assertEqual(table[1][4], "老.mp4")
        self.assertRegex(table[1][5], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertEqual(table[1][2], dup_scan._mb_num(100))

    def test_mb_and_path_helpers(self):
        self.assertEqual(dup_scan._fmt_mb(0), "0.00 MB")
        self.assertEqual(dup_scan._fmt_mb(50 * 1024), "0.05 MB")
        self.assertEqual(dup_scan._fmt_mb(100 * 1048576), "100.0 MB")
        self.assertEqual(dup_scan._fmt_mb(1546188226), "1474.6 MB")
        self.assertEqual(dup_scan._mb_num(1048576), "1.00")
        self.assertEqual(dup_scan._split_path("/a/b/c.mp4"), ("/a/b", "c.mp4"))
        self.assertEqual(dup_scan._split_path("/c.mp4"), ("/", "c.mp4"))
        self.assertEqual(dup_scan._fmt_dt(0), "时间未知")


class ExportReportTest(_ExportBase):

    def test_success_writes_local_and_mount_and_verifies(self):
        sizes = {}

        def fake_find(remote, force_refresh=False):
            lp = os.path.join(self.report_dir, "dup_reports",
                              remote.rsplit("/", 1)[-1])
            if os.path.exists(lp):
                return NS(size=os.path.getsize(lp))
            return None

        with mock.patch.object(cd2_api, "find_remote", side_effect=fake_find), \
                mock.patch.object(cd2_api, "upload_files_summary",
                                  return_value=(0, [])):
            res = dup_scan.export_report()
        self.assertTrue(res["ok"], res["note"])
        names = sorted(os.path.basename(p) for p in res["files"])
        self.assertEqual([n.rsplit(".", 1)[1] for n in names], ["csv", "txt"])
        mount_out = os.listdir(os.path.join(self.mount_base, "userBot报告"))
        self.assertEqual(len(mount_out), 2)
        for p in res["files"]:
            self.assertEqual(
                os.path.getsize(p),
                os.path.getsize(os.path.join(
                    self.mount_base, "userBot报告", os.path.basename(p))))

    def test_verify_fail_keeps_local_and_reports_note(self):
        with mock.patch.object(cd2_api, "find_remote", return_value=None):
            res = dup_scan.export_report()
        self.assertFalse(res["ok"])
        self.assertIn("对账", res["note"])
        self.assertIsNotNone(res["local_dir"])
        for p in res["files"]:
            self.assertTrue(os.path.exists(p))   # 本地存档永在

    def test_dir_outside_115open_local_only(self):
        with mock.patch.object(cd2_api, "find_remote", return_value=None):
            res = dup_scan.export_report(target_dir="/V1/other")
        self.assertFalse(res["ok"])
        self.assertIn("不在 /115open", res["note"])
        self.assertTrue(res["files"])

    def test_no_data_sources(self):
        # 清掉 seeded run：无 run 无 mem → 提示先扫描
        runtime_db._write(lambda conn: runtime_db._execute(
            conn, "DELETE FROM dup_files"), "清组员")
        runtime_db._write(lambda conn: runtime_db._execute(
            conn, "DELETE FROM dup_runs"), "清 run")
        res = dup_scan.export_report()
        self.assertFalse(res["ok"])
        self.assertIn("/115dup scan", res["note"])

    def test_memory_groups_export_without_db(self):
        runtime_db.close_db()   # 模拟 DB 不可用
        groups = [{"sha1": "AA", "size": 10,
                   "files": [("/a", 1), ("/b", 2)]}]

        def fake_find(remote, force_refresh=False):
            lp = os.path.join(self.report_dir, "dup_reports",
                              remote.rsplit("/", 1)[-1])
            return NS(size=os.path.getsize(lp)) if os.path.exists(lp) else None

        with mock.patch.object(cd2_api, "find_remote", side_effect=fake_find), \
                mock.patch.object(cd2_api, "upload_files_summary",
                                  return_value=(0, [])):
            res = dup_scan.export_report(
                mem_groups=groups, mem_stats={"dup_groups": 1},
                mem_root="/115open")
        self.assertTrue(res["ok"], res["note"])
        txt = next(p for p in res["files"] if p.endswith(".txt"))
        with open(txt, encoding="utf-8") as f:
            body = f.read()
        # 目录/文件名分离后的两种行都渲染（文件名行 + 目录行）
        self.assertRegex(body, r"✅ a ｜ \d{4}-\d{2}-\d{2} \d{2}:\d{2}")
        self.assertRegex(body, r"\n     /\n")


class ReportCommandTest(_ExportBase):

    def test_report_text_ok(self):
        def fake_find(remote, force_refresh=False):
            lp = os.path.join(self.report_dir, "dup_reports",
                              remote.rsplit("/", 1)[-1])
            return NS(size=os.path.getsize(lp)) if os.path.exists(lp) else None

        with mock.patch.object(cd2_api, "find_remote", side_effect=fake_find), \
                mock.patch.object(cd2_api, "upload_files_summary",
                                  return_value=(0, [])):
            out = dup_scan._report_text(dup_scan.REPORT_DIR_DEFAULT)
        self.assertIn("📤 报告已传到 115", out)
        self.assertIn(dup_scan.REPORT_DIR_DEFAULT, out)

    def test_scan_command_replies_and_spawns_background(self):
        """scan 只回执（后台 task 跑扫描）——命令协程不再背 10 分钟扫描，
        断线重连杀不掉导出（2026-10-23 22:22 事故的根因修复）。
        同时钉死：防重入标志同步置位（无调度窗口）、task 登记进强引用集。"""
        spawned = {}

        class _FakeTask:
            def __init__(self, coro):
                spawned["root"] = coro.cr_frame.f_locals.get("root")
                coro.close()

            def add_done_callback(self, cb):
                spawned["has_done_cb"] = True

        try:
            with mock.patch.object(asyncio, "create_task", _FakeTask):
                out = asyncio.run(
                    dup_scan.command_reply("/115dup scan /115open/x 5"))
            self.assertIn("扫描已启动", out)
            self.assertIn("/115open/x", out)
            self.assertIn("5 分钟", out)
            # 防重入标志已同步置位（FakeTask 不跑协程，finally 不清）
            self.assertEqual(dup_scan._SCAN_RUNNING.get("root"), "/115open/x")
            # 强引用登记 + done_callback 回收（§35）
            self.assertTrue(spawned.get("has_done_cb"))
        finally:
            dup_scan._SCAN_RUNNING = {}
            dup_scan._SCAN_TASKS.clear()

    def test_scan_reentry_rejected_while_running(self):
        dup_scan._SCAN_RUNNING = {"root": "/115open", "minutes": 10}
        try:
            with mock.patch.object(asyncio, "create_task") as ct:
                out = asyncio.run(dup_scan.command_reply("/115dup scan"))
            ct.assert_not_called()
            self.assertIn("已有扫描在跑", out)
        finally:
            dup_scan._SCAN_RUNNING = {}

    def test_run_scan_and_notify_sends_result(self):
        """后台主体：扫描→导出→notify_user（通知含 📤 结果行）。"""
        files = [("/115open/x/a.mp4", 100, "AA", 10),
                 ("/115open/x/b.mp4", 100, "AA", 20)]
        notes = []

        def fake_find(remote, force_refresh=False):
            lp = os.path.join(self.report_dir, "dup_reports",
                              remote.rsplit("/", 1)[-1])
            return NS(size=os.path.getsize(lp)) if os.path.exists(lp) else None

        async def fake_notify(text):
            notes.append(text)

        with mock.patch.object(cd2_api, "scan_remote",
                               return_value=(files, {"dirs": 1, "files": 2,
                                                     "no_hash": 0,
                                                     "errors": [],
                                                     "aborted": False,
                                                     "elapsed_s": 0.1})), \
             mock.patch.object(cd2_api, "find_remote", side_effect=fake_find), \
             mock.patch.object(cd2_api, "upload_files_summary",
                               return_value=(0, [])), \
             mock.patch("tg_userbot.notify.notify_user", fake_notify):
            asyncio.run(dup_scan.run_scan_and_notify("/115open/x", 5))
        self.assertEqual(len(notes), 1)
        self.assertIn("重复 1 组", notes[0])
        self.assertIn("📤 报告已传到 115", notes[0])
        self.assertEqual(dup_scan._SCAN_RUNNING, {}, "跑完必须清防重入标志")

    def test_export_crash_still_notifies(self):
        """导出阶段冒意外异常（如 gRPC 层 RuntimeError）不得杀死后台 task：
        通知必达，异常降级成文案行（审查 P2）。"""
        files = [("/115open/x/a.mp4", 100, "AA", 10),
                 ("/115open/x/b.mp4", 100, "AA", 20)]
        notes = []

        async def fake_notify(text):
            notes.append(text)

        with mock.patch.object(cd2_api, "scan_remote",
                               return_value=(files, {"dirs": 1, "files": 2,
                                                     "no_hash": 0,
                                                     "errors": [],
                                                     "aborted": False,
                                                     "elapsed_s": 0.1})), \
             mock.patch.object(dup_scan, "export_report",
                               side_effect=RuntimeError("CD2 令牌未配置")), \
             mock.patch("tg_userbot.notify.notify_user", fake_notify):
            asyncio.run(dup_scan.run_scan_and_notify("/115open/x", 5))
        self.assertEqual(len(notes), 1)
        self.assertIn("重复 1 组", notes[0])          # 扫描结果照常通知
        self.assertIn("报告导出异常", notes[0])
        self.assertIn("/115dup report", notes[0])     # 指引自救路径
        self.assertEqual(dup_scan._SCAN_RUNNING, {})


# ============================================================
# 端到端回归：2026-10-23 22:22 生产事故
# ============================================================
# 事故链：Telethon update 循环 bug 断线 → 事件处理器（命令协程）被连坐
# 取消 → 当时扫描挂在命令协程上，扫描线程在 executor 里跑完落库，但
# 导报告与完成回复随协程死掉、无任何日志（run #2 报告静默丢失）。
# 修复＝扫描改后台 task + 强引用（§35）+ 导出兜底。本回归让后台扫描
# 真实跑起来，在扫描中途施加「GC 压力（无强引用时 task 可被回收）+
# 触发者已不存在」，验证「落库 → 导报告 → 通知」一个不少。
class BackgroundResilienceRegressionTest(_ExportBase):

    def _patches(self, notes, fake_scan):
        def fake_find(remote, force_refresh=False):
            lp = os.path.join(self.report_dir, "dup_reports",
                              remote.rsplit("/", 1)[-1])
            return NS(size=os.path.getsize(lp)) if os.path.exists(lp) else None

        async def fake_notify(text):
            notes.append(text)

        return [
            mock.patch.object(cd2_api, "scan_remote", side_effect=fake_scan),
            mock.patch.object(cd2_api, "find_remote", side_effect=fake_find),
            mock.patch.object(cd2_api, "upload_files_summary",
                              return_value=(0, [])),
            mock.patch("tg_userbot.notify.notify_user", fake_notify),
        ]

    def test_disconnect_and_gc_do_not_lose_scan_results(self):
        import gc
        import time as _t
        files = [("/115open/reg/a.mp4", 100, "AA", 10),
                 ("/115open/reg/b.mp4", 100, "AA", 20)]
        release = {"go": False}

        def fake_scan(root, progress=None, deadline_s=None):
            # 「十分钟扫描」压缩成：阻塞等放行（断线发生时扫描线程仍在跑）
            until = _t.monotonic() + 5
            while not release["go"]:
                if _t.monotonic() > until:
                    raise AssertionError("扫描桩等待放行超时")
                _t.sleep(0.01)
            return (files, {"dirs": 1, "files": 2, "no_hash": 0,
                            "errors": [], "errors_total": 0,
                            "aborted": False, "elapsed_s": 0.1})

        notes = []

        async def scenario():
            # 命令触发即返回回执——命令协程（老架构的扫描宿主）已不存在
            ack = await dup_scan.command_reply("/115dup scan /115open/reg 1")
            self.assertIn("扫描已启动", ack)
            await asyncio.sleep(0.05)   # 后台扫描已进入 to_thread
            # 在途断言：强引用已登记（丢引用 = task 可能被 GC 静默吞掉）
            self.assertEqual(len(dup_scan._SCAN_TASKS), 1,
                             "扫描在途必须持强引用（§35）")
            self.assertTrue(dup_scan._SCAN_RUNNING, "在途须置防重入标志")
            # 再发一次 scan：必须被防重入挡住（replace-per-sscan 不被双开破坏）
            self.assertIn("已有扫描在跑",
                          await dup_scan.command_reply("/115dup scan"))
            # GC 压力 + 触发者消亡（模拟断线把事件处理器连坐杀掉之后的世界）
            gc.collect()
            release["go"] = True        # 扫描线程跑完剩余工作
            for _ in range(300):        # 等全链收尾（≤6s 预算）
                if notes:
                    break
                await asyncio.sleep(0.02)
            self.assertTrue(notes, "后台扫描未完成或通知丢失（事故复发）")
            # 让后台 task 走完 finally（清防重入标志/回收强引用）再退出场景
            await asyncio.sleep(0.05)

        for p in self._patches(notes, fake_scan):
            p.start()
            self.addCleanup(p.stop)
        asyncio.run(scenario())

        # 通知内容完整：扫描结果 + 报告去向
        self.assertIn("重复 1 组", notes[0])
        self.assertIn("📤 报告已传到 115", notes[0])
        # 落库完整：run 收尾为 done，组表可查
        run = runtime_db.dup_latest_run()
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["dup_groups"], 1)
        # 报告真落了双份（本地 + 挂载），gRPC 对账语义由 find 桩保证
        mount_out = os.listdir(os.path.join(self.mount_base, "userBot报告"))
        self.assertEqual(len(mount_out), 2)
        # 收尾清理：强引用集与防重入标志都归位
        self.assertEqual(dup_scan._SCAN_TASKS, set())
        self.assertEqual(dup_scan._SCAN_RUNNING, {})


if __name__ == "__main__":
    unittest.main()
