"""哔哩哔哩视频下载 —— /bili（2026-10-29；合集/系列 2026-10-30）。

**链路**：yt-dlp（venv 已装 2025.10.14，子进程）+ 登录 cookie
（tg_secrets.json 的 bilibili_cookie，SESSDATA 等；设置菜单「🌐 B站Cookie」
粘贴/浏览器导入，或 scripts/get_cookie.py 配置）。

/bili <BV号|URL|b23短链>          —— 下载单个视频（多P视频全收各分P）
/bili <合集|系列 URL>             —— 下载整个合集/系列（增量：重跑只收新视频）
/bili                            —— 状态（在跑进度 / 用法）

合集/系列 URL 形态：``space.bilibili.com/<UID>/channel/collectiondetail?sid=X``
（合集）/ ``…/seriesdetail?sid=X``（系列）——yt-dlp 的
BilibiliCollectionList/SeriesList extractor（2026-10-30 实测本机可枚举）。
b23 短链指向合集也自动识别（resolve 后过同一正则）。收藏夹仍不支持
（混杂他人内容，语义不同，用户定档不做）。

**画质档位由 cookie 账号决定**（工具决定不了）：游客 480P、普通登录
1080P、大会员解锁 1080P60/4K/HDR/杜比/8K。cookie 串写 cookies.txt 时强制
CURRENT_QUALITY=120 + CURRENT_FNVAL=4048——2025-02 起 B站 playurl 每次
只返回一个高级档、由该 cookie 决定给哪档（yt-dlp issue #12492 未修复，
这是官方 workaround；不带它 4K 档缺失）。

**关键坑（2026-10-29 实测）**：macOS 上 Python 会读**系统代理**
（scutil，Hiddify 12334），B 站对代理出口请求一律 412 Precondition
Failed——UA/cookie/impersonate 都救不了，yt-dlp 必须显式 ``--proxy ""``。
直连 API（nav/b23 解析）同理用 ProxyHandler({}) 显式禁代理。X/Pixiv 相反
要走代理——两条链路的代理纪律互斥，别混。

落盘：单视频 ``DOWNLOAD_DIR/B站/<UP主>/<日期>_<BV号>_<标题>.mp4``；
合集/系列多一层 ``B站/<UP主>/<合集名>/``。CD2 备份自动上 115。

**去重纪律（2026-10-30 定档：单视频也接 archive）**：``bili_archive.txt``
（BV 全局唯一，全模式共用）增量去重——合集重跑只收新视频（追更/断点续
传），单视频重发同链接 archive 命中即跳过（本地被 CD2 删源后也不重下；
想找回文件需清掉 archive 里该 BV 行，跳过提示带指引）。新增数 = archive
行数差（精确）。合集逐视频间 --sleep-interval 2（B站 429 风控严格）。

**纪律**：后台 task（防重入 + 完成通知，与 /x 同款）；串行执行；
无 cookie 时 yt-dlp 不带 --cookies（游客直连实测可下 480P）。
"""
import asyncio
import json
import os
import re
import shutil
import time
import urllib.request

from . import config
from .log import logger
from .x_dl import _run_proc

YTDLP = os.path.join(os.path.dirname(config.__file__),
                     "..", ".venv", "bin", "yt-dlp")
BILI_ROOT_NAME = "B站"                  # DOWNLOAD_DIR 下的子目录名
ARCHIVE_FILE = "bili_archive.txt"       # 合集/系列增量去重（BV 全局唯一）
# 番剧/普通视频 URL 直传 yt-dlp（BV 正则只做归一化，不拦 bangumi）
BV_RE = re.compile(r"BV[0-9A-Za-z]{8,12}")
# 合集/系列（collectiondetail/seriesdetail?sid=）；收藏夹 favlist 明确不收
PLAYLIST_RE = re.compile(
    r"space\.bilibili\.com/(\d+)/channel/(collectiondetail|seriesdetail)"
    r"\?sid=(\d+)")
SINGLE_TIMEOUT = 7200        # 单视频 2h
PLAYLIST_TIMEOUT = 86400     # 合集几十上百个视频，给到 24h
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/131.0.0.0 Safari/537.36")

