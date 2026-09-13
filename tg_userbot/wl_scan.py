"""下载白名单 —— 扫描生产者（停机补漏）+ 两条生产链共用的助手。

白名单改成双通道后（规格书 docs/plan/下载白名单双通道扫描制改造_设计规格.md）：

    whitelist_config.json（/wl）
       ├─ 事件生产者【在线实时】app.record_whitelist_media → 记任务
       └─ 扫描生产者【停机补漏】本模块按 wl 游标周期扫描 → 记任务

    listener_tasks（唯一索引跨链路防双转发）──► listener_worker 统一转发

本模块与标签监听 Scanner（listener.py）同构：**只建任务、不转发**；取消息
复用 listener 的同一批函数（netio 收口、相册边界补齐）。与监听扫描的三处
刻意差异：命中条件是「可下载媒体」而非标签；**不跳过镜像帖**（白名单契约
是「这个聊天的一切媒体都要」）；游标独立在 chain='wl'。

事件生产者也共用本模块的 dedup 前置与任务构建助手（all_members_dedup_hit /
经 listener.build_saved_messages_task），保证两条链产出形状一致。
"""
import asyncio
import time

from . import config
from . import dedup
from . import listener
from . import runtime_db
from . import state
from . import whitelist
from .log import logger
from .naming import pick_group_caption_text
from .sources import is_downloadable, resolve_origin_snapshot

# 扫描重入保护（与 listener._SCANNING 分开；检查与置位之间无 await）。
_SCANNING = False

# 连续失败的聊天（死聊天降噪：只报第一次 ERROR，成功后清除）。
_FAILING_CHATS = set()


def _mark_chat_failure(chat_id, reason):
    if chat_id in _FAILING_CHATS:
        logger.info(f"📋 白名单聊天 {chat_id} 扫描仍失败（降噪不重复报）：{reason}")
        return
    _FAILING_CHATS.add(chat_id)
    logger.error(f"📋 白名单聊天 {chat_id} 扫描失败：{reason}")


def _clear_chat_failure(chat_id):
    _FAILING_CHATS.discard(chat_id)


# ============================================================
# 两条生产链共用的助手
# ============================================================
def all_members_dedup_hit(media) -> bool:
    """整组**全部成员**的判重键都命中已下载/在途索引。

    dedup 前置（规格 §8）：回补时已下载过的内容连转发都不做——收藏夹不被
    重复副本刷屏。部分命中照建任务（下载侧 per-copy 拦截兜底）；键拿不到
    （None/空）按未命中处理，绝不因判重不确定性丢媒体。
    """
    for m in media or []:
        keys = dedup.media_keys(m)
        if not keys:
            return False
        skip, _ = dedup.should_skip(keys)
        if not skip:
            return False
    return bool(media)


