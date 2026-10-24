"""115 全量清单（/115inv，2026-10-24）——分类管理的数据底座。

**为什么**：网盘整理的第一步不是移动任何东西，而是先知道「有什么」。
查重扫描只留重复组，这里把**全部文件**（路径/大小/SHA1/写入时间/
空目录）按顶层目录分段落进 inv_files（schema v15）——classify 体检
报告、后续 /115mv 的 dry-run 校验都从这里读，SHA1 还能喂将来的
上传判重。

**分段语义**：以顶层目录为单位扫描，完成的记进 inv_state 的
done_tops，中断/超时则该顶层目录**不记完成**（下次整段重扫，先删
旧行再写——增量以整段为准，绝不留半截数据冒充完整）。

/115inv —— 进度视图｜/115inv scan [分钟]（默认 10，上限 1440）
/115inv reset —— 清空清单重扫。后台 task 纪律与 /115dup scan 同款：
先回执、强引用（§35）、防重入同步置位、完成 notify_user 通知。
"""
import asyncio
import json
import os
import time

from . import cd2_api
from .log import logger

INV_DEFAULT_MIN = 10
INV_MAX_MIN = 1440
INBOX_TOPS = ("temp", "云下载", "画集temp")   # 收件箱候选（体检报告用）

_INV_RUNNING = {}
_INV_TASKS = set()


def _ext_of(path):
    _, ext = os.path.splitext(str(path))
    return ext.lstrip(".").lower()[:8]


def list_top_dirs():
    """顶层目录名（跳过点开头；CD2 不可用抛 RuntimeError 由调用方转文案）。"""
    from .cd2_proto import clouddrive_pb2 as pb
    stub, md = cd2_api._md()
    tops = []
    for reply in stub.GetSubFiles(
            pb.ListSubFileRequest(path="/115open"),
            metadata=md, timeout=60):
        for f in reply.subFiles:
            if f.isDirectory and not f.name.startswith("."):
                tops.append(f.name)
    return sorted(tops)


def inventory_scan(minutes=INV_DEFAULT_MIN):
    """阻塞跑一段清单（后台 task 的线程体）。返回进度 dict。"""
    from . import runtime_db
    minutes = max(1, min(int(minutes), INV_MAX_MIN))
    deadline = time.monotonic() + minutes * 60
    tops = list_top_dirs()
    try:
        done = set(json.loads(runtime_db.inv_get_state("done_tops") or "[]"))
    except Exception:
        done = set()
    runtime_db.inv_set_state("all_tops", json.dumps(tops, ensure_ascii=False))
    todo = [t for t in tops if t not in done]
    new_files = new_bytes = 0
    for t in todo:
        remain = deadline - time.monotonic()
        if remain <= 0:
            break
        root = f"/115open/{t}"
        raw, st = cd2_api.scan_remote(root=root, deadline_s=remain)
        files = [{"path": p, "ext": _ext_of(p), "size": s, "sha1": h,
                  "write_time": w} for p, s, h, w in raw]
        try:
            runtime_db.inv_replace_top(t, files, st.get("empty_dirs") or [])
        except runtime_db.DbUnavailable as e:
            logger.warning(f"📦 清单落库失败（{t}，本段作废重试）：{e}")
            break
        if st.get("aborted"):
            # 时限到：该顶层目录只落了半截——**不记完成**，下次整段重扫
            logger.info(f"📦 清单时限到，{t} 只扫了一部分（下次重扫该目录）")
            break
        done.add(t)
        try:
            runtime_db.inv_set_state("done_tops",
                                     json.dumps(sorted(done),
                                                ensure_ascii=False))
        except runtime_db.DbUnavailable as e:
            logger.warning(f"📦 清单进度写失败（{t} 已入库，下次可能重扫）：{e}")
        new_files += len(files)
        new_bytes += sum(f["size"] for f in files)
        logger.info(f"📦 清单完成 {t}：{len(files)} 文件｜累计新增 "
                    f"{new_files} 文件")
    try:
        n, b, _e = runtime_db.inv_counts()
    except runtime_db.DbUnavailable:
        n = b = 0
    return {"tops": tops, "done": sorted(done), "todo": [t for t in tops
                                                          if t not in done],
            "files": n, "bytes": b, "new_files": new_files,
            "new_bytes": new_bytes}


