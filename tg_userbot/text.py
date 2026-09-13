"""展示文本（Saved Messages 命令与 bot 按钮菜单共用）。

status_text / progress_text / done_reply_text / wl_list_text 都是同步函数，
被 commands.py（handle_command）与 bot.py（handle_menu_action）复用。
运行态一律读 state.*（client / MY_ID / ACTIVE_DOWNLOADS / WHITELIST_CHATS）；
format_size 来自 naming、get_history_lines 来自 history（均为纯叶子）。
"""
from . import state
from .config import LOG_FILE, SAVE_FOLDER
from .history import get_history_lines
from .naming import format_size


def status_text():
    """生成 /status 回复文本。"""
    try:
        connected = bool(state.client and state.client.is_connected())
    except Exception:
        connected = False
    return (
        "🟢 TG Userbot 状态正常\n\n"
        f"连接：{'正常' if connected else '断开'}\n"
        f"用户 ID：{state.MY_ID}\n"
        f"保存目录：{SAVE_FOLDER}\n"
        f"日志：{LOG_FILE}"
    )


def progress_text():
    """生成 /progress 回复文本。"""
    if not state.ACTIVE_DOWNLOADS:
        return "📊 当前没有进行中的下载"
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
    return "📊 当前下载进度：\n\n" + "\n".join(lines)


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
