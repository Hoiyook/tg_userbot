"""115 全盘查重 —— /115dup（2026-10-23）。

**为什么**：115 给每个文件都存好全量 SHA1（秒传的基础），CD2 的
GetSubFiles 列表直接带出（``fileHashes[2]``）——查重＝递归列举＋按
SHA1 分组，全程不读文件内容（绝不会把网盘流量打爆）。115 开放平台
API 2026-08-09 起暂停，这条路走本机 CD2 gRPC（cd2_api），不新增
账号风险面。

**判据**：SHA1 相同 = 字节级相同（改名/挪目录照样命中）。空文件
（size=0：SHA1 全同为常量，没有区分度）与无哈希文件不参与分组，
只计数上报。

**删除纪律**：默认 dry-run。``/115dup del <组号>`` 先出「将删清单」，
同命令尾加 ``确认`` 才真删；删除走 CD2 ``DeleteFile``（**回收站**，
可恢复）。保留策略 keep=oldest|newest 按 115 写入时间，默认 oldest。
"""
import asyncio
import os

from . import cd2_api
from . import config
from .log import logger

PAGE_SIZE = 8          # /115dup list 每页组数
# 扫描时限（分钟）：可由命令指定（/115dup scan [路径] [分钟]），默认 10。
# 超时按已扫部分出报告——实测全盘 1 小时只扫到一半（40.6 万文件/约 0.25s
# 目录，CD2 对 115 的目录缓存 TTL 很短基本每次回源），与其挂一小时不如
# 分段多跑几次：每次 10 分钟，报告累计靠人工比对（判重口径=SHA1，稳定）
SCAN_DEADLINE_DEFAULT_MIN = 10
SCAN_DEADLINE_MAX_MIN = 1440

# ============================================================
# 报告导出（2026-10-23 起）：查重结果落一份 TXT/CSV 到 115。
# 链路＝extract_worker 解压回传同款：写 CD2 挂载（=上传任务）→ gRPC
# 尺寸对账 → 等 CD2 上传任务清空（尾段在 cd2_api.upload_to_115，与
# ops_report 共用）。报告必有本地存档，挂载失败不丢数据。
# 目录 2026-10-24 起从「查重报告」改为「userBot报告」——运维报告
# （/report）也落这里，115 上旧「查重报告」目录可手动改名并入。
# ============================================================
REPORT_DIR_DEFAULT = "/115open/userBot报告"
REPORT_VERIFY_TRIES = 5        # gRPC 尺寸对账重试（CD2 写入到可见有间隙）
REPORT_VERIFY_GAP_S = 3.0
REPORT_UPLOAD_WAIT_TRIES = 3   # CD2 后台上传任务清空等待（KB 级文件）
REPORT_UPLOAD_WAIT_GAP_S = 2.0


def group_duplicates(files):
    """[(path, size, sha1, wtime)] → (groups, stats)。纯函数。

    groups: [{sha1, size, files: [(path, wtime)]}] 按可释放空间降序
    （size×(份数-1)）——落库后组号 1..N 即此序，list 页序/del 组号都
    建立在它上面。组内按写入时间升序（oldest 保留策略直接取首个）。
    stats: {total_files, dup_groups, dup_files, wasted_bytes,
            no_hash, empty}
    """
    stats = {"total_files": len(files), "dup_groups": 0, "dup_files": 0,
             "wasted_bytes": 0, "no_hash": 0, "empty": 0}
    by_sha1 = {}
    for path, size, sha1, wtime in files:
        if not sha1:
            stats["no_hash"] += 1
            continue
        if size <= 0:
            stats["empty"] += 1
            continue
        by_sha1.setdefault(sha1, []).append((path, size, wtime))
    groups = []
    for sha1, members in by_sha1.items():
        if len(members) < 2:
            continue
        size = members[0][1]   # 同 SHA1 尺寸必同；取首个防脏数据
        files_sorted = sorted(((p, t) for p, s, t in members),
                              key=lambda x: (x[1] or 0, x[0]))
        groups.append({"sha1": sha1, "size": size, "files": files_sorted})
        stats["dup_groups"] += 1
        stats["dup_files"] += len(members)
        stats["wasted_bytes"] += size * (len(members) - 1)
    groups.sort(key=lambda g: g["size"] * (len(g["files"]) - 1), reverse=True)
    return groups, stats


