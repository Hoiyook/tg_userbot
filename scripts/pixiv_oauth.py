#!/usr/bin/env python3
"""全自动 Pixiv OAuth：拿 gallery-dl 所需的 refresh_token（无人工输入）。

链路（2026-10-25 实测 10s 拿 code）：自生成 PKCE → CDP 驱动 Agent Chrome
（127.0.0.1:9222，须已登录 pixiv）点穿「繼續使用此帳號/稍後再說」→
Network 域拦截 302 的 pixiv://account/login?code=… → 走 socks 代理 POST
oauth.secure.pixiv.net/auth/token 交换 → 写入 tg_secrets.json 的
pixiv.refresh_token。

前提：
- Agent Chrome 在跑且登录了 pixiv.net（accounts 会话决定能不能点穿）
- Hiddify 类 socks 代理在 127.0.0.1:12334（pixiv 全域被墙）

用法：.venv/bin/python scripts/pixiv_oauth.py
（token 失效时重跑即可；pixiv_dl 的 /pixiv 认证失败也会自动调它）
"""
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import struct
import sys
import time
import urllib.request

CDP_PORT = 9222
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
REDIRECT_URI = ("https://app-api.pixiv.net"
                "/web/v1/users/auth/pixiv/callback")
PROXIES = {"http": "socks5h://127.0.0.1:12334",
           "https": "socks5h://127.0.0.1:12334"}

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS_FILE = os.environ.get("TG_SECRETS_FILE",
                              os.path.join(REPO_ROOT, "tg_secrets.json"))


def http_json(path):
    with urllib.request.urlopen(
            f"http://127.0.0.1:{CDP_PORT}{path}", timeout=5) as r:
        return json.load(r)


def open_tab(url):
    req = urllib.request.Request(
        f"http://127.0.0.1:{CDP_PORT}/json/new?"
        f"{urllib.parse.urlencode({'url': url})}", method="PUT")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


