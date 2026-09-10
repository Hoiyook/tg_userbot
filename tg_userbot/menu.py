"""bot 按钮菜单：回调数据编解码与各视图按钮构造（纯函数）。

encode_menu_data 生成 m:<action>[:<arg>]（≤64 字节），parse_menu_data 逆解析。
build_main_menu_text 为菜单头部；各 *_menu_buttons 依运行态 state.QUEUE /
state.WHITELIST_CHATS 在**调用时**读取（菜单每次展示都取最新状态）。
Button 为 Telethon 类型（telethon.Button），import 期无副作用。
"""
from telethon import Button

from . import state
from . import config
from .config import DOWNLOAD_CONCURRENCY_MAX, LOG_RETENTION_DAYS, MENU_ACTIONS


def encode_menu_data(action, arg=None):
    """把菜单动作编码成回调数据（Telegram 限制 ≤64 字节）。"""
    data = f"m:{action}"
    if arg is not None:
        data += f":{arg}"
    return data.encode("utf-8")


def parse_menu_data(data):
    """解析回调数据，返回 (动作, 参数)；无法识别返回 ("unknown", None)。"""
    try:
        parts = data.decode("utf-8").split(":")
        if len(parts) < 2 or len(parts) > 3 or parts[0] != "m":
            return ("unknown", None)
        action = parts[1]
        arg = parts[2] if len(parts) == 3 else None
        if action not in MENU_ACTIONS:
            return ("unknown", None)
        return (action, arg)
    except Exception:
        return ("unknown", None)


def build_main_menu_text():
    """主菜单文本；头部拼一行实时状态，打开菜单即所见、0 次额外点击。"""
    from .stats import collect_stats  # 函数内引用：查台账要读日志/历史文件
    from .naming import format_size

    in_flight = len(state.ACTIVE_DOWNLOADS) if state.ACTIVE_DOWNLOADS else 0
    pending = len(state.QUEUE.get("tasks", [])) if state.QUEUE else 0
    to_retry = len(state.QUEUE.get("retry", [])) if state.QUEUE else 0
    today = collect_stats(1)

    lines = ["🤖菜单", ""]
    if not (in_flight or pending or to_retry or today["success_count"]):
        lines.append("✅ 空闲：暂无在途与排队任务")
    else:
        lines.append(
            f"⏳ 在途 {in_flight} | 📥 待处理 {pending} | 🔁 待重试 {to_retry}"
        )
        lines.append(
            f"✅ 今日 {today['success_count']} 个 / "
            f"{format_size(today['success_bytes'])}"
        )
    lines += [
        "",
        "点击按钮操作，结果会更新在这条消息里。",
        "【我的收藏】 里的命令照常可用。",
    ]
    return "\n".join(lines)


def main_menu_buttons():
    return [
        [Button.inline("📊 状态", encode_menu_data("status")),
         Button.inline("📈 进度", encode_menu_data("progress"))],
        [Button.inline("📜 下载记录", encode_menu_data("done")),
         Button.inline("📋 白名单", encode_menu_data("wl"))],
        [Button.inline("📥 队列", encode_menu_data("queue")),
         Button.inline("🔁 待重试", encode_menu_data("retry"))],
        [Button.inline("🧵 并发", encode_menu_data("thread")),
         Button.inline("🧹 清理", encode_menu_data("clean"))],
        [Button.inline("🛡 去重", encode_menu_data("dedup")),
         Button.inline("🍪 抖音Cookie", encode_menu_data("cookie"))],
        [Button.inline("🖥 启动CD2", encode_menu_data("cd2")),
         Button.inline("🛑 停止CD2", encode_menu_data("cd2_stop"))],
        [Button.inline("🗂 备份记录", encode_menu_data("bak")),
         Button.inline("📊 台账", encode_menu_data("stats"))],
        [Button.inline("🔍 查询", encode_menu_data("find")),
         Button.inline("🧹 Caption 清洗", encode_menu_data("capf"))],
    ]


def caption_filter_menu_buttons():
    """Caption 清洗视图：查看/添加/删除/测试/恢复默认/清空 + 返回。"""
    return [
        [Button.inline("📋 查看规则", encode_menu_data("capf"))],
        [Button.inline("➕ 添加规则", encode_menu_data("capf_add")),
         Button.inline("➖ 删除规则", encode_menu_data("capf_del"))],
        [Button.inline("🧪 测试清洗", encode_menu_data("capf_test"))],
        [Button.inline("♻️ 恢复默认", encode_menu_data("capf_reset")),
         Button.inline("🗑 清空规则", encode_menu_data("capf_clear"))],
        [Button.inline("🔙 返回主菜单", encode_menu_data("home"))],
    ]


def stats_menu_buttons(days=1):
    """台账视图的窗口切换按钮：今日 / 3 天 / 7 天，当前窗口打 ✅。"""
    def _btn(n):
        label = "今日" if n == 1 else f"{n} 天"
        mark = "✅ " if n == days else ""
        return Button.inline(
            f"{mark}{label}", encode_menu_data("stats", str(n))
        )

    return [
        [_btn(1), _btn(3), _btn(LOG_RETENTION_DAYS)],
        [Button.inline("🔙 返回主菜单", encode_menu_data("home"))],
    ]


