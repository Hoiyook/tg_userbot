"""Pawchive Worker —— 生命周期消费：领取帖子 → 内置并发下载 → 终态。

**扫描 ≠ 执行**的另一半（与 listener_worker 同构，执行体是本进程的 httpx
并发下载器，2026-09-15 起不再经 Chrome Agent——实测 Chrome 串行 + 代理路线
吞吐 0.27~10MB/s 波动且单文件阻塞整队，直连并发吞吐高一个量级）：

    claim（短事务落 PROCESSING + 租约）
      ↓  ← 事务到此结束，网络/文件操作一律在事务外
    死链预检（HEAD）：404/410 的直链直接标 FAILED——死链没有内容可下
      ↓
    帖内附件并发下载（asyncio.gather + 共享 DOWNLOAD_SEMAPHORE 限流；
    .part 断点续传、大小校验、3 次尝试、404/410 就地判死）
      ↓
    全 DONE 且无外链 → COMPLETED；有外链 → MANUAL（通知外链清单）；
    有 FAILED → FAILED（/paw retry 重投；全部是站点死链则只落库不通知）

落盘 `DOWNLOAD_DIR/Pawchive/<作者>/<日期>_<帖子ID>_<标题>/`（旧 Chrome 时代
的产物在 `TG Chrome Download/Pawchive/`）。进度注册进 register_download
（/progress 可见）；帖子级终态才是 worker 的通知面（COMPLETED 只记日志）。

**直连纪律**：httpx trust_env=False + 空 ProxyHandler opener——系统/环境
代理（socks5）urllib/httpx 都不友好，pawchive 直连可达且更快。

**幂等性**：at-least-once。DONE 写入前崩溃 → 租约到期 → 重领 → 已 DONE 的
文件跳过；.part 续传 + 目标已存在且大小一致 → 跳过下载。
"""
import asyncio
import collections
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request

import httpx

from . import config
from . import netio
from . import notify
from . import runtime_db
from . import state
from . import text as text_mod
from .log import logger

# 强制直连：系统代理（socks5）会让 urllib 秒抛 ValueError（2026-09-15 实测）
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 站点侧死链的失败标记（单一事实源在 runtime_db，retry 重投据此跳过死链）
_MISSING_MARK = runtime_db.PAW_DEAD_LINK_MARK
_DEAD_STATUS = (404, 410)
# 单文件重试策略（2026-09-15 深夜 CDN 断流爆发后强化）：
#   _DOWNLOAD_ATTEMPTS = 无进展尝试的上限（连续 3 次一无所获才判 failed）
#   _DOWNLOAD_TOTAL_CAP = 总尝试硬上限（有进展的断流不计入上限，但防死循环）
# 断流（RemoteProtocolError 等）若本次尝试有字节进账（.part 变大）→ 不消耗
# 重试额度，退避后从断点继续——大视频在抖动 CDN 上就是这样一段段搬完的。
_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_TOTAL_CAP = 12

# 正在处理的帖子行 id 集合；停机时全部放回 PENDING。
_INFLIGHT = set()
# 后台任务强引用（asyncio 只对 Task 持弱引用）
_TASKS = set()

# 人工暂停开关（/paw pause）：磁盘保护也走它。
_PAUSED = False
_PAUSE_REASON = ""

# 进度面板（主账号发 bot 对话、原地编辑；同 Runtime Reporter 模式）
_PANEL_MSG_ID = None
_PANEL_LAST_TEXT = None
# 里程碑计数：自上次汇总以来各终态的帖子数
_MILESTONE = {"completed": 0, "manual": 0, "failed": 0}


# ============================================================
# 暂停 / 状态
# ============================================================
def pause(reason="手动暂停"):
    """暂停 worker（不再领取新帖子；在途帖子继续到终态）。返回提示文案。"""
    global _PAUSED, _PAUSE_REASON
    _PAUSED = True
    _PAUSE_REASON = str(reason)
    logger.warning(f"🐾 Pawchive worker 已暂停：{_PAUSE_REASON}")
    return (f"⏸ 已暂停处理（{_PAUSE_REASON}）。在途帖子会跑完当前状态，"
            "不再领新帖。/paw resume 恢复")


def resume():
    """恢复 worker。返回提示文案。"""
    global _PAUSED, _PAUSE_REASON
    was = _PAUSED
    _PAUSED = False
    _PAUSE_REASON = ""
    logger.info("🐾 Pawchive worker 已恢复")
    return "▶️ 已恢复处理" if was else "本就处于运行状态"


def paused():
    return _PAUSED


def worker_state_text():
    return f"已暂停（{_PAUSE_REASON}）" if _PAUSED else "运行中"


def _fmt_elapsed(seconds):
    """已处理时长的人类可读形态：秒 → 秒/分秒/时分；None/负数返回空。"""
    if seconds is None or seconds < 0:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        return f"{seconds // 60}分{seconds % 60:02d}秒"
    return f"{seconds // 3600}时{(seconds % 3600) // 60:02d}分"


def current_post_label():
    """在途帖子的展示标签；无 / DB 不可用返回 None。

    每帖带「已处理 X」——从 claim 写入的 started_at 起算（帖级处理时长，
    用户 2026-09-16 要求）。"""
    if not _INFLIGHT:
        return None
    try:
        rows = [runtime_db.get_pawchive_post(r) for r in sorted(_INFLIGHT)]
    except runtime_db.DbUnavailable:
        return None
    # 只显示仍在 PROCESSING 的：_INFLIGHT 可能短暂残留已终态的行
    rows = [r for r in rows
            if r and r["status"] == runtime_db.PAW_POST_PROCESSING]
    if not rows:
        return None
    now = time.time()
    parts = []
    for r in rows[:3]:
        elapsed = _fmt_elapsed(
            now - r["started_at"] if r.get("started_at") else None)
        tail = f"（已处理 {elapsed}）" if elapsed else ""
        parts.append(f"#{r['id']} {r['creator_name']} "
                     f"{(r['title'] or '')[:24]}{tail}")
    return "；".join(parts)


def _disk_free_gb():
    try:
        return shutil.disk_usage(config.DOWNLOAD_DIR).free / 1024 ** 3
    except OSError:
        return None


# ============================================================
# 领取 / 释放
# ============================================================
def claim_next_post():
    """领一条 PENDING 帖子；暂停中 / 无任务 / DB 不可用返回 None。"""
    if _PAUSED:
        return None
    try:
        post = runtime_db.claim_next_pawchive_post()
    except runtime_db.DbUnavailable as e:
        logger.error(f"🐾 领取帖子失败（数据库不可用），本轮跳过：{e}")
        return None
    if post is not None:
        _INFLIGHT.add(post["id"])
    return post


