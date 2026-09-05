"""下载白名单：读写 whitelist_config.json、命令解析、增删。

运行态白名单 dict 在 state.WHITELIST_CHATS（main() 启动时 load_whitelist()），
增删走 add_to_whitelist/del_from_whitelist；同一模块内它们以裸名调用
save_whitelist / resolve_wl_del_key，保证测试 monkeypatch（patch
whitelist.save_whitelist 后调 add_to_whitelist）可见。
classify_message_chat 为纯函数（入参白名单），供事件入口判定消息归属。
"""
import os
import json
import re

from telethon.utils import get_peer_id

from . import state
from .config import WHITELIST_FILE
from .log import logger
from .naming import sanitize_filename
from .sources import entity_display_name, forward_source_info


def load_whitelist(path=None):
    """读取下载白名单 JSON，返回 {chat_id: 标题}；文件缺失/损坏返回空。"""
    path = path or WHITELIST_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        chats = {}
        for chat_id, title in data.get("chats", []):
            chats[int(chat_id)] = str(title)
        return chats
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"读取白名单配置失败，使用空白名单：{e}")
        return {}


def save_whitelist(chats, path=None):
    """把白名单写入 JSON（按 ID 排序，输出稳定）。"""
    path = path or WHITELIST_FILE
    try:
        pairs = sorted(chats.items())
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"chats": [[int(cid), str(title)] for cid, title in pairs]},
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        logger.warning(f"保存白名单配置失败：{e}")


# 除 Saved Messages（"me"）外，白名单内的 chat 收到媒体消息也会自动下载，
# 保存到 SAVE_FOLDER/<chat标题>/。/wl 命令只在 "me" 生效。
def classify_message_chat(chat_id, my_id, whitelist):
    """决定一条消息属于哪个来源：
    ("me", None)    — 自己的 Saved Messages，全功能（命令/链接解析/下载）
    ("chat", 标题)  — 白名单 chat，仅下载媒体
    (None, None)    — 忽略
    """
    if chat_id == my_id:
        return ("me", None)
    if chat_id in (whitelist or {}):
        return ("chat", whitelist[chat_id])
    return (None, None)


def is_wl_command(text):
    # /wl、/wl list、/wl add @xxx、/wl del 123 ...
    return bool(re.fullmatch(r"/wl(?:\s+\S+)*", text.strip(), re.IGNORECASE))


def parse_wl_command(text):
    """解析 /wl 命令，返回 (动作, 参数)；非 /wl 命令返回 None。

    动作：list / add / del / invalid
    add 参数：@用户名、数字 ID（可负数），空字符串表示配合回复的转发消息。
    """
    if not is_wl_command(text):
        return None
    parts = text.split(maxsplit=1)
    if len(parts) == 1:
        return ("list", None)
    sub = parts[1].strip()
    if sub == "list":
        return ("list", None)
    if sub.startswith("add"):
        return ("add", sub[len("add"):].strip())
    if sub.startswith("del"):
        return ("del", sub[len("del"):].strip())
    return ("invalid", None)


def resolve_wl_del_key(key, whitelist):
    """/wl del 参数 → 要删除的 chat id：先按 ID 匹配，再按列表序号。"""
    try:
        numeric = int(key)
    except (TypeError, ValueError):
        return None
    if numeric in whitelist:
        return numeric
    if numeric > 0 and numeric <= len(whitelist):
        return sorted(whitelist)[numeric - 1]
    return None


async def resolve_wl_target(cli, target, fwd_from=None):
    """把 /wl add 的目标解析成 (chat_id, 标题)，返回 (None, None) 表示失败。

    target：数字 ID / @用户名 / Peer 对象。
    fwd_from：转发消息的 MessageFwdHeader。带 from_name 时优先用它，
    避免对私密频道调用 get_entity 被服务端拒绝。
    """
    if fwd_from is not None:
        chat_id, name = forward_source_info(fwd_from)
        if chat_id is not None:
            if name:
                return chat_id, sanitize_filename(name)
            try:
                entity = await cli.get_entity(fwd_from.from_id)
                return chat_id, sanitize_filename(
                    entity_display_name(entity) or f"chat_{chat_id}"
                )
            except Exception:
                return chat_id, f"chat_{chat_id}"
        return None, None

    try:
        entity = await cli.get_entity(target)
    except Exception:
        return None, None
    chat_id = get_peer_id(entity)
    return chat_id, sanitize_filename(
        entity_display_name(entity) or f"chat_{chat_id}"
    )


def add_to_whitelist(chat_id, title):
    """把 chat 加入下载白名单并持久化，返回 (是否成功, 提示文本)。"""
    if chat_id == state.MY_ID:
        return False, "✅ Saved Messages 始终生效，无需加入白名单"
    if chat_id in state.WHITELIST_CHATS:
        return False, (
            f"✅ 该 chat 已在白名单：{state.WHITELIST_CHATS[chat_id]} ({chat_id})"
        )
    state.WHITELIST_CHATS[chat_id] = title
    save_whitelist(state.WHITELIST_CHATS)
    return True, f"✅ 已加入白名单：{title} ({chat_id})"


def del_from_whitelist(key):
    """从下载白名单移除 chat 并持久化，返回 (是否成功, 提示文本)。"""
    key_id = resolve_wl_del_key(key or "", state.WHITELIST_CHATS)
    if key_id is None:
        return False, "❌ /wl：白名单中没有该 ID/序号。用 /wl 查看列表"
    title = state.WHITELIST_CHATS.pop(key_id)
    save_whitelist(state.WHITELIST_CHATS)
    return True, f"✅ 已从白名单移除：{title} ({key_id})"
