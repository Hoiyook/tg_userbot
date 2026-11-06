"""asmr-300 / Kikoeru 音声下载（/asmr）——链接入队 → 串行逐文件 → 115。

2026-10-26 实测定稿的链路（调研报告 docs/plan/ASMR300接入调研.md）：

    /asmr https://www.asmr-300.com/work/RJ01207846 [only 含SE]
      ↓ RJ 号（URL/RJ号/裸数字三形态；API 只认数字、有无前导零均可）
    GET {ASMR_API_BASE}/api/work/{rj}（元数据）+ /api/tracks/{rj}（文件树）
      ⚠ 必须走 TG_PROXY 同款代理（直连超时）；统一带 Accept-Language: zh-CN
      （www 拦 en 头出 403 反爬页；API 实测不拦，带上防镜像收紧）
      ⚠ API 域名可能换：tg_secrets.json 的 asmr.api_base / TG_ASMR_API 可覆盖
    逐文件 GET mediaDownloadUrl（raw CDN：直连可用、支持 Range 断点续传、
      **不支持 HEAD**——死链预检只能 Range GET；直链嵌首次入库日期，
      永远取 API 返回原文、绝不自己拼）
      ↓ 本地 staging（.part 续传）→ 按 API size 对账 → 拷进挂载
    /115open/声音/asmr/RJ号/<原目录树>/（盘上既有约定），CD2 后台上传 115

纪律：串行一次一部 + 文件间节流 ASMR_THROTTLE_SECONDS（社区公益库）；
RJ 主键判重（重复发链接直接回执已有）；幂等（挂载上同名同尺寸文件直接
跳过，重启/重投天然续跑）；长作品逐文件续租（单部可达数 GB，不续租会
被 /health 误报过期租约）。
"""
import asyncio
import os
import re
import time

import httpx

from . import config
from . import runtime_db
from .log import logger
from .naming import format_size, sanitize_filename

TEXT_PREFIX = "🎧"
_PAUSED = {"paused": False}
_CURRENT = {}   # /now 实时视图用：{"rj", "title", "stage"}
_RJ_RE = re.compile(r"RJ(\d+)", re.IGNORECASE)
# 瞬时故障标记（不占尝试上限，退避无限重试——与 /wnacg 同语义）
_TRANSIENT_MARKS = ("API 请求失败", "ConnectError", "ReadError",
                    "Timeout", "挂载不可用", "上传超时",
                    "not found under")


# ============================================================
# 解析（纯函数）
# ============================================================
def parse_asmr_ref(text):
    """任意形态 → RJ 数字号（int）；认不出返回 None。

    支持：作品页 URL（任意 Kikoeru 镜像域名的 /work/RJxxx）、裸 RJ号、
    裸数字（老 6 位/新 8 位都认，API 对有无前导零不敏感）。"""
    q = str(text or "").strip()
    if not q:
        return None
    if q.isdigit():
        return int(q)
    m = _RJ_RE.search(q)
    return int(m.group(1)) if m else None


def is_asmr_command(cmd_text):
    """/asmr 或 /asmr_<子命令>（/asmrfoo 不算，避免吞别的命令）。"""
    head = str(cmd_text or "").strip().split()[0] if str(
        cmd_text or "").strip() else ""
    return head.lower() == "/asmr" or head.lower().startswith("/asmr_")


def parse_asmr_command(cmd_text):
    """命令 → (action, arg)。action ∈ status/stop/start/retry/del/enqueue。

    兼容下划线标准形与空格形（入口规范化只改写空格形，/asmr_retry 直发
    也会进来——剥 /asmr 头后去前导下划线，与 wnacg 同法）。"""
    raw = str(cmd_text or "").strip()
    body = raw[len("/asmr"):].strip()
    if body.startswith("_"):
        body = body[1:]
    if not body:
        return "status", None
    low = body.lower()
    if low in ("stop", "start"):
        return low, None
    head, _, rest = body.partition(" ")
    if head.lower() == "retry":
        return "retry", rest.strip() or None
    if head.lower() == "del":
        return "del", rest.strip() or None
    return "enqueue", body


