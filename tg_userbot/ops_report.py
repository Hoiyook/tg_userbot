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
import time

from . import cd2_api
from . import config
from . import state
from .log import logger

REPORT_TYPES = ("manual", "failed", "retry", "queue", "stats", "classify")
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
        lines.append(f"{r['no']}. {r['name']}（已尝试 {r['attempts']} 次，"
                     f"下次 {r['next_retry']}）")
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


_COLLECTORS = {
    "manual": lambda: _collect_manual(),
    "failed": lambda: _collect_failed(),
    "retry": lambda: _collect_retry(),
    "queue": lambda: _collect_queue(),
    "stats": lambda: _collect_stats(STATS_DEFAULT_DAYS),
    "classify": lambda: _collect_classify(),
}


# ============================================================
# 导出与命令
# ============================================================
def export_reports(types, target_dir=None):
    """采集 → 渲染 → 本地存档 → 传 115。返回 {ok, files, note, local_dir}。

    每类一个 TXT（failed/retry 附 CSV）；单类失败不断整个导出
    （运维报告的价值在「有多少出多少」）。
    """
    from .dup_scan import REPORT_DIR_DEFAULT
    target_dir = target_dir or REPORT_DIR_DEFAULT
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
    ok, note = cd2_api.upload_to_115(out["files"], f"{target_dir}/{day}",
                                     name_prefix=None)
    out["ok"] = ok
    out["note"] = note
    return out


USAGE = ("📤 /report [类型] —— 运维报告一键传 115"
         "（默认 /115open/userBot报告/）\n\n"
         "无参 = 全套；类型：\n"
         "　manual —— 待人工处理（Pawchive 外链/解压密码/监听失败）\n"
         "　failed —— 失败全景（重试榜全量+错误+原帖链接，附 CSV）\n"
         "　retry —— 待重试明细（附 CSV）\n"
         "　queue —— 运行快照（在途/积压/各链路存量/115 容量）\n"
         "　stats —— 台账（默认 3 天）\n"
         "　classify —— 分类体检（需先 /115inv scan 建清单）")


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
