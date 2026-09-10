"""Chrome Agent V1 核心：CDP 驱动专用 Chrome 实例完成远程下载。

与 User Bot 的分工（规格 17/24，本地 JSON 文件通道——不用 Telegram 会话，
避免独立 session 首次人工登录破坏「全自动」原则）：
  * runtime/chrome_requests.json —— User Bot 独占写（任务请求 + 用户映射）；
  * runtime/chrome_tasks.json   —— Agent 独占写（任务状态机事实源）；
  * 双方 temp + os.replace 原子写、只读对方文件，无同写冲突。

Chrome 采用专用 Profile（~/tg_chrome_agent_profile，2026-09-09 用户允许）：
Chrome ≥136 禁止默认 Profile 开 CDP，专用实例是唯一合法形态；它由 Agent
独占拉起/复用，与用户正常 Chrome 并行，绝不触碰后者的 Profile/Cookie。
下载经 CDP 事件判定（downloadWillBegin/downloadProgress），不靠 sleep 猜。
"""
import asyncio
import json
import os
import signal
import subprocess
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse

from .config import (
    CHROME_AGENT_LOG_FILE,
    CHROME_AGENT_PID_FILE,
    CHROME_CANCEL_GUID_GRACE_SECONDS,
    CHROME_CANCEL_REQUESTS_FILE,
    CHROME_CDP_CONNECT_TIMEOUT,
    CHROME_CDP_HOST,
    CHROME_CDP_PORT,
    CHROME_DOWNLOAD_DIR,
    CHROME_DOWNLOAD_RETRIES,
    CHROME_DOWNLOAD_TIMEOUT,
    CHROME_POLL_SECONDS,
    CHROME_PROFILE_DIR,
    CHROME_PROXY_SERVER,
    CHROME_REQUESTS_FILE,
    CHROME_RETRY_WAIT_SECONDS,
    CHROME_TASKS_FILE,
    CHROME_TASKS_KEEP_TERMINAL,
    LOG_RETENTION_DAYS,
)

# 任务取消（任务书 §3）：CANCELLED 是终态，任务记录**保留**（绝不 remove）、
# attempts 不因取消增加、claim_next 永不认领。
CANCEL_REASON = "用户主动取消"
TERMINAL_STATUSES = ("SUCCESS", "FAILED", "CANCELLED")
# Agent 读取消请求文件的轮询间隔。文件 IPC 只能轮询（规格 §9 禁止引入
# socket/队列服务）；这是唯一的轮询点，下载本身仍是 CDP 事件驱动。
CANCEL_POLL_SECONDS = 1.0
# 取消请求到达时 guid 还没产生的宽限窗口（§11）：继续等 downloadWillBegin，
# 等到就按 guid 真正取消；等不到说明下载还没开始，由收尾的 close_tab 兜底。
# 取值见 config.CHROME_CANCEL_GUID_GRACE_SECONDS。
CANCEL_GUID_GRACE_SECONDS = CHROME_CANCEL_GUID_GRACE_SECONDS
# 「取消先到」的内部哨兵（与 None=无事件区分开）
_CANCEL_SENTINEL = object()
# 启动清扫孤儿 .crdownload 的最小「没人动过」时长（秒）。见 sweep_orphan_partials：
# 刚写过的半成品可能是上一个 Agent 进程（Chrome 尚未退出）仍在写的，不能删。
ORPHAN_PARTIAL_MIN_AGE_SECONDS = 600.0
from .log import configure as configure_log
from .log import logger
from .naming import sanitize_filename, unique_path

# Chrome 可执行文件候选（规格 5：默认路径 + 自动检测）
_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


# ------------------------------------------------------------
# 基础纯函数
# ------------------------------------------------------------

def find_chrome_binary():
    """探测 Chrome 可执行文件；找不到返回 None（启动时如实报告）。"""
    for path in _CHROME_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def validate_chrome_url(url):
    """URL 验证（规格 16）：只收 http/https 且有主机；其余一律拒绝。"""
    if not url or not isinstance(url, str):
        return False
    try:
        parts = urlparse(url.strip())
    except Exception:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def safe_subdir(subdir):
    """校验并规范化下载子目录，非法返回 None（调用方退回根目录）。

    子目录直接来自用户在 Telegram 里敲的命令，必须挡住越界写法——
    `A/../../..` 或绝对路径会让文件落到 TG Chrome Download 之外。只做
    底线校验（拒绝上跳 / 当前目录 / 反斜杠，去掉首尾与重复的 "/"），
    不引入任何新语法。合法时返回用 "/" 分隔的相对路径。
    """
    if not subdir:
        return None
    text = str(subdir).strip().strip("/")
    if not text:
        return None
    parts = []
    for part in text.split("/"):
        part = part.strip()
        if not part:
            continue
        if part in (".", ".."):
            return None
        if "\\" in part:
            return None
        parts.append(part)
    return "/".join(parts) if parts else None


