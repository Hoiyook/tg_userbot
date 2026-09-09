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
- 去重跳过        = `⏭️ 重复媒体跳过入队`（收了但不入队）+ `⏭️ 内容重复已
  拦截落盘`（下载后落盘前内容级判重命中，无成功行也不转 retry）。
- 在途/待处理/待重试 = state.ACTIVE_DOWNLOADS / state.QUEUE（实时快照）。

勾稽恒等式：收到 = 成功 + 待重试 + 移除 + 去重 + 在途/待处理。在途/待处理/
待重试是实时快照（含往日任务），成功/收到按自然日窗口 —— 跨日往来时恒等式
天然有出入，回复里把差额亮出来（差 ±N）而不是硬凑相等。

解析全部用宽松子串匹配：日志措辞调整只会少计、不会抛错。
"""
import os
import re
import json
from datetime import date, datetime, timedelta

from . import state
from .config import (
    DOWNLOAD_HISTORY_FILE,
    LOG_FILE,
    LOG_RETENTION_DAYS,
    TASK_EVENTS_FILE,
    TASK_EVENTS_MAX_EVENTS,
)
from .log import logger

# 与 format_queue_text / /stats 回复同一套前缀，进自动清理白名单
STATS_TEXT_PREFIX = "📊 台账"

_MEDIA_RE = re.compile(r"media=(?!None\b|MessageMediaWebPage)\S+")
_SIZE_RE = re.compile(r"([\d.]+)\s*(B|KB|MB|GB|TB|PB)", re.IGNORECASE)
_UNIT_MULT = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3,
              "TB": 1024**4, "PB": 1024**5}


# ------------------------------------------------------------
# 任务生命周期事件层（task_events.jsonl）：一个逻辑下载任务围绕同一个
# task_id（= 队列记录 id）记 RECEIVED→QUEUED→RUNNING→(RETRY→RUNNING)*→
# SUCCESS / FAILED / CANCELLED / REMOVED / DEDUP_HIT 的完整轨迹；DEDUP_
# SKIPPED 是「收到但未产生任务」。台账历史窗口优先按它重建，不再靠日志
# 关键词勉强推断。
# ------------------------------------------------------------

def emit_event(ev, task_id=None, label=None, **extra):
    """追加一行任务事件（JSONL append-only；失败仅告警绝不影响下载）。

    lockless 同步单行 append（与 history.append_history 同一纪律——单线程
    事件循环内单行短写实际原子）。label 里的制表/换行压成空格防拆行。
    """
    try:
        rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "ev": ev}
        if task_id:
            rec["id"] = task_id
        if label:
            rec["label"] = str(label).replace("\t", " ").replace("\n", " ")[:80]
        for key, value in extra.items():
            if value is not None:
                rec[key] = value
        with open(TASK_EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"写任务事件失败（不影响下载）：{e}")


def load_events(path=None):
    """读取全部任务事件（坏行跳过、文件缺失返回空列表）。"""
    path = path or TASK_EVENTS_FILE
    events = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("ev"):
                    events.append(rec)
    except OSError:
        pass
    return events


def trim_event_file(path=None, max_events=None):
    """启动裁剪：超上限保尾部、原子重写一次（与 dedup 索引同款）。"""
    path = path or TASK_EVENTS_FILE
    max_events = max_events or TASK_EVENTS_MAX_EVENTS
    events = load_events(path)
    if len(events) <= max_events:
        return
    kept = events[-max_events:]
    try:
        temp_path = path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            for rec in kept:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(temp_path, path)
        logger.info(f"任务事件日志超上限，已裁剪保留最近 {len(kept)} 条")
    except Exception as e:
        logger.warning(f"裁剪任务事件日志失败（下次启动再试）：{e}")


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
        "dedup_skip": (_count(lines, "重复媒体跳过入队")
                       + _count(lines, "内容重复已拦截")),
    }


# ------------------------------------------------------------
# 任务级重建：按 task_id 归类事件流，窗口统计严格可解释（取代关键词计数）。
# ------------------------------------------------------------

# 终态事件；任务集成员按「窗口内最后一个终态」归类，无终态 = 进行中
_TERMINAL_EVENTS = ("SUCCESS", "FAILED", "REMOVED", "CANCELLED", "DEDUP_HIT")


def rebuild_stats(events, days=1, today=None):
    """从事件流重建窗口统计；纯函数（events/日期可注入，测试用）。

    任务集 = 窗口内有任何带 id 事件的 task（retry 重跑/跨日不产生新任务，
    一个逻辑任务只出现一次）；终态看窗口内最后一个终态事件——所以「跨日
    任务」今天的窗口看到的是它今天的终态，昨日窗口看到昨日终态，各自成立。
    bytes 只累计窗口内 SUCCESS 事件的精确字节数（时间归属明确）。
    """
    today = today or date.today()
    dates = _window_dates(days, today)
    prefixes = {d.isoformat() for d in dates}

    def in_window(rec):
        return str(rec.get("ts", ""))[:10] in prefixes

    window = [e for e in events if in_window(e)]
    s = {
        "received": sum(1 for e in window if e.get("ev") == "RECEIVED"),
        "dedup_skipped": sum(1 for e in window
                             if e.get("ev") == "DEDUP_SKIPPED"),
        "queued_new": sum(1 for e in window if e.get("ev") == "QUEUED"),
        "queued_media": sum(1 for e in window if e.get("ev") == "QUEUED"
                            and e.get("kind") == "media"),
        "retries": sum(1 for e in window if e.get("ev") == "RETRY"),
        "received_me": sum(1 for e in window if e.get("ev") == "RECEIVED"
                           and e.get("src") == "me"),
        "received_wl": sum(1 for e in window if e.get("ev") == "RECEIVED"
                           and e.get("src") == "wl"),
    }

    # 按任务聚合（全量事件流），只收窗口内出现过的任务
    by_task = {}
    for e in events:
        tid = e.get("id")
        if tid:
            by_task.setdefault(tid, []).append(e)

    counts = {"success": 0, "failed_final": 0, "removed": 0,
              "removed_manual": 0, "removed_source": 0, "cancelled": 0,
              "dedup_hit": 0, "active": 0}
    task_total = 0
    for tid, evs in by_task.items():
        if not any(in_window(e) for e in evs):
            continue
        task_total += 1
        window_terminals = [e for e in evs
                            if in_window(e) and e.get("ev") in _TERMINAL_EVENTS]
        if not window_terminals:
            counts["active"] += 1
            continue
        last = window_terminals[-1]
        ev = last.get("ev")
        if ev == "SUCCESS":
            counts["success"] += 1
        elif ev == "FAILED":
            counts["failed_final"] += 1
        elif ev == "DEDUP_HIT":
            counts["dedup_hit"] += 1
        else:  # REMOVED / CANCELLED → 移除桶（取消也算移除的下落）
            counts["removed"] += 1
            if ev == "CANCELLED":
                counts["cancelled"] += 1
            elif last.get("why") == "source_deleted":
                counts["removed_source"] += 1
            else:
                counts["removed_manual"] += 1
    s.update(counts)
    s["task_total"] = task_total
    s["success_bytes"] = sum(int(e.get("bytes") or 0) for e in window
                             if e.get("ev") == "SUCCESS")
    return s


def _event_text(events, days, today, log_path, history_path):
    """事件模式渲染：按用户约定的分节格式输出。"""
    from .naming import format_size  # 函数内引用：naming 是叶子，无环

    s = rebuild_stats(events, days=days, today=today)
    dates = _window_dates(days, today)
    prefixes = {d.isoformat() for d in dates}

    # 解析统计事件流里没有，沿用日志关键词（仅作参考行）
    lines = _window_log_lines(dates, log_path)
    parse_local = _count(lines, "已本地解析并入队下载")
    parse_relay = _count(lines, "发给解析 bot")

    in_flight = len(state.ACTIVE_DOWNLOADS)
    pending = len(state.QUEUE.get("tasks", [])) if state.QUEUE else 0
    to_retry = len(state.QUEUE.get("retry", [])) if state.QUEUE else 0

    total = s["task_total"]
    balanced = (total == s["success"] + s["failed_final"] + s["removed"]
                + s["dedup_hit"] + s["active"])
    tail = " ✓" if balanced else "（分区异常，请查事件日志）"

    return "\n".join([
        _header(days, today),
        "",
        "📥 输入事件",
        f"收到媒体：{s['received']} 条"
        f"（收藏 {s['received_me']} / 中转 {s['received_wl']}）",
        f"去重跳过：{s['dedup_skipped']} 条",
        f"🛠 抖音解析：本地 {parse_local} + bot 中转 {parse_relay}",
        "",
        "📦 下载任务",
        f"新建任务：{s['queued_new']}"
        f"（媒体 {s['queued_media']} / 链接 {s['queued_new'] - s['queued_media']}）",
        f"成功任务：{s['success']}",
        f"最终失败任务：{s['failed_final']}",
        f"移除任务：{s['removed']}"
        f"（手动 {s['removed_manual']} / 原消息删除 {s['removed_source']}"
        f" / 取消 {s['cancelled']}）",
        f"内容拦截任务：{s['dedup_hit']}",
        "",
        "🔄 执行情况",
        f"重试次数：{s['retries']}",
        "",
        "⏳ 当前存量",
        f"下载中 {in_flight} | 待处理 {pending} | 待重试 {to_retry}",
        "",
        "💾 成功容量",
        f"精确 {s['success_bytes']} bytes（≈ {format_size(s['success_bytes'])}）",
        "",
        f"🧮 对账：窗口任务 {total} = 成功 {s['success']}"
        f" + 最终失败 {s['failed_final']} + 移除 {s['removed']}"
        f" + 拦截 {s['dedup_hit']} + 进行中 {s['active']}{tail}",
    ])


def _header(days, today):
    dates = _window_dates(days, today)
    if len(dates) == 1:
        return f"{STATS_TEXT_PREFIX} | 今日（{dates[0].strftime('%m-%d')}）"
    return (
        f"{STATS_TEXT_PREFIX} | 最近 {len(dates)} 天"
        f"（{dates[0].strftime('%m-%d')} ~ {dates[-1].strftime('%m-%d')}）"
    )


def stats_text(days=1, today=None, log_path=None, history_path=None,
               events_path=None):
    """台账回复文本（命令与菜单按钮共用）。

    窗口内有任务事件 → 按 task_id 严格重建（新口径）；窗口内无事件
    （功能上线前的老日子）→ 回落日志关键词口径并注明估算。
    """
    days = max(1, min(days, LOG_RETENTION_DAYS))
    today = today or date.today()
    # 命令/菜单入口不传路径：在这里统一落默认值（两种口径都要读日志）
    log_path = log_path or LOG_FILE
    history_path = history_path or DOWNLOAD_HISTORY_FILE
    events = load_events(events_path)
    dates = _window_dates(days, today)
    prefixes = {d.isoformat() for d in dates}
    if any(str(e.get("ts", ""))[:10] in prefixes for e in events):
        return _event_text(events, days, today, log_path, history_path)
    text = _legacy_text(days, today, log_path, history_path)
    return text + "\n\n（该窗口无任务事件，以上为日志关键词估算）"


def _legacy_text(days, today, log_path, history_path):
    """旧口径：download.log 关键词 + history 行数（事件日志未覆盖时回落）。"""
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
