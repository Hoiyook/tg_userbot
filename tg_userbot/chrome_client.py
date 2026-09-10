"""Chrome Agent 的 User Bot 侧客户端：Agent 进程管理 + /chrome* 命令 +
task_id → 用户映射持久化 + 结果通知。

进程通道（规格 17 的偏离，2026-09-09 定案）：Agent 不用 Telegram 会话
（独立 session 首次要人工输验证码，破坏「全自动」原则），改走本地 JSON：
本模块独占写 chrome_requests.json（任务请求 + 用户映射），Agent 独占写
chrome_tasks.json（状态机事实源）；双方 temp+os.replace 原子写、只读对方。

权限（规格 35）：chrome 命令只在 Saved Messages（handle_command 上游已限
"me"）且 sender == CHROME_AGENT_OWNER_ID（缺省 MY_ID）时执行。
"""
import asyncio
import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime

from . import chrome_agent
from . import state
from .config import (
    CHROME_AGENT_PID_FILE,
    CHROME_CDP_HOST,
    CHROME_CDP_PORT,
    CHROME_DOWNLOAD_DIR,
    CHROME_REQUESTS_FILE,
    CHROME_TASKS_FILE,
)
from .log import logger
from .naming import format_size

# 回复统一前缀（进自动清理白名单）
CHROME_TEXT_PREFIX = "🤖 Chrome"

_CHROME_CMD_RE = re.compile(
    r"^/(chrome_start|chrome_stop|chrome_status|chrome)"
    r"(?:\s+(\S+))?(?:\s+(\S+))?\s*$",
    re.IGNORECASE,
)


# ------------------------------------------------------------
# 基础
# ------------------------------------------------------------

def download_dir():
    return CHROME_DOWNLOAD_DIR


def is_chrome_command(text):
    """严格形态（用于命令面板/白名单判定）：/chrome 必须带合法 URL。"""
    match = _CHROME_CMD_RE.fullmatch((text or "").strip())
    if not match:
        return False
    if match.group(1).lower() == "chrome":
        return chrome_agent.validate_chrome_url(chrome_url_token(match))
    return True


def _split_subdir_label(token):
    """把 `/chrome` 的头 token 拆成 (download_subdir, label)。

    规则（任务书 §1）：**最后一个 `/` 之后的 `#xxx` 是文件名标注，前面的
    全部内容才是目录路径**；不带 `#` 的头 token 整个就是目录。

        "#标注"      → (None, "#标注")
        "A/#标注"    → ("A", "#标注")
        "A/B/#标注"  → ("A/B", "#标注")
        "A"          → ("A", None)
        "A/B"        → ("A/B", None)

    空 token 返回 (None, None)。
    """
    token = (token or "").strip()
    if not token:
        return None, None
    if "/" in token:
        head, _, tail = token.rpartition("/")
        if tail.startswith("#"):
            return (head.strip("/") or None), tail
        return token.strip("/") or None, None
    if token.startswith("#"):
        return None, token
    return token, None


def parse_chrome_submit(match):
    """解析 `/chrome [目录/][#标注] <URL>`，返回 (url, label, download_subdir)。

    命令的 URL 恒为最后一个 token；它前面的那个 token（若有）是「目录+标注」。
    例外：头 token 自身就是合法 http(s) URL 时维持旧行为（把它当 URL）——
    否则 `/chrome <URL1> <URL2>` 会把 URL1 降级成目录，凭空建出名叫
    `https:` 的目录。`is_chrome_command` 与 `handle_chrome_command` 共用
    本函数，保证判定与执行用的是同一套规则。
    """
    first, second = match.group(2), match.group(3)
    if second is None:
        return (first or "").strip(), None, None
    if chrome_agent.validate_chrome_url(first):
        return (first or "").strip(), None, None
    subdir, label = _split_subdir_label(first)
    return second.strip(), label, subdir


def chrome_url_token(match):
    """/chrome 的 URL token（判定用）；解析规则与 handle_chrome_command 共用。"""
    return parse_chrome_submit(match)[0]


