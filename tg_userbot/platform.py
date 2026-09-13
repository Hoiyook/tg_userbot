"""平台链接流（抖音 / Instagram）：链接识别 + 本地解析优先 / 转发解析 bot。

旧版「链接 → 平台对话 → 点按钮 → 平台自下」链路已删除：解析 bot 的私聊
在下载白名单上，其回复的直发视频由白名单事件生产者（app.record_whitelist_media）
记为转发任务、经 listener_worker 转发进 Saved Messages → 走统一媒体下载
（落转发来源目录，不再写 Douyin/Instagram 子目录，也不再区分平台短命名）。
本模块只负责：

  * 从消息文字提取抖音 / Instagram 链接（extract_*_urls，cleanup 与 app 复用）；
  * 桌面端（config.RESOLVER_ENABLED）抖音链接先走 resolver 本地解析，
    成功 → 入 kind=url 队列任务直接 HTTP 下载（不再依赖解析 bot 在线）；
    失败/超时/未启用 → 链接原文发给解析 bot（_relay_kind_links），
    之后等白名单流接手（原有路径原封不动，作为兜底永不丢）。

不再有 queue ↔ platform 循环依赖；运行态一律走 state.*（client /
PROCESSING_DOUYIN_IDS / WHITELIST_CHATS）。命名/历史等纯函数模块允许 from-import。
"""
import re
import uuid
from datetime import datetime

from telethon.utils import get_peer_id

from . import state
from . import notify
from . import config
from . import dedup
from . import stats
from .config import (
    DOUYIN_URL_PATTERN,
    INSTAGRAM_URL_PATTERN,
    LOG_FILE,
    PLATFORM_LINKS,
)
from .log import logger
from .naming import compute_url_filename


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


def extract_link_comment(text, urls):
    """从「评论 + 链接」混合消息里提取链接外的文字作为命名标注（纯函数）。

    例：'自存 https://v.douyin.com/x' → '自存'。纯链接、命令（/ 开头）、
    空文本 → None。抖音分享口令的剩余文案（'6.46 mqR:/ … 复制此链接，
    打开Dou音搜索…'）不是用户标注，也判 None——否则每次粘贴分享文案
    都会把这些碎屑拼进文件名。与媒体转发的待关联标注窗口不同，链接的
    评论必须和链接同一条消息（媒体是「前一条评论消息 + 后续媒体」）。
    """
    # 已见的分享口令变体：'复制此链接，打开Dou音搜索…' 与
    # '复制打开抖音，看看【xx的作品】…'——标记词按出现过的格式累加；
    # 长度上限兜住没见过的新变体（真用户标注不会长到 60 字）
    _share_junk_markers = (
        "复制此链接", "复制打开抖音", "打开Dou音搜索", "打开抖音搜索",
        "直接观看视频", "看看【",
    )
    if not text:
        return None
    comment = text
    for url in urls or []:
        comment = comment.replace(url, " ")
    comment = re.sub(r"\s+", " ", comment).strip()
    if not comment or comment.startswith("/"):
        return None
    if len(comment) > 60:
        return None
    if any(marker in comment for marker in _share_junk_markers):
        return None
    return comment


def build_url_record(kind, url, result, user_label=None):
    """组装一条 kind=url 队列任务记录（入队展示与下载命名共用 final_name）。

    result：resolver.ResolveResult。final_name 在入队时算好并持久化，
    与 download_url_media 实际落盘名保持一致（媒体任务的同一约定）。
    """
    created_at = datetime.now()
    record = {
        "id": uuid.uuid4().hex,
        "kind": "url",
        "url": url,
        "platform": kind,
        "direct_url": result.direct_url,
        "title": result.title,
        "author": result.author,
        "user_label": user_label,
        "final_name": compute_url_filename(
            result.title, created_at, label=user_label
        ),
        # 落盘目录：SAVE_FOLDER/抖音（见 download._douyin_folder）
        "source": PLATFORM_LINKS.get(kind, {}).get("label", kind),
        "source_link": None,
        "created_at": created_at.strftime("%Y-%m-%d %H:%M:%S"),
    }
    dedup_key = dedup.douyin_key(getattr(result, "aweme_id", None))
    if dedup_key:
        record["dedup_key"] = dedup_key  # 在途判重 + 成功后 remember 复用
    return record


