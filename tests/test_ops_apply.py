"""报告 CSV 执行器（ops_apply.py）的单元测试。

**防丢失红线全部有断言钉死**：组保全、防覆盖、行身份校验、事后校验、
幂等跳过。CD2 层全 mock，不联网不碰真实网盘。
运行方式（项目根目录）：
    .venv/bin/python -m unittest tests.test_ops_apply -v
"""
import asyncio
import atexit
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_apply_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import cd2_api     # noqa: E402
from tg_userbot import ops_apply   # noqa: E402
from tg_userbot import runtime_db  # noqa: E402
from tg_userbot import config      # noqa: E402


CSV_HEADER = ("group_id,sha1,size_mb,directory,filename,write_time,"
              "deleted,新目录,新文件名")


def _csv(rows):
    lines = [CSV_HEADER] + rows
    return "\n".join(lines)


def _row(gid="1", sha1="A" * 40, d="/115open/B站", f="a.mp4",
         deleted="", nd="", nn=""):
    return f'{gid},{sha1},100.00,{d},{f},"2026-07-21 14:30",{deleted},{nd},{nn}'


class ParseTest(unittest.TestCase):

    def test_parse_by_header_and_flags(self):
        rows = ops_apply.parse_plan_csv(_csv([
            _row(deleted="1"),
            _row(f="b.mp4", nd="/115open/视频", nn="b_new.mp4"),
            "",   # 空行容忍
        ]))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["path"], "/115open/B站/a.mp4")
        self.assertTrue(ops_apply._is_delete(rows[0]["deleted"]))
        self.assertEqual(rows[1]["new_dir"], "/115open/视频")
        # 是/y/1 都认删除；空/0 不删
        self.assertTrue(ops_apply._is_delete("是"))
        self.assertFalse(ops_apply._is_delete("0"))
        self.assertFalse(ops_apply._is_delete(""))
        # sha1 科学计数法污染识别（40 位 hex 才算）
        self.assertFalse(ops_apply._looks_sha1("4.98E+39"))
        self.assertTrue(ops_apply._looks_sha1("a" * 40))


class PlanTest(unittest.TestCase):
    """计划构建：动作判定 + 全部防丢失守卫。"""

    def test_actions_and_precedence(self):
        rows = ops_apply.parse_plan_csv(_csv([
            _row(f="del.mp4", deleted="1"),
            _row(f="mv.mp4", nd="/115open/视频"),
            _row(f="rn.mp4", nn="new.mp4"),
            _row(f="both.mp4", nd="/115open/视频", nn="new.mp4"),
            _row(f="delmv.mp4", deleted="1", nd="/115open/视频"),
            _row(f="none.mp4"),
        ]))
        plan = ops_apply.build_plan(rows)
        actions = {p["row"]["filename"]: p for p in plan}
        self.assertEqual(actions["del.mp4"]["action"], "delete")
        self.assertEqual(actions["mv.mp4"]["action"], "move")
        self.assertEqual(actions["rn.mp4"]["action"], "rename")
        self.assertEqual(actions["both.mp4"]["action"], "combo")
        # deleted 优先，H/I 忽略但标注
        self.assertEqual(actions["delmv.mp4"]["action"], "delete")
        self.assertIn("忽略", actions["delmv.mp4"]["note"])
        self.assertEqual(actions["none.mp4"]["action"], "noop")

    def test_group_guard_blocks_full_deletion(self):
        rows = ops_apply.parse_plan_csv(_csv([
            _row(f="a.mp4", deleted="1"),
            _row(f="b.mp4", deleted="1"),
            _row(f="c.mp4"),   # 未标注 → 组内保留 → 放行删除 a/b
        ]))
        plan = ops_apply.build_plan(rows)
        acts = {p["row"]["filename"]: p["action"] for p in plan}
        self.assertEqual(acts["a.mp4"], "delete")
        # 全组删除 → 被拒
        rows2 = ops_apply.parse_plan_csv(_csv([
            _row(f="a.mp4", deleted="1"),
            _row(f="b.mp4", deleted="1"),
        ]))
        plan2 = ops_apply.build_plan(rows2)
        self.assertTrue(all(p["action"] == "skip" and "整组删除" in p["note"]
                            for p in plan2))
        # 显式「全组」放行
        plan3 = ops_apply.build_plan(rows2, allow_full_group=True)
        self.assertTrue(all(p["action"] == "delete" for p in plan3))

    def test_empty_group_id_delete_rejected(self):
        """group_id 列被破坏（空）的删除行：无法验证组保全 → 拒绝。"""
        rows = ops_apply.parse_plan_csv(_csv([
            _row(gid="", f="a.mp4", deleted="1"),
            _row(gid="", f="b.mp4"),
        ]))
        plan = ops_apply.build_plan(rows)
        acts = {p["row"]["filename"]: p for p in plan}
        self.assertEqual(acts["a.mp4"]["action"], "skip")
        self.assertIn("group_id 为空", acts["a.mp4"]["note"])

    def test_path_safety(self):
        rows = ops_apply.parse_plan_csv(_csv([
            _row(f="x.mp4", nd="/V1/elsewhere"),          # 盘外
            _row(f="y.mp4", nd="/115open/a/../b"),        # 穿越
            _row(f="z.mp4", nn="sub/name.mp4"),           # 文件名带 /
            _row(f="ok.mp4", nd="/115open/视频/"),        # 尾斜杠规范化
        ]))
        plan = ops_apply.build_plan(rows)
        by = {p["row"]["filename"]: p for p in plan}
        self.assertEqual(by["x.mp4"]["action"], "skip")
        self.assertIn("/115open", by["x.mp4"]["note"])
        self.assertEqual(by["y.mp4"]["action"], "skip")
        self.assertIn("穿越", by["y.mp4"]["note"])
        self.assertEqual(by["z.mp4"]["action"], "skip")
        self.assertEqual(by["ok.mp4"]["action"], "move")
        self.assertEqual(by["ok.mp4"]["row"]["new_dir"], "/115open/视频")


