"""运维报告（/report，2026-10-24）——把「系统状态」一键搬到 115。

**为什么**：查重报告（/115dup）开了「报告落 115」的先例后，运维数据
同样值得离线翻阅——手机 115 App 直开即看，不依赖 bot 在线。目录与查重
共用 ``/115open/userBot报告/``（dup_scan.REPORT_DIR_DEFAULT）。

**用户视角的五类问题**（每类一个采集器，全部只读）：
  manual  「哪些在等我出手」——Pawchive 待人工（外链清单）、手动外链
          台账 PENDING、解压缺密码/损坏、监听失败（源已删）
  failed  「哪些失败了、为什么」——retry 榜全量（次数/错误/下次重试/
          原帖跳转）+ 监听失败 + Pawchive 失败（附 CSV）
  retry   待重试全量明细（failed 的子集，单出用）
  queue   「系统现在在干嘛」——在途下载进度、队列积压、监听/解压/
          Pawchive 存量、115 容量
  stats   「最近干成了什么」——台账（事件流精确口径，N 天，默认 3）

/report [类型] —— 无参 = 全套；文件名 类型_YYYYMMDD-HHMM.txt
（failed/retry 另附 .csv）。导出尾段与 /115dup 共用（cd2_api.
upload_to_115）：本地必有存档（RUNTIME_DIR/userbot_reports/），
挂载失败不丢数据。
"""
import asyncio
import os
import re
import time

from . import cd2_api
from . import config
from . import runtime_db
from . import state
from .log import logger

REPORT_TYPES = ("manual", "failed", "retry", "queue", "stats",
                 "classify", "cmdq", "chrome")
STATS_DEFAULT_DAYS = 3

# 类型桶（classify 用）：扩展名 → 内容类型
_EXT_BUCKETS = {
    "视频": ("mp4", "mkv", "avi", "mov", "wmv", "flv", "ts", "webm", "m4v",
             "mpg", "mpeg", "3gp"),
    "图片": ("jpg", "jpeg", "png", "gif", "webp", "heic", "bmp", "jfif",
             "tiff", "avif"),
    "音频": ("mp3", "flac", "wav", "m4a", "ogg", "opus", "aac", "wma"),
    "压缩包": ("zip", "rar", "7z", "tar", "gz", "bz2", "xz"),
    "文档": ("pdf", "epub", "mobi", "txt", "doc", "docx", "xls", "xlsx",
             "ppt", "pptx", "md"),
}
_EXT_TO_BUCKET = {e: b for b, exts in _EXT_BUCKETS.items() for e in exts}


def _dt(ts):
    try:
        ts = int(ts or 0)
    except (TypeError, ValueError):
        ts = 0
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts \
        else "—"


def _safe(read, fallback, what):
    """采集器的统一容错：DB 不可用/读失败 → 空数据 + 记日志，报告照出。"""
    try:
        return read()
    except Exception as e:      # 含 DbUnavailable：运维报告绝不能因 DB 抖死
        logger.warning(f"_report 采集失败（{what}）：{e}")
        return fallback


def _origin_link(r):
    """retry 记录的原帖跳转（死文件可回原帖重新转发救回，最有用的一列）。"""
    oc, om = r.get("origin_chat_id"), r.get("origin_msg_id")
    try:
        oc_i = int(oc) if oc is not None else None
    except (TypeError, ValueError):
        oc_i = None
    if oc_i is not None and oc_i < -1000000000 and om:
        return f"https://t.me/c/{str(oc_i).replace('-100', '', 1)}/{om}"
    return ""


def _record_name(r):
    return (r.get("final_name") or r.get("label")
            or str(r.get("id") or "?"))[:80]


