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
import time
import uuid
from datetime import datetime

from . import config
from . import dedup
from . import notify
from . import state
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


def merge_cookie_fragments(base_cookie, fragments):
    """base cookie 串按名字段合并 fragments（同名覆盖，其余保留）。

    典型：登录态（sessionid 等）来自用户 Chrome，时效件（msToken/ttwid）
    来自无头采集——msToken 是设备级滚动值，不与登录态绑定，可跨源拼接。
    """
    if not fragments:
        return base_cookie or ""
    parts = [p.strip() for p in (base_cookie or "").split(";") if p.strip()]
    kept, seen = [], set()
    for p in parts:
        name = p.split("=", 1)[0].strip().lower()
        if name in {k.lower() for k in fragments}:
            continue          # 待会由 fragments 统一提供
        kept.append(p)
        seen.add(name)
    for k, v in fragments.items():
        if v:
            kept.append(f"{k}={v}")
    return "; ".join(kept)




async def refresh_cookie_from_browser():
    """从本地浏览器取新鲜 douyin cookie（含时效敏感的 msToken）。

    2026-10-02 生产实测：secrets 里的陈旧 cookie 触发 403 风控——抖音的
    msToken 几小时就过期。Chrome 路径 browser_cookie3 可在浏览器运行中读
    （macOS 首次弹钥匙串授权框）。成功 → save_douyin_cookie 持久化 + 实时
    生效（单链接解析链同时受益）；失败 → 保留现值，仅记日志。
    """
    browser = str(getattr(config, "DYU_BROWSER_COOKIE", "chrome")).lower()
    if browser in ("", "off", "none"):
        return False
    from . import browser_cookies
    try:
        cookie_str, err = await asyncio.to_thread(
            browser_cookies.load_browser_cookie_string, browser)
    except Exception as e:
        logger.info(f"🎵 浏览器 cookie 读取异常（沿用现值）：{e}")
        return False
    if err or not cookie_str:
        logger.info(f"🎵 浏览器 cookie 不可用（沿用现值）：{err or '空'}")
        return False
    # msToken 是 post 接口的硬门槛（false/缺失必 403）。注意：Chrome 打开
    # 作者页并不会把它落成 cookie（新版 webmssdk 在内存里用）——枚举的
    # 免疫路径是 Chrome DOM 收割（harvest_author_links_via_chrome），
    # 这里只做守卫：没有就不采用，绝不覆盖好值。
    if "mstoken=" not in cookie_str.lower():
        logger.warning(
            "🎵 仍无 msToken（未采用，沿用现值）——请确认 Chrome 可用")
        return False
    save_err = config.save_douyin_cookie(cookie_str)
    if save_err:
        logger.warning(f"🎵 浏览器 cookie 持久化失败（仅本次生效）：{save_err}")
    else:
        logger.info(f"🎵 已从 {browser} 保鲜 douyin cookie（含新 msToken）")
    return True


def _handler_kwargs():
    """f2 DouyinHandler 配置：cookie 调用时读（/cookie 与浏览器保鲜立即生效）。"""
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


# 在途目录戳：转交解析 bot 的瞬间登记，bot 回流的**下一个**视频媒体
# 消息盖作者目录章（20s/条串行转交 → 在途至多一条，一进一出可靠关联；
# bot 回文本/超时不消费，TTL 过期自然作废，下一轮转交重盖）
_BOT_STAMP = {"subdir": None, "aweme_id": None, "expires": 0.0}


def stamp_next_bot_video(subdir, aweme_id, ttl=180.0):
    """登记：解析 bot 即将回流的下一个视频落 <抖音>/<subdir>/。"""
    if not subdir:
        return
    _BOT_STAMP.update(subdir=str(subdir), aweme_id=str(aweme_id or ""),
                      expires=time.time() + ttl)
    logger.info(f"🎵 已盖在途目录戳：抖音/{subdir}（等待解析 bot 回流）")


def pop_bot_stamp():
    """取走当前戳（一次性）；过期/未盖返回 None。"""
    if _BOT_STAMP["subdir"] and time.time() <= _BOT_STAMP["expires"]:
        subdir = _BOT_STAMP["subdir"]
        _BOT_STAMP.update(subdir=None, aweme_id=None, expires=0.0)
        return subdir
    return None


