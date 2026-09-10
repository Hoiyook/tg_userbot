"""多 worker 下载池：N 条独立 Telethon 连接并行拉文件。

背景/瓶颈：Telethon 单个客户端只有一条共享发送 socket（_sender），同 DC 的
全部文件下载串行分块 → 实测聚合吞吐卡在 ~220KB/s，与并发文件数无关。本模块
为每个进行中下载配一条独立连接（worker），聚合 ≈ N × 单路速度，逼近官方
客户端。worker 是「下载专用」的瘦客户端：

- 从运行中主客户端的内存 session（state.client.session，登录后必在内存、无
  磁盘竞争）拷出 dc_id / server_address / port / auth_key，喂给 MemorySession
  （零磁盘写，避开 SQLite 写竞争）。同一账号同 auth_key 多连接 = 官方客户端
  多 socket 语义，不产生新登录会话。
- receive_updates=False：connect 的初始化请求与发出的请求都包
  InvokeWithoutUpdates，Telegram 不给 worker 推更新流 → 主客户端仍是唯一更新
  消费者；worker 也没注册任何事件处理器。
- 只 connect() 不 .start()：auth_key 已拷入、本账号已授权，无需登录。

事件循环规则：本模块**不**在 import 期构造任何 client/Queue/锁 —— 全部只在
app.main()（asyncio.run 内）调用本模块 async 函数时才建；import 期仅引用
telethon 的类与 config 只读常量。真实下载字节只走 worker.download_media，
通知/来源解析/重连等主链路一律留在 state.client。
"""
import asyncio

from telethon import TelegramClient
from telethon.network.connection import ConnectionTcpFull, ConnectionTcpObfuscated
from telethon.sessions import MemorySession

from . import state
from .config import (
    API_HASH,
    API_ID,
    CONNECTION_TYPE,
    PROXY,
    TELEGRAM_AUTO_RECONNECT,
)
from .log import logger

# 单条 worker connect() 的超时（秒）：代理节点卡住时不能拖住启动/扩容
_WORKER_CONNECT_TIMEOUT = 45

# ------------------------------------------------------------
# 只读观察数据（Runtime Reporter 的 Worker 区块数据源）
# ------------------------------------------------------------
# 池本身**不依赖**这些字典做任何调度决策——它们只被写入、被读取展示。
# worker 是裸 TelegramClient、没有持久标识，而 asyncio.Queue 也无法按客户端
# 反查归属，故这里以 id(client) 为键另记：编号 / BUSY-IDLE / 健康。
# _clear_observation() 必须随池的建立与拆除成对调用，否则 id() 会被后续
# 对象复用、把陈标记安到新 worker 头上。
_WORKER_LABELS = {}    # id(client) -> "#N"（按建池顺序，跨扩容递增）
_WORKER_STATE = {}     # id(client) -> "IDLE" / "BUSY"
_WORKER_HEALTH = {}    # id(client) -> (健康字符串, 原因字符串或 None)
_WORKER_SEQ = 0

_HEALTHY = "HEALTHY"
_UNHEALTHY = "UNHEALTHY"


def _clear_observation():
    """清空观察数据（建池失败/拆池时调用；同时重置编号计数器）。"""
    global _WORKER_SEQ
    _WORKER_LABELS.clear()
    _WORKER_STATE.clear()
    _WORKER_HEALTH.clear()
    _WORKER_SEQ = 0


def _register_worker(client):
    """给新加入池的 worker 编号并置为 IDLE/HEALTHY（仅观察用）。"""
    global _WORKER_SEQ
    _WORKER_SEQ += 1
    _WORKER_LABELS[id(client)] = f"#{_WORKER_SEQ}"
    _WORKER_STATE[id(client)] = "IDLE"
    _WORKER_HEALTH[id(client)] = (_HEALTHY, None)


def worker_label(client):
    """worker 的稳定编号（"#3"）；不认识的对象/None 返回 None（展示为 --）。"""
    if client is None:
        return None
    return _WORKER_LABELS.get(id(client))


def mark_unhealthy(client, reason):
    """把 worker 标记为不健康并记下原因（借出重连失败、下载中途连接被取消等）。

    纯观察：不改变借还/调度行为。不在池里的对象静默忽略。
    """
    if client is not None and id(client) in _WORKER_LABELS:
        _WORKER_HEALTH[id(client)] = (_UNHEALTHY, str(reason))


def mark_healthy(client):
    """清除 worker 的不健康标记（重连成功、下载成功）。"""
    if client is not None and id(client) in _WORKER_LABELS:
        _WORKER_HEALTH[id(client)] = (_HEALTHY, None)


def worker_snapshot():
    """池的只读快照，按编号排序：[{label, state, health, reason}, ...]。

    state 由借还簿记给出（borrow→BUSY、release→IDLE），不读 asyncio.Queue 的
    内部结构。池禁用时返回空列表——Reporter 据此显示 Workers 区块为 --。
    """
    out = []
    for client in state.DOWNLOAD_WORKERS:
        cid = id(client)
        health, reason = _WORKER_HEALTH.get(cid, (_HEALTHY, None))
        out.append({
            "label": _WORKER_LABELS.get(cid, "--"),
            "state": _WORKER_STATE.get(cid, "IDLE"),
            "health": health,
            "reason": reason,
        })
    out.sort(key=lambda w: (len(w["label"]), w["label"]))
    return out


