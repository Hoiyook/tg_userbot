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
    return target


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


def daily_maintenance():
    """每日维护入口（app._maintenance_loop 每天调用一次）：
    ① DB 备份（滚动 7 份）② 事件流裁剪（台账封顶）。失败只告警不抛。"""
    try:
        backup_db(config.RUNTIME_DIR)
    except Exception as e:
        logger.warning(f"🗄 每日 DB 备份失败：{e}")
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
    except sqlite3.Error as e:
        logger.warning(f"🗄 事件流裁剪失败：{e}")
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
    except sqlite3.Error as e:
        logger.warning(f"🗄 监听任务终态清理失败：{e}")
    # 凭据健康度巡检（2026-11-12）：零联网字段完整性检查，新失效才告警。
    # 挂最后——它有自己的 try 兜底，绝不影响前面任何清理动作。
    try:
        from . import credentials, notify as _notify_mod
        results, alert = credentials.daily_check_and_alert(
            notify_coro=_notify_mod.notify_user)
        bad = [r for r in results if not r["ok"]]
        if bad:
            logger.warning(f"🔑 凭据巡检：{len(bad)} 站失效（已告警）")
        else:
            logger.info("🔑 凭据巡检：全部正常")
    except Exception as e:
        logger.warning(f"🔑 凭据巡检失败（不影响维护）：{e}")
