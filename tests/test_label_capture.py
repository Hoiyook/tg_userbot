"""手工转发「评论 + 紧跟媒体」前置标注的待关联窗口行为。

关注点：一条评论后连续到达的 N 条媒体都继承同一条标注（取用不清空，靠时间
自然过期）；过了关联窗口后不再被认领。纯内存状态、无 I/O、无 Telegram。
另覆盖 _enqueue_me 的「文本在后」宽限：评论的事件可能落在媒体之后（实测两
种顺序都会出现），入队前应等一小段宽限再取一次标注。
"""
import asyncio
import unittest
from unittest import mock

from tg_userbot import app


def _reset():
    app._ME_PENDING_LABEL = None
    app._ME_PENDING_LABEL_AT = 0.0


class MeLabelCaptureTest(unittest.TestCase):
    """_record_me_label / _take_me_label 的窗口语义。"""

    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def test_consume_does_not_clear_same_label_for_burst(self):
        # 一条评论 + 连续 N 条媒体：多次认领都拿到同一条（不清空）
        app._record_me_label("自存")
        self.assertEqual(app._take_me_label(), "自存")
        self.assertEqual(app._take_me_label(), "自存")  # 第二条媒体同样继承

    def test_expires_after_window(self):
        # 过了窗口：不再返回，且待关联清空（下次 None）
        base = 1000.0
        app._record_me_label("自存")
        app._ME_PENDING_LABEL_AT = base
        with mock.patch.object(app.time, "monotonic", return_value=base + 3):
            self.assertEqual(app._take_me_label(), "自存")  # 窗口内
        with mock.patch.object(app.time, "monotonic", return_value=base + 10):
            self.assertIsNone(app._take_me_label())  # 已过期
        self.assertIsNone(app._take_me_label())  # 已清空

    def test_later_comment_overwrites(self):
        app._record_me_label("第一条")
        app._record_me_label("第二条")  # 紧跟的媒体属于后一条评论
        self.assertEqual(app._take_me_label(), "第二条")

    def test_none_when_nothing_recorded(self):
        self.assertIsNone(app._take_me_label())


def _fake_msg(mid=1):
    msg = mock.Mock()
    msg.id = mid
    return msg


class _EnqueueMeHarness:
    """把 _enqueue_me 的协程依赖（相册说明、入队）换成内存假件，记录 user_label。"""

    def __init__(self):
        self.labels = []

    def patch(self, grace):
        async def fake_enqueue(message, chat_id, source_override,
                               source_link=None, album_caption=None,
                               user_label=None, src=None, parent_date=None,
                               parent_caption=None):
            self.labels.append(user_label)

        return mock.patch.multiple(
            "tg_userbot.app",
            ME_LABEL_GRACE_SECONDS=grace,
            _maybe_album_caption=mock.AsyncMock(return_value=""),
            enqueue_media=fake_enqueue,
        )


class MeLabelGraceTest(unittest.IsolatedAsyncioTestCase):
    """_enqueue_me 的「文本在后」宽限：评论事件可能落在媒体之后。"""

    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    async def test_trailing_comment_within_grace_attaches(self):
        # 媒体先到（此刻无评论）→ 宽限窗口内评论落地 → 应等到并继承
        h = _EnqueueMeHarness()
        with h.patch(0.05):
            task = asyncio.create_task(app._enqueue_me(_fake_msg(1)))
            await asyncio.sleep(0.01)      # 媒体已入取标点、评论尚未到达
            app._record_me_label("自存")   # 尾随评论落地
            await task
        self.assertEqual(h.labels, ["自存"])

    async def test_pending_label_takes_immediately_without_waiting(self):
        # 已有待关联标注时不等宽限，立即继承入队（宽限被故意放大以暴露等待）
        app._record_me_label("自存")
        h = _EnqueueMeHarness()
        with h.patch(50):
            await asyncio.wait_for(
                app._enqueue_me(_fake_msg(2)), timeout=1
            )
        self.assertEqual(h.labels, ["自存"])


if __name__ == "__main__":
    unittest.main()