_BILI_RUNNING = {}   # 在途进度视图（yt-dlp 输出解析线程实时写；非锁——
                     # 串行由 worker 队列保证；/api/status 读它渲染进度条）

# ── SerialTaskWorker 接线（2026-11-05 迁移：内存锁+待办位 → DB 队列）──
# 排队/重试/租约/重启恢复全部收进 bili_tasks 表（runtime_db.TaskDomain），
# 与 wnacg/asmr 同一套实战语义；_BILI_RUNNING 只承担进度展示。
from . import runtime_db   # noqa: E402
from .task_worker import SerialTaskWorker   # noqa: E402


class _BiliWorker(SerialTaskWorker):
    name = "📺 bili"

    def __init__(self):
        super().__init__()
        self.poll_seconds = 5.0
        self.idle_sleep = 5.0    # 任务间 5s 间隔（防 429，原待办 kick 语义）
        self.inner_task = None   # 在途 yt-dlp 线程包装（/bili stop 取消用）
        self._stop_flag = False  # True：取消时标 FAILED 终态（不回队列）

    def startup_banner(self):
        return ("📺 bili worker 已启动（串行一次一条，任务间 5s，"
                "yt-dlp archive 增量去重）")

    def claim(self):
        return runtime_db.BILI_TASKS.claim_next()

    def release(self, task):
        return runtime_db.BILI_TASKS.release(task["url"])

    def recover_startup(self):
        runtime_db.BILI_TASKS.recover_expired()

    async def process(self, task):
        from . import notify
        global _BILI_RUNNING
        raw = task["url"]
        # 延迟解析（b23 短链/合集归属在领取时才展开——排队不被解析阻断）
        url, kind, err = parse_input(raw)
        if err:
            # 无效链接终态（不自动重试——链接不会自己变对）；可 /bili retry
            runtime_db.BILI_TASKS.fail(raw, err, max_attempts=1)
            try:
                await notify.notify_user(
                    f"📺 B站 链接无法识别：{raw[:60]}\n{err}")
            except Exception:
                pass
            return
        label = _label_for(url, kind)
        runtime_db.BILI_TASKS.update(raw, kind=kind, label=label)
        _BILI_RUNNING = {"label": label, "kind": kind,
                         "started": time.time(), "pct": 0.0}
        playlist = kind in ("合集", "系列")
        try:
            self.inner_task = asyncio.ensure_future(
                asyncio.to_thread(run_bili, url, playlist))
            try:
                ok, note = await self.inner_task
            except asyncio.CancelledError:
                # 两种取消：/bili stop（_stop_flag → FAILED 终态）与停机
                # （无标志 → 框架 release 回 PENDING，重启自动续跑）。
                # 先落终态再 raise——框架 release 的 WHERE status=
                # PROCESSING 对已 FAILED 的行是 no-op
                if self._stop_flag:
                    runtime_db.BILI_TASKS.fail(raw, "用户停止",
                                               max_attempts=1)
                    self._stop_flag = False
                raise
            except Exception as e:
                logger.exception(f"[bili] 后台执行异常（{label}）")
                runtime_db.BILI_TASKS.fail(raw, f"{type(e).__name__}: {e}")
                try:
                    await notify.notify_user(
                        f"📺 B站 下载异常（{label}）——已转自动重试："
                        f"{type(e).__name__}: {e}")
                except Exception:
                    pass
                return
            if ok:
                runtime_db.BILI_TASKS.complete(raw)
                from .history import append_history
                append_history(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} | B站 | "
                    f"{label} | {_BILI_RUNNING.get('elapsed', '?')} | "
                    "来源：B站下载")
                where = ("落盘 B站/<UP主>/<合集名>/"
                         if playlist else "落盘 B站/<UP主>/")
                text = (f"📺 B站 下载完成：{label}"
                        f"（{_BILI_RUNNING.get('elapsed', '?')}）\n"
                        f"{where}（CD2 会自动备份上 115）")
            else:
                row = runtime_db.BILI_TASKS.get(raw) or {}
                runtime_db.BILI_TASKS.fail(raw, note or "下载失败")
                text = (f"❌ B站 下载失败：{label}——已转自动重试"
                        f"（第 {row.get('attempts', '?')}/5 次）")
            if note:
                text += "\n" + note
            try:
                await notify.notify_user(text)
            except Exception:
                pass
        finally:
            self.inner_task = None
            _BILI_RUNNING = {}


