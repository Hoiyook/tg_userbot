# -*- coding: utf-8 -*-
"""Chaquopy 探针：验证 Android 单 APK 内嵌 Python 的核心链路。

只回答三个问题：
1. CPython 3.12 能不能在 APK 里起来；
2. telethon / aiohttp / httpx / python-socks 能不能 import；
3. app 私有目录里的 SQLite 能不能开 WAL。

aiohttp 可用时用它提供页面；不可用时自动回落到 stdlib
http.server——这样即使 aiohttp 没有 Android wheel，你仍能在
WebView 里看到失败原因，而不是只看到白屏。
"""
import asyncio
import html
import json
import os
import platform
import sqlite3
import sys
import threading
import time
import traceback

import android_paths

_START = time.time()
_STOP = threading.Event()
_THREAD = None
_HTTPD = None
_DATA_DIR = ""
_DB = {"ok": False, "detail": "not probed yet"}
_IMPORT = {}
_SERVER_ERROR = ""
_TG = {}
_TG_PATHS = {}
_TG_READY = False


def _try(name, fn):
    try:
        mod = fn()
        _IMPORT[name] = {
            "ok": True,
            "version": getattr(mod, "__version__", "?"),
            "error": "",
        }
        return mod
    except Exception:
        _IMPORT[name] = {
            "ok": False,
            "version": "",
            "error": traceback.format_exc(),
        }
        return None


telethon = _try("telethon", lambda: __import__("telethon"))
aiohttp = _try("aiohttp", lambda: __import__("aiohttp"))
httpx = _try("httpx", lambda: __import__("httpx"))
python_socks = _try("python_socks", lambda: __import__("python_socks"))


def _probe_db(data_dir):
    global _DB
    try:
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, "probe.db")
        con = sqlite3.connect(path)
        mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        con.execute(
            "CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY, ts TEXT)")
        con.execute("INSERT INTO probe (ts) VALUES (?)",
                    (time.strftime("%Y-%m-%d %H:%M:%S"),))
        con.commit()
        rows = con.execute("SELECT COUNT(*) FROM probe").fetchone()[0]
        con.close()
        _DB = {"ok": True,
               "detail": f"journal_mode={mode}, rows={rows}, path={path}"}
    except Exception:
        _DB = {"ok": False, "detail": traceback.format_exc()}


