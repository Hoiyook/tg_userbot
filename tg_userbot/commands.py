"""Saved Messages 的 / 命令分发器（handle_command）。

只处理来自 “me” 的消息（事件入口 new_message_handler 先判定归属，白名单
chat 无法触发命令）。是各命令服务函数的薄分发器——正文逻辑分别住在
text / thread / whitelist / queue / cleanup 模块，与 bot 按钮菜单共用。
运行态（client / QUEUE / QUEUE_LOCK / EXECUTING / DOWNLOAD_CONCURRENCY /
CLEAR_INTERVAL_SECONDS / CLEAR_TIME_CHANGED）一律读 state.*；跨模块函数
以模块对象调用。
"""
import asyncio
import time

from . import state
from . import config
from . import msg_history
from . import chrome_client
from . import text
from . import queue
from . import thread
from . import dedup
from . import whitelist
from . import cleanup
from . import stats
from . import finder
from . import manual_links
from . import listener
from . import caption_filter
from . import wl_scan
from . import runtime_db
from . import sql_templates
from . import shell
from . import upload
from . import pawchive
from . import sources
from . import cmd_templates
from .config import (
    BOT_USERNAME,
    DONE_DEFAULT_LINES,
    DONE_MAX_LINES,
    DOWNLOAD_CONCURRENCY_MAX,
    DOWNLOAD_CONCURRENCY_MIN,
    LOG_FILE,
    LOG_RETENTION_DAYS,
    DOWNLOAD_DIR,
    SQL_CONSOLE_MAX_ROWS,
)
from .log import logger


async def _reply(event, payload, **kwargs):
    """指令回复统一出口：多行文本代码块化（首行前缀行留外，见
    text.with_code_block）。全部 handle_command 分支的回复都走这里。"""
    if "buttons" in kwargs:
        kwargs["buttons"] = text.clean_buttons(kwargs["buttons"])
    await event.reply(text.with_code_block(payload), **kwargs)


def _help2_text():
    """数据字典：数据库表 + 运行目录配置文件的作用（/help2）。

    表清单动态取自 sqlite_master（未来新增表自动出现），作用说明为静态
    映射；未收录的表标注（未记录）。"""
    lines = ["📖 TG Userbot 数据字典", ""]
    lines.append("【数据库表】")
    known_tables = {
        "schema_meta": "schema 版本与元信息",
        "listener_checkpoints": "监听/白名单扫描的消息游标（listen/wl 双链）",
        "listener_tasks": "标签监听+白名单双通道的转发任务（origin 区分）",
        "listener_follows": "评论跟进关注列表（命中帖按天跟进）",
        "task_events": "listener 任务事件（自增 id，与下载事件无关）",
        "download_tasks": "下载队列（QUEUED/RETRY，终态删行）",
        "download_events": "下载任务事件流水（/stats 台账数据源，追加不改）",
        "download_history": "下载历史（/done、/find 数据源）",
        "dedup_index": "去重索引（tg:/dyc:/f:/c: 四类键）",
        "pawchive_posts": "Pawchive 帖子生命周期（PENDING→PROCESSING→终态/ARCHIVED）",
        "pawchive_files": "Pawchive 帖子附件文件状态（.part 续传）",
        "manual_links": "手动外链台账（/links，发送链接即登记）",
    }
    try:
        tables = sorted(runtime_db.list_tables())
    except runtime_db.DbUnavailable:
        tables = sorted(known_tables)
        lines.append("（DB 未连接：以下为静态清单）")
    for t in tables:
        if t == "sqlite_sequence":
            continue
        lines.append(f"  {t}  ——  {known_tables.get(t, '（未记录）')}")
    lines.append("")
    lines.append("【运行目录配置文件】（RUNTIME_DIR）")
    lines.append("  listen.json  ——  标签监听规则（/listen 维护）")
    lines.append("  whitelist_config.json  ——  下载白名单（/wl 维护）")
    lines.append("  thread_config.json  ——  并发下载数（/thread 维护）")
    lines.append("  clear_time.json  ——  自动清理间隔（/setcleartime）")
    lines.append("  dedup_config.json  ——  去重开关（/dedup）")
    lines.append("  caption_filter.json  ——  Caption 命名清洗规则")
    lines.append("  sql_templates.json  ——  SQL 查询模板（/sqlt）")
    lines.append("  shell_state.json  ——  /sh 的工作目录（会记住）")
    lines.append("  download_queue.json  ——  旧队列 JSON（仅 json 回滚模式用）")
    lines.append("  *.imported  ——  已迁 DB 的旧文件归档（别删）")
    lines.append("  download.log / chrome_agent.log / cd2_launch.log  ——  技术日志")
    lines.append("  chrome_tasks/requests/cancel_requests.json  ——  Chrome IPC")
    lines.append("  userbot.out / chrome_agent.out / *.pid  ——  启动输出与 PID")
    lines.append("")
    lines.append("【仓库根】tg_secrets.json  ——  密钥与 cookie（api_id / ")
    lines.append("bot_token / douyin_cookie / pawchive_cookie / cd2 等）")
    return "\n".join(lines)


