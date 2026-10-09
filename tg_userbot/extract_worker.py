"""115 解压回传 worker：claim → COPY → EXTRACT → UPLOAD → VERIFY。

流程（任务书 docs/plan/Userbot_115解压回传任务书.md §1）：

1. COPY    挂载读远端压缩包到本地 staging（已有同名同尺寸 → 跳过，幂等）；
2. EXTRACT extract_util.extract_archive（加密/炸弹/空间守卫，串行线程）；
3. UPLOAD  staging 产物逐文件写回挂载 <原目录>/<包名>/（已存在同尺寸跳过）；
4. VERIFY  gRPC 逐父目录尺寸对账 + CD2 上传任务清空——只信对账，不信
   「写入返回」（CD2 写挂载是异步上传，写完 ≠ 传完）。

数据来源全部走 FUSE 挂载（/Volumes/CloudDrive = 115 根），gRPC 只做对账。
串行处理一次一包；租约 + 退避重试 + TERMINAL 终态（需密码/炸弹/损坏）。
"""
import asyncio
import os
import re
import time

from . import config
from . import notify
from . import runtime_db
from . import state
from .extract_util import extract_archive
from .log import logger

# 支持的压缩包扩展名（bsdtar/libarchive 可读集合的常用子集）
ARCHIVE_EXTS = (".zip", ".rar", ".7z", ".tar", ".tgz", ".tar.gz",
                ".tar.bz2", ".tar.xz", ".bz2", ".xz")

# worker 停止请求（/115x stop 置位；串行循环每轮检查）
_STOP = {"requested": False}

# 挂载/磁盘不可用时的告警节流（每轮 tick 都来会刷屏）
_WARN_AT = {"mount": 0.0, "disk": 0.0}
_WARN_INTERVAL = 600.0


def is_archive_name(name):
    low = str(name or "").lower()
    return any(low.endswith(ext) for ext in ARCHIVE_EXTS)


def mount_of(remote_dir):
    """gRPC 路径 → 挂载路径；不在 /115open/ 下返回 None。"""
    d = str(remote_dir or "")
    if not d.startswith("/115open/"):
        return None
    rel = d[len("/115open"):].strip("/")
    if not rel or rel.startswith(".."):
        return None
    return os.path.join(config.CLOUD_MOUNT_BASE, rel)


def remote_of(mount_path):
    """挂载路径 → gRPC 路径（对账用）。"""
    rel = os.path.relpath(mount_path, config.CLOUD_MOUNT_BASE)
    return "/115open/" + rel.replace(os.sep, "/")


def _throttled_warn(kind, message):
    now = time.monotonic()
    if now - _WARN_AT.get(kind, 0.0) >= _WARN_INTERVAL:
        _WARN_AT[kind] = now
        logger.warning(message)


def _backoff_delay(attempts):
    """退避秒数：base × 2^(attempts-1)，封顶 max。"""
    base = config.EXTRACT_BACKOFF_BASE_SECONDS
    return min(base * (2 ** max(0, attempts - 1)),
               config.EXTRACT_BACKOFF_MAX_SECONDS)


def request_stop():
    """请求 worker 停止（/115x stop）；返回是否确有任务在跑。"""
    _STOP["requested"] = True
    return bool(_CURRENT.get("task_id"))


# 当前在途任务（/115x stop 回执与状态视图用）
_CURRENT = {"task_id": None, "label": None, "stage": None}
_WATCH_TASKS = set()


async def _renew_loop(task_id):
    """租约续期循环：处理期间每 EXTRACT_LEASE_RENEW 秒续一次。"""
    interval = int(getattr(config, "PAWCHIVE_LEASE_RENEW_SECONDS", 60))
    try:
        while True:
            await asyncio.sleep(interval)
            runtime_db.renew_extract_lease(task_id)
    except asyncio.CancelledError:
        raise


def _copy_file(src, dst, progress=None, chunk=1024 * 1024, expected=None):
    """分块拷贝（进度回调每块触发；阻塞，放线程）。返回写入字节数。

    expected 给定时校验总字节数：挂载源在 CD2 重负载下可能提前吐 EOF
    （2026-10-03 生产实测：21GB 包只拷出 122 字节还「成功」）——短读
    一律抛错重拷，绝不把残缺副本送进解压。
    """
    done = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            data = fin.read(chunk)
            if not data:
                break
            fout.write(data)
            done += len(data)
            if progress:
                progress(done)
    if expected is not None and done != int(expected):
        raise IOError(
            f"拷贝短读：{done} / {expected} 字节（挂载源提前 EOF），"
            "将重拷")
    return done