def _fmt_date(ts):
    import time as _time
    return _time.strftime("%Y-%m-%d", _time.localtime(ts)) if ts else "时间未知"


def _fmt_dt(ts):
    """写入时间（Unix 秒）→ 'YYYY-MM-DD HH:MM'；0/缺 → '时间未知'。"""
    import time as _time
    return _time.strftime("%Y-%m-%d %H:%M", _time.localtime(ts)) if ts \
        else "时间未知"


def _fmt_mb(size):
    """字节 → MB 字符串（查重口径统一 MB）。<0.1 MB 保留两位防失真。"""
    mb = float(size) / 1048576
    return f"{mb:.2f} MB" if mb < 0.1 else f"{mb:.1f} MB"


def _mb_num(size):
    """字节 → MB 数值字符串（CSV 列用，两位小数）。"""
    return f"{float(size) / 1048576:.2f}"


def _split_path(path):
    """绝对路径 → (目录, 文件名)；无斜杠时目录记 '/'。"""
    d, _, name = str(path).rpartition("/")
    return (d or "/", name)


def scan_and_save(root="/115open", deadline_s=None):
    """全盘扫描 → 分组 → 落库（阻塞；命令层用 to_thread 跑）。

    deadline_s 扫描软时限，超时按已扫部分收尾（分组/落库/导报告照常）。
    返回 (run_id, groups, stats, scan_stats)。Runtime DB 不可用时照常
    扫描、跳过落库（run_id=None）——分组结果在内存里照样能回报告，
    只是查不到 del 用的组号。
    """
    from . import runtime_db
    if not deadline_s or deadline_s <= 0:
        deadline_s = SCAN_DEADLINE_DEFAULT_MIN * 60
    deadline_min = deadline_s / 60

    def _progress(st):
        # 每 500 个目录打一行进度（曾因全程无进度输出被问"还没好吗"）
        if st["dirs"] and st["dirs"] % 500 == 0:
            logger.info(f"🧹 查重扫描进度：目录 {st['dirs']}｜文件 {st['files']}"
                        f"｜无哈希 {st['no_hash']}｜耗时 {st['elapsed_s']}s")

    run_id = None
    try:
        run_id = runtime_db.dup_start_run(root)
    except runtime_db.DbUnavailable as e:
        logger.warning(f"查重 run 开档失败（结果不落库）：{e}")
    raw, scan_stats = cd2_api.scan_remote(root=root, progress=_progress,
                                          deadline_s=deadline_s)
    groups, stats = group_duplicates(raw)
    stats["scan"] = scan_stats
    if run_id is not None:
        err = (f"扫描时限到（{deadline_min:g} 分钟），结果为已扫部分"
               if scan_stats.get("aborted") else None)
        try:
            runtime_db.dup_finish_run(run_id, stats, groups, error=err)
        except runtime_db.DbUnavailable as e:
            logger.warning(f"查重 run 回填失败：{e}")
    return run_id, groups, stats, scan_stats


def _scan_summary_text(root, stats, scan_stats):
    scan = scan_stats or {}
    lines = [f"🧹 115 查重完成（{root}）"]
    bits = [f"文件 {stats['total_files']} 个"]
    if stats.get("no_hash"):
        bits.append(f"无哈希 {stats['no_hash']}")
    if stats.get("empty"):
        bits.append(f"空文件 {stats['empty']} 跳过")
    lines.append("｜".join(bits))
    lines.append(f"目录 {scan.get('dirs', 0)} 个｜耗时 {scan.get('elapsed_s', 0)}s"
                 + ("｜⚠️ 时限到，结果为已扫部分" if scan.get("aborted")
                    else ""))
    n_err = scan.get("errors_total", len(scan.get("errors") or []))
    if n_err:
        lines.append(f"⚠️ {n_err} 个目录列举失败（日志有明细）")
    if not stats["dup_groups"]:
        lines.append("✅ 没有发现重复文件（SHA1 判据）")
        return "\n".join(lines)
    lines.append("")
    lines.append(f"重复 {stats['dup_groups']} 组、涉及 {stats['dup_files']} 个文件，"
                 f"可释放 {_fmt_mb(stats['wasted_bytes'])}")
    lines.append("/115dup list 看清单｜/115dup show <组号> 看全路径")
    lines.append("/115dup del <组号> [keep=oldest|newest] 预览删除")
    return "\n".join(lines)


