"""消息 / 来源 / 链接解析。

纯函数：message_link、message_source_link、entity_display_name、
get_media_type、is_downloadable、forward_source_info。
需网络/运行态的：get_forward_source、resolve_download_source（读
state.client）。同一模块内 resolve_download_source 以裸名调用
get_forward_source，保证测试 monkeypatch（patch sources.get_forward_source
后调 sources.resolve_download_source）可见。
"""
import asyncio
import json
import os
import time

from telethon.utils import get_peer_id

from . import config
from . import netio
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


# 父消息缓存：{(标记peer, msg_id): (过期时刻 monotonic, message)}。有界 + 有时效
# （防原帖被编辑/删除后长期供旧值），只服务缺省 fetcher。
_FETCH_CACHE = {}
_FETCH_CACHE_MAX = 256
_FETCH_CACHE_TTL = 300.0


def _as_message_id(value):
    """telethon 的消息 id 字段是正整数；其余（None / 字符串 / Mock）一律当没有。

    **刻意不靠真值判断**：`if fwd.channel_post:` 在测试替身（MagicMock）上恒真，
    会让「是不是镜像帖」在假消息上误判成是，整条扫描被跳过——本项目已在
    dedup 的 `file.unique_id` 上吃过一次「属性形状想当然」的亏，这里从判据上
    收死。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def forward_origin_ref(fwd_from):
    """转发头 → 原消息引用 ``(peer, msg_id)``；取不到返回 ``None``。

    两个字段各有各的适用面，实测（scripts/probe_comment_parent.py）：

    * ``channel_post``——转发自**频道帖**时带（镜像帖就是这种），且与
      ``from_id`` 配对即可定位；
    * ``saved_from_peer + saved_from_msg_id``——转发自**超级群**时
      ``channel_post`` 为 **None**，原消息 id 只在这个字段里。收藏夹里
      「从讨论组转来的评论副本」正是这种形状，少了它就回不了源。

    注意**不能**改用 ``message_source_link`` 那套（它只看 channel_post），
    在群转发的副本上会静默拿不到原消息 id。
    """
    if fwd_from is None:
        return None
    from_id = getattr(fwd_from, "from_id", None)
    if not from_id:
        return None
    post = _as_message_id(getattr(fwd_from, "channel_post", None))
    if post:
        return from_id, post
    saved_id = _as_message_id(getattr(fwd_from, "saved_from_msg_id", None))
    saved_peer = getattr(fwd_from, "saved_from_peer", None)
    if saved_peer is not None and saved_id:
        return saved_peer, saved_id
    return None


def is_channel_mirror(message) -> bool:
    """这条消息是不是「频道帖在讨论组里的镜像副本」。

    镜像帖是被转发进讨论组的频道原帖，自带 ``fwd_from.channel_post``。
    **扫描讨论组时要跳过它**：同一篇帖子会在频道侧与讨论组侧各命中一次标签，
    转发会重复（下载有 dedup 拦，转发没有）。纯函数、无 I/O。
    """
    fwd = getattr(message, "fwd_from", None)
    if fwd is None:
        return False
    return _as_message_id(getattr(fwd, "channel_post", None)) is not None


async def _resolve_folder_name(mirror, namer=None):
    """镜像帖 → 该频道帖的落盘目录名（失败返回 ``None``）。

    复用 ``get_forward_source``（它本来就是「转发头 → 目录名」的唯一实现），
    并照样经 netio 收口——这条路径跑在**入队**上，一次网络层取消冒出来会把
    正在入队的任务当停服信号打死（本项目最贵的坑，已咬过三次）。

    ``namer`` 注入替身即可离线测；缺省走主客户端。取不到（含 "未分类" 这个
    失败哨兵值）返回 None，调用方退回按副本自身的 fwd_from 解析目录——**绝不
    把文件丢进一个叫「未分类」的目录**。
    """
    if mirror is None:
        return None
    try:
        if namer is not None:
            name = await namer(mirror)
        else:
            name = await netio.shielded(
                lambda: get_forward_source(mirror),
                config.ORIGIN_FETCH_TIMEOUT_SECONDS,
                "解析原帖目录名",
            )
    except Exception as e:
        logger.warning(f"📎 解析原帖目录名失败，退回副本自身来源：{e}")
        return None
    name = (name or "").strip()
    if not name or name == "未分类":     # get_forward_source 的失败哨兵
        return None
    return name


class _OriginRetryable(Exception):
    """一次上溯尝试因「取父消息失败」（超时/限流/返回空）而中断。

    与「确证无关联」（消息不是评论、链走到头）严格区分：前者**值得整体重试**
    （2026-09-15 事故：突发转发触发限流，30s 超时 → 静默降级落错目录），后者
    重试也不会有不同结果。只拿这个异常驱动 resolve_origin_snapshot 的退避重试。
    """


async def _walk_origin(message, fetch, namer):
    """单次上溯：成功返回快照 dict；确证无关联返回 None；取消息失败抛
    ``_OriginRetryable``（由调用方决定重试）。"""
    cur = message
    # 副本路径：先从转发头回源取到「原消息」本身。取不到**不再**退回副本
    # 自身继续走（副本之间的 reply_to 是转发批次内的串扰，顺着走只会耗尽
    # 跳数后静默降级）——直接判为可重试失败。
    fwd = getattr(cur, "fwd_from", None)
    had_mirror = (_as_message_id(getattr(
        fwd, "channel_post", None)) is not None) if fwd is not None else False
    if fwd is not None and not had_mirror:
        ref = forward_origin_ref(fwd)
        if ref is not None:
            got = await _safe_fetch(fetch, _normalize_peer(ref[0]), ref[1])
            if got is None:
                raise _OriginRetryable(
                    f"回源取原消息失败（{brief_peer(ref[0])}/#{ref[1]}）")
            cur = got

    seen = set()
    for _ in range(int(getattr(config, "ORIGIN_MAX_HOPS", 5))):
        if cur is None:
            raise _OriginRetryable("父消息为空")
        fwd = getattr(cur, "fwd_from", None)
        post = _as_message_id(getattr(fwd, "channel_post", None)) \
            if fwd is not None else None
        if post:
            # 镜像帖 = 原帖的副本：它的文字与 fwd_from.date 就是原帖的。
            caption = (getattr(cur, "message", "") or "").strip()
            date = getattr(fwd, "date", None)
            name = await _resolve_folder_name(cur, namer)
            logger.info(
                f"📎 已解析到频道原帖：消息 #{getattr(cur, 'id', '?')} → "
                f"原帖 {getattr(fwd, 'from_id', None)}/{post}"
                f"（caption {len(caption)} 字，日期 {date}，"
                f"目录 {name or '（取不到，退回原来源）'}）"
            )
            return {
                "peer_id": _peer_id_or_none(getattr(fwd, "from_id", None)),
                "channel_post": post,
                "caption": caption,
                "date": date,
                "source_name": name,
            }
        reply_to = getattr(cur, "reply_to", None)
        pid = _as_message_id(
            getattr(reply_to, "reply_to_msg_id", None)) if reply_to else None
        if not pid:
            # 不是回复 → 绝大多数消息走这里。**静默**：这条路径跑在每条
            # 媒体消息上，打日志就是刷屏（任务书 §6 的「避免刷屏」）。
            # 这是「确证无关联」——重试不会有不同结果，不抛可重试异常。
            return None
        peer = (getattr(reply_to, "reply_to_peer_id", None)
                or getattr(cur, "chat_id", None))
        if peer is None:
            return None
        peer = _normalize_peer(peer)
        key = (peer, pid)
        if key in seen:      # 成环（A 回复 B、B 回复 A）——停，别死循环
            return None
        seen.add(key)
        cur = await _safe_fetch(fetch, peer, pid)
        if cur is None:
            raise _OriginRetryable(f"上溯取消息失败（{brief_peer(peer)}/#{pid}）")
    # 走满跳数都是真实消息（没有取消息失败）——链太深，重试也无益
    logger.warning("📎 评论上溯超过最大跳数仍未撞见镜像帖，放弃继承")
    return None


async def resolve_origin_snapshot(message, fetcher=None, namer=None):
    """把一条消息解析成「频道原帖命名快照」；解析不出来返回 ``None``。

    要解决的是：讨论组里的评论，怎么拿到它评论的那条**频道原帖**的 caption
    与日期。真机实测的形状（**与直觉不同，别按 telethon 文档想当然**）：

    1. 评论的 ``reply_to.reply_to_peer_id`` 恒为 ``None``（97/97 条）——
       「peer + msg_id 直接取父」这条路在本项目里**根本不存在**；
    2. ``Message.get_reply_message()`` 在真实评论上**静默返回 None**——它内部
       走 InputMessageReplyTo，服务端不给。用它 = 功能永远 fallback 且不报错；
    3. 评论的 ``reply_to_msg_id`` 指向的是**群内的镜像帖**（频道帖在讨论组里的
       转发副本），镜像帖自带 ``fwd_from.channel_post`` 和原帖的完整 caption
       与原始日期——**拿到镜像帖就是拿到答案，不必再去频道取一次**。

    所以要「顺着 reply_to 一路向上走」直到撞见镜像帖（评论可以回复评论）。
    副本路径（收藏夹里手动转来的评论）先经 ``forward_origin_ref`` 回源取到群里
    的原消息，再走同一段上溯。

    **准确性保障（2026-09-15）**：取父消息失败（超时/限流/返回空）会退避重试
    整条上溯（ORIGIN_RETRY_ATTEMPTS × ORIGIN_RETRY_DELAY_SECONDS），重试耗尽
    才返回 None 降级——降级前**必须**写失败账本（origin_failures.jsonl）并通知，
    /origin 可查、可溯源。绝不再静默落错目录。确证无关联（消息不是评论）不重试、
    不记账——那不是失败。

    返回值：``{"peer_id", "channel_post", "caption", "date", "source_name"}``
    或 ``None``。软失败一律 ``None``，由调用方 fallback 到消息自身命名——
    **绝不抛异常、绝不让 B 丢失**（任务书 §6）。

    ``fetcher`` 形如 ``async (peer, msg_id) -> message | None``，缺省走
    state.client；``namer`` 形如 ``async (message) -> str``；测试注入替身即可
    完全离线。
    """
    fetch = fetcher or _default_fetcher
    attempts = max(1, int(getattr(config, "ORIGIN_RETRY_ATTEMPTS", 3)))
    delay = float(getattr(config, "ORIGIN_RETRY_DELAY_SECONDS", 30))
    last_reason = None
    for attempt in range(1, attempts + 1):
        try:
            return await _walk_origin(message, fetch, namer)
        except _OriginRetryable as e:
            last_reason = str(e)
            logger.warning(
                f"📎 来源解析第 {attempt}/{attempts} 次失败：{e}"
                + (f"，{delay:.0f}s 后重试" if attempt < attempts else "，已耗尽重试"))
            if attempt < attempts:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            # 真取消（停服）原样上抛；网络层取消已由 netio 收口成 None
            raise
        except Exception as e:
            # 兜底：这条路径跑在**入队**上，任何意外都只能变成「没继承到」，
            # 绝不能把媒体丢掉或把入队打断（任务书 §6）。
            last_reason = f"{type(e).__name__}: {e}"
            logger.warning(f"📎 解析评论所属原帖失败（未预期），退回自身命名：{e}")
            break
    if last_reason is None:
        return None   # 确证无关联（_walk_origin 返回 None），不是失败
    entry = record_origin_failure(message, reason=last_reason, attempts=attempts)
    await _notify_origin_failure(message, entry)
    return None


def brief_peer(peer):
    """/origin 与日志用的 peer 简写（-100… 形式）。"""
    pid = _peer_id_or_none(peer)
    return str(pid) if pid is not None else repr(peer)


# ============================================================
# 来源解析失败账本（origin_failures.jsonl，2026-09-15）
# ============================================================
def record_origin_failure(message, reason, attempts):
    """记一条解析失败：JSONL 单行追加（与 download_history 同款无锁纪律），
    返回账本条目。写失败只告警——账本绝不能把入队打断。"""
    fwd = getattr(message, "fwd_from", None)
    media = getattr(message, "file", None)
    entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "epoch": int(time.time()),
        "saved_chat": brief_peer(getattr(message, "chat_id", None)),
        "saved_msg_id": getattr(message, "id", None),
        "filename": (getattr(media, "name", None) or "")[:120],
        "text": ((getattr(message, "message", "") or "").strip() or "")[:80],
        "from_peer": brief_peer(getattr(fwd, "from_id", None)) if fwd else None,
        "saved_from_peer": brief_peer(getattr(fwd, "saved_from_peer", None)) if fwd else None,
        "saved_from_msg_id": getattr(fwd, "saved_from_msg_id", None) if fwd else None,
        "reason": str(reason)[:300],
        "attempts": int(attempts),
    }
    path = getattr(config, "ORIGIN_FAILURES_FILE", None)
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"📎 写来源解析失败账本出错（不影响入队）：{e}")
    logger.warning(
        f"🕘 来源解析失败已记账：消息 #{entry['saved_msg_id']} "
        f"{entry['filename']}（{entry['reason']}；/origin 查看）")
    return entry


def read_origin_failures(limit=20):
    """读最近 limit 条失败账本（新的在前）。文件缺失/损坏行跳过。"""
    path = getattr(config, "ORIGIN_FAILURES_FILE", None)
    if not path or not os.path.isfile(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError as e:
        logger.warning(f"📎 读来源解析失败账本出错：{e}")
        return []
    out.sort(key=lambda x: x.get("epoch", 0), reverse=True)
    return out[:max(1, int(limit))]


def origin_failures_text(limit=20):
    """/origin 视图：最近失败 + 溯源字段 + 处置提示。"""
    rows = read_origin_failures(limit)
    if not rows:
        return "🕘 没有来源解析失败记录（评论继承一直健康）"
    lines = [f"🕘 来源解析失败账本：最近 {len(rows)} 条（新→旧）", ""]
    for r in rows:
        lines.append(
            f"#{r.get('saved_msg_id')} {r.get('ts')}｜{(r.get('filename') or r.get('text') or '')[:36]}")
        lines.append(f"   原因：{r.get('reason')}（重试 {r.get('attempts')} 次）")
        lines.append(
            f"   溯源：副本 {r.get('saved_chat')}/#{r.get('saved_msg_id')}"
            f" ← 群 {r.get('saved_from_peer')}/#{r.get('saved_from_msg_id')}")
    lines += [
        "",
        "处置：重发该消息到收藏夹可重新解析（已下载过的会因 /dedup 跳过，"
        "需先 /dedup off 或手动移动文件）。",
    ]
    return "\n".join(lines)


async def _notify_origin_failure(message, entry):
    """降级前的硬性汇报（用户要求：失败必须可见、可溯源）。一行一条，紧凑。"""
    try:
        from . import notify
        await notify.notify_user(
            f"🕘 来源解析失败：{(entry.get('filename') or entry.get('text') or '')[:40]}"
            f"（{entry.get('reason')}）\n"
            f"将按转发来源目录落盘（可能落进群目录）；详情 /origin")
    except Exception as e:
        logger.warning(f"📎 来源解析失败通知发送出错（不影响入队）：{e}")


def _normalize_peer(peer):
    """统一成标记 id（-100…）：PeerChannel 之类的对象也归一，缓存键才稳定。"""
    if isinstance(peer, int):
        return peer
    pid = _peer_id_or_none(peer)
    return pid if pid is not None else peer


def _peer_id_or_none(peer):
    try:
        return get_peer_id(peer)
    except Exception:
        return None


async def _safe_fetch(fetch, peer, msg_id):
    """取一条消息；任何失败都只是「这条没取到」，绝不冒给调用方。

    ``except Exception`` 不会吞掉 ``CancelledError``（py3.8+ 它是 BaseException）
    ——调用方被真取消时照常上抛；网络层取消由缺省 fetcher 的 netio 收口转成
    None，不会走到这里。
    """
    try:
        return await fetch(peer, msg_id)
    except Exception as e:
        logger.warning(f"📎 读取评论父消息失败（{peer}/{msg_id}）：{e}")
        return None


# 回源取消息的全局限速：并发闸 + 最小间隔。2026-09-15 事故的根因就是一次
# 转发 ~40 个文件时几十个取消息请求并发砸向同一群组触发 Telegram 限流。
# asyncio 原语禁止 import 期创建（事件循环规则）→ 懒初始化。
_FETCH_SEMAPHORE = None
_LAST_FETCH_AT = 0.0


def _fetch_gate():
    global _FETCH_SEMAPHORE
    if _FETCH_SEMAPHORE is None:
        _FETCH_SEMAPHORE = asyncio.Semaphore(
            int(getattr(config, "ORIGIN_FETCH_CONCURRENCY", 3)))
    return _FETCH_SEMAPHORE


async def _default_fetcher(peer, msg_id):
    """缺省取消息实现：主客户端 + 全局限速 + netio 收口 + 缓存。

    缓存放在**这里**而不是 ``resolve_origin_snapshot`` 里，是有意的：注入
    fetcher 的单测因此完全碰不到缓存，不会被跨用例的残留状态干扰；而生产路径
    该省的一次都不多发。

    省的是真金白银：一个帖子的 N 条评论的父消息是**同一条镜像帖**，一个媒体
    突发里 N 个成员也常常指向同一条原消息——没有缓存就是 N 次重复请求，正是
    任务书 §11 点名的请求风暴。

    限速（并发闸 + 最小间隔）是 2026-09-15 事故的根治：突发批量转发时几十个
    取消息并发砸同一群组会触发服务端限流，30s 保护超时后整条继承链静默降级。
    """
    global _LAST_FETCH_AT
    client = state.client
    if client is None:
        return None
    key = (_normalize_peer(peer), int(msg_id))
    now = time.monotonic()
    cached = _FETCH_CACHE.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    async with _fetch_gate():
        pace = float(getattr(config, "ORIGIN_FETCH_PACE_SECONDS", 0.4))
        if pace > 0:
            wait = _LAST_FETCH_AT + pace - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            _LAST_FETCH_AT = time.monotonic()
        msg = await netio.shielded(
            lambda: client.get_messages(peer, ids=msg_id),
            config.ORIGIN_FETCH_TIMEOUT_SECONDS,
            "读取评论父消息",
        )
    if msg is not None:
        if len(_FETCH_CACHE) >= _FETCH_CACHE_MAX:
            # 先清过期；一个都没过期就整个清掉（简单、有界，不值得为它上 LRU）
            for k in [k for k, v in _FETCH_CACHE.items() if v[0] <= now]:
                _FETCH_CACHE.pop(k, None)
            if len(_FETCH_CACHE) >= _FETCH_CACHE_MAX:
                _FETCH_CACHE.clear()
        _FETCH_CACHE[key] = (now + _FETCH_CACHE_TTL, msg)
    return msg


def clear_origin_cache():
    """清空父消息缓存（单测/排查用）。"""
    _FETCH_CACHE.clear()


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
