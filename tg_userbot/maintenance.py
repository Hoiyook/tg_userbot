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

BACKUP_KEEP = 7            # 备份滚动保留份数
EVENTS_TRIM_MAX = 30000    # 与 TASK_EVENTS_MAX_EVENTS 同源（台账封顶）


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
    logger.info(f"🗄 Runtime DB 已备份 → {target}")
    return target


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
        removed = runtime_db.download_events_trim(EVENTS_TRIM_MAX)
        if removed:
            logger.info(f"🗄 事件流已裁剪 {removed} 条（封顶 {EVENTS_TRIM_MAX}）")
    except runtime_db.DbUnavailable as e:
        logger.warning(f"🗄 事件流裁剪失败：{e}")