def _latest_run_or_text():
    from . import runtime_db
    try:
        run = runtime_db.dup_latest_run()
    except runtime_db.DbUnavailable as e:
        return None, f"❌ Runtime DB 不可用：{e}"
    if not run:
        return None, ("🧹 还没有查重结果。先跑 /115dup scan [路径]"
                      "（默认全盘 /115open）")
    return run, None


def _list_text(page=1):
    from . import runtime_db
    run, err = _latest_run_or_text()
    if err:
        return err
    if run["status"] == "running":
        # run 未收尾（进行中/进程重启遗留）：dup_groups 恒为 0，照旧展示
        # 会误报「没有重复文件」
        return (f"🧹 最近一次扫描（{run['root']}）尚未完成——若没有扫描"
                "在跑说明进程重启过，重扫 /115dup scan 覆盖即可")
    total = run["dup_groups"]
    if not total:
        return "🧹 最近一次扫描没有重复文件（重扫：/115dup scan）"
    page = max(1, int(page or 1))
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    page = min(page, pages)
    try:
        rows = runtime_db.dup_group_page(run["id"], (page - 1) * PAGE_SIZE,
                                         PAGE_SIZE)
    except runtime_db.DbUnavailable as e:
        return f"❌ Runtime DB 不可用：{e}"
    lines = []
    if run.get("error"):
        lines.append(f"⚠️ 上次扫描未完整：{run['error']}")
    lines.append(f"🧹 重复 {total} 组 · 可释放 "
                 f"{_fmt_mb(run['wasted_bytes'])}"
                 f"（第 {page}/{pages} 页，组号即可释放降序）")
    for r in rows:
        n_alive = r["n"] if r["n_alive"] is None else r["n_alive"]
        flag = "✂️" if n_alive < r["n"] else " "
        name = ""
        try:
            members = runtime_db.dup_group_members(run["id"], r["group_id"])
            name = (members[0]["path"].rsplit("/", 1)[-1] if members else "")
        except runtime_db.DbUnavailable:
            pass
        lines.append(f"{flag}{r['group_id']}. {_fmt_mb(r['size'])} ×{r['n']}"
                     f"（省 {_fmt_mb(r['size'] * (r['n'] - 1))}）"
                     f" {str(name)[:36]}")
    lines.append(f"/115dup list {page + 1} 翻页｜/115dup show <组号> 看全路径")
    return "\n".join(lines)


def _show_text(group_id):
    from . import runtime_db
    run, err = _latest_run_or_text()
    if err:
        return err
    try:
        members = runtime_db.dup_group_members(run["id"], group_id)
    except runtime_db.DbUnavailable as e:
        return f"❌ Runtime DB 不可用：{e}"
    if not members:
        return f"❌ 组 {group_id} 不存在（重扫后组号会变：/115dup scan）"
    size = members[0]["size"]
    sha1 = members[0].get("sha1") or ""
    lines = [f"🧹 组 {group_id}（{_fmt_mb(size)} ×{len(members)}，"
             f"省 {_fmt_mb(size * (len(members) - 1))}）",
             f"SHA1 {sha1}"]
    for m in members:
        mark = "🗑已删" if m["deleted"] else "　"
        directory, name = _split_path(m["path"])
        lines.append(f" {mark} {name} ｜ {_fmt_dt(m['write_time'])}")
        lines.append(f"      {directory}")
    lines.append("")
    lines.append("/115dup del %d [keep=oldest|newest] 预览删除" % group_id)
    return "\n".join(lines)


def _plan_delete(members, keep):
    """组员 → (keep_member, del_members)。keep: oldest|newest。

    只对未删成员决策；组员已全部删过 → (None, [])。写入时间未知（0）
    的成员在**两种**策略下都优先当保留那份——内容身份最不确定，宁多
    留一份也不让它成为被删的那份；全组都未知时按路径定一个。"""
    alive = [m for m in members if not m["deleted"]]
    if not alive:
        return None, []
    unknown = [m for m in alive if not (m["write_time"] or 0)]
    pool = unknown or alive
    if keep == "newest":
        keep_member = max(pool,
                          key=lambda m: ((m["write_time"] or 0), m["path"]))
    else:
        keep_member = min(pool,
                          key=lambda m: ((m["write_time"] or 0), m["path"]))
    del_members = [m for m in alive if m is not keep_member]
    return keep_member, del_members