def _connection_class():
    return (
        ConnectionTcpObfuscated
        if CONNECTION_TYPE == "obfuscated"
        else ConnectionTcpFull
    )


def read_live_session():
    """从运行中的主客户端内存 session 拷出授权快照。

    返回 {dc_id, server_address, port, auth_key}，取不到（session 未就绪/字段
    不全）返回 None → 调用方据此降级不启用池。auth_key 是 AuthKey 对象，构造
    后只读、可被多个连接共享（单线程事件循环内使用，无并发写）。
    """
    sess = getattr(state.client, "session", None)
    if sess is None:
        return None
    try:
        dc_id = sess.dc_id
        server_address = sess.server_address
        port = sess.port
        auth_key = sess.auth_key
    except Exception as e:
        logger.warning(f"读取主客户端 session 失败，不启用下载 worker：{e}")
        return None
    if not dc_id or not server_address or not port or auth_key is None:
        return None
    return {
        "dc_id": dc_id,
        "server_address": server_address,
        "port": port,
        "auth_key": auth_key,
    }


async def _spawn_one(snapshot):
    """建一条下载 worker 并连上，返回客户端。失败抛异常由调用方决定降级。"""
    session = MemorySession()
    session.set_dc(
        snapshot["dc_id"], snapshot["server_address"], snapshot["port"]
    )
    session.auth_key = snapshot["auth_key"]
    client = TelegramClient(
        session,
        API_ID,
        API_HASH,
        connection=_connection_class(),
        connection_retries=10,
        retry_delay=3,
        auto_reconnect=TELEGRAM_AUTO_RECONNECT,
        proxy=PROXY,
        receive_updates=False,
    )
    await asyncio.wait_for(
        client.connect(), timeout=_WORKER_CONNECT_TIMEOUT
    )
    return client


def _forget_worker(client):
    """把已移出池的 worker 从观察数据里摘掉（防止 id() 复用导致张冠李戴）。"""
    _WORKER_LABELS.pop(id(client), None)
    _WORKER_STATE.pop(id(client), None)
    _WORKER_HEALTH.pop(id(client), None)


def _reset_pool():
    """把池状态清成「禁用」：borrow() 一律返回 None，下载回退 state.client。"""
    state.DOWNLOAD_WORKERS = []
    state.DOWNLOAD_WORKER_QUEUE = None
    state.DOWNLOAD_WORKER_TARGET = 0
    _clear_observation()


async def spawn_pool(n):
    """启动时建 n 条下载 worker（app.main 登录后调用）。

    失败自动降级：能连上几条用几条（TARGET=实连数）；一条都连不上则池禁用
    （QUEUE=None），下载照常走主客户端单连接 —— 功能永不丢。
    返回实连 worker 数（0 = 未启用）。
    """
    snapshot = read_live_session()
    if snapshot is None or n < 1:
        logger.warning(
            "下载 worker：未取得 session 快照或目标数 <1，本次运行不启用多 worker"
        )
        _reset_pool()
        return 0

    workers = []
    for i in range(1, n + 1):
        try:
            client = await _spawn_one(snapshot)
        except Exception as e:
            logger.warning(
                f"下载 worker #{i} 连接失败：{type(e).__name__}: {e}"
            )
            break
        workers.append(client)
        _register_worker(client)
        logger.info(
            f"✅ 下载 worker #{i} 已连接 "
            f"（{client.session.server_address}）"
        )

    if not workers:
        logger.warning("下载 worker 全部连接失败，回退主客户端单连接下载")
        _reset_pool()
        return 0

    state.DOWNLOAD_WORKERS = workers
    q = asyncio.Queue()
    for client in workers:
        q.put_nowait(client)
    state.DOWNLOAD_WORKER_QUEUE = q
    state.DOWNLOAD_WORKER_TARGET = len(workers)
    logger.info(
        f"🧵 已建立 {len(workers)} 条并行下载 worker 连接"
        f"（下载并发数={len(workers)}，/thread 可调）"
    )
    return len(workers)


