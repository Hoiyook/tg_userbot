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
import urllib.request

import httpx

from . import config
from . import notify
from . import runtime_db
from . import state
from .log import logger

# 强制直连：系统代理（socks5）会让 urllib 秒抛 ValueError（2026-09-15 实测）
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 站点侧死链的失败标记（finalize 用它区分「不可行动的死链」与「值得重试的失败」）
_MISSING_MARK = "站点缺文件(404)"
_DEAD_STATUS = (404, 410)
# 单文件最大尝试次数与退避基数（秒）——对应原 Chrome 端的 3 次重试语义
_DOWNLOAD_ATTEMPTS = 3

# 正在处理的帖子行 id 集合；停机时全部放回 PENDING。
_INFLIGHT = set()
# 后台任务强引用（asyncio 只对 Task 持弱引用）
_TASKS = set()

# 人工暂停开关（/paw pause）：磁盘保护也走它。
_PAUSED = False
_PAUSE_REASON = ""


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


def current_post_label():
    """在途帖子的展示标签；无 / DB 不可用返回 None。"""
    if not _INFLIGHT:
        return None
    try:
        rows = [runtime_db.get_pawchive_post(r) for r in sorted(_INFLIGHT)]
    except runtime_db.DbUnavailable:
        return None
    rows = [r for r in rows if r]
    if not rows:
        return None
    return "；".join(
        f"#{r['id']} {r['creator_name']} {(r['title'] or '')[:24]}"
        for r in rows[:3])


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


def _head_dead_ids(targets):
    """并发 HEAD 一批 (file_row, url)，返回死链（404/410）的 file id 集合。

    纯 HTTP，在 to_thread 里跑；**绝不碰 runtime_db**（DB 连接属主线程，
    sqlite3 的 check_same_thread 会拒绝跨线程使用）。
    """
    import concurrent.futures

    if not targets:
        return set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(ex.map(lambda t: _head_status(t[1]), targets))
    summary = collections.Counter(str(code) for code in codes)
    logger.info(f"🐾 死链预检：{len(targets)} 个直链 → {dict(summary)}")
    return {t[0]["id"] for t, code in zip(targets, codes)
            if code in _DEAD_STATUS}


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
    """落盘绝对路径：DOWNLOAD_DIR/<subdir=「Pawchive/作者/帖子」>/<文件名>。"""
    from .naming import sanitize_filename
    name = sanitize_filename(filename or "untitled")
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
    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        offset = os.path.getsize(part) if os.path.isfile(part) else 0
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
            last_err = f"HTTP {e.response.status_code}"
            if e.response.status_code in _DEAD_STATUS:
                return ("dead", None, last_err)
        except Exception as e:   # noqa: BLE001 —— 网络类错误统一重试
            last_err = f"{type(e).__name__}: {e}"
        logger.warning(
            f"🐾 文件第 {attempt} 次尝试失败：{f['filename']}（{last_err}）"
            + ("，退避后 .part 续传重试" if attempt < _DOWNLOAD_ATTEMPTS else ""))
        time.sleep(min(30, 5 * attempt))
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
                logger.info(
                    f"🐾 文件完成：{f['filename']}（{size or '?'} bytes）")
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
        # 全部文件都是站点死链（一个都没下成）→ 不可行动（重试也一样 404），
        # 只落库不刷屏；只要还有下载成功的内容就通知，让用户知道丢了哪些
        all_missing = (
            not any(f["status"] == runtime_db.PAW_FILE_DONE for f in files)
            and all((f.get("error") or "").startswith(_MISSING_MARK)
                    for f in failed))
        if all_missing:
            err = (f"{len(failed)}/{len(files)} 个文件是站点死链"
                   f"（{_MISSING_MARK}）")
            runtime_db.finalize_pawchive_post(
                post["id"], runtime_db.PAW_POST_FAILED, error=err)
            logger.warning(f"🐾 帖子全部为站点死链，标失败：{label}｜{title}")
            return
        err = f"{len(failed)}/{len(files)} 个文件下载失败"
        runtime_db.finalize_pawchive_post(
            post["id"], runtime_db.PAW_POST_FAILED, error=err)
        lines = [f"❌ Pawchive 帖子失败：{label}｜{title}",
                 post.get("post_url") or "",
                 f"{err}（/paw retry {post['id']} 重投）"]
        for f in failed[:5]:
            lines.append(f"  · {f['filename']}：{(f.get('error') or '')[:80]}")
        await notify.notify_user("\n".join(lines))
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
        return
    runtime_db.finalize_pawchive_post(post["id"], runtime_db.PAW_POST_COMPLETED)
    logger.info(f"🐾 帖子完成：{label}｜{title}（{len(files)} 个文件）")


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
        dead_ids = await asyncio.to_thread(_head_dead_ids, targets)
        for f in files:
            if f["status"] == runtime_db.PAW_FILE_PENDING \
                    and f["id"] in dead_ids:
                _mark_dead(f)
                logger.warning(f"🐾 死链预检跳过：{f['filename']}")

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
        return True
    except asyncio.CancelledError:
        # 停服：把手上的帖子放回待处理（文件级状态保证不重复下载）
        release_inflight(post["id"])
        raise
    except Exception as e:
        logger.exception(f"🐾 帖子 #{post.get('id')} 执行时未预期异常：{e}")
        try:
            runtime_db.postpone_pawchive_post(
                post["id"],
                int(time.time()) + int(config.PAWCHIVE_AGENT_RETRY_SECONDS),
                error=f"未预期异常：{type(e).__name__}: {e}")
        except runtime_db.DbUnavailable as db_err:
            logger.error(f"🐾 帖子转退避失败（租约到期后会恢复）：{db_err}")
        return False


async def worker_loop():
    """常驻循环：有活就干，没活就按轮询间隔歇一会儿。"""
    logger.info(
        f"🐾 Pawchive worker 已启动（内置并发下载器，轮询 "
        f"{config.PAWCHIVE_WORKER_POLL_SECONDS}s，租约 "
        f"{config.PAWCHIVE_LEASE_SECONDS}s，磁盘保护线 "
        f"{config.PAWCHIVE_MIN_FREE_GB}GB，与 /thread 共享并发池）")
    poll = float(config.PAWCHIVE_WORKER_POLL_SECONDS)
    try:
        while True:
            try:
                did = await run_once()
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
    """把 Worker 循环挂成后台任务并保持强引用，返回该任务。"""
    task = asyncio.create_task(worker_loop())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task