def release_inflight(post_row=None):
    """把在途帖子放回 PENDING（优雅停机，不等租约到期）。

    文件状态原样保留（SUBMITTED→PENDING 的迁移在下一轮 reconcile 做）。
    """
    rows = [post_row] if post_row is not None else sorted(_INFLIGHT)
    ok = False
    for row in rows:
        try:
            ok = runtime_db.release_pawchive_post(row) or ok
        except runtime_db.DbUnavailable as e:
            logger.error(f"🐾 停机释放帖子 #{row} 失败（租约到期会自动恢复）：{e}")
    _INFLIGHT.difference_update(rows)
    return ok


def recover_expired():
    """启动时恢复过期租约。DB 不可用只记日志。"""
    try:
        return runtime_db.recover_expired_pawchive_posts()
    except runtime_db.DbUnavailable as e:
        logger.error(f"🐾 恢复过期租约帖子失败（不影响启动）：{e}")
        return 0


# ============================================================
# 死链预检（HTTP 在线程池跑，DB 写留在事件循环线程）
# ============================================================
def _head_status(url, timeout=15):
    """HEAD 探测直链，返回 HTTP 状态码；网络异常返回 None（不预判）。"""
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "Mozilla/5.0 tg-userbot-pawchive")
        with _DIRECT_OPENER.open(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        # 异常不判死（临时网络抖动交给下载重试），但类型要可见
        return "ERR:" + type(e).__name__


def _repair_url(url, filename):
    """老数据无扩展名 URL → 按文件名补扩展名；本就有扩展名返回 None。

    扩展名插在 query（?f=…）之前；文件名无合法扩展名（字母数字、≤5 位）
    不动。纯函数可单测。"""
    base = os.path.basename(urllib.parse.urlsplit(url).path)
    if "." in base:
        return None
    _stem, dot, ext = str(filename or "").rpartition(".")
    # 纯数字段（如「2024.09.22」的 22）不是扩展名，别往上拼
    if not dot or not ext.isalnum() or len(ext) > 5 or ext.isdigit():
        return None
    if "?" in url:
        head, _sep, query = url.partition("?")
        return f"{head}.{ext}?{query}"
    return f"{url}.{ext}"


def _head_dead_ids(targets):
    """并发 HEAD 一批 (file_row, url)，返回 (死链 file id 集合, 自愈映射)。

    纯 HTTP，在 to_thread 里跑；**绝不碰 runtime_db**（DB 连接属主线程，
    sqlite3 的 check_same_thread 会拒绝跨线程使用）。

    自愈（2026-09-24）：站点 CDN 起对无扩展名路径 404，而老数据存的 URL
    都不带扩展名——404 时先按文件名补扩展名重探一次，活着就把修复后的
    URL 交回调用方回写 DB，绝不在这种情况下误判死链。"""
    import concurrent.futures

    if not targets:
        return set(), {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(ex.map(lambda t: _head_status(t[1]), targets))
    summary = collections.Counter(str(code) for code in codes)
    logger.info(f"🐾 死链预检：{len(targets)} 个直链 → {dict(summary)}")

    repaired = {}
    still_dead = []
    recheck = []
    for t, code in zip(targets, codes):
        if code not in _DEAD_STATUS:
            continue
        fixed = _repair_url(t[1], (t[0] or {}).get("filename"))
        if fixed:
            recheck.append((t, fixed))
        else:
            still_dead.append(t)
    if recheck:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            codes2 = list(ex.map(lambda t: _head_status(t[1]), recheck))
        for (t, fixed), code in zip(recheck, codes2):
            if code in _DEAD_STATUS:
                still_dead.append(t)
            else:
                repaired[t[0]["id"]] = fixed
                logger.info(f"🐾 URL 自愈：{t[0].get('filename')} "
                            f"补扩展名后可访问（HTTP {code}）")
    return {t[0]["id"] for t in still_dead}, repaired


def _archive_dest(archive_path):
    """压缩包 → 解压目标文件夹（同名去扩展名）。"""
    return os.path.splitext(archive_path)[0]


def _extract_archive_sync(archive_path):
    """解压单个压缩包（阻塞，线程内执行）。返回 (status, detail)。

    status: "extracted"（成功，压缩包已删）/ "password"（需密码，保留）/
            "failed"（解压出错，保留）/ "no-space"（磁盘不足，保留）
    规则（用户 2026-09-28）：zip 用 stdlib（加密位检测+zip 炸弹守卫）；
    rar 用 bsdtar（先列表探测，失败=需密码/损坏 → 保留）；解压成功且目标
    目录非空才删除压缩包，绝不丢数据。
    """
    import zipfile
    lower = archive_path.lower()
    dest = _archive_dest(archive_path)
    free = shutil.disk_usage(os.path.dirname(archive_path)).free
    margin = int(config.PAWCHIVE_MIN_FREE_GB) * 1024 ** 3

    if lower.endswith(".zip"):
        try:
            with zipfile.ZipFile(archive_path) as z:
                infos = z.infolist()
                if any(zi.flag_bits & 0x1 for zi in infos):
                    return "password", "zip 成员加密"
                total_unc = sum(zi.file_size for zi in infos)
                if total_unc and total_unc > free - margin:
                    return ("no-space",
                            f"解压需 {total_unc/1e9:.1f}GB 超出磁盘余量")
                z.extractall(dest)
        except RuntimeError as e:
            # stdlib 读到加密成员时的典型异常
            return "password", str(e)[:120]
        except Exception as e:
            return "failed", f"{type(e).__name__}: {e}"
    else:  # .rar → bsdtar（macOS/Termux 自带；列表探测失败=需密码/损坏）
        import subprocess
        try:
            probe = subprocess.run(
                ["bsdtar", "-tf", archive_path],
                capture_output=True, timeout=120,
                stdin=subprocess.DEVNULL)
            if probe.returncode != 0:
                # 加密/损坏 rar 无法列出成员 → 需密码，原样保留
                return "password", (probe.stderr.decode("utf-8", "replace")
                                    or "list failed")[:120]
        except subprocess.TimeoutExpired:
            return "password", "list 超时（疑似加密卷）"
        except FileNotFoundError:
            return "failed", "bsdtar 不可用"

        try:
            os.makedirs(dest, exist_ok=True)
            p = subprocess.run(
                ["bsdtar", "-xf", archive_path, "-C", dest],
                capture_output=True, timeout=600,
                stdin=subprocess.DEVNULL)
            if p.returncode != 0:
                return "failed", p.stderr.decode("utf-8", "replace")[:120]
        except subprocess.TimeoutExpired:
            return "failed", "bsdtar 解压超时"
        except FileNotFoundError:
            return "failed", "bsdtar 不可用"

    # 成功判定：目标目录非空
    extracted = [n for _, _, ns in os.walk(dest) for n in ns]
    if not extracted:
        return "failed", "解压后目录为空"
    os.remove(archive_path)
    return "extracted", f"{len(extracted)} 个文件 → {os.path.basename(dest)}"


async def _extract_archive_serial(archive_path):
    """串行解压入口（持全局锁 + 线程下放 + 超时保护）。"""
    import asyncio as _a
    async with _extract_guard():
        try:
            return await _a.wait_for(
                asyncio.to_thread(_extract_archive_sync, archive_path),
                timeout=float(config.PAWCHIVE_EXTRACT_TIMEOUT_SECONDS))
        except _a.TimeoutError:
            return "failed", f"解压超时（>{config.PAWCHIVE_EXTRACT_TIMEOUT_SECONDS}s）"


def _thumb_url(file_url):
    """原文件 URL → 对应缩略图 URL（img.pawchive.pw/thumbnail/data{同路径}）。"""
    i = file_url.find("/data")
    if i < 0:
        return None
    return "https://img.pawchive.pw/thumbnail" + file_url[i + len("/data"):].split("?")[0]


def _fetch_thumbnails(targets):
    """并发抓取死链文件的缩略图（纯 HTTP，线程内跑；不碰 DB）。

    targets: [(file_row, original_url)]。返回 {file_id: bytes}——只含抓到的。
    """
    import concurrent.futures
    import urllib.request

    if not targets:
        return {}
    out = {}

    def one(item):
        f, _url = item
        thumb = _thumb_url(f["url"])
        if not thumb:
            return
        try:
            req = urllib.request.Request(thumb)
            req.add_header("User-Agent", "Mozilla/5.0 tg-userbot-pawchive")
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status == 200:
                    return f["id"], r.read()
        except Exception:
            return
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        for res in ex.map(one, targets):
            if res:
                out[res[0]] = res[1]
    return out


def _mark_dead(file_row, code=404):
    """死链就地标 FAILED（只在事件循环线程调用——DB 连接属主线程）。"""
    runtime_db.mark_pawchive_file_failed(
        file_row["id"], error=f"{_MISSING_MARK} HTTP {code}")
    file_row["status"] = runtime_db.PAW_FILE_FAILED
    file_row["error"] = _MISSING_MARK


# ============================================================
# 内置下载器（httpx 直连 + 并发）
# ============================================================
def _target_path(post, filename):
    """落盘绝对路径：DOWNLOAD_DIR/<subdir=「Pawchive/作者/帖子」>/<文件名>。

    文件名走带字节预算的变体：附件「名」可能是整条 patreon URL（2026-09-25
    MofuMochii 帖实测），不截则 OSError [Errno 63] File name too long。"""
    from .naming import sanitize_filename_bounded
    name = sanitize_filename_bounded(filename or "untitled")
    return os.path.join(config.DOWNLOAD_DIR,
                        post.get("subdir") or "Pawchive/unknown", name)


def _head_alive(url):
    """直连 HEAD：返回 (status, content_length)；异常返回 (None, None)。"""
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "Mozilla/5.0 tg-userbot-pawchive")
        with _DIRECT_OPENER.open(req, timeout=15) as r:
            n = r.headers.get("Content-Length")
            return r.status, (int(n) if n else None)
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None


def _download_one(post, f, progress):
    """下载单个文件（在线程池里跑阻塞 IO）。

    直连（不走代理——实测代理路线吞吐低一个量级且不稳定）；.part 断点
    续传；大小校验；404/410 判死。返回 (终态, size, error)：
    终态 ∈ {"done", "dead", "failed"}。
    """
    target = _target_path(post, f["filename"])
    part = target + ".part"
    # 目标已存在：与远端大小一致就跳过（重启/重投的幂等下载）
    if os.path.isfile(target):
        st, remote_len = _head_alive(f["url"])
        if st == 200 and remote_len is not None \
                and remote_len == os.path.getsize(target):
            return ("done", remote_len, None)
    os.makedirs(os.path.dirname(target), exist_ok=True)

    last_err = None
    attempts = 0          # 无进展尝试计数（有进账的断流不计）
    total_attempts = 0    # 总尝试硬上限（防服务端边给边断的死循环）
    while True:
        total_attempts += 1
        if total_attempts > _DOWNLOAD_TOTAL_CAP:
            break
        offset = os.path.getsize(part) if os.path.isfile(part) else 0
        exc = None        # except as e 在块尾会被删除——捕获到变量再判定
        rate_limited = False
        try:
            headers = {"User-Agent": "Mozilla/5.0 tg-userbot-pawchive"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            # trust_env=False：环境/系统代理（socks5）会让 httpx 直接不可用
            with httpx.Client(
                    timeout=httpx.Timeout(config.DOWNLOAD_IDLE_TIMEOUT),
                    trust_env=False, follow_redirects=True) as client:
                with client.stream("GET", f["url"], headers=headers) as resp:
                    if resp.status_code in _DEAD_STATUS:
                        return ("dead", None, f"HTTP {resp.status_code}")
                    resp.raise_for_status()
                    if offset and resp.status_code != 206:
                        offset = 0   # 服务端不支持 Range，从头下
                    length = resp.headers.get("Content-Length")
                    total = offset + (int(length) if length else 0)
                    got = offset
                    mode = "ab" if offset else "wb"
                    with open(part, mode) as fh:
                        for chunk in resp.iter_bytes(256 * 1024):
                            fh.write(chunk)
                            got += len(chunk)
                            progress(got, total)
            size = os.path.getsize(part)
            if total and size != total:
                raise IOError(f"大小不符 got={size} expect={total}")
            os.replace(part, target)
            return ("done", size, None)
        except httpx.HTTPStatusError as e:
            exc = e
            last_err = f"HTTP {e.response.status_code}"
            if e.response.status_code in _DEAD_STATUS:
                return ("dead", None, last_err)
            if e.response.status_code == 429:
                # 限流是暂时性的：等 60s 再试，不消耗无进展额度
                #（total_attempts 硬上限仍防死循环）
                rate_limited = True
        except Exception as e:   # noqa: BLE001 —— 网络类错误统一重试
            exc = e
            last_err = f"{type(e).__name__}: {e}"
        gained = os.path.getsize(part) - offset if os.path.isfile(part) else 0
        progressed = gained > 0 and isinstance(
            exc, (httpx.RemoteProtocolError, httpx.ReadError,
                  ConnectionError, TimeoutError))
        rate_limited = rate_limited or (
            exc is not None and "429" in str(exc))
        if rate_limited:
            # 429 限流：不消耗额度，等满 60s（服务端要求的节奏）
            logger.warning(
                f"🐾 限流（429），{config.PAWCHIVE_429_WAIT_SECONDS}s 后"
                f"断点续传：{f['filename']}")
            time.sleep(float(config.PAWCHIVE_429_WAIT_SECONDS))
        elif progressed:
            # 有进账的断流：不消耗重试额度，退避后从断点继续——
            # 大视频在抖动 CDN 上就是这样一段段搬完的
            logger.warning(
                f"🐾 文件尝试中断但有进账（+{gained} bytes，"
                f"累计 {os.path.getsize(part) if os.path.isfile(part) else 0}）："
                f"{f['filename']}（{last_err}），退避后 .part 续传")
        else:
            attempts += 1
            logger.warning(
                f"🐾 文件第 {attempts} 次无进展尝试失败：{f['filename']}"
                f"（{last_err}）"
                + ("，退避后重试" if attempts < _DOWNLOAD_ATTEMPTS else ""))
        if attempts >= _DOWNLOAD_ATTEMPTS:
            break
        if not rate_limited:
            time.sleep(min(30, 5 * (attempts or 1)))
    return ("failed", None, last_err)


async def _download_post_files(post, files):
    """帖内附件并发下载（共享 DOWNLOAD_SEMAPHORE 限流），就地更新状态。

    重名文件加前缀去重；进度经 register_download 的 did 上报（/progress）。
    返回是否有下载失败。
    """
    from .download import register_download, unregister_download, update_download
    from .naming import sanitize_filename

    pending = [f for f in files if f["status"] == runtime_db.PAW_FILE_PENDING]
    if not pending:
        return False

    seen = set()
    for f in pending:
        name = sanitize_filename(f["filename"] or "untitled")
        if name.lower() in seen:
            f["filename"] = f"dup_{f['id']}_{name}"
        seen.add(name.lower())

    async def one(f):
        did = register_download(
            "pawchive", f["filename"] or "untitled", None,
            link=post.get("post_url"))
        try:
            def progress(current, total):
                update_download(did, current, total)

            status, size, err = await asyncio.to_thread(
                _download_one, post, f, progress)
            if status == "done":
                runtime_db.mark_pawchive_file_done(f["id"], size_bytes=size)
                f["status"] = runtime_db.PAW_FILE_DONE
                f["size_bytes"] = size   # 内存同步：完成通知的统计读这里
                logger.info(
                    f"🐾 文件完成：{f['filename']}（{size or '?'} bytes）")
                # 附件压缩包自动解压（2026-09-28 用户要求）：串行、需密码跳过、
                # 成功后删压缩包；解压产物随 CD2 一并备份到 115
                fname_l = (f["filename"] or "").lower()
                if (config.PAWCHIVE_EXTRACT_ARCHIVES
                        and fname_l.endswith((".zip", ".rar"))):
                    target = _target_path(post, f["filename"])
                    try:
                        est, detail = await _extract_archive_serial(target)
                        if est == "extracted":
                            logger.info(
                                f"🗜 解压完成：{f['filename']} → {detail}")
                        elif est == "password":
                            logger.info(
                                f"🗜 压缩包需密码，保留原样：{f['filename']}")
                        else:
                            logger.warning(
                                f"🗜 解压未完成（{est}），压缩包保留："
                                f"{f['filename']}（{detail}）")
                    except Exception as e:
                        logger.warning(
                            f"🗜 解压异常（压缩包保留）：{f['filename']}：{e}")
            elif status == "dead":
                _mark_dead(f)
                logger.warning(f"🐾 死链（下载期 404/410）：{f['filename']}")
            else:
                runtime_db.mark_pawchive_file_failed(f["id"], error=err)
                f["status"] = runtime_db.PAW_FILE_FAILED
                f["error"] = err
                logger.warning(f"🐾 文件失败：{f['filename']}（{err}）")
        finally:
            unregister_download(did)

    await asyncio.gather(*(one(f) for f in pending))
    return any(f["status"] == runtime_db.PAW_FILE_FAILED for f in files)


# ============================================================
# 终态
# ============================================================
async def _finalize(post, files):
    """终态流转 + 帖子级通知。COMPLETED 不发 TG 通知（/paw status 可查）。"""
    failed = [f for f in files if f["status"] == runtime_db.PAW_FILE_FAILED]
    label = f"{post['creator_name']} #{post['id']}"
    title = (post.get("title") or "")[:50]
    if failed:
        # 2026-09-26 用户决策：**404 死链不计入失败**——除死链外全部完成
        # 的帖子直接 COMPLETED（死链附件从未存在于站点，不影响内容完整性）。
        # 仅剩死链、一个可用文件都没有的帖（纯死链）仍标 FAILED，走
        # /paw_archive 归档——标「完成」但零内容会误导。
        non_dead_failed = [f for f in failed
                           if not (f.get("error") or "").startswith(_MISSING_MARK)]
        if not non_dead_failed:
            done_n = sum(1 for f in files
                         if f["status"] == runtime_db.PAW_FILE_DONE)
            if done_n:
                err = (f"✅ 除 {len(failed)} 个站点死链附件外全部完成"
                       f"（{_MISSING_MARK}）")
                runtime_db.finalize_pawchive_post(
                    post["id"], runtime_db.PAW_POST_COMPLETED, error=err)
                logger.info(
                    f"🐾 帖子完成（含 {len(failed)} 个死链附件不计失败）："
                    f"{label}｜{title}")
                _bump_milestone("completed")
                await _milestone_notify_if_due()
                return
            err = (f"{len(failed)}/{len(files)} 个文件是站点死链"
                   f"（{_MISSING_MARK}）")
            runtime_db.finalize_pawchive_post(
                post["id"], runtime_db.PAW_POST_FAILED, error=err)
            logger.warning(f"🐾 帖子全部为站点死链，标失败：{label}｜{title}")
            _bump_milestone("failed")
            await _milestone_notify_if_due()
            return
        err = f"{len(failed)}/{len(files)} 个文件下载失败"
        runtime_db.finalize_pawchive_post(
            post["id"], runtime_db.PAW_POST_FAILED, error=err)
        lines = [f"❌ Pawchive 帖子失败：{label}｜{title}",
                 post.get("post_url") or "",
                 f"{err}（/paw retry {post['id']} 重投）"]
        for f in failed[:5]:
            reason = netio.humanize_net_error(f.get("error") or "")
            lines.append(f"  · {f['filename']}：{reason}")
        await notify.notify_user("\n".join(lines))
        _bump_milestone("failed")
        await _milestone_notify_if_due()
        return
    ext_links = post.get("ext_links") or []
    if ext_links:
        runtime_db.finalize_pawchive_post(
            post["id"], runtime_db.PAW_POST_MANUAL)
        lines = [f"👤 Pawchive 帖子待人工处理：{label}｜{title}",
                 post.get("post_url") or "",
                 f"直链已全部下载，另有 {len(ext_links)} 条外链："]
        for l in ext_links[:8]:
            lines.append(f"  · [{l.get('domain')}] {l['url']}")
        if len(ext_links) > 8:
            lines.append(f"  … 等 {len(ext_links) - 8} 条（/paw manual 查看全部）")
        await notify.notify_user("\n".join(lines))
        _bump_milestone("manual")
        await _milestone_notify_if_due()
        return
    runtime_db.finalize_pawchive_post(post["id"], runtime_db.PAW_POST_COMPLETED)
    logger.info(f"🐾 帖子完成：{label}｜{title}（{len(files)} 个文件）")
    _bump_milestone("completed")
    # 完成通知（同一开关；批量扫描嫌吵就 /paw notify off）
    from . import pawchive   # 函数内导入避免环
    if pawchive.notify_each_post_enabled():
        total = sum(f.get("size_bytes") or 0 for f in files)
        from .naming import format_size
        try:
            done_url = (f"\n原帖：{post['post_url']}"
                        if post.get("post_url") else "")
            await notify.notify_user(
                f"✅ Pawchive 完成：{post['creator_name']}｜{title}\n"
                f"发布：{(post.get('published') or '')[:10]}｜"
                f"{len(files)} 个文件 / {format_size(total)}\n"
                f"落盘 {config.DOWNLOAD_DIR}/{post.get('subdir') or ''}"
                + done_url)
        except Exception as e:
            logger.warning(f"完成通知发送失败（忽略）：{e}")
    _bump_milestone("completed")
    await _milestone_notify_if_due()


def _requeue_legacy_submitted(files):
    """旧 Chrome 时代的 SUBMITTED 文件全部放回 PENDING（执行体切换迁移）。

    chrome_task_id 清空：内置下载器不认 chrome task，活链接会重新下载；
    已 DONE 的不动。
    """
    for f in files:
        if f["status"] == runtime_db.PAW_FILE_SUBMITTED:
            runtime_db.mark_pawchive_file_pending(f["id"])
            f["status"] = runtime_db.PAW_FILE_PENDING
            f["chrome_task_id"] = None


def _panel_progress_lines():
    """进行中的 pawchive 下载明细（文件名 + 百分比 + 已下/总量）。"""
    from .naming import format_size
    lines = []
    for info in list(state.ACTIVE_DOWNLOADS.values()):
        if info.get("label") != "pawchive":
            continue
        name = (info.get("filename") or "")[:34]
        total = info.get("total") or 0
        done = info.get("downloaded") or 0
        pct = info.get("percent")
        if total:
            lines.append(f"  ↓ {name} {pct}%（{format_size(done)} / {format_size(total)}）")
        else:
            lines.append(f"  ↓ {name} {format_size(done)}")
    return lines


# 状态计数标签（进度面板与 /paw status 同源；ARCHIVED=死链归档）
_STATUS_LABELS = {
    runtime_db.PAW_POST_PENDING: "⏳",
    runtime_db.PAW_POST_PROCESSING: "🔄",
    runtime_db.PAW_POST_COMPLETED: "✅",
    runtime_db.PAW_POST_MANUAL: "👤",
    runtime_db.PAW_POST_FAILED: "❌",
    runtime_db.PAW_POST_ARCHIVED: "🗄",
}


def build_progress_text():
    """🐾 进度面板正文：状态计数 + 进行中明细 + 磁盘（/paw status 同源）。"""
    try:
        counts = runtime_db.pawchive_status_counts()
    except runtime_db.DbUnavailable as e:
        return f"🐾 Pawchive 进度\n❌ Runtime DB 不可用：{e}"
    parts = [f"{_STATUS_LABELS.get(s, s)}{n}" for s, n in sorted(counts.items())]
    lines = [f"🐾 Pawchive 进度（{time.strftime('%H:%M:%S')}）",
             " ".join(parts) if counts else "（队列为空）"]
    # 扫描实时进度（/paw status 同源）：让「有没有在扫、扫到哪了」一眼可见
    if state.PAW_SCAN_RUNNING:
        prog = state.PAW_SCAN_PROGRESS or {}
        bits = [p for p in (prog.get("stage"), prog.get("detail")) if p]
        head = f"🔎 扫描中：{state.PAW_SCAN_RUNNING}"
        lines.append(head + ("：" + " · ".join(bits) if bits else ""))
    inflight = current_post_label()
    if inflight:
        lines.append(f"当前：{inflight}")
    prog = _panel_progress_lines()
    if prog:
        lines += prog
    free = _disk_free_gb()
    if free is not None:
        lines.append(f"磁盘剩余 {free:.1f} GB（保护线 "
                     f"{config.PAWCHIVE_MIN_FREE_GB:.0f} GB）")
    ms = _MILESTONE
    if any(ms.values()):
        lines.append(f"本期：✅{ms['completed']} 👤{ms['manual']} ❌{ms['failed']}")
    return "\n".join(lines)


async def netio_shielded_panel(proc):
    """面板收发的 netio 收口（超时/网络层取消 → None），from . import netio
    延迟到调用时以避免潜在环。"""
    from . import netio
    return await netio.shielded(proc, 30, "Pawchive 进度面板")


async def _panel_send(text):
    """主账号发面板消息到 bot 对话（bot 未配置则收藏夹），返回消息 id。"""
    target = state.BOT_ID or "me"

    async def _send():
        return await state.client.send_message(target, text)

    msg = await netio_shielded_panel(_send)
    return getattr(msg, "id", None) if msg else None


async def _panel_edit(text):
    """原地编辑面板；返回 "ok" / "unchanged" / "invalid" / None。"""
    from telethon.errors import MessageNotModifiedError
    target = state.BOT_ID or "me"

    async def _edit():
        try:
            await state.client.edit_message(
                state.BOT_ID or "me", _PANEL_MSG_ID, text)
            return "ok"
        except MessageNotModifiedError:
            return "unchanged"
        except Exception as e:
            # 类名或消息文本任一含 MessageIdInvalid 都按面板失效处理
            # （Telethon 偶有包装变体，靠字符串兜底更稳）
            if "MessageIdInvalid" in type(e).__name__ or \
                    "MessageIdInvalid" in str(e):
                return "invalid"
            raise

    return await netio_shielded_panel(_edit)


_LAST_DISCONNECT_WARN = 0.0


def _throttled_disconnect_warning():
    """断连告警节流：代理抖动期 15 分钟最多一条（用户被「✅ 已恢复」刷屏）。

    只影响日志/观感，不改变 keepalive 的重连行为。
    """
    global _LAST_DISCONNECT_WARN
    now = time.monotonic()
    if now - _LAST_DISCONNECT_WARN > 900:
        _LAST_DISCONNECT_WARN = now
        return True
    return False


async def _refresh_panel():
    """刷新一轮面板：内容没变不编辑；失效重建；网络失败保留面板 id。"""
    global _PANEL_MSG_ID, _PANEL_LAST_TEXT
    text = text_mod.with_code_block(build_progress_text())
    if text == _PANEL_LAST_TEXT and _PANEL_MSG_ID is not None:
        return True
    if state.client is None:
        return False
    if _PANEL_MSG_ID is None:
        _PANEL_MSG_ID = await _panel_send(text)
        if _PANEL_MSG_ID is not None:
            _PANEL_LAST_TEXT = text
        return _PANEL_MSG_ID is not None
    outcome = await _panel_edit(text)
    if outcome == "ok":
        _PANEL_LAST_TEXT = text
        return True
    if outcome == "invalid":
        logger.info("🐾 进度面板消息已失效，下一轮重建")
        _PANEL_MSG_ID = None
    return False


async def _panel_loop():
    """进度面板循环：有活动内容变化就原地编辑（间隔 PAWCHIVE_PANEL_INTERVAL）。"""
    interval = float(config.PAWCHIVE_PANEL_INTERVAL_SECONDS)
    logger.info(f"🐾 Pawchive 进度面板已启动（每 {interval:.0f}s 刷新）")
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await _refresh_panel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"🐾 进度面板刷新失败（下轮再试）：{e}")
    except asyncio.CancelledError:
        logger.info("🐾 Pawchive 进度面板停止")
        raise


def _bump_milestone(kind):
    """终态计数 +1；攒够 PAWCHIVE_MILESTONE_POSTS 发一次汇总（异步部分由
    调用方 await _milestone_notify_if_due()）。"""
    _MILESTONE[kind] = _MILESTONE.get(kind, 0) + 1
    due = sum(_MILESTONE.values()) >= int(
        getattr(config, "PAWCHIVE_MILESTONE_POSTS", 50))
    return due


async def _milestone_notify_if_due():
    due = sum(_MILESTONE.values()) >= int(
        getattr(config, "PAWCHIVE_MILESTONE_POSTS", 50))
    if not due:
        return False
    ms = dict(_MILESTONE)
    for k in _MILESTONE:
        _MILESTONE[k] = 0
    try:
        await notify.notify_user(
            f"🐾 Pawchive 阶段汇总：✅完成 {ms['completed']} ｜"
            f"👤待人工 {ms['manual']} ｜❌失败 {ms['failed']}"
            "（/paw 看全局；/paw manual 看外链待办）")
    except Exception as e:
        logger.warning(f"🐾 里程碑汇总通知失败（忽略）：{e}")
    return True


_LAST_429_SWEEP = None   # None = 从未扫描过（monotonic 每进程从 0 起算，
                         # 初始 0.0 会让首次扫描被「now-0<900s」永久拦下）

# 缩略图追回任务状态（/paw recoverthumbs on|off；面板只读展示）
_THUMB_RECOVER = {"running": False, "stop": False, "done": 0, "total": None}

# 附件解压：全局串行锁（多帖并发的解压排队执行，一次一个）
_EXTRACT_LOCK = None
_EXTRACT_LOCK_LOOP = None


def _extract_guard():
    """解压串行锁（懒建 + 循环守卫，与 _fetch_gate 同款纪律）。"""
    global _EXTRACT_LOCK, _EXTRACT_LOCK_LOOP
    loop = asyncio.get_running_loop()
    if _EXTRACT_LOCK is None or _EXTRACT_LOCK_LOOP is not loop:
        _EXTRACT_LOCK = asyncio.Lock()
        _EXTRACT_LOCK_LOOP = loop
    return _EXTRACT_LOCK


def _is_429_only_failure(post):
    """帖子 FAILED 且所有 FAILED 文件都是 429 限流（可恢复，非死链）。"""
    files = runtime_db.list_pawchive_files(post["id"])
    failed = [f for f in files if f["status"] == runtime_db.PAW_FILE_FAILED]
    if not failed:
        return False
    return all("429" in (f.get("error") or "") for f in failed)


async def _auto_requeue_429():
    """定期重投「仅因 429 限流失败」的帖子（自动恢复，不再等手动 retry）。

    节流：每 PAWCHIVE_429_SWEEP_SECONDS 扫一次；attempts 超过
    PAWCHIVE_429_AUTO_RETRY_MAX_ATTEMPTS 的不再自动投（防无限循环，
    人工 /paw retry 仍可用）。429 文件本身不动——worker 重领后会重新
    预检/下载。
    """
    global _LAST_429_SWEEP
    now = time.monotonic()
    if _LAST_429_SWEEP is not None and \
            now - _LAST_429_SWEEP < float(config.PAWCHIVE_429_SWEEP_SECONDS):
        return 0
    _LAST_429_SWEEP = now
    try:
        rows = runtime_db.list_pawchive_posts(
            status=runtime_db.PAW_POST_FAILED, limit=200)
    except runtime_db.DbUnavailable as e:
        logger.error(f"🐾 429 自动重投扫描失败（本轮跳过）：{e}")
        return 0
    targets = []
    for p in rows:
        dbg_files = runtime_db.list_pawchive_files(p["id"])
        logger.warning(
            f"DBG sweep: #{p['id']} attempts={p.get('attempts')} "
            f"files={[(f['status'], f.get('error')) for f in dbg_files]}")
        if int(p.get("attempts") or 0) > int(
                config.PAWCHIVE_429_AUTO_RETRY_MAX_ATTEMPTS):
            logger.warning("DBG sweep: 跳过（超限）")
            continue
        if _is_429_only_failure(p):
            logger.warning(f"DBG sweep: 命中 target #{p['id']}")
            targets.append(p["id"])
    if not targets:
        logger.warning("DBG sweep: 无 target")
        return 0
    requeued = runtime_db.retry_pawchive_posts(row_ids=targets)
    if requeued:
        logger.info(
            f"🐾 自动重投 {requeued} 条 429 限流失败的帖子"
            f"（限流窗口已过，恢复正常下载）")
    return requeued


async def thumbnail_recovery_loop():
    """缩略图追回后台循环：历史死链文件的缩略图仍挂在站点缩略图服务器上
    （实测 ~100% 残留，35-75KB jpeg）——分批抓回落盘，把死链文件翻成 DONE。

    分批（每批 300）+ 批间 1s 节流，避免打爆缩略图服务器；/paw recoverthumbs
    off 置 stop 停止；重启后天然续跑（DONE 的文件不再出现在清单里）。
    全部追完后自动停止并通知。
    """
    st = _THUMB_RECOVER
    st["running"] = True
    st["stop"] = False
    total_dead = 0
    try:
        total_dead = runtime_db.count_pawchive_dead_thumbs()
    except runtime_db.DbUnavailable:
        pass
    st["total"] = total_dead
    logger.info(f"🖼 缩略图追回启动：待追回约 {total_dead} 个（每批 300，批间节流）")
    try:
        await notify.notify_user(
            f"🖼 Pawchive 缩略图追回已启动：待处理约 {total_dead} 个死链文件"
            "（低分辨率 jpeg 替代，35-75KB/张）。/paw recoverthumbs off 停止。")
        while not st["stop"]:
            try:
                batch = runtime_db.list_pawchive_dead_thumbs(limit=300)
            except runtime_db.DbUnavailable as e:
                logger.error(f"🖼 缩略图追回读批次失败（暂停 60s）：{e}")
                await asyncio.sleep(60)
                continue
            if not batch:
                break
            # 「原图已在本地」的不抓缩略图（用户 2026-09-28 要求）：
            # 死链前可能已下载过同名文件（同帖早期尝试/同作者其他帖），
            # 原件在就直接记账，省一次缩略图请求
            need_thumb = []
            for f in batch:
                target = _target_path({"subdir": f["subdir"]}, f["filename"])
                if os.path.isfile(target) and os.path.getsize(target) > 0:
                    runtime_db.mark_pawchive_file_done(
                        f["id"], size_bytes=os.path.getsize(target),
                        note="原图已在本地(死链前已下载)")
                    f["status"] = runtime_db.PAW_FILE_DONE
                    st["done"] += 1
                    logger.info(
                        f"🖼 原图已在本地，跳过缩略图：{f['filename']}")
                    continue
                need_thumb.append(f)
            data_map = await asyncio.to_thread(
                _fetch_thumbnails, [(f, f["url"]) for f in need_thumb])
            for f in need_thumb:
                data = data_map.get(f["id"])
                if not data:
                    continue   # 缩略图也 404：保持 FAILED，下轮不再列出？
                # 会再列出——用 DONE 标记跳过它：无缩略图的死链标 FAILED 不动，
                # 但追回循环靠 status 过滤，会重复。解决：标记特殊 error。
                target = _target_path({"subdir": f["subdir"]}, f["filename"])
                stem, _ext = os.path.splitext(target)
                thumb_target = stem + ".thumb.jpg"
                if os.path.isfile(thumb_target):
                    runtime_db.mark_pawchive_file_done(
                        f["id"], size_bytes=os.path.getsize(thumb_target),
                        note="缩略图追回(原图404)")
                    st["done"] += 1
                    continue
                os.makedirs(os.path.dirname(thumb_target), exist_ok=True)
                with open(thumb_target, "wb") as fh:
                    fh.write(data)
                runtime_db.mark_pawchive_file_done(
                    f["id"], size_bytes=len(data),
                    note="缩略图追回(原图404)")
                f["status"] = runtime_db.PAW_FILE_DONE
                st["done"] += 1
            # 帖内全 DONE → 翻 COMPLETED（ARCHIVED/FAILED 均可翻）
            touched = {f["post_row"] for f in batch
                       if f["post_row"] and data_map.get(f["id"])}
            for pr in touched:
                try:
                    if runtime_db.complete_pawchive_post_if_all_done(pr):
                        logger.info(f"🖼 缩略图追回补齐整帖 #{pr} → COMPLETED")
                except runtime_db.DbUnavailable as e:
                    logger.error(f"🖼 帖子完成翻转失败（忽略）：{e}")
            await asyncio.sleep(1)   # 批间节流
        done_msg = (f"🖼 Pawchive 缩略图追回完成：共追回 {st['done']} 张"
                    f"（低分辨率替代）")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"🖼 缩略图追回循环异常终止：{e}")
        done_msg = f"🖼 Pawchive 缩略图追回异常停止（已追回 {st['done']}）：{e}"
    finally:
        st["running"] = False
    try:
        await notify.notify_user(done_msg)
    except Exception:
        pass


