"""持久化下载队列的单元测试。

运行方式（在项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_queue_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import state, config, queue  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
