"""标签监听：按周期主动扫描指定聊天，命中配置的标签后转发到目标并按需下载。

**与下载白名单完全独立**（规格书最重要的设计约束）：

    下载白名单 /wl ──► 聊天收到媒体 → 实时转发收藏夹 → 下载
    标签监听 listen ──► 定时主动扫描 → 标签匹配 → 转发目标 / 下载

监听来源来自自己的 ``runtime/listen.json`` 与 ``state.LISTEN_RULES``，**绝不**
从 ``state.WHITELIST_CHATS`` 推导，也不要求监听聊天加入 /wl。一个聊天可以只在
下载白名单、只在标签监听、两边都在、或两边都不在。唯一一次「读」白名单是 §14
的重叠判定：源聊天已在下载白名单时，实时链路已经转发+下载过这条消息，监听就
跳过 me 目标与下载（``_effective_work``），只跑其余目标——只读，不改其语义。

**download=true 的实现**：把消息转发进收藏夹，并入队那份转发副本——复用现有
``app.enqueue_media`` → 下载队列 → dedup → 命名的整条链路，不新增第二套下载器。
为什么要显式入队而不是指望收藏夹的事件入口：**userbot 自己发出的转发不会回流成
更新**（2026-09-11 用生产日志核实：转发出的副本 ID=31160 在 208 条「📨 Saved
Messages 收到消息」里没有对应行），这正是既有白名单链路 ``_relay_single`` 转发
完也要显式 ``enqueue_media(fwd, …)`` 的原因。转发之所以仍然必要：副本自带系统
「转发自 <来源>」头，用户在收藏夹看得出处，落盘目录也由副本的 ``fwd_from`` 解析
回原来源频道。

**扫描纪律**：
* 按 chat 扫描一次，再匹配该 chat 下的全部规则（不按规则重复请求 Telegram）；
* 第一层：同一消息命中多个标签/多条规则 → 先「匹配 → 合并」再执行，同一
  (消息, 目标) 只转发一次；
* 相册按 grouped_id 整组处理（标签常只挂在其中一个成员的说明上）；
* 只处理**媒体**消息——纯文本转发进收藏夹会被 ``app._record_me_label`` 当成
  「待关联评论标注」拼进下一个下载文件的文件名，污染命名；
* 首次添加监听时以当前最新消息 id 作 checkpoint，**绝不扫历史**（§8）；
* checkpoint 只推进；失败的目标以 ``pending`` 精确续做（一个目标失败不会让
  整条消息重放、也不会因为推进 checkpoint 而永久丢失）。

**稳定性**：每 chat 独立 try/except（一个聊天失败不影响其它）；所有网络调用经
``netio.shielded`` 收口（断线的网络层取消只会变成「本轮没做成」，绝不会把这个
后台循环打死——本项目最贵的坑，见 CLAUDE.md）；配置/状态读失败回落空配置、
写盘 temp + ``os.replace`` 原子。
"""
import asyncio
import json
import os
import re
import time
from datetime import datetime

from telethon import Button
from telethon.errors import FloodWaitError
from telethon.utils import get_peer_id

from . import config
from . import netio
from . import notify
from . import state
from . import stats
from .config import (
    LISTEN_CONFIG_FILE,
    LISTEN_FETCH_TIMEOUT_SECONDS,
    LISTEN_FORWARD_TIMEOUT_SECONDS,
    LISTEN_MAX_MESSAGES_PER_SCAN,
    LISTEN_MAX_PENDING,
    LISTEN_MAX_RETRY_UNITS,
    LISTEN_MATCH_PREFIX,
    LISTEN_MAX_INTERVAL_MINUTES,
    LISTEN_MIN_INTERVAL_MINUTES,
    LISTEN_NOTIFY_PREFIX,
)
from .log import logger
from .naming import pick_group_caption_text
from .sources import entity_display_name, is_downloadable, message_source_link

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


def work_chat(chat_id) -> str:
    """普通目标聊天的工作项键（与 target_key 的 ("chat", id) 一一对应）。"""
    return f"chat:{int(chat_id)}"


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


def work_item_for(target) -> str:
    """目标 → 工作项键（pending 里存的就是它）。"""
    key = target_key(target)
    if key is None:
        return ""
    return WORK_SAVED if key[0] == "saved_messages" else work_chat(key[1])


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