def new_task_id():
    """任务唯一 id（uuid hex，与队列记录同款；展示取前 8 位）。"""
    return uuid.uuid4().hex


def create_task(url, task_id, now=None, download_subdir=None):
    """新建任务记录（规格 25 的完整字段，PENDING 起点）。

    download_subdir：可选的下载子目录（`/chrome A/B/#标注 URL`）。只在非空
    时写入——没有子目录的任务保持原有数据结构不变。
    """
    ts = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    task = {
        "task_id": task_id,
        "url": url,
        "status": "PENDING",
        "attempts": 0,
        "created_at": ts,
        "updated_at": ts,
        "started_at": None,
        "finished_at": None,
        "filename": None,
        "size_bytes": None,
        "error": None,
        "guid": None,
    }
    subdir = safe_subdir(download_subdir)
    if subdir:
        task["download_subdir"] = subdir
    return task


def get_task_download_dir(root_dir, task):
    """任务的实际下载目录 = 根目录 + task 的 download_subdir（无则根目录）。

    这里是越界的最后一道闸：即便 task 里的 download_subdir 是历史脏数据，
    非法值也只会退回根目录，绝不会把文件写出 root_dir 之外。
    """
    subdir = safe_subdir((task or {}).get("download_subdir"))
    if not subdir:
        return root_dir
    return os.path.join(root_dir, *subdir.split("/"))


# ------------------------------------------------------------
# 任务持久化（chrome_tasks.json，Agent 独占写；规格 24/25）
# ------------------------------------------------------------

