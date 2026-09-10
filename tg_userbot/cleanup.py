"""清理子系统：Saved Messages / bot 菜单对话的定时与手动清理。

含清理解析谓词（is_cleanup_message）、单文件时代 `clear_program_messages`
死代码已按重构计划删除（/clearmsg 手动清理逻辑内联在 handle_command）。

运行态一律走 state.*：MY_ID / client / bot_client / BOT_ID /
CLEAR_INTERVAL_SECONDS / CLEAR_TIME_CHANGED。跨模块运行时谓词（thread /
whitelist / queue / platform 的 is_* / extract_*）以模块对象调用，符合
「运行时模块间不顶层 from-import 函数」约定；history 为纯叶子允许导入。
"""
import os
import re
import json
import asyncio

from . import state
from . import queue
from . import thread
from . import dedup
from . import whitelist
from . import platform
from . import stats
from . import finder
from . import chrome_client
from .config import (
    CLEANUP_DELETE_TIMEOUT,
    CLEANUP_FETCH_TIMEOUT,
    CLEAN_COMMANDS,
    CLEAN_MESSAGE_AGE_MINUTES,
    CLEAN_NOTIFICATION_PREFIXES,
    PERSISTENT_NOTIFICATION_PREFIXES,
    CLEAR_TIME_CONFIG_FILE,
    DEFAULT_CLEAR_INTERVAL_SECONDS,
    REPORT_STATUS_PREFIX,
    SAVE_FOLDER,
)
from .history import is_done_command
from .log import logger
from .sources import is_downloadable


def format_clear_interval(seconds):
    seconds = int(seconds)
    if seconds % 3600 == 0:
        return f"{seconds // 3600}小时"
    if seconds % 60 == 0:
        return f"{seconds // 60}分钟"
    return f"{seconds}秒"


def parse_clear_interval(value):
    value = value.strip().lower()
    if value in {"off", "0", "关闭"}:
        return 0
    match = re.fullmatch(r"(\d+)(s|m|h)", value)
    if not match:
        raise ValueError("格式错误")
    seconds = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]
    if seconds < 10:
        raise ValueError("清理间隔不能小于 10 秒")
    return seconds


def load_clear_interval():
    """启动时读取持久化的自动清理间隔，写入 state.CLEAR_INTERVAL_SECONDS。

    读取失败时重置为默认（与单文件时代行为一致：直接覆盖当前值）。
    """
    try:
        if os.path.exists(CLEAR_TIME_CONFIG_FILE):
            with open(CLEAR_TIME_CONFIG_FILE, "r", encoding="utf-8") as f:
                value = int(json.load(f).get(
                    "interval_seconds", DEFAULT_CLEAR_INTERVAL_SECONDS
                ))
            if value != 0 and value < 10:
                value = DEFAULT_CLEAR_INTERVAL_SECONDS
            state.CLEAR_INTERVAL_SECONDS = value
    except Exception as e:
        state.CLEAR_INTERVAL_SECONDS = DEFAULT_CLEAR_INTERVAL_SECONDS
        logger.warning(
            f"读取自动清理配置失败，使用默认 "
            f"{DEFAULT_CLEAR_INTERVAL_SECONDS // 60} 分钟：{e}"
        )


def save_clear_interval(seconds):
    try:
        with open(CLEAR_TIME_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"interval_seconds": int(seconds)},
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        logger.warning(f"保存自动清理配置失败：{e}")


def is_setcleartime_command(text):
    return bool(re.fullmatch(
        r"/setcleartime(?:\s+.*)?", text.strip(), re.IGNORECASE
    ))


