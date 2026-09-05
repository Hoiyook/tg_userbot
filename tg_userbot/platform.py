"""平台链接流（抖音 / Instagram）：链接识别 + 转发给解析 bot。

旧版「链接 → 平台对话 → 点按钮 → 平台自下」链路已删除：解析 bot 的私聊
在下载白名单上，其回复的直发视频会由白名单转发流（app.relay_chat_media）
自动转发进 Saved Messages → 走统一媒体下载（落转发来源目录，不再写
Douyin/Instagram 子目录，也不再区分平台短命名）。本模块只负责：

  * 从消息文字提取抖音 / Instagram 链接（extract_*_urls，cleanup 与 app 复用）；
  * 把链接原文发给解析 bot（relay_platform_links），之后等白名单流接手。

不再有 queue ↔ platform 循环依赖；运行态一律走 state.*（client /
PROCESSING_DOUYIN_IDS / WHITELIST_CHATS）。命名/历史等纯函数模块允许 from-import。
"""
from telethon.utils import get_peer_id

from . import state
from .config import (
    DOUYIN_URL_PATTERN,
    INSTAGRAM_URL_PATTERN,
    LOG_FILE,
    PLATFORM_LINKS,
)
from .log import logger


def extract_urls_by_pattern(text: str, pattern):
    """用指定正则从文字中提取链接，去重并补全协议。"""
    if not text:
        return []

    urls = []
    for match in pattern.finditer(text):
        url = match.group(0).strip().rstrip(".,，。！？；;)")
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url

        if url not in urls:
            urls.append(url)

    return urls


def extract_douyin_urls(text: str):
    """从消息文字中提取抖音链接。"""
    return extract_urls_by_pattern(text, DOUYIN_URL_PATTERN)


def extract_instagram_urls(text: str):
    """从消息文字中提取 Instagram 链接。"""
    return extract_urls_by_pattern(text, INSTAGRAM_URL_PATTERN)


async def relay_platform_links(message, douyin_urls, instagram_urls):
    """把一条 Saved Messages 消息中的抖音 / Instagram 链接原文发给解析 bot。

    只负责投递链接与失败提示，不解析、不下载——解析 bot 回复的首条直发视频
    因解析 bot 本身在下载白名单上，会被白名单转发流自动转发进收藏夹下载。
    同一消息 id 通过 state.PROCESSING_DOUYIN_IDS 去重，防重复投递。
    """
    if not douyin_urls and not instagram_urls:
        return

    if message.id in state.PROCESSING_DOUYIN_IDS:
        logger.info(f"⏭️ 消息 ID={message.id} 已在处理中，跳过重复触发")
        return
    state.PROCESSING_DOUYIN_IDS.add(message.id)

    try:
        await _relay_kind_links("douyin", douyin_urls)
        await _relay_kind_links("instagram", instagram_urls)
    finally:
        state.PROCESSING_DOUYIN_IDS.discard(message.id)


async def _relay_kind_links(kind: str, urls):
    """把一个 kind（douyin/instagram）下的若干链接原文发给其解析 bot。"""
    if not urls:
        return
    cfg = PLATFORM_LINKS.get(kind)
    if not cfg:
        logger.warning(f"未知平台 kind：{kind}，跳过转发")
        return
    bot_username = cfg["bot"]
    label = cfg["label"]

    # 解析 bot 须在下载白名单上，其回复视频才会被自动转发进收藏夹下载；
    # 仅探测 + 提醒，不阻断投递（万一 bot 私聊不可转发，白名单流另有直下兜底）。
    try:
        bot_entity = await state.client.get_entity(bot_username)
        bot_id = get_peer_id(bot_entity)
        if bot_id not in state.WHITELIST_CHATS:
            logger.warning(
                f"⚠️ 解析 bot {bot_username} 不在下载白名单：其回复视频不会被"
                "自动转发进收藏夹。请用 /wl add 把它加入白名单"
            )
    except Exception as e:
        logger.warning(f"读取解析 bot 实体失败（不影响转发）：{e}")

    for url in urls:
        try:
            await state.client.send_message(bot_username, url)
            logger.info(
                f"📤 已把{label}链接发给解析 bot（{bot_username}），"
                "回复视频将自动转发进收藏夹下载"
            )
        except Exception as e:
            logger.exception(f"❌ {label}链接处理失败：{url} | {e}")
            try:
                await state.client.send_message(
                    "me",
                    f"❌ {label}链接处理失败\n\n"
                    f"链接：{url}\n"
                    f"请查看：{LOG_FILE}",
                )
            except Exception:
                pass