def status_text():
    """裸 /dyu：批量任务进度（队列在 SQLite，跨重启续跑）。"""
    from . import queue as queue_mod
    rows = list(state.QUEUE.get("tasks") or []) + \
        list(state.QUEUE.get("retry") or [])
    mine = [r for r in rows if r.get("serial")
            and r.get("source") == "抖音作者合集"]
    pending = [r for r in mine if r.get("id") not in state.EXECUTING]
    executing = [r for r in mine if r.get("id") in state.EXECUTING]
    retry = [r for r in mine if r in (state.QUEUE.get("retry") or [])]
    lines = ["🎵 抖音作者批量进度", ""]
    lines.append(f"待下载 {len(pending)} · 下载中 {len(executing)}"
                 f" · 待重试 {len(retry)}")
    for r in executing[:2]:
        lines.append(f"🔄 正在：{r.get('final_name', '')[:46]}")
    for r in pending[:3]:
        lines.append(f"⏳ 排队：{r.get('final_name', '')[:46]}")
    if len(pending) > 3:
        lines.append(f"  … 共 {len(pending)} 条")
    if retry:
        for r in retry[:3]:
            lines.append(f"🔁 重试：{r.get('final_name', '')[:40]}"
                         f"（第 {r.get('attempts', 0)} 次）")
    # 最近完成（runtime_db 只读查询，bot 进程内合法）
    try:
        import sqlite3
        from . import runtime_db
        def _q(c):
            from .runtime_db import _execute
            return _execute(
                c,
                "SELECT filename FROM download_history WHERE source LIKE ? "
                "ORDER BY id DESC LIMIT 3",
                ("抖音作者合集%",)).fetchall()
        rows_done = runtime_db._read(_q, "查作者合集最近完成") \
            if runtime_db.has_connection() else []
        names = [r[0] for r in rows_done]
        if names:
            lines.append("")
            lines.append("最近完成：")
            for n in names:
                lines.append(f"✅ {n[:46]}")
    except Exception as e:
        logger.debug(f"🎵 完成记录读取失败：{e}")
    lines.append("")
    lines.append("说明：任务全部持久化在 SQLite——重启/关机后自动续跑；"
                 "已完成的凭 dyc: 判重永不重下。")
    return "\n".join(lines)


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


def parse_video_hrefs(href_list):
    """DOM href 列表 → 去重保序的 aweme 列表 [{aweme_id, desc, …}]。

    纯函数（可单测）：/video/{id} 提取 id，query/尾斜杠剥掉；非视频
    href 忽略。desc 由调用方另行从 DOM 文本补（此处只管 id）。
    """
    import re as _re
    seen, out = set(), []
    pat = _re.compile(r"/video/(\d+)")
    for href in href_list:
        m = pat.search(str(href or ""))
        if not m:
            continue
        aid = m.group(1)
        if aid in seen:
            continue
        seen.add(aid)
        out.append({"aweme_id": aid, "desc": "", "create_time": None})
    return out


def scroll_should_stop(counts, stable_rounds=2):
    """滚动收割的停止判定（纯函数）：连续 stable_rounds 轮计数无增长。

    counts = 历轮去重链接数序列。到顶（页面加载完）后计数不再涨。
    """
    if len(counts) >= stable_rounds + 1 \
            and counts[-1] == counts[-(stable_rounds + 1)]:
        return True
    return False


