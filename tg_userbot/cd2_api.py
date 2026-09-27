"""CloudDrive2 API 客户端 —— 备份对账（2026-09-17，场景 1）。

**为什么**：此前"媒体是否真的搬到了 115"只能靠解析 CD2 日志行
（``backup_records_text`` 认 ``delete file and remove from all dests``）——
日志轮转/丢失/CD2 异常退出都会让判断失真。有了 API 令牌（tg_secrets.json
``cd2.api_token``），可以直接查 115 远端：文件是否存在、大小是否一致。

**协议**：CD2 的 gRPC 服务 ``clouddrive.CloudDriveFileSrv``：
  - gRPC 明文端口 127.0.0.1:19798（本机直连，不走代理——localhost 例外于
    系统代理，且 grpc 的 trust_env 行为与 httpx 不同，这里显式 local）
  - 认证：metadata ``("authorization", "Bearer <api_token>")``（令牌在
    CD2 网页 → 设置 → API 令牌 创建）
  - proto：官方 clouddrive.proto（v1.0.17）生成桩在 ``cd2_proto/``

**路径映射**：备份任务 ``sourcePath → destinations[0].destinationPath``
（实测 /V1/downloads → /115open/Nekogram）。对账即：本地成品文件在远端
镜像路径下存在且大小一致 = 已备份；缺失/大小不符 = 备份未完成。

**懒加载纪律**：grpcio 与桩都是 import 期零副作用（与 chrome_client 的
纯函数约定一致）；未配置令牌时所有函数返回 None/报错文案，不影响主流程。
"""
import os

# gRPC 的 C 核心会在 fork 出的子进程（/sh 子命令等）里打印初始化日志污染输出，
# 并需要显式开启 fork 支持——必须在 import grpc 之前设置这两个环境变量
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "1")
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")

from . import config
from .log import logger

# 本地成品文件的临时后缀（与 Pawchive/普通下载一致）
_TEMP_SUFFIXES = (".part", ".download", ".crdownload")


def cd2_api_token():
    """tg_secrets.json 的 cd2.api_token；未配置返回 None。

    属性访问 config.CD2_API_TOKEN（与抖音 cookie 同款纪律：改密钥文件即时生效）。
    """
    return (config.CD2_API_TOKEN or "").strip() or None


def _stub():
    """懒建 gRPC channel + stub（import 期零副作用；连接按调用缓存）。
    token 缺失抛 RuntimeError（调用方转用户文案）。"""
    global _STUB_CACHE
    token = cd2_api_token()
    if not token:
        raise RuntimeError("未配置 CD2 API 令牌（tg_secrets.json 的 cd2.api_token）")
    if _STUB_CACHE is None:
        import grpc
        from .cd2_proto import clouddrive_pb2_grpc as pbg
        channel = grpc.insecure_channel("127.0.0.1:19798")
        _STUB_CACHE = (pbg.CloudDriveFileSrvStub(channel), token)
    return _STUB_CACHE


_STUB_CACHE = None

_AUTH_MD = None


def _md():
    stub, token = _stub()
    return stub, (("authorization", "Bearer " + token),)


def backup_tasks():
    """备份任务列表 [{source, destination, enabled}]；CD2 未运行/异常返回 None。"""
    from google.protobuf import empty_pb2
    try:
        stub, md = _md()
    except RuntimeError as e:
        raise RuntimeError(str(e)) from e
    try:
        bk = stub.BackupGetAll(empty_pb2.Empty(), metadata=md, timeout=20)
    except Exception as e:
        logger.warning(f"_cd2 BackupGetAll 失败：{e}")
        return None
    out = []
    for bs in bk.backups:
        b = getattr(bs, "backup", bs)   # BackupGetAll 返回 BackupStatus 包 Backup
        dests = [d.destinationPath for d in b.destinations if d.isEnabled]
        if not dests:
            continue
        out.append({"source": b.sourcePath, "destination": dests[0],
                    "enabled": b.isEnabled})
    return out


def local_to_remote_root():
    """本地媒体根（DOWNLOAD_DIR）→ 远端镜像根；取第一个启用备份任务。

    返回 (remote_root, local_root) 或 (None, None)（无任务/CD2 不可用）。
    """
    from . import state
    tasks = backup_tasks()
    if not tasks:
        return None, None
    for t in tasks:
        src = os.path.normpath(t["source"])
        local = os.path.normpath(config.DOWNLOAD_DIR)
        if src == local or src in local or local in src:
            return t["destination"], config.DOWNLOAD_DIR
    # 没有精确匹配就拿第一个任务（单任务部署即正确）
    return tasks[0]["destination"], config.DOWNLOAD_DIR


def local_to_remote(local_path, remote_root, local_root):
    """本地文件 → 远端镜像路径（相对路径平移）。"""
    rel = os.path.relpath(local_path, local_root)
    return remote_root.rstrip("/") + "/" + rel.replace(os.sep, "/")


