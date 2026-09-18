"""bot 按钮菜单：回调数据编解码与各视图按钮构造（纯函数）。

encode_menu_data 生成 m:<action>[:<arg>]（≤64 字节），parse_menu_data 逆解析。
build_main_menu_text 为菜单头部；各 *_menu_buttons 依运行态 state.QUEUE /
state.WHITELIST_CHATS 在**调用时**读取（菜单每次展示都取最新状态）。
Button 为 Telethon 类型（telethon.Button），import 期无副作用。
"""
import os

from telethon import Button

from . import state
from . import config
from . import cmd_templates
from . import shell
from . import upload
from .config import DOWNLOAD_CONCURRENCY_MAX, LOG_RETENTION_DAYS, MENU_ACTIONS


def encode_menu_data(action, arg=None):
    """把菜单动作编码成回调数据（Telegram 限制 ≤64 字节）。"""
    data = f"m:{action}"
    if arg is not None:
        data += f":{arg}"
    return data.encode("utf-8")


def parse_menu_data(data):
    """解析回调数据，返回 (动作, 参数)；无法识别返回 ("unknown", None)。

    arg 用 **split(":", 2)**（最多切两刀）：mlink_open 的参数可能是带
    // 的完整 URL——全切会把 https:// 劈成 4 段误判 unknown（2026-09-18
    实测：旧消息上的 🌐 按钮点了没反应即此因）。"""
    try:
        parts = data.decode("utf-8").split(":", 2)
        if len(parts) < 2 or parts[0] != "m":
            return ("unknown", None)
        action = parts[1]
        arg = parts[2] if len(parts) == 3 else None
        if action not in MENU_ACTIONS:
            return ("unknown", None)
        return (action, arg)
    except Exception:
        return ("unknown", None)


def chrome_agent_load_tasks():
    """Chrome Agent 的任务列表（menu 总览用；读失败返回空）。"""
    from . import chrome_agent, config
    try:
        return chrome_agent.load_tasks(config.CHROME_TASKS_FILE)
    except Exception:
        return []


def build_main_menu_text():
    """主菜单文本；头部拼一行实时状态，打开菜单即所见、0 次额外点击。"""
    from .stats import collect_stats  # 函数内引用：查台账要读日志/历史文件
    from .naming import format_size

    in_flight = len(state.ACTIVE_DOWNLOADS) if state.ACTIVE_DOWNLOADS else 0
    pending = len(state.QUEUE.get("tasks", [])) if state.QUEUE else 0
    to_retry = len(state.QUEUE.get("retry", [])) if state.QUEUE else 0
    today = collect_stats(1)

    lines = ["🤖菜单", ""]
    has_any = bool(in_flight or pending or to_retry or today["success_count"])
    if has_any:
        lines.append(
            f"⏳ 在途 {in_flight} | 📥 待处理 {pending} | 🔁 待重试 {to_retry}"
        )
        lines.append(
            f"✅ 今日 {today['success_count']} 个 / "
            f"{format_size(today['success_bytes'])}"
        )
    # 系统级总览（2026-09-18 UX Round1 P0-1）：普通下载空闲时，其它子系统
    # 在工作也要看得见。数据全部来自既有可靠统计；某子系统不可用就跳过，
    # 绝不编数字。
    sys_lines = []
    try:
        from . import runtime_db
        counts = runtime_db.pawchive_status_counts()
        paw_proc = counts.get(runtime_db.PAW_POST_PROCESSING, 0)
        paw_pend = counts.get(runtime_db.PAW_POST_PENDING, 0)
        if paw_proc or paw_pend:
            sys_lines.append(f"🐾 Pawchive：处理 {paw_proc} / 待 {paw_pend}")
        pend = runtime_db.count_pending_listener_tasks()
        if pend:
            sys_lines.append(f"📡 标签监听：待处理 {pend}")
    except Exception:
        pass                      # 总览失败绝不挡菜单
    try:
        from . import chrome_client
        tasks = chrome_agent_load_tasks()
        active = [t for t in tasks
                  if t.get("status") not in ("SUCCESS", "FAILED", "CANCELLED")]
        if active:
            sys_lines.append(f"🌐 Chrome：进行中 {len(active)}")
    except Exception:
        pass
    if sys_lines:
        lines.extend(sys_lines)
    if not (has_any or sys_lines):
        lines.append("✅ 空闲：暂无在途与排队任务")
    lines += [
        "",
        "点击按钮操作，结果会更新在这条消息里。",
        "【我的收藏】 里的命令照常可用。",
    ]
    return "\n".join(lines)


