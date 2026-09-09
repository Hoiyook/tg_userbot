"""Chrome Event Dispatcher (chrome_events.py) 单元测试：

GUID-based 事件过滤与所有权管理：
- 事件过滤：Browser.downloadWillBegin 和 Browser.downloadProgress
- 防止并发下载间的事件窃取
- 事件处理器注册与分发
- GUID 过期与任务生命周期管理

运行方式：.venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import asyncio
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_events_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import chrome_events  # noqa: E402


class ChromeEventDispatcherTest(unittest.TestCase):
    """EventDispatcher 核心功能测试。"""

    def setUp(self):
        self.dispatcher = chrome_events.EventDispatcher(guid_timeout_seconds=10)
        self.task_id = "test-task-1234"
        self.guid = "12345678-1234-1234-1234-123456789012"  # Proper GUID format

    def test_register_and_clear_task(self):
        """任务注册与清理。"""
        # 注册任务应成功
        self.dispatcher.register_task(self.task_id, self.guid)
        self.assertEqual(self.dispatcher._active_tasks[self.task_id]["guid"], self.guid)

        # 清理任务应成功
        self.dispatcher.clear_task(self.task_id)
        self.assertNotIn(self.task_id, self.dispatcher._active_tasks)

    def test_event_filtering_same_task(self):
        """同一任务的事件应通过。"""
        self.dispatcher.register_task(self.task_id, self.guid)

        # downloadWillBegin 事件 GUID 匹配
        event1 = {
            "method": "Browser.downloadWillBegin",
            "params": {"guid": self.guid, "suggestedFilename": "test.zip"}
        }
        filtered = self.dispatcher.filter_event(event1, self.task_id)
        self.assertEqual(filtered, event1)  # 不应修改事件内容

        # downloadProgress 事件 GUID 匹配
        event2 = {
            "method": "Browser.downloadProgress",
            "params": {"guid": self.guid, "state": "completed"}
        }
        filtered = self.dispatcher.filter_event(event2, self.task_id)
        self.assertEqual(filtered, event2)

        # 事件处理器应触发
        handler_called = []
        def event_handler(event):
            handler_called.append(event)

        self.dispatcher.add_event_handler("Browser.downloadWillBegin", event_handler)
        self.dispatcher.dispatch_event(event1, self.task_id)
        self.assertEqual(len(handler_called), 1)
        self.assertEqual(handler_called[0], event1)

    def test_event_filtering_different_task_guid_mismatch(self):
        """不同任务 GUID 不匹配时丢弃事件。"""
        self.dispatcher.register_task(self.task_id, self.guid)
        other_guid = "87654321-4321-4321-4321-0987654321"  # Proper GUID format

        # downloadWillBegin 事件 GUID 不匹配
        event = {
            "method": "Browser.downloadWillBegin",
            "params": {"guid": other_guid, "suggestedFilename": "stolen.zip"}
        }
        filtered = self.dispatcher.filter_event(event, self.task_id)
        self.assertIsNone(filtered)  # 应丢弃

        # 事件处理器不应触发
        handler_called = []
        def event_handler(event):
            handler_called.append(event)

        self.dispatcher.add_event_handler("Browser.downloadWillBegin", event_handler)
        self.dispatcher.dispatch_event(event, self.task_id)
        self.assertEqual(len(handler_called), 0)

    def test_event_filtering_non_browser_events(self):
        """非浏览器下载事件直接通过。"""
        self.dispatcher.register_task(self.task_id, self.guid)

        # 非 downloadWillBegin/downloadProgress 事件应直接通过
        event = {
            "method": "Page.loadEventFired",
            "params": {}
        }
        filtered = self.dispatcher.filter_event(event, self.task_id)
        self.assertEqual(filtered, event)

    def test_unregister_event_handlers(self):
        """事件处理器注销测试。"""
        handler_called = []
        def event_handler(event):
            handler_called.append(event)

        # 添加处理器
        self.dispatcher.add_event_handler("Browser.downloadWillBegin", event_handler)
        self.assertIn("Browser.downloadWillBegin", self.dispatcher._event_handlers)

        # 注销处理器
        self.dispatcher.unregister_event_handlers("Browser.downloadWillBegin")
        self.assertNotIn("Browser.downloadWillBegin", self.dispatcher._event_handlers)

        # 事件分发不应触发已注销的处理器
        event = {
            "method": "Browser.downloadWillBegin",
            "params": {"guid": self.guid, "suggestedFilename": "test.zip"}
        }
        self.dispatcher.register_task(self.task_id, self.guid)
        self.dispatcher.dispatch_event(event, self.task_id)
        self.assertEqual(len(handler_called), 0)

    def test_validate_guid(self):
        """GUID 验证测试。"""
        # 有效 GUID
        valid_guid = "12345678-1234-1234-1234-123456789012"
        self.assertTrue(chrome_events.validate_guid(valid_guid))

        # 无效 GUID
        invalid_guids = [
            "",  # 空
            "not-a-guid",  # 格式错误
            "12345678-1234-1234-1234",  # 过短
            "12345678-1234-1234-1234-1234567890123",  # 过长
            "12345678-1234-1234-1234-12345678901x",  # 包含非法字符
        ]
        for guid in invalid_guids:
            self.assertFalse(chrome_events.validate_guid(guid))

    def test_event_ownership_error(self):
        """EventOwnershipError 异常测试。"""
        try:
            raise chrome_events.EventOwnershipError(
                "test-task", "wrong-guid", "correct-guid")
        except chrome_events.EventOwnershipError as e:
            self.assertEqual(e.task_id, "test-task")
            self.assertEqual(e.actual_guid, "wrong-guid")
            self.assertEqual(e.expected_guid, "correct-guid")
            self.assertIn("任务 test-task", str(e))
            self.assertIn("实际 GUID: wrong-guid", str(e))
            self.assertIn("期望 GUID: correct-guid", str(e))

    def test_event_handling(self):
        """事件处理流程测试。"""
        event_received = []

        def event_handler(event):
            event_received.append(event)

        # 注册处理器
        self.dispatcher.add_event_handler("Browser.downloadWillBegin", event_handler)

        # 注册任务
        self.dispatcher.register_task(self.task_id, self.guid)

        # 发送事件
        event = {
            "method": "Browser.downloadWillBegin",
            "params": {"guid": self.guid, "suggestedFilename": "test.zip"}
        }

        # 分发事件
        self.dispatcher.dispatch_event(event, self.task_id)

        # 验证处理器被调用
        self.assertEqual(len(event_received), 1)
        self.assertEqual(event_received[0], event)

    def test_multiple_event_handlers(self):
        """单个事件类型支持多个处理器。"""
        handler1_calls = []
        handler2_calls = []

        def handler1(event):
            handler1_calls.append(event)

        def handler2(event):
            handler2_calls.append(event)

        # 注册两个处理器
        self.dispatcher.add_event_handler("Browser.downloadWillBegin", handler1)
        self.dispatcher.add_event_handler("Browser.downloadWillBegin", handler2)

        # 注册任务
        self.dispatcher.register_task(self.task_id, self.guid)

        # 发送事件
        event = {
            "method": "Browser.downloadWillBegin",
            "params": {"guid": self.guid, "suggestedFilename": "test.zip"}
        }

        # 分发事件
        self.dispatcher.dispatch_event(event, self.task_id)

        # 验证两个处理器都被调用
        self.assertEqual(len(handler1_calls), 1)
        self.assertEqual(len(handler2_calls), 1)
        self.assertEqual(handler1_calls[0], event)
        self.assertEqual(handler2_calls[0], event)

    def test_clear_task_removes_registration(self):
        """清理任务应移除任务注册。"""
        self.dispatcher.register_task(self.task_id, self.guid)
        self.assertIn(self.task_id, self.dispatcher._active_tasks)

        # 清理任务
        self.dispatcher.clear_task(self.task_id)
        self.assertNotIn(self.task_id, self.dispatcher._active_tasks)

        # 后续事件应被丢弃（因为任务已不存在）
        event = {
            "method": "Browser.downloadWillBegin",
            "params": {"guid": self.guid, "suggestedFilename": "test.zip"}
        }
        filtered = self.dispatcher.filter_event(event, self.task_id)
        self.assertIsNone(filtered)


if __name__ == "__main__":
    unittest.main()