def _del_text(group_id, keep, confirmed):
    from . import runtime_db
    run, err = _latest_run_or_text()
    if err:
        return err
    try:
        members = runtime_db.dup_group_members(run["id"], group_id)
    except runtime_db.DbUnavailable as e:
        return f"❌ Runtime DB 不可用：{e}"
    if not members:
        return f"❌ 组 {group_id} 不存在（重扫后组号会变：/115dup scan）"
    keep_member, del_members = _plan_delete(members, keep)
    if not del_members:
        return f"🧹 组 {group_id}：没有可删成员（只剩 1 份或已全删过）"
    size = members[0]["size"]
    head = (f"🧹 组 {group_id} 删除预览（keep={keep}，"
            f"省 {_fmt_mb(size * len(del_members))}）")
    if not confirmed:
        lines = [head]
        lines.append(f" ✅保留 {_fmt_dt(keep_member['write_time'])} "
                     f"{keep_member['path']}")
        for m in del_members:
            lines.append(f" 🗑将删 {_fmt_dt(m['write_time'])} {m['path']}")
        lines.append("")
        lines.append(f"确认无误发：/115dup del {group_id} keep={keep} 确认"
                     "（删除进回收站，可恢复）")
        return "\n".join(lines)
    results = cd2_api.delete_remotes([m["path"] for m in del_members])
    ok_paths = [p for p, ok, _e in results if ok]
    failed = [(p, e) for p, ok, e in results if not ok]
    if ok_paths:
        try:
            runtime_db.dup_mark_deleted(run["id"], group_id, ok_paths)
        except runtime_db.DbUnavailable as e:
            logger.warning(f"查重组员打删标失败：{e}")
    lines = [f"🧹 组 {group_id} 删除完成：{len(ok_paths)} 个进回收站 ✅"
             + (f"，失败 {len(failed)} 个" if failed else "")]
    for p, e in failed[:5]:
        lines.append(f" ❌ {p.rsplit('/', 1)[-1]}：{e}")
    if failed:
        lines.append("（失败多为文件刚被移动/改名——重扫 /115dup scan 后重试）")
    return "\n".join(lines)


# ============================================================
# 报告导出
# ============================================================
def _rows_from_db(run_id):
    """DB → [(group_id, size, members)]，组号升序（= 可释放降序）。"""
    from . import runtime_db
    rows = []
    for g in runtime_db.dup_group_page(run_id, 0, 10 ** 9):
        members = runtime_db.dup_group_members(run_id, g["group_id"])
        rows.append((g["group_id"], g["size"], members))
    return rows


def _rows_from_memory(groups):
    """内存分组（scan_and_save 的返回）→ 同构行（无 DB 时的导出数据源）。"""
    rows = []
    for gid, g in enumerate(groups, start=1):
        members = [{"path": p, "size": g["size"], "sha1": g["sha1"],
                    "write_time": t, "deleted": 0}
                   for p, t in g["files"]]
        rows.append((gid, g["size"], members))
    return rows


def _render_report_text(root, stats, rows):
    """人读报告（115 App 手机端可直接预览）。✅=保留建议（最早）。"""
    import time as _time
    lines = [
        "🧹 115 查重报告",
        f"导出：{_time.strftime('%Y-%m-%d %H:%M')}｜根路径：{root}",
        f"文件 {stats.get('total_files', 0)} 个｜重复 {stats.get('dup_groups', 0)} 组、"
        f"{stats.get('dup_files', 0)} 个，可释放 "
        f"{_fmt_mb(stats.get('wasted_bytes', 0))}",
        "判据：SHA1 相同=字节级相同；✅保留建议(最早) 🗑建议删 [已删]=已进回收站",
        "",
    ]
    for gid, size, members in rows:
        lines.append(f"── 组 {gid}（{_fmt_mb(size)} ×{len(members)}，"
                     f"可省 {_fmt_mb(size * (len(members) - 1))}）──")
        alive = [m for m in members if not m["deleted"]]
        keep = alive[0] if alive else None
        for m in members:
            if m["deleted"]:
                mark = "[已删]"
            elif m is keep:
                mark = "✅"
            else:
                mark = "🗑"
            directory, name = _split_path(m["path"])
            lines.append(f" {mark} {name} ｜ {_fmt_dt(m['write_time'])}")
            lines.append(f"     {directory}")
        lines.append("")
    return "\n".join(lines)


