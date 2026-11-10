"""本地 API（2026-11-01）：套壳 app / 手机看板的唯一数据面。

**定位与纪律**（与调研定稿一致）：
- 只读观察 + 薄封装既有服务函数——与 bot 菜单/``handle_command`` 同层调用，
  **不建第二套状态**（reporter 同款纪律）。看板/分享直达看到的队列、台账、
  配置与 Telegram 侧完全同源。
- 写操作仅两个入口：``/api/enqueue``（链接分流到既有下载链路，语义与
  「往收藏夹发一条链接」逐字一致）与 ``/api/action/<name>``（动作白名单，
  每项对应一个既有服务函数）。``/sql`` 等 owner-only 危险命令**不进** API。
- 鉴权：``Authorization: Bearer <token>`` / ``X-Token`` 头 / ``?token=``
  三选一。token 取 tg_secrets.json ``api`` 段；为空时只允许 loopback 绑定
  （本机免鉴权可信），非 loopback 必须配 token 否则拒绝启动（config 校验
  + 这里双保险）。
- 入队链路本身自带三级去重（tg:/dyc:/f:/c:），同一链接重复提交天然安全；
  解析/入队失败也自动降级解析 bot，绝不静默丢链接——这是收藏夹路径的
  既有性质，API 直接继承。

依赖 aiohttp。import 期零副作用；服务协程由 app.main() create_task 拉起，
随 STOP_EVENT 收尾（cancel → finally runner.cleanup）。
"""
import asyncio
import collections
import hmac
import json
import os
import re
import shutil
import time
from pathlib import Path

from aiohttp import web

from . import config
from . import state
from . import stats
from . import history
from . import link_journal
from . import notify
from . import routing
from .spawn import spawn
from .log import logger

_STARTED_AT = time.time()
_DASHBOARD_PATH = Path(__file__).with_name("api_dashboard.html")

# 单次链接分流的硬顶：resolver 每条 30s、转 bot 每条 60s，多条链接取上限
_DISPATCH_TIMEOUT_SECONDS = 120

# 分享幂等 id：套壳 app 用 UUID4 去掉横线（32 位小写十六进制）。只认这个
# 精确形态，避免把普通文本开头的 32 字符单词误当 id。
_REQUEST_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def _clean_request_id(value):
    """合法 request_id 原样返回；缺失/形态不对返回 None（按无幂等处理）。"""
    rid = str(value or "").strip()
    return rid if _REQUEST_ID_RE.match(rid) else None


def _split_relay_payload(payload):
    """拆 🛰 中转载荷的 ``<request_id> <原文>`` 前缀。

    旧版 app / 手工发的 🛰 没有前缀：整段当原文（向后兼容）。
    """
    text = str(payload or "").strip()
    if not text:
        return None, ""
    head, _, rest = text.partition(" ")
    rid = _clean_request_id(head)
    if rid:
        return rid, rest.strip()
    return None, text

# 动作白名单：名字 → 描述（/api/action/<name> 只认这里的键）。
# 全部薄封装既有服务函数；thread/chrome_cancel 带 JSON body 参数。
ACTION_WHITELIST = {
    # 队列/扫描
    "clean": "清理 .download 临时文件（/clean 同源）",
    "wl_scan": "立即跑一轮下载白名单扫描（/wl scan 同源）",
    "listen_scan": "立即跑一轮标签监听扫描（/listen scan 同源）",
    "retry_all": "重放全部待重试任务（/retry all 同源）",
    # 开关/设置
    "dedup_on": "开启重复去重",
    "dedup_off": "关闭重复去重（重下已删文件时用）",
    "listen_on": "开启标签监听",
    "listen_off": "关闭标签监听",
    "thread": "设置并发路数（body: {\"n\": 1-25}）",
    # Chrome Agent
    "chrome_start": "启动 Chrome Agent",
    "chrome_stop": "停止 Chrome Agent（Chrome 保持运行）",
    "chrome_cancel": "取消一个 Chrome 任务（body: {\"id\": 序号或task_id}）",
    # Pawchive / B站
    "paw_pause": "Pawchive 暂停下载 worker",
    "paw_resume": "Pawchive 恢复下载 worker",
    "paw_status": "Pawchive 状态视图",
    "bili_status": "B站 下载状态视图",
    # CD2 / 健康
    "cd2_start": "启动 CloudDrive2（慢操作，最多 ~30s）",
    "cd2_stop": "停止 CloudDrive2",
    "health": "一键巡检（/health 同源）",
    "now": "系统现在在干什么（/now 同源）",
}

# 套壳 app 的 Telegram 中转标记（2026-11-01）：手机上 Tailscale 与代理互斥
# （安卓单 VPN），app 的分享改为持 bot token 经 Bot API 以 bot 身份把
# 「🛰 <原文>」发进 bot 对话，主账号（本进程主客户端）收到后分流入队。
# 前缀必须不出现在 CLEAN_NOTIFICATION_PREFIXES（tests 钉死），否则通知
# 会被误当分享处理。
APP_RELAY_MARKER = "🛰 "

# ── 限流（2026-11-01，公网加固）────────────────────────────
# 非 loopback 来源每 IP 每分钟 _RATE_LIMIT 次，超了 429。本机脚本
# （127.0.0.1/::1）豁免——Funnel 转发回来的 remote 是 Tailscale 网络扩展
# 呈现的来源，不是 127.0.0.1，同样受限；阈值放宽到正常用不掉的量级。
_RATE_LIMIT = 120
_RATE_WINDOW = 60.0
_rate_hits: dict = {}          # ip -> deque[monotonic 时间戳]


def _rate_allow(ip: str, now=None) -> bool:
    """滑动窗口限流判定（纯函数，可注入 now 单测）。"""
    now = time.monotonic() if now is None else now
    if not ip or ip in ("127.0.0.1", "::1"):
        return True
    dq = _rate_hits.setdefault(ip, collections.deque())
    while dq and now - dq[0] > _RATE_WINDOW:
        dq.popleft()
    if len(dq) >= _RATE_LIMIT:
        return False
    dq.append(now)
    return True


@web.middleware
async def _rate_limit_middleware(request, handler):
    if not _rate_allow(request.remote or ""):
        return _json({"ok": False, "error": "请求过于频繁，稍后再试"}, 429)
    return await handler(request)


# ── 粘贴历史 + 人工待处理链接（2026-11-01）──────────────────
# 粘贴记录：app/API 每次链接分流都追加一行 paste_log.jsonl（单行 append
# 纪律，读时裁剪到上限）；状态回查按路由去向各查真相源（下载任务=事件流、
# iwara/Chrome=chrome_tasks、台账=runtime_db），查询失败不影响返回。

_PASTE_LOG_FILE = link_journal.PATH

# ── PWA 静态资产（2026-11-02）────────────────────────────
# manifest / SW / 图标 / 分享接收页必须**免鉴权可取**——PWA 安装与系统
# 分享面板不携带令牌；页面内不含敏感信息，数据端点仍全部鉴权。
_PWA_DIR = Path(__file__).with_name("pwa")
_PWA_PUBLIC_PATHS = ("/manifest.json", "/sw.js", "/share",
                     "/icons/icon-192.png", "/icons/icon-512.png")
_PASTE_LOG_MAX = link_journal.MAX_ENTRIES


def _log_paste(text, url, routed, ref=None, **kwargs) -> None:
    """兼容旧调用：实际写 link_journal（全入口统一日志）。"""
    link_journal.record(text, url, routed, ref=ref,
                        path=_PASTE_LOG_FILE, **kwargs)


def _read_paste_log(n=50) -> list:
    return link_journal.read(n, path=_PASTE_LOG_FILE)


_CHROME_STATUS_VIEW = {
    "SUCCESS": ("✅ 已完成", "done"), "FAILED": ("❌ 失败", "bad"),
    "RUNNING": ("下载中", "run"), "PENDING": ("排队", "wait"),
    "RETRY_WAIT": ("等待重试", "retry"), "CANCELLED": ("已取消", "info"),
}
_EVENTS_STATUS_VIEW = {
    "QUEUED": ("排队", "wait"), "RECEIVED": ("已受理", "info"),
    "RUNNING": ("下载中", "run"), "RETRY": ("待重试", "retry"),
    "SUCCESS": ("✅ 已完成", "done"), "FAILED": ("❌ 失败", "bad"),
    "DEDUP_HIT": ("去重拦截", "info"), "REMOVED": ("已移除", "info"),
    "CANCELLED": ("已取消", "info"), "DEDUP_SKIPPED": ("去重跳过", "info"),
}
_TASK_STATUS_VIEW = {
    "PENDING": ("⏳ 排队", "wait"), "PROCESSING": ("下载中", "run"),
    "DONE": ("✅ 已完成", "done"), "FAILED": ("❌ 失败", "bad"),
}


def _task_domain_status(row, label_key=None):
    """TaskDomain 行（bili/xn/xi）→ 看板状态文本（含标签与失败原因）。"""
    status = str(row.get("status") or "")
    text, kind = _TASK_STATUS_VIEW.get(status, (status or "处理中", "info"))
    label = str(row.get("label")
                or (row.get(label_key) if label_key else "") or "")
    if label:
        text += f" · {label[:40]}"
    if status == "FAILED" and row.get("error"):
        text += f" · {str(row['error'])[:60]}"
    elif status == "PENDING" and row.get("next_retry_at") \
            and row.get("attempts"):
        text += "（等待重试）"
    return text, kind


