"""健康巡检与实时状态（/health /now）+ 卡死哨兵——「怎么没动静」的答案自动化。

owner 痛点（2026-10-25 复盘）：backfill 饿死 3h、/115dup 卡死 34min、
paw_plan 站点超时、代理挂整体假死——每次都是人先发现。三个部件：

    /now     10 行内答「系统现在在干什么」：各 worker 当前活动 + 队列深度
             + 卡死嫌疑（数据全来自各模块已有的内存态 + DB 计数）
    /health  一键巡检：连接/磁盘/挂载/代理/staging 残留/死段/全部队列
    哨兵循环 每 10 分钟 DB 快照 → 卡死信号（有 due 活但 30min 零完成、
             租约过期、查重 run 超长、代理 TCP 不可达）→ 通知（30min 冷却）

判据全部 DB/OS 驱动，不给 worker 加仪表——各 recover 循环本就在自愈，
哨兵只在「该自愈没自愈」时喊人。
"""
import asyncio
import os
import socket
import time
from urllib.parse import urlsplit

from . import config
from . import runtime_db
from . import state
from .log import logger

TEXT_PREFIX = "🩺"
_MONITOR_TASKS = set()
_COOLDOWN_SECONDS = 1800.0
_last_notify = {}          # 信号 key → monotonic（30 分钟冷却）
_proxy_fail_streak = 0


# ============================================================
# /now：系统现在在干什么
# ============================================================
def _fmt_age(ts):
    if not ts:
        return "-"
    s = max(0, int(time.time() - ts))
    return f"{s // 60}m{s % 60:02d}s" if s < 3600 else f"{s // 3600}h"


def render_now() -> str:
    """10 行内的实时视图。任何子块失败都降级为该行标注，绝不整页炸。"""
    lines = [f"{TEXT_PREFIX} 现在（{time.strftime('%H:%M:%S')}）"]
    try:
        snap = runtime_db.queue_health_counts()
    except Exception as e:
        snap = None
        lines.append(f"⚠️ 队列快照失败：{str(e)[:50]}")

    # ① 115 解压
    try:
        from . import extract_worker as ew
        cur = dict(getattr(ew, "_CURRENT", {}) or {})
        if cur.get("task_id"):
            lines.append(f"🗜 115解压：E{cur['task_id']} {cur.get('stage') or ''}"
                         f"｜{(cur.get('label') or '')[:28]}")
        else:
            lines.append("🗜 115解压：空闲")
    except Exception:
        lines.append("🗜 115解压：？（模块未载入）")

    # ② pawchive（当前帖 + 在途文件进度行）
    try:
        from . import pawchive_worker as pw
        cur = pw.current_post_label()
        lines.append(f"🐾 pawchive：{cur}" if cur else "🐾 pawchive：空闲")
        for pl in pw._panel_progress_lines()[:3]:
            lines.append(f"   {pl.strip()}")
    except Exception:
        lines.append("🐾 pawchive：？（模块未载入）")

    # ③ wnacg
    try:
        from . import wnacg
        cur = dict(getattr(wnacg, "_CURRENT", {}) or {})
        if cur.get("aid"):
            lines.append(f"📖 wnacg：#{cur['aid']} {cur.get('stage') or ''}"
                         f"｜{(cur.get('title') or '')[:28]}")
        else:
            lines.append("📖 wnacg：空闲")
    except Exception:
        lines.append("📖 wnacg：？（模块未载入）")

    # ④ 下载队列（主下载在途）
    try:
        actives = [v for v in (state.ACTIVE_DOWNLOADS or {}).values()]
        dl = [a for a in actives if a.get("label") != "pawchive"]
        head = f"📥 下载队列：{len(dl)} 在跑"
        if snap:
            dc = snap["download"]["counts"]
            if dc.get("RETRY"):
                head += f"｜待重试 {dc['RETRY']}"
            if dc.get("QUEUED"):
                head += f"｜排队 {dc['QUEUED']}"
        lines.append(head)
        for a in dl[:2]:
            pct = a.get("percent")
            lines.append(f"   ↓ {(a.get('filename') or '')[:30]}"
                         + (f" {pct}%" if pct is not None else ""))
    except Exception:
        lines.append("📥 下载队列：？（状态缺失）")

    # ⑤ 监听 / 扫描类
    try:
        from . import listener
        bits = []
        if getattr(listener, "_HISTORY_PROGRESS", None):
            bits.append("📜回扫进行中")
        scan = getattr(state, "PAW_SCAN_RUNNING", None)
        if scan:
            bits.append(f"🔎扫描 {scan}")
        lines.append("📡 监听：" + ("｜".join(bits) if bits else "平稳"))
    except Exception:
        lines.append("📡 监听：？（模块未载入）")

    # ⑥ 卡死嫌疑 + 队列总量一行
    if snap:
        exp = {k: snap[k]["expired"] for k in
               ("extract", "pawchive", "listener", "wnacg")
               if snap[k]["expired"]}
        due = sum(snap[k]["due"] for k in
                  ("extract", "pawchive", "listener", "wnacg"))
        if exp:
            lines.append(f"⚠️ 租约过期（自愈中）：{exp}")
        lines.append(f"待办合计 {due}"
                     + (f"｜近30min 完成 {sum(snap[k]['recent_done'] for k in ('extract', 'pawchive', 'listener')) + snap['recent_dl_history']}"
                        if snap else ""))
    return "\n".join(l for l in lines if l.strip())[:3900]