def _work_items(keys):
    """目标键集合 → 工作项列表；收藏夹排最前（下载依赖它的副本）。"""
    items = []
    for key in sorted(keys, key=lambda k: (k[0] != "saved_messages", str(k[1]))):
        items.append(WORK_SAVED if key[0] == "saved_messages"
                     else work_chat(key[1]))
    return items


# ============================================================
# 配置与状态持久化
#
# 与 caption_filter 同构：路径与默认值都在**调用时**读 config 模块属性
# （方便单测 monkeypatch，不能 from-import 成别名）；写盘一律 temp +
# os.replace 原子；读失败/JSON 损坏回落空配置，绝不影响主进程。
# ============================================================
def _config_path():
    return getattr(config, "LISTEN_CONFIG_FILE", LISTEN_CONFIG_FILE)


def _state_path():
    return getattr(config, "LISTEN_STATE_FILE", config.LISTEN_STATE_FILE)


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


def save_listen_state() -> bool:
    """把扫描游标（checkpoint + pending）原子写盘。"""
    path = _state_path()
    tmp_path = path + ".tmp"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state.LISTEN_STATE or {}, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
        return True
    except Exception as e:
        logger.warning(f"保存标签监听状态失败（不影响运行）：{e}")
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return False


def load_listen_state() -> int:
    """载入 listen_state.json，返回聊天条目数；损坏回落空状态。"""
    state.LISTEN_STATE = {}
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("顶层不是对象")
    except FileNotFoundError:
        return 0
    except Exception as e:
        logger.warning(f"读取标签监听状态失败，改用空状态：{e}")
        return 0

    clean = {}
    for chat_id, entry in data.items():
        if not isinstance(entry, dict):
            continue
        try:
            last_id = int(entry.get("last_message_id") or 0)
        except (TypeError, ValueError):
            continue
        pending = {}
        for key, rec in (entry.get("pending") or {}).items():
            if not isinstance(rec, dict):
                continue
            ids = [int(i) for i in (rec.get("ids") or [])
                   if str(i).lstrip("-").isdigit()]
            work = [str(w) for w in (rec.get("work") or [])]
            if not ids or not work:
                continue
            pending[str(key)] = {
                "ids": ids,
                "work": work,
                "dl": bool(rec.get("dl")),
                "cap": str(rec.get("cap") or ""),
            }
        clean[str(chat_id)] = {"last_message_id": last_id, "pending": pending}
    state.LISTEN_STATE = clean
    return len(clean)


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

    if str(chat_id) not in state.LISTEN_STATE:
        newest = await _fetch_newest_id(chat_id)
        if newest is None:
            return False, (f"{LISTEN_NOTIFY_PREFIX}：读取该聊天最新消息失败，"
                           "已放弃添加（请确认有访问权限后重试）")
        state.LISTEN_STATE[str(chat_id)] = {
            "last_message_id": newest, "pending": {},
        }
        save_listen_state()
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
    """取 checkpoint 之后的新消息（升序，最多一轮上限）；失败返回 None。"""
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
    return sorted(msgs, key=lambda m: m.id)


async def _fetch_by_ids(chat_id, ids):
    """按 id 取消息（pending 续做用）；失败返回空列表。"""
    cli = state.client
    if cli is None or not ids:
        return []
    got = await netio.shielded(
        lambda: cli.get_messages(chat_id, ids=list(ids)),
        LISTEN_FETCH_TIMEOUT_SECONDS,
        f"读取待续做消息（{chat_id}）",
    )
    if got is None:
        return []
    msgs = got if isinstance(got, (list, tuple)) else [got]
    return [m for m in msgs if m is not None]