def parse_enqueue_body(body):
    """enqueue 正文 → (rj:int|None, only:str|None)。

    `only <关键词>` 取该词之后的全部文本（目录名可含空格/括号）；关键词
    对文件相对路径做忽略大小写子串匹配——多版本作品（无SE/含SE）只下一版
    就靠它。"""
    tokens = str(body or "").split()
    only = None
    for i, t in enumerate(tokens):
        if t.lower() == "only" and i + 1 < len(tokens):
            only = " ".join(tokens[i + 1:]).strip()
            tokens = tokens[:i]
            break
    return parse_asmr_ref(" ".join(tokens)), only


def flatten_tracks(nodes):
    """API 文件树 → 逐文件清单 [{path, size, url}]。

    path 逐段过 sanitize_filename（站点标题可能带文件系统非法字符）；
    没拿到 mediaDownloadUrl / 无文件名的节点跳过（宁少勿错，与站点
    deferred 附件同族的边界）。"""
    out = []

    def walk(items, prefix=""):
        for n in items or []:
            t = n.get("type")
            if t == "folder":
                seg = sanitize_filename(str(n.get("title") or "").strip())
                walk(n.get("children") or [], prefix + seg + "/")
                continue
            url = n.get("mediaDownloadUrl")
            title = str(n.get("title") or "").strip()
            if not url or not title or title in (".", ".."):
                continue
            out.append({
                "path": prefix + sanitize_filename(title),
                "size": int(n.get("size") or 0),
                "url": url,
            })

    walk(nodes)
    return out


def apply_only_filter(files, only):
    """only 关键词过滤（路径忽略大小写子串）；only 空返回原清单。"""
    if not only:
        return files
    low = str(only).lower()
    return [f for f in files if low in f["path"].lower()]


def _folder_name(rj, source_id=None):
    """115 目录名：用站点显示原样的 source_id（如 RJ01267237，与盘上既有
    RJ 前缀约定一致）；缺省回落 RJ+数字主键。目录名只影响落盘位置，
    判重/重投永远走 DB 的数字主键。"""
    sid = str(source_id or "").strip()
    if not sid:
        return f"RJ{int(rj)}"
    return sanitize_filename(sid) or f"RJ{int(rj)}"


# ============================================================
# 网络步骤
# ============================================================
def _proxy_url():
    """API 用的代理（TG_PROXY 同款，run.sh 注入）；无代理返回 None。"""
    return (os.environ.get("TG_PROXY") or "").strip() or None


def fetch_work_info_blocking(rj, retries=3):
    """元数据 + 文件树一次拉齐（阻塞，放线程跑）。失败抛 RuntimeError。

    网络类瞬时错误（代理抖动）内部重试 3 次；400/404（作品不在库）与
    空文件树是永久事实，不重试。"""
    proxy = _proxy_url()
    if proxy is None:
        raise RuntimeError("未配置 TG_PROXY——asmr API 必须走代理（直连超时）")
    headers = {"User-Agent": config.WNACG_UA,
               "Accept-Language": config.ASMR_ACCEPT_LANGUAGE}
    base = config.ASMR_API_BASE
    last = None
    for attempt in range(retries):
        try:
            with httpx.Client(proxy=proxy, trust_env=False,
                              timeout=float(config.ASMR_FETCH_TIMEOUT_SECONDS),
                              follow_redirects=True, headers=headers) as cli:
                r = cli.get(f"{base}/api/work/{int(rj)}")
                if r.status_code in (400, 404):
                    raise RuntimeError(
                        f"RJ{rj} 不在库中（API {r.status_code}）")
                r.raise_for_status()
                work = r.json()
                t = cli.get(f"{base}/api/tracks/{int(rj)}")
                if t.status_code in (400, 404):
                    raise RuntimeError(
                        f"RJ{rj} 文件树不可得（API {t.status_code}）")
                t.raise_for_status()
                tracks = t.json()
            files = flatten_tracks(tracks)
            if not files:
                raise RuntimeError("文件树为空（作品可能尚未入库文件）")
            return {
                "rj": int(rj),
                "title": str(work.get("title") or f"RJ{rj}"),
                "circle": str((work.get("circle") or {}).get("name") or ""),
                "source_id": str(work.get("source_id") or "").strip(),
                "files": files,
            }
        except RuntimeError:
            raise
        except Exception as e:
            last = f"API 请求失败：{type(e).__name__}: {e}"
            if attempt < retries - 1:
                time.sleep(5)
            continue
    raise RuntimeError(last or "API 请求失败：未知错误")


