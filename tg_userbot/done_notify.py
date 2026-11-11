"""下载完成即时通知开关（/donenotify，2026-11-12）。

背景：主队列（收藏夹媒体/URL 直链）的 SUCCESS 长期是静默的（owner 的
收藏夹降噪决策——完成通知只会刷屏）。但长任务（Chrome 大文件、wnacg
大包、批量 /dyu 后续直链）完成无感知，只能 /done 或看板翻。本开关让
"完成即知道"成为**可选项**：默认关（维持降噪），开了之后主队列每个
SUCCESS 推一条精简通知（文件名+大小，bot 对话发，不进收藏夹）。

持久化与 /dedup /thread 同款 JSON 模式；state.DONE_NOTIFY_ENABLED 为
唯一读取面。
"""
import json
import os

from . import state
from .log import logger

def _config_path():
    """配置文件放 RUNTIME_DIR（与 thread_config 同目录策略）；延迟取
    config 避免循环导入。"""
    global _CONFIG_PATH
    if _CONFIG_PATH is None:
        from . import config
        _CONFIG_PATH = os.path.join(config.RUNTIME_DIR,
                                    "done_notify_config.json")
    return _CONFIG_PATH


_CONFIG_PATH = None


def is_command(text):
    """/donenotify [on|off]（无参 = 查看状态）。"""
    import re
    return bool(re.fullmatch(r"/donenotify(?:\s+(on|off))?", text.strip(),
                             re.IGNORECASE))


def handle(text):
    """处理 /donenotify，返回回复文本。"""
    import re
    m = re.fullmatch(r"/donenotify(?:\s+(on|off))?", text.strip(),
                     re.IGNORECASE)
    if not m:
        return None
    if m.group(1):
        set_enabled(m.group(1).lower() == "on")
    return status_text()


def set_enabled(enabled):
    """切换并持久化。"""
    state.DONE_NOTIFY_ENABLED = bool(enabled)
    save_config(state.DONE_NOTIFY_ENABLED)


def status_text():
    word = "开启" if state.DONE_NOTIFY_ENABLED else "关闭（默认，收藏夹降噪）"
    return (f"🔔 下载完成即时通知：{word}\n"
            "开启后主队列每个任务成功都会推送一条精简通知"
            "（站点任务的完成通知不受此开关影响，恒有）。")


def save_config(enabled):
    try:
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump({"enabled": bool(enabled)}, f,
                      ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存完成通知开关失败：{e}")


def load_config():
    """启动时还原；无配置/失败保持默认关闭。"""
    try:
        if os.path.exists(_config_path()):
            with open(_config_path(), "r", encoding="utf-8") as f:
                state.DONE_NOTIFY_ENABLED = bool(
                    json.load(f).get("enabled", False))
    except Exception as e:
        logger.warning(f"读取完成通知开关失败（保持默认）：{e}")


async def notify_success(record):
    """主队列 SUCCESS 的通知点（execute_queued_task 成功分支调用）。

    开关关闭 = no-op。通知发 bot 控制面板（notify_user 自带回落），
    内容精简：文件名 + 大小 + 平台。"""
    if not getattr(state, "DONE_NOTIFY_ENABLED", False):
        return
    try:
        from . import notify
        from .naming import format_size
        size = record.get("total_bytes") or record.get("size_bytes")
        size_line = f"\n大小：{format_size(size)}" if size else ""
        label = (record.get("final_name") or record.get("name")
                 or record.get("url") or "?")
        await notify.notify_user(
            f"✅ 下载完成{size_line}\n{str(label)[:60]}"
            + (f"\n平台：{record.get('platform')}" if record.get("platform")
               else ""))
    except Exception as e:
        logger.warning(f"完成通知发送失败（不影响下载）：{e}")
