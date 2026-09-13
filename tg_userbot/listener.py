"""标签监听 —— Scanner 侧：扫出匹配、落成持久化任务（**不执行**）。

**扫描 ≠ 执行**（Producer/Consumer）。本模块只做生产者：

    Telegram ──► Scanner（本模块）──► SQLite 任务表 ──► listener_worker ──► 转发/下载

这么拆的理由是原先「扫描即执行」有三个真实缺陷：① 一轮扫描命中几十条时，
进程在中途被杀会把这一轮状态**全部丢掉**（checkpoint 与待续做都没落盘），重启
后整批重跑、已转发过的重发一遍；② 转发之间零间隔，没有节流手段；③ 失败续做
用的 ``pending`` 是「半个任务表」，没有状态/租约/次数上限。

**与下载白名单完全独立**：监听来源来自自己的 ``runtime/listen.json`` 与
``state.LISTEN_RULES``，**绝不**从 ``state.WHITELIST_CHATS`` 推导，也不要求监听
聊天加入 ``/wl``。一个聊天可以只在一边、两边都在、或都不在。唯一一次读白名单是
§14 的重叠判定（且只读）：源聊天已在下载白名单时，实时链路已经转发+下载过这条
消息，监听就不再为它建「收藏夹」任务（``_effective_work``），只建其余目标。

**Scanner 的职责边界**（§11）：读配置 → 发现 source chat → 按 checkpoint 扫新消息
→ 标签匹配 → 多标签合并 → 相册整组 → 算目标与 download → **建任务** → 在同一
事务里推进 checkpoint。**Scanner 不转发、不下载、不等待 FloodWait、不重试**
——那些全是 Worker 的事。

**扫描纪律**：
* 按 chat 扫描一次，再匹配该 chat 下的全部规则（不按规则重复请求 Telegram）；
* 同一消息命中多标签/多规则 → 先「匹配 → 合并」（目标是**集合**），再按
  (单元, 目标) 各建一条任务，同一目标只建一次（§14）；
* 相册按 grouped_id 整组为一个单元，``message_id`` 存**组内最小成员 id**（锚点），
  整组成员在 payload 里——这样唯一索引与「整组转发」同时成立（见 runtime_db）；
* 只处理**媒体**消息——纯文本转发进收藏夹会被 ``app._record_me_label`` 当成
  「待关联评论标注」拼进下一个下载文件的文件名，污染命名；
* 首次添加监听时以当前最新消息 id 作 checkpoint，**绝不扫历史**（§17/§18：新增
  标签也沿用同一聊天的 checkpoint，不会回头扫历史）；
* checkpoint 与任务**同事务**提交，语义是「此位置之前需要建的任务都已落盘」
  （§7/§13）；队列触顶时**绝不推进 checkpoint**（§30，否则那些消息被永久跳过）。

**配置热加载**：每轮扫描前按 mtime 按需重读 listen.json（§17）；文件损坏时保持
当前生效配置——半截文件把规则清空后，用户下一次点任何按钮就会把空规则保存回
文件，规则就真没了。

**稳定性**：每 chat 独立 try/except（一个聊天失败不影响其它，§23）；所有网络调用
经 ``netio.shielded`` 收口（断线的网络层取消只会变成「本轮没做成」，绝不会把这个
后台循环打死——本项目最贵的坑，见 CLAUDE.md）；数据库不可用时整轮放弃且
checkpoint 原地不动，下一轮重扫。
"""
import asyncio
import json
import os
import re
import time
from datetime import datetime

from telethon import Button
from telethon.errors import MsgIdInvalidError
from telethon.tl.functions.messages import GetRepliesRequest
from telethon.utils import get_peer_id

from . import config
from . import netio
from . import notify
from . import runtime_db
from . import state
from . import stats
from .config import (
    LISTEN_ALBUM_SIBLING_RANGE,
    LISTEN_CONFIG_FILE,
    LISTEN_FETCH_TIMEOUT_SECONDS,
    LISTEN_MAX_MESSAGES_PER_SCAN,
    LISTEN_MATCH_PREFIX,
    LISTEN_MAX_INTERVAL_MINUTES,
    LISTEN_MIN_INTERVAL_MINUTES,
    LISTEN_NOTIFY_PREFIX,
)
from .log import logger
from .naming import parse_date, pick_group_caption_text
from .sources import (
    entity_display_name,
    is_channel_mirror,
    is_downloadable,
    resolve_origin_snapshot,
)

# 扫描重入保护：定时扫描与「▶️ 立即扫描」不能同时跑（同一份 checkpoint 会被
# 两个执行流各自推进）。检查与置位之间没有 await，单线程事件循环内不可能被
# 抢占，故用普通布尔即可——不必新增 loop 绑定原语（事件循环规则）。
_SCANNING = False

# 「添加/修改监听」向导的草稿（瞬态，不持久化；保存或取消即清）。
_DRAFT = None

# listen.json 最近一次「已知内容」的 mtime：reload_listen_config 靠它判断
# 文件是否被外部改过（每轮扫描前按需重读，§17 改配置无需重启）。
_LAST_CONFIG_MTIME = 0.0

# 工作项（pending 里存的「还没做成的部分」）
WORK_SAVED = "saved_messages"      # 转发收藏夹（download=true 时同时入队副本）


# ============================================================
# 纯函数：标签匹配（规格书 §10）
# ============================================================
def tag_matches(text: str, tag: str) -> bool:
    """消息文本里是否存在这个完整标签（有边界、大小写不敏感）。

    不用朴素的 ``tag in text``：那样 ``#01musume2`` / ``x#01musume`` /
    ``##01musume`` 都会误命中。两侧边界刻意不对称，各有各的理由：

    * **左** ``(?<![A-Za-z0-9_#])`` —— 挡的是「英文词粘标签」（``x#tag``）
      与 ``##tag``；用 ASCII 判据，于是中文后不加空格的常见写法
      （``这是#01musume``）照样命中。
    * **右** ``(?!\\w)`` —— 挡的是「后面还有字，其实是另一个标签」。
      用 Unicode 判据（``\\w`` 含中文），因为 Telegram 的标签本身允许中文，
      ``#01musume的`` 是另一个标签而不是 ``#01musume`` 后面跟了「的」。

    空文本/空标签一律 False。纯函数、无 I/O。
    """
    text = str(text or "")
    tag = str(tag or "").strip()
    if not text.strip() or not tag:
        return False
    return re.search(
        r"(?<![A-Za-z0-9_#])" + re.escape(tag) + r"(?!\w)",
        text,
        re.IGNORECASE,
    ) is not None


def matched_tags(text: str, tags) -> set:
    """文本命中的标签集合（供「多标签命中合并」用）。"""
    return {t for t in (tags or []) if tag_matches(text, t)}


