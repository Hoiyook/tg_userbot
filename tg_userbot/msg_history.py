"""发给 bot 的消息历史（/cmdhis，2026-09-25 用户需求重定义）。

记录 owner 发给 bot 对话的**文本消息**（指令/链接/普通文本都算），
供 /cmdhis 回看与整块复制重发。敏感输入不进历史：cookie 等输入窗口的
文本在窗口分支就被消费（且 cookie 会当场删除原消息），记录点设在全部
输入窗口之后，天然不会碰到；程序自产面板（Reporter/Pawchive 进度）在
更早的守卫已返回，也进不来。

量小（≤50 条）纯个人数据 → JSON 原子写落盘；读写失败只告警，绝不影响
消息本身的处理。
"""
import json
import os

from . import config
from .log import logger

MAX_MESSAGES = 50


def _history_path():
    return getattr(config, "BOT_MSG_HISTORY_FILE", "")


def load_messages(path=None):
    """读消息历史（新→旧）；文件缺失/损坏按空处理。"""
    p = path or _history_path()
    if not p:
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [str(x) for x in data if str(x).strip()]
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning(f"bot 消息历史读取失败（按空处理）：{e}")
        return []


def record_message(text, path=None):
    """追加一条消息（头部插入 = 新→旧；与上一条相同跳过；截 50 条）。

    多行消息原样保留（复制价值就在原文）；纯空白不入。"""
    raw = str(text or "")
    if not raw.strip():
        return
    p = path or _history_path()
    if not p:
        return
    try:
        items = load_messages(p)
        if items and items[0] == raw:
            return
        items.insert(0, raw)
        del items[MAX_MESSAGES:]
        tmp = p + ".tmp"
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception as e:
        logger.warning(f"bot 消息历史写入失败（不影响消息处理）：{e}")


def render_text(limit=30):
    """/cmdhis 正文：代码块化后整块长按即可复制（无序号，复制单行不带前缀）。"""
    items = load_messages()
    if not items:
        return "📜 消息记录：还没有发给 bot 的文本消息"
    shown, used = [], 0
    for m in items[:max(1, int(limit))]:
        if used + len(m) + 1 > 3600:
            break
        shown.append(m)
        used += len(m) + 1
    head = (f"📜 最近发给 bot 的消息（新→旧，{len(shown)}/{len(items)} 条，"
            "长按代码块可复制）")
    return head + "\n" + "\n".join(shown)
