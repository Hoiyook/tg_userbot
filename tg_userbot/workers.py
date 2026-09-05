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
from .config import API_HASH, API_ID, CONNECTION_TYPE, PROXY
from .log import logger

# 单条 worker connect() 的超时（秒）：代理节点卡住时不能拖住启动/扩容
_WORKER_CONNECT_TIMEOUT = 45


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
        auto_reconnect=True,
        proxy=PROXY,
        receive_updates=False,
    )
    await asyncio.wait_for(
        client.connect(), timeout=_WORKER_CONNECT_TIMEOUT
    )
    return client


def _reset_pool():
    """把池状态清成「禁用」：borrow() 一律返回 None，下载回退 state.client。"""
    state.DOWNLOAD_WORKERS = []
    state.DOWNLOAD_WORKER_QUEUE = None
    state.DOWNLOAD_WORKER_TARGET = 0


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
    """
    q = state.DOWNLOAD_WORKER_QUEUE
    if q is None:
        return None
    return await q.get()


async def release(client):
    """归还 worker；顺带收敛：空闲多于目标且存活仍 ≥ 目标时，摘一条断开。

    始终先放回队列（live 只降不破 target，borrow 才不会饿死），再把超出目标
    的空闲摘一条断开 —— 多次 /thread 升升降降后不会累积闲置连接，也不会打断
    在途下载。队列已禁用（极边角：池被 reset 而 worker 尚未还回）则直接断开。
    """
    q = state.DOWNLOAD_WORKER_QUEUE
    if q is None:
        try:
            await client.disconnect()
        except Exception:
            pass
        return
    q.put_nowait(client)
    target = state.DOWNLOAD_WORKER_TARGET
    if q.qsize() > target and len(state.DOWNLOAD_WORKERS) > target:
        try:
            extra = q.get_nowait()
        except asyncio.QueueEmpty:
            return
        state.DOWNLOAD_WORKERS.remove(extra)
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
    for client in workers:
        try:
            await client.disconnect()
        except Exception:
            pass
    if workers:
        logger.info(f"🧵 已断开 {len(workers)} 条下载 worker 连接")