def download_file_blocking(url, dest, expected=None, attempts=3):
    """直连下载到 dest.part（Range 断点续传）→ 按 expected 对账 → rename。

    大小不符删除 .part 重来（对账基准是 API 的精确字节数；短读续传可能
    接不齐，从头下最稳）。CDN 不支持 HEAD，预检语义只能靠这里的 GET。
    停顿内建快速重试（2026-10-26 实测：Cloudflare 偶发首字节停顿，0 字节
    卡满读超时；换新连接立刻通）——重试间保留 .part 续传，403/404 是
    永久事实立即抛，不烧重试。"""
    part = dest + ".part"
    last_err = None
    for attempt in range(max(1, int(attempts))):
        try:
            _download_once(url, part)
            break
        except RuntimeError:
            raise          # 403/404：直链失效，永久事实
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < attempts - 1:
                time.sleep(5)
    size = os.path.getsize(part) if os.path.isfile(part) else -1
    if size < 0:
        raise RuntimeError(f"下载失败：{last_err or '无字节落盘'}")
    if expected and size != int(expected):
        try:
            os.remove(part)
        except OSError:
            pass
        raise RuntimeError(f"大小不符：{size} ≠ API 声明 {expected}")
    os.replace(part, dest)
    return size


def _download_once(url, part):
    """单次尝试：Range 续传写 part（异常原样上抛给调用方重试）。

    含**流速看门狗**（2026-10-26 生产事故：RJ1267237 的 CDN 连接涓流——
    每块间隔 <60s 读超时、但 21 分钟下不完一个 34MB 文件，串行 worker
    被占住几小时，租约过期触发卡死哨兵）。60 秒窗口不足 256KB
    （<4.4KB/s）即判死抛 TimeoutError（属瞬时标记，不烧尝试额度，
    .part 保留续传）。"""
    headers = {"User-Agent": config.WNACG_UA}
    offset = os.path.getsize(part) if os.path.isfile(part) else 0
    if offset:
        headers["Range"] = f"bytes={offset}-"
    window = float(getattr(config, "ASMR_STALL_WINDOW_SECONDS", 60))
    floor = int(getattr(config, "ASMR_STALL_FLOOR_BYTES", 256 * 1024))
    win_t0 = time.monotonic()
    win_bytes = 0
    with httpx.Client(trust_env=False,
                      timeout=httpx.Timeout(float(
                          config.ASMR_FILE_TIMEOUT_SECONDS)),
                      follow_redirects=True) as cli:
        with cli.stream("GET", url, headers=headers) as resp:
            if resp.status_code in (403, 404):
                raise RuntimeError(f"CDN HTTP {resp.status_code}（直链失效）")
            resp.raise_for_status()
            if offset and resp.status_code != 206:
                offset = 0   # 服务端不支持续传，从头
            mode = "ab" if offset else "wb"
            with open(part, mode) as f:
                for chunk in resp.iter_bytes(1024 * 512):
                    f.write(chunk)
                    win_bytes += len(chunk)
                    now = time.monotonic()
                    if now - win_t0 >= window:
                        if win_bytes < floor:
                            raise TimeoutError(
                                f"下载流速过低（{int(now - win_t0)}s 仅 "
                                f"{win_bytes} 字节，CDN 涓流/半死连接）")
                        win_t0 = now
                        win_bytes = 0