def is_chrome_dispatch(text):
    """分发谓词（比 is_chrome_command 宽）：凡 /chrome* 都交给
    handle_chrome_command，由它对非法形态回用法提示（/chrome 无参、
    /chrome abc 非法 URL 等）。"""
    text = (text or "").strip()
    return text == "/chrome" or text.startswith("/chrome ") \
        or text.startswith("/chrome_")


def resolve_owner_id(my_id):
    """owner：secrets chrome_agent.owner_id 覆盖，缺省回落主账号。"""
    from . import config
    return config.CHROME_AGENT_OWNER_ID or my_id


# ------------------------------------------------------------
# 文本构建（纯函数）
# ------------------------------------------------------------

def submit_text(task_id, url, label=None, download_subdir=None):
    label_line = f"标注：{label}\n" if label else ""
    subdir = chrome_agent.safe_subdir(download_subdir)
    dir_line = f"子目录：{subdir}\n" if subdir else ""
    return (
        f"{CHROME_TEXT_PREFIX} 下载任务已提交\n\n"
        f"任务 ID：{task_id[:8]}\n"
        f"{label_line}"
        f"{dir_line}"
        f"URL：{url}\n\n"
        "完成后会在此通知结果。"
    )


def not_running_text():
    return (
        f"⚠️ Chrome Agent 当前未运行。\n\n请先发送：\n/chrome_start"
    )


def invalid_url_text(url):
    return (
        f"{CHROME_TEXT_PREFIX} URL 无效：{url or '(空)'}\n\n"
        "只支持 http:// 或 https:// 链接。"
    )


def forbidden_text():
    return f"⛔ 无权使用 Chrome 命令（仅 owner）。"


def start_report_text(chrome_path, chrome_version, already, agent_up):
    lines = [
        f"{CHROME_TEXT_PREFIX} Agent 启动"
        + ("成功" if agent_up else "失败"),
        "",
        f"Chrome：{'🟢' if chrome_path else '🔴'} {chrome_version or '未找到'}",
        f"路径：{chrome_path or '(未检测到)'}",
        f"Agent：{'🟢' if agent_up else '🔴'}",
        "",
        "Profile：Agent 专用独立 Profile（不触碰正常 Chrome，"
        "不导出/注入 Cookie）",
        f"下载目录：\n{download_dir()}",
    ]
    if already:
        lines[0] = f"ℹ️ Chrome Agent 已经在运行"
        lines[1] = ""
    if not agent_up:
        lines.append("\n查看日志：runtime/ 下 download.log 搜「Chrome Agent」")
    return "\n".join(lines)


