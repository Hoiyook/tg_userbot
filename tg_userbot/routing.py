# -*- coding: utf-8 -*-
"""链接分流的**单一实现**（2026-11-08）。

链接从三个文本入口进入系统：收藏夹消息、bot 对话、看板粘贴框 / PWA。
此前每个入口各抄一份「抖音/IG → 种子 → B站 → X → t.me → 外链台账」，
分流顺序与参数构造重复三处，已经实际漏过（iwara 只挂了 bot 与 API，
收藏夹掉进外链台账；X 只挂收藏夹，看板掉台账）。CLAUDE.md 的「四入口
必须同步」纪律说明这类约束不能靠人记。

本模块把分流拆成两半：

- ``classify(text, allow=...)``：纯识别，返回 ``Decision``（哪一路 +
  该路的 URL 与参数）。**不调用任何有副作用的服务函数**（外链台账的
  ``observe`` 会写库，必须留给 execute）。
- ``execute(decision, event=...)``：只负责调对应模块的既有入口，返回
  ``RouteResult`` 给各入口处理交付方式（回帖 / 面板发送 / JSON 回执）。

``allow`` 是入口策略表（数据，不是逻辑）：入口之间若真有差异，只允许
表达在这张表里；识别与执行永远只有一份。**2026-11-08 起三张表统一为完整集合**
（抖音/IG、种子、iwara、B站、X、t.me、外链台账）——入口差异只允许在
「怎么交付结果」，不允许在「能识别哪些链接」：此前收藏夹缺 iwara、
bot 对话缺抖音/IG 与 t.me，同样的链接换个入口就掉进外链台账。
收藏夹的转发消息仍不记外链台账（``skip_ledger_for_forwarded`` 是记录
策略，不是链接类型差异）。
"""
from dataclasses import dataclass, field

from . import bili_dl
from . import chrome_client
from . import config
from . import iwara
from . import manual_links
from . import platform
from . import state
from . import tme
from . import torrent_offline
from . import x_dl

# 入口策略（顺序无关：真正的优先级在 classify 里固定）。
# 三张表内容必须一致（tests/test_routing.py 钉住）；保留三个名字是为了
# 未来真出现「交付能力」差异时有明确的落点，而不是让入口各抄一份判断。
_ALL_LINK_TYPES = ("douyin", "instagram", "torrent", "iwara", "bili", "x",
                   "tme", "ledger")
SURFACE_ME = tuple(_ALL_LINK_TYPES)
SURFACE_BOT = tuple(_ALL_LINK_TYPES)
SURFACE_API = tuple(_ALL_LINK_TYPES)


@dataclass(frozen=True)
class Decision:
    """一次分流的识别结果。kind='none' 表示没有可处理的链接。"""

    kind: str = "none"
    urls: tuple = ()
    text: str = ""
    message: object = None
    extra: dict = field(default_factory=dict)

    @property
    def handled(self):
        return self.kind != "none"


@dataclass
class RouteResult:
    """执行结果：各入口自行决定怎么交付（回帖 / 面板 / JSON）。"""

    kind: str
    handled: bool = True
    message: str = ""
    buttons: object = None
    urls: tuple = ()
    ref: object = None
    extra: dict = field(default_factory=dict)


def _extract_entity_urls(entities):
    """从 Telegram entity 里补抓 URL（WebPage 预览形态，message.message
    可能只有标题没有链接）。"""
    out = []
    for entity in entities or []:
        url = getattr(entity, "url", None)
        if url:
            out.append(url)
    return out