def main_menu_buttons():
    """主菜单：按使用频率分区（2026-09-17 用户要求重排）。

      监控   → 状态 / 台账 / 进度 / 记录（看数据，最常用，占前两行）
      管理   → 队列 / 待重试 / 并发 / 白名单（管任务与来源）
      工具   → 查询 / 监听 / 去重 / Cookie（低频配置与排查）
      子系统 → Pawchive / CD2 / Chrome / 命令行（独立功能入口）

    📡 标签监听是**独立于下载白名单**的入口（规格书 §18：不能塞进
    📋 白名单，用户必须能明显区分两套系统）——放进工具区独立成对。
    CD2 的启停/备份记录在其子菜单（2026-09-15 菜单合并）。
    """
    return [
        # 监控
        [Button.inline("📊 状态", encode_menu_data("status")),
         Button.inline("📊 台账", encode_menu_data("stats"))],
        [Button.inline("📈 进度", encode_menu_data("progress")),
         Button.inline("🔍 查询", encode_menu_data("find"))],
        # 管理
        [Button.inline("📥 队列", encode_menu_data("queue")),
         Button.inline("🔁 待重试", encode_menu_data("retry"))],
        [Button.inline("🧵 并发", encode_menu_data("thread")),
         Button.inline("📜 记录", encode_menu_data("done"))],
        [Button.inline("📋 白名单", encode_menu_data("wl")),
         Button.inline("📡 监听", encode_menu_data("listen"))],
        # 工具
        [Button.inline("🛡 去重", encode_menu_data("dedup")),
         Button.inline("🍪 Cookie", encode_menu_data("cookie"))],
        [Button.inline("🧹 Caption", encode_menu_data("capf")),
         Button.inline("📐 SQL模板", encode_menu_data("sqlt"))],
        # 子系统
        [Button.inline("🐾 Pawchive", encode_menu_data("paw")),
         Button.inline("☁️ CD2", encode_menu_data("cd2_menu"))],
        [Button.inline("🌐 Chrome", encode_menu_data("chrome_tasks")),
         Button.inline("🖥 命令行", encode_menu_data("tools"))],
    ]


def cd2_menu_text():
    """☁️ CD2 子菜单正文。"""
    return (
        "☁️ CD2 云盘\n\n"
        "启动后媒体经 CloudDrive2 自动备份到 115；"
        "备份记录展示最近 7 天的搬运日志。")


def cd2_menu_buttons():
    return [
        [Button.inline("▶️ 启动 / 查状态", encode_menu_data("cd2")),
         Button.inline("🛑 停止", encode_menu_data("cd2_stop"))],
        [Button.inline("🗂 备份记录", encode_menu_data("bak"))],
        [Button.inline("🔙 返回主菜单", encode_menu_data("home"))],
    ]


def tools_view_text():
    """🖥 命令行/上传 合并工具箱正文（sh 与 up 两段拼接；up_view_text 同模块）。"""
    return shell.sh_view_text() + "\n\n──────\n\n" + up_view_text()


def tools_menu_buttons(candidates):
    """🖥 命令行/上传 合并视图：预设命令 + 自定义 + 上传文件 + 输入路径。

    candidates 与 up_menu_buttons 同约定（快照进 state.UP_CANDIDATES，按钮
    只带序号）。
    """
    entries = [(label, encode_menu_data("sh_run", cmd))
               for cmd, label in shell.PRESET_COMMANDS.items()]
    entries += [(f"▶️ {name}", encode_menu_data("cmdt_run", name))
                for name in cmd_templates.names()]
    rows = [[Button.inline(text, data) for text, data in entries[i:i + 2]]
            for i in range(0, len(entries), 2)]
    rows.append(
        [Button.inline("✏️ 自定义命令", encode_menu_data("sh_input"))])
    for i, path in enumerate(candidates):
        rows.append([Button.inline(
            f"📤 {os.path.basename(path)}",
            encode_menu_data("up_file", str(i)))])
    rows.append([
        Button.inline("✏️ 输入上传路径", encode_menu_data("up_input")),
        Button.inline("🔄 刷新", encode_menu_data("tools")),
    ])
    rows.append([
        Button.inline("🔗 外链台账", encode_menu_data("mlink_view")),
        Button.inline("📜 命令模板", encode_menu_data("cmdt")),
    ])
    rows.append([Button.inline("🔙 返回主菜单", encode_menu_data("home"))])
    return rows


def chrome_menu_buttons(tasks):
    """Chrome 任务视图按钮：每条可取消任务一个 🛑 按钮 + 刷新 + 返回。

    🛑 **直接带 task_id**（不是序号）：按钮是一次点击，不存在「看到列表之后
    列表又变了」的漂移，用户也不用去数序号——这正是 /chrome_cancel 序号形态
    的已知坑，菜单路径天然绕开它。标签里带短 ID 与文件名尾段，好认人。
    """
    rows = []
    for task in tasks:
        tid = str(task.get("task_id") or "")
        icon = {"RUNNING": "🟢", "PENDING": "⏳",
                "RETRY_WAIT": "⏳"}.get(task.get("status"), "•")
        rows.append([Button.inline(
            f"🛑 {icon} {tid[:8]} {chrome_task_short_name(task)}",
            encode_menu_data("chrome_cancel", tid))])
    rows.append([
        Button.inline("▶️ 启动 Agent", encode_menu_data("chrome_start")),
        Button.inline("⏹ 停止 Agent", encode_menu_data("chrome_stop")),
    ])
    rows.append([
        Button.inline("🔄 刷新", encode_menu_data("chrome_tasks")),
        Button.inline("ℹ️ Agent 状态", encode_menu_data("chrome_status")),
    ])
    rows.append([Button.inline("🏠 返回主菜单", encode_menu_data("home"))])
    return rows