async def harvest_author_links_via_chrome(sec_user_id, max_items=None,
                                           max_scrolls=60):
    """Agent 真 Chrome 打开作者页，注入登录态后滚动收割全部作品链接。

    2026-10-02 生产实测链路：游客只有 8 条预览（「登录后免费畅享」墙）；
    注入用户 Chrome 的 douyin 登录态（sessionid 等）后全量可见；列表滚动
    容器是内层 [class*=route-scroll-container]（window 滚动无效）。
    浏览器真指纹 + 页面自己签名 → 对 f2 的 403 风控免疫（同日实测：
    f2 被拒期间页面照常渲染，102 条稳定收满）。需要 Chrome Agent 在线，
    不在线时自动拉起一次。返回 (awemes, err)——desc 有、无发布时间
    （文件名日期退化为入队日）。
    """
    import json as _json
    import urllib.request

    import websockets
    from . import config as _cfg

    if max_items is None:
        max_items = int(getattr(_cfg, "DYU_CHROME_MAX_ITEMS", 2000))
    port = int(getattr(_cfg, "CHROME_CDP_PORT", 9222))
    if not await _ensure_cdp(port):
        return None, f"Chrome Agent CDP({port}) 不可达且自动拉起失败"
    ws_url = _json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{port}/json/version",
        timeout=3).read())["webSocketDebuggerUrl"]

    pause = float(getattr(_cfg, "DYU_CHROME_SCROLL_PAUSE", 2.0))
    init_wait = float(getattr(_cfg, "DYU_CHROME_INIT_WAIT", 5.0))
    async with websockets.connect(ws_url, open_timeout=10,
                                   max_size=16 * 1024 * 1024) as ws:
        _id = [0]

        async def call(method, params=None, session_id=None):
            _id[0] += 1
            msg = {"id": _id[0], "method": method, "params": params or {}}
            if session_id:
                msg["sessionId"] = session_id
            await ws.send(_json.dumps(msg))
            while True:
                recv = _json.loads(await asyncio.wait_for(ws.recv(),
                                                          timeout=60))
                if recv.get("id") == _id[0]:
                    if "error" in recv:
                        raise RuntimeError(
                            str(recv["error"].get("message", "?")))
                    return recv.get("result", {})

        tgt = await call("Target.createTarget", {"url": "about:blank"})
        sid = (await call("Target.attachToTarget",
                          {"targetId": tgt["targetId"],
                           "flatten": True}))["sessionId"]

        async def eval_js(expr):
            r_ = await call("Runtime.evaluate",
                            {"expression": expr, "returnByValue": True},
                            session_id=sid)
            return r_.get("result", {}).get("value")

        try:
            # 注入用户 Chrome 的 douyin 登录态（免扫码；游客只见 8 条）
            jar = await asyncio.to_thread(
                _user_douyin_cookie_jar)
            if jar:
                await call("Storage.setCookies", {"cookies": jar},
                           session_id=sid)
            else:
                logger.warning("🎵 未取到用户登录 cookie，游客模式收割"
                               "（仅前几个作品）")
            await call("Page.navigate",
                       {"url": f"https://www.douyin.com/user/{sec_user_id}"},
                       session_id=sid)
            await asyncio.sleep(init_wait)
            # 验证码中间页（2026-10-02 生产实测：短时多次访问触发）：页面
            # 留在屏幕上让 owner 手动滑一次，标签不关（关了就没得滑了）
            title = str(await eval_js("document.title") or "")
            if "验证" in title:
                try:
                    await notify.notify_user(
                        "🎵 抖音要求人机验证（访问频率触发）\n\n"
                        "已在 Chrome Agent 的 Chrome 窗口打开验证页——"
                        "请到那个窗口完成一次滑块验证，然后重发 /dyu 命令。"
                        "（验证一次即解除，标签页会自动保留）")
                except Exception:
                    pass
                _KEEP_TAB.add(tgt["targetId"])
                return None, "被验证码拦截：请在 Chrome 窗口完成验证后重发"

            href_expr = ("JSON.stringify("
                         "Array.from(document.querySelectorAll('a[href*="
                         "\"/video/\"]')).map(a=>a.href))")
            desc_expr = ("JSON.stringify("
                         "Array.from(document.querySelectorAll("
                         "'a[href*=\"/video/\"]')).map(a=>"
                         "(a.getAttribute('aria-label')||a.textContent||'')"
                         ".trim().slice(0,60)))")
            # 滚动目标：内层 route-scroll-container（window 滚动无效）；
            # 兜底 window，再补一发真滚轮事件覆盖其他容器形态
            scroll_expr = ("(() => {const el = [...document."
                           "querySelectorAll('[class*=\"route-scroll-"
                           "container\"]')].find(e => e.scrollHeight > "
                           "e.clientHeight + 300);"
                           "(el || document.documentElement).scrollTo(0, "
                           "999999); return !!el;})()")
            counts, awemes = [], []
            for _round in range(max_scrolls):
                hrefs = _json.loads(await eval_js(href_expr) or "[]")
                descs = _json.loads(await eval_js(desc_expr) or "[]")
                awemes = parse_video_hrefs(hrefs)
                for i, a in enumerate(awemes):
                    if i < len(descs) and not a["desc"]:
                        a["desc"] = str(descs[i] or "")
                counts.append(len(awemes))
                if len(awemes) >= max_items \
                        or scroll_should_stop(counts, stable_rounds=4):
                    break
                await eval_js(scroll_expr)
                await call("Input.dispatchMouseEvent",
                           {"type": "mouseWheel", "x": 500, "y": 500,
                            "deltaX": 0, "deltaY": 2500}, session_id=sid)
                await asyncio.sleep(pause)
            if not awemes:
                return None, "页面未渲染出作品（作者不存在或被挑战页拦截）"
            return awemes, None
        finally:
            if tgt["targetId"] not in _KEEP_TAB:
                try:
                    await call("Target.closeTarget",
                               {"targetId": tgt["targetId"]})
                except Exception:
                    pass
            else:
                logger.info("🎵 验证码页已保留，等待 owner 手动完成验证")


# 验证码页保留集（owner 完成验证后页面自然放行，无需程序回收）
_KEEP_TAB = set()


