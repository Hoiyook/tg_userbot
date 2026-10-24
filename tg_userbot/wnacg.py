"""wnacg 漫画下载（/wnacg）——发送地址 → 解析 → 下载 zip → 115 → 解压管线。

2026-10-24 实测定稿的链路（别按直觉改）：

    /wnacg https://www.wnacg.com/photos-index-aid-398337.html
      ↓ aid=398337（photos/download/裸数字 三种形态都认）
    GET download-index-aid-398337.html（**必须走 TG_PROXY 同款代理**——
      HTML 站国内直连超时；页面本身就是答案：h1 = 标题.zip、按钮 href =
      CDN 直链，无需浏览器）
    GET //dl1.wn01.download/down/…zip（**必须直连**——走代理反而不通；
      支持 Range 断点续传，.part 恢复）
    拷贝进挂载 云下载/<标题>.zip（CD2 异步上传 115）→ gRPC 轮询验大小
      → enqueue_extract_tasks 进 /115x 管线（解压 → 回传 → 对账 → 通知）

纪律：站点明示「每日限額下載」——串行一次一本 + 节流
WNACG_THROTTLE_SECONDS；aid 主键判重（重复发链接直接回执已有）；
下载 403/404 时先重拉下载页换新直链再计失败（直链带哈希基本长效，
但每日限额可能以 403 呈现——重试退避）。
"""
import asyncio
import os
import re
import time
from urllib.parse import unquote

import httpx

from . import config
from . import runtime_db
from .log import logger
from .naming import format_size, sanitize_filename

TEXT_PREFIX = "📖"
_STOP = {"requested": False}
_PAUSED = {"paused": False}
_AID_RE = re.compile(r"aid-(\d+)")
_ZIP_RE = re.compile(r"(//[a-z0-9.]+/down/[a-z0-9/]+\.zip)")
_H1_RE = re.compile(r"<h1[^>]*>([^<]{1,300})</h1>")
# 瞬时故障标记（不占尝试上限，退避无限重试——与 /115x 同语义）
_TRANSIENT_MARKS = ("下载页请求失败", "ConnectError", "ReadError",
                    "Timeout", "挂载不可用", "上传超时",
                    "not found under")


# ============================================================
# 解析（纯函数）
# ============================================================
def parse_wnacg_ref(text):
    """任意 wnacg 形态 → aid（int）；认不出返回 None。

    支持完整 URL（photos-index / download-index / aid-N）、裸数字。
    """
    q = str(text or "").strip()
    if not q:
        return None
    if q.isdigit():
        return int(q)
    if "wnacg" not in q:
        return None
    m = _AID_RE.search(q)
    return int(m.group(1)) if m else None


def parse_download_page(html):
    """下载页 HTML → (title, zip_url)；解析不出返回 (None, None)。

    页面事实（2026-10-24 实测）：``<h1>标题.zip</h1>`` 与按钮
    ``//dl1.wn01.download/down/…/哈希.zip?n=标题``。
    """
    m = _ZIP_RE.search(html or "")
    zip_url = ("https:" + m.group(1)) if m else None
    h = _H1_RE.search(html or "")
    title = None
    if h:
        title = h.group(1).strip()
        if title.lower().endswith(".zip"):
            title = title[:-4].strip()
        title = unquote(title)
    if not title:
        # <title> 兜底：「标题.zip 下載 - 紳士漫畫…」取前段
        m2 = re.search(r"<title>([^<]{1,300})</title>", html or "")
        if m2:
            t = m2.group(1).split(" 下載")[0].strip()
            if t.lower().endswith(".zip"):
                t = t[:-4].strip()
            if t:
                title = unquote(t)
    return title, zip_url


def _proxy_url():
    """HTML 站用的代理（TG_PROXY 同款，run.sh 注入）；无代理返回 None。"""
    return (os.environ.get("TG_PROXY") or "").strip() or None


