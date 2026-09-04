"""持久化下载队列的单元测试。

运行方式（在项目根目录）：
    .venv/bin/python -m unittest test_queue -v
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

# 必须在导入 tg_userbot_final 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_queue_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tg_userbot_final as tg  # noqa: E402


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
        self.assertEqual(tg.load_queue(self.path), empty_queue())

    def test_corrupt_file_returns_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("不是 JSON")
        self.assertEqual(tg.load_queue(self.path), empty_queue())

    def test_roundtrip_preserves_order_and_fields(self):
        queue = {
            "tasks": [media_record(chat_id=1), media_record(chat_id=2)],
            "retry": [media_record(chat_id=3, attempts=2)],
        }
        tg.save_queue(queue, path=self.path)
        self.assertEqual(tg.load_queue(self.path), queue)

    def test_save_queue_writes_json_structure(self):
        tg.save_queue(empty_queue(), path=self.path)
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data, {"tasks": [], "retry": []})


class QueueMutationTest(unittest.TestCase):
    """入队 / 失败流转 / 移除 / 重试流转。"""

    def setUp(self):
        self.queue = empty_queue()

    def test_enqueue_appends_to_tasks_with_id(self):
        rec = tg.queue_enqueue(self.queue, media_record())
        self.assertEqual(len(self.queue["tasks"]), 1)
        self.assertEqual(rec["kind"], "media")
        self.assertTrue(rec.get("id"))
        self.assertEqual(rec.get("attempts", 0), 0)

    def test_enqueue_appends_to_end(self):
        tg.queue_enqueue(self.queue, media_record(chat_id=1))
        tg.queue_enqueue(self.queue, media_record(chat_id=2))
        self.assertEqual(
            [r["chat_id"] for r in self.queue["tasks"]], [1, 2]
        )

    def test_fail_to_retry_moves_and_increments(self):
        rec = tg.queue_enqueue(self.queue, media_record(chat_id=1))
        tg.queue_enqueue(self.queue, media_record(chat_id=2))
        tg.queue_fail_to_retry(self.queue, rec)
        self.assertEqual(
            [r["chat_id"] for r in self.queue["tasks"]], [2]
        )
        self.assertEqual(len(self.queue["retry"]), 1)
        self.assertEqual(self.queue["retry"][0]["chat_id"], 1)
        self.assertEqual(self.queue["retry"][0]["attempts"], 1)

    def test_fail_to_retry_unknown_record_is_noop(self):
        tg.queue_fail_to_retry(self.queue, media_record(chat_id=999))
        self.assertEqual(self.queue, empty_queue())

    def test_remove_by_index(self):
        rec1 = tg.queue_enqueue(self.queue, media_record(chat_id=1))
        rec2 = tg.queue_enqueue(self.queue, media_record(chat_id=2))
        ok, removed = tg.queue_remove(self.queue, "tasks", 1)
        self.assertTrue(ok)
        self.assertEqual(removed["id"], rec1["id"])
        self.assertEqual([r["chat_id"] for r in self.queue["tasks"]], [2])

    def test_remove_invalid_index_fails(self):
        tg.queue_enqueue(self.queue, media_record())
        ok, removed = tg.queue_remove(self.queue, "tasks", 5)
        self.assertFalse(ok)
        self.assertEqual(len(self.queue["tasks"]), 1)

    def test_remove_from_empty_fails(self):
        ok, removed = tg.queue_remove(self.queue, "tasks", 1)
        self.assertFalse(ok)

    def test_retry_success_removes_by_id(self):
        rec = tg.queue_enqueue(self.queue, media_record())
        tg.queue_fail_to_retry(self.queue, rec)
        tg.queue_retry_success(self.queue, self.queue["retry"][0])
        self.assertEqual(self.queue["retry"], [])

    def test_retry_failed_keeps_position_and_increments(self):
        rec1 = tg.queue_enqueue(self.queue, media_record(chat_id=1))
        rec2 = tg.queue_enqueue(self.queue, media_record(chat_id=2))
        tg.queue_fail_to_retry(self.queue, rec1)
        tg.queue_fail_to_retry(self.queue, rec2)
        # 重试 retry 列表第 2 条（chat_id=2），失败 → 位置不变、attempts+1
        target = self.queue["retry"][1]
        tg.queue_retry_failed(self.queue, target)
        self.assertEqual(
            [r["chat_id"] for r in self.queue["retry"]], [1, 2]
        )
        self.assertEqual(self.queue["retry"][1]["attempts"], 2)


class EnqueueAndStartTest(unittest.IsolatedAsyncioTestCase):
    """enqueue_and_start：入队后必须用带 id 的记录触发执行（回归：KeyError 'id'）。"""

    async def asyncSetUp(self):
        tg.QUEUE = {"tasks": [], "retry": []}
        tg.QUEUE_LOCK = asyncio.Lock()

    def tearDown(self):
        tg.QUEUE = {"tasks": [], "retry": []}
        tg.QUEUE_LOCK = None

    async def test_spawned_record_has_id(self):
        captured = []

        async def fake_execute(record):
            captured.append(record)

        with mock.patch.object(tg, "save_queue"), mock.patch.object(
            tg, "execute_queued_task", side_effect=fake_execute
        ):
            await tg.enqueue_and_start({
                "kind": "media", "chat_id": 1, "msg_id": 2, "label": "x.mp4",
            })
        await asyncio.sleep(0)  # 让被 spawn 的任务执行

        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0].get("id"))
        # 队列文件中的记录与执行记录必须是同一个 id
        self.assertEqual(tg.QUEUE["tasks"][0]["id"], captured[0]["id"])


class QueueDeadlockRegressionTest(unittest.IsolatedAsyncioTestCase):
    """回归：execute_queued_task 不能与 download_file 嵌套抢同一个信号量
    （信号量限 1 时，嵌套 acquire 会死锁，任务永远无法完成）。"""

    async def asyncSetUp(self):
        self.old_sem = tg.DOWNLOAD_SEMAPHORE
        self.old_client = tg.client
        self.old_queue = tg.QUEUE
        self.old_lock = tg.QUEUE_LOCK
        self.old_save = tg.save_queue
        tg.DOWNLOAD_SEMAPHORE = tg.AdjustableSemaphore(1)
        tg.QUEUE_LOCK = asyncio.Lock()
        tg.QUEUE = {"tasks": [], "retry": []}
        tg.save_queue = mock.MagicMock()  # 测试期间不写真实文件

    def tearDown(self):
        tg.DOWNLOAD_SEMAPHORE = self.old_sem
        tg.client = self.old_client
        tg.QUEUE = self.old_queue
        tg.QUEUE_LOCK = self.old_lock
        tg.save_queue = self.old_save

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
        tg.client = fake_client

        record = {
            "kind": "media", "chat_id": 1, "msg_id": 1,
            "source_override": None, "label": "a.mp4", "id": "test1",
        }
        tg.queue_enqueue(tg.QUEUE, record)

        # 旧代码（execute 内层再拿信号量）会在 10 秒内死锁 → 超时失败
        await asyncio.wait_for(
            tg.execute_queued_task(record), timeout=10
        )

        self.assertEqual(tg.QUEUE["tasks"], [])
        self.assertEqual(tg.QUEUE["retry"], [])


class QueueFormatTest(unittest.TestCase):
    """队列/待重试列表文本格式化。"""

    def test_queue_text_empty(self):
        self.assertIn("空", tg.format_queue_text(empty_queue()))

    def test_queue_text_entries(self):
        queue = empty_queue()
        tg.queue_enqueue(queue, media_record(label="视频.mp4"))
        text = tg.format_queue_text(queue)
        self.assertIn("视频.mp4", text)
        self.assertIn("[媒体]", text)

    def test_retry_text_shows_attempts(self):
        queue = empty_queue()
        rec = tg.queue_enqueue(
            queue, {"kind": "douyin", "url": "https://v.douyin.com/x/",
                    "label": "https://v.douyin.com/x/"}
        )
        tg.queue_fail_to_retry(queue, rec)
        text = tg.format_retry_text(queue)
        self.assertIn("v.douyin.com", text)
        self.assertIn("尝试", text)
        self.assertIn("1", text)

    def test_retry_text_empty(self):
        self.assertIn("空", tg.format_retry_text(empty_queue()))


if __name__ == "__main__":
    unittest.main()
