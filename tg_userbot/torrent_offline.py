"""种子（.torrent）→ 115 离线下载：URL 与文件字节的统一入口。

流程：URL 尾段自带 infohash 直接构造磁力（ehtracker 形态）→ 否则下载
种子文件解析 bencode → 复用 cd2_api.add_offline_download 丢给 115 离线。
私有种子（private 标记）如实拒绝——115 离线服务器拿不到资源。
"""
import asyncio
import os
import urllib.request

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
                                   timeout_secs=None):
    """离线完成守望：轮询目标目录，发现新增文件即 notify（用户要求：
    离线完成要有反馈）。超时则提示仍可能离线中。后台任务，强引用。"""
    timeout = float(timeout_secs or config.OFFLINE_WATCH_TIMEOUT_SECONDS)
    poll = float(getattr(config, "OFFLINE_WATCH_POLL_SECONDS", 30))

    async def _run():
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
        try:
            await notify.notify_user(
                f"⏳ 离线 {int(timeout/60)} 分钟未见新文件（{label}）——"
                "任务可能仍在离线中（冷门资源无 peer 会久挂），"
                "可稍后在 115 云下载目录确认。")
        except Exception:
            pass

    t = asyncio.create_task(_run())
    _OFFLINE_WATCH_TASKS.add(t)
    t.add_done_callback(_OFFLINE_WATCH_TASKS.discard)
    return t


def snapshot_dir(to_folder):
    """目录快照（提交前抓，供守望比对新增）。同步阻塞，调用方放线程。"""
    return _dir_names(to_folder)


def spawn_watch(to_folder, label="", known_names=None):
    """提交成功后启动完成守望。known_names 缺省用当前目录快照。"""
    known = known_names if known_names is not None else set(
        _dir_names(to_folder) or [])
    return spawn_completion_watcher(to_folder, known, label=label)


def _submit(magnet):
    """丢给 CD2 离线（独立函数便于测试 mock）。返回 (ok, err)。"""
    from . import cd2_api
    return cd2_api.add_offline_download(magnet, "/115open/云下载")


async def handle_torrent_url(url):
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
        ok, err = await asyncio.to_thread(_submit, magnet)
    except Exception as e:
        return f"🧲 离线提交失败：{e}"
    if not ok:
        if "10008" in (err or "") or "已存在" in (err or ""):
            return "🧲 该种子已在 115 离线列表中（/cd2tasks 看进度）"
        return f"🧲 离线提交被拒绝：{err}"
    lines = ["🧲 种子已转为 115 离线任务", f"名称：{name}"]
    if info:
        lines.append(f"内容 {format_size(info['size'])}｜"
                     f"{'⚠️ 私有种子' if info['private'] else '公开种子'}")
    lines.append("CD2 离线拉取到 /115open/云下载（完成后会通知你）")
    spawn_watch("/115open/云下载", label=name)
    from .text import fit_4096
    return fit_4096("\n".join(lines))


async def handle_torrent_bytes(data):
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
        ok, err = await asyncio.to_thread(_submit, magnet)
    except Exception as e:
        return f"🧲 离线提交失败：{e}"
    if not ok:
        if "10008" in (err or "") or "已存在" in (err or ""):
            return "🧲 该种子已在 115 离线列表中（/cd2tasks 看进度）"
        return f"🧲 离线提交被拒绝：{err}"
    lines = ["🧲 种子已转为 115 离线任务", f"名称：{info['name']}",
             f"内容 {format_size(info['size'])}｜"
             f"{'⚠️ 私有种子' if info['private'] else '公开种子'}",
             "CD2 离线拉取到 /115open/云下载",
             "完成后会通知你（默认守望 30 分钟）"]
    spawn_watch("/115open/云下载", label=info["name"])
    from .text import fit_4096
    return fit_4096("\n".join(lines))