def _paste_status(p):
    """回查一条链接记录的当前处理状态（尽力而为，查询失败不炸）。"""
    routed, ref, url = p.get("routed"), p.get("ref"), p.get("url")
    # 统一日志里的失败/说明回执：任何类型都能先显示明确结果
    msg = str(p.get("message") or "")
    if msg.startswith("❌"):
        return msg.strip(), "bad"
    if msg.startswith("ℹ️"):
        return msg.strip(), "info"
    try:
        if routed == "tme":
            # ref = JSON [[chat_id, msg_id], …]（v2.16 起写入）——
            # 用 origin_chat_id/origin_msg_id 在当前队列里定位任务状态；
            # 老记录无 ref → 静态文案
            if not ref:
                return "📥 已入队下载（t.me 帖）", "run"
            try:
                pairs = json.loads(ref)
            except ValueError:
                return "📥 已入队下载（t.me 帖）", "run"
            q = state.QUEUE or {}
            found = None
            for r in (q.get("tasks") or []) + (q.get("retry") or []):
                oc = r.get("origin_chat_id")
                om = r.get("origin_msg_id")
                for pair in pairs:
                    if oc is not None and om is not None and \
                            [oc, om] == list(pair):
                        found = r
                        break
                if found:
                    break
            if found:
                last_ev = None
                for e in stats.load_events():
                    if e.get("task_id") == found.get("id"):
                        last_ev = e
                ev = last_ev.get("ev") if last_ev else "QUEUED"
                text, kind = _EVENTS_STATUS_VIEW.get(
                    ev, (ev, "info"))
                fname = found.get("final_name") or ""
                if fname:
                    text += f" · {fname[:40]}"
                return text, kind
            # 队列里没有（已完成的任务会被移除）→ 查历史/日志关键词
            url_hint = (url or "").rsplit("/", 1)[-1]
            if url_hint:
                for e in stats.load_events():
                    label = e.get("label") or ""
                    if url_hint in label or e.get("task_id") == ref:
                        text, kind = _EVENTS_STATUS_VIEW.get(
                            e.get("ev"), ("已完成", "done"))
                        return f"✅ 已完成 · {label[:40]}" if e.get("ev") == \
                            "SUCCESS" else (text, kind)
                return "✅ 已处理完成（已出队列）", "done"
            return "📥 已入队下载（t.me 帖）", "run"
        if routed in ("iwara", "iwara_dup"):
            # ① Chrome 任务实时状态（排队/下载中/完成/失败/重试）
            from . import chrome_agent, chrome_client
            tasks = chrome_agent.load_tasks(config.CHROME_TASKS_FILE)
            t = None
            if ref:
                t = next((x for x in tasks if x.get("task_id") == ref), None)
            if t is None and url:
                # 老粘贴记录 ref 丢失：按 URL 找回当前任务（取最新一条）
                t = next((x for x in reversed(tasks)
                          if x.get("url") == url), None)
            if t:
                text, kind = _CHROME_STATUS_VIEW.get(
                    t.get("status"), (t.get("status") or "未知", "info"))
                if t.get("filename"):
                    text += f" · {t['filename']}"
                return text, kind
            # ② Agent 还没认领：requests 台账里已有 task_id（提交瞬间
            #    routing 现在会把它的 ref 写进粘贴记录）
            reqs = chrome_client.load_requests()
            r = None
            if ref:
                r = next((x for x in reqs if x.get("task_id") == ref), None)
            if r is None and url:
                r = next((x for x in reversed(reqs)
                          if x.get("url") == url), None)
            if r:
                if r.get("notified_at"):
                    # 任务滚出 200 条窗口后，requests 里的终态仍可回查
                    outcome = str(r.get("outcome") or "").upper()
                    text, kind = {
                        "SUCCESS": ("✅ 已完成", "done"),
                        "FAILED": ("❌ 失败", "bad"),
                        "CANCELLED": ("🛑 已取消", "info"),
                    }.get(outcome, ("已处理（详见 Chrome 任务）", "info"))
                    if r.get("filename"):
                        text += f" · {r['filename']}"
                    return text, kind
                return "⏳ 排队（待 Agent 认领）", "wait"
            # ③ 兜底：按视频 ID 查既往成功——ref 缺失、任务滚出窗口、
            #    去重拦截三种情况都能对上（与 dispatch 判重同一数据源）
            from .iwara import extract_iwara_video_id
            vid = (extract_iwara_video_id(url or "")
                   or extract_iwara_video_id(str(p.get("text") or "")))
            if vid:
                from . import iwara as iwara_mod
                try:
                    hit = iwara_mod.find_previous_success(vid)
                except Exception:
                    hit = None
                if hit:
                    fname = hit.get("filename") or ""
                    return ("✅ 已处理（重复拦截）"
                            + (f" · {fname}" if fname else ""), "done")
        if routed == "torrent":
            # 115 离线任务的精确进度在 CD2 侧（需 info_hash）；这里至少把
            # 提交结果如实显示，不再回落「已受理」
            msg = str(p.get("message") or "")
            first = msg.splitlines()[0].strip() if msg else ""
            if any(k in msg for k in ("失败", "拒绝", "错误")):
                return first[:100] or "❌ 种子提交失败", "bad"
            if first:
                return first[:100], "wait"
            return "🧲 已提交 115 离线（完成会通知）", "wait"
        if routed == "bili" and url:
            # B站任务在 bili_tasks（TaskDomain）；粘贴记录的 url 就是业务键
            from . import bili_dl, runtime_db
            row = runtime_db.BILI_TASKS.get(url)
            if row is None:
                # URL 带 query / 不同形态：按 BV 号从近期任务里找回
                m = (bili_dl.BV_RE.search(url)
                     or bili_dl.BV_RE.search(str(p.get("text") or "")))
                if m:
                    row = next(
                        (r for r in runtime_db.BILI_TASKS.list(limit=200)
                         if m.group(0) in (r.get("url") or "")), None)
            if row:
                return _task_domain_status(row, label_key="url")
            return "已不在 B站队列（已完成或已移除）", "info"
        if routed == "x" and (url or p.get("text")):
            # X 任务的业务键 = normalize_target 后的 URL/@作者
            from . import runtime_db, x_dl
            key, _gallery, _label, _err = x_dl.normalize_target(
                url or p.get("text"))
            if key:
                row = runtime_db.XN_TASKS.get(key)
                if row:
                    return _task_domain_status(row, label_key="target")
                return "已不在 X 队列（已完成或已移除）", "info"
        if routed == "wnacg" and url:
            # wnacg 业务键 = URL 里的 aid
            from . import runtime_db, wnacg
            aid = wnacg.parse_wnacg_ref(url)
            row = runtime_db.get_wnacg_task(aid) if aid else None
            if row:
                return _task_domain_status(row, label_key="title")
            return "已不在 wnacg 队列（已完成或已移除）", "info"
        if routed == "asmr" and url:
            # asmr 业务键 = URL 里的 RJ 号
            from . import asmr_dl, runtime_db
            rj = asmr_dl.parse_asmr_ref(url)
            row = runtime_db.get_asmr_task(rj) if rj else None
            if row:
                return _task_domain_status(row, label_key="rj")
            return "已不在 asmr 队列（已完成或已移除）", "info"
        if routed == "ehentai":
            # 无任务表：后台跑完即通知；受理态静态展示
            return "🌸 后台下载中（结果会通知）", "run"
        if routed in ("douyin", "instagram") and ref:
            last = None
            for e in stats.load_events():
                if e.get("task_id") == ref:
                    last = e
            if last:
                text, kind = _EVENTS_STATUS_VIEW.get(
                    last.get("ev"), (last.get("ev") or "处理中", "info"))
                return text, kind
            return "已受理（解析中）", "info"
        if routed in ("douyin", "instagram") and not ref:
            return "已转解析 bot", "info"
        if routed == "ledger" and url:
            from . import runtime_db
            hits = runtime_db.search_manual_links(url, limit=1)
            if hits:
                row = hits[0]
                return (("👤 待人工处理", "pending")
                        if row["status"] == "PENDING" else ("✅ 已处理", "done"))
            return "已登记台账", "info"
    except Exception as e:
        logger.debug(f"粘贴状态回查失败：{e}")
    return "已受理", "info"


async def handle_pastes(request):
    """粘贴历史（新→旧）+ 逐条当前处理状态。"""
    try:
        n = int(request.query.get("n", 30))
    except ValueError:
        n = 30
    n = max(1, min(n, 200))
    items = _read_paste_log(n)
    for p in items:
        p["status"], p["kind"] = _paste_status(p)
    return _json({"ok": True, "items": items})


async def handle_links(request):
    """人工待处理外链台账（/links 同源 runtime DB，PENDING 清单）。"""
    from . import runtime_db
    try:
        pending = runtime_db.list_manual_links(status="PENDING", limit=100)
        done_n = runtime_db.count_manual_links(status="DONE")
    except Exception as e:
        logger.exception(f"API 台账读取失败：{e}")
        return _json({"ok": False, "error": "台账读取失败（详见服务端日志）"}, 500)
    rows = [{"id": r["id"], "url": r["url"], "host": r.get("host") or "",
             "note": r.get("note") or ""} for r in pending]
    return _json({"ok": True, "pending": rows, "done_count": done_n})


async def handle_links_done(request):
    try:
        body = await request.json()
    except Exception:
        return _json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
    link_id = str(body.get("id") or "").strip()
    if not link_id.isdigit():
        return _json({"ok": False, "error": "缺少数字 link id"}, 400)
    from . import runtime_db
    try:
        ok = runtime_db.manual_link_done(int(link_id))
    except Exception as e:
        logger.exception(f"API 台账标记完成失败：{e}")
        return _json({"ok": False, "error": "标记失败（详见服务端日志）"}, 500)
    return _json({"ok": bool(ok),
                  "message": "✅ 已标记完成" if ok else "ℹ️ 该条不存在或已不是待处理"})


async def handle_app_relay(text: str) -> None:
    """处理 app 经 Bot API 中转来的分享文本：与 /api/enqueue 同一入口。

    fire-and-forget：app 端拿不到同步回执，结果统一经 Telegram 通知回
    控制面板（成功/失败都说人话）。任何异常都不外泄——这是后台任务。

    载荷形态：``<request_id> <原文>``（2026-11-09 起的套壳 app）；没有
    32 位十六进制前缀的旧形态整段当原文。重复提交（duplicate）不再发
    通知——首次已发过，重放只记日志。
    """
    request_id, payload = _split_relay_payload(text)
    try:
        result = await asyncio.wait_for(
            dispatch_link_text(payload, surface="app",
                               request_id=request_id),
            timeout=_DISPATCH_TIMEOUT_SECONDS)
    except Exception as e:
        logger.exception(f"🛰 app 中转入队失败：{e}")
        try:
            await notify.notify_user(
                f"📲 分享直达失败：{type(e).__name__}: {e}\n{payload[:80]}")
        except Exception:
            pass
        return
    if result.get("ok"):
        if result.get("duplicate"):
            logger.info(
                f"🛰 app 中转重复提交已忽略（{result.get('routed')}）："
                f"{payload[:60]}")
            return
        logger.info(
            f"🛰 app 中转已受理（{result.get('routed')}）：{payload[:60]}")
        try:
            await notify.notify_user(
                f"📲 分享直达已受理\n{result.get('message', '')}")
        except Exception:
            pass
    else:
        try:
            await notify.notify_user(
                f"📲 分享直达未受理：{result.get('message', '')}")
        except Exception:
            pass


class _ShimMessage:
    """给 platform.relay_platform_links 的最小消息替身（只供 API 用）。

    platform 只读 ``message.id``（PROCESSING_DOUYIN_IDS 去重键）与
    ``message.message``（原文：链接提取评论标注 extract_link_comment 用）。
    id 取 10^12 起的递增数——真实 Telegram 消息 id 远小于此，永不碰撞。
    """

    _next_id = 10 ** 12

    def __init__(self, text):
        _ShimMessage._next_id += 1
        self.id = _ShimMessage._next_id
        self.message = text


_LOOPBACK_HOSTS = frozenset({
    "127.0.0.1", "localhost", "::1", "[::1]",
})


def _host_allowed_loopback(request) -> bool:
    """token 为空（纯 loopback 部署）时的 Host 头校验。

    防两类浏览器侧攻击（2026-11-10 审查 M5）：恶意网页 no-cors POST
    （CSRF——浏览器会带真实 Host，跨站伪造不了 loopback Host 之外的值）
    与 DNS rebinding（rebinding 域名解析到 127.0.0.1 后 Host 是攻击者
    域名，此处拒之）。Host 带端口一起比，缺省端口（80/443 隐式）按裸
    host 名放宽。"""
    host = (request.headers.get("Host") or "").strip().lower()
    if not host:
        return False
    if host in _LOOPBACK_HOSTS:
        return True
    # host:port 形态——按最后一个冒号切端口（IPv6 已被 [::1]:p 覆盖）
    name, _, port = host.rpartition(":")
    return port.isdigit() and name in _LOOPBACK_HOSTS


