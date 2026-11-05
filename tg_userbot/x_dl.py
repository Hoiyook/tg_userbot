"""X（Twitter）作者作品下载 —— /x（2026-10-25）。

**链路**：gallery-dl（venv 已装 1.32.15）+ Agent Chrome 的登录 cookie
（2026-10-25 实测：匿名 AuthRequired；Agent profile 登录后
auth_token/ct0/twid 齐活，走 Hiddify 代理拉时间线全通）。

/x @作者      —— 全量拉取该作者（图片+视频，日期+推文ID 命名）
/x           —— 已跟作者与累计数；增量重跑同命令即收新作品
                （download-archive 记已下 ID，天然增量）

落盘 ``DOWNLOAD_DIR/X/<作者>/`` → CD2 备份自动上 115 的 ``X/<作者>/``。

**登录态管理**：cookie 从 Agent Chrome profile（CHROME_PROFILE_DIR）
解密导出到 ``RUNTIME_DIR/x_cookies.txt``——首次自动导出；X 踢登录
（401/authenticate）时自动重新导出重试一次；仍失败提示去 Agent Chrome
重新登录。browser_cookie3 解密走 Keychain（首次可能要授权一次）。

**纪律**：后台 task（防重入+强引用+完成 notify，与 /115dup scan 同款）；
gallery-dl 串行单作者执行 + 限速（--sleep）防风控。
"""
import asyncio
import json
import os
import re
import subprocess
import time

from .spawn import spawn
from . import config
from . import runtime_db
from .log import logger
from .task_worker import DomainWorker

GALLERY_DL = os.path.join(os.path.dirname(config.__file__),
                          "..", ".venv", "bin", "gallery-dl")
X_ROOT_NAME = "X"                       # DOWNLOAD_DIR 下的子目录名
ARCHIVE_FILE = "x_archive.txt"          # download-archive（ID 去重）
STATE_FILE = "x_state.json"
# 裸 "401" 会命中文件名/推文 ID 里的数字子串（约 1/10 的 ID 含 401）
# → 失败时误刷 cookie 双倍重试——只认带上下文的强信号（2026-10-25 评审）
AUTH_FAIL_MARKS = ("http 401", "401 client error", "authenticate",
                   "login", "forbidden")

_X_RUNNING = {}
_X_TASKS = set()
_PROCS = set()          # 在跑的 gallery-dl 子进程（停机回收用）


def _x_root():
    return os.path.join(config.DOWNLOAD_DIR, X_ROOT_NAME)


def _cookie_path():
    return os.path.join(config.RUNTIME_DIR, "x_cookies.txt")


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


def refresh_cookies():
    """Agent Chrome profile → 解密导出 Netscape cookie 文件。

    Chrome 运行中持锁——复制库文件再读。auth_token/ct0 缺一即失败。
    返回 (ok, msg)。
    """
    import shutil
    import sqlite3
    import tempfile
    db = os.path.join(config.CHROME_PROFILE_DIR, "Default", "Cookies")
    if not os.path.exists(db):
        return False, f"Agent Chrome 无 cookie 库：{db}"
    tmp = tempfile.mktemp(suffix=".db")
    try:
        shutil.copy(db, tmp)
        import browser_cookie3
        cj = browser_cookie3.chrome(cookie_file=tmp, domain_name=".x.com")
        rows = [(c.name, c.value, c.domain, c.path, c.expires, c.secure)
                for c in cj]
    except Exception as e:
        return False, f"cookie 提取失败：{type(e).__name__}: {e}"[:160]
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    names = {n for n, *_ in rows}
    if "auth_token" not in names or "ct0" not in names:
        return False, ("Agent Chrome 未登录 X（缺 auth_token/ct0）——"
                       "在 Agent 的 Chrome 里登录 x.com 后重试")
    lines = ["# Netscape HTTP Cookie File"]
    for n, v, dom, path, exp, sec in rows:
        lines.append(f"{dom}\tTRUE\t{path}\t"
                     f"{'TRUE' if sec else 'FALSE'}\t{int(exp or 0)}\t{n}\t{v}")
    out = _cookie_path()
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return True, f"已导出 {len(rows)} 条 cookie"


def _proxy_arg():
    raw = (os.environ.get("TG_PROXY")
           or getattr(config, "CHROME_PROXY", "") or "").strip()
    # gallery-dl --proxy 接受 socks5://host:port / http://...（与 TG_PROXY
    # 同格式）；socks5h 让 DNS 也走代理
    if raw.startswith("socks5://"):
        return "socks5h://" + raw[len("socks5://"):]
    return raw or None


