"""报告 CSV 执行器（/115dup apply，2026-10-24）——人工审表后的批量落地。

**工作流（用户设计，2026-10-24）**：bot 导出查重报告 CSV（9 列，含
``新目录``/``新文件名`` 两列空模板）→ 人在表格里标注意图（``deleted``
改 1＝删该份；H 列填新目录＝移动；I 列填新文件名＝重命名；不填＝不动）
→ ``/115dup apply <csv路径>`` dry-run 出执行计划 → 加 ``确认`` 重发执行。

**防丢失红线（用户明确要求"保证数据不丢"，全部硬约束）**：
1. 删除只走 CD2 ``DeleteFile``（115 回收站，可恢复），绝不永久删。
2. **组保全**：同一 group_id 的全部成员都被标删 → 默认拒绝该组；确属
   有意整组删除须显式加 ``全组`` 标记。
3. **绝不覆盖**：移动用 ConflictPolicy=Rename（同名冲突 CD2 自动改名
   保留双方）；重命名前预检目标名不存在，存在即跳过。
4. **行身份校验**：执行时逐行验证 源存在 + SHA1 与报告一致（报告到
   执行之间文件可能已变——不符即跳过，绝不动错文件）。
5. **事后校验**：每个操作完成后核对新路径存在且尺寸一致；移动/重命名
   后新旧路径双双不存在 = 文件失踪，立即标红上报。
6. 幂等：已处理过的行（源不存在）自动跳过，重复执行同一张表无害。
7. 全程审计：每行结果回写 ``*_执行结果.csv``（本地+115 同目录），
   操作逐行进日志。

执行是逐行独立（单行失败不断整批），CLI 只认 /115open 内的绝对路径。
"""
import asyncio
import csv
import io
import os
import time

from . import cd2_api
from . import config
from .log import logger

_TRUE_TOKENS = {"1", "true", "y", "yes", "是", "删"}
SHA1_LEN = 40


def _norm_cell(v):
    return str(v or "").strip()


def _is_delete(v):
    return _norm_cell(v).lower() in _TRUE_TOKENS


def _looks_sha1(v):
    v = _norm_cell(v).lower()
    return len(v) == SHA1_LEN and all(c in "0123456789abcdef" for c in v)


def parse_plan_csv(text):
    """CSV 文本 → 行 dict 列表。按表头名取列（列序变化不敏感），
    表头缺失时回落位置 1-9。抛 ValueError（表头认不出）。"""
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise ValueError("CSV 为空")
    header = [_norm_cell(h).lower() for h in rows[0]]
    col = {}
    for want, names in (("group_id", ("group_id", "组")),
                        ("sha1", ("sha1",)),
                        ("size_mb", ("size_mb",)),
                        ("directory", ("directory", "目录")),
                        ("filename", ("filename", "文件名")),
                        ("write_time", ("write_time", "写入时间")),
                        ("deleted", ("deleted",)),
                        ("new_dir", ("新目录", "new_dir", "h")),
                        ("new_name", ("新文件名", "new_name", "i"))):
        for n in names:
            if n in header:
                col[want] = header.index(n)
                break
    if "directory" not in col or "filename" not in col:
        # 位置回落：现模板 directory=第4列(下标3)、filename=第5列(下标4)
        col.setdefault("directory", 3)
        col.setdefault("filename", 4)

    def cell(r, key, default_idx):
        idx = col.get(key, default_idx)
        return _norm_cell(r[idx]) if idx < len(r) else ""

    out = []
    for r in rows[1:]:
        if not any(_norm_cell(c) for c in r):
            continue    # 空行
        row = {
            "group_id": cell(r, "group_id", 0),
            "sha1": cell(r, "sha1", 1).lower(),
            "directory": cell(r, "directory", 3),
            "filename": cell(r, "filename", 4),
            "write_time": cell(r, "write_time", 5),
            "deleted": cell(r, "deleted", 6),
            "new_dir": cell(r, "new_dir", 7),
            "new_name": cell(r, "new_name", 8),
        }
        row["path"] = (row["directory"].rstrip("/")
                       + "/" + row["filename"])
        out.append(row)
    return out


def _validate_target(row):
    """H/I 列的路径安全校验，返回错误文案或 ''。"""
    nd, nn = row["new_dir"], row["new_name"]
    if nd:
        if not nd.startswith("/115open/") or ".." in nd:
            return f"新目录必须在 /115open 内且无穿越：{nd}"
        if nd.endswith("/"):
            row["new_dir"] = nd.rstrip("/")
    if nn and ("/" in nn or nn in (".", "..")):
        return f"非法新文件名：{nn}"
    return ""