def _render_report_csv(rows):
    """CSV：目录与文件名分列、size 用 MB、写入时间格式化（表格直开即用）。"""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["group_id", "sha1", "size_mb", "directory", "filename",
                "write_time", "deleted"])
    for gid, size, members in rows:
        for m in members:
            directory, name = _split_path(m["path"])
            w.writerow([gid, m["sha1"], _mb_num(size), directory, name,
                        _fmt_dt(m["write_time"]), m["deleted"]])
    return buf.getvalue()


def export_report(target_dir=REPORT_DIR_DEFAULT, run=None,
                  mem_groups=None, mem_stats=None, mem_root=None):
    """渲染 TXT+CSV → 本地存档 → 写 CD2 挂载（=上传 115）→ gRPC 对账。

    数据源：run（DB 行）优先；run 为 None 且给了 mem_groups 时用内存分组
    （扫描完即时导出、DB 不可用也能出报告）。
    返回 {ok, remote_dir, files, local_dir, note}——任何失败都不抛，
    本地存档永远先落（RUNTIME_DIR/dup_reports/）。
    """
    import time as _time
    from . import runtime_db
    from . import cd2_api
    out = {"ok": False, "remote_dir": str(target_dir), "files": [],
           "local_dir": None, "note": ""}
    if run is None and mem_groups is None:
        run, err = _latest_run_or_text()
        if err:
            out["note"] = err.lstrip("❌ ")
            return out
    if run is not None:
        root = run["root"]
        stats = {"total_files": run["total_files"],
                 "dup_groups": run["dup_groups"],
                 "dup_files": run["dup_files"],
                 "wasted_bytes": run["wasted_bytes"]}
        rows = _rows_from_db(run["id"])
    else:
        root = mem_root or "/115open"
        stats = mem_stats or {}
        rows = _rows_from_memory(mem_groups)
    if not rows:
        out["note"] = "没有重复文件，无可导出内容"
        return out

    stem = "查重报告_" + _time.strftime("%Y%m%d-%H%M")
    local_dir = os.path.join(config.RUNTIME_DIR, "dup_reports")
    try:
        os.makedirs(local_dir, exist_ok=True)
        for ext, content in (("txt", _render_report_text(root, stats, rows)),
                             ("csv", _render_report_csv(rows))):
            path = os.path.join(local_dir, f"{stem}.{ext}")
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            out["files"].append(path)
        out["local_dir"] = local_dir
    except OSError as e:
        out["note"] = f"本地存档失败：{e}"
        return out

    # 写挂载（=CD2 上传任务）→ gRPC 尺寸对账 → 等上传清空（与 /report
    # 共用的导出尾段，见 cd2_api.upload_to_115）
    ok, note = cd2_api.upload_to_115(
        out["files"], target_dir,
        verify_tries=REPORT_VERIFY_TRIES, verify_gap_s=REPORT_VERIFY_GAP_S,
        upload_wait_tries=REPORT_UPLOAD_WAIT_TRIES,
        upload_wait_gap_s=REPORT_UPLOAD_WAIT_GAP_S,
        name_prefix="查重报告_")
    if not ok:
        out["note"] = f"{note}；本地存档可靠"
        return out
    out["ok"] = True
    return out


def _export_note(res):
    """/115dup report 与扫描自动导出共用的结果行。"""
    if res["ok"]:
        names = "、".join(os.path.basename(p) for p in res["files"])
        return f"📤 报告已传到 115：{res['remote_dir']}/{names}"
    return f"📤 报告未上传：{res['note']}｜本地存档：{res['local_dir'] or '（无）'}"


def is_dup_command(cmd_text):
    head = str(cmd_text or "").split()[0] if str(cmd_text or "").strip() else ""
    return head.lower() in ("/115dup", "/115dup_scan", "/115dup_list",
                            "/115dup_show", "/115dup_del", "/115dup_report")


