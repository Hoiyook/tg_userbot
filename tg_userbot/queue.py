"""持久化下载队列：纯数据函数 + 异步执行器。

纯数据函数（load/save/enqueue/remove/retry_success/failed/format_*）以
queue 参数传入、不碰全局；异步执行器读写 state.QUEUE / state.QUEUE_LOCK /
state.EXECUTING / state.client。队列层**刻意不 acquire DOWNLOAD_SEMAPHORE**
—— download_file 内部持有同一个信号量，外层再包会嵌套死锁（并发 ≥2 时
槽位互相等待；已由死锁回归测试覆盖）。真实下载并发由内部信号量约束。

只有 kind=media 一种任务：平台链接（douyin/instagram）不再入队——链接只由
platform.relay_platform_links 转发给解析 bot，其回复视频走白名单转发流进
收藏夹后以 media 入队。重启后历史 JSON 里残留的 douyin/instagram 任务落入
「未知类型 → 移除 + log」，安全兜底（QUEUE_KIND_LABELS 保留使展示可读）。

enqueue_and_start / recover_queue_tasks 内部以裸名调用 execute_queued_task，
供测试 monkeypatch（patch queue.execute_queued_task）可见。
"""
import os
import json
import re
import uuid
import asyncio

from . import state
from . import download
from .config import (
    QUEUE_FETCH_TIMEOUT,
    QUEUE_FILE,
    QUEUE_KIND_LABELS,
)
from .log import clear_trace, logger, set_trace
from .sources import message_link

# 对 create_task 产出的执行任务持有强引用：事件循环只对 Task 持有弱引用，任务在
# 完成前可能被 GC（asyncio 官方建议显式持引用；3.9 下 pending 任务实测虽不会被
# 回收，仍按最佳实践持有，兼为未来 Python 版本兜底）。done 回调里自我移除。
_SPAWNED_TASKS = set()


def spawn_execute(record):
    """后台执行一个队列任务并持有强引用（入队/启动恢复/手动重试共用）。"""
    task = asyncio.create_task(execute_queued_task(record))
    _SPAWNED_TASKS.add(task)
    task.add_done_callback(_SPAWNED_TASKS.discard)
    return task


def is_queue_command(text):
    # /queue、/queue del 1 ...
    return bool(re.fullmatch(r"/queue(?:\s+\S+)*", text.strip(), re.IGNORECASE))


def is_retry_command(text):
    # /retry、/retry 1、/retry del 1 ...
    return bool(re.fullmatch(r"/retry(?:\s+\S+)*", text.strip(), re.IGNORECASE))


def load_queue(path=None):
    """读取队列文件，返回 {"tasks": [...], "retry": [...]}；缺失/损坏返回空。"""
    path = path or QUEUE_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "tasks": list(data.get("tasks", [])),
            "retry": list(data.get("retry", [])),
        }
    except FileNotFoundError:
        return {"tasks": [], "retry": []}
    except Exception as e:
        logger.warning(f"读取下载队列失败，使用空队列：{e}")
        return {"tasks": [], "retry": []}


def save_queue(queue, path=None):
    """原子写入队列文件（temp + os.replace）。"""
    path = path or QUEUE_FILE
    try:
        temp_path = path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    except Exception as e:
        logger.warning(f"保存下载队列失败：{e}")


def queue_enqueue(queue, record):
    """把任务追加到活跃队列末尾，补上 id/attempts 字段，返回该记录。"""
    record = dict(record)
    record.setdefault("id", uuid.uuid4().hex)
    record.setdefault("attempts", 0)
    queue["tasks"].append(record)
    return record


def queue_fail_to_retry(queue, record):
    """执行失败：从 tasks 移除该记录（按 id），attempts+1，追加到 retry 末尾。"""
    for i, r in enumerate(queue["tasks"]):
        if r.get("id") == record.get("id"):
            moved = queue["tasks"].pop(i)
            moved["attempts"] = moved.get("attempts", 0) + 1
            queue["retry"].append(moved)
            return


def queue_remove(queue, list_key, index):
    """按 1 起始序号从指定列表（tasks/retry）移除，返回 (是否成功, 被移除记录)。"""
    lst = queue.get(list_key)
    if not lst or not isinstance(index, int) or index < 1 or index > len(lst):
        return False, None
    return True, lst.pop(index - 1)


