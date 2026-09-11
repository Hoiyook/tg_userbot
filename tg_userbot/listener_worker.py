"""标签监听 —— Worker 侧：常驻受控消费任务（转发 / 触发下载）。

**扫描 ≠ 执行**的另一半。Scanner（``listener.py``）只把匹配结果落成持久化任务，
本模块负责把它们**按节奏**执行：

    claim（短事务落 PROCESSING + 租约）
      ↓  ← 事务到此结束，Telegram API 一律在事务外
    forward 到目标（收藏夹 / 指定聊天）
      ↓  download=1 且目标是收藏夹
    现有 app.enqueue_media → 现有下载队列 → 现有下载 Worker
      ↓
    SUCCESS / 退避重试 / FAILED

**为什么要有 Worker 而不是扫描时直接发**：一次扫描可能命中几十条，连着发就是
几十个转发请求砸过去，既容易吃 FloodWait 也没有节流手段；而且进程在中途被杀会
把整轮状态丢掉。拆开之后：任务先落盘（崩溃最多丢正在执行的那一条）、发送速率
可控、重试与租约独立成状态机。

**Worker 不读 listen.json**（§12）：已入队任务的目标以 ``listener_tasks`` 为准
——「10:00 建的任务指向 Chat B，10:02 把配置改成 Chat C」之后，那条任务仍然发给
Chat B，新消息才发给 Chat C（§19）。配置只影响**新任务生成**。

**FloodWait 的处理原则**（§27）：以服务端返回的 N 为最高优先级（**不许固定等
60 秒、不许立即重试**），并且**暂停整个 Worker**——FloodWait 是账号级的，只给
那条任务设退避、转头去发别的目标，会继续吃限流甚至加重。只有离谱到超过
``LISTEN_FLOODWAIT_MAX_WAIT_SECONDS`` 的返回值才截断并显著告警。

**幂等性**：本系统是 **at-least-once**（§29）。``SUCCESS`` 写入前崩溃 → 租约到期
→ 任务重跑 → 理论上会重复转发一次。SQLite 的 UNIQUE 只能防重复**建任务**，防不了
Telegram API 的重复投递；重复转发带来的重复下载由现有 ``dedup`` 拦下（它拦下载
不拦转发）。

**稳定性**：单个任务失败绝不让 Worker 退出（§42）；DB 不可用只记日志跳过本轮；
被取消（停服）时原样上抛，并把手上的任务放回 PENDING。
"""
import asyncio
import time

from telethon.errors import (
    ChannelPrivateError,
    ChatWriteForbiddenError,
    FloodWaitError,
    PeerIdInvalidError,
)

from . import config
from . import runtime_db
from . import state
from .log import logger

# 正在执行的任务 id（只允许 1 条，见 LISTEN_WORKER_CONCURRENCY）；停机时用它
# 把手上的任务放回 PENDING。单线程事件循环内读写，无需加锁。
_INFLIGHT = None

# 后台任务强引用（§35：asyncio 只对 Task 持弱引用，不持引用可能被 GC）
_TASKS = set()

# FloodWait 造成的全局暂停：monotonic 时间戳，超过即恢复发送能力。
_PAUSED_UNTIL = 0.0

# 上一条转发的发送时刻（monotonic），用于最小间隔节流。
_LAST_SEND_AT = 0.0

# 永久错误：等待不会解决，直接 FAILED 而不是无限重试（§28）。
# 按当前 Telethon 版本的实际异常类型列出，不猜。
_PERMANENT_ERRORS = (
    ChatWriteForbiddenError,     # 无发送权限 / 被禁言
    ChannelPrivateError,         # 频道转私密 / 已被踢出
    PeerIdInvalidError,          # 目标不存在 / 解析不到
    ValueError,                  # 目标 id 非法（我们自己的参数问题）
)

# 明确的「暂时性」错误类型：网络抖动 / 超时。其余未列出的异常按「未知」处理——
# 未知错误**重试有限次**再 FAILED，而不是当场判死（网络层可能只是抖了一下）。
_TRANSIENT_ERRORS = (
    TimeoutError,
    ConnectionError,
    asyncio.TimeoutError,
    OSError,
)


# ============================================================
# 错误分类 / 退避（纯函数，便于测试与日后调整）
# ============================================================
def classify_error(exc):
    """异常 → (类别, 建议等待秒数)。类别：flood / permanent / retry。

    FloodWait 采信服务端返回值（只对离谱值截断），其余按异常类型分「永久」与
    「暂时」。分不清的算「retry」——有限次重试后仍失败会转 FAILED，不会无限循环。
    """
    if isinstance(exc, FloodWaitError):
        try:
            seconds = int(getattr(exc, "seconds", 0) or 0)
        except (TypeError, ValueError):
            seconds = 0
        if seconds <= 0:
            seconds = config.LISTEN_WORKER_BACKOFF_BASE_SECONDS
        limit = int(config.LISTEN_FLOODWAIT_MAX_WAIT_SECONDS)
        if seconds > limit:
            logger.warning(
                f"📡 FloodWait 返回值异常大（{seconds}s），按上限 {limit}s 处理"
            )
            seconds = limit
        return "flood", seconds
    if isinstance(exc, _PERMANENT_ERRORS):
        return "permanent", 0
    return "retry", 0