def token_ok(request) -> bool:
    """校验请求令牌：Bearer 头 / X-Token 头 / ?token= 三选一。

    未配置 token（config.API_TOKEN 为空）= 仅 loopback 部署（start_api
    已校验），此时仍校验 Host 头 ∈ loopback——挡 CSRF / DNS rebinding，
    浏览器跨站请求伪造不出 loopback 之外的合法 Host。比较用非常量时间
    hmac.compare_digest——本地方案不严格需要，顺手做对。
    """
    token = config.API_TOKEN
    if not token:
        return _host_allowed_loopback(request)
    auth = request.headers.get("Authorization", "")
    supplied = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if not supplied:
        supplied = request.headers.get("X-Token", "")
    if not supplied:
        supplied = request.query.get("token", "")
    return hmac.compare_digest(supplied, token)


@web.middleware
async def _auth_middleware(request, handler):
    if request.path in _PWA_PUBLIC_PATHS:
        pass                                   # PWA 静态资产免鉴权
    elif not token_ok(request):
        # 临时诊断（2026-11-07 壳 app 401 排查）：看清请求形态
        logger.warning(f"🔒 401 {request.method} {request.path} "
                       f"remote={request.remote} "
                       f"query_token={bool(request.query.get('token'))} "
                       f"bearer={bool(request.headers.get('Authorization', '').startswith('Bearer '))} "
                       f"xtoken={bool(request.headers.get('X-Token'))}")
        return web.json_response(
            {"ok": False, "error": "令牌无效或缺失（Authorization: Bearer / "
                                   "X-Token 头 / ?token= 任选其一）"},
            status=401,
        )
    resp = await handler(request)
    # 访问留痕（2026-11-01 排查手机端可达性）：来源 IP + XFF（Funnel 代理
    # 会带手机出口 IP）+ 路径 + 状态码——用户「打不开」时一眼看出请求
    # 是否到达、卡在哪一层
    # 安全（2026-11-05 审计）：不记 path_qs——?token= 查询通道的令牌会
    # 明文落日志；只记 path，query 是否带 token 一律不体现
    logger.info(f"🌐 API {request.method} {request.path} ← {request.remote}"
                f" xff={request.headers.get('X-Forwarded-For', '-')} → {resp.status}")
    return resp


def _json(data, status=200):
    return web.json_response(data, status=status)


def _in_flight_rows() -> list:
    """在途下载快照（download.register_download 的登记簿，只读）。"""
    rows = []
    for info in (state.ACTIVE_DOWNLOADS or {}).values():
        rows.append({
            "name": info.get("filename") or info.get("label") or "",
            "percent": info.get("percent"),
            "downloaded": info.get("downloaded") or 0,
            "total": info.get("total"),
        })
    return rows


def _task_rows(records, state_label) -> list:
    """队列/待重试记录 → 看板行（字段取自入队即快照的 final_name 等）。"""
    rows = []
    for r in records or []:
        rows.append({
            "id": str(r.get("id") or "")[:8],
            "name": r.get("final_name") or r.get("url") or "(未知名)",
            "kind": r.get("kind"),
            "platform": r.get("platform") or r.get("source") or "",
            "state": state_label,
            "attempts": r.get("attempts") or 0,
        })
    return rows


def build_status() -> dict:
    """总览快照：纯只读，任何 state 缺席（如单测未跑 main）都安全降级。"""
    q = state.QUEUE or {}
    tasks = q.get("tasks") or []
    retry = q.get("retry") or []
    try:
        s = stats.rebuild_stats(stats.load_events(), days=1)
    except Exception as e:  # DB/事件流异常 → 台账回落关键词口径
        logger.warning(f"API 台账重建失败，回落关键词口径：{e}")
        s = stats.collect_stats(1)
    try:
        disk_free_gb = round(shutil.disk_usage(config.DOWNLOAD_DIR).free
                             / 1024 ** 3, 1)
    except Exception:
        disk_free_gb = None
    try:
        from . import bili_dl as _bili
        bili = dict(getattr(_bili, "_BILI_RUNNING", {}) or {})
        bili["pending"] = _bili.bili_pending_list()   # DB 队列 PENDING 行
    except Exception:
        bili = {}
    try:
        from . import chrome_client as _cc
        chrome_progress = _cc.read_progress()
    except Exception:
        chrome_progress = None
    return {
        "ok": True,
        "uptime_s": int(time.time() - _STARTED_AT),
        "save_folder": config.DOWNLOAD_DIR,
        "rate": _recent_rate(),
        "db_ready": bool(state.RUNTIME_DB_READY),
        "android": config.IS_ANDROID,
        "concurrency": state.DOWNLOAD_CONCURRENCY,
        "toggles": {
            "dedup": bool(getattr(state, "DEDUP_ENABLED", False)),
            "listen": bool(getattr(state, "LISTEN_ENABLED", False)),
        },
        "bili": bili or None,
        "chrome_progress": chrome_progress,
        "stats": {
            "in_flight": len(state.ACTIVE_DOWNLOADS or {}),
            "queued": len(tasks),
            "retry": len(retry),
            "done_today": s.get("success", s.get("success_count", 0)),
            "bytes_today": s.get("success_bytes", 0),
            "failed_today": s.get("failed_final", 0),
            "dedup_hit_today": s.get("dedup_hit", 0),
        },
        "in_flight": _in_flight_rows(),
        "queue": _task_rows(tasks, "排队"),
        "retry": _task_rows(retry, "待重试"),
        "disk_free_gb": disk_free_gb,
    }


async def dispatch_link_text(text: str, surface: str = "api",
                             request_id: str = None) -> dict:
    """把一段含链接的文本按平台分流——语义与「往收藏夹发这条消息」一致。

    分流顺序与每路参数构造的唯一实现在 ``routing``（收藏夹 / bot 对话 /
    看板三入口共用）；本函数只做 API 特有的交付：JSON 回执与种子任务
    结果通知。链接日志由 ``routing.execute`` 统一写（surface 标明入口）。

    ``request_id``（2026-11-09）：可选的 32 位十六进制幂等键。同一 id
    首次执行后结果落 api_requests；重放直接返回首次结果并带
    ``duplicate=True``，绝不再次执行分流。客户端转屏重提、Telegram/HTTP
    双通道回落、离线 spool 补投都靠它去重。没有 id 时行为与旧版一致。
    幂等台账不可用时按无幂等继续——它只是防重复，不是任务真相源。
    """
    text = str(text or "").strip()
    rid = _clean_request_id(request_id)
    rdb = None
    claimed = False
    if rid:
        try:
            from . import runtime_db as rdb
            claim_state, stored = rdb.api_request_claim(rid)
            if claim_state == "done":
                out = dict(stored or {"ok": True, "routed": None,
                                      "message": ""})
                out["duplicate"] = True
                base_msg = str(out.get("message") or "").strip()
                out["message"] = ("♻️ 重复提交已忽略（首次已受理）"
                                  + (f"：{base_msg}" if base_msg else ""))
                logger.info(f"♻️ 分享重复提交已忽略：{rid[:8]}")
                return out
            if claim_state == "processing":
                logger.info(f"⏳ 分享正在处理中（重复提交已忽略）：{rid[:8]}")
                return {"ok": True, "routed": None, "duplicate": True,
                        "message": "⏳ 该分享正在处理中（重复提交已忽略）"}
            claimed = True
        except Exception as e:
            logger.warning(f"分享幂等台账不可用（按无幂等继续）：{e}")
            rdb = None
            claimed = False

    try:
        result = await _dispatch_link_text_inner(text, surface)
    except BaseException:
        # 含 CancelledError：handle_enqueue / handle_app_relay 的 wait_for
        # 超时会取消本协程。必须释放认领，否则 300s 内同一 id 的重放
        # 会读到 PROCESSING 被当成「正在处理」而丢掉，第一次却永远没完成。
        if claimed and rdb is not None:
            try:
                rdb.api_request_release(rid)
            except Exception as e:
                logger.warning(f"分享幂等台账释放失败（忽略）：{e}")
        raise

    if claimed and rdb is not None:
        try:
            if result.get("ok"):
                rdb.api_request_complete(rid, result)
            else:
                # 失败结果不缓存：重放允许重新执行（例如解析 bot 恢复后）
                rdb.api_request_release(rid)
        except Exception as e:
            logger.warning(f"分享幂等台账写回失败（忽略）：{e}")
    return result


async def _dispatch_link_text_inner(text: str, surface: str) -> dict:
    """真正的分流执行（不含幂等包装；供 ``dispatch_link_text`` 调用）。"""
    decision = routing.classify(text, allow=routing.SURFACE_API,
                                message=_ShimMessage(text))
    if not decision.handled:
        return {"ok": False, "routed": None,
                "message": "没有识别到可处理的链接"}

    event = _HeadlessEvent() if decision.kind == "iwara" else None
    res = await routing.execute(decision, event=event, surface=surface)
    kind = res.kind
    message = res.message

    if kind in ("iwara", "iwara_dup") and event is not None:
        message = "\n".join(event.replies) or message
    elif kind in ("douyin", "instagram"):
        message = ("链接已受理：本地解析成功会自动入队下载，"
                   "失败已转解析 bot——进度看 Telegram 控制面板通知")
    elif kind == "torrent":
        try:
            await notify.notify_user(message)
        except Exception as e:
            logger.warning(f"API 种子离线结果通知失败：{e}")

    return {"ok": True, "routed": kind, "message": message}


# ── HTTP 处理器 ──────────────────────────────────────────────

async def handle_dashboard(request):
    try:
        html = _DASHBOARD_PATH.read_text(encoding="utf-8")
    except OSError as e:
        return _json({"ok": False, "error": f"看板页面缺失：{e}"}, 500)
    # no-store：看板随仓库更新，WebView（尤其壳 app 内）绝不许拿旧缓存——
    # 否则「后端改了 UI、手机还显示旧版」会变成玄学问题
    return web.Response(text=html, content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "no-store"})


_EXTRACT_STATE = {"running": False, "rounds": [], "last": "", "started": 0}


def _extract_run_blocking(target):
    """同步跑一轮解压并记录（后台线程执行）。"""
    from . import extract_dir
    _EXTRACT_STATE["running"] = True
    try:
        lines, st = extract_dir.extract_directory(target)
        _EXTRACT_STATE["rounds"].append(
            {"target": target, "st": st, "lines": lines[-40:],
             "ts": time.strftime("%m-%d %H:%M")})
        _EXTRACT_STATE["last"] = (
            f"成功 {st['done']} | 失败 {st['fail']} | 跳过 {st['skip']}")
    except Exception as e:
        _EXTRACT_STATE["last"] = f"异常: {e}"
    finally:
        _EXTRACT_STATE["running"] = False


