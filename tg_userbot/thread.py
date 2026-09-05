"""下载并发数：读写 thread_config.json、/thread 谓词与设置逻辑。

当前并发数存 state.DOWNLOAD_CONCURRENCY，默认取 config.DOWNLOAD_CONCURRENCY
(=3)；信号量实例 state.DOWNLOAD_SEMAPHORE 在 app.main() 里创建，这里只
在 apply_thread_limit 中调用其 set_limit()（动态调限）。
load_thread_config 失败时保持当前值不变（与单文件时代语义一致）。
"""
import os
import json
import re

from . import state
from .config import (
    DOWNLOAD_CONCURRENCY_MAX,
    DOWNLOAD_CONCURRENCY_MIN,
    THREAD_CONFIG_FILE,
)
from .log import logger


def load_thread_config():
    """启动时读取持久化的并发数；无配置/失败则保持默认（3）。"""
    try:
        if os.path.exists(THREAD_CONFIG_FILE):
            with open(THREAD_CONFIG_FILE, "r", encoding="utf-8") as f:
                value = int(json.load(f).get("concurrency", state.DOWNLOAD_CONCURRENCY))
            if not (DOWNLOAD_CONCURRENCY_MIN <= value <= DOWNLOAD_CONCURRENCY_MAX):
                value = state.DOWNLOAD_CONCURRENCY
            state.DOWNLOAD_CONCURRENCY = value
    except Exception as e:
        logger.warning(f"读取并发配置失败，使用默认 {state.DOWNLOAD_CONCURRENCY}：{e}")


def save_thread_config(concurrency):
    try:
        with open(THREAD_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"concurrency": int(concurrency)}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存并发配置失败：{e}")


def is_thread_command(text):
    return bool(re.fullmatch(r"/thread(?:\s+\d+)?", text.strip(), re.IGNORECASE))


def apply_thread_limit(value):
    """设置并发下载数，返回 (是否成功, 提示文本)。命令与 bot 菜单共用。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return False, (
            f"❌ 格式错误。用法：/thread 3"
            f"（{DOWNLOAD_CONCURRENCY_MIN}-{DOWNLOAD_CONCURRENCY_MAX}）"
        )
    if not (DOWNLOAD_CONCURRENCY_MIN <= n <= DOWNLOAD_CONCURRENCY_MAX):
        return False, (
            f"❌ 格式错误。并发数需在 "
            f"{DOWNLOAD_CONCURRENCY_MIN}-{DOWNLOAD_CONCURRENCY_MAX} 之间"
        )
    state.DOWNLOAD_CONCURRENCY = n
    if state.DOWNLOAD_SEMAPHORE is not None:
        state.DOWNLOAD_SEMAPHORE.set_limit(n)
    save_thread_config(n)
    return True, f"✅ 并发下载数已设置为 {n}"
