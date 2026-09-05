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
from . import whitelist
from . import platform
from .config import (
    CLEAN_COMMANDS,
    CLEAN_MESSAGE_AGE_MINUTES,
    CLEAN_NOTIFICATION_PREFIXES,
    CLEAR_TIME_CONFIG_FILE,
    DEFAULT_CLEAR_INTERVAL_SECONDS,
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


def is_cleanup_message(message) -> bool:
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

        # /wl 指令（/wl、/wl add @xxx、/wl del 123 ...）
        if whitelist.is_wl_command(text):
            return True

        # /queue、/retry 指令（含子命令）
        if queue.is_queue_command(text) or queue.is_retry_command(text):
            return True

        # 程序自己发送/产生的链接指令：
        # 只要消息中包含抖音 / Instagram URL，就视为下载指令，纳入定时清理。
        if platform.extract_douyin_urls(text) or platform.extract_instagram_urls(text):
            return True

        return any(
            text.startswith(prefix) for prefix in CLEAN_NOTIFICATION_PREFIXES
        )
    except Exception:
        return False


async def cleanup_saved_messages_once():
    """清理一段时间以前的程序指令和通知。"""
    if not state.MY_ID:
        return

    try:
        from datetime import datetime, timezone, timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(
            minutes=CLEAN_MESSAGE_AGE_MINUTES
        )

        delete_ids = []

        # Saved Messages 通常不会很多，逐页检查即可。
        async for message in state.client.iter_messages("me", limit=300):
            if not is_cleanup_message(message):
                continue

            msg_date = message.date
            if msg_date and msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)

            if msg_date and msg_date < cutoff:
                delete_ids.append(message.id)

        if delete_ids:
            await state.client.delete_messages("me", delete_ids)
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
    """bot 菜单对话清理决策：删除超过时限的消息，但始终保留最新一条带按钮的菜单。

    messages: 按时间从新到旧排列的 [{"id", "age_minutes", "has_buttons"}, ...]
    返回 (要删除的 id 列表, 要保留的 id 集合)
    """
    keep = set()
    delete = []
    for m in messages:
        if m["has_buttons"] and not keep:
            keep.add(m["id"])
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
    try:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        infos = []
        async for m in state.client.iter_messages(state.BOT_ID, limit=300):
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
                }
            )
        del_ids, _ = plan_bot_chat_cleanup(infos, CLEAN_MESSAGE_AGE_MINUTES)
        if del_ids:
            await state.client.delete_messages(state.BOT_ID, del_ids)
            logger.info(
                f"🧹 自动清理 bot 菜单对话：删除 {len(del_ids)} 条消息"
            )
        else:
            logger.info("⏱ bot 菜单对话清理检查完成：没有需要删除的消息")
    except Exception as e:
        logger.exception(f"自动清理 bot 菜单对话失败：{e}")