def _probe_tg(data_dir):
    """设好 Android 路径环境，逐个 import tg_userbot 核心模块。

    只 import，不启动 Telethon/主循环——先确认模块在 Chaquopy 下能加载。
    """
    global _TG_READY
    if _TG_READY:
        return
    os.environ["HOME"] = data_dir
    os.environ["TG_DATA_ROOT"] = data_dir
    dl = android_paths.download_dir(data_dir)
    os.environ["TG_DOWNLOAD_DIR"] = dl
    os.environ["TG_RUNTIME_DIR"] = os.path.join(data_dir, "runtime")
    os.environ["TG_RUNTIME_DB"] = os.path.join(
        data_dir, "runtime", "tg_userbot.db")
    os.environ["TG_RESOLVER"] = "off"     # Android 上走解析 bot，不 import f2
    os.environ["TG_ANDROID"] = "1"        # 桌面专属能力守卫
    os.environ["TG_SECRETS_FILE"] = os.path.join(
        data_dir, "tg_secrets.json")
    env_path = os.path.join(data_dir, "android_env.json")
    if os.path.exists(env_path):
        try:
            with open(env_path, encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    os.environ.setdefault(str(k), str(v))
        except Exception:
            pass
    os.makedirs(dl, exist_ok=True)
    os.makedirs(os.environ["TG_RUNTIME_DIR"], exist_ok=True)

    mods = [
        "tg_userbot.config", "tg_userbot.log", "tg_userbot.state",
        "tg_userbot.spawn", "tg_userbot.naming", "tg_userbot.sources",
        "tg_userbot.history", "tg_userbot.netio", "tg_userbot.runtime_db",
        "tg_userbot.dedup", "tg_userbot.caption_filter",
        "tg_userbot.workers", "tg_userbot.download", "tg_userbot.queue",
        "tg_userbot.cleanup", "tg_userbot.stats", "tg_userbot.text",
        "tg_userbot.notify", "tg_userbot.platform", "tg_userbot.routing",
        "tg_userbot.commands", "tg_userbot.api_server", "tg_userbot.bot",
        "tg_userbot.listener", "tg_userbot.listener_worker",
        "tg_userbot.wl_scan", "tg_userbot.reporter", "tg_userbot.app",
    ]
    for name in mods:
        try:
            __import__(name)
            _TG[name] = {"ok": True, "error": ""}
        except Exception:
            _TG[name] = {"ok": False, "error": traceback.format_exc()}
    try:
        from tg_userbot import config
        _TG_PATHS.update({
            "DATA_ROOT": config.DATA_ROOT,
            "DOWNLOAD_DIR": config.DOWNLOAD_DIR,
            "RUNTIME_DIR": config.RUNTIME_DIR,
            "RUNTIME_DB": config.RUNTIME_DB_FILE,
            "SESSION": config.SESSION_NAME,
        })
    except Exception:
        _TG_PATHS["error"] = traceback.format_exc()
    _TG_READY = True


def _tg_section():
    if not _TG:
        return "<p class='note'>未执行（服务还没启动）</p>"
    total = len(_TG)
    ok = sum(1 for v in _TG.values() if v["ok"])
    fails = [(n, v) for n, v in _TG.items() if not v["ok"]]
    head = (
        f"<div class='note'><b class='{'ok' if not fails else 'bad'}'>"
        f"{ok}/{total} OK</b>"
        + ("：tg_userbot 核心模块全部可导入。" if not fails
           else f"：{len(fails)} 个模块失败，见下表。")
        + "</div>")
    rows = ""
    if fails:
        rows = ("<table><tr><th>模块</th><th>结果</th><th>错误</th></tr>")
        for n, v in fails:
            rows += (f"<tr><td>{html.escape(n)}</td>"
                     f"<td class='bad'>FAILED</td>"
                     f"<td><pre>{html.escape(v['error'])}</pre></td></tr>")
        rows += "</table>"
    paths = ""
    if _TG_PATHS:
        paths = ("<p class='note'>" + "<br>".join(
            f"{html.escape(k)}: {html.escape(str(v))}"
            for k, v in _TG_PATHS.items()) + "</p>")
    return head + rows + paths


def _core_status():
    try:
        import android_main
        return android_main.status()
    except Exception:
        return {"state": "unavailable", "error": traceback.format_exc()}


def _core_section():
    c = _core_status()
    state = str(c.get("state") or "?")
    if state == "connected":
        detail = (
            "<b class='ok'>已连接</b>：@"
            + html.escape(str(c.get("user") or "?"))
            + f" · 队列 {c.get('queue', 0)} · 待重试 {c.get('retry', 0)}")
    elif state == "error":
        detail = ("<b class='bad'>启动失败</b>：<pre>"
                  + html.escape(str(c.get("error") or "")) + "</pre>")
    else:
        detail = (f"状态：<b>{html.escape(state)}</b>"
                  "（点主界面的「启动核心」）")
    return f"<div class='note'>{detail}</div>"


def _status():
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "uptime_s": int(time.time() - _START),
        "imports": _IMPORT,
        "sqlite": {"version": sqlite3.sqlite_version, **_DB},
        "data_dir": _DATA_DIR,
        "server": "aiohttp" if aiohttp is not None else "stdlib",
        "server_error": _SERVER_ERROR,
        "tg_userbot": {"ready": _TG_READY, "imports": _TG,
                       "paths": _TG_PATHS},
        "core": _core_status(),
        "executable": sys.executable,
    }