def status_text(agent_up, chrome_running, cdp_ok, tasks, dl_dir,
                unclaimed=None):
    running = next((t for t in tasks if t.get("status") == "RUNNING"), None)
    queued = sum(1 for t in tasks if t.get("status") in ("PENDING",
                                                         "RETRY_WAIT"))
    success = sum(1 for t in tasks if t.get("status") == "SUCCESS")
    failed = sum(1 for t in tasks if t.get("status") == "FAILED")
    # 最近完成的任务单行展示（终态任务在「当前任务/队列」里都看不到，
    # 验收反馈：任务成功后状态里没有任何统计）。优先展示最近成功的下载
    # （用户关心什么落了地）；没有成功才回落展示最近的失败。
    finished = [t for t in tasks
                if t.get("status") in ("SUCCESS", "FAILED")
                and t.get("finished_at")]
    finished.sort(key=lambda t: t["finished_at"])
    successes = [t for t in finished if t.get("status") == "SUCCESS"]
    last = (successes or finished)[-1] if finished else None
    lines = [
        f"{CHROME_TEXT_PREFIX} Agent",
        "",
        f"Agent：{'🟢 Running' if agent_up else '🔴 Stopped'}",
        f"Chrome：{'🟢 Running' if chrome_running else '🔴 Not Running'}",
        f"CDP：{'🟢 Connected' if cdp_ok else '🔴 Unavailable'}",
        f"CDP 地址：{CHROME_CDP_HOST}:{CHROME_CDP_PORT}",
        "",
        f"当前任务：{running['task_id'][:8] if running else '无'}",
        f"队列：{queued}",
        f"任务统计：累计 {len(tasks)}"
        f"（成功 {success} / 失败 {failed}"
        f" / 进行中 {1 if running else 0} / 排队 {queued}）",
    ]
    # 已提交未被 Agent 认领的请求（当前任务下载中时提交的链接住在这里，
    # 验收反馈：提交回执说进了队列、状态里却看不见）
    if unclaimed:
        lines.append(f"待入队请求：{len(unclaimed)} 条"
                     "（当前任务完成后自动认领）")
        for req in unclaimed[:3]:
            lines.append(f"  · {req.get('url', '')[:70]}")
        if len(unclaimed) > 3:
            lines.append(f"  · …共 {len(unclaimed)} 条")
    if last:
        size = last.get("size_bytes")
        size_part = f"，{size} bytes" if size else ""
        lines.append(
            f"最近完成：{last['task_id'][:8]} "
            f"{last.get('status')} {last.get('filename') or ''}{size_part}"
            f"（{last.get('finished_at')}）")
    lines += [
        "",
        f"下载目录：\n{dl_dir}",
    ]
    if not agent_up:
        lines += ["", "请发送：", "/chrome_start"]
    return "\n".join(lines)


def result_text(task):
    """终态通知（规格 20 的 [CHROME_RESULT] 字段以人类可读形式呈现）。"""
    tid = task.get("task_id", "")[:8]
    status = task.get("status")
    if status == "SUCCESS":
        size = task.get("size_bytes")
        size_line = f"大小：{size} bytes（≈ {format_size(size)}）"
        return (
            f"✅ Chrome 下载完成\n\n"
            f"任务 ID：{tid}\n"
            f"状态：SUCCESS\n"
            f"URL：{task.get('url')}\n"
            f"文件：{task.get('filename')}\n"
            f"{size_line}\n"
            f"目录：\n{chrome_agent.get_task_download_dir(download_dir(), task)}"
        )
    attempts = task.get("attempts", 0)
    return (
        f"❌ Chrome 下载失败\n\n"
        f"任务 ID：{tid}\n"
        f"状态：FAILED\n"
        f"URL：{task.get('url')}\n"
        f"错误：{task.get('error') or '(未知)'}\n"
        f"尝试次数：{attempts}"
    )


# ------------------------------------------------------------
# Agent 进程管理（规格 33：PID 文件 + 实际进程检测，防 PID 复用误判）
# ------------------------------------------------------------

