"""网络调用收口：把会产生网络请求的协程放进子任务跑，结局经 result() 读取。

**存在的唯一理由**：telethon 断线会对 pending 请求 future 调 `cancel()`，
py3.8+ 的 `CancelledError` 是 `BaseException`，会绕开 `except Exception` 一路
冒到调用方的循环外。异步循环（清理、汇报）若把它当停服信号漏出去，外层的
`except asyncio.CancelledError: ... raise` 会当场把循环打死，而进程照常重连
下载——任务静默消失、再无人重启。本项目已被咬过三次：

- 清理循环（`cleanup`，实测：主客户端中途掉线正撞上清理在拉消息，清理被杀，
  此后数小时收藏夹的下载通知再无人清理）；
- 下载任务（`download`，网络层取消在子任务的 `.result()` 处转成可重试的
  `ConnectionError`）；
- 汇报循环（`reporter`，2026-09-11 07:08 起静默停摆数小时，直到用户发现
  「不再主动汇总数据」）。

结构上区分两件事，不靠猜：

- 子任务以 `CancelledError` 收场 = **网络层取消** → 返回 None（本轮跳过）；
- 调用方自己被真取消（停服）→ 在 `await` 处原样上抛，并把子任务一并取消。

顺带解决第二个坑：telethon 请求没有读超时，僵死连接会让调用永久挂住。
`timeout` 到点即取消子任务、返回 None——调用方的循环因此永远不会被一条
僵死连接冻死。

返回值 `None` 一律表示「本轮没做成」，成功值原样返回。
"""
import asyncio

from .log import logger


async def shielded(proc, timeout, what):
    """在子任务上跑协程 `proc()`，返回其成功结果；失败/取消/超时一律 None。

    proc: 无参协程工厂（`lambda: client.send_message(...)`）
    timeout: 秒；到点取消子任务并按「没做成」返回
    what: 中文描述，只进日志（如「发送通知」）
    """
    task = asyncio.ensure_future(proc())
    try:
        try:
            await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            # 调用方被真取消（停服）：回收子任务后原样上抛
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            raise
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.warning(f"⏰ {what} 超时（{timeout}s），本轮跳过")
            return None
        try:
            return task.result()
        except asyncio.CancelledError:
            logger.warning(
                f"{what} 被底层连接取消（网络层 future.cancel()），本轮跳过"
            )
            return None
        except Exception as e:
            logger.warning(f"{what} 失败：{e}")
            return None
    finally:
        # 兜底：任何路径都不留孤儿子任务（未完成即取消）
        if not task.done():
            task.cancel()


def humanize_net_error(err_text):
    """把下载/网络报错的原文翻译成人话原因（通知直接可用）。

    输入是异常原文（含类型名）；按特征词分类，匹配不到就原样截断返回。
    只做展示层翻译，不改变任何重试/判死逻辑——技术原文始终在
    download.log 与数据库 error 字段里可溯源。
    """
    t = str(err_text or "")
    low = t.lower()
    if "404" in t:
        return "站点没有这个文件（404）"
    if "410" in t:
        return "文件已被站点删除（410）"
    if ("peer closed" in low or "remoteprotocolerror" in low
            or "incompletere" in low):
        return "连接被远端中断（网络/CDN 不稳，已自动断点续传重试）"
    if "timed out" in low or "timeout" in low:
        return "下载超时（服务器长时间无响应）"
    if "connection refused" in low or "connection reset" in low \
            or "connecterror" in low:
        return "网络连接失败（代理或服务端不可达）"
    if "floodwait" in low:
        return "Telegram 限流"
    if "大小不符" in t or "incomplete" in low:
        return "下载不完整（大小校验失败，服务端可能中途截断）"
    if "http 5" in low:
        return "服务器错误（5xx）"
    return (t[:120] or "未知错误")
