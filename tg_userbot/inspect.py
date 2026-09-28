"""系统巡检（/inspect，2026-09-28）：把日常手工巡检的固定动作抽象成一个指令。

一次采集并渲染：
  连接与断连 · 进程运行时长 · 磁盘水位 · 下载队列 · Pawchive（含 24h
  产出与可行动失败）· 标签监听 · Chrome · 外链台账 · DB 备份 · CD2 实时

原则：
- 只读现有状态，绝不重写任何 worker/状态机；
- 某项拿不到就显示「--」并继续，巡检本身绝不能成为新的故障面；
- 结论区自动汇总需关注项（断连频繁/磁盘低/可重投失败/无今日备份…）。
"""
import os
import subprocess
import time
from datetime import datetime

from . import config
from . import runtime_db
from .log import logger

DISCONNECT_MARK = "主客户端连接已断开"
WARN_DISK_FREE_GB = 50
WARN_DISCONNECTS = 100


def _ps_uptime(pattern):
    """按命令行模式取进程运行时长（etime）；找不到返回 None。"""
    try:
        out = subprocess.run(
            ["ps", "-eo", "etime=,command="], capture_output=True,
            text=True, timeout=10).stdout
        for line in out.splitlines():
            if pattern in line:
                return line.strip().split()[0]
    except Exception:
        pass
    return None


def _today_disconnects():
    """今日主客户端硬断开次数（读 userbot.out 按日期前缀计数）。"""
    today = time.strftime("%Y-%m-%d")
    path = os.path.join(config.RUNTIME_DIR, "userbot.out")
    n = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith(today) and DISCONNECT_MARK in line:
                    n += 1
    except OSError:
        return None
    return n


def _disk_free_gb():
    try:
        import shutil
        return shutil.disk_usage(config.DOWNLOAD_DIR).free / 1024 ** 3
    except Exception:
        return None


def _section_counters(con):
    """各子系统计数（一次连接全查完）。返回 dict；DB 不可用返回 None。"""
    today = time.time() - 86400
    out = {}
    out["queue"] = dict(con.execute(
        "SELECT state, COUNT(*) FROM download_tasks GROUP BY state").fetchall())
    out["paw"] = dict(con.execute(
        "SELECT status, COUNT(*) FROM pawchive_posts GROUP BY status").fetchall())
    n, sz = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(size_bytes),0) FROM pawchive_files "
        "WHERE status='DONE' AND updated_at>=?", (today,)).fetchone()
    out["paw24h"] = (int(n), float(sz or 0))
    out["listener"] = dict(con.execute(
        "SELECT status, COUNT(*) FROM listener_tasks GROUP BY status").fetchall())
    out["links"] = dict(con.execute(
        "SELECT status, COUNT(*) FROM manual_links GROUP BY status").fetchall())
    out["paw_actionable"] = con.execute(
        "SELECT COUNT(DISTINCT p.id) FROM pawchive_posts p "
        "JOIN pawchive_files f ON f.post_row=p.id "
        "WHERE p.status='FAILED' AND f.status='FAILED' "
        "AND f.error NOT LIKE '站点缺文件(404)%'").fetchone()[0]
    return out