def parse_dup_command(cmd_text):
    """→ (action, args) 或 None（不是本模块命令）。

    /115dup [scan [路径]] / list [页] / show <组号> /
    del <组号> [keep=oldest|newest] [确认]
    """
    parts = str(cmd_text or "").split()
    if not parts:
        return None
    head = parts[0].split("@")[0].lower()
    if head == "/115dup":
        if len(parts) == 1:
            return "list", {"page": 1}
        sub = parts[1].lower()
    elif head in ("/115dup_scan", "/115dup_list", "/115dup_show",
                  "/115dup_del", "/115dup_report"):
        sub = head.rsplit("_", 1)[1]
        parts = ["", ""] + parts[1:]
    else:
        return None
    if sub == "scan":
        # 参数序不拘：纯数字 token=分钟数（1-1440），首个非数字 token=路径。
        # 默认全盘 /115open + 10 分钟；超时按已扫部分出报告并上传。
        root, minutes, path_seen = "/115open", SCAN_DEADLINE_DEFAULT_MIN, False
        for tok in parts[2:]:
            if tok.isdigit():
                minutes = max(1, min(int(tok), SCAN_DEADLINE_MAX_MIN))
            elif not path_seen:
                root, path_seen = tok, True
        return "scan", {"root": root, "minutes": minutes}
    if sub == "report":
        return "report", {"dir": (parts[2] if len(parts) > 2
                                  else REPORT_DIR_DEFAULT)}
    if sub == "list":
        page = 1
        if len(parts) > 2:
            try:
                page = int(parts[2])
            except ValueError:
                return "usage", {}
        return "list", {"page": page}
    if sub == "show":
        if len(parts) < 3 or not parts[2].isdigit():
            return "usage", {}
        return "show", {"gid": int(parts[2])}
    if sub == "del":
        if len(parts) < 3 or not parts[2].isdigit():
            return "usage", {}
        keep, confirmed = "oldest", False
        for tok in parts[3:]:
            t = tok.lower()
            if t.startswith("keep="):
                v = t[5:]
                if v in ("oldest", "newest"):
                    keep = v
            elif t in ("确认", "confirm", "yes"):
                confirmed = True
        return "del", {"gid": int(parts[2]), "keep": keep,
                       "confirmed": confirmed}
    return "usage", {}


USAGE = ("🧹 /115dup —— 115 查重（SHA1 判据，走 CD2 不读文件内容）\n\n"
         "/115dup scan [路径] [分钟] —— 扫描（默认全盘 /115open、"
         "10 分钟；超时按已扫部分出报告传 115）\n"
         "/115dup list [页] —— 重复组清单（按可释放空间降序）\n"
         "/115dup show <组号> —— 组内明细（文件名/目录/写入时间）\n"
         "/115dup del <组号> [keep=oldest|newest] [确认] —— 预览/删除\n"
         "/115dup report [115目录] —— 重出报告传 115"
         "（默认 /115open/userBot报告）\n"
         "　例：/115dup scan /115open/视频 20\n"
         "　保留策略默认 keep=oldest（留最早写入那份）；确认后进回收站")


# 后台扫描状态（防重入 + 进度查询）。单事件循环内读写，无锁。
_SCAN_RUNNING = {}
# 在途扫描 task 持强引用防 GC（§35 同款；事件循环只持 task 弱引用，
# 无引用的 task 可能中途被回收——整轮扫描无声消失）
_SCAN_TASKS = set()


def scan_status_text():
    """当前是否有后台扫描在跑（命令回执与菜单共用）。"""
    if not _SCAN_RUNNING:
        return ""
    r = _SCAN_RUNNING.get("root", "/115open")
    m = _SCAN_RUNNING.get("minutes", "?")
    return f"（后台扫描进行中：{r}，时限 {m} 分钟）"


