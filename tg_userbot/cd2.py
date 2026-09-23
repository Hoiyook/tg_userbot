"""CloudDrive2（CD2）集成：独立进程启停 + 备份记录解析。

【启动 CD2】【停止 CD2】【🗂 备份记录】三个 bot 菜单按钮的动作主体。CD2
独立程序路径 / 管理端口 / 备份日志目录都含本机用户名，属机器相关敏感配置，
只从 gitignore 的 tg_secrets.json 的 `cd2` 段读取（config._SECRET_CONFIG，
**调用时**读模块对象属性，供测试 monkeypatch）——绝不入代码不入库。

启停靠端口 TCP 探测 + `ps` 按可执行完整路径匹配 PID（主进程与 Start-Service
子进程同路径一并命中，天然避开系统 iCloud 同名进程），SIGTERM 优雅退出、
超时 SIGKILL 兜底。备份记录解析 backup.<日期>.log 里的逐文件删除源行。
进程句柄 state._CD2_PROC 仅防后台进程被 GC 回收。
"""
import os
import re
import time
import asyncio
import signal
import subprocess
from datetime import date, datetime, timedelta

from . import config
from . import state
from .config import CD2_LAUNCH_LOG
from .log import logger


def cd2_config():
    """读取 CD2 启动配置，返回 (command, port)。command 为空串表示未配置。"""
    cfg = config._SECRET_CONFIG.get("cd2") or {}
    command = (cfg.get("command") or "").strip()
    port = cfg.get("port") or 19798
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 19798
    return command, port


def cd2_log_dir():
    """读取 CD2 备份日志目录（tg_secrets.json 的 cd2.log_dir）；空串=未配置。

    备份逐文件记录在 <数据目录>/log/backup.<日期>.log 里，目录含本机用户名，
    故也属敏感配置放 tg_secrets.json，不入库。
    """
    cfg = config._SECRET_CONFIG.get("cd2") or {}
    return (cfg.get("log_dir") or "").strip()


async def cd2_is_running(port):
    """探测 CD2 是否已在运行：管理端口能连上即视为运行中。"""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=2
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


def cd2_launch(command):
    """后台启动 CD2 独立进程，脱离本进程组（随 userbot 退出不会被连带终止）。

    输出追加写入 RUNTIME_DIR/cd2_launch.log 便于排查。返回日志文件路径。
    进程句柄存 state._CD2_PROC（无人读取，仅防 GC 回收后台进程）。
    """
    command = os.path.expanduser(command)
    log_path = CD2_LAUNCH_LOG
    log_file = open(log_path, "a", encoding="utf-8")
    log_file.write(
        f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 启动 CD2：{command}\n"
    )
    log_file.flush()
    state._CD2_PROC = subprocess.Popen(
        [command],
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
        close_fds=True,
    )
    return log_path


async def cd2_start_or_status():
    """【启动 CD2】动作主体：已在运行 → 提示无需重启；否则启动并轮询端口。"""
    command, port = cd2_config()
    if await cd2_is_running(port):
        return f"✅ CloudDrive2 已在运行（端口 {port}），无需重启"
    if not command:
        return (
            "❌ 未配置 CD2 启动路径：请在 tg_secrets.json 的 cd2 段填 "
            "command（独立程序绝对路径）后重试"
        )
    try:
        log_path = cd2_launch(command)
    except Exception as e:
        return f"❌ 启动 CD2 失败：{type(e).__name__}: {e}"
    for _ in range(20):
        await asyncio.sleep(1.5)
        if await cd2_is_running(port):
            return f"✅ CloudDrive2 已启动（端口 {port}）"
    return (
        f"⏳ 启动命令已执行，但 {port} 端口约 30 秒内未就绪，"
        f"可能仍在初始化。启动日志：{log_path}"
    )


def _cd2_pids_from_ps_output(ps_output, command):
    """从 `ps -eo pid=,command=` 的文本里筛出可执行路径为 command 的进程 PID。

    主进程与其 Start-Service 子进程都是同一个可执行文件，都会被匹配到。
    用“完整路径开头”匹配，避免误伤系统里名称带 clouddrive 的 iCloud 进程
    （那些是 /System/... 下不同的程序）。返回 PID 列表，纯函数可单测。
    """
    pids = []
    for line in ps_output.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_str, _, cmd = line.partition(" ")
        if cmd.startswith(command):
            try:
                pids.append(int(pid_str))
            except ValueError:
                pass
    return pids


