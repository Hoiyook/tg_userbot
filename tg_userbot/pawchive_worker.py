"""Pawchive Worker —— 生命周期消费：领取帖子 → Chrome Agent 下载直链 → 终态。

**扫描 ≠ 执行**的另一半（与 listener_worker 同构，但执行体是 Chrome Agent
而不是 Telegram 转发）：

    claim（短事务落 PROCESSING + 租约）
      ↓  ← 事务到此结束，Chrome/文件操作一律在事务外
    对账 SUBMITTED 文件的 chrome task（终态就地吸收；失联才重投）
      ↓
    死链预检（HEAD）：404/410 的直链直接标 FAILED——死链没有下载事件，
    进 Chrome 只会白烧 CHROME_DOWNLOAD_TIMEOUT×3（实测某创作者 88% 死链）
      ↓
    PENDING 文件逐条 add_request 交给 Chrome Agent（FIFO 串行下载）
      ↓
    轮询 chrome_tasks.json 等本帖全部终态（期间周期续租——大视频单帖可达数小时）
      ↓
    全 DONE 且无外链 → COMPLETED；有外链 → MANUAL（通知外链清单）；
    有 FAILED → FAILED（/paw retry 重投；全部是站点死链则只落库不通知）

**为什么经 Chrome Agent 而不是下载队列**：用户指定用 Chrome 模块——直链挂到
专用 Profile 的真实浏览器上下载，自带重试（3 次）、任务面板（/chrome_tasks）
与取消（/chrome_cancel），且 pawchive 直链无需 Cookie，与 Agent 独立 profile
天然兼容。代价是 Agent 串行 FIFO：大批量帖子注定是长流水，状态全在 SQLite 可查。

**通知粒度**：per-file 的结果通知由 chrome_client.notify_loop 发（提交时即
mark_notified 屏蔽掉——1620 个文件就是 1620 条刷屏）；本 worker 只在帖子级
终态通知：MANUAL / FAILED 必通知，COMPLETED 只记日志（/paw status 可查）。

**幂等性**：at-least-once。COMPLETED 写入前崩溃 → 租约到期 → 重领 → 文件级
状态（DONE/SUBMITTED）保证已下载的不再重下；SUBMITTED 且 task 失联（Agent
端终态任务被 trim）才重投，极端情况下可能多下一次，可接受。
"""
import asyncio
import collections
import shutil
import time
import urllib.request

from . import chrome_agent
from . import chrome_client
from . import config
from . import notify
from . import runtime_db
from . import state
from .log import logger

# 正在处理的帖子行 id（单并发）；停机时放回 PENDING。
_INFLIGHT = None
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
    return f"⏸ 已暂停处理（{_PAUSE_REASON}）。在途帖子会跑完当前状态，不再领新帖。/paw resume 恢复"


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
    """在途帖子的展示标签；没有在途 / DB 不可用返回 None。"""
    if _INFLIGHT is None:
        return None
    try:
        post = runtime_db.get_pawchive_post(_INFLIGHT)
    except runtime_db.DbUnavailable:
        return None
    if post is None:
        return None
    return (f"#{post['id']} {post['creator_name']} "
            f"{(post['title'] or '')[:36]}")


def _disk_free_gb():
    try:
        return shutil.disk_usage(config.CHROME_DOWNLOAD_DIR).free / 1024 ** 3
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
        global _INFLIGHT
        _INFLIGHT = post["id"]
    return post


def release_inflight(post_row=None):
    """把手上的帖子放回 PENDING（优雅停机，不等租约到期）。"""
    global _INFLIGHT
    row = post_row if post_row is not None else _INFLIGHT
    if row is None:
        return False
    try:
        ok = runtime_db.release_pawchive_post(row)
    except runtime_db.DbUnavailable as e:
        logger.error(f"🐾 停机释放帖子失败（租约到期后会自动恢复）：{e}")
        ok = False
    if _INFLIGHT == row:
        _INFLIGHT = None
    return ok


def recover_expired():
    """启动时恢复过期租约。DB 不可用只记日志。"""
    try:
        return runtime_db.recover_expired_pawchive_posts()
    except runtime_db.DbUnavailable as e:
        logger.error(f"🐾 恢复过期租约帖子失败（不影响启动）：{e}")
        return 0


# ============================================================
# 执行：提交 / 对账 / 等待终态
# ============================================================
def _ensure_agent():
    """确保 Chrome Agent 进程在跑；没有就拉起（/chrome_start 同款）。"""
    if chrome_client.agent_running():
        return True, None
    try:
        chrome_client.spawn_agent()
        logger.info("🐾 Pawchive worker 已自动拉起 Chrome Agent")
        return True, None
    except Exception as e:
        return False, f"Chrome Agent 启动失败：{type(e).__name__}: {e}"