async def run_scan_and_notify(root="/115open", minutes=None):
    """后台跑完整扫描：扫 → 汇总 → 导报告，完成 notify_user 通知。

    **为什么放后台 task**（/dyu 同款先例）：扫描以十分钟计，若挂在命令
    协程上，中途 Telethon 断线/重连会连坐杀掉整个命令（2026-10-23 22:22
    实测：update 循环 bug 断线，扫描线程在 executor 里跑完落库，但命令
    协程死在 await 边界——导出与完成回复全部静默丢失）。后台 task 挂在
    主循环上，不随事件处理器死；通知走 notify_user（有收藏夹回落）。
    """
    from . import notify
    global _SCAN_RUNNING
    minutes = minutes or SCAN_DEADLINE_DEFAULT_MIN
    _SCAN_RUNNING = {"root": root, "minutes": minutes}
    try:
        try:
            _run_id, groups, stats, scan_stats = await asyncio.to_thread(
                scan_and_save, root, minutes * 60)
        except RuntimeError as e:   # cd2_api 未配令牌/CD2 不在
            text = f"🧹 115 查重\n❌ {e}"
        else:
            text = _scan_summary_text(root, stats, scan_stats)
            # 扫描完成自动导报告（数据源用内存分组——DB 不可用也照传）。
            # 导出兜底：export_report 自身按约定不抛，但 gRPC 层（find_remote
            # 的 _md() 等）仍可能冒意外异常——通知必达，异常降级成文案行
            if stats.get("dup_groups"):
                try:
                    res = await asyncio.to_thread(
                        export_report, mem_groups=groups, mem_stats=stats,
                        mem_root=root)
                    text += "\n" + _export_note(res)
                except Exception as e:
                    logger.exception(f"查重报告导出异常（扫描结果不受影响）：{e}")
                    text += (f"\n📤 报告导出异常：{type(e).__name__}: {e}"
                             "\n扫描结果已落库，稍后 /115dup report 重出")
        try:
            await notify.notify_user(text)
        except Exception:
            pass
    finally:
        _SCAN_RUNNING = {}


async def command_reply(cmd_text):
    """执行 /115dup …，返回回复文本。scan 只回执，扫描在后台 task 跑。"""
    global _SCAN_RUNNING   # scan 分支同步置位防重入（赋值使其成局部名）
    parsed = parse_dup_command(cmd_text)
    if parsed is None:
        return None
    action, args = parsed
    if action == "usage":
        return USAGE
    if action == "scan":
        if _SCAN_RUNNING:
            return ("🧹 已有扫描在跑" + scan_status_text() +
                    "，完成会通知；/115dup list 看上次结果")
        # 防重入标志**同步置位再 create_task**：置位在 task 体内的话，
        # 「检查为空 → create_task → task 开跑」之间存在调度窗口，两个
        # 入口几乎同时触发会双开扫描（replace-per-scan 互相清数据）
        _SCAN_RUNNING = {"root": args["root"], "minutes": args["minutes"]}
        task = asyncio.create_task(
            run_scan_and_notify(args["root"], args["minutes"]))
        _SCAN_TASKS.add(task)
        task.add_done_callback(_SCAN_TASKS.discard)
        return (f"🧹 扫描已启动（{args['root']}，时限 {args['minutes']} 分钟）\n"
                "完成后自动出报告传 115 并通知；进度每 500 目录打一行日志")
    if action == "list":
        return await asyncio.to_thread(_list_text, args["page"])
    if action == "show":
        return await asyncio.to_thread(_show_text, args["gid"])
    if action == "del":
        return await asyncio.to_thread(_del_text, args["gid"], args["keep"],
                                       args["confirmed"])
    if action == "report":
        return await asyncio.to_thread(_report_text, args["dir"])
    return USAGE


def _report_text(target_dir):
    """/115dup report：最近 run → 报告传 115 的结果回执。"""
    res = export_report(target_dir)
    if not res["files"] and not res["ok"]:
        return f"📤 {res['note']}"
    return _export_note(res)


def summary_text():
    """菜单「🧹 115 查重」按钮正文：最近 run 的速览（阻塞，bot 层 to_thread）。"""
    run, err = _latest_run_or_text()
    if err:
        return err
    lines = [f"🧹 115 查重（{run['root']}，{_fmt_date(run['finished_at'])}）"]
    if run["status"] != "done":
        lines.append(f"⚠️ 状态：{run['status']}"
                     + (f"——{run['error']}" if run.get("error") else ""))
    lines.append(f"文件 {run['total_files']} 个｜重复 {run['dup_groups']} 组、"
                 f"{run['dup_files']} 个，可释放 "
                 f"{_fmt_mb(run['wasted_bytes'])}")
    if run.get("error"):
        lines.append(f"⚠️ {run['error']}")
    lines.append("重扫：/115dup scan｜清单：/115dup list")
    return "\n".join(lines)
