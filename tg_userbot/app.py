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
from . import notify
from . import commands
from . import caption_filter
from . import chrome_client
from . import dedup
from . import listener
from . import listener_worker
from . import wl_scan
from . import queue
from . import runtime_db
from . import reporter
from . import stats
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
    AUTO_RETRY_SWEEP_SECONDS,
    BOT_KEEPALIVE_INTERVAL,
    BOT_SESSION_NAME,
    BOT_TOKEN,
    BOT_USERNAME,
    CONNECTION_TYPE,
    DOUYIN_BOT_USERNAME,
    INSTAGRAM_BOT_USERNAME,
    IS_TERMUX,
    LISTEN_STARTUP_DELAY_SECONDS,
    LOG_FILE,
    LOGIN_RETRIES,
    LOGIN_TIMEOUT_SECONDS,
    ME_LABEL_GRACE_SECONDS,
    ME_LABEL_WINDOW_SECONDS,
    PROXY,
    QUEUE_FETCH_TIMEOUT,
    REPORT_ENABLED,
    REPORT_INTERVAL_SECONDS,
    REPORT_PROGRESS_INTERVAL_SECONDS,
    REPORT_RESTART_DELAY_SECONDS,
    SAVE_FOLDER,
    SECRETS_FILE,
    SESSION_NAME,
    SERVE_RECONNECT_BASE_DELAY,
    SERVE_RECONNECT_MAX_DELAY,
    TELEGRAM_AUTO_RECONNECT,
    WHITELIST_SCAN_INTERVAL_SECONDS,
    AdjustableSemaphore,
)
from .log import logger
from .naming import (
    compute_final_filename,
    effective_caption,
    parse_date,
    pick_group_caption_text,
)
from .sources import (
    get_media_type,
    is_downloadable,
    message_source_link,
    resolve_origin_snapshot,
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


async def _await_child_task(task):
    """等子任务结束并原样抛出它的结局；**调用方被取消时不波及子任务**。

    为什么不直接 `await task`：asyncio 的 ``Task.cancel()`` 会顺手取消
    ``self._fut_waiter``，而 ``await task`` 的 ``_fut_waiter`` **正是那条子任务**
    ——于是取消一到手，子任务就已经 done，「子任务自己被取消（网络层）」与
    「调用方被取消（停服）」再也没法区分（实测：`_reporter_supervisor` 因此把
    自己的取消当成子任务意外死亡而重启，任务变成杀不掉的僵尸）。

    ``asyncio.wait`` 只等它自己的内部 waiter，取消调用方不会波及子任务 ——
    判据这才立得住。与 ``netio.shielded`` 用的是同一个办法、同一个理由。
    """
    await asyncio.wait({task})
    if task.cancelled():
        raise asyncio.CancelledError()
    exc = task.exception()
    if exc is not None:
        raise exc


async def _reporter_supervisor(instance):
    """守护 Runtime Reporter 主循环：意外结束就记 ERROR 并节流重启。

    2026-09-11 事故：当天 07:08 起汇报静默停摆数小时，直到用户发现「不再主动
    汇总数据」。原因是 telethon 断线对 pending 请求 future 调 cancel()，
    CancelledError 穿透 `Reporter.run()`（它对取消是 re-raise）把任务打死；
    而任务句柄只在进程退出时才被 await，中间无人发现、也无人重启。

    第一道网在 `reporter._safe_send/_safe_edit`（收口把网络层取消变成「本轮
    没做成」），这里是最后一道：循环还是以取消/异常收场时，只要不是停服就
    记 ERROR、等 `REPORT_RESTART_DELAY_SECONDS` 再用**同一条实例**重跑
    （沿用实例才保得住 status_message_id，面板继续原地编辑而不是重建）。

    「是不是停服」以 `state.STOP_EVENT` 判定：它只在 main() 收到停止信号时
    置位，本函数被 main 取消时也已经置位。
    """
    delay = REPORT_RESTART_DELAY_SECONDS
    while True:
        task = asyncio.create_task(instance.run())
        try:
            await _await_child_task(task)
        except asyncio.CancelledError:
            stopping = (state.STOP_EVENT is not None
                        and state.STOP_EVENT.is_set())
            if not task.done():
                # 子任务还没结束就收到了取消 → 取消是打给**本函数**的（停服，
                # 或外面直接 task.cancel() 这个守护任务）：把子任务一并收走，
                # 然后**原样上抛**。
                #
                # 这里必须 raise，不能顺着往下走去重启：吞掉自己的取消会让
                # 这个守护任务变成杀不掉的僵尸（外部 cancel 只有落在
                # `asyncio.sleep(delay)` 那一行才生效）。判据能立住，全靠
                # _await_child_task 用 asyncio.wait 而不是直接 await task。
                task.cancel()
                try:
                    await asyncio.wait({task})
                except asyncio.CancelledError:
                    pass
                raise
            if stopping:
                raise
            logger.error(
                f"🤖 汇报任务被意外取消（非停服），{delay}s 后自动重启")
        except Exception:
            logger.exception(f"🤖 汇报任务异常结束，{delay}s 后自动重启")
        else:
            if state.STOP_EVENT is not None and state.STOP_EVENT.is_set():
                return
            logger.error(f"🤖 汇报任务意外结束，{delay}s 后自动重启")
        await asyncio.sleep(delay)


async def _start_reporter():
    """构造并启动 Runtime Reporter，返回 (实例, 任务)。

    REPORT_ENABLED 关闭时返回 (None, None)。启动通知（start）在此发出——
    此时客户端与 worker 池都已就绪。start 自身失败只记日志：Reporter 是
    观察者，它起不来绝不能拖垮主程序。

    返回的任务是 `_reporter_supervisor`（而非 `instance.run()`）：主循环由
    守护函数持有，意外死亡能自愈（见其 docstring）。
    """
    if not REPORT_ENABLED:
        return None, None
    instance = reporter.Reporter()
    try:
        await instance.start()
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise
    except Exception:
        logger.exception("🤖 Reporter 启动失败（不影响核心系统）")
    logger.info(
        f"🤖 Runtime Reporter 已启动（状态面板每 "
        f"{REPORT_INTERVAL_SECONDS}s 刷新，下载中 {REPORT_PROGRESS_INTERVAL_SECONDS}s）"
    )
    return instance, asyncio.create_task(_reporter_supervisor(instance))


async def _retry_sweeper():
    """retry 榜到期自动重放（issues/002）：每 AUTO_RETRY_SWEEP_SECONDS 扫一次。

    链路分钟级抖动时，3 次尝试必然全撞上、任务快速入榜后原本「永久停靠」等人肉
    /retry all。这里按指数退避（queue._backoff_delay）逐轮重放，网络恢复后无需
    人工干预；坏窗口内退避自动拉长、每轮又只放空闲 worker 数条，不会形成风暴。
    异常一律兜住 → 本任务永不因单次失败退出。被 main 取消（停止信号）时以
    CancelledError 收尾。
    """
    while True:
        await asyncio.sleep(AUTO_RETRY_SWEEP_SECONDS)
        try:
            n = queue.replay_due()
            if n:
                logger.info(f"♻️ 自动重放到期任务 {n} 条（retry 榜共 "
                            f"{len(state.QUEUE['retry'])} 条）")
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:
            logger.error(f"♻️ 自动重放扫描失败（{type(e).__name__}: {e}）")


async def _listener_loop():
    """标签监听后台扫描循环（规格书 §16）：按周期主动扫描监听的聊天。

    **与下载白名单是两套完全独立的配置**：本循环只读 state.LISTEN_RULES，
    从不碰 state.WHITELIST_CHATS（唯一一次读它是 listener 内部的 §14 重叠
    判定，而且是只读）。每轮扫描前按需重读 listen.json（§17：改配置无需
    重启；损坏的文件不会清空正在生效的规则，见 listener.reload_listen_config）。

    异常一律兜住 → 本任务永不因单次失败退出；单个聊天的隔离在
    listener.scan_all 内部（一个聊天失败不影响其它）。被 main 取消（停止
    信号）时以 CancelledError 收尾。
    """
    if not state.RUNTIME_DB_READY:
        # Runtime DB 没起来就不扫：扫了也落不了盘，只会每个周期刷一遍错误日志。
        logger.warning("📡 标签监听扫描循环未启动（Runtime DB 不可用）")
        return
    await asyncio.sleep(LISTEN_STARTUP_DELAY_SECONDS)
    while True:
        try:
            if state.LISTEN_ENABLED:
                if state.LISTEN_RULES:
                    await listener.scan_all()
                # 评论跟进：命中标签的帖子按天跟进它的评论区。**节奏由 DB 把关**
                # ——list_due_follows 只返回「距上次检查 ≥ LISTEN_FOLLOW_INTERVAL」
                # 的帖子，所以这里每轮（30 分钟）都调也无妨：没到期就一条都不查、
                # 不产日志、不发请求。跟随总开关（关掉监听就一并停）。
                await listener.follow_scan()
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:
            logger.error(f"📡 标签监听扫描异常（{type(e).__name__}: {e}）")
        # 周期每轮重读：/listen interval 改完立即作用于下一轮
        delay = max(60, int(state.LISTEN_INTERVAL_MINUTES) * 60)
        await asyncio.sleep(delay)


async def _whitelist_scan_loop():
    """白名单扫描生产者后台循环：按 WHITELIST_SCAN_INTERVAL_SECONDS 补漏。

    与标签监听是两套独立配置/游标（chain='wl'）；**不跟随 LISTEN_ENABLED**——
    白名单没有总开关概念，/wl del 移除聊天即停。在线时事件生产者兜实时，
    这里只负责补停机缺口，周期可以放宽。异常一律兜住；被 main 取消时以
    CancelledError 收尾。
    """
    if not state.RUNTIME_DB_READY:
        logger.warning("📋 白名单扫描循环未启动（Runtime DB 不可用）")
        return
    await asyncio.sleep(LISTEN_STARTUP_DELAY_SECONDS)
    while True:
        try:
            await wl_scan.scan_all()
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:
            logger.error(f"📋 白名单扫描异常（{type(e).__name__}: {e}）")
        await asyncio.sleep(WHITELIST_SCAN_INTERVAL_SECONDS)


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
        # 白名单 chat：媒体记为持久化转发任务（origin='wl'），由常驻 Worker
        # 转发进收藏夹再入队下载那份转发副本——副本自带「转发自」来源头
        # （用户在收藏夹看得出处/落盘目录），目录下载时经 fwd_from 自动解析
        # 回原来源。来源禁转由 Worker 回退直下原消息；停机漏掉的由扫描
        # 生产者按 wl 游标补。
        if is_me:
            asyncio.create_task(_enqueue_me(message))
        else:
            # 白名单双通道（2026-09-13）：事件生产者只**记任务**，转发由
            # listener_worker 受控执行；停机漏掉的由 wl 扫描生产者按游标补。
            asyncio.create_task(
                record_whitelist_media(message, event.chat_id, source_override)
            )

    except Exception as e:
        logger.exception(f"❌ 消息处理异常：{e}")


def _build_media_record(message, chat_id, source_override, source_link=None,
                        album_caption=None, user_label=None, parent_date=None,
                        parent_caption=None):
    """组装一条 media 队列任务记录（入队展示与实际下载命名共用同一规则）。

    album_caption：相册无自身文字的成员继承到的同组说明（**fallback**：消息自己
    有文字就用它自己的）。转发副本本身没有 caption，把它持久化进记录，下载/列表
    展示命名时无文字图片即可沿用相册标题（而非 photo_时间戳 兜底）。
    user_label：手工转发评论（待关联标注，见 _record_me_label）。代码加 '#' 后
    拼到命名最前，下载/展示与入队保持一致；持久化供重启后下载侧沿用。
    parent_caption：讨论组评论继承到的频道原帖 caption（**强制**：盖过评论自己
    的文字——「👍」这种评论文字没有命名价值，原帖标题才是信息）。
    parent_date：讨论组评论继承到的频道原帖日期（ISO 字符串，见
    sources.resolve_origin_snapshot）。**入队时快照**，不留给下载 worker 事后再
    查——原帖被编辑/删除时 retry 出来的文件名必须还是同一个（任务书 §5）。

    caption 的取舍只有 naming.effective_caption 一处，且它与 download_file 用的是
    同一个调用——列表展示名与实际落盘名因此不可能漂移。
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
            message,
            caption=effective_caption(message, parent_caption, album_caption),
            label=user_label,
            date_override=parse_date(parent_date),
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
    if parent_date:
        record["parent_date"] = parent_date
    if parent_caption:
        record["parent_caption"] = parent_caption
    return record


def _origin_caption(origin):
    """频道原帖快照 → 命名用说明（无则 None），走 parent_caption 槽（**强制**）。

    刻意**不**复用 album_caption：那个槽是 fallback（消息有字就用消息的），
    而原帖标题要盖过评论自己那句「👍」。两者语义不同，混用一个槽会连相册命名
    一起改坏（见 naming.effective_caption）。
    """
    if not origin:
        return None
    return (origin.get("caption") or "").strip() or None


def _origin_folder(origin):
    """频道原帖快照 → 落盘目录名（无则 None）。

    B 是 A 的评论时，B 下载的文件与 A 放同一个目录（用户要求）——副本自身的
    fwd_from 指向讨论组，不覆盖的话评论会另起一个「××群组/」目录。
    """
    if not origin:
        return None
    return (origin.get("source_name") or "").strip() or None


def _origin_date(origin):
    """频道原帖快照 → ISO 日期串（无则 None），随记录持久化。"""
    if not origin or origin.get("date") is None:
        return None
    try:
        return origin["date"].isoformat()
    except Exception:
        return None


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
    origin = await resolve_origin_snapshot(message)
    await enqueue_media(
        message, state.MY_ID, _origin_folder(origin),
        album_caption=album_caption,
        user_label=user_label,
        parent_date=_origin_date(origin),
        parent_caption=_origin_caption(origin),
    )


async def enqueue_media(message, chat_id, source_override, source_link=None,
                        album_caption=None, user_label=None, src=None,
                        parent_date=None, parent_caption=None):
    """把一条媒体消息入队下载（持久化，重启不丢任务）。

    source_link 显式传入时覆盖默认的来源链接；album_caption 为相册无文字
    成员继承到的同组说明（入队即随记录持久化，下载命名时使用）；user_label
    为手工转发评论标注（同样随记录持久化）；src 为台账输入侧来源标记
    （标签监听传 "listen"，缺省按 chat_id 归属推断 收藏/中转）。
    入队前先过重复媒体判重（tg:<file_unique_id> 两级：已下载索引 + 在途
    队列）：命中只拦下载不拦转发（白名单转发的「转发自」副本照旧留在收藏
    夹当书签），并通知；键拿不到或 /dedup off 时照常入队。
    """
    keys = dedup.media_keys(message)
    if keys:
        logger.info(f"🛡 判重键 {' '.join(keys)}（消息 {message.id}）")
    skip, notice = dedup.should_skip(keys)
    if skip:
        logger.info(f"⏭️ 重复媒体跳过入队（消息 {message.id}）")
        # 台账输入侧事件：收到但未产生下载任务（无 task_id，不进任务集）
        stats.emit_event("DEDUP_SKIPPED")
        try:
            await notify.notify_user(notice)
        except Exception as e:
            logger.warning(f"发送重复媒体通知失败：{e}")
        return
    record = _build_media_record(
        message, chat_id, source_override, source_link,
        album_caption, user_label, parent_date, parent_caption,
    )
    if keys:
        record["dedup_keys"] = keys  # 在途判重 + 成功后 remember 复用
    await queue.enqueue_and_start(record, src=src)


# ============================================================
# 白名单事件生产者：媒体事件 → 记持久化转发任务（转发归 listener_worker）
# ============================================================
async def record_whitelist_media(message, chat_id, source_override):
    """白名单 chat 媒体事件 → 记持久化转发任务（origin='wl'）。

    事件生产者只「记录」，不转发（与标签监听 Scanner 同构）：任务落
    listener_tasks，Worker 受控转发 + 入队下载。同一单元与扫描生产者并发
    产生时由唯一索引兜底，双转发结构上不可能。DB 不可用回退直下原消息
    （媒体不丢，只丢收藏夹副本——规格 §4.3）。
    """
    if getattr(message, "grouped_id", None):
        await _record_album_member(message, chat_id, source_override)
    else:
        await _record_single(message, chat_id, source_override)


async def _record_single(message, chat_id, source_override):
    """单条媒体 → 一条收藏夹任务。"""
    if wl_scan.all_members_dedup_hit([message]):
        logger.info(
            f"⏭️ 白名单媒体 {message.id} 已下载过（dedup 前置），不建转发任务")
        return
    origin = await resolve_origin_snapshot(message)
    caption = (message.message or "").strip()
    records = listener.build_saved_messages_task(
        chat_id, [message], caption, origin)
    try:
        runtime_db.enqueue_listener_tasks(chat_id, records,
                                          origin="wl", chain="wl")
        logger.info(f"📋 白名单媒体已记任务：{chat_id} #{message.id}")
    except runtime_db.DbUnavailable as e:
        logger.warning(
            f"📋 记白名单任务失败（DB 不可用），回退直下原消息 "
            f"#{message.id}：{e}")
        await _fallback_direct_download(message, chat_id, source_override,
                                        origin)


async def _fallback_direct_download(message, chat_id, source_override,
                                    origin=None):
    """回退直下原消息（DB 不可用；不转发、无收藏夹副本，媒体不丢）。"""
    try:
        await enqueue_media(
            message, chat_id, _origin_folder(origin) or source_override,
            parent_date=_origin_date(origin),
            parent_caption=_origin_caption(origin),
        )
    except Exception as e:
        logger.exception(f"白名单媒体直下回退也失败（msg_id={message.id}）：{e}")


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
# 白名单相册整组建任务协调
# ============================================================
# 相册到达白名单 chat 时是 N 条各自独立的媒体事件、共享 grouped_id。逐条建
# 任务会让 Worker 逐条转发、收藏夹里是 N 条散消息；要变成「一个相册」，整组
# 成员要放进**一条**任务（member_ids 进 payload，Worker 一次 forward 整组）。
# 第一个成员事件到达后不立即建任务，等一个攒批窗口让洪峰其余成员的事件落齐，
# 再从源 chat 拉权威完整成员列表，合成一条任务。与旧直转协调器同构，产出从
# 「转发」变「任务」。单线程事件循环内访问、无需加锁。
_WL_ALBUM_DEBOUNCE_SECONDS = 1.5  # 攒批窗口：等洪峰其余成员事件落齐
_WL_ALBUM_SETTLE_SECONDS = 5.0    # 建任务完成后保留协调条目的宽限（迟到成员直接忽略）
# key=(chat_id, grouped_id) → {"seen": {msg_id: msg}, "task": Task, "done": bool}
_WL_ALBUM_TASKS = {}


async def _record_album_member(message, chat_id, source_override):
    """相册成员事件：登记进组协调状态；首个成员负责起建任务协程。"""
    key = (chat_id, message.grouped_id)
    st = _WL_ALBUM_TASKS.get(key)
    if st is None:
        st = _WL_ALBUM_TASKS[key] = {"seen": {}, "task": None, "done": False}
    if st["done"]:
        return  # 整组已落盘；迟到的重复成员直接忽略（唯一索引双保险）
    st["seen"][message.id] = message
    if st["task"] is None:
        st["task"] = asyncio.create_task(
            _record_album_group(key, chat_id, source_override)
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


async def _record_album_group(key, chat_id, source_override):
    """攒批窗口后整组建任务：拉权威成员 → dedup 前置 → 一条任务落盘。"""
    grouped_id = key[1]
    try:
        await asyncio.sleep(_WL_ALBUM_DEBOUNCE_SECONDS)

        st = _WL_ALBUM_TASKS.get(key)
        if st is None or st["done"]:
            return
        seen_ids = list(st["seen"].keys())
        if not seen_ids:
            return

        members = await _fetch_album_members(
            chat_id, grouped_id, min(seen_ids), max(seen_ids)
        )
        if not members:
            # 拉不到权威列表（读源 chat 失败等）→ 用已登记成员兜底建任务，
            # 媒体不丢；缺失成员由扫描生产者按游标补。
            members = [st["seen"][i] for i in sorted(seen_ids)]
            logger.warning(
                f"📋 相册建任务：未能从源 chat 取到完整成员"
                f"（grouped_id={grouped_id}，已登记 {len(members)} 个）")

        if wl_scan.all_members_dedup_hit(members):
            logger.info(
                f"📋 相册 {chat_id} 组 {grouped_id} 整组已下载过"
                "（dedup 前置），不建转发任务")
        else:
            caption = pick_group_caption_text(members, grouped_id)
            # 整组共用一次解析（同一单元的目标一致），别 N 个成员各查一遍
            origin = await resolve_origin_snapshot(members[0])
            records = listener.build_saved_messages_task(
                chat_id, members, caption, origin)
            try:
                runtime_db.enqueue_listener_tasks(chat_id, records,
                                                  origin="wl", chain="wl")
                logger.info(
                    f"📋 白名单相册已记任务：{chat_id} 组 {grouped_id}"
                    f"（{len(members)} 个成员）")
            except runtime_db.DbUnavailable as e:
                logger.warning(
                    f"📋 相册建任务失败（DB 不可用），逐条回退直下：{e}")
                for m in members:
                    await _fallback_direct_download(m, chat_id,
                                                    source_override, origin)

        st["done"] = True
        # 建完任务保留协调条目一段宽限，迟到成员事件直接忽略；再摘除防积累
        await asyncio.sleep(_WL_ALBUM_SETTLE_SECONDS)
        _WL_ALBUM_TASKS.pop(key, None)

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"相册建任务流程异常（grouped_id={grouped_id}）：{e}")
        _WL_ALBUM_TASKS.pop(key, None)


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
    caption_filter.load_caption_filter_config()
    listener.load_listen_config()
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
    logger.info(
        "📡 标签监听：{}（规则 {} 条，周期 {} 分钟）"
        .format("开启" if state.LISTEN_ENABLED else "关闭",
                len(state.LISTEN_RULES), state.LISTEN_INTERVAL_MINUTES)
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

    # Runtime DB：标签监听的业务状态层（SQLite）。**必须在登录前就绪**，
    # 后面的队列恢复/Worker 启动都要用。初始化失败只降级「标签监听不工作」，
    # 绝不让整个 userbot 起不来。
    if runtime_db.init_db():
        state.RUNTIME_DB_READY = True
        migrated = listener.migrate_legacy_state()
        if migrated.get("chats") or migrated.get("tasks"):
            logger.warning(
                f"🗄 已从旧 listen_state.json 迁移：checkpoint {migrated['chats']} 个"
                f" / 待续做任务 {migrated['tasks']} 条"
                + (f"（跳过 {migrated['skipped']} 个已有游标）"
                   if migrated.get("skipped") else "")
                + ("，旧文件已改名 .migrated" if migrated.get("moved") else "")
            )
    else:
        state.RUNTIME_DB_READY = False
        logger.error(
            "🗄 Runtime DB 不可用：标签监听本次不启动（下载等其余功能不受影响）"
        )

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

    # 任务事件日志裁剪（台账按 task_id 重建的数据源，保尾部控制体积）
    stats.trim_event_file()

    # Chrome Agent 结果通知轮询（读 chrome_tasks.json 终态 → 通知收藏夹）
    asyncio.create_task(chrome_client.notify_loop())

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
    retry_sweeper_task = asyncio.create_task(_retry_sweeper())
    # 标签监听：Scanner（定时生产任务）+ Worker（常驻受控执行）
    listener_task = asyncio.create_task(_listener_loop())
    listener_worker_task = None
    if state.RUNTIME_DB_READY:
        # 启动时必须先恢复过期租约（§24）：上次进程崩溃遗留的 PROCESSING
        # 任务否则会一直卡到租约自然到期。
        recovered = listener_worker.recover_expired()
        if recovered:
            logger.warning(f"🗄 启动恢复：{recovered} 条租约过期的监听任务已回到待执行")
        listener_worker_task = listener_worker.start_worker()
    # 白名单扫描生产者（停机补漏链）：与标签监听扫描并列的独立循环
    wl_scan_task = None
    if state.RUNTIME_DB_READY:
        wl_scan_task = asyncio.create_task(_whitelist_scan_loop())
    # Runtime Reporter（只读观察者）：发启动通知 + 后台刷新状态面板
    reporter_instance, reporter_task = await _start_reporter()

    try:
        await state.STOP_EVENT.wait()
        logger.info("🛑 收到停止信号，正在收尾退出...")
    finally:
        # Reporter 的关闭通知必须赶在客户端被拆掉之前发；stop() 内部带超时，
        # Telegram 不可用时也只是多等一会儿，绝不阻塞退出（规格 §28）。
        if reporter_instance is not None:
            try:
                await reporter_instance.stop()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("🤖 Reporter 关闭通知失败（不影响退出）")
        # 取消后台服务任务：主客户端挂在 run_until_disconnected，取消会触发其
        # finally 里的 disconnect，随后以 CancelledError 收尾；bot 探活/重放
        # 扫描/Reporter 主循环同理。
        for t in (main_serve_task, bot_keepalive_task, retry_sweeper_task,
                  reporter_task, listener_task, listener_worker_task,
                  wl_scan_task):
            if t is not None:
                t.cancel()
        for t in (main_serve_task, bot_keepalive_task, retry_sweeper_task,
                  reporter_task, listener_task, listener_worker_task,
                  wl_scan_task):
            if t is not None:
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("后台服务任务清理出错")
        # Worker 已在取消时把手上的任务放回 PENDING；这里兜底再释放一次
        listener_worker.release_inflight()
        runtime_db.close_db()
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
