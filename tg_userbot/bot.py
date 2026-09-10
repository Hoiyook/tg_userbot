"""bot 账号的菜单交互：回调分发 handle_menu_action + 消息/回调处理器。

bot_client 在 app.main() 里创建并注册事件；这里只做 owner-only 判定
（event.chat_id == state.MY_ID）后执行。按钮菜单只存在于 bot 私聊——
按钮/键盘是 bot 账号专属能力，userbot 账号发不出。

运行态一律读 state.*（bot_client / MY_ID / QUEUE / QUEUE_LOCK / EXECUTING /
DOWNLOAD_CONCURRENCY / WHITELIST_CHATS）；共享服务函数分布在 text / menu /
queue / thread / whitelist / cleanup / cd2 模块，以模块对象调用。
"""
import asyncio
import time

from telethon import Button
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault
from telethon.utils import get_peer_id

from . import state
from . import text
from . import menu
from . import queue
from . import dedup
from . import thread
from . import whitelist
from . import cleanup
from . import cd2
from . import stats
from . import finder
from . import caption_filter
from . import commands
from . import config
from .config import DONE_DEFAULT_LINES, REPORT_STATUS_PREFIX
from .log import logger
from .naming import sanitize_filename
from .sources import entity_display_name

# bot 原生命令面板：注册后 owner 在 bot 对话点输入框 "/" 即见带说明的命令
# 菜单（此前从未注册，命令全靠记）。这些命令在 bot 对话同样执行
# （bot_message_handler 分发到 commands.handle_command），未识别的回落主菜单。
BOT_COMMANDS = (
    ("stats", "台账：今日转发/解析/成功/失败汇总"),
    ("find", "按关键字查一条媒体的下落"),
    ("progress", "查看进行中下载的实时进度"),
    ("queue", "查看下载队列"),
    ("retry", "查看待重试列表"),
    ("done", "查看最近下载记录"),
    ("thread", "查看/设置并行下载路数"),
    ("dedup", "查看/设置重复媒体去重"),
    ("caption_filter", "查看/修改 Caption 命名清洗规则"),
    ("wl", "查看下载白名单"),
    ("clean", "清理 .download 临时文件"),
    ("chrome_start", "启动 Chrome 下载 Agent"),
    ("chrome_stop", "停止 Chrome 下载 Agent"),
    ("chrome_status", "查看 Chrome Agent 状态"),
    ("chrome_tasks", "查看可取消的 Chrome 任务"),
    ("chrome_cancel", "取消 Chrome 任务：/chrome_cancel <序号>"),
    ("chrome", "用 Chrome 下载：/chrome <URL>"),
    ("clearmsg", "清理程序产生的消息"),
    ("help", "查看全部命令"),
)


async def register_bot_commands(client):
    """注册命令面板（启动时一次，服务端持久，重连无需重注）。"""
    await client(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(),
        lang_code="",
        commands=[
            BotCommand(command=name, description=desc)
            for name, desc in BOT_COMMANDS
        ],
    ))
    logger.info(f"🤖 bot 命令面板已注册：{len(BOT_COMMANDS)} 个命令")


def open_input_window(kind):
    """开一个「等待下一条文本」的输入窗口，并关掉其它两个。

    同一时刻只允许一个窗口开着（cookie / 查询 / Caption 清洗）：三个窗口的
    判定是 if 顺序执行，若同时非零，排在前的会把本该给后者的文本吃掉——
    cookie 排最前、代价也最重（一段 Caption 规则会被当成抖音 cookie 存进
    tg_secrets.json）。kind：\"cookie\" / \"find\" / \"add\"|\"del\"|\"test\"。
    """
    state.COOKIE_INPUT_UNTIL = 0.0
    state.FIND_INPUT_UNTIL = 0.0
    state.CAPTION_INPUT_UNTIL = 0.0
    state.CAPTION_INPUT_MODE = ""
    if kind == "cookie":
        state.COOKIE_INPUT_UNTIL = (
            time.monotonic() + config.COOKIE_INPUT_WINDOW_SECONDS
        )
    elif kind == "find":
        state.FIND_INPUT_UNTIL = (
            time.monotonic() + config.FIND_INPUT_WINDOW_SECONDS
        )
    else:
        state.CAPTION_INPUT_UNTIL = (
            time.monotonic() + config.CAPTION_INPUT_WINDOW_SECONDS
        )
        state.CAPTION_INPUT_MODE = kind


