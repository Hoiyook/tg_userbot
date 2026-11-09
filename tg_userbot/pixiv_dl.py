"""Pixiv 画师作品下载 —— /pixiv（2026-10-25）。

**链路**：gallery-dl（App API）+ refresh_token（tg_secrets.json 的
pixiv.refresh_token）。gallery-dl 的 pixiv 只认数字用户 ID，用户名先经
官方短链 pixiv.me/<名> 301 解析成 ID（2026-10-25 实测）。
refresh_token 由 scripts/pixiv_oauth.py 全自动获取（CDP 驱动 Agent
Chrome 点穿授权页）；认证失效时本模块自动重跑该脚本换新 token 再重试
一次。pixiv 全域被墙，一律走 socks 代理。

/pixiv <ID|用户名> —— 全量下载该画师（图+动图；目录 作者名_画师ID，
                     文件名 日期_标题_作品ID_p第几张）
/pixiv            —— 已跟画师与累计数；增量重跑同命令即收新作
                     （download-archive 记已下 ID，天然增量）

落盘 ``DOWNLOAD_DIR/Pixiv/<作者名>_<画师ID>/`` → CD2 备份自动上 115 的
同名目录；文件名 ``日期_标题_作品ID_p第几张``。

**纪律**：后台 task（防重入+强引用+完成 notify，与 /x 同款）；串行
单画师 + 最安全限速档（--sleep 2 + --sleep-request 1，2026-10-25 用户
定档）防风控。
"""
import asyncio
import json
import os
import subprocess
import sys
import time

from . import config
from .log import logger
from .x_dl import GALLERY_DL, _proxy_arg, _run_proc

PIXIV_ROOT_NAME = "Pixiv"                # DOWNLOAD_DIR 下的子目录名
ARCHIVE_FILE = "pixiv_archive.txt"       # download-archive（作品ID 去重）
STATE_FILE = "pixiv_state.json"
OAUTH_SCRIPT = os.path.join(config.REPO_ROOT, "scripts", "pixiv_oauth.py")
# 认证失效的**强信号**（2026-10-25 评审收紧）：绝不能放裸词——
# gallery-dl 每轮正常输出都含 "Refreshing access token"，裸 "refresh"
# 会把代理抖动等任何失败误判成认证失效，触发 3 分钟 oauth 重跑
AUTH_FAIL_MARKS = ("invalid_grant", "refresh-token", "authenticationerror",
                   "login failed", "http 401", "401 client error")

_PIX_RUNNING = {}


def _pixiv_root():
    return os.path.join(config.DOWNLOAD_DIR, PIXIV_ROOT_NAME)


def _state_path():
    return os.path.join(config.RUNTIME_DIR, STATE_FILE)


def _load_state():
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(data):
    path = _state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _load_token():
    """refresh_token（每次现读——自动 reauth 换新后无需重启生效）。"""
    sec = config.load_secret_config()
    pix = sec.get("pixiv") or {}
    tok = (pix.get("refresh_token") or "").strip()
    return tok or None