def load_tasks(path):
    """读任务列表；缺失/损坏/形状不对一律回空列表（任务事实以文件为准，
    坏文件宁可从空开始也不能带崩 Agent）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning(f"读取 Chrome 任务文件失败，从空开始：{e}")
        return []
    if not isinstance(data, dict):
        return []
    tasks = data.get("tasks") or []
    if not isinstance(tasks, list):
        return []
    return [t for t in tasks if isinstance(t, dict) and t.get("task_id")]


def save_tasks(tasks, path):
    """原子写任务列表（temp + os.replace；失败仅告警）。"""
    try:
        temp_path = path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump({"tasks": tasks}, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    except Exception as e:
        logger.warning(f"保存 Chrome 任务文件失败：{e}")


def get_task(tasks, task_id):
    return next((t for t in tasks if t.get("task_id") == task_id), None)


def trim_terminal_tasks(tasks, keep):
    """只保留最近的 keep 条终态任务（就地裁剪），返回被裁掉的记录。

    终态任务（SUCCESS / FAILED / CANCELLED）只用于展示与统计，但会一直堆在
    chrome_tasks.json 里，而 chrome_client 的 notify_loop 每 5s 全表扫一遍、
    /chrome_status 也全表算。这里只裁**最老的**——刚终结的必须留着：通知是
    轮询发的，裁太早用户就收不到那条结果了。

    非终态任务一条都不动（它们还要被认领/重试）。列表是原地改的，调用方的
    引用与文件顺序都保持不变。
    """
    terminal_idx = [i for i, t in enumerate(tasks)
                    if t.get("status") in TERMINAL_STATUSES]
    excess = len(terminal_idx) - int(keep)
    if excess <= 0:
        return []
    drop = set(terminal_idx[:excess])
    removed = [t for i, t in enumerate(tasks) if i in drop]
    tasks[:] = [t for i, t in enumerate(tasks) if i not in drop]
    logger.info(f"🧹 清理历史终态任务 {len(removed)} 条（保留最近 {keep} 条）")
    return removed


# ------------------------------------------------------------
# 状态机（规格 26）：PENDING → RUNNING → SUCCESS / RETRY_WAIT → … / FAILED
# attempts 从进入 RUNNING 起计数；失败时 attempts 已含本次。
# ------------------------------------------------------------

def _fmt(now=None):
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _parse(ts):
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def start_attempt(task, now=None):
    """进入 RUNNING：尝试计数 +1（重跑同一任务沿用同一 task_id）。"""
    task["status"] = "RUNNING"
    task["attempts"] = int(task.get("attempts", 0)) + 1
    task["started_at"] = _fmt(now)
    task["updated_at"] = _fmt(now)
    task["error"] = None
    task["guid"] = None


def finish_success(task, filename, size_bytes, now=None):
    task["status"] = "SUCCESS"
    task["filename"] = filename
    task["size_bytes"] = size_bytes
    task["finished_at"] = _fmt(now)
    task["updated_at"] = _fmt(now)


def fail_attempt(task, error, retries, wait_seconds, now=None):
    """本次尝试失败：未达上限 → RETRY_WAIT（含下次执行时间）；达上限 →
    FAILED 终态（attempts/error 永久保留，规格 29）。"""
    task["error"] = str(error)
    task["updated_at"] = _fmt(now)
    if int(task.get("attempts", 0)) >= int(retries):
        task["status"] = "FAILED"
        task["finished_at"] = _fmt(now)
    else:
        task["status"] = "RETRY_WAIT"
        base = now or datetime.now()
        nxt = base + __import__("datetime").timedelta(seconds=wait_seconds)
        task["next_retry_at"] = nxt.strftime("%Y-%m-%d %H:%M:%S")


def mark_cancelled(task, now=None):
    """用户主动取消：任务进 CANCELLED 终态（§3）。

    记录**保留不删**（`tasks.remove()` 是明确禁止的写法——删掉就再也查不到
    这个任务去过哪里），并补齐 finished_at/updated_at 便于统计与展示。
    取消不是一次尝试，故刻意不动 attempts。
    """
    task["status"] = "CANCELLED"
    task["error"] = CANCEL_REASON
    task["finished_at"] = _fmt(now)
    task["updated_at"] = _fmt(now)


def claim_next(tasks, now=None):
    """FIFO 认领下一个可执行任务（规格 23：一次只跑一个）。

    PENDING 立即可领；RETRY_WAIT 到期（next_retry_at ≤ now）可领；
    RUNNING / SUCCESS / FAILED **/ CANCELLED** 永不认领。无可执行返回 None。
    """
    now = now or datetime.now()
    for task in tasks:
        status = task.get("status")
        if status == "PENDING":
            return task
        if status == "RETRY_WAIT":
            due = _parse(task.get("next_retry_at"))
            if due is not None and due <= now:
                return task
    return None


# ------------------------------------------------------------
# 启动恢复（规格 27/28）
# ------------------------------------------------------------

def recover_tasks(tasks, download_dir, now=None):
    """Agent 启动时对遗留任务做恢复判定（就地修改）。

    * RUNNING = 旧进程遗留 → 先查下载目录：filename 已记且成品文件存在
      （且无对应 .crdownload）→ 恢复 SUCCESS（防止「completed 后、写盘前
      崩溃」的重复下载）；无法可靠判断 → 回 PENDING 重下；
    * PENDING / RETRY_WAIT 原样（认领逻辑按到期时间自然恢复）；
    * SUCCESS / FAILED 终态绝不触碰（SUCCESS 不重下，FAILED 不自动重跑）。
    """
    for task in tasks:
        if task.get("status") != "RUNNING":
            continue
        filename = task.get("filename")
        # 成品要按**任务自己的**目录找：带子目录的任务文件在 A/B 下，
        # 拿根目录去找会认不出来 → 重启后重复下载已完成的文件
        task_dir = get_task_download_dir(download_dir, task)
        final_path = os.path.join(task_dir, filename) if filename else None
        if (final_path and os.path.isfile(final_path)
                and not os.path.exists(final_path + ".crdownload")):
            try:
                size = os.path.getsize(final_path)
            except OSError:
                size = None
            if size:
                finish_success(task, filename, size, now=now)
                logger.info(
                    f"🔁 任务恢复：发现已完成成品，直接记 SUCCESS "
                    f"[{task['task_id'][:8]}] {filename}"
                )
                continue
        task["status"] = "PENDING"
        task["updated_at"] = _fmt(now)
        logger.info(
            f"🔁 任务恢复：RUNNING → PENDING [{task['task_id'][:8]}]"
        )


# ------------------------------------------------------------
# Chrome 拉起 + CDP 下载执行
# ------------------------------------------------------------

def launch_chrome_detached(binary, profile_dir, host, port, proxy_server=None):
    """拉起专用 Chrome 实例（脱离本进程组；参数不含任何 0.0.0.0）。

    proxy_server 非空时加 --proxy-server（专用实例的网络出口与用户bot一致，
    否则被墙资源同样下不动）；不影响用户正常 Chrome 的任何设置。
    """
    cmd = [
        binary,
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        f"--remote-debugging-address={host}",
    ]
    if proxy_server:
        cmd.append(f"--proxy-server={proxy_server}")
    return subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def clean_partial_download(download_dir, task, filename=None):
    """删除**本任务**的半成品文件（`<文件名>.crdownload`），§15。

    清理严格绑定「任务自己的目录 + 自己的文件名」：绝不扫描目录批量删
    `.crdownload`——同一个目录里可能还有别的任务正在下载（错误 D）。文件名
    为 None（downloadWillBegin 还没到）时无从归属，宁可不动手，只记一条
    日志留痕。

    只删 .crdownload，绝不碰成品：完整下载的文件正是我们要保留的东西。
    """
    name = filename or task.get("filename")
    if not name:
        logger.warning(
            f"[T={task.get('task_id', '')[:8]}] 取消时文件名未知，"
            "不清理半成品（无从归属，避免误删其他任务）")
        return None
    task_dir = get_task_download_dir(download_dir, task)
    partial = os.path.join(task_dir, name + ".crdownload")
    try:
        if os.path.exists(partial):
            os.remove(partial)
            logger.info(f"[T={task.get('task_id', '')[:8]}] 🧹 已删除半成品 "
                        f"{name}.crdownload")
            return partial
    except OSError as e:
        logger.warning(f"删除半成品失败（继续按取消收尾）：{e}")
    return None


def sweep_orphan_partials(download_dir, tasks,
                          min_age_seconds=ORPHAN_PARTIAL_MIN_AGE_SECONDS):
    """启动时清掉没人认领的 .crdownload（§15 单任务清理的漏网场景）。

    单个任务取消时的清理「认名字」（只删自己那一个），所以有个漏网：取消到达
    时下载还没开始、宽限窗口内也没等到 downloadWillBegin → 任务已 CANCELLED、
    我们手上没有文件名；**之后** Chrome 才真正开始下载并留下 .crdownload。
    那个文件没有任何人的清理逻辑会碰它（/clean 只管主下载链路的 .download）。

    清理规则（只管 Chrome 下载目录内的 .crdownload，绝不碰成品）：
      * 属于某个**非终态**任务的（按任务自己的目录 + filename 算全路径）→ 保留；
      * mtime 还不到 min_age_seconds 的 → 保留。
        为什么看 mtime：/chrome_stop 只停 Agent，专用 Chrome 实例照常活着，
        上一个 Agent 进程发起的下载可能仍在写盘；刚启动就按名字删会把它的
        半成品端掉，而 Chrome 会继续往那个 inode 写，用户拿到一个永远不完整
        的文件。只有「又没人认领、又确实很久没动过」才敢删。

    只在 Agent 刚启动、本进程还没有任何下载时调用（与主链路启动跑一次
    clean_temp_files() 同款纪律）。返回被删的路径列表。
    """
    alive = set()
    for task in tasks:
        if task.get("status") in TERMINAL_STATUSES:
            continue
        if task.get("filename"):
            alive.add(os.path.join(
                get_task_download_dir(download_dir, task),
                str(task["filename"]) + ".crdownload"))
    now = time.time()
    removed = []
    for root, _dirs, files in os.walk(download_dir):
        for name in files:
            if not name.endswith(".crdownload"):
                continue
            path = os.path.join(root, name)
            if path in alive:
                continue
            try:
                if now - os.path.getmtime(path) < min_age_seconds:
                    continue
                os.remove(path)
                removed.append(path)
            except OSError as e:
                logger.warning(f"清理孤儿半成品失败（跳过）：{e}")
    if removed:
        logger.info(f"🧹 启动清理孤儿半成品 {len(removed)} 个")
    return removed


async def _next_event_or_cancel(cdp, timeout, cancel_task):
    """等下一个 CDP 事件；取消请求到达则返回 `_CANCEL_SENTINEL`（§13）。

    用 asyncio 的任务等待，不做 sleep 轮询：next_event 与取消等待同时挂在事件
    循环上、谁先完成用谁。`cancel_task` 由调用方在**整个尝试期间**持有一个
    （每次事件都现建一个纯属浪费），这里只收掉没赢的 event_task——
    `Queue.get()` 被取消不会消费队列，所以 downloadWillBegin /
    downloadProgress 一个都不会丢。

    两边同一拍完成时**优先采纳事件**（§14：完成先被确认就保留完成）。
    """
    event_task = asyncio.ensure_future(cdp.next_event(timeout=timeout))
    if cancel_task is None:
        return await event_task
    try:
        await asyncio.wait({event_task, cancel_task},
                           return_when=asyncio.FIRST_COMPLETED)
        if not event_task.done():
            # 让同一拍上已经产出的事件先落地，再决定是不是真取消
            await asyncio.sleep(0)
        if event_task.done():
            return event_task.result()
        return _CANCEL_SENTINEL
    finally:
        if not event_task.done():
            event_task.cancel()
        await asyncio.gather(event_task, return_exceptions=True)


async def _wait_begin_within_grace(cdp, deadline):
    """取消时 guid 还没产生：宽限窗口内继续等 downloadWillBegin（§11）。

    等到了就有 guid 可以真正取消；等不到说明下载还没开始（或不会开始），
    返回 (None, None)，由调用方 close_tab 兜底。窗口同时受本任务剩余超时
    约束，不会把取消拖成又一轮完整等待（错误 G：不许因 guid 未产生而
    取消失败）。
    """
    loop = asyncio.get_event_loop()
    limit = min(loop.time() + CANCEL_GUID_GRACE_SECONDS, deadline)
    while True:
        remaining = limit - loop.time()
        if remaining <= 0:
            return None, None
        event = await cdp.next_event(timeout=remaining)
        if event is None:
            continue
        params = event.get("params") or {}
        if event.get("method") == "Browser.downloadWillBegin":
            return params.get("guid"), params.get("suggestedFilename")


async def _finish_cancelled(cdp, guid, filename):
    """取消收尾：真正中止 Chrome 下载，返回统一的取消结果。

    guid 已知 → 发 `Browser.cancelDownload`（§10）。调用失败也照常按取消
    收尾：任务状态与半成品清理不依赖 CDP 是否应答，标签页随后由 finally
    关闭，下载不会继续。
    """
    if guid:
        try:
            await cdp.command("Browser.cancelDownload", {"guid": guid})
            logger.info(f"🛑 已向 Chrome 发出取消（guid {guid}）")
        except Exception as e:
            logger.warning(f"CDP 取消下载调用失败（任务仍按取消收尾）：{e}")
    return False, filename, None, CANCEL_REASON


async def run_download_attempt(cdp, url, download_dir, timeout,
                               cancel_event=None):
    """执行一次下载尝试（规格 22：完全由 CDP 事件判定，不 sleep 猜测）。

    返回 (ok, filename, size_bytes, error)。事件流：
      Browser.downloadWillBegin → 记录 guid/建议文件名（归因本次下载）；
      Browser.downloadProgress  → completed 查成品文件记精确 bytes；
                                   canceled 判失败；
      截止时间内无终态事件 → 「下载超时」失败（含网页类 URL 不触发下载）。

    cancel_event（§8/§12）：非空时支持取消——到达即中止 Chrome 下载并提前
    返回。返回值形状**不变**，调用方看着自己传进来的 cancel_event 判断这次
    是不是取消；这样现存调用点与测试一行都不用改。
    """
    await cdp.setup_download(download_dir)
    target_id = await cdp.open_tab(url)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + float(timeout)
    filename = None
    guid = None
    # 整个尝试期只挂一个取消等待任务（每次事件现建一个会白白churn任务对象）
    cancel_task = (asyncio.ensure_future(cancel_event.wait())
                   if cancel_event is not None else None)
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                if guid is None:
                    guid, filename = await _wait_begin_within_grace(
                        cdp, deadline)
                return await _finish_cancelled(cdp, guid, filename)
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False, filename, None, "下载超时（超时内无终态事件）"
            event = await _next_event_or_cancel(cdp, remaining, cancel_task)
            if event is _CANCEL_SENTINEL:
                continue  # 取消处理只在循环顶部一处（此刻 cancel_event 已置位）
            if event is None:
                continue
            method = event.get("method")
            params = event.get("params") or {}
            if method == "Browser.downloadWillBegin":
                guid = params.get("guid")
                filename = params.get("suggestedFilename")
                continue
            if method == "Browser.downloadProgress":
                if guid is not None and params.get("guid") != guid:
                    continue  # 不是本任务的下载事件
                state = params.get("state")
                if state == "completed":
                    if not filename:
                        return False, None, None, "下载完成但文件名缺失"
                    final_path = os.path.join(download_dir, filename)
                    if not os.path.isfile(final_path):
                        return False, filename, None, "下载完成但成品文件缺失"
                    return True, filename, os.path.getsize(final_path), None
                if state == "canceled":
                    return False, filename, None, "浏览器取消下载"
    finally:
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
        try:
            await cdp.close_tab(target_id)
        except Exception:
            pass


# ------------------------------------------------------------
# 真 CDP 客户端（websockets；接口与 FakeCDP 一致：setup_download /
# open_tab / close_tab / next_event）
# ------------------------------------------------------------

class ChromeCDPClient:
    """浏览器级 CDP WebSocket 客户端（只实现下载所需的最小集）。"""

    def __init__(self, ws_url):
        self._ws_url = ws_url
        self._ws = None
        self._recv_task = None
        self._next_id = 0
        self._pending = {}
        self._events = asyncio.Queue()

    async def connect(self):
        import websockets
        self._ws = await websockets.connect(self._ws_url, max_size=None)
        self._recv_task = asyncio.ensure_future(self._recv_loop())

    async def close(self):
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
            self._recv_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _recv_loop(self):
        try:
            async for raw in self._ws:
                message = json.loads(raw)
                if "id" in message:
                    fut = self._pending.pop(message.get("id"), None)
                    if fut and not fut.done():
                        fut.set_result(message)
                elif "method" in message:
                    await self._events.put(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            return  # 连接断开：事件流自然枯竭，由调用方超时/重连兜底

    async def command(self, method, params=None, timeout=30):
        self._next_id += 1
        msg_id = self._next_id
        fut = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send(json.dumps(
            {"id": msg_id, "method": method, "params": params or {}}))
        reply = await asyncio.wait_for(fut, timeout)
        if reply.get("error"):
            raise RuntimeError(f"CDP {method} 失败：{reply['error']}")
        return reply.get("result") or {}

    async def setup_download(self, download_path):
        await self.command("Browser.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": download_path,
            "eventsEnabled": True,
        })

    async def open_tab(self, url):
        result = await self.command("Target.createTarget", {"url": url})
        return result.get("targetId")

    async def close_tab(self, target_id):
        await self.command("Target.closeTarget", {"targetId": target_id})

    async def next_event(self, timeout):
        try:
            return await asyncio.wait_for(self._events.get(), timeout)
        except asyncio.TimeoutError:
            return None


def _http_get_json(url):
    """同步取 JSON（跑在线程池里，避免阻塞事件循环）。"""
    import urllib.request
    with urllib.request.urlopen(url, timeout=3) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def wait_for_cdp(host, port, timeout):
    """轮询 /json/version 直到拿到 webSocketDebuggerUrl；超时返回 None。"""
    url = f"http://{host}:{port}/json/version"
    deadline = asyncio.get_event_loop().time() + float(timeout)
    while True:
        try:
            data = await asyncio.to_thread(_http_get_json, url)
            ws_url = data.get("webSocketDebuggerUrl")
            if ws_url:
                return ws_url
        except Exception:
            pass
        if asyncio.get_event_loop().time() + 0.5 > deadline:
            return None
        await asyncio.sleep(0.5)


async def ensure_chrome_cdp(binary, profile_dir, host, port,
                            connect_timeout, proxy_server=None):
    """确保专用 Chrome + CDP 可用（规格 12 幂等）：已有 CDP 直接复用；
    没有则拉起专用实例并等待端口就绪。返回 webSocketDebuggerUrl 或 None。"""
    ws_url = await wait_for_cdp(host, port, 2.0)
    if ws_url:
        logger.info("🌐 Chrome 专用实例已在运行，直接复用 CDP")
        return ws_url
    logger.info("🌐 拉起 Chrome 专用实例（CDP 就绪前最多等待 "
                f"{connect_timeout}s）")
    launch_chrome_detached(binary, profile_dir, host, port, proxy_server)
    return await wait_for_cdp(host, port, connect_timeout)


# ------------------------------------------------------------
# 请求认领 + 待办执行 + 进程入口
# ------------------------------------------------------------

def load_requests(path):
    """读 User Bot 写的任务请求（Agent 只读，绝不写这个文件）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning(f"读取 Chrome 请求文件失败：{e}")
        return []
    reqs = (data or {}).get("requests") if isinstance(data, dict) else None
    if not isinstance(reqs, list):
        return []
    return [r for r in reqs if isinstance(r, dict)]