# ============================================================
# 采集器：每个返回 (正文, CSV 行列表或 None)
# ============================================================
def _collect_manual():
    from . import runtime_db
    lines = ["👤 待人工处理清单", "「需要我去原站手动下载 / 补密码 / 认死亡」的一切", ""]

    posts = _safe(lambda: runtime_db.list_pawchive_posts(
        status=runtime_db.PAW_POST_MANUAL, limit=200), [], "paw manual")
    lines.append(f"【Pawchive 待人工（外链去原站下载）】{len(posts)} 帖")
    for p in posts:
        lines.append(f"  #{p['id']} {str(p.get('creator_name') or '')[:20]}"
                     f"｜{str(p.get('title') or '')[:44]}")
        if p.get("post_url"):
            lines.append(f"    ↪ 原帖：{p['post_url']}")
        for l in (p.get("ext_links") or [])[:6]:
            lines.append(f"    外链：{str(l.get('url') or l)[:100]}")
    lines.append("")

    links = _safe(lambda: runtime_db.list_manual_links(
        status="PENDING", limit=200), [], "manual_links")
    lines.append(f"【手动外链台账 PENDING（登记了还没完成）】{len(links)} 条")
    for m in links:
        lines.append(f"  #{m['id']} {str(m.get('url') or '')[:80]}"
                     f"（{_dt(m.get('created_at'))}）"
                     + (f"｜{m['note']}" if m.get("note") else ""))
    lines.append("")

    for st, label in (("PASSWORD", "缺密码（/115x 目录名即密码可自动试）"),
                      ("TERMINAL", "已终结（损坏/炸弹，确认后可清理）")):
        rows = _safe(lambda st=st: runtime_db.list_extract_tasks(
            status=st, limit=100), [], f"extract {st}")
        lines.append(f"【115 解压 {label}】{len(rows)} 个")
        for t in rows:
            lines.append(f"  {str(t.get('remote_dir') or '')[-40:]}/"
                         f"{str(t.get('archive_name') or '')[:48]}"
                         f"（{str(t.get('error') or '')[:60]}）")
    lines.append("")

    failed = _safe(lambda: runtime_db.list_listener_tasks(
        status="FAILED", limit=100), [], "listener failed")
    lines.append(f"【监听失败（多为源消息已删/失效，重试无解）】{len(failed)} 条")
    for t in failed:
        lines.append(f"  #{t['id']} msg {t.get('message_id')}｜"
                     f"{str(t.get('last_error') or '')[:70]}")
    return "\n".join(lines), None


def _retry_rows():
    retry = list((state.QUEUE or {}).get("retry") or [])
    rows = []
    for i, r in enumerate(retry, start=1):
        rows.append({
            "no": i, "name": _record_name(r),
            "attempts": r.get("attempts", 0),
            "next_retry": _dt(r.get("next_retry_at")),
            "error": str(r.get("error") or "")[:120],
            "origin": _origin_link(r),
        })
    return rows


def _render_retry(rows):
    lines = [f"🔁 待重试列表：{len(rows)} 条（到期自动重放；原帖链接可手动救回）",
             ""]
    for r in rows:
        # 「已尝试/下次」独立成行：防 TG 把「URL（已尝试…）」整行吞成链接
        lines.append(f"{r['no']}. {r['name']}")
        lines.append(f"    ↻ 已尝试 {r['attempts']} 次｜下次 {r['next_retry']}")
        if r["error"]:
            lines.append(f"    错误：{r['error']}")
        if r["origin"]:
            lines.append(f"    ↪ 原帖：{r['origin']}")
    return "\n".join(lines)


def _retry_csv(rows):
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["no", "name", "attempts", "next_retry", "error", "origin"])
    for r in rows:
        w.writerow([r["no"], r["name"], r["attempts"], r["next_retry"],
                    r["error"], r["origin"]])
    return buf.getvalue()


def _collect_retry():
    rows = _retry_rows()
    return _render_retry(rows), _retry_csv(rows)


def _collect_failed():
    from . import runtime_db
    retry_rows = _retry_rows()
    lines = [f"❌ 失败全景（重试榜 {len(retry_rows)} 条 + 各链路终态失败）", ""]
    lines.append(_render_retry(retry_rows))
    lines.append("")

    failed = _safe(lambda: runtime_db.list_listener_tasks(
        status="FAILED", limit=200), [], "listener failed")
    lines.append(f"【监听失败】{len(failed)} 条")
    for t in failed:
        lines.append(f"  #{t['id']} msg {t.get('message_id')}｜"
                     f"{str(t.get('last_error') or '')[:70]}")
    lines.append("")

    posts = _safe(lambda: runtime_db.list_pawchive_posts(
        status=runtime_db.PAW_POST_FAILED, limit=200), [], "paw failed")
    lines.append(f"【Pawchive 失败（/paw retry 重投）】{len(posts)} 帖")
    for p in posts:
        lines.append(f"  #{p['id']} {str(p.get('title') or '')[:50]}"
                     f"（{str(p.get('last_error') or '')[:60]}）")
    return "\n".join(lines), _retry_csv(retry_rows)


