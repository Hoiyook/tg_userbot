"""程序入口：客户端创建 / 登录看门狗 / 事件入口 / main()。

本模块是唯一在事件循环里构造 loop 绑定原语的地方（事件循环规则，py3.9）：
client、bot_client、AdjustableSemaphore、各 asyncio.Lock/Event 全部只在
app.main()（asyncio.run 内）创建并赋给 state.*。import 本模块**不产生**
任何客户端或异步原语，仅定义函数。

new_message_handler 是唯一的用户事件入口：classify_message_chat 判定归属
后在命令 → 平台链接 → 普通媒体三个分支里早退分发。main() 负责凭据检查、
启动清理、state 装配、事件注册、队列恢复、bot 菜单、自动清理任务与主循环。
"""
import asyncio
import os
import signal
import time

from telethon import TelegramClient, events
from telethon.network.connection import ConnectionTcpFull, ConnectionTcpObfuscated

from . import state
from . import commands
from . import dedup
from . import queue
from . import thread
from . import whitelist
from . import platform
from . import cleanup
from . import bot
from . import workers
from .config import (
    API_HASH,
    API_ID,
    AUTO_CLEAN_SAVED_MESSAGES,
    BOT_KEEPALIVE_INTERVAL,
    BOT_SESSION_NAME,
    BOT_TOKEN,
    BOT_USERNAME,
    CONNECTION_TYPE,
    DOUYIN_BOT_USERNAME,
    INSTAGRAM_BOT_USERNAME,
    IS_TERMUX,
    LOG_FILE,
    LOGIN_RETRIES,
    LOGIN_TIMEOUT_SECONDS,
    ME_LABEL_GRACE_SECONDS,
    ME_LABEL_WINDOW_SECONDS,
    PROXY,
    QUEUE_FETCH_TIMEOUT,
    SAVE_FOLDER,
    SECRETS_FILE,
    SESSION_NAME,
    SERVE_RECONNECT_BASE_DELAY,
    SERVE_RECONNECT_MAX_DELAY,
    TELEGRAM_AUTO_RECONNECT,
    AdjustableSemaphore,
)
from .log import logger
from .naming import compute_final_filename, pick_group_caption_text
from .sources import (
    get_media_type,
    is_downloadable,
    message_source_link,
)


def create_client() -> TelegramClient:
    return TelegramClient(
        SESSION_NAME,
        API_ID,
        API_HASH,
        connection=(
            ConnectionTcpObfuscated
            if CONNECTION_TYPE == "obfuscated"
            else ConnectionTcpFull
        ),
        connection_retries=10,
        retry_delay=3,
        auto_reconnect=TELEGRAM_AUTO_RECONNECT,
        proxy=PROXY,
    )


def create_bot_client() -> TelegramClient:
    """创建 bot 账号客户端（按钮菜单用），连接配置与 userbot 一致。"""
    return TelegramClient(
        BOT_SESSION_NAME,
        API_ID,
        API_HASH,
        connection=(
            ConnectionTcpObfuscated
            if CONNECTION_TYPE == "obfuscated"
            else ConnectionTcpFull
        ),
        connection_retries=10,
        retry_delay=3,
        auto_reconnect=TELEGRAM_AUTO_RECONNECT,
        proxy=PROXY,
    )


async def start_with_retry(cli, bot_token=None):
    """
    带超时看门狗地执行 cli.start()。

    Telethon 的连接/收包路径没有读超时，代理节点卡住时 client.start()
    会无限期挂起。这里用 wait_for 加外部超时（混淆传输全程异步、
    可以被取消），超时后断开重试，直到成功或重试次数用尽。
    bot_token 非空时按 bot 账号登录（按钮菜单客户端）。
    """
    last_error = None
    for attempt in range(1, LOGIN_RETRIES + 1):
        try:
            if bot_token:
                await asyncio.wait_for(
                    cli.start(bot_token=bot_token),
                    timeout=LOGIN_TIMEOUT_SECONDS,
                )
            else:
                await asyncio.wait_for(
                    cli.start(), timeout=LOGIN_TIMEOUT_SECONDS
                )
            return
        except Exception as e:
            last_error = e
            logger.error(
                f"❌ 连接/登录失败，尝试 {attempt}/{LOGIN_RETRIES}："
                f"{type(e).__name__}: {e}"
            )
            if attempt < LOGIN_RETRIES:
                try:
                    await cli.disconnect()
                except Exception:
                    pass
                logger.info("🔄 5 秒后重试连接...")
                await asyncio.sleep(5)

    raise last_error


