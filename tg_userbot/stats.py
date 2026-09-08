"""台账/对账：查询时解析 download.log + download_history.txt 汇总统计。

设计：**零新增持久化** —— 事件与时间戳都已在 download.log（按天轮转、保留
LOG_RETENTION_DAYS 天）与 download_history.txt（永久）里，查询时按自然日
窗口过滤计数即可。窗口上限 = 日志保留天数，更早的日子无数据可查。

口径：
- 收到媒体        = 日志 `📦 检测到可下载媒体`（每条入队媒体一行）；
  收藏/中转拆分 = `📨 …收到消息` 行里 media 非 None/网页预览 的行
  （近似：以 📦 总数为准，拆分供定位）。
- 解析            = 本地 `🛠 …已本地解析并入队下载` / bot 中转 `…发给解析 bot`。
- 下载成功        = history 行（时间 | 类型 | 文件名 | 大小 | 来源），合计字节。
- 失败            = `失败，尝试第`（重试过程）/ `已达到最大重试次数`（最终）。
- 移除            = `手动移除队列任务`（/queue del 与菜单删除，queue_del_task 落
  日志）+ `队列任务原消息已被删除`（来源消息被删，任务终结移除）。
- 去重跳过        = `⏭️ 重复媒体跳过入队`（收了但不入队）。
- 在途/待处理/待重试 = state.ACTIVE_DOWNLOADS / state.QUEUE（实时快照）。

勾稽恒等式：收到 = 成功 + 待重试 + 移除 + 去重 + 在途/待处理。在途/待处理/
待重试是实时快照（含往日任务），成功/收到按自然日窗口 —— 跨日往来时恒等式
天然有出入，回复里把差额亮出来（差 ±N）而不是硬凑相等。

解析全部用宽松子串匹配：日志措辞调整只会少计、不会抛错。
"""
import os
import re
from datetime import date, timedelta

from . import state
from .config import DOWNLOAD_HISTORY_FILE, LOG_FILE, LOG_RETENTION_DAYS

# 与 format_queue_text / /stats 回复同一套前缀，进自动清理白名单
STATS_TEXT_PREFIX = "📊 台账"

_MEDIA_RE = re.compile(r"media=(?!None\b|MessageMediaWebPage)\S+")
_SIZE_RE = re.compile(r"([\d.]+)\s*(B|KB|MB|GB|TB|PB)", re.IGNORECASE)
_UNIT_MULT = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3,
              "TB": 1024**4, "PB": 1024**5}


def parse_size(text):
    """把 format_size 的输出（如 "1.35 GB"）反解成字节数；解析不了返回 0。"""
    m = _SIZE_RE.search(text or "")
    if not m:
        return 0
    return int(float(m.group(1)) * _UNIT_MULT[m.group(2).upper()])


def _window_dates(days, today):
    """窗口内的自然日列表：today 起往前的 days 天（含 today）。"""
    return [today - timedelta(offset) for offset in range(days - 1, -1, -1)]


def _log_files_for(dates, log_path):
    """窗口涉及日志文件：当前文件 + 各历史日的轮转文件（存在的才收）。"""
    files = [log_path]
    for d in dates:
        rotated = f"{log_path}.{d.isoformat()}"
        if os.path.exists(rotated):
            files.append(rotated)
    return files