def classify(text, *, allow, message=None, entities=None,
             skip_ledger_for_forwarded=False):
    """识别文本该走哪一路；纯函数，无副作用、无网络。

    ``allow`` 见模块头部的 SURFACE_* 策略表。``skip_ledger_for_forwarded``
    用于收藏夹：转发消息自带的外链是频道内容，不是「我在外面找到的链接」，
    不登记外链台账。
    """
    text = str(text or "").strip()

    # ── 抖音 / Instagram（可被 AUTOROUTE_DOUYIN 关闭）────────────
    douyin_urls = []
    instagram_urls = []
    if "douyin" in allow and config.AUTOROUTE_DOUYIN:
        douyin_urls = platform.extract_douyin_urls(text)
    if "instagram" in allow:
        instagram_urls = platform.extract_instagram_urls(text)
    if entities and ("douyin" in allow or "instagram" in allow):
        for url in _extract_entity_urls(entities):
            if "douyin" in allow and config.AUTOROUTE_DOUYIN:
                douyin_urls.extend(platform.extract_douyin_urls(url))
            if "instagram" in allow:
                instagram_urls.extend(platform.extract_instagram_urls(url))
    douyin_urls = list(dict.fromkeys(douyin_urls))
    instagram_urls = list(dict.fromkeys(instagram_urls))
    if douyin_urls or instagram_urls:
        kind = "douyin" if douyin_urls else "instagram"
        return Decision(
            kind=kind,
            urls=tuple(douyin_urls + instagram_urls),
            text=text, message=message,
            extra={"douyin_urls": douyin_urls,
                   "instagram_urls": instagram_urls})

    # ── 种子直链 ────────────────────────────────────────────────
    urls = manual_links.extract_urls(text)
    if "torrent" in allow:
        torrent_urls = [u for u in urls if torrent_offline.is_torrent_url(u)]
        if torrent_urls:
            url = torrent_urls[0]
            author = torrent_offline.trailing_author(text, url)
            return Decision(
                kind="torrent", urls=(url,), text=text, message=message,
                extra={"to_folder": (torrent_offline.resolve_to_folder(author)
                                     if author else None)})

    # ── iwara（Chrome Agent 直下）────────────────────────────────
    if "iwara" in allow:
        iwara_urls = [u for u in urls if iwara.extract_iwara_video_id(u)]
        if iwara_urls:
            url = iwara_urls[0]
            return Decision(
                kind="iwara", urls=(url,), text=text, message=message,
                extra={"video_id": iwara.extract_iwara_video_id(url)})

    # ── B站 / X（各自 auto_reply 会从混合文本抽链接）────────────
    if "bili" in allow and config.AUTOROUTE_BILI \
            and bili_dl.looks_like_bili(text):
        return Decision(kind="bili", urls=tuple(urls), text=text,
                        message=message)
    if "x" in allow and config.AUTOROUTE_X and x_dl.looks_like_x(text):
        return Decision(kind="x", urls=tuple(urls), text=text,
                        message=message)

    # ── t.me 帖子 ───────────────────────────────────────────────
    if "tme" in allow:
        tme_urls = tme.extract_tme_post_urls(text)
        if tme_urls:
            return Decision(
                kind="tme", urls=tuple(tme_urls), text=text, message=message,
                extra={"label": tme.strip_link_text(text, tme_urls)})

    # ── 其余外链 → 手动外链台账 ─────────────────────────────────
    if "ledger" in allow and urls:
        if skip_ledger_for_forwarded and getattr(message, "fwd_from", None):
            return Decision(kind="none", urls=tuple(urls), text=text,
                            message=message,
                            extra={"ledger_skipped": "forwarded"})
        return Decision(kind="ledger", urls=tuple(urls), text=text,
                        message=message)

    return Decision(kind="none", urls=tuple(urls), text=text, message=message)


def _lookup_queue_ref(url):
    """抖音/IG 入队后按原始 URL 回查任务 id（看板粘贴历史用）。"""
    try:
        records = (list(state.QUEUE.get("tasks") or [])
                   + list(state.QUEUE.get("retry") or []))
    except Exception:
        return None
    for rec in records:
        if rec.get("url") == url:
            return rec.get("id")
    return None