def find_remote(remote_path, force_refresh=False):
    """远端文件/目录信息（存在返回 CloudDriveFile，不存在/不可达返回 None）。"""
    from .cd2_proto import clouddrive_pb2 as pb
    stub, md = _md()
    try:
        return stub.FindFileByPath(
            pb.FindFileByPathRequest(path=remote_path),
            metadata=md, timeout=20)
    except Exception as e:
        if "not found" in str(e):
            return None
        logger.warning(f"_cd2 查询远端失败 {remote_path}：{e}")
        return None


def get_space_info(remote_root="/115open"):
    """网盘容量 (total, used, free) 字节；失败返回 None。"""
    from .cd2_proto import clouddrive_pb2 as pb
    stub, md = _md()
    try:
        sp = stub.GetSpaceInfo(pb.FileRequest(path=remote_root),
                               metadata=md, timeout=30)
        return (sp.totalSpace, sp.usedSpace, sp.freeSpace)
    except Exception as e:
        logger.warning(f"_cd2 容量查询失败：{e}")
        return None


def list_remote_dir(remote_path, limit=50):
    """列远端目录 [(name, size, is_dir)]；失败返回 None。"""
    from .cd2_proto import clouddrive_pb2 as pb
    stub, md = _md()
    try:
        sub = stub.GetSubFiles(pb.ListSubFileRequest(path=remote_path),
                               metadata=md, timeout=60)
    except Exception as e:
        logger.warning(f"_cd2 列目录失败 {remote_path}：{e}")
        return None
    out = []
    for reply in sub:
        for f in reply.subFiles:
            out.append((f.name, f.size, f.fileType == 0))
            if len(out) >= limit:
                return out
    return out