# ============================================================
# 扫描
# ============================================================
async def scan_wl_chat(chat_id, nap=None):
    """扫一个白名单聊天：按 wl 游标分页取新消息 → 建任务 → 同事务推游标。

    分页上限（规格 §5）：每轮最多 WHITELIST_SCAN_PAGES_PER_ROUND 页、页间睡
    WHITELIST_SCAN_PAGE_SLEEP_SECONDS——几千条积压摊到多轮读，不一口气砸请求。
    背压（规格 §8）：待执行 ≥ LISTEN_MAX_PENDING_TASKS 停扫、游标原地不动。

    返回 {scanned, created, duplicate, capped, failed}。
    """
    chat_id = int(chat_id)
    result = {"scanned": 0, "created": 0, "duplicate": 0,
              "capped": False, "failed": False}
    nap = nap or asyncio.sleep
    page_size = int(config.LISTEN_MAX_MESSAGES_PER_SCAN)
    pages = max(1, int(config.WHITELIST_SCAN_PAGES_PER_ROUND))
    page_sleep = float(config.WHITELIST_SCAN_PAGE_SLEEP_SECONDS)

    checkpoint = runtime_db.get_listener_checkpoint(chat_id, chain="wl")
    if checkpoint is None:
        newest = await listener.fetch_newest_id(chat_id)
        if newest is None:
            result["failed"] = True
            _mark_chat_failure(chat_id, "读不到最新消息（无 wl 游标）")
            return result
        runtime_db.set_listener_checkpoint(chat_id, newest, chain="wl")
        checkpoint = newest
        logger.info(
            f"📋 白名单扫描初始化游标：{chat_id} → {newest}"
            "（不扫历史；回补存量用 /wl since）")

    for page_no in range(pages):
        budget = (int(config.LISTEN_MAX_PENDING_TASKS)
                  - runtime_db.count_pending_listener_tasks())
        if budget <= 0:
            result["capped"] = True
            logger.warning(
                f"📋 待执行任务已达上限 {config.LISTEN_MAX_PENDING_TASKS}，"
                f"{chat_id} 的 wl 游标停在 {checkpoint}，Worker 消费后下轮继续")
            return result

        msgs = await listener.fetch_new_messages(chat_id, checkpoint)
        if msgs is None:
            result["failed"] = True
            _mark_chat_failure(chat_id, "读取新消息失败")
            return result
        result["scanned"] += len(msgs)
        if not msgs:
            _clear_chat_failure(chat_id)
            break

        tasks = []
        processed_upto = checkpoint
        budget_left = True
        for unit in listener.group_by_album(msgs):
            unit_max_id = max(m.id for m in unit)
            media = [m for m in unit if is_downloadable(m)]
            if not media:
                # 纯文本：不建任务（与监听扫描同一纪律），游标照推
                processed_upto = max(processed_upto, unit_max_id)
                continue
            if len(tasks) >= budget:
                budget_left = False
                logger.warning(
                    f"📋 {chat_id} 本轮队列额度用尽，停在消息 "
                    f"{min(m.id for m in media)}（下轮继续）")
                break
            if all_members_dedup_hit(media):
                logger.info(
                    f"📋 {chat_id} #{min(m.id for m in media)}：整组已下载过"
                    "（dedup 前置），跳过转发")
                processed_upto = max(processed_upto, unit_max_id)
                continue
            gid = getattr(media[0], "grouped_id", None)
            caption = (pick_group_caption_text(media, gid) if gid
                       else (getattr(media[0], "message", "") or "").strip())
            origin = await resolve_origin_snapshot(media[0])
            tasks.extend(listener.build_saved_messages_task(
                chat_id, media, caption, origin))
            processed_upto = max(processed_upto, unit_max_id)

        if tasks or processed_upto != checkpoint:
            ids = runtime_db.enqueue_listener_tasks(
                chat_id, tasks, checkpoint=processed_upto,
                chain="wl", origin="wl")
            created = sum(1 for i in ids if i)
            result["created"] += created
            result["duplicate"] += len(ids) - created
        checkpoint = processed_upto

        # 走到这里说明本轮读消息成功——聊天可达，失败降噪标记就地清除
        #（背压不是聊天故障，也要清；失败路径在上面各自 return，不清）。
        _clear_chat_failure(chat_id)
        if not budget_left:
            result["capped"] = True
            break
        if len(msgs) < page_size:
            break
        if page_no < pages - 1 and page_sleep > 0:
            await nap(page_sleep)
    return result


async def scan_all(manual=False) -> dict:
    """扫描全部白名单聊天（wl 链）。被重入挡下时带 skipped。

    每轮重新遍历 state.WHITELIST_CHATS（/wl add/del 即时生效）；**不跟随
    LISTEN_ENABLED**——白名单没有总开关概念，/wl del 移除即停。单聊天失败
    不影响其它（与 listener.scan_all 同款隔离）。
    """
    global _SCANNING
    empty = {"chats": 0, "failed_chats": 0, "scanned": 0, "created": 0,
             "duplicate": 0, "capped": 0}
    if _SCANNING:
        logger.info("📋 已有白名单扫描在进行中，跳过本次")
        return dict(empty, skipped=True)
    if not state.RUNTIME_DB_READY:
        return dict(empty, db_unavailable=True)
    chats = dict(state.WHITELIST_CHATS or {})
    if not chats:
        return dict(empty, empty_chats=True)

    _SCANNING = True
    totals = dict(empty)
    try:
        for chat_id in sorted(chats):
            totals["chats"] += 1
            try:
                r = await scan_wl_chat(chat_id)
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except runtime_db.DbUnavailable as e:
                totals["failed_chats"] += 1
                _mark_chat_failure(chat_id, f"数据库不可用：{e}")
                continue
            except Exception as e:
                totals["failed_chats"] += 1
                _mark_chat_failure(chat_id, f"{type(e).__name__}: {e}")
                continue
            if r.get("failed"):
                totals["failed_chats"] += 1
                continue
            _clear_chat_failure(chat_id)
            for key in ("scanned", "created", "duplicate"):
                totals[key] += r.get(key, 0)
            if r.get("capped"):
                totals["capped"] += 1
        totals["ts"] = time.strftime("%H:%M")
        totals["queue"] = _wl_queue_totals()
        state.WL_LAST_SCAN = dict(totals)
        logger.info(
            f"📋 白名单扫描完成（{'手动' if manual else '定时'}）："
            f"{totals['chats']} 个聊天 | 检查 {totals['scanned']} 条 | "
            f"落盘任务 {totals['created']} 条"
            + (f" | 重复跳过 {totals['duplicate']} 条" if totals["duplicate"] else "")
            + (f" | {totals['capped']} 个聊天触到队列上限" if totals["capped"] else "")
            + (f" | 失败 {totals['failed_chats']} 个" if totals["failed_chats"] else "")
        )
        return totals
    finally:
        _SCANNING = False