def build_plan(rows, allow_full_group=False):
    """行 → 执行计划。每行：{action, row, note}，note 非空=跳过原因。

    action ∈ delete / move / rename / combo / noop。校验全在做执行前——
    执行阶段会再校验一遍（防 dry-run 与确认之间状态漂移）。"""
    group_total = {}
    group_deleted = {}
    for r in rows:
        g = r["group_id"]
        group_total[g] = group_total.get(g, 0) + 1
        if _is_delete(r["deleted"]):
            group_deleted[g] = group_deleted.get(g, 0) + 1
    plan = []
    for r in rows:
        del_flag = _is_delete(r["deleted"])
        has_move, has_rename = bool(r["new_dir"]), bool(r["new_name"])
        note = ""
        if not (del_flag or has_move or has_rename):
            plan.append({"action": "noop", "row": r, "note": ""})
            continue
        if del_flag and (has_move or has_rename):
            note = "（H/I 已忽略：deleted 优先）"
            has_move = has_rename = False
        err = _validate_target(r)
        if err:
            plan.append({"action": "skip", "row": r, "note": err})
            continue
        if del_flag:
            g = r["group_id"]
            if not g:
                plan.append({"action": "skip", "row": r,
                             "note": "group_id 为空（表格列被破坏？）——"
                                     "无法验证组保全，拒绝执行"})
                continue
            if (not allow_full_group
                    and group_deleted.get(g, 0) >= group_total.get(g, 1)):
                plan.append({"action": "skip", "row": r,
                             "note": f"组 {g} 将被整组删除（唯一副本不复"
                                     "存在）——确属有意请加「全组」标记"})
                continue
            action = "delete"
        elif has_move and has_rename:
            action = "combo"
        elif has_move:
            action = "move"
        else:
            action = "rename"
        plan.append({"action": action, "row": r, "note": note})
    return plan


def _prefetch_index(rows):
    """执行前批量预取：涉及目录各列一次 → {full_path: (size, sha1)}。

    一次目录列表顶替 N 次逐文件 find_remote——后者实测单行分钟级
    （CD2 路径查找触发父目录回源），前者每目录一次 gRPC（2026-10-25
    优化）。目录列失败的行执行时回落逐文件查询，绝不含糊。"""
    dirs = sorted({r["directory"].rstrip("/") for r in rows
                   if r["directory"]})
    index = {}
    for d in dirs:
        files = cd2_api.list_dir_index(d)
        if files is None:
            continue    # 该目录的行执行时回落 find_remote
        for name, (size, sha1) in files.items():
            index[f"{d}/{name}"] = (size, sha1)
    return index


def _verify_row_live(row, index=None):
    """执行时行身份校验：源存在 + SHA1 一致。返回 (size, err)。

    index 命中（预取目录列表）→ 零 gRPC；未命中 → 回落 find_remote。
    防丢失语义不变：校验不过照样跳过，绝不动错文件。"""
    path = row["path"]
    if index is not None and path in index:
        size, sha1_up = index[path]
        if _looks_sha1(row["sha1"]) and sha1_up \
                and sha1_up.lower() != row["sha1"]:
            return None, (f"SHA1 不符（报告 {row['sha1'][:12]}… ≠ 当前 "
                          f"{sha1_up[:12]}…）——文件已变，跳过")
        return size, ""
    f = cd2_api.find_remote(path)
    if f is None:
        return None, "源不存在（可能已处理/被移动）——跳过"
    sha1 = ""
    hashes = getattr(f, "fileHashes", None)
    if hashes is not None:
        sha1 = str(hashes.get(cd2_api.HASH_KEY_SHA1) or "").lower()
    if _looks_sha1(row["sha1"]) and sha1 and sha1 != row["sha1"]:
        return None, (f"SHA1 不符（报告 {row['sha1'][:12]}… ≠ 当前 "
                      f"{sha1[:12]}…）——文件已变，跳过")
    return int(f.size), ""


def _dest_path(row):
    directory = row["new_dir"] or row["directory"]
    name = row["new_name"] or row["filename"]
    return f"{directory.rstrip('/')}/{name}"


def _ensure_dest_dir(dest_dir):
    from .extract_worker import mount_of
    mp = mount_of(dest_dir)
    if mp is None:
        return False, f"目标目录不在 /115open 内：{dest_dir}"
    try:
        os.makedirs(mp, exist_ok=True)
        return True, ""
    except OSError as e:
        return False, f"目标目录创建失败：{e}"