# ============================================================
# 115 落盘
# ============================================================
def _remote_size_blocking(name, remote_dir):
    """gRPC 列目录找文件尺寸；目录不存在/不可读一律 None（= 不在）。"""
    from . import cd2_api
    try:
        listing = cd2_api.list_remote_dir(remote_dir, limit=500)
    except Exception:
        return None
    if listing is None:
        return None
    for n, s, _d in listing:
        if n == name:
            return int(s)
    return None


async def _wait_remote_file(name, expect, remote_dir):
    """收尾等 CD2 把最后一个文件传上 115（best-effort，超时不算失败——
    挂载写入已落、CD2 后台队列自会传完）。"""
    for i in range(int(config.ASMR_UPLOAD_WAIT_TRIES)):
        size = await asyncio.to_thread(_remote_size_blocking, name, remote_dir)
        if size is not None and size == int(expect):
            return True
        if i % 8 == 7:
            logger.info(f"🎧 等 115 后台上传：{name}（第 {i + 1} 轮）")
        await asyncio.sleep(float(config.ASMR_UPLOAD_WAIT_GAP_SECONDS))
    return False


def _copy_file(src, dst):
    """阻塞拷贝（线程跑）。"""
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            b = fi.read(4 * 1024 * 1024)
            if not b:
                break
            fo.write(b)


async def _process_task(task):
    """处理一部：拉文件树 → 逐文件下载（幂等跳过已就位）→ 拷挂载 → 续租。"""
    rj = task["rj"]
    only = (task.get("only_filter") or "").strip() or None
    info = await asyncio.to_thread(fetch_work_info_blocking, rj)
    files = apply_only_filter(info["files"], only)
    if not files:
        raise RuntimeError(f"only「{only}」没有命中任何文件路径")
    total = sum(f["size"] for f in files)
    runtime_db.update_asmr_task(rj, title=info["title"], circle=info["circle"],
                                total_files=len(files), size_bytes=total)

    from .extract_worker import mount_of
    folder = _folder_name(rj, info.get("source_id"))
    remote_dir = f"{config.ASMR_REMOTE_DIR}/{folder}"
    mount_dir = mount_of(remote_dir)
    if mount_dir is None:
        raise RuntimeError(f"挂载不可用：{remote_dir}"
                           "（CD2/挂载死了，稍后自动重试）")
    # 目录先建再写：CD2 的挂载 makedirs 即建远端目录，后续 gRPC 对账
    # 才有处可查（wnacg 首跑同款坑）
    os.makedirs(mount_dir, exist_ok=True)
    staging = os.path.join(config.RUNTIME_DIR, "asmr", str(rj))
    # 看门狗双层防线（2026-10-26 生产事故）：download_with_watchdog 兜
    # 同步层挂死（DNS/连接零字节零超时，.part 冻结 13 分钟）；copy_
    # with_watchdog 兜 FUSE 冻结——协程侧监控文件增长，弃线程转重试
    from .task_worker import copy_with_watchdog, download_with_watchdog

    done = 0
    for f in files:
        target = os.path.join(mount_dir, f["path"])
        if os.path.isfile(target) and os.path.getsize(target) == f["size"]:
            done += 1
            continue   # 幂等：重启/重投续跑，已就位的文件不重下
        os.makedirs(os.path.dirname(target), exist_ok=True)
        local = os.path.join(staging, f["path"])
        os.makedirs(os.path.dirname(local), exist_ok=True)
        _CURRENT.update({"rj": rj, "title": info["title"], "stage":
                         f"{done + 1}/{len(files)}"
                         f" {os.path.basename(f['path'])[:32]}"})
        await download_with_watchdog(
            f["url"], local, f["size"],
            check=float(getattr(config, "ASMR_DL_CHECK_SECONDS", 30)),
            stall=float(getattr(config, "ASMR_DL_STALL_SECONDS", 240)),
            download_fn=download_file_blocking, label=f"🎧 RJ{rj}")
        await copy_with_watchdog(
            local, target, f["size"],
            check=float(getattr(config, "ASMR_COPY_CHECK_SECONDS", 60)),
            stall=float(getattr(config, "ASMR_COPY_STALL_SECONDS", 900)),
            copy_fn=_copy_file, label=f"🎧 RJ{rj}")
        try:
            os.remove(local)
        except OSError:
            pass
        done += 1
        runtime_db.update_asmr_task(rj, done_files=done)
        try:
            runtime_db.renew_asmr_lease(rj)   # 长作品逐文件续租
        except Exception:
            pass
        await asyncio.sleep(float(config.ASMR_THROTTLE_SECONDS))

    # 收尾 best-effort 等最后一个文件上云（其余文件 CD2 队列自会传完）
    last = files[-1]
    last_dir = f"{remote_dir}/{os.path.dirname(last['path'])}".rstrip("/")
    uploaded = await _wait_remote_file(os.path.basename(last["path"]),
                                       last["size"], last_dir)
    return {"files": done, "total": len(files), "size": total,
            "title": info["title"], "circle": info["circle"],
            "remote_dir": remote_dir, "uploaded": uploaded}


