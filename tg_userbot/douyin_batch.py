"""抖音作者作品批量下载（/dyu）：主页链接 → 全量作品枚举 → 串行下载。

链路（2026-10-02 可行性实测通过）：
    /dyu <主页链接> [子目录]
      ① SecUserIdFetcher：短链/网页链接 → sec_user_id
      ② fetch_user_profile → 作者昵称（缺省子目录名）
      ③ fetch_user_post_videos 游标翻页 → 全部 aweme（流式：每翻一页就
         入队，下载与枚举并行推进）
      ④ 每条作品：dyc:<aweme_id> 判重 → url 任务入队（direct_url 留空 +
         resolve_first，执行时才解析直链——douyinvod 签名只活 ~3 小时，
         深队列等不起；serial=True 走队列的串行门）
      ⑤ 汇总通知

f2 的 import 副作用（联网取 mssdk msToken，本机网络实测被重置）用离线
假 token 补丁绕开——f2 官方提供的降级路径；签名（X-Bogus/aBogus）本地
计算不受影响。补丁在 import 前打，只打一次。
"""
import asyncio
import uuid
from datetime import datetime

from . import config
from . import dedup
from . import notify
from .log import logger
from .naming import compute_url_filename, sanitize_filename

_f2 = {"loaded": False, "ok": False, "reason": ""}
_ENUM_LOCK = asyncio.Lock()   # 同时只允许一个 /dyu 枚举在跑


def load_f2():
    """补丁化 import f2（幂等）。成功返回 (SecUserIdFetcher, DouyinHandler)。"""
    if _f2["loaded"]:
        if not _f2["ok"]:
            return None
        import f2.apps.douyin.handler as h
        import f2.apps.douyin.utils as u
        return u.SecUserIdFetcher, h.DouyinHandler
    _f2["loaded"] = True
    try:
        # 补丁必须先于 f2.apps.douyin.model 的 import：model 的 pydantic
        # 字段默认值在类定义期就联网取真 msToken（mssdk 直连被重置）
        import f2.apps.douyin.utils as u
        u.TokenManager.gen_real_msToken = classmethod(
            lambda cls: cls.gen_false_msToken())
        import f2.apps.douyin.handler as h
        _f2["ok"] = True
        logger.info("🎵 f2 已加载（离线 msToken 补丁）")
        return u.SecUserIdFetcher, h.DouyinHandler
    except Exception as e:
        _f2["reason"] = f"{type(e).__name__}: {e}"
        logger.warning(f"⚠️ f2 加载失败（/dyu 不可用）：{_f2['reason']}")
        return None


def _handler_kwargs():
    """f2 DouyinHandler 配置：cookie 调用时读（/cookie 更新立即生效）。"""
    return {
        "cookie": getattr(config, "DOUYIN_COOKIE", "") or "",
        "headers": dict(getattr(config, "DOUYIN_HEADERS", {})),
        "proxies": {"http://": None, "https://": None},   # 境内服务直连
        "timeout": 15,
        "max_retries": 2,
    }


def _work_url(aweme_id):
    """aweme_id → 作品页链接（AwemeIdFetcher 的 video/ 正则认得，刷新链
    用它重新解析直链）。"""
    return f"https://www.douyin.com/video/{aweme_id}"


def parse_dyu_command(text):
    """/dyu <主页链接> [子目录] → (url, subdir_raw)；不合法返回 (None, None)。"""
    import re
    m = re.match(r"^/dyu(?:\s+|$)(.*)$", str(text or "").strip(),
                 re.IGNORECASE)
    if not m:
        return None, None
    parts = m.group(1).split()
    if not parts:
        return None, None
    url = parts[0]
    if "douyin.com" not in url:
        return None, None
    subdir = " ".join(parts[1:]) if len(parts) > 1 else ""
    return url, subdir


def build_aweme_record(aweme, author, subdir):
    """单个作品 → url 任务记录（direct_url 留空，执行时解析）。"""
    created = datetime.fromtimestamp(int(aweme.get("create_time") or 0)) \
        if aweme.get("create_time") else datetime.now()
    desc = (aweme.get("desc") or "").strip() or None
    return {
        "id": uuid.uuid4().hex,
        "kind": "url",
        "platform": "douyin",
        "url": _work_url(aweme["aweme_id"]),
        "direct_url": None,
        "resolve_first": True,    # 执行时先解析（规避 3h 签名过期）
        "serial": True,           # 走队列串行门（一次一条）
        "subdir": subdir,
        "title": desc,
        "author": author,
        "user_label": None,
        "final_name": compute_url_filename(desc, created),
        "source": "抖音作者合集",
        "source_link": None,
        "created_at": created.strftime("%Y-%m-%d %H:%M:%S"),
        "dedup_key": dedup.douyin_key(aweme["aweme_id"]),
    }