# ============================================================
# 网络步骤
# ============================================================
def fetch_album_info_blocking(aid, retries=3):
    """拉下载页解析标题与直链（阻塞，放线程跑）。失败抛 RuntimeError。

    网络类瞬时错误（代理节点 TLS 抖动——2026-10-24 实测 ConnectError:
    EOF 连续出现）内部重试 3 次（5s 退避），单次抖动不再烧任务尝试
    额度；404/解析失败不重试（永久事实）。"""
    url = f"{config.WNACG_BASE}/download-index-aid-{int(aid)}.html"
    proxy = _proxy_url()
    if proxy is None:
        raise RuntimeError("未配置 TG_PROXY——wnacg HTML 站必须走代理")
    last = None
    for attempt in range(retries):
        try:
            r = httpx.get(url, proxy=proxy, trust_env=False,
                          headers={"User-Agent": config.WNACG_UA},
                          timeout=float(config.WNACG_FETCH_TIMEOUT_SECONDS),
                          follow_redirects=True)
        except Exception as e:
            last = f"下载页请求失败：{type(e).__name__}: {e}"
            if attempt < retries - 1:
                time.sleep(5)
            continue
        if r.status_code == 404:
            raise RuntimeError(f"aid {aid} 不存在（404）")
        if r.status_code != 200:
            raise RuntimeError(f"下载页 HTTP {r.status_code}")
        title, zip_url = parse_download_page(r.text)
        if not zip_url:
            raise RuntimeError("下载页解析不到直链（改版或限额提示页？）")
        return {"title": title or f"aid_{aid}", "zip_url": zip_url,
                "filename": sanitize_filename(title or f"aid_{aid}") + ".zip"}
    raise RuntimeError(last or "下载页请求失败：未知错误")


def download_zip_blocking(zip_url, dest, expected=None):
    """直连下载 zip 到 dest（.part 断点续传）。返回落盘大小；失败抛错。"""
    part = dest + ".part"
    headers = {"User-Agent": config.WNACG_UA}
    offset = os.path.getsize(part) if os.path.isfile(part) else 0
    if offset:
        headers["Range"] = f"bytes={offset}-"
    total = None
    with httpx.Client(trust_env=False, timeout=httpx.Timeout(60.0),
                      follow_redirects=True) as cli:
        with cli.stream("GET", zip_url, headers=headers) as resp:
            if resp.status_code in (403, 404):
                raise RuntimeError(f"CDN HTTP {resp.status_code}（限额或直链失效）")
            resp.raise_for_status()
            if offset and resp.status_code != 206:
                offset = 0   # 服务端不支持续传，从头
            length = resp.headers.get("Content-Length")
            total = offset + (int(length) if length else 0)
            mode = "ab" if offset else "wb"
            with open(part, mode) as f:
                for chunk in resp.iter_bytes(1024 * 512):
                    f.write(chunk)
    size = os.path.getsize(part)
    if total and size != total:
        raise RuntimeError(f"短读：{size} / {total} 字节，重试续传")
    if expected is not None and size != int(expected):
        raise RuntimeError(f"大小不符：{size} ≠ 页面声明 {expected}")
    os.replace(part, dest)
    return size


# ============================================================
# 115 落盘 + 解压入队
# ============================================================
def _remote_dir_for(task):
    """任务的目标 115 目录：漫画/ 或 漫画/<作者>/。"""
    base = config.WNACG_REMOTE_DIR
    sub = (task.get("subdir") or "").strip()
    return f"{base}/{sub}" if sub else base


def _remote_size_blocking(name, remote_dir):
    """gRPC 列目录找文件尺寸；目录不存在/不可读一律 None（= 文件不在）。

    新作者子目录首跑会踩「find by path not found」的 gRPC 异常（目录
    尚未建/CD2 尚未同步），吞掉按「不在」处理——轮询自会等到它出现。"""
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


async def _wait_remote_size(name, expect, remote_dir, tries=120, gap=15.0):
    """等 CD2 把挂载写入传到 115（gRPC 轮询对尺寸）。None=超时。"""
    for i in range(tries):
        size = await asyncio.to_thread(_remote_size_blocking, name, remote_dir)
        if size is not None and size == int(expect):
            return size
        if i % 8 == 7:
            logger.info(f"📖 等待 115 上传完成：{name}（第 {i + 1} 轮）")
        await asyncio.sleep(gap)
    return None


