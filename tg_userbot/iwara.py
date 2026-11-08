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


# ============================================================
# URL 判重（2026-10-26 用户定档：只有成功过的才拦截，失败重发不拦）
# ============================================================
def find_previous_success(video_id, requests_path=None, tasks_path=None):
    """该 iwara 视频是否**成功**下载过。命中返回请求记录 dict，否则 None。

    数据源两层：① 请求台账的 outcome=SUCCESS（上线后的持久记录，台账
    只进不出永不过期）；② 任务窗口内 status=SUCCESS 的兜底（上线前的
    历史成功，窗口 200 条内仍可命中）。FAILED/CANCELLED 一律不拦——
    失败重发正是期望行为。"""
    from . import chrome_agent, chrome_client
    from . import config as _cfg
    requests_path = requests_path or chrome_client.CHROME_REQUESTS_FILE
    tasks_path = tasks_path or chrome_client.CHROME_TASKS_FILE
    success_ids = set()
    try:
        for t in chrome_agent.load_tasks(tasks_path):
            if t.get("status") == "SUCCESS":
                success_ids.add(t.get("task_id"))
    except Exception:
        pass
    try:
        reqs = chrome_client.load_requests(requests_path)
    except Exception:
        return None
    for rec in reqs:
        if extract_iwara_video_id(rec.get("url") or "") != video_id:
            continue
        if rec.get("outcome") == "SUCCESS":
            return rec
        if rec.get("outcome"):          # FAILED/CANCELLED 明确不拦
            continue
        if rec.get("task_id") in success_ids:
            return rec                  # 上线前的老成功（窗口内兜底）
    return None


def duplicate_text(rec):
    """判重命中回执。"""
    when = rec.get("notified_at") or rec.get("created_at") or ""
    fname = rec.get("filename") or ""
    lines = ["⏭️ 该 iwara 视频已成功下载过，已拦截重复下载",
             f"首次成功：{when}"]
    if fname:
        lines.append(f"当时文件：{fname}")
    lines.append("确要重新下载可显式发：/chrome <视频链接>")
    return "\n".join(lines)