def _window_log_lines(dates, log_path):
    """拼出窗口内所有日志行（当前文件 + 轮转文件），再按日期前缀过滤。

    当天轮转文件还没生成时当前文件即含全部；轮转文件本身只含单日，
    统一再过滤一次，语义一致。
    """
    prefixes = {d.isoformat() for d in dates}
    lines = []
    for path in _log_files_for(dates, log_path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line[:10] in prefixes:
                        lines.append(line)
        except OSError:
            continue
    return lines


def _count(lines, needle):
    return sum(1 for line in lines if needle in line)


def collect_stats(days=1, today=None, log_path=None, history_path=None):
    """汇总窗口内的台账指标；纯函数，路径/基准日可注入（测试用）。"""
    today = today or date.today()
    days = max(1, min(days, LOG_RETENTION_DAYS))
    log_path = log_path or LOG_FILE
    history_path = history_path or DOWNLOAD_HISTORY_FILE
    dates = _window_dates(days, today)

    lines = _window_log_lines(dates, log_path)
    media_me = media_wl = 0
    for line in lines:
        if "📨 " not in line or "收到消息" not in line:
            continue
        if not _MEDIA_RE.search(line):
            continue
        if "Saved Messages 收到消息" in line:
            media_me += 1
        elif "白名单 chat" in line:
            media_wl += 1

    success_count = 0
    success_bytes = 0
    prefixes = {d.isoformat() for d in dates}
    try:
        with open(history_path, "r", encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                if line[:10] in prefixes and " | " in line:
                    success_count += 1
                    parts = line.split(" | ")
                    success_bytes += parse_size(parts[3]) if len(parts) > 3 \
                        else 0
    except OSError:
        pass

    return {
        "media_total": _count(lines, "📦 检测到可下载媒体"),
        "media_me": media_me,
        "media_wl": media_wl,
        "parse_local": _count(lines, "已本地解析并入队下载"),
        "parse_relay": _count(lines, "发给解析 bot"),
        "success_count": success_count,
        "success_bytes": success_bytes,
        "fail_attempts": _count(lines, "失败，尝试第"),
        "fail_final": _count(lines, "已达到最大重试次数"),
        # 勾稽出口桶：收到媒体除「成功/待重试」外的下落，凑齐恒等式
        "manual_del": _count(lines, "手动移除队列任务"),
        "msg_deleted": _count(lines, "队列任务原消息已被删除"),
        "dedup_skip": _count(lines, "重复媒体跳过入队"),
    }


def _header(days, today):
    dates = _window_dates(days, today)
    if len(dates) == 1:
        return f"{STATS_TEXT_PREFIX} | 今日（{dates[0].strftime('%m-%d')}）"
    return (
        f"{STATS_TEXT_PREFIX} | 最近 {len(dates)} 天"
        f"（{dates[0].strftime('%m-%d')} ~ {dates[-1].strftime('%m-%d')}）"
    )


def stats_text(days=1, today=None, log_path=None, history_path=None):
    """台账回复文本（命令与菜单按钮共用）。"""
    days = max(1, min(days, LOG_RETENTION_DAYS))
    s = collect_stats(days, today=today, log_path=log_path,
                      history_path=history_path)
    from .naming import format_size  # 函数内引用：naming 是叶子，无环

    in_flight = len(state.ACTIVE_DOWNLOADS)
    pending = len(state.QUEUE.get("tasks", [])) if state.QUEUE else 0
    to_retry = len(state.QUEUE.get("retry", [])) if state.QUEUE else 0

    # 勾稽恒等式：收到 = 成功 + 待重试 + 移除 + 去重 + 在途/待处理。
    # 待重试/在途/待处理是实时快照（含往日任务），跨日窗口下天然有出入，
    # 对不上时把差额亮出来而不是硬凑相等。
    removed = s["manual_del"] + s["msg_deleted"]
    live = in_flight + pending
    total = (s["success_count"] + to_retry + removed + s["dedup_skip"] + live)
    if total == s["media_total"]:
        tail = " ✓"
    else:
        tail = (
            f"，差 {s['media_total'] - total:+d}"
            "（跨日任务/历史删除/非媒体成功）"
        )

    return "\n".join([
        _header(days, today or date.today()),
        "",
        f"📥 收到媒体：{s['media_total']} 条"
        f"（收藏 {s['media_me']} / 中转 {s['media_wl']}）",
        f"🛠 抖音解析：本地 {s['parse_local']} + bot 中转 {s['parse_relay']}",
        f"✅ 下载成功：{s['success_count']} 个，共 {format_size(s['success_bytes'])}",
        f"🗑 移除：手动 {s['manual_del']} + 原消息删除 {s['msg_deleted']}",
        f"⏭️ 去重跳过：{s['dedup_skip']} 条",
        f"❌ 失败：重试 {s['fail_attempts']} 次，"
        f"最终失败 {s['fail_final']} 次（同一文件多次重跑失败会重复计次）",
        f"⏳ 在途 {in_flight} | 待处理 {pending} | 待重试 {to_retry}",
        f"🧮 勾稽：成功 {s['success_count']} + 待重试 {to_retry} + 移除 {removed}"
        f" + 去重 {s['dedup_skip']} + 在途/待处理 {live}"
        f" = 收到 {s['media_total']}{tail}",
    ])


def is_stats_command(text):
    # /stats、/stats 3（天数在命令分支里解析）
    return bool(re.fullmatch(r"/stats(?:\s+\S+)*", (text or "").strip(),
                             re.IGNORECASE))