# ============================================================
# Worker
# ============================================================
async def asmr_worker_loop():
    """常驻循环（SerialTaskWorker 框架实例；保留函数名兼容 app.py）。"""
    await _WORKER.run()


async def _run_one(task):
    from . import notify
    rj = task["rj"]
    title = task.get("title") or f"RJ{rj}"
    logger.info(f"🎧 [RJ{rj}] 开始：{title}")
    try:
        _CURRENT.update({"rj": rj, "title": title, "stage": "解析文件树"})
        result = await _process_task(task)
        runtime_db.complete_asmr_task(rj, size_bytes=result["size"],
                                      total_files=result["total"])
        from .history import append_history
        append_history(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} | asmr | "
            f"{title}（{result['files']} 文件）| "
            f"{format_size(result['size'])} | 来源：asmr下载")
        up_note = "" if result["uploaded"] else \
            "（最后一个文件仍在 CD2 后台上传队列，稍后自会传完）"
        logger.info(f"🎧 [RJ{rj}] 完成：{title}"
                    f"（{result['files']} 文件 / {format_size(result['size'])}）")
        try:
            await notify.notify_user(
                f"🎧 ASMR 作品已下载完成{up_note}\n\n"
                f"{result['title']}\n"
                + (f"社团：{result['circle']}\n" if result["circle"] else "")
                + f"文件：{result['files']} 个｜{format_size(result['size'])}\n"
                f"位置：{result['remote_dir']}/")
        except Exception:
            pass
    except asyncio.CancelledError:
        # 优雅停机取消：立即放回 PENDING。本地 .part 续传保留，下轮续跑。
        try:
            runtime_db.release_asmr_task(rj)
        except Exception as e:
            logger.warning(f"🎧 [RJ{rj}] 放回 PENDING 失败（转租约自愈）：{e}")
        raise
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        if any(m in err for m in _TRANSIENT_MARKS):
            # 瞬时故障（代理抖动/挂载抖动）：不占尝试上限，退避无限重试
            delay = min(300 * (2 ** min(int(task.get("attempts") or 0), 4)), 7200)
            runtime_db.postpone_asmr_task(rj, err, delay_seconds=delay)
            logger.warning(f"🎧 [RJ{rj}] 瞬时故障不占上限：{err[:80]}"
                           f"，{delay}s 后重试")
            return
        st = runtime_db.fail_asmr_task(
            rj, err, max_attempts=int(getattr(config, "ASMR_MAX_ATTEMPTS", 5)))
        logger.warning(f"🎧 [RJ{rj}] 失败：{err}"
                       + ("（已达上限，/asmr retry 可重投）"
                          if st == runtime_db.ASMR_FAILED else "，退避重试"))
        try:
            if st == runtime_db.ASMR_FAILED:
                await notify.notify_user(
                    f"🎧 ASMR 下载失败（已达重试上限）\n\n{title}\n"
                    f"原因：{err[:200]}\n可用 /asmr retry {rj} 重投。")
        except Exception:
            pass
    finally:
        _CURRENT.clear()