def _collect_queue():
    from . import runtime_db
    from .naming import format_size
    q = state.QUEUE or {}
    lines = ["🚦 运行快照", ""]

    active = list((state.ACTIVE_DOWNLOADS or {}).values())
    lines.append(f"【在途下载】{len(active)} 个 / 队列 "
                 f"{len(q.get('tasks') or [])} 条")
    for a in sorted(active, key=lambda x: -(x.get("percent") or 0))[:20]:
        prog = (f"{a['percent']}%" if a.get("percent") is not None
                else "大小未知")
        lines.append(f"  {prog} {str(a.get('filename') or '')[:60]}")
    lines.append("")

    counts = _safe(runtime_db.count_pending_listener_tasks, None,
                   "listener pending")
    if counts is None:
        counts = "?"
    lines.append(f"【标签监听】待执行任务 {counts} 条")
    lines.append("")

    ec = _safe(runtime_db.extract_status_counts, {}, "extract counts") or {}
    if ec:
        lines.append("【115 解压】" + "｜".join(
            f"{k} {v}" for k, v in sorted(ec.items())))
        lines.append("")

    pc = _safe(runtime_db.pawchive_status_counts, {}, "paw counts") or {}
    if pc:
        lines.append("【Pawchive】" + "｜".join(
            f"{k} {v}" for k, v in sorted(pc.items())))
        lines.append("")

    from collections import Counter as _Counter
    ac = _safe(lambda: dict(_Counter(
        r["status"] for r in runtime_db.list_asmr_tasks(limit=200))), {},
        "asmr counts") or {}
    if ac:
        lines.append("【asmr 音声】" + "｜".join(
            f"{k} {v}" for k, v in sorted(ac.items())))
        lines.append("")

    try:
        sp = cd2_api.get_space_info()
    except Exception:
        sp = None
    if sp:
        total, used, free = sp
        lines.append(f"【115 容量】已用 {format_size(used)} / "
                     f"{format_size(total)}（余 {format_size(free)}）")
    return "\n".join(lines), None


def _collect_stats(days=STATS_DEFAULT_DAYS):
    from . import stats as stats_mod
    text = _safe(lambda: stats_mod.stats_text(days=days),
                 "（台账读取失败）", "stats")
    return text, None


