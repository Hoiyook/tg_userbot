"""命令模板 —— 🖥 命令行面板的常用命令收藏（增删改查 + 菜单按钮执行）。

与 sql_templates.py 同构：模板是**配置** → JSON（``runtime/command_templates.json``，
名字 → shell 命令原文，dict 保序即列表顺序）；执行统一走 shell.command_reply
（黑名单/超时/工作目录全套纪律都在那边，本模块不做第二套执行器）。

名字规则（validate_name）：1-16 字符，中文/字母/数字/下划线；add/del/list 是
命令保留字。16 字符上限来自 Telegram 回调数据 64 字节——「▶️ 执行/🗑 删除」
按钮直接带名字，序号会漂移、名字不会。
"""
import json
import os
import re

from . import config
from . import state
from .log import logger

_RESERVED = ("add", "del", "list", "help")
_NAME_MAX = 16
_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff]{1,16}$", re.UNICODE)

CMDT_TEXT_PREFIX = "📜 命令模板"


def _config_path():
    return getattr(config, "CMD_TEMPLATES_FILE",
                   os.path.join(config.RUNTIME_DIR, "command_templates.json"))


def load_cmd_templates():
    """启动时载入 command_templates.json 到 state.CMD_TEMPLATES，返回条数。

    文件缺失/损坏 → 空模板（绝不抛——模板只是便利功能，起不来不影响主程序）。
    """
    path = _config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        templates = data.get("templates") or {}
        if not isinstance(templates, dict):
            raise ValueError("templates 不是对象")
    except FileNotFoundError:
        state.CMD_TEMPLATES = {}
        return 0
    except Exception as e:
        logger.warning(f"📜 读取命令模板失败，改用空模板：{e}")
        state.CMD_TEMPLATES = {}
        return 0
    state.CMD_TEMPLATES = {str(k): str(v) for k, v in templates.items()}
    return len(state.CMD_TEMPLATES)


def save_cmd_templates():
    """把 state.CMD_TEMPLATES 原子写盘（temp + os.replace）；失败仅告警。"""
    path = _config_path()
    tmp = path + ".tmp"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"templates": state.CMD_TEMPLATES},
                      f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.warning(f"📜 保存命令模板失败：{e}")
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


def validate_name(name):
    """名字校验。返回 (是否合法, 提示)。"""
    name = str(name or "").strip()
    if not name:
        return False, "❌ 模板名不能为空"
    if len(name) > _NAME_MAX:
        return False, f"❌ 模板名最长 {_NAME_MAX} 个字符"
    if not _NAME_RE.match(name):
        return False, "❌ 模板名只能用中文/字母/数字/下划线"
    if name.lower() in _RESERVED:
        return False, f"❌ {name} 是保留字，换个名字"
    return True, ""


def upsert(name, command):
    """新增或覆盖（改）一条模板。返回 (是否成功, 提示)。"""
    ok, msg = validate_name(name)
    if not ok:
        return False, msg
    command = str(command or "").strip()
    if not command:
        return False, "❌ 命令不能为空"
    existed = str(name).strip() in state.CMD_TEMPLATES
    state.CMD_TEMPLATES[str(name).strip()] = command
    save_cmd_templates()
    verb = "已覆盖" if existed else "已保存"
    logger.info(f"📜 命令模板{verb}：{name}")
    return True, f"{verb}模板「{name}」"


def delete(name):
    """删除一条模板。返回 (是否成功, 提示)。"""
    name = str(name or "").strip()
    if name not in state.CMD_TEMPLATES:
        return False, f"❌ 没有叫「{name}」的模板（/cmdt 查看列表）"
    state.CMD_TEMPLATES.pop(name)
    save_cmd_templates()
    logger.info(f"📜 命令模板已删除：{name}")
    return True, f"已删除模板「{name}」"


def get(name):
    """取模板的命令原文；不存在返回 None。"""
    return state.CMD_TEMPLATES.get(str(name or "").strip())


def names():
    return list(state.CMD_TEMPLATES.keys())


def list_text():
    """模板列表视图（命令与菜单共用）。"""
    items = state.CMD_TEMPLATES
    if not items:
        return (f"{CMDT_TEXT_PREFIX}：空\n\n"
                "用法：/cmdt add <名字> <命令> —— 把常用命令存起来\n"
                "之后 /cmdt <名字> 直接执行")
    lines = [f"{CMDT_TEXT_PREFIX}：{len(items)} 条", ""]
    for i, (name, cmd) in enumerate(items.items(), start=1):
        preview = cmd if len(cmd) <= 60 else cmd[:59] + "…"
        lines.append(f"{i}. {name}")
        lines.append(f"   {preview}")
    lines.append("")
    lines.append("执行：/cmdt <名字>｜保存/覆盖：/cmdt add <名字> <命令>"
                 "｜删除：/cmdt del <名字>")
    return "\n".join(lines)


# ============================================================
# 命令解析
# ============================================================
_CMDT_CMD_RE = re.compile(r"^/cmdt(?:\s|$)", re.IGNORECASE)


def is_cmdt_command(text) -> bool:
    """/cmdt 开头（含裸命令）；/cmdtfoo 不算。"""
    return bool(_CMDT_CMD_RE.match(str(text or "").strip()))


def parse_cmdt_command(text):
    """/cmdt 子命令 → (action, arg)：list / add / del / run。"""
    raw = str(text or "").strip()
    body = raw[len("/cmdt"):].strip()
    if not body:
        return ("list", None)
    head, _, rest = body.partition(" ")
    head_l = head.lower()
    if head_l in ("list", "help"):
        return ("list", None)
    if head_l == "add":
        return ("add", rest.strip())
    if head_l == "del":
        return ("del", rest.strip())
    # 其余首 token 当模板名执行（多余 token 忽略）
    return ("run", head)


async def execute(name):
    """按名字执行模板。返回 (模板是否存在, 执行结果文本)。

    执行走 shell.command_reply——黑名单/超时/工作目录全套纪律在那边。
    """
    cmd = get(name)
    if cmd is None:
        return False, f"❌ 没有叫「{name}」的模板（/cmdt 查看列表）"
    from . import shell
    return True, await shell.command_reply(f"/sh {cmd}")


# ============================================================
# 菜单按钮（模板视图：每行 ▶️ 执行 + 🗑 删除）
# ============================================================
def menu_buttons():
    """模板视图按钮组。名字直接进回调数据（≤16 字符，64 字节限内）。"""
    from telethon import Button
    from .menu import encode_menu_data   # 函数内导入避免 menu↔本模块成环
    rows = [[Button.inline("➕ 新增模板", encode_menu_data("cmdt_add"))]]
    for name in names():
        rows.append([
            Button.inline(f"▶️ {name}", encode_menu_data("cmdt_run", name)),
            Button.inline("🗑", encode_menu_data("cmdt_del", name)),
        ])
    rows.append([Button.inline("🔙 返回主菜单", encode_menu_data("home"))])
    return rows
