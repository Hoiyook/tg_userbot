"""持久化下载队列的单元测试。

运行方式（在项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import asyncio
import atexit
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_queue_test_")
# 退出时回收临时目录（测试跑完就地删，别让 /var/folders 越堆越多）
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import state, config, queue  # noqa: E402
from tg_userbot import stats as stats_mod  # noqa: E402


def empty_queue():
    return {"tasks": [], "retry": []}


def media_record(**kw):
    rec = {
        "kind": "media",
        "chat_id": 123,
        "msg_id": 456,
        "source": "频道A",
        "label": "视频.mp4",
    }
    rec.update(kw)
    return rec


class QueuePersistenceTest(unittest.TestCase):
    """load_queue / save_queue：文件持久化往返。"""

    def setUp(self):
        self.path = os.path.join(_TMP, "test_queue.json")
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_missing_file_returns_empty(self):
        self.assertEqual(queue.load_queue(self.path), empty_queue())

    def test_corrupt_file_returns_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("不是 JSON")
        self.assertEqual(queue.load_queue(self.path), empty_queue())

    def test_roundtrip_preserves_order_and_fields(self):
        q = {
            "tasks": [media_record(chat_id=1), media_record(chat_id=2)],
            "retry": [media_record(chat_id=3, attempts=2)],
        }
        queue.save_queue(q, path=self.path)
        self.assertEqual(queue.load_queue(self.path), q)

    def test_save_queue_writes_json_structure(self):
        queue.save_queue(empty_queue(), path=self.path)
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data, {"tasks": [], "retry": []})


class QueueMutationTest(unittest.TestCase):
    """入队 / 失败流转 / 移除 / 重试流转。"""

    def setUp(self):
        self.q = empty_queue()

    def test_enqueue_appends_to_tasks_with_id(self):
        rec = queue.queue_enqueue(self.q, media_record())
        self.assertEqual(len(self.q["tasks"]), 1)
        self.assertEqual(rec["kind"], "media")
        self.assertTrue(rec.get("id"))
        self.assertEqual(rec.get("attempts", 0), 0)

    def test_enqueue_appends_to_end(self):
        queue.queue_enqueue(self.q, media_record(chat_id=1))
        queue.queue_enqueue(self.q, media_record(chat_id=2))
        self.assertEqual(
            [r["chat_id"] for r in self.q["tasks"]], [1, 2]
        )

    def test_fail_to_retry_moves_and_increments(self):
        rec = queue.queue_enqueue(self.q, media_record(chat_id=1))
        queue.queue_enqueue(self.q, media_record(chat_id=2))
        queue.queue_fail_to_retry(self.q, rec)
        self.assertEqual(
            [r["chat_id"] for r in self.q["tasks"]], [2]
        )
        self.assertEqual(len(self.q["retry"]), 1)
        self.assertEqual(self.q["retry"][0]["chat_id"], 1)
        self.assertEqual(self.q["retry"][0]["attempts"], 1)

    def test_fail_to_retry_unknown_record_is_noop(self):
        queue.queue_fail_to_retry(self.q, media_record(chat_id=999))
        self.assertEqual(self.q, empty_queue())

    def test_remove_by_index(self):
        rec1 = queue.queue_enqueue(self.q, media_record(chat_id=1))
        rec2 = queue.queue_enqueue(self.q, media_record(chat_id=2))
        ok, removed = queue.queue_remove(self.q, "tasks", 1)
        self.assertTrue(ok)
        self.assertEqual(removed["id"], rec1["id"])
        self.assertEqual([r["chat_id"] for r in self.q["tasks"]], [2])

    def test_remove_invalid_index_fails(self):
        queue.queue_enqueue(self.q, media_record())
        ok, removed = queue.queue_remove(self.q, "tasks", 5)
        self.assertFalse(ok)
        self.assertEqual(len(self.q["tasks"]), 1)

    def test_remove_from_empty_fails(self):
        ok, removed = queue.queue_remove(self.q, "tasks", 1)
        self.assertFalse(ok)

    def test_retry_success_removes_by_id(self):
        rec = queue.queue_enqueue(self.q, media_record())
        queue.queue_fail_to_retry(self.q, rec)
        queue.queue_retry_success(self.q, self.q["retry"][0])
        self.assertEqual(self.q["retry"], [])

    def test_retry_failed_keeps_position_and_increments(self):
        rec1 = queue.queue_enqueue(self.q, media_record(chat_id=1))
        rec2 = queue.queue_enqueue(self.q, media_record(chat_id=2))
        queue.queue_fail_to_retry(self.q, rec1)
        queue.queue_fail_to_retry(self.q, rec2)
        # 重试 retry 列表第 2 条（chat_id=2），失败 → 位置不变、attempts+1
        target = self.q["retry"][1]
        queue.queue_retry_failed(self.q, target)
        self.assertEqual(
            [r["chat_id"] for r in self.q["retry"]], [1, 2]
        )
        self.assertEqual(self.q["retry"][1]["attempts"], 2)


class EnqueueAndStartTest(unittest.IsolatedAsyncioTestCase):
    """enqueue_and_start：入队后必须用带 id 的记录触发执行（回归：KeyError 'id'）。"""

    async def asyncSetUp(self):
        state.QUEUE = {"tasks": [], "retry": []}
        state.QUEUE_LOCK = asyncio.Lock()

    def tearDown(self):
        state.QUEUE = {"tasks": [], "retry": []}
        state.QUEUE_LOCK = None

    async def test_spawned_record_has_id(self):
        captured = []

        async def fake_execute(record):
            captured.append(record)

        with mock.patch.object(queue, "save_queue"), mock.patch.object(
            queue, "execute_queued_task", side_effect=fake_execute
        ):
            await queue.enqueue_and_start({
                "kind": "media", "chat_id": 1, "msg_id": 2, "label": "x.mp4",
            })
        await asyncio.sleep(0)  # 让被 spawn 的任务执行

        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0].get("id"))
        # 队列文件中的记录与执行记录必须是同一个 id
        self.assertEqual(state.QUEUE["tasks"][0]["id"], captured[0]["id"])


class QueueDeadlockRegressionTest(unittest.IsolatedAsyncioTestCase):
    """回归：execute_queued_task 不能与 download_file 嵌套抢同一个信号量
    （信号量限 1 时，嵌套 acquire 会死锁，任务永远无法完成）。"""

    async def asyncSetUp(self):
        self.old_sem = state.DOWNLOAD_SEMAPHORE
        self.old_client = state.client
        self.old_queue = state.QUEUE
        self.old_lock = state.QUEUE_LOCK
        self.old_save = queue.save_queue
        state.DOWNLOAD_SEMAPHORE = config.AdjustableSemaphore(1)
        state.QUEUE_LOCK = asyncio.Lock()
        state.QUEUE = {"tasks": [], "retry": []}
        queue.save_queue = mock.MagicMock()  # 测试期间不写真实文件

    def tearDown(self):
        state.DOWNLOAD_SEMAPHORE = self.old_sem
        state.client = self.old_client
        state.QUEUE = self.old_queue
        state.QUEUE_LOCK = self.old_lock
        queue.save_queue = self.old_save

    async def test_media_task_completes_with_semaphore_limit_one(self):
        fake_file = mock.MagicMock()
        fake_file.name = "a.mp4"
        fake_file.size = 123
        fake_message = mock.MagicMock()
        fake_message.id = 1
        fake_message.file = fake_file
        fake_message.fwd_from = None
        fake_message.message = ""
        fake_message.media = None
        fake_message.photo = None
        fake_message.video = None
        fake_message.audio = None
        fake_message.voice = None
        fake_message.document = None

        async def fake_download_media(file=None, progress_callback=None):
            with open(file, "wb") as f:
                f.write(b"x")
            return file

        fake_message.download_media = fake_download_media

        fake_client = mock.MagicMock()
        fake_client.is_connected = lambda: True

        async def fake_get_messages(*args, **kwargs):
            return fake_message

        async def fake_send_message(*args, **kwargs):
            return None

        fake_client.get_messages = fake_get_messages
        fake_client.send_message = fake_send_message
        state.client = fake_client

        record = {
            "kind": "media", "chat_id": 1, "msg_id": 1,
            "source_override": None, "label": "a.mp4", "id": "test1",
        }
        queue.queue_enqueue(state.QUEUE, record)

        # 旧代码（execute 内层再拿信号量）会在 10 秒内死锁 → 超时失败
        await asyncio.wait_for(
            queue.execute_queued_task(record), timeout=10
        )

        self.assertEqual(state.QUEUE["tasks"], [])
        self.assertEqual(state.QUEUE["retry"], [])


class QueueTaskCancellationRegressionTest(unittest.IsolatedAsyncioTestCase):
    """回归（静默死亡 bug）：
    - 执行失败（download 重试耗尽返回 False）→ 任务必须落 retry 并持久化，绝不
      再像旧代码那样因 CancelledError 逃逸而永远卡在 tasks（「队列有任务
      /progress 却没数据」的根因）。
    - 真正的 Task.cancel()（进程退出等主动取消）→ 原样放行，记录留在 tasks 原位，
      重启后由 recover_queue_tasks 重跑 —— 不被误移入 retry。"""

    async def asyncSetUp(self):
        self.old_queue = state.QUEUE
        self.old_lock = state.QUEUE_LOCK
        self.old_exec = state.EXECUTING
        self.old_save = queue.save_queue
        state.QUEUE_LOCK = asyncio.Lock()
        state.QUEUE = {"tasks": [], "retry": []}
        state.EXECUTING = set()
        queue.save_queue = mock.MagicMock()  # 测试期间不写真实文件

    def tearDown(self):
        state.QUEUE = self.old_queue
        state.QUEUE_LOCK = self.old_lock
        state.EXECUTING = self.old_exec
        queue.save_queue = self.old_save

    async def test_failed_run_moves_record_to_retry(self):
        rec = queue.queue_enqueue(state.QUEUE, media_record(label="视频.mp4"))

        async def always_fail(record):
            return False  # download_file 重试耗尽后的真实返回值

        with mock.patch.object(queue, "_run_queued_task", side_effect=always_fail):
            await queue.execute_queued_task(rec)

        self.assertNotIn(
            rec["id"], [r["id"] for r in state.QUEUE["tasks"]],
        )
        self.assertIn(rec["id"], [r["id"] for r in state.QUEUE["retry"]])
        self.assertEqual(state.QUEUE["retry"][0]["attempts"], 1)
        queue.save_queue.assert_called()  # 落 retry 后必须持久化

    async def test_genuine_task_cancel_stays_in_tasks(self):
        rec = queue.queue_enqueue(state.QUEUE, media_record(label="视频.mp4"))
        started = asyncio.Event()

        async def hang(record):
            started.set()
            await asyncio.sleep(3600)

        with mock.patch.object(queue, "_run_queued_task", side_effect=hang):
            task = asyncio.create_task(queue.execute_queued_task(rec))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        # 真取消应放行：既不在 retry，也不该被当成失败改状态；仍留在 tasks 原位
        self.assertNotIn(
            rec["id"], [r["id"] for r in state.QUEUE["retry"]],
        )
        self.assertIn(rec["id"], [r["id"] for r in state.QUEUE["tasks"]])
        self.assertEqual(state.QUEUE["tasks"][0]["attempts"], 0)


class QueueDelegatedUrlTaskTest(unittest.IsolatedAsyncioTestCase):
    """kind=url 任务返回 "delegated"（直链失效已降级转交解析 bot）时的簿记。

    download_url_media 的直链过期防护在「刷新无效 → 链接已转解析 bot」时
    返回 truthy 字符串 "delegated"：任务必须按成功移除（不进 retry、不重放
    死链，bot 回复的视频走白名单流另入队）。本测试钉住 execute_queued_task
    的 truthiness 契约——防止将来把 `if success:` 收紧成 `if success is True:`
    时悄悄弄坏降级路径。characterization 测试（现有行为守卫，非新增行为）。
    """

    async def asyncSetUp(self):
        self.old_queue = state.QUEUE
        self.old_lock = state.QUEUE_LOCK
        self.old_exec = state.EXECUTING
        self.old_save = queue.save_queue
        state.QUEUE_LOCK = asyncio.Lock()
        state.QUEUE = {"tasks": [], "retry": []}
        state.EXECUTING = set()
        queue.save_queue = mock.MagicMock()

    def tearDown(self):
        state.QUEUE = self.old_queue
        state.QUEUE_LOCK = self.old_lock
        state.EXECUTING = self.old_exec
        queue.save_queue = self.old_save

    async def test_delegated_task_removed_from_tasks(self):
        rec = queue.queue_enqueue(state.QUEUE, {
            "kind": "url", "url": "https://v.douyin.com/x/", "label": "x",
        })

        async def delegated(record):
            return "delegated"

        with mock.patch.object(queue, "_run_queued_task", side_effect=delegated):
            await queue.execute_queued_task(rec)

        self.assertNotIn(rec["id"], [r["id"] for r in state.QUEUE["tasks"]])
        self.assertNotIn(rec["id"], [r["id"] for r in state.QUEUE["retry"]])

    async def test_delegated_task_removed_from_retry(self):
        """手动 /retry 重放的降级任务同样从 retry 列表移除（不再反复重放死链）。"""
        rec = queue.queue_enqueue(state.QUEUE, {
            "kind": "url", "url": "https://v.douyin.com/x/", "label": "x",
        })
        queue.queue_fail_to_retry(state.QUEUE, rec)

        async def delegated(record):
            return "delegated"

        with mock.patch.object(queue, "_run_queued_task", side_effect=delegated):
            await queue.execute_queued_task(rec)

        self.assertEqual(
            [r["id"] for r in state.QUEUE["retry"]],
            [],
        )


class QueueCancelRunningTest(unittest.IsolatedAsyncioTestCase):
    """在途任务取消：/queue del 命中执行中的任务时必须能真正中断下载。

    旧行为：/queue del 只删记录，字节继续跑完（白下几个 GB）。现在
    spawn_execute 按记录 id 登记任务句柄，cancel_running 定点 cancel；
    真取消沿 download_file/execute_queued_task 的既有链路收尾（清
    .download 半成品、归还 worker、记录原位），queue_del_task 再把记录
    从 tasks 移除。
    """

    async def asyncSetUp(self):
        self.old = (state.QUEUE, state.QUEUE_LOCK, state.EXECUTING)
        state.QUEUE = {"tasks": [], "retry": []}
        state.QUEUE_LOCK = asyncio.Lock()
        state.EXECUTING = set()
        queue._RUNNING_TASKS.clear()
        queue._SPAWNED_TASKS.clear()

    async def asyncTearDown(self):
        # 收尾：取消可能还挂着的假任务，恢复全局
        for t in list(queue._SPAWNED_TASKS):
            t.cancel()
        state.QUEUE, state.QUEUE_LOCK, state.EXECUTING = self.old
        queue._RUNNING_TASKS.clear()

    async def _spawn_hanging_task(self, rec):
        started = asyncio.Event()

        async def hang(record):
            started.set()
            await asyncio.sleep(3600)

        with mock.patch.object(queue, "execute_queued_task", side_effect=hang):
            task = queue.spawn_execute(rec)
        await started.wait()
        return task

    async def test_cancel_running_cancels_and_cleans_registry(self):
        rec = queue.queue_enqueue(state.QUEUE, media_record(label="x.mp4"))
        task = await self._spawn_hanging_task(rec)
        self.assertIn(rec["id"], queue._RUNNING_TASKS)

        self.assertTrue(queue.cancel_running(rec["id"]))
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn(rec["id"], queue._RUNNING_TASKS)  # done 回调清簿

    async def test_cancel_running_unknown_or_done_returns_false(self):
        self.assertFalse(queue.cancel_running("no-such-id"))

        rec = queue.queue_enqueue(state.QUEUE, media_record())
        done = asyncio.Event()

        async def instant(record):
            done.set()

        with mock.patch.object(queue, "execute_queued_task",
                               side_effect=instant):
            queue.spawn_execute(rec)
        await done.wait()
        await asyncio.sleep(0)  # 让 done 回调跑完
        self.assertFalse(queue.cancel_running(rec["id"]))  # 已结束不可取消

    async def test_queue_del_task_cancels_executing_and_removes(self):
        rec = queue.queue_enqueue(state.QUEUE, media_record(label="大文件.mp4"))
        task = await self._spawn_hanging_task(rec)
        # 假执行体不写 EXECUTING（真身才写），手动补上「执行中」语义
        state.EXECUTING.add(rec["id"])

        ok, removed, cancelled = await queue.queue_del_task(index=1)

        self.assertTrue(ok)
        self.assertEqual(removed["id"], rec["id"])
        self.assertTrue(cancelled)
        self.assertEqual(state.QUEUE["tasks"], [])  # 记录已移除
        # 取消是异步收尾：等任务真正结束后再断言句柄簿记已清
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn(rec["id"], queue._RUNNING_TASKS)

    async def test_queue_del_task_idle_just_removes(self):
        rec = queue.queue_enqueue(state.QUEUE, media_record(label="排队中.mp4"))

        ok, removed, cancelled = await queue.queue_del_task(index=1)

        self.assertTrue(ok)
        self.assertFalse(cancelled)  # 没在执行，无取消发生
        self.assertEqual(state.QUEUE["tasks"], [])

    async def test_queue_del_task_by_record_id(self):
        rec = queue.queue_enqueue(state.QUEUE, media_record())
        ok, removed, cancelled = await queue.queue_del_task(record_id=rec["id"])
        self.assertTrue(ok)
        self.assertFalse(cancelled)
        self.assertEqual(state.QUEUE["tasks"], [])

    async def test_queue_del_task_bad_index_fails(self):
        ok, removed, cancelled = await queue.queue_del_task(index=9)
        self.assertFalse(ok)
        self.assertIsNone(removed)
        self.assertFalse(cancelled)

    async def test_queue_del_task_logs_removal(self):
        """手动删除必须落日志：台账勾稽靠它计「移除」桶（此前完全无痕，
        收到的媒体被删后成功/待重试都数不到，台账永远差一口）。"""
        rec = queue.queue_enqueue(
            state.QUEUE, media_record(label="黑窟窿.mp4")
        )
        with mock.patch.object(queue, "logger") as mlog:
            await queue.queue_del_task(index=1)
        msgs = [c.args[0] for c in mlog.info.call_args_list if c.args]
        self.assertTrue(any("手动移除队列任务" in m for m in msgs))
        self.assertTrue(any("黑窟窿.mp4" in m for m in msgs))

        # 未在途（纯排队中）的删除同样落日志
        rec2 = queue.queue_enqueue(
            state.QUEUE, media_record(label="排队中.mp4")
        )
        with mock.patch.object(queue, "logger") as mlog:
            await queue.queue_del_task(record_id=rec2["id"])
        msgs = [c.args[0] for c in mlog.info.call_args_list if c.args]
        self.assertTrue(any("手动移除队列任务" in m for m in msgs))

    async def test_queue_del_task_skips_when_task_failed_in_between(self):
        """del 两段锁之间任务恰好收尾（失败转 retry）：不得记「移除」假账。

        竞态：del 在锁①找到记录后出锁，executor 恰在此间隙完成收尾把记录
        挪进 retry；del 的锁②必须发现记录已不在 tasks、按「已不存在」返回，
        绝不能照记「🗑 手动移除队列任务」——否则台账「移除」桶假账 +1，
        而记录其实躺在 retry 里。用打桩 cancel_running 在同步窗口内注入
        收尾，确定性复现该交错。
        """
        rec = queue.queue_enqueue(state.QUEUE, media_record(label="竞态.mp4"))
        state.EXECUTING.add(rec["id"])

        def finish_concurrently(record_id):
            # 模拟 executor 在 del 两段锁之间收尾：失败 → 挪进 retry
            for i, r in enumerate(state.QUEUE["tasks"]):
                if r.get("id") == record_id:
                    moved = state.QUEUE["tasks"].pop(i)
                    moved["attempts"] = moved.get("attempts", 0) + 1
                    state.QUEUE["retry"].append(moved)
                    break
            return False

        with mock.patch.object(queue, "cancel_running",
                               side_effect=finish_concurrently), \
                mock.patch.object(queue, "logger") as mlog:
            ok, removed, cancelled = await queue.queue_del_task(index=1)

        self.assertFalse(ok)  # 删除并没有发生
        self.assertIsNone(removed)
        self.assertFalse(cancelled)
        self.assertEqual(state.QUEUE["tasks"], [])
        self.assertEqual([r["id"] for r in state.QUEUE["retry"]], [rec["id"]])
        msgs = [c.args[0] for c in mlog.info.call_args_list if c.args]
        self.assertFalse(any("手动移除队列任务" in m for m in msgs))

    async def test_queue_del_task_skips_when_task_succeeded_in_between(self):
        """同上竞态的成功变体：任务在间隙内下载完成被 executor 移除，
        del 不得报「已移除」（文件其实已落盘，台账「成功+移除」双计）。"""
        rec = queue.queue_enqueue(state.QUEUE, media_record(label="竞成.mp4"))
        state.EXECUTING.add(rec["id"])

        def finish_concurrently(record_id):
            # 模拟 executor 收尾：成功 → 从 tasks 移除
            state.QUEUE["tasks"] = [
                r for r in state.QUEUE["tasks"] if r.get("id") != record_id
            ]
            return False

        with mock.patch.object(queue, "cancel_running",
                               side_effect=finish_concurrently), \
                mock.patch.object(queue, "logger") as mlog:
            ok, removed, _ = await queue.queue_del_task(index=1)

        self.assertFalse(ok)
        self.assertIsNone(removed)
        self.assertEqual(state.QUEUE["tasks"], [])
        msgs = [c.args[0] for c in mlog.info.call_args_list if c.args]
        self.assertFalse(any("手动移除队列任务" in m for m in msgs))

    async def test_queue_del_task_cancels_spawned_but_unregistered(self):
        """「已 spawn 未登记 EXECUTING」窗口：del 仍须能取消在途任务。

        EXECUTING 要到 execute_queued_task 协程首段运行才登记，spawn 到
        首段之间有个调度窗口；窗口内 del 只看 EXECUTING 会把在途任务误判
        成「未在途」而不取消，随后协程照常下载（记录却已被移除，失败也
        不再转 retry）。_RUNNING_TASKS 句柄在 spawn 时同步登记，在途判定
        交给 cancel_running 以句柄为准即关掉该窗口。假执行体不写
        EXECUTING（见 _spawn_hanging_task），正是窗口内 del 看到的状态。
        """
        rec = queue.queue_enqueue(state.QUEUE, media_record(label="窗口.mp4"))
        task = await self._spawn_hanging_task(rec)
        # 不补 EXECUTING —— 复现「句柄已登记、EXECUTING 未登记」的窗口状态

        ok, removed, cancelled = await queue.queue_del_task(index=1)

        self.assertTrue(ok)
        self.assertTrue(cancelled)  # 窗口内的在途任务也必须被取消
        self.assertEqual(state.QUEUE["tasks"], [])
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn(rec["id"], queue._RUNNING_TASKS)


class RetryAllTest(unittest.IsolatedAsyncioTestCase):
    """/retry all：重放待重试列表全部任务（执行中的跳过）。"""

    async def asyncSetUp(self):
        self.old = (state.QUEUE, state.QUEUE_LOCK, state.EXECUTING)
        state.QUEUE = {"tasks": [], "retry": []}
        state.QUEUE_LOCK = asyncio.Lock()
        state.EXECUTING = set()
        queue._SPAWNED_TASKS.clear()

    async def asyncTearDown(self):
        for t in list(queue._SPAWNED_TASKS):
            t.cancel()
        state.QUEUE, state.QUEUE_LOCK, state.EXECUTING = self.old
        queue._SPAWNED_TASKS.clear()

    async def test_retry_all_spawns_all_not_executing(self):
        spawned = []

        def fake_spawn(record):
            spawned.append(record)

        for i in range(3):
            rec = queue.queue_enqueue(
                state.QUEUE, media_record(label=f"r{i}.mp4")
            )
            queue.queue_fail_to_retry(state.QUEUE, rec)  # 内部自行 pop
        # 第 2 条假装在执行 → 跳过
        state.EXECUTING.add(state.QUEUE["retry"][1]["id"])

        with mock.patch.object(queue, "spawn_execute", fake_spawn):
            n = queue.retry_all()

        self.assertEqual(n, 2)
        self.assertEqual(len(spawned), 2)
        # 重放不改列表归属（成功/失败仍由 execute_queued_task 收尾处理）
        self.assertEqual(len(state.QUEUE["retry"]), 3)

    async def test_retry_all_empty_returns_zero(self):
        self.assertEqual(queue.retry_all(), 0)


class AutoReplayDueTest(unittest.IsolatedAsyncioTestCase):
    """retry 榜到期自动重放：指数退避 + 自动重试次数上限 + 空闲 worker 预算。"""

    async def asyncSetUp(self):
        self.old = (state.QUEUE, state.QUEUE_LOCK, state.EXECUTING,
                    state.DOWNLOAD_WORKER_QUEUE, state.DOWNLOAD_CONCURRENCY)
        state.QUEUE = {"tasks": [], "retry": []}
        state.QUEUE_LOCK = asyncio.Lock()
        state.EXECUTING = set()
        state.DOWNLOAD_WORKER_QUEUE = None
        state.DOWNLOAD_CONCURRENCY = 3
        queue._SPAWNED_TASKS.clear()
        self.spawned = []

    async def asyncTearDown(self):
        for t in list(queue._SPAWNED_TASKS):
            t.cancel()
        (state.QUEUE, state.QUEUE_LOCK, state.EXECUTING,
         state.DOWNLOAD_WORKER_QUEUE, state.DOWNLOAD_CONCURRENCY) = self.old
        queue._SPAWNED_TASKS.clear()

    def _to_retry(self, label, attempts=None, due=None):
        """造一条停在 retry 榜里的记录，可控 attempts 与 next_retry_at。"""
        rec = queue.queue_enqueue(state.QUEUE, media_record(label=label))
        queue.queue_fail_to_retry(state.QUEUE, rec)
        rec = state.QUEUE["retry"][-1]
        if attempts is not None:
            rec["attempts"] = attempts
        rec.pop("next_retry_at", None)
        if due is not None:
            rec["next_retry_at"] = due
        return rec

    def _set_idle_workers(self, n):
        q = asyncio.Queue()
        for _ in range(n):
            q.put_nowait(object())
        state.DOWNLOAD_WORKER_QUEUE = q

    def _replay(self, **kw):
        with mock.patch.object(queue, "spawn_execute",
                               lambda r: self.spawned.append(r)):
            return queue.replay_due(**kw)

    # ---------- 退避 ----------

    def test_backoff_sequence_doubles_then_caps(self):
        f = queue._backoff_delay
        base, cap = config.AUTO_RETRY_BASE_DELAY, config.AUTO_RETRY_MAX_DELAY
        self.assertEqual(f(1), base)
        self.assertEqual(f(2), base * 2)
        self.assertEqual(f(3), base * 4)
        self.assertEqual(f(4), base * 8)
        self.assertEqual(f(99), cap)   # 封顶，不溢出
        self.assertLessEqual(f(5), cap)

    def test_fail_to_retry_stamps_next_retry_at(self):
        rec = queue.queue_enqueue(state.QUEUE, media_record())
        queue.queue_fail_to_retry(state.QUEUE, rec)
        moved = state.QUEUE["retry"][-1]
        self.assertEqual(moved["attempts"], 1)
        self.assertIsNotNone(moved.get("next_retry_at"))
        # 首次失败 → 退避 base，而不是立即到期
        self.assertGreater(moved["next_retry_at"], time.time())

    def test_retry_failed_refreshes_next_retry_at(self):
        rec = self._to_retry("a.mp4", attempts=1)
        rec["next_retry_at"] = 0.0            # 假装已到期
        queue.queue_retry_failed(state.QUEUE, rec)
        self.assertEqual(rec["attempts"], 2)
        self.assertGreater(rec["next_retry_at"], time.time())

    # ---------- 到期判定 ----------

    def test_not_yet_due_is_skipped(self):
        self._set_idle_workers(5)
        self._to_retry("a.mp4", attempts=1, due=2000.0)
        self.assertEqual(self._replay(now=1000.0), 0)
        self.assertEqual(self.spawned, [])

    def test_due_is_replayed(self):
        self._set_idle_workers(5)
        self._to_retry("a.mp4", attempts=1, due=500.0)
        self.assertEqual(self._replay(now=1000.0), 1)
        self.assertEqual(len(self.spawned), 1)

    def test_missing_next_retry_at_counts_as_due(self):
        """旧记录（本功能上线前入的榜）没有该字段 → 视为到期，重启即自愈。"""
        self._set_idle_workers(5)
        self._to_retry("old.mp4", attempts=1)      # 不写 due
        self.assertNotIn("next_retry_at", state.QUEUE["retry"][0])
        self.assertEqual(self._replay(now=1000.0), 1)

    def test_executing_records_are_skipped(self):
        self._set_idle_workers(5)
        rec = self._to_retry("a.mp4", attempts=1, due=500.0)
        state.EXECUTING.add(rec["id"])
        self.assertEqual(self._replay(now=1000.0), 0)

    # ---------- 自动重试次数上限 ----------

    def test_stops_auto_replay_past_attempt_cap(self):
        self._set_idle_workers(9)
        cap = config.AUTO_RETRY_MAX_TIMES
        self._to_retry("at-cap.mp4", attempts=cap, due=500.0)       # 仍可放
        self._to_retry("over-cap.mp4", attempts=cap + 1, due=500.0)  # 超限不放
        self.assertEqual(self._replay(now=1000.0), 1)
        self.assertEqual(self.spawned[0]["label"], "at-cap.mp4")
        # 超限任务仍留在榜上，等人工 /retry
        self.assertEqual(len(state.QUEUE["retry"]), 2)

    def test_manual_retry_all_ignores_backoff_and_cap(self):
        """手动路径不受退避与上限约束（退避只约束自动路径）。"""
        self._to_retry("over-cap.mp4", attempts=99, due=9e9)
        with mock.patch.object(queue, "spawn_execute",
                               lambda r: self.spawned.append(r)):
            n = queue.retry_all()
        self.assertEqual(n, 1)
        self.assertEqual(len(self.spawned), 1)

    # ---------- 自动重放事件（供 Reporter 与用户区分「自动」与「手动」）----------

    def _ev_file(self):
        path = os.path.join(_TMP, "task_events_autoreplay_test.jsonl")
        if os.path.exists(path):
            os.remove(path)
        return path

    def test_replay_due_emits_auto_replay_event_per_task(self):
        """自动放行必须留事件痕：否则事件流里分不清「自动重放」和「手动 /retry」，
        面板与通知都只能显示 --。"""
        self._set_idle_workers(5)
        r1 = self._to_retry("a.mp4", attempts=1, due=500.0)
        r2 = self._to_retry("b.mp4", attempts=1, due=500.0)
        path = self._ev_file()
        try:
            with mock.patch.object(stats_mod, "TASK_EVENTS_FILE", path):
                n = self._replay(now=1000.0)
            auto = [e for e in stats_mod.load_events(path)
                    if e.get("ev") == "AUTO_REPLAY"]
        finally:
            if os.path.exists(path):
                os.remove(path)
        self.assertEqual(n, 2)
        self.assertEqual({e["id"] for e in auto}, {r1["id"], r2["id"]})
        self.assertTrue(all(e.get("label") for e in auto))

    def test_manual_retry_all_emits_no_auto_replay_event(self):
        """手动路径不得打上自动重放标记（否则统计与通知会把人工重放算成自动）。"""
        self._to_retry("a.mp4", attempts=1, due=9e9)
        path = self._ev_file()
        try:
            with mock.patch.object(stats_mod, "TASK_EVENTS_FILE", path), \
                    mock.patch.object(queue, "spawn_execute",
                                      lambda r: self.spawned.append(r)):
                queue.retry_all()
            auto = [e for e in stats_mod.load_events(path)
                    if e.get("ev") == "AUTO_REPLAY"]
        finally:
            if os.path.exists(path):
                os.remove(path)
        self.assertEqual(auto, [])

    # ---------- 空闲 worker 预算 ----------

    def test_round_capped_by_idle_worker_count(self):
        self._set_idle_workers(2)
        for i in range(5):
            self._to_retry(f"r{i}.mp4", attempts=1, due=500.0)
        self.assertEqual(self._replay(now=1000.0), 2)   # 只放空闲的 2 条
        self.assertEqual(len(state.QUEUE["retry"]), 5)  # 其余留榜待下轮

    def test_zero_idle_workers_replays_nothing(self):
        self._set_idle_workers(0)
        self._to_retry("a.mp4", attempts=1, due=500.0)
        self.assertEqual(self._replay(now=1000.0), 0)
        self.assertEqual(self.spawned, [])

    def test_pool_disabled_falls_back_to_concurrency(self):
        """池被禁用（spawn 全失败）时回落到并发上限，而不是永不自动重放。"""
        state.DOWNLOAD_WORKER_QUEUE = None
        state.DOWNLOAD_CONCURRENCY = 2
        for i in range(4):
            self._to_retry(f"r{i}.mp4", attempts=1, due=500.0)
        self.assertEqual(self._replay(now=1000.0), 2)


class QueuePaginationTest(unittest.TestCase):
    """重试/队列列表分页：60 条会超 Telegram 4096 上限导致回复发不出
    （「点重试列表没数据返回」的根因），按页渲染 + 页头统计。序号保持
    全局（第 2 页从 11 起），/retry <n> 的序号语义不变。"""

    def _q(self, n, key="retry"):
        q = empty_queue()
        for i in range(n):
            q[key].append(media_record(label=f"任务{i}.mp4"))
        return q

    def test_retry_text_paginated_with_stats_header(self):
        text = queue.format_retry_text(self._q(12), page=1)
        self.assertIn("共 12 条", text)
        self.assertIn("第 1/2 页", text)
        self.assertIn("\n1. ", text)
        self.assertIn("\n10. ", text)
        self.assertNotIn("\n11. ", text)  # 只渲染本页
        self.assertLess(len(text), 4096)

    def test_retry_text_page2_global_numbering(self):
        text = queue.format_retry_text(self._q(12), page=2)
        self.assertIn("第 2/2 页", text)
        self.assertIn("\n11. ", text)
        self.assertIn("\n12. ", text)
        self.assertNotIn("\n1. ", text)  # "1. " 是 "11. " 的子串，须带行首

    def test_retry_text_page_clamped(self):
        text = queue.format_retry_text(self._q(12), page=99)
        self.assertIn("第 2/2 页", text)

    def test_retry_text_empty_unchanged(self):
        self.assertIn("空", queue.format_retry_text(empty_queue()))

    def test_queue_text_paginated_too(self):
        """队列列表同样分页（39 条大队列时同样会撞 4096）。"""
        text = queue.format_queue_text(self._q(25, key="tasks"), page=1)
        self.assertIn("共 25 条", text)
        self.assertIn("第 1/3 页", text)
        self.assertLess(len(text), 4096)


class QueueFormatTest(unittest.TestCase):
    """队列/待重试列表文本格式化。"""

    def test_queue_text_empty(self):
        self.assertIn("空", queue.format_queue_text(empty_queue()))

    def test_queue_text_entries(self):
        q = empty_queue()
        queue.queue_enqueue(q, media_record(label="视频.mp4"))
        text = queue.format_queue_text(q)
        self.assertIn("视频.mp4", text)
        self.assertIn("[媒体]", text)

    def test_retry_text_shows_attempts(self):
        q = empty_queue()
        rec = queue.queue_enqueue(
            q, {"kind": "douyin", "url": "https://v.douyin.com/x/",
                "label": "https://v.douyin.com/x/"}
        )
        queue.queue_fail_to_retry(q, rec)
        text = queue.format_retry_text(q)
        self.assertIn("v.douyin.com", text)
        self.assertIn("尝试", text)
        self.assertIn("1", text)

    def test_retry_text_empty(self):
        self.assertIn("空", queue.format_retry_text(empty_queue()))

    def test_long_final_name_truncated_to_tail(self):
        """超长 final_name（相册长 caption 名）截到 48 字符、保留尾部——
        区分性最强的原文件名在后半段；约 27 条 250 字符长名就会撞 Telegram
        4096 字符上限（2026-09-07 实测 20 条 ≈ 3041 字符），截断后列表
        恒在限内且更易读。"""
        long_name = ("26-07-08 作者：#腿玩年_期数：bl208_角色：#达妮娅 "
                     "文件大小：1297M_i站视频预览地址_布料【 https___www 】"
                     " - bl208布料4k【达妮娅】可愛くてごめん.mp4")
        self.assertGreater(len(long_name), 48)
        q = empty_queue()
        queue.queue_enqueue(q, {
            "kind": "media", "chat_id": 987654321, "msg_id": 1,
            "final_name": long_name, "label": long_name,
        })
        text = queue.format_queue_text(q)
        self.assertNotIn(long_name, text)          # 全名不再出现
        self.assertIn("…", text)                    # 有省略号标记
        self.assertIn("可愛くてごめん.mp4", text)    # 尾部（原文件名）保留
        self.assertLess(len(text), 4096)            # 单条也远在限内

    def test_short_final_name_untouched(self):
        """守卫：短名不截断、不加省略号。"""
        q = empty_queue()
        queue.queue_enqueue(q, {
            "kind": "media", "chat_id": 987654321, "msg_id": 1,
            "final_name": "a.mp4", "label": "a.mp4",
        })
        text = queue.format_queue_text(q)
        self.assertIn("a.mp4", text)
        self.assertNotIn("…", text)


class TaskEventEmissionTest(unittest.IsolatedAsyncioTestCase):
    """台账事件发点：队列层在生命周期转换处发事件，同一 task 全程同 id。

    一个逻辑下载任务有且只有一个 task_id（= record id）；retry 重放、
    手动重试都沿用原 id；白名单媒体只在入队咽喉处产生一次 QUEUED。
    """

    async def asyncSetUp(self):
        self.old = (state.QUEUE, state.QUEUE_LOCK, state.EXECUTING,
                    state.client, state.MY_ID)
        state.QUEUE = {"tasks": [], "retry": []}
        state.QUEUE_LOCK = asyncio.Lock()
        state.EXECUTING = set()
        state.MY_ID = 111
        state.QUEUE = {"tasks": [], "retry": []}

        self.notified = []

        async def fake_send(*args, **kwargs):
            self.notified.append(args)

        fake_client = mock.MagicMock()
        fake_client.send_message = fake_send
        state.client = fake_client

        queue._RUNNING_TASKS.clear()
        queue._SPAWNED_TASKS.clear()
        self.ev_file = os.path.join(_TMP, "task_events_queue_test.jsonl")
        if os.path.exists(self.ev_file):
            os.remove(self.ev_file)

    async def asyncTearDown(self):
        for t in list(queue._SPAWNED_TASKS):
            t.cancel()
        (state.QUEUE, state.QUEUE_LOCK, state.EXECUTING,
         state.client, state.MY_ID) = self.old
        queue._RUNNING_TASKS.clear()
        if os.path.exists(self.ev_file):
            os.remove(self.ev_file)

    def _events(self):
        return stats_mod.load_events(self.ev_file)

    async def test_enqueue_emits_received_and_queued_once(self):
        rec = media_record(chat_id=111, label="收藏.mp4")
        with mock.patch.object(stats_mod, "TASK_EVENTS_FILE", self.ev_file):
            await queue.enqueue_and_start(rec)
            await asyncio.sleep(0)  # 让执行任务首段跑完（RUNNING）
        events = self._events()
        names = [e["ev"] for e in events]
        # 收藏夹直发：RECEIVED+QUEUED 各一次（白名单中转同样只入队一次）
        self.assertEqual(names.count("RECEIVED"), 1)
        self.assertEqual(names.count("QUEUED"), 1)
        self.assertEqual(names[1], "QUEUED")
        self.assertEqual(events[1].get("kind"), "media")
        self.assertEqual(events[0].get("src"), "me")
        # 全部事件同一个 task_id
        ids = {e.get("id") for e in events}
        self.assertEqual(len(ids), 1)

    async def test_whitelist_enqueue_marks_wl_source(self):
        rec = media_record(chat_id=222, label="中转.mp4")
        with mock.patch.object(stats_mod, "TASK_EVENTS_FILE", self.ev_file):
            await queue.enqueue_and_start(rec)
            await asyncio.sleep(0)
        self.assertEqual(self._events()[0].get("src"), "wl")

    async def test_retry_failures_keep_same_task_id(self):
        rec = media_record(chat_id=111, label="重试.mp4")
        calls = {"n": 0}

        async def flaky(record):
            calls["n"] += 1
            return calls["n"] >= 3  # 前两次失败，第三次成功

        with mock.patch.object(stats_mod, "TASK_EVENTS_FILE", self.ev_file), \
                mock.patch.object(queue, "_run_queued_task", side_effect=flaky):
            await queue.enqueue_and_start(rec)
            while calls["n"] < 1:
                await asyncio.sleep(0.01)
            # 失败转入 retry 列表；/retry <n> / 菜单 ▶️ 手动重放同一记录
            retried = state.QUEUE["retry"][0]
            queue.spawn_execute(retried)
            while calls["n"] < 2:
                await asyncio.sleep(0.01)
            queue.spawn_execute(state.QUEUE["retry"][0])
            while calls["n"] < 3:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)

        events = self._events()
        names = [e["ev"] for e in events]
        # RUNNING×3 → (RETRY+FAILED)*2；SUCCESS 由下载层发（假执行体没有）
        self.assertEqual(names.count("RUNNING"), 3)
        self.assertEqual(names.count("RETRY"), 2)
        self.assertEqual(names.count("FAILED"), 2)
        # retry 重放全程同一个 task_id，没有被统计成新任务
        self.assertEqual(len({e.get("id") for e in events}), 1)
        retries = [e for e in events if e["ev"] == "RETRY"]
        self.assertEqual(retries[-1].get("attempts"), 2)
        # 成功收尾：记录已从 retry 列表移除
        self.assertEqual(state.QUEUE["retry"], [])

    async def test_del_emits_cancelled_or_removed(self):
        started = asyncio.Event()

        async def hang(record):
            started.set()
            await asyncio.sleep(3600)

        rec = media_record(chat_id=111, label="取消.mp4")
        with mock.patch.object(stats_mod, "TASK_EVENTS_FILE", self.ev_file), \
                mock.patch.object(queue, "execute_queued_task", side_effect=hang):
            # 真实路径：入队咽喉分配 id 副本后再 spawn
            queue.spawn_execute(queue.queue_enqueue(state.QUEUE, rec))
            await started.wait()
            ok, _, cancelled = await queue.queue_del_task(index=1)
        self.assertTrue(ok)
        self.assertTrue(cancelled)
        self.assertIn(("CANCELLED" in [e["ev"] for e in self._events()]), [True])

        # 未在途删除 → REMOVED(manual)
        rec2 = media_record(chat_id=111, label="手动删.mp4")
        with mock.patch.object(stats_mod, "TASK_EVENTS_FILE", self.ev_file):
            queue.queue_enqueue(state.QUEUE, rec2)
            ok2, _, cancelled2 = await queue.queue_del_task(index=1)
        self.assertTrue(ok2)
        self.assertFalse(cancelled2)
        removed = [e for e in self._events() if e["ev"] == "REMOVED"]
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0].get("why"), "manual")

    async def test_source_message_deleted_emits_removed(self):
        rec = media_record(chat_id=111, label="没源.mp4")

        async def fake_get_messages(*args, **kwargs):
            return None

        state.client.get_messages = fake_get_messages
        with mock.patch.object(stats_mod, "TASK_EVENTS_FILE", self.ev_file):
            ok = await queue._run_queued_task(rec)
        self.assertTrue(ok)
        removed = [e for e in self._events() if e["ev"] == "REMOVED"]
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0].get("why"), "source_deleted")


if __name__ == "__main__":
    unittest.main()