def agent_pid():
    try:
        with open(CHROME_AGENT_PID_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None
    except Exception as e:
        logger.warning(f"读取 Chrome Agent PID 文件失败：{e}")
        return None


def _pid_command(pid):
    """进程命令行（用于确认 PID 确属 chrome_agent，防 PID 复用）。"""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return ""
    return out.strip()


def agent_running():
    pid = agent_pid()
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return "chrome_agent" in _pid_command(pid)


def spawn_agent():
    """以同一 venv 解释器拉起 Agent（脱离本进程组；日志走 download.log）。"""
    return subprocess.Popen(
        [sys.executable, "-m", "tg_userbot.chrome_agent"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


async def wait_agent_up(timeout=15):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if agent_running():
            return True
        await asyncio.sleep(0.5)
    return agent_running()


def stop_agent():
    """SIGTERM Agent 并等待退出（同步、阻塞最长 10s——仅命令路径调用）。"""
    pid = agent_pid()
    if not pid:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as e:
        logger.warning(f"停止 Chrome Agent 失败：{e}")
        return False
    import time
    for _ in range(20):  # 最多等 10s 优雅退出
        if not _pid_alive(pid):
            break
        time.sleep(0.5)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        os.remove(CHROME_AGENT_PID_FILE)
    except OSError:
        pass
    logger.info(f"🛑 Chrome Agent 已停止（PID {pid}；Chrome 保持运行）")
    return True


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def chrome_app_running():
    """本机是否有 Chrome 进程（含专用实例与正常实例，仅用于状态展示）。"""
    try:
        out = subprocess.run(["pgrep", "-x", "Google Chrome"],
                             capture_output=True, timeout=5)
    except Exception:
        return False
    return out.returncode == 0


async def cdp_available():
    ws_url = await chrome_agent.wait_for_cdp(
        CHROME_CDP_HOST, CHROME_CDP_PORT, 1.5)
    return ws_url is not None


# ------------------------------------------------------------
# chrome_requests.json（User Bot 独占写；规格 24/32）
# ------------------------------------------------------------

def load_requests(path=None):
    path = path or CHROME_REQUESTS_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning(f"读取 Chrome 请求文件失败：{e}")
        return []
    reqs = (data or {}).get("requests") if isinstance(data, dict) else None
    return [r for r in reqs if isinstance(r, dict)] if isinstance(
        reqs, list) else []


def save_requests(reqs, path=None):
    path = path or CHROME_REQUESTS_FILE
    try:
        temp_path = path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump({"requests": reqs}, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    except Exception as e:
        logger.warning(f"保存 Chrome 请求文件失败：{e}")


def add_request(task_id, url, user_id, chat_id, message_id, path=None,
                now=None, label=None, download_subdir=None):
    """登记一条下载请求。

    download_subdir：可选的下载子目录（`/chrome A/B/#标注 URL`）。只在非空且
    合法时写入——没有子目录的请求保持原有数据结构不变。
    """
    reqs = load_requests(path)
    ts = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    record = {
        "task_id": task_id,
        "url": url,
        "user_id": user_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "created_at": ts,
    }
    if label:
        record["label"] = label
    subdir = chrome_agent.safe_subdir(download_subdir)
    if subdir:
        record["download_subdir"] = subdir
    reqs.append(record)
    save_requests(reqs, path)


def get_request(task_id, path=None):
    return next((r for r in load_requests(path)
                 if r.get("task_id") == task_id), None)


def mark_notified(task_id, path=None):
    reqs = load_requests(path)
    for r in reqs:
        if r.get("task_id") == task_id:
            r["notified_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_requests(reqs, path)


# ------------------------------------------------------------
# 命令流（规格 12-16）
# ------------------------------------------------------------

def _chrome_version(binary):
    try:
        out = subprocess.run([binary, "--version"], capture_output=True,
                             text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:
        return None


async def handle_chrome_command(event, cmd_text, owner_id, sender_id=None):
    """处理 /chrome* 命令；返回 True（命令已被识别消费）。"""
    sender = sender_id if sender_id is not None else getattr(
        event, "sender_id", None)
    if sender is not None and sender != owner_id:
        await event.reply(forbidden_text())
        logger.warning(
            f"Chrome 命令被拒绝：sender {sender} ≠ owner {owner_id}")
        return True

    match = _CHROME_CMD_RE.fullmatch(cmd_text.strip())
    if not match:
        await event.reply(
            f"{CHROME_TEXT_PREFIX} 用法：\n"
            "/chrome_start | /chrome_stop | /chrome_status\n"
            "/chrome <URL>（http/https）")
        return True
    sub, arg = match.group(1).lower(), match.group(2)

    if sub == "chrome_start":
        if agent_running():
            binary = chrome_agent.find_chrome_binary()
            await event.reply(start_report_text(
                binary, _chrome_version(binary) if binary else None,
                already=True, agent_up=True))
            return True
        binary = chrome_agent.find_chrome_binary()
        if not binary:
            await event.reply(
                f"⚠️ 未找到 Chrome 可执行文件，无法启动 Agent。")
            return True
        spawn_agent()
        agent_up = await wait_agent_up()
        await event.reply(start_report_text(
            binary, _chrome_version(binary), already=False, agent_up=agent_up))
        logger.info(f"执行命令：/chrome_start（agent_up={agent_up}）")
        return True

    if sub == "chrome_stop":
        stopped = stop_agent()
        if stopped:
            await event.reply(
                f"{CHROME_TEXT_PREFIX} Agent 已停止\n\n"
                "Chrome（含专用实例）保持运行，不受影响。")
        else:
            await event.reply(f"ℹ️ Chrome Agent 未在运行")
        logger.info(f"执行命令：/chrome_stop（stopped={stopped}）")
        return True

    if sub == "chrome_status":
        agent_up = agent_running()
        chrome_running = chrome_app_running()
        cdp_ok = await cdp_available()
        tasks = chrome_agent.load_tasks(CHROME_TASKS_FILE)
        # 已提交但 Agent 尚未认领的请求（下载中提交的链接住在这里）：
        # 不显示的话用户会以为提交丢了
        known = {t.get("task_id") for t in tasks}
        unclaimed = [r for r in load_requests(CHROME_REQUESTS_FILE)
                     if r.get("task_id") not in known]
        await event.reply(status_text(
            agent_up, chrome_running, cdp_ok, tasks, download_dir(),
            unclaimed=unclaimed))
        logger.info("执行命令：/chrome_status")
        return True

    # sub == "chrome"：[目录/][#标注] <URL> 提交下载（规格 15：Agent 未运行不自动启动）
    # 最后一个 "/" 之后的 #xxx 是文件名标注，前面的是下载子目录
    url, label, download_subdir = parse_chrome_submit(match)
    if not chrome_agent.validate_chrome_url(url):
        await event.reply(invalid_url_text(url))
        return True
    if not agent_running():
        await event.reply(not_running_text())
        return True
    task_id = chrome_agent.new_task_id()
    add_request(task_id, url,
                user_id=sender if sender is not None else owner_id,
                chat_id=owner_id,  # Saved Messages：通知发回收藏夹
                message_id=getattr(event, "id", 0) or 0,
                label=label, download_subdir=download_subdir)
    await event.reply(submit_text(task_id, url, label, download_subdir))
    logger.info(
        f"执行命令：/chrome {url}（task {task_id[:8]}"
        f"{'，标注 ' + label if label else ''}"
        f"{'，子目录 ' + download_subdir if download_subdir else ''}）")
    return True


# ------------------------------------------------------------
# 结果通知（规格 32：User Bot 重启后仍能按 task_id 找到用户）
# ------------------------------------------------------------

async def send_owner_message(chat_id, text):
    if state.client is None:
        return
    if chat_id == getattr(state, "MY_ID", None):
        await state.client.send_message("me", text, link_preview=False)
    else:
        await state.client.send_message(chat_id, text, link_preview=False)


async def notify_pending_results():
    """扫描终态任务，按映射逐条通知并打 notified 标记（幂等，可重复调用）。"""
    tasks = chrome_agent.load_tasks(CHROME_TASKS_FILE)
    for task in tasks:
        if task.get("status") not in ("SUCCESS", "FAILED"):
            continue
        rec = get_request(task["task_id"])
        if not rec or rec.get("notified_at"):
            continue
        try:
            await send_owner_message(rec.get("chat_id"), result_text(task))
            mark_notified(task["task_id"])
            logger.info(
                f"📨 Chrome 结果已通知 [{task['task_id'][:8]}] "
                f"{task.get('status')}")
        except Exception as e:
            logger.warning(f"Chrome 结果通知失败：{e}")


async def notify_loop(interval=5.0):
    """后台轮询：Agent 写完终态 → 本循环发 TG 通知（进程重启不丢映射）。"""
    while True:
        try:
            await notify_pending_results()
        except Exception as e:
            logger.warning(f"Chrome 结果通知轮询异常：{e}")
        await asyncio.sleep(interval)