def _collect_classify():
    """分类体检：全量清单（/115inv）聚合出的网盘结构画像。

    数据全来自 inv_* 表（清单过期/不全时头部如实提示）；重复概况取
    最近一次查重 run。只读不动任何文件。"""
    import json as _json
    from . import inventory
    from . import runtime_db
    from .naming import format_size

    try:
        all_tops = _json.loads(runtime_db.inv_get_state("all_tops") or "[]")
        done = _json.loads(runtime_db.inv_get_state("done_tops") or "[]")
    except Exception as e:
        return f"🧭 分类体检\n❌ 清单读取失败：{e}（/115inv scan 先建清单）", None
    if not all_tops:
        return ("🧭 分类体检\n还没有清单——/115inv scan [分钟] 先建"
                "（全盘约 60 分钟，可分段）", None)
    by_top = {r["top"]: r for r in _safe(runtime_db.inv_by_top, [],
                                         "inv_by_top")}
    by_top_ext = _safe(runtime_db.inv_by_top_ext, [], "inv_by_top_ext")

    lines = ["🧭 115 分类体检报告"]
    if len(done) < len(all_tops):
        lines.append(f"⚠️ 清单不完整（{len(done)}/{len(all_tops)} 个顶层目录）"
                     "——未扫部分不在统计内，/115inv scan 续扫")
    else:
        lines.append(f"清单覆盖 {len(all_tops)} 个顶层目录（/115inv reset 重扫）")
    lines.append("")

    lines.append("【顶层目录画像】（大小｜文件数｜类型构成｜最近写入）")
    for t in all_tops:
        r = by_top.get(t)
        if not r:
            lines.append(f"  {t}｜（未扫）")
            continue
        mix = {}
        for row in by_top_ext:
            if row["top"] != t:
                continue
            b = _EXT_TO_BUCKET.get(row["ext"], "其他")
            mix[b] = mix.get(b, 0) + row["bytes"]
        total = r["bytes"] or 1
        top3 = sorted(mix.items(), key=lambda x: -x[1])[:3]
        mix_s = "＋".join(f"{b} {p * 100 // total}%"
                          for b, p in top3) or "空"
        lines.append(f"  {t}｜{format_size(r['bytes'])}｜{r['n']} 个｜"
                     f"{mix_s}｜新至 {_dt(r['newest'])[:10]}")
    lines.append("")

    overall = {}
    for row in by_top_ext:
        b = _EXT_TO_BUCKET.get(row["ext"], "其他")
        overall[b] = overall.get(b, [0, 0])
        overall[b][0] += row["n"]
        overall[b][1] += row["bytes"]
    if overall:
        lines.append("【全盘类型分布】")
        for b, (n, bts) in sorted(overall.items(), key=lambda x: -x[1][1]):
            lines.append(f"  {b}：{n} 个｜{format_size(bts)}")
        lines.append("")

    stale = _safe(lambda: runtime_db.inv_inbox_stale(
        inventory.INBOX_TOPS, time.time() - 30 * 86400), [], "inv_inbox")
    stale = [s for s in stale if s["top"] in all_tops]
    if stale:
        lines.append("【收件箱滞留（temp/云下载/画集temp 超 30 天未归位）】")
        for s in stale:
            lines.append(f"  {s['top']}：{s['n']} 个｜{format_size(s['bytes'])}")
        lines.append("")

    empty = _safe(runtime_db.inv_empty_dirs, [], "inv_empty")
    if empty:
        lines.append(f"【空目录】{len(empty)} 个（样本）")
        for d in empty[:10]:
            lines.append(f"  {d}")
        if len(empty) > 10:
            lines.append(f"  … 其余 {len(empty) - 10} 个")
        lines.append("")

    try:
        run = runtime_db.dup_latest_run()
    except Exception:
        run = None
    if run and run.get("dup_groups"):
        lines.append(f"【重复概况】（最近查重：{run.get('status')}）")
        lines.append(f"  {run['dup_groups']} 组、{run['dup_files']} 个文件，"
                     f"可释放 {format_size(run['wasted_bytes'])}"
                     "（/115dup list 处理）")
    return "\n".join(lines), None


def _latest_local_chrome_csv():
    """本地存档里最新一份 chrome CSV（增量合并的上游；无则 None）。"""
    import glob
    cands = glob.glob(os.path.join(config.RUNTIME_DIR, "userbot_reports",
                                   "*", "chrome_*.csv"))
    return max(cands, key=os.path.getmtime) if cands else None


def _merged_chrome_rows(tasks):
    """当前台账 ∪ 上一份 CSV = 全历史台账（P0，2026-11-01 owner 定档）。

    chrome_tasks.json 终态只留 200 条，快照又被「同类只留最新」去旧——
    裁掉的行必须由合并保住。合并键 task_id（新增列；旧格式无此列的行
    原样保留）；当前台账是活状态，同 id 以当前为准。"""
    from . import chrome_agent
    cur = {}
    for t in tasks:
        tid = str(t.get("task_id") or "")
        if tid:
            cur[tid] = t
    # 旧格式行（无任务ID列）的过渡去重：链接+状态+完成时间 与当前任务
    # 一致 → 已被当前行代表，丢弃（否则每轮合并都带着重复旧行）
    cur_sig = {(t.get("url"), t.get("status"), t.get("finished_at"))
               for t in tasks}
    rows = []
    prev_path = _latest_local_chrome_csv()
    if prev_path:
        try:
            import csv as _csv
            with open(prev_path, encoding="utf-8-sig") as f:
                for r in _csv.DictReader(f):
                    tid = (r.get("任务ID") or "").strip()
                    if tid and tid in cur:
                        continue        # 当前台账是更新的活状态
                    if not tid and (r.get("链接"), r.get("状态"),
                                    r.get("完成时间")) in cur_sig:
                        continue        # 过渡期：已被当前行代表
                    rows.append(r)
        except Exception as e:
            logger.warning(f"📊 上一份 chrome CSV 读取失败（本次从当前"
                           f"台账起算）：{e}")
    rows.extend(cur.values())
    rows.sort(key=lambda t: str(t.get("finished_at")
                                or t.get("updated_at") or ""),
              reverse=True)
    return rows


