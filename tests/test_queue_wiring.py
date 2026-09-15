"""queue.py 的 SQLite write-through 接线与启动迁移导入测试。

任务书：docs/plan/Userbot_下载队列SQLite化_DeepSeek任务书.md §11 测试 5-8。
持久化层本体（表 + 函数）在 tests/test_queue_db.py。

    .venv/bin/python -m unittest tests.test_queue_wiring -v
"""
import asyncio
import inspect
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录（config import 期
# 有真实副作用，见 test_config 的基座说明）。
_TMP = tempfile.mkdtemp(prefix="tg_userbot_queue_wiring_test_")
import atexit  # noqa: E402
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import commands  # noqa: E402
from tg_userbot import bot  # noqa: E402
from tg_userbot import config  # noqa: E402
from tg_userbot import queue as queue_mod  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402
from tg_userbot import state  # noqa: E402


def _record(rid="a1b2c3d4e5f60718293a4b5c6d7e8f90", **extra):
    rec = {
        "id": rid, "kind": "media", "label": "测试 标注.mp4",
        "chat_id": -1001234567890, "msg_id": 1,
        "dedup_keys": ["tg:ABC"],
    }
    rec.update(extra)
    return rec


class _SqliteModeBase(unittest.IsolatedAsyncioTestCase):
    """sqlite 模式基座：临时 DB + RUNTIME_DB_READY=True + 干净队列。"""

    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="qwiring_", dir=_TMP)
        self.db_path = os.path.join(self.dir, "tg_userbot.db")
        queue_file = os.path.join(self.dir, "download_queue.json")
        self._patches = [
            mock.patch.object(config, "RUNTIME_DB_FILE", self.db_path),
            mock.patch.object(config, "QUEUE_STORE", "sqlite"),
            mock.patch.object(config, "QUEUE_FILE", queue_file),
            # queue.py 经 from-import 绑定了 QUEUE_FILE（import 期值，生产恒定），
            # 测试必须同时 patch 模块属性才对 save_queue/load_queue 生效
            mock.patch.object(queue_mod, "QUEUE_FILE", queue_file),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        self._old = (state.QUEUE, state.QUEUE_LOCK, state.EXECUTING,
                     state.RUNTIME_DB_READY)
        state.QUEUE = {"tasks": [], "retry": []}
        state.QUEUE_LOCK = asyncio.Lock()
        state.EXECUTING = set()
        state.RUNTIME_DB_READY = True
        self.addCleanup(self._restore)

    def _restore(self):
        state.QUEUE, state.QUEUE_LOCK, state.EXECUTING, \
            state.RUNTIME_DB_READY = self._old

    def _db_ids(self, state_=None):
        rows = runtime_db.queue_load_all()
        if state_ is not None:
            rows = [r for r in rows if r["__state"] == state_]
        return [r["id"] for r in rows]


class TestMutationWiring(_SqliteModeBase):
    """5 个突变点在 sqlite 模式下落到 DB；内存字典语义保持不变。"""

    async def test_enqueue_and_start_persists_row(self):
        captured = []

        async def fake_execute(record):
            captured.append(record)

        with mock.patch.object(queue_mod, "execute_queued_task",
                               side_effect=fake_execute):
            await queue_mod.enqueue_and_start(_record(), src="me")
        await asyncio.sleep(0)

        self.assertEqual(self._db_ids("QUEUED"), [_record()["id"]])
        # 内存与 DB 是同一个 task_id；payload round-trip（label 等无损）
        row = runtime_db.queue_load_all()[0]
        self.assertEqual(row["label"], _record()["label"])
        self.assertEqual(row["dedup_keys"], ["tg:ABC"])
        self.assertEqual(captured[0]["id"], _record()["id"])

    async def test_success_deletes_row(self):
        record = _record()
        state.QUEUE["tasks"].append(record)
        runtime_db.queue_insert(record, "QUEUED")
        with mock.patch.object(queue_mod, "_run_queued_task",
                               return_value=True):
            await queue_mod.execute_queued_task(record)
        self.assertEqual(state.QUEUE["tasks"], [])
        self.assertEqual(self._db_ids(), [])

    async def test_failure_moves_row_to_retry(self):
        record = _record()
        state.QUEUE["tasks"].append(record)
        runtime_db.queue_insert(record, "QUEUED")
        with mock.patch.object(queue_mod, "_run_queued_task",
                               return_value=False):
            await queue_mod.execute_queued_task(record)
        self.assertEqual(state.QUEUE["tasks"], [])
        self.assertEqual(len(state.QUEUE["retry"]), 1)
        self.assertEqual(record["attempts"], 1)
        self.assertGreater(record["next_retry_at"], 0)
        row = runtime_db.queue_load_all()[0]
        self.assertEqual(row["__state"], "RETRY")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["next_retry_at"], record["next_retry_at"])

    async def test_retry_failed_again_updates_in_place(self):
        # 首败后的形态：内存记录 attempts=1（内存是权威，DB 与之同步）
        record = _record(attempts=1, next_retry_at=10.0)
        state.QUEUE["retry"].append(record)
        runtime_db.queue_insert(record, "RETRY")
        before_seq = runtime_db.queue_load_all()[0]["__seq"]
        with mock.patch.object(queue_mod, "_run_queued_task",
                               return_value=False):
            await queue_mod.execute_queued_task(record)
        row = runtime_db.queue_load_all()[0]
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["__state"], "RETRY")
        self.assertEqual(row["__seq"], before_seq)     # 原位，不追加
        self.assertEqual(state.QUEUE["retry"], [record])

    async def test_retry_success_deletes_row(self):
        record = _record()
        state.QUEUE["retry"].append(record)
        runtime_db.queue_insert(record, "RETRY")
        with mock.patch.object(queue_mod, "_run_queued_task",
                               return_value=True):
            await queue_mod.execute_queued_task(record)
        self.assertEqual(state.QUEUE["retry"], [])
        self.assertEqual(self._db_ids(), [])

    async def test_queue_del_task_deletes_row(self):
        record = _record()
        state.QUEUE["tasks"].append(record)
        runtime_db.queue_insert(record, "QUEUED")
        ok, _removed, cancelled = await queue_mod.queue_del_task(
            record_id=record["id"])
        self.assertTrue(ok)
        self.assertFalse(cancelled)
        self.assertEqual(state.QUEUE["tasks"], [])
        self.assertEqual(self._db_ids(), [])