def queue_retry_success(queue, record):
    """手动重试成功：从 retry 移除（按 id）。"""
    for i, r in enumerate(queue["retry"]):
        if r.get("id") == record.get("id"):
            queue["retry"].pop(i)
            return


def queue_retry_failed(queue, record):
    """手动重试失败：attempts+1，保持 retry 中的位置不变。"""
    for r in queue["retry"]:
        if r.get("id") == record.get("id"):
            r["attempts"] = r.get("attempts", 0) + 1
            return


def _queue_record_display(record):
    kind_label = QUEUE_KIND_LABELS.get(record.get("kind"), record.get("kind"))
    # media / url 都在入队时算好 final_name，展示与实际下载命名共用
    if record.get("final_name"):
        label = record["final_name"]
    else:
        label = record.get("label") or record.get("url") or "(无)"
    lines = [f"[{kind_label}] {label}"]
    source = _queue_record_source(record)
    if source:
        lines.append(f"来源：{source}")
    return "\n".join(lines)


def _queue_record_source(record):
    """任务来源展示：有链接（频道/转发原频道）优先，否则 #消息ID。"""
    link = record.get("source_link") or message_link(
        record.get("chat_id"), record.get("msg_id")
    )
    if link:
        return link
    if record.get("msg_id") is not None:
        return f"#{record['msg_id']}"
    return ""


def format_queue_text(queue):
    """活跃队列列表文本。"""
    tasks = queue.get("tasks", [])
    if not tasks:
        return "📥 下载队列：空"
    lines = [
        f"{i}. {_queue_record_display(r)}"
        for i, r in enumerate(tasks, start=1)
    ]
    return f"📥 下载队列（等待中 {len(tasks)} 条）：\n\n" + "\n".join(lines)


def format_retry_text(queue):
    """待重试列表文本。"""
    retry = queue.get("retry", [])
    if not retry:
        return "🔁 待重试列表：空"
    lines = [
        f"{i}. {_queue_record_display(r)}（已尝试 {r.get('attempts', 0)} 次）"
        for i, r in enumerate(retry, start=1)
    ]
    return f"🔁 待重试列表（{len(retry)} 条）：\n\n" + "\n".join(lines)


# ------------------------------------------------------------
# 队列执行（异步部分）
# ------------------------------------------------------------

async def enqueue_and_start(record):
    """任务入队（持久化）并立即触发执行。"""
    async with state.QUEUE_LOCK:
        # queue_enqueue 返回带 id 的副本，执行必须用这份副本，
        # 否则 execute_queued_task 按 id 收尾时对不上队列里的记录。
        record = queue_enqueue(state.QUEUE, record)
        save_queue(state.QUEUE)
    spawn_execute(record)


