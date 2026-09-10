"""Caption 命名清洗：把转发说明里的字段标签 / URL / 包裹括号从文件名里去掉。

只负责「Caption → 清洗后的 Caption」这一件事：不碰 Telegram、不碰下载、
不碰文件名长度（截断仍归 naming.truncate_filename）。规则是普通字符串列表，
4 种前缀（见 config.DEFAULT_CAPTION_FILTER_RULES 的说明）：

  * ``exact:<串>``    删除这个确切子串（不做任何语义推断）；
  * ``contains:<串>`` 删除所有出现的该子串（**有意的宽匹配**）；
  * ``regex:<正则>``  按正则删除；非法正则跳过，不影响其它规则；
  * ``field:<字段名>`` 剥掉「字段名 + 冒号」，**字段值保留**。

field 的语义（真实用例的验收口径）：``作者：#腿玩年 期数：bl11`` 要变成
``#腿玩年 bl11``——去掉的是标签，不是整个字段。字段起点必须落在「Caption
开头或空白之后」，否则 ``这个作者真的很厉害`` 里那个「作者」会被误删；
字段名后**紧跟左括号**也算字段起点，覆盖 ``i站地址【 https://… 】`` 这类
没有冒号的写法（括号里的内容交给 regex 规则处理）。

执行顺序固定为 field → exact → contains → regex → 空白归一：field 依赖
Caption 原始的字段结构，必须先跑，否则 ``contains:作者`` 会先把
``作者：#腿玩年`` 的结构打碎（同类型规则之间保持用户配置的顺序）。
清洗必须在 ``sanitize_filename`` **之前**做——sanitize 会把换行和 ASCII
冒号换成 ``_``，之后字段边界就认不出来了。
"""
import os
import re
import json

from . import config
from . import state
from .log import logger

# 合法的规则类型前缀
RULE_TYPES = ("exact", "contains", "regex", "field")

# 字段名后紧跟这些左括号时，同样算作字段起点（剥名字、留括号内容）
_BRACKET_OPEN = r"[【（(\[]"


def parse_caption_filter_rule(rule):
    """把一条规则字符串解析成 {"type":…, "pattern":…}；非法规则抛 ValueError。

    只切**第一个**冒号——``regex:https?://\\S+`` 的正文里还有冒号，按
    ``split(":")`` 会切碎。空规则（``field:``）、不支持的前缀（``abc:x``）、
    非法正则（``regex:(abc``）都拒绝，调用方负责给用户提示。
    """
    raw = str(rule or "").strip()
    prefix, separator, pattern = raw.partition(":")
    if not separator or prefix not in RULE_TYPES:
        raise ValueError(f"不支持的规则类型：{raw or '(空)'}")
    if not pattern:
        raise ValueError(f"规则内容为空：{raw}")
    if prefix == "regex":
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError(f"正则表达式无效：{pattern}（{e}）")
    return {"type": prefix, "pattern": pattern}


def _alternation(names):
    """把字段名拼成正则分支，长的在前（避免短名抢先匹配掉长名的前缀）。"""
    ordered = sorted(set(names), key=len, reverse=True)
    return "|".join(re.escape(n) for n in ordered)


def _strip_field_labels(text, names):
    """剥掉字段标签、保留字段值（两种写法各扫一遍）。

    ① 名字 + 冒号（中英文都认，冒号附近可有空格）→ 整段（标签）去掉；
    ② 名字后紧跟左括号（``i站地址【…】``）→ 只去掉名字本身，括号内容留给
       regex 规则处理（``regex:【.*?】`` 会连同 URL 一起清掉）。

    两遍都用 ``(?<!\\S)`` 要求字段名落在开头或空白之后：``这个作者：x`` /
    ``第2作者：x`` 这类自然语言不构成字段起点，不会被误删。
    """
    alt = _alternation(names)
    text = re.sub(
        r"(?<!\S)(?P<name>" + alt + r")\s*[：:]\s*", "", text
    )
    return re.sub(
        r"(?<!\S)(?P<name>" + alt + r")\s*(?=" + _BRACKET_OPEN + r")", "", text
    )


def _normalize_whitespace(text):
    """首尾空白去掉、连续空白（含换行）压成一个空格。

    只压空白、不动字符，所以 ``#MMD #掉装备`` 不会变成 ``#MMD#掉装备``。
    """
    return re.sub(r"\s+", " ", text).strip()