def _cd2_pids():
    """返回本机正在运行的 CD2 进程 PID 列表（未配置或未启动时返回空）。"""
    command, _ = cd2_config()
    command = os.path.expanduser(command or "")
    if not command:
        return []
    try:
        ps_output = subprocess.run(
            ["ps", "-eo", "pid=,command="], capture_output=True, text=True
        ).stdout
    except Exception:
        return []
    return _cd2_pids_from_ps_output(ps_output, command)


async def cd2_stop_or_status():
    """【停止 CD2】动作主体：未运行 → 提示；运行中 → SIGTERM，
    管理端口未按时关闭再 SIGKILL 兜底。"""
    _, port = cd2_config()
    running = await cd2_is_running(port)
    pids = _cd2_pids()
    if not running and not pids:
        return "✅ CloudDrive2 未在运行，无需停止"
    if not pids:
        return (
            f"❌ 端口 {port} 有进程监听，但未匹配到配置路径的 CD2 进程，"
            "为安全起见不强行终止，请手动检查"
        )
    logger.info(f"🛑 停止 CD2：对进程 {pids} 发送 SIGTERM（优雅退出）")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.warning(f"停止 CD2 失败（PID {pid}）：{e}")
    # 轮询管理端口：优雅退出后应关闭，最长约 20 秒
    for _ in range(10):
        await asyncio.sleep(2)
        if not await cd2_is_running(port):
            logger.info("🛑 CD2 已优雅退出（管理端口已关闭）")
            return "🛑 CloudDrive2 已停止"
    # 优雅退出超时 → 强制终止仍存活的进程
    killed = []
    for pid in _cd2_pids():
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except ProcessLookupError:
            pass
    await asyncio.sleep(2)
    if not await cd2_is_running(port):
        logger.info(f"🛑 CD2 已强制停止：{killed}")
        return "🛑 CloudDrive2 已停止（优雅退出超时，已强制终止）"
    return f"❌ 未能停止 CloudDrive2（端口 {port} 仍开放），请手动检查"


# ------------------------------------------------------------
# 【备份记录】菜单按钮：查看 CD2 已经备份到 115、并按“完成=删除源”清理了本地
# 的媒体文件记录。数据来自 CD2 的 <数据目录>/log/backup.<日期>.log：
#   “handle_localfs/cloudfs_notify: delete file and remove from all dests "<路径>”"
# 即文件已同步到所有目标、随后删除本地源的那一行（时间戳 + 完整路径）。
# 注意：同一文件会有 cloudfs/localfs 双通知，需按路径去重；日志里没有文件大小。
# ------------------------------------------------------------
BACKUP_MEDIA_EXTS = frozenset(
    "mp4 mkv mov avi wmv flv webm ts m4v mpg mpeg 3gp "
    "jpg jpeg png gif webp heic bmp tiff "
    "mp3 m4a ogg opus flac wav aac".split()
)

# 对账口径的产物扩展名：在备份记录视图白名单之外，CD2 实际也搬档案与文档
# （2026-09-20~23 日志实测 zip 3669 行、pdf/psd 273 行）——滞留判定必须贴
# CD2 真实搬运口径，否则 zip 类产物全被漏判。
RECONCILE_MEDIA_EXTS = BACKUP_MEDIA_EXTS | frozenset(
    "zip rar 7z pdf psd epub".split())

# 匹配备份日志里的“删除源文件”逐文件行，捕获（时间, 虚拟路径）
_BACKUP_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\S+\s+INFO\s+cloudapi::backup_manager:\s+"
    r"(?:handle_localfs_notify|handle_cloudfs_notify): delete file and remove "
    r'from all dests "(.+)"\s*$'
)


def parse_backup_log_lines(lines, exts=None):
    """从备份日志的文本行解析出 (时间, 虚拟路径)，扩展名白名单内、按路径去重。

    只认两种逐文件“delete file...”行；批量的 notify callback 行跳过（重复）。
    exts 缺省 = BACKUP_MEDIA_EXTS（备份记录视图口径）；115 对账传
    RECONCILE_MEDIA_EXTS（含 zip 等档案）。返回按时间倒序的 [(时间, 路径), ...]，
    纯函数可单测。
    """
    exts = BACKUP_MEDIA_EXTS if exts is None else exts
    seen = {}
    for raw in lines:
        m = _BACKUP_LINE_RE.match(raw.strip())
        if not m:
            continue
        ts, path = m.group(1), m.group(2)
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        if ext not in exts:
            continue
        seen[path] = ts  # 同一路径多次出现保留最后时间
    items = [(ts, path) for path, ts in seen.items()]
    items.sort(reverse=True)
    return items