def run_inspection():
    """采集并渲染巡检报告（≤3900 字符）。纯只读。"""
    now = time.strftime("%m-%d %H:%M")
    warnings = []

    # 连接
    try:
        from . import state
        conn_ok = bool(state.client and state.client.is_connected())
    except Exception:
        conn_ok = False
    disc = _today_disconnects()
    conn_line = "🟢 正常" if conn_ok else "🔴 断开（自动重连中）"
    if disc is not None:
        conn_line += f"（今日断连 {disc} 次）"
        if disc >= WARN_DISCONNECTS:
            warnings.append(f"今日断连 {disc} 次——代理节点劣化，建议换节点")

    # 进程
    up_main = _ps_uptime("tg_userbot_final")
    up_agent = _ps_uptime("chrome_agent")
    proc_line = (f"主 {up_main or '--'} · Agent {up_agent or '--'}")
    if up_agent is None:
        warnings.append("Chrome Agent 未运行")

    # 磁盘
    free = _disk_free_gb()
    disk_line = f"{free:.0f} GB 可用" if free is not None else "--"
    if free is not None and free < WARN_DISK_FREE_GB:
        warnings.append(f"磁盘仅剩 {free:.0f} GB")

    # 子系统计数
    lines = [f"🔍 系统巡检（{now}）", ""]
    lines.append(f"连接：{conn_line}")
    lines.append(f"进程：{proc_line}")
    lines.append(f"磁盘：{disk_line}")

    con = None
    try:
        con = _open_read_conn()
        sec = _section_counters(con)
    except runtime_db.DbUnavailable:
        sec = None
    except Exception as e:
        logger.warning(f"巡检子系统计数失败：{e}")
        sec = None
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass

    if sec:
        q = sec["queue"]
        lines.append(
            f"📥 下载队列：待处理 {q.get('QUEUED', 0)} · 重试 "
            f"{q.get('RETRY', 0)}")
        if q.get("RETRY", 0) >= 20:
            warnings.append(f"待重试积压 {q.get('RETRY')} 条")
        paw = sec["paw"]
        n24, sz24 = sec["paw24h"]
        from .naming import format_size
        proc = paw.get('PROCESSING', 0)
        lines.append(
            f"🐾 Pawchive：✅{paw.get('COMPLETED', 0)} 👤"
            f"{paw.get('MANUAL', 0)} ❌{paw.get('FAILED', 0)} 🗄"
            f"{paw.get('ARCHIVED', 0)}"
            + (f" 🔄{proc}" if proc else "")
            + f"（24h {n24} 文件 / {format_size(sz24)}）")
        act = sec["paw_actionable"]
        if act:
            lines.append(f"　失败帖中 {act} 帖有可重投文件"
                         "（/paw_retry all）")
            warnings.append(f"Pawchive {act} 帖有可重投失败"
                            "（429/超时类，建议择机 /paw_retry all）")
        lis = sec["listener"]
        lines.append(
            f"📡 标签监听：✅{lis.get('SUCCESS', 0)} ⏳{lis.get('PENDING', 0)}"
            f" ❌{lis.get('FAILED', 0)}（明细 /listen_failed）")
        lnk = sec["links"]
        lines.append(f"🔗 外链台账：待处理 {lnk.get('PENDING', 0)} · "
                     f"完成 {lnk.get('DONE', 0)}")

    # Chrome
    try:
        from collections import Counter
        import json
        data = json.load(open(config.CHROME_TASKS_FILE))
        c = Counter(t.get("status") for t in data.get("tasks", []))
        lines.append(
            f"🌐 Chrome：✅{c.get('SUCCESS', 0)} ⏳{c.get('PENDING', 0)}"
            f" 🔄{c.get('RUNNING', 0)}")
    except Exception:
        lines.append("🌐 Chrome：--")

    # 备份
    bak_ok = os.path.isfile(os.path.join(
        config.RUNTIME_DIR,
        "tg_userbot." + time.strftime("%Y%m%d") + ".db"))
    lines.append(f"🗄 今日 DB 备份：{'✅' if bak_ok else '❌ 尚未生成'}")
    if not bak_ok:
        warnings.append("今日 DB 备份尚未生成")
    try:
        from . import cd2
        st = cd2.read_backup_status()
        if st:
            last = max(s[2] for s in st if s[2]) if any(s[2] for s in st) else 0
            if last:
                mins = int((time.time() - last) / 60)
                lines.append(f"☁️ CD2 备份：最近完成 {mins} 分钟前"
                             + ("（超过 24h 未动）" if mins > 1440 else ""))
                if mins > 1440:
                    warnings.append("CD2 备份超过 24h 无完成记录")
    except Exception:
        pass

    lines.append("")
    if warnings:
        lines.append("⚠️ 需关注：")
        for w in warnings:
            lines.append(f"  · {w}")
    else:
        lines.append("✅ 结论：系统健康，无需处理")

    from .text import fit_4096
    return fit_4096("\n".join(lines))


def _open_read_conn():
    """巡检专用只读连接（独立于主连接，避免线程归属问题）。"""
    import sqlite3
    con = sqlite3.connect(f"file:{runtime_db.db_path()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con