def _extract_run_targets(targets, dry=False):
    """解压执行体：目录 → 整轮；单个压缩包文件 → 单包解压。

    混合列表按序跑完（2026-11-07 补：此前只接受目录，用户解压单个
    文件 2#123.zip 报「目录不存在或越界」）。"""
    from pathlib import Path
    from . import extract_dir
    _EXTRACT_STATE["running"] = True
    try:
        all_lines = []
        total = {"done": 0, "fail": 0, "skip": 0}
        for t in targets:
            real = _fs_safe(t)
            if real is None:
                continue
            try:
                if os.path.isdir(real):
                    lines, st = extract_dir.extract_directory(
                        real, dry=dry)
                    all_lines += lines
                    for k in total:
                        total[k] += st[k]
                elif os.path.isfile(real) and real.lower().endswith(
                        (".zip", ".rar", ".7z", ".tar.gz", ".tgz", ".tar")):
                    action, msg = extract_dir.process_archive(
                        Path(real), dry=dry,
                        extra_pwd=os.path.basename(
                            os.path.dirname(real)))
                    all_lines.append(
                        f"{action}: {os.path.basename(real)}  {msg}")
                    key = {"done": "done", "fail": "fail",
                           "skip": "skip"}.get(action, "skip")
                    total[key] += 1
                else:
                    all_lines.append(f"skip: {os.path.basename(real)}"
                                     "（不是压缩包也不是目录）")
                    total["skip"] += 1
            except Exception as e:
                all_lines.append(f"fail: {os.path.basename(real)}  "
                                 f"{type(e).__name__}: {e}")
                total["fail"] += 1
        _EXTRACT_STATE["rounds"].append(
            {"target": ", ".join(os.path.basename(t) for t in targets)[:80],
             "st": total, "lines": all_lines[-40:],
             "ts": time.strftime("%m-%d %H:%M"), "dry": dry})
        _EXTRACT_STATE["last"] = (
            f"成功 {total['done']} | 失败 {total['fail']} | "
            f"跳过 {total['skip']}")
    except Exception as e:
        _EXTRACT_STATE["last"] = f"异常: {e}"
    finally:
        _EXTRACT_STATE["running"] = False


async def handle_extract_state(request):
    """解压卡：当前状态 + 最近轮次结果。"""
    s = dict(_EXTRACT_STATE)
    s["running"] = bool(_EXTRACT_STATE["running"])
    s["ok"] = True
    return _json(s)


# ── 文件管理器（数据盘 V1；所有操作 realpath 限定在 DATA_ROOT 下）──
def _fs_safe(p):
    """路径规范化并限定在数据盘内；越界返回 None。"""
    from . import config as _cfg
    base = os.path.realpath(_cfg.DATA_ROOT)
    real = os.path.realpath(str(p or ""))
    if real == base or real.startswith(base + os.sep):
        return real
    return None


_FS_OP = {"running": False, "msg": "", "error": ""}


def _fs_paste_blocking(action, srcs, dest):
    """粘贴执行体（后台线程）：同盘 move=瞬时改名；copy 大文件长任务。"""
    import shutil
    _FS_OP.update(running=True, msg="", error="")
    try:
        for src in srcs:
            name = os.path.basename(src.rstrip("/"))
            dst = os.path.join(dest, name)
            n = 1
            while os.path.exists(dst):
                stem, ext = os.path.splitext(name)
                dst = os.path.join(dest, f"{stem} ({n}){ext}")
                n += 1
            _FS_OP["msg"] = f"{'移动' if action == 'move' else '复制'} {name}"
            if action == "move":
                shutil.move(src, dst)
            elif os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
        _FS_OP["msg"] = "完成"
    except Exception as e:
        _FS_OP["error"] = f"{type(e).__name__}: {e}"
    finally:
        _FS_OP["running"] = False


async def handle_fs_list(request):
    from . import config as _cfg
    real = _fs_safe(request.query.get("path", _cfg.DATA_ROOT))
    if real is None:
        return _json({"ok": False, "error": "路径必须在数据盘内"}, 400)
    if not os.path.isdir(real):
        return _json({"ok": False, "error": "目录不存在"}, 400)
    base = os.path.realpath(_cfg.DATA_ROOT)
    items = []
    try:
        for name in sorted(os.listdir(real)):
            if name.startswith("."):
                continue   # 隐藏文件不进列表（.DS_Store 等噪声）
            full = os.path.join(real, name)
            try:
                st = os.stat(full)
                items.append({"name": name,
                              "is_dir": os.path.isdir(full),
                              "size": 0 if os.path.isdir(full)
                                      else st.st_size,
                              "mtime": int(st.st_mtime)})
            except OSError:
                continue
    except OSError as e:
        return _json({"ok": False, "error": str(e)[:80]}, 500)
    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return _json({"ok": True, "path": real,
                  "parent": os.path.dirname(real) if real != base else "",
                  "items": items})


async def handle_fs_rename(request):
    try:
        b = await request.json()
    except Exception:
        b = {}
    real = _fs_safe(b.get("path"))
    new_name = str(b.get("new_name") or "").strip()
    if real is None or not new_name or "/" in new_name:
        return _json({"ok": False, "error": "参数非法（路径越界或新名含 /）"},
                     400)
    dst = os.path.join(os.path.dirname(real), new_name)
    if os.path.exists(dst):
        return _json({"ok": False, "error": "同名目标已存在"}, 400)
    try:
        os.rename(real, dst)
    except OSError as e:
        return _json({"ok": False, "error": str(e)[:100]}, 500)
    return _json({"ok": True, "path": dst})


async def handle_fs_mkdir(request):
    """新建文件夹（数据盘内；同名拒绝）。"""
    try:
        b = await request.json()
    except Exception:
        b = {}
    dest = _fs_safe(b.get("dest"))
    name = str(b.get("name") or "").strip()
    if dest is None or not name or "/" in name:
        return _json({"ok": False, "error": "参数非法"}, 400)
    dst = os.path.join(dest, name)
    if os.path.exists(dst):
        return _json({"ok": False, "error": "同名已存在"}, 400)
    try:
        os.makedirs(dst)
    except OSError as e:
        return _json({"ok": False, "error": str(e)[:100]}, 500)
    return _json({"ok": True, "path": dst})


async def handle_fs_touch(request):
    """新建空文件（数据盘内；同名拒绝）。"""
    try:
        b = await request.json()
    except Exception:
        b = {}
    dest = _fs_safe(b.get("dest"))
    name = str(b.get("name") or "").strip()
    if dest is None or not name or "/" in name:
        return _json({"ok": False, "error": "参数非法"}, 400)
    dst = os.path.join(dest, name)
    if os.path.exists(dst):
        return _json({"ok": False, "error": "同名已存在"}, 400)
    try:
        open(dst, "a", encoding="utf-8").close()
    except OSError as e:
        return _json({"ok": False, "error": str(e)[:100]}, 500)
    return _json({"ok": True, "path": dst})


async def handle_fs_delete(request):
    import shutil
    try:
        b = await request.json()
    except Exception:
        b = {}
    n = err = 0
    for p in (b.get("paths") or []):
        real = _fs_safe(p)
        if real is None or real == os.path.realpath(
                __import__("tg_userbot.config", fromlist=["DATA_ROOT"])
                .DATA_ROOT):
            err = err or "路径越界"
            continue
        try:
            if os.path.isdir(real):
                shutil.rmtree(real)
            else:
                os.remove(real)
            n += 1
        except OSError as e:
            err = str(e)[:100]
    return _json({"ok": n > 0 and not err, "deleted": n,
                  "error": err or ""})


async def handle_fs_paste(request):
    try:
        b = await request.json()
    except Exception:
        b = {}
    dest = _fs_safe(b.get("dest"))
    srcs = [s for s in (_fs_safe(x) for x in (b.get("srcs") or [])) if s]
    action = "move" if b.get("move") else "copy"
    if dest is None or not srcs:
        return _json({"ok": False, "error": "参数非法（越界/无源）"}, 400)
    if _FS_OP["running"]:
        return _json({"ok": False, "error": "上一个粘贴任务还在跑"}, 409)
    spawn(asyncio.to_thread(_fs_paste_blocking, action, srcs, dest),
          "文件粘贴")
    return _json({"ok": True, "message":
                  f"{'移动' if action == 'move' else '复制'} "
                  f"{len(srcs)} 项已启动"})


async def handle_fs_extract(request):
    """文件管理器解压：V1 内任意目录（复用解压卡的状态与管线）。"""
    try:
        b = await request.json()
    except Exception:
        b = {}
    targets = b.get("targets") or (
        [b.get("target")] if b.get("target") else [])
    safe = [t for t in (_fs_safe(x) for x in targets) if t]
    if not safe:
        return _json({"ok": False,
                      "error": "目标不存在或越界（支持目录与压缩包文件）"},
                     400)
    if _EXTRACT_STATE["running"]:
        return _json({"ok": False, "error": "已有一轮解压在跑"}, 409)
    _EXTRACT_STATE["running"] = True
    spawn(asyncio.to_thread(_extract_run_targets, safe, False),
          "文件管理器解压")
    return _json({"ok": True, "message":
                  f"解压已启动（{len(safe)} 个目标；状态看解压卡）"})


async def handle_fs_op(request):
    return _json({"ok": True, **_FS_OP})


class _FakeReq:
    """batch 内部调 handler 的最小 request 替身（GET；只有 .query）。"""
    def __init__(self, q):
        self.query = q


async def handle_batch(request):
    """看板数据聚合端点：一次请求带回全部子页数据。

    手机经 ts.net 每 RTT 50-200ms，看板一轮 12 个请求 = 1-2.4s 纯网络
    等待——合并为 1 个请求后 ≈ 1×RTT。paths=逗号分隔的 GET 端点列表
    （含 query）。未知/出错的端点单独降级，不影响其它。"""
    from urllib.parse import urlparse, parse_qsl
    spec = request.query.get("paths", "")
    paths = [s for s in spec.split(",") if s][:24]
    return _json({"ok": True, "data": await _batch_data(paths)})


_BATCH_HANDLERS = {
    "/api/status": lambda r: handle_status(r),
    "/api/queues": lambda r: handle_queues(r),
    "/api/chrome": lambda r: handle_chrome(r),
    "/api/backup": lambda r: handle_backup(r),
    "/api/pd/jobs": lambda r: handle_pd_jobs(r),
    "/api/paw_backfill": lambda r: handle_paw_backfill(r),
    "/api/paw_schedule": lambda r: handle_paw_schedule(r),
    "/api/history": lambda r: handle_history(r),
    "/api/pastes": lambda r: handle_pastes(r),
    "/api/submissions": lambda r: handle_pastes(r),
    "/api/links": lambda r: handle_links(r),
    "/api/commands": lambda r: handle_commands_registry(r),
}


_SSE_CLIENTS = set()