def clean_caption(caption, rules=None):
    """按规则清洗 Caption（纯函数；rules=None 时用当前生效的规则）。

    规则里出现非法项（未知前缀 / 空 pattern / 非法正则）当场跳过——一条坏
    规则绝不能让下载链路崩掉。没有匹配时原样返回（仅空白归一）。不做截断。
    """
    text = str(caption or "")
    if not text.strip():
        return ""
    if rules is None:
        rules = state.CAPTION_FILTER_RULES

    parsed = []
    for rule in rules or []:
        try:
            parsed.append(parse_caption_filter_rule(rule))
        except ValueError as e:
            logger.warning(f"跳过非法 Caption 清洗规则：{e}")

    field_names = [p["pattern"] for p in parsed if p["type"] == "field"]
    if field_names:
        text = _strip_field_labels(text, field_names)

    # 同类型之间保持用户配置的顺序；类型之间按 field → exact → contains → regex
    for rule_type in ("exact", "contains"):
        for p in parsed:
            if p["type"] == rule_type:
                text = text.replace(p["pattern"], "")
    for p in parsed:
        if p["type"] == "regex":
            text = re.sub(p["pattern"], "", text)

    return _normalize_whitespace(text)


# ============================================================
# 配置存取（runtime/caption_filter.json）
#
# 与 dedup 同构：默认规则是 config 里的常量（DEFAULT_CAPTION_FILTER_RULES），
# 当前值在 state.CAPTION_FILTER_RULES，改完立即生效（clean_caption 每次调用
# 都读 state），同时原子写盘保证重启不丢。路径与默认值都在**调用时**读
# config 模块属性，方便单测 monkeypatch（不能 from-import 成别名）。
# ============================================================
def load_caption_filter_config():
    """启动时载入持久化规则；文件缺失/损坏 → 保持默认规则。

    逐条校验，非法规则丢弃并告警（坏规则只影响自己，不拦住启动）。
    返回最终生效的规则条数。
    """
    rules = list(config.DEFAULT_CAPTION_FILTER_RULES)
    try:
        with open(config.CAPTION_FILTER_CONFIG_FILE, "r",
                  encoding="utf-8") as f:
            saved = json.load(f).get("rules")
        if isinstance(saved, list):
            rules = [str(r) for r in saved]
        else:
            logger.warning(
                "caption_filter.json 缺少 rules 列表，改用默认规则"
            )
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"读取 Caption 清洗规则失败，改用默认规则：{e}")

    valid = []
    for rule in rules:
        try:
            parse_caption_filter_rule(rule)
            valid.append(rule)
        except ValueError as e:
            logger.warning(f"丢弃非法 Caption 清洗规则：{e}")

    state.CAPTION_FILTER_RULES = valid
    return len(valid)


def save_caption_filter_config(rules):
    """原子写盘（temp + os.replace）；失败仅告警，绝不影响下载。"""
    path = config.CAPTION_FILTER_CONFIG_FILE
    tmp_path = path + ".tmp"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"rules": list(rules)}, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception as e:
        logger.warning(f"保存 Caption 清洗规则失败（不影响运行）：{e}")
        try:
            os.remove(tmp_path)
        except Exception:
            pass


def get_rules():
    """当前生效的规则（副本，防调用方就地改动绕过持久化）。"""
    return list(state.CAPTION_FILTER_RULES)


def _rule_error_text(err):
    """把规则校验错误翻译成给用户看的提示。"""
    msg = str(err)
    if "不支持的规则类型" in msg:
        return (
            "❌ 不支持的规则类型。\n\n"
            "支持：\nexact:\ncontains:\nregex:\nfield:"
        )
    return f"❌ {msg}"


def add_rule(rule):
    """新增一条规则（校验通过才保存）。返回 (ok, 提示文本)。"""
    raw = str(rule or "").strip()
    try:
        parse_caption_filter_rule(raw)
    except ValueError as e:
        return False, _rule_error_text(e)
    rules = list(state.CAPTION_FILTER_RULES)
    rules.append(raw)
    state.CAPTION_FILTER_RULES = rules
    save_caption_filter_config(rules)
    return True, f"✅ 已添加规则：\n\n{raw}"


def del_rule(index):
    """按序号（1 起）删除一条规则。返回 (ok, 提示文本)。"""
    rules = list(state.CAPTION_FILTER_RULES)
    try:
        position = int(str(index).strip())
    except (TypeError, ValueError):
        return False, "❌ 规则编号不存在。"
    if not 1 <= position <= len(rules):
        return False, "❌ 规则编号不存在。"
    removed = rules.pop(position - 1)
    state.CAPTION_FILTER_RULES = rules
    save_caption_filter_config(rules)
    return True, f"✅ 已删除规则 {position}：\n\n{removed}"