async def _process_task(task):
    """处理单包。永不抛异常（内部把一切异常归到失败/终结路径）。"""
    tid = task["id"]
    name = task["archive_name"]
    mount_dir = mount_of(task["remote_dir"])
    if mount_dir is None:
        runtime_db.terminate_extract_task(tid, f"路径不在 /115open/ 下："
                                          f"{task['remote_dir']}")
        return
    src = os.path.join(mount_dir, name)
    if not os.path.isfile(src):
        # 挂载视图在重负载下会抖动（CD2 缓存丢条目），绝不直接判死——
        # 用 gRPC 权威视角复核，确实不在才终结（2026-10-03 生产实测：
        # 24 条假终结，包其实都在）
        from . import cd2_api
        remote_dir = task["remote_dir"]
        listing = await asyncio.to_thread(cd2_api.list_remote_dir,
                                          remote_dir, 300)
        still_there = listing is not None and any(
            n == name for n, _s, _d in listing)
        if still_there:
            logger.warning(
                f"🗜 [E{tid}] 挂载视图抖动（gRPC 确认包在），转退避重试")
            await _fail(tid, "挂载视图抖动：本地 isfile 不可见但 gRPC 确认存在")
            return
        runtime_db.terminate_extract_task(
            tid, f"远端压缩包已不存在：{task['remote_dir']}/{name}")
        try:
            await notify.notify_user(
                f"🗜 115 解压任务终止\n\n远端压缩包已不存在：\n"
                f"{task['remote_dir']}/{name}")
        except Exception:
            pass
        return

    from .naming import sanitize_filename
    staging = os.path.join(config.EXTRACT_STAGING_ROOT,
                           f"{tid}_{sanitize_filename(name)}")
    os.makedirs(staging, exist_ok=True)
    runtime_db.set_extract_staging(tid, staging)
    _CURRENT["task_id"] = tid
    _CURRENT["label"] = name
    _CURRENT["stage"] = "拷贝"
    logger.info(f"🗜 [E{tid}] 开始处理：{name}（{task['archive_size']} 字节）")

    local_arch = os.path.join(staging, "archive" +
                              os.path.splitext(name)[1].lower())
    try:
        # ── ① COPY（幂等：副本已存在且尺寸一致 → 跳过） ──
        if os.path.isfile(local_arch) \
                and os.path.getsize(local_arch) == task["archive_size"]:
            logger.info(f"🗜 [E{tid}] 副本已存在（断点续用），跳过拷贝")
        else:
            await asyncio.to_thread(
                _copy_file, src, local_arch,
                lambda done: logger.info(
                    f"🗜 [E{tid}] 拷贝进度：{done} / "
                    f"{task['archive_size']} 字节")
                if done % (64 * 1024 * 1024) == 0 else None,
                expected=task["archive_size"])
            actual = os.path.getsize(local_arch)
            if actual != task["archive_size"]:
                raise IOError(
                    f"拷贝后尺寸不一致（本地 {actual} ≠ 远端 "
                    f"{task['archive_size']}），远端可能仍在生成")

        # ── ② EXTRACT（阻塞线程；worker 串行，无需 pawchive 的全局锁） ──
        _CURRENT["stage"] = "解压"
        content_dir = os.path.join(staging, "content")
        # 重试时清掉上轮残留的 content：os.replace 无法覆盖非空目录
        # （OSError 66），archive 副本在本地，重解压零成本
        shutil_rmtree(content_dir)
        status, detail, files = await asyncio.to_thread(
            extract_archive, local_arch, content_dir, False)
        if status == "password":
            # 目录名密码尝试（owner 指令 2026-10-09）：压缩包所在最后一层
            # 目录名做密码，只试一次（如 /云下载/Telegram@XXX 下的包用
            # Telegram@XXX 做密码）
            dir_pwd = os.path.basename(mount_dir.rstrip("/"))
            logger.info(f"🗜 [E{tid}] 需密码，尝试目录名密码（{dir_pwd}）")
            status, detail, files = await asyncio.to_thread(
                extract_archive, local_arch, content_dir, False, dir_pwd)
            if status != "extracted":
                runtime_db.mark_extract_password(
                    tid, f"需密码（目录名 {dir_pwd} 尝试失败）：{detail}")
                shutil_rmtree(staging)
                try:
                    await notify.notify_user(
                        f"🗜 115 解压跳过（需密码）\n\n{name}\n"
                        f"已尝试目录名「{dir_pwd}」做密码仍失败。\n"
                        "已跳过；下次扫描该目录时会自动再试。")
                except Exception:
                    pass
                return
            status, detail, files = "extracted", detail, files
        if status == "no-space":
            await _fail(tid, f"磁盘不足：{detail}")
            return
        if status != "extracted":
            await _fail(tid, f"解压失败：{detail}")
            return
        logger.info(f"🗜 [E{tid}] 解压完成：{detail}")

        # ── ③ UPLOAD（逐文件回传 <原目录>/<包名>/，已存在同尺寸跳过） ──
        _CURRENT["stage"] = "回传"
        stem = os.path.splitext(name)[0]
        target_root = os.path.join(mount_dir, stem)
        rel_files = [os.path.relpath(f, content_dir) for f in files]
        uploaded = skipped = total_bytes = 0
        for rel in rel_files:
            local = os.path.join(content_dir, rel)
            if os.path.islink(local):
                logger.warning(f"🗜 [E{tid}] 跳过符号链接（安全）：{rel}")
                continue
            target = os.path.join(target_root, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            size = os.path.getsize(local)
            if os.path.isfile(target) \
                    and os.path.getsize(target) == size:
                skipped += 1      # 115 秒传/重试幂等：远端已有同尺寸
            else:
                await asyncio.to_thread(_copy_file, local, target)
                uploaded += 1
                total_bytes += size
            runtime_db.set_extract_progress(
                tid, uploaded + skipped, total_bytes)
        logger.info(f"🗜 [E{tid}] 回传完成：上传 {uploaded} / 跳过已有 "
                    f"{skipped} / 共 {len(rel_files)} 个文件")

        # ── ④ VERIFY（gRPC 对账 + CD2 上传任务清空，双重保险） ──
        _CURRENT["stage"] = "对账"
        diffs = await _verify_with_retry(tid, target_root, rel_files,
                                         content_dir)
        if diffs:
            await _fail(tid, "对账不一致：" + "；".join(diffs[:5]))
            return

        runtime_db.complete_extract_task(tid)
        try:
            shutil_rmtree(staging)
        except Exception as e:
            logger.warning(f"🗜 [E{tid}] staging 清理失败（不影响完成）：{e}")
        try:
            await notify.notify_user(
                f"🗜 115 解压回传完成\n\n{name}\n"
                f"产物：{task['remote_dir']}/{stem}/（{len(rel_files)} 个"
                f"文件，上传 {uploaded} / 已有 {skipped}）\n"
                f"对账：全部一致；原压缩包保留。")
        except Exception:
            pass
    except Exception as e:
        logger.exception(f"🗜 [E{tid}] 处理异常：{e}")
        await _fail(tid, f"{type(e).__name__}: {e}")
    finally:
        _CURRENT.update({"task_id": None, "label": None, "stage": None})


def shutil_rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


async def _fail(tid, error):
    """失败分流：重试未到上限 → 退避重投；到上限 → 终结 + 通知。"""
    task = runtime_db.get_extract_task(tid)
    attempts = int(task["attempts"]) if task else 1
    if attempts >= int(config.EXTRACT_MAX_ATTEMPTS):
        runtime_db.terminate_extract_task(tid, error)
        try:
            await notify.notify_user(
                f"🗜 115 解压任务终止（重试 {attempts} 次未成）\n\n"
                f"{task['archive_name'] if task else tid}\n"
                f"原因：{error}\n可用 /115x retry {tid} 人工重投。")
        except Exception:
            pass
        return
    delay = _backoff_delay(attempts)
    runtime_db.postpone_extract_task(tid, time.time() + delay, error)
    logger.warning(f"🗜 [E{tid}] {error}；{delay}s 后重试（第 {attempts} 次）")


async def _verify_with_retry(tid, target_root, rel_files, content_dir,
                             tries=None, gap=None):
    """对账重试：等 CD2 后台上传清空 + 逐父目录尺寸一致。

    返回差异清单（空 = 通过）。每轮之间睡 gap 秒（CD2 异步上传在途）。
    """
    from . import cd2_api
    if tries is None:
        tries = int(getattr(config, "EXTRACT_VERIFY_TRIES", 9))
    if gap is None:
        gap = float(getattr(config, "EXTRACT_VERIFY_GAP_SECONDS", 15.0))
    for attempt in range(1, tries + 1):
        if attempt > 1:
            # 逐轮加倍：CD2 异步上传大树可能要几分钟到十几分钟，
            # 固定短间隔的窗口不够（2026-10-09 E444：深层目录 60s 未落定）
            gap = min(gap * 2, 120.0)
        # 注：不做「CD2 全局在途==0」的闸门——在途计数覆盖全盘（/dyu 视频等
        # 其他目录也在备份），永远可能 >0，会把本任务的对账轮次全烧在等待
        # 上。权威判据只有下面的逐文件尺寸对账，重试轮本身已覆盖「上传未
        # 落定」的时延。

        # 逐父目录 gRPC 尺寸对账（阻塞 gRPC → 放线程）
        diffs = await asyncio.to_thread(
            _verify_remote_sizes, target_root, rel_files, content_dir)
        if not diffs:
            logger.info(f"🗜 [E{tid}] 对账通过：{len(rel_files)} 个文件尺寸"
                        "全部一致")
            return []
        if attempt < tries:
            logger.warning(f"🗜 [E{tid}] 对账 {len(diffs)} 处不一致，"
                           f"{gap}s 后重试（{attempt}/{tries}）")
            await asyncio.sleep(gap)
    return diffs


def _verify_remote_sizes(target_root, rel_files, content_dir):
    """远端逐父目录对账。返回 [差异描述, ...]（空 = 全部一致）。"""
    from . import cd2_api
    diffs = []
    dirs = {}
    for rel in rel_files:
        local = os.path.join(content_dir, rel)
        if os.path.islink(local):
            continue
        parent = os.path.dirname(os.path.join(target_root, rel))
        dirs.setdefault(parent, []).append(rel)
    for parent, names in dirs.items():
        remote_dir = remote_of(parent)
        try:
            listing = cd2_api.list_remote_dir(remote_dir, limit=2000)
        except Exception as e:
            # CD2 瞬断（重启/升级/网络抖）：当「本轮对不上」交重试轮兜底，
            # 绝不让 gRPC 异常逃出对账（冒烟实测：CD2 掉线时 grpc 异常会
            # 从 list_remote_dir 穿出来）
            logger.warning(f"🗜 对账列目录失败（{remote_dir}）：{e}")
            diffs.append(f"远端目录不可读：{remote_dir}")
            continue
        if listing is None:
            diffs.append(f"远端目录不可读：{remote_dir}")
            continue
        sizes = {n: s for n, s, _d in listing}
        if len(sizes) >= 2000:
            # 清单撞到分页上限：可能被截断，缺失判定不可信——本轮按
            # 「对不上」重试（下一轮仍截断则任务会重试耗尽后人工介入）
            diffs.append(f"远端目录条目≥2000，清单可能截断：{remote_dir}")
            continue
        for rel in names:
            expect = os.path.getsize(os.path.join(content_dir, rel))
            got = sizes.get(os.path.basename(rel))
            if got is None:
                diffs.append(f"远端缺失：{rel}")
            elif int(got) != int(expect):
                diffs.append(f"尺寸不符：{rel}（远端 {got} ≠ 本地 {expect}）")
    return diffs


def _preconditions_ok():
    """领取前的环境检查：挂载在、磁盘余量足。不满足返回 False（不烧重试）。"""
    if not os.path.isdir(config.CLOUD_MOUNT_BASE):
        _throttled_warn(
            "mount", f"🗜 CD2 挂载不可用（{config.CLOUD_MOUNT_BASE}），"
                     "115 解压 worker 暂停领取")
        return False
    usage = os.statvfs(config.EXTRACT_STAGING_ROOT
                       if os.path.isdir(config.EXTRACT_STAGING_ROOT)
                       else config.DOWNLOAD_DIR)
    free_gb = usage.f_bavail * usage.f_frsize / 1024 ** 3
    if free_gb < float(config.PAWCHIVE_MIN_FREE_GB):
        _throttled_warn(
            "disk", f"🗜 磁盘余量 {free_gb:.1f}GB 低于保护线，"
                    "115 解压 worker 暂停领取")
        return False
    return True


async def _tick():
    if not state.RUNTIME_DB_READY:
        return
    if not _preconditions_ok():
        return
    task = runtime_db.claim_next_extract_task()
    if task is None:
        return
    renamer = asyncio.ensure_future(_renew_loop(task["id"]))
    try:
        await _process_task(task)
    finally:
        renamer.cancel()
        try:
            await renamer
        except asyncio.CancelledError:
            pass


async def extract_worker_loop():
    """常驻循环：串行处理，任何一轮异常都吞掉记日志，永不退出。"""
    logger.info(
        "🗜 115 解压 worker 已启动（串行一次一包，退避重试上限 "
        f"{config.EXTRACT_MAX_ATTEMPTS}，staging {config.EXTRACT_STAGING_ROOT}）")
    load_watch_dirs()
    if _WATCH_DIRS["dirs"]:
        logger.info(f"🗜 watch 已登记 {len(_WATCH_DIRS['dirs'])} 个目录，"
                    "每小时自动扫描")
    watch_task = asyncio.ensure_future(_watch_loop())
    _WATCH_TASKS.add(watch_task)
    watch_task.add_done_callback(_WATCH_TASKS.discard)
    # 崩溃自愈：上次进程遗留的过期 PROCESSING → PENDING
    try:
        runtime_db.recover_expired_extract_tasks()
    except Exception as e:
        logger.warning(f"🗜 115 解压任务租约恢复失败：{e}")
    while not _STOP["requested"]:
        try:
            if _PAUSED["paused"]:
                await asyncio.sleep(float(getattr(
                    config, "EXTRACT_POLL_SECONDS", 2.0)))
                continue
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"🗜 115 解压 worker 轮询异常：{e}")
        await asyncio.sleep(float(getattr(config, "EXTRACT_POLL_SECONDS",
                                          2.0)))