async def handle_sse(request):
    """SSE 推送流（2026-11-07 批2）：手机保持一条连接，服务器每 2s 推
    全量 batch 数据——页面零轮询、准实时；连接断开自动清理。"""
    resp = web.StreamResponse(
        status=200,
        headers={"Content-Type": "text/event-stream",
                 "Cache-Control": "no-store",
                 "Connection": "keep-alive"})
    await resp.prepare(request)
    _SSE_CLIENTS.add(resp)
    try:
        while True:
            data = {"ok": True, "data": await _batch_data(
                ["/api/status", "/api/queues", "/api/chrome", "/api/backup",
                 "/api/pd/jobs", "/api/paw_backfill", "/api/paw_schedule",
                 "/api/history?n=30", "/api/submissions?n=30", "/api/links",
                 "/api/commands"])}
            await resp.write(
                f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
            await asyncio.sleep(2)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        _SSE_CLIENTS.discard(resp)
    return resp


async def _batch_data(paths):
    from urllib.parse import urlparse, parse_qsl
    out = {}
    for p in paths:
        u = urlparse(p)
        handler = _BATCH_HANDLERS.get(u.path)
        if handler is None:
            continue
        q = dict(parse_qsl(u.query))
        try:
            resp = await handler(_FakeReq(q))
            out[p] = json.loads(resp.text)
        except Exception as e:
            out[p] = {"ok": False, "error": str(e)[:80]}
    return out


async def handle_version(request):
    """看板/后端版本一致性核对（SW 缓存错版本时肉眼可判）。"""
    import subprocess
    ver = "v2.24"
    try:
        d = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if d.returncode == 0:
            ver += " (" + d.stdout.strip()[:12] + ")"
    except Exception:
        pass
    return _json({"ok": True, "version": ver})


def _recent_rate(window_s=60):
    """近 N 秒成功字节速率（task_events SUCCESS 差分）→ MB/s 字符串。"""
    try:
        import time as _t
        import json as _json
        from . import config as _cfg
        now = _t.time()
        total = 0
        path = os.path.join(_cfg.RUNTIME_DIR, "task_events.jsonl")
        if not os.path.exists(path):
            return ""
        # 文件会涨到几十 MB——只读尾部 512KB（60s 窗口的事件必在其中）
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 524288))
            tail = f.read().decode("utf-8", "replace")
        for line in tail.splitlines():
                try:
                    e = _json.loads(line)
                except Exception:
                    continue
                if e.get("event") != "SUCCESS":
                    continue
                ts = e.get("ts") or 0
                if now - ts <= window_s:
                    total += int(e.get("bytes") or 0)
        if total <= 0:
            return ""
        mbps = total / window_s / 1024 / 1024
        return f" | {mbps:.1f} MB/s" if mbps >= 0.1 else ""
    except Exception:
        return ""


async def handle_status(request):
    return _json(build_status())


async def handle_enqueue(request):
    try:
        body = await request.json()
    except Exception:
        return _json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
    text = str(body.get("text") or body.get("url") or body.get("link")
               or "").strip()
    if not text:
        return _json({"ok": False,
                      "error": "缺少 text 字段（分享的原文即可，无需自行截取链接）"},
                     400)
    request_id = _clean_request_id(body.get("request_id"))
    try:
        result = await asyncio.wait_for(
            dispatch_link_text(text, request_id=request_id),
            timeout=_DISPATCH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return _json({"ok": False,
                      "error": f"处理超时（>{_DISPATCH_TIMEOUT_SECONDS}s），"
                               "链接可能已部分受理，请看控制面板通知"}, 504)
    except Exception as e:
        logger.exception(f"API 链接分流异常：{e}")
        return _json({"ok": False, "error": "处理失败（详见服务端日志）"}, 500)
    return _json(result, 200 if result.get("ok") else 400)


async def handle_action(request):
    """动作白名单入口：全部薄封装既有服务函数（与 bot 菜单/命令同层）。

    带参数的动作（thread/chrome_cancel）从 JSON body 取 args；无参数动作
    忽略 body。任何服务函数的异常都收口成 500 JSON，绝不 500 裸 traceback。
    """
    name = request.match_info["name"]
    if name not in ACTION_WHITELIST:
        return _json({"ok": False,
                      "error": f"未知动作：{name}（白名单："
                               f"{'、'.join(ACTION_WHITELIST)}）"}, 404)
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}

    from . import queue as _queue
    from . import cleanup as _cleanup

    try:
        if name == "clean":
            n = _cleanup.clean_temp_files()
            return _json({"ok": True, "routed": "clean",
                          "message": f"🧹 已清理 {n} 个临时文件" if n
                          else "没有需要清理的临时文件"})
        if name == "retry_all":
            n, over = _queue.retry_all()
            return _json({"ok": True, "routed": "retry_all",
                          "message": f"🔁 已重放 {n} 条待重试任务"
                          + (f"（{over} 条在途跳过）" if over else "")})
        if name == "wl_scan":
            from . import wl_scan
            totals = await wl_scan.scan_all(manual=True)
            return _json({"ok": True, "routed": "wl_scan",
                          "message": wl_scan.summary_text(totals)})
        if name == "listen_scan":
            from . import listener
            totals = await listener.scan_all(manual=True)
            return _json({"ok": True, "routed": "listen_scan",
                          "message": f"📡 标签监听扫描完成：{totals}"})

        # ── 开关类（设置 tab）────────────────────────────────
        if name in ("dedup_on", "dedup_off"):
            from . import dedup
            msg = dedup.set_enabled(name == "dedup_on")
            return _json({"ok": True, "routed": name, "message": msg})
        if name in ("listen_on", "listen_off"):
            from . import listener
            listener.set_enabled(name == "listen_on")
            return _json({"ok": True, "routed": name,
                          "message": f"📡 标签监听已"
                                     f"{'开启' if name == 'listen_on' else '关闭'}"})
        if name == "thread":
            from . import thread
            ok, msg = thread.apply_thread_limit(
                str(body.get("n") or "").strip())
            return _json({"ok": bool(ok), "routed": "thread", "message": msg})

        # ── Chrome Agent ─────────────────────────────────────
        if name == "chrome_start":
            from . import chrome_client
            return _json({"ok": True, "routed": "chrome_start",
                          "message": await chrome_client.start_view()})
        if name == "chrome_stop":
            from . import chrome_client
            stopped = await chrome_client.stop_view()
            return _json({"ok": True, "routed": "chrome_stop",
                          "message": "🤖 Chrome Agent 已停止（Chrome 保持运行）"
                          if stopped else "ℹ️ Chrome Agent 未在运行"})
        if name == "chrome_cancel":
            from . import chrome_client
            task_id = str(body.get("id") or body.get("arg") or "").strip()
            if not task_id:
                return _json({"ok": False,
                              "error": "缺少要取消的任务 id/序号"}, 400)
            ok, msg = chrome_client.request_cancel(task_id)
            return _json({"ok": bool(ok), "routed": "chrome_cancel",
                          "message": msg})

        # ── Pawchive / B站 / CD2 / 健康 ─────────────────────
        if name == "paw_pause":
            from . import pawchive
            return _json({"ok": True, "routed": "paw_pause",
                          "message": pawchive.pause_reply()})
        if name == "paw_resume":
            from . import pawchive
            return _json({"ok": True, "routed": "paw_resume",
                          "message": pawchive.resume_reply()})
        if name == "paw_status":
            from . import pawchive
            return _json({"ok": True, "routed": "paw_status",
                          "message": pawchive.status_text()})
        if name == "bili_status":
            from . import bili_dl
            return _json({"ok": True, "routed": "bili_status",
                          "message": bili_dl.status_text()})
        if name == "cd2_start":
            from . import cd2
            return _json({"ok": True, "routed": "cd2_start",
                          "message": await cd2.cd2_start_or_status()})
        if name == "cd2_stop":
            from . import cd2
            return _json({"ok": True, "routed": "cd2_stop",
                          "message": await cd2.cd2_stop_or_status()})
        if name == "health":
            from . import health
            return _json({"ok": True, "routed": "health",
                          "message": health.render_health()})
        if name == "now":
            from . import health
            return _json({"ok": True, "routed": "now",
                          "message": health.render_now()})
    except Exception as e:
        logger.exception(f"API 动作 {name} 执行异常：{e}")
        return _json({"ok": False,
                      "error": "动作执行失败（详见服务端日志）"}, 500)
    # 白名单新增了键却没写分支时绝不返回 None（aiohttp 会 500）
    return _json({"ok": False, "error": f"动作 {name} 尚未实现"}, 500)


async def handle_queues(request):
    """全部下载队列聚合摘要（2026-11-05 统一队列·读侧 union）。

    主媒体队列走内存 state.QUEUE；各 worker 域走 runtime_db（bili/wnacg/
    asmr/extract）。任一域读取失败只降级该域（error 字段），不影响其它。
    """
    out = []

    # ① 主媒体队列（内存）
    try:
        from . import state as _state
        async with _state.QUEUE_LOCK:
            tasks = list(_state.QUEUE.get("tasks", []))
            retry = list(_state.QUEUE.get("retry", []))
        out.append({"name": "媒体队列", "emoji": "📥",
                    "pending": len(tasks), "retry": len(retry),
                    "running": len([r for r in tasks
                                    if r.get("status") == "downloading"]),
                    "items": [f"{r.get('platform', '?')}:{str(r.get('name') or r.get('url') or '')[:36]}"
                              for r in tasks[:5]]})
    except Exception as e:
        out.append({"name": "媒体队列", "emoji": "📥", "error": str(e)[:80]})

    # ② bili（DB）
    try:
        from . import runtime_db
        d = runtime_db.BILI_TASKS
        out.append({"name": "B站", "emoji": "📺",
                    "pending": d.count(status="PENDING"),
                    "running": d.count(status="PROCESSING"),
                    "failed": d.count(status="FAILED"),
                    "items": [(r.get("label") or r["url"])[:36]
                              for r in d.list(status="PENDING", limit=5)]})
    except Exception as e:
        out.append({"name": "B站", "emoji": "📺", "error": str(e)[:80]})

    # ③④ wnacg / asmr（DB 域）
    for name, emoji, fn in (("wnacg 漫画", "📖", "list_wnacg_tasks"),
                            ("asmr 音声", "🎧", "list_asmr_tasks")):
        try:
            from . import runtime_db
            rows = getattr(runtime_db, fn)(limit=200)
            from collections import Counter
            counts = Counter(r["status"] for r in rows)
            # items 只放真正待办/在途的条目（2026-11-05 owner 反馈：
            # 历史 DONE/FAILED 的标题混进卡片里看着像「正在进行中」）
            active = [r for r in rows
                      if r["status"] in ("PENDING", "PROCESSING")]
            out.append({"name": name, "emoji": emoji,
                        "pending": counts.get("PENDING", 0),
                        "running": counts.get("PROCESSING", 0),
                        "failed": counts.get("FAILED", 0),
                        "items": [str(r.get("title") or r.get("aid")
                                      or r.get("rj"))[:36].replace(
                                      "&#124;", "|")
                                  for r in active[:5]]})
        except Exception as e:
            out.append({"name": name, "emoji": emoji, "error": str(e)[:80]})

    # ⑤ 标签监听（listener_tasks；2026-11-10 全链路可见性补齐）
    try:
        from . import runtime_db
        rows = runtime_db.list_listener_tasks(limit=200)
        from collections import Counter
        counts = Counter(r["status"] for r in rows)
        active = [r for r in rows
                  if r["status"] in ("PENDING", "PROCESSING", "FORWARDED")]
        out.append({
            "name": "标签监听", "emoji": "📡",
            "pending": counts.get("PENDING", 0),
            "running": (counts.get("PROCESSING", 0)
                        + counts.get("FORWARDED", 0)),
            "failed": counts.get("FAILED", 0),
            "items": [f"#{r['id']} {r.get('target_type', '?')}→"
                      f"{r.get('target_chat_id') or '收藏'}"
                      f"（第 {r.get('attempts', 0)} 次）"
                      for r in active[:5]]})
    except Exception as e:
        out.append({"name": "标签监听", "emoji": "📡", "error": str(e)[:80]})

    # ⑥ pawchive（posts 域）
    try:
        from . import runtime_db
        rows = runtime_db.list_pawchive_posts(limit=200)
        from collections import Counter
        counts = Counter(r["status"] for r in rows)
        active = [r for r in rows if r["status"] in ("PENDING", "PROCESSING")]
        out.append({
            "name": "pawchive", "emoji": "🐾",
            "pending": counts.get("PENDING", 0),
            "running": counts.get("PROCESSING", 0),
            "failed": counts.get("FAILED", 0),
            "items": [str(r.get("title") or r.get("post_id")
                          or "?")[:36] for r in active[:5]]})
    except Exception as e:
        out.append({"name": "pawchive", "emoji": "🐾",
                    "error": str(e)[:80]})

    problems = await _queue_problems()
    return _json({"ok": True, "queues": out, "problems": problems})