def release_inflight():
    """优雅停机：在途任务放回 PENDING（框架兜底——取消路径已先放过）。"""
    return _WORKER.release_inflight()


# ── SerialTaskWorker 框架接线（2026-10-26 评审迁移：取消放回 +
# 周期租约回收此前缺失——挂死任务要等进程重启才回 PENDING）──
from .task_worker import SerialTaskWorker   # noqa: E402


class _AsmrWorker(SerialTaskWorker):
    name = "🎧 asmr"
    poll_seconds = 5.0
    # 节流在文件级（ASMR_THROTTLE_SECONDS），任务级不另加间隔

    def __init__(self):
        super().__init__()
        # 周期租约回收（框架调度）：挂死/SIGKILL 遗留的 PROCESSING 回 PENDING
        self.recover_seconds = float(getattr(
            config, "ASMR_RECOVER_SECONDS", 600))

    def claim(self):
        return runtime_db.claim_next_asmr_task()

    async def process(self, task):
        await _run_one(task)

    def release(self, task):
        return runtime_db.release_asmr_task(task["rj"])

    def recover_startup(self):
        runtime_db.recover_expired_asmr_tasks()

    def recover_periodic(self):
        runtime_db.recover_expired_asmr_tasks()

    def paused(self):
        return _PAUSED["paused"]

    def startup_banner(self):
        return (f"🎧 asmr worker 已启动（串行一次一部，"
                f"文件节流 {config.ASMR_THROTTLE_SECONDS}s，"
                f"API {config.ASMR_API_BASE}，"
                f"落 {config.ASMR_REMOTE_DIR}）")


_WORKER = _AsmrWorker()


# ============================================================
# 命令
# ============================================================
def _status_text():
    try:
        rows = runtime_db.list_asmr_tasks(limit=15)
    except runtime_db.DbUnavailable as e:
        return f"{TEXT_PREFIX}\n❌ Runtime DB 不可用：{e}"
    if not rows:
        return (f"{TEXT_PREFIX}\n队列为空。发送作品页链接入队：\n"
                "  /asmr https://www.asmr-300.com/work/RJ01207846 [only 含SE]")
    from collections import Counter
    counts = Counter(r["status"] for r in rows)
    head = " ".join(f"{s}={n}" for s, n in counts.items())
    lines = [f"{TEXT_PREFIX} asmr 队列（{head}，最近 15 条）", ""]
    for r in rows:
        size = f" {format_size(r['size_bytes'])}" if r.get("size_bytes") else ""
        prog = (f"｜{r['done_files']}/{r['total_files']}"
                if r.get("total_files") else "")
        only = (f"｜only:{r['only_filter']}"
                if r.get("only_filter") else "")
        err = f"｜{str(r['error'])[:28]}" if r.get("error") else ""
        lines.append(f"  [{r['status']}] #{r['rj']} "
                     f"{(r['title'] or '')[:30]}{size}{prog}{only}{err}")
    lines.append("\n/asmr retry <RJ号|all>｜/asmr del <RJ号>｜"
                 "/asmr stop 暂停｜/asmr start 恢复")
    return "\n".join(lines)[:3900]