def _gallery_dl_cmd(author, dest_dir, archive, url=None):
    # 限速是**最安全档**（2026-10-25 用户定档）：sleep 2s/文件 +
    # sleep-request 1s/HTTP 请求（低于人肉浏览密度）+ 收到 429 限流
    # 自动退避 10s 重试。想提速必须用户显式改这里，别在别处覆盖。
    # url：直投目标（推文链接 2026-11-05 分享自动识别）——缺省作者主页；
    # 落盘目录仍按作者（推文媒体归到作者名下，命名规约不变）。
    cmd = [GALLERY_DL,
           "--cookies", _cookie_path(),
           "--directory", dest_dir,
           "--download-archive", archive,
           # 项目命名规约：日期前缀 + 推文 ID（画师推文常无有效文本）
           "-o", "filename={date:%y-%m-%d} {tweet_id} {num}.{extension}",
           "--sleep", "2", "--sleep-request", "1", "--sleep-retries", "10.0",
           ]
    proxy = _proxy_arg()
    if proxy:
        cmd += ["--proxy", proxy]
    cmd.append(url or f"https://x.com/{author.lstrip('@')}")
    return cmd


def _run_proc(cmd, on_line=None, timeout=3600, tag="x"):
    """流式跑 gallery-dl：每输出行回调 on_line（新文件行实时计数）。

    返回 (returncode, tail, stdout)。子进程输出同时进 bot 日志——
    排查风控/卡死时有据可查（tag 区分调用方，如 pixiv）。超时 kill
    返回 (9, "", "")。
    """
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                bufsize=1)
    except OSError as e:
        return 8, f"{type(e).__name__}: {e}", ""
    _PROCS.add(proc)    # 停机时可 terminate（否则成孤儿，与续跑并发打架）
    lines = []
    deadline = time.monotonic() + timeout
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            if len(lines) > 400:
                lines.pop(0)      # tail 窗口
            logger.info(f"[{tag}] {line}")
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:
                    pass
            if time.monotonic() > deadline:
                proc.kill()
                joined = "\n".join(lines)
                return 9, joined[-1500:], joined
        proc.wait(timeout=max(0, deadline - time.monotonic()) or 1)
    except Exception as e:
        proc.kill()
        return 9, f"{type(e).__name__}: {e}", "\n".join(lines)
    finally:
        _PROCS.discard(proc)
    out = "\n".join(lines)
    return proc.returncode, out[-1500:], out


def shutdown_children():
    """优雅停机：terminate 所有在跑 gallery-dl 子进程（X/Pixiv 共用）。

    不杀会成孤儿继续下载——重启后 state 标记触发续跑，两个进程写同一
    download-archive（sqlite 锁）会打架。杀掉的半份文件（--no-part）由
    续跑重下该文件，archive 保证其余作品不重复。
    """
    for proc in list(_PROCS):
        try:
            proc.terminate()
        except Exception:
            pass
    for proc in list(_PROCS):
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    n = len(_PROCS)
    _PROCS.clear()
    if n:
        logger.info(f"[x] 停机回收：terminate {n} 个 gallery-dl 子进程")
    return n