async def _ensure_cdp(port):
    """CDP 可达性探测；不可达时拉起 Chrome Agent 再探一次。"""
    import urllib.request as _u

    def _alive():
        try:
            with _u.urlopen(f"http://127.0.0.1:{port}/json/version",
                            timeout=2) as r:
                return bool(r.read())
        except Exception:
            return False

    if await asyncio.to_thread(_alive):
        return True
    try:
        from . import chrome_client
        logger.info("🎵 Chrome Agent 不在线，自动拉起…")
        await chrome_client.spawn_agent()
    except Exception as e:
        logger.warning(f"🎵 Chrome Agent 拉起失败：{e}")
        return False
    for _ in range(20):
        await asyncio.sleep(1.5)
        if await asyncio.to_thread(_alive):
            return True
    return False


def _user_douyin_cookie_jar():
    """用户 Chrome 的 douyin cookie → CDP Storage.setCookies 形态。"""
    from . import browser_cookies
    cookie_str, err = browser_cookies.load_browser_cookie_string(
        str(getattr(config, "DYU_BROWSER_COOKIE", "chrome")))
    if err or not cookie_str:
        return []
    return [{"name": kv.split("=", 1)[0].strip(),
             "value": kv.split("=", 1)[1],
             "domain": ".douyin.com", "path": "/"}
            for kv in cookie_str.split(";") if "=" in kv]


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
    # 手动游标翻页（max_counts=page_counts 让生成器恰好产一页）：页级
    # 403 可重试——生成器一旦抛异常即死，重建 handler 换同一游标再来
    cursor, pages = 0, 0
    while pages < max_pages:
        page = None
        for attempt in range(3):
            try:
                async for p in DouyinHandler(_handler_kwargs()) \
                        .fetch_user_post_videos(
                            sec_user_id, max_cursor=cursor,
                            page_counts=20, max_counts=20):
                    page = p
                    break
                break
            except Exception as e:
                transient = "403" in str(e) and attempt < 3
                if not transient:
                    raise
                wait = (15, 30, 60)[attempt]
                logger.warning(
                    f"🎵 枚举被拒（403 风控），{wait}s 后重试"
                    f"（{attempt + 1}/3，游标不动）：{str(e)[:80]}")
                await asyncio.sleep(wait)
                # 重试前重新保鲜：msToken 是滚动值，Chrome 若在访问
                # douyin.com，cookie 里已是新一代
                await refresh_cookie_from_browser()
        if page is None:
            return
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
        if not raw.get("has_more"):
            return
        cursor = int(raw.get("max_cursor") or 0)


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
            # 先保鲜 cookie（msToken 几小时过期，陈旧值是 403 主因）
            await refresh_cookie_from_browser()
            return await _run_dyu_inner(url, subdir_raw)
        except Exception as e:
            logger.exception(f"🎵 /dyu 失败：{e}")
            hint = ""
            if "403" in str(e):
                hint = ("\n\n💡 403 风控：cookie/msToken 已失效。用 Chrome "
                        "登录 douyin.com 后重发命令（会自动取新 cookie），"
                        "或 /cookie 手动更新。")
            try:
                await notify.notify_user(
                    f"❌ 抖音作者批量任务失败\n\n{type(e).__name__}: "
                    f"{str(e)[:200]}\n链接：{url}{hint}\n"
                    "（已入队的部分不受影响）")
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
    all_awemes = []
    enum_via = "Chrome DOM"
    harvested, chrome_err = await harvest_author_links_via_chrome(sec_uid)
    if harvested is not None:
        all_awemes = harvested
    else:
        # Chrome 收割失败（Agent 起不来/页面异常）→ f2 兜底（能拿发布时间，
        # 但受 msToken 时效与风控影响，2026-10-02 生产实测）
        logger.warning(f"🎵 Chrome 收割失败（{chrome_err}），降级 f2 枚举")
        enum_via = "f2"
        try:
            async for awemes, has_more in enumerate_author_posts(sec_uid):
                all_awemes.extend(awemes)
                logger.info(f"🎵 f2 枚举累计 {len(all_awemes)} 条"
                            f"{'…' if has_more else '（完）'}")
        except Exception as e:
            raise RuntimeError(
                f"Chrome 收割与 f2 枚举都失败：{chrome_err} / "
                f"{str(e)[:80]}")
    for a in all_awemes:
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
    logger.info(f"🎵 入队进度：已发现 {found}（新 {queued} / 已有 {skipped}）")

    target = f"下载/抖音/{subdir}" if subdir else "下载/抖音"
    text = (f"🎵 抖音作者批量任务已开始\n\n"
            f"作者：{label}\n"
            f"作品：{found} 个｜新入队 {queued}｜跳过已下载 {skipped}"
            f"｜枚举：{enum_via}\n"
            f"目录：{target}\n"
            f"模式：串行逐条（解析→下载），完成后逐条通知")
    try:
        await notify.notify_user(text)
    except Exception:
        pass
    return text
