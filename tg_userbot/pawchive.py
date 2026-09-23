"""Pawchive（pawchive.pw 归档站）—— 扫描 / 收藏对比 / 命令与菜单入口。

数据流（与独立 CLI 脚本 ~/Documents/pawchive/pawchive.py 同源逻辑，落点不同）：

    /paw plan <作者>
      ↓ 同源 API：创作者解析 → 帖子分页（o=50）→ 收藏对比（可选 Cookie）→ 外链提取
    帖子级落 SQLite（pawchive_posts/pawchive_files，schema v8，重复扫描幂等）
      ↓ pawchive_worker 逐帖领取
    附件直链由内置并发下载器下载（2026-09-15 起，早期经 Chrome Agent）
    → COMPLETED / MANUAL（有外链）/ FAILED

站点结构（2026-09-14 实测）：
    创作者   GET /api/v1/creators                  （q 参数无效→全量+本地过滤+缓存）
    帖子列表 GET /api/v1/{svc}/user/{id}/posts?o=  公开，每页 50
    收藏     GET /api/v1/account/favorites?type=post   需要 Cookie（Flask session）
    直链     https://file.pawchive.pw/data/{path}?f=   公开，content-disposition
             attachment（Chrome 打开即触发下载而非内嵌播放，worker 依赖这一点）

阻塞 HTTP 一律经 ``asyncio.to_thread`` 下放线程：urllib 没有异步形态，而主循环
上还跑着下载/通知/菜单，绝不能被 20MB 的创作者列表卡住。
"""
import asyncio
import csv
import html as html_mod
import json
import os
import re
import shutil
import time
import urllib.parse
import urllib.request

from telethon import Button

from . import config
from . import runtime_db
from . import state
from .log import logger

TEXT_PREFIX = "🐾 Pawchive"

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) tg-userbot-pawchive/1.0"
_PAGE = 50

# 扫描是后台一次性工作：强引用集防 GC（asyncio 只对 Task 持弱引用）
_SPAWNED_SCANS = set()


# ============================================================
# HTTP（阻塞层，调用方一律 asyncio.to_thread）
# ============================================================
# 强制直连：系统代理（socks5）会让 urllib 秒抛 ValueError，见 worker 同款注释
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_get_json(url, cookie=None, timeout=30, retries=4):
    """GET 并解析 JSON；429/5xx/网络抖动按指数退避重试。

    收藏接口单次可能 >30s（返回全量收藏帖子），调用方给足 timeout。
    """
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", _UA)
            if cookie:
                req.add_header("Cookie", cookie)
            with _DIRECT_OPENER.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                last = e
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code}: {url}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"重试 {retries} 次仍失败：{url}（{last}）")


# ============================================================
# 创作者：全量缓存 + 本地过滤（站点 q 参数无效）
# ============================================================
def _cache_path():
    return os.path.join(config.PAWCHIVE_DATA_DIR, "creators.json")


