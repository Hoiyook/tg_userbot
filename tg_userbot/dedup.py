"""重复媒体去重：append-only 判重索引 + 入队前检查 + /dedup 开关。

同一频道帖子转发两次 / 同一抖音视频换口令重发会原样再下载一遍（几个 GB
白吃）。判重键用跨转发稳定的元数据：

  * Telegram 媒体  → ``tg:<file_unique_id>``（同一媒体文件在任意聊天/
    转发副本里 unique_id 相同，正是「同一频道帖子转两次」的判据）；
  * 抖音本地解析  → ``dyc:<aweme_id>``（不同分享口令指向同一视频也能判）。

局限（已知且接受）：解析 bot 中转回的视频是 bot 每次重新上传的，
file_unique_id 每次都不同，判不了——那部分靠 dyc 键那条腿覆盖。

纪律：索引文件 append-only 单行追加（与 download_history.txt 同款——
事件循环内单行 ``"a"`` 写原子，永不全量重写），启动时载入内存并裁剪到
DEDUP_MAX_ENTRIES 条（保尾部，超限只在启动原子重写一次）。判重不确定的
（file 缺失 / aweme_id 拿不到）一律放行——不确定性永不拦下载。
"""
import os
import re
import json
from datetime import datetime

from . import state
from .config import (
    DEDUP_CONFIG_FILE,
    DEDUP_INDEX_FILE,
    DEDUP_MAX_ENTRIES,
)
from .log import logger


def media_key(message):
    """Telegram 媒体的判重键 tg:<file.id>；拿不到 → None（放行）。

    用 telethon File.id（document/photo 的服务端文件 ID，跨转发稳定——
    同一媒体转发到任何聊天 id 不变，正是「同一帖子转两次」的判据）。
    注意不能用 file.unique_id：telethon 1.44 的 File 没有该属性，取它
    恒 AttributeError → 静默 None → 去重永不生效（2026-09-08 实测 81 个
    下载 0 入索引的根因）。
    """
    try:
        uid = message.file.id if message.file else None
    except Exception:
        return None
    return f"tg:{uid}" if uid else None


def douyin_key(aweme_id):
    """抖音视频的判重键 dyc:<aweme_id>；拿不到 → None（放行）。"""
    return f"dyc:{aweme_id}" if aweme_id else None


def seen(key):
    """查已下载索引；命中返回 {"date":…, "filename":…}，否则 None。"""
    if not key:
        return None
    return state.DEDUP_INDEX.get(key)


def find_in_queue(key):
    """在途判重：state.QUEUE 的 tasks/retry 里是否已有同键任务。"""
    if not key or state.QUEUE is None:
        return False
    for section in ("tasks", "retry"):
        for record in state.QUEUE.get(section) or []:
            if record.get("dedup_key") == key:
                return True
    return False


def should_skip(key):
    """入队前两级判重总入口。返回 (是否跳过, 通知文本)。

    开关关闭或键为 None → 永远 (False, None)；索引命中（已下载过）→ 拦并
    带原下载信息；队列在途（排队/待重试）→ 拦并提示等它下完。
    """
    if not state.DEDUP_ENABLED or not key:
        return False, None
    prior = seen(key)
    if prior:
        return True, (
            "⏭️ 重复媒体已跳过下载\n\n"
            f"文件：{prior.get('filename') or '(未知)'}\n"
            f"原下载：{prior.get('date') or '(未知)'}\n\n"
            "（/dedup off 可临时关闭去重后重新下载）"
        )
    if find_in_queue(key):
        return True, (
            "⏭️ 相同媒体已在下载队列中，跳过重复入队\n\n"
            "（/dedup off 可临时关闭去重）"
        )
    return False, None


def remember(key, filename, size=None, now=None):
    """成功下载后记入索引：追加一行进文件 + 更新内存 dict。

    lockless 同步单行 append（与 history.append_history 同一纪律）。键为
    None（判重不了）时是 no-op；文件名里的制表/换行就地压成空格防拆行。
    """
    if not key:
        return
    ts = (now or datetime.now()).strftime("%y-%m-%d %H:%M")
    safe_name = str(filename or "(未知)").replace("\t", " ").replace("\n", " ")
    line = f"{key}\t{ts}\t{safe_name}\n"
    try:
        with open(DEDUP_INDEX_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        logger.warning(f"写去重索引失败（不影响下载）：{e}")
    state.DEDUP_INDEX[key] = {"date": ts, "filename": safe_name}


def load_index():
    """启动载入判重索引，返回载入条数；超上限裁剪保尾部（原子重写一次）。"""
    state.DEDUP_INDEX = {}
    try:
        with open(DEDUP_INDEX_FILE, "r", encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    except FileNotFoundError:
        return 0
    except Exception as e:
        logger.warning(f"读去重索引失败，从空索引开始：{e}")
        return 0

    trimmed = False
    if len(lines) > DEDUP_MAX_ENTRIES:
        lines = lines[-DEDUP_MAX_ENTRIES:]
        trimmed = True

    loaded = 0
    for ln in lines:
        parts = ln.split("\t")
        if len(parts) < 3 or not parts[0]:
            logger.warning(f"去重索引跳过坏行：{ln[:40]}…")
            continue
        state.DEDUP_INDEX[parts[0]] = {
            "date": parts[1],
            "filename": "\t".join(parts[2:]),
        }
        loaded += 1

    if trimmed:
        # 只在启动发生：把裁剪后的尾部原子写回（temp + os.replace）
        try:
            temp_path = DEDUP_INDEX_FILE + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                for key, rec in state.DEDUP_INDEX.items():
                    f.write(f"{key}\t{rec['date']}\t{rec['filename']}\n")
            os.replace(temp_path, DEDUP_INDEX_FILE)
            logger.info(
                f"去重索引超上限，已裁剪保留最近 {loaded} 条"
            )
        except Exception as e:
            logger.warning(f"裁剪去重索引失败（下次启动再试）：{e}")
    return loaded


def is_dedup_command(text):
    # /dedup、/dedup on、/dedup off
    return bool(re.fullmatch(r"/dedup(?:\s+(?:on|off))?", text.strip(),
                             re.IGNORECASE))


def set_enabled(enabled):
    """切换去重开关并持久化，返回提示文本（命令与 bot 面板共用）。"""
    state.DEDUP_ENABLED = bool(enabled)
    save_dedup_config(state.DEDUP_ENABLED)
    word = "开启" if state.DEDUP_ENABLED else "关闭"
    return f"✅ 重复媒体去重已{word}"


def status_text():
    """当前去重状态（/dedup 无参与菜单共用）。"""
    if state.DEDUP_ENABLED:
        return (
            f"🛡 重复媒体去重：开启（索引 {len(state.DEDUP_INDEX)} 条）\n"
            "/dedup off 关闭（重新下载已删文件时用）"
        )
    return "🛡 重复媒体去重：关闭\n/dedup on 重新开启"


def save_dedup_config(enabled):
    try:
        with open(DEDUP_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"enabled": bool(enabled)}, f,
                      ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存去重开关失败：{e}")


def load_dedup_config():
    """启动时还原持久化的开关；无配置/失败保持默认开启。"""
    try:
        if os.path.exists(DEDUP_CONFIG_FILE):
            with open(DEDUP_CONFIG_FILE, "r", encoding="utf-8") as f:
                state.DEDUP_ENABLED = bool(json.load(f).get("enabled", True))
    except Exception as e:
        logger.warning(f"读取去重开关失败，保持默认开启：{e}")

