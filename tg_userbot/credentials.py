"""凭据健康度巡检（2026-11-12）。

四个站点凭据失效的表现各不相同，但共同点是**只在用时才暴露**：
douyin cookie 失效 = /dyu 与本地解析 403；ehentai 失效 = 种子页进
登录流；pawchive 失效 = 扫描全拒；bilibili 失效 = 画质掉档。

巡检设计为**零联网**（每日维护跑，绝不主动打站点）：
- 只验字段完整性——登录态 cookie 的关键缺一不可的字段（如 douyin 的
  sessionid、ehentai 的 ipb_pass_hash）。写坏/清空/残缺粘贴在此刻暴露，
  而不是等下次下载失败。
- 结果落 schema_meta（credentials_last_check / 每站状态），供 /health
  与每日维护对比：**与上次不同的新失效**才发 TG 告警（每日一次天然节流，
  状态没变化不重复吵）。
- 活体探测（真打站点验证）刻意不做：douyin 探测本身可能触发风控。
"""
import re
import time

from .log import logger

# 各站 cookie 的"登录态存在"判据：至少包含这些字段之一（=1 才算有值）
# cookie 串按 "; " 分段、name= 不分大小写。
_SITE_RULES = (
    ("douyin", "DOUYIN_COOKIE",
     ("sessionid", "sessionid_ss"), "抖音（/dyu 与本地解析）"),
    ("ehentai", "EHENTAI_COOKIE",
     ("ipb_member_id", "ipb_pass_hash"), "ehentai（/ehentai 种子/原包）"),
    ("pawchive", "PAWCHIVE_COOKIE",
     ("session",), "pawchive（/paw 扫描）"),
    ("bilibili", "BILIBILI_COOKIE",
     ("sessdata",), "B站画质档位（/bili）"),
)


def _cookie_fields(cookie):
    """cookie 串 → {小写字段名: 值}；空段/无等号段跳过。"""
    out = {}
    for part in str(cookie or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name and value:
            out[name.strip().lower()] = value.strip()
    return out


def check_site(site, attr, required, display):
    """单站巡检 → {site, display, ok, detail}。

    cookie 缺失 = ok（该站功能未启用是合法状态，不算故障）；
    cookie 存在但关键登录态字段全缺 = ❌ 残缺（多半是手滑粘了半截）。"""
    cookie = getattr_config(attr)
    if not cookie.strip():
        return {"site": site, "display": display, "ok": True,
                "detail": "未配置（功能未启用）", "configured": False}
    fields = _cookie_fields(cookie)
    missing = [k for k in required
               if not any(f == k.lower() or f.startswith(k.lower())
                          for f in fields)]
    if len(missing) == len(required):
        return {"site": site, "display": display, "ok": False,
                "configured": True,
                "detail": f"cookie 残缺：缺少登录态字段 {missing[0]}"
                          + (f" 等 {len(missing)} 项" if len(missing) > 1 else "")
                          + "——下载时会 403，请重取"}
    return {"site": site, "display": display, "ok": True, "configured": True,
            "detail": f"字段完整（{len(fields)} 项）"}


def getattr_config(attr):
    from . import config as _config
    return getattr(_config, attr, "")


def check_all():
    """全站巡检 → 列表 [{site, display, ok, detail, configured}]。"""
    out = []
    for site, attr, required, display in _SITE_RULES:
        try:
            out.append(check_site(site, attr, required, display))
        except Exception as e:
            out.append({"site": site, "display": display, "ok": False,
                        "configured": True,
                        "detail": f"巡检异常：{type(e).__name__}: {str(e)[:60]}"})
    return out


def _status_key(results):
    """失效站集合 → 稳定 key（'-' 表示全好）。"""
    bad = sorted(r["site"] for r in results if not r["ok"])
    return ",".join(bad) if bad else "-"


def daily_check_and_alert(notify_coro=None):
    """每日维护挂钩：巡检 → 结果落 schema_meta → **新增失效**才告警。

    告警语义（增量，不重复吵）：
    - 恢复某站 / 全部持续失效 → 不告警（状态没变化）
    - 上轮完好、本轮失效的站 → 告警（列全部失效项，标明新增）
    notify_coro: async callable(text)（注入 notify.notify_user，测试可替）。
    返回 (results, alerted_text|None)。失败不抛（维护主流程不许被它打断）。"""
    from . import runtime_db
    results = check_all()
    bad_sites = sorted(r["site"] for r in results if not r["ok"])
    prev = runtime_db.get_schema_meta("credentials_last_bad") or "-"
    prev_bad = set(prev.split(",")) if prev and prev != "-" else set()
    new_bad = [s for s in bad_sites if s not in prev_bad]
    try:
        runtime_db.set_schema_meta(
            "credentials_last_check", str(int(time.time())))
        runtime_db.set_schema_meta(
            "credentials_last_bad", ",".join(bad_sites) if bad_sites else "-")
    except Exception as e:
        logger.warning(f"凭据巡检结果写库失败（忽略）：{e}")
    if new_bad:
        bad_rows = [r for r in results if not r["ok"]]
        bad_lines = [f"  ❌ {r['display']}：{r['detail']}"
                     + ("（🆕 新失效）" if r["site"] in new_bad else "")
                     for r in bad_rows]
        text = ("🔑 凭据巡检发现失效站点\n\n" + "\n".join(bad_lines)
                + "\n\n（每日巡检一次；处理前每次下载该站都会失败）")
        if notify_coro is not None:
            try:
                # daily_maintenance 在 to_thread 线程里跑（无事件循环）——
                # asyncio.run 新建独立 loop，不碰主线程的
                import asyncio
                asyncio.run(notify_coro(text))
            except Exception as e:
                logger.warning(f"凭据失效告警发送失败（忽略）：{e}")
        return results, text
    return results, None


def health_line():
    """/health 集成行：一行汇总（全好 / 列失效站）。返回 None=无事。"""
    results = check_all()
    bad = [r for r in results if not r["ok"]]
    if not bad:
        return None
    return "🔑 凭据：" + "；".join(
        f"{r['display']} {r['detail'][:40]}" for r in bad)
