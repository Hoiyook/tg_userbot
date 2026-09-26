"""展示文本（Saved Messages 命令与 bot 按钮菜单共用）。

status_text / progress_text / done_reply_text / wl_list_text 都是同步函数，
被 commands.py（handle_command）与 bot.py（handle_menu_action）复用。
运行态一律读 state.*（client / MY_ID / ACTIVE_DOWNLOADS / WHITELIST_CHATS）；
format_size 来自 naming、get_history_lines 来自 history（均为纯叶子）。
"""
from . import state
from . import config
from .config import LOG_FILE, DOWNLOAD_DIR
from .history import get_history_lines
from .naming import format_size


def status_text():
    """生成 /status 回复文本。健康灯随实际连接状态变化（2026-09-18 修复：
    此前固定 🟢「状态正常」，断连时出现「绿色正常 + 连接断开」的自相矛盾）。"""
    try:
        connected = bool(state.client and state.client.is_connected())
    except Exception:
        connected = False
    if connected:
        head = "🟢 TG Userbot 运行正常"
        conn_line = "连接：正常"
    else:
        head = "🔴 TG Userbot 连接异常"
        conn_line = "连接：断开"
    return (
        f"{head}\n\n"
        f"{conn_line}\n"
        f"用户 ID：{state.MY_ID}\n"
        f"保存目录：{DOWNLOAD_DIR}\n"
        f"日志：{LOG_FILE}"
    )


def progress_text():
    """生成 /progress 回复文本（2026-09-26 UX3：全局聚合视图）。

    只读各模块现有状态，绝不重写 worker：普通下载有明细（百分比），
    其余模块给计数——某模块拿不到就跳过，不伪造。"""
    sections = []

    # 📥 普通下载：有明细
    if state.ACTIVE_DOWNLOADS:
        lines = []
        for info in sorted(
            state.ACTIVE_DOWNLOADS.values(), key=lambda x: x["filename"]
        ):
            if info["percent"] is None:
                prog = f"{format_size(info['downloaded'])}/未知大小"
            else:
                prog = (
                    f"{info['percent']}% "
                    f"({format_size(info['downloaded'])}/{format_size(info['total'])})"
                )
            line = f"[{info['label']}] {info['filename']} - {prog}"
            if info.get("link"):
                line += f"\n  来源：{info['link']}"
            lines.append(line)
        sections.append("📥 普通下载\n" + "\n".join(lines))

    # 🐾 Pawchive：状态计数 + 当前帖
    try:
        from . import runtime_db
        counts = runtime_db.pawchive_status_counts()
        proc = counts.get("PROCESSING", 0)
        pend = counts.get("PENDING", 0)
        if proc or pend:
            line = f"🐾 Pawchive：处理中 {proc} · 待处理 {pend}"
            from . import pawchive_worker
            inflight = pawchive_worker.current_post_label()
            if inflight:
                line += f"\n  当前：{inflight}"
            sections.append(line)
    except Exception:
        pass

    # 📡 标签监听：待处理
    try:
        from . import runtime_db as _rdb
        stats = _rdb.get_listener_stats(origin="listen")
        if stats.get("pending"):
            sections.append(f"📡 标签监听：待处理 {stats['pending']}")
    except Exception:
        pass

    # 🌐 Chrome：进行中
    try:
        from . import chrome_agent, config as _cfg
        tasks = chrome_agent.load_tasks(_cfg.CHROME_TASKS_FILE)
        n = sum(1 for t in tasks
                if t.get("status") in ("RUNNING", "PENDING", "RETRY_WAIT"))
        if n:
            sections.append(f"🌐 Chrome：进行中 {n}")
    except Exception:
        pass

    if not sections:
        return "📊 当前没有进行中的任务（各模块均空闲）"
    return "📊 实时进度\n\n" + "\n\n".join(sections)


def clean_buttons(rows):
    """空按钮列表降级为 None：telethon 发 buttons=[] 会被服务端以
    ReplyMarkupInvalid 拒收（2026-09-16 实测：/links 空态与标记完成后的
    刷新视图因此全部「没反应」）。非空原样返回。

    二道护栏（2026-09-25）：按钮总量超预算时从**倒数第二行**开始逐行丢弃
    （保留首行数据与末行导航），防止 ReplyMarkupTooLongError——任何视图
    的按钮行数失控（如 ls 目录网格）都在这里被兜住。单行就超预算则整体
    降级 None（正文仍在，损失的是按钮）。"""
    if not rows:
        return None
    budget = 5500   # Telegram reply markup 总量上限之下留余量

    def _size(rs):
        total = 0
        for row in rs:
            total += 4   # 行结构开销
            for b in row:
                total += 60   # 单按钮序列化固定开销（构造器名等）
                total += len((getattr(b, "text", "") or "").encode("utf-8"))
                d = getattr(getattr(b, "type", None), "data", None)
                if isinstance(d, (bytes, str)):
                    total += len(d)
                u = getattr(getattr(b, "type", None), "url", None)
                if u:
                    total += len(u.encode("utf-8"))
        return total

    if _size(rows) <= budget:
        return rows
    trimmed = list(rows)
    while len(trimmed) > 2 and _size(trimmed) > budget:
        del trimmed[-2]          # 保末行（导航）与首行（最新数据）
    if _size(trimmed) > budget:
        return None
    return trimmed


def with_code_block(text):
    """指令回复统一代码块化：多行回复的首行（前缀行）留在围栏外——自动
    清理白名单按 startswith 匹配依赖它；其余正文包进 ``` 围栏（Telegram
    客户端自动渲染成可复制的等宽块）。已含围栏（/sh 输出）与单行回复
    （确认类短消息）原样不动。"""
    if not text or "```" in text or "\n" not in text:
        return text
    first, rest = text.split("\n", 1)
    return f"{first}\n```\n{rest}\n```"