class TestJsonSwitch(_SqliteModeBase):
    """测试 7：TG_QUEUE_STORE=json 一键回滚——全程不碰 DB。"""

    async def test_json_mode_never_touches_db(self):
        async def fake_execute(record):
            pass

        with mock.patch.object(config, "QUEUE_STORE", "json"), \
                mock.patch.object(queue_mod, "execute_queued_task",
                                  side_effect=fake_execute), \
                mock.patch.object(runtime_db, "queue_insert") as fake_ins, \
                mock.patch.object(queue_mod, "save_queue") as fake_save:
            await queue_mod.enqueue_and_start(_record())
        await asyncio.sleep(0)
        fake_ins.assert_not_called()
        fake_save.assert_called()          # 旧路径照常
        self.assertEqual(len(state.QUEUE["tasks"]), 1)
        self.assertEqual(runtime_db.queue_count(),
                         {"queued": 0, "retry": 0})    # DB 一行未增

    def test_persist_routes_to_save_queue_in_json_mode(self):
        record = _record()
        state.QUEUE["tasks"].append(record)
        with mock.patch.object(config, "QUEUE_STORE", "json"), \
                mock.patch.object(queue_mod, "save_queue") as fake_save, \
                mock.patch.object(runtime_db, "queue_delete") as fake_del:
            queue_mod._save_after_mutation(record, "delete")
        fake_save.assert_called_once()
        fake_del.assert_not_called()


async def _async_noop(*a, **k):
    return None


