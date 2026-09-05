"""下载历史（download_history.txt）读写 + /done 命令谓词。

append_history 是无锁同步追加：整个下载管线是单线程事件循环，单行短写入
在文件末尾 "a" 模式下实际是原子的，无需加锁（旧文档所称 DOWNLOAD_LOCK
并不存在）。失败仅记日志，不影响下载。
"""
import re

from .config import DOWNLOAD_HISTORY_FILE
from .log import logger


def append_history(record: str):
    """向下载历史文件追加一行记录（失败仅记日志，不影响下载）。"""
    try:
        with open(DOWNLOAD_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(record + "\n")
    except Exception as e:
        logger.warning(f"写入下载历史失败：{e}")


def get_history_lines(n=None):
    """读取历史文件最后 n 行（n 为 None 时读全部），文件不存在返回空列表。"""
    try:
        with open(DOWNLOAD_HISTORY_FILE, "r", encoding="utf-8") as f:
            lines = [line.rstrip("\n") for line in f.readlines()]
        if n is None:
            return lines
        return lines[-n:]
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning(f"读取下载历史失败：{e}")
        return []


def is_done_command(text):
    # /done、/done 10、/done 关键词、/done 10 关键词...
    return bool(re.fullmatch(r"/done(?:\s+\S+)*", text.strip(), re.IGNORECASE))