async def handle_menu_action(action, arg, event):
    """按按钮动作执行并返回 (新文本, 新按钮)；返回 (None, None) 表示不改动消息。"""
    if action == "home":
        return menu.build_main_menu_text(), menu.main_menu_buttons()
    if action == "status":
        return text.status_text(), menu.back_home_buttons()
    if action == "progress":
        return text.progress_text(), [
            [Button.inline("🔄 刷新", menu.encode_menu_data("progress")),
             Button.inline("🔙 返回主菜单", menu.encode_menu_data("home"))],
        ]
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
                f"🧵 当前并发下载数：{state.DOWNLOAD_CONCURRENCY}"
                "（每条占一条独立连接）\n选择新值：",
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
    if action == "stats":
        # 窗口天数随按钮参数（m:stats:<n>），非法/缺省回落今日；
        # 上限即日志保留天数（更早无数据）。
        try:
            days = int(arg) if arg else 1
        except ValueError:
            days = 1
        days = max(1, min(days, config.LOG_RETENTION_DAYS))
        return stats.stats_text(days), menu.stats_menu_buttons(days)
    if action == "find":
        # 查询按钮没法打字：进入输入窗口（同 cookie 模式），下一条普通文本
        # 即关键字；/ 开头视为命令退出窗口。
        open_input_window("find")
        return (
            "🔍 请直接发送要查询的关键字（发到本对话）。\n\n"
            f"{config.FIND_INPUT_WINDOW_SECONDS} 秒内有效，"
            "超时请重新点【🔍 查询】。发送 / 开头的命令可取消。",
            menu.back_home_buttons(),
        )
    if action == "cookie":
        return menu.cookie_status_text(), menu.cookie_menu_buttons()
    if action == "cookie_set":
        open_input_window("cookie")
        return (
            "🍪 请直接发送 cookie 内容（整段粘贴，发到本对话）。\n\n"
            f"⚠️ 你发的这条消息会被立即删除；"
            f"{config.COOKIE_INPUT_WINDOW_SECONDS} 秒内有效，"
            "超时请重新点【✏️ 更新】。发送 / 开头的命令可取消。",
            menu.back_home_buttons(),
        )
    if action == "cookie_clear":
        err = config.save_douyin_cookie("")
        if err:
            return f"❌ {err}", menu.cookie_menu_buttons()
        return (
            "🗑 已清除（实时生效，抖音链接将走解析 bot 兜底）",
            menu.cookie_menu_buttons(),
        )
    if action == "cookie_imp":
        # 读浏览器 Cookie 库可能等钥匙串授权框，放线程池，不阻塞事件循环
        from . import browser_cookies
        browser = (arg or "").strip().lower()
        cookie, err = await asyncio.get_event_loop().run_in_executor(
            None, browser_cookies.load_browser_cookie_string, browser
        )
        if err:
            return f"❌ {err}", menu.cookie_menu_buttons()
        err = config.save_douyin_cookie(cookie)
        if err:
            return f"❌ {err}", menu.cookie_menu_buttons()
        saved = config.DOUYIN_COOKIE
        sess = "含登录态 sessionid ✅" if "sessionid=" in saved else (
            "⚠️ 未检测到 sessionid（可能非登录态）"
        )
        return (
            f"🌐 已从 {browser} 导入并实时生效\n\n"
            f"长度：{len(saved)} 字符\n"
            f"片段：{config.mask_douyin_cookie(saved)}\n"
            f"{sess}",
            menu.cookie_menu_buttons(),
        )
    if action == "queue":
        return queue.format_queue_text(state.QUEUE), menu.queue_menu_buttons()
    if action == "queue_del":
        # 与 /queue del 同路：执行中的任务先真正取消在途下载再移除记录
        ok, removed, cancelled = await queue.queue_del_task(record_id=arg)
        if not ok:
            return "❌ 任务已不存在", menu.back_home_buttons()
        verb = "🛑 已取消下载并移除" if cancelled else "✅ 已从队列移除"
        return f"{verb}：{removed.get('label', '')}", menu.back_home_buttons()
    if action == "capf":
        return caption_filter.rules_text(), menu.caption_filter_menu_buttons()
    if action in ("capf_add", "capf_del", "capf_test"):
        # 这三个要用户输入内容（规则 / 序号 / 原文）：进输入窗口，下一条普通
        # 文本按 mode 处理（同 cookie / 查询的窗口模式）
        mode = action[len("capf_"):]
        open_input_window(mode)
        return (
            caption_filter.input_prompt(mode),
            menu.caption_filter_menu_buttons(),
        )
    if action in ("capf_reset", "capf_clear"):
        return (
            caption_filter.command_reply(action[len("capf_"):], None),
            menu.caption_filter_menu_buttons(),
        )
    if action == "dedup":
        return dedup.status_text(), menu.dedup_menu_buttons()
    if action == "dedup_toggle":
        msg = dedup.set_enabled(not state.DEDUP_ENABLED)
        return msg, menu.dedup_menu_buttons()
    if action == "retry":
        try:
            page = int(arg) if arg else 1
        except ValueError:
            page = 1
        return (
            queue.format_retry_text(state.QUEUE, page),
            menu.retry_menu_buttons(page),
        )
    if action == "retry_all":
        n = queue.retry_all()
        return (
            (f"🔁 已重放全部待重试任务：{n} 条" if n
             else "🔁 待重试列表为空（或都在执行中）"),
            menu.retry_menu_buttons(1),
        )
    if action == "retry_run":
        async with state.QUEUE_LOCK:
            record = next(
                (r for r in state.QUEUE["retry"] if r.get("id") == arg), None
            )
        if record is None:
            return "❌ 任务已不存在", menu.back_home_buttons()
        if record["id"] in state.EXECUTING:
            return "⏳ 该任务正在执行中", menu.back_home_buttons()
        queue.spawn_execute(record)
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

    # Runtime Reporter 的汇报发到这个对话（面板 + 启动/关闭/异常通知），但它们
    # 不是「给我的指令」。这行必须排在下面两个等待窗口**之前**：cookie 与
    # /find 窗口期内任何非 "/" 开头的文本都会被当成输入内容——面板正文要是
    # 正好落在那 120 秒里，会被当成抖音 cookie 存下来。
    if text.startswith(REPORT_STATUS_PREFIX):
        return

    fwd = getattr(message, "fwd_from", None)
    from_id = getattr(fwd, "from_id", None) if fwd else None

    # cookie 等待窗口：普通文本当作 cookie 内容（/ 开头视为命令退出窗口）。
    # 窗口一次即关：无论存没存上，都不会把后续普通文本误吃成 cookie。
    if state.COOKIE_INPUT_UNTIL and time.monotonic() < state.COOKIE_INPUT_UNTIL:
        if not text.startswith("/"):
            state.COOKIE_INPUT_UNTIL = 0.0
            await _handle_cookie_input(event, text)
            return
        state.COOKIE_INPUT_UNTIL = 0.0

    # 查询等待窗口：普通文本当作 /find 的关键字，执行后窗口即关。
    if state.FIND_INPUT_UNTIL and time.monotonic() < state.FIND_INPUT_UNTIL:
        if not text.startswith("/"):
            state.FIND_INPUT_UNTIL = 0.0
            await _handle_find_input(event, text)
            return
        state.FIND_INPUT_UNTIL = 0.0

    # Caption 清洗等待窗口：普通文本按 mode 当规则 / 序号 / 待清洗原文。
    if (state.CAPTION_INPUT_UNTIL
            and time.monotonic() < state.CAPTION_INPUT_UNTIL):
        if not text.startswith("/"):
            mode = state.CAPTION_INPUT_MODE
            state.CAPTION_INPUT_UNTIL = 0.0
            state.CAPTION_INPUT_MODE = ""
            await _handle_caption_input(mode, text)
            return
        state.CAPTION_INPUT_UNTIL = 0.0
        state.CAPTION_INPUT_MODE = ""

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

    # 注册过的 / 命令在 bot 对话同样执行（命令面板点出来的命令落在本对话）；
    # 未识别的 / 命令（含 /start）回落主菜单，语义与旧行为一致。
    if text.startswith("/"):
        if await commands.handle_command(event, text):
            return

    # 任意文本（含 /start）→ 主菜单
    logger.info(f"🤖 bot 菜单：owner 发送 {text[:30]!r}，显示主菜单")
    await state.bot_client.send_message(
        state.MY_ID,
        menu.build_main_menu_text(),
        buttons=menu.main_menu_buttons(),
    )