def release_inflight():
    """把在途任务放回 PENDING（优雅停机，不等租约到期）。staging 保留。"""
    tid = _CURRENT.get("task_id")
    if tid is None:
        return False
    try:
        return runtime_db.release_extract_task(tid)
    except runtime_db.DbUnavailable as e:
        logger.warning(f"🗜 在途解压任务释放失败（租约到期后自愈）：{e}")
        return False


# ============================================================
# /115x 命令（Saved Messages / bot 命令面板）
# ============================================================
_EXTRACT_CMD_RE = re.compile(r"^/115x(?:\s|_|$)", re.IGNORECASE)
_PAUSED = {"paused": False}


def is_extract_command(text):
    return bool(_EXTRACT_CMD_RE.match(str(text or "").strip()))


def normalize_remote_dir(raw):
    """用户输入 → gRPC 路径：『云下载』『/云下载』『/115open/云下载』同义。

    返回 None 表示路径为空或含穿越段（绝不拼出 /115open/../）。
    """
    p = str(raw or "").strip().strip("/")
    if not p:
        return None
    if p.startswith("115open/"):
        p = p[len("115open/"):]
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if not parts or any(seg == ".." for seg in parts):
        return None
    return "/115open/" + "/".join(parts)


def scan_remote_archives(root_remote_dir, max_depth=None, max_dirs=None):
    """递归扫描目录树下所有压缩包（BFS，gRPC 权威视角）。

    返回 (archives, scanned, ghost_dirs)：
    archives = [(parent_remote_dir, name, size)]——产物落在各自父目录；
    scanned = 实际列过的目录数；ghost_dirs = 根层 .zip 命名的目录
    （115 云解压产物，客户端里长得像压缩包）。
    """
    from . import cd2_api
    if max_depth is None:
        max_depth = int(getattr(config, "EXTRACT_SCAN_MAX_DEPTH", 8))
    if max_dirs is None:
        max_dirs = int(getattr(config, "EXTRACT_SCAN_MAX_DIRS", 400))
    archives, ghost_dirs = [], []
    scanned = 0
    root = str(root_remote_dir).rstrip("/")
    queue = [(root, 0)]
    seen = set()
    while queue and scanned < max_dirs:
        d, depth = queue.pop(0)
        if d in seen:
            continue
        seen.add(d)
        try:
            listing = cd2_api.list_remote_dir(d, limit=500)
        except Exception as e:
            logger.warning(f"🗜 扫描列目录失败（{d}）：{e}")
            continue
        scanned += 1
        if not listing:
            continue
        for n, s, is_dir in listing:
            if is_dir:
                if is_archive_name(n) and d == root:
                    ghost_dirs.append(n)   # 只在根层提示，子层不刷屏
                if depth < max_depth:
                    queue.append((f"{d}/{n}", depth + 1))
            elif is_archive_name(n):
                archives.append((d, n, int(s or 0)))
    return archives, scanned, ghost_dirs


