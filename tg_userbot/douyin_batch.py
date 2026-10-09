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


def _gateway_headers(cookie):
    """抖音列表接口网关头（f2 上游 PR #446 的方案，未合入装的 0.0.1.6）。

    2026-10-06 实测：列表接口一律 403，原因是网关要求把 cookie 里的 UIFID
    同时作为 uifid 请求头发送，外加 x-tt-argus: 1（列表端点只验存在不验
    签名，字面量即可）。cookie 没有 UIFID（游客/旧 cookie）时不加。
    """
    for part in (cookie or "").split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if name.strip().casefold() == "uifid" and value.strip():
            return {"uifid": value.strip(), "x-tt-argus": "1"}
    return {}


def _handler_kwargs():
    """f2 DouyinHandler 配置：cookie 调用时读（/cookie 与浏览器保鲜立即生效）。"""
    cookie = getattr(config, "DOUYIN_COOKIE", "") or ""
    headers = _gateway_headers(cookie)
    headers.update(dict(getattr(config, "DOUYIN_HEADERS", {})))  # 显式配置优先
    return {
        "cookie": cookie,
        "headers": headers,
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

# 最近/当前批量任务的生命周期（进程内记忆）。裸 /dyu 的进度视图靠它：
# 队列只在「任务执行中」有货——枚举阶段、每条销账出榜后的间隙、全部
# 完成后队列都是空的，只看队列会把进度视图错落成用法说明（2026-10-06）。
_BATCH = {"label": None, "started_at": None, "ended_at": None, "note": None,
          "found": None, "queued": None, "skipped": None,
          "enum_via": None, "rescued": False}


def note_batch_start(label):
    _BATCH.update(label=label, started_at=time.time(), ended_at=None,
                  note=None, found=None, queued=None, skipped=None,
                  enum_via=None, rescued=False)


def note_batch_end(note=None):
    _BATCH.update(ended_at=time.time(), note=note)


def has_batch_history():
    """裸 /dyu 是否应显示进度视图：本进程跑过批量，或队列里还有批量活。"""
    if _BATCH.get("label"):
        return True
    rows = list(state.QUEUE.get("tasks") or []) + \
        list(state.QUEUE.get("retry") or [])
    return any(r.get("serial") and r.get("source") == "抖音作者合集"
               for r in rows)


def stamp_next_bot_video(subdir, aweme_id, ttl=180.0):
    """登记：解析 bot 即将回流的下一个视频落 <抖音>/<subdir>/。"""
    if not subdir:
        return
    _BOT_STAMP.update(subdir=str(subdir), aweme_id=str(aweme_id or ""),
                      expires=time.time() + ttl)
    logger.info(f"🎵 已盖在途目录戳：抖音/{subdir}（等待解析 bot 回流）")


def pop_bot_stamp():
    """取走当前戳（一次性）；过期/未盖返回 None。

    返回 (subdir, aweme_id)——aweme_id 供销账：bot 回流视频 = 该作品已
    解决，对应的 url 重试任务应当出榜（进度可见）。
    """
    if _BOT_STAMP["subdir"] and time.time() <= _BOT_STAMP["expires"]:
        subdir = _BOT_STAMP["subdir"]
        aweme_id = _BOT_STAMP["aweme_id"]
        _BOT_STAMP.update(subdir=None, aweme_id=None, expires=0.0)
        return subdir, aweme_id
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
    # 批量生命周期：进行中 / 上次结果——队列空（枚举中、逐条销账间隙、
    # 全部完成）时这是唯一能说明「批量到底怎么样了」的信息
    b = _BATCH
    if b.get("label"):
        if b.get("ended_at") is None:
            lines.append(f"📌 批量进行中：{b['label']}"
                         f"（{time.strftime('%H:%M:%S', time.localtime(b['started_at']))}"
                         " 开始枚举；枚举阶段队列还看不到任务）")
        else:
            note = f"，{b['note']}" if b.get("note") else ""
            lines.append(f"📌 上次批量：{b['label']}"
                         f"（{time.strftime('%H:%M:%S', time.localtime(b['ended_at']))}"
                         f" 枚举完成{note}）")
        if b.get("found") is not None:
            lines.append(f"枚举 {b['found']} ｜ 入队 {b['queued']}"
                         f" ｜ 跳过已下载 {b['skipped']} ｜ 枚举路：{b['enum_via']}"
                         + ("（页面直出兜底，可能不全）" if b.get("rescued") else ""))
        lines.append("")
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
    """/dyu <主页链接> [since YYYY-MM-DD] [all] [子目录]。

    → (url, subdir, since, include_forward)。since 沿 /paw_plan 的关键词
    约定；all = 连转发一起收（默认仅原创）。不合法返回全 None。
    """
    import re
    from datetime import datetime as _dt
    m = re.match(r"^/dyu(?:\s+|$)(.*)$", str(text or "").strip(),
                 re.IGNORECASE)
    if not m:
        return None, None, None, None
    parts = m.group(1).split()
    if not parts:
        return None, None, None, None
    url = parts[0]
    if "douyin.com" not in url:
        return None, None, None, None
    rest = parts[1:]
    since = None
    if len(rest) >= 2 and rest[0].lower() == "since":
        try:
            since = _dt.strptime(rest[1], "%Y-%m-%d")
        except ValueError:
            return None, None, None, None   # since 后必须跟合法日期
        rest = rest[2:]
    include_fwd = False
    if rest and rest[0].lower() == "all":
        include_fwd = True
        rest = rest[1:]
    subdir = " ".join(rest)
    return url, subdir, since, include_fwd


def filter_awemes_original(awemes, sec_uid, nickname=None):
    """只保留作者本人的原创作品（纯函数）。

    判据（2026-10-03 单作品接口实测）：转发作品的 author 是**原作者**
    （sec_uid ≠ 目标作者），原创的 author.sec_uid == 目标作者。author
    信息缺失的作品剔除（无法核实，宁缺勿错——与 since 的无时间剔除
    同一原则）。返回 (保留列表, 转发数, 无主数)。
    """
    kept, forwards, unknown = [], 0, 0
    for a in awemes:
        a_sec = a.get("author_sec_uid")
        if a_sec:
            if a_sec == sec_uid:
                kept.append(a)
            else:
                forwards += 1
        elif nickname and a.get("author_nickname") == nickname:
            kept.append(a)      # 接口偶尔缺 sec_uid，昵称兜底
        else:
            unknown += 1
    return kept, forwards, unknown


def filter_awemes_since(awemes, since_dt):
    """按发布时间过滤（纯函数）。返回 (保留列表, 本页是否已全部早于 since)。

    create_time 缺失的作品**剔除**（无法核实发布日期，宁缺勿错——数据
    准确性优先）；「本页全早于」供调用方提前停止翻页（页序新→旧）。
    """
    import time as _time
    from datetime import datetime as _dt
    if since_dt is None:
        return list(awemes), False
    floor = _dt.timestamp(since_dt)
    kept, any_newer = [], False
    for a in awemes:
        ct = a.get("create_time")
        if not ct:
            continue
        if int(ct) >= floor:
            kept.append(a)
            any_newer = True
    return kept, not any_newer


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


# 页面内捕获钩子：包装 fetch 与 XMLHttpRequest，把 /aweme/post/ 的响应
# JSON 原地存进 window.__captured。**绝不能用 CDP Network 域**——Argus
# 会探测它并令页面自身的请求签名失效（2026-10-04 生产实测：开
# Network.enable → 全部 XHR 403 Sign Invalid；不开 → 翻页正常）。
_PAGE_HOOK_JS = """
(() => {
  if (window.__captured) return 'already';
  window.__captured = [];
  const keep = (u, getBody) => {
    if (!String(u).includes('/aweme/post/')) return;
    try {
      const t = getBody();
      if (t) { try { window.__captured.push(JSON.parse(t)); } catch (e) {} }
    } catch (e) {}
  };
  const of_ = window.fetch;
  window.fetch = function(...args) {
    const p = of_.apply(this, args);
    const u = String((args[0] && args[0].url) || args[0] || '');
    if (u.includes('/aweme/post/')) {
      p.then(r => r.clone().text().then(t => keep(u, () => t)))
       .catch(() => {});
    }
    return p;
  };
  const oo = XMLHttpRequest.prototype.open;
  const os_ = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u, ...rest) {
    this.__u = u; return oo.call(this, m, u, ...rest);
  };
  XMLHttpRequest.prototype.send = function(...rest) {
    this.addEventListener('load', () => keep(this.__u,
      () => this.responseText));
    return os_.apply(this, ...rest);
  };
  return 'hooked';
})()
"""


def merge_captured_batches(batches):
    """页面捕获的响应批次 → 去重保序的 aweme 列表（纯函数，可单测）。

    每条提取枚举/过滤所需四字段：aweme_id / desc / create_time /
    author_sec_uid / author_nickname。字段缺失的作品剔除（宁缺勿错，
    与既有 since/original 过滤同一原则）。
    """
    seen, out = set(), []
    for d in batches or []:
        for a in (d or {}).get("aweme_list") or []:
            aid = a.get("aweme_id")
            if not aid or aid in seen:
                continue
            seen.add(aid)
            author = a.get("author") or {}
            if not (a.get("create_time") and author.get("sec_uid")):
                continue     # 过滤所需字段不全 → 剔除
            out.append({"aweme_id": str(aid),
                        "desc": a.get("desc") or "",
                        "create_time": a.get("create_time"),
                        "author_sec_uid": author.get("sec_uid"),
                        "author_nickname": author.get("nickname")})
    return out


async def harvest_author_awemes_via_chrome(sec_user_id, max_scrolls=60):
    """Agent Chrome 页面内钩子收割（字段全量版，2026-10-04）。

    与 DOM href 收割的差异：从页面自己的 /aweme/post/ XHR 响应里拿完整
    aweme 数据（含 create_time 与 author.sec_uid）→ 原创/since 过滤可用；
    DOM 收割只有链接无字段。两者都**不开 CDP Network 域**（Argus 探测它
    就拒签）。需要 Agent Chrome 在线且已登录抖音（登录态与浏览器环境
    同源才过 Argus）。返回 (awemes, err)。
    """
    import json as _json
    import urllib.request

    import websockets
    from . import config as _cfg

    port = int(getattr(_cfg, "CHROME_CDP_PORT", 9222))
    if not await _ensure_cdp(port):
        return None, f"Chrome Agent CDP({port}) 不可达且自动拉起失败"
    ws_url = _json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{port}/json/version",
        timeout=3).read())["webSocketDebuggerUrl"]

    pause = float(getattr(_cfg, "DYU_CHROME_SCROLL_PAUSE", 2.2))
    init_wait = float(getattr(_cfg, "DYU_CHROME_INIT_WAIT", 7.0))
    async with websockets.connect(ws_url, open_timeout=10,
                                   max_size=32 * 1024 * 1024) as ws:
        _id = [0]

        async def call(method, params=None, session_id=None):
            _id[0] += 1
            msg = {"id": _id[0], "method": method, "params": params or {}}
            if session_id:
                msg["sessionId"] = session_id
            await ws.send(_json.dumps(msg))
            import time as _t
            deadline = _t.monotonic() + 60
            while _t.monotonic() < deadline:
                try:
                    recv = _json.loads(await asyncio.wait_for(
                        ws.recv(), timeout=deadline - _t.monotonic()))
                except asyncio.TimeoutError:
                    break
                if recv.get("id") == _id[0]:
                    if "error" in recv:
                        raise RuntimeError(
                            str(recv["error"].get("message", "?"))[:80])
                    return recv.get("result", {})
            raise RuntimeError(f"CDP 应答超时：{method}")

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
            # 导航前注入钩子（新文档自动执行，首屏 XHR 也能捕获）；
            # 加载后再补一次兜底（前两批 XHR 由滚动触发，来得及）
            await call("Page.addScriptToEvaluateOnNewDocument",
                       {"source": _PAGE_HOOK_JS}, session_id=sid)
            await call("Page.navigate",
                       {"url": f"https://www.douyin.com/user/{sec_user_id}"},
                       session_id=sid)
            await asyncio.sleep(init_wait)
            title = str(await eval_js("document.title") or "")
            if "验证" in title:
                try:
                    await notify.notify_user(
                        "🎵 抖音要求人机验证\n\n已在 Agent Chrome 窗口打开"
                        "验证页——请到那个窗口完成一次滑块验证后重发命令。")
                except Exception:
                    pass
                return None, "被验证码拦截：请在 Chrome 窗口完成验证后重发"
            # 「服务异常」占位页（owner 2026-10-04 实测：手动 F5 一次即恢复）。
            # 刷新必须用 Input.dispatchKeyEvent 模拟**真实 F5 按键**——
            # Page.reload 会被 Argus 识别（实测重载后 addScript 注入的钩子
            # 不执行、页面仍吐降级页），真实按键事件则与手动操作同效
            for _reload in range(2):
                page_state = await eval_js(
                    "JSON.stringify({err: document.body.innerText.includes("
                    "'服务异常'), dom: document.querySelectorAll("
                    "'a[href*=\"/video/\"]').length, cap: "
                    "(window.__captured||[]).length, hook: "
                    "typeof window.__captured})")
                import json as _j2
                st = _j2.loads(page_state or "{}")
                if (not st.get("err") and (st.get("cap")
                        or (st.get("dom") and st.get("hook") != "undefined"))):
                    break
                logger.info(f"🎵 页面未就绪（{st}），F5 重载（{_reload + 1}/2）")
                await call("Input.dispatchKeyEvent", {
                    "type": "keyDown", "key": "F5", "code": "F5",
                    "windowsVirtualKeyCode": 116,
                    "nativeVirtualKeyCode": 116}, session_id=sid)
                await call("Input.dispatchKeyEvent", {
                    "type": "keyUp", "key": "F5", "code": "F5",
                    "windowsVirtualKeyCode": 116,
                    "nativeVirtualKeyCode": 116}, session_id=sid)
                await asyncio.sleep(max(init_wait, 9.0))
                # 重载后钩子可能未随新文档执行（Argus 干扰 addScript）——
                # 无条件补注入，宁可重复注入（钩子自身幂等）
                await eval_js(_PAGE_HOOK_JS)
                title = str(await eval_js("document.title") or "")
                if "验证" in title:
                    return None, "刷新后遇到验证码：请手动完成后重发"
            await eval_js(_PAGE_HOOK_JS)

            scroll_expr = ("(() => {const el = [...document."
                           "querySelectorAll('[class*=\"route-scroll-"
                           "container\"]')].find(e => e.scrollHeight > "
                           "e.clientHeight + 300);"
                           "(el || document.documentElement).scrollTo(0, "
                           "999999); return !!el;})()")
            counts, batches = [], []
            for _round in range(max_scrolls):
                raw = await eval_js(
                    "JSON.stringify(window.__captured || [])")
                batches = _json.loads(raw or "[]")
                counts.append(len(merge_captured_batches(batches)))
                if len(counts) >= 5 and counts[-1] == counts[-5]:
                    break          # 连续多轮无新增（DOM 与捕获都稳定）
                await eval_js(scroll_expr)
                await asyncio.sleep(pause)
            awemes = merge_captured_batches(batches)
            if not awemes:
                # 兜底信号：DOM 也没渲染 → 会话被临时风控/挑战页
                dom_n = await eval_js(
                    "document.querySelectorAll("
                    "'a[href*=\"/video/\"]').length")
                return None, (f"页面未渲染（DOM={dom_n}，捕获 0 批）——"
                              "会话可能被临时风控，稍等几分钟再试")
            return awemes, None
        finally:
            try:
                await call("Target.closeTarget",
                           {"targetId": tgt["targetId"]})
            except Exception:
                pass


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

            # 只收作者作品网格（data-e2e=user-post-list）里的链接——全页
            # 收割会把页脚「相关推荐/猜你喜欢」的**别人**视频也混进来
            # （2026-10-03 生产实测：作者 5 个作品全页却收出 13 条）。
            # user-post-list 缺席时回退 scroll-list（喜欢/收藏页的容器）。
            scope_expr = (
                '(() => {'
                'for (const n of ["user-post-list", "scroll-list"]) {'
                'const el = document.querySelector(`[data-e2e="${n}"]`);'
                'if (el) return JSON.stringify('
                "Array.from(el.querySelectorAll('a[href*=\"/video/\"]'))"
                '.map(a=>a.href));}'
                'return "[]";})()'
            )
            raw = await eval_js(scope_expr)
            hrefs = _json.loads(raw or "[]")
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
                if _round > 0:
                    raw = await eval_js(scope_expr)
                    hrefs = _json.loads(raw or "[]")
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
            # 三轮全 403 耗尽：必须 raise 让上层发失败通知——静默 return
            # 会把风控故障伪装成「作者没有作品」（2026-10-04 生产误报：
            # 「没有符合条件的作品」实为 403 全拒）
            raise RuntimeError(
                "f2 枚举连续被 403 拒绝（msToken 失效/风控）——Chrome 打开"
                "一次 douyin.com 刷新 msToken 后重发命令")
        pages += 1
        raw = page._to_raw() if hasattr(page, "_to_raw") else {}
        awemes = []
        for a in raw.get("aweme_list") or []:
            aid = a.get("aweme_id")
            if aid:
                author = a.get("author") or {}
                awemes.append({"aweme_id": aid,
                               "desc": a.get("desc") or "",
                               "create_time": a.get("create_time"),
                               "author_sec_uid": author.get("sec_uid"),
                               "author_nickname": author.get("nickname")})
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