def run_author(author, url=None, label=None):
    """同步拉一个作者/推文（后台 task 的线程体）。返回 (新增文件数, 说明)。

    cookie 就绪 → gallery-dl → 认证失败自动刷新 cookie 重试一次。
    进度实时写 _X_RUNNING（done/last_file）——/x 裸命令可查。
    url：推文直链（2026-11-05 分享自动识别）——缺省作者主页；落盘仍按
    作者目录（dest=作者名），state 累计也归作者。
    """
    global _X_RUNNING
    author = author.lstrip("@").strip()
    label = label or f"@{author}"
    dest = os.path.join(_x_root(), author)
    os.makedirs(dest, exist_ok=True)
    archive = os.path.join(config.RUNTIME_DIR, ARCHIVE_FILE)
    if not os.path.exists(_cookie_path()):
        ok, msg = refresh_cookies()
        if not ok:
            return 0, f"❌ {msg}"

    def _once():
        cmd = _gallery_dl_cmd(author, dest, archive, url=url)

        def on_line(line):
            if line.startswith("."):
                _X_RUNNING.update({"done": _X_RUNNING.get("done", 0) + 1,
                                   "last": line.rsplit("/", 1)[-1][:48]})

        return _run_proc(cmd, on_line)

    # 失败自动重跑（2026-10-26 定档）：非零退出按指数退避整跑重试
    # GALLERY_DL_AUTO_RETRY_MAX 次——archive 幂等，重跑只补漏不重复。
    # 认证失效分支在环内先行处理（刷 cookie 一次）；刷不出来的早退
    # 不重试（重试也没用）。
    retry_max = int(getattr(config, "GALLERY_DL_AUTO_RETRY_MAX", 2))
    attempt = 0
    new_total = 0
    last_tail = ""
    while True:
        code, tail, out = _once()
        if code != 0 and any(m in tail.lower() for m in AUTH_FAIL_MARKS):
            logger.warning(f"[x] 登录态失效信号，刷新 cookie 重试：{author}")
            ok, msg = refresh_cookies()
            if not ok:
                return 0, f"❌ 登录态失效且刷新失败：{msg}"
            code, tail, out = _once()
        last_tail = (tail or "").strip()[-200:]
        new_files = [ln for ln in (out or "").splitlines()
                     if ln.startswith(".") and not ln.endswith(("#",))]
        new_total += len(new_files)
        if code == 0 or attempt >= retry_max:
            break
        delay = min(
            float(getattr(config, "GALLERY_DL_AUTO_RETRY_BASE_DELAY", 300))
            * (3 ** attempt),
            float(getattr(config, "GALLERY_DL_AUTO_RETRY_MAX_DELAY", 1800)))
        logger.warning(f"[x] 本轮失败（退出码 {code}），{int(delay)}s 后自动"
                       f"重跑（第 {attempt + 1}/{retry_max} 次）：{author}")
        _X_RUNNING.update({"retry_wait": int(delay),
                           "attempt": attempt + 1})
        time.sleep(delay)
        _X_RUNNING.pop("retry_wait", None)
        _X_RUNNING.pop("attempt", None)
        attempt += 1
    # 新增数：本次输出里实际下载行（# 结尾为跳过标记）——gallery-dl 对
    # 已在 archive 的条目静默跳过，输出的路径行即本次新下载（跨重跑累计）
    state = _load_state()
    entry = state.get(author) or {"total": 0}
    entry["total"] += new_total
    entry["last_run"] = int(time.time())
    state[author] = entry
    _save_state(state)
    if code != 0:
        retried = f"（已自动重跑 {attempt} 次仍失败）" if attempt else ""
        return new_total, (f"⚠️ gallery-dl 退出码 {code}{retried}"
                           f"｜{last_tail}")
    return new_total, ""


# ── SerialTaskWorker 接线（2026-11-05 队列统一）──────────────
# 排队/重试/租约/重启恢复收进 xn_tasks 表（runtime_db.TaskDomain）；
# _X_RUNNING 只承担进度展示。run_author 的同步层（cookie 刷新/内部
# 退避/作者累计 state）原样保留——测试钉住，语义不变。

_TWEET_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:x|twitter)\.com/(\w{1,20})/status/(\d+)", re.I)
_AUTHOR_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:x|twitter)\.com/(@?\w{1,20})/?\s*$", re.I)
_X_LINK_RE = re.compile(
    r"https?://(?:www\.)?(?:x|twitter)\.com/\S+", re.I)


def looks_like_x(text):
    """文本里是否含 X/Twitter 链接（app.py 自动识别入口判定）。"""
    return bool(_X_LINK_RE.search(str(text or "")))


def normalize_target(raw):
    """输入目标 → (规范键, gallery_url, 展示名, 错误)。

    推文链接 → 键=裁剪 URL（去 query 跟踪参数）；作者页/裸名/@名 →
    键=@name。None 键 = 不可识别（err 给原因）。"""
    raw = str(raw or "").strip()
    if not raw:
        return None, None, "", "用法：/x <@作者|作者页链接|推文链接>"
    m = _TWEET_URL_RE.search(raw)
    if m:
        url = f"https://x.com/{m.group(1)}/status/{m.group(2)}"
        return url, url, f"@{m.group(1)}/status/{m.group(2)}", None
    m = _AUTHOR_URL_RE.search(raw)
    if m:
        a = m.group(1).lstrip("@")
        return f"@{a}", f"https://x.com/{a}", f"@{a}", None
    if raw.lower().startswith(("http://", "https://")):
        # 链接但不是认识的形态（搜索页/首页/i/ 流）——不猜
        return None, None, "", "认不出 X 链接（支持推文 /status/ 与作者页）"
    a = raw.lstrip("@").strip()
    if not re.fullmatch(r"\w{1,20}", a):
        return None, None, "", "认不出作者名（字母数字下划线 ≤20）"
    return f"@{a}", f"https://x.com/{a}", f"@{a}", None


