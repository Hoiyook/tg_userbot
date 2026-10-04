"""Iwara 视频下载支持（2026-09-28 调研定稿）。

机制（全部实测验证）：
- iwara 全站 Cloudflare：服务端裸 HTTP 一律 403，必须经 Agent 的 Chrome
- 视频 API 的 fileUrl 清单（fetch 复刻）只回 360/preview——**与 SPA 首屏
  拿到的完整清单（含 540/Source）不一致**，差异在服务端按请求特征分发，
  外部无法观测也无法复刻
- 但 SPA 播放器的画质菜单（齿轮 → Source）真实可点，切换后
  video.currentSrc 即**原画直链**（lumi/kafka.iwara.tv 的 view URL），
  view→download 同 hash 转换即为下载直链（206 + video/mp4 已验证）

因此 resolve 走「真实 UI 复刻」：开视频页 → 点齿轮 → 点 Source →
读 video.currentSrc → view 转 download。这不是脆弱的 hack——它复刻的
就是用户手动下载的精确路径。
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


# 齿轮按钮（播放器设置）的 svg path 指纹与画质项匹配规则
_GEAR_PATH_PREFIX = "M487.4 315.7"
_QUALITY_ORDER = ("Source", "1080", "720", "540", "360")

# 页面内脚本：点齿轮开菜单 → 依序尝试画质（Source 优先）→ 读播放源
_RESOLVE_JS = """
(async (qualities) => {
  const findBtn = (prefix) => [...document.querySelectorAll('button')].find(b => {
    const p = b.querySelector('svg path');
    return p && (p.getAttribute('d') || '').startsWith(prefix);
  });
  const gear = findBtn('%(gear)s');
  if (!gear) return JSON.stringify({error: 'no-gear'});
  gear.click();
  await new Promise(r => setTimeout(r, 600));
  for (const q of qualities) {
    const item = [...document.querySelectorAll('li, [role=menuitem], button')]
      .find(e => (e.textContent || '').trim().toLowerCase() === q.toLowerCase());
    if (!item) continue;
    item.click();
    await new Promise(r => setTimeout(r, 1500));
    const v = document.querySelector('video');
    const src = v ? (v.currentSrc || '') : '';
    if (src) {
      // 元数据从 video API 抓（页面上下文 fetch 一直 200；DOM 选择器在
      // SPA 重渲染后不稳定——抓到过侧栏 Playlists/Chapters）
      let author = '', date = '', title = '';
      try {
        const meta = await fetch(
          'https://api.iwara.tv/video/' + location.pathname.split('/')[2]);
        const mj = await meta.json();
        author = (mj.user && mj.user.name) || '';
        date = (mj.createdAt || '').slice(0, 10);
        title = (mj.title || '').slice(0, 80);
      } catch (e) {}
      // download= 参数让 Chrome 直接以该名落盘（SPA 同款机制）——
      // 绕开「下载完成 → CD2 秒删 → rename 追不上」的竞态
      const stem = [date, title].filter(x => x).join(' ');
      const orig = (src.match(/filename=([^&]+)/) || ['', 'video.mp4'])[1];
      const ext = (orig.match(/\.[a-z0-9]+$/i) || ['.mp4'])[0];
      const safe = (stem || orig.replace(ext, '')).replace(
        /[\\/:*?"<>|]/g, '_').slice(0, 120);
      const dl = src.replace('/view?', '/download?') +
        '&download=' + encodeURIComponent(safe + ext);
      return JSON.stringify({quality: q, url: dl,
        author: author, date: date, title: title});
    }
  }
  return JSON.stringify({error: 'no-quality-item'});
})(%(qualities)s)
"""


async def resolve_best_download_url(cdp, video_id, target_id, timeout=40):
    """在目标 tab 会话里复刻 UI：齿轮 → 最高画质 → 播放源转下载直链。

    返回 (download_url, title, quality, author, date)；失败抛 RuntimeError。"""
    expr = _RESOLVE_JS % {
        "gear": _GEAR_PATH_PREFIX,
        "qualities": json.dumps(list(_QUALITY_ORDER)),
    }
    attach = await cdp.command("Target.attachToTarget", {
        "targetId": target_id, "flatten": True})
    session_id = attach["sessionId"]
    try:
        result = await cdp.command("Runtime.evaluate", {
            "expression": expr, "awaitPromise": True, "returnByValue": True},
            session_id=session_id, timeout=timeout)
    finally:
        try:
            await cdp.command("Target.detachFromTarget", {
                "sessionId": session_id})
        except Exception:
            pass
    value = (result.get("result") or {}).get("value")
    if not value:
        raise RuntimeError("iwara 页面脚本无返回（页面未加载完？）")
    info = json.loads(value)
    if "error" in info:
        raise RuntimeError(f"iwara UI 复刻失败：{info['error']}")
    return (info["url"], info.get("title") or "", info.get("quality") or "",
            info.get("author") or "", info.get("date") or "")
