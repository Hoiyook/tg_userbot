"""种子（.torrent）→ 115 离线下载：URL 与文件字节的统一入口。

流程：URL 尾段自带 infohash 直接构造磁力（ehtracker 形态）→ 否则下载
种子文件解析 bencode → 复用 cd2_api.add_offline_download 丢给 115 离线。
私有种子（private 标记）如实拒绝——115 离线服务器拿不到资源。
"""
import asyncio
import os
import urllib.request

from . import config
from . import notify
from . import torrent_util
from .log import logger


def is_torrent_url(url):
    """URL 是否以 .torrent 结尾（忽略 query）。"""
    from urllib.parse import urlsplit
    path = urlsplit(str(url or "")).path.lower()
    return path.endswith(".torrent")


def _download_torrent_bytes(url, timeout=30):
    """下载 .torrent 直链内容（种子文件为 KB 级，4MB 硬顶防异常）。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh) tg-userbot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(4 * 1024 * 1024)


def torrent_to_magnet(data):
    """种子字节 → (磁力链接, 摘要)；私有种子抛 ValueError。委托 torrent_util。"""
    return torrent_util.torrent_to_magnet(data)


_OFFLINE_WATCH_TASKS = set()


def _dir_names(path):
    """远端目录快照 {文件名: 大小}；查询失败返回 None。"""
    from . import cd2_api
    items = cd2_api.list_remote_dir(path)
    if items is None:
        return None
    return {name: size for name, size, is_dir in items}


async def spawn_completion_watcher(to_folder, known_names, label="",
                                   timeout_secs=None, info_hash=None):
    """离线完成守望：轮询目标目录，发现新增文件即 notify（用户要求：
    离线完成要有反馈）。超时则提示仍可能离线中。后台任务，强引用。"""
    timeout = float(timeout_secs or config.OFFLINE_WATCH_TIMEOUT_SECONDS)
    poll = float(getattr(config, "OFFLINE_WATCH_POLL_SECONDS", 30))

    async def _run():
        # 持久化（2026-11-05 审计）：守望 task 只存内存，重启即丢通知承诺；
        # 行即真相源，完结（通知发出）置 DONE
        from . import runtime_db
        watch_id = None
        try:
            watch_id = runtime_db.add_offline_watch(
                to_folder, label, known_names, info_hash)
        except Exception as e:
            logger.warning(f"🧲 离线守望登记失败（不影响本次守望）：{e}")
        try:
            await _run_watch()
        finally:
            if watch_id is not None:
                try:
                    runtime_db.mark_offline_done(watch_id)
                except Exception:
                    pass

    async def _run_watch():
        before = set(known_names or [])
        waited = 0.0
        while waited < timeout:
            await asyncio.sleep(poll)
            waited += poll
            names = await asyncio.to_thread(_dir_names, to_folder)
            if names is None:
                continue   # 查询失败：下轮再试
            new = [n for n in names if n not in before]
            if new:
                from .naming import format_size
                lines = [f"🧲 离线完成：{label or to_folder}"]
                for n in new[:5]:
                    sz = names.get(n)
                    lines.append(f"  · {n[:60]}（{format_size(sz)}）"
                                 if sz else f"  · {n[:60]}")
                if len(new) > 5:
                    lines.append(f"  … 共 {len(new)} 个新文件")
                logger.info(f"🧲 离线完成：{len(new)} 个新文件（{label}）")
                try:
                    await notify.notify_user("\n".join(lines))
                except Exception as e:
                    logger.warning(f"🧲 离线完成通知失败：{e}")
                return
        verdict = ""
        if info_hash:
            try:
                st = await asyncio.to_thread(offline_status, info_hash,
                                             to_folder or DEFAULT_TO)
            except Exception as e:
                logger.warning(f"🧲 离线状态查询失败：{e}")
                st = None
            if st is None:
                verdict = ("❌ 任务不在 115 离线队列中——提交很可能被拒"
                           "（内容审核/链接无效），不会再来新文件了")
            elif st[0] == "已完成":
                verdict = ("⚠️ 任务标记完成但目标目录未见新文件——"
                           "去 115 云下载目录核对落点")
            else:
                verdict = f"⏳ 任务确认仍在队列：{st[0]}（{st[1]:.0f}%）"
        if not verdict:
            verdict = ("任务可能仍在离线中（冷门资源无 peer 会久挂），"
                       "可稍后在 115 云下载目录确认")
        try:
            await notify.notify_user(
                f"⏳ 离线 {int(timeout/60)} 分钟未见新文件（{label}）——"
                f"{verdict}")
        except Exception:
            pass

    t = asyncio.create_task(_run())
    _OFFLINE_WATCH_TASKS.add(t)
    t.add_done_callback(_OFFLINE_WATCH_TASKS.discard)
    return t


_OFFLINE_STATUS_ZH = {1: "排队中", 2: "已完成", 3: "下载中"}


def offline_status(info_hash, sample_path=None):
    sample_path = sample_path or DEFAULT_TO
    """按 infoHash 查 115 离线任务的真实状态（2026-11-01 P2）。

    返回 (状态中文, 完成百分比) 或 None（任务不在队列——提交被拒/被
    清理/从未入列，正是 2026-10-31 App 端「提交成功但不入列」的实锤
    探测手段）。阻塞 gRPC，调用方放线程。"""
    import re as _re
    from . import cd2_api
    from .cd2_proto import clouddrive_pb2 as pb
    f = cd2_api.find_remote(sample_path)
    if f is None:
        return None
    m = _re.match(r"\{(\w+)-(\d+)\}", str(getattr(f, "id", "") or ""))
    if not m:
        return None
    stub, md = cd2_api._md()
    ih = (info_hash or "").lower()
    for page in (1, 2, 3):
        r = stub.ListAllOfflineFiles(pb.OfflineFileListAllRequest(
            cloudName=m.group(1), cloudAccountId=m.group(2), page=page),
            metadata=md, timeout=30)
        for t in r.offlineFiles:
            if (t.infoHash or "").lower() == ih:
                st = _OFFLINE_STATUS_ZH.get(t.status, f"未知状态{t.status}")
                return st, float(t.percendDone or 0)
        if len(r.offlineFiles) < 30:
            break
    return None


def snapshot_dir(to_folder):
    """目录快照（提交前抓，供守望比对新增）。同步阻塞，调用方放线程。"""
    return _dir_names(to_folder)


def spawn_watch(to_folder, label="", known_names=None, info_hash=None):
    """提交成功后启动完成守望（sync 入口，内部建任务）。

    2026-09-29 修复：原来直接 ``return spawn_completion_watcher(...)``——
    返回的协程没人 await，守望从未运行（RuntimeWarning 被刷在 stderr 里）。
    现在在任务里先抓目录快照（_dir_names 是 CD2 gRPC 调用，放线程避免卡
    事件循环）再进守望；任务强引用防 GC。"""
    async def _run():
        known = known_names if known_names is not None else set(
            await asyncio.to_thread(_dir_names, to_folder) or [])
        await spawn_completion_watcher(to_folder, known, label=label,
                                       info_hash=info_hash)

    t = asyncio.ensure_future(_run())
    _OFFLINE_WATCH_TASKS.add(t)
    t.add_done_callback(_OFFLINE_WATCH_TASKS.discard)
    return t


async def resume_offline_watchers():
    """启动重建（2026-11-05 审计止血）：WATCHING 行逐个重新拉起守望。

    known_names 用登记时的快照——重启期间落的新文件会被当「新增」正常
    通知，不丢承诺。返回恢复条数。"""
    from . import runtime_db
    try:
        rows = runtime_db.list_offline_watching()
    except Exception as e:
        logger.warning(f"🧲 离线守望重建读取失败：{e}")
        return 0
    for r in rows:
        await spawn_completion_watcher(
            r["to_folder"], r["known_names"], label=r["label"],
            info_hash=r["info_hash"])
    if rows:
        logger.warning(f"🧲 离线守望重建：{len(rows)} 个在途任务恢复完成通知")
    return len(rows)


DEFAULT_TO = "/115open/云下载"


def resolve_to_folder(arg=None):
    """下载目标目录：显式 /115open 路径原样；裸 token 当作者 → 漫画/<作者>/。

    （2026-10-25 owner 需求：磁链/种子后面跟作者名，落到作者目录。）"""
    from .naming import sanitize_filename
    q = str(arg or "").strip()
    if not q:
        return DEFAULT_TO
    if q.startswith("/115open/"):
        return q.rstrip("/")
    return "/115open/漫画/" + (sanitize_filename(q)[:60] or "未命名")


def ensure_remote_dir(to_folder):
    """目标目录不存在 CD2 离线会拒（NOT_FOUND）——经挂载 makedirs 建之。"""
    import os
    from .extract_worker import mount_of
    mount_dir = mount_of(to_folder)
    if mount_dir is not None:
        os.makedirs(mount_dir, exist_ok=True)


def trailing_author(text, url):
    """消息文本里 url 之外的裸 token = 作者（如「<链接> 某作者」）。

    返回作者串或 None；不认 / 开头（防把命令/路径误当作者）。"""
    import re as _re
    rest = str(text or "").replace(url, " ")
    for tok in _re.split(r"\s+", rest.strip()):
        if tok and not tok.startswith(("/", "magnet:", "http", "ed2k:")):
            return tok
    return None


def _submit(magnet, to_folder=None):
    """丢给 CD2 离线（独立函数便于测试 mock）。返回 (ok, err)。"""
    from . import cd2_api
    to_folder = to_folder or DEFAULT_TO
    ensure_remote_dir(to_folder)
    return cd2_api.add_offline_download(magnet, to_folder)


async def handle_torrent_url(url, to_folder=None):
    """🧲 入口一：.torrent 直链 → 磁力 → 提交 115 离线。返回回执文案。

    URL 尾段自带 infohash（ehtracker 等形态）时免下载秒提交；否则下载
    种子文件（KB 级）解析。"""
    from .naming import format_size
    from urllib.parse import urlsplit
    ih = torrent_util.infohash_from_url(url)
    name, info = (os.path.basename(
        urlsplit(url).path)[:-len(".torrent")]), None
    if ih:
        # URL 尾段自带 infohash（ehtracker 形态）：直接构造磁力，免下载
        magnet = torrent_util.magnet_from(name, ih)
    else:
        try:
            data = await asyncio.to_thread(_download_torrent_bytes, url)
            magnet, info = torrent_to_magnet(data)
            name = info["name"]
        except ValueError as e:
            return f"🧲 {e}"
        except Exception as e:
            logger.warning(f"🧲 种子下载失败：{e}")
            return f"🧲 种子下载失败：{e}"
    try:
        ok, err = await asyncio.to_thread(_submit, magnet, to_folder)
    except Exception as e:
        return f"🧲 离线提交失败：{e}"
    if not ok:
        if "10008" in (err or "") or "已存在" in (err or ""):
            # 已在列表 ≠ 已完成：照样挂守望，完成后通知（否则重发链接
            # 永远拿不到完成反馈）
            spawn_watch(to_folder or DEFAULT_TO, label=name, info_hash=ih)
            return "🧲 该种子已在 115 离线列表中（完成后会通知你）"
        return f"🧲 离线提交被拒绝：{err}"
    lines = ["🧲 种子已转为 115 离线任务", f"名称：{name}"]
    if info:
        lines.append(f"内容 {format_size(info['size'])}｜"
                     f"{'⚠️ 私有种子' if info['private'] else '公开种子'}")
    lines.append(f"CD2 离线拉取到 {to_folder or DEFAULT_TO}（完成后会通知你）")
    spawn_watch(to_folder or DEFAULT_TO, label=name, info_hash=ih)
    from .text import fit_4096
    return fit_4096("\n".join(lines))


async def handle_torrent_bytes(data, to_folder=None):
    """🧲 入口二：.torrent 文件字节 → 转磁力 → 提交 115 离线。"""
    from .naming import format_size
    try:
        magnet, info = await asyncio.to_thread(
            torrent_to_magnet, data)
    except ValueError as e:
        return f"🧲 {e}"
    except Exception as e:
        return f"🧲 种子解析失败：{e}"
    try:
        ok, err = await asyncio.to_thread(_submit, magnet, to_folder)
    except Exception as e:
        return f"🧲 离线提交失败：{e}"
    if not ok:
        if "10008" in (err or "") or "已存在" in (err or ""):
            # 已在列表 ≠ 已完成：照样挂守望（同 handle_torrent_url）
            spawn_watch(to_folder or DEFAULT_TO, label=info["name"], info_hash=info.get("infohash"))
            return "🧲 该种子已在 115 离线列表中（完成后会通知你）"
        return f"🧲 离线提交被拒绝：{err}"
    lines = ["🧲 种子已转为 115 离线任务", f"名称：{info['name']}",
             f"内容 {format_size(info['size'])}｜"
             f"{'⚠️ 私有种子' if info['private'] else '公开种子'}",
             "CD2 离线拉取到 /115open/云下载",
             "完成后会通知你（默认守望 30 分钟）"]
    spawn_watch(to_folder or DEFAULT_TO, label=info["name"], info_hash=info.get("infohash"))
    from .text import fit_4096
    return fit_4096("\n".join(lines))
