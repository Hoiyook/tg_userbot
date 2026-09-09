"""Chrome Event Dispatcher (chrome_events.py) - GUID-based 事件过滤与所有权管理

为 Chrome Agent V2 实现的事件分发系统，防止并发下载间的事件窃取，
确保每个下载事件只被所属的任务处理。

核心功能：
- GUID-based 事件过滤（Browser.downloadWillBegin, Browser.downloadProgress）
- 任务注册与清理
- 事件处理器注册与分发
- GUID 验证与过期管理
- 事件所有权验证
"""
import asyncio
import time
from typing import Dict, List, Optional, Callable, Any
from datetime import datetime, timedelta

from .config import CHROME_EVENTS_DEFAULT_GUID_TIMEOUT, CHROME_EVENTS_MAX_HANDLERS


class EventOwnershipError(Exception):
    """事件所有权异常：当事件不属于当前任务时抛出。"""

    def __init__(self, task_id: str, actual_guid: str, expected_guid: str):
        self.task_id = task_id
        self.actual_guid = actual_guid
        self.expected_guid = expected_guid
        super().__init__(
            f"任务 {task_id} 的事件验证失败：实际 GUID: {actual_guid}, "
            f"期望 GUID: {expected_guid}"
        )


class EventDispatcher:
    """GUID-based 事件分发器，用于防止并发下载间的事件窃取。"""

    def __init__(self, guid_timeout_seconds: int = None):
        """初始化事件分发器。

        Args:
            guid_timeout_seconds: GUID 超时时间（秒），默认使用配置值
        """
        self._guid_timeout_seconds = guid_timeout_seconds or CHROME_EVENTS_DEFAULT_GUID_TIMEOUT
        self._active_tasks: Dict[str, Dict[str, Any]] = {}  # task_id -> {guid, timestamp}
        self._event_handlers: Dict[str, List[Callable]] = {}  # event_type -> [handlers]

    def register_task(self, task_id: str, guid: str) -> None:
        """注册任务及其 GUID。

        Args:
            task_id: 任务标识符
            guid: 浏览器分配的下载 GUID
        """
        if not validate_guid(guid):
            raise ValueError(f"无效的 GUID: {guid}")

        self._active_tasks[task_id] = {
            "guid": guid,
            "timestamp": time.time()
        }

    def clear_task(self, task_id: str) -> None:
        """清理已完成或失败的任务。

        Args:
            task_id: 任务标识符
        """
        self._active_tasks.pop(task_id, None)

    def validate_guid(self, task_id: str, guid: str) -> bool:
        """验证任务 GUID 是否有效且未过期。

        Args:
            task_id: 任务标识符
            guid: 要验证的 GUID

        Returns:
            bool: GUID 是否有效
        """
        task = self._active_tasks.get(task_id)
        if not task:
            return False

        # 检查 GUID 是否匹配
        if task["guid"] != guid:
            return False

        # 检查 GUID 是否过期
        elapsed = time.time() - task["timestamp"]
        if elapsed > self._guid_timeout_seconds:
            return False

        return True

    def filter_event(self, event: Dict[str, Any], task_id: str) -> Optional[Dict[str, Any]]:
        """过滤事件，只保留属于当前任务的事件。

        Args:
            event: 原始事件
            task_id: 任务标识符

        Returns:
            过滤后的事件（如果属于当前任务）或 None（如果被过滤掉）
        """
        method = event.get("method")

        # 只处理浏览器下载事件
        if method not in ("Browser.downloadWillBegin", "Browser.downloadProgress"):
            return event

        # 对于下载事件，验证 GUID
        guid = event.get("params", {}).get("guid")
        if not guid:
            return None

        if not self.validate_guid(task_id, guid):
            return None

        return event

    def add_event_handler(self, event_type: str, handler: Callable[[Dict[str, Any]], None]) -> None:
        """添加事件处理器。

        Args:
            event_type: 事件类型（如 "Browser.downloadWillBegin"）
            handler: 事件处理函数
        """
        if event_type not in self._event_handlers:
            self._event_handlers[event_type] = []

        # 检查处理器数量限制
        if len(self._event_handlers[event_type]) >= CHROME_EVENTS_MAX_HANDLERS:
            raise ValueError(f"事件类型 {event_type} 的处理器数量已达到上限 {CHROME_EVENTS_MAX_HANDLERS}")

        self._event_handlers[event_type].append(handler)

    def unregister_event_handlers(self, event_type: str) -> None:
        """注销指定事件类型所有处理器。

        Args:
            event_type: 事件类型
        """
        self._event_handlers.pop(event_type, None)

    def dispatch_event(self, event: Dict[str, Any], task_id: str) -> None:
        """分发事件到注册的处理器。

        Args:
            event: 要分发的事件
            task_id: 任务标识符
        """
        # 先过滤事件
        filtered_event = self.filter_event(event, task_id)
        if filtered_event is None:
            return

        # 分发到对应类型的处理器
        method = event.get("method")
        if method in self._event_handlers:
            for handler in self._event_handlers[method]:
                try:
                    handler(filtered_event)
                except Exception as e:
                    # 记录错误但不中断分发
                    print(f"事件处理器错误: {e}")

    def get_active_tasks(self) -> Dict[str, str]:
        """获取当前所有活动任务及其 GUID。

        Returns:
            task_id -> guid 的映射
        """
        return {
            task_id: task["guid"]
            for task_id, task in self._active_tasks.items()
        }

    def cleanup_expired_tasks(self) -> None:
        """清理过期的任务。

        Returns:
            清理的任务数量
        """
        current_time = time.time()
        expired_tasks = []

        for task_id, task in self._active_tasks.items():
            if current_time - task["timestamp"] > self._guid_timeout_seconds:
                expired_tasks.append(task_id)

        for task_id in expired_tasks:
            self.clear_task(task_id)

        return len(expired_tasks)


def validate_guid(guid: str) -> bool:
    """验证 GUID 格式是否正确。

    Args:
        guid: 要验证的 GUID 字符串

    Returns:
        bool: GUID 是否有效
    """
    if not guid or not isinstance(guid, str):
        return False

    # GUID 格式：xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    parts = guid.split('-')
    if len(parts) != 5:
        return False

    # 检查各部分长度
    if len(parts[0]) != 8 or len(parts[1]) != 4 or len(parts[2]) != 4:
        return False
    if len(parts[3]) != 4 or len(parts[4]) != 12:
        return False

    # 检查是否都是十六进制字符
    hex_chars = set("0123456789abcdefABCDEF")
    for part in parts:
        if not all(c in hex_chars for c in part):
            return False

    return True