async def _forward_to_target(target, messages, from_peer):
    """整组转发到目标，返回副本列表；失败返回 None（留给 pending 续做）。

    不给 ``netio.shielded`` 传很紧的超时：转发触发 FloodWait 时 Telethon 会
    自行等待，收口到点取消会把这种合法等待掐成失败。收口仍然必要——它把
    断线的网络层取消变成「本轮没做成」，而不是把后台扫描循环打死。
    """
    key = target_key(target)
    if key is None:
        return None
    peer = "me" if key[0] == "saved_messages" else key[1]
    cli = state.client

    async def _do():
        last_error = None
        for attempt in range(1, 3):
            try:
                return await cli.forward_messages(
                    peer, messages, from_peer=from_peer)
            except FloodWaitError as e:
                wait = min(int(getattr(e, "seconds", None) or 30), 60)
                last_error = e
                logger.warning(
                    f"📡 监听转发触发频率限制，等待 {wait}s 后重试"
                    f"（{peer}，第 {attempt}/2 次）"
                )
                await asyncio.sleep(wait)
        raise last_error or RuntimeError("转发多次被频率限制")

    sent = await netio.shielded(
        _do, LISTEN_FORWARD_TIMEOUT_SECONDS, f"标签监听转发 → {peer}")
    if sent is None:
        return None
    sent = sent if isinstance(sent, (list, tuple)) else [sent]
    return [s for s in sent if s is not None]


async def _enqueue_copy(copy, source_link, album_caption, src):
    """入队一份转发副本（走现有下载链路，不新增第二套下载器）。

    ``app`` 在函数内导入：app 顶层 `from . import listener`，模块级互相导入
    会成环（与 menu 里函数内导入 dedup 同一处理）。
    """
    from . import app
    await app.enqueue_media(
        copy, state.MY_ID, None,
        source_link=source_link,
        album_caption=album_caption,
        src=src,
    )


# ============================================================
# 执行：一个消息单元 × 一组工作项
# ============================================================
async def _execute_unit(members, work, chat_id, caption, download):
    """执行一个单元（单条/整组相册）的工作项，返回 (剩余工作项, 成功数, 失败数)。

    每个工作项各自成败：收藏夹转发成功就先入队副本，绝不因为「另一个目标
    失败」把已完成的部分回滚重来（否则下一轮会重复转发/重复下载）。
    """
    remaining = []
    ok_count = fail_count = 0
    media = [m for m in (members or []) if is_downloadable(m)]
    if not media:
        return remaining, 0, 0
    source_link = message_source_link(media[0], chat_id)

    for item in work:
        if item == WORK_SAVED:
            copies = await _forward_to_target(
                {"type": "saved_messages"}, media, chat_id)
            if copies is None:
                remaining.append(item)
                fail_count += 1
                continue
            ok_count += 1
            if download:
                for copy in copies:
                    # 转发副本保留它自己的说明；无文字的副本继承源侧读到的
                    # 相册说明做命名（否则图片会退化成 媒体类型_时间戳）
                    own_text = (getattr(copy, "message", "") or "").strip()
                    cap = None if own_text else (caption or None)
                    try:
                        await _enqueue_copy(copy, source_link, cap, "listen")
                    except Exception as e:
                        logger.exception(f"📡 标签监听副本入队失败：{e}")
        elif isinstance(item, str) and item.startswith("chat:"):
            try:
                cid = int(item.split(":", 1)[1])
            except (IndexError, ValueError):
                continue
            sent = await _forward_to_target({"type": "chat", "chat_id": cid},
                                            media, chat_id)
            if sent is None:
                remaining.append(item)
                fail_count += 1
            else:
                ok_count += 1
    return remaining, ok_count, fail_count


def _pending_cap(pending):
    """pending 条数超上限时丢最旧的（按消息 id 小的先丢）并告警。

    兜底用：目标频道长期不可达时不会让状态文件无界增长。丢了会记日志，
    不是静默吞掉。
    """
    if len(pending) <= LISTEN_MAX_PENDING:
        return
    try:
        ordered = sorted(pending, key=lambda k: int(k))
    except (TypeError, ValueError):
        ordered = sorted(pending)
    excess = len(pending) - LISTEN_MAX_PENDING
    for key in ordered[:excess]:
        pending.pop(key, None)
    logger.warning(f"📡 标签监听待续做条目超上限，已丢弃最旧 {excess} 条")