class ExecuteTest(unittest.TestCase):
    """逐行执行：成功路径 + 每条防丢失校验的失败路径。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="applydb_", dir=_TMP)
        p = mock.patch.object(config, "RUNTIME_DB_FILE",
                              os.path.join(self.dir, "t.db"))
        p.start()
        self.addCleanup(p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        runtime_db.init_db()
        runtime_db.dup_start_run("/115open")
        runtime_db.dup_finish_run(1, {"dup_groups": 1}, [
            {"sha1": "A" * 40, "size": 100,
             "files": [("/115open/B站/a.mp4", 1), ("/115open/B站/b.mp4", 2)]}])

    def _remote(self, path, size=100, sha1="A" * 40):
        return NS(size=size, fileHashes={2: sha1})

    def test_delete_success_collects_for_batch_marking(self):
        """删除成功只收集路径，打标由 apply_csv 收尾批量做（防跨线程
        DB 撞车，2026-10-25 优化）——本用例钉死 _execute_one 不再逐行写库。"""
        rows = ops_apply.parse_plan_csv(_csv([
            _row(f="a.mp4", deleted="1"), _row(f="b.mp4")]))
        plan = ops_apply.build_plan(rows)
        deleted = []
        with mock.patch.object(cd2_api, "find_remote",
                               return_value=self._remote("/x")), \
             mock.patch.object(cd2_api, "delete_remotes",
                               return_value=[("/115open/B站/a.mp4",
                                              True, "")]):
            out = ops_apply._execute_one(plan[0], deleted=deleted)
        self.assertTrue(out.startswith("✅"), out)
        self.assertEqual(deleted, ["/115open/B站/a.mp4"])
        # 未批量打标前 DB 不动
        members = runtime_db.dup_group_members(1, 1)
        self.assertTrue(all(m["deleted"] == 0 for m in members))

    def test_spot_check_flags_zombie_delete(self):
        """抽查复核：DeleteFile 报成功但路径仍在 → 结果里 ❌ 上报。"""
        rows = ops_apply.parse_plan_csv(_csv([_row(deleted="1")]))
        csv_path = os.path.join(_TMP, "spot.csv")
        with open(csv_path, "w", encoding="utf-8-sig") as f:
            f.write(_csv([_row(deleted="1")]))
        runtime_db.dup_start_run("/115open")
        runtime_db.dup_finish_run(1, {"dup_groups": 1}, [
            {"sha1": "A" * 40, "size": 100,
             "files": [("/115open/B站/a.mp4", 1)]}])
        with mock.patch.object(cd2_api, "list_dir_index",
                               return_value={"a.mp4": (100, "A" * 40)}), \
             mock.patch.object(cd2_api, "delete_remotes",
                               return_value=[("/115open/B站/a.mp4",
                                              True, "")]), \
             mock.patch.object(cd2_api, "find_remote",
                               return_value=self._remote("/x")):
            out = ops_apply.apply_csv(csv_path, confirmed=True,
                                      allow_full_group=True)
        self.assertIn("成功 1", out)
        self.assertIn("仍存在", out)
        # P1 回归：抽查条目带 action 键，_write_results 不炸且结果落盘
        self.assertIn("执行结果", out)

    def test_sha1_mismatch_skips(self):
        rows = ops_apply.parse_plan_csv(_csv([_row(deleted="1")]))
        plan = ops_apply.build_plan(rows, allow_full_group=True)
        with mock.patch.object(cd2_api, "find_remote",
                               return_value=self._remote("/x",
                                                         sha1="B" * 40)):
            out = ops_apply._execute_one(plan[0])
        self.assertIn("SHA1 不符", out)

    def test_sha1_check_via_prefetch_index_no_grpc(self):
        """预取索引命中时行校验零 gRPC——SHA1 不符照样拦（防丢失不降级）。"""
        rows = ops_apply.parse_plan_csv(_csv([_row(deleted="1")]))
        plan = ops_apply.build_plan(rows, allow_full_group=True)
        index = {"/115open/B站/a.mp4": (100, "B" * 40)}   # 索引说已变
        with mock.patch.object(cd2_api, "find_remote") as fr:
            out = ops_apply._execute_one(plan[0], index=index)
        self.assertIn("SHA1 不符", out)
        fr.assert_not_called()          # 全程没碰 gRPC
        # 索引一致时放行删除
        index2 = {"/115open/B站/a.mp4": (100, "A" * 40)}
        with mock.patch.object(cd2_api, "find_remote") as fr2, \
             mock.patch.object(cd2_api, "delete_remotes",
                               return_value=[("/115open/B站/a.mp4",
                                              True, "")]) as dr:
            out2 = ops_apply._execute_one(plan[0], index=index2)
        self.assertTrue(out2.startswith("✅"), out2)
        fr2.assert_not_called()
        dr.assert_called_once()

    def test_source_missing_skips_idempotently(self):
        rows = ops_apply.parse_plan_csv(_csv([_row(deleted="1")]))
        plan = ops_apply.build_plan(rows, allow_full_group=True)
        with mock.patch.object(cd2_api, "find_remote", return_value=None):
            out = ops_apply._execute_one(plan[0])
        self.assertIn("源不存在", out)

    def test_move_uses_rename_conflict_policy_and_verifies(self):
        rows = ops_apply.parse_plan_csv(
            _csv([_row(f="a.mp4", nd="/115open/视频")]))
        plan = ops_apply.build_plan(rows)
        queried = []

        def fake_find(p, **k):
            queried.append(p)
            return self._remote(p)

        with mock.patch.object(cd2_api, "find_remote",
                               side_effect=fake_find), \
             mock.patch.object(cd2_api, "move_remote",
                               return_value=(True, "")) as mv, \
             mock.patch("os.makedirs"):
            out = ops_apply._execute_one(plan[0])
        self.assertTrue(out.startswith("✅"), out)
        self.assertIn("→ /115open/视频/a.mp4", out)
        mv.assert_called_once()
        args, kwargs = mv.call_args
        self.assertEqual(args[0], "/115open/B站/a.mp4")
        self.assertEqual(args[1], "/115open/视频")
        # 事后校验确实查过终态路径
        self.assertIn("/115open/视频/a.mp4", queried)

    def test_rename_blocked_when_target_exists(self):
        rows = ops_apply.parse_plan_csv(_csv([_row(nn="exists.mp4")]))
        plan = ops_apply.build_plan(rows)
        with mock.patch.object(cd2_api, "find_remote",
                               return_value=self._remote("/x")), \
             mock.patch.object(cd2_api, "rename_remote",
                               return_value=(False, "目标名已存在")) as rn:
            out = ops_apply._execute_one(plan[0])
        self.assertIn("重命名失败", out)
        self.assertIn("目标名已存在", out)
        rn.assert_called_once()


class ApplyCsvFlowTest(unittest.TestCase):

    def test_missing_file_and_dry_run_and_confirm(self):
        out = ops_apply.apply_csv("/nope.csv")
        self.assertIn("不存在", out)
        # dry-run
        csv_path = os.path.join(_TMP, "plan.csv")
        with open(csv_path, "w", encoding="utf-8-sig") as f:
            f.write(_csv([_row(f="a.mp4", deleted="1"), _row(f="b.mp4")]))
        out = ops_apply.apply_csv(csv_path)
        self.assertIn("执行计划", out)
        self.assertIn("删除 1", out)
        self.assertIn("确认", out)
        # 确认执行（mock 全链路：索引命中 + 删除成功 + 抽查已消失）
        with mock.patch.object(cd2_api, "list_dir_index",
                               return_value={"a.mp4": (100, "A" * 40),
                                             "b.mp4": (100, "A" * 40)}), \
             mock.patch.object(cd2_api, "delete_remotes",
                               return_value=[("/115open/B站/a.mp4",
                                              True, "")]), \
             mock.patch.object(cd2_api, "find_remote",
                               return_value=None):
            out = ops_apply.apply_csv(csv_path, confirmed=True)
        self.assertIn("成功 1", out)
        self.assertIn("执行结果", out)
        # 结果 CSV 落地（本地）；115 上传因 dest=/private… 非 /115open 跳过
        self.assertIn("_执行结果_", out)

    def test_read_115_path(self):
        with mock.patch.object(cd2_api, "read_remote_text",
                               return_value=_csv([_row()])):
            out = ops_apply.apply_csv("/115open/userBot报告/2026-10-24/x.csv")
        self.assertIn("执行计划", out)
        with mock.patch.object(cd2_api, "read_remote_text", return_value=None):
            out = ops_apply.apply_csv("/115open/userBot报告/x.csv")
        self.assertIn("读不到", out)


if __name__ == "__main__":
    unittest.main()


class ApplyBackgroundTest(unittest.TestCase):
    """确认执行后台化（2026-10-25）：回执立即、执行进后台 task、防重入。"""

    def test_confirmed_runs_in_background_and_notifies(self):
        import asyncio
        notes = []

        async def fake_notify(text):
            notes.append(text)

        spawned = {}

        class _FakeTask:
            def __init__(self, coro):
                spawned["coro"] = coro
                coro.close()

            def add_done_callback(self, cb):
                pass

        try:
            with mock.patch.object(asyncio, "create_task", _FakeTask), \
                 mock.patch.object(ops_apply, "apply_csv",
                                   return_value="📋 执行完成：成功 3"):
                out = asyncio.run(ops_apply.command_reply(
                    "/115dup apply /115open/x/a.csv 确认"))
            self.assertIn("开始执行", out)          # 立即回执
            self.assertIn("完成后通知", out)
            self.assertEqual(ops_apply._APPLY_RUNNING.get("csv"),
                             "/115open/x/a.csv")
            # 防重入：第二条被拒
            with mock.patch.object(asyncio, "create_task") as ct:
                out2 = asyncio.run(ops_apply.command_reply(
                    "/115dup apply /115open/x/b.csv 确认"))
            ct.assert_not_called()
            self.assertIn("进行", out2)
        finally:
            ops_apply._APPLY_RUNNING = {}

        # 后台主体：执行 + 通知（apply_csv 已 mock）
        ops_apply._APPLY_RUNNING = {}
        with mock.patch.object(ops_apply, "apply_csv",
                               return_value="📋 执行完成：成功 3"), \
             mock.patch("tg_userbot.notify.notify_user", fake_notify):
            asyncio.run(ops_apply.run_apply_and_notify("/115open/x/c.csv",
                                                       False))
        self.assertEqual(notes, ["📋 执行完成：成功 3"])
        self.assertEqual(ops_apply._APPLY_RUNNING, {})


class InterruptMarkTest(unittest.TestCase):
    """中断标记（重启不丢在途）：执行期落文件，完成/异常清除，启动可恢复。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="applymark_", dir=_TMP)
        self.rt = os.path.join(self.dir, "runtime")
        os.makedirs(self.rt, exist_ok=True)
        for p in (mock.patch.object(config, "RUNTIME_DIR", self.rt),):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_mark_written_during_run_and_cleared_after(self):
        seen = {}

        def slow_apply(csv, confirm, full_group):
            seen["mark_at_run"] = os.path.exists(ops_apply._mark_path())
            return "📋 执行完成：成功 1"

        async def fake_notify(text):
            pass

        with mock.patch.object(ops_apply, "apply_csv",
                               side_effect=slow_apply), \
             mock.patch("tg_userbot.notify.notify_user", fake_notify):
            asyncio.run(ops_apply.run_apply_and_notify("/a/b.csv", False))
        self.assertTrue(seen["mark_at_run"], "执行中标记文件必须在")
        self.assertFalse(os.path.exists(ops_apply._mark_path()),
                         "完成后标记必须清掉")

    def test_mark_cleared_even_on_exception(self):
        async def fake_notify(text):
            pass

        with mock.patch.object(ops_apply, "apply_csv",
                               side_effect=RuntimeError("boom")), \
             mock.patch("tg_userbot.notify.notify_user", fake_notify):
            asyncio.run(ops_apply.run_apply_and_notify("/a/c.csv", True))
        self.assertFalse(os.path.exists(ops_apply._mark_path()),
                         "异常也要清标记（否则重启误报中断）")

    def test_recover_interrupted_reads_and_clears(self):
        ops_apply._write_mark("/115open/x/c.csv", True)
        mark = ops_apply.recover_interrupted()
        self.assertEqual(mark["csv"], "/115open/x/c.csv")
        self.assertTrue(mark["full_group"])
        self.assertFalse(os.path.exists(ops_apply._mark_path()))
        # 无标记 → None；损坏标记 → None（清掉不炸）
        self.assertIsNone(ops_apply.recover_interrupted())
        open(ops_apply._mark_path(), "w").write("{bad json")
        self.assertIsNone(ops_apply.recover_interrupted())
        self.assertFalse(os.path.exists(ops_apply._mark_path()))
