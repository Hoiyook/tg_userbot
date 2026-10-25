"""e-hentai 漫画下载（/ehentai）——画廊 URL → 种子优先 → 原包回退 → 漫画/作者/。

2026-10-24 浏览器实测定稿（别按直觉改）：

    种子路（优先，免登录零成本）：
      /g/<gid>/<token>/ → gallerytorrents.php?gid=&t=（公开可读）
      种子链接形如 ehtracker.org/get/<id>/<40位infohash>.torrent——
      尾段即 infohash，无需下载种子文件直接构造磁链（torrent_util 同款）
      → CD2 离线拉到 /115open/漫画[/<作者>]/ + 完成守望
    原包路（回退，需 cookie + 耗 GP）：
      archiver.php?gid=&token= 登录墙内；POST dltype=org&dlcheck=
      Download+Original+Archive → 302 到 hath.network/…/0 中转页 →
      「Click Here」链接 = …/0?start=1 即 zip 直链（支持 Range、免 cookie、
      直连与代理均可下）→ httpx 流式下载 → 写挂载 → gRPC 对账

指令：/ehentai <画廊URL> [作者]；/ehentai cookie <粘贴>（archiver 用）。
无种子且无 cookie：明确回执，不静默失败。
"""
import asyncio
import os
import re

import httpx

from . import config

TEXT_PREFIX = "📕"
_GALLERY_RE = re.compile(r"(?:e-?hentai|exhentai)\.org/g/(\d+)/([0-9a-f]+)", re.I)
_TORRENT_URL_RE = re.compile(
    r"href=\"(https?://[^\"']+\.torrent)\"")
_TORRENT_ROW_RE = re.compile(
    r"<tr[^>]*>(.*?)</tr>", re.S)
_TORRENT_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_SEEDS_RE = re.compile(r"Seeds?:?\s*(\d+)", re.I)
_POSTED_NAME_RE = re.compile(r"Posted:.*?\n(.*?)\nClose", re.S)
_HATH_LINK_RE = re.compile(r"href=\"(https?://[^\"']+?/archive/[^\"]+)\"")
_COST_RE = re.compile(r"Download Cost:\s*([\d,]+)\s*GP", re.I)
_FETCH_TIMEOUT = 25.0
_STALE = {"running": False}


def parse_gallery_ref(text):
    """画廊 URL → (gid, token)；认不出返回 None。"""
    m = _GALLERY_RE.search(str(text or ""))
    if not m:
        return None
    return int(m.group(1)), m.group(2)


def _proxy_url():
    return (os.environ.get("TG_PROXY") or "").strip() or None


def _headers(extra=None):
    h = {"User-Agent": config.WNACG_UA}
    if extra:
        h.update(extra)
    return h


# ============================================================
# 种子路
# ============================================================
def parse_torrent_page(html):
    """gallerytorrents 页 → [(seeds, name, torrent_url)]，按 seeds 降序。

    页面是表格（Posted/Size/Seeds/Peers/Downloads/Uploader + 内层名字行），
    每行一个 .torrent 链接（2026-10-24 实测形态）。"""
    out = []
    for row_m in _TORRENT_ROW_RE.finditer(html or ""):
        row = row_m.group(1)
        u = _TORRENT_URL_RE.search(row)
        if not u:
            continue
        seeds = 0
        sm = _SEEDS_RE.search(row)
        if sm:
            seeds = int(sm.group(1))
        cells = _TORRENT_CELL_RE.findall(row)
        # 内层 .zip/.rar 名（种子内容名）优先；无则用链接段
        name = ""
        for c in cells:
            cm = re.search(r"([^\"]+\.(?:zip|rar|7z))", c)
            if cm:
                name = cm.group(1).strip()
                break
        out.append((seeds, name, u.group(1)))
    out.sort(key=lambda t: -t[0])
    return out


def fetch_torrents_blocking(gid, token):
    """拉种子页并解析（阻塞，线程跑）。失败抛 RuntimeError。"""
    url = f"https://e-hentai.org/gallerytorrents.php?gid={int(gid)}&t={token}"
    proxy = _proxy_url()
    if proxy is None:
        raise RuntimeError("未配置 TG_PROXY——e-hentai 须走代理访问")
    try:
        r = httpx.get(url, proxy=proxy, trust_env=False,
                      headers=_headers(), timeout=_FETCH_TIMEOUT,
                      follow_redirects=True)
    except Exception as e:
        raise RuntimeError(f"种子页请求失败：{type(e).__name__}: {e}") from e
    if r.status_code != 200:
        raise RuntimeError(f"种子页 HTTP {r.status_code}")
    torrents = parse_torrent_page(r.text)
    if not torrents and "No torrents" not in r.text and "沒有" not in r.text:
        # 页面在但没解析出行：可能改版——如实报
        raise RuntimeError("种子页解析不到条目（疑似改版，报维护者）")
    return torrents