async def relay_platform_links(message, douyin_urls, instagram_urls):
    """处理一条 Saved Messages 消息中的抖音 / Instagram 链接。

    桌面端抖音链接先试本地解析（resolver）：成功 → url 任务入队，bot 不再
    参与该链接；失败/超时/未启用 → 链接原文发给解析 bot（原有路径兜底）。
    Instagram 始终走 bot（匿名本地解析不可行）。同一消息 id 通过
    state.PROCESSING_DOUYIN_IDS 去重，防重复触发。
    """
    if not douyin_urls and not instagram_urls:
        return

    if message.id in state.PROCESSING_DOUYIN_IDS:
        logger.info(f"⏭️ 消息 ID={message.id} 已在处理中，跳过重复触发")
        return
    state.PROCESSING_DOUYIN_IDS.add(message.id)

    try:
        await _handle_douyin_urls(message, douyin_urls)
        await _relay_kind_links("instagram", instagram_urls)
    finally:
        state.PROCESSING_DOUYIN_IDS.discard(message.id)


async def _handle_douyin_urls(message, douyin_urls):
    """抖音链接分流：本地解析优先（桌面端），失败/未启用降级 bot 中转。"""
    if not douyin_urls:
        return

    if not config.RESOLVER_ENABLED:
        await _relay_kind_links("douyin", douyin_urls)
        return

    # 函数内导入：隔离 f2 的 import 副作用（联网取 msToken），且避免
    # platform → resolver → f2 在无关场景（TG_RESOLVER=off / Termux）被触发
    from . import resolver
    from . import queue

    user_label = extract_link_comment(message.message or "", douyin_urls)
    remaining = []
    for url in douyin_urls:
        try:
            result = await resolver.resolve_douyin(url)
        except Exception as e:
            # resolve_douyin 理论上不抛（内部已全捕获），防御性兜底：
            # 任何异常一律视同解析失败走 bot，链接绝不静默丢失
            logger.warning(
                f"⚠️ 本地解析异常，降级 bot：{type(e).__name__}: {e} | {url}"
            )
            result = None
        if result is None:
            remaining.append(url)
            continue
        # 入队前判重（dyc:<aweme_id> 两级：已下载索引 + 在途队列）：同一视频
        # 换个分享口令重发也判得住；命中只跳过入队、不再降级 bot（已下载过，
        # 再中转一份就是白下）。键拿不到（aweme_id 缺失）照常入队。
        key = dedup.douyin_key(getattr(result, "aweme_id", None))
        skip, notice = dedup.should_skip(key)
        if skip:
            logger.info(f"⏭️ 抖音重复视频跳过入队：{url}")
            # 台账输入侧事件：收到但未产生下载任务
            stats.emit_event("DEDUP_SKIPPED")
            try:
                await notify.notify_user(notice)
            except Exception as e:
                logger.warning(f"发送重复视频通知失败：{e}")
            continue
        try:
            record = build_url_record("douyin", url, result, user_label=user_label)
            await queue.enqueue_and_start(record)
        except Exception as e:
            # 入队失败（锁/磁盘等）同样不能让链接静默丢失 → bot 兜底
            logger.warning(
                f"⚠️ url 任务入队失败，降级 bot：{type(e).__name__}: {e} | {url}"
            )
            remaining.append(url)
            continue
        logger.info(f"🛠 抖音链接已本地解析并入队下载：{record['final_name']}")
        try:
            await notify.notify_user(
                "🛠 本地解析成功，已入队下载\n\n"
                f"文件：{record['final_name']}\n"
                f"链接：{url}",
            )
        except Exception as e:
            logger.warning(f"发送本地解析通知失败：{e}")

    if remaining:
        await _relay_kind_links("douyin", remaining)


async def relay_links_to_parse_bot(kind: str, urls):
    """把链接原文发给解析 bot 的公开入口（与 _relay_kind_links 同路径）。

    download 的直链失效降级（_delegate_url_task_to_bot）用它把原始分享
    链接兜底转给解析 bot——bot 在自己服务端解析、不依赖本地 cookie。
    """
    await _relay_kind_links(kind, urls)


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
                await notify.notify_user(
                    f"❌ {label}链接处理失败\n\n"
                    f"链接：{url}\n"
                    f"请查看：{LOG_FILE}",
                )
            except Exception:
                pass