def retry_delay_seconds(attempts):
    """退避秒数：base × 2^(attempts-1)，封顶 max。

    与下载队列的 ``queue._backoff_delay`` 同一风格（§26：复用现有重试风格，
    不另设计一套）。
    """
    base = int(config.LISTEN_WORKER_BACKOFF_BASE_SECONDS)
    cap = int(config.LISTEN_WORKER_BACKOFF_MAX_SECONDS)
    try:
        attempts = max(1, int(attempts))
    except (TypeError, ValueError):
        attempts = 1
    return min(base * (2 ** (attempts - 1)), cap)


def should_retry(attempts):
    """是否还值得自动重试（超过上限转 FAILED，只能人工处理）。"""
    try:
        attempts = int(attempts)
    except (TypeError, ValueError):
        return False
    return attempts <= int(config.LISTEN_WORKER_MAX_ATTEMPTS)


# ============================================================
# 节流与暂停
# ============================================================
def pause_for(seconds):
    """暂停 Worker 的发送能力（FloodWait 或人工）。"""
    global _PAUSED_UNTIL
    seconds = max(0.0, float(seconds or 0))
    _PAUSED_UNTIL = max(_PAUSED_UNTIL, time.monotonic() + seconds)
    if seconds:
        logger.warning(
            f"📡 标签监听 Worker 暂停发送 {int(seconds)}s"
            f"（FloodWait 是账号级限流，暂停整个 Worker 而不只是那一条任务）"
        )


def paused_for():
    """剩余暂停秒数（0 = 未暂停）。"""
    return max(0.0, _PAUSED_UNTIL - time.monotonic())


def reset_pause():
    """清除暂停（测试与人工恢复用）。"""
    global _PAUSED_UNTIL
    _PAUSED_UNTIL = 0.0


async def _sleep(seconds):
    """睡眠的间接层：让测试能观察/跳过节流而不去 patch 全局 asyncio.sleep。"""
    await asyncio.sleep(seconds)


async def pace():
    """最小转发间隔节流：距上次发送不足阈值就先睡够（§31/§32）。

    ``LISTEN_WORKER_MIN_FORWARD_INTERVAL_SECONDS`` **不是** Telegram 官方安全
    阈值，只是保守节流；真正的限流以服务端 FloodWait 为最高优先级。
    """
    global _LAST_SEND_AT
    gap = float(config.LISTEN_WORKER_MIN_FORWARD_INTERVAL_SECONDS)
    if gap <= 0:
        return
    now = time.monotonic()
    if _LAST_SEND_AT and now - _LAST_SEND_AT < gap:
        wait = gap - (now - _LAST_SEND_AT)
        logger.info(f"📡 转发节流：等待 {wait:.1f}s（最小间隔 {gap}s）")
        await _sleep(wait)
    _LAST_SEND_AT = time.monotonic()


# ============================================================
# 领取
# ============================================================
def claim_next_task():
    """领一条可执行任务；暂停中或无任务返回 None。

    暂停检查放在 DB 之前：FloodWait 期间连「看一眼数据库」都不必，直接回 None
    让主循环去睡。
    """
    if paused_for() > 0:
        return None
    try:
        task = runtime_db.claim_listener_task()
    except runtime_db.DbUnavailable as e:
        logger.error(f"📡 领取任务失败（数据库不可用），本轮跳过：{e}")
        return None
    if task is not None:
        global _INFLIGHT
        _INFLIGHT = task["id"]
    return task


def release_inflight(task_id=None):
    """把手上的任务放回 PENDING（优雅停机，不等租约到期）。"""
    global _INFLIGHT
    task_id = task_id if task_id is not None else _INFLIGHT
    if task_id is None:
        return False
    try:
        ok = runtime_db.release_listener_task(task_id)
    except runtime_db.DbUnavailable as e:
        logger.error(f"📡 停机释放任务失败（租约到期后会自动恢复）：{e}")
        ok = False
    _INFLIGHT = None
    return ok


def recover_expired(now=None):
    """启动时恢复过期租约（§24）。DB 不可用只记日志，不影响启动。"""
    try:
        return runtime_db.recover_expired_listener_tasks(now=now)
    except runtime_db.DbUnavailable as e:
        logger.error(f"📡 恢复过期租约任务失败（不影响启动）：{e}")
        return 0


