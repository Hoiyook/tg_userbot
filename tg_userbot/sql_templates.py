"""SQL 模板 —— /sql 诊断控制台的常用查询收藏（增删改查 + 菜单按钮）。

数据分层：模板是**配置** → JSON（``runtime/sql_templates.json``，名字 → SQL
原文，dict 保序即列表顺序）；执行仍走 ``runtime_db.execute_user_sql``（权限
全放开、错误当结果——与 /sql 完全同一套语义），本模块不做第二套执行器。

名字规则（``validate_name``）：1-16 字符，中文/字母/数字/下划线；``add``/
``del``/``list`` 是命令保留字不可用作名字。16 字符的上限来自 Telegram
回调数据 64 字节——菜单里「▶️ 执行/🗑 删除」按钮直接带名字（中文 16 字
= 48 字节 + 前缀仍在限内），序号会漂移、名字不会（与 /chrome_cancel 用
task_id 同一个理由）。

「改」= 同名 upsert 覆盖（命令与菜单的 ➕ 输入同名即改），不单设编辑入口。
"""
import json
import os
import re

from telethon import Button

from . import config
from . import runtime_db
from . import state
from .log import logger

# 名字保留字（parse 的动作字）与长度上限
_RESERVED = ("add", "del", "list", "help")
_NAME_MAX = 16
_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff]{1,16}$", re.UNICODE)

SQLT_TEXT_PREFIX = "📋 SQL 模板"     # 以「📋 SQL」开头，落在既有清理白名单内


def _config_path():
    return getattr(config, "SQL_TEMPLATES_FILE",
                   os.path.join(config.RUNTIME_DIR, "sql_templates.json"))


def load_sql_templates():
    """启动时载入 sql_templates.json 到 state.SQL_TEMPLATES，返回条数。

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
        state.SQL_TEMPLATES = {}
        return 0
    except Exception as e:
        logger.warning(f"📋 读取 SQL 模板失败，改用空模板：{e}")
        state.SQL_TEMPLATES = {}
        return 0
    state.SQL_TEMPLATES = {str(k): str(v) for k, v in templates.items()}
    return len(state.SQL_TEMPLATES)


def save_sql_templates():
    """把 state.SQL_TEMPLATES 原子写盘（temp + os.replace）；失败仅告警。"""
    path = _config_path()
    tmp = path + ".tmp"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"templates": state.SQL_TEMPLATES},
                      f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.warning(f"📋 保存 SQL 模板失败：{e}")
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


def upsert(name, sql):
    """新增或覆盖（改）一条模板。返回 (是否成功, 提示)。"""
    ok, msg = validate_name(name)
    if not ok:
        return False, msg
    sql = str(sql or "").strip()
    if not sql:
        return False, "❌ SQL 不能为空"
    existed = str(name).strip() in state.SQL_TEMPLATES
    state.SQL_TEMPLATES[str(name).strip()] = sql
    save_sql_templates()
    verb = "已覆盖" if existed else "已保存"
    logger.info(f"📋 SQL 模板{verb}：{name}")
    return True, f"{verb}模板「{name}」"


def delete(name):
    """删除一条模板。返回 (是否成功, 提示)。"""
    name = str(name or "").strip()
    if name not in state.SQL_TEMPLATES:
        return False, f"❌ 没有叫「{name}」的模板（/sqlt 查看列表）"
    state.SQL_TEMPLATES.pop(name)
    save_sql_templates()
    logger.info(f"📋 SQL 模板已删除：{name}")
    return True, f"已删除模板「{name}」"


def get(name):
    """取模板的 SQL 原文；不存在返回 None。"""
    return state.SQL_TEMPLATES.get(str(name or "").strip())


def names():
    return list(state.SQL_TEMPLATES.keys())


def list_text():
    """模板列表视图（命令与菜单共用）。"""
    items = state.SQL_TEMPLATES
    if not items:
        return (f"{SQLT_TEXT_PREFIX}：空\n\n"
                "用法：/sqlt add <名字> <SQL> —— 把常用查询存起来\n"
                "之后 /sqlt <名字> 直接执行")
    lines = [f"{SQLT_TEXT_PREFIX}：{len(items)} 条", ""]
    for i, (name, sql) in enumerate(items.items(), start=1):
        preview = sql if len(sql) <= 60 else sql[:59] + "…"
        lines.append(f"{i}. {name}")
        lines.append(f"   {preview}")
    lines.append("")
    lines.append("执行：/sqlt <名字>｜保存/覆盖：/sqlt add <名字> <SQL>"
                 "｜删除：/sqlt del <名字>")
    return "\n".join(lines)


# ============================================================
# 命令解析
# ============================================================
_SQLT_CMD_RE = re.compile(r"^/sqlt(?:_\w+)?(?:\s|$)", re.IGNORECASE)


def is_sqlt_command(text) -> bool:
    """/sqlt 开头（含裸命令）；/sqltfoo 与 /sql 都不算。"""
    return bool(_SQLT_CMD_RE.match(str(text or "").strip()))


def parse_sqlt_command(text):
    """/sqlt 子命令 → (action, arg)：list / add / del / run。"""
    raw = str(text or "").strip()
    body = raw[len("/sqlt"):].strip()
    if not body:
        return ("list", None)
    head, _, rest = body.partition(" ")
    head_l = head.lstrip("_").lower()   # /sqlt_add（标准）兼容
    if head_l in ("list", "help"):
        return ("list", None)
    if head_l == "add":
        return ("add", rest.strip())
    if head_l == "del":
        return ("del", rest.strip())
    # 其余首 token 当模板名执行（多余 token 忽略）
    return ("run", head)


def execute_template(name):
    """按名字执行模板。返回 (模板是否存在, 执行结果 dict 或错误提示)。"""
    sql = get(name)
    if sql is None:
        return False, f"❌ 没有叫「{name}」的模板（/sqlt 查看列表）"
    return True, runtime_db.execute_user_sql(sql)


# ============================================================
# 菜单按钮（模板视图：每行 ▶️ 执行 + 🗑 删除）
# ============================================================
def menu_buttons():
    """模板视图按钮组。名字直接进回调数据（≤16 字符，64 字节限内）。"""
    from .menu import encode_menu_data   # 函数内导入避免 menu↔本模块成环
    rows = [[Button.inline("➕ 新增模板", encode_menu_data("sqlt_add")),
             Button.inline("🖥 SQL控制台", encode_menu_data("sql_console"))]]
    for name in names():
        rows.append([
            Button.inline(f"▶️ {name}", encode_menu_data("sqlt_run", name)),
            Button.inline("🗑", encode_menu_data("sqlt_del", name)),
        ])
    rows.append([Button.inline("🔙 返回主菜单", encode_menu_data("home"))])
    return rows