async def command_reply(cmd_text):
    """/asmr 命令入口。enqueue 需拉一次 API（数秒）后回执。"""
    action, arg = parse_asmr_command(cmd_text)
    if action == "status":
        return _status_text()
    if action == "stop":
        _PAUSED["paused"] = True
        logger.info("🎧 asmr worker 暂停领取（/asmr stop）")
        return f"{TEXT_PREFIX} ⏸ 已暂停领取（在途作品会跑完）。恢复：/asmr start"
    if action == "start":
        _PAUSED["paused"] = False
        logger.info("🎧 asmr worker 恢复领取（/asmr start）")
        return f"{TEXT_PREFIX} ▶️ 已恢复领取。"
    if action == "retry":
        if not arg:
            return f"{TEXT_PREFIX} 用法：/asmr retry <RJ号|all>"
        if arg.lower() == "all":
            n = runtime_db.retry_asmr_task(None)
            return f"{TEXT_PREFIX} 🔁 重投 {n} 部 FAILED"
        if not arg.lstrip("RJrj").isdigit():
            return f"{TEXT_PREFIX} RJ号须为数字（如 /asmr retry 1207846）"
        rj = parse_asmr_ref(arg)
        if rj is None:
            return f"{TEXT_PREFIX} RJ号须为数字（如 /asmr retry 1207846）"
        if runtime_db.retry_asmr_task(rj):
            return f"{TEXT_PREFIX} 🔁 #{rj} 已重投"
        return f"{TEXT_PREFIX} #{rj} 不在 FAILED 状态"
    if action == "del":
        if not arg or not arg.lstrip("RJrj").isdigit():
            return f"{TEXT_PREFIX} 用法：/asmr del <RJ号>"
        rj = parse_asmr_ref(arg)
        if rj is None:
            return f"{TEXT_PREFIX} 用法：/asmr del <RJ号>"
        runtime_db.delete_asmr_task(rj)
        return f"{TEXT_PREFIX} 🗑 已移除 #{rj}"

    # enqueue：拉元数据+文件树（数秒）→ 入库 → 回执摘要
    rj, only = parse_enqueue_body(arg)
    if rj is None:
        return (f"{TEXT_PREFIX} 认不出作品号。支持：\n"
                "  作品页链接（/work/RJxxxxxxx）/ 裸 RJ号 / 数字 id\n"
                "  多版本作品可加 only 过滤：/asmr RJ01207846 only 含SE")
    existed = runtime_db.get_asmr_task(rj)
    if existed:
        size = (f"｜{format_size(existed['size_bytes'])}"
                if existed.get("size_bytes") else "")
        only_s = f"｜only:{existed['only_filter']}" if existed.get(
            "only_filter") else ""
        return (f"{TEXT_PREFIX} #{rj} 已在队列（{existed['status']}{size}"
                f"{only_s}）：{existed.get('title') or ''}"[:3900])
    try:
        info = await asyncio.to_thread(fetch_work_info_blocking, rj)
    except RuntimeError as e:
        return f"{TEXT_PREFIX} ❌ {e}"
    files = apply_only_filter(info["files"], only)
    if not files:
        return (f"{TEXT_PREFIX} ❌ only「{only}」没有命中任何文件路径"
                f"（对照作品目录名，如 /asmr RJ{rj} only wav(含SE)）")
    total = sum(f["size"] for f in files)
    if not runtime_db.enqueue_asmr_task(
            rj, title=info["title"], circle=info["circle"],
            only_filter=only, total_files=len(files), size_bytes=total):
        return (f"{TEXT_PREFIX} #{rj} 已在队列"
                f"：{runtime_db.get_asmr_task(rj).get('title') or ''}")
    where = (f"{config.ASMR_REMOTE_DIR}/"
             f"{_folder_name(rj, info.get('source_id'))}/")
    return (f"{TEXT_PREFIX} 📥 已入队 #{rj} → {where}\n"
            f"{info['title']}\n"
            + (f"社团：{info['circle']}\n" if info["circle"] else "")
            + f"文件：{len(files)} 个｜{format_size(total)}"
            + (f"｜only:{only}" if only else "")
            + "\n串行下载中（文件间隔 "
              f"{config.ASMR_THROTTLE_SECONDS:.0f}s），完成后通知。")