# ============================================================
# 执行
# ============================================================
async def _enqueue_copy(copy, source_link, album_caption, src):
    """入队一份转发副本（走现有下载链路，不新增第二套下载器 §22）。

    ``app`` 函数内导入：app 顶层 `from . import listener_worker`（要挂 Worker
    任务），模块级互相导入会成环。本模块只在发送路径上用到它。
    """
    from . import app
    await app.enqueue_media(
        copy, state.MY_ID, None,
        source_link=source_link,
        album_caption=album_caption,
        src=src,
    )


async def _forward(target_type, target_chat_id, messages, source_chat_id):
    """转发一组消息到目标，返回副本列表；失败原样抛（由调用方分类）。

    ``netio.shielded`` 不在这里用：本函数由 execute_task 的 try/except 兜住，
    且超时语义要保留给「无读超时」的僵死连接——用 asyncio.wait_for 会与
    网络层取消混淆（见 CLAUDE.md「网络层取消」段）。所以这里只做超时控制
    之外的裸调用，异常分类交给 classify_error。
    """
    peer = "me" if target_type == "saved_messages" else int(target_chat_id)
    sent = await state.client.forward_messages(
        peer, messages, from_peer=int(source_chat_id))
    sent = sent if isinstance(sent, (list, tuple)) else [sent]
    return [s for s in sent if s is not None]


async def execute_task(task):
    """执行一条任务，更新其状态与事件。返回是否成功。

    异常绝不外抛（除非被真取消）——Worker 不能因为单条任务炸掉（§42）。
    """
    task_id = task["id"]
    label = _task_label(task)
    logger.info(f"📡 开始执行任务 #{task_id}：{label}")

    members = task.get("payload", {}).get("member_ids") \
        if isinstance(task.get("payload"), dict) else None
    member_ids = members or [task["message_id"]]

    try:
        messages = await listener_fetch(state, task["source_chat_id"], member_ids)
        if not messages:
            # 取不回源消息：多为原消息已被删除。这是**永久**失败——
            # 重试多少次都取不回来，不能让它反复占队列。
            logger.warning(
                f"📡 任务 #{task_id} 的源消息取不回（可能已删除），标记为永久失败："
                f"{label}"
            )
            _safe(lambda: runtime_db.fail_listener_task(
                task_id, error="源消息不可读（可能已删除）"))
            return False

        is_saved = task.get("target_type") == "saved_messages"
        copies = await _forward(task["target_type"], task.get("target_chat_id"),
                                messages, task["source_chat_id"])
        logger.info(
            f"📡 任务 #{task_id} 转发成功：{len(copies)} 条 → "
            f"{'收藏夹' if is_saved else task.get('target_chat_id')}"
        )

        if is_saved and task.get("download"):
            caption = (task.get("payload") or {}).get("caption") or ""
            source_link = _source_link(messages[0], task["source_chat_id"])
            for copy in copies:
                own_text = (getattr(copy, "message", "") or "").strip()
                # 转发副本保留自己的说明；无文字的副本继承源侧读到的相册说明
                # （否则相册里的图片会退化成 媒体类型_时间戳 命名）
                cap = None if own_text else (caption or None)
                try:
                    await _enqueue_copy(copy, source_link, cap, "listen")
                except Exception as e:
                    # 转发已发生且成功：入队失败不该把任务判成失败（重跑会重复
                    # 转发）。记错误即可——副本已躺在收藏夹，用户看得见。
                    logger.exception(
                        f"📡 任务 #{task_id} 副本入队失败（转发已完成，任务仍算成功）：{e}"
                    )

        _safe(lambda: runtime_db.complete_listener_task(task_id))
        return True

    except asyncio.CancelledError:
        # 真取消（停服）：状态留给调用方 release_inflight 处理，原样上抛
        raise
    except Exception as e:
        await _handle_failure(task, e)
        return False
    finally:
        global _INFLIGHT
        if _INFLIGHT == task_id:
            _INFLIGHT = None


def _source_link(message, source_chat_id):
    """任务的来源链接（转发副本进收藏夹后，落盘目录由副本 fwd_from 解析）。"""
    try:
        from .sources import message_source_link
        return message_source_link(message, source_chat_id)
    except Exception:
        return None


async def listener_fetch(state_mod, source_chat_id, member_ids):
    """取回任务的整组消息（委托给 listener，所有 Telegram 读操作都在那边）。"""
    from . import listener
    return await listener.fetch_unit_messages(source_chat_id, member_ids)


def _safe(proc):
    """跑一个 DB 小操作，失败只记日志（状态更新失败不该炸掉 Worker）。"""
    try:
        return proc()
    except runtime_db.DbUnavailable as e:
        logger.error(f"📡 更新任务状态失败（租约到期后会恢复）：{e}")
        return None