# 多词指令的子命令映射（2026-09-25 统一重构）：空格形态在指令入口被
# 规范化改写为下划线形态（/paw plan → /paw_plan），下划线为标准形、
# 空格形保留为兼容别名——Telegram 的命令补全面板只认单 token，
# /paw_plan 是「真命令」，/paw plan 不是。各模块 parse 同步兼容两种头。
_COMMAND_SUBS = {
    "/paw": {"help", "status", "plan", "search", "retry", "pause",
             "resume", "manual", "done", "archive", "att", "post",
             "cookie", "csv", "find", "pr", "since", "fail",
             "progress", "backfill", "notify"},
    "/listen": {"on", "off", "scan", "list", "add", "del", "interval",
                "edit", "failed", "retry"},
    "/wl": {"list", "add", "del", "scan", "since"},
    "/retry": {"all", "del"},
    "/queue": {"del"},
    "/sqlt": {"add", "del"},
    "/cmdt": {"add", "del", "run"},
    "/caption_filter": {"add", "del", "test"},
}


def _canonical_command(text):
    """『/基指令 子命令 …』→『/基指令_子命令 …』（仅映射表内的组合改写）。

    纯文本改写：/paw_plan X 与 /paw plan X 在入口即归一为同一串
    （parse 头部均已兼容下划线）。非映射组合原样返回。"""
    parts = str(text or "").split(maxsplit=1)
    if len(parts) < 2:
        return text
    base = parts[0].split("@")[0].lower()
    subs = _COMMAND_SUBS.get(base)
    if not subs:
        return text
    head, _sep, rest = parts[1].partition(" ")
    sub = head.strip().lower()
    if sub in subs:
        return f"{base}_{sub}" + ((f" {rest}") if rest else "")
    return text


def resolve_shortcut(text):
    """快捷指令解析：文本精确命中 COMMAND_SHORTCUTS → 返回映射命令。

    守卫：不以 / 开头（/ 开头走正常命令）、≤8 字符防误触；命中返回
    「/xxx」形式的命令文本，未命中返回 None。纯函数可单测。
    """
    if not text:
        return None
    key = text.strip()
    if not key or key.startswith("/") or len(key) > 8:
        return None
    return config.COMMAND_SHORTCUTS.get(key)


def usage_name(cmd_text):
    """功能使用审计的名字：指令首词 + 多子命令指令带子词。

    /paw plan → 「/paw plan」（plan/search/retry 等是不同功能）；
    /chrome <URL> → 「/chrome」（绝不把 URL/参数记进名字）。"""
    parts = str(cmd_text or "").split()
    if not parts:
        return ""
    base = parts[0].split("@")[0].lower()
    if base == "/paw" and len(parts) > 1:
        return f"/paw {parts[1].split('@')[0].lower()}"
    return base


def _record_usage(cmd_text):
    """命令使用审计落库；只记已注册指令，DB 异常绝不影响命令执行。"""
    try:
        name = usage_name(cmd_text)
        if not name or name.lstrip("/") not in config.REGISTERED_COMMAND_NAMES:
            return
        runtime_db.feature_usage_bump(name)
    except Exception:
        pass