_WORKER = _BiliWorker()


def _label_for(url, kind):
    """解析结果 → 展示名（合集sidN / BV号 / URL 尾段）。"""
    if kind in ("合集", "系列"):
        pm = PLAYLIST_RE.search(url)
        return f"{kind}sid{pm.group(3)}" if pm else url[-20:]
    m = BV_RE.search(url)
    return m.group(0) if m else url.rsplit("/", 1)[-1][:40]


async def bili_worker_loop():
    """常驻循环（app.py create_task 持强引用；与 wnacg_worker_loop 同款）。"""
    await _WORKER.run()


def release_inflight():
    """停机兜底：外层在协程取消后再调一次（幂等）。"""
    return _WORKER.release_inflight()


def bili_stop():
    """/bili stop：取消在途并标 FAILED 终态（不自动重试），队列继续下一条。

    与旧语义差异（2026-11-05 迁移）：旧=放弃任务需重发链接；新=任务标失败
    可 /bili retry <id> 重投；队列后续任务不受影响自动继续。"""
    if not _BILI_RUNNING:
        return "ℹ️ 没有 B站 下载在跑"
    label = _BILI_RUNNING.get("label", "?")
    _WORKER._stop_flag = True
    if _WORKER.inner_task is not None:
        _WORKER.inner_task.cancel()
    left = runtime_db.BILI_TASKS.count(status="PENDING")
    return (f"🛑 已停止「{label}」（不再自动重试，/bili retry <id> 可重投）；"
            f"队列剩 {left} 条继续自动跑")


def bili_pending_list() -> list:
    """待办队列快照（API/看板用）——DB 队列 PENDING 行（旧内存版替代）。"""
    try:
        rows = runtime_db.BILI_TASKS.list(status="PENDING", limit=10)
    except Exception:
        return []
    return [{"id": r["id"], "url": r["url"], "kind": r.get("kind")}
            for r in rows]

# issue #12492 workaround：CURRENT_QUALITY=120（4K，非大会员时 API 自动
# 降回账号可用最高档）；fnval 4048 = DASH|HDR|4K|杜比|8K 全开
FORCE_COOKIES = {"CURRENT_QUALITY": "120", "CURRENT_FNVAL": "4048"}
NETSCAPE_EXPIRY = 2145916800  # 2038 前的最大值：会话 cookie 写成远期


def _bili_root():
    return os.path.join(config.DOWNLOAD_DIR, BILI_ROOT_NAME)


def _cookie_path():
    return os.path.join(config.RUNTIME_DIR, "bili_cookies.txt")


def _archive_path():
    return os.path.join(config.RUNTIME_DIR, ARCHIVE_FILE)


def _archive_count():
    """archive 里已归档的 BV 数（缺失/坏文件按 0——新增计数以它为基线）。"""
    try:
        with open(_archive_path(), "r", encoding="utf-8") as f:
            return sum(1 for ln in f if ln.strip() and not ln.startswith("#"))
    except OSError:
        return 0


def build_cookie_file():
    """bilibili_cookie 字符串 → Netscape cookies.txt（附画质档位 cookie）。

    返回 (ok, msg)。未配置返回 (False, "未配置")——调用方不带 --cookies
    走游客路（直连可下 480P）。域固定 .bilibili.com/path=/：yt-dlp 发请求
    时按域匹配携带，SESSDATA/buvid/DedeUserID 全覆盖。
    """
    raw = (getattr(config, "BILIBILI_COOKIE", "") or "").strip()
    if not raw:
        return False, "未配置"
    kv = {}
    for part in raw.split(";"):
        name, sep, val = part.strip().partition("=")
        if sep and name.strip():
            kv[name.strip()] = val.strip()
    if not kv:
        return False, "未配置"
    kv.update(FORCE_COOKIES)
    lines = ["# Netscape HTTP Cookie File"]
    for n, v in kv.items():
        lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t{NETSCAPE_EXPIRY}\t{n}\t{v}")
    out = _cookie_path()
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, out)
    return True, f"{len(kv)} 条（含画质档位 cookie）"


