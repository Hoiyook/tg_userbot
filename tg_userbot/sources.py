"""消息 / 来源 / 链接解析。

纯函数：message_link、message_source_link、entity_display_name、
get_media_type、is_downloadable、forward_source_info。
需网络/运行态的：get_forward_source、resolve_download_source（读
state.client）。同一模块内 resolve_download_source 以裸名调用
get_forward_source，保证测试 monkeypatch（patch sources.get_forward_source
后调 sources.resolve_download_source）可见。
"""
from telethon.utils import get_peer_id

from . import state
from .log import logger
from .naming import sanitize_filename


def message_link(chat_id, msg_id):
    """生成可跳转的 Telegram 消息链接。

    频道/超级群组（-100 前缀的负 id）→ https://t.me/c/<id>/<msg_id>；
    私聊与 Saved Messages（正 id）没有链接格式 → None。
    """
    if chat_id is None or msg_id is None:
        return None
    try:
        chat_id = int(chat_id)
        msg_id = int(msg_id)
    except (TypeError, ValueError):
        return None
    if chat_id < 0 and str(chat_id).startswith("-100"):
        return f"https://t.me/c/{str(chat_id)[4:]}/{msg_id}"
    return None


def message_source_link(message, fallback_chat_id=None):
    """消息的来源链接：转发消息优先链到原频道消息（fwd_from.channel_post），
    否则用消息自身的 chat 生成；私聊/Saved Messages 返回 None。"""
    fwd = getattr(message, "fwd_from", None)
    if fwd:
        from_id = getattr(fwd, "from_id", None)
        post = getattr(fwd, "channel_post", None)
        if from_id and post:
            try:
                link = message_link(get_peer_id(from_id), post)
                if link:
                    return link
            except Exception:
                pass
    return message_link(fallback_chat_id, getattr(message, "id", None))


def entity_display_name(entity) -> str:
    """实体 → 可读名称：频道用标题，用户用姓名，都没有用用户名。"""
    try:
        title = getattr(entity, "title", None)
        if title:
            return str(title)

        first_name = getattr(entity, "first_name", "") or ""
        last_name = getattr(entity, "last_name", "") or ""
        username = getattr(entity, "username", "") or ""

        full_name = f"{first_name} {last_name}".strip()
        if full_name:
            return full_name

        return username
    except Exception:
        return ""


async def get_forward_source(message) -> str:
    """获取 Forward From 名称，用作目录名。"""
    try:
        forward = message.fwd_from
        if not forward:
            return "未分类"

        from_id = getattr(forward, "from_id", None)
        if not from_id:
            return "未分类"

        entity = await state.client.get_entity(from_id)

        name = entity_display_name(entity)
        if name:
            return sanitize_filename(name)

    except Exception as e:
        logger.warning(f"获取 Forward From 失败：{e}")

    return "未分类"


def get_media_type(message) -> str:
    """仅用于日志显示。"""
    if not message.media:
        return "None"

    try:
        if message.photo:
            return "Photo"
        if message.video:
            return "Video"
        if message.audio:
            return "Audio"
        if message.voice:
            return "Voice"
        if message.document:
            return "Document"
    except Exception:
        pass

    return type(message.media).__name__


def is_downloadable(message) -> bool:
    """判断消息是否包含可下载媒体。"""
    try:
        # Telethon 对文件、图片、视频等通常都会提供 message.file
        if message.file:
            return True

        # 某些媒体情况下 file 属性可能暂时无法判断，再检查这些属性
        if message.photo or message.document or message.video or message.audio:
            return True

    except Exception:
        pass

    return False


async def resolve_download_source(message, source_override=None):
    """下载来源目录名：白名单 chat 用 chat 标题；否则沿用转发来源。"""
    if source_override:
        return source_override
    return await get_forward_source(message)


def forward_source_info(fwd_from):
    """从转发头提取 (chat_id, 标题)，不依赖网络解析。

    from_name 是 Telegram 在转发来源不可访问（如私密频道）时附带在
    转发头里的标题；有它就不需要 get_entity（bot 账号无权限解析私密频道）。
    返回 (None, None) 表示没有可用的转发来源。
    """
    from_id = getattr(fwd_from, "from_id", None)
    if not from_id:
        return None, None
    chat_id = get_peer_id(from_id)
    name = getattr(fwd_from, "from_name", None) or ""
    return chat_id, name