def is_cleanup_message(message, include_persistent=False) -> bool:
    """判断 Saved Messages 中的消息是否属于程序指令/通知。

    真实媒体（含白名单转发进收藏夹待下载/已下载的副本）一律保留，即使
    caption 里含抖音 URL 或通知前缀——副本是用户明言要留的记录/收藏。
    纯文本命令/通知/抖音链接指令消息才清理（is_downloadable 对纯 WebPage
    预览返回 False，带链接的纯文本指令照旧清理）。

    include_persistent=True（仅 /clearmsg 手动批量清理使用）时，持久保留
    的程序通知（PERSISTENT_NOTIFICATION_PREFIXES，如 Chrome 下载结果报告）
    也算清理对象；自动清理循环用默认 False——结果报告用户可能晚些才看，
    瞬态清理会让人错过（2026-09-09 验收实测）。
    """
    """判断 Saved Messages 中的消息是否属于程序指令/通知。

    真实媒体（含白名单转发进收藏夹待下载/已下载的副本）一律保留，即使
    caption 里含抖音 URL 或通知前缀——副本是用户明言要留的记录/收藏。
    纯文本命令/通知/抖音链接指令消息才清理（is_downloadable 对纯 WebPage
    预览返回 False，带链接的纯文本指令照旧清理）。
    """
    try:
        if is_downloadable(message):
            return False

        text = (message.message or "").strip()
        if text in CLEAN_COMMANDS:
            return True

        if is_setcleartime_command(text):
            return True

        # /done 指令（/done 或 /done 数字）
        if is_done_command(text):
            return True

        # /thread 指令（/thread 或 /thread 数字）
        if thread.is_thread_command(text):
            return True

        # /dedup 指令（/dedup、/dedup on、/dedup off）
        if dedup.is_dedup_command(text):
            return True

        # /wl 指令（/wl、/wl add @xxx、/wl del 123 ...）
        if whitelist.is_wl_command(text):
            return True

        # /queue、/retry 指令（含子命令）
        if queue.is_queue_command(text) or queue.is_retry_command(text):
            return True

        # /stats 指令（台账，含天数参数）
        if stats.is_stats_command(text):
            return True

        # /find 指令（媒体下落查询，含关键字）
        if finder.is_find_command(text):
            return True

        # /chrome* 指令（Chrome Agent，含 URL 参数）
        if chrome_client.is_chrome_dispatch(text):
            return True

        # 程序自己发送/产生的链接指令：
        # 只要消息中包含抖音 / Instagram URL，就视为下载指令，纳入定时清理。
        if platform.extract_douyin_urls(text) or platform.extract_instagram_urls(text):
            return True

        prefixes = CLEAN_NOTIFICATION_PREFIXES
        if include_persistent:
            prefixes = tuple(prefixes) + tuple(PERSISTENT_NOTIFICATION_PREFIXES)
        return any(
            text.startswith(prefix) for prefix in prefixes
        )
    except Exception:
        return False


async def _collect_messages(client, entity, limit):
    """把 iter_messages 拉成列表（供 _shielded 在子任务上跑）。"""
    out = []
    async for m in client.iter_messages(entity, limit=limit):
        out.append(m)
    return out


async def _shielded(proc, timeout, what):
    """在子任务上跑一个会产生网络请求的协程 proc()，返回其成功结果。

    本模块所有网络请求（iter_messages / delete_messages）都经它收口。原因与
    queue/download 对取消息、传字节的处理同源：telethon 断线会对 pending
    请求 future 调 cancel()，py3.8+ 的 CancelledError 是 BaseException，会绕开
    except Exception 一路冒上来。清理若把它当停服信号漏给 cleanup_loop，外层
    except asyncio.CancelledError 会记「🛑 已停止」并 re-raise → 自动清理任务
    永久退出，而进程照常重连下载（实测：主客户端中途掉线正撞上清理在拉消息，
    清理当场被杀，此后数小时收藏夹的下载通知再无人清理）。这里把请求放进子
    任务、结局一律经 result() 读取：子任务以 CancelledError 收场 = 网络层取消
    → 返回 None（本轮跳过、下轮再试）；只有清理任务本身被真取消（停服）才在
    此 await 处抛 CancelledError 原样上抛。返回值 None 一律表示「本轮没做成」。
    """
    task = asyncio.ensure_future(proc())
    try:
        try:
            await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
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
    except asyncio.CancelledError:
        raise


async def _fetch_for_cleanup(client, entity, limit):
    """带超时地拉回待清理消息；取不到（超时/断线取消/异常）返回 None → 本轮跳过。

    telethon 请求没有读超时，代理节点卡住时 iter_messages 会无限等待；经
    _shielded 用超时兜住，不让清理周期被一条僵死连接永久卡住。
    """
    return await _shielded(
        lambda: _collect_messages(client, entity, limit),
        CLEANUP_FETCH_TIMEOUT,
        "拉取待清理消息",
    )


async def cleanup_saved_messages_once():
    """清理一段时间以前的程序指令和通知。"""
    if not state.MY_ID:
        return
    cli = state.client
    if cli is None or not cli.is_connected():
        logger.info("⏱ 主客户端未连接，跳过本轮 Saved Messages 清理")
        return

    try:
        from datetime import datetime, timezone, timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(
            minutes=CLEAN_MESSAGE_AGE_MINUTES
        )

        delete_ids = []
        # Saved Messages 通常不会很多，逐条检查即可。
        messages = await _fetch_for_cleanup(cli, "me", 300)
        if messages is None:
            return
        for message in messages:
            if not is_cleanup_message(message):
                continue

            msg_date = message.date
            if msg_date and msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)

            if msg_date and msg_date < cutoff:
                delete_ids.append(message.id)

        if delete_ids:
            result = await _shielded(
                lambda: cli.delete_messages("me", delete_ids),
                CLEANUP_DELETE_TIMEOUT,
                "删除 Saved Messages 程序消息",
            )
            if result is None:
                return
            logger.info(
                f"🧹 自动清理 Saved Messages：删除 {len(delete_ids)} 条程序消息"
            )
        else:
            logger.info("⏱ 自动清理检查完成：没有超过时限的程序消息")

    except Exception as e:
        logger.exception(f"自动清理 Saved Messages 失败：{e}")


