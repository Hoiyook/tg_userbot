"""备忘录（memo.py）—— /memo：随手记、随时查。

数据：RUNTIME_DIR/memo.json，{"memos": [{"id", "ts", "text"}…]}（原子写）。
id 为自增序号（删除后不复用），列表按 id 升序展示、新的在后。
"""
import json
import os
import time

from . import config
from .log import logger

MEMO_FILE = config.MEMO_FILE

_MEMOS = []          # [{"id", "ts", "text"}]，按 id 升序
_NEXT_ID = 1


def _load():
    global _MEMOS, _NEXT_ID
    try:
        with open(MEMO_FILE, encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("memos") or []
        _MEMOS = [m for m in items
                  if isinstance(m, dict) and m.get("id") and m.get("text")]
        _NEXT_ID = (max((m["id"] for m in _MEMOS), default=0) + 1)
    except FileNotFoundError:
        _MEMOS = []
        _NEXT_ID = 1
    except Exception as e:
        logger.warning(f"📝 读取备忘录失败，从空开始：{e}")
        _MEMOS = []
        _NEXT_ID = 1


def _save():
    tmp = MEMO_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"memos": _MEMOS}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, MEMO_FILE)
        return True
    except Exception as e:
        logger.warning(f"📝 保存备忘录失败：{e}")
        return False


def load_memo():
    """启动载入（app.main 调用）。返回条数。"""
    _load()
    return len(_MEMOS)


def add(text):
    """追加一条备忘。返回 (是否成功, 提示)。"""
    text = str(text or "").strip()
    if not text:
        return False, "❌ 备忘内容不能为空"
    global _NEXT_ID
    mid = _NEXT_ID
    _NEXT_ID += 1
    _MEMOS.append({
        "id": mid,
        "ts": time.strftime("%Y-%m-%d %H:%M"),
        "text": text,
    })
    ok = _save()
    if not ok:
        # 落盘失败：回滚内存（数据准确性优先——不假成功）
        _MEMOS[:] = [m for m in _MEMOS if m["id"] != mid]
        return False, "❌ 保存失败（磁盘写入出错），备忘未记录"
    return True, f"📝 已记录 #{mid}（共 {len(_MEMOS)} 条）"


def delete(mid):
    """按序号删除。返回 (是否成功, 提示)。"""
    try:
        mid = int(mid)
    except (TypeError, ValueError):
        return False, "❌ 序号须为数字（/memo 查看列表）"
    before = len(_MEMOS)
    _MEMOS[:] = [m for m in _MEMOS if m["id"] != mid]
    if len(_MEMOS) == before:
        return False, f"❌ 没有序号 {mid} 的备忘"
    if _save():
        return True, f"🗑 已删除 #{mid}（剩 {len(_MEMOS)} 条）"
    return False, "❌ 删除后保存失败（已回滚），请重试"


def clear():
    """清空全部备忘。返回 (剩余条数=0, 提示)。"""
    n = len(_MEMOS)
    _MEMOS.clear()
    if _save():
        return True, f"🧹 已清空 {n} 条备忘"
    return False, "❌ 清空后保存失败（已回滚）"


def list_text(limit=30):
    """备忘列表（新的在后；超量显示最近 limit 条）。"""
    if not _MEMOS:
        return "📝 备忘录：空\n用法：/memo 内容 —— 随手记一条"
    shown = _MEMOS[-limit:]
    lines = [f"📝 备忘录（共 {len(_MEMOS)} 条，显示最近 {len(shown)} 条）：", ""]
    for m in shown:
        lines.append(f"#{m['id']} {m['ts']}")
        lines.append(f"   {m['text'][:200]}")
    lines.append("")
    lines.append("删除：/memo del 序号｜清空：/memo clear")
    return "\n".join(lines)