# ── 渠道队列详情（2026-11-10 面板重构）────────────────────────
# 每渠道独立视图：正在执行 / 排队中 / 失败待重试（带原因）/ 已完成。
# 条目统一形状 {label, sub, attempts, error, when, next_in}，前端不认识
# 渠道差异。done 只取最近 15 条——队列页要的是"现在怎么样"，不是全史。

_QD_LIMIT = 50
_QD_DONE_LIMIT = 15


def _qd_item(label=None, sub=None, attempts=None, error=None, when=None,
             next_in=None):
    return {"label": str(label or "?")[:60], "sub": sub,
            "attempts": attempts,
            "error": (str(error)[:120] if error else None),
            "when": when, "next_in": next_in}


def _qd_when(ts):
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _qd_payload_label(payload):
    if isinstance(payload, dict):
        return payload.get("label")
    try:
        return json.loads(payload or "{}").get("label")
    except (ValueError, TypeError):
        return None


def _qd_taskdomain(domain, label_key):
    """TaskDomain 三兄弟（bili/x/pixiv）通用：list 全量按 status 分桶。

    list 按 id 升序（老→新）→ done 取尾部（最新完成的在最后）。"""
    out = {"running": [], "pending": [], "failed": [], "done": []}
    for status, bucket in (("PROCESSING", "running"), ("PENDING", "pending"),
                           ("FAILED", "failed")):
        for r in domain.list(status=status, limit=_QD_LIMIT):
            out[bucket].append(_qd_item(
                r.get("label") or r.get("url") or r.get("target"),
                attempts=r.get("attempts"),
                error=r.get("error"),
                next_in=(max(0, int(r["next_retry_at"] - time.time()))
                         if r.get("next_retry_at") else None)))
    done_rows = domain.list(status="DONE", limit=200)
    for r in done_rows[-_QD_DONE_LIMIT:][::-1]:
        out["done"].append(_qd_item(
            r.get("label") or r.get("url") or r.get("target"),
            when=_qd_when(r.get("updated_at"))))
    return out


async def _qd_media():
    from . import state as _state
    out = {"running": [], "pending": [], "failed": [], "done": []}
    async with _state.QUEUE_LOCK:
        tasks = list(_state.QUEUE.get("tasks", []))
        retry = list(_state.QUEUE.get("retry", []))
    executing = set(_state.EXECUTING)
    for r in tasks:
        item = _qd_item(r.get("name") or r.get("final_name") or r.get("url"),
                        sub=r.get("platform") or r.get("kind"),
                        attempts=r.get("attempts"))
        (out["running"] if r.get("id") in executing
         else out["pending"]).append(item)
    now = time.time()
    for r in retry[:_QD_LIMIT]:
        nra = r.get("next_retry_at")
        out["failed"].append(_qd_item(
            r.get("name") or r.get("url"), sub="待自动重放",
            attempts=r.get("attempts"),
            next_in=(max(0, int(nra - now)) if nra else None)))
    # recent_events 的 rec 是"payload 平铺"形状（label 在顶层，无 payload 键）
    for e in runtime_db_recent_events("SUCCESS", _QD_DONE_LIMIT):
        out["done"].append(_qd_item(
            e.get("label") or e.get("id"), when=_qd_when(e.get("ts"))))
    for e in runtime_db_recent_events("FAILED", _QD_DONE_LIMIT):
        out["failed"].append(_qd_item(
            e.get("label") or e.get("id"),
            sub="终态失败（/retry 可重投）",
            when=_qd_when(e.get("ts"))))
    return out


def runtime_db_recent_events(ev, limit):
    """download_events 按 ev 取最近 N 条（payload 反序列化）。"""
    from . import runtime_db
    return runtime_db.recent_events(ev, limit)


async def _qd_wnacg():
    from . import runtime_db
    out = {"running": [], "pending": [], "failed": [], "done": []}
    for status, bucket in (("PROCESSING", "running"), ("PENDING", "pending"),
                           ("FAILED", "failed")):
        for r in runtime_db.list_wnacg_tasks(status=status,
                                             limit=_QD_LIMIT):
            out[bucket].append(_qd_item(
                r.get("title") or f"aid {r.get('aid')}",
                attempts=r.get("attempts"), error=r.get("error"),
                next_in=(max(0, int(r["next_retry_at"] - time.time()))
                         if r.get("next_retry_at") else None)))
    done_rows = runtime_db.list_wnacg_tasks(status="DONE", limit=200)
    for r in done_rows[-_QD_DONE_LIMIT:][::-1]:
        out["done"].append(_qd_item(
            r.get("title") or f"aid {r.get('aid')}",
            when=_qd_when(r.get("updated_at"))))
    return out


async def _qd_asmr():
    from . import runtime_db
    out = {"running": [], "pending": [], "failed": [], "done": []}
    for status, bucket in (("PROCESSING", "running"), ("PENDING", "pending"),
                           ("FAILED", "failed")):
        for r in runtime_db.list_asmr_tasks(status=status,
                                            limit=_QD_LIMIT):
            out[bucket].append(_qd_item(
                r.get("title") or f"RJ {r.get('rj')}",
                attempts=r.get("attempts"), error=r.get("error"),
                next_in=(max(0, int(r["next_retry_at"] - time.time()))
                         if r.get("next_retry_at") else None)))
    done_rows = runtime_db.list_asmr_tasks(status="DONE", limit=200)
    for r in done_rows[-_QD_DONE_LIMIT:][::-1]:
        out["done"].append(_qd_item(
            r.get("title") or f"RJ {r.get('rj')}",
            when=_qd_when(r.get("updated_at"))))
    return out


async def _qd_extract():
    from . import runtime_db
    out = {"running": [], "pending": [], "failed": [], "done": []}
    for status, bucket in (("PROCESSING", "running"), ("PENDING", "pending"),
                           ("FAILED", "failed")):
        for r in runtime_db.list_extract_tasks(status=status,
                                               limit=_QD_LIMIT):
            out[bucket].append(_qd_item(
                r.get("src_path") or r.get("id"),
                attempts=r.get("attempts"), error=r.get("error")))
    done_rows = runtime_db.list_extract_tasks(status="COMPLETED", limit=200)
    for r in done_rows[-_QD_DONE_LIMIT:][::-1]:
        out["done"].append(_qd_item(
            r.get("src_path") or r.get("id"),
            when=_qd_when(r.get("updated_at"))))
    return out


async def _qd_pawchive():
    from . import runtime_db
    out = {"running": [], "pending": [], "failed": [], "done": []}
    for status, bucket in (("PROCESSING", "running"), ("PENDING", "pending"),
                           ("FAILED", "failed")):
        for r in runtime_db.list_pawchive_posts(status=status,
                                                limit=_QD_LIMIT):
            out[bucket].append(_qd_item(
                r.get("title") or f"post {r.get('post_id')}",
                attempts=r.get("attempts"), error=r.get("last_error")))
    for status in ("COMPLETED", "MANUAL"):
        done_rows = runtime_db.list_pawchive_posts(status=status, limit=200)
        for r in done_rows[-_QD_DONE_LIMIT:][::-1]:
            out["done"].append(_qd_item(
                r.get("title") or f"post {r.get('post_id')}",
                sub=status, when=_qd_when(r.get("updated_at"))))
    return out


async def _qd_listener():
    from . import runtime_db
    out = {"running": [], "pending": [], "failed": [], "done": []}
    for status, bucket in (("PROCESSING", "running"),
                           ("FORWARDED", "running"), ("PENDING", "pending"),
                           ("FAILED", "failed")):
        for t in runtime_db.list_listener_tasks(status=status,
                                                limit=_QD_LIMIT):
            sub = (f"#{t['id']} 第 {t.get('attempts', 0)} 次"
                   if bucket != "running" else
                   f"#{t['id']} 第 {t.get('attempts', 0)} 次"
                   + ("·对账补建中" if status == "FORWARDED" else ""))
            out[bucket].append(_qd_item(
                f"#{t['id']} {_task_label_of(t)}", sub=sub,
                attempts=t.get("attempts"), error=t.get("last_error"),
                next_in=(max(0, int(t["next_retry_at"] - time.time()))
                         if t.get("next_retry_at") else None)))
    done_rows = runtime_db.list_listener_tasks(status="SUCCESS", limit=200)
    for t in done_rows[-_QD_DONE_LIMIT:][::-1]:
        out["done"].append(_qd_item(
            f"#{t['id']} {_task_label_of(t)}",
            when=_qd_when(t.get("completed_at"))))
    return out


async def _qd_chrome():
    from . import chrome_agent
    from . import config as _config
    out = {"running": [], "pending": [], "failed": [], "done": []}
    tasks = chrome_agent.load_tasks(_config.CHROME_TASKS_FILE) or []
    buckets = {"RUNNING": "running", "PENDING": "pending",
               "RETRY_WAIT": "pending", "FAILED": "failed",
               "SUCCESS": "done"}
    for t in reversed(tasks):          # 新任务在前
        bucket = buckets.get(str(t.get("status")).upper())
        if bucket is None:
            continue
        item = _qd_item(t.get("filename") or t.get("url"),
                        sub=(str(t.get("status")) if bucket == "pending"
                             else None),
                        error=t.get("error"))
        if bucket == "done":
            item["when"] = _qd_when(t.get("updated_at"))
        if len(out[bucket]) >= _QD_LIMIT:
            continue
        out[bucket].append(item)
    return out


async def _qd_pd():
    from . import pd_dl
    out = {"running": [], "pending": [], "failed": [], "done": [],
           "note": None}
    try:
        jobs = (pd_dl.api_jobs() or {}).get("jobs") or {}
    except Exception as e:
        out["note"] = f"pd_daemon 不可达：{e}"
        return out
    for tag, j in list(jobs.items())[-_QD_LIMIT:]:
        item = _qd_item(tag, sub=j.get("dest"),
                        when=_qd_when(j.get("started")) if j.get("started")
                        else None)
        out["running" if j.get("alive") else "done"].append(item)
    return out


async def handle_queue_detail(request):
    """单渠道四段队列：?domain=media|bili|x|pixiv|wnacg|asmr|extract|
    pawchive|listener|chrome|pd。条目统一形状，前端零渠道知识。"""
    domain = request.query.get("domain", "media").strip()
    provider = _QUEUE_DETAIL_PROVIDER.get(domain)
    if provider is None:
        return _json({"ok": False,
                      "error": f"未知渠道：{domain}（支持："
                               f"{'、'.join(sorted(_QUEUE_DETAIL_PROVIDER))}）"},
                     400)
    emoji, name = _QUEUE_DETAIL_META[domain]
    try:
        buckets = await provider()
    except Exception as e:
        logger.exception(f"渠道队列详情读取失败（{domain}）：{e}")
        return _json({"ok": False, "domain": domain,
                      "error": "读取失败（详见服务端日志）"}, 500)
    return _json({"ok": True, "domain": domain, "emoji": emoji,
                  "name": name, **buckets})


