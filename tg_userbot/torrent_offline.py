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
    if not ih:
        try:
            data = await asyncio.to_thread(_download_torrent_bytes, url)
            magnet, info = torrent_to_magnet(data)
            name = info["name"]
        except ValueError as e:
            return f"🧲 {e}"
        except Exception as e:
            logger.warning(f"🧲 种子下载失败：{e}")
            return f"🧲 种子下载失败：{e}"
        magnet = torrent_util.magnet_from(name, ih)
    try:
        ok, err = await asyncio.to_thread(_submit, magnet)
    except Exception as e:
        return f"🧲 离线提交失败：{e}"
    if not ok:
        if "10008" in (err or "") or "已存在" in (err or ""):
            return "🧲 该种子已在 115 离线列表中（/paw_offline_tasks 看进度）"
        return f"🧲 离线提交被拒绝：{err}"
    lines = ["🧲 种子已转为 115 离线任务", f"名称：{name}"]
    if info:
        lines.append(f"内容 {format_size(info['size'])}｜"
                     f"{'⚠️ 私有种子' if info['private'] else '公开种子'}")
    lines.append("CD2 离线拉取到 /115open/云下载（/paw_offline_tasks 看进度）")
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
            return "🧲 该种子已在 115 离线列表中（/paw_offline_tasks 看进度）"
        return f"🧲 离线提交被拒绝：{err}"
    lines = ["🧲 种子已转为 115 离线任务", f"名称：{info['name']}",
             f"内容 {format_size(info['size'])}｜"
             f"{'⚠️ 私有种子' if info['private'] else '公开种子'}",
             "CD2 离线拉取到 /115open/云下载"]
    from .text import fit_4096
    return fit_4096("\n".join(lines))
