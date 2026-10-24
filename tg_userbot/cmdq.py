"""指令回放（/cmdq）——离线期间的指令不丢：水位线回放 + 台账恰好执行一次。

owner 场景（2026-10-24）：userbot 不在线时发的指令会丢。真相：消息躺在
聊天历史里没丢，丢的是消费——实时更新通道在长离线后有缺口，指令层没有
回放兜底（媒体层早有启动对账）。

机制：
    入口聊天（bot 对话 + 收藏夹 + config.CMDQ_EXTRA_INTAKES 可选频道）
      实时路径：commands.handle_command 照常处理，入账 + 推水位线
      回放路径：启动后/周期扫描拉水位线之后的历史
        → 是指令 → cmd_ledger 入账（UNIQUE(chat_id,msg_id)：恰好一次）
        → 过期保护：received 距今 > CMDQ_STALE_DAYS → STALE 跳过（⏭）
        → 执行（shim event 走同一 handle_command）→ ✅/❌
    水位线对**每条扫到的消息**推进（指令与否），成功失败都推进——不卡队列。

标记（best-effort，失败不影响执行）：对原消息打表情反应
    ⏳ 已入队 → ✅ 成功 / ❌ 失败 / ⏭ 过期跳过
查询：/cmdq [成功|失败|待执行|全部]；报告：ops_report collector。
"""
import asyncio
import time

from . import config
from . import runtime_db
from . import state
from .log import logger

TEXT_PREFIX = "📥"
_REPLAY_SCAN_MESSAGES = 100     # 每次回放最多回看的消息数
_REPLAY_LOOP_SECONDS = 300.0    # 周期回放（兜住重连缺口）
_OK_MARK = "✅"
_FAIL_MARK = "❌"
_QUEUED_MARK = "⏳"
_STALE_MARK = "🕳"


# ============================================================
# shim event：回放执行时替身（commands 只用 reply/message/client）
# ============================================================
class _ReplayMessage:
    def __init__(self, msg_id, text, reply_to_msg_id=None):
        self.id = int(msg_id)
        self.message = text
        self.reply_to_msg_id = reply_to_msg_id


class ReplayEvent:
    """handle_command 的最小替身：回复统一发 bot 对话（owner 交互面），
    原消息 id/文本保留供 /cmdhis 类路径使用。"""

    def __init__(self, chat_id, msg_id, text, reply_to_msg_id=None):
        try:
            self.chat_id = int(chat_id)
        except (TypeError, ValueError):
            self.chat_id = str(chat_id)
        self.message = _ReplayMessage(msg_id, text, reply_to_msg_id)
        self.out = False
        self.client = None    # 惰性：client 属性极少被用（/cmdhis 删除路径）

    async def reply(self, payload, **kwargs):
        cli = state.bot_client
        if cli is None:
            return None
        kwargs.pop("buttons", None)      # 回放不带交互按钮（无人点）
        kwargs.setdefault("link_preview", False)
        return await cli.send_message(
            state.MY_ID, str(payload)[:4000], **kwargs)


# ============================================================
# 判定：文本是不是指令（以已注册指令名打头）
# ============================================================
def is_command_text(text):
    q = str(text or "").strip()
    if not q.startswith("/"):
        return False
    head = q.split()[0].lstrip("/").split("@")[0].lower()
    if not head:
        return False
    names = getattr(config, "REGISTERED_COMMAND_NAMES", ()) or ()
    return head in names or head in {"wnacg", "115x", "115dup", "115inv"}


# ============================================================
# 反应标记（best-effort）
# ============================================================
async def _mark(chat_id, msg_id, emoji):
    try:
        from telethon.tl.functions.messages import SendReactionRequest
        from telethon.tl.types import ReactionEmoji
        def _is_bot(cid):
            try:
                return int(cid) == int(state.MY_ID or 0)
            except (TypeError, ValueError):
                return False
        cli = state.bot_client if _is_bot(chat_id) else state.client
        if cli is None:
            return
        await cli(SendReactionRequest(
            peer=int(chat_id), msg_id=int(msg_id),
            reaction=[ReactionEmoji(emoticon=emoji)], big=False))
    except Exception as e:
        logger.debug(f"📥 标记反应失败（忽略）：{chat_id}/{msg_id} {emoji} {e}")