async def sync_pool_to_target():
    """把存活 worker 数对齐到 state.DOWNLOAD_WORKER_TARGET（/thread 运行时增减）。

    扩：补 spawn 差数、放回空闲队列；缩：只摘「空闲」的断开，在途下载不受
    打断 —— 归还后由 release 里的收敛逻辑按需断开，live 恒不低于 target。
    池未初始化（QUEUE=None）时是 no-op（下载回退主客户端）。
    """
    q = state.DOWNLOAD_WORKER_QUEUE
    if q is None:
        return
    target = state.DOWNLOAD_WORKER_TARGET

    # 每次循环重读 live 长度，天然幂等：并发触发的两次 sync 也不会加过头
    if target > len(state.DOWNLOAD_WORKERS):
        snapshot = read_live_session()
        if snapshot is None:
            logger.warning("下载 worker 扩容：取不到 session 快照，放弃扩容")
            return
        while len(state.DOWNLOAD_WORKERS) < target:
            try:
                client = await _spawn_one(snapshot)
            except Exception as e:
                logger.warning(
                    f"下载 worker 扩容失败：{type(e).__name__}: {e}"
                )
                break
            state.DOWNLOAD_WORKERS.append(client)
            _register_worker(client)
            q.put_nowait(client)
            logger.info(
                f"🧵 下载 worker 扩容：现有 {len(state.DOWNLOAD_WORKERS)} 条"
            )
        logger.info(
            f"🧵 下载 worker 目标 {target}，实际 {len(state.DOWNLOAD_WORKERS)} 条"
        )

    elif target < len(state.DOWNLOAD_WORKERS):
        # 缩容：先摘空闲断开；当前在途（队列拿不到）的等 release 时收敛
        while len(state.DOWNLOAD_WORKERS) > target:
            try:
                client = q.get_nowait()
            except asyncio.QueueEmpty:
                break
            state.DOWNLOAD_WORKERS.remove(client)
            _forget_worker(client)
            try:
                await client.disconnect()
                logger.info("🧵 下载 worker 缩容：断开一条空闲连接")
            except Exception as e:
                logger.warning(f"断开空闲下载 worker 失败：{e}")
        logger.info(
            f"🧵 下载 worker 目标 {target}，当前 {len(state.DOWNLOAD_WORKERS)} 条"
            "（多余在途连接完成归还后自动收敛）"
        )


async def borrow():
    """借一条空闲 worker 用于一次下载；池未启用返回 None（下载走原单连接路径）。

    返回 None 时调用方照旧用 state.client；返回客户端时用它做 download_media。
    由「信号量先于 borrow」的次序保证不饿死：可并行下载数（≤ 并发数 target）
    恒不大于存活 worker 数，借出必有着落。

    借出前验活：池没有自动重连守护，空闲停靠期间的网络抖动会把连接静默杀死
    （实测 2026-09-07：04:00 代理断连把 20 条 worker 全数陈尸池中，此后每个
    下载任务的第 1 次尝试都 0 秒败在「Cannot send requests while disconnected」，
    白烧 1 次重试预算，个别任务耗尽重试永久失败）。所以借出时发现连接已死就
    地重连（带超时，防代理黑洞把借出挂死）；重连失败（网络仍断）也照常借出
    —— 扣下/换走会把池抽干饿死借方，交给下载自身的重试路径兜底不劣于旧版。
    """
    q = state.DOWNLOAD_WORKER_QUEUE
    if q is None:
        return None
    client = await q.get()
    if id(client) not in _WORKER_LABELS:   # 自愈：池若在 spawn 之外被组装也能编号
        _register_worker(client)
    _WORKER_STATE[id(client)] = "BUSY"
    if not client.is_connected():
        try:
            await asyncio.wait_for(
                client.connect(), timeout=_WORKER_CONNECT_TIMEOUT
            )
            mark_healthy(client)
            logger.info("🧵 下载 worker 借出时已断开，已重新连接")
        except Exception as e:
            mark_unhealthy(client, f"{type(e).__name__}: {e}")
            logger.warning(
                f"下载 worker 借出时重连失败（{type(e).__name__}: {e}），"
                "照常借出，由下载重试兜底"
            )
    return client


async def release(client):
    """归还 worker；顺带收敛：空闲多于目标且存活仍 ≥ 目标时，摘一条断开。

    始终先放回队列（live 只降不破 target，borrow 才不会饿死），再把超出目标
    的空闲摘一条断开 —— 多次 /thread 升升降降后不会累积闲置连接，也不会打断
    在途下载。队列已禁用（极边角：池被 reset 而 worker 尚未还回）则直接断开。
    """
    q = state.DOWNLOAD_WORKER_QUEUE
    if q is None:
        _forget_worker(client)
        try:
            await client.disconnect()
        except Exception:
            pass
        return
    _WORKER_STATE[id(client)] = "IDLE"
    q.put_nowait(client)
    target = state.DOWNLOAD_WORKER_TARGET
    if q.qsize() > target and len(state.DOWNLOAD_WORKERS) > target:
        try:
            extra = q.get_nowait()
        except asyncio.QueueEmpty:
            return
        state.DOWNLOAD_WORKERS.remove(extra)
        _forget_worker(extra)
        try:
            await extra.disconnect()
            logger.info("🧵 下载 worker 收敛：断开一条超额空闲连接")
        except Exception as e:
            logger.warning(f"断开超额下载 worker 失败：{e}")


async def shutdown():
    """断开全部下载 worker（app.main 退出前调用，尽力而为）。"""
    workers = state.DOWNLOAD_WORKERS
    state.DOWNLOAD_WORKERS = []
    state.DOWNLOAD_WORKER_QUEUE = None
    state.DOWNLOAD_WORKER_TARGET = 0
    _clear_observation()
    for client in workers:
        try:
            await client.disconnect()
        except Exception:
            pass
    if workers:
        logger.info(f"🧵 已断开 {len(workers)} 条下载 worker 连接")
