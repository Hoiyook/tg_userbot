"""bot 账号的菜单交互：回调分发 handle_menu_action + 消息/回调处理器。

bot_client 在 app.main() 里创建并注册事件；这里只做 owner-only 判定
（event.chat_id == state.MY_ID）后执行。按钮菜单只存在于 bot 私聊——
按钮/键盘是 bot 账号专属能力，userbot 账号发不出。

运行态一律读 state.*（bot_client / MY_ID / QUEUE / QUEUE_LOCK / EXECUTING /
DOWNLOAD_CONCURRENCY / WHITELIST_CHATS）；共享服务函数分布在 text / menu /
queue / thread / whitelist / cleanup / cd2 模块，以模块对象调用。
"""
import asyncio

from telethon import Button
from telethon.utils import get_peer_id

from . import state
from . import text
from . import menu
from . import queue
from . import thread
from . import whitelist
from . import cleanup
from . import cd2
from .config import DONE_DEFAULT_LINES
from .log import logger
from .naming import sanitize_filename
from .sources import entity_display_name


async def handle_menu_action(action, arg, event):
    """按按钮动作执行并返回 (新文本, 新按钮)；返回 (None, None) 表示不改动消息。"""
    if action == "home":
        return menu.build_main_menu_text(), menu.main_menu_buttons()
    if action == "status":
        return text.status_text(), menu.back_home_buttons()
    if action == "progress":
        return text.progress_text(), menu.back_home_buttons()
    if action == "done":
        return text.done_reply_text(DONE_DEFAULT_LINES), menu.back_home_buttons()
    if action == "wl":
        return text.wl_list_text(), menu.wl_menu_buttons()
    if action == "wl_add":
        if arg is None:
            return (
                "📋 添加白名单：\n\n"
                "转发一条来自目标 chat 的消息到本对话，"
                "我会读取转发来源并请你确认添加。",
                menu.back_home_buttons(),
            )
        try:
            entity = await state.bot_client.get_entity(int(arg))
            chat_id = get_peer_id(entity)
            title = sanitize_filename(
                entity_display_name(entity) or f"chat_{chat_id}"
            )
            ok, msg = whitelist.add_to_whitelist(chat_id, title)
            return msg, menu.back_home_buttons()
        except Exception as e:
            logger.warning(f"bot 菜单添加白名单失败：{e}")
            return "❌ 添加失败，无法找到该 chat", menu.back_home_buttons()
    if action == "wl_del":
        ok, msg = whitelist.del_from_whitelist(arg or "")
        return msg, menu.back_home_buttons()
    if action == "thread":
        if arg is None:
            return (
                f"🧵 当前并发下载数：{state.DOWNLOAD_CONCURRENCY}\n选择新值：",
                menu.thread_menu_buttons(),
            )
        ok, msg = thread.apply_thread_limit(arg)
        return msg, menu.back_home_buttons()
    if action == "clean":
        count = cleanup.clean_temp_files()
        return f"🧹 清理完成，共删除 {count} 个临时文件", menu.back_home_buttons()
    if action == "cd2":
        return await cd2.cd2_start_or_status(), menu.back_home_buttons()
    if action == "cd2_stop":
        return await cd2.cd2_stop_or_status(), menu.back_home_buttons()
    if action == "bak":
        return cd2.backup_records_text(), menu.back_home_buttons()
    if action == "queue":
        return queue.format_queue_text(state.QUEUE), menu.queue_menu_buttons()
    if action == "queue_del":
        async with state.QUEUE_LOCK:
            before = len(state.QUEUE["tasks"])
            state.QUEUE["tasks"] = [
                r for r in state.QUEUE["tasks"] if r.get("id") != arg
            ]
            removed_any = len(state.QUEUE["tasks"]) != before
            if removed_any:
                queue.save_queue(state.QUEUE)
        return (
            ("✅ 已从队列移除" if removed_any else "❌ 任务已不存在"),
            menu.back_home_buttons(),
        )
    if action == "retry":
        return queue.format_retry_text(state.QUEUE), menu.retry_menu_buttons()
    if action == "retry_run":
        async with state.QUEUE_LOCK:
            record = next(
                (r for r in state.QUEUE["retry"] if r.get("id") == arg), None
            )
        if record is None:
            return "❌ 任务已不存在", menu.back_home_buttons()
        if record["id"] in state.EXECUTING:
            return "⏳ 该任务正在执行中", menu.back_home_buttons()
        asyncio.create_task(queue.execute_queued_task(record))
        return (
            f"▶️ 已重新执行：{record.get('label', '')}",
            menu.back_home_buttons(),
        )
    if action == "retry_del":
        async with state.QUEUE_LOCK:
            before = len(state.QUEUE["retry"])
            state.QUEUE["retry"] = [
                r for r in state.QUEUE["retry"] if r.get("id") != arg
            ]
            removed_any = len(state.QUEUE["retry"]) != before
            if removed_any:
                queue.save_queue(state.QUEUE)
        return (
            ("✅ 已从待重试列表移除" if removed_any else "❌ 任务已不存在"),
            menu.back_home_buttons(),
        )
    if action == "back":
        return menu.build_main_menu_text(), menu.main_menu_buttons()
    return None, None