def dedup_menu_buttons():
    """去重视图按钮：开/关切换 + 回填扫描 + 返回主菜单。"""
    from . import dedup  # 函数内导入：menu 为叶子模块，避免环

    toggle_label = "⏸ 关闭去重" if dedup.state.DEDUP_ENABLED else "▶️ 开启去重"
    return [
        [Button.inline(toggle_label, encode_menu_data("dedup_toggle"))],
        [Button.inline("🏠 返回主菜单", encode_menu_data("home"))],
    ]


def queue_menu_buttons():
    rows = []
    for i, r in enumerate(state.QUEUE["tasks"], start=1):
        label = (r.get("label") or "(无)")[:20]
        rows.append(
            [Button.inline(
                f"❌ {i} {label}", encode_menu_data("queue_del", r["id"])
            )]
        )
    rows.append([
        Button.inline("🔄 刷新", encode_menu_data("queue")),
        Button.inline("🔙 返回主菜单", encode_menu_data("home")),
    ])
    return rows


def retry_menu_buttons(page=1):
    """待重试视图：本页条目按钮 + 翻页/刷新 + 全部重放 + 返回。序号全局。"""
    from .queue import LIST_PAGE_SIZE  # 函数内导入，避免 menu → queue 环

    records = state.QUEUE.get("retry", []) if state.QUEUE else []
    total = len(records)
    total_pages = max(1, (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    page = min(max(1, page), total_pages)
    start = (page - 1) * LIST_PAGE_SIZE
    rows = []
    for i, r in enumerate(records[start:start + LIST_PAGE_SIZE],
                          start=start + 1):
        label = (r.get("label") or "(无)")[:14]
        rows.append(
            [
                Button.inline(
                    f"▶️ {i}", encode_menu_data("retry_run", r["id"])
                ),
                Button.inline(
                    f"❌ {i} {label}", encode_menu_data("retry_del", r["id"])
                ),
            ]
        )
    nav = []
    if page > 1:
        nav.append(Button.inline("◀️ 上一页",
                                 encode_menu_data("retry", str(page - 1))))
    nav.append(Button.inline(f"🔄 {page}/{total_pages}",
                             encode_menu_data("retry", str(page))))
    if page < total_pages:
        nav.append(Button.inline("▶️ 下一页",
                                 encode_menu_data("retry", str(page + 1))))
    rows.append(nav)
    rows.append([
        Button.inline("♻️ 全部重放", encode_menu_data("retry_all")),
        Button.inline("🔙 返回主菜单", encode_menu_data("home")),
    ])
    return rows


def back_home_buttons():
    return [[Button.inline("🔙 返回主菜单", encode_menu_data("home"))]]


def cookie_status_text():
    """【🍪 抖音Cookie】按钮的状态视图（调用时动态读内存值，实时）。"""
    cookie = getattr(config, "DOUYIN_COOKIE", "") or ""
    if not cookie:
        return (
            "🍪 抖音 Cookie\n\n"
            "状态：未配置\n"
            "（本地解析大概率失败，抖音链接将走解析 bot 兜底）"
        )
    sess = "含登录态 sessionid ✅" if "sessionid=" in cookie else (
        "⚠️ 未检测到 sessionid（可能非登录态）"
    )
    return (
        "🍪 抖音 Cookie\n\n"
        f"状态：已配置（{len(cookie)} 字符，{sess}）\n"
        f"片段：{config.mask_douyin_cookie(cookie)}\n"
        "来源：tg_secrets.json（更新后实时生效）"
    )


def cookie_menu_buttons():
    return [
        [Button.inline("✏️ 更新", encode_menu_data("cookie_set")),
         Button.inline("🗑 清除", encode_menu_data("cookie_clear"))],
        [Button.inline("🌐 Chrome", encode_menu_data("cookie_imp", "chrome")),
         Button.inline("🌐 Edge", encode_menu_data("cookie_imp", "edge")),
         Button.inline("🌐 Firefox", encode_menu_data("cookie_imp", "firefox"))],
        [Button.inline("🔙 返回主菜单", encode_menu_data("home"))],
    ]


def wl_menu_buttons():
    rows = [[Button.inline("➕ 添加", encode_menu_data("wl_add"))]]
    for cid, title in sorted(state.WHITELIST_CHATS.items()):
        rows.append(
            [Button.inline(f"➖ {title}", encode_menu_data("wl_del", str(cid)))]
        )
    rows.append([Button.inline("🔙 返回主菜单", encode_menu_data("home"))])
    return rows


def thread_menu_buttons():
    # 预设档位（只出不大于 DOWNLOAD_CONCURRENCY_MAX 的），每行 3 个
    presets = [p for p in (3, 5, 10, 15, 20, 25) if p <= DOWNLOAD_CONCURRENCY_MAX]
    rows = [
        [Button.inline(str(p), encode_menu_data("thread", str(p))) for p in presets[i:i + 3]]
        for i in range(0, len(presets), 3)
    ]
    rows.append([Button.inline("🔙 返回主菜单", encode_menu_data("home"))])
    return rows