class _XWorker(DomainWorker):
    name = "🐦 x"

    def __init__(self):
        super().__init__(runtime_db.XN_TASKS, key_field="target")
        self.poll_seconds = 5.0
        self.idle_sleep = 2.0    # 任务间间隔（限速纪律与 gallery-dl 内叠加）
        self.inner_task = None
        self._stop_flag = False  # True：取消时标 FAILED 终态（不回队列）

    def startup_banner(self):
        return ("🐦 x worker 已启动（串行一次一个目标，gallery-dl 限速，"
                "archive 幂等增量）")

    async def process(self, task):
        from . import notify
        global _X_RUNNING
        raw = task["target"]
        key, gurl, label, err = normalize_target(raw)
        if err:
            # 不可识别 → 终态（重试也不会变对）；/x retry 可重投
            self.domain.fail(raw, err, max_attempts=1)
            try:
                await notify.notify_user(f"🐦 X 目标无法识别：{raw[:60]}")
            except Exception:
                pass
            return
        is_tweet = "/status/" in (gurl or "")
        self.domain.update(raw, kind="tweet" if is_tweet else "author",
                           label=label)
        _X_RUNNING = {"author": label, "started": time.time(), "done": 0}
        try:
            self.inner_task = asyncio.ensure_future(
                asyncio.to_thread(run_author, key, url=gurl, label=label))
            try:
                n, note = await self.inner_task
            except asyncio.CancelledError:
                # 双取消语义（与 bili 同款）：stop→FAILED 终态；停机→
                # 框架 release 回 PENDING（重启自动续跑，archive 幂等）
                if self._stop_flag:
                    self.domain.fail(raw, "用户停止", max_attempts=1)
                    self._stop_flag = False
                raise
            except Exception as e:
                logger.exception(f"[x] 后台执行异常（{label}）")
                self.domain.fail(raw, f"{type(e).__name__}: {e}")
                try:
                    await notify.notify_user(
                        f"🐦 X 下载异常（{label}）——已转自动重试："
                        f"{type(e).__name__}: {e}")
                except Exception:
                    pass
                return
            if note.startswith("❌") or "退出码" in note:
                row = self.domain.get(raw) or {}
                self.domain.fail(raw, note or "下载失败")
                text = (f"❌ X 下载失败（{label}）：{note[:120]}——"
                        f"已转自动重试（第 {row.get('attempts', '?')}/3 次）")
            else:
                self.domain.complete(raw)
                from .history import append_history
                append_history(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} | X | "
                    f"{label} | {n} 个新 | 来源：X下载")
                text = (f"🐦 X 下载完成（{label}）：本次新增 {n} 个"
                        "｜落盘 X/<作者>/（CD2 会自动备份上 115）")
                if note:
                    text += "\n" + note
            try:
                await notify.notify_user(text)
            except Exception:
                pass
        finally:
            self.inner_task = None
            _X_RUNNING = {}


_WORKER = _XWorker()


def x_worker_loop():
    """常驻循环入口（app.py create_task 持强引用）。"""
    return _WORKER.run()


def release_inflight():
    """停机兜底：外层在协程取消后再调一次（幂等）。"""
    return _WORKER.release_inflight()


def x_stop():
    """/x stop：取消在途并标 FAILED 终态（可 retry 重投），队列继续。"""
    if not _X_RUNNING:
        return "ℹ️ 没有 X 下载在跑"
    label = _X_RUNNING.get("author", "?")
    _WORKER._stop_flag = True
    if _WORKER.inner_task is not None:
        _WORKER.inner_task.cancel()
    left = runtime_db.XN_TASKS.count(status="PENDING")
    return (f"🛑 已停止「{label}」（不再自动重试，/x retry <id> 可重投）；"
            f"队列剩 {left} 条继续自动跑")