# watch 登记（/115x watch <路径>）：每小时自动扫描这些目录，新压缩包
# 自动入队（唯一键判重，只收新的）。持久化在 config.PAWCHIVE_COOKIE 同级
# 的 JSON——重启后恢复。
_WATCH_FILE = None
_WATCH_DIRS = {"dirs": [], "last_scan": 0.0}


def load_watch_dirs():
    """启动时从 runtime/watch_dirs.json 恢复登记。"""
    import json as _json
    from . import runtime_db as _rtdb
    path = os.path.join(os.path.dirname(config.RUNTIME_DB_FILE),
                        "watch_dirs.json")
    global _WATCH_FILE
    _WATCH_FILE = path
    try:
        with open(path, encoding="utf-8") as f:
            d = _json.load(f)
        _WATCH_DIRS["dirs"] = [str(x) for x in d.get("dirs") or []]
        _WATCH_DIRS["last_scan"] = float(d.get("last_scan") or 0)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"🗜 watch 登记读取失败：{e}")


def save_watch_dirs():
    import json as _json
    try:
        path = _WATCH_FILE or os.path.join(
            os.path.dirname(config.RUNTIME_DB_FILE), "watch_dirs.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            _json.dump({"dirs": _WATCH_DIRS["dirs"],
                        "last_scan": _WATCH_DIRS["last_scan"]}, f,
                       ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"🗜 watch 登记保存失败：{e}")