async def cleanup_loop():
    """后台定时清理任务。"""
    while True:
        try:
            if state.CLEAR_INTERVAL_SECONDS <= 0:
                if state.CLEAR_TIME_CHANGED is not None:
                    await state.CLEAR_TIME_CHANGED.wait()
                    state.CLEAR_TIME_CHANGED.clear()
                else:
                    await asyncio.sleep(60)
                continue

            interval = state.CLEAR_INTERVAL_SECONDS
            if state.CLEAR_TIME_CHANGED is not None:
                try:
                    await asyncio.wait_for(
                        state.CLEAR_TIME_CHANGED.wait(), timeout=interval
                    )
                    state.CLEAR_TIME_CHANGED.clear()
                    continue
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(interval)

            if state.CLEAR_INTERVAL_SECONDS != interval:
                continue
            await cleanup_saved_messages_once()
            await cleanup_bot_chat_once()
        except asyncio.CancelledError:
            logger.info("🛑 Saved Messages 自动清理任务已停止")
            raise
        except Exception as e:
            logger.exception(f"自动清理循环异常：{e}")
            await asyncio.sleep(30)


def clean_temp_files(root=None):
    """删除 .download 临时文件，返回删除数量。命令与 bot 菜单共用。"""
    root = root or SAVE_FOLDER
    count = 0
    for dirpath, dirs, files in os.walk(root):
        for filename in files:
            if filename.endswith(".download"):
                path = os.path.join(dirpath, filename)
                try:
                    os.remove(path)
                    count += 1
                except Exception as e:
                    logger.warning(f"清理失败：{path} | {e}")
    return count


def plan_bot_chat_cleanup(messages, age_limit):
    """bot 菜单对话清理决策：删除超过时限的消息，但始终保留两条「活消息」——
    最新一条带按钮的菜单，以及最新一条 Runtime Reporter 状态面板。

    面板也必须留（2026-09-10 起汇报发到这个对话）：它靠 edit_message 原地刷新，
    被删掉后下一轮会 MessageIdInvalid → 重建，于是每两分钟多一条面板、永远刷屏。

    messages: 按时间从新到旧排列
        [{"id", "age_minutes", "has_buttons", "is_panel"}, ...]
    返回 (要删除的 id 列表, 要保留的 id 集合)
    """
    keep = set()
    delete = []
    kept_menu = False
    kept_panel = False
    for m in messages:
        if m.get("has_buttons") and not kept_menu:
            keep.add(m["id"])
            kept_menu = True
            continue
        if m.get("is_panel") and not kept_panel:
            keep.add(m["id"])
            kept_panel = True
            continue
        if m["age_minutes"] > age_limit:
            delete.append(m["id"])
    return delete, keep


async def cleanup_bot_chat_once():
    """清理 bot 菜单对话：删除超时消息，保留最新一条带按钮的菜单。

    注意：必须用 userbot 账号（client）读写这个对话，peer 是 bot 的
    用户 id（BOT_ID）——bot 账号调用 messages.getHistory 会被服务端
    拒绝（bot API 限制），且从 userbot 视角 bot 对话的 peer 不是 MY_ID。
    """
    if not state.MY_ID or not state.bot_client or not state.BOT_ID:
        return
    cli = state.client
    if cli is None or not cli.is_connected():
        logger.info("⏱ 主客户端未连接，跳过本轮 bot 菜单对话清理")
        return
    try:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        infos = []
        messages = await _fetch_for_cleanup(cli, state.BOT_ID, 300)
        if messages is None:
            return
        for m in messages:
            if m.date:
                if m.date.tzinfo is None:
                    m.date = m.date.replace(tzinfo=timezone.utc)
                age = (now - m.date).total_seconds() / 60.0
            else:
                age = 0.0
            infos.append(
                {
                    "id": m.id,
                    "age_minutes": age,
                    "has_buttons": bool(m.buttons),
                    # Runtime Reporter 面板：要长期保留（见 plan_bot_chat_cleanup）
                    "is_panel": bool(
                        m.buttons is None
                        and (m.message or "").startswith(REPORT_STATUS_PREFIX)
                    ),
                }
            )
        del_ids, _ = plan_bot_chat_cleanup(infos, CLEAN_MESSAGE_AGE_MINUTES)
        if del_ids:
            result = await _shielded(
                lambda: cli.delete_messages(state.BOT_ID, del_ids),
                CLEANUP_DELETE_TIMEOUT,
                "删除 bot 菜单对话消息",
            )
            if result is None:
                return
            logger.info(
                f"🧹 自动清理 bot 菜单对话：删除 {len(del_ids)} 条消息"
            )
        else:
            logger.info("⏱ bot 菜单对话清理检查完成：没有需要删除的消息")
    except Exception as e:
        logger.exception(f"自动清理 bot 菜单对话失败：{e}")