def load_creators(refresh=False):
    """全量创作者列表（约 20MB / 九万条），TTL 内读缓存。阻塞，放线程跑。"""
    path = _cache_path()
    if not refresh:
        try:
            age = time.time() - os.path.getmtime(path)
            if age < config.PAWCHIVE_CREATORS_CACHE_TTL:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except (FileNotFoundError, OSError, ValueError):
            pass
    data = _http_get_json(f"{config.PAWCHIVE_API_BASE}/api/v1/creators",
                          timeout=120)
    os.makedirs(config.PAWCHIVE_DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return data


def search_creators(term, limit=5):
    """按名字找创作者：先精确（大小写不敏感），再子串；热门优先。"""
    term = str(term or "").strip().lower()
    if not term:
        return []
    creators = load_creators()
    exact = [c for c in creators if c.get("name", "").lower() == term]
    if exact:
        return exact[:limit]
    fuzzy = [c for c in creators if term in c.get("name", "").lower()]
    fuzzy.sort(key=lambda c: -c.get("favorited", 0))
    return fuzzy[:limit]


def resolve_creator(name):
    """名字 → 创作者 dict（精确优先，否则取最热子串匹配）；找不到返回 None。"""
    term = str(name or "").strip().lower()
    if not term:
        return None
    creators = load_creators()
    for c in creators:
        if c.get("name", "").lower() == term:
            return c
    hits = [c for c in creators if term in c.get("name", "").lower()]
    return max(hits, key=lambda c: c.get("favorited", 0)) if hits else None


async def resolve_creator_async(name):
    """resolve_creator 的异步包装（阻塞 HTTP 下放线程；bot 菜单窗口用）。"""
    return await asyncio.to_thread(resolve_creator, name)


# ============================================================
# 帖子 / 收藏 / 外链
# ============================================================
def fetch_creator_posts(service, creator_id, cookie=None, progress=None,
                        known_ids=None):
    """分页拉取创作者帖子；**整页均已入库时提前停止**（增量扫描）。

    帖子按发布时间倒序返回；**连续两整页均已入库**才提前停止——单页假象
    （站点排序抖动/补传插页）不会导致漏数据，任何未见过的帖子都会让扫描
    继续。正确性锚点仍是入库时的 INSERT OR IGNORE 唯一索引；提前停止只是
    省请求，绝不影响完整性。known_ids=None 时全量分页（首扫）。
    """
    posts, offset = [], 0
    all_known_pages = 0
    while True:
        page = _http_get_json(
            f"{config.PAWCHIVE_API_BASE}/api/v1/{service}/user/{creator_id}"
            f"/posts?o={offset}", cookie=cookie)
        posts.extend(page)
        if progress:
            progress(f"已拉取 {len(posts)} 条")
        if known_ids is not None and page and all(
                str(p["id"]) in known_ids for p in page):
            all_known_pages += 1
            if all_known_pages >= 2:
                logger.info(
                    f"🐾 连续 {all_known_pages} 整页均已入库（offset {offset}），"
                    f"增量扫描提前停止，共拉取 {len(posts)} 条")
                return posts
        else:
            all_known_pages = 0
        if len(page) < _PAGE:
            return posts
        offset += _PAGE


def fetch_favorited_ids(cookie):
    """当前账号收藏的全部帖子 id（Cookie 会话）。阻塞且慢（>30s 常态）。"""
    favs = _http_get_json(
        f"{config.PAWCHIVE_API_BASE}/api/v1/account/favorites?type=post",
        cookie=cookie, timeout=180)
    return {str(f["id"]) for f in favs}


def validate_cookie_blocking(cookie=None, timeout=20, retries=2):
    """校验 Pawchive Cookie 会话是否仍有效（阻塞 HTTP，调用方放线程跑）。

    打同一个 favorites 接口：返回 JSON（哪怕空收藏）= 会话有效；HTTP
    401/403 = 明确失效；登录墙返回 HTML（JSON 解码炸）也按失效。网络抖动
    **不算**失效——detail 里区分「已失效」与「校验失败（网络）」，调用方
    只对前者提醒用户换 Cookie。返回 (ok, detail)。
    """
    cookie = config.PAWCHIVE_COOKIE if cookie is None else cookie
    if not (cookie or "").strip():
        return False, "未配置"
    url = f"{config.PAWCHIVE_API_BASE}/api/v1/account/favorites?type=post"
    try:
        favs = _http_get_json(url, cookie=cookie, timeout=timeout,
                              retries=retries)
    except RuntimeError as e:
        msg = str(e)
        if "HTTP 401" in msg or "HTTP 403" in msg:
            return False, f"已失效（{msg.split(':', 1)[0]}）"
        return False, f"校验失败（{msg[:60]}）"
    except ValueError:
        # json.JSONDecodeError：接口吐登录页 HTML 而不是 JSON = 会话过期
        return False, "已失效（站点返回登录页）"
    except Exception as e:
        return False, f"校验失败（{type(e).__name__}）"
    if isinstance(favs, list):
        return True, f"有效（收藏 {len(favs)} 条）"
    return True, "有效"


# 校验结果缓存：🧪 按钮连点 / 面板刷新共用，TTL 内不重复打站点
COOKIE_CHECK_TTL_SECONDS = 600


async def cookie_check_cached(force=False):
    """带缓存的 Cookie 校验；结果写 state.PAW_COOKIE_CHECK（status 只读展示）。

    TTL 内重复调用直接回上次结论（面板/连点不重复打站点）；🧪 按钮与每日
    体检传 force=True 现打一次。网络类失败也如实缓存（detail 里带
    「校验失败」字样，与「已失效」区分），下次 force 自然刷新。
    """
    st = state.PAW_COOKIE_CHECK
    now = time.monotonic()
    if (not force and st.get("detail") and st.get("ts")
            and now - st["ts"] < COOKIE_CHECK_TTL_SECONDS):
        return st.get("ok"), st.get("detail")
    ok, detail = await asyncio.to_thread(validate_cookie_blocking)
    st.update({"ts": now, "ok": ok, "detail": detail})
    return ok, detail


def _normalize_single(data):
    """站点接口差异：详情/资料可能包成单元素数组，归一化成 dict。"""
    if isinstance(data, list) and data:
        return data[0]
    return data


def fetch_creator_profile(service, creator_id):
    """创作者资料（名字等，公开接口）——单帖下载用它补齐子目录命名。

    注意走 /profile 子路径：`/user/{id}` 本身返回的是该作者最新一条帖子。
    """
    return _normalize_single(_http_get_json(
        f"{config.PAWCHIVE_API_BASE}/api/v1/{service}/user/{creator_id}"
        f"/profile", timeout=30))


def fetch_post_detail(service, creator_id, post_id, cookie=None):
    """单帖详情（公开接口）：attachments/file/content 与列表接口同构。

    站点实现差异：详情可能包成单元素数组返回，这里归一化成 dict。
    """
    data = _http_get_json(
        f"{config.PAWCHIVE_API_BASE}/api/v1/{service}/user/{creator_id}"
        f"/post/{post_id}", cookie=cookie, timeout=30)
    return _normalize_single(data)


_POST_URL_RE = re.compile(
    r"^https?://pawchive\.pw/(\w+)/user/(\d+)/post/(\d+)", re.I)


def parse_post_ref(text):
    """帖子引用 → (service, creator_id, post_id) 或 None。

    支持两种输入：站点帖子 URL（含 service/创作者）；纯数字帖子 ID
    （此时 service/创作者未知，返回 (None, None, id) 由调用方查库）。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    m = _POST_URL_RE.match(raw)
    if m:
        return (m.group(1).lower(), m.group(2), m.group(3))
    if raw.isdigit():
        return (None, None, raw)
    return None


# URL 尾部粘连的全角标点/空白（ASCII 半角 ) ] 需配平判定，见 _trim_url_tail）
_URL_TAIL_PUNCT = "。，、；：！？…】」』〉》＞\u3000 \t"


def _trim_url_tail(url):
    """剪掉正文里 URL 尾部粘连的标点（中文帖子 URL 后几乎总跟句读）。

    全角标点直接剪；ASCII 的 ) ] 只在不成对时剪（保护 wiki 式 URL 内的
    合法括号）。"""
    url = url.rstrip(_URL_TAIL_PUNCT)
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1]
    while url.endswith("]") and url.count("]") > url.count("["):
        url = url[:-1]
    return url


def find_manual_ext_link(url, limit=1000):
    """查重数据源③：该 URL 是否在某个 MANUAL（待人工）帖的外链里。

    大小写不敏感精确匹配；返回命中帖子 dict，没有则 None。"""
    low = str(url or "").strip().lower()
    if not low:
        return None
    for post in runtime_db.list_pawchive_posts(
            status=runtime_db.PAW_POST_MANUAL, limit=limit):
        for l in (post.get("ext_links") or []):
            if str(l.get("url") or "").strip().lower() == low:
                return post
    return None


def manual_posts_for_view(limit=100):
    """待人工帖视图数据（作者/日期/标题/原帖/外链），按行 id 倒序。"""
    try:
        return runtime_db.list_pawchive_posts(
            status=runtime_db.PAW_POST_MANUAL, limit=limit)
    except runtime_db.DbUnavailable:
        return []


def find_completed_ext_link(url, limit=1000):
    """查重第二数据源：该 URL 是否出现在某个 COMPLETED 帖子的外链里。

    大小写不敏感精确匹配（用户重发同链接时大小写可能有出入）；返回
    命中的帖子 dict（供通知里给 作者/#行id 上下文），没有则 None。"""
    low = str(url or "").strip().lower()
    if not low:
        return None
    for post in runtime_db.list_pawchive_posts(
            status=runtime_db.PAW_POST_COMPLETED, limit=limit):
        for l in (post.get("ext_links") or []):
            if str(l.get("url") or "").strip().lower() == low:
                return post
    return None


