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
import shutil
import time
from pathlib import Path

from aiohttp import web

from . import config
from . import state
from . import stats
from . import history
from . import notify
from . import platform
from .log import logger

_STARTED_AT = time.time()
_DASHBOARD_PATH = Path(__file__).with_name("api_dashboard.html")

# 单次链接分流的硬顶：resolver 每条 30s、转 bot 每条 60s，多条链接取上限
_DISPATCH_TIMEOUT_SECONDS = 120

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

_PASTE_LOG_FILE = os.path.join(config.RUNTIME_DIR, "paste_log.jsonl")
_PASTE_LOG_MAX = 500


def _log_paste(text, url, routed, ref=None) -> None:
    try:
        entry = {"ts": time.time(), "text": str(text or "")[:200],
                 "url": url, "routed": routed, "ref": ref}
        with open(_PASTE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"粘贴记录落盘失败：{e}")


def _read_paste_log(n=50) -> list:
    try:
        with open(_PASTE_LOG_FILE, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    items = []
    for line in lines[-_PASTE_LOG_MAX:][-n:]:
        try:
            items.append(json.loads(line))
        except ValueError:
            continue
    return list(reversed(items))    # 新→旧


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


def _paste_status(p):
    """回查一条粘贴记录的当前处理状态（尽力而为，查询失败不炸）。"""
    routed, ref, url = p.get("routed"), p.get("ref"), p.get("url")
    try:
        if routed in ("iwara", "iwara_dup"):
            # ① 有 ref → chrome 任务实时状态（排队/下载中/完成/失败）
            if ref:
                from . import chrome_agent
                tasks = chrome_agent.load_tasks(config.CHROME_TASKS_FILE)
                t = next((x for x in tasks if x.get("task_id") == ref), None)
                if t:
                    text, kind = _CHROME_STATUS_VIEW.get(
                        t.get("status"), (t.get("status") or "未知", "info"))
                    if t.get("filename"):
                        text += f" · {t['filename']}"
                    return text, kind
            # ② 兜底：按视频 ID 查既往成功——ref 缺失、任务滚出窗口、
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
        if routed in ("douyin",) and ref:
            last = None
            for e in stats.load_events():
                if e.get("task_id") == ref:
                    last = e
            if last:
                text, kind = _EVENTS_STATUS_VIEW.get(
                    last.get("ev"), (last.get("ev") or "处理中", "info"))
                return text, kind
            return "已受理（解析中）", "info"
        if routed in ("douyin",) and not ref:
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
        return _json({"ok": False, "error": f"读取失败：{e}"}, 500)
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
        return _json({"ok": False, "error": f"标记失败：{e}"}, 500)
    return _json({"ok": bool(ok),
                  "message": "✅ 已标记完成" if ok else "ℹ️ 该条不存在或已不是待处理"})


async def handle_app_relay(text: str) -> None:
    """处理 app 经 Bot API 中转来的分享文本：与 /api/enqueue 同一入口。

    fire-and-forget：app 端拿不到同步回执，结果统一经 Telegram 通知回
    控制面板（成功/失败都说人话）。任何异常都不外泄——这是后台任务。
    """
    try:
        result = await asyncio.wait_for(
            dispatch_link_text(text), timeout=_DISPATCH_TIMEOUT_SECONDS)
    except Exception as e:
        logger.exception(f"🛰 app 中转入队失败：{e}")
        try:
            await notify.notify_user(
                f"📲 分享直达失败：{type(e).__name__}: {e}\n{text[:80]}")
        except Exception:
            pass
        return
    if result.get("ok"):
        logger.info(
            f"🛰 app 中转已受理（{result.get('routed')}）：{text[:60]}")
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


def token_ok(request) -> bool:
    """校验请求令牌：Bearer 头 / X-Token 头 / ?token= 三选一。

    未配置 token（config.API_TOKEN 为空）= 仅 loopback 部署（start_api
    已校验），一律放行。比较用非常量时间 hmac.compare_digest——本地方案
    不严格需要，顺手做对。
    """
    token = config.API_TOKEN
    if not token:
        return True
    auth = request.headers.get("Authorization", "")
    supplied = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if not supplied:
        supplied = request.headers.get("X-Token", "")
    if not supplied:
        supplied = request.query.get("token", "")
    return hmac.compare_digest(supplied, token)


@web.middleware
async def _auth_middleware(request, handler):
    if not token_ok(request):
        return web.json_response(
            {"ok": False, "error": "令牌无效或缺失（Authorization: Bearer / "
                                   "X-Token 头 / ?token= 任选其一）"},
            status=401,
        )
    resp = await handler(request)
    # 访问留痕（2026-11-01 排查手机端可达性）：来源 IP + XFF（Funnel 代理
    # 会带手机出口 IP）+ 路径 + 状态码——用户「打不开」时一眼看出请求
    # 是否到达、卡在哪一层
    logger.info(f"🌐 API {request.method} {request.path_qs} ← {request.remote}"
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
        "db_ready": bool(state.RUNTIME_DB_READY),
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


async def dispatch_link_text(text: str) -> dict:
    """把一段含链接的文本按平台分流——语义与「往收藏夹发这条消息」一致。

    分流顺序照抄 new_message_handler 的链接段：抖音/IG → platform（本地
    解析入队 + bot 兜底）→ 种子直链 → torrent_offline → B站 → bili_dl →
    其余外链 → manual_links 外链台账。每一路都复用既有入口，不在 API 里
    重写任何下载/解析逻辑。
    """
    from . import manual_links

    douyin_urls = platform.extract_douyin_urls(text)
    instagram_urls = platform.extract_instagram_urls(text)
    if douyin_urls or instagram_urls:
        # 同一条入口（含实体 URL 补抓的收藏夹路径之外，API 文本里链接
        # 已经是明文，不需要 entity 兜底）。失败在 platform 内部已降级
        # bot 并通知，不会静默丢。
        await platform.relay_platform_links(
            _ShimMessage(text), douyin_urls, instagram_urls)
        kind = "douyin" if douyin_urls else "instagram"
        _url0 = (douyin_urls or instagram_urls or [None])[0]
        _ref = next((r.get("id") for r in
                     (state.QUEUE.get("tasks") or []) +
                     (state.QUEUE.get("retry") or [])
                     if r.get("url") == _url0), None)
        _log_paste(text, _url0, kind, _ref)
        return {
            "ok": True, "routed": kind,
            "message": "链接已受理：本地解析成功会自动入队下载，"
                       "失败已转解析 bot——进度看 Telegram 控制面板通知",
        }

    from . import torrent_offline
    torrent_urls = [u for u in manual_links.extract_urls(text)
                    if torrent_offline.is_torrent_url(u)]
    if torrent_urls:
        arg = torrent_offline.trailing_author(text, torrent_urls[0])
        out = await torrent_offline.handle_torrent_url(
            torrent_urls[0],
            to_folder=(torrent_offline.resolve_to_folder(arg)
                       if arg else None))
        try:
            await notify.notify_user(out)
        except Exception as e:
            logger.warning(f"API 种子离线结果通知失败：{e}")
        _log_paste(text, torrent_urls[0], "torrent", None)
        return {"ok": True, "routed": "torrent", "message": out}

    # iwara 视频链接 → 提交 Chrome Agent 下载（CDP 解析签名直链）。
    # 与 bot 对话路径（bot.py 收藏夹分支）同语义：判重（成功过才拦）+
    # 提交 /chrome。结果通知走 chrome_requests 映射 → 收藏夹。
    from .iwara import extract_iwara_video_id
    iwara_urls = [u for u in manual_links.extract_urls(text)
                  if extract_iwara_video_id(u)]
    if iwara_urls:
        from . import chrome_client
        from . import iwara as iwara_mod
        _vid = extract_iwara_video_id(iwara_urls[0])
        try:
            _hit = iwara_mod.find_previous_success(_vid) if _vid else None
        except Exception:
            _hit = None
        if _hit:
            _log_paste(text, iwara_urls[0], "iwara_dup", None)
            return {"ok": True, "routed": "iwara_dup",
                    "message": iwara_mod.duplicate_text(_hit)}
        ev = _HeadlessEvent()
        out = await chrome_client.handle_chrome_command(
            ev, "/chrome " + iwara_urls[0],
            owner_id=chrome_client.resolve_owner_id(state.MY_ID))
        try:
            from . import chrome_agent as _ca
            _tasks = _ca.load_tasks(config.CHROME_TASKS_FILE)
            _ref = next((t.get("task_id") for t in _tasks
                         if t.get("url") == iwara_urls[0]), None)
        except Exception:
            _ref = None
        _log_paste(text, iwara_urls[0], "iwara", _ref)
        return {"ok": True, "routed": "iwara",
                "message": "\n".join(ev.replies) or out or "已提交 Chrome 下载"}

    from . import bili_dl
    if bili_dl.looks_like_bili(text):
        reply = await bili_dl.auto_reply(text)
        return {"ok": True, "routed": "bili", "message": reply}

    if not manual_links.extract_urls(text):
        return {"ok": False, "routed": None, "message": "没有识别到可处理的链接"}

    reply, _buttons = manual_links.observe(text)
    _urls = manual_links.extract_urls(text)
    _log_paste(text, (_urls or [None])[0], "ledger", None)
    return {"ok": True, "routed": "ledger", "message": reply}


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
    try:
        result = await asyncio.wait_for(
            dispatch_link_text(text), timeout=_DISPATCH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return _json({"ok": False,
                      "error": f"处理超时（>{_DISPATCH_TIMEOUT_SECONDS}s），"
                               "链接可能已部分受理，请看控制面板通知"}, 504)
    except Exception as e:
        logger.exception(f"API 链接分流异常：{e}")
        return _json({"ok": False, "error": f"处理失败：{type(e).__name__}: {e}"},
                     500)
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
                      "error": f"动作执行失败：{type(e).__name__}: {e}"}, 500)
    # 白名单新增了键却没写分支时绝不返回 None（aiohttp 会 500）
    return _json({"ok": False, "error": f"动作 {name} 尚未实现"}, 500)


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
        return _json({"ok": False, "error": f"台账渲染失败：{e}"}, 500)
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
        return _json({"ok": False, "error": f"查询失败：{e}"}, 500)
    return _json({"ok": True, "q": q, "text": text})


async def handle_chrome(request):
    """Chrome Agent 视图（status_view：状态 + 任务列表，bot 菜单同源）。"""
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
    from . import cd2_api

    try:
        total, files = await asyncio.wait_for(
            asyncio.to_thread(cd2_api.upload_files_summary, 10), timeout=30)
    except RuntimeError as e:
        return _json({"ok": False, "error": f"CD2 不可达：{e}"})
    except asyncio.TimeoutError:
        return _json({"ok": False, "error": "CD2 查询超时（30s）——稍后再试"})
    except Exception as e:
        logger.exception(f"API backup 查询失败：{e}")
        return _json({"ok": False, "error": f"查询失败：{type(e).__name__}: {e}"}, 500)
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


async def handle_history(request):
    try:
        n = int(request.query.get("n", 20))
    except ValueError:
        n = 20
    n = max(1, min(n, 200))
    lines = history.get_history_lines(n)
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
        return _json({"ok": False, "error": f"执行失败：{type(e).__name__}: {e}"},
                     500)
    if not handled:
        return _json({"ok": False, "error": f"未识别的指令：/{head}"}, 400)
    text = "\n\n".join(r for r in ev.replies if r).strip()
    return _json({"ok": True, "cmd": cmd.split()[0],
                  "text": text or "已执行（无文本回复，结果看控制面板通知）"})


def make_app() -> web.Application:
    """组装 aiohttp 应用（独立出来便于测试注入）。"""
    app = web.Application(middlewares=[_auth_middleware, _rate_limit_middleware],
                          client_max_size=1024 * 1024)
    app.add_routes([
        web.get("/", handle_dashboard),
        web.get("/api/status", handle_status),
        web.get("/api/stats", handle_stats),
        web.get("/api/find", handle_find),
        web.get("/api/chrome", handle_chrome),
        web.get("/api/backup", handle_backup),
        web.get("/api/pastes", handle_pastes),
        web.get("/api/links", handle_links),
        web.post("/api/links_done", handle_links_done),
        web.get("/api/commands", handle_commands_registry),
        web.post("/api/cmd", handle_cmd),
        web.post("/api/enqueue", handle_enqueue),
        web.post("/api/action/{name}", handle_action),
        web.get("/api/history", handle_history),
        web.get("/api/paw_schedule", handle_paw_schedule),
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