async def _handle_failure(task, exc):
    """失败分类 → 重试或永久失败。"""
    task_id = task["id"]
    label = _task_label(task)
    kind, server_wait = classify_error(exc)
    attempts = int(task.get("attempts") or 0)

    if kind == "permanent":
        logger.error(
            f"📡 任务 #{task_id} 永久失败（{type(exc).__name__}）：{label} —— "
            f"{exc}"
        )
        _safe(lambda: runtime_db.fail_listener_task(
            task_id, error=f"{type(exc).__name__}: {exc}"))
        return

    if not should_retry(attempts):
        logger.error(
            f"📡 任务 #{task_id} 已重试 {attempts} 次仍失败，转永久失败：{label}"
            f"（最后一次：{type(exc).__name__}: {exc}）"
        )
        _safe(lambda: runtime_db.fail_listener_task(
            task_id, error=f"重试 {attempts} 次仍失败：{type(exc).__name__}: {exc}"))
        return

    if kind == "flood":
        # 服务端明确说了等多久：按它说的等，并暂停整个 Worker（账号级限流）
        delay = server_wait
        pause_for(delay)
        logger.warning(
            f"📡 任务 #{task_id} 触发 FloodWait {delay}s（这是服务端返回的值，"
            f"不是固定等待）：{label}"
        )
    else:
        delay = retry_delay_seconds(attempts)
        logger.warning(
            f"📡 任务 #{task_id} 暂时失败（{type(exc).__name__}: {exc}），"
            f"{delay}s 后重试（第 {attempts} 次尝试）：{label}"
        )

    next_at = int(time.time()) + int(delay)
    _safe(lambda: runtime_db.retry_listener_task(
        task_id, next_retry_at=next_at, error=f"{type(exc).__name__}: {exc}"))


def _task_label(task):
    target = ("收藏夹" if task.get("target_type") == "saved_messages"
              else f"chat {task.get('target_chat_id')}")
    members = None
    payload = task.get("payload")
    if isinstance(payload, dict):
        members = payload.get("member_ids")
    size = f"{len(members)} 个成员" if members and len(members) > 1 else "单条"
    return (f"来源 {task.get('source_chat_id')} 消息 "
            f"{task.get('message_id')}（{size}）→ {target}"
            + (f" + 下载" if task.get("download") else ""))


# ============================================================
# 主循环
# ============================================================
async def run_once():
    """跑一轮：恢复过期租约 + 领一条任务执行。返回本轮是否真的干了活。

    Worker 不因为单条任务失败而退出（§42）：execute_task 自己吞掉异常，这里
    再兜一层防意外。
    """
    recover_expired()
    task = claim_next_task()
    if task is None:
        pause = paused_for()
        if pause > 0:
            logger.info(f"📡 Worker 暂停中，剩余 {int(pause)}s")
        return False
    try:
        await pace()
        return await execute_task(task)
    except asyncio.CancelledError:
        # 停服：把手上的任务放回待执行，让重启立刻重跑（不等租约到期）
        release_inflight(task["id"])
        raise
    except Exception as e:
        logger.exception(f"📡 任务 #{task.get('id')} 执行时未预期异常：{e}")
        _safe(lambda: runtime_db.retry_listener_task(
            task["id"],
            next_retry_at=int(time.time()) + retry_delay_seconds(1),
            error=f"未预期异常：{type(e).__name__}: {e}"))
        return False


async def worker_loop():
    """常驻循环：有活就干，没活就按轮询间隔歇一会儿。

    异常一律兜住 → 本任务永不因单次失败退出；被 main 取消（停止信号）时以
    CancelledError 收尾并释放手上的任务。
    """
    logger.info(
        f"📡 标签监听 Worker 已启动（并发 "
        f"{config.LISTEN_WORKER_CONCURRENCY}，最小转发间隔 "
        f"{config.LISTEN_WORKER_MIN_FORWARD_INTERVAL_SECONDS}s，租约 "
        f"{config.LISTEN_WORKER_LEASE_SECONDS}s，最大自动重试 "
        f"{config.LISTEN_WORKER_MAX_ATTEMPTS} 次）"
    )
    poll = float(config.LISTEN_WORKER_POLL_SECONDS)
    try:
        while True:
            try:
                did = await run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"📡 Worker 主循环异常（继续运行）：{e}")
                did = False
            if not did:
                # 暂停中按剩余暂停时间睡，避免空转；否则按轮询间隔
                await asyncio.sleep(max(poll, min(paused_for(), 60.0) or poll))
    except asyncio.CancelledError:
        logger.info("📡 标签监听 Worker 正在停止…")
        release_inflight()
        raise


def start_worker():
    """把 Worker 循环挂成后台任务并**保持强引用**（§35），返回该任务。"""
    task = asyncio.create_task(worker_loop())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task
