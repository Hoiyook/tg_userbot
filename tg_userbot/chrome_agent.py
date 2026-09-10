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
import uuid
from datetime import datetime
from urllib.parse import urlparse

from .config import (
    CHROME_AGENT_LOG_FILE,
    CHROME_AGENT_PID_FILE,
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
    LOG_RETENTION_DAYS,
)
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


def new_task_id():
    """任务唯一 id（uuid hex，与队列记录同款；展示取前 8 位）。"""
    return uuid.uuid4().hex


def create_task(url, task_id, now=None):
    """新建任务记录（规格 25 的完整字段，PENDING 起点）。"""
    ts = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    return {
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


def claim_next(tasks, now=None):
    """FIFO 认领下一个可执行任务（规格 23：一次只跑一个）。

    PENDING 立即可领；RETRY_WAIT 到期（next_retry_at ≤ now）可领；
    RUNNING / SUCCESS / FAILED 永不认领。无可执行返回 None。
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
        final_path = os.path.join(download_dir, filename) if filename else None
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


async def run_download_attempt(cdp, url, download_dir, timeout):
    """执行一次下载尝试（规格 22：完全由 CDP 事件判定，不 sleep 猜测）。

    返回 (ok, filename, size_bytes, error)。事件流：
      Browser.downloadWillBegin → 记录 guid/建议文件名（归因本次下载）；
      Browser.downloadProgress  → completed 查成品文件记精确 bytes；
                                   canceled 判失败；
      截止时间内无终态事件 → 「下载超时」失败（含网页类 URL 不触发下载）。
    """
    await cdp.setup_download(download_dir)
    target_id = await cdp.open_tab(url)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + float(timeout)
    filename = None
    guid = None
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False, filename, None, "下载超时（超时内无终态事件）"
            event = await cdp.next_event(timeout=remaining)
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
        task = create_task(url, task_id)
        if req.get("label"):
            task["label"] = str(req["label"])
        tasks.append(task)
        known.add(task_id)
        claimed.append(task)
        logger.info(f"📋 认领 Chrome 任务 [{task_id[:8]}] {url}")
    return claimed


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


async def process_pending_tasks(cdp, tasks, tasks_path, download_dir,
                                timeout, retries, wait_seconds,
                                stop_event=None):
    """把当前可执行的任务按 FIFO 串行处理完（一次只跑一个，规格 23）。

    每次尝试前后都落盘：任何时刻进程死掉，chrome_tasks.json 都是最新事实。
    RETRY_WAIT 回到认领循环按到期时间自然续跑；收到 stop 立即返回，
    RUNNING 记录原样保留（下次启动走恢复规则）。返回处理任务数。
    """
    processed = 0
    counted = set()
    while not (stop_event and stop_event.is_set()):
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
            logger.info(f"[T={trace}] ▶️ 下载开始（第 {task['attempts']} 次）"
                        f" {task['url']}")
            ok, filename, size, error = await run_download_attempt(
                cdp, task["url"], download_dir, timeout)
            if stop_event and stop_event.is_set():
                return processed  # RUNNING 原样保留 → 重启恢复
            if ok:
                finish_success(task, filename, size)
                apply_label_rename(download_dir, task)
                save_tasks(tasks, tasks_path)
                logger.info(f"[T={trace}] ✅ 下载成功 {filename} "
                            f"({size} bytes)")
                break
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
    try:
        while not stop.is_set():
            claim_new_requests(tasks, CHROME_REQUESTS_FILE)
            await process_pending_tasks(
                client, tasks, CHROME_TASKS_FILE, CHROME_DOWNLOAD_DIR,
                CHROME_DOWNLOAD_TIMEOUT, CHROME_DOWNLOAD_RETRIES,
                CHROME_RETRY_WAIT_SECONDS, stop_event=stop)
            if stop.is_set():
                break
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
