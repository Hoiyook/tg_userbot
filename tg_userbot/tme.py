"""t.me 帖子链接直下（2026-11-02 owner）。

收藏夹/App 粘贴 ``https://t.me/<频道>/<帖id>`` 或 ``https://t.me/c/<内部id>/<帖id>``：
bot 本身就是 Telegram 客户端，直接拉原帖媒体走 ``app._enqueue_me`` 同款入队
（相册 caption 继承 / 溯源 / 命名 / 下载侧 file_unique_id 去重全部复用）——
此前这类链接一律落外链台账 PENDING 等人工，实际没人处理。

设计边界：
- 只处理**帖子**链接（带消息 id）；裸频道链接（无 id）不是下载诉求；
- 消息自身带媒体时不抢占（app 分支有 no-media 守卫）——caption 里自带的
  t.me 宣传链接不构成下载指令（沿用 2026-10-30 转发噪声的同类考量）；
- 相册：grouped_id 命中时按 id 窗口 ``[mid-9, mid+9]`` 拉同组成员整组入队
  （Telethon 无「按组拉」API，窗口过滤是通行做法）；
- 拉不到（不在频道/已删除/无权限）如实回报，不落台账、不静默。

``fetcher`` / ``enqueue`` 可注入替身，测试完全离线（sources 同款纪律）。
"""
import re

from .log import logger

# t.me/<用户名>/<帖id> | t.me/c/<内部id>/<帖id>；容忍 s/ 预览前缀与
# ?single 等查询尾。用户名段与 c/ 互斥，帖 id 必须是纯数字。
_TME_POST_RE = re.compile(
    r"^https?://t\.me/(?:s/)?(?:c/(\d+)|([A-Za-z0-9_]{3,32}))/(\d+)"
    r"(?:[/?#].*)?$",
    re.IGNORECASE)


def parse_tme_post(url):
    """t.me 帖子链接 → ``(entity_ref, msg_id)``；非帖子链接返回 ``None``。

    entity_ref：公开频道/群 → 用户名字符串；私有 → 带标记的数字 id
    （``-100<内部id>``，Telethon get_messages 直接认）。
    """
    m = _TME_POST_RE.match(str(url or "").strip())
    if not m:
        return None
    internal, username, mid = m.groups()
    if internal:
        return int("-100" + internal), int(mid)
    return username, int(mid)


def extract_tme_post_urls(text):
    """文本里的 t.me 帖子链接（保序去重；非帖子形态不计）。"""
    from . import manual_links
    out = []
    for u in manual_links.extract_urls(text or ""):
        if parse_tme_post(u) and u not in out:
            out.append(u)
    return out


def strip_link_text(text, urls):
    """把链接从粘贴文本里剥掉，剩下的当命名标注（≥2 字才有效）。"""
    rest = str(text or "")
    for u in urls or []:
        rest = rest.replace(u, " ")
    rest = re.sub(r"\s+", " ", rest).strip()
    return rest if len(rest) >= 2 else ""


def _channel_title(msg):
    chat = getattr(msg, "chat", None)
    return getattr(chat, "title", None) or None


async def handle_tme_url(url, fetcher=None, enqueue=None, on_enqueued=None):
    """处理一条 t.me 帖子链接：拉原帖 → 相册展开 → 逐条入队。

    on_enqueued(msg)：每条成功入队后回调（App 粘贴历史用它收集
    (chat_id, msg_id) 做状态回查）。
    返回回执文本（给收藏夹回复 / App 粘贴回执共用）。绝不抛异常——
    任何失败都收口成 ❌ 文案。
    """
    parsed = parse_tme_post(url)
    if not parsed:
        return "❌ 不是 t.me 帖子链接（缺消息 id）"
    ref, mid = parsed
    if fetcher is None:
        from . import state
        client = getattr(state, "client", None)
        if client is None:
            return "❌ 客户端未就绪"
        fetcher = client.get_messages
    if enqueue is None:
        async def enqueue(msg):
            from . import app
            # chat_id 传频道自身 id：消息 id 在频道坐标系里，缺省的收藏夹
            # 坐标会让下载 Worker 回拉时「原消息已被删除」
            await app._enqueue_me(
                msg, no_label_wait=True,
                chat_id=getattr(msg, "chat_id", None))
    try:
        msgs = await fetcher(ref, ids=mid)
    except Exception as e:
        logger.warning(f"t.me 拉帖失败 {url}：{type(e).__name__}: {e}")
        return f"❌ 拉取帖子失败（不在该频道/无权限/已删除）：{type(e).__name__}"
    msg = msgs[0] if isinstance(msgs, (list, tuple)) else msgs
    if not msg:
        return "❌ 帖子不存在（可能已删除）"

    from .sources import is_downloadable
    title = _channel_title(msg)
    if not is_downloadable(msg):
        return f"ℹ️ 该帖无媒体（纯文本）：{title or ref}"

    group = [msg]
    gid = getattr(msg, "grouped_id", None)
    if gid:
        window = list(range(max(1, mid - 9), mid + 10))
        try:
            around = await fetcher(ref, ids=window)
        except Exception as e:
            logger.warning(f"t.me 相册窗口拉取失败（退回单条）：{e}")
            around = []
        same = sorted(
            (x for x in (around or [])
             if x and getattr(x, "grouped_id", None) == gid
             and is_downloadable(x)),
            key=lambda x: x.id)
        if same:
            group = same

    done = 0
    for m in group:
        try:
            await enqueue(m)
            if on_enqueued is not None:
                try:
                    on_enqueued(m)
                except Exception as e:
                    logger.debug(f"on_enqueued 回调失败：{e}")
            done += 1
        except Exception as e:
            logger.exception(f"t.me 入队失败 msg={getattr(m, 'id', '?')}：{e}")
            break
    if done < len(group):
        return (f"⚠️ 部分入队（{done}/{len(group)}），其余见日志重发一次即可："
                f"{title or ref}")
    kind = f"相册 {len(group)} 个文件" if len(group) > 1 else "1 个文件"
    return f"📥 t.me 帖已入队：{title or ref} · {kind}"


async def backfill_pending(limit=100):
    """历史台账回灌：PENDING 的 t.me 帖子链接逐条拉帖入队 + 销账。

    下载侧 file_unique_id 去重是护栏——「开始下载」过但状态没销的
    重复条目重灌也不会重下。返回回执文本。
    """
    from . import runtime_db
    rows = [r for r in runtime_db.list_manual_links(
                status="PENDING", limit=limit)
            if parse_tme_post(r.get("url") or "")]
    if not rows:
        return "没有待处理的 t.me 链接 ✅"
    lines = []
    for r in rows:
        out = await handle_tme_url(r["url"])
        try:
            runtime_db.manual_link_done(int(r["id"]))
        except Exception as e:
            logger.warning(f"t.me 回灌销账失败 #{r.get('id')}：{e}")
            out += "（⚠️ 台账销账失败）"
        mark = "ℹ️" if out.startswith(("ℹ️", "❌")) else "✅"
        lines.append(f"{mark} {r['url']}\n　{out}")
    return (f"🔁 t.me 历史回灌完成：{len(rows)} 条\n\n" + "\n".join(lines))