# ============================================================
# 执行一条已入账指令
# ============================================================
async def execute_ledger_row(row, stale_days=None):
    """执行台账里一条 PENDING 指令；终态落账 + 标记。返回终态。"""
    stale_days = stale_days or int(getattr(config, "CMDQ_STALE_DAYS", 7))
    chat_id, msg_id, cmd_text = row["chat_id"], row["msg_id"], row["cmd_text"]
    if time.time() - float(row["received_at"] or 0) > stale_days * 86400:
        runtime_db.finish_cmd(chat_id, msg_id, runtime_db.CMD_STALE,
                              error=f"离线超过 {stale_days} 天，跳过未执行")
        await _mark(chat_id, msg_id, _STALE_MARK)
        logger.info(f"📥 回放跳过（过期）：{cmd_text[:40]}")
        return runtime_db.CMD_STALE
    from . import commands
    ev = ReplayEvent(chat_id, msg_id, cmd_text)
    try:
        runtime_db.finish_cmd(chat_id, msg_id, runtime_db.CMD_RUNNING)
        handled = await commands.handle_command(ev, cmd_text)
        status = runtime_db.CMD_SUCCESS if handled \
            else runtime_db.CMD_FAILED
        error = None if handled else "指令未识别（无 handler 处理）"
    except asyncio.CancelledError:
        runtime_db.finish_cmd(chat_id, msg_id, runtime_db.CMD_PENDING)
        raise
    except Exception as e:
        status, error = runtime_db.CMD_FAILED, f"{type(e).__name__}: {e}"
        logger.exception(f"📥 回放执行异常：{cmd_text[:40]}")
    runtime_db.finish_cmd(chat_id, msg_id, status, error=error)
    await _mark(chat_id, msg_id,
                _OK_MARK if status == runtime_db.CMD_SUCCESS else _FAIL_MARK)
    logger.info(f"📥 回放执行{'成功' if status == runtime_db.CMD_SUCCESS else '失败'}："
                f"{cmd_text[:60]}" + (f"（{error}）" if error else ""))
    return status


# ============================================================
# 回放扫描：水位线之后的历史 → 入账 → 逐条执行
# ============================================================
async def replay_chat(chat_id, execute=True):
    """扫一个入口聊天：水位线后的消息逐条推进；指令入账并（可选）执行。
    返回 {scanned, found, executed, failed, stale}。"""
    def _is_bot_chat(cid):
        try:
            return int(cid) == int(state.MY_ID or 0)
        except (TypeError, ValueError):
            return False
    cli = state.bot_client if _is_bot_chat(chat_id) else state.client
    if cli is None:
        return {"scanned": 0, "found": 0, "executed": 0, "failed": 0,
                "stale": 0}
    cursor = runtime_db.get_cmd_cursor(chat_id) or 0
    try:
        peer = int(chat_id)
    except (TypeError, ValueError):
        peer = chat_id          # "me" 原样
    got = await cli.get_messages(peer,
                                 limit=_REPLAY_SCAN_MESSAGES,
                                 min_id=cursor, reverse=True)
    msgs = [m for m in (got or []) if m is not None and m.id > cursor]
    res = {"scanned": len(msgs), "found": 0, "executed": 0,
           "failed": 0, "stale": 0}
    if not msgs:
        return res
    pending_rows = []
    for m in msgs:
        text = (m.message or "").strip()
        if is_command_text(text):
            res["found"] += 1
            if runtime_db.record_cmd(chat_id, m.id, text):
                pending_rows.append((chat_id, m.id))
                await _mark(chat_id, m.id, _QUEUED_MARK)
        # 水位线对每条消息推进（指令与否；成功失败无关——不卡队列）
        runtime_db.advance_cmd_cursor(chat_id, m.id)
    if not execute:
        return res
    for chat_id_, msg_id_ in pending_rows:
        row = runtime_db.get_cmd_status(chat_id_, msg_id_)
        if row and row["status"] == runtime_db.CMD_PENDING:
            st = await execute_ledger_row(row)
            if st == runtime_db.CMD_SUCCESS:
                res["executed"] += 1
            elif st == runtime_db.CMD_FAILED:
                res["failed"] += 1
            elif st == runtime_db.CMD_STALE:
                res["stale"] += 1
    return res


async def replay_all(execute=True):
    """全部入口聊天回放。返回聚合结果与失败明细。"""
    chats = []
    if state.MY_ID:
        chats.append(int(state.MY_ID))          # bot 对话
    chats.append("me")                          # 收藏夹
    for spec in getattr(config, "CMDQ_EXTRA_INTAKES", ()) or ():
        chats.append(spec)                      # 频道等（resolve 由调用方做）
    total = {"scanned": 0, "found": 0, "executed": 0, "failed": 0, "stale": 0}
    fails = []
    for chat in chats:
        try:
            r = await replay_chat(chat, execute=execute)
        except Exception as e:
            logger.warning(f"📥 回放扫描失败（{chat}）：{e}")
            continue
        for k in total:
            total[k] += r.get(k, 0)
        if r["failed"]:
            for row in runtime_db.list_cmd_ledger(
                    status=runtime_db.CMD_FAILED, limit=10):
                fails.append(row)
    return total, fails