def _submit(arg):
    """提交目标入队（DB 队列；重复目标任何状态都判重）。"""
    key, gurl, label, err = normalize_target(arg)
    if err:
        return f"❌ {err}"
    if runtime_db.XN_TASKS.enqueue(key):
        pos = runtime_db.XN_TASKS.count(status="PENDING")
        return (f"🐦 已入队（第 {pos} 位）：{label}\n"
                "串行执行（gallery-dl 限速防风控），完成会通知；"
                "增量去重——已在 archive 的推文不重下。进度 /x 查看")
    row = runtime_db.XN_TASKS.get(key) or {}
    st, rid = row.get("status", "?"), row.get("id", "?")
    if st == "FAILED":
        return f"⏭️ 该目标已在队列且失败过（#{rid}）——/x retry {rid} 重投"
    return f"⏭️ 该目标已在队列中（#{rid}，{st}）——跑完自动继续下一条"


async def auto_reply(text):
    """收藏夹直接发 X 链接（无 /x 前缀）→ 自动入队（worker 串行）。"""
    return "🔗 已自动识别 X 链接（/x 可省略）\n" + _submit(text)


def status_text():
    """进度（内存视图）+ 队列（DB 真相源）合一状态。"""
    head = ""
    if _X_RUNNING:
        a = _X_RUNNING.get("author", "?")
        done = _X_RUNNING.get("done", 0)
        started = _X_RUNNING.get("started")
        elapsed = ""
        if started:
            m, s = divmod(int(time.time() - started), 60)
            elapsed = f"｜已跑 {m}分{s:02d}秒"
        head = f"🐦 X 下载进行中：{a}｜本次已收 {done} 个{elapsed}"
        # a 可能带 @（worker label）也可能不带（run_author 内部直写）
        if _X_RUNNING.get("retry_wait"):
            head += (f"\n⏳ 上轮失败，{_X_RUNNING['retry_wait']}s 后自动重跑"
                     f"（第 {_X_RUNNING.get('attempt', '?')} 次）")
        last = _X_RUNNING.get("last", "")
        if last:
            head += f"\n最新：{last}"
    rows = _WORKER.queue_rows()
    if not rows and not head:
        return ("🐦 /x <@作者|作者页|推文链接> —— 下载该作者全部作品"
                "（图+视频，Agent Chrome 登录 X 后可用）\n"
                "🐦 直接发 x.com/twitter.com 链接也自动识别\n"
                "⏳ 队列：空")
    from collections import Counter
    counts = Counter(r["status"] for r in rows)
    summary = " ".join(f"{s}={n}" for s, n in counts.items())
    lines = ([head] if head else []) + [f"⏳ 队列（{summary}，最近 "
                                        f"{len(rows)} 条）："]
    for r in rows:
        name = (r.get("label") or r["target"])[:44]
        err = f"｜{str(r['error'])[:24]}" if r.get("error") else ""
        lines.append(f"  #{r['id']} [{r['status']}] {name}{err}")
    lines.append("操作：/x stop 停当前｜/x retry <id|all>｜/x del <id>")
    return "\n".join(lines)[:3900]


def pct_text(running):
    return f"{running.get('done', 0)} 个"


async def command_reply(cmd_text):
    """/x [@作者|推文链接]；裸命令看状态；子命令 stop/retry/del。"""
    parts = str(cmd_text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if not arg:
        return status_text()
    head = arg.split()[0].lower()
    if head == "stop":
        return x_stop()
    if head == "retry":
        toks = arg.split()
        target = toks[1] if len(toks) > 1 else ""
        if target == "all":
            n = runtime_db.XN_TASKS.retry()
            return f"🐦 已重投 {n} 条失败任务"
        if target.isdigit():
            n = runtime_db.XN_TASKS.retry_by_id(int(target))
            return (f"🐦 已重投 #{target}" if n
                    else f"ℹ️ #{target} 不是可重投的失败任务")
        return "🐦 用法：/x retry <id|all>（id 看 /x 队列列表）"
    if head == "del":
        toks = arg.split()
        target = toks[1] if len(toks) > 1 else ""
        if target.isdigit():
            runtime_db.XN_TASKS.delete(int(target))
            return f"🐦 🗑 已移除 #{target}"
        return "🐦 用法：/x del <id>"
    return _submit(arg)


def is_x_command(cmd_text):
    head = str(cmd_text or "").split()[0] if str(cmd_text or "").strip() else ""
    return head.lower() == "/x"