# ============================================================
# 原包路（archiver → hath 直链 → httpx 下载）
# ============================================================
def _cookie_header():
    ck = (getattr(config, "EHENTAI_COOKIE", "") or "").strip()
    return {"Cookie": ck} if ck else None


def fetch_archiver_cost_blocking(gid, token):
    """GET archiver 页：解析 GP 费用（顺带验登录）。返回 int；无登录抛错。"""
    url = f"https://e-hentai.org/archiver.php?gid={int(gid)}&token={token}"
    ck = _cookie_header()
    if ck is None:
        raise RuntimeError("未配置 e-hentai cookie（/ehentai cookie <粘贴>）")
    r = httpx.get(url, proxy=_proxy_url(), trust_env=False,
                  headers=_headers(ck), timeout=_FETCH_TIMEOUT,
                  follow_redirects=False)
    if r.status_code in (301, 302) and "bounce_login" in r.headers.get(
            "location", ""):
        raise RuntimeError("cookie 已失效（登录墙拦截）——重新发 /ehentai cookie")
    if r.status_code != 200:
        raise RuntimeError(f"archiver 页 HTTP {r.status_code}")
    m = _COST_RE.search(r.text)
    # 两个 Download Cost（原包/重采样）取第一个（原包）
    return int(m.group(1).replace(",", "")) if m else 0


def fetch_archive_link_blocking(gid, token):
    """POST 原包下载 → 解析 hath.network 中转页 → zip 直链（?start=1）。

    返回 (zip_url, cost_gp)。"""
    url = f"https://e-hentai.org/archiver.php?gid={int(gid)}&token={token}"
    ck = _cookie_header()
    if ck is None:
        raise RuntimeError("未配置 e-hentai cookie")
    proxy = _proxy_url()
    # ① POST 提交（原包）→ 302 到 hath 中转页
    with httpx.Client(proxy=proxy, trust_env=False, timeout=_FETCH_TIMEOUT,
                      headers=_headers(ck), follow_redirects=True) as cli:
        r = cli.post(url, data={
            "dltype": "org", "dlcheck": "Download Original Archive"})
        body = r.text
    m = _HATH_LINK_RE.search(body)
    if not m:
        if "successfully prepared" in body or "start=1" in body:
            # 已在直链页：链接可能就是当前 URL（?start 缺省）
            m2 = re.search(r"(https?://[^\"' ]+?/archive/[^\"' ]+)", body)
            if not m2:
                raise RuntimeError("hath 页解析不到下载链接（报维护者）")
            link = m2.group(1)
        else:
            raise RuntimeError("原包生成失败（GP 不足或改版）——看 /ehentai cookie")
        link = m.group(1)
    else:
        link = m.group(1)
    if "start=" not in link:
        link += "?start=1"
    cost = 0
    cm = _COST_RE.search(body)
    if cm:
        cost = int(cm.group(1).replace(",", ""))
    return link, cost


def download_zip_blocking(zip_url, dest):
    """hath 直链 → 本地 dest（.part 续传 + 尺寸校验）。返回字节数。"""
    from .wnacg import download_zip_blocking as _dl
    return _dl(zip_url, dest)