async def _process_task(task):
    """处理一本：解析（若缺直链）→ 下载 → 落 115 → 解压入队。"""
    aid = task["aid"]
    # ① 解析（zip_url 空或下载报 403/404 时重拉下载页换新链）
    info = None
    if task.get("zip_url"):
        info = {"title": task.get("title"), "zip_url": task["zip_url"],
                "filename": task.get("filename")}
    if info is None:
        info = await asyncio.to_thread(fetch_album_info_blocking, aid)
        runtime_db.update_wnacg_task(
            aid, title=info["title"], zip_url=info["zip_url"],
            filename=info["filename"])
        task.update(info)

    # ② 下载到本地暂存
    staging = os.path.join(config.RUNTIME_DIR, "wnacg")
    os.makedirs(staging, exist_ok=True)
    local = os.path.join(staging, task["filename"] or f"aid_{aid}.zip")
    try:
        size = await asyncio.to_thread(
            download_zip_blocking, info["zip_url"], local)
    except RuntimeError as e:
        if "403" in str(e) or "404" in str(e):
            # 直链可能过期/限额：换新链再试一次
            info = await asyncio.to_thread(fetch_album_info_blocking, aid)
            runtime_db.update_wnacg_task(aid, zip_url=info["zip_url"])
            size = await asyncio.to_thread(
                download_zip_blocking, info["zip_url"], local)
        else:
            raise
    runtime_db.update_wnacg_task(aid, size_bytes=size)

    # ③ 落 115：同名同尺寸已在 → 跳拷贝；否则写挂载等 CD2 上传
    from .extract_worker import mount_of
    remote_dir = _remote_dir_for(task)
    mount_dir = mount_of(remote_dir)
    if mount_dir is None:
        raise RuntimeError(f"挂载不可用：{remote_dir}"
                           "（CD2/挂载死了，稍后自动重试）")
    # 作者子目录可能不存在：挂载 mkdir 即 CD2 建远端目录——必须先建，
    # 后续 gRPC 列目录/上传对账才有处可查（首跑实测教训）
    os.makedirs(mount_dir, exist_ok=True)
    remote_now = await asyncio.to_thread(
        _remote_size_blocking, task["filename"], remote_dir)
    if remote_now != size:
        target = os.path.join(mount_dir, task["filename"])
        await asyncio.to_thread(_copy_with_progress, local, target)
        got = await _wait_remote_size(task["filename"], size, remote_dir)
        if got is None:
            raise RuntimeError("115 上传超时（CD2 未在时限内传完），稍后重试")
    try:
        os.remove(local)
    except OSError:
        pass

    # ④ 进 /115x 解压管线（解压 → 回传 → 对账 → 通知全复用）
    ins, _skip = runtime_db.enqueue_extract_tasks(
        remote_dir, [(task["filename"], size)])
    if not ins:
        logger.info(f"📖 [aid {aid}] 解压任务已存在（幂等跳过）")
    return {"size": size, "title": task.get("title") or info["title"]}


def _copy_with_progress(src, dst):
    """阻塞拷贝（线程跑）。"""
    done = 0
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            b = fi.read(4 * 1024 * 1024)
            if not b:
                break
            fo.write(b)
            done += len(b)


# ============================================================
# Worker
# ============================================================
async def wnacg_worker_loop():
    """常驻循环：串行一次一本，节流间隔，任何一轮异常吞掉不退出。"""
    logger.info("📖 wnacg worker 已启动（串行一次一本，"
                f"节流 {config.WNACG_THROTTLE_SECONDS}s，落 {config.WNACG_REMOTE_DIR}）")
    try:
        runtime_db.recover_expired_wnacg_tasks()
    except Exception as e:
        logger.warning(f"📖 wnacg 任务租约恢复失败：{e}")
    while not _STOP["requested"]:
        try:
            if _PAUSED["paused"]:
                await asyncio.sleep(5.0)
                continue
            task = runtime_db.claim_next_wnacg_task()
            if task is None:
                await asyncio.sleep(5.0)
                continue
            await _run_one(task)
            await asyncio.sleep(float(config.WNACG_THROTTLE_SECONDS))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"📖 wnacg worker 轮询异常：{e}")
            await asyncio.sleep(10.0)


async def _run_one(task):
    from . import notify
    aid = task["aid"]
    title = task.get("title") or f"aid_{aid}"
    logger.info(f"📖 [aid {aid}] 开始：{title}")
    try:
        result = await _process_task(task)
        runtime_db.complete_wnacg_task(aid, size_bytes=result["size"])
        logger.info(f"📖 [aid {aid}] 完成：{title}（{format_size(result['size'])}）"
                    "→ 已入解压管线")
        try:
            await notify.notify_user(
                f"📖 漫画已转 115 并入解压管线\n\n{title}\n"
                f"大小：{format_size(result['size'])}\n"
                f"位置：{_remote_dir_for(task)}/{task['filename']}\n"
                "解压回传的进度与结果由 115 解压管线另行通知。")
        except Exception:
            pass
    except asyncio.CancelledError:
        raise
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        if any(m in err for m in _TRANSIENT_MARKS):
            # 瞬时故障（代理抖动/挂载抖动）：不占尝试上限，退避无限重试
            delay = min(300 * (2 ** min(int(task.get("attempts") or 0), 4)), 7200)
            runtime_db.postpone_wnacg_task(aid, err, delay_seconds=delay)
            logger.warning(f"📖 [aid {aid}] 瞬时故障不占上限：{err[:80]}"
                           f"，{delay}s 后重试")
            return
        st = runtime_db.fail_wnacg_task(
            aid, err, max_attempts=int(getattr(config, "WNACG_MAX_ATTEMPTS", 5)))
        logger.warning(f"📖 [aid {aid}] 失败：{err}"
                       + ("（已达上限，/wnacg retry 可重投）"
                          if st == runtime_db.WNACG_FAILED else "，退避重试"))
        try:
            if st == runtime_db.WNACG_FAILED:
                await notify.notify_user(
                    f"📖 漫画下载失败（已达重试上限）\n\n{title}\n"
                    f"原因：{err[:200]}\n可用 /wnacg retry {aid} 重投。")
        except Exception:
            pass