async def _main_serve():
    """主客户端稳态守护：任何掉线都带指数退避自动重连，直到被取消退出。

    依赖 create_client 关掉 telethon 内建自动重连（TELEGRAM_AUTO_RECONNECT=
    False）：断线会及时让 run_until_disconnected 返回/抛错，由这里接管重连，
    避免内建重连在「连上即再失败」（如代理持续回 HTTP 429）时的无界递归风暴
    （曾叠上千层把事件循环拖死、进程退出）。异常一律兜住 → 本任务永不意外
    return/退出，进程靠它保持存活；外部停止（SIGINT/SIGTERM 设 STOP_EVENT）
    由 main 取消本任务（CancelledError 原样上抛）。run_until_disconnected 结束
    时 telethon 会 disconnect，故每次循环都先确保已连接再挂起。
    """
    delay = SERVE_RECONNECT_BASE_DELAY
    while True:
        if not state.client.is_connected():
            try:
                await start_with_retry(state.client)
                delay = SERVE_RECONNECT_BASE_DELAY
                logger.info("🔁 主客户端连接已恢复")
            except (KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as e:
                logger.error(
                    f"❌ 主客户端重连失败（{type(e).__name__}: {e}），"
                    f"{delay} 秒后重试"
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, SERVE_RECONNECT_MAX_DELAY)
                continue

        try:
            await state.client.run_until_disconnected()
            logger.warning("⚠️ 主客户端连接已断开，稍后自动重连...")
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:
            logger.warning(
                f"⚠️ 主客户端连接异常中断（{type(e).__name__}: {e}），"
                f"{delay} 秒后自动重连..."
            )
        await asyncio.sleep(delay)
        delay = min(delay * 2, SERVE_RECONNECT_MAX_DELAY)


async def _bot_keepalive():
    """bot 菜单连接守护：定期探活，掉线则用 bot_token 重新登录。

    与主客户端一样不依赖 telethon 内建自动重连；bot 客户端没有常驻的
    run_until_disconnected，故用周期探活代替。bot 启动失败/被置 None 时本任务
    结束。被 main 取消（停止信号）时以 CancelledError 收尾。
    """
    while True:
        await asyncio.sleep(BOT_KEEPALIVE_INTERVAL)
        bot = state.bot_client
        if bot is None:
            return
        if bot.is_connected():
            continue
        logger.warning("🤖 bot 菜单连接已断开，尝试重连...")
        try:
            await start_with_retry(bot, bot_token=BOT_TOKEN)
            logger.info("🤖 bot 菜单已重新连接")
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:
            logger.error(
                f"🤖 bot 菜单重连失败（{type(e).__name__}: {e}），"
                f"{BOT_KEEPALIVE_INTERVAL} 秒后重试"
            )


def _install_stop_handlers(stop_event):
    """把 SIGINT/SIGTERM 转成「设 STOP_EVENT 优雅退出」，接管 telethon 对
    KeyboardInterrupt 的吞并。

    run_until_disconnected 内部有 except KeyboardInterrupt，若不加信号处理器，
    Ctrl-C 会被它吞掉后当普通断开处理 → 稳态守护会立刻重连，Ctrl-C 就失效了。
    这里把两个信号都转为设 STOP_EVENT，由 main 取消服务任务收尾；第二次信号
    直接强制退出（os._exit，兜底进程已卡死的情况）。非 Unix 平台不支持则忽略。
    """
    loop = asyncio.get_running_loop()

    def _on_signal(signame):
        if stop_event.is_set():
            logger.warning(
                f"⚠️ 收到第二次 {signame}，强制退出"
            )
            os._exit(1)
        logger.info(f"🛑 收到 {signame}，正在保存并退出...")
        stop_event.set()

    for sig, name in (
        (signal.SIGINT, "Ctrl-C(SIGINT)"),
        (signal.SIGTERM, "SIGTERM"),
    ):
        try:
            loop.add_signal_handler(sig, _on_signal, name)
        except (NotImplementedError, RuntimeError, ValueError):
            # 非 Unix / 非主线程：保持默认（Ctrl-C 会以 KeyboardInterrupt 打断）
            logger.warning(f"无法注册 {name} 信号处理器，改用默认行为")
            continue


async def new_message_handler(event):
    try:
        message = event.message

        # 只处理 Saved Messages（"me"）与白名单 chat
        chat_kind, source_override = whitelist.classify_message_chat(
            event.chat_id, state.MY_ID, state.WHITELIST_CHATS
        )
        if chat_kind is None:
            return
        is_me = chat_kind == "me"

        media_type = get_media_type(message)
        file_name = None

        try:
            file_name = message.file.name if message.file else None
        except Exception:
            pass

        chat_label = (
            "Saved Messages" if is_me else f"白名单 chat（{source_override}）"
        )
        logger.info(
            f"📨 {chat_label} 收到消息 | "
            f"ID={message.id} | "
            f"media={media_type} | "
            f"grouped={getattr(message, 'grouped_id', None)} | "
            f"file={file_name or 'None'} | "
            f"has_document={'yes' if message.document else 'no'} | "
            f"has_photo={'yes' if message.photo else 'no'}"
        )

        text = (message.message or "").strip()

        # 处理命令（只在 Saved Messages 生效）
        if is_me and text.startswith("/"):
            handled = await commands.handle_command(event, text)
            if handled:
                return

        # ========================================================
        # 抖音 / Instagram 链接：即使消息本身没有媒体，也要检查文字 URL
        # 链接解析只在 Saved Messages 生效（白名单 chat 仅下载媒体）。
        # ========================================================
        if is_me:
            douyin_urls = platform.extract_douyin_urls(text)
            instagram_urls = platform.extract_instagram_urls(text)

            # Telegram 可能把链接显示成 WebPage 预览（MessageMediaWebPage）。
            # 即使 message.file=None，也必须按文字中的 URL 继续处理。
            if not douyin_urls or not instagram_urls:
                try:
                    for entity in getattr(message, "entities", None) or []:
                        url = getattr(entity, "url", None)
                        if not url:
                            continue
                        if not douyin_urls:
                            douyin_urls.extend(
                                platform.extract_douyin_urls(url)
                            )
                        if not instagram_urls:
                            instagram_urls.extend(
                                platform.extract_instagram_urls(url)
                            )
                except Exception as e:
                    logger.warning(f"读取 Telegram URL entity 失败：{e}")

            # 去重
            douyin_urls = list(dict.fromkeys(douyin_urls))
            instagram_urls = list(dict.fromkeys(instagram_urls))

            if douyin_urls or instagram_urls:
                logger.info(
                    f"🔎 检测到 {len(douyin_urls)} 个抖音链接、"
                    f"{len(instagram_urls)} 个 Instagram 链接"
                )
                asyncio.create_task(
                    platform.relay_platform_links(
                        message, douyin_urls, instagram_urls
                    )
                )
                return

        # 判断是否是可下载媒体
        if not is_downloadable(message):
            # Saved Messages 里的一条「纯用户评论」：转发带 caption 的媒体前在
            # 输入框打的字（Telegram 把它发成评论消息、媒体紧跟其后，评论不进
            # 转发副本的 caption）。记作待关联标注，让随后窗口内到达的媒体在
            # 下载命名时拼上。排除命令/抖音链接（前面已早退）/程序消息（命令
            # 回复、通知等由 is_cleanup_message 判定）——只有像样的人类短文本
            # 才当作评论记录。
            if (
                is_me
                and text
                and not text.startswith("/")
                and not cleanup.is_cleanup_message(message)
            ):
                _record_me_label(text)
                logger.info(f"🏷 记录待关联转发评论：\"{text}\"")
            else:
                logger.info(
                    f"消息 ID={message.id} 没有检测到可下载媒体，忽略"
                )
            return

        logger.info(
            f"📦 检测到可下载媒体 | 类型={media_type} | "
            f"文件={file_name or '无文件名'}"
        )

        # 入队持久化下载（重启不丢任务），不阻塞消息监听。
        # Saved Messages：直接入队下载原消息（不变）。
        # 白名单 chat：先转发一份进 Saved Messages，再入队下载那份转发副本——
        # Saved Messages 由此成为唯一下载入口；副本保留作记录/收藏。转发副本自带
        # 「转发自」来源头（用户在收藏夹看得出处/落盘目录），目录下载时经 fwd_from
        # 自动解析回原来源。转发失败（如来源禁转）由 relay_chat_media 回退直下原消息。
        if is_me:
            asyncio.create_task(_enqueue_me(message))
        else:
            asyncio.create_task(
                relay_chat_media(message, event.chat_id, source_override)
            )

    except Exception as e:
        logger.exception(f"❌ 消息处理异常：{e}")


def _build_media_record(message, chat_id, source_override, source_link=None,
                        album_caption=None, user_label=None):
    """组装一条 media 队列任务记录（入队展示与实际下载命名共用同一规则）。

    album_caption：相册无自身文字的成员继承到的同组说明。转发副本本身没有
    caption，把它持久化进记录，下载/列表展示命名时无文字图片即可沿用相册标题
    （而非 photo_时间戳 兜底）。
    user_label：手工转发评论（待关联标注，见 _record_me_label）。代码加 '#' 后
    拼到命名最前，下载/展示与入队保持一致；持久化供重启后下载侧沿用。
    """
    text = (message.message or "").strip()
    file_name = None
    try:
        file_name = message.file.name if message.file else None
    except Exception:
        pass
    label = file_name or (text[:50] if text else f"消息 {message.id}")
    record = {
        "kind": "media",
        "chat_id": chat_id,
        "msg_id": message.id,
        "source_override": source_override,
        "label": label,
        # 入队时算好最终文件名，列表展示与实际下载命名保持一致
        "final_name": compute_final_filename(
            message, caption=album_caption, label=user_label
        ),
        # 转发消息链到原频道消息；否则用消息自身 chat 生成
        "source_link": (
            source_link if source_link is not None
            else message_source_link(message, chat_id)
        ),
    }
    if album_caption:
        record["album_caption"] = album_caption
    if user_label:
        record["user_label"] = user_label
    return record


async def _enqueue_me(message):
    """Saved Messages 原消息入队：相册无自身文字的成员先继承同组说明再入队。

    手工转发评论（窗口内的待关联标注）一并继承为命名标注——Telegram 把它发成
    「评论 + 紧跟媒体」；一条评论后连续转发的 N 条媒体都拿到同一条标注。
    实测评论事件也可能落在媒体之后（同批更新的事件循环调度顺序不定），故到
    达时无待关联标注就先等一小段宽限（ME_LABEL_GRACE_SECONDS）再取一次，给
    尾随评论一个落地机会；已有标注时不等待、立即继承。
    """
    user_label = _take_me_label()
    if user_label is None and ME_LABEL_GRACE_SECONDS > 0:
        await asyncio.sleep(ME_LABEL_GRACE_SECONDS)
        user_label = _take_me_label()
    if user_label:
        logger.info(f"🏷 媒体 {message.id} 继承转发评论标注：\"{user_label}\"")
    album_caption = await _maybe_album_caption(message)
    await enqueue_media(
        message, state.MY_ID, None,
        album_caption=album_caption, user_label=user_label,
    )


async def enqueue_media(message, chat_id, source_override, source_link=None,
                        album_caption=None, user_label=None):
    """把一条媒体消息入队下载（持久化，重启不丢任务）。

    source_link 显式传入时覆盖默认的来源链接；album_caption 为相册无文字
    成员继承到的同组说明（入队即随记录持久化，下载命名时使用）；user_label
    为手工转发评论标注（同样随记录持久化）。
    入队前先过重复媒体判重（tg:<file_unique_id> 两级：已下载索引 + 在途
    队列）：命中只拦下载不拦转发（白名单转发的「转发自」副本照旧留在收藏
    夹当书签），并通知；键拿不到或 /dedup off 时照常入队。
    """
    key = dedup.media_key(message)
    if key:
        logger.info(f"🛡 判重键 tg:{key.split(':', 1)[1]}（消息 {message.id}）")
    skip, notice = dedup.should_skip(key)
    if skip:
        logger.info(f"⏭️ 重复媒体跳过入队（消息 {message.id}）")
        try:
            await state.client.send_message("me", notice)
        except Exception as e:
            logger.warning(f"发送重复媒体通知失败：{e}")
        return
    record = _build_media_record(
        message, chat_id, source_override, source_link,
        album_caption, user_label,
    )
    if key:
        record["dedup_key"] = key
    await queue.enqueue_and_start(record)


async def relay_chat_media(message, origin_chat_id, source_override):
    """白名单 chat 收到媒体 → 转发一份进 Saved Messages → 入队下载转发副本。

    单条媒体即时转发（_relay_single）；带 grouped_id 的相册成员走整组协调
    （_relay_album_member）：攒批到完整一组后用一次 forward_messages 转发，
    让收藏夹里是「一个相册」而非 N 条散消息——更接近用户手动转发一组数据的
    效果。下载侧行为不变：每个副本仍带系统「转发自 X」来源头（据此看得出处/
    落盘目录，目录下载时经 fwd_from 自动解析回原来源）；已知代价是源消息若
    带内联按钮，按钮会随转发进入副本（Telegram 禁止编辑被转发消息无法事后剥）。
    """
    if getattr(message, "grouped_id", None):
        await _relay_album_member(message, origin_chat_id, source_override)
    else:
        await _relay_single(message, origin_chat_id, source_override)


async def _relay_single(message, chat_id, source_override):
    """单条媒体：转发进收藏夹 → 入队下载转发副本（整组回退的逐条单转同此）。

    转发用裸 forward_to，不给 wait_for：一次相册 ~20 个成员并发转发会触发
    Telegram 转发频率限制，Telethon 会按 FloodWait 自行等待后重试，wait_for
    会把这种合法等待掐成超时、进而降级直下原消息——同一相册被劈到两个来源
    目录。转发失败（如来源禁转）时仍回退直下原消息，功能不丢。

    相册无自身文字的成员（图片等）：转发副本补不了 caption，于是转发前从源
    chat 读同组说明（_maybe_album_caption），作为 album_caption 存进队列
    记录，下载命名时套用——图片名是 `日期 相册说明 - 原名` 而非 photo_时间戳。
    """
    link = message_source_link(message, chat_id or message.chat_id)
    album_caption = await _maybe_album_caption(message)
    try:
        fwd = await _forward_to_me(message)
        logger.info(
            f"📤 已把白名单媒体转发进收藏夹（新消息 ID={fwd.id}），"
            "将下载该转发副本"
        )
        await enqueue_media(
            fwd, state.MY_ID, None, source_link=link,
            album_caption=album_caption,
        )
    except Exception as e:
        logger.exception(
            f"转发白名单媒体进收藏夹失败（{source_override}，msg_id="
            f"{message.id}），回退直下原消息：{e}"
        )
        await enqueue_media(
            message, chat_id, source_override,
            album_caption=album_caption,
        )


async def _forward_to_me(message):
    """把单条消息转发到收藏夹并返回副本；转发频率限制时等待后重试。

    不给外部 wait_for（见 relay_chat_media 说明）。Telethon 默认对 FloodWait
    自动等待 ≤60s 后重试；超过阈值的 FloodWaitError 在这里自己等够 seconds
    （上限 120s）再试，而不是直接降级——降级会把该成员落去白名单标题目录、
    拆散整组相册。重试用尽或其它错误照常抛出，由调用方回退。
    """
    from telethon.errors import FloodWaitError

    last_error = None
    for attempt in range(1, 4):
        try:
            forwarded = await message.forward_to("me")
        except FloodWaitError as e:
            wait = min(int(getattr(e, "seconds", None) or 30), 120)
            last_error = e
            logger.warning(
                f"转发触发频率限制，等待 {wait}s 后重试 "
                f"（msg_id={message.id}，第 {attempt}/3 次）"
            )
            await asyncio.sleep(wait)
            continue
        forwarded = forwarded[0] if isinstance(forwarded, (list, tuple)) else forwarded
        if forwarded is None:
            raise RuntimeError("转发到 Saved Messages 未返回新消息")
        return forwarded
    raise last_error or RuntimeError(f"转发 {message.id} 多次被频率限制，放弃")


# 找相册兄弟的窗口半径：Telegram 相册最多 10 个媒体且 id 连续，±10 足够。
_ALBUM_SIBLING_RANGE = 10

# 相册说明缓存（grouped_id → 说明文字）：一次相册洪峰里只读源 chat 一次，
# 同组其它无文字成员直接复用，避免并发洪峰压垮连接（见 _maybe_album_caption）。
# grouped_id 全局唯一，量级小（每天几十条），常驻内存即可。
_ALBUM_CAPTIONS = {}


async def _fetch_group_caption(message) -> str:
    """相册 caption 继承：在源 chat 里找同 grouped_id 且带文字的兄弟文本。

    仅当本消息自身无 caption 且属于相册（grouped_id 非空）时调用。相册说明
    只挂在其中一个成员上，从源 chat 拉一个以本消息为中心的 id 窗口（相册
    成员 id 连续、挨在一起），交给纯函数挑文字。读不到/失败返回空串，由
    调用方决定放弃继承——绝不阻塞入队/下载。
    """
    grouped_id = getattr(message, "grouped_id", None)
    msg_id = message.id
    if not grouped_id or not msg_id:
        return ""
    try:
        ids = list(
            range(max(1, msg_id - _ALBUM_SIBLING_RANGE),
                  msg_id + _ALBUM_SIBLING_RANGE + 1)
        )
        msgs = await asyncio.wait_for(
            state.client.get_messages(message.chat_id, ids=ids),
            timeout=QUEUE_FETCH_TIMEOUT,
        )
        siblings = msgs if isinstance(msgs, (list, tuple)) else []
        return pick_group_caption_text(siblings, grouped_id)
    except asyncio.TimeoutError:
        logger.warning(f"读取相册说明超时（msg_id={msg_id}），放弃继承")
        return ""
    except Exception as e:
        logger.warning(f"读取相册说明失败（msg_id={msg_id}）：{e}")
        return ""


async def _maybe_album_caption(message):
    """相册无自身文字的成员 → 同组说明（供下载/展示命名继承）；否则 None。

    仅当消息自身无文字且属相册（grouped_id 非空）时读源 chat 的兄弟消息；
    有文字、非相册或读取失败都返回 None（有文字时本消息自己的 caption 才是
    权威，无需继承）。I/O 只在必要且可行时发生，失败不阻塞入队/下载。

    一次相册洪峰（~20 个成员几乎同时到达）里，无文字成员共享同一条说明——
    只对第一个读源 chat 并缓存（仅缓存读到内容的成功结果；没读到的成员各自
    重试直到说明成员出现），避免每个成员都多发一次 get_messages 把连接压垮、
    拖出前一轮那种超时/降级。
    """
    if (message.message or "").strip() or not getattr(message, "grouped_id", None):
        return None
    grouped_id = message.grouped_id
    cached = _ALBUM_CAPTIONS.get(grouped_id)
    if cached:
        return cached
    cap = await _fetch_group_caption(message)
    if cap:
        _ALBUM_CAPTIONS[grouped_id] = cap
    return cap or None


# ============================================================
# 手工转发「评论 + 紧跟媒体」的前置标注
# ============================================================
# 转发带 caption 的媒体到收藏夹时，若在输入框里打了一句话再转发，Telegram 并
# 不会把这句话合并进转发副本的 caption——它变成「评论消息在前、媒体紧跟其后」
# 的两条（多转发则是一条评论 + N 条媒体）。这里把这条评论记作待关联标注：
# 窗口（ME_LABEL_WINDOW_SECONDS）内到达的媒体入队时都继承它，下载命名时代码加
# '#' 拼到文件名最前（<date> #标注 原caption …）。窗口只随到达时间自然过期，
# 取用不清空，一条评论后连续 N 条媒体都能拼上同一条标注。单线程事件循环内
# 访问，无需加锁。
_ME_PENDING_LABEL = None     # 最近一条待关联的用户评论文本
_ME_PENDING_LABEL_AT = 0.0   # 记录时刻的 time.monotonic()


def _record_me_label(text):
    """记住一条用户纯文本评论，作为随后到达媒体的待关联标注。"""
    global _ME_PENDING_LABEL, _ME_PENDING_LABEL_AT
    _ME_PENDING_LABEL = text
    _ME_PENDING_LABEL_AT = time.monotonic()


def _take_me_label():
    """取仍在新鲜窗口内的待关联标注；无/已过期返回 None。取用不清空。"""
    global _ME_PENDING_LABEL
    if not _ME_PENDING_LABEL:
        return None
    if time.monotonic() - _ME_PENDING_LABEL_AT > ME_LABEL_WINDOW_SECONDS:
        _ME_PENDING_LABEL = None
        return None
    return _ME_PENDING_LABEL


# ============================================================
# 相册整组转发协调
# ============================================================
# 相册到达白名单 chat 时是 N 条各自独立的媒体事件、共享 grouped_id。逐条即时
# 转发会让收藏夹里是 N 条散消息；要变成「一个相册」，需把同组所有成员放进同
# 一次 forward_messages 请求（服务端据此在目的地重组相册）。第一个成员事件
# 到达后不立即转发，等一个攒批窗口让洪峰其余成员的事件落齐，再从源 chat 拉
# 权威完整成员列表，一次性整组转发。与 caption 缓存同以 grouped_id 为键，
# 单线程事件循环内访问、无需加锁。
_ALBUM_DEBOUNCE_SECONDS = 1.5  # 攒批窗口：等洪峰其余成员事件落齐
_ALBUM_SETTLE_SECONDS = 5.0    # 转发完成后保留协调条目的宽限（迟到成员直接忽略）
# key=(chat_id, grouped_id) → {"seen": {msg_id: msg}, "task": Task, "done": bool}
_ALBUM_RELAY = {}


async def _relay_album_member(message, chat_id, source_override):
    """相册成员事件：登记进组协调状态；首个成员负责起整组转发任务。"""
    key = (chat_id, message.grouped_id)
    st = _ALBUM_RELAY.get(key)
    if st is None:
        st = _ALBUM_RELAY[key] = {"seen": {}, "task": None, "done": False}
    if st["done"]:
        return  # 整组已转发完；迟到的重复成员直接忽略，避免二次转发
    st["seen"][message.id] = message
    if st["task"] is None:
        st["task"] = asyncio.create_task(
            _relay_album_group(key, chat_id, source_override)
        )


async def _fetch_album_members(chat_id, grouped_id, lo_id, hi_id):
    """从源 chat 拉整组相册成员：以已见成员 id 跨度为心开窗口，过滤同组媒体。

    相册成员 id 在源 chat 里连续（同一组转发过去的），窗口半径
    _ALBUM_SIBLING_RANGE 足够覆盖整组。兜住事件洪峰里个别没触发/晚到的成员，
    也顺带拿到挂说明文字的那个成员。失败返回空列表，由调用方回退逐条单转。
    """
    try:
        ids = list(
            range(max(1, lo_id - _ALBUM_SIBLING_RANGE),
                  hi_id + _ALBUM_SIBLING_RANGE + 1)
        )
        msgs = await asyncio.wait_for(
            state.client.get_messages(chat_id, ids=ids),
            timeout=QUEUE_FETCH_TIMEOUT,
        )
        out = []
        for m in (msgs if isinstance(msgs, (list, tuple)) else []):
            try:
                if getattr(m, "grouped_id", None) == grouped_id and is_downloadable(m):
                    out.append(m)
            except Exception:
                continue
        out.sort(key=lambda m: m.id)
        return out
    except asyncio.TimeoutError:
        logger.warning(f"读取相册整组成员超时（grouped_id={grouped_id}），放弃")
        return []
    except Exception as e:
        logger.warning(f"读取相册整组成员失败（grouped_id={grouped_id}）：{e}")
        return []


async def _forward_album_to_me(chat_id, members):
    """整组转发进收藏夹：一次 forward_messages 返回副本列表（个别失败位为 None）。

    不给外部 wait_for：一次请求若触发转发频率限制，Telethon 会按 FloodWait
    自行等待 ≤60s 后重试；超过阈值自行等够 seconds（上限 120s）再试——降级成
    逐条单转反而更易触频、拆组。
    """
    from telethon.errors import FloodWaitError

    last_error = None
    for attempt in range(1, 4):
        try:
            sent = await state.client.forward_messages(
                "me", members, from_peer=chat_id
            )
            if isinstance(sent, (list, tuple)):
                return list(sent)
            return [sent]
        except FloodWaitError as e:
            wait = min(int(getattr(e, "seconds", None) or 30), 120)
            last_error = e
            logger.warning(
                f"相册整组转发触发频率限制，等待 {wait}s 后重试 "
                f"（第 {attempt}/3 次，{len(members)} 个成员）"
            )
            await asyncio.sleep(wait)
    raise last_error or RuntimeError("相册整组转发多次被频率限制，放弃")


async def _relay_album_group(key, chat_id, source_override):
    """攒批窗口后整组转发：从源 chat 拉完整成员 → 一次 forward → 逐个入队。"""
    grouped_id = key[1]
    try:
        await asyncio.sleep(_ALBUM_DEBOUNCE_SECONDS)

        st = _ALBUM_RELAY.get(key)
        if st is None:
            return
        seen_ids = list(st["seen"].keys())
        if not seen_ids:
            return

        members = await _fetch_album_members(
            chat_id, grouped_id, min(seen_ids), max(seen_ids)
        )
        if not members:
            # 拉不到权威列表（读源 chat 失败等）→ 用已登记成员逐条单转兜底
            logger.warning(
                f"相册整组转发：未能从源 chat 取到完整成员"
                f"（grouped_id={grouped_id}，已登记 {len(seen_ids)} 个），改逐条单转"
            )
            await _fallback_relay_each(chat_id, source_override, st["seen"].values())
            st["done"] = True
            await asyncio.sleep(_ALBUM_SETTLE_SECONDS)
            _ALBUM_RELAY.pop(key, None)
            return

        group_caption = pick_group_caption_text(members, grouped_id)
        try:
            copies = await _forward_album_to_me(chat_id, members)
        except Exception as e:
            logger.exception(
                f"相册整组转发失败（{source_override}，grouped_id={grouped_id}，"
                f"{len(members)} 个成员），回退逐条单转：{e}"
            )
            if group_caption:
                _ALBUM_CAPTIONS[grouped_id] = group_caption  # 单转的无文字成员复用
            await _fallback_relay_each(chat_id, source_override, members)
            st["done"] = True
            await asyncio.sleep(_ALBUM_SETTLE_SECONDS)
            _ALBUM_RELAY.pop(key, None)
            return

        copies = [c for c in copies if c is not None]  # 个别失败位为 None 时跳过
        ok = len(copies)
        logger.info(
            f"📤 已把相册整组转发进收藏夹（{len(members)} 个成员，成功 {ok} 个，"
            f"新消息 ID={copies[0].id if ok else '-'}），将逐个下载转发副本"
        )
        link = message_source_link(members[0], chat_id)
        for copy in copies:
            # 转发保留挂说明成员自己的 caption；无文字副本沿用相册说明做命名
            own_text = (copy.message or "").strip()
            album_caption = None if own_text else (group_caption or None)
            await enqueue_media(
                copy, state.MY_ID, None,
                source_link=link, album_caption=album_caption,
            )

        st["done"] = True
        # 转发完保留协调条目一段宽限，迟到成员事件直接忽略；再摘除防积累
        await asyncio.sleep(_ALBUM_SETTLE_SECONDS)
        _ALBUM_RELAY.pop(key, None)

    except Exception as e:
        logger.exception(f"相册整组转发流程异常（grouped_id={grouped_id}）：{e}")
        st = _ALBUM_RELAY.get(key)
        if st is not None and not st["done"]:
            st["done"] = True
        _ALBUM_RELAY.pop(key, None)


async def _fallback_relay_each(chat_id, source_override, members):
    """整组转发失败/不可行时逐条单转（旧行为），保证媒体不丢、收藏夹不空。"""
    await asyncio.gather(
        *(_relay_single(m, chat_id, source_override) for m in members),
        return_exceptions=True,
    )


async def main():
    # 敏感配置检查：api_id / api_hash 缺失时给出明确提示（否则 create_client
    # 会用空凭据报出晦涩错误），bot_token 缺失仅禁用按钮菜单。
    if not API_ID or not API_HASH:
        logger.error(
            "❌ 未配置 Telegram API 凭据：请参照 tg_secrets.example.json "
            "在 %s 中填写 api_id / api_hash 后重新运行。",
            SECRETS_FILE,
        )
        return
    if not BOT_TOKEN:
        logger.warning(
            "ℹ️ 未配置 bot_token：按钮菜单不可用，下载等其余功能正常。"
            "如需菜单请在 %s 中补充 bot_token / bot_username。",
            SECRETS_FILE,
        )

    # 启动清理：此时尚无任何下载，安全地删除历史遗留的 .download 半成品
    # 临时文件（正常中断会由队列重启恢复，这里只清异常退出或文件名变更
    # 产生的孤儿文件，避免它们长期占用磁盘）。
    try:
        _cleaned = cleanup.clean_temp_files()
        if _cleaned:
            logger.warning(
                f"🧹 启动清理：删除 {_cleaned} 个遗留 .download 临时文件"
            )
    except Exception as e:
        logger.warning(f"启动清理 .download 临时文件失败：{e}")

    # 必须在事件循环内创建（见 CLAUDE.md「事件循环规则」一节）
    state.client = create_client()
    state.bot_client = None
    thread.load_thread_config()
    dedup.load_dedup_config()
    loaded = dedup.load_index()
    logger.info(f"🛡 去重索引已载入：{loaded} 条")
    state.WHITELIST_CHATS = whitelist.load_whitelist()
    state.DOWNLOAD_SEMAPHORE = AdjustableSemaphore(state.DOWNLOAD_CONCURRENCY)
    state.QUEUE_LOCK = asyncio.Lock()
    state.QUEUE = queue.load_queue()
    state.client.add_event_handler(new_message_handler, events.NewMessage())

    logger.info("=====================")
    logger.info("🚀 TG Userbot v2.10 正在启动")
    logger.info(f"运行平台：{'Termux/Android' if IS_TERMUX else 'macOS/桌面'}")
    if PROXY:
        logger.info(f"代理：已启用（{PROXY[0]} {PROXY[1]}:{PROXY[2]}）")
    else:
        logger.info("代理：未启用（直连）")
    logger.info(
        "传输方式：{}".format(
            "混淆(Obfuscated)" if CONNECTION_TYPE == "obfuscated" else "TLS(Full)"
        )
    )
    logger.info(f"保存目录：{SAVE_FOLDER}")
    logger.info(f"日志文件：{LOG_FILE}")
    logger.info(
        f"监听范围：Saved Messages + 白名单 {len(state.WHITELIST_CHATS)} 个 chat"
    )
    for cid, title in sorted(state.WHITELIST_CHATS.items()):
        logger.info(f"  - 白名单：{title} ({cid})")
    logger.info(
        f"📥 下载队列：{len(state.QUEUE['tasks'])} 个任务 | "
        f"待重试 {len(state.QUEUE['retry'])} 个"
    )
    if BOT_TOKEN:
        logger.info(f"🤖 bot 菜单：{BOT_USERNAME}")
    logger.info("自动重试：3 次")
    logger.info(f"并发下载数：{state.DOWNLOAD_CONCURRENCY}（/thread 可调，每条占一条独立连接）")
    logger.info(
        f"抖音解析机器人：{DOUYIN_BOT_USERNAME}（链接转发，回复视频自动进收藏夹下载）"
    )
    logger.info(
        f"Instagram 解析机器人：{INSTAGRAM_BOT_USERNAME}（同上）"
    )
    logger.info("============================================")

    await start_with_retry(state.client)

    me = await state.client.get_me()
    state.MY_ID = me.id

    logger.info(
        f"✅ 登录成功 | 用户：{me.first_name or ''} "
        f"{me.last_name or ''} | ID={state.MY_ID}"
    )

    # 建立多 worker 下载池：DOWNLOAD_CONCURRENCY 条独立连接并发拉文件，
    # 破掉主客户端单 socket 的聚合瓶颈。失败自动降级回单连接（功能不丢）。
    try:
        await workers.spawn_pool(state.DOWNLOAD_CONCURRENCY)
    except Exception as e:
        logger.exception(f"启动下载 worker 池失败：{e}")

    # 恢复持久化队列：重启前没跑完的任务自动重新执行
    if state.QUEUE["tasks"]:
        logger.info(f"📥 恢复下载队列：{len(state.QUEUE['tasks'])} 个任务")
        queue.recover_queue_tasks()
    if state.QUEUE["retry"]:
        logger.info(f"🔁 待重试列表：{len(state.QUEUE['retry'])} 个任务（手动重试）")

    # bot 按钮菜单：登录失败只影响菜单，不影响主功能
    if BOT_TOKEN:
        try:
            state.bot_client = create_bot_client()
            await start_with_retry(state.bot_client, bot_token=BOT_TOKEN)
            bot_me = await state.bot_client.get_me()
            state.BOT_ID = bot_me.id
            state.bot_client.add_event_handler(
                bot.bot_message_handler, events.NewMessage()
            )
            state.bot_client.add_event_handler(
                bot.bot_callback_handler, events.CallbackQuery()
            )
            logger.info(f"✅ bot 菜单已启用：{BOT_USERNAME}（ID={state.BOT_ID}）")
            try:
                await bot.register_bot_commands(state.bot_client)
            except Exception as e:
                logger.warning(f"注册 bot 命令面板失败（不影响菜单）：{e}")
        except Exception as e:
            logger.error(f"❌ bot 菜单启动失败（不影响主功能）：{e}")
            state.bot_client = None
            state.BOT_ID = None

    if os.access(SAVE_FOLDER, os.W_OK):
        logger.info("✅ 保存目录可访问")
    else:
        if IS_TERMUX:
            logger.error("❌ 保存目录不可写，请检查 Termux 存储权限")
        else:
            logger.error("❌ 保存目录不可写，请检查目录权限")

    logger.info(
        "🟢 TG Userbot v2.10 已启动，等待 Saved Messages / 白名单 chat 的媒体与抖音链接"
    )
    logger.info("💡 测试：在 Saved Messages 发送 /status")
    logger.info("💡 下载：把文件转发到 Saved Messages")
    logger.info("💡 抖音：把抖音链接发送到 Saved Messages")
    logger.info("💡 Instagram：把 Instagram 链接发送到 Saved Messages")
    logger.info("💡 诊断：发送消息后，日志必须出现‘📨 Saved Messages 收到消息’")

    # 启动自动清理任务
    cleanup.load_clear_interval()
    state.CLEAR_TIME_CHANGED = asyncio.Event()

    cleanup_task = None
    if AUTO_CLEAN_SAVED_MESSAGES:
        cleanup_task = asyncio.create_task(cleanup.cleanup_loop())
        # 启动时先清理一次已经过期的程序消息（Saved Messages 与 bot 对话）
        await cleanup.cleanup_saved_messages_once()
        await cleanup.cleanup_bot_chat_once()

    # ========================================================
    # 稳态：主客户端 / bot 连接各自掉线自动重连，进程保持存活直到收到停止信号。
    # SIGINT/SIGTERM → 设 STOP_EVENT → 取消服务任务 → 收尾退出（信号处理器在
    # 稳态才开始注册，登录等启动阶段 Ctrl-C 仍以 KeyboardInterrupt 直接打断）。
    # ========================================================
    state.STOP_EVENT = asyncio.Event()
    _install_stop_handlers(state.STOP_EVENT)

    bot_keepalive_task = None
    if BOT_TOKEN and state.bot_client is not None:
        bot_keepalive_task = asyncio.create_task(_bot_keepalive())
    main_serve_task = asyncio.create_task(_main_serve())

    try:
        await state.STOP_EVENT.wait()
        logger.info("🛑 收到停止信号，正在收尾退出...")
    finally:
        # 取消后台服务任务：主客户端挂在 run_until_disconnected，取消会触发其
        # finally 里的 disconnect，随后以 CancelledError 收尾；bot 探活同理。
        for t in (main_serve_task, bot_keepalive_task):
            if t is not None:
                t.cancel()
        for t in (main_serve_task, bot_keepalive_task):
            if t is not None:
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("后台服务任务清理出错")
        # 断开下载 worker 连接（尽力而为，不影响主客户端退出）
        try:
            await workers.shutdown()
        except Exception:
            pass
        if cleanup_task:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