def thumbnail_recovery_toggle(on):
    """开/关缩略图追回。返回 (running, 提示)。"""
    st = _THUMB_RECOVER
    if on:
        if st["running"]:
            return True, "已在运行中"
        start_thumbnail_recovery()
        return True, "已启动（后台分批追回，完成/停止时通知）"
    if st["running"]:
        st["stop"] = True
        return False, "已请求停止（当前批次完成后退出）"
    return False, "本就未在运行"


def start_thumbnail_recovery():
    """挂缩略图追回后台任务（强引用）。"""
    t = asyncio.create_task(thumbnail_recovery_loop())
    _TASKS.add(t)
    t.add_done_callback(_TASKS.discard)
    return t


async def _renew_lease_loop(post_row):
    """单帖下载期间每 PAWCHIVE_LEASE_RENEW_SECONDS 续一次租。"""
    try:
        while True:
            await asyncio.sleep(float(config.PAWCHIVE_LEASE_RENEW_SECONDS))
            try:
                runtime_db.renew_pawchive_lease(post_row)
            except runtime_db.DbUnavailable as e:
                logger.error(f"🐾 续租失败（不影响本帖处理）：{e}")
    except asyncio.CancelledError:
        raise


async def process_post(post):
    """处理一条帖子（领到 PROCESSING 之后的全过程）。异常向上抛给 run_once。"""
    label = f"{post['creator_name']} #{post['id']}"
    logger.info(f"🐾 开始处理帖子：{label}｜{(post.get('title') or '')[:50]}")

    # 1) 磁盘保护：低于保护线暂停整个 worker（继续下只会撑爆盘）
    free = _disk_free_gb()
    if free is not None and free < config.PAWCHIVE_MIN_FREE_GB:
        pause(f"磁盘剩余 {free:.1f} GB，低于保护线 "
              f"{config.PAWCHIVE_MIN_FREE_GB:.0f} GB")
        release_inflight(post["id"])
        await notify.notify_user(
            f"⚠️ Pawchive 已暂停：下载盘仅剩 {free:.1f} GB"
            f"（保护线 {config.PAWCHIVE_MIN_FREE_GB:.0f} GB）。\n"
            "清理空间后 /paw resume 继续。")
        return False

    files = runtime_db.list_pawchive_files(post["id"])

    # 开始通知（2026-09-25 用户要求与转发链路一致）；/paw notify off 可关
    # 2026-09-28 用户要求完善：带发布日期与原帖链接（可点）
    from . import pawchive   # 函数内导入：pawchive → worker 已有环，避免模块级
    if pawchive.notify_each_post_enabled() and files:
        try:
            start_url = (post.get("post_url") + "\n"
                         if post.get("post_url") else "")
            await notify.notify_user(
                f"🐾 开始下载：{post['creator_name']}｜"
                f"{(post.get('title') or '')[:40]}\n"
                f"发布：{(post.get('published') or '')[:10]}｜"
                f"附件 {len(files)} 个\n"
                + start_url
                + "（/paw status 看进度）")
        except Exception as e:
            logger.warning(f"开始通知发送失败（忽略）：{e}")

    if not files:
        # 没有直链（纯外链帖）：直接按外链判定终态
        await _finalize(post, files)
        return True

    # 2) 旧 Chrome 时代的 SUBMITTED 放回 PENDING（执行体切换迁移）
    _requeue_legacy_submitted(files)

    # 3) 死链预检（HTTP 在线程池跑，DB 写留在本线程）
    if config.PAWCHIVE_PRECHECK_HEAD:
        targets = [(f, f["url"]) for f in files
                   if f["status"] == runtime_db.PAW_FILE_PENDING]
        dead_ids, repaired = await asyncio.to_thread(_head_dead_ids, targets)
        for f in files:
            if f["status"] != runtime_db.PAW_FILE_PENDING:
                continue
            if f["id"] in repaired:
                # 老数据无扩展名 URL 自愈：回写 DB + 内存，照常下载
                runtime_db.update_pawchive_file_url(f["id"], repaired[f["id"]])
                f["url"] = repaired[f["id"]]
            elif f["id"] in dead_ids:
                _mark_dead(f)
                logger.warning(f"🐾 死链预检跳过：{f['filename']}")
        # 缩略图替代（2026-09-28 用户要求）：原文件 404 时抓对应缩略图存档——
        # 站点删原图后缩略图常残留（实测 200），有总比没有强
        dead_files = [f for f in files
                      if f["status"] == runtime_db.PAW_FILE_FAILED
                      and (f.get("error") or "").startswith(_MISSING_MARK)]
        if dead_files:
            data_map = await asyncio.to_thread(
                _fetch_thumbnails,
                [(f, f["url"]) for f in dead_files])
            for f in dead_files:
                data = data_map.get(f["id"])
                if not data:
                    continue
                target = _target_path(post, f["filename"])
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as fh:
                    fh.write(data)
                runtime_db.mark_pawchive_file_done(
                    f["id"], size_bytes=len(data),
                    note=f"缩略图替代(原图404) {len(data)}B")
                f["status"] = runtime_db.PAW_FILE_DONE
                logger.info(
                    f"🖼 缩略图替代完成：{f['filename']}（{len(data)}B）")

    # 4) 并发下载 + 周期续租（单帖可能跑很久）
    renew = asyncio.create_task(_renew_lease_loop(post["id"]))
    try:
        await _download_post_files(post, files)
    finally:
        renew.cancel()
        try:
            await renew
        except asyncio.CancelledError:
            pass

    # 5) 终态流转
    await _finalize(post, files)
    return True


