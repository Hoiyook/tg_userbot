"""可靠性维护任务（2026-09-18 风险审视 R1/R2 的落地）。

- backup_db：Runtime DB 的在线备份（sqlite backup API，不依赖进程内
  连接、备份期间不锁业务写入）+ 目标目录滚动保留 keep 份。
- 每日周期由 app._maintenance_loop 驱动：备份 + 事件流裁剪一次搞定。
"""
import os
import sqlite3
import time

from . import config
from . import runtime_db
from .log import logger

BACKUP_KEEP = 3            # 备份滚动保留份数（2026-10-29：7→3——
                           # DB 日增量极小，3 份足够回滚，省 ~370MB）
DISK_WARN_FREE_GB = 50     # 磁盘剩余低于此值（GB）→ 每日维护告警
                           # （媒体库 373G，磁盘满只会静默下载失败——
                           # wnacg/pawchive 各有自己的保护线，但主链
                           # 没有全局预警）
EVENTS_TRIM_MAX = 30000    # 与 TASK_EVENTS_MAX_EVENTS 同源（台账封顶）
LISTENER_DONE_RETENTION_DAYS = 90   # 监听任务 SUCCESS/DISMISSED 保留天数


def backup_db(out_dir, keep=BACKUP_KEEP, now=None):
    """在线备份 Runtime DB 到 out_dir/tg_userbot.<YYYYMMDD>.db。

    用 sqlite3 的 backup API（源为独立只读连接）：不依赖也不干扰进程内的
    业务连接（可以 DB 未连接时跑）；滚动清理旧备份只保留最近 keep 份。
    返回备份文件路径；失败抛异常（调用方记日志，不阻塞主流程）。
    """
    os.makedirs(out_dir, exist_ok=True)
    day = time.strftime("%Y%m%d", time.localtime(
        now if now is not None else time.time()))
    target = os.path.join(out_dir, f"tg_userbot.{day}.db")
    src = sqlite3.connect(f"file:{runtime_db.db_path()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    _rotate(out_dir, keep)
    ok, detail = verify_backup(target)
    if not ok:
        # 备份坏了必须高声——备份的"在"不等于"能恢复"，静默坏备份比
        # 没有备份更危险（给人虚假的安全感）
        logger.error(f"🗄 备份完整性验证失败：{target} — {detail}")
    logger.info(f"🗄 Runtime DB 已备份 → {target}（验证 {'✅' if ok else '❌ ' + detail}）")
    return target, ok, detail


def verify_backup(path):
    """备份可恢复性验证（2026-11-12）：integrity_check + 关键表行数抽样。

    "备份文件存在"不等于"能恢复"——磁盘满时 backup API 可能留下截断
    文件。这里对备份副本独立开连接：quick_check 通过 + 5 张核心表都
    SELECT 得出行数，才算验证过。返回 (ok, detail)。"""
    try:
        # immutable=1：纯只读打开，不产生 -shm/-wal 旁文件（此前验证一次
        # 就在备份目录留一对残片——2026-11-10 发现的孤儿残片同源问题）
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        try:
            rc = conn.execute("PRAGMA quick_check").fetchone()[0]
            if rc != "ok":
                return False, f"quick_check: {rc}"
            counts = {}
            for table in ("listener_tasks", "download_events",
                          "wnacg_tasks", "pawchive_posts", "schema_meta"):
                counts[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            size = os.path.getsize(path)
            if size < 4096:
                return False, f"文件过小（{size}B），疑似截断"
            return True, (f"表行数 listener={counts['listener_tasks']} "
                          f"events={counts['download_events']} "
                          f"pawchive={counts['pawchive_posts']}")
        finally:
            conn.close()
    except sqlite3.Error as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def _rotate(out_dir, keep):
    """滚动清理：按 mtime 保最新 keep 份。"""
    files = [
        os.path.join(out_dir, name)
        for name in os.listdir(out_dir)
        if name.startswith("tg_userbot.") and name.endswith(".db")
    ]
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for path in files[keep:]:
        try:
            os.remove(path)
        except OSError:
            pass


def disk_free_gb(path):
    """路径所在卷的剩余 GB（异常返回 None——不阻塞维护）。"""
    try:
        import shutil as _sh
        return _sh.disk_usage(path).free / 1024 ** 3
    except OSError:
        return None


def daily_maintenance():
    """每日维护入口（app._maintenance_loop 每天调用一次）：
    ① DB 备份（滚动 7 份+验证）② 事件流裁剪 ③ 监听终态清理
    ④ 凭据巡检 ⑤ 磁盘预警。汇总一条维护报告推 TG（N4，2026-11-12）。
    失败只告警不抛——维护报告本身也聚合失败项，绝不静默黑盒。"""
    report = []          # 维护报告行（好/坏都记，最后一条 TG 汇总）
    bad = []             # 异常项（决定报告的标题色）

    # ① DB 备份 + 验证
    try:
        target, ok, vdetail = backup_db(config.RUNTIME_DIR)
        report.append(("✅" if ok else "❌", f"备份 {os.path.basename(target)}"
                       + ("" if ok else f" — {vdetail}")))
        if not ok:
            bad.append("备份验证失败")
    except Exception as e:
        logger.warning(f"🗄 每日 DB 备份失败：{e}")
        report.append(("❌", f"备份失败：{type(e).__name__}: {str(e)[:60]}"))
        bad.append("备份失败")
    try:
        # 维护在 to_thread 线程里跑：不能复用主线程的 sqlite 连接
        #（check_same_thread），这里开独立连接直写。业务连接若同时写
        # 也不冲突——WAL + busy_timeout 兜住。
        conn = sqlite3.connect(runtime_db.db_path(), timeout=10)
        try:
            cur = conn.execute(
                "DELETE FROM download_events WHERE id NOT IN "
                "(SELECT id FROM download_events ORDER BY id DESC LIMIT ?)",
                (EVENTS_TRIM_MAX,))
            conn.commit()
            removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        finally:
            conn.close()
        if removed:
            logger.info(f"🗄 事件流已裁剪 {removed} 条（封顶 {EVENTS_TRIM_MAX}）")
        report.append(("✅", f"事件流裁剪 {'-' + str(removed) + ' 条' if removed else '无需'}"))
    except sqlite3.Error as e:
        logger.warning(f"🗄 事件流裁剪失败：{e}")
        report.append(("❌", f"事件流裁剪失败：{str(e)[:50]}"))
        bad.append("事件流裁剪失败")
    try:
        # 监听任务 SUCCESS 终态 90 天清理（2026-10-29）：SUCCESS 行永久
        # 留存是唯一无界的业务表；判重不依赖它（dedup_index 独立），删
        # 旧成功行不影响任何功能。DISMISSED 同为终态一并清。FAILED 保留
        # （是待办）。独立连接（同上，to_thread 线程不能复用业务连接）。
        conn = sqlite3.connect(runtime_db.db_path(), timeout=10)
        try:
            cutoff = int(time.time()) - LISTENER_DONE_RETENTION_DAYS * 86400
            cur = conn.execute(
                "DELETE FROM listener_tasks WHERE status IN (?, ?) "
                "AND completed_at IS NOT NULL AND completed_at < ?",
                (runtime_db.STATUS_SUCCESS, runtime_db.STATUS_DISMISSED,
                 cutoff))
            conn.commit()
            removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        finally:
            conn.close()
        if removed:
            logger.info(f"🗄 监听任务终态清理 {removed} 条"
                        f"（SUCCESS/DISMISSED 保留 {LISTENER_DONE_RETENTION_DAYS} 天）")
        report.append(("✅", f"监听终态清理 {'-' + str(removed) + ' 条' if removed else '无需'}"))
    except sqlite3.Error as e:
        logger.warning(f"🗄 监听任务终态清理失败：{e}")
        report.append(("❌", f"监听终态清理失败：{str(e)[:50]}"))
        bad.append("监听终态清理失败")
    # 凭据健康度巡检（2026-11-12）：零联网字段完整性检查，新失效才告警。
    # 挂最后——它有自己的 try 兜底，绝不影响前面任何清理动作。
    cred_bad_sites = []
    try:
        from . import pawchive as _paw
        cl = _paw.creators_cache_health_line()
        if cl:
            report.append(("⚠️", cl))
    except Exception:
        pass
    try:
        from . import credentials, notify as _notify_mod
        results, alert = credentials.daily_check_and_alert(
            notify_coro=_notify_mod.notify_user)
        cred_bad = [r for r in results if not r["ok"]]
        cred_bad_sites = [r["site"] for r in cred_bad]
        if cred_bad:
            logger.warning(f"🔑 凭据巡检：{len(cred_bad)} 站失效（已告警）")
            report.append(("❌", "凭据失效：" + "、".join(
                f"{r['display']}（{r['detail'][:40]}）" for r in cred_bad)))
            bad.append("凭据失效")
        else:
            logger.info("🔑 凭据巡检：全部正常")
            report.append(("✅", "凭据巡检：四站正常"))
    except Exception as e:
        logger.warning(f"🔑 凭据巡检失败（不影响维护）：{e}")
        report.append(("❌", f"凭据巡检失败：{str(e)[:50]}"))
        bad.append("凭据巡检失败")

    # ⑤ 磁盘容量预警（2026-11-12）：媒体根与运行盘都查，增量告警——
    #    "低于阈值"的新状态才推 TG（恢复/持续低不重复吵），状态落 schema_meta
    disk_bad = []
    try:
        for label, path in (("媒体盘", config.DOWNLOAD_DIR),
                            ("运行盘", os.path.dirname(runtime_db.db_path()))):
            free = disk_free_gb(path)
            if free is None:
                continue
            low = free < DISK_WARN_FREE_GB
            mark = "⚠️" if low else "✅"
            report.append((mark, f"{label} {path} 剩余 {free:.0f}GB"))
            if low:
                disk_bad.append((label, free))
        if disk_bad:
            bad.append("磁盘空间不足")
        _disk_alert(disk_bad, notify=_notify_mod.notify_user
                    if "_notify_mod" in dir() else None)
    except Exception as e:
        logger.warning(f"💾 磁盘预警失败（不影响维护）：{e}")

    # ⑥ 维护报告（一条 TG：N4）——报告自身失败绝不影响主流程
    try:
        icon, title = (("🔴", "每日维护报告（有异常）") if bad
                       else ("🟢", "每日维护报告"))
        body = "\n".join(f"{m} {t}" for m, t in report)
        try:
            import asyncio as _aio
            _aio.run(_notify_mod.notify_user(f"{icon} {title}\n\n{body}"))
        except Exception as _ne:
            logger.warning(f"🗄 维护报告发送失败（忽略）：{_ne}")
        logger.info(f"🗄 每日维护完成：{'异常 ' + str(bad) if bad else '全部正常'}")
    except Exception as e:
        logger.warning(f"🗄 维护报告组装失败：{e}")


def _disk_alert(disk_bad, notify=None):
    """磁盘预警增量推送：状态（各卷是否低于阈值）与上次不同才推。"""
    if not disk_bad:
        key = "-"
    else:
        key = ",".join(f"{label}:{free:.0f}GB" for label, free in disk_bad)
    try:
        prev = runtime_db.get_schema_meta("disk_last_warn")
        runtime_db.set_schema_meta("disk_last_warn", key)
    except Exception as e:
        logger.warning(f"磁盘预警状态写库失败（忽略）：{e}")
        return
    if key == "-" or key == (prev or "-"):
        return
    if notify is not None:
        import asyncio as _aio
        lines = "\n".join(f"  ⚠️ {label} 剩余 {free:.0f}GB（阈值 "
                          f"{DISK_WARN_FREE_GB}GB）" for label, free in disk_bad)
        try:
            _aio.run(notify("💾 磁盘空间预警\n\n" + lines
                            + "\n\n（媒体盘满会导致下载静默失败，请清理或扩容）"))
        except Exception as e:
            logger.warning(f"磁盘预警发送失败（忽略）：{e}")
