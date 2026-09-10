"""Runtime Reporter：把运行状态主动汇报到 Telegram（只读观察者）。

设计约束（见 docs/tg_userbot_Runtime_Reporter_AI实施规格.md）：

- **只观察，不控制**：只读 `state.*` 与 `stats` 事件流，不参与任何调度决策；
  不建立第二套状态（没有 reporter_queue / reporter_workers / 独立统计文件）。
- **不改核心业务**：下载/队列/worker/retry/去重/`/progress`/`/stats` 一律不碰。
  本模块自己的异常被全部兜住，绝不拖垮核心系统。
- **两类输出**：① Status Panel——一条消息，首发 send_message、之后原地
  edit_message；② Event Notification——重要事件发独立消息。
- **汇报目标**：默认发到 bot 私聊（`state.BOT_ID`，控制/状态频道，bot 对话
  自带清理）；bot 未就绪时回落收藏夹。必须用**主账号**发——面板靠
  edit_message 原地刷新，而 Telegram 只允许编辑自己发的消息。
- **拿不到就显示 `--`**，不猜。

数据源全部是既有事实：
- `state.ACTIVE_DOWNLOADS`（`download.register_download` 写入，含 started_at/worker）
- `state.QUEUE` / `state.EXECUTING`
- `workers.worker_snapshot()`（编号 / BUSY-IDLE / 健康）
- `state.client` / `state.bot_client`
- `runtime/task_events.jsonl`（增量读，游标在内存，重启不重放历史）
"""
import asyncio
import json
import os
import re
import time
from datetime import datetime

from telethon.errors import (
    FloodWaitError,
    MessageIdInvalidError,
    MessageNotModifiedError,
    RPCError,
)

from . import state
from . import stats
from . import workers
from .config import (
    REPORT_AUTO_REPLAY,
    REPORT_DOWNLOAD_FAILED,
    REPORT_DOWNLOAD_START,
    REPORT_DOWNLOAD_SUCCESS,
    REPORT_ENABLED,
    REPORT_ERROR,
    REPORT_EVENT_POLL_SECONDS,
    REPORT_INTERVAL_SECONDS,
    REPORT_FALLBACK_TARGET,
    REPORT_MAX_DOWNLOADS_SHOWN,
    REPORT_MAX_FILENAME_CHARS,
    REPORT_MAX_MESSAGE_CHARS,
    REPORT_MAX_WORKER_ALERTS_SHOWN,
    REPORT_PROGRESS_ENABLED,
    REPORT_PROGRESS_INTERVAL_SECONDS,
    REPORT_RECOVERY,
    REPORT_RETRY,
    REPORT_SHUTDOWN,
    REPORT_STARTUP,
    REPORT_STATS_CACHE_SECONDS,
    REPORT_STATUS_PREFIX,
    REPORT_TO_BOT_CHAT,
    TASK_EVENTS_FILE,
)
from .log import logger

# 关闭通知的最长等待（秒）：Telegram 不可用时也不能阻塞进程退出（规格 §28）
REPORT_SHUTDOWN_TIMEOUT = 10

_ICONS = {"RUNNING": "🟢", "DEGRADED": "🟡", "ERROR": "🔴"}