# ============================================================
# /health：一键巡检
# ============================================================
def _proxy_probe():
    """TG_PROXY 的 TCP 可达探测（毫秒级）。返回 (ok, 描述)。"""
    url = (os.environ.get("TG_PROXY") or "").strip()
    if not url:
        return True, "未配置（直连）"
    try:
        p = urlsplit(url if "//" in url else "//" + url)
        host, port = p.hostname, p.port
        t0 = time.monotonic()
        with socket.create_connection((host, port), timeout=3):
            return True, f"{host}:{port} 可达 ({(time.monotonic()-t0)*1000:.0f}ms)"
    except Exception as e:
        return False, f"{host}:{port} 不可达（{type(e).__name__}）"


def _dir_size_gb(path):
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1048576 / 1024


def _dead_segments():
    """超过 24h 没动过的 .download 死段（个, GB）。只查一层常用目录。"""
    base = getattr(config, "DOWNLOAD_DIR", "")
    if not base or not os.path.isdir(base):
        return 0, 0.0
    n, total = 0, 0
    day_ago = time.time() - 86400
    for root, _dirs, files in os.walk(base, onerror=lambda e: None):
        if root.count(os.sep) - base.count(os.sep) > 3:
            continue
        for f in files:
            if f.endswith(".download"):
                p = os.path.join(root, f)
                try:
                    if os.path.getmtime(p) < day_ago:
                        n += 1
                        total += os.path.getsize(p)
                except OSError:
                    pass
    return n, total / 1048576 / 1024


def render_health() -> str:
    lines = [f"{TEXT_PREFIX} 系统巡检（{time.strftime('%m-%d %H:%M')}）", ""]
    ok_all = True

    def row(name, ok, detail):
        nonlocal ok_all
        ok_all = ok_all and bool(ok)
        lines.append(f"{'✅' if ok else '❌'} {name}：{detail}")

    # 连接
    cli = state.client
    row("主客户端", cli is not None and cli.is_connected(),
        "已连接" if (cli and cli.is_connected()) else "断连")
    row("bot 客户端", state.bot_client is not None,
        "在线" if state.bot_client else "未启动")
    pok, pdesc = _proxy_probe()
    row("代理", pok, pdesc)
    # 磁盘/挂载
    try:
        import shutil
        free = shutil.disk_usage(config.DOWNLOAD_DIR).free / 1048576 / 1024
        row("下载盘", free > 20, f"剩余 {free:.0f} GB")
    except Exception as e:
        row("下载盘", False, str(e)[:40])
    mount_ok = os.path.isdir(getattr(config, "CLOUD_MOUNT_BASE", "/nonexist"))
    row("115 挂载", mount_ok,
        "在线" if mount_ok else f"{getattr(config, 'CLOUD_MOUNT_BASE', '')} 不可见")
    # staging / 死段
    try:
        st = getattr(config, "EXTRACT_STAGING_ROOT", "")
        if st and os.path.isdir(st):
            import glob
            n = len([d for d in glob.glob(os.path.join(st, "*"))
                     if os.path.isdir(d)])
            gb = _dir_size_gb(st)
            row("解压 staging", True, f"{n} 个任务目录 {gb:.1f}GB"
                + ("（可 /115x 查看是否有滞留）" if n else "（干净）"))
        dn, dgb = _dead_segments()
        row("下载死段", dn == 0,
            "无" if not dn else f"{dn} 个超24h未动 {dgb:.1f}GB（可清理）")
    except Exception:
        pass
    # 队列
    try:
        snap = runtime_db.queue_health_counts()
        def ql(name, k):
            c = snap[k]["counts"]
            bits = [f"{s}={n}" for s, n in sorted(c.items())
                    if s not in ("COMPLETED", "DONE", "SUCCESS", "ARCHIVED")]
            due = snap[k]["due"]
            exp = snap[k]["expired"]
            tail = f"｜due {due}" if due else ""
            tail += f"｜⚠️过期租约 {exp}" if exp else ""
            row(name, exp == 0, " ".join(bits) + tail or "空")
        ql("115解压", "extract")
        ql("pawchive", "pawchive")
        ql("监听任务", "listener")
        ql("wnacg", "wnacg")
        dc = snap["download"]["counts"]
        row("下载队列", True, " ".join(f"{s}={n}" for s, n in
            sorted(dc.items())) or "空")
        if snap["dup_running_long"]:
            row("查重 run", False,
                f"{snap['dup_running_long']} 个 running 超 90min（疑似卡死）")
    except Exception as e:
        row("队列快照", False, str(e)[:50])
    lines.append("")
    lines.append("总体：" + ("🟢 健康" if ok_all else "🔴 有异常项（见上）")
                 + "｜/now 看实时在干什么")
    return "\n".join(lines)[:3900]


