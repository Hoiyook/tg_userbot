#!/usr/bin/env python3
"""通用站点 cookie 提取：从本地浏览器读任意域 cookie → 打印/存 tg_secrets.json。

用法：
  python scripts/get_cookie.py bilibili.com                 # 提取并预览
  python scripts/get_cookie.py bilibili.com --save          # 写入 bilibili_cookie 字段
  python scripts/get_cookie.py bilibili.com --save my_field --browser edge

前提：目标浏览器已登录该站点。Chrome/Edge 可运行中读取（macOS 首次会弹
钥匙串授权框，点「允许」）；Firefox 须完全退出；Safari 不支持。

--save 字段名缺省 = 域名首段 + "_cookie"（bilibili.com → bilibili_cookie）。
存入 tg_secrets.json 与 bot 内置 cookie（douyin/pawchive/ehentai/bilibili）
同款纪律：原子替换、保留其它字段；bot 运行中写入的字段下次重启生效
（bot 菜单里更新则实时生效）。
"""
import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from tg_userbot.browser_cookies import load_browser_cookie_string  # noqa: E402

SECRETS_FILE = os.environ.get(
    "TG_SECRETS_FILE", os.path.join(REPO_ROOT, "tg_secrets.json"))

# 登录态标志字段（命中任意一个 ≈ 已登录；其余多为风控/埋点 cookie）
LOGIN_MARKERS = ("SESSDATA", "sessionid", "auth_token", "sessdata",
                 "token", "uid")


def mask(text, head=12, tail=4):
    raw = str(text or "")
    if len(raw) <= head + tail + 2:
        return raw
    return f"{raw[:head]}…{raw[-tail:]}"


def main():
    ap = argparse.ArgumentParser(
        description="从本地浏览器提取指定域 cookie（如 bilibili.com）")
    ap.add_argument("domain", help="目标域，如 bilibili.com / ehentai.org")
    ap.add_argument("--browser", default="chrome",
                    choices=("chrome", "edge", "firefox"),
                    help="从哪个浏览器读（默认 chrome）")
    ap.add_argument("--save", nargs="?", const="", default=None,
                    metavar="字段名",
                    help="存入 tg_secrets.json（缺省字段名 = 域名首段_cookie）")
    args = ap.parse_args()

    domain = args.domain.strip().lstrip(".")
    cookie, err = load_browser_cookie_string(args.browser, domain=domain)
    if err:
        sys.exit(f"❌ {err}")

    names = [p.split("=", 1)[0] for p in cookie.split(";") if "=" in p]
    logged = any(m.lower() in (n.lower() for n in names)
                 for m in LOGIN_MARKERS)
    print(f"✅ {args.browser} 里 {domain} 域共 {len(names)} 条 cookie"
          + ("（含登录态字段 ✅）" if logged else "（⚠️ 未发现登录态字段，"
             "先在浏览器登录该站点）"))
    print(f"   字段：{', '.join(names[:12])}"
          + ("…" if len(names) > 12 else ""))
    print(f"   片段：{mask(cookie)}")

    if args.save is None:
        return
    field = args.save.strip() or f"{domain.split('.')[0]}_cookie"
    try:
        with open(SECRETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except FileNotFoundError:
        data = {}
    except Exception as e:
        sys.exit(f"❌ tg_secrets.json 解析失败，拒绝覆盖：{e}")
    data[field] = cookie
    tmp = SECRETS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, SECRETS_FILE)
    print(f"💾 已写入 {SECRETS_FILE} 的 {field} 字段"
          f"（bot 运行中则重启后生效）")


if __name__ == "__main__":
    main()