def _help_text():
    """完整命令手册（下划线标准形；空格形保留为兼容别名）。
    详版（含例子与语义说明）在仓库 docs/操作手册.md，两处同源维护。"""
    return (
        "📖 TG Userbot 操作手册（详版见 docs/操作手册.md）\n"
        "\n"
        "【监控查询】\n"
        "/status｜/folder｜/logpath —— 状态/目录/日志\n"
        "/start —— 打开按钮菜单（给 bot 发任意文本同效）\n"
        "/stats [天数] —— 台账（默认1，上限7）\n"
        "/progress —— 下载进度\n"
        "/done [N][关键词] —— 下载记录（默认10）\n"
        "/find 关键词 —— 媒体下落三源查询\n"
        "/queue；/queue_del 序号 —— 队列/移除\n"
        "/retry；/retry_all；/retry_del 序号 —— 待重试\n"
        "/usage —— 功能使用统计\n"
        "/cmdhis [N] —— 最近发给 bot 的消息（发 1 同效）\n"
        "\n"
        "【下载自动行为】\n"
        "媒体发进/转发进收藏夹 = 自动下载；白名单 chat 同理（留副本）\n"
        "评论『/子目录#标注』（前后5秒）= 落子目录并加标注\n"
        "抖音/IG 链接发收藏夹 = 自动转解析 bot 下载\n"
        "/dedup [off]；/thread [3-20] —— 去重/并发\n"
        "\n"
        "【外链台账】\n"
        "发『链接 备注』即登记（自动查重）；/links [关键词] 清单/搜索\n"
        "\n"
        "【Pawchive】\n"
        "/paw（=status）—— 状态/失败画像/Cookie/扫描进度\n"
        "/paw_plan 作者 [all]；/paw_plan 作者 since 日期 [all]\n"
        "/paw_progress 作者 —— 按作者进度（完成率/死链/待办）\n"
        "/paw_search 词；/paw_post URL|ID（可刷新补差）\n"
        "/paw_att URL|ID|行id —— 附件外链状态\n"
        "/paw_pr URL|ID|行id —— 执行详情报告\n"
        "/paw_find 词；/paw_csv [作者]\n"
        "/paw_manual [export]；/paw_done 行id|起-止|作者\n"
        "/paw_fail —— 非死链失败明细\n"
        "/paw_retry 行ID|all；/paw_pause｜/paw_resume\n"
        "/paw_archive —— 归档明细；/paw_archive_del 行id|起-止\n"
        "/paw_backfill 作者 —— 回填历史帖（补站点后补的数据）\n"
        "/paw_since 日期|off —— 默认时间下限\n"
        "/paw_cookie Cookie —— 会话 Cookie\n"
        "/paw_notify on|off —— 帖子开始/完成通知开关\n"
        "\n"
        "【Chrome】\n"
        "/chrome [子目录/][#标注] URL —— 直链下载，网盘页可见打开\n"
        "/chrome_start｜/chrome_stop｜/chrome_status\n"
        "/chrome_tasks；/chrome_cancel 序号\n"
        "\n"
        "【工具/系统】\n"
        "/sh [命令]；/sh cd 目录 —— 命令行（黑名单纪律）\n"
        "/up 路径 —— 上传到收藏夹\n"
        "/cmdt 列表｜/cmdt_add 名 命令｜/cmdt_del 名｜/cmdt_run 名\n"
        "/sql 一条SQL —— 诊断控制台\n"
        "/sqlt 列表｜/sqlt_add 名 SQL｜/sqlt_del 名｜/sqlt 名\n"
        "/listen 列表｜/listen_on｜/listen_off｜/listen_scan\n"
        "/listen_failed —— 失败任务明细（/listen_retry 行id 重试）\n"
        "/listen_add 聊天 标签 [目标,目标] [on|off]｜/listen_edit 序号\n"
        "/listen_del 序号｜/listen_interval 分钟\n"
        "/wl 列表｜/wl_add ID或@名｜/wl_del ID或序号｜/wl_scan\n"
        "/wl_since 聊天 消息id —— 回补存量\n"
        "/caption_filter 列表｜/caption_filter_add 规则\n"
        "/caption_filter_del 序号｜/caption_filter_test 原文\n"
        "/clean｜/clearmsg｜/setcleartime 30s|1m|1h|off\n"
        "/origin —— 解析失败账本；/cd2ck —— 115 备份对账\n"
        "/restart —— 重启 bot（优雅停机，约 30 秒）\n"
        "/help2 —— 数据表与配置文件字典\n"
        "注：/paw_plan 与 /paw plan 两种写法等效（下划线为标准形）"
    )



