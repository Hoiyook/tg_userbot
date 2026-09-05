"""Saved Messages 的 / 命令分发器（handle_command）。

只处理来自 “me” 的消息（事件入口 new_message_handler 先判定归属，白名单
chat 无法触发命令）。是各命令服务函数的薄分发器——正文逻辑分别住在
text / thread / whitelist / queue / cleanup 模块，与 bot 按钮菜单共用。
运行态（client / QUEUE / QUEUE_LOCK / EXECUTING / DOWNLOAD_CONCURRENCY /
CLEAR_INTERVAL_SECONDS / CLEAR_TIME_CHANGED）一律读 state.*；跨模块函数
以模块对象调用。
"""
import asyncio

from . import state
from . import text
from . import queue
from . import thread
from . import whitelist
from . import cleanup
from .config import (
    BOT_USERNAME,
    DONE_DEFAULT_LINES,
    DONE_MAX_LINES,
    DOWNLOAD_CONCURRENCY_MAX,
    DOWNLOAD_CONCURRENCY_MIN,
    LOG_FILE,
    SAVE_FOLDER,
)
from .log import logger


async def handle_command(event, cmd_text):
    if cmd_text == "/status":
        await event.reply(text.status_text())
        logger.info("执行命令：/status")
        return True

    if cmd_text == "/folder":
        await event.reply(f"📁 保存目录：\n{SAVE_FOLDER}")
        logger.info("执行命令：/folder")
        return True

    if cmd_text == "/logpath":
        await event.reply(f"📋 日志文件：\n{LOG_FILE}")
        logger.info("执行命令：/logpath")
        return True

    if cmd_text == "/done" or cmd_text.startswith("/done "):
        # 用法：/done、/done 10、/done 关键词、/done 10 关键词
        parts = cmd_text.split(maxsplit=2)
        n = DONE_DEFAULT_LINES
        keyword = None
        if len(parts) >= 2 and parts[1]:
            if parts[1].isdigit():
                n = int(parts[1])
                if len(parts) == 3 and parts[2]:
                    keyword = parts[2].strip().lower()
            else:
                keyword = parts[1].strip().lower()
                n = DONE_MAX_LINES
        if n < 1:
            n = 1
        elif n > DONE_MAX_LINES:
            n = DONE_MAX_LINES

        if keyword is not None:
            # 模糊匹配：大小写不敏感的子串匹配，扫描全部历史
            await event.reply(text.done_reply_text(n, keyword))
            logger.info(f"执行命令：/done 关键词「{keyword}」")
            return True

        await event.reply(text.done_reply_text(n))
        logger.info(f"执行命令：/done {n}")
        return True

    if cmd_text == "/progress" or cmd_text == "/downloading":
        await event.reply(text.progress_text())
        logger.info(f"执行命令：/progress | 进行中 {len(state.ACTIVE_DOWNLOADS)} 个")
        return True

    if cmd_text == "/thread" or cmd_text.startswith("/thread "):
        parts = cmd_text.split(maxsplit=1)
        if len(parts) == 1:
            await event.reply(
                f"🧵 当前并发下载数：{state.DOWNLOAD_CONCURRENCY}\n"
                f"用法：/thread 3（{DOWNLOAD_CONCURRENCY_MIN}-{DOWNLOAD_CONCURRENCY_MAX}）\n"
                "每条下载各占一条独立连接，n 路 ≈ n 倍单路速度"
            )
            logger.info("执行命令：/thread（查询）")
            return True
        ok, msg = thread.apply_thread_limit(parts[1])
        await event.reply(msg)
        logger.info(f"执行命令：/thread {parts[1]} 成功={ok}")
        return True

    parsed_wl = whitelist.parse_wl_command(cmd_text)
    if parsed_wl is not None:
        action, arg = parsed_wl
        logger.info(f"执行命令：/wl {action}")

        if action == "list":
            await event.reply(text.wl_list_text())
            return True

        if action == "add":
            try:
                if arg:
                    try:
                        target = int(arg)
                    except ValueError:
                        target = arg
                    chat_id, title = await whitelist.resolve_wl_target(
                        state.client, target
                    )
                else:
                    # 不带参数：从回复的转发消息里取来源 chat。
                    reply_id = event.message.reply_to_msg_id
                    if not reply_id:
                        await event.reply(
                            "❌ /wl：请带参数（ID 或 @用户名），或回复一条"
                            "从目标 chat 转发的消息后发送 /wl add"
                        )
                        return True
                    reply_msg = await state.client.get_messages(
                        "me", ids=reply_id
                    )
                    if not reply_msg or not getattr(
                        reply_msg, "fwd_from", None
                    ):
                        await event.reply(
                            "❌ /wl：回复的消息不是转发的，取不到来源 chat"
                        )
                        return True
                    chat_id, title = await whitelist.resolve_wl_target(
                        state.client, None, reply_msg.fwd_from
                    )

                if chat_id is None:
                    await event.reply(
                        f"❌ /wl：无法找到该 chat：{arg or '转发来源'}"
                    )
                    return True
                ok, msg = whitelist.add_to_whitelist(chat_id, title)
                await event.reply(msg)
            except Exception as e:
                logger.warning(f"/wl add 失败：{e}")
                await event.reply(
                    f"❌ /wl：无法找到该 chat：{arg or '转发来源'}"
                )
            return True

        if action == "del":
            ok, msg = whitelist.del_from_whitelist(arg or "")
            await event.reply(msg)
            return True

        await event.reply(
            "❌ 用法：/wl list | /wl add <ID或@用户名> | /wl del <ID或序号>"
        )
        return True

    if queue.is_queue_command(cmd_text):
        parts = cmd_text.split(maxsplit=1)
        if len(parts) == 1:
            await event.reply(
                queue.format_queue_text(state.QUEUE), link_preview=False
            )
        elif parts[1].startswith("del "):
            try:
                idx = int(parts[1].split(None, 1)[1])
            except (IndexError, ValueError):
                await event.reply("❌ /queue del 用法：/queue del <序号>")
                return True
            async with state.QUEUE_LOCK:
                ok, removed = queue.queue_remove(state.QUEUE, "tasks", idx)
                if ok:
                    queue.save_queue(state.QUEUE)
            if ok:
                await event.reply(
                    f"✅ 已从队列移除：{removed.get('label', '')}"
                )
            else:
                await event.reply("❌ /queue：序号无效，用 /queue 查看列表")
        else:
            await event.reply("❌ 用法：/queue | /queue del <序号>")
        logger.info(f"执行命令：/queue {parts[1] if len(parts) > 1 else ''}")
        return True

    if queue.is_retry_command(cmd_text):
        parts = cmd_text.split(maxsplit=1)
        if len(parts) == 1:
            await event.reply(
                queue.format_retry_text(state.QUEUE), link_preview=False
            )
        elif parts[1].startswith("del "):
            try:
                idx = int(parts[1].split(None, 1)[1])
            except (IndexError, ValueError):
                await event.reply("❌ /retry del 用法：/retry del <序号>")
                return True
            async with state.QUEUE_LOCK:
                ok, removed = queue.queue_remove(state.QUEUE, "retry", idx)
                if ok:
                    queue.save_queue(state.QUEUE)
            if ok:
                await event.reply(
                    f"✅ 已从待重试列表移除：{removed.get('label', '')}"
                )
            else:
                await event.reply(
                    "❌ /retry：序号无效，用 /retry 查看列表"
                )
        else:
            try:
                idx = int(parts[1])
            except ValueError:
                await event.reply(
                    "❌ 用法：/retry | /retry <序号> | /retry del <序号>"
                )
                return True
            async with state.QUEUE_LOCK:
                retry_list = state.QUEUE["retry"]
                record = (
                    retry_list[idx - 1] if 1 <= idx <= len(retry_list) else None
                )
            if record is None:
                await event.reply("❌ /retry：序号无效，用 /retry 查看列表")
                return True
            if record["id"] in state.EXECUTING:
                await event.reply("⏳ 该任务正在执行中")
                return True
            asyncio.create_task(queue.execute_queued_task(record))
            await event.reply(f"▶️ 已重新执行：{record.get('label', '')}")
        logger.info(f"执行命令：/retry {parts[1] if len(parts) > 1 else ''}")
        return True

    if cmd_text == "/help":
        await event.reply(
            "📖 TG Userbot 命令\n\n"
            "/status - 查看运行状态\n"
            "/folder - 查看保存目录\n"
            "/logpath - 查看日志路径\n"
            "/done - 查看最近下载记录（默认 10 条）\n"
            "/done 20 - 查看最近 20 条下载记录\n"
            "/done 关键词 - 模糊匹配下载记录\n"
            "/done 20 关键词 - 最近 20 条中模糊匹配\n"
            "/progress - 查看进行中下载的进度\n"
            "/thread - 查看当前并发下载数\n"
            f"/thread 5 - 设置并行下载路数为 5"
            f"（{DOWNLOAD_CONCURRENCY_MIN}-{DOWNLOAD_CONCURRENCY_MAX}，每条各占一条独立连接）\n"
            "/wl - 查看下载白名单\n"
            "/wl add @用户名 - 加入白名单（也可回复转发消息后 /wl add）\n"
            "/wl del ID或序号 - 移出白名单\n"
            "/queue - 查看下载队列\n"
            "/queue del 序号 - 从队列移除任务\n"
            "/retry - 查看待重试列表\n"
            "/retry 序号 - 重新执行某条失败任务\n"
            "/retry del 序号 - 从待重试列表移除\n"
            "/clean - 清理 .download 临时文件\n"
            "/clearmsg - 清理程序产生的命令、通知和链接指令\n"
            "/setcleartime 1m - 设置自动清理间隔\n"
            "/setcleartime off - 关闭自动清理\n"
            "/help - 查看帮助\n\n"
            "🎬 抖音/Instagram：发送链接到 Saved Messages，自动转给解析 bot，\n"
            "   回复视频自动转发进收藏夹下载。\n"
            "📥 文件、图片、视频发到（或转发进）Saved Messages 会自动下载。\n"
            "📥 白名单 chat 收到媒体会自动转发进收藏夹下载，并保留副本。\n"
            f"🤖 按钮菜单：给 {BOT_USERNAME} 发任意消息，用按钮操作。"
        )
        logger.info("执行命令：/help")
        return True

    if cmd_text.startswith("/setcleartime"):
        parts = cmd_text.split(maxsplit=1)
        if len(parts) == 1:
            current = (
                "已关闭" if state.CLEAR_INTERVAL_SECONDS <= 0
                else cleanup.format_clear_interval(state.CLEAR_INTERVAL_SECONDS)
            )
            await event.reply(
                f"⏱ 自动清理当前间隔：{current}\n用法：/setcleartime 1m\n"
                "支持：30s、1m、2m、1h\n关闭：/setcleartime off"
            )
            return True
        try:
            seconds = cleanup.parse_clear_interval(parts[1])
        except ValueError:
            await event.reply(
                "❌ 格式错误。示例：/setcleartime 1m、/setcleartime 2m、"
                "/setcleartime 1h、/setcleartime off"
            )
            return True
        state.CLEAR_INTERVAL_SECONDS = seconds
        cleanup.save_clear_interval(seconds)
        if state.CLEAR_TIME_CHANGED is not None:
            state.CLEAR_TIME_CHANGED.set()
        await event.reply(
            "⏸ 自动清理已关闭。"
            if seconds == 0
            else f"✅ 自动清理间隔已设置为 {cleanup.format_clear_interval(seconds)}。"
        )
        return True

    if cmd_text == "/clearmsg":
        logger.info("执行命令：/clearmsg")
        try:
            # 先统计并删除其它程序消息；当前 /clearmsg 留到最后删除。
            delete_ids = []

            async for message in state.client.iter_messages("me", limit=3000):
                if message.id == event.message.id:
                    continue

                if cleanup.is_cleanup_message(message):
                    delete_ids.append(message.id)

            count = len(delete_ids) + 1

            # 先反馈结果，避免当前 /clearmsg 被删除后无法回复。
            await event.reply(
                f"🧹 清理完成，共删除 {count} 条程序相关消息\n\n"
                "已清理：抖音/IG 链接指令、程序通知、程序命令、/clearmsg 指令\n"
                "收藏的媒体副本与普通收藏内容会保留。"
            )

            # 删除其它消息 + 当前 /clearmsg 指令。
            if delete_ids:
                await state.client.delete_messages("me", delete_ids)

            await state.client.delete_messages("me", [event.message.id])

            logger.info(
                f"执行命令：/clearmsg | 删除 {count} 条程序相关消息"
            )

        except Exception as e:
            logger.exception(f"/clearmsg 执行失败：{e}")
            # 如果清理过程中失败，至少尝试保留错误信息。
            try:
                await event.reply(f"❌ 清理失败：{e}")
            except Exception:
                pass

        return True

    if cmd_text == "/clean":
        count = cleanup.clean_temp_files()
        await event.reply(f"🧹 清理完成，共删除 {count} 个临时文件")
        logger.info(f"执行命令：/clean | 删除 {count} 个临时文件")
        return True

    return False