# ============================================================
# 卡死哨兵循环
# ============================================================
def decide_stall_signals(snap, now=None):
    """纯函数：DB 快照 → 卡死信号列表 [{key, text}]。"""
    now = now if now is not None else time.time()
    out = []
    if not snap:
        return out
    queues = ("extract", "pawchive", "listener", "wnacg")
    due = sum(snap[k]["due"] for k in queues)
    completions = (sum(snap[k]["recent_done"] for k in queues)
                   + snap.get("recent_dl_history", 0))
    if due > 0 and completions == 0:
        detail = "｜".join(f"{k} due {snap[k]['due']}" for k in queues
                           if snap[k]["due"])
        out.append({"key": "stall", "text":
                    f"⚠️ 疑似卡死：有到期待办（{detail}）但近 30 分钟零完成。"
                    "发 /health 巡检、/now 看现场；必要时重启后 /cmdq 台账会补指令"})
    exp = {k: snap[k]["expired"] for k in queues if snap[k]["expired"]}
    if exp:
        out.append({"key": "lease", "text":
                    f"⚠️ 租约过期 {exp}（recover 循环应自愈；反复出现=自愈失灵，"
                    "报给维护者）"})
    if snap.get("dup_running_long"):
        out.append({"key": "dup", "text":
                    f"⚠️ 查重 run running 超 90 分钟（疑似巨目录流卡死，"
                    "上次同类卡死已修页粒度判停——若再现在报维护者）"})
    return out


async def _notify_signal(key, text):
    """带 30 分钟冷却的通知；True=已发出。"""
    now = time.monotonic()
    last = _last_notify.get(key)
    # 显式 None 判缺失：本机 monotonic 绝对值近零（睡眠时钟），拿 0 当
    # 默认值会把首次通知也挡掉
    if last is not None and now - last < _COOLDOWN_SECONDS:
        return False
    _last_notify[key] = now
    from . import notify
    try:
        await notify.notify_user(f"{TEXT_PREFIX} 卡死哨兵\n{text}")
        return True
    except Exception:
        return False


async def stall_monitor_loop(poll_seconds=600.0):
    """每 10 分钟：DB 快照 → 卡死信号 → 冷却通知。另盯代理 TCP 连续不可达。"""
    global _proxy_fail_streak
    logger.info(f"🩺 卡死哨兵已启动（每 {poll_seconds:.0f}s 快照，"
                "信号通知 30min 冷却）")
    try:
        while True:
            await asyncio.sleep(poll_seconds)
            try:
                snap = runtime_db.queue_health_counts()
                for sig in decide_stall_signals(snap):
                    sent = await _notify_signal(sig["key"], sig["text"])
                    if sent:
                        logger.warning(f"🩺 哨兵信号（{sig['key']}）："
                                       f"{sig['text'][:80]}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"🩺 哨兵快照异常（下轮再试）：{e}")
            try:
                pok, _d = _proxy_probe()
                _proxy_fail_streak = 0 if pok else _proxy_fail_streak + 1
                if _proxy_fail_streak >= 3:
                    await _notify_signal(
                        "proxy",
                        "⚠️ 代理连续 30 分钟不可达——系统处于假死：指令已入 "
                        "/cmdq 台账不会丢，但需要你去换代理节点。")
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
    except asyncio.CancelledError:
        logger.info("🩺 卡死哨兵停止")
        raise


def start_monitor():
    task = asyncio.ensure_future(stall_monitor_loop())
    _MONITOR_TASKS.add(task)
    task.add_done_callback(_MONITOR_TASKS.discard)
    return task