async def _scan_chat(chat_id, rules):
    """扫一个聊天：先续做 pending，再取新消息匹配执行。返回本轮统计。"""
    entry = state.LISTEN_STATE.setdefault(
        str(chat_id), {"last_message_id": 0, "pending": {}})
    entry.setdefault("last_message_id", 0)
    entry.setdefault("pending", {})
    pending = entry["pending"]

    result = {"scanned": 0, "matched": 0, "forwarded": 0, "failed": 0}

    # ---------- 1. 先续做上一轮没做成的（用记录里的 work，不再看规则） ----------
    # 每轮最多续做 LISTEN_MAX_RETRY_UNITS 个单元：一条 get_messages 的 ids 数组
    # 不宜过大（200 个单元 × 最多 10 个相册成员 = 2000 个 id），没轮到的留在
    # pending 里下一轮继续，不会丢。
    retry_keys = list(pending.keys())[:LISTEN_MAX_RETRY_UNITS]
    retry_ids = [i for k in retry_keys for i in (pending[k].get("ids") or [])]
    retry_msgs = await _fetch_by_ids(chat_id, retry_ids)
    by_id = {m.id: m for m in retry_msgs}
    for key in retry_keys:
        rec = pending[key]
        unit = [by_id[i] for i in (rec.get("ids") or []) if i in by_id]
        if not unit:
            logger.warning(
                f"📡 待续做消息已不可读（可能被删除），放弃：{chat_id} #{key}")
            pending.pop(key, None)
            continue
        left, ok_n, fail_n = await _execute_unit(
            unit, rec.get("work") or [], chat_id,
            rec.get("cap") or "", rec.get("dl"))
        result["forwarded"] += ok_n
        result["failed"] += fail_n
        if left:
            rec["work"] = left
        else:
            pending.pop(key, None)

    # ---------- 2. 取新消息并匹配 ----------
    new_msgs = await _fetch_new(chat_id, entry["last_message_id"])
    if new_msgs is None:
        # 读失败：checkpoint 原地不动，本轮不处理这个聊天（下轮再试）
        raise RuntimeError(f"读取新消息失败：{chat_id}")
    result["scanned"] = len(new_msgs)

    in_whitelist = int(chat_id) in (state.WHITELIST_CHATS or {})
    tags = [r["tag"] for r in rules]

    for unit in group_by_album(new_msgs):
        media = [m for m in unit if is_downloadable(m)]
        if not media:
            continue   # 只处理媒体：纯文本转发进收藏夹会污染标注命名
        gid = getattr(media[0], "grouped_id", None)
        text = (pick_group_caption_text(media, gid) if gid
                else (getattr(media[0], "message", "") or "").strip())
        hits = matched_tags(text, tags)
        if not hits:
            continue

        keys, download = set(), False
        for rule in rules:
            if rule["tag"] in hits:
                k, d = _effective_work(rule, in_whitelist)
                keys |= k
                download = download or d
        if not keys:
            continue   # 例如源在白名单、规则只有收藏夹目标 → 无事可做

        result["matched"] += 1
        unit_key = str(min(m.id for m in media))
        work = _work_items(keys)
        left, ok_n, fail_n = await _execute_unit(
            media, work, chat_id, text, download)
        result["forwarded"] += ok_n
        result["failed"] += fail_n
        if left:
            pending[unit_key] = {
                "ids": [m.id for m in media],
                "work": left,
                "dl": download,
                "cap": text,
            }
        logger.info(
            f"🏷 标签监听命中：{chat_id} #{unit_key} "
            f"标签 {' '.join(sorted(hits))} → 转发 {ok_n} 项"
            + (f"，失败 {fail_n} 项转下轮续做" if fail_n else "")
        )

    # ---------- 3. 推进 checkpoint（pending 已按 id 记下，不会丢） ----------
    if new_msgs:
        newest = max(m.id for m in new_msgs)
        if newest > int(entry["last_message_id"] or 0):
            entry["last_message_id"] = newest
    _pending_cap(pending)

    stats.emit_event(
        "LISTEN_SCAN", label=str(chat_id),
        scanned=result["scanned"], matched=result["matched"],
        forwarded=result["forwarded"], failed=result["failed"],
    )
    save_listen_state()
    return result


def is_scanning() -> bool:
    return _SCANNING