def _lookup_chrome_ref(url):
    """iwara 提交后按 URL 回查 Chrome task_id（粘贴历史用；best-effort）。

    两条数据源缺一不可：**requests** 是 User Bot 刚写入的提交台账（Agent
    每 5s 轮询才认领，提交瞬间任务只在这里）；**tasks** 是 Agent 认领后的
    实时任务台账。先 requests 后 tasks，各自取最新一条——否则 ref 永远是
    None，看板粘贴历史只能显示「已受理」，后续状态全丢。
    """
    try:
        for req in reversed(chrome_client.load_requests()):
            if req.get("url") == url and req.get("task_id"):
                return req["task_id"]
    except Exception:
        pass
    try:
        from . import chrome_agent
        tasks = chrome_agent.load_tasks(config.CHROME_TASKS_FILE)
        for t in reversed(tasks):
            if t.get("url") == url and t.get("task_id"):
                return t["task_id"]
    except Exception:
        pass
    return None


async def execute(decision, *, event=None, message=None):
    """调用该路对应的既有服务入口；不改变它们的内部行为。

    ``event`` 只在 iwara 需要（``chrome_client.handle_chrome_command`` 要
    一个能收 replies 的事件对象；看板用 _HeadlessEvent，Telegram 用真
    event）。``message`` 可覆盖 decision.message（测试/替身场景）。
    """
    kind = decision.kind
    if kind == "none":
        return RouteResult(kind="none", handled=False, urls=decision.urls)

    if kind in ("douyin", "instagram"):
        msg = decision.message if decision.message is not None else message
        douyin_urls = list(decision.extra.get("douyin_urls") or [])
        instagram_urls = list(decision.extra.get("instagram_urls") or [])
        await platform.relay_platform_links(
            msg, douyin_urls, instagram_urls)
        first = (douyin_urls or instagram_urls or [None])[0]
        # 必须带非空回执：bot 对话把 message 直接发给面板，空文本会让
        # send_message 报「message is empty」；收藏夹入口忽略它（另有通知）。
        return RouteResult(
            kind=kind, urls=decision.urls, ref=_lookup_queue_ref(first),
            message="🔗 链接已受理：本地解析成功会自动入队，"
                    "失败转解析 bot——进度看控制面板通知")

    if kind == "torrent":
        out = await torrent_offline.handle_torrent_url(
            decision.urls[0], to_folder=decision.extra.get("to_folder"))
        return RouteResult(kind=kind, message=out or "", urls=decision.urls)

    if kind == "iwara":
        url = decision.urls[0]
        video_id = decision.extra.get("video_id")
        try:
            hit = iwara.find_previous_success(video_id) if video_id else None
        except Exception:
            hit = None
        if hit:
            return RouteResult(kind="iwara_dup",
                               message=iwara.duplicate_text(hit),
                               urls=decision.urls,
                               extra={"video_id": video_id})
        if event is None:
            raise ValueError("iwara 路由需要 event（Chrome 命令入口）")
        out = await chrome_client.handle_chrome_command(
            event, "/chrome " + url,
            owner_id=chrome_client.resolve_owner_id(state.MY_ID))
        return RouteResult(kind="iwara",
                           message=out or "已提交 Chrome 下载",
                           urls=decision.urls,
                           ref=_lookup_chrome_ref(url),
                           extra={"video_id": video_id})

    if kind == "bili":
        out = await bili_dl.auto_reply(decision.text)
        return RouteResult(kind=kind, message=out or "", urls=decision.urls)

    if kind == "x":
        out = await x_dl.auto_reply(decision.text)
        return RouteResult(kind=kind, message=out or "", urls=decision.urls)

    if kind == "tme":
        out = await tme.handle_tme_url(decision.urls[0])
        return RouteResult(kind=kind, message=out or "", urls=decision.urls,
                           extra={"label": decision.extra.get("label")})

    if kind == "ledger":
        reply, buttons = manual_links.observe(decision.text)
        return RouteResult(kind=kind, message=reply or "",
                           buttons=buttons, urls=decision.urls)

    raise ValueError(f"未知路由类型：{kind}")