async def bot_message_handler(event):
    """bot 账号收到 owner 私聊消息：转发消息走确认添加，其余显示主菜单。"""
    if event.out or state.MY_ID is None or event.chat_id != state.MY_ID:
        return
    message = event.message
    text = (message.message or "").strip()
    fwd = getattr(message, "fwd_from", None)
    from_id = getattr(fwd, "from_id", None) if fwd else None

    if from_id:
        chat_id, title = await whitelist.resolve_wl_target(
            state.bot_client, None, fwd
        )
        if chat_id is None:
            logger.warning("bot 菜单解析转发来源失败")
            await state.bot_client.send_message(state.MY_ID, "❌ 无法解析转发来源")
            return
        if chat_id == state.MY_ID:
            await state.bot_client.send_message(
                state.MY_ID, "✅ Saved Messages 始终生效，无需加入白名单"
            )
        elif chat_id in state.WHITELIST_CHATS:
            await state.bot_client.send_message(
                state.MY_ID,
                f"✅ 该 chat 已在白名单："
                f"{state.WHITELIST_CHATS[chat_id]} ({chat_id})",
            )
        else:
            await state.bot_client.send_message(
                state.MY_ID,
                f"检测到转发来源：{title} ({chat_id})\n\n是否加入下载白名单？",
                buttons=[
                    [Button.inline(
                        "✅ 添加", menu.encode_menu_data("wl_add", str(chat_id))
                    )],
                    [Button.inline("❌ 取消", menu.encode_menu_data("home"))],
                ],
            )
        return

    # 任意文本（含 /start）→ 主菜单
    logger.info(f"🤖 bot 菜单：owner 发送 {text[:30]!r}，显示主菜单")
    await state.bot_client.send_message(
        state.MY_ID,
        menu.build_main_menu_text(),
        buttons=menu.main_menu_buttons(),
    )


async def bot_callback_handler(event):
    """bot 按钮回调：解析动作、执行、原地更新消息。"""
    if state.MY_ID is None or event.chat_id != state.MY_ID:
        return
    try:
        await event.answer()
    except Exception:
        pass
    action, arg = menu.parse_menu_data(event.data)
    logger.info(f"🤖 bot 菜单回调：{action} {arg or ''}")
    try:
        text, buttons = await handle_menu_action(action, arg, event)
        if text is not None:
            await event.edit(
                text, buttons=buttons, link_preview=False
            )
    except Exception as e:
        logger.exception(f"bot 菜单处理失败：{e}")
        try:
            await event.edit(
                "❌ 操作失败，请查看日志", buttons=menu.back_home_buttons()
            )
        except Exception:
            pass