def _execute_one(item, verify=True, index=None, deleted=None):
    """执行单行，返回结果文案（以 ✅/⏭️/❌ 开头）。

    deleted：调用方传入的列表——删除成功的路径 append 进去，由
    apply_csv 收尾**一次批量打标**（原先每行一个小事务，且 apply 跑在
    to_thread 里与主循环的写库并发——2026-10-25 实测撞出
    "cannot start a transaction within a transaction"）。
    逐行删除后的 find_remote 复核也移除：DeleteFile 的 success 本身是
    服务端确认，收尾统一抽查（防丢失不降级——SHA1 校验仍在每行执行）。"""
    r = item["row"]
    if item["action"] == "noop":
        return "⏭️ 未标注，不动"
    if item["action"] == "skip":
        return f"⏭️ {item['note']}"
    if verify:
        size, err = _verify_row_live(r, index)
        if err:
            return f"⏭️ {err}"
    else:
        size = None
    if item["action"] == "delete":
        results = cd2_api.delete_remotes([r["path"]])
        ok, msg = results[0][1], results[0][2]
        if not ok:
            return f"❌ 删除失败：{msg}"
        if deleted is not None:
            deleted.append(r["path"])
        return "✅ 已删（回收站）"
    # move / rename / combo
    if r["new_dir"]:
        ok, msg = _ensure_dest_dir(r["new_dir"])
        if not ok:
            return f"❌ {msg}"
    cur_path = r["path"]
    if r["new_name"]:
        ok, msg = cd2_api.rename_remote(cur_path, r["new_name"],
                                        expected_size=size)
        if not ok:
            return f"❌ 重命名失败：{msg}"
        cur_path = f"{r['directory'].rstrip('/')}/{r['new_name']}"
    if r["new_dir"]:
        ok, msg = cd2_api.move_remote(cur_path, r["new_dir"],
                                      expected_size=size)
        if not ok:
            return f"❌ 移动失败：{msg}"
    final = _dest_path(r)
    if verify:
        f = cd2_api.find_remote(final)
        if f is None:
            return f"❌ ⚠️ 终态路径不可见：{final}——请立即核查"
        if size is not None and int(f.size) != size:
            return f"❌ 终态尺寸不符（{f.size} ≠ {size}）——请核查"
    return f"✅ → {final}"


def _render_plan(plan, csv_path, allow_full_group):
    ops = [p for p in plan if p["action"] != "noop"]
    skips = [p for p in plan if p["action"] == "skip"]
    n_del = sum(1 for p in ops if p["action"] == "delete")
    n_mv = sum(1 for p in ops if p["action"] in ("move", "rename", "combo"))
    lines = [f"📋 执行计划（{os.path.basename(csv_path)}）",
             f"标注行 {len(ops)}（删除 {n_del}｜移动/重命名 {n_mv}）"
             f"｜跳过 {len(skips)}｜未标注 {len(plan) - len(ops) - len(skips)}"]
    if skips:
        lines.append("")
        lines.append("⏭️ 将跳过：")
        for p in skips[:10]:
            lines.append(f"  · {p['row']['filename'][:40]}：{p['note']}")
        if len(skips) > 10:
            lines.append(f"  … 其余 {len(skips) - 10} 条")
    lines.append("")
    lines.append("操作预览（前 15 条）：")
    shown = 0
    for p in plan:
        if p["action"] == "noop" or shown >= 15:
            continue
        r = p["row"]
        dest = _dest_path(r) if p["action"] != "delete" else "（回收站）"
        lines.append(f"  {p['action']:6s} {r['path'][-52:]} → {dest[-52:]}")
        shown += 1
    lines.append("")
    lines.append(f'确认执行请发：/115dup apply {csv_path} 确认'
                 + (" 全组" if allow_full_group else ""))
    lines.append("（删除走回收站可恢复；移动冲突自动改名保留双方）")
    return "\n".join(lines)