def read_cd2_backup_records(log_dir=None, days=7, limit=15):
    """读取最近 days 天 backup.<日期>.log 里的媒体备份记录。

    log_dir 为空或目录不存在返回 None（调用方据此提示未配置）。
    否则返回去重、按时间倒序、最多 limit 条的 [(时间, 路径)]。
    """
    log_dir = os.path.expanduser(log_dir or cd2_log_dir())
    if not log_dir or not os.path.isdir(log_dir):
        return None
    lines = []
    today = date.today()
    for i in range(days):
        d = today - timedelta(days=i)
        p = os.path.join(log_dir, f"backup.{d.isoformat()}.log")
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                lines.extend(f.read().splitlines())
        except (OSError, ValueError):
            continue
    return parse_backup_log_lines(lines)[:limit]


def backup_records_text(days=7, limit=15):
    """【备份记录】菜单的文本。无日志目录 → 提示未配置；无记录 → 提示空。"""
    items = read_cd2_backup_records(None, days, limit)
    if items is None:
        return (
            "❌ 未配置 CD2 备份日志目录：请在 tg_secrets.json 的 cd2.log_dir "
            "填入 CloudDrive2 数据目录下的 log 目录（如 ~/Waytech/CloudDrive2/log）"
        )
    if not items:
        return f"🗂 近 {days} 天没有 CD2 备份清理记录（没有媒体被处理过）"
    head = (
        f"🗂 CD2 备份记录（媒体已备份到 115 并删除本地）\n"
        f"近 {days} 天内最新 {len(items)} 条\n\n"
    )
    out = []
    for ts, path in items:
        folder = os.path.basename(os.path.dirname(path))
        name = os.path.basename(path)
        mmdd = ts[5:16]  # MM-DD HH:MM
        display = f"{folder}/{name}" if folder else name
        if len(display) > 70:
            display = display[:69] + "…"
        out.append(f"· {mmdd}  {display}")
    text = head + "\n".join(out)
    return text[:4000]


# ------------------------------------------------------------
# 【115 对账】（2026-09-24）：本地滞留媒体 × 近期备份日志交叉。
#
# CD2 的语义是「备份到 115 完成即删本地源」——正常情况下下载目录里不该有
# 超过一天的媒体文件。滞留 = 竞态漏备（CD2 扫描时文件还没落盘，之后无人
# 再扫）或备份后重新出现（重下）。两种都需要 CD2 应用内对那个目录手动补
# 一次同步；本对账只负责把它们找出来，绝不删除/移动任何文件。
#
# 备份日志里的路径是 CD2 虚拟路径（卷根不带 /Volumes，如 /V1/downloads/…），
# 与本地真实路径差一个前缀，见 _local_to_virtual / _virtual_to_local。
# ------------------------------------------------------------
_STALE_MIN_AGE_HOURS = 24   # 滞留判定线：比这更「新」的文件视为还在正常排队
_RECON_LOG_DAYS = 3         # 交叉比对的备份日志天数
_RECON_TOP_DIRS = 8         # 报告里最多列几个目录


def _volume_name():
    """从 DOWNLOAD_DIR（/Volumes/<卷>/…）取数据卷名；解析不出返回 None。"""
    m = re.match(r"^/Volumes/([^/]+)/", config.DOWNLOAD_DIR)
    return m.group(1) if m else None


def _local_to_virtual(path):
    """本地真实路径 → CD2 虚拟路径（去掉一级 /Volumes）；不在数据卷上返回 None。"""
    vol = _volume_name()
    if vol and path.startswith(f"/Volumes/{vol}/"):
        return path[len("/Volumes"):]
    return None


def _virtual_to_local(path):
    """CD2 虚拟路径 → 本地真实路径（补回 /Volumes 前缀）；对不上返回 None。"""
    vol = _volume_name()
    if vol and path.startswith(f"/{vol}/"):
        return "/Volumes" + path
    return None


def _read_backup_log_lines(log_dir=None, days=_RECON_LOG_DAYS):
    """读最近 days 天 backup.<日期>.log 的原始文本行（列表；目录缺失返回 None）。"""
    log_dir = os.path.expanduser(log_dir or cd2_log_dir())
    if not log_dir or not os.path.isdir(log_dir):
        return None
    lines = []
    today = date.today()
    for i in range(days):
        d = today - timedelta(days=i)
        p = os.path.join(log_dir, f"backup.{d.isoformat()}.log")
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                lines.extend(f.read().splitlines())
        except (OSError, ValueError):
            continue
    return lines


