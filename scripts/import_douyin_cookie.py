#!/usr/bin/env python3
"""从本地浏览器提取 douyin.com cookie 并写入 tg_secrets.json（实时生效）。

用法：
    .venv/bin/python scripts/import_douyin_cookie.py chrome   # chrome/edge/firefox

macOS 首次读取 Chrome/Edge 会弹钥匙串授权框，点「允许」即可；
Firefox 读取前需完全退出浏览器。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tg_userbot import config  # noqa: E402
from tg_userbot.browser_cookies import load_browser_cookie_string  # noqa: E402


def main():
    if len(sys.argv) != 2 or sys.argv[1].lower() not in (
        "chrome", "edge", "firefox"
    ):
        print(__doc__)
        return 2
    browser = sys.argv[1].lower()
    print(f"从 {browser} 读取 douyin.com cookie…")
    cookie, err = load_browser_cookie_string(browser)
    if err:
        print(f"❌ {err}")
        return 1
    err = config.save_douyin_cookie(cookie)
    if err:
        print(f"❌ 保存失败：{err}")
        return 1
    saved = config.DOUYIN_COOKIE
    sess = "含登录态 sessionid ✅" if "sessionid=" in saved else (
        "⚠️ 未检测到 sessionid（可能非登录态）"
    )
    print(f"✅ 已写入 tg_secrets.json，实时生效")
    print(f"   长度：{len(saved)} 字符")
    print(f"   片段：{config.mask_douyin_cookie(saved)}")
    print(f"   {sess}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