async def _run_queued_task(record):
    """执行单个队列任务，返回是否成功。

    媒体任务原消息已被删除时返回 True（视为终结，直接移除并通知）。
    """
    kind = record.get("kind")
    if kind == "media":
        # 取消息（原消息引用）跑在子任务上：Telethon 请求没有读超时，代理卡住会
        # 永久挂起 → 手动在「子任务完成 / 超时」间等待（QUEUE_FETCH_TIMEOUT 硬顶）。
        # 不用 asyncio.wait_for：它会把子任务的网络层 CancelledError（telethon 断线
        # 对 pending 请求 future 调 cancel()）也当普通取消抛上来，与真取消难分辨。
        # 规则与 download_file 一致：子任务以 CancelledError 收场 = 网络层 → 按失败
        # 转待重试；父任务被真取消只在 await 处出现 → 放行，记录留在队列原位。
        fetch = asyncio.ensure_future(
            state.client.get_messages(record["chat_id"], ids=record["msg_id"])
        )
        try:
            try:
                await asyncio.wait({fetch}, timeout=QUEUE_FETCH_TIMEOUT)
            except asyncio.CancelledError:
                if not fetch.done():
                    fetch.cancel()
                    try:
                        await fetch
                    except asyncio.CancelledError:
                        pass
                raise
            if not fetch.done():
                fetch.cancel()
                try:
                    await fetch
                except asyncio.CancelledError:
                    pass
                logger.error(
                    f"⏰ 队列任务取消息超时（{QUEUE_FETCH_TIMEOUT}s）："
                    f"{record.get('label') or '(无)'}"
                )
                return False
            try:
                message = fetch.result()
            except asyncio.CancelledError as exc:
                # 网络层 future.cancel()：不冒充父任务取消，按失败转待重试
                logger.warning(
                    "队列任务取消息被底层连接取消（网络层 future.cancel()），"
                    "转入待重试",
                    exc_info=True,
                )
                return False
            except Exception as e:
                logger.warning(f"队列任务取消息失败：{e}")
                return False
        except asyncio.TimeoutError:
            logger.error(
                f"⏰ 队列任务取消息超时（{QUEUE_FETCH_TIMEOUT}s）："
                f"{record.get('label') or '(无)'}"
            )
            return False
        except asyncio.CancelledError:
            raise
        if not message:
            label = record.get("label") or ""
            logger.warning(f"队列任务原消息已被删除：{label}")
            try:
                await state.client.send_message(
                    "me",
                    f"❌ 队列任务原消息已被删除，已移除：\n{label}",
                )
            except Exception:
                pass
            return True
        return await download.download_file(
            message,
            record.get("source_override"),
            caption_override=record.get("album_caption"),
            label_override=record.get("user_label"),
        )
    if kind == "url":
        # 本地解析链的 HTTP 直链下载：没有 Telegram 消息概念，直链/标题/
        # 最终名都在入队时定死在记录里，这里只负责执行 + 失败转 retry。
        return await download.download_url_media(record)
    # 旧版平台链接任务（douyin/instagram）已随统一下载链路退役：落到这里的
    # 是历史 JSON 残留，按未知类型移除 + 记日志，不崩不卡队列。
    logger.warning(f"未知队列任务类型：{kind}，直接移除")
    return True


async def execute_queued_task(record):
    """执行队列任务并更新持久化状态：
    成功 → 移除；失败 → 移入 retry（已在 retry 的手动重试失败则留原处）。
    """
    # 全链路追踪：以队列记录 id 前 8 位作 trace，写进本任务 contextvar，
    # 随 await 自动传导到取消息/download_file/通知等全部下游日志（[T=xxxx]）。
    # 每个任务一个独立 context，互不串扰；finally 里清除。
    set_trace(record["id"][:8])
    state.EXECUTING.add(record["id"])
    logger.info(
        f"▶️ 队列任务开始：{record.get('label') or record.get('url') or '(无)'}"
    )
    try:
        try:
            # 注意：这里不能再拿 DOWNLOAD_SEMAPHORE —— download_file 内部
            # 会 acquire 同一个信号量，队列层再包一层会嵌套死锁（并发 ≥2
            # 时槽位互相等待）。真正的下载并发仍由内部信号量约束。
            success = await _run_queued_task(record)
        except BaseException as e:
            # py3.8+ CancelledError 是 BaseException，except Exception 拦不住。
            # 网络层的 future.cancel() 已在 download_file 与取消息处转成普通失败
            # （重试耗尽返回 False / ConnectionError），能作为 CancelledError 逃到
            # 这里的是「真取消」（进程退出等主动 Task.cancel()）—— 原样放行，让
            # 队列记录留在 tasks、重启后由 recover_queue_tasks 重新执行。若把真取消
            # 吞掉当失败移入 retry，会在每次退出时把在途任务误标成失败。
            if isinstance(e, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            logger.exception(f"队列任务执行异常：{e}")
            success = False

        async with state.QUEUE_LOCK:
            in_retry = any(
                r.get("id") == record["id"] for r in state.QUEUE["retry"]
            )
            if success:
                if in_retry:
                    queue_retry_success(state.QUEUE, record)
                else:
                    state.QUEUE["tasks"] = [
                        r for r in state.QUEUE["tasks"]
                        if r.get("id") != record["id"]
                    ]
            else:
                if in_retry:
                    queue_retry_failed(state.QUEUE, record)
                else:
                    queue_fail_to_retry(state.QUEUE, record)
            save_queue(state.QUEUE)
    finally:
        state.EXECUTING.discard(record["id"])
        clear_trace()


def recover_queue_tasks():
    """启动时重新触发活跃队列中的任务（失败/中断的任务重启后自动重来）。"""
    for record in list(state.QUEUE["tasks"]):
        spawn_execute(record)
