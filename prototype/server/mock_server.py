#!/usr/bin/env python3
"""演示：套壳 app 与 userbot 后端的网络交互。

模拟真实版要加在 userbot 进程内的那层薄 HTTP API：
  GET  /api/status   → state.QUEUE 快照 + 台账 + 健康探测（真实版读内存 state）
  POST /api/enqueue  → 入队 url 任务（真实版调 build_url_record → enqueue_and_start）
  GET  /             → 直接 serve 看板页面（WebView 同源加载，天然无 CORS）

真实部署把监听地址换成 Tailscale/LAN 网卡并加 token 校验即可。
仅用标准库：python3 mock_server.py [端口]   （默认 8765，绑 127.0.0.1）
"""
import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8765
LOG = Path(__file__).parent / "mock_queue.log"

# 模拟 state.QUEUE / 台账；done_today 每 8 秒 +1，让轮询效果可见
STATE = {"in_flight": 3, "retry": 2, "done_today": 47, "bytes": 1_932_735_488}
QUEUE = [
    {"name": "26-10-31 88231 #仓鼠 cosplay 团拍 - DSC_8821.nef", "src": "白名单", "state": "在途", "ts": "12:41"},
    {"name": "26-10-30 ASMR300 RJ332100 全轨包.zip", "src": "asmr", "state": "排队", "ts": "12:38"},
    {"name": "26-10-30 71203 #MMD 掉装备 - model.pmx", "src": "标签监听", "state": "排队", "ts": "12:30"},
]


def _ticker():
    while True:
        time.sleep(8)
        STATE["done_today"] += 1


