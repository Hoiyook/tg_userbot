"""下载历史（download_history.txt）读写 + /done 命令谓词。

append_history 是无锁同步追加：整个下载管线是单线程事件循环，单行短写入
在文件末尾 "a" 模式下实际是原子的，无需加锁（旧文档所称 DOWNLOAD_LOCK
并不存在）。失败仅记日志，不影响下载。
"""
import os
import re

from . import runtime_db
from .config import DOWNLOAD_HISTORY_FILE
from .log import logger


def append_history(record: str):
    """追加一行下载历史（失败仅记日志，不影响下载）。

    DB 在连 → download_history 表（行→列拆解，不合 5 段格式进 raw 兜底，
    零丢失）；未连接（测试环境 / init 失败）→ 文件旧路径。
    """
    try:
        if runtime_db.has_connection():
            runtime_db.history_append_record(record)
        else:
            with open(DOWNLOAD_HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(record + "\n")
    except Exception as e:
        logger.warning(f"写入下载历史失败：{e}")


def _read_history_file(path):
    """文件读取（get_history_lines / iter_history_lines 的文件分支）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [line.rstrip("\n") for line in f.readlines()]
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning(f"读取下载历史失败：{e}")
        return []


def get_history_lines(n=None):
    """读取最后 n 行渲染记录（n 为 None 时读全部），不存在返回空列表。

    DB 在连 → download_history 表（渲染行与文件逐字符同格式，消费方
    无感）；未连接 → 文件旧路径。
    """
    if runtime_db.has_connection():
        try:
            return runtime_db.history_lines(n)
        except runtime_db.DbUnavailable as e:
            logger.warning(f"读取下载历史失败：{e}")
            return []
    lines = _read_history_file(DOWNLOAD_HISTORY_FILE)
    if n is None:
        return lines
    return lines[-n:]


def iter_history_lines(path=None):
    """stats/finder 的历史行迭代入口：显式 path（测试/诊断注入口）→
    读该文件；None → DB 在连走表、否则默认文件。渲染行格式两路一致。"""
    if path is None and runtime_db.has_connection():
        try:
            return runtime_db.history_lines(None)
        except runtime_db.DbUnavailable as e:
            logger.warning(f"读取下载历史失败：{e}")
            return []
    return _read_history_file(path or DOWNLOAD_HISTORY_FILE)


def _archive_history_file():
    """旧 download_history.txt 改名 .imported 保留（绝不删除）。"""
    archive = DOWNLOAD_HISTORY_FILE + ".imported"
    try:
        os.replace(DOWNLOAD_HISTORY_FILE, archive)
    except OSError as e:
        logger.warning(f"旧下载历史文件归档失败（原样保留）：{e}")
        return False
    return True


def migrate_history_to_db():
    """启动导入（app.main 调用）：表空 + 文件非空 → 逐行入库（raw 兜底
    零丢失）后改名 .imported；表有行 → DB 赢、文件归档并 WARNING；
    文件缺失/空 → 无动作；导入失败 → 文件原样保留（可观测数据，下轮重试）。"""
    if not runtime_db.has_connection():
        return
    try:
        count = runtime_db.history_count()
    except runtime_db.DbUnavailable as e:
        logger.warning(f"下载历史存储不可用，跳过导入：{e}")
        return
    if not os.path.exists(DOWNLOAD_HISTORY_FILE):
        return
    if count > 0:
        if _archive_history_file():
            logger.warning(
                f"download_history 表已有数据，忽略并归档旧历史文件 → "
                f"{DOWNLOAD_HISTORY_FILE}.imported")
        return
    lines = _read_history_file(DOWNLOAD_HISTORY_FILE)
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        return
    try:
        for ln in lines:
            runtime_db.history_append_record(ln)
    except Exception as e:
        logger.warning(
            f"历史下载记录导入失败（文件原样保留，重启重试）："
            f"{type(e).__name__}: {e}")
        return
    if _archive_history_file():
        logger.info(
            f"🗄 历史下载记录已导入：{len(lines)} 条"
            f"（原文件改名 .imported 保留）")


def is_done_command(text):
    # /done、/done 10、/done 关键词、/done 10 关键词...
    return bool(re.fullmatch(r"/done(?:\s+\S+)*", text.strip(), re.IGNORECASE))