def watch_add(remote_dir):
    """登记目录（归一化去重）。已在列表返回 False。"""
    remote_dir = normalize_remote_dir(remote_dir)
    if remote_dir is None:
        return False
    if remote_dir in _WATCH_DIRS["dirs"]:
        return False
    _WATCH_DIRS["dirs"].append(remote_dir)
    save_watch_dirs()
    return True


def watch_remove(remote_dir):
    """取消登记。返回是否移除了。"""
    remote_dir = normalize_remote_dir(remote_dir)
    if remote_dir in _WATCH_DIRS["dirs"]:
        _WATCH_DIRS["dirs"].remove(remote_dir)
        save_watch_dirs()
        return True
    return False


async def watch_scan_once():
    """扫一轮全部登记目录：递归（含子目录）发现新压缩包自动入队。

    返回 (新入队总数, 摘要行列表)。用户的包常按「一包一夹」存放，
    顶层扫描会一无所获（2026-10-10 生产实测）——与命令扫描共用同一
    递归实现（scan_remote_archives，深度/目录数上限内置）。"""
    total_ins = 0
    lines = []
    for d in list(_WATCH_DIRS["dirs"]):
        found, scanned, _ghosts = await asyncio.to_thread(
            scan_remote_archives, d)
        by_dir = {}
        for pd, name, sz in found:
            by_dir.setdefault(pd, []).append((name, sz))
        ins = skip = 0
        for pd, archives in by_dir.items():
            a, b = runtime_db.enqueue_extract_tasks(pd, archives)
            # 该目录的 PASSWORD 任务重置回 PENDING（下次再试目录名密码）
            runtime_db.reset_extract_password_tasks(pd)
            ins += a
            skip += b
        total_ins += ins
        if ins or skip:
            lines.append(f"· {d}：新 {ins}｜已有 {skip}（扫 {scanned} 目录）")
    _WATCH_DIRS["last_scan"] = time.time()
    save_watch_dirs()
    return total_ins, lines