def _schedule_restart(delay=2.0):
    """延时优雅重启自身：派生脱离进程组的守护脚本，等本进程退出后跑
    ./run.sh start（userbot 拉起；Chrome Agent 本就独立运行，不受影响）。

    关键点：helper 必须 start_new_session 脱离本进程组——否则本进程退出
    连带杀掉等待中的 helper，重启就断了。SIGTERM 走既有优雅停机处理器
    （保存状态/断开 worker/释放租约），与 run.sh stop 同一退出路径。"""
    import os
    import signal
    import subprocess
    import threading

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pid = os.getpid()
    helper = (
        f"sleep {delay + 1:.0f}; "
        f"while kill -0 {pid} 2>/dev/null; do sleep 1; done; "
        f"cd '{repo}' && ./run.sh start "
        f">>'{config.RUNTIME_DIR}/restart.log' 2>&1"
    )
    subprocess.Popen(["sh", "-c", helper], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _term():
        os.kill(pid, signal.SIGTERM)

    threading.Timer(delay, _term).start()


async def handle_command(event, cmd_text):

    # 多词指令规范化（2026-09-25）：/paw plan 与 /paw_plan 归一为下划线
    # 标准形，此后全链路（审计/分发）只见标准形
    cmd_text = _canonical_command(cmd_text)

    # 功能使用审计（2026-09-24）：只记已注册指令，失败不影响命令
    _record_usage(cmd_text)

    # 快捷指令（如发 1 = /cmdhis）：调用方（bot 对话 / 主账号 Saved Messages）
    # 已各自解析过才会带映射命令进来，这里不做二次解析
    if cmd_text == "/cmdhis" or cmd_text.startswith("/cmdhis "):
        # 最近发给 bot 的消息记录（2026-09-25 需求重定义：不是 sh 历史）：
        # 正文进代码块、无序号——整块长按即可复制，单行复制不带前缀
        parts = cmd_text.split(maxsplit=1)
        limit = 30
        if len(parts) == 2 and parts[1].isdigit():
            limit = max(1, min(int(parts[1]), 50))
        await _reply(event, msg_history.render_text(limit))
        logger.info("执行命令：/cmdhis")
        return True

    if cmd_text == "/status":
        await _reply(event, text.status_text())
        logger.info("执行命令：/status")
        return True

    if cmd_text == "/folder":
        await _reply(event, f"📁 保存目录：\n{DOWNLOAD_DIR}")
        logger.info("执行命令：/folder")
        return True

    if cmd_text == "/logpath":
        await _reply(event, f"📋 日志文件：\n{LOG_FILE}")
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
            await _reply(event, text.done_reply_text(n, keyword))
            logger.info(f"执行命令：/done 关键词「{keyword}」")
            return True

        await _reply(event, text.done_reply_text(n))
        logger.info(f"执行命令：/done {n}")
        return True

    if cmd_text == "/progress" or cmd_text == "/downloading":
        await _reply(event, text.progress_text())
        logger.info(f"执行命令：/progress | 进行中 {len(state.ACTIVE_DOWNLOADS)} 个")
        return True

    if cmd_text == "/thread" or cmd_text.startswith("/thread "):
        parts = cmd_text.split(maxsplit=1)
        if len(parts) == 1:
            await _reply(event, 
                f"🧵 当前并发下载数：{state.DOWNLOAD_CONCURRENCY}\n"
                f"用法：/thread 3（{DOWNLOAD_CONCURRENCY_MIN}-{DOWNLOAD_CONCURRENCY_MAX}）\n"
                "每条下载各占一条独立连接，n 路 ≈ n 倍单路速度"
            )
            logger.info("执行命令：/thread（查询）")
            return True
        ok, msg = thread.apply_thread_limit(parts[1])
        await _reply(event, msg)
        logger.info(f"执行命令：/thread {parts[1]} 成功={ok}")
        return True

    if dedup.is_dedup_command(cmd_text):
        parts = cmd_text.split(maxsplit=1)
        if len(parts) == 1:
            await _reply(event, dedup.status_text())
            logger.info("执行命令：/dedup（查询）")
            return True
        arg = parts[1].strip().lower()
        await _reply(event, dedup.set_enabled(arg == "on"))
        logger.info(f"执行命令：/dedup {arg}")
        return True

    parsed_wl = whitelist.parse_wl_command(cmd_text)
    if parsed_wl is not None:
        action, arg = parsed_wl
        logger.info(f"执行命令：/wl {action}")

        if action == "list":
            await _reply(event, text.wl_list_text(
                scan_info=wl_scan.collect_scan_info(),
                last_scan=state.WL_LAST_SCAN))
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
                        await _reply(event, 
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
                        await _reply(event, 
                            "❌ /wl：回复的消息不是转发的，取不到来源 chat"
                        )
                        return True
                    chat_id, title = await whitelist.resolve_wl_target(
                        state.client, None, reply_msg.fwd_from
                    )

                if chat_id is None:
                    await _reply(event, 
                        f"❌ /wl：无法找到该 chat：{arg or '转发来源'}"
                    )
                    return True
                ok, msg = whitelist.add_to_whitelist(chat_id, title)
                await _reply(event, msg)
            except Exception as e:
                logger.warning(f"/wl add 失败：{e}")
                await _reply(event, 
                    f"❌ /wl：无法找到该 chat：{arg or '转发来源'}"
                )
            return True

        if action == "del":
            ok, msg = whitelist.del_from_whitelist(arg or "")
            await _reply(event, msg)
            return True

        if action == "scan":
            totals = await wl_scan.scan_all(manual=True)
            await _reply(event, wl_scan.summary_text(totals))
            return True

        if action == "since":
            parts = (arg or "").split()
            if len(parts) != 2:
                await _reply(event, 
                    "❌ 用法：/wl since <序号|@用户名|ID> <消息id>\n"
                    "例：/wl since 1 88000 —— 从 #88000 之后开始回补")
                return True
            ok, msg = await wl_scan.since_checkpoint(
                state.client, parts[0], parts[1])
            await _reply(event, msg)
            if ok:
                wl_scan.spawn_scan()
            return True

        await _reply(event, 
            "❌ 用法：/wl list | /wl add <ID或@用户名> | /wl del <ID或序号>\n"
            "        /wl scan（立即扫描）| /wl since <聊天> <消息id>（回补）")
        return True

    if runtime_db.is_sql_command(cmd_text):
        arg = cmd_text[len("/sql"):].strip()
        if not arg:
            await _reply(event, 
                "📋 SQL 诊断控制台（owner-only，直接作用于 runtime DB）\n\n"
                "用法：/sql <一条 SQL>\n"
                "例：/sql SELECT * FROM listener_tasks ORDER BY id DESC "
                f"LIMIT 5\n"
                "    /sql PRAGMA table_info(listener_tasks)\n"
                "    /sql SELECT * FROM listener_checkpoints\n"
                "查询最多显示 "
                f"{SQL_CONSOLE_MAX_ROWS} 行；写语句立即生效，无撤销；"
                "病态慢查询会卡住程序，请勿对大表做无 LIMIT 的笛卡尔积。")
            return True
        try:
            result = runtime_db.execute_user_sql(arg)
        except runtime_db.DbUnavailable as e:
            await _reply(event, f"{text.SQL_TEXT_PREFIX}\n❌ Runtime DB 不可用：{e}")
            return True
        await _reply(event, text.format_sql_result(result))
        logger.info(f"执行命令：/sql {arg[:80]}")
        return True

    if sql_templates.is_sqlt_command(cmd_text):
        action, arg = sql_templates.parse_sqlt_command(cmd_text)
        if action == "list":
            await _reply(event, sql_templates.list_text())
            return True
        if action == "add":
            parts = (arg or "").split(None, 1)
            if len(parts) != 2:
                await _reply(event, 
                    f"❌ 用法：/sqlt add <名字> <SQL>\n"
                    "例：/sqlt add 待执行 SELECT * FROM listener_tasks")
                return True
            ok, msg = sql_templates.upsert(parts[0], parts[1])
            await _reply(event, msg)
            return True
        if action == "del":
            ok, msg = sql_templates.delete(arg or "")
            await _reply(event, msg)
            return True
        # run：按名字执行模板（执行语义与 /sql 完全一致）
        ok, result = sql_templates.execute_template(arg or "")
        if not ok:
            await _reply(event, result)
            return True
        try:
            await _reply(event, text.format_sql_result(result))
        except runtime_db.DbUnavailable as e:
            await _reply(event, 
                f"{text.SQL_TEXT_PREFIX}\n❌ Runtime DB 不可用：{e}")
        logger.info(f"执行命令：/sqlt {arg}")
        return True

    if queue.is_queue_command(cmd_text):
        parts = cmd_text.split(maxsplit=1)
        if len(parts) == 1:
            await _reply(event, 
                queue.format_queue_text(state.QUEUE), link_preview=False
            )
        elif cmd_text.startswith("/queue_del"):
            try:
                idx = int(parts[1])
            except (IndexError, ValueError):
                idx = None
            except (IndexError, ValueError):
                await _reply(event, "❌ /queue del 用法：/queue del <序号>")
                return True
            # 执行中的任务先真正取消在途下载（task.cancel → 清半成品、归还
            # worker），再把记录移除；排队中的直接移除。
            ok, removed, cancelled = await queue.queue_del_task(index=idx)
            if ok:
                verb = "🛑 已取消下载并移除" if cancelled else "✅ 已从队列移除"
                await _reply(event, f"{verb}：{removed.get('label', '')}")
            else:
                await _reply(event, "❌ /queue：序号无效，用 /queue 查看列表")
        else:
            await _reply(event, "❌ 用法：/queue | /queue del <序号>")
        logger.info(f"执行命令：/queue {parts[1] if len(parts) > 1 else ''}")
        return True

    if queue.is_retry_command(cmd_text):
        parts = cmd_text.split(maxsplit=1)
        # 标准形 /retry_all、/retry_del N（入口规范化已把空格形归一）
        if len(parts) == 1:
            view_text, buttons = queue.format_retry_view(state.QUEUE)
            await _reply(event, view_text, buttons=buttons,
                         link_preview=False)
        elif cmd_text.startswith("/retry_all"):
            n, over = queue.retry_all()
            msg = f"🔁 已重放全部待重试任务：{n} 条" if n                 else "🔁 待重试列表为空（或都在执行中）"
            await _reply(event, msg)
            logger.info(f"执行命令：/retry_all | 触发 {n} 条")
            return True
        elif cmd_text.startswith("/retry_del"):
            try:
                idx = int(parts[1])
            except (IndexError, ValueError):
                await _reply(event, "❌ /retry_del 用法：/retry_del <序号>")
                return True
            async with state.QUEUE_LOCK:
                ok, removed = queue.queue_remove(state.QUEUE, "retry", idx)
                if ok:
                    queue._save_after_mutation(removed, "delete")
            if ok:
                await _reply(event, 
                    f"✅ 已从待重试列表移除：{removed.get('label', '')}"
                )
            else:
                await _reply(event, 
                    "❌ /retry_del：序号无效，用 /retry 查看列表"
                )
        else:
            try:
                idx = int(parts[1])
            except ValueError:
                await _reply(event, 
                    "❌ 用法：/retry | /retry all | /retry <序号> | /retry del <序号>"
                )
                return True
            async with state.QUEUE_LOCK:
                retry_list = state.QUEUE["retry"]
                record = (
                    retry_list[idx - 1] if 1 <= idx <= len(retry_list) else None
                )
            if record is None:
                await _reply(event, "❌ /retry：序号无效，用 /retry 查看列表")
                return True
            if record["id"] in state.EXECUTING:
                await _reply(event, "⏳ 该任务正在执行中")
                return True
            queue.spawn_execute(record)
            await _reply(event, f"▶️ 已重新执行：{record.get('label', '')}")
        logger.info(f"执行命令：/retry {parts[1] if len(parts) > 1 else ''}")
        return True

    if cmd_templates.is_cmdt_command(cmd_text):
        action, arg = cmd_templates.parse_cmdt_command(cmd_text)
        logger.info(f"执行命令：/cmdt {action}")
        if action in ("list", "help"):
            await _reply(event, cmd_templates.list_text())
            return True
        if action == "add":
            parts = (arg or "").split(None, 1)
            if len(parts) != 2:
                await _reply(event,
                             "❌ 用法：/cmdt add <名字> <命令>")
                return True
            ok, msg = cmd_templates.upsert(parts[0], parts[1])
            await _reply(event, msg)
            return True
        if action == "del":
            ok, msg = cmd_templates.delete(arg or "")
            await _reply(event, msg)
            return True
        if action == "run":
            ok, result = await cmd_templates.execute(arg or "")
            await _reply(event, result if ok else result)
            return True
        return True

    if cmd_text == "/origin":
        await _reply(event, sources.origin_failures_text())
        logger.info("执行命令：/origin")
        return True

    if pawchive.is_paw_command(cmd_text):
        # Pawchive：扫描/收藏对比/Chrome 批量下载（解析与回复全在模块内）
        logger.info(f"执行命令：{cmd_text[:60]}")
        await pawchive.command_reply(event, cmd_text)
        return True

    if chrome_client.is_chrome_dispatch(cmd_text):
        await chrome_client.handle_chrome_command(
            event, cmd_text,
            owner_id=chrome_client.resolve_owner_id(state.MY_ID))
        return True

    if stats.is_stats_command(cmd_text):
        parts = cmd_text.split()
        days = 1
        if len(parts) > 1:
            try:
                days = int(parts[1])
            except ValueError:
                await _reply(event, 
                    f"❌ /stats：参数须为天数（1-{LOG_RETENTION_DAYS}），"
                    "如 /stats 3"
                )
                return True
        days = max(1, min(days, LOG_RETENTION_DAYS))
        logger.info(f"执行命令：/stats {days if days > 1 else ''}".rstrip())
        await _reply(event, stats.stats_text(days), link_preview=False)
        return True

    parsed_caption = caption_filter.parse_caption_filter_command(cmd_text)
    if parsed_caption is not None:
        action, arg = parsed_caption
        logger.info(f"执行命令：/caption_filter {action}")
        await _reply(event, caption_filter.command_reply(action, arg))
        return True

    parsed_listen = listener.parse_listen_command(cmd_text)
    if parsed_listen is not None:
        action, arg = parsed_listen
        logger.info(f"执行命令：/listen {action}")
        if action == "failed":
            await _reply(event, listener.failed_tasks_text())
            return True
        if action == "retry":
            await _reply(event, listener.retry_failed_task(arg))
            return True
        await _reply(event, await listener.command_reply(action, arg),
                          link_preview=False)
        return True

    if finder.is_find_command(cmd_text):
        # 媒体下落查询：完整字段子串匹配（列表视图截尾 48 字符是它存在的理由）
        parts = cmd_text.split(maxsplit=1)
        keyword = parts[1].strip() if len(parts) > 1 else ""
        logger.info(f"执行命令：/find {keyword}")
        await _reply(event, finder.find_media(keyword), link_preview=False)
        return True

    if cmd_text == "/links" or cmd_text.startswith("/links "):
        parts = cmd_text.split(maxsplit=1)
        keyword = parts[1].strip() if len(parts) > 1 else None
        logger.info(f"执行命令：/links {keyword or ''}".rstrip())
        view_text, buttons = manual_links.links_view(keyword=keyword)
        await _reply(event, view_text, buttons=buttons)
        return True

    if shell.is_shell_command(cmd_text):
        logger.info(f"执行命令：{cmd_text[:60]}")
        await _reply(event, await shell.command_reply(cmd_text),
                          link_preview=False)
        return True

    if cmd_text == "/up" or cmd_text.startswith("/up "):
        await upload.command_reply(event, cmd_text)
        return True

    if cmd_text == "/help":
        await _reply(event, _help_text())
        logger.info("执行命令：/help")
        return True

    if cmd_text == "/help2":
        logger.info("执行命令：/help2")
        await _reply(event, _help2_text())
        return True

    if cmd_text.startswith("/setcleartime"):
        parts = cmd_text.split(maxsplit=1)
        if len(parts) == 1:
            current = (
                "已关闭" if state.CLEAR_INTERVAL_SECONDS <= 0
                else cleanup.format_clear_interval(state.CLEAR_INTERVAL_SECONDS)
            )
            await _reply(event, 
                f"⏱ 自动清理当前间隔：{current}\n用法：/setcleartime 1m\n"
                "支持：30s、1m、2m、1h\n关闭：/setcleartime off"
            )
            return True
        try:
            seconds = cleanup.parse_clear_interval(parts[1])
        except ValueError:
            await _reply(event, 
                "❌ 格式错误。示例：/setcleartime 1m、/setcleartime 2m、"
                "/setcleartime 1h、/setcleartime off"
            )
            return True
        state.CLEAR_INTERVAL_SECONDS = seconds
        cleanup.save_clear_interval(seconds)
        if state.CLEAR_TIME_CHANGED is not None:
            state.CLEAR_TIME_CHANGED.set()
        await _reply(event, 
            "⏸ 自动清理已关闭。"
            if seconds == 0
            else f"✅ 自动清理间隔已设置为 {cleanup.format_clear_interval(seconds)}。"
        )
        return True

    if cmd_text == "/clearmsg":
        logger.info("执行命令：/clearmsg")
        try:
            # 双入口甄别：bot 对话与收藏夹的消息 id 是两个空间。只有收藏夹
            # 路径（event.client 即主客户端）才有「本命令消息」要跳过/删除；
            # bot 路径若按 bot 对话 id 操作收藏夹，会按同数字 id 盲删用户
            # 保留的内容（2026-09-09 验收发现）。扫描大收藏夹约需 1 分钟，
            # 先回执再干活的避免「以为无效」。
            from_saved = getattr(event, "client", None) is state.client

            await _reply(event, 
                "🧹 正在扫描收藏夹程序消息（最多 3000 条，约需 1 分钟）…\n"
                "完成后会再回复结果。"
            )

            delete_ids = []
            async for message in state.client.iter_messages("me", limit=3000):
                if from_saved and message.id == event.message.id:
                    continue

                if cleanup.is_cleanup_message(message,
                                           include_persistent=True):
                    delete_ids.append(message.id)

            count = len(delete_ids) + (1 if from_saved else 0)

            await _reply(event, 
                f"🧹 清理完成，共删除 {count} 条程序相关消息\n\n"
                "已清理：抖音/IG 链接指令、程序通知、程序命令、/clearmsg 指令\n"
                "收藏的媒体副本与普通收藏内容会保留。"
            )

            # 删除其它程序消息；收藏夹路径最后删除当前 /clearmsg 指令。
            if delete_ids:
                await state.client.delete_messages("me", delete_ids)
            if from_saved:
                await state.client.delete_messages("me", [event.message.id])

            logger.info(
                f"执行命令：/clearmsg | 删除 {count} 条程序相关消息"
                f"{'（收藏夹入口）' if from_saved else '（bot 对话入口）'}"
            )

        except Exception as e:
            logger.exception(f"/clearmsg 执行失败：{e}")
            # 如果清理过程中失败，至少尝试保留错误信息。
            try:
                await _reply(event, f"❌ 清理失败：{e}")
            except Exception:
                pass

        return True

    if cmd_text == "/restart":
        await _reply(event,
                     "🔁 重启中……预计 20~40 秒恢复（自动拉起，无需手动操作）\n"
                     "期间消息不丢：队列/监听 checkpoint 已持久化，"
                     "恢复后自动继续。")
        logger.info("执行命令：/restart —— 触发优雅重启")
        _schedule_restart()
        return True

    if cmd_text == "/usage":
        # 功能使用审计（2026-09-24）：次数排行 + 近 7 天 + 最后使用
        rows = runtime_db.feature_usage_top(20)
        if not rows:
            await _reply(event, "📊 功能使用统计：还没有记录")
            return True
        lines = ["📊 功能使用统计（累计｜近7天｜最后使用）", ""]
        for i, r in enumerate(rows, 1):
            last = time.strftime("%m-%d %H:%M", time.localtime(r["last_at"]))
            lines.append(
                f"{i}. {r['name']} — {r['total']}｜{r['recent7']}"
                f"｜{last}")
        lines.append("")
        lines.append("共 {n} 个功能（/sql 查 feature_usage 表可自由聚合）"
                     .format(n=len(rows)))
        await _reply(event, "\n".join(lines)[:3900])
        logger.info("执行命令：/usage")
        return True

    if cleanup.is_botclean_command(cmd_text):
        arg = cmd_text.split(maxsplit=1)[1] if " " in cmd_text else None
        await _reply(event, cleanup.botclean_reply(arg))
        logger.info(f"执行命令：{cmd_text[:40]}")
        return True

    if cmd_text == "/clean":
        count = cleanup.clean_temp_files()
        await _reply(event, f"🧹 清理完成，共删除 {count} 个临时文件")
        logger.info(f"执行命令：/clean | 删除 {count} 个临时文件")
        return True

    if cmd_text == "/cd2ck":
        # 115 备份对账：walk 大目录 + 读备份日志是阻塞活，放线程跑
        await _reply(event, "🔍 对账中：扫描本地滞留媒体 × 近 3 天备份日志…")
        from . import cd2
        out = await asyncio.to_thread(cd2.reconcile_text)
        await _reply(event, out)
        logger.info("执行命令：/cd2ck | 115 备份对账")
        return True

    return False