async def run_once():
    """跑一轮：恢复过期租约 + 领一条帖子处理。返回本轮是否领到了帖子。"""
    recover_expired()
    post = claim_next_post()
    if post is None:
        return False
    try:
        await process_post(post)
        _INFLIGHT.discard(post["id"])   # 成功路径也清在途标记（旧实现泄漏）
        return True
    except asyncio.CancelledError:
        # 停服：把手上的帖子放回待处理（文件级状态保证不重复下载）
        release_inflight(post["id"])
        raise
    except Exception as e:
        _INFLIGHT.discard(post["id"])
        logger.exception(f"🐾 帖子 #{post.get('id')} 执行时未预期异常：{e}")
        try:
            runtime_db.postpone_pawchive_post(
                post["id"],
                int(time.time()) + int(config.PAWCHIVE_AGENT_RETRY_SECONDS),
                error=f"未预期异常：{type(e).__name__}: {e}")
        except runtime_db.DbUnavailable as db_err:
            logger.error(f"🐾 帖子转退避失败（租约到期后会恢复）：{db_err}")
    finally:
        _INFLIGHT.discard(post["id"])
        return False


async def _spawn_post(post):
    """单帖处理任务：成功/失败/取消都清在途标记；异常转退避不留死角。

    2026-09-18 事故：成功路径漏 discard——4 帖完成后 _INFLIGHT 仍持有
    4 个 id，worker_loop 的 len(_INFLIGHT) < 4 永不成立，1019 PENDING
    饿死。改为 finally 统一清理（取消/异常分支各自的额外语义保留）。"""
    try:
        await process_post(post)
    except asyncio.CancelledError:
        release_inflight(post["id"])
        raise
    except Exception as e:
        _INFLIGHT.discard(post["id"])
        logger.exception(f"🐾 帖子 #{post.get('id')} 执行时未预期异常：{e}")
        try:
            runtime_db.postpone_pawchive_post(
                post["id"],
                int(time.time()) + int(config.PAWCHIVE_AGENT_RETRY_SECONDS),
                error=f"未预期异常：{type(e).__name__}: {e}")
        except runtime_db.DbUnavailable as db_err:
            logger.error(f"🐾 帖子转退避失败（租约到期后会恢复）：{db_err}")
    finally:
        # 成功/失败/取消统一清槽（R-泄漏事故：成功路径漏清 → 4 槽死锁）
        _INFLIGHT.discard(post["id"])