def _wl_queue_totals():
    """wl 任务的存量（视图展示用；DB 不可用返回 None）。"""
    try:
        return runtime_db.get_listener_stats(origin="wl")
    except runtime_db.DbUnavailable:
        return None


def is_scanning() -> bool:
    return _SCANNING


# 「回补/立即扫描」后台任务强引用（§35：asyncio 只对 Task 持弱引用，
# 不持引用可能被 GC——命令与菜单的 fire-and-forget 扫描共用这里）。
_SPAWNED_SCANS = set()


def spawn_scan(manual=True):
    """把一轮即时扫描挂成后台任务并保持强引用，返回该任务。"""
    task = asyncio.create_task(scan_all(manual=manual))
    _SPAWNED_SCANS.add(task)
    task.add_done_callback(_SPAWNED_SCANS.discard)
    return task


# ============================================================
# 回补（/wl since）
# ============================================================
async def since_checkpoint(cli, chat_token, msgid_text):
    """/wl since 的实现：校验聊天在白名单 → 写 wl 游标（返回 (是否成功, 提示)）。

    游标语义与扫描一致：扫描处理该 id **之后**的消息（min_id 不含本身）。
    写成比当前更大的 id 也合法——自然扫不到东西而已。
    """
    chat_id = whitelist.resolve_wl_del_key(str(chat_token or "").strip(),
                                           state.WHITELIST_CHATS)
    if chat_id is None:
        try:
            cid, _title = await whitelist.resolve_wl_target(
                cli, str(chat_token or "").strip())
        except Exception:
            cid = None
        if cid is not None and cid in state.WHITELIST_CHATS:
            chat_id = cid
    if chat_id is None:
        return False, (f"❌ /wl since：{chat_token} 不在下载白名单"
                       "（先 /wl add，或用列表序号）")
    try:
        msg_id = int(str(msgid_text).strip())
    except (TypeError, ValueError):
        msg_id = 0
    if msg_id <= 0:
        return False, "❌ /wl since：消息 id 须为正整数"
    runtime_db.set_listener_checkpoint(chat_id, msg_id, chain="wl")
    logger.info(f"📋 白名单回补游标：{chat_id} → {msg_id}（扫描其后的消息）")
    return True, (f"✅ 已把「{state.WHITELIST_CHATS[chat_id]}」的扫描起点设为 "
                  f"#{msg_id}，正在回补其后消息（受 Worker 节流控制，逐步转发）")


# ============================================================
# 视图 / 命令文案
# ============================================================
def collect_scan_info():
    """{chat_id: (checkpoint|None, 待执行任务数)}（/wl 视图用）。"""
    info = {}
    if not state.RUNTIME_DB_READY:
        return info
    for cid in state.WHITELIST_CHATS or {}:
        try:
            ckpt = runtime_db.get_listener_checkpoint(cid, chain="wl")
            pending = runtime_db.count_listener_tasks_for_chat(cid, origin="wl")
        except runtime_db.DbUnavailable:
            continue
        info[cid] = (ckpt, pending)
    return info


def summary_text(totals) -> str:
    """/wl scan 的结果正文（命令与菜单共用）。"""
    if totals.get("skipped"):
        return "📋 白名单扫描\n\n⏳ 已有扫描在进行中，请稍候。"
    if totals.get("db_unavailable"):
        return "📋 白名单扫描\n\n❌ Runtime DB 不可用，无法落盘任务。"
    if totals.get("empty_chats"):
        return "📋 白名单扫描\n\n白名单为空。"
    lines = [
        "📋 白名单扫描完成",
        "",
        f"聊天：{totals['chats']} 个（失败 {totals['failed_chats']}）",
        f"检查消息：{totals['scanned']} 条",
        f"新建转发任务：{totals.get('created', 0)} 条",
    ]
    if totals.get("duplicate"):
        lines.append(f"重复跳过：{totals['duplicate']} 条")
    if totals.get("capped"):
        lines.append("⚠️ 有聊天触到任务队列上限（下轮继续）")
    lines.append("")
    lines.append("转发由常驻 Worker 受控执行（标签监听任务优先），稍后完成。")
    return "\n".join(lines)