def claim_new_requests(tasks, path):
    """把请求文件里未见过的合法任务并入任务列表，返回本次认领的记录。"""
    known = {t.get("task_id") for t in tasks}
    claimed = []
    for req in load_requests(path):
        task_id = req.get("task_id")
        url = req.get("url")
        if not task_id or task_id in known or not validate_chrome_url(url):
            continue
        task = create_task(url, task_id,
                           download_subdir=req.get("download_subdir"))
        if req.get("label"):
            task["label"] = str(req["label"])
        tasks.append(task)
        known.add(task_id)
        claimed.append(task)
        logger.info(f"📋 认领 Chrome 任务 [{task_id[:8]}] {url}")
    return claimed


def load_cancellations(path):
    """读 User Bot 写的取消请求（Agent 只读，绝不写这个文件）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning(f"读取 Chrome 取消请求文件失败：{e}")
        return []
    items = (data or {}).get("cancellations") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [c for c in items if isinstance(c, dict)]


def apply_cancellations(tasks, cancel_path, now=None):
    """把取消请求落到**尚未开始**的任务上（§7），返回本次真正取消的 task_id。

    只处理 PENDING / RETRY_WAIT：这两种任务没有任何东西在跑，改状态即完成
    取消。RUNNING 的任务不在这里处理——它必须真正中止 Chrome 下载，走
    process_pending_tasks 里那条并行 watcher 的路（边跑边写状态会让
    「下载还在写文件、任务已判 CANCELLED」自相矛盾）。

    SUCCESS / FAILED / 已 CANCELLED 一律不动（§14：已完成的不因取消改判）。
    请求文件是 append-only 的，同一 id 会被反复读到，因此本函数是幂等的。
    """
    if not cancel_path:
        return []
    ids = {c.get("task_id") for c in load_cancellations(cancel_path)}
    ids.discard(None)
    if not ids:
        return []
    cancelled = []
    for task in tasks:
        if task.get("task_id") not in ids:
            continue
        if task.get("status") not in ("PENDING", "RETRY_WAIT"):
            continue
        mark_cancelled(task, now=now)
        cancelled.append(task["task_id"])
        logger.info(f"🛑 任务已取消 [{task['task_id'][:8]}]（排队/等待重试）")
    return cancelled


def apply_label_rename(download_dir, task):
    """成功后按 #标注 重命名：「#标注 原文件名」（磁盘 + 记录同步）。

    标注经 sanitize_filename 清理（'#' 保留）；目标重名时走 unique_path
    追加 (n) 后缀，绝不覆盖已有文件；重命名失败仅告警，保留原名——下载
    本身已成功，命名失败不应把任务打成失败。
    """
    label = str(task.get("label") or "").strip()
    filename = task.get("filename")
    if not label or not filename:
        return
    safe_label = sanitize_filename(label)
    final_path = os.path.join(download_dir, filename)
    if not safe_label or not os.path.isfile(final_path):
        return
    target = unique_path(
        os.path.join(download_dir, f"{safe_label} {filename}"))
    try:
        os.rename(final_path, target)
        task["filename"] = os.path.basename(target)
        task["updated_at"] = _fmt()
        logger.info(
            f"🏷 任务命名 [{task['task_id'][:8]}] → {task['filename']}")
    except OSError as e:
        logger.warning(f"任务命名失败（保留原名）：{e}")


async def _watch_cancel(cancel_path, task_id, cancel_event, poll):
    """看住取消请求文件：见到本任务 id 就置位（Agent 读、User Bot 写）。

    文件 IPC 只能轮询（规格 §9 禁止为此引入 socket/队列服务）。这是全流程
    唯一的轮询点，且与 CDP 事件流并行——下载判定本身仍是事件驱动。
    """
    if not cancel_path:
        return False
    while True:
        for item in load_cancellations(cancel_path):
            if item.get("task_id") == task_id:
                cancel_event.set()
                return True
        await asyncio.sleep(poll)


async def process_pending_tasks(cdp, tasks, tasks_path, download_dir,
                                timeout, retries, wait_seconds,
                                stop_event=None, cancel_path=None,
                                cancel_poll=CANCEL_POLL_SECONDS):
    """把当前可执行的任务按 FIFO 串行处理完（一次只跑一个，规格 23）。

    每次尝试前后都落盘：任何时刻进程死掉，chrome_tasks.json 都是最新事实。
    RETRY_WAIT 回到认领循环按到期时间自然续跑；收到 stop 立即返回，
    RUNNING 记录原样保留（下次启动走恢复规则）。返回处理任务数。

    cancel_path（§7/§16）：取消请求文件路径。认领前先落一遍排队任务的取消；
    下载中另起一个并行 watcher，取消到达即真正中止本次下载（而不是只改
    JSON），随后**继续处理下一个任务**——取消单个任务绝不停掉整个 Agent。
    """
    processed = 0
    counted = set()
    while not (stop_event and stop_event.is_set()):
        # 排队/等待重试的任务：认领前先落取消，取消过的绝不会被领起来
        apply_cancellations(tasks, cancel_path)
        task = claim_next(tasks)
        if task is None:
            break
        if task["task_id"] not in counted:
            counted.add(task["task_id"])
            processed += 1
        trace = task["task_id"][:8]
        while not (stop_event and stop_event.is_set()):
            start_attempt(task)
            save_tasks(tasks, tasks_path)
            # 每个任务用自己的目录（带 download_subdir 的落到子目录里）；
            # 目录按需创建——子目录是第一次下载时才出现的
            task_dir = get_task_download_dir(download_dir, task)
            try:
                os.makedirs(task_dir, exist_ok=True)
            except OSError as e:
                logger.warning(f"[T={trace}] 创建下载子目录失败：{e}")
            logger.info(f"[T={trace}] ▶️ 下载开始（第 {task['attempts']} 次）"
                        f" {task['url']}")
            cancel_event = asyncio.Event()
            watcher = asyncio.ensure_future(_watch_cancel(
                cancel_path, task["task_id"], cancel_event, cancel_poll))
            try:
                ok, filename, size, error = await run_download_attempt(
                    cdp, task["url"], task_dir, timeout,
                    cancel_event=cancel_event)
            finally:
                watcher.cancel()
                try:
                    await watcher
                except (asyncio.CancelledError, Exception):
                    pass
            if stop_event and stop_event.is_set():
                return processed  # RUNNING 原样保留 → 重启恢复
            if ok:
                finish_success(task, filename, size)
                apply_label_rename(task_dir, task)
                save_tasks(tasks, tasks_path)
                logger.info(f"[T={trace}] ✅ 下载成功 {filename} "
                            f"({size} bytes)")
                break
            if cancel_event.is_set():
                # 完成先确认的已在上面的 ok 分支消化（§14）；走到这里就是
                # 取消先确认 → CANCELLED，不重试、不重新执行（错误 E）
                mark_cancelled(task)
                clean_partial_download(download_dir, task, filename=filename)
                save_tasks(tasks, tasks_path)
                logger.info(f"[T={trace}] 🛑 下载已取消（{CANCEL_REASON}）")
                break  # 回到认领循环，继续下一个任务（§16）
            fail_attempt(task, error, retries, wait_seconds)
            save_tasks(tasks, tasks_path)
            logger.warning(
                f"[T={trace}] ❌ 下载失败（{task['status']}）：{error}")
            if task["status"] == "FAILED":
                break
            # RETRY_WAIT：回到认领循环按到期时间自然续跑
            break
    return processed


def configure_agent_logging():
    """把 Agent 进程的日志切到独占文件（config.CHROME_AGENT_LOG_FILE）。

    不能写在模块级：主 userbot 进程也会 import 本模块（chrome_client.py 用它的
    纯函数），模块级 configure 会把主进程日志一并劫走。import 包时 config 已按
    主进程路径 configure 过一次，进程入口再调一次覆盖它即可（configure 幂等，
    先 clear 旧 handler）。详见 issues/001。
    """
    configure_log(CHROME_AGENT_LOG_FILE, LOG_RETENTION_DAYS)


async def agent_main():
    """Chrome Agent 进程入口（python -m tg_userbot.chrome_agent）。"""
    configure_agent_logging()
    os.makedirs(CHROME_DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    with open(CHROME_AGENT_PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    logger.info(f"🤖 Chrome Agent 启动（PID {os.getpid()}）")

    binary = find_chrome_binary()
    if not binary:
        logger.error("❌ 未找到 Chrome 可执行文件，Agent 退出")
        return 1
    ws_url = await ensure_chrome_cdp(
        binary, CHROME_PROFILE_DIR, CHROME_CDP_HOST, CHROME_CDP_PORT,
        CHROME_CDP_CONNECT_TIMEOUT, proxy_server=CHROME_PROXY_SERVER)
    if not ws_url:
        logger.error(
            "❌ CDP 不可用（专用 Chrome 未能就绪），Agent 退出。"
            "未创建新的 Profile 目录以外的任何东西。")
        return 1

    client = ChromeCDPClient(ws_url)
    await client.connect()
    await client.setup_download(CHROME_DOWNLOAD_DIR)
    logger.info(f"🟢 Chrome Agent 就绪：CDP 已连接，下载目录 "
                f"{CHROME_DOWNLOAD_DIR}")

    tasks = load_tasks(CHROME_TASKS_FILE)
    recover_tasks(tasks, CHROME_DOWNLOAD_DIR)
    save_tasks(tasks, CHROME_TASKS_FILE)
    # 恢复判定之后再扫：非终态任务（含刚被恢复成 PENDING 的）的半成品要留着
    sweep_orphan_partials(CHROME_DOWNLOAD_DIR, tasks)
    try:
        while not stop.is_set():
            claim_new_requests(tasks, CHROME_REQUESTS_FILE)
            await process_pending_tasks(
                client, tasks, CHROME_TASKS_FILE, CHROME_DOWNLOAD_DIR,
                CHROME_DOWNLOAD_TIMEOUT, CHROME_DOWNLOAD_RETRIES,
                CHROME_RETRY_WAIT_SECONDS, stop_event=stop,
                cancel_path=CHROME_CANCEL_REQUESTS_FILE)
            if stop.is_set():
                break
            if trim_terminal_tasks(tasks, CHROME_TASKS_KEEP_TERMINAL):
                save_tasks(tasks, CHROME_TASKS_FILE)
            await asyncio.sleep(CHROME_POLL_SECONDS)
    finally:
        await client.close()
        try:
            os.remove(CHROME_AGENT_PID_FILE)
        except OSError:
            pass
        logger.info("🛑 Chrome Agent 已停止（Chrome 专用实例保持运行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(agent_main()))
