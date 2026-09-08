"""媒体下落查询：/find <关键字> → 「这条媒体到底去哪了」一屏看全。

零新增持久化（与 stats 同哲学）：事件与时间戳已在 download.log（近 7 天
窗口、含轮转文件）、download_history.txt（全量）与 download_queue.json
（当前队列/待重试）里，查询时汇聚。

为什么需要它：/retry、/queue 列表把长名截到尾部 48 字符（防 4096），
期数/编号这类区分度最高的词通常在名字**头部**——正好被截掉（实测
「bl44专属」三条就因此翻不到）。本查询搜**完整字段**的忽略大小写子串，
命中后给每条一个终态。

口径（每条命中一行，重信息轻措辞；日志措辞变化只会少计不会抛错）：
- ⏳ 待处理 / 🔁 待重试   = 当前队列 tasks/retry 命中（freshest，权威）
- 📨 收到 / ⏭️ 去重跳过
  / 🗑 已移除 / ❌ 原消息已删除 = 日志里无 trace 的事件行
- 🔁 失败 / ❓ 中断        = 日志里按 [T=] 聚合的任务组终态
  （组内任一行 ✅ 下载完成 → 不单列：历史里已有同一条，避免双计；
   组 id8 与队列记录 id 相同 → 不单列：队列条目已代表它）
- ✅ 已下载               = download_history.txt 全量命中

回复统一前缀「🔍 查询」，进自动清理白名单（config.CLEAN_NOTIFICATION_PREFIXES）。
"""
import re
from datetime import date, timedelta

from . import state
from . import stats
from .config import DOWNLOAD_HISTORY_FILE, LOG_FILE, LOG_RETENTION_DAYS

FIND_TEXT_PREFIX = "🔍 查询"
MIN_KEYWORD_LEN = 2

# 与 log.set_trace 写入的 [T=xxxxxxxx] 同形
_TRACE_RE = re.compile(r"\[T=([0-9a-f]{8})\]")


def is_find_command(text):
    # /find、/find 关键字（与 is_stats_command 同形）
    return bool(re.fullmatch(r"/find(?:\s+\S+)*", (text or "").strip(),
                             re.IGNORECASE))


def _trim(s, n=64, keep_tail=28):
    """长名压到 n 字符但保住尾部 keep_tail 字符。

    命名规则是「<日期> <caption> - <原文件名>」——同组条目的 caption 相同、
    区分点全在尾巴的真文件名上（bl专属44a/b/c），只保头会把它们显示成
    一模一样，查询就白查了。
    """
    s = " ".join((s or "").split())
    if len(s) <= n + keep_tail:
        return s
    return s[: n - 1] + "…" + s[-keep_tail:]


def _dt(line):
    # "2026-09-08 01:26:22 | ..." → "09-08 01:26"
    return line[5:16]


def _read_history_matches(kw, history_path):
    """历史文件全量倒序命中（append-only，越靠后越新）。"""
    hits = []
    try:
        with open(history_path, "r", encoding="utf-8",
                  errors="replace") as f:
            for line in reversed(f.readlines()):
                if kw not in line.lower() or " | " not in line:
                    continue
                parts = line.split(" | ")
                if len(parts) > 3:
                    hits.append(
                        f"✅ 已下载 | {_dt(line)} | {_trim(parts[2])}"
                        f" | {parts[3].strip()}"
                    )
    except OSError:
        pass
    return hits