# ============================================================
# 执行体：种子优先 → 原包回退
# ============================================================
async def process_gallery(gid, token, author=None):
    """完整流程（worker 协程）。返回回执文案。"""
    from . import cd2_api
    from . import torrent_offline
    from . import torrent_util
    from .naming import format_size, sanitize_filename
    where = f"/115open/漫画/{author}" if author else "/115open/漫画"

    # ① 种子路
    try:
        torrents = await asyncio.to_thread(fetch_torrents_blocking, gid, token)
    except RuntimeError as e:
        return f"{TEXT_PREFIX} ❌ {e}"
    if torrents:
        seeds, name, turl = torrents[0]
        ih = torrent_util.infohash_from_url(turl)
        if not ih:
            # 尾段无 infohash：下载种子文件解析（极少见的非 ehtracker 源）
            try:
                data = await asyncio.to_thread(
                    torrent_offline._download_torrent_bytes, turl)
                magnet, info = await asyncio.to_thread(
                    torrent_offline.torrent_to_magnet, data)
                name = name or info["name"]
            except Exception as e:
                return f"{TEXT_PREFIX} ❌ 种子解析失败：{e}"
        else:
            magnet = torrent_util.magnet_from(name or f"ehentai_{gid}", ih)
        ok, err = await asyncio.to_thread(
            cd2_api.add_offline_download, magnet, where)
        if not ok and "10008" not in (err or "") and "已存在" not in (err or ""):
            return f"{TEXT_PREFIX} ❌ 离线提交被拒：{err}"
        torrent_offline.spawn_watch(where, label=name or f"ehentai_{gid}")
        return (f"{TEXT_PREFIX} 🧲 已走种子（最优 {seeds} seeds）\n"
                f"名称：{(name or f'ehentai_{gid}')[:60]}\n"
                f"→ 115 离线拉取到 {where}（完成后会通知你）")

    # ② 原包路
    try:
        cost = await asyncio.to_thread(fetch_archiver_cost_blocking, gid, token)
    except RuntimeError as e:
        return (f"{TEXT_PREFIX} 该画廊无种子，且原包路不可用：{e}\n"
                "提供 cookie 后即可走原包（/ehentai cookie <粘贴>）")
    try:
        zip_url, cost2 = await asyncio.to_thread(
            fetch_archive_link_blocking, gid, token)
    except RuntimeError as e:
        return f"{TEXT_PREFIX} ❌ 原包生成失败：{e}"
    cost = cost or cost2
    # 下载 → 写挂载（漫画/作者，目录不存在自动建）→ gRPC 对账
    staging = os.path.join(config.RUNTIME_DIR, "ehentai")
    os.makedirs(staging, exist_ok=True)
    fname = sanitize_filename(f"ehentai_{gid}.zip")
    local = os.path.join(staging, fname)
    try:
        size = await asyncio.to_thread(download_zip_blocking, zip_url, local)
    except Exception as e:
        return f"{TEXT_PREFIX} ❌ 原包下载失败：{e}"
    from .extract_worker import mount_of
    mount_dir = mount_of(where)
    if mount_dir is None:
        return (f"{TEXT_PREFIX} ⚠️ 已下载 {format_size(size)} 但挂载不可用，"
                f"本地暂存 {local}（挂载恢复后重发即可续传秒传）")
    os.makedirs(mount_dir, exist_ok=True)
    target = os.path.join(mount_dir, fname)
    await asyncio.to_thread(_copy_file, local, target)
    try:
        os.remove(local)
    except OSError:
        pass
    return (f"{TEXT_PREFIX} 📦 原包已下载并上传 115\n"
            f"位置：{where}/{fname}｜{format_size(size)}｜花费 {cost} GP\n"
            "（zip 未自动解压——需要的话发 /115x "
            f"{where}）")


def _copy_file(src, dst):
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            b = fi.read(4 * 1024 * 1024)
            if not b:
                break
            fo.write(b)


# ============================================================
# 命令
# ============================================================
def command_reply_sync(cmd_text):
    """/ehentai <URL> [作者]｜/ehentai cookie <粘贴>（同步入口）。"""
    raw = str(cmd_text or "").strip()
    parts = raw.split(None, 1)
    body = parts[1].strip() if len(parts) > 1 else ""
    if body.lower().startswith("cookie"):
        val = body.split(None, 1)[1].strip() if len(body.split()) > 1 else ""
        if not val:
            return (f"{TEXT_PREFIX} 用法：/ehentai cookie <ipb_member_id=x;"
                    " ipb_pass_hash=y; …>（浏览器 F12 → Network → 请求头复制）")
        err = config.save_ehentai_cookie(val)
        if err:
            return f"{TEXT_PREFIX} ❌ {err}"
        return f"{TEXT_PREFIX} 🍪 cookie 已保存（原包路可用）"
    return None    # 下载流程异步，见 command_reply


async def command_reply(cmd_text):
    """/ehentai 主入口。"""
    raw = str(cmd_text or "").strip()
    parts = raw.split(None, 1)
    body = parts[1].strip() if len(parts) > 1 else ""
    if not body:
        return (f"{TEXT_PREFIX} 用法：/ehentai <画廊URL> [作者]\n"
                "　例：/ehentai https://e-hentai.org/g/4277638/baedd2e998/ 作者\n"
                "有种子→115离线到 漫画/[作者]/；无种子→原包（需 cookie，"
                "耗 GP）；/ehentai cookie <粘贴> 配置")
    sync_out = command_reply_sync(raw)
    if sync_out:
        return sync_out
    toks = body.split()
    ref = parse_gallery_ref(toks[0])
    if ref is None:
        return f"{TEXT_PREFIX} 认不出画廊 URL（形如 /g/<id>/<token>/）"
    author = " ".join(toks[1:]).strip()
    from .naming import sanitize_filename
    author = sanitize_filename(author)[:60] if author else None
    gid, token = ref
    _STALE["running"] = True
    try:
        return await process_gallery(gid, token, author=author)
    finally:
        _STALE["running"] = False
