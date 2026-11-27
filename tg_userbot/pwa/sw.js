/* TG Userbot 看板 Service Worker（2026-11-02）。
 * 职责：① 让 PWA 可安装（必须有 fetch handler）；
 *       ② 只缓存图标/manifest（静态壳），**绝不缓存看板 HTML**；
 *       ③ /api/* 一律网络优先、绝不缓存（数据必须实时）。
 *
 * 2026-11-27 教训：此前 HTML 也进缓存兜底——网络抖动/断连时 SW 拿旧
 * HTML 顶上，页面看起来"活着"（SSE 会自动重连），实际是几天前的 UI。
 * owner 三次部署都看到旧布局的根因之一。看板由后端 serve，改 UI 必须
 * 立刻生效：导航请求一律纯网络，失败就显示离线提示，绝不喂旧页面。
 *
 * 更新策略：版本号变更 + skipWaiting + clients.claim，重开即新版。
 */
const CACHE = "nagram-dash-v5";
const ASSETS = /\.(png|ico|svg|webp|jpg)$/;

self.addEventListener("install", (e) => self.skipWaiting());

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;   // 数据实时，不缓存

  // 导航请求（看板 HTML）：纯网络、绕过 HTTP 缓存。失败不喂旧页面。
  if (e.request.mode === "navigate") {
    e.respondWith(
      fetch(e.request, { cache: "no-store" }).catch(
        () => new Response(
          "<!doctype html><meta charset=utf-8>"
          + "<body style='font:15px system-ui;padding:24px'>"
          + "📴 离线——连不上下载器。代码不会用旧页面冒充新版本；"
          + "恢复网络后刷新即可。</body>",
          { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } })
      )
    );
    return;
  }

  // 其余静态资源（图标/manifest）：网络优先，失败才用缓存兜底。
  if (!ASSETS.test(url.pathname) && !url.pathname.endsWith("manifest.json")) {
    return;   // 其它请求交给浏览器默认处理
  }
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