def _direct_opener():
    """绕开系统代理的 urllib opener（macOS sysconf 代理会让 B站 412）。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def account_info():
    """nav 接口查账号态 (is_login, vip_status, uname)；查询失败返回 None。

    只为文案提示，不阻塞下载：cookie 失效就按游客画质下。
    """
    raw = (getattr(config, "BILIBILI_COOKIE", "") or "").strip()
    if not raw:
        return (False, 0, "")
    req = urllib.request.Request(
        "https://api.bilibili.com/x/web-interface/nav",
        headers={"User-Agent": _UA, "Cookie": raw})
    with _direct_opener().open(req, timeout=10) as r:
        data = json.load(r)
    d = data.get("data") or {}
    return (bool(d.get("isLogin")), int(d.get("vipStatus") or 0),
            str(d.get("uname") or ""))


def resolve_short(link):
    """b23.tv 短链 → 最终 URL（GET 跟随 302；直连不走代理）。"""
    req = urllib.request.Request(link, headers={"User-Agent": _UA})
    with _direct_opener().open(req, timeout=15) as r:
        r.read(1024)  # 短链响应体极小，读了才能拿到最终 url
    return r.geturl()


def probe_season(bvid):
    """查视频的合集归属（view API 的 ugc_season，直连不走代理）。

    返回 (sid, owner_mid, 合集标题) 或 None（不属于合集/查询失败——
    查询失败绝不阻塞单视频下载）。"""
    try:
        req = urllib.request.Request(
            f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
            headers={"User-Agent": _UA})
        with _direct_opener().open(req, timeout=8) as r:
            data = json.load(r)
        d = data.get("data") or {}
        season = d.get("ugc_season") or {}
        mid = (d.get("owner") or {}).get("mid")
        sid = season.get("id")
        if sid and mid:
            return int(sid), int(mid), str(season.get("title") or "")
    except Exception as e:
        logger.warning(f"[bili] 合集归属探测失败（按单视频处理）：{e}")
    return None


def parse_input(arg):
    """BV号/URL/b23 短链/合集·系列链接 → (规范URL, kind, err)。

    kind："合集" / "系列"（播放列表模式，增量去重）或 "video"。
    BV 会先探合集归属（view API ugc_season）——合集里的视频自动转
    collectiondetail 走合集管线（2026-10-31，b23 分享的合集链接只指向
    单视频页，曾只下到 1 条）。
    """
    arg = str(arg or "").strip()
    if not arg:
        return "", "video", "用法：/bili <BV号|URL|b23短链|合集链接>"
    if "b23.tv" in arg and "bilibili.com" not in arg:
        # 分享文本常带标题前缀（app 分享直达：「【xx】笑纳 https://b23.tv/x」）
        # ——先抠出短链本体再展开，整段字符串喂 urllib 会炸
        # unknown url type（2026-11-01 API 分享直达实测）。
        m = re.search(r"https?://b23\.tv/\S+", arg)
        if m:
            arg = m.group(0).rstrip("。，！？；)")
        try:
            arg = resolve_short(arg)
        except Exception as e:
            return "", "video", f"b23 短链解析失败：{type(e).__name__}: {e}"
    m = PLAYLIST_RE.search(arg)
    if m:
        kind = "合集" if m.group(2) == "collectiondetail" else "系列"
        # 从匹配组重建规范 URL（丢掉 share_medium 等噪声参数，保留 sid）
        url = (f"https://space.bilibili.com/{m.group(1)}/channel/"
               f"{m.group(2)}?sid={m.group(3)}")
        return url, kind, None
    m = BV_RE.search(arg)
    if m:
        bvid = m.group(0)
        # 合集归属探测（2026-10-31）：b23/分享链接指向的是合集里的某个
        # 视频（share_from=season），用户意图是整个合集——单视频模式只
        # 展开分P 不扩季，曾只下到 1 条。view API 带出 ugc_season → 转
        # collectiondetail 走既有合集管线（增量/archive 全复用）。
        season = probe_season(bvid)
        if season:
            sid, mid, _title = season
            return (f"https://space.bilibili.com/{mid}/channel/"
                    f"collectiondetail?sid={sid}", "合集", None)
        return f"https://www.bilibili.com/video/{bvid}", "video", None
    if "bilibili.com" in arg and ("/video/" in arg or "bangumi/play" in arg):
        return arg.split("?")[0], "video", None   # 番剧 ep/ss 或带参 URL 直传
    return "", "video", "未识别到 BV 号或合集/系列链接（收藏夹暂不支持）"


def _ffmpeg_path():
    """ffmpeg 定位：PATH → Homebrew 常见位置。找不到返回 None（yt-dlp
    合并音视频必需，缺了会在输出里报清晰错误）。"""
    found = shutil.which("ffmpeg")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.exists(cand):
            return cand
    return None


def _ytdlp_cmd(url, playlist=False):
    """下载命令。画质：bv*+ba 取最高档视频+音频；--proxy "" 是硬性要求
    （见模块 docstring 的 412 实测）。文件名：日期_BV_标题。

    playlist=True（合集/系列）：多一层 <合集名> 目录 + download-archive
    增量 + 逐视频 2s 限速。"""
    if playlist:
        out_tpl = os.path.join(
            _bili_root(), "%(uploader|未知UP主)s", "%(playlist_title|合集)s",
            "%(upload_date>%Y-%m-%d)s_%(id)s_%(title).50B.%(ext)s")
    else:
        out_tpl = os.path.join(
            _bili_root(), "%(uploader|未知UP主)s",
            "%(upload_date>%Y-%m-%d)s_%(id)s_%(title).50B.%(ext)s")
    cmd = [YTDLP,
           "--proxy", "",              # 系统代理（Hiddify）→ B站 412，必须显式置空
           "--sleep-requests", "1",    # B站限流严格（429），请求间留 1s
           # 全模式 archive 去重（2026-10-30 用户定档）：BV 全局唯一，
           # 单视频重发同链接 → archive 命中跳过（本地删源后也不重下；
           # 想找回得清 archive 该行，跳过提示里带指引）
           "--download-archive", _archive_path(),
           "-f", "bv*+ba/b",
           "--merge-output-format", "mp4",
           "--no-progress",
           "-o", out_tpl,
           ]
    if playlist:
        cmd += ["--yes-playlist",
                "--sleep-interval", "2"]   # 逐视频间 2s（B站 429 风控）
    elif "bangumi" in url:
        cmd += ["--no-playlist"]   # 番剧 ep URL 只下指向的一集（不扩整季）
    else:
        # 普通视频 URL 也全量（2026-10-30 用户定档）：多P全收——分P是
        # 独立条目（id=BV_pN，archive 键/文件名互不冲突）；单P视频展开
        # 仍只有它自己；实测合集内的视频不会误扩成整季（只展开为自身）
        cmd += ["--yes-playlist"]
    ffmpeg = _ffmpeg_path()
    if ffmpeg:
        cmd += ["--ffmpeg-location", ffmpeg]
    ok, _msg = build_cookie_file()
    if ok:
        cmd += ["--cookies", _cookie_path()]
    cmd.append(url)
    return cmd


def _classify_note(out):
    """从 yt-dlp 输出提炼画质/档位提示（None=没有要说的）。"""
    tail = (out or "").lower()
    if "you have to become a premium member" in tail:
        return ("⚠️ 4K/1080P高码率等档位缺失——未检测到大会员档 cookie"
                "（游客/普通登录拿不到该档，详见 /bili 裸命令说明）")
    if "412" in tail:
        return ("⚠️ 触发 B站 412 风控（代理没绕干净？--proxy \"\" 必须带上）")
    return None


def run_bili(url, playlist=False):
    """同步下载单个视频/合集（后台 task 的线程体）。返回 (ok, note)。

    新增数 = archive 行数差（精确：每个新下完的 BV 恰好记一行；重复
    链接命中 archive 零新增——单视频命中时 note 带跳过说明）。"""
    os.makedirs(os.path.join(_bili_root()), exist_ok=True)
    archive_before = _archive_count()

    def on_line(line):
        m = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
        if m:
            _BILI_RUNNING["pct"] = float(m.group(1))
        pm = re.search(r"\[download\] Downloading item (\d+) of (\d+)", line)
        if pm:
            _BILI_RUNNING["item"] = int(pm.group(1))
            _BILI_RUNNING["item_count"] = int(pm.group(2))
        for key in ("Destination:", "Merger:", "has already been downloaded"):
            if key in line:
                _BILI_RUNNING["last"] = line.rsplit("/", 1)[-1][:60]

    try:
        acct = account_info()
    except Exception as e:
        logger.warning(f"[bili] 账号态查询失败（不影响下载）：{e}")
        acct = None
    notes = []
    if acct is not None:
        if acct[0]:
            who = f"{acct[2]}（{'大会员' if acct[1] else '普通账号'}）"
            notes.append(f"👤 {who}｜画质取该账号可用最高档")
        else:
            notes.append("⚠️ cookie 未配置或已失效——游客画质（最高 480P）")

    timeout = PLAYLIST_TIMEOUT if playlist else SINGLE_TIMEOUT
    started = time.monotonic()
    code, tail, out = _run_proc(_ytdlp_cmd(url, playlist=playlist), on_line,
                                timeout=timeout, tag="bili")
    m, s = divmod(int(time.monotonic() - started), 60)
    _BILI_RUNNING["elapsed"] = f"{m}分{s:02d}秒"
    note = _classify_note(out)
    if note:
        notes.append(note)
    if code != 0:
        if playlist:
            added = _archive_count() - archive_before
            notes.append(f"中断前已新增 {added} 个（archive 已记录，"
                         "重跑同链接自动续）")
        return False, (f"yt-dlp 退出码 {code}\n{tail[-400:]}"
                       + ("\n" + "\n".join(notes) if notes else ""))
    added = _archive_count() - archive_before
    if playlist:
        notes.append(f"本次新增 {added} 个（archive 累计 "
                     f"{_archive_count()} 个）")
    elif added > 1:
        notes.append(f"多P视频：本次新增 {added} 个分P")
    elif added == 0:
        notes.append("♻️ 已下载过（archive 命中，跳过）——"
                     "如本地文件已删想重新获取：清掉 runtime/"
                     "bili_archive.txt 里该 BV 行后重发")
    return True, "\n".join(n for n in notes if n)


# 任意 bilibili.com / b23.tv 链接的快速识别（无网络探测，纯正则）——
# 收藏夹直接发链接即自动按 /bili 处理（2026-10-31 owner 定档）。宽匹配：
# 专栏/收藏夹等不支持的形态也会被拦进来给友好报错，而不是落进外链台账


def status_text():
    """进度（内存视图）+ 队列（DB 真相源）合一状态。"""
    head = ""
    if _BILI_RUNNING:
        label = _BILI_RUNNING.get("label", "?")
        started = _BILI_RUNNING.get("started")
        elapsed = ""
        if started:
            m, s = divmod(int(time.time() - started), 60)
            elapsed = f"｜已跑 {m}分{s:02d}秒"
        item = _BILI_RUNNING.get("item")
        item_count = _BILI_RUNNING.get("item_count")
        if item is not None and item_count:
            head = (f"📺 B站 下载进行中：{label}\n"
                    f"第 {item}/{item_count} 个，当前 {pct_text(_BILI_RUNNING)}"
                    f"{elapsed}")
        else:
            head = (f"📺 B站 下载进行中：{label}\n进度 "
                    f"{pct_text(_BILI_RUNNING)}{elapsed}")
        last = _BILI_RUNNING.get("last", "")
        if last:
            head += f"\n最新：{last}"
    try:
        rows = runtime_db.BILI_TASKS.list(limit=12)
    except Exception as e:
        return head or f"📺 bili 队列读取失败：{e}"
    if not rows and not head:
        return ("📺 /bili <BV号|URL|b23短链> —— 下载单个视频\n"
                "📺 /bili <合集|系列 URL> —— 下载整个合集/系列"
                "（增量，重跑只收新视频）\n"
                "画质由 cookie 账号决定：游客 480P｜登录 1080P｜"
                "大会员 1080P60/4K/HDR/8K\n"
                "cookie 配置：设置 → 🌐 B站Cookie（粘贴或从浏览器导入）\n"
                "⏳ 队列：空")
    from collections import Counter
    counts = Counter(r["status"] for r in rows)
    summary = " ".join(f"{s}={n}" for s, n in counts.items())
    lines = ([head] if head else []) + [f"⏳ 队列（{summary}，最近 "
                                        f"{len(rows)} 条）："]
    for r in rows:
        name = (r.get("label") or r["url"])[:44]
        err = f"｜{str(r['error'])[:24]}" if r.get("error") else ""
        lines.append(f"  #{r['id']} [{r['status']}] {name}{err}")
    lines.append("操作：/bili stop 停当前｜/bili retry <id|all>｜"
                 "/bili del <id>")
    return "\n".join(lines)[:3900]


def pct_text(running):
    """当前条目进度文本（合集里逐视频刷新）。"""
    return f"{running.get('pct', 0):.0f}%"


async def command_reply(cmd_text):
    """/bili <BV号|URL|b23短链|合集链接>；裸命令看状态；子命令 stop/retry/del。"""
    parts = str(cmd_text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if not arg:
        return status_text()
    head = arg.split()[0].lower()
    if head == "stop":
        return bili_stop()
    if head == "retry":
        toks = arg.split()
        target = toks[1] if len(toks) > 1 else ""
        if target == "all":
            n = runtime_db.BILI_TASKS.retry()
            return f"📺 已重投 {n} 条失败任务"
        if target.isdigit():
            n = runtime_db.BILI_TASKS.retry_by_id(int(target))
            return (f"📺 已重投 #{target}" if n
                    else f"ℹ️ #{target} 不是可重投的失败任务")
        return "📺 用法：/bili retry <id|all>（id 看 /bili 队列列表）"
    if head == "del":
        toks = arg.split()
        target = toks[1] if len(toks) > 1 else ""
        if target.isdigit():
            runtime_db.BILI_TASKS.delete(int(target))
            return f"📺 🗑 已移除 #{target}"
        return "📺 用法：/bili del <id>"
    return _submit(arg)


# 文本+链接混合形态（2026-11-05 owner：「[标题] xxx https://b23.tv/yyy」
# 的转发很常见）——提交时抽出链接再入队，队列键干净（同链接不同标题
# 判重命中），worker 领取后 parse_input 再做完整解析
_BILI_URL_RE = re.compile(
    r"https?://[^\s（）()【】\[\]]*(?:bilibili\.com|b23\.tv)[^\s（）()【】\[\]]*",
    re.I)


def looks_like_bili(text):
    """文本里是否含 B 站链接（app.py 自动识别入口判定）。"""
    return bool(_BILI_URL_RE.search(str(text or "")))


def _extract_bili_url(text):
    """混合文本 → 首个 B站链接（剥尾随标点）；无则 None。"""
    m = _BILI_URL_RE.search(str(text or ""))
    if not m:
        return None
    return m.group(0).rstrip("。，,;；！!？?~～").rstrip("。，")


def _submit(arg):
    """提交链接入队（DB 队列，worker 串行领取；重复链接任何状态都拒）。

    文本+链接混合形态自动抽链接；b23 短链需网络解析（延迟到 worker
    领取时）；其余形态本地正则预检——收藏夹/专栏等不支持形态当场
    友好报错，不入队后失败。"""
    arg_norm = _extract_bili_url(arg) or str(arg or "").strip()
    if "b23.tv" not in arg_norm:
        _, _, err = parse_input(arg_norm)
        if err:
            return f"❌ {err}"
    if runtime_db.BILI_TASKS.enqueue(arg_norm):
        pos = runtime_db.BILI_TASKS.count(status="PENDING")
        return (f"📺 已入队（第 {pos} 位）：{arg_norm[:50]}\n"
                "串行执行（任务间 5s 防风控）；完成会通知。"
                "进度随时 /bili 查看")
    row = runtime_db.BILI_TASKS.get(arg_norm) or {}
    st, rid = row.get("status", "?"), row.get("id", "?")
    if st == "FAILED":
        return (f"⏭️ 该链接已在队列且失败过（#{rid}）——"
                f"/bili retry {rid} 重投")
    return f"⏭️ 该链接已在队列中（#{rid}，{st}）——跑完自动继续下一条"


async def auto_reply(text):
    """收藏夹直接发 B站链接（无 /bili 前缀）→ 自动入队（worker 串行）。"""
    return "🔗 已自动识别 B站链接（/bili 可省略）\n" + _submit(text)


def is_bili_command(cmd_text):
    head = str(cmd_text or "").split()[0] if str(cmd_text or "").strip() else ""
    return head.lower() == "/bili"