# 汇报文本清洗（规格 §41）：bot token / api hash / cookie / 本机绝对路径
_REDACTIONS = (
    (re.compile(r"(bot_token\s*[=:]\s*)\S+", re.I), r"\1<redacted>"),
    (re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{25,}\b"), "<redacted-token>"),
    (re.compile(r"(api_hash\s*[=:]?\s*)[0-9a-fA-F]{32}"), r"\1<redacted>"),
    (re.compile(r"\b[0-9a-fA-F]{32}\b"), "<redacted-hash>"),
    (re.compile(r"((?:sessionid|authorization|cookie)\s*[=:]\s*)\S+", re.I),
     r"\1<redacted>"),
    (re.compile(r"/(?:Users|home)/[^/\s]+"), "~"),
)


def redact(text):
    """清洗汇报文本里的敏感片段（token / hash / cookie / 本机路径）。"""
    out = str(text)
    for pattern, repl in _REDACTIONS:
        out = pattern.sub(repl, out)
    return out


# ------------------------------------------------------------
# Level 1 纯函数（规格 §44：便于测试）
# ------------------------------------------------------------

def format_bytes(n):
    """字节 → 人类可读；None/非法 → `--`（不猜）。"""
    if n is None:
        return "--"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "--"
    if n < 0:
        return "--"
    for unit, div, fmt in (("TB", 1024 ** 4, "%.2f"),
                           ("GB", 1024 ** 3, "%.2f"),
                           ("MB", 1024 ** 2, "%.1f"),
                           ("KB", 1024, "%.1f")):
        if n >= div:
            return (fmt % (n / div)) + f" {unit}"
    return f"{int(n)} B"


def format_duration(seconds):
    """秒 → 人类可读；None/负数 → `--`。"""
    if seconds is None:
        return "--"
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "--"
    if seconds < 0:
        return "--"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60:02d}m {s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600:02d}h {(s % 3600) // 60:02d}m"
    return f"{s // 86400}d {(s % 86400) // 3600:02d}h"


def format_speed(bps):
    """字节/秒 → 文本；None/≤0 → `--`。"""
    if bps is None:
        return "--"
    try:
        bps = float(bps)
    except (TypeError, ValueError):
        return "--"
    if bps <= 0:
        return "--"
    if bps >= 1024 ** 2:
        return f"{bps / 1024 ** 2:.2f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.2f} KB/s"
    return f"{bps:.0f} B/s"


def format_eta(remaining_bytes, speed):
    """剩余字节 / 速度 → `MM:SS` 或 `H:MM:SS`；任一未知或速度 ≤0 → `--`。"""
    if remaining_bytes is None or speed is None:
        return "--"
    try:
        remaining_bytes = float(remaining_bytes)
        speed = float(speed)
    except (TypeError, ValueError):
        return "--"
    if speed <= 0 or remaining_bytes < 0:
        return "--"
    secs = int(remaining_bytes / speed)
    if secs < 3600:
        return f"{secs // 60:02d}:{secs % 60:02d}"
    return f"{secs // 3600}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"


def format_ago(seconds):
    """距今秒数 → 「12 秒前」这类文本；None → `--`。"""
    if seconds is None:
        return "--"
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "--"
    if seconds < 5:
        return "刚刚"
    if seconds < 60:
        return f"{int(seconds)} 秒前"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    return f"{int(seconds // 3600)} 小时前"


def _stat_line(label, stats_map, key):
    """统计行：没有这个指标就显示 `--`，不编数字。"""
    value = None
    if isinstance(stats_map, dict):
        value = stats_map.get(key)
    return f"{label}：{value if value is not None else '--'}"


def _clip(text, limit):
    """裁剪过长文件名（保尾——区分性最强的原始文件名在末尾，与 /queue 同口径）。"""
    text = str(text or "--")
    if len(text) <= limit:
        return text
    return "…" + text[-(limit - 1):]


def _download_block(downloads, limit=REPORT_MAX_DOWNLOADS_SHOWN):
    """「当前下载」区块：只列前 limit 条，其余折叠并保留总数。

    文件名做保尾裁剪：本项目文件名常带 200+ 字符的 caption 前缀，原样铺开
    三五条就能把消息顶到 Telegram 4096 上限（那样会直接发不出去）。
    """
    if not downloads:
        return ["⬇️ 当前下载", "（无进行中的下载）"]
    shown = downloads[:max(0, limit)]
    lines = ["⬇️ 当前下载"]
    for i, dl in enumerate(shown, 1):
        percent = dl.get("percent")
        pct = f"{percent}%" if percent is not None else "--"
        lines.append(f"{i}. "
                     f"{_clip(dl.get('filename'), REPORT_MAX_FILENAME_CHARS)}")
        lines.append(f"   {pct} | {format_bytes(dl.get('downloaded'))}"
                     f" / {format_bytes(dl.get('total'))}")
        lines.append(f"   {format_speed(dl.get('speed'))}"
                     f" | ETA {dl.get('eta') or '--'}")
        lines.append(f"   Worker {dl.get('worker') or '--'}"
                     f" | {format_duration(dl.get('elapsed'))}")
    rest = len(downloads) - len(shown)
    if rest > 0:
        lines.append(f"…还有 {rest} 个下载任务……")
    return lines


def _workers_block(snapshot):
    """Workers 区块：**只给计数 + 异常明细**，不逐条罗列每条 worker。

    20 条 worker 时逐行罗列要 20 行、把消息撑到几千字符（每个状态位还只是
    BUSY/IDLE 这种低信息量词），正常状态下纯属噪声。异常才值得看，所以
    正常只出计数，出问题时才逐条列（有上限）。
    """
    rows = snapshot.get("workers") or []
    total = snapshot.get("workers_total") or 0
    if not total:
        return ["⚙️ Workers", "（未启用多 worker，下载走主连接）"]
    busy = snapshot.get("workers_busy") or 0
    unhealthy = snapshot.get("workers_unhealthy") or 0
    lines = ["⚙️ Workers",
             f"{total} 条：{busy} 忙碌 / {max(0, total - busy - unhealthy)} 空闲"
             + (f" / {unhealthy} 异常" if unhealthy else "")]
    bad = [r for r in rows if r.get("health") == "UNHEALTHY"]
    for row in bad[:REPORT_MAX_WORKER_ALERTS_SHOWN]:
        reason = row.get("reason")
        suffix = f" · {_clip(reason, 40)}" if reason else ""
        lines.append(f"⚠️ {row.get('label') or '--'} UNHEALTHY{suffix}")
    rest = len(bad) - min(len(bad), REPORT_MAX_WORKER_ALERTS_SHOWN)
    if rest > 0:
        lines.append(f"…另有 {rest} 条异常")
    return lines


def _compose(snapshot, max_downloads=REPORT_MAX_DOWNLOADS_SHOWN):
    """渲染面板正文（内层；下载条数由外层按长度预算逐级下调）。"""
    st = snapshot.get("state") or "RUNNING"
    started = snapshot.get("started_wall")
    lines = [
        REPORT_STATUS_PREFIX,
        "",
        f"{_ICONS.get(st, '⚪')} {st}",
    ]
    reason = snapshot.get("degraded_reason")
    if reason and st != "RUNNING":
        lines.append(f"（{redact(reason)}）")
    lines += [
        "",
        "⏱ 运行时间",
        format_duration(snapshot.get("uptime_seconds")),
        f"启动：{started.strftime('%Y-%m-%d %H:%M:%S') if started else '--'}",
        "",
        "📊 当前任务",
    ]
    q = snapshot.get("queue") or {}
    lines += [
        f"队列：{q.get('pending', 0)}",
        f"下载中：{q.get('running', 0)}",
        f"重试中：{q.get('retry', 0)}",
        "",
    ]
    lines += _download_block(snapshot.get("downloads") or [], max_downloads)
    lines.append("")
    lines += _workers_block(snapshot)
    lines += [
        "",
        "📈 今日统计",
        _stat_line("收到", snapshot.get("stats"), "received"),
        _stat_line("成功", snapshot.get("stats"), "success"),
        _stat_line("失败", snapshot.get("stats"), "failed_final"),
        _stat_line("重试", snapshot.get("stats"), "retries"),
        _stat_line("自动重放", snapshot.get("stats"), "auto_replay"),
        _stat_line("去重", snapshot.get("stats"), "dedup_hit"),
        _stat_line("取消", snapshot.get("stats"), "cancelled"),
        "",
        "🔌 Connections",
    ]
    clients = snapshot.get("clients") or {}
    lines += [
        f"Main Client：{'🟢' if clients.get('main') else '🔴'}",
        f"Bot Client：{'🟢' if clients.get('bot') else '🔴'}",
        "",
        "🕐 最后活动",
        format_ago(snapshot.get("last_activity_ago")),
        "",
        "更新时间",
        (snapshot.get("now_wall") or datetime.now()).strftime(
            "%Y-%m-%d %H:%M:%S"),
    ]
    return "\n".join(lines)


def build_status_text(snapshot):
    """把快照渲染成 Status Panel 文本（纯函数，**保证不超长**）。

    Telegram 单条消息上限 4096 字符，超了会直接发送失败——而失败又被本模块
    静默吞成"没反应"，用户只会看到面板不再更新。所以这里做两级收敛：
    先按预算逐条减少下载列表，仍超长再做最终硬截断（保底）。
    """
    downloads = snapshot.get("downloads") or []
    shown = min(len(downloads), REPORT_MAX_DOWNLOADS_SHOWN)
    while True:
        text = _compose(snapshot, shown)
        if len(text) <= REPORT_MAX_MESSAGE_CHARS or shown == 0:
            break
        shown -= 1
    if len(text) > REPORT_MAX_MESSAGE_CHARS:
        text = text[:REPORT_MAX_MESSAGE_CHARS - 1] + "…"
    return text


# ------------------------------------------------------------
# Reporter
# ------------------------------------------------------------

class Reporter:
    """只读观察者：采集快照 → 渲染 → 发/改 Telegram 消息。

    Telegram 侧任何异常都被兜住（规格 §33）：记日志、本轮放弃、下轮再试，
    绝不进入紧密重试，也绝不向上冒泡影响核心系统。
    """

    def __init__(self, client=None):
        self._client = client
        self.status_message_id = None
        self._started_at = time.monotonic()
        self._started_wall = datetime.now()
        self._last_activity = time.monotonic()
        # 哨兵用 None 而不是 0.0：macOS 上 time.monotonic() 从 ~0 起步，
        # 拿 0.0 当「从未刷新过」会让 now-0.0 < 间隔 恒成立、面板永不出现。
        self._last_status_at = None
        self._event_cursor = None
        self._samples = {}          # did -> (monotonic, downloaded)
        self._stats_cache = None    # (采样时刻, 统计 dict)
        self._stats_dirty = True
        self._error_keys = set()    # 处于 ERROR 的 {组件|名称}
        self._client_up = {}        # 组件 -> 上次连接态
        self._worker_health = {}    # 编号 -> 上次健康态

    # ---------- 基础设施 ----------

    @property
    def client(self):
        """汇报用的客户端；注入优先，否则用主客户端。

        必须是**主账号**：面板靠 edit_message 原地刷新，而 Telegram 只允许
        编辑自己发的消息——bot 账号发的消息主账号无权编辑。
        """
        return self._client if self._client is not None else state.client

    @property
    def target(self):
        """汇报目标：bot 私聊（控制/状态频道，自带清理）优先，否则回落收藏夹。

        peer 用 state.BOT_ID（bot 的用户 id）——cleanup_bot_chat_once 也用
        同一个 peer 读写，从主账号视角能正常读到这条对话。
        """
        if REPORT_TO_BOT_CHAT and state.BOT_ID:
            return state.BOT_ID
        return REPORT_FALLBACK_TARGET

    @staticmethod
    def _fit(text):
        """最终长度护栏：任何一条外发消息都不得超 Telegram 上限。"""
        text = redact(text)
        if len(text) > REPORT_MAX_MESSAGE_CHARS:
            text = text[:REPORT_MAX_MESSAGE_CHARS - 1] + "…"
        return text

    def _touch_activity(self):
        self._last_activity = time.monotonic()

    # ---------- 快照采集（只读） ----------

    def collect_downloads(self, now=None):
        """进行中下载的快照；速度由本模块两次采样差分得出（真测量，非猜测）。"""
        now = time.monotonic() if now is None else now
        out = []
        for did, info in list(state.ACTIVE_DOWNLOADS.items()):
            downloaded = info.get("downloaded") or 0
            speed = None
            prev = self._samples.get(did)
            if prev is not None and now > prev[0] and downloaded >= prev[1]:
                speed = (downloaded - prev[1]) / (now - prev[0])
            self._samples[did] = (now, downloaded)
            total = info.get("total")
            started = info.get("started_at")
            out.append({
                "filename": info.get("filename"),
                "percent": info.get("percent"),
                "downloaded": downloaded,
                "total": total,
                "speed": speed,
                "eta": format_eta(
                    (total - downloaded) if total else None, speed),
                "worker": info.get("worker"),
                "elapsed": (now - started) if started is not None else None,
            })
        for did in list(self._samples):                  # 收尾的下载清采样
            if did not in state.ACTIVE_DOWNLOADS:
                self._samples.pop(did, None)
        out.sort(key=lambda d: d.get("elapsed") or 0.0, reverse=True)
        return out

    def collect_stats(self, now=None):
        """今日统计：复用 stats 事件口径；读不到就返回 None（面板显示 --）。

        带缓存：`load_events` 是**同步全量**读+解析整个事件文件（实测 3 万行
        封顶规模一次 load+rebuild 约 116ms，会整段阻塞事件循环），而下载中
        面板每 15s 就刷一次。所以只在「事件流有新行」或「缓存超时」时重算，
        其余时候直接复用——事件没变，统计必然没变。
        """
        now = time.monotonic() if now is None else now
        cached = self._stats_cache
        if cached is not None and not self._stats_dirty:
            if now - cached[0] < REPORT_STATS_CACHE_SECONDS:
                return cached[1]
        if not isinstance(state.QUEUE, dict):
            return None
        try:
            events = stats.load_events()
        except OSError:
            return None
        built = stats.rebuild_stats(events, days=1)
        built["auto_replay"] = sum(
            1 for e in events if e.get("ev") == "AUTO_REPLAY")
        self._stats_cache = (now, built)
        self._stats_dirty = False
        return built

    def _collect_connections(self):
        main = self.client
        bot = state.bot_client
        return {
            "main": bool(main is not None and main.is_connected()),
            "bot": bool(bot is not None and bot.is_connected()),
        }

    def snapshot(self, now=None):
        """一次只读快照（先复制再渲染，绝不在持锁状态下做网络请求）。"""
        now = time.monotonic() if now is None else now
        now_wall = datetime.now()
        queue = state.QUEUE if isinstance(state.QUEUE, dict) else {}
        tasks = queue.get("tasks") or []
        retry = queue.get("retry") or []
        downloads = self.collect_downloads(now=now)
        worker_rows = workers.worker_snapshot()
        clients = self._collect_connections()

        running = len(state.ACTIVE_DOWNLOADS)
        unhealthy = sum(1 for w in worker_rows
                        if w.get("health") == "UNHEALTHY")
        busy = sum(1 for w in worker_rows if w.get("state") == "BUSY")

        if not clients["main"]:
            runtime_state, reason = "ERROR", "主客户端未连接"
        elif unhealthy:
            runtime_state, reason = "DEGRADED", f"{unhealthy} 条 worker 异常"
        elif not clients["bot"]:
            runtime_state, reason = "DEGRADED", "bot 菜单未连接"
        else:
            runtime_state, reason = "RUNNING", None

        return {
            "state": runtime_state,
            "degraded_reason": reason,
            "started_wall": self._started_wall,
            "uptime_seconds": now - self._started_at,
            "now_wall": now_wall,
            "queue": {
                "pending": max(0, len(tasks) - running),
                "running": running,
                "retry": len(retry),
            },
            "downloads": downloads,
            "workers": worker_rows,
            "workers_total": max(len(worker_rows),
                                 len(state.DOWNLOAD_WORKERS)),
            "workers_busy": busy,
            "workers_unhealthy": unhealthy,
            "clients": clients,
            "stats": self.collect_stats(now=now),
            "last_activity_ago": now - self._last_activity,
        }

    def build_status_text(self, now=None):
        return build_status_text(self.snapshot(now=now))

    # ---------- Telegram 发送 ----------

    async def _safe_send(self, text):
        """发一条独立消息；返回发出的 Message（失败/异常返回 None）。

        任何 Telegram 异常都吞掉并记日志：记日志 → 本轮结束 → 下轮再试，
        绝不紧密重试（规格 §33）。
        """
        client = self.client
        if client is None:
            return None
        try:
            msg = await client.send_message(self.target, self._fit(text))
            self._touch_activity()
            return msg
        except asyncio.CancelledError:
            raise
        except FloodWaitError as e:
            logger.warning(f"🤖 汇报被 FloodWait 限流 {e.seconds}s，跳过本轮")
            return None
        except (MessageIdInvalidError, RPCError) as e:
            logger.warning(f"🤖 汇报发送失败（{type(e).__name__}: {e}）")
            return None
        except Exception as e:
            logger.warning(f"🤖 汇报发送失败（{type(e).__name__}: {e}）")
            return None

    async def _safe_edit(self, text):
        """原地刷新面板；返回 True=已刷新，False=本轮放弃。"""
        client = self.client
        try:
            await client.edit_message(
                self.target, self.status_message_id, self._fit(text))
            self._touch_activity()
            return True
        except asyncio.CancelledError:
            raise
        except MessageNotModifiedError:
            return True                      # 内容没变 = 正常，不是错误
        except MessageIdInvalidError:
            # 面板被清理/删除了 → 下一轮重建，本轮不连发两条
            logger.info("🤖 状态面板消息已失效，下一轮重建")
            self.status_message_id = None
            return False
        except FloodWaitError as e:
            logger.warning(f"🤖 状态面板刷新被 FloodWait 限流 {e.seconds}s")
            return False
        except Exception as e:
            logger.warning(f"🤖 状态面板刷新失败（{type(e).__name__}: {e}）")
            return False

    async def update_status(self, now=None):
        """刷新 Status Panel：首轮创建、之后原地编辑（不重复创建消息）。"""
        text = self.build_status_text(now=now)
        if self.status_message_id is None:
            # 消息 id 直接取 send_message 的返回值——不再多发一次请求去查
            msg = await self._safe_send(text)
            self.status_message_id = getattr(msg, "id", None)
            if self.status_message_id is not None:
                logger.info(f"🤖 状态面板已创建（消息 id={self.status_message_id}，"
                            f"目标={self.target}）")
            return self.status_message_id is not None
        return await self._safe_edit(text)

    async def notify_event(self, kind, **kw):
        """发一条事件通知（独立消息）。未知 kind 不发。"""
        text = build_event_text(kind, **kw)
        if text is None:
            return False
        return await self._safe_send(text) is not None

    # ---------- 异常指纹与去重（规格 §26） ----------

    @staticmethod
    def _error_key(component, name):
        return f"{component}|{name}"

    @staticmethod
    def normalize_error(message):
        """归一化错误文本：数字（端口/时长/地址位）替换为 #，压空白、转小写。

        避免「同一异常因为带了个端口号就每次都算新错误」。
        """
        text = re.sub(r"\d+", "#", str(message))
        return re.sub(r"\s+", " ", text).strip().lower()

    def error_state(self, component, name):
        return ("ERROR" if self._error_key(component, name) in self._error_keys
                else "NORMAL")

    def note_error(self, component, name, message):
        """记一次异常；返回 True 表示「首次进入 ERROR，应当通知」。"""
        key = self._error_key(component, name)
        fingerprint = f"{key}|{self.normalize_error(message)}"
        if key in self._error_keys:
            logger.debug(f"🤖 异常持续中（不重复通知）：{fingerprint}")
            return False
        self._error_keys.add(key)
        return True

    def note_recovery(self, component, name):
        """记一次恢复；返回 True 表示「确实从 ERROR 恢复，应当通知」。"""
        key = self._error_key(component, name)
        if key not in self._error_keys:
            return False
        self._error_keys.discard(key)
        return True

    # ---------- 事件流增量读取（规格 §39） ----------

    def mark_event_cursor(self):
        """把游标移到当前文件末尾：启动前的历史事件不再重复通知。"""
        self._event_cursor = self._event_file_size()

    def _event_file_size(self):
        try:
            return os.path.getsize(TASK_EVENTS_FILE)
        except OSError:
            return 0

    async def poll_events(self):
        """读取事件文件的新增行并派发通知（增量，不整文件重扫）。"""
        size = self._event_file_size()
        if self._event_cursor is None:
            self._event_cursor = size
            return
        if size < self._event_cursor:
            # 文件被原子重写（stats.trim_event_file）→ 游标失效，重置
            self._event_cursor = size
            return
        if size == self._event_cursor:
            return
        try:
            with open(TASK_EVENTS_FILE, "rb") as f:
                f.seek(self._event_cursor)
                chunk = f.read()
        except OSError:
            return
        self._event_cursor = size
        events = []
        for line in chunk.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue                     # 半行/坏行：跳过，不阻断汇报
        if events:
            self._touch_activity()
            self._stats_dirty = True      # 统计口径变了，下一轮重算
        await self.dispatch_events(events)

    async def dispatch_events(self, events):
        """把新事件映射成通知。自动重放按批聚合，避免 N 条刷屏。"""
        auto = [e for e in events if e.get("ev") == "AUTO_REPLAY"]
        if auto and REPORT_AUTO_REPLAY:
            await self.notify_event(
                "auto_replay", count=len(auto),
                labels=[e.get("label") for e in auto if e.get("label")])
        if REPORT_DOWNLOAD_SUCCESS:
            for e in events:
                if e.get("ev") == "SUCCESS":
                    await self.notify_event("success", label=e.get("label"),
                                            bytes=e.get("bytes"))
        if REPORT_DOWNLOAD_FAILED:
            for e in events:
                if e.get("ev") == "FAILED":
                    await self.notify_event("failed", label=e.get("label"),
                                            attempts=e.get("attempts"))
        if REPORT_RETRY:
            for e in events:
                if e.get("ev") == "RETRY":
                    await self.notify_event("retry", label=e.get("label"),
                                            attempts=e.get("attempts"))

    # ---------- 健康转换 → 异常/恢复通知 ----------

    async def check_health(self):
        """把「客户端连接态 / worker 健康态」的变化转成 ERROR/RECOVERY 通知。

        只汇报**能观测到**的：worker 池没有任何心跳/健康机制，这里的 UNHEALTHY
        来自借出重连失败与下载中途重连失败的真实标记，不是猜的。
        """
        clients = self._collect_connections()
        for name, label in (("main", "Main Client"), ("bot", "Bot Client")):
            up = clients.get(name)
            before = self._client_up.get(name)
            self._client_up[name] = up
            if before is None or before == up:
                continue
            if up:
                if REPORT_RECOVERY and self.note_recovery("client", name):
                    await self.notify_event("recovery", component=label)
            else:
                if REPORT_ERROR and self.note_error("client", name, "已断开"):
                    await self.notify_event("error", component=label,
                                            status="已断开", reason="连接中断")

        for row in workers.worker_snapshot():
            label = row.get("label") or "--"
            health = row.get("health")
            before = self._worker_health.get(label)
            self._worker_health[label] = health
            if before is None or before == health:
                continue
            if health == "HEALTHY":
                if REPORT_RECOVERY and self.note_recovery("worker", label):
                    await self.notify_event("recovery", component=f"Worker {label}")
            else:
                if REPORT_ERROR and self.note_error(
                        "worker", label, row.get("reason") or "unknown"):
                    await self.notify_event(
                        "error", component=f"Worker {label}",
                        status=health, reason=row.get("reason"))

    # ---------- 生命周期 ----------

    async def start(self):
        """启动：定起点、跳过历史事件、发启动通知。"""
        self._started_at = time.monotonic()
        self._started_wall = datetime.now()
        self._last_status_at = None
        self.mark_event_cursor()
        if REPORT_STARTUP:
            await self.notify_event(
                "startup", workers=len(state.DOWNLOAD_WORKERS),
                started=self._started_wall)

    async def stop(self):
        """停止：发关闭通知（有超时，绝不阻塞进程退出）。"""
        if not REPORT_SHUTDOWN:
            return
        try:
            await asyncio.wait_for(
                self.notify_event(
                    "shutdown", uptime=time.monotonic() - self._started_at,
                    downloads=len(state.ACTIVE_DOWNLOADS),
                    queued=len((state.QUEUE or {}).get("tasks") or [])),
                timeout=REPORT_SHUTDOWN_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"🤖 关闭通知未发出（{type(e).__name__}: {e}）")

    def _status_interval(self):
        if REPORT_PROGRESS_ENABLED and state.ACTIVE_DOWNLOADS:
            return REPORT_PROGRESS_INTERVAL_SECONDS
        return REPORT_INTERVAL_SECONDS

    async def tick(self):
        """一个汇报周期：健康转换 → 事件通知 → 按节奏刷新面板。"""
        await self.check_health()
        await self.poll_events()
        now = time.monotonic()
        if self._last_status_at is None or \
                now - self._last_status_at >= self._status_interval():
            await self.update_status(now=now)
            self._last_status_at = now

    async def run(self):
        """后台主循环（规格 §31/§32）：独立 Task，异常永不外逸、取消原样上抛。"""
        if not REPORT_ENABLED:
            return
        while True:
            if state.STOP_EVENT is not None and state.STOP_EVENT.is_set():
                return
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"🤖 汇报周期异常（不影响核心系统）：{e}")
            await asyncio.sleep(REPORT_EVENT_POLL_SECONDS)