def resolve_user_id(target):
    """数字 ID 直通；用户名走 pixiv.me 301 解析。返回 (id|None, 说明)。

    pixiv.me 免 cookie 免登录（2026-10-25 实测 dramjou → 101238283）。
    """
    target = (target or "").strip().lstrip("@")
    if not target:
        return None, "空的用户名/ID"
    if target.isdigit():
        return target, ""
    import requests
    proxy = _proxy_arg()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = requests.head(
            f"https://pixiv.me/{target}",
            headers={"User-Agent":
                     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36"},
            proxies=proxies, timeout=15, allow_redirects=False)
    except Exception as e:
        return None, f"pixiv.me 解析失败：{type(e).__name__}: {e}"[:160]
    loc = r.headers.get("Location", "")
    if r.status_code in (301, 302, 307, 308) and "/users/" in loc:
        mid = loc.rstrip("/").rsplit("/", 1)[-1]
        if mid.isdigit():
            return mid, ""
        return None, f"跳转异常：{loc[:120]}"
    if r.status_code == 404:
        return None, f"用户名「{target}」不存在（pixiv.me 404）"
    return None, f"pixiv.me 返回 {r.status_code}：{loc[:100]}"


def _gallery_dl_cmd(user_id, dest_dir, archive, token):
    # 限速是**最安全档**（与 /x 同档，2026-10-25 用户定档）：2s/文件 +
    # 1s/HTTP 请求 + 429 自动退避 10s。想提速必须用户显式改这里。
    cmd = [GALLERY_DL,
           "--directory", dest_dir,
           "--download-archive", archive,
           "-o", f"extractor.pixiv.refresh-token={token}",
           # 目录规约（2026-10-25 用户定）：作者名_作者ID——作者名由
           # gallery-dl 用下载元数据拼（自动清洗非法字符/截长）。
           # directory 必须是**列表**（path.py 逐项迭代；裸字符串会被
           # 逐字符迭代炸成 DirectoryFormatError），JSON 数组过 -o 正好
           "-o", 'directory=["{user[name]}_{user[id]}"]',
           # 文件名规约（2026-10-25 用户定）：日期_标题_作品ID_p第几张
           "-o", "filename={date:%y-%m-%d}_{title}_{id}_p{num}.{extension}",
           "--sleep", "2", "--sleep-request", "1",
           "--sleep-retries", "10.0",
           "--no-part",
           ]
    proxy = _proxy_arg()
    if proxy:
        cmd += ["--proxy", proxy]
    cmd.append(f"https://www.pixiv.net/users/{user_id}")
    return cmd


def _try_reauth():
    """重跑 scripts/pixiv_oauth.py 换新 refresh_token。返回 (ok, msg)。"""
    if not os.path.exists(OAUTH_SCRIPT):
        return False, f"缺少 {OAUTH_SCRIPT}"
    try:
        p = subprocess.run(
            [sys.executable, OAUTH_SCRIPT],
            capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return False, "reauth 超时（180s）"
    except Exception as e:
        return False, f"reauth 执行失败：{type(e).__name__}: {e}"[:160]
    tail = ((p.stdout or "") + (p.stderr or "")).strip()[-200:]
    logger.info(f"[pixiv] reauth 退出码 {p.returncode}：{tail}")
    if p.returncode == 0 and _load_token():
        return True, "reauth 成功，token 已更新"
    return False, f"reauth 未成功：{tail[-120:]}"


def run_user(target):
    """同步拉一个画师（后台 task 的线程体）。返回 (新增文件数, 说明)。

    解析用户名 → gallery-dl → 认证失效自动 reauth 重试一次。
    进度实时写 _PIX_RUNNING（done/last_file）——/pixiv 裸命令可查。
    """
    user_id, note = resolve_user_id(target)
    if not user_id:
        return 0, f"❌ {note}"
    _PIX_RUNNING.update({"user_id": user_id})
    token = _load_token()
    if not token:
        ok, msg = _try_reauth()
        if not ok:
            return 0, f"❌ 无 refresh_token 且 reauth 失败：{msg}"
        token = _load_token()

    dest = _pixiv_root()
    os.makedirs(dest, exist_ok=True)   # 叶子目录（作者名_ID）由 gallery-dl 建
    archive = os.path.join(config.RUNTIME_DIR, ARCHIVE_FILE)

    def _once(tok):
        cmd = _gallery_dl_cmd(user_id, dest, archive, tok)

        def on_line(line):
            if line.startswith("."):
                _PIX_RUNNING.update(
                    {"done": _PIX_RUNNING.get("done", 0) + 1,
                     "last": line.rsplit("/", 1)[-1][:48]})

        return _run_proc(cmd, on_line, tag="pixiv")

    # 失败自动重跑（2026-10-26 定档，与 /x 同款参数）：非零退出按指数
    # 退避整跑重试——archive 幂等，重跑只补漏。解析失败/reauth 失败在
    # 环外早退，不重试。
    retry_max = int(getattr(config, "GALLERY_DL_AUTO_RETRY_MAX", 2))
    attempt = 0
    new_total = 0
    last_tail = ""
    dir_label = ""
    while True:
        code, tail, out = _once(token)
        if code != 0 and any(m in tail.lower() for m in AUTH_FAIL_MARKS):
            logger.warning(f"[pixiv] 认证失效信号，自动 reauth 重试：{user_id}")
            ok, msg = _try_reauth()
            if not ok:
                return 0, f"❌ 认证失效且自动 reauth 失败：{msg}"
            code, tail, out = _once(_load_token())
        last_tail = (tail or "").strip()[-200:]
        # 本次新下载 = 输出里实际落盘的路径行（archive 命中的静默跳过）；
        # 目录名（作者名_ID）从落盘路径学出来存 state——零新文件时沿用旧值
        new_files = [ln for ln in (out or "").splitlines()
                     if ln.startswith(".") and not ln.endswith(("#",))]
        new_total += len(new_files)
        for ln in new_files:
            seg = ln.lstrip("./").split("/")
            if len(seg) >= 2 and seg[-2]:   # 紧邻文件的父目录 = 作者名_ID
                dir_label = seg[-2]
                break
        if code == 0 or attempt >= retry_max:
            break
        delay = min(
            float(getattr(config, "GALLERY_DL_AUTO_RETRY_BASE_DELAY", 300))
            * (3 ** attempt),
            float(getattr(config, "GALLERY_DL_AUTO_RETRY_MAX_DELAY", 1800)))
        logger.warning(f"[pixiv] 本轮失败（退出码 {code}），{int(delay)}s 后"
                       f"自动重跑（第 {attempt + 1}/{retry_max} 次）：{user_id}")
        _PIX_RUNNING.update({"retry_wait": int(delay),
                             "attempt": attempt + 1})
        time.sleep(delay)
        _PIX_RUNNING.pop("retry_wait", None)
        _PIX_RUNNING.pop("attempt", None)
        attempt += 1
    state = _load_state()
    entry = state.get(user_id) or {"total": 0}
    entry["total"] += new_total
    entry["last_run"] = int(time.time())
    entry["target"] = target
    if dir_label:
        entry["dir"] = dir_label
    state[user_id] = entry
    _save_state(state)
    if code != 0:
        retried = f"（已自动重跑 {attempt} 次仍失败）" if attempt else ""
        return new_total, (f"⚠️ gallery-dl 退出码 {code}{retried}"
                           f"｜{last_tail}")
    return new_total, ""


# ── SerialTaskWorker 接线（2026-11-05 队列统一）──────────────
# 排队/重试/租约/重启恢复收进 xi_tasks 表（runtime_db.TaskDomain）；
# _PIX_RUNNING 只承担进度展示。run_user 同步层（resolve/reauth/画师
# 累计 state）原样保留——测试钉住，语义不变。
from . import runtime_db   # noqa: E402
from .task_worker import DomainWorker   # noqa: E402


class _PixivWorker(DomainWorker):
    name = "🎨 pixiv"

    def __init__(self):
        super().__init__(runtime_db.XI_TASKS, key_field="target")
        self.poll_seconds = 5.0
        self.idle_sleep = 2.0
        self.inner_task = None
        self._stop_flag = False

    def startup_banner(self):
        return ("🎨 pixiv worker 已启动（串行一次一个画师，限速档，"
                "archive 幂等增量）")

    async def process(self, task):
        from . import notify
        global _PIX_RUNNING
        raw = task["target"]
        label = str(raw or "").strip()[:40] or "?"
        _PIX_RUNNING = {"target": label, "started": time.time(), "done": 0}
        try:
            self.inner_task = asyncio.ensure_future(
                asyncio.to_thread(run_user, raw))
            try:
                n, note = await self.inner_task
            except asyncio.CancelledError:
                # 双取消语义（与 bili/x 同款）：stop→FAILED；停机→release
                if self._stop_flag:
                    self.domain.fail(raw, "用户停止", max_attempts=1)
                    self._stop_flag = False
                raise
            except Exception as e:
                logger.exception(f"[pixiv] 后台执行异常（{label}）")
                self.domain.fail(raw, f"{type(e).__name__}: {e}")
                try:
                    await notify.notify_user(
                        f"🎨 Pixiv 下载异常（{label}）——已转自动重试："
                        f"{type(e).__name__}: {e}")
                except Exception:
                    pass
                return
            if note.startswith("❌") or "退出码" in note:
                row = self.domain.get(raw) or {}
                self.domain.fail(raw, note or "下载失败")
                text = (f"❌ Pixiv 下载失败（{label}）：{note[:120]}——"
                        f"已转自动重试（第 {row.get('attempts', '?')}/3 次）")
            else:
                self.domain.complete(raw)
                from .history import append_history
                append_history(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} | Pixiv | "
                    f"{label} | {n} 个新 | 来源：Pixiv下载")
                text = (f"🎨 Pixiv 下载完成（{label}）：本次新增 {n} 个"
                        "｜落盘 Pixiv/作者名_画师ID/（CD2 会自动备份上 115）")
                if note:
                    text += "\n" + note
            try:
                await notify.notify_user(text)
            except Exception:
                pass
        finally:
            self.inner_task = None
            _PIX_RUNNING = {}


_WORKER = _PixivWorker()


def pixiv_worker_loop():
    """常驻循环入口（app.py create_task 持强引用）。"""
    return _WORKER.run()


def release_inflight():
    """停机兜底：外层在协程取消后再调一次（幂等）。"""
    return _WORKER.release_inflight()


def pixiv_stop():
    """/pixiv stop：取消在途并标 FAILED 终态（可 retry 重投），队列继续。"""
    if not _PIX_RUNNING:
        return "ℹ️ 没有 Pixiv 下载在跑"
    label = _PIX_RUNNING.get("user_id") or _PIX_RUNNING.get("target", "?")
    _WORKER._stop_flag = True
    if _WORKER.inner_task is not None:
        _WORKER.inner_task.cancel()
    left = runtime_db.XI_TASKS.count(status="PENDING")
    return (f"🛑 已停止「{label}」（不再自动重试，/pixiv retry <id> 可重投）；"
            f"队列剩 {left} 条继续自动跑")


def _submit(arg):
    """提交画师入队（DB 队列；重复目标任何状态都判重）。"""
    key = str(arg or "").strip()
    if not key:
        return "🎨 用法：/pixiv <用户ID|用户名>"
    if runtime_db.XI_TASKS.enqueue(key):
        pos = runtime_db.XI_TASKS.count(status="PENDING")
        return (f"🎨 已入队（第 {pos} 位）：{key[:40]}\n"
                "串行执行（限速防风控），完成会通知；"
                "增量去重——已在 archive 的作品不重下。进度 /pixiv 查看")
    row = runtime_db.XI_TASKS.get(key) or {}
    st, rid = row.get("status", "?"), row.get("id", "?")
    if st == "FAILED":
        return f"⏭️ 该画师已在队列且失败过（#{rid}）——/pixiv retry {rid} 重投"
    return f"⏭️ 该画师已在队列中（#{rid}，{st}）——跑完自动继续下一条"


def status_text():
    """进度（内存视图）+ 队列（DB 真相源）合一状态。"""
    head = ""
    if _PIX_RUNNING:
        uid = _PIX_RUNNING.get("user_id") or _PIX_RUNNING.get("target", "?")
        done = _PIX_RUNNING.get("done", 0)
        started = _PIX_RUNNING.get("started")
        elapsed = ""
        if started:
            m, s = divmod(int(time.time() - started), 60)
            elapsed = f"｜已跑 {m}分{s:02d}秒"
        head = f"🎨 Pixiv 下载进行中（{uid}）：本次已收 {done} 个{elapsed}"
        if _PIX_RUNNING.get("retry_wait"):
            head += (f"\n⏳ 上轮失败，{_PIX_RUNNING['retry_wait']}s 后自动重跑"
                     f"（第 {_PIX_RUNNING.get('attempt', '?')} 次）")
        last = _PIX_RUNNING.get("last", "")
        if last:
            head += f"\n最新：{last}"
    rows = _WORKER.queue_rows()
    # 已跟画师累计（run_user 学到的目录名——作者名_ID，含历史总量）
    try:
        state = _load_state()
    except Exception:
        state = {}
    followed = {k: v for k, v in (state or {}).items()
                if isinstance(v, dict) and v.get("total")}
    if not rows and not head:
        if not followed:
            return ("🎨 /pixiv <用户ID|用户名> —— 下载该画师全部作品"
                    "（图+动图，refresh_token 已配置）\n"
                    "⏳ 队列：空")
        lines = ["🎨 /pixiv <用户ID|用户名> —— 下载该画师全部作品"
                 "（图+动图，refresh_token 已配置）\n⏳ 队列：空",
                 "🎨 已跟画师："]
        for uid, v in sorted(followed.items(),
                             key=lambda kv: -kv[1].get("total", 0))[:10]:
            t = time.strftime("%m-%d %H:%M",
                              time.localtime(v.get("last_run", 0)))
            shown = v.get("dir") or v.get("target") or uid
            lines.append(f"  {shown}：累计 {v.get('total', 0)} 个｜上次 {t}")
        return "\n".join(lines)[:3900]
    from collections import Counter
    counts = Counter(r["status"] for r in rows)
    summary = " ".join(f"{s}={n}" for s, n in counts.items())
    lines = ([head] if head else []) + [f"⏳ 队列（{summary}，最近 "
                                        f"{len(rows)} 条）："]
    for r in rows:
        name = (r.get("label") or r["target"])[:36]
        err = f"｜{str(r['error'])[:24]}" if r.get("error") else ""
        lines.append(f"  #{r['id']} [{r['status']}] {name}{err}")
    lines.append("操作：/pixiv stop 停当前｜/pixiv retry <id|all>｜"
                 "/pixiv del <id>")
    if followed:
        lines.append("🎨 已跟画师：")
        for uid, v in sorted(followed.items(),
                             key=lambda kv: -kv[1].get("total", 0))[:10]:
            t = time.strftime("%m-%d %H:%M",
                              time.localtime(v.get("last_run", 0)))
            shown = v.get("dir") or v.get("target") or uid
            lines.append(f"  {shown}：累计 {v.get('total', 0)} 个｜上次 {t}")
    return "\n".join(lines)[:3900]


async def command_reply(cmd_text):
    """/pixiv [ID|用户名]；裸命令看状态；子命令 stop/retry/del。"""
    parts = str(cmd_text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if not arg:
        return status_text()
    head = arg.split()[0].lower()
    if head == "stop":
        return pixiv_stop()
    if head == "retry":
        toks = arg.split()
        target = toks[1] if len(toks) > 1 else ""
        if target == "all":
            n = runtime_db.XI_TASKS.retry()
            return f"🎨 已重投 {n} 条失败任务"
        if target.isdigit():
            n = runtime_db.XI_TASKS.retry_by_id(int(target))
            return (f"🎨 已重投 #{target}" if n
                    else f"ℹ️ #{target} 不是可重投的失败任务")
        return "🎨 用法：/pixiv retry <id|all>（id 看 /pixiv 队列列表）"
    if head == "del":
        toks = arg.split()
        target = toks[1] if len(toks) > 1 else ""
        if target.isdigit():
            runtime_db.XI_TASKS.delete(int(target))
            return f"🎨 🗑 已移除 #{target}"
        return "🎨 用法：/pixiv del <id>"
    return _submit(arg)


def is_pixiv_command(cmd_text):
    head = str(cmd_text or "").split()[0] if str(cmd_text or "").strip() else ""
    return head.lower() == "/pixiv"