async def worker_loop():
    """常驻循环：**多帖并发**——在途帖数 < PAWCHIVE_MAX_INFLIGHT_POSTS 时
    持续领取并 spawn；帖内文件并发由共享 DOWNLOAD_SEMAPHORE 总控。

    单帖串行是 2026-09-16「一帖几小时」抱怨的根因之一：几百个文件的帖子
    逐个爬，并发池吃不满。多帖在途让信号量始终有多路流在跑。
    """
    logger.info(
        f"🐾 Pawchive worker 已启动（内置并发下载器，多帖并发 ≤"
        f"{config.PAWCHIVE_MAX_INFLIGHT_POSTS}，轮询 "
        f"{config.PAWCHIVE_WORKER_POLL_SECONDS}s，租约 "
        f"{config.PAWCHIVE_LEASE_SECONDS}s，磁盘保护线 "
        f"{config.PAWCHIVE_MIN_FREE_GB}GB，与 /thread 共享并发池）")
    poll = float(config.PAWCHIVE_WORKER_POLL_SECONDS)
    try:
        while True:
            try:
                recover_expired()
                await _auto_requeue_429()
                spawned = 0
                while len(_INFLIGHT) < int(
                        config.PAWCHIVE_MAX_INFLIGHT_POSTS):
                    post = claim_next_post()
                    if post is None:
                        break
                    t = asyncio.create_task(_spawn_post(post))
                    _TASKS.add(t)
                    t.add_done_callback(_TASKS.discard)
                    spawned += 1
                did = spawned > 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"🐾 Pawchive worker 主循环异常（继续运行）：{e}")
                did = False
            if not did:
                await asyncio.sleep(poll)
    except asyncio.CancelledError:
        logger.info("🐾 Pawchive worker 正在停止…")
        release_inflight()
        raise


def start_worker():
    """把 Worker 循环与进度面板挂成后台任务并保持强引用，返回 worker 任务。"""
    task = asyncio.create_task(worker_loop())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    panel = asyncio.create_task(_panel_loop())
    _TASKS.add(panel)
    panel.add_done_callback(_TASKS.discard)
    return task