def backup_log_paths(days=_RECON_LOG_DAYS):
    """近期备份日志里出现过的虚拟路径集合（媒体白名单内、已去重）。

    日志目录未配置/不可读返回 None——调用方据此在对账报告里如实标注
    「无法交叉比对」，而不是把所有滞留都算成漏备。
    """
    lines = _read_backup_log_lines(days=days)
    if lines is None:
        return None
    return {path for _ts, path in
            parse_backup_log_lines(lines, exts=RECONCILE_MEDIA_EXTS)}


def reconcile_local_backup(download_root=None, min_age_hours=_STALE_MIN_AGE_HOURS,
                           log_days=_RECON_LOG_DAYS, top=_RECON_TOP_DIRS):
    """对账主体：本地滞留媒体 × 近期备份日志交叉，返回结构化结果。

    返回 dict：
      stale_n / stale_bytes      本地滞留媒体总数与总字节（超龄未删源）
      missed_n / missed_bytes    漏备候选（滞留且近 N 天日志未见）
      reappeared_n               备份后重现（滞留但日志里有——重下后未再备份）
      dirs                       [(相对目录, 个数, 字节)] 按字节降序最多 top 条
      log_available              备份日志是否可读（False 时 missed 口径=全部滞留）
    纯只读：只 walk 与读日志，绝不改文件。
    """
    root = os.path.expanduser(download_root or config.DOWNLOAD_DIR)
    cutoff = time.time() - min_age_hours * 3600
    log_paths = backup_log_paths(log_days)
    stale = {}   # dir -> [count, bytes]
    s_n = s_b = missed_n = missed_b = reappeared = 0
    if os.path.isdir(root):
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                ext = os.path.splitext(name)[1].lstrip(".").lower()
                if ext not in RECONCILE_MEDIA_EXTS:
                    continue
                path = os.path.join(dirpath, name)
                try:
                    st_ = os.stat(path)
                except OSError:
                    continue
                if st_.st_mtime > cutoff:
                    continue
                s_n += 1
                s_b += st_.st_size
                bucket = stale.setdefault(
                    os.path.relpath(dirpath, root), [0, 0])
                bucket[0] += 1
                bucket[1] += st_.st_size
                virtual = _local_to_virtual(path)
                if log_paths is not None:
                    if virtual is not None and virtual in log_paths:
                        reappeared += 1
                        continue
                    missed_n += 1
                    missed_b += st_.st_size
    dirs = sorted(
        ((d, n, b) for d, (n, b) in stale.items()),
        key=lambda x: x[2], reverse=True)[:top]
    return {
        "stale_n": s_n, "stale_bytes": s_b,
        "missed_n": missed_n, "missed_bytes": missed_b,
        "reappeared_n": reappeared,
        "dirs": dirs,
        "log_available": log_paths is not None,
    }


def reconcile_text(download_root=None):
    """【🔍 115 对账】的展示文本（命令 /cd2ck 与 CD2 子菜单按钮共用）。"""
    from .naming import format_size
    root = os.path.expanduser(download_root or config.DOWNLOAD_DIR)
    r = reconcile_local_backup(root)
    head = (f"🔍 115 备份对账\n"
            f"范围：{root}（滞留 = 超过 {_STALE_MIN_AGE_HOURS}h 仍在本地）\n\n")
    if not r["stale_n"]:
        return head + "✅ 对账干净：没有滞留媒体，CD2 备份链路正常"
    lines = [f"本地滞留媒体：{r['stale_n']} 个 / "
             f"{format_size(r['stale_bytes'])}"]
    if r["log_available"]:
        lines.append(
            f"  ├ 漏备候选（近 {_RECON_LOG_DAYS} 天日志未见）："
            f"{r['missed_n']} 个 / {format_size(r['missed_bytes'])}")
        lines.append(f"  └ 备份后重现（重下未再备）：{r['reappeared_n']} 个")
    else:
        lines.append("  ⚠️ 备份日志不可读，无法区分漏备与重现")
    if r["dirs"]:
        lines.append("\n滞留最多的目录：")
        for d, n, b in r["dirs"]:
            lines.append(f"  · {d} —— {n} 个 / {format_size(b)}")
    lines.append(
        "\n建议：CD2 应用内对上述目录手动触发一次同步（补扫描），"
        "备份完成后会自动删除本地源。")
    text = head + "\n".join(lines)
    return text[:4000]