def _absorb_terminal(file_row, task):
    """把 Chrome 侧终态落到文件行：SUCCESS→DONE，FAILED/CANCELLED→FAILED。"""
    if task.get("status") == "SUCCESS":
        runtime_db.mark_pawchive_file_done(file_row["id"],
                                           size_bytes=task.get("size_bytes"))
        file_row["status"] = runtime_db.PAW_FILE_DONE
        logger.info(
            f"🐾 文件完成：{file_row['filename']}"
            f"（{task.get('size_bytes') or '?'} bytes）")
    else:
        err = task.get("error") or task.get("status") or "unknown"
        runtime_db.mark_pawchive_file_failed(file_row["id"], error=err)
        file_row["status"] = runtime_db.PAW_FILE_FAILED
        logger.warning(
            f"🐾 文件失败：{file_row['filename']}（{err}）")


# 站点侧死链的失败标记（finalize 用它区分「不可行动的死链」与「值得重试的失败」）
_MISSING_MARK = "站点缺文件(404)"
_DEAD_STATUS = (404, 410)


# 强制直连的 opener：macOS 的 urllib 会读系统代理（Hiddify 常配 socks5，
# urllib 不支持 socks5 直接 ValueError 秒败——2026-09-15 实测）。pawchive
# 的文件/接口直连可达，绕开一切环境/系统代理，行为才可预测。
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _head_status(url, timeout=15):
    """HEAD 探测直链，返回 HTTP 状态码；网络异常返回 None（不预判）。"""
    import urllib.error
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "Mozilla/5.0 tg-userbot-pawchive")
        with _DIRECT_OPENER.open(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        # 异常不判死（临时网络抖动交给 Chrome），但类型要可见：
        # 代理环境变量泄漏进 urllib 时是秒败，得靠这里现形
        return "ERR:" + repr(e)[:100]


def _reconcile_submitted(post, files):
    """对账 SUBMITTED 文件：终态就地吸收；task 失联（被 trim/未认领）→
    重投 PENDING。无网络调用，同步小文件 IO。

    返回 Agent 队列里**还没开下**的 (file_row, task_id) 列表——调用方对它们
    做死链预检（RUNNING/RETRY_WAIT 的已经在跑，不动）。
    """
    tasks_json = chrome_agent.load_tasks(config.CHROME_TASKS_FILE)
    queued = []
    for f in files:
        if f["status"] != runtime_db.PAW_FILE_SUBMITTED:
            continue
        task = chrome_agent.get_task(tasks_json, f.get("chrome_task_id"))
        if task is not None:
            if task.get("status") in chrome_agent.TERMINAL_STATUSES:
                _absorb_terminal(f, task)
            elif task.get("status") == "PENDING":
                queued.append((f, f["chrome_task_id"]))
            # 其余（RUNNING/RETRY_WAIT）：继续等轮询
            continue
        runtime_db.mark_pawchive_file_pending(f["id"])
        f["status"] = runtime_db.PAW_FILE_PENDING
        logger.warning(f"🐾 文件 {f['filename']} 的 chrome task 失联，已重投")
    return queued


def _head_dead_ids(targets):
    """并发 HEAD 一批 (file_row, url)，返回死链（404/410）的 file id 集合。

    纯 HTTP，在 to_thread 里跑；**绝不碰 runtime_db**（DB 连接属主线程，
    sqlite3 的 check_same_thread 会拒绝跨线程使用）。网络异常不预判——
    交给 Chrome 正常走（临时网络抖动不该把文件判死）。
    """
    import concurrent.futures

    if not targets:
        return set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(ex.map(lambda t: _head_status(t[1]), targets))
    summary = collections.Counter(
        str(code) for code in codes)
    logger.info(f"🐾 死链预检：{len(targets)} 个直链 → {dict(summary)}")
    return {t[0]["id"] for t, code in zip(targets, codes)
            if code in _DEAD_STATUS}


def _mark_dead(file_row, code=404):
    """死链就地标 FAILED（只在事件循环线程调用——DB 连接属主线程）。"""
    runtime_db.mark_pawchive_file_failed(
        file_row["id"], error=f"{_MISSING_MARK} HTTP {code}")
    file_row["status"] = runtime_db.PAW_FILE_FAILED
    file_row["error"] = _MISSING_MARK


def _submit_pending(post, files):
    """把 PENDING 文件逐条提交给 Chrome Agent。返回提交条数。"""
    submitted = 0
    for f in files:
        if f["status"] != runtime_db.PAW_FILE_PENDING:
            continue
        task_id = chrome_agent.new_task_id()
        chrome_client.add_request(
            task_id, f["url"], user_id=state.MY_ID, chat_id=state.MY_ID,
            message_id=0, download_subdir=post.get("subdir"))
        # 屏蔽 chrome_client.notify_loop 的逐文件通知：结果由本 worker 按
        # 帖子级汇总（见模块 docstring 的通知粒度说明）
        chrome_client.mark_notified(task_id)
        runtime_db.mark_pawchive_file_submitted(f["id"], task_id)
        f["status"] = runtime_db.PAW_FILE_SUBMITTED
        f["chrome_task_id"] = task_id
        submitted += 1
        logger.info(
            f"🐾 提交下载 [{task_id[:8]}] {f['filename']} → "
            f"{post.get('subdir') or '(根目录)'}")
    return submitted


async def _wait_files_terminal(post, files):
    """轮询 chrome_tasks.json 直到本帖全部文件到终态；期间周期续租。"""
    poll = float(config.PAWCHIVE_CHROME_POLL_SECONDS)
    renew_every = float(config.PAWCHIVE_LEASE_RENEW_SECONDS)
    last_renew = time.monotonic()
    while True:
        waiting = [f for f in files
                   if f["status"] == runtime_db.PAW_FILE_SUBMITTED]
        if not waiting:
            return
        if time.monotonic() - last_renew >= renew_every:
            try:
                runtime_db.renew_pawchive_lease(post["id"])
            except runtime_db.DbUnavailable as e:
                logger.error(f"🐾 续租失败（不影响本帖处理）：{e}")
            last_renew = time.monotonic()
        tasks_json = await asyncio.to_thread(
            chrome_agent.load_tasks, config.CHROME_TASKS_FILE)
        for f in waiting:
            if f["status"] != runtime_db.PAW_FILE_SUBMITTED:
                continue
            task = chrome_agent.get_task(tasks_json, f.get("chrome_task_id"))
            if task is not None and task.get("status") in chrome_agent.TERMINAL_STATUSES:
                _absorb_terminal(f, task)
        if all(f["status"] in (runtime_db.PAW_FILE_DONE,
                               runtime_db.PAW_FILE_FAILED) for f in files):
            return
        await asyncio.sleep(poll)


def _notify_target():
    return state.MY_ID


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
            # 全部是站点死链：不可行动（重试也一样 404），只落库不刷屏
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
                 post.get("post_url") or "", f"{err}（/paw retry {post['id']} 重投）"]
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

    # 2) Chrome Agent 在跑（没跑就拉起；起不来按暂时性失败退避）
    ok, err = _ensure_agent()
    if ok:
        ok = await chrome_client.wait_agent_up(30)
        err = None if ok else "Chrome Agent 拉起后 30s 内未就绪"
    if not ok:
        delay = int(config.PAWCHIVE_AGENT_RETRY_SECONDS)
        runtime_db.postpone_pawchive_post(
            post["id"], int(time.time()) + delay, error=err)
        logger.warning(f"🐾 {err}，帖子 {label} {delay}s 后重试")
        await notify.notify_user(f"⚠️ Pawchive：{err}，稍后自动重试")
        return False

    # 3) 对账已提交 → 死链预检 → 提交存活文件
    files = runtime_db.list_pawchive_files(post["id"])
    if not files:
        # 没有直链（纯外链帖）：直接按外链判定终态
        await _finalize(post, files)
        return True
    queued = _reconcile_submitted(post, files)
    if config.PAWCHIVE_PRECHECK_HEAD:
        # 死链预检：PENDING + Agent 队列里未开下的 SUBMITTED 都查一遍。
        # HTTP 在线程池跑；DB 写与取消请求留在本线程（DB 连接属主线程）。
        # queued 的第二元是 chrome task_id 不是 URL——预检目标必须重新取 f["url"]
        targets = [(f, f["url"]) for f in files
                   if f["status"] == runtime_db.PAW_FILE_PENDING]
        targets += [(f, f["url"]) for f, _tid in queued]
        dead_ids = await asyncio.to_thread(_head_dead_ids, targets)
        for f, tid in queued:
            if f["id"] in dead_ids:
                ok, _ = chrome_client.request_cancel(tid)
                if ok:
                    _mark_dead(f)
                    logger.warning(f"🐾 排队中的死链已取消：{f['filename']}")
        for f in files:
            if f["status"] == runtime_db.PAW_FILE_PENDING \
                    and f["id"] in dead_ids:
                _mark_dead(f)
                logger.warning(f"🐾 死链预检跳过：{f['filename']}")
    _submit_pending(post, files)

    # 4) 等本帖全部文件终态（期间续租），5) 终态流转
    await _wait_files_terminal(post, files)
    await _finalize(post, files)
    return True


# ============================================================
# 主循环（克隆 listener_worker 的纪律）
# ============================================================
async def run_once():
    """跑一轮：恢复过期租约 + 领一条帖子处理。返回本轮是否真的干了活。"""
    recover_expired()
    post = claim_next_post()
    if post is None:
        return False
    try:
        return await process_post(post)
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
        f"🐾 Pawchive worker 已启动（轮询 "
        f"{config.PAWCHIVE_WORKER_POLL_SECONDS}s，租约 "
        f"{config.PAWCHIVE_LEASE_SECONDS}s，Chrome 轮询 "
        f"{config.PAWCHIVE_CHROME_POLL_SECONDS}s，磁盘保护线 "
        f"{config.PAWCHIVE_MIN_FREE_GB}GB）")
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