async def run_dyu(url, subdir_raw, since=None, original_only=True):
    """后台编排：枚举 → 逐条判重入队 → 汇总通知。返回给命令层的文案由
    command_reply 同步部分给出；本函数只发异步通知。"""
    if _ENUM_LOCK.locked():
        return "⏳ 已有一个 /dyu 枚举在进行中，稍后再发。"
    async with _ENUM_LOCK:
        try:
            # 先保鲜 cookie（msToken 几小时过期，陈旧值是 403 主因）
            await refresh_cookie_from_browser()
            return await _run_dyu_inner(url, subdir_raw, since,
                                        original_only)
        except Exception as e:
            logger.exception(f"🎵 /dyu 失败：{e}")
            note_batch_end(f"失败：{type(e).__name__} {str(e)[:60]}")
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


async def _run_dyu_inner(url, subdir_raw, since=None, original_only=True):
    from . import queue

    sec_uid = await get_sec_user_id(url)
    nickname = await fetch_author_nickname(sec_uid)
    subdir = sanitize_filename(subdir_raw) if subdir_raw else \
        (sanitize_filename(nickname) if nickname else "")
    label = nickname or sec_uid[:16]
    note_batch_start(label)
    since_note = f"，since {since:%Y-%m-%d}" if since else ""
    orig_note = "，只要原创" if original_only else "，含转发"
    logger.info(f"🎵 /dyu 开始枚举：{label}{since_note}{orig_note}"
                f"（sec_uid {sec_uid[:18]}…）")

    all_awemes = []
    dom_rescued = False
    if since is not None or original_only:
        # 需要字段的模式（since 要时间戳、原创要作者归属）。
        # 第 1 顺位：Agent Chrome 页面内钩子收割（字段全量，免 403——
        #   页面自己签名；2026-10-04 Argus 收紧后 f2 直连已被拒）。
        # 第 2 顺位：f2 直连（Argus 收紧前可用，保留作回落）。
        # 第 3 顺位：DOM href 收割无字段——本模式不可用，如实报错。
        enum_via = "Chrome 钩子"
        harvested, chrome_err = None, None
        for _attempt in (1, 2):
            try:
                harvested, chrome_err = await \
                    harvest_author_awemes_via_chrome(sec_uid)
            except Exception as _he:
                # 2026-10-05 实测：Target crashed / CDP 应答超时是 tab 级
                # 瞬态故障，但异常会整条 /dyu 直接死掉（连 f2 回落都不
                # 走）——收编为软失败，换新 tab 重试一次
                harvested, chrome_err = None, str(_he)[:120]
            if harvested is not None or "验证码" in (chrome_err or ""):
                break
            if _attempt == 1:
                logger.warning(
                    f"🎵 钩子收割第 1 次失败（{chrome_err}），"
                    "20s 后换新 tab 重试一次")
                await asyncio.sleep(20)
        if harvested is None:
            logger.warning(
                f"🎵 钩子收割失败（{chrome_err}），降级 f2 枚举")
            enum_via = "f2"
            fwd_total = unk_total = 0
            pages = 0
            f2_err = None
            try:
                async for awemes, has_more in enumerate_author_posts(sec_uid):
                    pages += 1
                    kept_by_since, page_all_older = filter_awemes_since(
                        awemes, since)
                    kept, fwd, unk = filter_awemes_original(
                        kept_by_since, sec_uid, nickname)
                    fwd_total += fwd
                    unk_total += unk
                    all_awemes.extend(kept)
                    logger.info(
                        f"🎵 f2 枚举累计 {len(all_awemes)} 条原创"
                        f"（转发 {fwd_total}｜无主 {unk_total}）")
                    if page_all_older and pages >= 2:
                        logger.info("🎵 本页已全部早于 since，停止翻页")
                        break
                    if not has_more:
                        break
            except Exception as _fe:
                # 403 耗尽等不再整条炸掉：记录后仍可走 DOM 兜底
                f2_err = str(_fe)[:120]
                logger.warning(f"🎵 f2 枚举失败：{f2_err}")
            # 第三兜底（2026-10-05 实测）：列表接口被风控「服务异常」时，
            # 作者页 SSR 直出的作品网格仍可收割。仅限无 since 模式——DOM
            # 链接无发布时间，since 过滤会全剔；作品网格里的链接全是作者
            # 本人作品（user-post-list 容器），直接标注 sec_uid 过原创关；
            # 无 create_time → 文件名日期退化为入队日，判重保证重发可补齐
            dom_err = None
            if since is None and (f2_err or (not all_awemes and pages == 0)):
                dom_items = None
                for _attempt in (1, 2):
                    try:
                        dom_items, dom_err = await \
                            harvest_author_links_via_chrome(sec_uid)
                    except Exception as _de:
                        dom_items, dom_err = None, str(_de)[:120]
                    if dom_items or "验证码" in (dom_err or ""):
                        break
                    if _attempt == 1:
                        logger.warning(
                            f"🎵 DOM 兜底第 1 次失败（{dom_err}），"
                            "20s 后换新 tab 重试一次")
                        await asyncio.sleep(20)
                if dom_items:
                    for _a in dom_items:
                        _a["author_sec_uid"] = sec_uid
                    all_awemes.extend(dom_items)
                    enum_via = "Chrome DOM 兜底"
                    dom_rescued = True
                    logger.warning(
                        f"🎵 列表接口不可用，DOM 兜底收割 {len(dom_items)} 条"
                        "（页面直出，可能不全；判重保证稍后重发可补齐）")
            if not all_awemes:
                parts = [f"Chrome 钩子：{chrome_err or '—'}",
                         f"f2 直连：{f2_err or '空结果'}"]
                if since is None:
                    parts.append(f"DOM 兜底：{dom_err or '空结果'}")
                text = ("❌ 枚举全部失败\n\n" + "\n".join(parts) +
                        "\n\n（列表接口被风控通常稍后自行恢复；也可到 "
                        "Agent Chrome 窗口手动打开一次该作者页再重发）")
                note_batch_end("枚举全部失败")
                try:
                    await notify.notify_user(text)
                except Exception:
                    pass
                return text
        else:
            kept_by_since, _ = filter_awemes_since(harvested, since)
            all_awemes, fwd_total, unk_total = filter_awemes_original(
                kept_by_since, sec_uid, nickname)
            logger.info(f"🎵 钩子收割 {len(harvested)} 条"
                        f"（原创 {len(all_awemes)}｜转发 {fwd_total}"
                        f"｜无主 {unk_total}）")
        if not all_awemes:
            text = (f"🎵 该作者没有符合条件的作品"
                    f"（{'since ' + format(since, '%Y-%m-%d') + ' 之后，' if since else ''}"
                    f"仅原创；转发 {fwd_total}｜无主 {unk_total}｜枚举：{enum_via}）")
            note_batch_end("没有符合条件的作品")
            try:
                await notify.notify_user(text)
            except Exception:
                pass
            return text
    else:
        # 与钩子收割同理：Target crashed / CDP 超时不许整条 /dyu 硬死，
        # 收编为软失败并换新 tab 重试一次，仍败才落 f2 兜底
        harvested, chrome_err = None, None
        for _attempt in (1, 2):
            try:
                harvested, chrome_err = await \
                    harvest_author_links_via_chrome(sec_uid)
            except Exception as _he:
                harvested, chrome_err = None, str(_he)[:120]
            if harvested is not None:
                break
            if _attempt == 1:
                logger.warning(
                    f"🎵 DOM 收割第 1 次失败（{chrome_err}），"
                    "20s 后换新 tab 重试一次")
                await asyncio.sleep(20)
        if harvested is not None:
            all_awemes = harvested
            enum_via = "Chrome DOM"
        else:
            # Chrome 收割失败（Agent 起不来/页面异常）→ f2 兜底（能拿发布
            # 时间，但受 msToken 时效与风控影响，2026-10-02 生产实测）
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

    found = queued = skipped = 0
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
    _BATCH.update(found=found, queued=queued, skipped=skipped,
                  enum_via=enum_via, rescued=dom_rescued)

    target = f"下载/抖音/{subdir}" if subdir else "下载/抖音"
    since_line = (f"范围：{since:%Y-%m-%d} 之后\n" if since else "")
    orig_line = ("内容：仅原创作品\n" if original_only else "")
    rescue_line = ("⚠️ 列表接口被风控，本次走页面直出兜底：可能不全，"
                   "稍后重发可补齐（判重）\n" if dom_rescued else "")
    note_batch_end(f"入队 {queued}/{found}，逐条下载中")
    text = (f"🎵 抖音作者批量任务已开始\n\n"
            f"作者：{label}\n"
            f"{since_line}{orig_line}"
            f"{rescue_line}"
            f"作品：{found} 个｜新入队 {queued}｜跳过已下载 {skipped}"
            f"｜枚举：{enum_via}\n"
            f"目录：{target}\n"
            f"模式：串行逐条（解析→下载），完成后逐条通知")
    try:
        await notify.notify_user(text)
    except Exception:
        pass
    return text