def extract_links(post):
    """从帖子 content HTML 与 embed 字段提取站外链接（MEGA/网盘等）。

    站内链接（pawchive.pw）剔除；同帖内按 URL 去重。MEGA 链接的解密密钥
    绝大多数在 URL #fragment 里，原样保留即可打开。

    三个来源、kind 区分：link = <a href>；text = 剥标签后正文裸写的 URL
    （尾部标点自动修剪）；embed = 帖子 embed 字段。
    """
    links, seen = [], set()
    for m in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                         post.get("content") or "", re.S | re.I):
        url = html_mod.unescape(m.group(1)).strip()
        if not re.match(r"^https?://", url, re.I):
            continue
        host = urllib.parse.urlparse(url).netloc.lower()
        if not host or host.endswith("pawchive.pw"):
            continue
        if url.lower() in seen:
            continue
        seen.add(url.lower())
        text = re.sub(r"<[^>]+>", "", m.group(2) or "").strip()
        links.append({"kind": "link", "domain": host,
                      "url": url, "text": text[:200]})
    # ② 纯文本补扫：剥掉 HTML 标签后，正文里裸写（未被 <a> 包裹）的 URL。
    # href 在标签属性里随标签一起消失 → 与 ① 不会重复计数；<a> 锚点文本
    # 恰好是同一 URL 时按 URL 去重兜住。
    plain = html_mod.unescape(
        re.sub(r"<[^>]+>", " ", post.get("content") or ""))
    # 字符集按 RFC 3986 白名单：天然排除中文标点/汉字（URL 尾随句读不再
    # 被吞进 URL），MEGA 的 #fragment 与 -_.~ 都在集内。
    for m in re.finditer(
            r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+", plain):
        url = _trim_url_tail(m.group(0))
        host = urllib.parse.urlparse(url).netloc.lower()
        if not host or host.endswith("pawchive.pw"):
            continue
        low = url.lower()
        if low in seen or any(u.startswith(low) for u in seen):
            # 去重含前缀形态：<a href="…#key"> 锚点文本常是不带 key 的同链接
            continue
        seen.add(low)
        links.append({"kind": "text", "domain": host,
                      "url": url, "text": ""})

    embed = post.get("embed") or {}
    if isinstance(embed, dict):
        url = str(embed.get("url") or "")
        if re.match(r"^https?://", url, re.I) and url.lower() not in seen:
            seen.add(url.lower())
            links.append({
                "kind": "embed",
                "domain": urllib.parse.urlparse(url).netloc.lower(),
                "url": url, "text": (embed.get("subject") or "")[:200]})
    return links


def _file_entry(att):
    """附件 → {url, filename}：直链指向 file 服务器原始文件（非缩略图）。"""
    path = att["path"]
    name = att.get("name") or os.path.basename(path)
    qname = urllib.parse.quote(name)
    return {"url": f"{config.PAWCHIVE_FILE_BASE}/data{path}?f={qname}",
            "filename": name}


def is_noise_ext_link(link):
    """YouTube 预览外链判定（作者贴的宣传预览，非可处理网盘资源）。"""
    import urllib.parse as _up
    try:
        host = _up.urlparse(str((link or {}).get("url") or "")).netloc.lower()
    except ValueError:
        return False
    return (host == "youtube.com" or host == "youtu.be"
            or host.endswith(".youtube.com") or host == "youtu.be")


def build_scan_records(creator, posts, faved_ids=None, scope="notfaved",
                       since=None):
    """帖子列表 → 入库记录；只保留目标范围内**有可下载直链或有外链**的帖子。

    faved_ids=None（无 Cookie）时范围判断跳过（scope=notfaved 退化为全部）。
    since：可选日期（YYYY-MM-DD），只收 published ≥ 该日的帖子（含当天），
    更早的一律过滤——用于「只要某个日期之后的新帖」。date_override 与
    published 无关；纯文字/纯外站帖仍不进生命周期。subdir 决定落盘目录：
    CHROME_DOWNLOAD_DIR/Pawchive/<作者>/<日期>_<帖子ID>_<标题>/
    """
    from .naming import sanitize_filename

    creator_name = creator.get("name") or f"{creator['service']}/{creator['id']}"
    records = []
    for p in posts:
        pid = str(p["id"])
        if since is not None and (p.get("published") or "")[:10] < since:
            continue
        if faved_ids is not None:
            if scope == "notfaved" and pid in faved_ids:
                continue
            if scope == "faved" and pid not in faved_ids:
                continue
        files = [_file_entry(a) for a in (p.get("attachments") or [])
                 if a and a.get("path")]
        ext_links = extract_links(p)
        if not files and not ext_links:
            continue
        title = p.get("title") or ""
        date = (p.get("published") or "")[:10] or "unknown"
        subdir = "Pawchive/{}/{}_{}_{}".format(
            sanitize_filename(creator_name) or "creator",
            date, pid, sanitize_filename(title)[:60] or "untitled")
        records.append({
            "post_id": pid,
            "title": title,
            "published": p.get("published") or "",
            "post_url": (f"{config.PAWCHIVE_API_BASE}/{creator['service']}"
                         f"/user/{creator['id']}/post/{pid}"),
            "subdir": subdir,
            "files": files,
            "ext_links": ext_links,
        })
    return records


# ============================================================
# 扫描（后台任务）
# ============================================================
def _creator_label(creator):
    return (creator.get("name")
            or f"{creator.get('service')}/{creator.get('id')}")


async def start_scan(creator, scope="notfaved", since=None):
    """后台扫描一个创作者并落库；立即返回，结果经 notify 汇报。"""
    if state.PAW_SCAN_RUNNING is not None:
        return (f"⏳ 已有扫描在进行（{state.PAW_SCAN_RUNNING}），"
                "等它结束再发起（/paw status 看进度）")
    state.PAW_SCAN_RUNNING = _creator_label(creator)
    task = asyncio.create_task(_scan_and_notify(creator, scope, since))
    _SPAWNED_SCANS.add(task)
    task.add_done_callback(_SPAWNED_SCANS.discard)
    return (f"🐾 开始扫描 {_creator_label(creator)}"
            + (f"，只收 {since} 之后" if since else "")
            + f"（范围：{'全部帖子' if scope == 'all' or not config.PAWCHIVE_COOKIE else '未收藏帖子'}）"
            "，完成后通知。期间可 /paw status 看进度")


async def _scan_and_notify(creator, scope, since=None):
    """扫描主体：帖子分页 + 收藏对比 + 落库。异常只通知，不炸后台。"""
    label = _creator_label(creator)
    try:
        cookie = config.PAWCHIVE_COOKIE or None
        # 增量扫描：预载已入库帖子 id，整页已知即提前停止（首扫为空集=全量）
        try:
            known_ids = await asyncio.to_thread(
                runtime_db.pawchive_known_post_ids,
                creator["service"], str(creator["id"]))
        except runtime_db.DbUnavailable as e:
            logger.warning(f"🐾 已知集预载失败（本次全量分页）：{e}")
            known_ids = None
        posts = await asyncio.to_thread(
            fetch_creator_posts, creator["service"], creator["id"], cookie,
            known_ids)
        if cookie:
            # Cookie 过期时站点吐登录页，这里会炸——不能让整场扫描失败，
            # 降级为「无收藏对比」（PAW_LAST_SCAN 的 scope 会如实标注）
            try:
                faved_ids = await asyncio.to_thread(fetch_favorited_ids, cookie)
            except Exception as e:
                logger.warning(f"🐾 收藏对比拉取失败（本次按全量处理）：{e}")
                faved_ids = None
        else:
            faved_ids = None
        records = build_scan_records(creator, posts, faved_ids, scope,
                                     since=since)
        created, skipped = runtime_db.enqueue_pawchive_posts(
            creator["service"], str(creator["id"]), label, records,
            scan_batch=time.strftime("%Y-%m-%d %H:%M:%S"))
        n_files = sum(len(r["files"]) for r in records)
        n_links = sum(len(r["ext_links"]) for r in records)
        state.PAW_LAST_SCAN = {
            "creator": label,
            "scope": scope if faved_ids is not None else "all(无Cookie)",
            "since": since,
            "fetched": len(posts),
            "created": created,
            "skipped": skipped,
            "files": n_files,
            "links": n_links,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        summary = (
            f"🐾 Pawchive 扫描完成：{label}\n"
            f"拉取帖子 {len(posts)} | 新入队 {created}（跳过已存在 {skipped}）\n"
            f"附件直链 {n_files} 个 | 站外链接 {n_links} 条"
            + (f"\n（只收 {since} 及之后的帖子）" if since else ""))
        if not cookie:
            summary += "\n⚠️ 未配置 Cookie，本次按全部帖子处理（无法对比收藏）"
        await notify_user(summary)
    except Exception as e:
        logger.exception(f"🐾 Pawchive 扫描失败：{label}")
        await notify_user(f"❌ Pawchive 扫描失败（{label}）：{e}")
    finally:
        state.PAW_SCAN_RUNNING = None


_FILE_STATUS_MARK = {
    runtime_db.PAW_FILE_DONE: "✅",
    runtime_db.PAW_FILE_FAILED: "❌",
}


def att_text(ref):
    """/paw att <URL|帖子ID|行id>：查帖子的全部附件与外链及其状态。

    附件行：状态符号 + 文件名（+ 大小/错误）；外链行：状态 + 域名 + URL。
    找不到帖子返回 ❌ 提示。"""
    q = str(ref or "").strip()
    row = None
    if q.isdigit():
        row = runtime_db.get_pawchive_post_row(int(q))
        if row is None:
            for r in runtime_db.find_pawchive_posts_by_post_id(q):
                row = r
                break
    else:
        parsed = parse_post_ref(q)
        if parsed is not None:
            _svc, _cid, pid = parsed
            rows = runtime_db.find_pawchive_posts_by_post_id(pid)
            if len(rows) == 1:
                row = rows[0]
            elif len(rows) > 1:
                listing = "\n".join(
                    f"  · #{r['id']} {r['creator_name']}（{r['status']}）"
                    for r in rows)
                return (f"{TEXT_PREFIX}\n⚠️ 帖子 {pid} 对应多条记录，"
                        f"请用行 id 精确指定：\n{listing}")
    if row is None:
        return (f"{TEXT_PREFIX}\n❌ 找不到帖子：{ref or '（空）'}\n"
                "用法：/paw att <帖子URL | 帖子ID | 行id>")
    try:
        files = runtime_db.list_pawchive_files(row["id"])
    except runtime_db.DbUnavailable:
        files = []

    lines = [f"{TEXT_PREFIX}：{row['creator_name']}｜"
             f"{(row['published'] or '')[:10]}｜{(row['title'] or '')[:40]}"
             f"（{row['status']}）", ""]
    if files:
        lines.append(f"📎 附件 {len(files)} 个：")
        for f in files:
            mark = _FILE_STATUS_MARK.get(f["status"], "⏳")
            size = (f"（{f['size_bytes'] // 1024 // 1024} MB）"
                    if f.get("size_bytes") else "")
            err = f"｜{f['error'][:40]}" if f.get("error") else ""
            lines.append(f"  {mark} {f['filename']}{size}{err}")
    else:
        lines.append("📎 无附件")
    ext = row.get("ext_links") or []
    if ext:
        shown = [l for l in ext if not is_noise_ext_link(l)]
        lines.append(f"🌐 外链 {len(shown)} 条：")
        for l in shown:
            st = "✅已处理" if row["status"] == "COMPLETED" else "👤未处理"
            lines.append(f"  {st} [{l.get('domain')}] {l.get('url')}")
    lines.append(f"原帖：{row.get('post_url') or '（无）'}")
    return "\n".join(lines)


async def att_reply(event, arg):
    """/paw att 分发：文本回复（纯查询，无按钮）。"""
    await event.reply(att_text(arg), link_preview=False)


async def post_reply_text(text):
    """/paw post <URL|帖子ID> 的主体：入队单帖或重投已有失败帖，返回回执。

    裸帖子 ID 只能在**已扫描入库**的帖子上工作（库里才有 service/创作者）；
    URL 输入则随时可用（详情接口公开，创作者名即时补齐）。
    """
    ref = parse_post_ref(text)
    if ref is None:
        return (f"{TEXT_PREFIX}\n用法：/paw post <帖子URL 或 帖子ID>\n"
                "例：/paw post https://pawchive.pw/patreon/user/1/post/2")
    service, creator_id, post_id = ref

    # 已入库的帖子：按状态分流（重扫不会复活它——这里提供显式入口）
    existing = runtime_db.find_pawchive_posts_by_post_id(post_id)
    if service is None:
        if not existing:
            return (f"{TEXT_PREFIX}\n❌ 帖子 {post_id} 不在已扫描记录里，"
                    "且裸 ID 无法定位创作者——请粘贴完整帖子 URL")
        if len(existing) > 1:
            rows = "\n".join(
                f"  · #{r['id']} {r['creator_name']}（{r['status']}）"
                for r in existing)
            return f"{TEXT_PREFIX}\n⚠️ 帖子 {post_id} 对应多条记录，请用 URL 精确指定：\n{rows}"
        return _post_status_reply(existing[0])

    # URL 输入：命中已入库记录同样按状态分流
    for r in existing:
        if r["service"] == service and r["creator_id"] == str(creator_id):
            return _post_status_reply(r)

    try:
        detail = await asyncio.to_thread(
            fetch_post_detail, service, creator_id, post_id, config.PAWCHIVE_COOKIE or None)
        profile = None
        try:
            profile = await asyncio.to_thread(
                fetch_creator_profile, service, creator_id)
        except Exception as e:
            logger.warning(f"🐾 创作者资料拉取失败（用 ID 代替名字）：{e}")
    except Exception as e:
        return f"{TEXT_PREFIX}\n❌ 帖子详情拉取失败：{e}"
    creator_name = (profile or {}).get("name") or f"{service}/{creator_id}"
    creator = {"service": service, "id": str(creator_id), "name": creator_name}
    records = build_scan_records(creator, [detail], faved_ids=None, scope="all")
    if not records:
        return (f"{TEXT_PREFIX}\n该帖子没有可下载附件也没有外链（纯文字帖）。")
    rec = records[0]
    created, _skipped = runtime_db.enqueue_pawchive_posts(
        service, str(creator_id), creator_name, [rec],
        scan_batch="单帖 " + time.strftime("%Y-%m-%d %H:%M:%S"))
    if created:
        return (f"{TEXT_PREFIX}\n📌 帖子已入队：{creator_name} #{post_id}\n"
                f"直链 {len(rec['files'])} 个（死链会被预检秒判）"
                f"｜外链 {len(rec['ext_links'])} 条\n"
                f"worker 将自动下载，落盘 {rec['subdir']}")
    return _post_status_reply(runtime_db.find_pawchive_posts_by_post_id(post_id)[0])


def _post_status_reply(row):
    """已入库帖子的状态回执 + 对应动作提示。"""
    st = row["status"]
    label = _STATUS_LABELS.get(st, st)
    head = f"{TEXT_PREFIX}\n帖子 #{row['id']}（{row['creator_name']}）状态：{label}"
    if st == runtime_db.PAW_POST_FAILED:
        n = runtime_db.retry_pawchive_posts(row_ids=[row["id"]])
        if n:
            return (head + "\n🔁 之前失败，已重投队列"
                    "（死链文件会被预检再次跳过，其余文件重新下载）")
    if st == runtime_db.PAW_POST_COMPLETED:
        return head + "\n✅ 已下载完成，文件在 " + config.CHROME_DOWNLOAD_DIR
    if st == runtime_db.PAW_POST_MANUAL:
        return head + "\n👤 直链已下载，另有外链需人工处理：\n" + "\n".join(
            f"  · [{l.get('domain')}] {l['url']}"
            for l in (row.get("ext_links") or [])[:6])
    return head


def _search_local_files(term, root=None, max_results=15, max_depth=4):
    """在 /sh 当前工作目录下按名称搜文件（深度/条数有界，跳过临时文件）。"""
    import fnmatch
    root = os.path.expanduser(root or state.SHELL_CWD)
    if not os.path.isdir(root):
        return None, []
    hits, temp_suffixes = [], (".part", ".crdownload", ".download")
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath[len(root):].count(os.sep)
        if depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith(".") or name.lower().endswith(temp_suffixes):
                continue
            if fnmatch.fnmatch(name.lower(), f"*{term.lower()}*"):
                hits.append(os.path.join(dirpath, name))
                if len(hits) >= max_results:
                    return root, hits
    return root, hits


async def find_reply(term):
    """/paw find <关键词>：按名称查 Pawchive 记录 + /sh 当前目录下的文件。"""
    term = str(term or "").strip()
    if not term:
        return (f"{TEXT_PREFIX}\n用法：/paw find <关键词>\n"
                "（搜已扫描的帖子标题/作者 + 当前 /sh 工作目录下的文件名）")
    sections = [f'🐾 查询「{term}」', ""]

    # 1) 库内记录
    try:
        rows = runtime_db.search_pawchive_posts(term, limit=10)
    except runtime_db.DbUnavailable as e:
        rows = []
        sections.append(f"❌ Runtime DB 不可用：{e}")
    if rows:
        sections.append(f"📋 扫描记录（{len(rows)} 条）：")
        for p in rows:
            mark = _STATUS_LABELS.get(p["status"], p["status"])
            sections.append(
                f"  #{p['id']} {mark} {p['creator_name']}｜"
                f"{(p['title'] or '')[:36]}")
        sections.append("")

    # 2) 当前目录下的文件（/sh 工作目录）
    root, hits = await asyncio.to_thread(_search_local_files, term)
    if root is None:
        sections.append(f"📂 当前目录不存在：{state.SHELL_CWD}")
    elif hits:
        sections.append(f"📂 当前目录文件（{len(hits)} 个）：")
        sections += [f"  {h}" for h in hits]
    else:
        sections.append(f"📂 当前目录（{root}）下没有匹配的文件")
    return "\n".join(sections)


async def notify_user(text):
    """统一通知出口（bot 控制面板对话）。函数内导入避免 app↔本模块成环。"""
    from . import notify
    await notify.notify_user(text)


# ============================================================
# 状态视图 / CSV
# ============================================================
_STATUS_LABELS = {
    runtime_db.PAW_POST_PENDING: "⏳ 待处理",
    runtime_db.PAW_POST_PROCESSING: "🔄 处理中",
    runtime_db.PAW_POST_COMPLETED: "✅ 已完成",
    runtime_db.PAW_POST_MANUAL: "👤 待人工",
    runtime_db.PAW_POST_FAILED: "❌ 失败",
    runtime_db.PAW_POST_ARCHIVED: "🗄 已归档",
}


def _disk_free_gb():
    try:
        return shutil.disk_usage(config.CHROME_DOWNLOAD_DIR).free / 1024 ** 3
    except OSError:
        return None


def status_text():
    """/paw 与菜单的 📊 状态视图（只读聚合，无副作用）。"""
    from . import chrome_client          # 函数内导入：保持模块导入轻量
    from . import pawchive_worker

    lines = [TEXT_PREFIX, ""]
    if state.PAW_SCAN_RUNNING:
        lines.append(f"🔄 正在扫描：{state.PAW_SCAN_RUNNING}")
    try:
        counts = runtime_db.pawchive_status_counts()
    except runtime_db.DbUnavailable as e:
        return f"{TEXT_PREFIX}\n❌ Runtime DB 不可用：{e}"
    if counts:
        lines.append(" | ".join(
            f"{_STATUS_LABELS.get(s, s)} {n}" for s, n in sorted(counts.items())))
    else:
        lines.append("（还没有扫描结果，/paw plan <作者名> 开始）")
    # 失败帖画像（2026-09-24）：把「❌ 失败」拆成可重投/纯死链，重投按钮
    # 才有决策依据（493 条失败里 464 条是死链，盲重投只空转）。
    failed_n = counts.get(runtime_db.PAW_POST_FAILED, 0)
    if failed_n:
        try:
            recoverable, dead = runtime_db.classify_pawchive_failed()
            lines.append(f"失败帖画像：♻️ 可重投 {recoverable} / "
                         f"🗄 纯死链 {dead}（/paw retry all 只重投前者）")
        except runtime_db.DbUnavailable:
            pass
    # Cookie 健康（只读展示最近一次校验结论，这里绝不打站点网络请求）
    if config.PAWCHIVE_COOKIE:
        ck = state.PAW_COOKIE_CHECK
        if ck.get("detail"):
            mark = "✅" if ck.get("ok") else "⚠️"
            lines.append(f"{mark} Cookie：{ck['detail']}")
        else:
            lines.append("🍪 Cookie：已配置（🧪 校验 可验证有效性）")
    if state.PAW_LAST_SCAN:
        s = state.PAW_LAST_SCAN
        lines.append(
            f"上次扫描：{s['creator']}（{s['scope']}）→ 新入队 {s['created']}，"
            f"直链 {s['files']}，外链 {s['links']}（{s['at']}）")

    inflight = pawchive_worker.current_post_label()
    if inflight:
        lines.append(f"当前处理：{inflight}")
    lines.append(
        "Worker：" + pawchive_worker.worker_state_text()
        + f" | Chrome Agent：{'运行中' if chrome_client.agent_running() else '未运行'}")
    free = _disk_free_gb()
    if free is not None:
        lines.append(f"下载盘剩余：{free:.1f} GB（保护线 "
                     f"{config.PAWCHIVE_MIN_FREE_GB:.0f} GB）")
    lines += [
        "",
        "命令：/paw plan <作者>｜/paw search <词>｜/paw manual",
        "/paw retry <ID|all>｜/paw pause｜/paw resume｜/paw csv <作者>",
    ]
    return "\n".join(lines)


def paw_view_text():
    """菜单 🐾 视图：状态正文 + 按钮组（build 时共用 status_text）。"""
    return status_text()


def menu_buttons():
    """🐾 Pawchive 面板按钮组（覆盖全部 /paw 子命令）。"""
    from . import pawchive_worker
    from .menu import encode_menu_data   # 函数内导入避免 menu↔本模块成环

    toggle = (Button.inline("▶️ 恢复处理", encode_menu_data("paw_resume"))
              if pawchive_worker.paused()
              else Button.inline("⏸ 暂停处理", encode_menu_data("paw_pause")))
    return [
        [Button.inline("🔄 刷新", encode_menu_data("paw")),
         toggle],
        [Button.inline("📥 扫描作者", encode_menu_data("paw_search")),
         Button.inline("📄 导出CSV清单", encode_menu_data("paw_csv"))],
        [Button.inline("👤 待人工处理", encode_menu_data("paw_manual")),
         Button.inline("🔁 重投全部失败", encode_menu_data("paw_retry_all"))],
        [Button.inline("📌 指定帖子下载", encode_menu_data("paw_post")),
         Button.inline("🔎 按名称查询", encode_menu_data("paw_find"))],
        [Button.inline("🍪 设置 Cookie", encode_menu_data("paw_cookie")),
         Button.inline("🧪 校验 Cookie", encode_menu_data("paw_cookie_check"))],
        [Button.inline("🔙 返回主菜单", encode_menu_data("home"))],
    ]


async def csv_reply(creator=None):
    """导出最近扫描作者（或指定作者）的 CSV 清单并发收藏夹，返回回执文案。"""
    if creator is None:
        last = (state.PAW_LAST_SCAN or {}).get("creator")
        creator = await resolve_creator_async(last) if last else None
    if creator is None:
        return f"{TEXT_PREFIX}\n❌ 还没有扫描过作者（先 📥 扫描作者 或 /paw plan <作者>）"
    creator_name = _creator_label(creator)
    try:
        path, err = await asyncio.to_thread(
            write_csv, creator["service"], str(creator["id"]), creator_name)
    except runtime_db.DbUnavailable as e:
        return f"{TEXT_PREFIX}\n❌ Runtime DB 不可用：{e}"
    if err:
        return f"{TEXT_PREFIX}\n{err}"
    await state.client.send_file("me", path)
    logger.info(f"🐾 Pawchive CSV 已发收藏夹：{path}")
    return f"{TEXT_PREFIX}\n📄 清单已发收藏夹：{os.path.basename(path)}"


async def retry_all_reply():
    """重投全部失败帖子（/paw retry all 的面板入口），返回回执文案。"""
    try:
        requeued, skipped = runtime_db.retry_pawchive_posts()
    except runtime_db.DbUnavailable as e:
        return f"{TEXT_PREFIX}\n❌ Runtime DB 不可用：{e}"
    if requeued:
        msg = f"🔁 已重投 {requeued} 条失败帖子"
        if skipped:
            msg += f"（跳过 {skipped} 条纯死链帖，重试无意义）"
    else:
        msg = "没有需要重投的失败帖子" + (
            f"（{skipped} 条纯死链帖已跳过）" if skipped else "")
    return f"{TEXT_PREFIX}\n{msg}"


def manual_view(limit=10):
    """MANUAL（人工处理）视图：文本（作者/日期/帖子/外链清单）+ 按钮行。

    每帖一行按钮：✅ 完成（paw_done 回调，人工处理完外链后点它标记
    COMPLETED）+ 🔗 原帖（URL 按钮，直接打开帖子）。"""
    from .menu import encode_menu_data   # 函数内导入避免 menu↔本模块成环
    try:
        posts = runtime_db.list_pawchive_posts(
            status=runtime_db.PAW_POST_MANUAL, limit=limit)
    except runtime_db.DbUnavailable as e:
        return (f"{TEXT_PREFIX}\n❌ Runtime DB 不可用：{e}", [])
    if not posts:
        return (f"{TEXT_PREFIX}\n👤 没有待人工处理的帖子"
                "（外链帖会在直链下完后出现在这里）", [])
    lines = [f"{TEXT_PREFIX}：👤 待人工处理 {len(posts)} 帖"
             "（处理完外链点 ✅ 标记完成）", ""]
    rows = []
    for p in posts:
        date = (p.get("published") or "")[:10] or "unknown"
        lines.append(f"#{p['id']} {p['creator_name']}｜{date}｜"
                     f"{(p['title'] or '')[:40]}")
        lines.append(p["post_url"])
        for l in (p.get("ext_links") or [])[:6]:
            lines.append(f"  · [{l.get('domain')}] {l['url']}")
        if len(p.get("ext_links") or []) > 6:
            lines.append(f"  … 等 {len(p['ext_links']) - 6} 条外链见帖子页")
        lines.append("")
        row = [Button.inline(f"✅ 完成 #{p['id']}",
                             encode_menu_data("paw_done", str(p["id"])))]
        if p.get("post_url"):
            row.append(Button.url("🔗 原帖", p["post_url"]))
        rows.append(row)
    return "\n".join(lines), rows


def manual_view_full(limit=10):
    """面板 paw_manual 分支用：manual_view + 底部面板导航按钮。"""
    view_text, rows = manual_view(limit)
    return view_text, rows + menu_buttons()


def manual_done_reply(arg):
    """面板 ✅ 完成 / /paw done 共用：标记完成 + 返回刷新后的视图
    （面板流程原地 edit，命令流程作为回复发出）。"""
    reply = mark_manual_done(arg)
    view_text, rows = manual_view()
    return f"{reply}\n\n{view_text}", rows + menu_buttons()


def manual_export_text(limit=500):
    """A1：全部待处理（MANUAL）帖的外链工作清单——按作者分组、带帖子行
    id 与原帖链接，供用户集中打开处理；处理后 /paw done <区间|作者> 批量
    标记完成。"""
    try:
        posts = runtime_db.list_pawchive_posts(
            status=runtime_db.PAW_POST_MANUAL, limit=limit)
    except runtime_db.DbUnavailable as e:
        return f"{TEXT_PREFIX}\n❌ Runtime DB 不可用：{e}"
    if not posts:
        return f"{TEXT_PREFIX}\n👤 没有待处理外链帖"
    lines = [f"{TEXT_PREFIX}：外链工作清单（{len(posts)} 帖，按作者）", ""]
    cur_author = None
    n_links = 0
    for p in posts:
        if p["creator_name"] != cur_author:
            cur_author = p["creator_name"]
            lines.append(f"━━ {cur_author}")
        date = (p.get("published") or "")[:10] or "unknown"
        lines.append(f"  #{p['id']} {date}｜{(p['title'] or '')[:44]}")
        lines.append(f"    原帖：{p.get('post_url') or '（无）'}")
        for l in (p.get("ext_links") or []):
            if is_noise_ext_link(l):
                continue
            n_links += 1
            lines.append(f"    🔗 [{l.get('domain')}] {l['url']}")
    lines.append("")
    lines.append(f"共 {len(posts)} 帖 / {n_links} 条外链。"
                 "处理完：/paw done <行id>、<起-止区间> 或 <作者名> 批量标记")
    return "\n".join(lines)


def archive_reply():
    """/paw archive failed：把 FAILED **死链帖**批量移入 ARCHIVED。
    帖内含可恢复文件的保持 FAILED（归档不得埋掉数据，2026-09-17 用户决策）。"""
    try:
        archived, kept = runtime_db.archive_pawchive_failed()
    except runtime_db.DbUnavailable as e:
        return f"{TEXT_PREFIX}\n❌ Runtime DB 不可用：{e}"
    if archived:
        msg = f"🗄 已归档 {archived} 个死链帖（不再干扰待办）"
        if kept:
            msg += f"；{kept} 帖含可恢复文件，保持 FAILED（/paw retry 重投）"
        return f"{TEXT_PREFIX}\n{msg}"
    return f"{TEXT_PREFIX}\nℹ️ 当前没有可归档的 FAILED 帖"


def manual_text(limit=10):
    """纯文本兼容形态（旧调用点）：只取 manual_view 的正文。"""
    return manual_view(limit)[0]


def mark_manual_done(arg):
    """/paw done：把 MANUAL 帖标成 COMPLETED（人工处理完外链）。

    三种形态：<#行id>（单条）/ <起-止区间>（批量行 id）/ <作者名>
    （该作者全部 MANUAL 帖）。已完成的幂等提示；不存在/非 MANUAL 报错。"""
    raw = str(arg or "").lstrip("#").strip()
    # 区间形态：105-120
    m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", raw)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        done = skipped = 0
        try:
            for rid in range(lo, hi + 1):
                if runtime_db.complete_pawchive_manual_post(rid):
                    done += 1
                else:
                    skipped += 1
        except runtime_db.DbUnavailable as e:
            return f"{TEXT_PREFIX}\n❌ Runtime DB 不可用：{e}"
        return (f"{TEXT_PREFIX}\n✅ 区间 {lo}-{hi}：标记完成 {done} 帖"
                + (f"（{skipped} 条非 MANUAL/不存在跳过）" if skipped else ""))
    # 作者形态：非数字 → 按作者名匹配
    if raw and not raw.isdigit():
        try:
            posts = runtime_db.list_pawchive_posts(
                status=runtime_db.PAW_POST_MANUAL, limit=1000)
        except runtime_db.DbUnavailable as e:
            return f"{TEXT_PREFIX}\n❌ Runtime DB 不可用：{e}"
        targets = [p for p in posts if raw.lower() in
                   (p["creator_name"] or "").lower()]
        if not targets:
            return f"{TEXT_PREFIX}\n❌ 没有找到作者含「{raw}」的待人工帖"
        done = 0
        try:
            for p in targets:
                if runtime_db.complete_pawchive_manual_post(p["id"]):
                    done += 1
        except runtime_db.DbUnavailable as e:
            return f"{TEXT_PREFIX}\n❌ Runtime DB 不可用：{e}"
        return (f"{TEXT_PREFIX}\n✅ 作者「{targets[0]['creator_name']}」："
                f"标记完成 {done}/{len(targets)} 帖")
    # 单条形态（原逻辑）
    if not raw.isdigit():
        return (f"{TEXT_PREFIX}\n❌ 用法：/paw done <行id | 起-止 | 作者名>"
                "（/paw manual 里 # 后面的数字）")
    try:
        ok = runtime_db.complete_pawchive_manual_post(int(raw))
    except runtime_db.DbUnavailable as e:
        return f"{TEXT_PREFIX}\n❌ Runtime DB 不可用：{e}"
    if ok:
        return (f"{TEXT_PREFIX}\n✅ #{raw} 外链帖已标记完成"
                "（MANUAL → COMPLETED）")
    row = runtime_db.get_pawchive_post_row(int(raw))
    if row and row["status"] == runtime_db.PAW_POST_COMPLETED:
        return f"{TEXT_PREFIX}\nℹ️ #{raw} 已经标记过了"
    return f"{TEXT_PREFIX}\n❌ #{raw} 不存在或不是待人工状态（只允许 MANUAL → 完成）"



def pause_reply():
    from . import pawchive_worker
    msg = pawchive_worker.pause()
    return f"{TEXT_PREFIX}\n{msg}"


def resume_reply():
    from . import pawchive_worker
    msg = pawchive_worker.resume()
    return f"{TEXT_PREFIX}\n{msg}"


def write_csv(creator_service, creator_id, creator_name):
    """从 SQLite 生成某作者的 CSV（附件直链 + 外链），返回文件路径或 (None, 错误)。

    列与独立 CLI 脚本产出的记录一致；utf-8-sig 让 Excel 直接打开不乱码。
    """
    posts = [p for p in runtime_db.list_pawchive_posts(limit=100000)
             if p["service"] == str(creator_service)
             and p["creator_id"] == str(creator_id)]
    if not posts:
        return None, f"❌ 没有创作者 {creator_name} 的扫描记录（先 /paw plan）"
    out_dir = os.path.join(config.DOWNLOAD_DIR, "Pawchive",
                           creator_name or f"{creator_service}_{creator_id}")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir,
                        f"清单_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    rows = []
    for p in sorted(posts, key=lambda x: x["id"], reverse=True):
        for f in runtime_db.list_pawchive_files(p["id"]):
            rows.append([p["creator_name"], p["service"], p["post_id"],
                         p["published"], p["title"], p["post_url"],
                         "附件", f["filename"] or "", f["url"],
                         _STATUS_LABELS.get(p["status"], p["status"])])
        for l in (p.get("ext_links") or []):
            rows.append([p["creator_name"], p["service"], p["post_id"],
                         p["published"], p["title"], p["post_url"],
                         "外链", l.get("domain") or "", l["url"],
                         _STATUS_LABELS.get(p["status"], p["status"])])
    if len(rows) > config.PAWCHIVE_CSV_MAX_ROWS:
        return None, (f"❌ 行数 {len(rows)} 超过上限 "
                      f"{config.PAWCHIVE_CSV_MAX_ROWS}，请用独立 CLI 脚本导出")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["作者", "平台", "帖子ID", "发布时间", "帖子标题",
                    "帖子页面", "类型", "附件文件名/外链域名", "链接", "帖子状态"])
        w.writerows(rows)
    return path, None


# ============================================================
# 命令解析
# ============================================================
_PAW_CMD_RE = re.compile(r"^/paw(?:\s|$)", re.IGNORECASE)


def is_paw_command(text) -> bool:
    """/paw 开头（含裸命令）；/pawfoo 不算。"""
    return bool(_PAW_CMD_RE.match(str(text or "").strip()))


def parse_paw_command(text):
    """/paw 子命令 → (action, arg)。裸 /paw = status（最常用的默认页）。"""
    raw = str(text or "").strip()
    body = raw[len("/paw"):].strip()
    if not body:
        return ("status", None)
    head, _, rest = body.partition(" ")
    head_l = head.lower()
    if head_l in ("help", "status", "plan", "search", "retry", "pause",
                  "resume", "manual", "done", "archive", "att", "post",
                  "cookie", "csv", "find"):
        return (head_l, rest.strip() or None)
    return ("help", None)


async def command_reply(event, cmd_text):
    """/paw 命令入口（commands.py 薄分发到这里）。每个分支自行回帖。"""
    action, arg = parse_paw_command(cmd_text)

    if action == "help":
        await event.reply(_help_text(), link_preview=False)
        return
    if action == "status":
        await event.reply(status_text(), link_preview=False)
        return
    if action == "pause":
        await event.reply(pause_reply(), link_preview=False)
        return
    if action == "resume":
        await event.reply(resume_reply(), link_preview=False)
        return
    if action == "manual":
        if (arg or "").strip().lower() == "export":
            await event.reply(manual_export_text(), link_preview=False)
            return
        view_text, buttons = manual_view()
        await event.reply(view_text, buttons=buttons, link_preview=False)
        return
    if action == "done":
        reply = mark_manual_done(arg)
        view_text, buttons = manual_view()
        await event.reply(f"{reply}\n\n{view_text}", buttons=buttons,
                          link_preview=False)
        return
    if action == "archive":
        await event.reply(archive_reply(), link_preview=False)
        return
    if action == "att":
        await event.reply(att_text(arg), link_preview=False)
        return
    if action == "paw_done":
        reply = mark_manual_done(arg)
        view_text, buttons = manual_view()
        await event.reply(f"{reply}\n\n{view_text}", buttons=buttons,
                          link_preview=False)
        return
    if action == "search":
        await _reply_search(event, arg)
        return
    if action == "plan":
        await _reply_plan(event, arg)
        return
    if action == "retry":
        await _reply_retry(event, arg)
        return
    if action == "cookie":
        await _reply_cookie(event, arg)
        return
    if action == "csv":
        await _reply_csv(event, arg)
        return
    if action == "post":
        await event.reply(await post_reply_text(arg), link_preview=False)
        return
    if action == "find":
        await event.reply(await find_reply(arg), link_preview=False)
        return
    await event.reply(_help_text(), link_preview=False)


def _help_text():
    return (
        f"{TEXT_PREFIX}\n\n"
        "用法：\n"
        "  /paw plan <作者名> [all] —— 扫描作者帖子入队（默认只收未收藏帖；"
        "  /paw plan <作者名> since <YYYY-MM-DD> —— 只收该日期之后的帖子"
        "带 Cookie 才能对比收藏，all=全部）\n"
        "  /paw search <关键词> —— 搜作者\n"
        "  /paw post <帖子URL|ID> —— 单独获取指定帖子的附件\n"
        "  /paw find <关键词> —— 按名称查扫描记录与当前目录文件\n"
        "  /paw manual —— 待人工处理的帖子（含外链清单与 ✅ 按钮）\n"
        "  /paw manual export —— 全部待处理外链导出为工作清单\n"
        "  /paw att <URL|帖子ID|行id> —— 查帖子的附件与外链状态\n"
        "  /paw archive —— FAILED 死链帖批量归档（不占待办）\n"
        "  /paw done <行id | 起-止 | 作者名> —— 批量标记完成\n"
        "  /paw retry <行ID|all> —— 失败帖子重投\n"
        "  /paw pause / resume —— 暂停/恢复下载 worker\n"
        "  /paw cookie <Cookie> —— 保存会话 Cookie（用于收藏对比）\n"
        "  /paw csv <作者名> —— 导出直链清单 CSV 发到收藏夹\n"
        "  /paw 或 /paw status —— 状态总览\n\n"
        "下载由内置并发下载器执行，落盘 "
        f"{config.DOWNLOAD_DIR}/Pawchive/<作者>/<帖子>/"
    )


async def _reply_search(event, term):
    if not term:
        await event.reply(f"{TEXT_PREFIX}\n用法：/paw search <关键词>",
                          link_preview=False)
        return
    try:
        hits = await asyncio.to_thread(search_creators, term, 5)
    except Exception as e:
        await event.reply(f"{TEXT_PREFIX}\n❌ 搜索失败：{e}", link_preview=False)
        return
    if not hits:
        await event.reply(
            f"{TEXT_PREFIX}\n没有叫「{term}」的创作者（试试更短的词）",
            link_preview=False)
        return
    state.PAW_SEARCH_CANDIDATES = {str(i): c for i, c in enumerate(hits, 1)}
    lines = [f"{TEXT_PREFIX}：🔍 「{term}」匹配 {len(hits)} 个", ""]
    buttons = []
    for i, c in enumerate(hits, 1):
        lines.append(
            f"{i}. {c['name']}（{c['service']}，收藏 {c.get('favorited', 0)}）")
        buttons.append([Button.inline(
            f"📌 扫描 {c['name']}", encode_pick(i))])
    lines += ["", "点按钮直接开始扫描（默认只收未收藏帖）。"]
    from .menu import encode_menu_data
    buttons.append([Button.inline("🔙 返回", encode_menu_data("paw"))])
    await event.reply("\n".join(lines), buttons=buttons, link_preview=False)


def encode_pick(index):
    """搜索候选按钮的回调数据：只带序号（64 字节限内），候选存 state。"""
    from .menu import encode_menu_data
    return encode_menu_data("paw_pick", str(index))


async def _reply_plan(event, arg):
    if state.PAW_SCAN_RUNNING is not None:
        await event.reply(
            f"{TEXT_PREFIX}\n⏳ 已有扫描在进行（{state.PAW_SCAN_RUNNING}）",
            link_preview=False)
        return
    scope = "notfaved"
    since = None
    parts = (arg or "").split()
    if parts and parts[-1].lower() == "all":
        scope = "all"
        parts = parts[:-1]
    if len(parts) >= 2 and parts[-2].lower() == "since":
        since = parts[-1]
        parts = parts[:-2]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
            await event.reply(
                f"{TEXT_PREFIX}\n❌ 日期格式：YYYY-MM-DD"
                "（例：/paw plan 作者 since 2026-09-01）",
                link_preview=False)
            return
    name = " ".join(parts).strip()
    if not name:
        await event.reply(
            f"{TEXT_PREFIX}\n用法：/paw plan <作者名> [all]"
            "\n　　/paw plan <作者名> since <YYYY-MM-DD> [all]",
            link_preview=False)
        return
    try:
        creator = await resolve_creator_async(name)
    except Exception as e:
        await event.reply(f"{TEXT_PREFIX}\n❌ 解析作者失败：{e}",
                          link_preview=False)
        return
    if creator is None:
        await event.reply(
            f"{TEXT_PREFIX}\n没有叫「{name}」的创作者，先 /paw search <词>",
            link_preview=False)
        return
    if not config.PAWCHIVE_COOKIE and scope == "notfaved":
        scope = "all"   # 无 Cookie 无从对比，直接按全部处理（摘要里会提示）
    msg = await start_scan(creator, scope, since=since)
    await event.reply(f"{TEXT_PREFIX}\n{msg}", link_preview=False)


async def _reply_retry(event, arg):
    if not arg:
        await event.reply(f"{TEXT_PREFIX}\n用法：/paw retry <行ID|all>",
                          link_preview=False)
        return
    try:
        if arg.lower() == "all":
            requeued, skipped = runtime_db.retry_pawchive_posts()
            if requeued:
                msg = f"🔁 已重投 {requeued} 条失败帖子"
                if skipped:
                    msg += f"（跳过 {skipped} 条纯死链帖，重试无意义）"
            else:
                msg = "没有需要重投的失败帖子" + (
                    f"（{skipped} 条纯死链帖已跳过）" if skipped else "")
            await event.reply(f"{TEXT_PREFIX}\n{msg}", link_preview=False)
            return
        requeued, _skipped = runtime_db.retry_pawchive_posts(row_ids=[int(arg)])
        await event.reply(
            f"{TEXT_PREFIX}\n🔁 已重投 #{arg}" if requeued
            else f"{TEXT_PREFIX}\n❌ #{arg} 不在失败状态（或全部是已确认死链）",
            link_preview=False)
    except ValueError:
        await event.reply(f"{TEXT_PREFIX}\n❌ 行ID 须为数字（/paw status 查看）",
                          link_preview=False)
    except runtime_db.DbUnavailable as e:
        await event.reply(f"{TEXT_PREFIX}\n❌ Runtime DB 不可用：{e}",
                          link_preview=False)


async def _reply_cookie(event, arg):
    if arg:
        err = config.save_pawchive_cookie(arg)
        if err:
            await event.reply(f"{TEXT_PREFIX}\n❌ {err}", link_preview=False)
            return
        await event.reply(
            f"{TEXT_PREFIX}\n🍪 Cookie 已保存（{config.mask_douyin_cookie(arg)}）",
            link_preview=False)
        return
    # 无参数：bot 对话里开输入窗口（与抖音 cookie 同款交互）
    from . import bot as bot_mod
    if state.bot_client is not None:
        bot_mod.open_input_window("paw_cookie")
        await event.reply(
            f"{TEXT_PREFIX}\n🍪 请直接发送 Cookie 内容"
            f"（{config.PAWCHIVE_INPUT_WINDOW_SECONDS} 秒内有效，"
            "发 / 开头的命令取消）",
            link_preview=False)
        return
    await event.reply(f"{TEXT_PREFIX}\n用法：/paw cookie <Cookie 字符串>",
                      link_preview=False)


async def _reply_csv(event, arg):
    name = (arg or "").strip()
    try:
        creator = await asyncio.to_thread(resolve_creator, name) if name else None
    except Exception as e:
        await event.reply(f"{TEXT_PREFIX}\n❌ 解析作者失败：{e}",
                          link_preview=False)
        return
    if creator is None and state.PAW_LAST_SCAN:
        # 缺省用最近一次扫描的作者
        last = state.PAW_LAST_SCAN.get("creator")
        creator = await asyncio.to_thread(resolve_creator, last) if last else None
    if creator is None:
        await event.reply(
            f"{TEXT_PREFIX}\n用法：/paw csv <作者名>（先 /paw plan 扫描过）",
            link_preview=False)
        return
    await event.reply(f"{TEXT_PREFIX}\n{await csv_reply(creator)}",
                      link_preview=False)