async def _handle_find_input(event, text):
    """处理查询等待窗口内发来的关键字：执行 /find 同款查询并回复。"""
    if len(text.strip()) < 2:
        await state.bot_client.send_message(
            state.MY_ID,
            f"🔍 关键字太短（≥2 字符）。请重新点【🔍 查询】再发，"
            f"或直接发 /find <关键字>",
        )
        return
    logger.info(f"🤖 bot 菜单查询：{text.strip()!r}")
    await state.bot_client.send_message(
        state.MY_ID, finder.find_media(text), link_preview=False
    )


async def _handle_caption_input(mode, text):
    """处理 Caption 清洗等待窗口内发来的文本：按 mode 当规则/序号/原文。"""
    action = mode if mode in ("add", "del", "test") else "add"
    logger.info(f"🤖 bot 菜单 Caption 清洗：{action} {text[:40]!r}")
    await state.bot_client.send_message(
        state.MY_ID, caption_filter.command_reply(action, text)
    )


async def _handle_cookie_input(event, text):
    """处理等待窗口内发来的 cookie 文本：立即删原文 → 原子持久化 → 实时生效。

    敏感凭据不留在对话里：即使保存失败也先删原文（失败可重新粘贴）。
    """
    try:
        await event.delete()
    except Exception as e:
        logger.warning(f"删除 cookie 消息失败（不影响保存）：{e}")

    if not text:
        await state.bot_client.send_message(state.MY_ID, "❌ 内容为空，已取消")
        return

    err = config.save_douyin_cookie(text)
    if err:
        await state.bot_client.send_message(state.MY_ID, f"❌ 保存失败：{err}")
        return

    cookie = config.DOUYIN_COOKIE
    sess = "含登录态 sessionid ✅" if "sessionid=" in cookie else (
        "⚠️ 未检测到 sessionid（可能非登录态，公开视频仍可解析）"
    )
    await state.bot_client.send_message(
        state.MY_ID,
        "✅ 抖音 Cookie 已更新，实时生效\n\n"
        f"长度：{len(cookie)} 字符\n"
        f"片段：{config.mask_douyin_cookie(cookie)}\n"
        f"{sess}",
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
    except MessageNotModifiedError:
        # 重复点击生成相同内容（如钥匙串拒绝后连点两次同一导入按钮）：
        # Telegram 拒绝无变化编辑，属正常噪音，静默应答即可
        logger.info("🤖 bot 菜单回调：内容无变化，忽略")
    except Exception as e:
        logger.exception(f"bot 菜单处理失败：{e}")
        try:
            await event.edit(
                "❌ 操作失败，请查看日志", buttons=menu.back_home_buttons()
            )
        except Exception:
            pass