def release_inflight():
    """优雅停机：在途任务放回 PENDING。"""
    pass   # 单任务同步短事务，租约 15 分钟自愈即可


# ============================================================
# 命令
# ============================================================
def _status_text():
    try:
        rows = runtime_db.list_wnacg_tasks(limit=15)
    except runtime_db.DbUnavailable as e:
        return f"{TEXT_PREFIX}\n❌ Runtime DB 不可用：{e}"
    if not rows:
        return (f"{TEXT_PREFIX}\n队列为空。发送漫画地址入队：\n"
                "  /wnacg https://www.wnacg.com/photos-index-aid-398337.html")
    from collections import Counter
    counts = Counter(r["status"] for r in rows)
    head = " ".join(f"{s}={n}" for s, n in counts.items())
    lines = [f"{TEXT_PREFIX} wnacg 队列（{head}，最近 15 条）", ""]
    for r in rows:
        size = f" {format_size(r['size_bytes'])}" if r.get("size_bytes") else ""
        err = f"｜{str(r['error'])[:28]}" if r.get("error") else ""
        sub = f"→{r['subdir']}/ " if r.get("subdir") else ""
        lines.append(f"  [{r['status']}] #{r['aid']} "
                     f"{(r['title'] or '')[:30]}{size} {sub}{err}")
    lines.append("\n/wnacg retry <aid|all>｜/wnacg del <aid>")
    return "\n".join(lines)[:3900]


def command_reply(cmd_text):
    """/wnacg 命令入口（同步返回文本）。"""
    raw = str(cmd_text or "").strip()
    # 剥命令头（/wnacg 或 /wnacg_xxx 形态），body 为空 = 状态视图
    parts = raw.split(None, 1)
    body = parts[1].strip() if len(parts) > 1 else ""
    low = body.lower()
    if not body:
        return _status_text()
    if low in ("stop", "start"):
        _PAUSED["paused"] = low == "stop"
        return (f"{TEXT_PREFIX} ⏸ 已暂停领取（在途跑完）" if low == "stop"
                else f"{TEXT_PREFIX} ▶️ 已恢复领取")
    parts = body.split()
    head, rest = parts[0], " ".join(parts[1:])
    if head.lower() == "retry":
        if not rest:
            return f"{TEXT_PREFIX} 用法：/wnacg retry <aid|all>"
        if rest.lower() == "all":
            n = runtime_db.retry_wnacg_task(None)
            return f"{TEXT_PREFIX} 🔁 重投 {n} 本 FAILED"
        if not rest.isdigit():
            return f"{TEXT_PREFIX} aid 须为数字"
        if runtime_db.retry_wnacg_task(int(rest)):
            return f"{TEXT_PREFIX} 🔁 #{rest} 已重投"
        return f"{TEXT_PREFIX} #{rest} 不在 FAILED 状态"
    if head.lower() == "del":
        if not rest.isdigit():
            return f"{TEXT_PREFIX} 用法：/wnacg del <aid>"
        runtime_db.delete_wnacg_task(int(rest))
        return f"{TEXT_PREFIX} 🗑 已移除 #{rest}"
    aid = parse_wnacg_ref(head)
    if aid is None:
        return (f"{TEXT_PREFIX} 认不出 wnacg 地址。支持：\n"
                "  photos-index-aid-N.html / download-index-aid-N.html / 裸 aid\n"
                "  可带作者：/wnacg <地址> <作者名> → 漫画/<作者>/")
    author = sanitize_filename(rest.strip())[:60] if rest.strip() else None
    existed = runtime_db.get_wnacg_task(aid)
    if existed:
        size = f"｜{format_size(existed['size_bytes'])}" if existed.get("size_bytes") else ""
        sub = f"/{existed['subdir']}" if existed.get("subdir") else ""
        return (f"{TEXT_PREFIX} #{aid} 已在队列（{existed['status']}{size}）"
                f"：{existed.get('title') or ''}"
                f"（→ 漫画{sub or ''}）"[:3900])
    if runtime_db.enqueue_wnacg_task(aid, subdir=author):
        where = f"漫画/{author}/" if author else "漫画/"
        return (f"{TEXT_PREFIX} 📥 已入队 #{aid} → {where}\n"
                "串行下载中（每本间隔 10s 防限额），完成后通知；"
                "标题稍后解析回填。")
    return f"{TEXT_PREFIX} #{aid} 入队失败（DB）"
