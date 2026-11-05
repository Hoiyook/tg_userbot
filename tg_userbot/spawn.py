# -*- coding: utf-8 -*-
"""fire-and-forget 后台任务的统一启动口（2026-11-05 审计止血）。

裸 ``asyncio.create_task(...)`` 的两个坑：① 事件循环对 Task 只持弱引用，
任务完成前可能被 GC；② 没有 done 回调，任务抛异常只会在 GC 时打一条
"Task exception was never retrieved"，调用方与用户都无感（实测
app._enqueue_me 静默死 = 收藏夹媒体不下载且无任何报错）。

本 helper 把 queue.py 的 ``_SPAWNED_TASKS`` 范式（强引用 + done 回调）
抽成公共件：强引用防 GC，异常完整记日志不静默。新代码一律用
``spawn(coro, what)``，不再裸写 create_task。
"""
import asyncio
import logging

logger = logging.getLogger(__name__)

# 强引用集：任务完成后由 done 回调自我移除
_SPAWNED = set()


def spawn(coro, what="后台任务"):
    """启动后台协程并持有强引用；异常记完整日志（不静默、不上抛）。"""
    task = asyncio.create_task(coro)

    def _done(t):
        _SPAWNED.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error(f"❌ {what} 后台任务异常：{t.exception()!r}",
                         exc_info=t.exception())

    _SPAWNED.add(task)
    task.add_done_callback(_done)
    return task