def _collect_chrome():
    """Chrome 下载台账：每个链接一行——成功/失败全记录。CSV 为
    **全历史增量合并**（当前台账 ∪ 上一份 CSV），被 200 条窗口裁掉的
    行不丢；TXT 为当前窗口的人读视图。"""
    from . import chrome_agent
    tasks = chrome_agent.load_tasks(config.CHROME_TASKS_FILE)
    rows = _merged_chrome_rows(tasks)      # 全历史（喂 CSV）
    from collections import Counter
    counts = Counter(t.get("status") for t in tasks)
    total = len(tasks)
    head = (f"Chrome 下载台账（当前窗口）：共 {total} 条｜"
            f"✅成功 {counts.get('SUCCESS', 0)}｜❌失败 "
            f"{counts.get('FAILED', 0)}｜⏳等重试 "
            f"{counts.get('RETRY_WAIT', 0)}｜▶️进行中 "
            f"{counts.get('RUNNING', 0)}｜排队 {counts.get('PENDING', 0)}"
            f"｜已取消 {counts.get('CANCELLED', 0)}")

    lines = [head, ""]
    # 时间字段 chrome_tasks.json 本就存 '%Y-%m-%d %H:%M:%S' 本地时间串，直接展示
    def _show(s):
        return str(s or "")[:16] or "—"
    failed = [t for t in tasks if t.get("status") == "FAILED"]
    if failed:
        lines.append("【❌ 失败明细】")
        for t in failed:
            lines.append(f"  [{_show(t.get('updated_at'))}] "
                         f"第{t.get('attempts', '?')}次耗尽｜{t.get('url', '')}")
            if t.get("error"):
                lines.append(f"      ↳ {str(t['error'])[:120]}")
        lines.append("")
    ok_rows = [t for t in tasks if t.get("status") == "SUCCESS"]
    if ok_rows:
        lines.append("【✅ 最近成功（前 30 条）】")
        for t in ok_rows[:30]:
            lines.append(f"  [{_show(t.get('finished_at'))}] "
                         f"{t.get('filename') or '?'} ← {t.get('url', '')[:80]}")
        if len(ok_rows) > 30:
            lines.append(f"  … 其余 {len(ok_rows) - 30} 条见 CSV")
    inflight = [t for t in tasks if t.get("status") in
                ("RUNNING", "PENDING", "RETRY_WAIT")]
    if inflight:
        lines.append("")
        lines.append("【⏳ 在途/排队/等重试】")
        for t in inflight:
            nxt = t.get("next_retry_at") or ""
            lines.append(f"  [{t.get('status')}] 第{t.get('attempts', 0)}次"
                         f"{'｜下次 ' + nxt if nxt else ''}｜{t.get('url', '')[:80]}")

    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["任务ID", "状态", "尝试次数", "完成时间", "文件名",
                "链接", "错误", "更新时间"])
    for t in rows:
        if "task_id" in t:          # 新格式：chrome_tasks.json 的任务对象
            w.writerow([t.get("task_id", ""), t.get("status"),
                        t.get("attempts", 0), t.get("finished_at", ""),
                        t.get("filename", ""), t.get("url", ""),
                        str(t.get("error", "")),
                        t.get("updated_at", "")])
        else:                       # 旧 CSV 行（中文列名）：原样迁移
            w.writerow([t.get("任务ID", ""), t.get("状态", ""),
                        t.get("尝试次数", ""), t.get("完成时间", ""),
                        t.get("文件名", ""), t.get("链接", ""),
                        t.get("错误", ""), t.get("更新时间", "")])
    lines.insert(1, f"（CSV 为全历史台账 {len(rows)} 行；"
                    f"本 TXT 为当前 {len(tasks)} 条窗口视图）")
    return "\n".join(lines), buf.getvalue()