def status_text():
    """/115inv 进度视图（不扫盘，DB 不可用降级提示）。"""
    from . import runtime_db
    from .naming import format_size
    if _INV_RUNNING:
        return ("📦 清单扫描进行中" +
                f"（{_INV_RUNNING.get('minutes', '?')} 分钟时限），完成会通知")
    try:
        done = json.loads(runtime_db.inv_get_state("done_tops") or "[]")
        all_tops = json.loads(runtime_db.inv_get_state("all_tops") or "[]")
        n, b, empty = runtime_db.inv_counts()
    except Exception as e:
        return f"📦 清单读取失败：{e}"
    if not all_tops:
        return ("📦 还没有清单。/115inv scan [分钟] 开始（默认 10 分钟/段，"
                "全盘约需 60 分钟，可分多次跑）")
    todo = [t for t in all_tops if t not in done]
    lines = [f"📦 115 全量清单：{len(done)}/{len(all_tops)} 个顶层目录已扫",
             f"文件 {n} 个｜{format_size(b)}｜空目录 {empty} 个"]
    if todo:
        lines.append(f"未扫：{'、'.join(todo[:8])}"
                     + ("…" if len(todo) > 8 else ""))
        lines.append("继续：/115inv scan [分钟]")
    else:
        lines.append("✅ 清单完整——/report classify 出分类体检报告")
    return "\n".join(lines)


async def run_inv_and_notify(minutes):
    """后台主体：分段清单 → 完成通知（异常兜底与 /115dup 同款）。"""
    from . import notify
    global _INV_RUNNING
    try:
        try:
            res = await asyncio.to_thread(inventory_scan, minutes)
        except RuntimeError as e:
            text = f"📦 115 清单\n❌ {e}"
        else:
            text = (f"📦 清单扫描完成（本轮 {minutes} 分钟）\n"
                    f"进度 {len(res['done'])}/{len(res['tops'])} 个顶层目录｜"
                    f"累计 {res['files']} 文件 "
                    f"({round(res['bytes'] / 1048576)} MB)")
            if res["todo"]:
                text += (f"\n未扫：{('、'.join(res['todo'][:6]))}"
                         f"{'…' if len(res['todo']) > 6 else ''}"
                         f"\n继续：/115inv scan {minutes}")
            else:
                text += "\n✅ 清单完整——/report classify 出分类体检报告"
        try:
            await notify.notify_user(text)
        except Exception:
            pass
    finally:
        _INV_RUNNING = {}


async def command_reply(cmd_text):
    """/115inv [scan [分钟]] / reset / 裸命令=进度视图。

    兼容下划线标准形（/115inv_scan 60、/115inv_reset）与空格形。"""
    global _INV_RUNNING
    parts = str(cmd_text or "").split()
    head = parts[0].lower()
    if head == "/115inv_scan":
        parts = ["/115inv", "scan"] + parts[1:]
    elif head == "/115inv_reset":
        parts = ["/115inv", "reset"]
    sub = parts[1].lower() if len(parts) > 1 else ""
    if sub == "reset":
        from . import runtime_db
        try:
            runtime_db.inv_reset()
        except Exception as e:
            return f"📦 清单清空失败：{e}"
        return "📦 清单已清空（进度归零）。/115inv scan 重新开始"
    if sub == "scan":
        if _INV_RUNNING:
            return "📦 清单扫描已在进行中，完成会通知"
        minutes = INV_DEFAULT_MIN
        if len(parts) > 2 and parts[2].isdigit():
            minutes = max(1, min(int(parts[2]), INV_MAX_MIN))
        # 防重入同步置位（赋值使其成局部名——global 必须声明在函数顶部）
        _INV_RUNNING = {"minutes": minutes}
        task = asyncio.create_task(run_inv_and_notify(minutes))
        _INV_TASKS.add(task)
        task.add_done_callback(_INV_TASKS.discard)
        return (f"📦 清单扫描已启动（{minutes} 分钟时限，按顶层目录分段，"
                "完成会通知；/115inv 看进度）")
    return await asyncio.to_thread(status_text)


def is_inv_command(cmd_text):
    head = str(cmd_text or "").split()[0] if str(cmd_text or "").strip() else ""
    return head.lower() in ("/115inv", "/115inv_scan", "/115inv_reset")