async def scan_all(manual=False) -> dict:
    """扫描全部监听聊天。返回汇总桶；被重入保护挡下时带 ``skipped``。

    每个聊天独立 try/except：``@source1`` 失败不影响 ``@source2/@source3``
    （§23）。整体异常也不上抛——调用方是后台循环/菜单回调，绝不能因为一次
    扫描失败把循环打死。
    """
    global _SCANNING
    if _SCANNING:
        logger.info("📡 已有扫描在进行中，跳过本次")
        return {"skipped": True, "chats": 0, "failed_chats": 0, "scanned": 0,
                "matched": 0, "forwarded": 0, "failed": 0}
    if not state.LISTEN_ENABLED:
        return {"disabled": True, "chats": 0, "failed_chats": 0, "scanned": 0,
                "matched": 0, "forwarded": 0, "failed": 0}
    if not state.LISTEN_RULES:
        return {"empty": True, "chats": 0, "failed_chats": 0, "scanned": 0,
                "matched": 0, "forwarded": 0, "failed": 0}

    _SCANNING = True
    totals = {"chats": 0, "failed_chats": 0, "scanned": 0,
              "matched": 0, "forwarded": 0, "failed": 0}
    try:
        # 按需重读配置（手工改了 listen.json 时无需重启）。失败保持当前内存态。
        try:
            reload_listen_config()
        except Exception as e:
            logger.warning(f"重读标签监听配置异常（忽略）：{e}")
        if not state.LISTEN_ENABLED or not state.LISTEN_RULES:
            return dict(totals, **{"empty": True})

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
            for key in ("scanned", "matched", "forwarded", "failed"):
                totals[key] += r.get(key, 0)

        totals["ts"] = started.strftime("%H:%M")
        state.LISTEN_LAST_SCAN = dict(totals)
        logger.info(
            f"📡 标签监听扫描完成（{'手动' if manual else '定时'}）："
            f"{totals['chats']} 个聊天 | 检查 {totals['scanned']} 条 | "
            f"命中 {totals['matched']} 条 | 转发 {totals['forwarded']} 项"
            + (f" | 失败 {totals['failed']} 项" if totals["failed"] else "")
            + (f" | 聊天失败 {totals['failed_chats']} 个"
               if totals["failed_chats"] else "")
        )
        if manual and (totals["matched"] or totals["failed_chats"]):
            await _notify_scan(totals)
        return totals
    finally:
        _SCANNING = False


async def _notify_scan(totals):
    """扫描结果通知（只报告「有内容」的扫描，空扫不发，避免定期刷屏）。"""
    lines = [
        f"{LISTEN_MATCH_PREFIX} 扫描完成",
        "",
        f"聊天：{totals['chats']} 个（失败 {totals['failed_chats']}）",
        f"检查消息：{totals['scanned']} 条",
        f"命中：{totals['matched']} 条",
        f"转发：{totals['forwarded']} 项",
    ]
    if totals["failed"]:
        lines.append(f"失败：{totals['failed']} 项（下轮自动续做）")
    try:
        await notify.notify_user("\n".join(lines))
    except Exception as e:
        logger.warning(f"发送标签监听通知失败：{e}")


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
            f" / 转发 {last.get('forwarded', 0)} 项")
    else:
        lines.append("上轮扫描：尚未扫描")
    return "\n".join(lines).rstrip()


def summary_text(totals) -> str:
    """「▶️ 立即扫描」的结果正文。"""
    if totals.get("skipped"):
        return f"{LISTEN_NOTIFY_PREFIX}\n\n⏳ 已有扫描在进行中，请稍候。"
    if totals.get("disabled"):
        return f"{LISTEN_NOTIFY_PREFIX}\n\n⚪ 标签监听已关闭，未扫描。"
    if totals.get("empty"):
        return f"{LISTEN_NOTIFY_PREFIX}\n\n尚未配置任何监听规则。"
    lines = [
        f"{LISTEN_NOTIFY_PREFIX} 扫描完成",
        "",
        f"聊天：{totals['chats']} 个（失败 {totals['failed_chats']}）",
        f"检查消息：{totals['scanned']} 条",
        f"命中：{totals['matched']} 条",
        f"转发：{totals['forwarded']} 项",
    ]
    if totals["failed"]:
        lines.append(f"失败：{totals['failed']} 项（下轮自动续做）")
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