# ============================================================
# 同类报告去旧（2026-10-31 owner 定档：userBot报告里相同类型只留最新）
# ============================================================
_REPORT_TS_RE = re.compile(r"^(?P<type>.+?)_\d{8}-\d{4}")
FRESH_GRACE_MINUTES = 30   # 该窗口内的报告绝不清理（列表可见性竞态防护）


def prune_old_reports(target_dir="/115open/userBot报告", keep=1,
                      keep_by_type=None):
    """远端报告目录里**同类只留最新 keep 份**（跨日期目录全局比新旧）。

    类型 = 文件名里 `_<YYYYMMDD-HHMM>` 时间戳前的段（chrome/failed/…）；
    带时间戳的名字字典序即时间序。删除走 CD2 DeleteFile（**回收站**可
    恢复）。只在「新报告已成功上传后」调用——旧的先删、新的没传上会
    两头空。返回 (deleted, kept)。
    """
    import re as _re
    from .cd2_proto import clouddrive_pb2 as _pb
    stub, md = cd2_api._md()
    files = []   # (path, name)

    def _walk(path, depth=0):
        if depth > 2:
            return
        d = cd2_api.list_remote_dir(path, limit=200)
        if not d:
            return
        for name, _size, is_dir in d:
            full = f"{path}/{name}"
            if is_dir:
                _walk(full, depth + 1)
            else:
                files.append((full, name))

    _walk(str(target_dir).rstrip("/"))
    # 同时间戳的 .txt+.csv 是同一份报告的两个面 → 按「类型+时间戳」分组，
    # 保留最新 keep 个**完整单元**（txt/csv 成对存活，不丢可读版）
    units = {}
    for full, name in files:
        m = _REPORT_TS_RE.match(name)
        if not m:
            continue                           # 无时间戳的报告永不清理
        stem = name.rsplit(".", 1)[0]
        units.setdefault(m.group("type"), {}).setdefault(stem, []).append(full)
    # 新鲜宽限窗（2026-10-31 实查教训）：N 分钟内生成的报告永不删除——
    # 刚上传的文件在 115 服务端列表可见性有延迟，可能被误判成旧单元清掉
    now_s = time.strftime("%Y%m%d-%H%M")
    def _is_fresh(stem):
        ts = stem.rsplit("_", 1)[-1]
        return ts >= (time.strftime("%Y%m%d", time.localtime())
                      + time.strftime("-%H%M",
                                      time.localtime(time.time() - FRESH_GRACE_MINUTES * 60)))
    keep_map = keep_by_type or {}
    to_delete = []
    for type_key, stems in units.items():
        eff_keep = int(keep_map.get(type_key, keep))
        ordered = sorted(stems.items(), reverse=True)  # stem 含时间戳，倒序=最新在前
        for stem, paths in ordered[eff_keep:]:
            if _is_fresh(stem):
                continue                       # 宽限窗内：宁多留一份不冒险
            to_delete.extend(paths)
    if to_delete:
        # 逐路径留痕（2026-10-31 实查：一次去旧后最新 csv 消失，因无
        # 删除明细无法溯源——每条删除必须可查）
        for p in to_delete:
            logger.info(f"📊 报告去旧：🗑 {p}")
        cd2_api.delete_remotes(to_delete)
    kept = {k: len(v) - sum(1 for stem, paths in v.items()
                            if all(p in to_delete for p in paths))
            for k, v in units.items()}
    return len(to_delete), kept


_COLLECTORS = {
    "manual": lambda: _collect_manual(),
    "failed": lambda: _collect_failed(),
    "retry": lambda: _collect_retry(),
    "queue": lambda: _collect_queue(),
    "stats": lambda: _collect_stats(STATS_DEFAULT_DAYS),
    "classify": lambda: _collect_classify(),
    "cmdq": lambda: _collect_cmdq(),
    "chrome": lambda: _collect_chrome(),
}


