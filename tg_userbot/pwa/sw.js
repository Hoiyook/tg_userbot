/* TG Userbot 看板 Service Worker（2026-11-02）。
 * 职责：① 让 PWA 可安装（必须有 fetch handler）；
 *       ② 静态壳（HTML/图标/manifest）缓存兜底——离线也能打开壳；
 *       ③ /api/* 一律网络优先、绝不缓存（数据必须实时）。
 * 更新策略：版本号变更 + skipWaiting + clients.claim，重开即新版。
 */
const CACHE = "nagram-dash-v4";   // v4：发现页签恢复（找文件移除）+ 粘贴框回归\n//   v3：应用内刷新 + 下拉刷新 + 新图标

self.addEventListener("install", (e) => self.skipWaiting());

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;   // 数据实时，不缓存
  e.respondWith(
    fetch(e.request)
      .then((r) => {
        const copy = r.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
        return r;
      })
      .catch(() =>
        caches.match(e.request).then(
          (hit) => hit || new Response("离线", { status: 503,
            headers: { "Content-Type": "text/plain; charset=utf-8" } }))
      )
  );
});
