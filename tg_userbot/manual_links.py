"""手动外链台账（2026-09-16，schema v9）——移动端外链的登记与查重。

场景：在外面自己找到网盘链接（MEGA 等）处理掉了/准备处理，把链接发给
bot 记一笔（默认 PENDING 未处理）；人工处理完手动标 DONE；**以后再发
同链接时自动查重提示**，防止重复处理。查重两个数据源：本台账历史 +
Pawchive 已完成帖子的外链（pawchive.find_completed_ext_link）。

入口：收藏夹/bot 菜单发含 http(s) 链接的文本 → observe()（记录 + 查重
回复）；/links → links_view()（未处理清单 + ✅ 按钮）；✅ 点击 →
mlink_done 回调 → done_reply()（原地刷新）。/paw done 标记完成的
Pawchive 帖子，其外链自动进入查重范围——两套记录互通。
"""
import re

from telethon import Button

from . import pawchive
from . import runtime_db
from . import state
from .log import logger

TEXT_PREFIX = "🔗 外链台账"

# 与 pawchive 裸 URL 补扫同一套字符白名单 + 尾部修剪（URL 尾随句读）
_URL_RE = re.compile(
    r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+", re.I)


def extract_urls(text):
    """从消息文本提取外链（去重保序；尾部标点修剪复用 pawchive 的规则）。"""
    urls, seen = [], set()
    for m in _URL_RE.finditer(str(text or "")):
        url = pawchive._trim_url_tail(m.group(0))
        if url.lower() in seen:
            continue
        seen.add(url.lower())
        urls.append(url)
    return urls


def _short(url, limit=40):
    """按钮/回复里的链接短显：host + 路径尾段。"""
    host = url.split("//", 1)[-1].split("/", 1)[0]
    rest = url.split("//", 1)[-1].split("/", 1)
    tail = ("/" + rest[1]) if len(rest) > 1 else ""
    body = host + (tail[-(limit - len(host) - 1):] if tail else "")
    return body if len(body) <= limit else body[:limit - 1] + "…"


def observe(urls, now=None):
    """登记一批链接并生成查重回复（文本, 按钮行）。

    每条链接的判定优先级：台账 DONE（人工标记过）→ Pawchive 已完成
    帖子外链 → 台账 PENDING（已在记录中）→ 新记录。仅新记录挂 ✅ 按钮
    （当时就能点完成）；已在 paw 完成的不重复入台账。"""
    from .menu import encode_menu_data   # 函数内导入避免 menu↔本模块成环
    lines = [f"{TEXT_PREFIX}：{len(urls)} 条"]
    rows = []
    for url in urls:
        host = url.split("//", 1)[-1].split("/", 1)[0].lower()
        # 先查 Pawchive 已完成外链：命中即报告，不重复入台账（paw 的
        # 帖子记录就是它的账；/paw done 标记完成过的自动覆盖）
        paw_post = pawchive.find_completed_ext_link(url)
        if paw_post:
            lines.append(
                f"⚠️ Pawchive 外链已完成："
                f"[{paw_post.get('creator_name')}] #{paw_post['id']} {url}")
            continue
        state_str, row = runtime_db.manual_link_add(
            url, host=host, now=now)
        if state_str == "done":
            done = row.get("done_at")
            done_str = ""
            if done:
                from datetime import datetime
                done_str = f"（{datetime.fromtimestamp(done):%m-%d %H:%M} 标记完成）"
            lines.append(f"⚠️ 已处理过：[{row['host'] or host}] {row['url']}"
                         + done_str)
        elif state_str == "pending":
            lines.append(f"ℹ️ 已在记录中（未处理）：[{row['host'] or host}] "
                         f"{row['url']}")
        else:
            lines.append(f"🔗 已记录（未处理）：[{row['host'] or host}] "
                         f"{row['url']}")
            rows.append([Button.inline(
                "✅ " + _short(row["url"]),
                encode_menu_data("mlink_done", str(row["id"])))])
    return "\n".join(lines), rows


def links_view(limit=20):
    """/links 视图：未处理清单（✅ 按钮逐条）+ 已完成计数。"""
    from .menu import encode_menu_data   # 函数内导入避免 menu↔本模块成环
    try:
        pending = runtime_db.list_manual_links(
            status="PENDING", limit=limit)
    except runtime_db.DbUnavailable as e:
        return f"{TEXT_PREFIX}\n❌ Runtime DB 不可用：{e}", []
    done_n = runtime_db.count_manual_links(status="DONE")
    if not pending:
        return (f"{TEXT_PREFIX}：未处理 0 条（已完成 {done_n} 条）\n"
                "把网盘链接直接发给我即可登记。", [])
    lines = [f"{TEXT_PREFIX}：未处理 {len(pending)} 条（已完成 {done_n} 条）",
             ""]
    rows = []
    for row in pending:
        lines.append(f"#{row['id']} [{row['host']}] {row['url']}")
        rows.append([Button.inline(
            "✅ " + _short(row["url"]),
            encode_menu_data("mlink_done", str(row["id"])))])
    return "\n".join(lines), rows


async def done_reply(arg):
    """mlink_done 回调 / 面板按钮：标记完成 + 刷新台账视图（原地 edit）。"""
    raw = str(arg or "").strip()
    if raw.isdigit():
        try:
            ok = runtime_db.manual_link_done(int(raw))
        except runtime_db.DbUnavailable as e:
            return f"{TEXT_PREFIX}\n❌ Runtime DB 不可用：{e}", []
        if not ok:
            return f"{TEXT_PREFIX}\nℹ️ #{raw} 已标记过或不存在", []
        logger.info(f"🔗 手动外链 #{raw} 标记完成")
    view_text, buttons = links_view()
    head = f"✅ 已标记完成：#{raw}" if raw.isdigit() else f"❌ 无效的链接 id：{raw!r}"
    return f"{TEXT_PREFIX}\n{head}\n\n{view_text}", buttons