async def _watch_loop():
    """常驻 watch 扫描循环：每小时一轮（有登记才扫）。"""
    interval = float(getattr(config, "EXTRACT_WATCH_INTERVAL_SECONDS",
                             3600))
    while not _STOP["requested"]:
        try:
            if _WATCH_DIRS["dirs"] and not _PAUSED.get("paused") \
                    and state.RUNTIME_DB_READY:
                ins, _lines = await watch_scan_once()
                if ins:
                    logger.info(f"🗜 watch 自动入队 {ins} 个新压缩包")
                    try:
                        await notify.notify_user(
                            f"🗜 115 watch：发现 {ins} 个新压缩包，"
                            "已自动入队解压回传")
                    except Exception:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"🗜 watch 扫描异常：{e}")
        await asyncio.sleep(interval)


def status_text():
    """状态总览（命令与菜单共用）。"""
    counts = runtime_db.extract_status_counts()
    P, T = runtime_db.EXTRACT_PENDING, runtime_db.EXTRACT_PROCESSING
    C, F, X = (runtime_db.EXTRACT_COMPLETED, runtime_db.EXTRACT_FAILED,
               runtime_db.EXTRACT_TERMINAL)
    cur = _CURRENT.get("label")
    try:
        usage = os.statvfs(config.EXTRACT_STAGING_ROOT
                           if os.path.isdir(config.EXTRACT_STAGING_ROOT)
                           else config.DOWNLOAD_DIR)
        free_gb = usage.f_bavail * usage.f_frsize / 1024 ** 3
        disk = f"{free_gb:.0f}GB"
    except OSError:
        disk = "?"
    lines = [
        "🗜 115 解压回传",
        f"worker：{'已暂停' if _PAUSED['paused'] else '运行中'}"
        f" · 磁盘余量 {disk}",
        f"待处理 {counts.get(P, 0)} · 处理中 {counts.get(T, 0)}"
        f" · 完成 {counts.get(C, 0)}",
        f"重试中 {counts.get(F, 0)} · 终结 {counts.get(X, 0)}",
    ]
    if cur:
        lines.append(f"当前：{cur}（{_CURRENT.get('stage') or '处理中'}）")
    recent = runtime_db.list_extract_tasks(limit=8)
    if recent:
        lines.append("")
        lines.append("最近任务：")
        mark = {P: "⏳", T: "🔄", C: "✅", F: "🔁", X: "⛔"}
        for t in recent:
            line = (f"{mark.get(t['status'], '·')} #{t['id']} "
                    f"{t['archive_name'][:40]}（第 {t['attempts']} 次）")
            if t["error"]:
                line += f"\n   {t['error'][:70]}"
            lines.append(line)
    lines.append("")
    if _WATCH_DIRS["dirs"]:
        lines.append("watch：" + "、".join(_WATCH_DIRS["dirs"]))
    lines.append("用法：/115x <115路径>（如 /115x 云下载）；"
                 "/115x stop 暂停领取｜/115x start 恢复｜"
                 "/115x retry id｜/115x del id")
    return "\n".join(lines)


