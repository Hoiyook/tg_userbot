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


def _split_note(text):
    """消息文本 → (urls, note)：恰好 1 个 URL 时其余文本是备注。

    多 URL 时备注归属有歧义 → 全部无备注（note=None）。备注取 URL 之外的
    全部文本（前后皆可），空白归一；无剩余文本 → None。"""
    urls = extract_urls(text)
    if not urls:
        return [], None
    if len(urls) > 1:
        return urls, None
    rest = str(text or "").replace(urls[0], " ")
    note = " ".join(rest.split())
    return urls, (note or None)


def observe(text, now=None):
    """登记一条消息里的链接并生成查重回复（文本, 按钮行）。

    支持「URL + 备注」（恰好 1 个 URL 时其余文本为备注；重发同链接带新
    备注 → 更新备注，状态不变）。每条链接的判定优先级：台账 DONE →
    Pawchive 已完成外链 → 台账 PENDING → 新记录。仅新记录挂 ✅ 按钮。"""
    from .menu import encode_menu_data   # 函数内导入避免 menu↔本模块成环
    urls, note = _split_note(text)
    if not urls:
        return f"{TEXT_PREFIX}\n（未识别到链接）", []
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
            url, host=host, note=note, now=now)
        host_disp = row["host"] or host
        note_disp = f"\n📝 {row['note']}" if row.get("note") else ""
        if state_str == "note_updated" and row["status"] == "DONE":
            state_str = "done"      # 已完成链接的新备注：留档但仍报已处理过
        if state_str == "done":
            done = row.get("done_at")
            done_str = ""
            if done:
                from datetime import datetime
                done_str = (f"（{datetime.fromtimestamp(done):%m-%d %H:%M}"
                            " 标记完成）")
            lines.append(f"⚠️ 已处理过：[{host_disp}] {row['url']}{done_str}"
                         + note_disp)
        elif state_str == "note_updated":
            lines.append(f"🔁 备注已更新：[{host_disp}] {row['url']}"
                         + note_disp)
        elif state_str == "pending":
            lines.append(f"ℹ️ 已在记录中（未处理）：[{host_disp}] "
                         f"{row['url']}" + note_disp)
        else:
            lines.append(f"🔗 已记录（未处理）：[{host_disp}] "
                         f"{row['url']}" + note_disp)
            rows.append([Button.inline(
                "✅ " + _short(row["url"]),
                encode_menu_data("mlink_done", str(row["id"])))])
    return "\n".join(lines), rows


def links_view(limit=20, keyword=None):
    """/links 视图：无参 = 未处理清单（✅ 按钮逐条）+ 已完成计数；
    带关键词 = 搜索（备注或 URL 子串、大小写不敏感、含已完成——
    按备注找回外链，2026-09-17 需求），命中行带终态标记与 ✅（未处理）。"""
    from .menu import encode_menu_data   # 函数内导入避免 menu↔本模块成环
    try:
        if keyword and str(keyword).strip():
            hits = runtime_db.search_manual_links(keyword, limit=limit)
            if not hits:
                return (f"{TEXT_PREFIX}：关键词「{keyword.strip()}」无匹配"
                        "（搜备注与链接）", [])
            lines = [f"{TEXT_PREFIX}：搜「{keyword.strip()}」命中 "
                     f"{len(hits)} 条", ""]
            rows = []
            for row in hits:
                mark = "✅ 已处理" if row["status"] == "DONE" else "⏳ 未处理"
                lines.append(f"#{row['id']} {mark} [{row['host']}] "
                             f"{row['url']}")
                if row.get("note"):
                    lines.append(f"📝 {row['note']}")
                if row["status"] == "PENDING":
                    rows.append([Button.inline(
                        "✅ " + _short(row["url"]),
                        encode_menu_data("mlink_done", str(row["id"])))])
            return "\n".join(lines), rows
        pending = runtime_db.list_manual_links(
            status="PENDING", limit=limit)
    except runtime_db.DbUnavailable as e:
        return f"{TEXT_PREFIX}\n❌ Runtime DB 不可用：{e}", []
    done_n = runtime_db.count_manual_links(status="DONE")
    if not pending:
        return (f"{TEXT_PREFIX}：未处理 0 条（已完成 {done_n} 条）\n"
                "把网盘链接直接发给我即可登记；/links 关键词 可按备注搜历史。",
                [])
    lines = [f"{TEXT_PREFIX}：未处理 {len(pending)} 条（已完成 {done_n} 条）",
             ""]
    rows = []
    for row in pending:
        lines.append(f"#{row['id']} [{row['host']}] {row['url']}")
        if row.get("note"):
            lines.append(f"📝 {row['note']}")
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