def chrome_task_short_name(task, limit=20):
    """任务按钮上的短名：URL 最后一段去掉 query，太长就截（纯函数）。"""
    url = str((task or {}).get("url") or "")
    tail = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or url
    return tail[:limit]


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


def listen_interval_buttons():
    """标签监听扫描周期预设（避免再加一个输入窗口模式）。"""
    presets = [(30, "30 分钟"), (60, "1 小时"), (360, "6 小时"),
               (1440, "24 小时"), (10080, "7 天")]
    rows = [[Button.inline(label, encode_menu_data("listen_interval_set",
                                                   str(minutes)))]
            for minutes, label in presets]
    rows.append([Button.inline("🔙 返回", encode_menu_data("listen"))])
    return rows


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
    rows = [
        [Button.inline("➕ 添加", encode_menu_data("wl_add")),
         Button.inline("⏪ 回补", encode_menu_data("wl_since"))],
        [Button.inline("🔄 立即扫描", encode_menu_data("wl_scan"))],
    ]
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


def sh_ls_buttons(dirs, up_token=None, home_token=None):
    """ls 浏览器的目录按钮网格：2 个/行 + 可选导航行（⬆️ 上一级/🏠 根目录）。

    dirs = [(显示名, hash8 回调键)]；回调数据 ≤64 字节由 hash8 保证。"""
    rows = []
    for i in range(0, len(dirs), 2):
        row = []
        for name, token in dirs[i:i + 2]:
            label = "📁 " + (name if len(name) <= 26 else name[:25] + "…")
            row.append(Button.inline(label, encode_menu_data("sh_ls", token)))
        rows.append(row)
    if up_token or home_token:
        nav = []
        if up_token:
            nav.append(Button.inline("⬆️ 上一级",
                                     encode_menu_data("sh_ls", up_token)))
        if home_token:
            nav.append(Button.inline("🏠 根目录",
                                     encode_menu_data("sh_ls", home_token)))
        rows.append(nav)
    return rows


def sh_menu_buttons():
    """🖥 命令行视图：预设命令与命令模板**同级**按钮网格 + 自定义输入。

    命令模板按钮化（2026-09-15 用户要求）：新增模板后自动出现在本网格，
    点了直接执行（cmdt_run 按名字走 /sh 全套纪律）。
    """
    entries = [(label, encode_menu_data("sh_run", cmd))
               for cmd, label in shell.PRESET_COMMANDS.items()]
    entries += [(f"▶️ {name}", encode_menu_data("cmdt_run", name))
                for name in cmd_templates.names()]
    rows = [[Button.inline(text, data) for text, data in entries[i:i + 2]]
            for i in range(0, len(entries), 2)]
    rows.append(
        [Button.inline("✏️ 自定义命令", encode_menu_data("sh_input"))])
    rows.append([
        Button.inline("📜 管理模板", encode_menu_data("cmdt")),
        Button.inline("🔙 返回主菜单", encode_menu_data("home")),
    ])
    return rows


def _template_buttons():
    """命令模板 → ▶️ 执行按钮（每个模板一枚；无模板返回空）。"""
    return [Button.inline(
        f"▶️ {name}", encode_menu_data("cmdt_run", name))
        for name in cmd_templates.names()]


def up_menu_buttons(candidates):
    """⬆️ 上传视图：文件按钮只带序号（candidates 已由调用方快照进
    state.UP_CANDIDATES），路径放不进 64 字节回调数据。纯函数。"""
    rows = []
    for i, path in enumerate(candidates):
        rows.append([Button.inline(
            f"📄 {os.path.basename(path)}",
            encode_menu_data("up_file", str(i)))])
    rows.append([Button.inline("✏️ 输入路径", encode_menu_data("up_input"))])
    rows.append([
        Button.inline("🔄 刷新", encode_menu_data("up")),
        Button.inline("🔙 返回主菜单", encode_menu_data("home")),
    ])
    return rows


def up_view_text():
    """⬆️ 上传视图正文：从哪找文件 + 当前目录。"""
    return (
        "⬆️ 上传文件到收藏夹\n\n"
        f"📂 当前目录：\n{state.SHELL_CWD}\n\n"
        "点文件直接上传；或 ✏️ 输入路径（相对路径基于当前目录，"
        "支持 ~，带空格不必加引号）。"
    )