def _collect_cmdq():
    """指令台账（/cmdq 同源）：状态计数 + 最近明细（含失败原因）。"""
    import time as _t
    try:
        counts = runtime_db.cmd_ledger_counts()
    except Exception as e:
        return f"📥 指令台账：读取失败（{e}）", None
    if not counts:
        return "📥 指令台账：空（尚无指令记录）", None
    zh = {"SUCCESS": "✅成功", "FAILED": "❌失败", "PENDING": "⏳待执行",
          "RUNNING": "🔄执行中", "STALE": "🕳过期跳过"}
    lines = ["📥 指令台账",
             "计数：" + " ".join(f"{zh.get(k, k)}{n}"
                                for k, n in sorted(counts.items())), ""]
    for r in runtime_db.list_cmd_ledger(limit=30):
        ts = _t.strftime("%m-%d %H:%M", _t.localtime(r["executed_at"] or
                                                     r["received_at"]))
        err = f"｜{str(r['error'] or '')[:40]}" if r.get("error") else ""
        lines.append(f"{ts} {zh.get(r['status'], r['status'])}"
                     f"｜{r['cmd_text'][:44]}{err}")
    return "\n".join(lines), None


# ============================================================
# 导出与命令
# ============================================================
def export_reports(types, target_dir=None):
    """采集 → 渲染 → 本地存档 → 传 115。返回 {ok, files, note, local_dir}。

    每类一个 TXT（failed/retry 附 CSV）；单类失败不断整个导出
    （运维报告的价值在「有多少出多少」）。
    """
    from .dup_scan import dated_report_dir
    target_dir = target_dir or dated_report_dir()
    out = {"ok": False, "files": [], "local_dir": None, "note": "",
           "remote_dir": str(target_dir)}
    # 按日期分目录（2026-10-24 owner 指令：日积月累文件太多）；
    # 本地与 115 两侧同构
    stem_suffix = time.strftime("%Y%m%d-%H%M")
    day = time.strftime("%Y-%m-%d")
    local_dir = os.path.join(config.RUNTIME_DIR, "userbot_reports", day)
    try:
        os.makedirs(local_dir, exist_ok=True)
        for t in types:
            text, csv_text = _COLLECTORS[t]()
            stem = f"{t}_{stem_suffix}"
            # utf-8-sig：带 BOM——Excel/老编辑器双击打开不再按 GBK 乱码
            with open(os.path.join(local_dir, f"{stem}.txt"), "w",
                      encoding="utf-8-sig") as f:
                f.write(text)
            out["files"].append(os.path.join(local_dir, f"{stem}.txt"))
            if csv_text:
                with open(os.path.join(local_dir, f"{stem}.csv"), "w",
                          encoding="utf-8-sig") as f:
                    f.write(csv_text)
                out["files"].append(os.path.join(local_dir, f"{stem}.csv"))
        out["local_dir"] = local_dir
    except OSError as e:
        out["note"] = f"本地存档失败：{e}"
        return out
    ok, note = cd2_api.upload_to_115(out["files"], target_dir,
                                     name_prefix=None)
    out["ok"] = ok
    out["note"] = note
    if ok:
        # 新报告已落云端才清旧的：同类只留最新 1 份（2026-10-31 定档）
        try:
            deleted, _kept = prune_old_reports(
                keep_by_type={"系统备份": 7})
            if deleted:
                logger.info(f"📊 报告去旧：同类只留最新，已清 {deleted} 份旧报告")
        except Exception as e:
            logger.warning(f"📊 报告去旧失败（不影响本次上传）：{e}")
    return out


USAGE = ("📤 /report [类型] —— 运维报告一键传 115"
         "（默认 /115open/userBot报告/）\n\n"
         "无参 = 全套；类型：\n"
         "　manual —— 待人工处理（Pawchive 外链/解压密码/监听失败）\n"
         "　failed —— 失败全景（重试榜全量+错误+原帖链接，附 CSV）\n"
         "　retry —— 待重试明细（附 CSV）\n"
         "　queue —— 运行快照（在途/积压/各链路存量/115 容量）\n"
         "　stats —— 台账（默认 3 天）\n"
        "　chrome —— Chrome 下载台账（每链接成败+错误，附 CSV）\n"
         "　classify —— 分类体检（需先 /115inv scan 建清单）\n"
         "（报告落 userBot报告/日期/ 子目录）")