DASHBOARD = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>TG Userbot 控制台</title>
<style>
  :root { --bg:#10151c; --card:#1a2230; --line:#2b3846; --tx:#e8edf4; --tx2:#8fa3bb; --tx3:#5d7189;
          --green:#4ade80; --blue:#60a5fa; --amber:#fbbf24; }
  * { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  body { background:var(--bg); color:var(--tx); font-family:-apple-system,"PingFang SC",sans-serif;
         font-size:15px; padding-bottom:130px; }
  .wrap { padding:16px; max-width:560px; margin:0 auto; }
  header { display:flex; align-items:center; gap:10px; padding:14px 0 4px; }
  header h1 { font-size:19px; }
  .dot { width:9px; height:9px; border-radius:50%; background:var(--green); box-shadow:0 0 8px var(--green); }
  .badge { margin-left:auto; font-size:11px; color:var(--amber); border:1px solid var(--amber);
           border-radius:20px; padding:2px 9px; }
  .sub { color:var(--tx3); font-size:12.5px; margin-bottom:14px; }
  .stats { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-bottom:16px; }
  .stat { background:var(--card); border-radius:12px; padding:11px 6px; text-align:center; }
  .stat b { display:block; font-size:21px; }
  .stat span { font-size:11.5px; color:var(--tx2); }
  h2 { font-size:14px; color:var(--tx2); margin:4px 0 9px; }
  .card { background:var(--card); border-radius:12px; padding:14px; margin-bottom:16px; }
  .row { display:flex; gap:8px; }
  input[type=text] { flex:1; background:#0c1017; border:1px solid var(--line); color:var(--tx);
                     border-radius:10px; padding:11px 12px; font-size:14px; }
  button { background:var(--blue); color:#0b1220; border:0; border-radius:10px;
           padding:11px 16px; font-size:14px; font-weight:600; }
  .fb { font-size:13px; color:var(--tx2); margin-top:9px; min-height:18px; }
  .qrow { display:flex; align-items:center; gap:11px; background:var(--card); border-radius:12px;
          padding:11px 13px; margin-bottom:8px; }
  .qrow .info { flex:1; min-width:0; }
  .nm { font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .dt { font-size:11.5px; color:var(--tx3); margin-top:3px; }
  .tag { font-size:11px; border-radius:6px; padding:2px 7px; }
  .t-run { color:var(--blue); background:rgba(96,165,250,.13); }
  .t-wait { color:var(--tx2); background:rgba(143,163,187,.12); }
</style></head><body>
<header><span class="dot"></span><h1>TG Userbot</h1><span class="badge">网络演示</span></header>
<div class="wrap">
  <div class="sub" id="conn">连接中…</div>
  <div class="stats">
    <div class="stat"><b id="s-run">–</b><span>在途</span></div>
    <div class="stat"><b id="s-q">–</b><span>排队</span></div>
    <div class="stat"><b id="s-r">–</b><span>待重试</span></div>
    <div class="stat"><b id="s-d">–</b><span>今日成功</span></div>
  </div>

  <h2>分享 / 粘贴链接入队</h2>
  <div class="card">
    <div class="row">
      <input type="text" id="u" placeholder="https://v.douyin.com/...">
      <button onclick="submit()">入队</button>
    </div>
    <div class="fb" id="fb">POST /api/enqueue → Mac 端落日志</div>
  </div>

  <h2>下载队列（GET /api/status 每 2 秒轮询）</h2>
  <div id="queue"></div>
</div>
<script>
function esc(s){ return (s||"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
async function refresh(){
  try {
    const d = await (await fetch("/api/status")).json();
    document.getElementById("conn").textContent =
      "已连接 · Mac 主机 · 今日 " + (d.stats.bytes/1e9).toFixed(1) + " GB";
    document.getElementById("s-run").textContent = d.stats.in_flight;
    document.getElementById("s-q").textContent = d.stats.queued;
    document.getElementById("s-r").textContent = d.stats.retry;
    document.getElementById("s-d").textContent = d.stats.done_today;
    document.getElementById("queue").innerHTML = d.queue.map(t =>
      '<div class="qrow"><div class="info"><div class="nm">' + esc(t.name) +
      '</div><div class="dt">' + esc(t.src) + " · " + esc(t.ts) + '</div></div>' +
      '<span class="tag ' + (t.state==="在途"?"t-run":"t-wait") + '">' + esc(t.state) + '</span></div>').join("");
  } catch(e) {
    document.getElementById("conn").textContent = "❌ 后端不可达：" + e;
  }
}
async function submit(){
  const url = document.getElementById("u").value.trim();
  if (!url) return;
  document.getElementById("fb").textContent = "提交中…";
  try {
    const d = await (await fetch("/api/enqueue", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({url: url})
    })).json();
    document.getElementById("fb").textContent =
      d.ok ? "✅ 已入队 task=" + d.task_id + "（Mac 端已收到）" : "❌ " + (d.error||"失败");
    document.getElementById("u").value = "";
    refresh();
  } catch(e) { document.getElementById("fb").textContent = "❌ 网络错误：" + e; }
}
setInterval(refresh, 2000); refresh();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/status"):
            STATE["queued"] = 12 + sum(1 for t in QUEUE if t["state"] == "排队")
            self._send(200, json.dumps({"ok": True, "stats": STATE, "queue": QUEUE[-8:]},
                                       ensure_ascii=False))
        elif self.path == "/":
            self._send(200, DASHBOARD, "text/html; charset=utf-8")
        else:
            self._send(404, '{"ok":false}')

    def do_POST(self):
        if self.path.startswith("/api/enqueue"):
            n = int(self.headers.get("Content-Length", 0))
            try:
                url = (json.loads(self.rfile.read(n) or b"{}").get("url") or "").strip()
            except ValueError:
                url = ""
            if not url:
                self._send(400, '{"ok":false,"error":"缺少 url"}')
                return
            task_id = uuid.uuid4().hex[:8]
            QUEUE.append({"name": url, "src": "分享直达", "state": "排队",
                          "ts": time.strftime("%H:%M:%S")})
            with LOG.open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%F %T')}\ttask={task_id}\t{url}\n")
            print(f"[enqueue] task={task_id} url={url}", flush=True)
            self._send(200, json.dumps({"ok": True, "task_id": task_id}))
        else:
            self._send(404, '{"ok":false}')

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    import threading
    threading.Thread(target=_ticker, daemon=True).start()
    print(f"mock API on http://127.0.0.1:{PORT}  (模拟器内用 http://10.0.2.2:{PORT})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