class WS:
    """极简 CDP WebSocket 客户端（无需三方依赖）。"""

    def __init__(self, ws_url):
        path = "/" + ws_url.split("/", 3)[3]
        self.sock = socket.create_connection(("127.0.0.1", CDP_PORT),
                                             timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        head, _, buf = buf.partition(b"\r\n\r\n")
        assert b"101" in head.split(b"\r\n")[0], head[:100]
        self.buf, self.mid = buf, 0

    def _ex(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                # 对端关闭（Agent Chrome 退出/tab 被关）：绝不能忙等空轮
                raise ConnectionError("CDP WebSocket 已关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, method, params=None):
        self.mid += 1
        d = json.dumps({"id": self.mid, "method": method,
                        "params": params or {}}).encode()
        mk = os.urandom(4)
        hdr = struct.pack(">BB", 0x81, 0x80 | len(d)) if len(d) < 126 \
            else struct.pack(">BBH", 0x81, 0x80 | 126, len(d))
        self.sock.sendall(hdr + mk + bytes(b ^ mk[i & 3]
                                           for i, b in enumerate(d)))
        return self.mid

    def recv(self, timeout=2):
        self.sock.settimeout(timeout)
        try:
            while True:
                h = self._ex(2)
                op, ln = h[0] & 15, h[1] & 127
                if ln == 126:
                    ln = struct.unpack(">H", self._ex(2))[0]
                elif ln == 127:
                    ln = struct.unpack(">Q", self._ex(8))[0]
                p = self._ex(ln)
                if op != 1:
                    continue
                return json.loads(p.decode())
        except (socket.timeout, TimeoutError):
            return None

    def call(self, method, params=None):
        mid = self.send(method, params)
        deadline = time.time() + 12
        while time.time() < deadline:
            m = self.recv(3)
            if m and m.get("id") == mid:
                return m.get("result", m.get("error"))
        return None


CLICK_JS = """
(function(){
  var all=[...document.querySelectorAll(
      'button,div[role="button"],a[href],li')]
    .filter(function(b){return b.offsetParent});
  function pick(rx){
    var m=all.filter(function(b){return rx.test(b.textContent||'')});
    return m.length?m[0]:null;
  }
  var pri=pick(/繼續使用|继续使用|此帳號|此账号|Continue with/i);
  if(pri){pri.click();return 'pri:'+pri.textContent.trim().slice(0,12);}
  var later=pick(/後で|あとで|稍後|稍后|Not now|Skip|スキップ/i);
  if(later){later.click();return 'later:'+later.textContent.trim().slice(0,12);}
  var go=pick(/続行|继续|Continue|次へ|Next/i);
  if(go){go.click();return 'go:'+go.textContent.trim().slice(0,12);}
  var sel=all.filter(function(b){return b.tagName==='LI'});
  if(sel.length){sel[0].click();return 'li:'+sel[0].textContent.trim().slice(0,12);}
  var auth=pick(/許可|授权|Allow|同意/i);
  if(auth){auth.click();return 'auth:'+auth.textContent.trim().slice(0,12);}
  return 'noop';
})()"""


def _close_tab(tid):
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{CDP_PORT}/json/close/{tid}", timeout=3)
    except Exception:
        pass


def fetch_code(login_url):
    """CDP 开 tab → 点穿登录/确认页 → 返回授权 code（失败 None）。"""
    tab = open_tab("about:blank")
    tid = tab["id"]
    target = next(t for t in http_json("/json/list") if t["id"] == tid)
    ws = WS(target["webSocketDebuggerUrl"])
    ws.send("Page.enable")
    ws.send("Network.enable")

    def drain(sec=1.2):
        """排网络事件，拦截 pixiv://…code=（自定义协议 tab URL 抓不到）。"""
        code = None
        t0 = time.time()
        while time.time() - t0 < sec:
            m = ws.recv(0.3)
            if not m:
                continue
            if m.get("method", "").startswith("Network."):
                mm = re.search(r"pixiv://account/login\?code=([A-Za-z0-9_-]+)",
                               json.dumps(m.get("params", {})))
                if mm:
                    code = mm.group(1)
        return code

    loaded = False
    for _attempt in range(3):          # SSL/代理瞬时抖动重试
        ws.call("Page.navigate", {"url": login_url})
        t1 = time.time()
        while time.time() - t1 < 30:
            if drain(1.5):
                break                  # 导航链上直接出了 code（罕见但兜底）
            r = ws.call("Runtime.evaluate", {"expression":
                        "document.readyState+' @ '+location.href.slice(0,70)",
                        "returnByValue": True})
            v = (r or {}).get("result", {}).get("value", "")
            if v.startswith("complete") and "accounts.pixiv" in v:
                loaded = True
                break
            if "chrome-error" in v:
                break
        if loaded:
            break
        time.sleep(2)
    if not loaded:
        _close_tab(tid)
        print("❌ 登录页没加载出来（查代理 12334 / Agent Chrome 9222）")
        return None

    code = None
    t0 = time.time()
    clicked_at = 0.0
    while time.time() - t0 < 60 and not code:
        code = drain(1.2)
        if code:
            break
        if time.time() - clicked_at > 4:   # 多阶段页面幂等补点
            clicked_at = time.time()
            r = ws.call("Runtime.evaluate",
                        {"expression": CLICK_JS, "returnByValue": True})
            v = (r or {}).get("result", {}).get("value", "")
            if v and v != "noop":
                print("   click →", v[:60])
    _close_tab(tid)
    return code


def exchange_token(code, verifier):
    """code + PKCE verifier → refresh_token（走 socks 代理）。"""
    import requests
    r = requests.post(
        "https://oauth.secure.pixiv.net/auth/token",
        headers={"User-Agent":
                 "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"},
        data={
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "code": code, "code_verifier": verifier,
            "grant_type": "authorization_code", "include_policy": "true",
            "redirect_uri": REDIRECT_URI,
        }, proxies=PROXIES, timeout=20)
    return r.json()


def save_token(token):
    sec = {}
    try:
        with open(SECRETS_FILE, "r", encoding="utf-8") as f:
            sec = json.load(f)
    except Exception:
        pass
    sec.setdefault("pixiv", {})["refresh_token"] = token
    tmp = SECRETS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sec, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SECRETS_FILE)


def main():
    verifier = base64.urlsafe_b64encode(
        secrets.token_bytes(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    login_url = ("https://app-api.pixiv.net/web/v1/login"
                 f"?code_challenge={challenge}&code_challenge_method=S256"
                 "&client=pixiv-android")
    code = fetch_code(login_url)
    if not code:
        sys.exit(1)
    print("code 抓到:", code[:12], "…")
    data = exchange_token(code, verifier)
    if "refresh_token" not in data:
        print("❌ 交换失败:", json.dumps(data, ensure_ascii=False)[:300])
        sys.exit(2)
    save_token(data["refresh_token"])
    print("✅ refresh_token 已写入 tg_secrets.json"
          f"（用户 {data.get('user', {}).get('name', '?')}）")


if __name__ == "__main__":
    import urllib.parse
    main()