async def command_reply(cmd_text):
    """/report …：采集快（秒级，全只读），同步执行直接回执。"""
    parts = str(cmd_text or "").split()
    arg = parts[1].lower() if len(parts) > 1 else ""
    if arg and arg not in REPORT_TYPES:
        return USAGE
    types = [arg] if arg else list(REPORT_TYPES)
    try:
        res = await asyncio.to_thread(export_reports, types)
    except RuntimeError as e:   # cd2_api 未配令牌
        return f"📤 运维报告\n❌ {e}"
    if not res["files"]:
        return f"📤 {res['note']}"
    names = "、".join(os.path.basename(p) for p in res["files"])
    if res["ok"]:
        return f"📤 已传 115：{res['remote_dir']}\n{names}"
    return (f"📤 未完成：{res['note']}\n本地存档：{res['local_dir']}"
            f"（目标 {res['remote_dir']}，可重试）")


def is_report_command(cmd_text):
    head = str(cmd_text or "").split()[0] if str(cmd_text or "").strip() else ""
    return head.lower() == "/report"


# ============================================================
# 每日失败报告自动推送（2026-10-29 定档：有限档失败烧到终态的明细，
# 按日期推 115 的 userBot 报告目录）
# ============================================================
FAILED_PUSH_STATE = "ops_failed_push.json"   # RUNTIME_DIR 下：上次推送指纹


def _push_state_path():
    return os.path.join(config.RUNTIME_DIR, FAILED_PUSH_STATE)


def failed_fingerprint():
    """当日失败量指纹（int 或 None=取不到）。四路终态合计：
    retry 榜 + 监听 FAILED + Pawchive 活跃失败 + Chrome 任务 FAILED。"""
    from . import runtime_db
    total = 0
    try:
        total += len(state.QUEUE.get("retry") or [])
    except Exception:
        pass
    try:
        total += len(runtime_db.list_listener_tasks(
            status="FAILED", limit=1000) or [])
    except Exception:
        pass
    try:
        total += runtime_db.pawchive_failed_active_count() or 0
    except Exception:
        pass
    try:
        tasks = _load_chrome_tasks_failed()
        total += tasks
    except Exception:
        pass
    return total


def _load_chrome_tasks_failed():
    """Chrome 任务台账里的 FAILED 计数（文件缺失/损坏 → 0）。"""
    import json
    path = os.path.join(config.RUNTIME_DIR, "chrome_tasks.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        tasks = (data or {}).get("tasks") or []
        return sum(1 for t in tasks
                   if isinstance(t, dict) and t.get("status") == "FAILED")
    except Exception:
        return 0


def _load_push_state():
    import json
    try:
        with open(_push_state_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_push_state(st):
    import json
    tmp = _push_state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, _push_state_path())


async def daily_failed_push(force=False):
    """每日失败报告推送（maintenance 循环调用；启动首跑即覆盖空窗）。

    语义：取当日（今天日期）失败指纹，与已推指纹比对——**有新增失败才
    推**（force=True 无条件推）；推送内容就是 failed 报告（retry 榜 +
    监听/Pawchive/Chrome 终态失败 + CSV），落到
    userBot报告/<今日>/，完成/跳过都 notify 一句话。
    返回描述文本（供日志与通知）。"""
    from . import notify
    today = time.strftime("%Y-%m-%d")
    st = _load_push_state()
    fp = failed_fingerprint()
    if fp is None:
        return "失败报告推送跳过（指纹取不到）"
    if not force and st.get("day") == today \
            and st.get("fingerprint") == fp:
        return f"失败报告无新增（指纹 {fp}），跳过"
    if not force and fp == 0:
        # 全绿日不推也不通知——「0 失败」没有信息量，天天推是打扰
        _save_push_state({"day": today, "fingerprint": 0,
                          "pushed_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        return "失败量 0，不推（静默）"
    out = export_reports(["failed"])
    _save_push_state({"day": today, "fingerprint": fp,
                      "pushed_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    note = (f"📤 每日失败报告已推 115（失败量 {fp}）："
            f"{out.get('remote_dir', '?')}")
    if not out.get("ok"):
        note = (f"⚠️ 每日失败报告推送未完成：{out.get('note', '')[:80]}"
                f"（本地留档 {out.get('local_dir', '?')}）")
    try:
        await notify.notify_user(note)
    except Exception:
        pass
    return note
