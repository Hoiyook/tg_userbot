#!/usr/bin/env python3
"""抖音本地解析链的真实冒烟测试（需真实网络，不进常规单测）。

用法：
    # 先在 tg_secrets.json 配好 douyin_cookie（推荐，浏览器复制）
    .venv/bin/python scripts/smoke_resolver.py "https://v.douyin.com/xxxx/"

只做解析（不下载），打印标题/作者/直链/直链 HEAD 大小。解析失败会打印
降级原因——任何失败都代表线上行为 = 回退解析 bot，不会卡死。
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tg_userbot import config  # noqa: E402
from tg_userbot.resolver import resolve_douyin  # noqa: E402


async def main(url: str):
    print(f"RESOLVER_ENABLED = {config.RESOLVER_ENABLED}")
    print(f"cookie 已配置   = {bool(config.DOUYIN_COOKIE)}")
    print(f"解析中：{url}\n")
    result = await resolve_douyin(url)
    if result is None:
        print("❌ 本地解析失败（线上将降级解析 bot）")
        return 1
    print(f"✅ aweme_id ：{result.aweme_id}")
    print(f"   标题    ：{result.title}")
    print(f"   作者    ：{result.author}")
    print(f"   直链    ：{result.direct_url[:120]}...")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.head(result.direct_url, follow_redirects=True)
        size = int(resp.headers.get("content-length") or 0)
        print(f"   大小    ：{size / 1024 / 1024:.2f} MB（HEAD {resp.status_code}）")
    except Exception as e:
        print(f"   大小    ：HEAD 校验失败（不影响解析）：{e}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