async def enumerate_author_posts(sec_user_id, max_pages=None):
    """游标翻页枚举作者全部作品。async yields [(aweme_id, desc, create_time)]。

    每页一批（约 20 条）；has_more=0 或翻到上限即止。单页失败抛异常交调用
    方（枚举半途而废时已入队的不受影响，剩余可重发命令补——dyc: 判重
    保证不重复下载）。
    """
    handles = load_f2()
    if handles is None:
        raise RuntimeError(f"f2 不可用：{_f2['reason']}")
    _SecUserIdFetcher, DouyinHandler = handles
    if max_pages is None:
        max_pages = int(getattr(config, "DYU_MAX_PAGES", 200))
    handler = DouyinHandler(_handler_kwargs())
    pages = 0
    async for page in handler.fetch_user_post_videos(
            sec_user_id, page_counts=20):
        pages += 1
        raw = page._to_raw() if hasattr(page, "_to_raw") else {}
        awemes = []
        for a in raw.get("aweme_list") or []:
            aid = a.get("aweme_id")
            if aid:
                awemes.append({"aweme_id": aid,
                               "desc": a.get("desc") or "",
                               "create_time": a.get("create_time")})
        yield awemes, bool(raw.get("has_more"))
        if pages >= max_pages or not raw.get("has_more"):
            return


async def fetch_author_nickname(sec_user_id):
    """作者昵称（缺省子目录名）；失败返回 None。"""
    handles = load_f2()
    if handles is None:
        return None
    _Sec, DouyinHandler = handles
    try:
        profile = await DouyinHandler(_handler_kwargs()).fetch_user_profile(
            sec_user_id)
        raw = profile._to_raw() if hasattr(profile, "_to_raw") else {}
        return (raw.get("user") or {}).get("nickname") or None
    except Exception as e:
        logger.warning(f"🎵 取作者昵称失败（不影响下载）：{e}")
        return None


async def get_sec_user_id(url):
    handles = load_f2()
    if handles is None:
        raise RuntimeError(f"f2 不可用：{_f2['reason']}")
    SecUserIdFetcher, _Handler = handles
    sec_uid = await SecUserIdFetcher.get_sec_user_id(url)
    if not sec_uid:
        raise RuntimeError("未能从链接提取作者 sec_user_id（链接是否为作者主页？）")
    return sec_uid


async def run_dyu(url, subdir_raw):
    """后台编排：枚举 → 逐条判重入队 → 汇总通知。返回给命令层的文案由
    command_reply 同步部分给出；本函数只发异步通知。"""
    if _ENUM_LOCK.locked():
        return "⏳ 已有一个 /dyu 枚举在进行中，稍后再发。"
    async with _ENUM_LOCK:
        try:
            return await _run_dyu_inner(url, subdir_raw)
        except Exception as e:
            logger.exception(f"🎵 /dyu 失败：{e}")
            try:
                await notify.notify_user(
                    f"❌ 抖音作者批量任务失败\n\n{type(e).__name__}: {e}\n"
                    f"链接：{url}\n（已入队的部分不受影响）")
            except Exception:
                pass
            return f"❌ {type(e).__name__}: {e}"


async def _run_dyu_inner(url, subdir_raw):
    from . import queue

    sec_uid = await get_sec_user_id(url)
    nickname = await fetch_author_nickname(sec_uid)
    subdir = sanitize_filename(subdir_raw) if subdir_raw else \
        (sanitize_filename(nickname) if nickname else "")
    label = nickname or sec_uid[:16]
    logger.info(f"🎵 /dyu 开始枚举：{label}（sec_uid {sec_uid[:18]}…）")

    found = queued = skipped = 0
    async for awemes, has_more in enumerate_author_posts(sec_uid):
        for a in awemes:
            found += 1
            key = dedup.douyin_key(a["aweme_id"])
            skip, _notice = dedup.should_skip(key) if key else (False, None)
            if skip:
                skipped += 1
                continue
            record = build_aweme_record(a, nickname, subdir)
            try:
                await queue.enqueue_and_start(record)
                queued += 1
            except Exception as e:
                logger.warning(
                    f"⚠️ 作品入队失败（继续下一条）：{a['aweme_id']} {e}")
        logger.info(f"🎵 枚举累计 {found} 条（新 {queued} / 已有 {skipped}）"
                    f"{'…' if has_more else '（完）'}")

    target = f"下载/抖音/{subdir}" if subdir else "下载/抖音"
    text = (f"🎵 抖音作者批量任务已开始\n\n"
            f"作者：{label}\n"
            f"作品：{found} 个｜新入队 {queued}｜跳过已下载 {skipped}\n"
            f"目录：{target}\n"
            f"模式：串行逐条（解析→下载），完成后逐条通知")
    try:
        await notify.notify_user(text)
    except Exception:
        pass
    return text