def find_media(keyword, today=None, log_path=None, history_path=None,
               queue=None, limit=10):
    """按关键字搜一条媒体的下落；返回回复文本（纯函数，可注入测试）。"""
    keyword = (keyword or "").strip()
    if len(keyword) < MIN_KEYWORD_LEN:
        return (
            f"{FIND_TEXT_PREFIX}用法：/find <关键字>"
            f"（≥{MIN_KEYWORD_LEN} 字符，如 /find bl44）\n"
            "范围：当前队列 + 近 7 天日志 + 全部下载历史，完整字段子串匹配"
        )
    kw = keyword.lower()
    today = today or date.today()
    log_path = log_path or LOG_FILE
    history_path = history_path or DOWNLOAD_HISTORY_FILE
    if queue is None:
        queue = state.QUEUE or {"tasks": [], "retry": []}

    entries = []

    # ① 当前队列（freshest：在队列里的以这里为准）
    queue_id8 = set()
    for rec in queue.get("tasks", []):
        blob = f"{rec.get('final_name', '')} {rec.get('label', '')}".lower()
        if kw in blob:
            queue_id8.add(rec["id"][:8])
            entries.append(f"⏳ 待处理 | {_trim(rec.get('final_name', ''))}")
    for rec in queue.get("retry", []):
        blob = f"{rec.get('final_name', '')} {rec.get('label', '')}".lower()
        if kw in blob:
            queue_id8.add(rec["id"][:8])
            attempts = rec.get("attempts")
            att = f" | 失败 {attempts} 次" if attempts else ""
            entries.append(
                f"🔁 待重试{att} | {_trim(rec.get('final_name', ''))}"
            )

    # ② 日志（近 LOG_RETENTION_DAYS 天窗口，复用 stats 的读文件逻辑）
    dates = [today - timedelta(offset)
             for offset in range(LOG_RETENTION_DAYS - 1, -1, -1)]
    lines = stats._window_log_lines(dates, log_path)
    # 先按 trace 收**全组**（终局行往往不含关键字），再按组内任一行命中过滤
    all_traces = {}
    untraced = []
    for line in lines:
        m = _TRACE_RE.search(line)
        if m:
            all_traces.setdefault(m.group(1), []).append(line)
        elif kw in line.lower():
            untraced.append(line)
    traces = {tid: ls for tid, ls in all_traces.items()
              if any(kw in l.lower() for l in ls)}

    _UNTRACED_LABELS = (
        ("📦 检测到可下载媒体", "📨 收到"),
        ("⏭️ 重复媒体跳过入队", "⏭️ 去重跳过"),
        ("🗑 手动移除队列任务", "🗑 已移除"),
        ("队列任务原消息已被删除", "❌ 原消息已删除"),
    )
    for line in untraced:
        label = None
        for marker, lab in _UNTRACED_LABELS:
            if marker in line:
                label = lab
                detail = _trim(
                    line.split(" | ", 2)[-1].replace(marker, "")
                    .strip(" ：|")
                )
                break
        else:
            continue  # 📨 收到消息等前置噪音行：📦 才是入队 canonical
        entries.append(f"{label} | {_dt(line)} | {detail}")

    for id8, group in traces.items():
        if id8 in queue_id8:
            continue  # 队列条目已代表它
        if any("✅ 下载完成" in l for l in group):
            continue  # 历史里已有同一份下载
        if any("已达到最大重试次数" in l or "取消息超时" in l
               for l in group):
            fate = "🔁 失败进待重试"
        else:
            fate = "❓ 中断（进行中被删/重启）"
        name = None
        for l in group:
            if "最终文件名：" in l:
                name = l.split("最终文件名：", 1)[1]
                break
        if name is None:
            for l in group:
                if "▶️ 队列任务开始：" in l:
                    name = l.split("▶️ 队列任务开始：", 1)[1]
                    break
        entries.append(
            f"{fate} | {_dt(group[0])} | [T={id8}] {_trim(name)}"
        )

    # ③ 已下载（历史全量，倒序=新在前）
    history_hits = _read_history_matches(kw, history_path)
    entries.extend(history_hits)

    total = len(entries)
    if total == 0:
        return (
            f"{FIND_TEXT_PREFIX}「{keyword}」无匹配\n"
            "范围：当前队列 + 近 7 天日志 + 全部下载历史（完整字段子串匹配）"
        )

    shown = entries[:limit]
    body = "\n".join(
        f"{i}. {line}" for i, line in enumerate(shown, 1)
    )
    note = f"\n（仅显示前 {limit} 条）" if total > limit else ""
    return (
        f"{FIND_TEXT_PREFIX}「{keyword}」匹配 {total} 处\n\n{body}{note}"
    )
