"""Iwara 视频下载支持（2026-09-28）：iwara.tv 全站 Cloudflare 防护，
服务端裸 HTTP 一律 403；但 CDN 签名直链**绑定浏览器会话**。因此获取
直链必须在 Agent 的 Chrome（CDP）里完成，下载也交给 Chrome（Agent
现有下载事件监听接住），复用全部现有架构。

流程（process_iwara_url）：
  CDP 打开视频页 → 页面内 fetch api.iwara.tv/video/<id> → fileUrl →
  版本清单 → 选最高画质（排除 preview）→ Page.navigate 到 download
  直链 → Agent 的 downloadWillBegin/downloadProgress 现有监听接住 →
  成品落 TG Chrome Download 目录。
"""
import json
import re

from .log import logger

IWARA_VIDEO_RE = re.compile(
    r"https?://(?:www\.)?iwara\.tv/video/([A-Za-z0-9]+)", re.IGNORECASE)


def extract_iwara_video_id(url):
    """iwara 视频页 URL → 视频 id；非 iwara 视频链接返回 None。"""
    m = IWARA_VIDEO_RE.match(str(url or "").strip())
    return m.group(1) if m else None


_FETCH_VERSIONS_JS = """
(async (videoId) => {
  const r = await fetch('https://api.iwara.tv/video/' + videoId);
  if (!r.ok) return JSON.stringify({error: 'video ' + r.status});
  const j = await r.json();
  const r1 = await fetch(j.fileUrl);
  if (!r1.ok) return JSON.stringify({error: 'fileUrl ' + r1.status});
  const versions = await r1.json();
  const cand = versions.filter(v => v.name !== 'preview');
  const best = cand.length ? cand[cand.length - 1] : versions[0];
  return JSON.stringify({
    best: best.name,
    url: 'https:' + best.src.download,
    title: (j.title || '').slice(0, 80),
    author: j.user ? j.user.name : '',
  });
})(%s)
"""


async def resolve_best_download_url(cdp, video_id):
    """在 CDP 页面上下文里解析 iwara 视频的最高画质下载直链。

    返回 (download_url, title, quality_name)；失败抛 RuntimeError。"""
    expr = _FETCH_VERSIONS_JS % json.dumps(video_id)
    result = await cdp.command("Runtime.evaluate", {
        "expression": expr, "awaitPromise": True, "returnByValue": True})
    value = (result.get("result") or {}).get("value")
    if not value:
        raise RuntimeError("iwara 页面脚本无返回（页面未加载完？）")
    info = json.loads(value)
    if "error" in info:
        raise RuntimeError(f"iwara API：{info['error']}")
    if not info.get("url"):
        raise RuntimeError("iwara 版本清单无下载直链")
    return info["url"], info.get("title") or "", info.get("best") or ""