async def replay_loop():
    """周期回放循环：兜住「重连后更新缺口」漏掉的指令。"""
    logger.info("📥 指令回放循环已启动"
                f"（每 {_REPLAY_LOOP_SECONDS:.0f}s 扫入口水位线）")
    try:
        while True:
            await asyncio.sleep(_REPLAY_LOOP_SECONDS)
            try:
                total, _f = await replay_all()
                if total["found"]:
                    logger.info(f"📥 周期回放：{total}")
                    if total["failed"]:
                        await _notify_failed(_f, total["failed"])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"📥 周期回放异常（下轮再试）：{e}")
    except asyncio.CancelledError:
        logger.info("📥 指令回放循环停止")
        raise


async def _notify_failed(fails, n):
    from . import notify
    lines = [f"📥 回放完成：{n} 条指令失败", ""]
    seen = set()
    for r in fails:
        key = (r["chat_id"], r["msg_id"])
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"· {r['cmd_text'][:40]}｜{str(r['error'] or '')[:40]}")
        if len(seen) >= 8:
            break
    try:
        await notify.notify_user("\n".join(lines)[:3900])
    except Exception:
        pass


# ============================================================
# 实时路径挂钩：handle_command 执行时入账（在线指令也进台账）
# ============================================================
async def record_live(event, cmd_text):
    """在线执行的指令也入账（幂等）：在线处理过的，回放不会重执行。"""
    try:
        msg = getattr(event, "message", None)
        chat_id = getattr(event, "chat_id", None)
        if msg is None or chat_id is None:
            return
        if runtime_db.record_cmd(chat_id, msg.id, cmd_text):
            runtime_db.finish_cmd(chat_id, msg.id, runtime_db.CMD_RUNNING)
            # 终态由 commands 层返回后由 wrapper 落；此处先占位 RUNNING，
            # 崩溃遗留的 RUNNING 由 recover 归位为 PENDING 重执行（恰好一次
            # 由 UNIQUE 保证不会双份）
    except Exception as e:
        logger.debug(f"📥 在线入账失败（忽略）：{e}")


def recover_stale_running():
    """启动自愈：遗留 RUNNING（上次进程中途死掉）→ 归位重执行。"""
    rows = runtime_db.list_cmd_ledger(status=runtime_db.CMD_RUNNING, limit=50)
    for r in rows:
        runtime_db.finish_cmd(r["chat_id"], r["msg_id"],
                              runtime_db.CMD_PENDING)
    return len(rows)


# ============================================================
# /cmdq 命令
# ============================================================
_STATUS_ZH = {"SUCCESS": "✅ 成功", "FAILED": "❌ 失败", "PENDING": "⏳ 待执行",
              "RUNNING": "🔄 执行中", "STALE": "🕳 过期跳过"}


def status_reply(arg):
    """/cmdq [成功|失败|待执行|全部]"""
    q = str(arg or "").strip().lower()
    by_zh = {"成功": "SUCCESS", "失败": "FAILED", "待执行": "PENDING",
             "全部": None}
    status = by_zh.get(q, by_zh.get(arg.strip(), None))
    if q and status is None and q not in ("", "全部", "成功", "失败", "待执行"):
        return (f"{TEXT_PREFIX} 用法：/cmdq [成功|失败|待执行|全部]\n"
                "（默认看最近全部）")
    try:
        counts = runtime_db.cmd_ledger_counts()
    except runtime_db.DbUnavailable as e:
        return f"{TEXT_PREFIX}\n❌ Runtime DB 不可用：{e}"
    head = " ".join(f"{_STATUS_ZH.get(k, k)}{n}" for k, n in sorted(counts.items()))
    rows = runtime_db.list_cmd_ledger(status=status, limit=20)
    lines = [f"{TEXT_PREFIX} 指令台账（{head or '空'}，最近 20 条）", ""]
    import time as _t
    for r in rows:
        ts = _t.strftime("%m-%d %H:%M", _t.localtime(r["executed_at"] or
                                                     r["received_at"]))
        err = f"｜{str(r['error'])[:26]}" if r.get("error") else ""
        lines.append(f"{ts} {_STATUS_ZH.get(r['status'], r['status'])}"
                     f"｜{r['cmd_text'][:40]}{err}")
    return "\n".join(lines)[:3900] or f"{TEXT_PREFIX} 台账为空"