class TestDegradation(_SqliteModeBase):
    """测试 8：write-through 失败 → 仅告警，内存权威，绝不抛给下载链路。"""

    async def test_db_failure_rolls_back_and_refuses(self):
        """P0-1：DB 写失败 → 回滚内存、不 spawn、通知，返回 False。

        旧契约（内存为准继续执行）会让「执行过的任务」在崩溃后蒸发——
        2026-09-15 任务书判为 P0。新契约：未被持久化的任务绝不执行；
        任务本体仍在 Telegram（收藏夹原件/源消息），上游有恢复路径。"""
        record = _record()
        spawned = []

        async def fake_spawn(rec):
            spawned.append(rec)

        with mock.patch.object(runtime_db, "queue_insert",
                               side_effect=runtime_db.DbUnavailable("炸了")), \
                mock.patch.object(queue_mod, "spawn_execute",
                                  side_effect=fake_spawn), \
                mock.patch("tg_userbot.notify.notify_user",
                           side_effect=_async_noop), \
                self.assertLogs("tg_userbot", level="ERROR"):
            ok = await queue_mod.enqueue_and_start(record)
        await asyncio.sleep(0)
        self.assertFalse(ok, "持久化失败必须返回 False（未入队）")
        self.assertEqual(len(state.QUEUE["tasks"]), 0, "内存必须回滚")
        self.assertEqual(self._db_ids(), [])
        self.assertEqual(spawned, [], "未持久化的任务绝不允许执行")

    def test_load_falls_back_to_json_when_db_not_ready(self):
        state.RUNTIME_DB_READY = False
        with open(config.QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump({"tasks": [_record()], "retry": []}, f)
        q = queue_mod.load_queue_any()
        self.assertEqual([r["id"] for r in q["tasks"]], [_record()["id"]])
        self.assertTrue(os.path.exists(config.QUEUE_FILE))  # 未被动过


class TestStartupImport(_SqliteModeBase):
    """测试 5/6：旧 JSON 一次性导入 → .imported 改名；DB 有行时 DB 赢。"""

    def _write_json(self, tasks, retry):
        with open(config.QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump({"tasks": tasks, "retry": retry}, f, ensure_ascii=False)

    def test_one_time_import(self):
        t1 = _record("b" * 32, label="队列里的.mp4",
                     future_field={"未知": "字段"})
        r1 = _record("c" * 32, attempts=2, next_retry_at=123.0)
        self._write_json([t1], [r1])
        q = queue_mod.load_queue_any()
        self.assertEqual([r["id"] for r in q["tasks"]], [t1["id"]])
        self.assertEqual([r["id"] for r in q["retry"]], [r1["id"]])
        self.assertEqual(runtime_db.queue_count(),
                         {"queued": 1, "retry": 1})
        # 字段无损（含未知字段）；retry 的次数与到期时间进列
        row = [r for r in runtime_db.queue_load_all()
               if r["id"] == r1["id"]][0]
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["next_retry_at"], 123.0)
        row_t = [r for r in runtime_db.queue_load_all()
                 if r["id"] == t1["id"]][0]
        self.assertEqual(row_t["future_field"], {"未知": "字段"})
        # 旧 JSON 只改名不删除
        self.assertFalse(os.path.exists(config.QUEUE_FILE))
        self.assertTrue(os.path.exists(config.QUEUE_FILE + ".imported"))
        with open(config.QUEUE_FILE + ".imported", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data["tasks"]), 1)

    def test_import_idempotent(self):
        self._write_json([_record("b" * 32)], [])
        queue_mod.load_queue_any()
        q = queue_mod.load_queue_any()      # 第二次：从 DB 装载，不重复导入
        self.assertEqual([r["id"] for r in q["tasks"]], ["b" * 32])
        self.assertEqual(runtime_db.queue_count(), {"queued": 1, "retry": 0})

    def test_db_wins_on_conflict(self):
        runtime_db.queue_insert(_record("d" * 32, label="DB 里的"), "QUEUED")
        self._write_json([_record("e" * 32, label="旧 JSON 的")], [])
        with self.assertLogs("tg_userbot", level="WARNING"):
            q = queue_mod.load_queue_any()
        self.assertEqual([r["id"] for r in q["tasks"]], ["d" * 32])
        self.assertEqual(runtime_db.queue_count(), {"queued": 1, "retry": 0})
        self.assertTrue(os.path.exists(config.QUEUE_FILE + ".imported"))

    def test_empty_json_no_import(self):
        self._write_json([], [])
        q = queue_mod.load_queue_any()
        self.assertEqual(q, {"tasks": [], "retry": []})
        self.assertFalse(os.path.exists(config.QUEUE_FILE + ".imported"))

    def test_json_mode_loads_file_without_touching_db(self):
        self._write_json([_record("f" * 32)], [])
        with mock.patch.object(config, "QUEUE_STORE", "json"):
            q = queue_mod.load_queue_any()
        self.assertEqual([r["id"] for r in q["tasks"]], ["f" * 32])
        self.assertEqual(runtime_db.queue_count(),
                         {"queued": 0, "retry": 0})
        self.assertTrue(os.path.exists(config.QUEUE_FILE))  # 不改名


class TestCallSiteContract(unittest.TestCase):
    """源码契约：commands/bot 的队列突变点必须走统一持久化选择器，
    不许再出现裸 save_queue（防未来新增突变点漏接线）。"""

    def test_no_bare_save_queue_outside_queue_py(self):
        for mod in (commands, bot):
            src = inspect.getsource(mod)
            self.assertNotIn(
                "queue.save_queue", src,
                f"{mod.__name__} 里出现裸 queue.save_queue——队列突变必须走 "
                f"queue._save_after_mutation（sqlite/json 双模式）")


if __name__ == "__main__":
    unittest.main()
