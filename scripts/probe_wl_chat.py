"""冒烟探针：查一个聊天（默认抖音解析 bot）的最新消息 id 与近期消息量。

用途：/wl since 设置扫描初始值前，先真机确认「最新消息 id」与历史体量——
游标回拨得越深，扫描生产者要补的历史越多（风险见 CLAUDE.md 白名单双通道段）。

回答三个问题：

  a. 最新消息 id 与时间（/wl since 的「不要超过这里」上界）；
  b. 近 24h / 7 天各有多少条消息（id 差近似，一次请求一个点）；
  c. 最近 100 条里有多少条带可下载媒体（体量感：这聊天主要是文字指令还是视频）。

为什么是独立脚本、**不 import tg_userbot**：import 包会在 import 期
``log.configure(LOG_FILE)``，给正在运行的 userbot 进程共用的 download.log 挂上
第二个轮转 handler（见 issues/001，两进程写同一文件午夜互相覆盖归档）。

不干扰生产进程的两重保险：session 库经 sqlite backup API 拷出**一致快照**
（保留 entities 缓存，MemorySession 没有 it 就解析不了 peer），连的是副本；
``receive_updates=False``——主客户端仍是唯一更新消费者，探针不抢消息事件。

用法（代理是必须的，直连在国内网络下连不上 Telegram）::

    TG_PROXY=socks5://127.0.0.1:12334 \\
        .venv/bin/python scripts/probe_wl_chat.py 5161622943
"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from telethon import TelegramClient
from telethon.sessions import SQLiteSession
from telethon.tl.types import PeerChannel, PeerUser

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSION_PATH = os.path.expanduser("~/tg_downloader.session")


def load_secrets():
    with open(os.path.join(REPO_ROOT, "tg_secrets.json"), encoding="utf-8") as f:
        data = json.load(f)
    return int(data["api_id"]), str(data["api_hash"])


def copy_session_snapshot():
    """backup API 拷出一致快照（运行中的主进程正持有原库，绝不直接打开它）。

    文件名必须带 ``.session`` 后缀——SQLiteSession 对不带后缀的路径会自动补一个，
    结果打开成不存在的空库、拿到全新未注册的 auth key（AuthKeyUnregisteredError）。
    """
    tmp = os.path.join(tempfile.mkdtemp(prefix="tg_probe_"), "session_copy.session")
    src = sqlite3.connect(f"file:{SESSION_PATH}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return tmp


def parse_proxy():
    value = (os.environ.get("TG_PROXY") or "").strip()
    if not value:
        print("⚠️ 未设置 TG_PROXY——直连在国内网络下连不上 Telegram，会超时挂住")
        return None
    parsed = urlparse(value)
    scheme = (parsed.scheme or "").lower()
    kind = {"socks5": "socks5", "socks5h": "socks5",
            "socks4": "socks4", "http": "http", "https": "http"}.get(scheme)
    if not kind:
        raise RuntimeError(f"无法识别的代理协议：{scheme}")
    return (kind, parsed.hostname, parsed.port, True,
            parsed.username, parsed.password)


async def make_client():
    api_id, api_hash = load_secrets()
    snapshot = copy_session_snapshot()
    try:
        session = SQLiteSession(snapshot)
        client = TelegramClient(
            session, api_id, api_hash,
            connection_retries=3, retry_delay=2,
            auto_reconnect=False, proxy=parse_proxy(),
            receive_updates=False,
        )
        await asyncio.wait_for(client.connect(), timeout=60)
        return client
    finally:
        shutil.rmtree(os.path.dirname(snapshot), ignore_errors=True)


def peer_of(chat_id):
    return PeerChannel(int(chat_id)) if int(chat_id) < 0 else PeerUser(int(chat_id))


def fmt(dt):
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S") if dt else "None"


async def main():
    chat_id = sys.argv[1] if len(sys.argv) > 1 else "5161622943"
    client = await make_client()
    try:
        entity = await client.get_entity(peer_of(chat_id))
        print(f"聊天：{getattr(entity, 'title', None) or getattr(entity, 'username', None) or entity.id}"
              f"（id={entity.id}, 类型={type(entity).__name__}）")

        newest = await client.iter_messages(entity, limit=1).__anext__()
        print(f"\n[a] 最新消息 id = {newest.id}  时间 = {fmt(newest.date)}")
        print(f"    （/wl since 的上界；扫的是该 id 之后的消息）")

        now = datetime.now(timezone.utc)
        for label, delta in (("24h", timedelta(hours=24)), ("7 天", timedelta(days=7))):
            first = await client.iter_messages(
                entity, offset_date=now - delta, limit=1).__anext__()
            if first is None:
                print(f"[b] 近 {label}：无消息")
            else:
                print(f"[b] 近 {label} ≈ {newest.id - first.id} 条"
                      f"（id 差近似；边界消息 id={first.id} @ {fmt(first.date)}）")

        media = 0
        total = 0
        async for m in client.iter_messages(entity, limit=100):
            total += 1
            if getattr(m, "media", None) is not None:
                media += 1
        print(f"[c] 最近 {total} 条里带 media 的：{media} 条"
              f"（扫描只对 media 建任务，纯文本只推游标）")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