def _match_task_id(prefix):
    """短 id 前缀 → 完整任务 id；唯一匹配才返回。"""
    prefix = str(prefix or "").strip()
    if not prefix:
        return None
    for t in runtime_db.list_extract_tasks(limit=500):
        if str(t["id"]) == prefix or str(t["id"]).startswith(prefix):
            return t["id"]
    return None


async def command_reply(cmd_text):
    """/115x 分发。返回回执文案（纯文本）。"""
    body = str(cmd_text or "").strip()[len("/115x"):].strip()
    if body.startswith("_"):
        body = body[1:]
    if not body:
        return status_text()
    head, _, rest = body.partition(" ")
    low = head.lower()

    if low == "watch":
        if not rest:
            if not _WATCH_DIRS["dirs"]:
                return "📭 未登记 watch 目录。用法：/115x watch <115路径>"
            lines = ["📡 watch 已登记目录："]
            for d in _WATCH_DIRS["dirs"]:
                lines.append(f"  · {d}")
            lines.append("每小时自动扫描，新压缩包自动入队。"
                         "取消：/115x unwatch <路径>")
            return "\n".join(lines)
        rd = normalize_remote_dir(rest)
        if rd is None:
            return "❌ 路径无效"
        if watch_add(rd):
            return f"📡 已登记 watch：{rd}\n每小时自动扫描，" \
                   "新压缩包自动入队解压回传（判重只收新的）。"
        return f"ℹ️ {rd} 已在 watch 列表里"
    if low == "unwatch":
        rd = normalize_remote_dir(rest)
        if rd is None:
            return "❌ 路径无效"
        if watch_remove(rd):
            return f"🔕 已取消 watch：{rd}"
        return f"❌ {rd} 不在 watch 列表里"
    if low == "stop":
        _PAUSED["paused"] = True
        logger.info("🗜 115 解压 worker 暂停领取（/115x stop）")
        return "⏸ 115 解压 worker 已暂停领取（在途任务会跑完）。\n" \
               "恢复：/115x start"
    if low == "start":
        _PAUSED["paused"] = False
        logger.info("🗜 115 解压 worker 恢复领取（/115x start）")
        return "▶️ 115 解压 worker 已恢复领取。"
    if low == "retry":
        tid = _match_task_id(rest)
        if tid is None:
            return f"❌ 没有匹配「{rest}」的任务（/115x 查看列表）"
        if runtime_db.retry_extract_task(tid):
            return f"🔁 任务 #{tid} 已重投（staging 保留，断点续用）"
        return f"❌ 任务 #{tid} 不在可重投状态（重试中/终结才可重投）"
    if low == "del":
        tid = _match_task_id(rest)
        if tid is None:
            return f"❌ 没有匹配「{rest}」的任务（/115x 查看列表）"
        task = runtime_db.get_extract_task(tid)
        if task and task.get("staging_dir"):
            shutil_rmtree(task["staging_dir"])
        runtime_db.delete_extract_task(tid)
        return f"🗑 已移除任务 #{tid}" + (
            f"（{task['archive_name']}）" if task else "")

    # 其余 = 115 路径 → 扫描 + 入队
    remote_dir = normalize_remote_dir(body)
    if remote_dir is None:
        return "❌ 路径无效（不能为空，也不能包含 ..）"
    from . import cd2_api
    listing = await asyncio.to_thread(cd2_api.list_remote_dir,
                                      remote_dir, 500)
    if listing is None:
        return (f"❌ 目录不可读：{remote_dir}\n"
                "（检查 CD2 是否在运行、挂载是否在线、路径是否正确）")
    # 递归扫描（含子目录，gRPC 权威视角）：每个包的任务记其父目录，
    # 产物回传到 <父目录>/<包名>/，天然防同名冲突
    found, scanned, ghost_dirs = await asyncio.to_thread(
        scan_remote_archives, remote_dir)
    if not found:
        hint = ""
        if ghost_dirs:
            shown = "\n".join(f"  · {n[:50]}" for n in ghost_dirs[:5])
            more = (f"\n  … 共 {len(ghost_dirs)} 个"
                    if len(ghost_dirs) > 5 else "")
            hint = (f"\n\n⚠️ 注意：根目录下有 {len(ghost_dirs)} 个以压缩包"
                    f"命名的**目录**（已是解压产物，非压缩包文件）：\n"
                    f"{shown}{more}")
        return (f"❌ 没有发现压缩包文件：{remote_dir}\n"
                f"（已递归扫描 {scanned} 个目录，含子目录，"
                f"深度上限 {config.EXTRACT_SCAN_MAX_DEPTH}）\n"
                f"支持：{' / '.join(ARCHIVE_EXTS)}{hint}")
    # 按父目录分组入队（唯一键 = 父目录+包名+尺寸，幂等）
    by_dir = {}
    for d, n, s_ in found:
        by_dir.setdefault(d, []).append((n, s_))
    ins = skip = 0
    for d, archives in by_dir.items():
        a, b = runtime_db.enqueue_extract_tasks(d, archives)
        ins += a
        skip += b
    dirs_n = len(by_dir)
    names = "\n".join(
        f"  · {d[len(remote_dir):].strip('/') or '(根)'}/{n}"
        for _d, n, _s in found[:8])
    more = f"\n  … 共 {len(found)} 个" if len(found) > 8 else ""
    return (f"🗜 递归扫描 {scanned} 个目录，发现 {len(found)} 个压缩包"
            f"（分布在 {dirs_n} 个目录）：新入队 {ins}｜跳过 {skip}"
            f"（在队/已完成）\n{names}{more}\n\n"
            "worker 将逐包：拷贝 → 解压 → 回传到 <所在目录>/<包名>/ → 对账。"
            "进度：/115x")
    logger.info("🗜 115 解压 worker 已停止（/115x stop）")