def _write_results(rows_with_outcome, src_csv_path):
    """回写执行结果 CSV：本地 + 上传到源 CSV 同目录。返回本地路径。"""
    stem = os.path.basename(str(src_csv_path)).rsplit(".", 1)[0]
    local_dir = os.path.join(config.RUNTIME_DIR, "dup_reports")
    os.makedirs(local_dir, exist_ok=True)
    path = os.path.join(local_dir, f"{stem}_执行结果_{time.strftime('%H%M%S')}.csv")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["group_id", "directory", "filename", "action",
                "outcome", "detail"])
    for item, outcome in rows_with_outcome:
        r = item["row"]
        w.writerow([r["group_id"], r["directory"], r["filename"],
                    item["action"], outcome.split(" ", 1)[0],
                    outcome])
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(buf.getvalue())
    dest_dir = str(src_csv_path).rsplit("/", 1)[0]
    if dest_dir.startswith("/115open"):
        try:
            cd2_api.upload_to_115([path], dest_dir)
        except Exception as e:
            logger.warning(f"_apply 结果 CSV 上传失败（本地有档）：{e}")
    return path


def apply_csv(csv_path, confirmed=False, allow_full_group=False):
    """入口：读 CSV → 计划 → dry-run 文案 / 确认后执行。"""
    csv_path = str(csv_path).strip()
    if not csv_path:
        return ("用法：/115dup apply <csv路径> [确认] [全组]\n"
                "csv 可为 /115open/… 路径或本地路径；先 dry-run 看计划")
    text = None
    if csv_path.startswith("/115open"):
        text = cd2_api.read_remote_text(csv_path)
        if text is None:
            return f"❌ 115 上读不到：{csv_path}（路径对吗？已上传覆盖？）"
    elif os.path.exists(csv_path):
        with open(csv_path, "rb") as f:
            raw = f.read()
        for enc in ("utf-8-sig", "utf-8", "gbk"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        text = text or raw.decode("utf-8", errors="replace")
    else:
        return f"❌ 文件不存在：{csv_path}"
    try:
        rows = parse_plan_csv(text)
    except ValueError as e:
        return f"❌ CSV 解析失败：{e}"
    plan = build_plan(rows, allow_full_group)
    if not confirmed:
        return _render_plan(plan, csv_path, allow_full_group)

    ops = [p for p in plan if p["action"] != "noop"]
    if not ops:
        return "没有可执行的标注行（全部未标注或被跳过）"
    # 预取目录索引（每目录一次 gRPC，替代逐行 find_remote 的分钟级回源）
    logger.info(f"📋 预取目录索引（{len(ops)} 个标注行）…")
    index = _prefetch_index([p["row"] for p in ops])
    logger.info(f"📋 索引就绪：{len(index)} 个文件")
    outcomes = []
    deleted_paths = []      # 删除成功的路径——收尾一次批量打标
    done = fail = skip = 0
    for i, item in enumerate(plan):
        if item["action"] == "noop":
            continue
        try:
            outcome = _execute_one(item, index=index,
                                   deleted=deleted_paths)
        except Exception as e:
            outcome = f"❌ 异常：{type(e).__name__}: {e}"[:200]
            logger.exception(f"_apply 行执行异常：{item['row']['path']}")
        outcomes.append((item, outcome))
        if outcome.startswith("✅"):
            done += 1
        elif outcome.startswith("⏭️"):
            skip += 1
        else:
            fail += 1
            logger.error(f"_apply 失败：{item['row']['path']}：{outcome}")
        if (i + 1) % 50 == 0:
            logger.info(f"📋 CSV 执行进度：{i + 1}/{len(plan)} 行")
        # 打标分批 flush：中断（重启/崩溃）时已删行不丢打标——重跑幂等
        if len(deleted_paths) >= 200:
            try:
                from . import runtime_db
                runtime_db.dup_mark_deleted_paths(deleted_paths)
                deleted_paths.clear()
            except Exception as e:
                logger.error(f"_apply 分批打标失败（继续执行）：{e}")
    # 批量打标（单事务一次写完——原先每行一个小事务，跨线程与主循环
    # 写库并发撞车；见 _execute_one 注释）
    if deleted_paths:
        try:
            from . import runtime_db
            runtime_db.dup_mark_deleted_paths(deleted_paths)
        except Exception as e:
            logger.error(f"_apply 批量打标失败（{len(deleted_paths)} 行，"
                         f"下次重跑同表会因源不存在自动跳过）：{e}")
    # 抽查复核（删除已被 DeleteFile 服务端确认，这里抽 3 行兜底验尸）
    spot_bad = 0
    for r in deleted_paths[:3]:
        try:
            if cd2_api.find_remote(r) is not None:
                spot_bad += 1
                logger.error(f"_apply 抽查：删除后仍存在 {r}——请核查！")
        except Exception:
            pass
    if spot_bad:
        outcomes.append((
            {"action": "spot", "row": {"filename": "抽查复核",
                                       "directory": "", "group_id": "-",
                                       "path": "（抽查）"}},
            f"❌ 抽查发现 {spot_bad} 个已删路径仍存在——请核查"))
    try:
        res_path = _write_results(outcomes, csv_path)
    except Exception as e:
        res_path = f"（结果回写失败：{e}）"
        logger.exception("_apply 结果回写失败")
    lines = [f"📋 执行完成：成功 {done}｜跳过 {skip}｜失败 {fail}",
             f"结果明细：{res_path}（已同步上传 115 同目录）"]
    fails = [(it, o) for it, o in outcomes if o.startswith("❌")]
    for it, o in fails[:8]:
        lines.append(f"  {o}｜{it['row']['path'][-60:]}")
    if len(fails) > 8:
        lines.append(f"  … 其余 {len(fails) - 8} 条见结果 CSV")
    if fail:
        lines.append("（失败行不影响其他行；多为目标已存在/源已变化，"
                     "核对后可修改 CSV 重新 apply）")
    return "\n".join(lines)


# 后台执行状态（防重入）+ 在途 task 强引用（§35 同款）。
# 确认执行以分钟计且曾绑在命令协程上被断线连坐（2026-10-25 实测：
# 协程死、线程跑、用户永远等不到回执）——现在确认执行转后台 task，
# 完成走 notify_user；dry-run 秒级仍同步回。
_APPLY_RUNNING = {}
_APPLY_TASKS = set()
APPLY_MARK_FILE = "ops_apply_running.json"   # RUNTIME_DIR 下的中断标记


def _mark_path():
    return os.path.join(config.RUNTIME_DIR, APPLY_MARK_FILE)


def _write_mark(csv_path, allow_full_group):
    import json
    path = _mark_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"csv": csv_path, "full_group": bool(allow_full_group),
                   "started": int(time.time())}, f, ensure_ascii=False)
    os.replace(tmp, path)