def _status_html():
    def row(name):
        info = _IMPORT.get(name) or {"ok": False, "version": "",
                                     "error": "missing"}
        if info["ok"]:
            return (f"<tr><td>{name}</td><td class='ok'>OK</td>"
                    f"<td>{html.escape(str(info['version']))}</td></tr>")
        return (f"<tr><td>{name}</td><td class='bad'>FAILED</td>"
                f"<td><pre>{html.escape(info['error'])}</pre></td></tr>")

    rows = "".join(row(n) for n in
                   ("telethon", "aiohttp", "httpx", "python_socks"))
    db_cls = "ok" if _DB["ok"] else "bad"
    db_label = "OK" if _DB["ok"] else "FAILED"
    db_detail = html.escape(_DB["detail"])

    bad = [n for n in ("telethon", "aiohttp") if not _IMPORT[n]["ok"]]
    if not _DB["ok"]:
        bad.append("sqlite-wal")
    if _SERVER_ERROR:
        bad.append("http-server")
    if not bad:
        verdict = ("<b class='ok'>核心链路通</b>：Python + Telethon + "
                   "aiohttp + SQLite WAL 都可用。")
    else:
        verdict = (f"<b class='bad'>有阻断项</b>：{', '.join(bad)}。"
                   "看下面错误详情。")

    uptime = int(time.time() - _START)
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chaquopy Probe</title>
<style>
body{{font-family:-apple-system,sans-serif;margin:16px;line-height:1.45;color:#111}}
h1{{font-size:18px}} h2{{font-size:15px;margin-top:20px}}
table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ddd;padding:6px;font-size:13px;vertical-align:top}}
pre{{white-space:pre-wrap;word-break:break-all;font-size:11px;margin:0;color:#a00}}
.ok{{color:#137333;font-weight:700}} .bad{{color:#b00020;font-weight:700}}
.note{{background:#f6f8fa;border:1px solid #e5e7eb;padding:10px;
border-radius:8px;font-size:13px}}
</style></head><body>
<h1>Chaquopy Probe</h1>
<div class="note">{verdict}</div>
<h2>环境</h2>
<table>
<tr><th>项</th><th>结果</th><th>详情</th></tr>
<tr><td>Python</td><td class="ok">OK</td>
<td>{html.escape(platform.python_version())} ·
{html.escape(platform.platform())}</td></tr>
<tr><td>Web 服务</td><td class="ok">OK</td>
<td>{html.escape(_status()['server'])} @ 127.0.0.1:8765 ·
已运行 {uptime}s</td></tr>
<tr><td>SQLite</td><td class="{db_cls}">{db_label}</td>
<td><pre>{db_detail}</pre></td></tr>
{('<tr><td>HTTP server</td><td class="bad">FAILED</td>'
  f'<td><pre>{html.escape(_SERVER_ERROR)}</pre></td></tr>')
 if _SERVER_ERROR else ''}
{rows}
</table>
<h2>tg_userbot 导入审计</h2>
{_tg_section()}
<h2>核心（Telethon）</h2>
{_core_section()}
<h2>说明</h2>
<p class="note">这个页面由手机里的 CPython 直接提供。能看到它，就说明
「APK 内嵌 Python + 前台服务 + WebView 本地回环」这条链路是通的。
下一步才是把 tg_userbot 的模块搬进来；Chrome Agent / CD2 仍然需要
桌面或远程服务。</p>
<p class="note">data_dir: {html.escape(_DATA_DIR)}<br>
python: {html.escape(sys.executable)}</p>
</body></html>"""


def _run_aiohttp():
    # 必须显式 import 子模块：aiohttp 3.10.10（Chaquopy wheel）的
    # __getattr__ 不暴露 web，`aiohttp.web` 会 AttributeError。
    from aiohttp import web

    async def root(request):
        return web.Response(text=_status_html(), content_type="text/html")

    async def api(request):
        return web.json_response(_status())

    async def run():
        app = web.Application()
        app.router.add_get("/", root)
        app.router.add_get("/api/status", api)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 8765)
        await site.start()
        while not _STOP.is_set():
            await asyncio.sleep(0.5)
        await runner.cleanup()

    asyncio.run(run())


def _run_stdlib():
    global _HTTPD
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/status"):
                body = json.dumps(_status(), ensure_ascii=False).encode("utf-8")
                ctype = "application/json; charset=utf-8"
            else:
                body = _status_html().encode("utf-8")
                ctype = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    _HTTPD = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    _HTTPD.serve_forever()


def _run_guarded(target):
    """线程入口：把绑端口/服务器的异常收进 _SERVER_ERROR。"""
    global _SERVER_ERROR
    try:
        target()
    except Exception:
        _SERVER_ERROR = traceback.format_exc()


def start(data_dir):
    """Kotlin 前台服务调用：启动 HTTP 服务（幂等）。"""
    global _THREAD, _DATA_DIR, _SERVER_ERROR
    _DATA_DIR = data_dir
    _SERVER_ERROR = ""
    _STOP.clear()
    _probe_db(data_dir)
    _probe_tg(data_dir)
    if _THREAD is not None and _THREAD.is_alive():
        return "already running via " + _status()["server"]
    target = _run_aiohttp if aiohttp is not None else _run_stdlib
    _THREAD = threading.Thread(target=lambda: _run_guarded(target),
                               daemon=True, name="probe-server")
    _THREAD.start()
    tg_ok = sum(1 for v in _TG.values() if v["ok"])
    return ("started via %s; telethon=%s aiohttp=%s sqlite_wal=%s; "
            "tg_userbot=%d/%d"
            % (_status()["server"], _IMPORT["telethon"]["ok"],
               _IMPORT["aiohttp"]["ok"], _DB["ok"], tg_ok, len(_TG)))


def server_error():
    """Kotlin 侧在 start 后 1.5s 调用：返回服务器绑端口/运行的 traceback。"""
    return _SERVER_ERROR


def stop():
    """Kotlin 服务 onDestroy 调用：停 HTTP 服务。"""
    _STOP.set()
    if _HTTPD is not None:
        threading.Thread(target=_HTTPD.shutdown, daemon=True).start()
    return "stopping"