# ============================================================
# 纯函数：目标身份（规格书 §22）
# ============================================================
def normalize_target(raw):
    """把配置/输入里的目标归一化成持久化形态；无法识别返回 None。

    持久化身份只能是 ``{"type": "saved_messages"}`` 或
    ``{"type": "chat", "chat_id": <int>, "name": …}``——username 与名称都会变，
    只有 chat_id 稳定（§22.4/§22.8）。名称类输入（``@xxx``）由
    ``resolve_chat`` 联网解析成 chat_id 之后再来这里。
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if text.lower() in ("me", "saved_messages", "收藏夹", "收藏"):
            return {"type": "saved_messages"}
        try:
            raw = {"type": "chat", "chat_id": int(text)}
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type") or "").strip().lower()
    if kind in ("saved_messages", "me", "saved"):
        return {"type": "saved_messages"}
    if kind == "chat":
        try:
            chat_id = int(raw.get("chat_id"))
        except (TypeError, ValueError):
            return None
        out = {"type": "chat", "chat_id": chat_id}
        if raw.get("name"):
            out["name"] = str(raw["name"])
        return out
    return None


def target_key(target):
    """目标的唯一身份 ``("saved_messages", None)`` / ``("chat", chat_id)``。

    去重与「同一 (消息, 目标) 只转发一次」全靠它——绝不拿 username 或名称
    作字符串比较（§22.8），否则改名/换 username 就会变出重复目标。
    """
    norm = normalize_target(target)
    if norm is None:
        return None
    if norm["type"] == "saved_messages":
        return ("saved_messages", None)
    return ("chat", int(norm["chat_id"]))


def target_label(target) -> str:
    """目标的展示名（只用于菜单/日志，不参与身份判断）。"""
    norm = normalize_target(target)
    if norm is None:
        return "(无效目标)"
    if norm["type"] == "saved_messages":
        return "📌 收藏夹"
    name = norm.get("name") or f"chat_{norm['chat_id']}"
    return f"📢 {name}"


# ============================================================
# 纯函数：相册分桶 / 规则校验
# ============================================================
def group_by_album(messages):
    """把消息按相册分组：同 grouped_id 的合成一组，其余各自成组。

    频道带标签的相册帖子，标签通常只挂在其中一个成员的说明上；整组一起
    转发/下载才能在收藏夹里还原成一个相册（而不是 N 条散消息）。组内按 id
    升序，方便把「组的最小 id」当作稳定单元键。
    """
    groups = {}
    order = []
    for m in messages or []:
        if m is None:
            continue
        gid = getattr(m, "grouped_id", None)
        key = ("gid", gid) if gid else ("one", getattr(m, "id", id(m)))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(m)
    return [sorted(groups[k], key=lambda m: m.id) for k in order]


def validate_interval(value):
    """扫描周期（分钟）校验：1 ~ 10080。返回 (是否合法, 提示文本)。"""
    try:
        minutes = int(str(value).strip())
    except (TypeError, ValueError):
        return False, "❌ 扫描周期须为分钟数（1-10080），如 30、1440"
    if not LISTEN_MIN_INTERVAL_MINUTES <= minutes <= LISTEN_MAX_INTERVAL_MINUTES:
        return False, (
            f"❌ 扫描周期须在 {LISTEN_MIN_INTERVAL_MINUTES}-"
            f"{LISTEN_MAX_INTERVAL_MINUTES} 分钟之间"
        )
    return True, ""


def validate_rule(rule):
    """规则校验（保存前唯一入口）。返回 (是否合法, 提示文本)。"""
    if not isinstance(rule, dict):
        return False, "❌ 规则格式错误"
    try:
        int(rule.get("source_chat_id"))
    except (TypeError, ValueError):
        return False, "❌ 缺少来源聊天（source_chat_id）"
    tag = str(rule.get("tag") or "").strip()
    if not tag.startswith("#") or len(tag) < 2:
        return False, "❌ 标签须以 # 开头，如 #01musume"
    targets = rule.get("targets")
    if targets is None:
        return False, "❌ 缺少转发目标（targets）"
    if not isinstance(targets, (list, tuple)):
        return False, "❌ 转发目标格式错误"
    for t in targets:
        if target_key(t) is None:
            return False, f"❌ 无法识别的目标：{t}"
    return True, ""


def normalize_rule(rule) -> dict:
    """规则归一化：目标是持久化形态、tag 去空白、download 转 bool。"""
    targets = []
    seen = set()
    for t in rule.get("targets") or []:
        norm = normalize_target(t)
        if norm is None:
            continue
        key = target_key(norm)
        if key in seen:
            continue
        seen.add(key)
        targets.append(norm)
    out = {
        "source_chat_id": int(rule["source_chat_id"]),
        "tag": str(rule["tag"]).strip(),
        "targets": targets,
        "download": bool(rule.get("download")),
    }
    for field in ("source_name", "source_username"):
        if rule.get(field):
            out[field] = str(rule[field])
    return out


def _effective_work(rule, in_download_whitelist):
    """规则 → (目标键集合, 是否入队下载)。

    ``download=true`` 的实现就是「转发到收藏夹 + 入队那份副本」，因此它**隐含**
    收藏夹目标（§12/§13）：规则里没写 me 也照样转发，否则无从下载。

    ``in_download_whitelist``（§14）：源聊天同时在下载白名单时，实时链路收到
    该媒体就已经转发进收藏夹并下载过，这里把 me 与 download 一并让位，只跑
    其余目标，避免重复转发与重复下载。
    """
    keys = set()
    for t in rule.get("targets") or []:
        key = target_key(t)
        if key is not None:
            keys.add(key)

    download = bool(rule.get("download"))
    if download:
        keys.add(("saved_messages", None))

    if in_download_whitelist:
        keys.discard(("saved_messages", None))
        download = False
    return keys, download


# ============================================================
# 配置与状态持久化
#
# 与 caption_filter 同构：路径与默认值都在**调用时**读 config 模块属性
# （方便单测 monkeypatch，不能 from-import 成别名）；写盘一律 temp +
# os.replace 原子；读失败/JSON 损坏回落空配置，绝不影响主进程。
# ============================================================
def _config_path():
    return getattr(config, "LISTEN_CONFIG_FILE", LISTEN_CONFIG_FILE)


def save_listen_config() -> bool:
    """把 state 里的监听配置原子写盘；失败仅告警（绝不影响运行）。"""
    global _LAST_CONFIG_MTIME
    path = _config_path()
    payload = {
        "enabled": bool(state.LISTEN_ENABLED),
        "interval_minutes": int(state.LISTEN_INTERVAL_MINUTES),
        "listeners": [normalize_rule(r) for r in state.LISTEN_RULES],
    }
    tmp_path = path + ".tmp"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
        try:
            _LAST_CONFIG_MTIME = os.path.getmtime(path)
        except OSError:
            _LAST_CONFIG_MTIME = 0.0
        return True
    except Exception as e:
        logger.warning(f"保存标签监听配置失败（不影响运行）：{e}")
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return False


def _apply_config(data) -> int:
    """把已解析的配置 dict 写进 state，返回生效的规则条数。

    坏规则**逐条丢弃**并告警：一条坏规则只影响自己，不牵连其它规则。
    周期非法回落默认值。
    """
    state.LISTEN_ENABLED = bool(data.get("enabled", True))
    ok, _ = validate_interval(
        data.get("interval_minutes", config.LISTEN_DEFAULT_INTERVAL_MINUTES))
    if ok:
        state.LISTEN_INTERVAL_MINUTES = int(data["interval_minutes"])
    else:
        logger.warning(
            f"标签监听周期非法（{data.get('interval_minutes')}），"
            f"回落默认 {config.LISTEN_DEFAULT_INTERVAL_MINUTES} 分钟"
        )
        state.LISTEN_INTERVAL_MINUTES = config.LISTEN_DEFAULT_INTERVAL_MINUTES

    valid = []
    for raw in data.get("listeners") or []:
        ok, msg = validate_rule(raw)
        if not ok:
            logger.warning(f"丢弃非法监听规则（{msg}）：{raw}")
            continue
        valid.append(normalize_rule(raw))
    state.LISTEN_RULES = valid
    return len(valid)


def load_listen_config() -> int:
    """启动时载入 listen.json，返回生效的规则条数。

    文件缺失 → 空规则 + 默认开关；JSON 损坏 → 记日志后用空配置（绝不抛）。
    """
    global _LAST_CONFIG_MTIME
    state.LISTEN_ENABLED = True
    state.LISTEN_INTERVAL_MINUTES = config.LISTEN_DEFAULT_INTERVAL_MINUTES
    state.LISTEN_RULES = []
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("顶层不是对象")
    except FileNotFoundError:
        return 0
    except Exception as e:
        logger.warning(f"读取标签监听配置失败，改用空配置：{e}")
        return 0
    # 记下「已知内容」的 mtime：下一轮扫描的 reload 只在文件真被外部改过时才读
    try:
        _LAST_CONFIG_MTIME = os.path.getmtime(_config_path())
    except OSError:
        _LAST_CONFIG_MTIME = 0.0
    return _apply_config(data)


def reload_listen_config() -> bool:
    """扫描前按需重读 listen.json（§17：改配置无需重启）。

    只在文件 mtime 变了才读，且**解析成功才替换内存态**——一只半截/损坏的
    文件绝不能把正在生效的规则清空：清空后用户下一次点任何按钮都会把空规则
    保存回文件，规则就真的没了。失败保持当前配置并记下 mtime（避免每轮扫描
    反复读同一个坏文件）。返回是否真的重新载入了配置。
    """
    global _LAST_CONFIG_MTIME
    path = _config_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False
    if mtime == _LAST_CONFIG_MTIME:
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("顶层不是对象")
    except Exception as e:
        logger.warning(f"重读标签监听配置失败（保持当前生效配置）：{e}")
        _LAST_CONFIG_MTIME = mtime
        return False
    _LAST_CONFIG_MTIME = mtime
    count = _apply_config(data)
    logger.info(f"📡 标签监听配置已重新载入：{count} 条规则")
    return True


# ============================================================
# 扫描游标：SQLite（原 listen_state.json 已退役）
#
# checkpoint 语义（§7）：**此位置之前需要建的任务都已落盘**。它不表示任务已经
# 发送成功——那是 Worker 的事。两者在同一事务里提交，所以这个语义成立。
# ============================================================
def get_checkpoint(source_chat_id):
    """取监听来源的 checkpoint（None = 从未建立，与 0 语义不同）。"""
    return runtime_db.get_listener_checkpoint(int(source_chat_id))


def _legacy_state_path():
    return getattr(config, "LISTEN_STATE_FILE", config.LISTEN_STATE_FILE)


def migrate_legacy_state(path=None) -> dict:
    """把旧的 listen_state.json 一次性迁进 SQLite（§38）。返回迁移计数。

    **两条守卫**，避免这个函数在任何情况下帮倒忙：
    1. 只有当 **DB 里还没有该聊天的 checkpoint** 时才写入旧 checkpoint——
       否则「升级后跑了一阵再触发迁移」会把已经推进的游标倒回去，导致重复
       扫描一大段。
    2. 迁移成功就把旧文件改名 ``.migrated``（不删，保留人工核对），下次不再跑。

    ``pending``（失败待续做的工作项）**逐项映射成 PENDING 任务**而不是丢弃：
    它本来就是「未完成的工作」，丢掉等于这些消息永不转发，违反「不降低现有
    功能能力」。一个 pending 单元（可能含相册整组）按其 work 列表拆成 N 条
    任务，member_ids 存进 payload。
    """
    src = path or _legacy_state_path()
    if not os.path.exists(src):
        return {"chats": 0, "tasks": 0, "skipped": 0, "moved": False}
    try:
        with open(src, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("顶层不是对象")
    except Exception as e:
        logger.warning(f"🗄 旧 listen_state.json 无法解析，保持原名不动：{e}")
        return {"chats": 0, "tasks": 0, "skipped": 0, "moved": False}

    chats = tasks = skipped = 0
    for chat_key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        try:
            chat_id = int(chat_key)
        except (TypeError, ValueError):
            logger.warning(f"🗄 迁移跳过非法聊天键：{chat_key!r}")
            continue

        try:
            legacy_ckpt = int(entry.get("last_message_id") or 0)
        except (TypeError, ValueError):
            legacy_ckpt = 0
        current = get_checkpoint(chat_id)
        if current is None:
            runtime_db.set_listener_checkpoint(chat_id, legacy_ckpt)
            chats += 1
            logger.info(f"🗄 迁移 checkpoint：{chat_id} → {legacy_ckpt}")
        else:
            skipped += 1
            logger.info(
                f"🗄 迁移跳过 checkpoint {chat_id}：DB 已有 {current}，"
                f"不能用旧值 {legacy_ckpt} 覆盖"
            )

        records = []
        for unit_key, rec in (entry.get("pending") or {}).items():
            if not isinstance(rec, dict):
                continue
            ids = [int(i) for i in (rec.get("ids") or [])
                   if str(i).lstrip("-").isdigit()]
            work = [str(w) for w in (rec.get("work") or [])]
            if not ids or not work:
                continue
            for item in work:
                target_type, target_chat_id = _work_item_to_target(item)
                if target_type is None:
                    continue
                records.append({
                    "message_id": min(ids),
                    "grouped_id": None,
                    "target_type": target_type,
                    "target_chat_id": target_chat_id,
                    "download": bool(rec.get("dl")),
                    "payload": {"member_ids": ids,
                                "caption": str(rec.get("cap") or ""),
                                "migrated_from": str(unit_key)},
                })
        if records:
            ids_out = runtime_db.enqueue_listener_tasks(chat_id, records)
            tasks += sum(1 for i in ids_out if i)
            logger.info(
                f"🗄 迁移待续做任务：{chat_id} {len(records)} 条"
                f"（成功落盘 {sum(1 for i in ids_out if i)} 条）"
            )

    moved = False
    if chats or tasks:
        dst = src + ".migrated"
        try:
            if os.path.exists(dst):
                dst = f"{dst}.{int(time.time())}"
            os.replace(src, dst)
            moved = True
            logger.warning(f"🗄 旧状态文件已迁移并改名为 {os.path.basename(dst)}")
        except OSError as e:
            logger.error(f"🗄 迁移完成但改名失败（下次启动会再跑一次，有守卫）：{e}")
    return {"chats": chats, "tasks": tasks, "skipped": skipped, "moved": moved}


def _work_item_to_target(item):
    """旧 pending 的工作项 → (target_type, target_chat_id)；认不出返回 (None, None)。"""
    item = str(item or "")
    if item == WORK_SAVED or item == "saved_messages":
        return ("saved_messages", None)
    if item.startswith("chat:"):
        try:
            return ("chat", int(item.split(":", 1)[1]))
        except (IndexError, ValueError):
            return (None, None)
    return (None, None)


# ============================================================
# 配置服务函数（命令与菜单共用）
# ============================================================
def set_enabled(enabled) -> str:
    state.LISTEN_ENABLED = bool(enabled)
    save_listen_config()
    return (f"{LISTEN_NOTIFY_PREFIX} 已开启"
            if state.LISTEN_ENABLED else f"{LISTEN_NOTIFY_PREFIX} 已关闭")


def set_interval(minutes) -> tuple:
    ok, msg = validate_interval(minutes)
    if not ok:
        return False, msg
    state.LISTEN_INTERVAL_MINUTES = int(str(minutes).strip())
    save_listen_config()
    return True, (f"{LISTEN_NOTIFY_PREFIX} 扫描周期已设置为 "
                  f"{_format_interval(state.LISTEN_INTERVAL_MINUTES)}")


def del_listener(index) -> tuple:
    """按序号（1 起）删除规则。返回 (是否成功, 提示文本)。"""
    try:
        pos = int(str(index).strip())
    except (TypeError, ValueError):
        return False, (f"{LISTEN_NOTIFY_PREFIX}：❌ 请给出规则序号"
                       f"（1-{len(state.LISTEN_RULES)}）")
    if not 1 <= pos <= len(state.LISTEN_RULES):
        return False, (f"{LISTEN_NOTIFY_PREFIX}：❌ 规则序号不存在，"
                       "用 /listen 查看列表")
    removed = state.LISTEN_RULES.pop(pos - 1)
    save_listen_config()
    return True, (f"{LISTEN_NOTIFY_PREFIX} 已删除规则 {pos}："
                  f"{_rule_oneline(removed)}")


async def add_listener(rule) -> tuple:
    """新增规则：校验 → 首次监听该聊天时初始化 checkpoint → 入库。

    **首次启用绝不扫历史**（§8）：解析出 chat_id 后立刻取该聊天当前最新
    消息 id 作 checkpoint，从下一条新消息开始监听。取不到最新 id（无权限/
    网络断）时**拒绝添加**——宁可不加，也不能留一条没有 checkpoint 的规则
    （下次扫描会把整个历史当成新消息灌下来）。
    """
    ok, msg = validate_rule(rule)
    if not ok:
        return False, msg
    rule = normalize_rule(rule)
    chat_id = rule["source_chat_id"]

    if get_checkpoint(chat_id) is None:
        newest = await _fetch_newest_id(chat_id)
        if newest is None:
            return False, (f"{LISTEN_NOTIFY_PREFIX}：读取该聊天最新消息失败，"
                           "已放弃添加（请确认有访问权限后重试）")
        runtime_db.set_listener_checkpoint(chat_id, newest)
        logger.info(
            f"📡 标签监听初始化 checkpoint：{chat_id} → {newest}"
            "（不扫描历史，从下一条新消息开始）"
        )

    state.LISTEN_RULES.append(rule)
    save_listen_config()
    return True, f"{LISTEN_NOTIFY_PREFIX} 已添加规则：{_rule_oneline(rule)}"


def replace_listener(index, rule) -> tuple:
    """按序号替换规则（「✏️ 修改」保存时用）。chat_id 不变则不重init checkpoint。"""
    try:
        pos = int(str(index).strip())
    except (TypeError, ValueError):
        return False, f"{LISTEN_NOTIFY_PREFIX}：规则序号无效"
    if not 1 <= pos <= len(state.LISTEN_RULES):
        return False, f"{LISTEN_NOTIFY_PREFIX}：规则序号不存在"
    ok, msg = validate_rule(rule)
    if not ok:
        return False, msg
    state.LISTEN_RULES[pos - 1] = normalize_rule(rule)
    save_listen_config()
    return True, f"{LISTEN_NOTIFY_PREFIX} 已更新规则 {pos}"


# ============================================================
# 网络：取消息 / 转发（全部经 netio.shielded 收口）
# ============================================================
async def _fetch_newest_id(chat_id):
    """取聊天当前最新消息 id；失败返回 None（0 = 空聊天）。"""
    cli = state.client
    if cli is None:
        return None
    got = await netio.shielded(
        lambda: cli.get_messages(chat_id, limit=1),
        LISTEN_FETCH_TIMEOUT_SECONDS,
        f"读取最新消息（{chat_id}）",
    )
    if got is None:
        return None
    msgs = got if isinstance(got, (list, tuple)) else [got]
    if not msgs:
        return 0
    return int(msgs[0].id)


async def _fetch_new(chat_id, checkpoint):
    """取 checkpoint 之后的新消息（升序，最多一轮上限）；失败返回 None。

    取完还会补一次「被条数上限切开的相册」（见 `_complete_boundary_group`）。
    """
    cli = state.client
    if cli is None:
        return None
    got = await netio.shielded(
        lambda: cli.get_messages(
            chat_id,
            limit=LISTEN_MAX_MESSAGES_PER_SCAN,
            min_id=int(checkpoint or 0),
            reverse=True,
        ),
        LISTEN_FETCH_TIMEOUT_SECONDS,
        f"扫描监听聊天（{chat_id}）",
    )
    if got is None:
        return None
    msgs = got if isinstance(got, (list, tuple)) else [got]
    msgs = sorted(msgs, key=lambda m: m.id)
    return await _complete_boundary_group(chat_id, msgs)


# 公开别名：白名单扫描生产者（wl_scan）复用同一取消息路径（netio 收口、
# 相册边界补齐都只有这一份实现）。
fetch_newest_id = _fetch_newest_id
fetch_new_messages = _fetch_new


async def _complete_boundary_group(chat_id, msgs):
    """把被一轮条数上限切开的相册补完整。

    ``limit`` 是从「最新」一侧切的：正好骑在边界上的相册，较旧的成员在里面、
    较新的成员被切在外面。不补的话 ``group_by_album`` 只看到半组 → 建出的任务
    只带半个相册 → Worker 转发半个相册，收藏夹里被劈开（这是改造前就存在的
    真 bug，顺手修掉）。只对「最后一条是相册成员」的情况多取一次，代价可忽略。
    """
    if not msgs:
        return msgs
    last = msgs[-1]
    gid = getattr(last, "grouped_id", None)
    if not gid:
        return msgs
    extra = await _fetch_after(chat_id, last.id, LISTEN_ALBUM_SIBLING_RANGE)
    tail = [m for m in extra if getattr(m, "grouped_id", None) == gid]
    if tail:
        logger.info(
            f"📡 补齐被扫描上限切开的相册：{chat_id} 组 {gid} 追加 {len(tail)} 个成员"
            f"（该组共 {len([m for m in msgs if getattr(m, 'grouped_id', None) == gid]) + len(tail)} 个）"
        )
    return msgs + tail


async def _fetch_after(chat_id, after_id, limit):
    """取 id > after_id 的最早 limit 条（补相册边界用）；失败返回空列表。"""
    cli = state.client
    if cli is None:
        return []
    got = await netio.shielded(
        lambda: cli.get_messages(chat_id, limit=int(limit),
                                 min_id=int(after_id), reverse=True),
        LISTEN_FETCH_TIMEOUT_SECONDS,
        f"补齐相册边界（{chat_id}）",
    )
    if got is None:
        return []
    msgs = got if isinstance(got, (list, tuple)) else [got]
    return [m for m in msgs if m is not None]


async def fetch_unit_messages(source_chat_id, member_ids):
    """按 id 取回一个任务单元的整组成员（Worker 执行前调用）。

    Worker 手上只有 ``listener_tasks`` 的行（来源聊天 + 锚点 + payload），
    必须先取回真实消息对象才能转发。**取消息这件事留在 Scanner 模块**（所有
    Telegram 读操作都在这边，Worker 只负责发送/入队），失败返回空列表由调用方
    按重试处理。
    """
    cli = state.client
    ids = [int(i) for i in (member_ids or [])]
    if cli is None or not ids:
        return []
    got = await netio.shielded(
        lambda: cli.get_messages(int(source_chat_id), ids=ids),
        LISTEN_FETCH_TIMEOUT_SECONDS,
        f"取任务消息（{source_chat_id}）",
    )
    if got is None:
        return []
    msgs = got if isinstance(got, (list, tuple)) else [got]
    by_id = {m.id: m for m in msgs if m is not None}
    # 保持成员顺序（相册顺序即源频道里的顺序）
    return [by_id[i] for i in ids if i in by_id]


def _build_tasks(chat_id, media, keys, download, caption, anchor, origin=None):
    """把「一个消息单元 × 一组目标」展开成待落盘的任务记录（纯函数）。

    **每个目标一条任务**（规格 §15）：目标之间彼此独立，A 失败不会牵连 B，
    重试也只重试自己那条。

    ``message_id`` 存**单元锚点**（相册取组内最小成员 id），整组成员 id 放
    payload["member_ids"]——这样 §8 的唯一索引与 §16「整组转发、收藏夹里仍是
    一个相册」同时成立。若按成员 id 各建一条任务，Worker 会逐条转发，收藏夹
    里相册就被劈成 N 条散消息（详见 runtime_db 的索引注释）。

    ``download`` 只挂在收藏夹目标上：下载的实现是「转发进收藏夹 + 入队那份
    副本」，普通聊天目标没有可入队的副本。

    ``origin``：这条消息评论的频道原帖快照（``sources.resolve_origin_snapshot``）。
    快照在**扫描时**定死进 payload，Worker 与下载侧都不会再去查一次——原帖被
    编辑/删除时，retry 出来的文件名必须还是同一个（任务书 §5）。
    """
    member_ids = [m.id for m in media]
    gid = getattr(media[0], "grouped_id", None)
    # caption 是**fallback**（相册同组说明：评论自己没写字才用）；原帖 caption
    # 走 parent_caption 槽，是**强制**的——「👍」这种评论文字没有命名价值。
    # 两个槽分开，才是 naming.effective_caption 那套优先级。
    payload_base = {"member_ids": member_ids, "caption": caption or ""}
    if origin is not None:
        if origin.get("caption"):
            payload_base["parent_caption"] = origin["caption"]
        if origin.get("date") is not None:
            payload_base["parent_date"] = origin["date"].isoformat()
        if origin.get("source_name"):
            payload_base["source_name"] = origin["source_name"]
    out = []
    for key in sorted(keys, key=lambda k: (k[0] != "saved_messages", str(k[1]))):
        is_saved = key[0] == "saved_messages"
        out.append({
            "message_id": int(anchor),
            "grouped_id": gid,
            "target_type": "saved_messages" if is_saved else "chat",
            "target_chat_id": None if is_saved else int(key[1]),
            "download": bool(download) and is_saved,
            "payload": dict(payload_base),
        })
    return out


def build_saved_messages_task(chat_id, media, caption, origin=None):
    """一个消息单元 → 收藏夹下载任务记录（白名单两条生产链共用）。

    ``origin`` 的 source_name 缺失时兜底为**聊天标题**：来源禁转回退直下原
    消息时副本不存在、没有 fwd_from 可解析，目录名只能靠 payload 里这个
    字段；转发路径上它与副本 fwd_from 的解析结果一致，不冲突。
    """
    origin = dict(origin) if origin else {}
    if not origin.get("source_name"):
        origin["source_name"] = (
            (state.WHITELIST_CHATS or {}).get(int(chat_id))
            or f"chat_{int(chat_id)}")
    return _build_tasks(chat_id, media, {("saved_messages", None)}, True,
                        caption, min(m.id for m in media), origin)


async def _scan_chat(chat_id, rules):
    """扫一个聊天：匹配 → 落成持久化任务。**不做任何转发**（规格 §11）。

    返回本轮统计。Scanner 的职责边界止于「任务已落盘 + checkpoint 已推进」，
    真正的 forward 由 listener_worker 受控执行——这样一次扫描发现几十条匹配
    也不会在短时间内砸出几十个转发请求。

    checkpoint 只在**同一个事务**里随任务一起推进（runtime_db 保证），所以
    任何时刻 checkpoint 都诚实地表示「此位置之前需要建的任务都已落盘」。
    """
    result = {"scanned": 0, "matched": 0, "created": 0, "duplicate": 0,
              "capped": False}

    checkpoint = runtime_db.get_listener_checkpoint(chat_id)
    if checkpoint is None:
        # 规则存在但 DB 里没有游标（历史遗留/迁移缺失）：就地初始化，
        # **绝不扫历史**——把当前最新 id 当起点，从下一条新消息开始。
        newest = await _fetch_newest_id(chat_id)
        if newest is None:
            raise RuntimeError(f"无 checkpoint 且读不到最新消息：{chat_id}")
        runtime_db.set_listener_checkpoint(chat_id, newest)
        logger.warning(
            f"📡 {chat_id} 缺少 checkpoint（历史遗留规则），已就地初始化为 "
            f"{newest}（从新消息开始，不扫历史）"
        )
        checkpoint = newest

    new_msgs = await _fetch_new(chat_id, checkpoint)
    if new_msgs is None:
        raise RuntimeError(f"读取新消息失败：{chat_id}")
    result["scanned"] = len(new_msgs)
    if not new_msgs:
        return result

    # 队列上限背压（§30）：满了就不再建新任务，且 checkpoint **停在原地**
    #（没入队的消息绝不能被跳过——否则那几条消息永久丢失）。
    pending_now = runtime_db.count_pending_listener_tasks()
    budget = int(config.LISTEN_MAX_PENDING_TASKS) - pending_now
    if budget <= 0:
        result["capped"] = True
        logger.warning(
            f"📡 待执行任务已达上限 {config.LISTEN_MAX_PENDING_TASKS}"
            f"（当前 {pending_now}），本轮不建新任务；{chat_id} 的 checkpoint "
            f"停在 {checkpoint}，Worker 消费后下一轮继续"
        )
        return result

    in_whitelist = int(chat_id) in (state.WHITELIST_CHATS or {})
    tags = [r["tag"] for r in rules]
    tasks = []
    processed_upto = checkpoint

    for unit in group_by_album(new_msgs):
        unit_max_id = max(m.id for m in unit)
        media = [m for m in unit if is_downloadable(m)]
        if media:
            # **跳过频道帖在讨论组里的镜像副本**（is_channel_mirror）：同一篇
            # 帖子在频道侧会被自己的监听规则命中一次，镜像副本又会在讨论组侧
            # 命中一次——转发会重复（下载有 dedup 拦，转发没有）。镜像帖的
            # caption 与标签跟原帖一模一样，靠内容根本区分不出来，只能认
            # fwd_from.channel_post 这个结构特征。
            kept = [m for m in media if not is_channel_mirror(m)]
            if len(kept) != len(media):
                logger.info(
                    f"📡 {chat_id} #{min(m.id for m in media)}：跳过 "
                    f"{len(media) - len(kept)} 条频道帖镜像副本（频道侧负责）"
                )
            media = kept
        if not media:
            # 只处理媒体消息：纯文本转发进收藏夹会被 app._record_me_label
            # 当成「待关联评论标注」拼进下一个下载文件的文件名。
            processed_upto = max(processed_upto, unit_max_id)
            continue

        anchor = min(m.id for m in media)
        gid = getattr(media[0], "grouped_id", None)
        text = (pick_group_caption_text(media, gid) if gid
                else (getattr(media[0], "message", "") or "").strip())
        hits = matched_tags(text, tags)
        if hits:
            keys, download = set(), False
            for rule in rules:
                if rule["tag"] in hits:
                    k, d = _effective_work(rule, in_whitelist)
                    keys |= k
                    download = download or d
            if keys:
                if len(tasks) + len(keys) > budget:
                    # 这一单元要建的都建不下 → 整单元留给下一轮（checkpoint 不动）
                    result["capped"] = True
                    logger.warning(
                        f"📡 {chat_id} 队列额度只剩 {budget - len(tasks)} 条，"
                        f"暂停在消息 {anchor}（下轮继续）"
                    )
                    break
                result["matched"] += 1
                # 评论继承频道原帖的 caption/日期：解析要走 1~2 次网络请求，
                # 只在**真的命中标签**时才做（未命中的消息一分钱不花）。
                origin = await resolve_origin_snapshot(media[0])
                tasks.extend(_build_tasks(chat_id, media, keys, download,
                                          text, anchor, origin))
                logger.info(
                    f"🏷 标签监听命中：{chat_id} #{anchor} "
                    f"标签 {' '.join(sorted(hits))} → 建任务 {len(keys)} 条"
                    f"（{'整组 ' + str(len(media)) + ' 个成员' if gid else '单条'}）"
                )
                # 命中过的帖子进关注列表：评论区里的差分是**帖子发布之后**才
                # 出现的，跟进靠 follow_scan 按天做。快照存 caption/日期/目录，
                # 之后十几次检查都不必再回频道取原帖。
                # 满额只是不再跟进（记日志），**绝不影响上面刚建好的下载任务**。
                _add_follow_from_scan(chat_id, anchor, text, origin, media[0])
        processed_upto = max(processed_upto, unit_max_id)

    ids = runtime_db.enqueue_listener_tasks(
        chat_id, tasks, checkpoint=processed_upto)
    result["created"] = sum(1 for i in ids if i)
    result["duplicate"] = len(ids) - result["created"]

    stats.emit_event(
        "LISTEN_SCAN", label=str(chat_id),
        scanned=result["scanned"], matched=result["matched"],
        created=result["created"], duplicate=result["duplicate"],
    )
    return result


def is_scanning() -> bool:
    return _SCANNING


async def scan_all(manual=False) -> dict:
    """扫描全部监听聊天，把匹配结果落成持久化任务。返回汇总桶。

    被重入保护挡下时带 ``skipped``；关闭/无规则时带 ``disabled``/``empty``。

    每个聊天独立 try/except：``@source1`` 失败不影响 ``@source2/@source3``
    （§23）。整体异常也不上抛——调用方是后台循环/菜单回调，绝不能因为一次
    扫描失败把循环打死。

    **本函数不发送任何 Telegram 消息**：匹配 → 落库 → 推进 checkpoint，到此
    为止。转发由 listener_worker 常驻受控执行。
    """
    global _SCANNING
    empty = {"chats": 0, "failed_chats": 0, "scanned": 0, "matched": 0,
             "created": 0, "duplicate": 0, "capped": 0}
    if _SCANNING:
        logger.info("📡 已有扫描在进行中，跳过本次")
        return dict(empty, skipped=True)
    if not state.LISTEN_ENABLED:
        return dict(empty, disabled=True)
    if not state.LISTEN_RULES:
        return dict(empty, empty_rules=True)

    _SCANNING = True
    totals = dict(empty)
    try:
        # 按需重读配置（手工改了 listen.json 时无需重启）。失败保持当前内存态。
        try:
            reload_listen_config()
        except Exception as e:
            logger.warning(f"重读标签监听配置异常（忽略）：{e}")
        if not state.LISTEN_ENABLED or not state.LISTEN_RULES:
            return dict(totals, empty_rules=True)

        by_chat = {}
        for rule in state.LISTEN_RULES:
            by_chat.setdefault(int(rule["source_chat_id"]), []).append(rule)

        started = datetime.now()
        for chat_id, rules in by_chat.items():
            totals["chats"] += 1
            try:
                r = await _scan_chat(chat_id, rules)
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except runtime_db.DbUnavailable as e:
                # 数据库写不进去 = 本轮没做成：记日志，**checkpoint 原地不动**
                #（事务回滚保证），下一轮重扫；绝不让它把扫描循环打死。
                totals["failed_chats"] += 1
                logger.error(f"📡 标签监听因数据库不可用中止：{chat_id}（{e}）")
                continue
            except Exception as e:
                totals["failed_chats"] += 1
                logger.exception(f"⚠️ 标签监听失败：{chat_id}（{e}）")
                # 用独立事件（不是 LISTEN_SCAN）——否则「扫描 N 次」会把失败的
                # 聊天也算进去，台账的扫描次数就虚高了。
                try:
                    stats.emit_event("LISTEN_FAIL", label=str(chat_id),
                                     error=str(e)[:80])
                except Exception:
                    pass
                continue
            for key in ("scanned", "matched", "created", "duplicate"):
                totals[key] += r.get(key, 0)
            if r.get("capped"):
                totals["capped"] += 1

        totals["ts"] = started.strftime("%H:%M")
        totals["queue"] = runtime_db.get_listener_stats()
        state.LISTEN_LAST_SCAN = dict(totals)
        logger.info(
            f"📡 标签监听扫描完成（{'手动' if manual else '定时'}）："
            f"{totals['chats']} 个聊天 | 检查 {totals['scanned']} 条 | "
            f"命中 {totals['matched']} 条 | 落盘任务 {totals['created']} 条"
            + (f" | 重复跳过 {totals['duplicate']} 条" if totals["duplicate"] else "")
            + (f" | {totals['capped']} 个聊天触到队列上限（下轮继续）"
               if totals["capped"] else "")
            + (f" | 聊天失败 {totals['failed_chats']} 个"
               if totals["failed_chats"] else "")
            + f" | 待执行 {totals['queue'].get('pending', 0)} 条"
        )
        if manual and (totals["matched"] or totals["failed_chats"]):
            await _notify_scan(totals)
        return totals
    except runtime_db.DbUnavailable as e:
        logger.error(f"📡 扫描汇总读数据库失败（不影响已落盘任务）：{e}")
        return totals
    finally:
        _SCANNING = False


async def _notify_scan(totals):
    """扫描结果通知（只报告「有内容」的扫描，空扫不发，避免定期刷屏）。

    注意措辞：这里是 **Scanner** 的汇总，说的是「落盘了多少条待执行任务」，
    不是「转发了多少条」——转发由 Worker 稍后受控执行，结果另有通知。
    """
    lines = [
        f"{LISTEN_MATCH_PREFIX} 扫描完成",
        "",
        f"聊天：{totals['chats']} 个（失败 {totals['failed_chats']}）",
        f"检查消息：{totals['scanned']} 条",
        f"命中：{totals['matched']} 条",
        f"已入队待转发：{totals['created']} 条",
    ]
    if totals.get("duplicate"):
        lines.append(f"重复跳过：{totals['duplicate']} 条")
    if totals.get("capped"):
        lines.append(f"⚠️ {totals['capped']} 个聊天触到队列上限，下轮继续")
    q = totals.get("queue") or {}
    if q:
        lines.append(f"队列存量：待执行 {q.get('pending', 0)} 条")
    try:
        await notify.notify_user("\n".join(lines))
    except Exception as e:
        logger.warning(f"发送标签监听通知失败：{e}")


# ============================================================
# 评论跟进：命中标签的帖子 → 之后按天跟进它的评论区
#
# 起因：频道主常把差分图放在**评论区**，而评论区在讨论组里、监听频道的
# Scanner 看不到；把整个群加白名单又会全盘接收（风控 + 不需要）。所以只跟进
# 「命中标签的那几条帖子」——每帖每次 1 次 GetReplies。
#
# 与 _scan_chat 的分工：Scanner 管「新消息命中标签」，这里管「命中过的帖子
# 的评论区后来长出东西」。两者都**只建任务、不转发**（转发仍是 Worker 的事）。
# ============================================================
async def _replies_probe(client, peer, post_id, limit):
    """取评论区，**把 telethon 异常当返回值交出去**（不让它冒进 netio 的告警）。

    「这条帖子还没有讨论串」会抛 ``MsgIdInvalidError``，它是**预期结果**而不是
    故障——一条帖子跟进 15 天、每天一次，用 warning 打 15 行就成刷屏了。这里
    把它变成返回值，由调用方分类后决定用什么级别记。
    """
    try:
        return await client(GetRepliesRequest(
            peer=peer, msg_id=int(post_id), offset_id=0, offset_date=None,
            add_offset=0, limit=int(limit), max_id=0, min_id=0, hash=0))
    except Exception as e:      # noqa: BLE001 —— 就是要分类，不能让它冒出去
        return e


async def _fetch_replies(channel_id, post_id, limit):
    """读一条帖子的评论区，返回 ``(评论列表 或 None, 失败原因 或 None)``。

    走 netio 收口：这条路径跑在**跟进循环**里，一次网络层取消冒出来会把整个
    循环当停服信号打死（本项目最贵的坑，已咬过三次）。
    """
    client = state.client
    if client is None:
        return None, "客户端未就绪"
    peer = await netio.shielded(
        lambda: client.get_input_entity(channel_id),
        LISTEN_FETCH_TIMEOUT_SECONDS, "解析原帖 peer")
    if peer is None:
        return None, "解析原帖 peer 失败"
    got = await netio.shielded(
        lambda: _replies_probe(client, peer, post_id, limit),
        LISTEN_FETCH_TIMEOUT_SECONDS, "读取帖子评论区")
    if got is None:
        return None, "读取评论区超时或连接被取消"
    if isinstance(got, MsgIdInvalidError):
        return None, "尚无讨论串"
    if isinstance(got, Exception):
        return None, f"{type(got).__name__}: {got}"
    return list(getattr(got, "messages", None) or []), None


def _add_follow_from_scan(chat_id, post_id, text, origin, message):
    """把命中标签的帖子写进关注列表（失败只记日志，绝不影响主链路）。

    关注是**附加收益**（评论区里的差分），它自己的成败不该波及 Scanner 本轮
    已经建好的下载任务。所以这里整个包在 try 里，任何异常都只是「这次没关注上」。
    """
    try:
        caption = (origin or {}).get("caption") or text or ""
        date = (origin or {}).get("date") or getattr(message, "date", None)
        runtime_db.add_listener_follow(
            channel_id=int(chat_id),
            post_id=int(post_id),
            caption=caption,
            post_date=(date.isoformat() if hasattr(date, "isoformat") else None),
            source_name=(origin or {}).get("source_name"),
        )
    except Exception as e:
        logger.warning(f"📡 帖子 {chat_id}/{post_id} 加入关注列表失败（不影响下载）：{e}")


def _origin_from_follow(follow):
    """关注记录里的快照 → ``_build_tasks`` 要的 origin 形态。

    用**建列表时**存下的 caption/日期/目录，不再回频道取原帖——跟进要跑 15 天，
    原帖被编辑或删除都不该让已定下的命名漂移。
    """
    return {
        "peer_id": None,
        "channel_post": follow.get("post_id"),
        "caption": follow.get("caption") or "",
        "date": parse_date(follow.get("post_date")),
        "source_name": follow.get("source_name"),
    }


async def follow_scan(now=None, fetcher=None, sleep=None):
    """跟进一轮关注列表：取到期关注 → 读评论区 → 新评论建成任务。

    ``fetcher`` / ``sleep`` 可注入（单测完全不联网、也不用真等节流）。
    """
    result = {"due": 0, "checked": 0, "no_thread": 0, "created": 0,
              "duplicate": 0, "expired": 0, "skipped_whitelist": 0}
    fetch = fetcher or _fetch_replies
    nap = sleep or asyncio.sleep
    try:
        gap = float(getattr(config, "LISTEN_FOLLOW_MIN_INTERVAL_SECONDS", 1.0))
    except (TypeError, ValueError):
        gap = 1.0

    # 先把到期的置失效（不删——留下「到底等到没有」的证据），再裁掉过老的
    result["expired"] = runtime_db.expire_listener_follows(now)
    runtime_db.trim_expired_follows()

    due = runtime_db.list_due_follows(now=now)
    result["due"] = len(due)
    tasks_by_chat = {}
    for idx, follow in enumerate(due):
        if idx:
            # 节流：500 条关注连起来发就是个突发，「一天一次」的本意是摊开
            await nap(gap)
        try:
            comments, err = await fetch(
                follow["channel_id"], follow["post_id"],
                int(config.LISTEN_FOLLOW_COMMENTS_LIMIT))
        except Exception as e:      # 单帖抛错不能打断整个跟进循环（稳定性 §23）
            comments, err = None, f"{type(e).__name__}: {e}"
        if comments is None:
            runtime_db.touch_listener_follow(follow["id"], error=err, now=now)
            result["no_thread"] += 1
            if not follow.get("checks"):
                # 只报第一次：一条帖子要跟进 15 天，每次都报就是刷屏
                logger.info(
                    f"📡 关注帖 {follow['post_id']} 本轮没取到评论区（{err}），"
                    f"继续等下一轮"
                )
            continue
        runtime_db.touch_listener_follow(follow["id"], now=now)
        result["checked"] += 1
        origin = _origin_from_follow(follow)
        for unit in group_by_album(comments):
            media = [m for m in unit if is_downloadable(m)]
            if not media:
                continue
            chat_id = getattr(media[0], "chat_id", None)
            if chat_id is None:
                continue
            chat_id = int(chat_id)
            # §14 的重叠判定：源群已在下载白名单时，实时链路已经转发+下载过，
            # 这里不再为它重复建任务。
            if chat_id in (state.WHITELIST_CHATS or {}):
                result["skipped_whitelist"] += 1
                continue
            anchor = min(m.id for m in media)
            gid = getattr(media[0], "grouped_id", None)
            text = (pick_group_caption_text(media, gid) if gid
                    else (getattr(media[0], "message", "") or "").strip())
            # 评论区一律「转发收藏夹 + 下载」：跟进的目的就是把这些文件拿下来。
            # 重复由 listener_tasks 的唯一索引挡（同一条评论只建一次任务）。
            tasks_by_chat.setdefault(chat_id, []).extend(
                _build_tasks(chat_id, media, {("saved_messages", None)}, True,
                             text, anchor, origin))

    for chat_id, tasks in tasks_by_chat.items():
        ids = runtime_db.enqueue_listener_tasks(chat_id, tasks)
        created = sum(1 for i in ids if i)
        result["created"] += created
        result["duplicate"] += len(ids) - created

    if due:
        logger.info(
            f"📡 评论跟进完成：到期 {result['due']} 条 | 取到讨论串 "
            f"{result['checked']} 条 | 没取到 {result['no_thread']} 条 | "
            f"新任务 {result['created']} 条（重复 {result['duplicate']}）"
            + (f" | 失效 {result['expired']} 条" if result["expired"] else "")
        )
    return result


# ============================================================
# 展示 / 菜单 / 命令
# ============================================================
def _format_interval(minutes) -> str:
    """周期的人话展示：1440 → 「24 小时」（与规格书 §19 一致），
    超过一天的整数天用「N 天」，否则整小时用「N 小时」，再否则分钟。"""
    minutes = int(minutes)
    if minutes % 1440 == 0 and minutes > 1440:
        return f"{minutes // 1440} 天"
    if minutes % 60 == 0:
        return f"{minutes // 60} 小时"
    return f"{minutes} 分钟"


def _rule_oneline(rule) -> str:
    """一条规则的紧凑单行（列表/通知用）。"""
    name = rule.get("source_name") or f"chat_{rule.get('source_chat_id')}"
    targets = " ".join(target_label(t) for t in (rule.get("targets") or []))
    if rule.get("download"):
        targets = (targets + " ⬇️自动下载").strip()
    elif not targets:
        targets = "（无目标）"
    return f"{name} {rule.get('tag')} → {targets}"


def view_text() -> str:
    """监听视图正文（/listen 无参与菜单共用）。"""
    lines = [LISTEN_NOTIFY_PREFIX, ""]
    lines.append(f"状态：{'🟢 开启' if state.LISTEN_ENABLED else '⚪ 已关闭'}")
    lines.append(f"扫描周期：{_format_interval(state.LISTEN_INTERVAL_MINUTES)}")
    rules = list(state.LISTEN_RULES)
    lines.append(f"监听规则：{len(rules)} 条")
    if rules:
        lines.append("")
        for i, rule in enumerate(rules, start=1):
            name = rule.get("source_name") or f"chat_{rule['source_chat_id']}"
            uname = f" (@{rule['source_username']})" if rule.get(
                "source_username") else ""
            lines.append(f"{i}️⃣ {name}{uname}")
            lines.append(f"   🏷 {rule['tag']}")
            tgts = [target_label(t) for t in (rule.get("targets") or [])]
            if tgts:
                lines.append(f"   {' '.join(tgts)}")
            lines.append(
                f"   ⬇️ 自动下载：{'开启' if rule.get('download') else '关闭'}")
            lines.append("")
    last = state.LISTEN_LAST_SCAN
    if last:
        lines.append(
            f"上轮扫描：{last.get('ts', '-')} 命中 {last.get('matched', 0)} 条"
            f" / 入队 {last.get('created', 0)} 条")
    else:
        lines.append("上轮扫描：尚未扫描")
    # 待执行/处理中/成败存量来自 SQLite（队列与 checkpoint 的真相所在），
    # 读失败只影响这一行展示，绝不让视图整个报错。
    try:
        q = runtime_db.get_listener_stats()
    except runtime_db.DbUnavailable:
        q = None
    if q and q["total"]:
        lines.append(
            f"任务队列：待执行 {q['pending']} | 处理中 {q['processing']}"
            f" | 成功 {q['success']} | 失败 {q['failed']}"
            + (f" | 取消 {q['cancelled']}" if q["cancelled"] else "")
        )
    # 评论跟进存量：命中标签的帖子在有效期内按天跟进它的评论区
    try:
        active = runtime_db.count_listener_follows(runtime_db.FOLLOW_ACTIVE)
        expired = runtime_db.count_listener_follows(runtime_db.FOLLOW_EXPIRED)
    except runtime_db.DbUnavailable:
        active = expired = None
    if active:
        days = int(config.LISTEN_FOLLOW_TTL_SECONDS // 86400)
        lines.append(
            f"评论跟进：{active} 条帖子在跟进（有效期 {days} 天"
            + (f"，已失效 {expired} 条" if expired else "") + "）"
        )
    return "\n".join(lines).rstrip()


def summary_text(totals) -> str:
    """「▶️ 立即扫描」的结果正文。"""
    if totals.get("skipped"):
        return f"{LISTEN_NOTIFY_PREFIX}\n\n⏳ 已有扫描在进行中，请稍候。"
    if totals.get("disabled"):
        return f"{LISTEN_NOTIFY_PREFIX}\n\n⚪ 标签监听已关闭，未扫描。"
    if totals.get("empty_rules"):
        return f"{LISTEN_NOTIFY_PREFIX}\n\n尚未配置任何监听规则。"
    lines = [
        f"{LISTEN_NOTIFY_PREFIX} 扫描完成",
        "",
        f"聊天：{totals['chats']} 个（失败 {totals['failed_chats']}）",
        f"检查消息：{totals['scanned']} 条",
        f"命中：{totals['matched']} 条",
        f"已入队待转发：{totals.get('created', 0)} 条",
    ]
    if totals.get("duplicate"):
        lines.append(f"重复跳过：{totals['duplicate']} 条")
    if totals.get("capped"):
        lines.append(f"⚠️ {totals['capped']} 个聊天触到队列上限（下轮继续）")
    q = totals.get("queue") or {}
    if q:
        lines.append(
            f"队列：待执行 {q.get('pending', 0)} | 处理中 {q.get('processing', 0)}"
            f" | 成功 {q.get('success', 0)} | 失败 {q.get('failed', 0)}"
        )
    lines.append("")
    lines.append("转发由常驻 Worker 受控执行，稍后完成。")
    return "\n".join(lines)


# ---- 命令 ----
_LISTEN_CMD_RE = re.compile(r"^/listen(?:\s|$)", re.IGNORECASE)


def is_listen_command(text) -> bool:
    """/listen 开头的命令（含裸命令）；/listeners 不算。"""
    return bool(_LISTEN_CMD_RE.match(str(text or "").strip()))


def parse_listen_command(text):
    """解析 /listen 子命令 → (action, arg)；不是该命令返回 None。"""
    raw = str(text or "").strip()
    if not is_listen_command(raw):
        return None
    body = raw[len("/listen"):].strip()
    if not body:
        return ("list", None)
    head, _, rest = body.partition(" ")
    head = head.strip().lower()
    rest = rest.strip()
    if head in ("on", "off", "scan", "list"):
        return (head, None)
    if head in ("add", "del", "interval", "edit"):
        return (head, rest)
    return ("usage", head)


USAGE_TEXT = (
    f"{LISTEN_NOTIFY_PREFIX} 用法：\n"
    "/listen — 查看状态与规则\n"
    "/listen on | off — 开启/关闭标签监听\n"
    "/listen add <聊天> <标签> [目标,目标] [on|off]\n"
    "    例：/listen add @source #01musume me,@speedlearnnn on\n"
    "    目标可用 me（收藏夹）/ @用户名 / 数字 ID；省略则只转发收藏夹\n"
    "/listen del <序号> — 删除规则\n"
    "/listen interval <分钟> — 设置扫描周期\n"
    "/listen scan — 立即扫描一次"
)


async def _resolve_targets(tokens):
    """把逗号分隔的目标 token 解析成持久化目标；返回 (targets, 错误)。"""
    targets = []
    for token in tokens:
        tok = token.strip()
        if not tok:
            continue
        if tok.lower() in ("me", "saved_messages", "收藏夹"):
            targets.append({"type": "saved_messages"})
            continue
        info, err = await resolve_chat(tok)
        if err:
            return [], err
        targets.append({"type": "chat", "chat_id": info["chat_id"],
                        "name": info["name"]})
    return targets, ""


async def resolve_chat(spec, cli=None):
    """把 @username / 数字 id 解析成 {chat_id,name,username}；失败返回 (None, 提示)。"""
    cli = cli or state.client
    if cli is None:
        return None, "❌ 客户端未就绪"
    target = spec
    try:
        target = int(str(spec).strip())
    except (TypeError, ValueError):
        target = str(spec).strip()
    try:
        entity = await cli.get_entity(target)
    except Exception as e:
        logger.warning(f"📡 解析监听聊天失败（{spec}）：{e}")
        return None, f"❌ 无法解析聊天：{spec}"
    chat_id = get_peer_id(entity)
    return {
        "chat_id": chat_id,
        "name": entity_display_name(entity) or f"chat_{chat_id}",
        "username": getattr(entity, "username", "") or "",
    }, ""


async def _cmd_add(arg):
    parts = str(arg or "").split()
    if len(parts) < 2:
        return USAGE_TEXT
    chat_spec, tag = parts[0], parts[1]
    rest = parts[2:]
    download = False
    if rest and rest[-1].lower() in ("on", "off"):
        download = rest[-1].lower() == "on"
        rest = rest[:-1]

    info, err = await resolve_chat(chat_spec)
    if err:
        return f"{LISTEN_NOTIFY_PREFIX}：{err}"

    tokens = ",".join(rest).split(",") if rest else []
    targets, err = await _resolve_targets(tokens)
    if err:
        return f"{LISTEN_NOTIFY_PREFIX}：{err}"
    if not targets:
        targets = [{"type": "saved_messages"}]

    rule = {
        "source_chat_id": info["chat_id"],
        "source_name": info["name"],
        "source_username": info["username"],
        "tag": tag,
        "targets": targets,
        "download": download,
    }
    ok, msg = await add_listener(rule)
    return msg


async def command_reply(action, arg) -> str:
    """执行一条 /listen 子命令并返回回复文本（命令与菜单共用同一套服务函数）。"""
    if action == "list":
        return view_text()
    if action == "on":
        return set_enabled(True)
    if action == "off":
        return set_enabled(False)
    if action == "interval":
        if not arg:
            return (f"{LISTEN_NOTIFY_PREFIX} 当前周期："
                    f"{_format_interval(state.LISTEN_INTERVAL_MINUTES)}\n"
                    "用法：/listen interval 30")
        return set_interval(arg)[1]
    if action == "del":
        return del_listener(arg)[1]
    if action == "add":
        return await _cmd_add(arg)
    if action == "scan":
        return summary_text(await scan_all(manual=True))
    return USAGE_TEXT


# ---- 菜单按钮 ----
def menu_buttons():
    """监听视图按钮：增删改 / 立即扫描 / 周期 / 总开关 / 返回。"""
    def enc(action, arg=None):
        from .menu import encode_menu_data   # 函数内导入避免 menu↔listener 环
        return encode_menu_data(action, arg)

    toggle = "🔄 关闭监听" if state.LISTEN_ENABLED else "🔄 开启监听"
    rows = [
        [Button.inline("➕ 添加监听", enc("listen_add")),
         Button.inline("✏️ 修改监听", enc("listen_edit"))],
        [Button.inline("🗑 删除监听", enc("listen_del")),
         Button.inline("▶️ 立即扫描", enc("listen_scan"))],
        [Button.inline("⏱ 扫描周期", enc("listen_interval")),
         Button.inline(toggle, enc("listen_toggle"))],
        [Button.inline("🔙 返回主菜单", enc("home"))],
    ]
    return rows


def rule_pick_buttons(prefix):
    """规则选择按钮（修改/删除用，每条一行，回调直接带序号）。"""
    from .menu import encode_menu_data
    rows = []
    for i, rule in enumerate(state.LISTEN_RULES, start=1):
        label = f"{i}. {rule.get('source_name') or rule['source_chat_id']} "\
                f"{rule['tag']}"
        rows.append([Button.inline(label[:60],
                                   encode_menu_data(prefix, str(i)))])
    rows.append([Button.inline("🔙 返回", encode_menu_data("listen"))])
    return rows


# ---- 添加/修改向导 ----
def draft_active() -> bool:
    return _DRAFT is not None


def draft_start(seed=None, edit_index=None):
    """开一份新草稿；seed 为现有规则（「✏️ 修改」用）时预填。"""
    global _DRAFT
    if seed:
        _DRAFT = {
            "source_chat_id": int(seed["source_chat_id"]),
            "source_name": seed.get("source_name", ""),
            "source_username": seed.get("source_username", ""),
            "tag": seed.get("tag", ""),
            "targets": [normalize_target(t) for t in seed.get("targets") or []],
            "download": bool(seed.get("download")),
            "edit_index": edit_index,
        }
    else:
        _DRAFT = {
            "source_chat_id": None,
            "source_name": "",
            "source_username": "",
            "tag": "",
            "targets": [],
            "download": False,
            "edit_index": None,
        }
    return _DRAFT


def draft_cancel():
    global _DRAFT
    _DRAFT = None


def draft_get():
    return _DRAFT


def draft_set_source(info):
    _DRAFT["source_chat_id"] = int(info["chat_id"])
    _DRAFT["source_name"] = info.get("name") or ""
    _DRAFT["source_username"] = info.get("username") or ""


def draft_set_tag(text) -> tuple:
    tag = str(text or "").strip()
    if not tag.startswith("#") or len(tag) < 2:
        return False, "❌ 标签须以 # 开头，如 #01musume"
    _DRAFT["tag"] = tag
    return True, ""


def draft_add_target(info):
    target = {"type": "chat", "chat_id": int(info["chat_id"]),
              "name": info.get("name") or ""}
    key = target_key(target)
    if any(target_key(t) == key for t in _DRAFT["targets"]):
        return False, "❌ 该目标已在列表里"
    _DRAFT["targets"].append(target)
    return True, ""


def draft_toggle_target(key_str) -> bool:
    """按目标键字符串（saved_messages / chat:<id>）切换勾选；返回是否成功。"""
    key_str = str(key_str or "")
    if key_str == WORK_SAVED:
        target = {"type": "saved_messages"}
    else:
        try:
            target = {"type": "chat", "chat_id": int(key_str.split(":", 1)[1])}
        except (IndexError, ValueError):
            return False
    for i, t in enumerate(_DRAFT["targets"]):
        if target_key(t) == target_key(target):
            _DRAFT["targets"].pop(i)
            return True
    _DRAFT["targets"].append(target)
    return True


def draft_set_download(value):
    _DRAFT["download"] = bool(value)


def draft_summary_text() -> str:
    if _DRAFT is None:
        return "（没有进行中的添加流程）"
    src = _DRAFT.get("source_name") or (
        f"chat_{_DRAFT['source_chat_id']}" if _DRAFT.get("source_chat_id")
        else "（未设置）")
    lines = [
        f"{LISTEN_NOTIFY_PREFIX} · 添加/修改",
        "",
        f"来源聊天：{src}",
        f"标签：{_DRAFT.get('tag') or '（未设置）'}",
        "目标：",
    ]
    tgts = _DRAFT.get("targets") or []
    if not tgts:
        lines.append("  （尚未选择）")
    for t in tgts:
        lines.append(f"  {target_label(t)}")
    if _DRAFT.get("download"):
        lines.append("  📌 收藏夹（自动下载）")
    lines.append(
        f"自动下载：{'🟢 开启' if _DRAFT.get('download') else '⚪ 关闭'}")
    lines.append("")
    lines.append("提示：download=true 会自动把消息转发进收藏夹并下载，"
                 "无需再勾选收藏夹。")
    return "\n".join(lines)


def draft_buttons():
    from .menu import encode_menu_data
    rows = []
    current = {target_key(t) for t in (_DRAFT.get("targets") or [])}
    me_mark = "☑" if ("saved_messages", None) in current else "☐"
    rows.append([Button.inline(
        f"{me_mark} 📌 收藏夹", encode_menu_data("listen_tgt", WORK_SAVED))])
    for t in _DRAFT.get("targets") or []:
        key = target_key(t)
        if key[0] == "saved_messages":
            continue
        mark = "☑" if key in current else "☐"
        label = f"{mark} {t.get('name') or t.get('chat_id')}"
        rows.append([Button.inline(
            label[:60], encode_menu_data("listen_tgt", f"chat:{key[1]}"))])
    rows.append([Button.inline("➕ 添加目标聊天",
                               encode_menu_data("listen_tgtadd"))])
    dl = "🟢 自动下载：开" if _DRAFT.get("download") else "⚪ 自动下载：关"
    rows.append([Button.inline(dl, encode_menu_data("listen_dl"))])
    rows.append([
        Button.inline("✅ 保存", encode_menu_data("listen_save")),
        Button.inline("✖️ 取消", encode_menu_data("listen_cancel")),
    ])
    return rows


async def draft_save() -> tuple:
    """保存草稿：新聊天先初始化 checkpoint（绝不扫历史），再落盘。"""
    if _DRAFT is None:
        return False, "❌ 没有进行中的添加流程"
    if not _DRAFT.get("source_chat_id"):
        return False, "❌ 还没有设置来源聊天"
    if not _DRAFT.get("tag"):
        return False, "❌ 还没有设置标签"

    targets = list(_DRAFT.get("targets") or [])
    if _DRAFT.get("download"):
        if not any(target_key(t) == ("saved_messages", None) for t in targets):
            targets.insert(0, {"type": "saved_messages"})

    rule = {
        "source_chat_id": _DRAFT["source_chat_id"],
        "source_name": _DRAFT.get("source_name", ""),
        "source_username": _DRAFT.get("source_username", ""),
        "tag": _DRAFT["tag"],
        "targets": targets,
        "download": bool(_DRAFT.get("download")),
    }
    if _DRAFT.get("edit_index") is not None:
        ok, msg = replace_listener(_DRAFT["edit_index"], rule)
    else:
        ok, msg = await add_listener(rule)
    if ok:
        draft_cancel()
    return ok, msg


def input_prompt(step) -> str:
    """向导某一步的提示文本（窗口内下一条文本即输入）。"""
    seconds = config.LISTEN_INPUT_WINDOW_SECONDS
    if step == "chat":
        return (
            f"{LISTEN_NOTIFY_PREFIX} · 第 1 步：来源聊天\n\n"
            "请发送要监听的聊天（@用户名 或 数字 ID，发到本对话）。\n\n"
            f"{seconds} 秒内有效，发送 / 开头的命令可取消。"
        )
    if step == "tag":
        return (
            f"{LISTEN_NOTIFY_PREFIX} · 第 2 步：标签\n\n"
            "请发送要匹配的标签（须以 # 开头，如 #01musume）。\n\n"
            f"{seconds} 秒内有效，发送 / 开头的命令可取消。"
        )
    return (
        f"{LISTEN_NOTIFY_PREFIX} · 添加转发目标\n\n"
        "请发送目标聊天（@用户名 或 数字 ID；发 me 表示收藏夹）。\n\n"
        f"{seconds} 秒内有效，发送 / 开头的命令可取消。"
    )