_QUEUE_DETAIL_PROVIDER = {
    "media": _qd_media,
    "bili": lambda: _qd_taskdomain(_td_bili(), "url"),
    "x": lambda: _qd_taskdomain(_td_x(), "url"),
    "pixiv": lambda: _qd_taskdomain(_td_pixiv(), "url"),
    "wnacg": _qd_wnacg,
    "asmr": _qd_asmr,
    "extract": _qd_extract,
    "pawchive": _qd_pawchive,
    "listener": _qd_listener,
    "chrome": _qd_chrome,
    "pd": _qd_pd,
}
_QUEUE_DETAIL_META = {
    "media": ("📥", "媒体队列"),
    "bili": ("📺", "B站"),
    "x": ("🐦", "X"),
    "pixiv": ("🎨", "Pixiv"),
    "wnacg": ("📖", "wnacg"),
    "asmr": ("🎧", "asmr"),
    "extract": ("📦", "115 解压回传"),
    "pawchive": ("🐾", "pawchive"),
    "listener": ("📡", "标签监听"),
    "chrome": ("🌐", "Chrome"),
    "pd": ("⚡", "pixeldrain"),
}


def _td_bili():
    from . import runtime_db
    return runtime_db.BILI_TASKS


def _td_x():
    from . import runtime_db
    return runtime_db.XN_TASKS


def _td_pixiv():
    from . import runtime_db
    return runtime_db.XI_TASKS


async def _queue_problems():
    """失败与重试明细（全链路可见性，2026-11-10）。

    每条 {where, label, status, attempts, next_in, hint}：next_in=距下次
    自动重试的秒数（null=无自动重试，需人工）；hint=处置命令。供看板
    「⚠️ 失败与重试」面板——与队列卡片（只显示进行中）互补，不破坏
    「DONE/FAILED 不混进进行中」的既有反馈（2026-11-05）。"""
    import time as _time
    problems = []

    def _add(where, label, status, attempts=None, next_retry_at=None,
             hint=None, error=None):
        now = _time.time()
        problems.append({
            "where": where, "label": str(label)[:52], "status": status,
            "attempts": attempts,
            "next_in": (max(0, int(next_retry_at - now))
                        if next_retry_at else None),
            "hint": hint, "error": (str(error)[:80] if error else None),
        })

    # ① 主队列 retry 榜（内存）——含退避剩余
    try:
        from . import state as _state
        async with _state.QUEUE_LOCK:
            retry = list(_state.QUEUE.get("retry", []))
        for r in retry[:10]:
            _add("媒体队列", r.get("name") or r.get("url") or r.get("id", "?"),
                 "RETRY", r.get("attempts"), r.get("next_retry_at"),
                 "/retry 或 /queue del")
    except Exception:
        pass

    # ② listener FAILED / 滞留 FORWARDED
    try:
        from . import runtime_db
        for t in runtime_db.list_listener_tasks(status="FAILED", limit=10):
            err = str(t.get("last_error") or "")
            # 源消息已消失类失败重试无用（源被删，再试还是取不回）——
            # hint 直接指向删除，不给无效的 retry 按钮（2026-11-10）
            gone = any(m in err for m in
                       ("源消息不可读", "源消息取不回", "可能已删除"))
            hint = (f"/listen_failed del {t['id']}" if gone
                    else f"/listen_retry {t['id']}")
            _add("标签监听", f"#{t['id']} {_task_label_of(t)}", "FAILED",
                 t.get("attempts"), None, hint, t.get("last_error"))
        for t in runtime_db.list_listener_tasks(
                status="FORWARDED", limit=5):
            _add("标签监听", f"#{t['id']} {_task_label_of(t)}",
                 "FORWARDED(对账补建中)", t.get("attempts"),
                 t.get("next_retry_at"), None, t.get("last_error"))
    except Exception:
        pass

    # ③ 各站点 FAILED
    try:
        from . import runtime_db
        domains = (
            ("B站", runtime_db.BILI_TASKS.list(status="FAILED", limit=5),
             lambda r: r.get("label") or r["url"]),
            ("X", runtime_db.XN_TASKS.list(status="FAILED", limit=5),
             lambda r: r.get("label") or r["url"]),
            ("Pixiv", runtime_db.XI_TASKS.list(status="FAILED", limit=5),
             lambda r: r.get("label") or r["url"]),
        )
        for name, rows, labeler in domains:
            for r in rows:
                _add(name, labeler(r), "FAILED", r.get("attempts"),
                     r.get("next_retry_at"), None, r.get("error"))
    except Exception:
        pass
    for name, fn, key in (("wnacg 漫画", "list_wnacg_tasks", "title"),
                          ("asmr 音声", "list_asmr_tasks", "rj"),
                          ("解压回传", "list_extract_tasks", "id")):
        try:
            from . import runtime_db
            for r in getattr(runtime_db, fn)(status="FAILED", limit=5):
                _add(name, r.get(key) or r.get("aid") or r.get("src_path")
                     or "?", "FAILED", r.get("attempts"),
                     r.get("next_retry_at"), None, r.get("error"))
        except Exception:
            pass
    try:
        from . import runtime_db
        for r in runtime_db.list_pawchive_posts(status="FAILED", limit=5):
            _add("pawchive", r.get("title") or r.get("post_id") or "?",
                 "FAILED", r.get("attempts"), None, None,
                 r.get("last_error"))
    except Exception:
        pass
    return problems


def _task_label_of(t):
    """listener 任务的人读标签（target/payload 摘要）。"""
    p = t.get("payload") or {}
    if isinstance(p, str):
        try:
            import json as _json
            p = _json.loads(p)
        except Exception:
            p = {}
    return (str(p.get("label") or p.get("source_name") or
                f"{t.get('target_type')}@{t.get('source_chat_id')}"))[:40]


async def handle_stats(request):
    """台账文本（/stats 同源渲染，days 1–7）。"""
    try:
        days = int(request.query.get("days", 1))
    except ValueError:
        days = 1
    days = max(1, min(days, 7))
    try:
        text = stats.stats_text(days)
    except Exception as e:
        logger.exception(f"API 台账渲染失败：{e}")
        return _json({"ok": False, "error": "台账渲染失败（详见服务端日志）"}, 500)
    return _json({"ok": True, "days": days, "text": text})


async def handle_find(request):
    """/find 三源查询（完整字段子串，≥2 字符）。"""
    q = (request.query.get("q") or "").strip()
    if len(q) < 2:
        return _json({"ok": False, "error": "关键字至少 2 个字符"}, 400)
    from . import finder
    try:
        text = finder.find_media(q)
    except Exception as e:
        logger.exception(f"API find 查询失败：{e}")
        return _json({"ok": False, "error": "查询失败（详见服务端日志）"}, 500)
    return _json({"ok": True, "q": q, "text": text})


async def handle_chrome(request):
    """Chrome Agent 视图（status_view：状态 + 任务列表，bot 菜单同源）。"""
    if config.IS_ANDROID:
        return _json({
            "ok": True, "android": True, "progress": None,
            "text": "🤖 Android 端不支持 Chrome Agent"
                    "（需要桌面 Chrome + CDP）",
        })
    from . import chrome_client
    try:
        text = await chrome_client.status_view()
    except Exception as e:
        logger.exception(f"API chrome 视图失败：{e}")
        return _json({"ok": False, "error": f"读取失败：{e}"}, 500)
    return _json({"ok": True, "text": text,
                  "progress": chrome_client.read_progress()})


async def handle_backup(request):
    """CD2 → 115 实时备份视图（/cd2tasks 同源 gRPC：在途任务 + 计数）。

    gRPC 阻塞调用下放线程并加 30s 硬顶——CD2 没跑/卡死时返回友好错误，
    绝不挂死事件循环。"""
    if config.IS_ANDROID:
        return _json({
            "ok": False, "android": True,
            "error": "Android 端不支持 CD2 备份（需要桌面 CloudDrive2）",
        })
    from . import cd2_api

    try:
        total, files = await asyncio.wait_for(
            asyncio.to_thread(cd2_api.upload_files_summary, 10), timeout=30)
    except RuntimeError as e:
        return _json({"ok": False, "error": f"CD2 不可达：{e}"}, 503)
    except asyncio.TimeoutError:
        return _json({"ok": False, "error": "CD2 查询超时（30s）——稍后再试"}, 504)
    except Exception as e:
        logger.exception(f"API backup 查询失败：{e}")
        return _json({"ok": False, "error": "查询失败（详见服务端日志）"}, 500)
    try:
        counts = await asyncio.wait_for(
            asyncio.to_thread(cd2_api.tasks_count), timeout=30)
    except Exception:
        counts = None
    tasks = [{"name": f.get("name", ""), "pct": f.get("pct"),
              "status": f.get("status", ""), "dest": f.get("dest", "")}
             for f in files]
    return _json({"ok": True, "total": total or 0, "counts": counts,
                  "tasks": tasks})


_PWA_FILES = {
    "manifest.json": ("manifest.json", "application/manifest+json"),
    "sw.js": ("sw.js", "application/javascript"),
    "icon-192.png": ("icons/icon-192.png", "image/png"),
    "icon-512.png": ("icons/icon-512.png", "image/png"),
}


async def handle_pwa_file(request):
    """PWA 静态文件（manifest/sw/icons），免鉴权可取（白名单硬编码）。"""
    name = os.path.basename(request.path)
    if name not in _PWA_FILES:
        return _json({"ok": False, "error": "not found"}, 404)
    path = _PWA_DIR / _PWA_FILES[name][0]
    if not path.is_file():
        return _json({"ok": False, "error": f"缺失: {name}"}, 404)
    ctype = _PWA_FILES[name][1]
    if ctype.startswith("image/"):
        return web.Response(body=path.read_bytes(), content_type=ctype)
    return web.Response(text=path.read_text(encoding="utf-8"),
                        content_type=ctype)


async def handle_share_page(request):
    """分享接收页：系统分享面板 → PWA share_target 落地。"""
    path = _PWA_DIR / "share.html"
    return web.Response(text=path.read_text(encoding="utf-8"),
                        content_type="text/html", charset="utf-8")


async def handle_history(request):
    try:
        n = int(request.query.get("n", 20))
    except ValueError:
        n = 20
    n = max(1, min(n, 200))
    kind = (request.query.get("kind") or "").strip() or None
    lines = history.get_history_lines(n, kind=kind)
    return _json({"ok": True, "lines": lines})


async def handle_paw_schedule(request):
    """回填执行计划（/paw schedule 同源渲染：每作者下次回填预计时间）。"""
    from . import pawchive
    try:
        text = pawchive.backfill_schedule_text()
    except Exception as e:
        logger.exception(f"API 回填计划渲染失败：{e}")
        return _json({"ok": False, "error": f"回填计划渲染失败：{e}"}, 500)
    return _json({"ok": True, "text": text})


async def handle_paw_backfill(request):
    """回填执行情况三段式：当前状态 / 下一批作者 / 历史轮次记录。"""
    from . import pawchive
    try:
        data = pawchive.backfill_status_overview()
    except Exception as e:
        logger.exception(f"API 回填执行情况读取失败：{e}")
        return _json({"ok": False, "error": f"读取失败：{e}"}, 500)
    return _json({"ok": True, **data})