def clear_rules():
    """清空全部规则并持久化。返回 (ok, 提示文本)。"""
    state.CAPTION_FILTER_RULES = []
    save_caption_filter_config([])
    return True, "🗑 已清空全部 Caption 清洗规则"


def reset_rules():
    """恢复**默认规则**（config 的常量，不是当前值）并持久化。"""
    rules = list(config.DEFAULT_CAPTION_FILTER_RULES)
    state.CAPTION_FILTER_RULES = rules
    save_caption_filter_config(rules)
    return True, f"♻️ 已恢复默认规则（共 {len(rules)} 条）"


def rules_text():
    """规则列表视图（/caption_filter 无参与菜单共用）。"""
    rules = list(state.CAPTION_FILTER_RULES)
    if not rules:
        return "🧹 Caption 清洗规则\n\n当前没有任何规则。"
    lines = ["🧹 Caption 清洗规则", ""]
    lines += [f"{i}. {rule}" for i, rule in enumerate(rules, start=1)]
    lines += ["", f"共 {len(rules)} 条"]
    return "\n".join(lines)


def test_text(raw):
    """清洗试跑视图：走**真正的** clean_caption，不复制一套逻辑。"""
    original = str(raw or "").strip()
    result = clean_caption(original)
    return (
        "🧪 Caption 清洗测试\n\n"
        f"原文：\n{original}\n\n"
        f"结果：\n{result or '(空)'}"
    )


_CAPTION_CMD_RE = re.compile(r"^/caption_filter(?:\s|$)", re.IGNORECASE)


def is_caption_filter_command(text):
    """/caption_filter 开头的命令（含裸命令）；/caption_filters 不算。"""
    return bool(_CAPTION_CMD_RE.match(str(text or "").strip()))


def parse_caption_filter_command(text):
    """解析 /caption_filter 子命令 → (action, arg)；不是该命令返回 None。

    命令与菜单共用同一套服务函数，这里只做分流。
    """
    raw = str(text or "").strip()
    if not is_caption_filter_command(raw):
        return None
    body = raw[len("/caption_filter"):].strip()
    if not body:
        return ("list", None)
    head, _, rest = body.partition(" ")
    head = head.strip().lower()
    rest = rest.strip()
    if head in ("add", "del", "test"):
        return (head, rest) if rest else ("usage", head)
    if head in ("clear", "reset"):
        return (head, None)
    return ("usage", head)


USAGE_TEXT = (
    "❌ /caption_filter 用法：\n"
    "/caption_filter — 查看规则\n"
    "/caption_filter add <规则> — 添加\n"
    "/caption_filter del <序号> — 删除\n"
    "/caption_filter clear — 清空\n"
    "/caption_filter reset — 恢复默认\n"
    "/caption_filter test <文本> — 试清洗\n\n"
    "规则类型：exact: / contains: / regex: / field:"
)


def command_reply(action, arg):
    """执行一条子命令并返回回复文本（命令与菜单共用同一套服务函数）。"""
    if action == "list":
        return rules_text()
    if action == "add":
        return add_rule(arg)[1]
    if action == "del":
        return del_rule(arg)[1]
    if action == "clear":
        return clear_rules()[1]
    if action == "reset":
        return reset_rules()[1]
    if action == "test":
        return test_text(arg)
    return USAGE_TEXT


def input_prompt(mode, window_seconds=None):
    """bot 菜单按下「添加/删除/测试」后提示用户发文本（窗口内下一条即内容）。"""
    seconds = window_seconds or config.CAPTION_INPUT_WINDOW_SECONDS
    if mode == "add":
        return (
            "🧹 Caption 清洗 · 添加规则\n\n"
            "请直接发送要添加的规则（发到本对话）。\n"
            "例：field:作者、regex:https?://\\S+\n\n"
            f"{seconds} 秒内有效，发送 / 开头的命令可取消。"
        )
    if mode == "del":
        return (
            "🧹 Caption 清洗 · 删除规则\n\n"
            f"请直接发送要删除的规则编号（1-{len(state.CAPTION_FILTER_RULES)}）。\n\n"
            f"{seconds} 秒内有效，发送 / 开头的命令可取消。"
        )
    return (
        "🧪 Caption 清洗 · 测试清洗\n\n"
        "请直接发送要试清洗的原文（发到本对话）。\n\n"
        f"{seconds} 秒内有效，发送 / 开头的命令可取消。"
    )