def _clear_mark():
    try:
        os.unlink(_mark_path())
    except OSError:
        pass


def recover_interrupted():
    """启动恢复：读中断标记（有则返回 dict 并清掉；无则 None）。

    apply 是幂等的——已打标行（deleted/moved）重跑自动跳过，所以中断
    的正确恢复动作就是提示用户重发同一条命令。
    """
    import json
    try:
        with open(_mark_path(), "r", encoding="utf-8") as f:
            mark = json.load(f)
    except Exception:
        _clear_mark()      # 损坏标记同样清掉（含写一半的残文件）
        return None
    _clear_mark()
    if not isinstance(mark, dict) or not mark.get("csv"):
        return None
    logger.warning(f"[apply] 重启恢复：上次执行被中断（{mark.get('csv')}），"
                   "幂等可重跑")
    return mark


async def run_apply_and_notify(csv_path, allow_full_group):
    """后台跑确认执行，完成 notify_user（异常兜底，通知必达）。"""
    from . import notify
    global _APPLY_RUNNING
    _write_mark(csv_path, allow_full_group)
    try:
        try:
            text = await asyncio.to_thread(
                apply_csv, csv_path, True, allow_full_group)
        except Exception as e:
            logger.exception("_apply 后台执行异常")
            text = f"📋 报告执行异常：{type(e).__name__}: {e}\n" \
                   f"已执行部分幂等可重跑：/115dup apply {csv_path} 确认"
        try:
            await notify.notify_user(text)
        except Exception:
            pass
    finally:
        _APPLY_RUNNING = {}
        _clear_mark()


async def command_reply(cmd_text):
    """/115dup apply 入口：dry-run 同步回执；确认执行后台化 + 通知。"""
    global _APPLY_RUNNING
    from .dup_scan import parse_dup_command
    parsed = parse_dup_command(cmd_text)
    if parsed is None:
        return None
    _action, args = parsed
    if not args["confirmed"]:
        return await asyncio.to_thread(
            apply_csv, args["csv"], False, args["allow_full_group"])
    if _APPLY_RUNNING:
        return ("📋 已有报告执行在进行（" +
                str(_APPLY_RUNNING.get("csv", ""))[-60:] +
                "），完成会通知")
    _APPLY_RUNNING = {"csv": args["csv"]}
    task = asyncio.create_task(
        run_apply_and_notify(args["csv"], args["allow_full_group"]))
    _APPLY_TASKS.add(task)
    task.add_done_callback(_APPLY_TASKS.discard)
    n_hint = ""
    return ("📋 开始执行（完成后通知；中断可重发同命令续跑，"
            "已执行行自动跳过）\n" + str(args["csv"]) + n_hint)