# ── 指令中心（2026-11-01）：全部指令的无头执行 ───────────────
# 复用 commands.handle_command 的全部分发逻辑（无头事件替身），app 端
# 「⌨ 指令」页 = 分组面板 + 万能输入框。安全边界：
#   * 白名单 = BOT_COMMANDS − 危险排除项（/sh 任意 shell、/sql /sqlt
#     任意 SQL、/restart 会杀掉 API 本体、/up 本机文件上传、
#     /paw_cookie 走输入窗口流程）；
#   * 回复不进 Telegram（_HeadlessEvent.reply 捕获后原样返回），
#     cmdq 台账因无真实对话自动跳过（handle_command 内部 try/except）。

_CMD_EXCLUDED = frozenset({
    "sh", "sql", "sqlt", "restart", "up", "paw_cookie",
})


class _HeadlessEvent:
    """commands.handle_command 的最小事件替身（只供 API 指令中心用）。

    commands.py 全部 handler 对 event 的使用面 = reply / chat_id /
    message / client（grep 验证过）。reply 捕获进 replies 由 API 原样
    返回；chat_id/message 置 None 让 cmdq 台账自动跳过（无真实对话）。
    """

    def __init__(self):
        self.replies: list = []
        self.chat_id = None
        self.message = None

    @property
    def client(self):
        return state.client

    async def reply(self, text, **kwargs):
        self.replies.append(text)


def _safe_command_names() -> frozenset:
    """BOT_COMMANDS − 危险排除项；bot 模块缺失时退空集（端点报 503）。"""
    try:
        from . import bot
        return frozenset(
            name for name, _ in getattr(bot, "BOT_COMMANDS", ())
            if name not in _CMD_EXCLUDED)
    except Exception as e:
        logger.warning(f"读取 BOT_COMMANDS 失败：{e}")
        return frozenset()


def _group_of(name: str) -> str:
    if name.startswith("paw"):
        return "🐾 Pawchive"
    if name.startswith("listen"):
        return "📡 标签监听"
    if name.startswith("wl"):
        return "📋 白名单"
    if name in ("cd2ck", "cd2check", "115dup", "115inv", "report"):
        return "☁️ CD2 与 115"
    if name in ("bili", "x", "pixiv", "dyu", "asmr", "wnacg",
                "ehentai", "115x", "chrome"):
        return "📺 平台下载"
    if name in ("thread", "dedup", "clean", "extract"):
        return "📥 下载与队列"
    return "📊 监控与查询"


_GROUP_ORDER = ("📊 监控与查询", "📺 平台下载", "📥 下载与队列",
                "🐾 Pawchive", "📡 标签监听", "📋 白名单",
                "☁️ CD2 与 115")


async def handle_pd_status(request):
    """pixeldrain 进度：?tag= 单任务；缺省全部任务。薄封装 pd_daemon。"""
    from . import pd_dl
    tag = request.query.get("tag") or None
    return _json(pd_dl.api_status(tag))


async def handle_pd_jobs(request):
    """pixeldrain 任务列表（含进程存活）。"""
    from . import pd_dl
    return _json(pd_dl.api_jobs())


async def handle_pd_start(request):
    """启动 pixeldrain 下载：body {"link": "...", "dest": "可选"}。"""
    from . import pd_dl
    try:
        body = await request.json()
    except Exception:
        return _json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
    link = str(body.get("link") or "").strip()
    if not link:
        return _json({"ok": False, "error": "缺少 link"}, 400)
    return _json(pd_dl.api_start(link, body.get("dest") or None))


async def handle_pd_clean(request):
    """清理 pixeldrain 云端中转：body {"link": "..."}。"""
    from . import pd_dl
    try:
        body = await request.json()
    except Exception:
        return _json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
    link = str(body.get("link") or "").strip()
    if not link:
        return _json({"ok": False, "error": "缺少 link"}, 400)
    return _json(pd_dl.api_clean(link))


async def handle_commands_registry(request):
    """指令中心的面板数据：分组 + 每条指令的用法说明（bot 注册表同源）。"""
    safe = _safe_command_names()
    if not safe:
        return _json({"ok": False, "error": "指令注册表不可用"}, 503)
    try:
        from . import bot
        descs = {name: desc for name, desc in getattr(bot, "BOT_COMMANDS", ())}
    except Exception:
        descs = {}
    grouped: dict = {}
    for name in sorted(safe):
        grouped.setdefault(_group_of(name), []).append(
            {"cmd": f"/{name}", "desc": descs.get(name, "")})
    groups = [{"name": g, "items": grouped[g]}
              for g in _GROUP_ORDER if g in grouped]
    for g, items in sorted(grouped.items()):
        if g not in _GROUP_ORDER:
            groups.append({"name": g, "items": items})
    return _json({"ok": True, "groups": groups,
                  "excluded": sorted(_CMD_EXCLUDED)})


async def handle_cmd(request):
    """执行一条白名单内指令：与在 bot 对话里打这条指令同语义。"""
    try:
        body = await request.json()
    except Exception:
        return _json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
    cmd = str(body.get("cmd") or "").strip()
    if not cmd.startswith("/"):
        # 非 / 开头：当作链接/分享文本（用户直接贴 iwara/B站/抖音地址是
        # 高频操作——与 /api/enqueue 同语义，识别不了再报错）
        try:
            result = await asyncio.wait_for(
                dispatch_link_text(cmd), timeout=90)
        except asyncio.TimeoutError:
            return _json({"ok": False,
                          "error": "处理超时（>90s）——可能仍在后台生效，"
                                   "结果看控制面板通知"}, 504)
        except Exception as e:
            logger.exception(f"API 文本分流失败：{cmd[:60]}")
            return _json({"ok": False,
                          "error": f"处理失败：{type(e).__name__}: {e}"}, 500)
        return _json(result, 200 if result.get("ok") else 400)
    head = cmd.split()[0][1:].split("@")[0].lower()
    safe = _safe_command_names()
    if not safe:
        return _json({"ok": False, "error": "指令注册表不可用"}, 503)
    if head not in safe:
        if head in _CMD_EXCLUDED:
            return _json({"ok": False,
                          "error": f"/{head} 属于高危或不适于远程执行的指令，"
                                   "不开放（请到 Telegram bot 对话里用）"}, 403)
        return _json({"ok": False, "error": f"未识别的指令：/{head}"}, 404)
    from . import commands
    ev = _HeadlessEvent()
    try:
        handled = await asyncio.wait_for(
            commands.handle_command(ev, cmd), timeout=90)
    except asyncio.TimeoutError:
        return _json({"ok": False,
                      "error": "执行超时（>90s）——指令可能仍在后台生效，"
                               "结果看控制面板通知"}, 504)
    except Exception as e:
        logger.exception(f"API 指令执行失败：{cmd[:60]}")
        return _json({"ok": False, "error": "执行失败（详见服务端日志）"}, 500)
    if not handled:
        return _json({"ok": False, "error": f"未识别的指令：/{head}"}, 400)
    text = "\n\n".join(r for r in ev.replies if r).strip()
    return _json({"ok": True, "cmd": cmd.split()[0],
                  "text": text or "已执行（无文本回复，结果看控制面板通知）"})


def make_app() -> web.Application:
    """组装 aiohttp 应用（独立出来便于测试注入）。"""
    # 限流在最外层（2026-11-05 审计）：此前鉴权在外，未带 token 的请求
    # 不限流且每条写一行 INFO（Funnel 暴露面被刷日志）
    app = web.Application(middlewares=[_rate_limit_middleware, _auth_middleware],
                          client_max_size=1024 * 1024)
    app.add_routes([
        web.get("/", handle_dashboard),
        web.get("/api/status", handle_status),
        web.get("/api/version", handle_version),
        web.get("/api/batch", handle_batch),
        web.get("/api/sse", handle_sse),
        web.get("/api/extract/state", handle_extract_state),
        web.get("/api/fs/list", handle_fs_list),
        web.post("/api/fs/rename", handle_fs_rename),
        web.post("/api/fs/delete", handle_fs_delete),
        web.post("/api/fs/mkdir", handle_fs_mkdir),
        web.post("/api/fs/touch", handle_fs_touch),
        web.post("/api/fs/paste", handle_fs_paste),
        web.post("/api/fs/extract", handle_fs_extract),
        web.get("/api/fs/op", handle_fs_op),
        web.get("/api/stats", handle_stats),
        web.get("/api/queues", handle_queues),
        web.get("/api/queue_detail", handle_queue_detail),
        web.get("/api/find", handle_find),
        web.get("/api/chrome", handle_chrome),
        web.get("/api/backup", handle_backup),
        web.get("/share", handle_share_page),
        web.get("/manifest.json", handle_pwa_file),
        web.get("/sw.js", handle_pwa_file),
        web.get("/icons/icon-192.png", handle_pwa_file),
        web.get("/icons/icon-512.png", handle_pwa_file),
        web.get("/api/pastes", handle_pastes),
        web.get("/api/submissions", handle_pastes),
        web.get("/api/links", handle_links),
        web.post("/api/links_done", handle_links_done),
        web.get("/api/commands", handle_commands_registry),
        web.get("/api/pd/status", handle_pd_status),
        web.get("/api/pd/jobs", handle_pd_jobs),
        web.post("/api/pd/start", handle_pd_start),
        web.post("/api/pd/clean", handle_pd_clean),
        web.post("/api/cmd", handle_cmd),
        web.post("/api/enqueue", handle_enqueue),
        web.post("/api/action/{name}", handle_action),
        web.get("/api/history", handle_history),
        web.get("/api/paw_schedule", handle_paw_schedule),
        web.get("/api/paw_backfill", handle_paw_backfill),
    ])
    return app


_LOOPBACK_BINDS = ("127.0.0.1", "localhost", "::1")


async def start_api(bind=None, port=None, token=None):
    """启动本地 API 服务并驻留，直到被取消（app.main 的服务任务模式）。

    bind/port/token 参数供测试注入；缺省读 config（import 期已从
    tg_secrets.json 的 api 段解析）。启动失败（端口占用等）只记 ERROR
    并返回——绝不影响 userbot 其余功能。
    """
    bind = config.API_BIND if bind is None else bind
    port = config.API_PORT if port is None else port
    token = config.API_TOKEN if token is None else token

    if not token and bind not in _LOOPBACK_BINDS:
        logger.error(
            "❌ 本地 API 拒绝启动：绑定 %s 但未配置 token（裸暴露局域网/"
            "公网）。请在 tg_secrets.json api 段设置 token，或把 bind "
            "改回 127.0.0.1", bind)
        return
    if not config.API_ENABLED:
        logger.info("本地 API 未启用（tg_secrets.json api.enabled）")
        return

    runner = web.AppRunner(make_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, bind, port)
    try:
        await site.start()
    except OSError as e:
        logger.error(f"❌ 本地 API 启动失败（端口 {bind}:{port}）：{e}"
                     "——不影响其余功能")
        await runner.cleanup()
        return
    logger.info(
        "🌐 本地 API 已启动：http://%s:%d（鉴权：%s）——看板/套壳 app 数据面",
        bind, port, "开" if token else "关（仅本机）")
    try:
        await asyncio.Event().wait()  # 驻留，直到停机取消
    except asyncio.CancelledError:
        raise
    finally:
        await runner.cleanup()
        logger.info("🌐 本地 API 已停止")
