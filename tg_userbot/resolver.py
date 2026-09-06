"""抖音链接本地解析（f2 库优先，失败降级解析 bot）。

设计目标：摆脱对第三方解析 bot 的单点依赖。桌面端（RESOLVER_ENABLED）
抖音链接先走本模块的 f2 解析：分享短链 → aweme_id → 作品详情接口 →
选最高码率档的播放直链。任何失败（f2 未安装 / import 失败 / 签名过期 /
网络断 / 超时 / 接口风控）一律返回 None，由 platform 层降级回原有
bot 中转路径——最坏情况 = 维持现状，功能永不丢。

集成注意（f2 0.0.1.x 实测源码）：
  * ``f2.apps.douyin.model`` 在 import 时就会联网取真实 msToken
    （pydantic 字段默认值），所以 f2 的 import 本身可能抛
    APIConnectionError——必须连同 import 一起纳入 try/except；
  * ``AwemeIdFetcher.get_aweme_id(url)`` 负责分享短链展开（v.douyin.com
    重定向 + 正则提取）；
  * ``DouyinHandler(kwargs).fetch_one_video(aweme_id)`` 返回
    PostDetailFilter；最高码率档的 URL 不在过滤器的便捷属性里
    （video_bit_rate 只给出数字），要从 ``_to_raw()`` 的完整
    aweme_detail.video.bit_rate 列表里挑 bit_rate 最大项。
"""
import asyncio
from dataclasses import dataclass

from . import config
from .log import logger

# 只警告一次：f2 缺失/损坏时每次链接都刷 warning 会淹掉日志
_f2_unavailable_warned = False


@dataclass
class ResolveResult:
    """一次成功本地解析的结果（喂给队列 url 任务）。"""

    aweme_id: str
    title: str
    author: str
    direct_url: str


def _warn_f2_unavailable(reason: str):
    global _f2_unavailable_warned
    if _f2_unavailable_warned:
        return
    _f2_unavailable_warned = True
    logger.warning(
        f"⚠️ f2 本地解析不可用（{reason}），抖音链接将走解析 bot 兜底"
    )


def _douyin_kwargs() -> dict:
    """构造 f2 DouyinHandler/Crawler 需要的配置 dict。

    DouyinCrawler 会取 kwargs["cookie"]（缺 KeyError）并合并进 headers；
    proxies 显式给「不走代理」的默认值（抖音是境内服务，直连）。
    """
    return {
        "cookie": getattr(config, "DOUYIN_COOKIE", "") or "",
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.douyin.com/",
        },
        "proxies": {"http://": None, "https://": None},
        "timeout": 15,
        "max_retries": 2,
    }


def pick_best_video_url(aweme_detail) -> str:
    """从 aweme_detail 里选最高码率档的播放直链（纯函数，可单测）。

    bit_rate 列表每项形如 {gear_name, bit_rate, play_addr: {url_list}}，
    取 bit_rate 最大的非空项首个 URL；bit_rate 缺失/为空时退回
    video.play_addr.url_list 首个（默认转码档）。两者都拿不到 → None。
    """
    video = (aweme_detail or {}).get("video") or {}

    play_addr = video.get("play_addr") or {}
    fallback_url = ((play_addr.get("url_list") or []) or [None])[0]

    best_url = None
    best_rate = -1
    for item in video.get("bit_rate") or []:
        if not isinstance(item, dict):
            continue
        rate = item.get("bit_rate") or 0
        urls = (item.get("play_addr") or {}).get("url_list") or []
        if urls and rate > best_rate:
            best_url = urls[0]
            best_rate = rate

    return best_url or fallback_url


async def resolve_douyin(url: str):
    """本地解析一条抖音分享链接；任何失败返回 None（调用方降级 bot）。

    整个解析（含 f2 的 import——它会在 import 时联网取 msToken）包在
    try/except + 整体超时里：f2 的任何坏味道都不会把异常漏出去。
    """
    try:
        from f2.apps.douyin.filter import PostDetailFilter  # noqa: F401
        from f2.apps.douyin.handler import DouyinHandler
        from f2.apps.douyin.utils import AwemeIdFetcher
    except Exception as e:
        _warn_f2_unavailable(f"import 失败：{type(e).__name__}: {e}")
        return None

    try:
        return await asyncio.wait_for(
            _resolve_douyin_inner(AwemeIdFetcher, DouyinHandler, url),
            timeout=config.RESOLVER_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"⏰ 本地解析超时（{config.RESOLVER_TIMEOUT_SECONDS}s），"
            f"降级解析 bot：{url}"
        )
        return None
    except Exception as e:
        logger.warning(
            f"⚠️ 本地解析失败，降级解析 bot：{type(e).__name__}: {e} | {url}"
        )
        return None


async def _resolve_douyin_inner(AwemeIdFetcher, DouyinHandler, url: str):
    """真正跑解析的协程（拆出来便于整体 wait_for 超时包裹）。"""
    aweme_id = await AwemeIdFetcher.get_aweme_id(url)
    if not aweme_id:
        logger.warning(f"⚠️ 本地解析未取到 aweme_id：{url}")
        return None

    handler = DouyinHandler(_douyin_kwargs())
    video_filter = await handler.fetch_one_video(aweme_id)

    raw = (video_filter._to_raw() or {}).get("aweme_detail") or {}
    direct_url = pick_best_video_url(raw)
    if not direct_url:
        logger.warning(f"⚠️ 本地解析未取得视频直链：aweme_id={aweme_id}")
        return None

    title = str(video_filter.desc or "").strip()
    author = str(video_filter.nickname or "").strip()
    logger.info(
        f"✅ 抖音本地解析成功：{title or aweme_id}（作者：{author or '未知'}）"
    )
    return ResolveResult(
        aweme_id=aweme_id,
        title=title,
        author=author,
        direct_url=direct_url,
    )