# ------------------------------------------------------------
# 事件通知文案（纯函数）
# ------------------------------------------------------------

def build_event_text(kind, **kw):
    """事件通知文案；未知 kind 返回 None（不发明消息）。"""
    if kind == "startup":
        started = kw.get("started")
        return ("🚀 Userbot 已启动\n\n"
                f"启动时间：{started.strftime('%H:%M:%S') if started else '--'}\n"
                f"Workers：{kw.get('workers', 0)}\n"
                "状态：🟢 RUNNING")
    if kind == "shutdown":
        return ("🛑 Userbot 正在关闭\n\n"
                f"运行时间：{format_duration(kw.get('uptime'))}\n"
                f"当前下载：{kw.get('downloads', 0)}\n"
                f"队列任务：{kw.get('queued', 0)}")
    if kind == "auto_replay":
        count = kw.get("count") or 0
        labels = [l for l in (kw.get("labels") or []) if l]
        lines = ["♻️ 自动重放", "", f"本次放行：{count} 个任务"]
        if labels:
            preview = "、".join(labels[:3])
            more = len(labels) - min(len(labels), 3)
            lines.append(f"文件：{preview}" + (f" 等 {more} 个" if more else ""))
        lines.append("原因：等待期已过，网络恢复后自动续跑")
        return "\n".join(lines)
    if kind == "error":
        return ("⚠️ Userbot 异常\n\n"
                f"组件：{kw.get('component') or '--'}\n"
                f"状态：{kw.get('status') or '--'}\n"
                f"原因：{kw.get('reason') or '--'}")
    if kind == "recovery":
        return ("✅ 已恢复\n\n"
                f"组件：{kw.get('component') or '--'}\n"
                "状态：🟢 HEALTHY")
    if kind == "success":
        return ("✅ 下载成功\n\n"
                f"文件：{kw.get('label') or '--'}\n"
                f"大小：{format_bytes(kw.get('bytes'))}\n"
                f"耗时：{format_duration(kw.get('elapsed'))}\n"
                f"Worker：{kw.get('worker') or '--'}")
    if kind == "failed":
        return ("❌ 下载失败\n\n"
                f"文件：{kw.get('label') or '--'}\n"
                f"重试：{kw.get('attempts') or '--'}\n"
                f"原因：{kw.get('reason') or '--'}")
    if kind == "retry":
        return ("🔄 任务重试\n\n"
                f"文件：{kw.get('label') or '--'}\n"
                f"第：{kw.get('attempts') or '--'} 次\n"
                f"原因：{kw.get('reason') or '--'}")
    return None
