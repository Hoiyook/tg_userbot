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
        # CDN 常拒绝裸 HEAD（403），用「带标准头的 Range GET」模拟真实
        # 下载行为（下载分支同款 UA/Referer），并从 content-range 取总大小
        async with httpx.AsyncClient(
            timeout=15, headers=config.DOUYIN_HEADERS, follow_redirects=True
        ) as client:
            resp = await client.get(
                result.direct_url, headers={"Range": "bytes=0-1023"}
            )
        total = None
        cr = resp.headers.get("content-range") or ""
        if "/" in cr:
            try:
                total = int(cr.rsplit("/", 1)[1])
            except ValueError:
                pass
        if total is None and resp.headers.get("content-length"):
            try:
                total = int(resp.headers["content-length"])
            except ValueError:
                pass
        if resp.status_code in (200, 206):
            size_txt = f"{total / 1024 / 1024:.2f} MB" if total else "未知"
            print(f"   大小    ：{size_txt}（Range GET {resp.status_code}，可下载 ✅）")
        else:
            print(f"   大小    ：验证请求 {resp.status_code}（解析已成功；"
                  "若实际下载也 403 需要再调请求头）")
    except Exception as e:
        print(f"   大小    ：HEAD 校验失败（不影响解析）：{e}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