def done_reply_text(n, keyword=None):
    """生成 /done 回复文本（命令与 bot 菜单共用）。"""
    if keyword is not None:
        matched = [
            line
            for line in get_history_lines(None)
            if keyword in line.lower()
        ]
        lines = matched[-n:]
        if not lines:
            return f"📜 没有匹配「{keyword}」的下载记录"

        # Telegram 单条消息上限 4096 字符，从最新往前凑到约 3800
        shown = []
        total = 0
        for line in reversed(lines):
            if total + len(line) + 1 > 3800:
                break
            shown.append(line)
            total += len(line) + 1
        shown.reverse()

        if not shown:
            shown = [lines[-1][:3800]]

        return (
            f"📜 匹配「{keyword}」的下载记录（最近 {len(shown)} 条）：\n\n"
            + "\n".join(shown)
        )

    lines = get_history_lines(n)
    if not lines:
        return "📜 下载记录：暂无记录"

    # Telegram 单条消息上限 4096 字符，从最新往前凑到约 3800
    shown = []
    total = 0
    for line in reversed(lines):
        if total + len(line) + 1 > 3800:
            break
        shown.append(line)
        total += len(line) + 1
    shown.reverse()

    if not shown:
        shown = [lines[-1][:3800]]

    return f"📜 最近 {len(shown)} 条下载记录：\n\n" + "\n".join(shown)


def wl_list_text(chats=None, scan_info=None, last_scan=None):
    """生成白名单列表文本（命令与 bot 菜单共用）。

    scan_info：{chat_id: (checkpoint|None, 待执行任务数)}，来自
    wl_scan.collect_scan_info()；last_scan：state.WL_LAST_SCAN 快照。
    两者缺省时省略对应行（旧调用与测试兼容）。保持「📋 下载白名单」字面
    前缀不变（/wl 回复靠前缀自动清理）。
    """
    chats = state.WHITELIST_CHATS if chats is None else chats
    if not chats:
        return (
            "📋 下载白名单：空\n\n"
            "机制：白名单 chat 的媒体记为转发任务，由常驻 Worker 转发进"
            "收藏夹下载（副本保留）。\n"
            "用法：/wl add <ID或@用户名>，或回复一条从目标 chat "
            "转发的消息后发送 /wl add"
        )
    lines = []
    for i, (cid, title) in enumerate(sorted(chats.items()), start=1):
        lines.append(f"{i}. {title} ({cid})")
        info = (scan_info or {}).get(cid)
        if info:
            ckpt, pending = info
            state_parts = [
                f"已扫至 #{ckpt}" if ckpt else "未扫描",
                f"待执行 {pending} 条",
            ]
            lines.append("   " + " | ".join(state_parts))
    head = ("📋 下载白名单：媒体记为转发任务，由常驻 Worker 转发进收藏夹"
            "下载（副本保留）")
    if last_scan:
        head += (f"\n上轮扫描：{last_scan.get('ts', '-')} 检查 "
                 f"{last_scan.get('scanned', 0)} 条 / 新建 "
                 f"{last_scan.get('created', 0)} 条")
    return (
        head + "\n\n" + "\n".join(lines) + "\n\n"
        "回补停机漏掉的存量：/wl since <序号|@用户名|ID> <消息id>\n"
        "立即扫描一轮：/wl scan"
    )


SQL_TEXT_PREFIX = "📋 SQL"


def _sql_cell(value, limit):
    """单元格展示形态：None→∅，超长截断带省略号。"""
    if value is None:
        return "∅"
    s = str(value)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def format_sql_result(result):
    """/sql 结果渲染：查询 → 对齐文本表；写 → 影响行数；错误 → 原文。

    纯函数（runtime_db.execute_user_sql 的结果 dict → Telegram 文本）。
    单元格截到 SQL_CONSOLE_CELL_LIMIT、宽表砍到 8 列、总长压到 3900 内
    （Telegram 4096 上限留余量）。
    """
    if result.get("kind") == "error":
        return f"{SQL_TEXT_PREFIX}\n❌ {result.get('message', '执行失败')}"
    if result.get("kind") == "done":
        n = int(result.get("rowcount", -1))
        return (f"{SQL_TEXT_PREFIX}\n✅ 已执行（影响 {n} 行）" if n >= 0
                else f"{SQL_TEXT_PREFIX}\n✅ 已执行")
    columns = list(result.get("columns") or [])
    rows = [list(r) for r in (result.get("rows") or [])]
    if not columns:
        return f"{SQL_TEXT_PREFIX}\n（无结果集）"
    if len(columns) > 8:
        columns = columns[:8] + ["…"]
        rows = [r[:8] + ["…"] for r in rows]
    limit = int(getattr(config, "SQL_CONSOLE_CELL_LIMIT", 48))
    rows = [[_sql_cell(v, limit) for v in r] for r in rows]
    widths = [len(str(c)) for c in columns]
    for r in rows:
        for i, v in enumerate(r):
            widths[i] = min(max(widths[i], len(str(v))), 24)
    lines = ["  ".join(str(c).ljust(widths[i])
                       for i, c in enumerate(columns))]
    lines.append("  ".join("-" * w for w in widths))
    for r in rows:
        lines.append("  ".join(str(v).ljust(widths[i])
                               for i, v in enumerate(r)))
    more = "（还有更多行，请用 LIMIT 收窄）" if result.get("more") else ""
    out = (f"{SQL_TEXT_PREFIX}\n" + "\n".join(lines)
           + f"\n共 {len(rows)} 行{more}")
    if len(out) > 3900:
        out = out[:3900] + "\n…（超长截断）"
    return out