def iter_local_files(local_root, max_files=500):
    """遍历本地成品文件（跳过临时/隐藏），返回绝对路径列表（有界）。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(local_root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith(".") or name.lower().endswith(_TEMP_SUFFIXES):
                continue
            out.append(os.path.join(dirpath, name))
            if len(out) >= max_files:
                return out
    return out


def reconcile(limit=30, min_age_minutes=10):
    """备份对账：本地成品文件逐个核对远端镜像。

    只对账"下载后静置 ≥ min_age_minutes"的文件——刚完成的文件 CD2 还在
    排队/上传，查了也是未完成。返回 dict（供命令渲染）：
    {checked, ok, missing: [(local, remote)], size_mismatch: [...], ...}
    """
    remote_root, local_root = local_to_remote_root()
    if not remote_root:
        return {"error": "没有可用的 CD2 备份任务"}
    import time as _time
    now = _time.time()
    checked = ok = 0
    missing, mismatch, too_new = [], [], 0
    for lp in iter_local_files(local_root, max_files=limit * 3):
        if checked >= limit:
            break
        try:
            age_min = (now - os.path.getmtime(lp)) / 60
        except OSError:
            continue
        if age_min < min_age_minutes:
            too_new += 1
            continue
        size = os.path.getsize(lp)
        rp = local_to_remote(lp, remote_root, local_root)
        f = find_remote(rp)
        if f is None:
            missing.append((lp, rp))
        elif f.size != size:
            mismatch.append((lp, rp, f.size, size))
        else:
            ok += 1
        checked += 1
    return {"checked": checked, "ok": ok, "missing": missing,
            "mismatch": mismatch, "too_new": too_new,
            "remote_root": remote_root, "local_root": local_root}


async def tasks_reply(limit=10):
    """/cd2tasks：CD2 上传任务视图（在途/失败明细 + 任务计数）。"""
    import asyncio
    from .naming import format_size
    try:
        total, files = await asyncio.to_thread(upload_files_summary, limit)
        counts = await asyncio.to_thread(tasks_count)
    except RuntimeError as e:
        return f"☁ CD2 上传任务\n❌ {e}"
    lines = ["☁ CD2 上传任务"]
    if counts:
        lines.append(f"计数：⬇️下载 {counts['download']} | "
                     f"⬆️上传 {counts['upload']} | 复制 {counts['copy']}")
    if not files:
        lines.append("（没有在途/失败的上传任务）")
        return "\n".join(lines)
    lines.append(f"在途/失败 {len(files)} 个（失败优先，按进度排序）：")
    for f in files:
        lines.append(f"  [{f['status']}] {f['pct']}% {f['name']}")
        if "Error" in f["status"] or "Fatal" in f["status"]:
            lines.append(f"     → {f['dest']}")
    if total and total > len(files):
        lines.append(f"  … 其余 {total - len(files)} 个略")
    lines.append("完整列表看 CD2 网页（127.0.0.1:19799）")
    return "\n".join(lines)


async def reconcile_reply(limit=20):
    """/cd2check：对账 + 容量的人话报告（阻塞 gRPC 下放线程）。"""
    import asyncio
    from .naming import format_size
    try:
        r = await asyncio.to_thread(reconcile, limit=limit)
    except RuntimeError as e:
        return f"☁ CD2 对账\n❌ {e}"
    if r.get("error"):
        return f"☁ CD2 对账\n❌ {r['error']}"
    lines = [f"☁ CD2 备份对账（{r['remote_root']}）", ""]
    lines.append(
        f"检查 {r['checked']} 个 | ✅已备份 {r['ok']} | "
        f"⚠️远端缺失 {len(r['missing'])} | 大小不符 {len(r['mismatch'])} | "
        f"过新跳过 {r['too_new']}")
    exts = sorted({os.path.splitext(f)[1].lower()
                   for f, _r in r["missing"]})
    if r["missing"] and exts:
        lines.append(f"缺失文件的扩展名：{','.join(exts)}")
        lines.append("（若为 .jpeg/.html 等白名单外扩展，CD2 备份规则本就不搬运它们）")
    if r["mismatch"]:
        lines.append("大小不符（疑似远端截断）：")
        for lp, rp, rs, ls in r["mismatch"][:5]:
            lines.append(f"  · {os.path.basename(rp)}：远端 {format_size(rs)} ≠ 本地 {format_size(ls)}")
    sp = await asyncio.to_thread(get_space_info)
    if sp:
        total, used, free = sp
        lines.append(f"115 容量：已用 {format_size(used)} / {format_size(total)}"
                     f"（余 {format_size(free)}）")
    counts = await asyncio.to_thread(tasks_count)
    if counts and (counts["upload"] or counts["copy"]):
        lines.append(f"CD2 任务：⬆️上传 {counts['upload']} | 复制 {counts['copy']}（/cd2tasks 看明细）")
    lines.append("说明：本地文件静置 ≥10 分钟才对账（刚完成的还在备份队列）")
    return "\n".join(lines)


def restart_backup_walkthrough(source="/V1/downloads"):
    """重启备份遍历（自愈：CD2 卡在扫描时让任务重新走一遍）。返回是否成功。"""
    from google.protobuf import wrappers_pb2
    stub, md = _md()
    try:
        stub.BackupRestartWalkingThrough(
            wrappers_pb2.StringValue(value=source), metadata=md, timeout=20)
        logger.info(f"_cd2 已重启备份遍历：{source}")
        return True
    except Exception as e:
        logger.warning(f"_cd2 重启备份遍历失败：{e}")
        return False


def upload_files_summary(limit=10):
    """上传任务摘要（正在进行/排队，含进度）。

    返回 (total, [ {dest, name, pct, done, size, status} ])；失败 (None, [])。
    """
    from .cd2_proto import clouddrive_pb2 as pb
    stub, md = _md()
    try:
        resp = stub.GetUploadFileList(
            pb.GetUploadFileListRequest(getAll=True), metadata=md, timeout=30)
    except Exception as e:
        logger.warning(f"_cd2 上传列表查询失败：{e}")
        return None, []
    files = []
    for f in resp.uploadFiles if hasattr(resp, "uploadFiles") else []:
        if f.status in ("Finish", "Skipped", "Cancelled", "Ignored"):
            continue   # 只看在途/失败
        total = f.size or 0
        done = f.transferedBytes or 0
        pct = min(int(done * 100 / total), 100) if total else 0
        files.append({
            "dest": (f.destPath or "")[:80],
            "name": (f.key or "").rsplit("/", 1)[-1][:60],
            "pct": pct, "done": done, "size": total,
            "status": f.status,
        })
    # 排序：失败/错误优先，其次进行中
    # 失败优先展示，其次按进度倒序
    files.sort(key=lambda x: (not ("Error" in x["status"]
                                   or "Fatal" in x["status"]), -x["pct"]))
    return len(files), files[:limit]


def tasks_count():
    """全任务计数（download/upload/copy）；失败 None。"""
    from google.protobuf import empty_pb2
    stub, md = _md()
    try:
        c = stub.GetAllTasksCount(empty_pb2.Empty(), metadata=md, timeout=20)
        return {"download": c.downloadCount, "upload": c.uploadCount,
                "copy": c.copyTaskCount}
    except Exception as e:
        logger.warning(f"_cd2 任务计数查询失败：{e}")
        return None


def add_offline_download(urls, to_folder="/115open/云下载"):
    """离线下载：磁力/ED2K/HTTP 直链（\n 分隔）丢给 CD2 下到网盘目录。"""
    from .cd2_proto import clouddrive_pb2 as pb
    stub, md = _md()
    try:
        r = stub.AddOfflineFiles(
            pb.AddOfflineFileRequest(urls=urls, toFolder=to_folder),
            metadata=md, timeout=30)
        return r.success, (r.errorMessage or "")[:120]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def space_text():
    """/paw status 附加行：115 容量（无令牌/不可用返回空串）。"""
    try:
        sp = get_space_info()
    except RuntimeError:
        return ""
    if not sp:
        return ""
    from .naming import format_size
    total, used, free = sp
    return (f"☁ 115 网盘：已用 {format_size(used)} / "
            f"{format_size(total)}（余 {format_size(free)}）")
