"""冒烟探针：讨论组评论能否确认它的「频道原帖」（评论继承命名信息的前提验证）。

《评论继承原帖命名信息》任务书的第 0 步——在写任何产品代码之前，先真机确认
「B→A」这条链在 Telethon 1.44 上到底走得通哪几条路。

要回答三个问题：

  0a. 讨论组评论消息的 ``reply_to`` 是否带 ``reply_to_peer_id``；
      ``get_reply_message()``（内部走 InputMessageReplyTo，服务端解析跨 peer）
      能否直接取回频道原帖 A。
  0b. 转发进收藏夹的副本，``fwd_from`` 里到底有什么（channel_post / saved_from_*），
      用哪个字段能**回源**取到 B 的原消息。
  0c. 从 B 的原消息再读 ``reply_to`` - 能不能拿到 A。

为什么是独立脚本、**不 import tg_userbot**：import 包会在 import 期
``log.configure(LOG_FILE)``，给 ``runtime/download.log`` 挂上第二个
TimedRotatingFileHandler——而正在运行的 userbot 进程持有一个。两个进程写同一个
按天轮转的日志文件，午夜会互相覆盖归档（见 issues/001）。探针是短命的，但没
必要去碰这个坑；直接读 tg_secrets.json + 裸 telethon 更干净。

同样为了不干扰生产进程：session 以**只读**方式打开（sqlite URI mode=ro）拷出
授权快照喂 MemorySession，连接用 ``receive_updates=False``（与 workers.py 同一
套做法）——主客户端仍是唯一的更新消费者，探针不会抢走它的消息事件。

用法（代理是必须的，直连在国内网络下连不上 Telegram）::

    TG_PROXY=socks5://127.0.0.1:12334 \\
        .venv/bin/python scripts/probe_comment_parent.py [-1001719225045]

不带参数时默认读 runtime/listen.json 的第一条监听规则作为源聊天。
"""
import asyncio
import json
import os
import sqlite3
import sys
from urllib.parse import urlparse

from telethon import TelegramClient
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession
from telethon.tl.functions.channels import GetFullChannelRequest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSION_PATH = os.path.expanduser("~/tg_downloader.session")


def find_listen_json():
    """定位 runtime/listen.json。

    非媒体运行时文件都在 ``SAVE_FOLDER/runtime/`` 下（不在仓库根）——平台默认值
    是 macOS ``~/Downloads/Nagram``、Termux 的 ``/storage/emulated/0/Download/Nagram``。
    这里刻意不 import tg_userbot.config（见模块 docstring：import 会给正在运行的
    进程共用的 download.log 挂第二个轮转 handler），所以照它的规则自己找一遍。
    """
    candidates = []
    env = (os.environ.get("TG_SAVE_FOLDER") or "").strip()
    if env:
        candidates.append(os.path.join(env, "runtime", "listen.json"))
    candidates.append(os.path.join(
        os.path.expanduser("~/Downloads/Nagram"), "runtime", "listen.json"))
    candidates.append(os.path.join(
        "/storage/emulated/0/Download/Nagram", "runtime", "listen.json"))
    candidates.append(os.path.join(REPO_ROOT, "runtime", "listen.json"))
    for path in candidates:
        if os.path.exists(path):
            return path
    raise FileNotFoundError(
        "找不到 runtime/listen.json，试过：\n  " + "\n  ".join(candidates))


# ============================================================
# 连接（只读 session + 不接收更新）
# ============================================================
def load_secrets():
    with open(os.path.join(REPO_ROOT, "tg_secrets.json"), encoding="utf-8") as f:
        data = json.load(f)
    return int(data["api_id"]), str(data["api_hash"])


def read_session_snapshot():
    """只读打开 session 库，拷出授权快照（不写、不加锁、不干扰运行中的进程）。"""
    con = sqlite3.connect(f"file:{SESSION_PATH}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT dc_id, server_address, port, auth_key, tmp_auth_key "
            "FROM sessions LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise RuntimeError("session 库里没有可用授权记录")
    dc_id, addr, port, auth_key, tmp_auth_key = row
    auth_key = auth_key or tmp_auth_key
    if not (dc_id and addr and port and auth_key):
        raise RuntimeError(f"session 授权字段不全：dc={dc_id} addr={addr} port={port}")
    return dc_id, addr, port, AuthKey(bytes(auth_key))


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
    dc_id, addr, port, auth_key = read_session_snapshot()
    session = MemorySession()
    session.set_dc(dc_id, addr, port)
    session.auth_key = auth_key
    client = TelegramClient(
        session, api_id, api_hash,
        connection_retries=3, retry_delay=2,
        auto_reconnect=False, proxy=parse_proxy(),
        receive_updates=False,
    )
    await asyncio.wait_for(client.connect(), timeout=60)
    return client


# ============================================================
# 打印助手
# ============================================================
def dump_reply_to(msg, indent="    "):
    rt = getattr(msg, "reply_to", None)
    if rt is None:
        print(f"{indent}reply_to: None（不是回复）")
        return
    print(f"{indent}reply_to type={type(rt).__name__}")
    for field in ("reply_to_msg_id", "reply_to_peer_id", "reply_to_top_id",
                  "top_msg_id", "forum_topic", "quote_text"):
        if hasattr(rt, field):
            print(f"{indent}  {field} = {short(getattr(rt, field))}")


def dump_fwd_from(msg, indent="    "):
    fwd = getattr(msg, "fwd_from", None)
    if fwd is None:
        print(f"{indent}fwd_from: None（不是转发）")
        return
    print(f"{indent}fwd_from type={type(fwd).__name__}")
    for field in ("from_id", "from_name", "date", "channel_post", "post_author",
                  "saved_from_peer", "saved_from_msg_id", "psa_type"):
        if hasattr(fwd, field):
            print(f"{indent}  {field} = {short(getattr(fwd, field))}")


def short(value):
    text = str(value)
    return text if len(text) <= 90 else text[:87] + "..."


def describe(msg, indent="    "):
    if msg is None:
        print(f"{indent}(None)")
        return
    text = (getattr(msg, "message", "") or "").replace("\n", " ")[:80]
    print(f"{indent}id={getattr(msg, 'id', None)} date={getattr(msg, 'date', None)}")
    print(f"{indent}text={text!r}")
    print(f"{indent}grouped_id={getattr(msg, 'grouped_id', None)} "
          f"media={'有' if getattr(msg, 'media', None) else '无'}")


# ============================================================
# 探针主体
# ============================================================
async def probe_linked_group(client, source_chat_id):
    """0a + 0c：源聊天 → 讨论组 → 评论 → 原帖。"""
    print(f"\n【1】解析源聊天 {source_chat_id}")
    entity = await client.get_entity(source_chat_id)
    print(f"    实体：{type(entity).__name__} title={getattr(entity, 'title', None)}")

    print("\n【2】查关联讨论组（linked_chat_id）")
    try:
        full = await client(GetFullChannelRequest(await client.get_input_entity(entity)))
    except Exception as e:
        print(f"    ❌ GetFullChannel 失败：{type(e).__name__}: {e}")
        return None
    linked_id = getattr(full.full_chat, "linked_chat_id", None)
    print(f"    linked_chat_id = {linked_id}")
    if not linked_id:
        print("    ⚠️ 该聊天没有关联讨论组——0a 无法从这里验证")
        return None

    print(f"\n【3】读讨论组 {linked_id} 的近期消息，把「评论」分类")
    group = await client.get_entity(linked_id)
    print(f"    讨论组：{getattr(group, 'title', None)}")

    print("\n【3b】反向确认：讨论组的 full info 是否指回频道 A")
    try:
        gfull = await client(GetFullChannelRequest(
            await client.get_input_entity(group)))
        print(f"    讨论组 linked_chat_id = "
              f"{getattr(gfull.full_chat, 'linked_chat_id', None)}"
              f"（应等于 {source_chat_id}）")
    except Exception as e:
        print(f"    ❌ {type(e).__name__}: {e}")

    print("\n【3c】从**频道帖子**往下取评论区（找真正的「对频道帖子的评论」）")
    posts = await client.get_messages(source_chat_id, limit=30)
    with_replies = [p for p in posts if getattr(p, "replies", None) is not None]
    print(f"    频道近 {len(posts)} 条帖子里有 {len(with_replies)} 条带评论区")
    sample_comment = None
    for post in with_replies[:5]:
        rep = post.replies
        print(f"\n    · 帖子 #{post.id} date={post.date}")
        print(f"      replies.comments={getattr(rep, 'comments', None)} "
              f"count={getattr(rep, 'replies', None)} "
              f"channel_id={getattr(rep, 'channel_id', None)}")
        try:
            from telethon.tl.functions.messages import GetRepliesRequest
            res = await asyncio.wait_for(client(GetRepliesRequest(
                peer=await client.get_input_entity(source_chat_id),
                msg_id=post.id, offset_id=0, offset_date=None,
                add_offset=0, limit=20, max_id=0, min_id=0, hash=0,
            )), timeout=30)
            got = [m for m in res.messages if getattr(m, "reply_to", None)]
            print(f"      GetReplies 返回 {len(res.messages)} 条，"
                  f"其中 {len(got)} 条带 reply_to")
            for m in got[:3]:
                rt = m.reply_to
                print(f"        #{m.id} msg_id={rt.reply_to_msg_id} "
                      f"peer_id={short(rt.reply_to_peer_id)} "
                      f"top_id={getattr(rt, 'reply_to_top_id', None)} "
                      f"media={'有' if getattr(m, 'media', None) else '无'}")
                if sample_comment is None:
                    sample_comment = m
            if sample_comment is not None:
                break
        except Exception as e:
            print(f"      ❌ GetReplies 失败：{type(e).__name__}: {e}")

    if sample_comment is not None:
        print("\n【3d】评论的父消息 id 到底能不能取回来（链路的关键一环）")
        parent_id = sample_comment.reply_to.reply_to_msg_id
        post_id = None
        for post in with_replies[:5]:
            pass
        print(f"    评论 #{sample_comment.id} 声称在回复 {parent_id}")
        for label, kwargs in (
            ("ids=[parent]（批量形态）", {"ids": [parent_id]}),
            ("ids=parent（单值形态）", {"ids": parent_id}),
        ):
            try:
                got = await asyncio.wait_for(
                    client.get_messages(group, **kwargs), timeout=30)
                if isinstance(got, list):
                    got = got[0] if got else None
                print(f"    · get_messages(group, {label}) → "
                      f"{'None' if got is None else '取到了'}")
                if got is not None:
                    describe(got, indent="        ")
                    dump_fwd_from(got, indent="        ")
            except Exception as e:
                print(f"    · {label} ❌ {type(e).__name__}: {e}")

        print(f"    · 直接在群里取 id 区间 [{parent_id - 3}, {parent_id + 3}]：")
        try:
            near = await asyncio.wait_for(client.get_messages(
                group, min_id=parent_id - 3, max_id=parent_id + 3, limit=20),
                timeout=30)
            for m in near:
                print(f"        #{m.id} date={m.date} "
                      f"text={(getattr(m, 'message', '') or '')[:40]!r} "
                      f"fwd={'有' if getattr(m, 'fwd_from', None) else '无'} "
                      f"media={'有' if getattr(m, 'media', None) else '无'}")
        except Exception as e:
            print(f"      ❌ {type(e).__name__}: {e}")

        print("    · 反查：这条群消息 id 在**频道 A** 里取得到吗")
        try:
            probe_chan = await asyncio.wait_for(
                client.get_messages(source_chat_id, ids=parent_id), timeout=30)
            print(f"      → {'None' if probe_chan is None else '取到了（id 空间重合？）'}")
        except Exception as e:
            print(f"      ❌ {type(e).__name__}: {e}")

    messages = await client.get_messages(group, limit=200)
    if sample_comment is not None:
        print("\n    拿 GetReplies 取到的评论做进一步验证：")
        describe(sample_comment)
        dump_reply_to(sample_comment)
    replies = [m for m in messages if getattr(m, "reply_to", None)]
    print(f"    近 {len(messages)} 条里有 {len(replies)} 条带 reply_to")
    if not replies:
        print("    ⚠️ 没有评论样本——请先在讨论组里对频道帖子发一条评论再跑")
        return None

    # reply_to_msg_id 能不能在**本群**里取回：能 = 群内互相回复；不能 = 指向别处
    # （频道原帖）。一次批量取，避免逐条发请求。
    ids = [m.reply_to.reply_to_msg_id for m in replies
           if getattr(m.reply_to, "reply_to_msg_id", None)]
    fetched = await client.get_messages(group, ids=ids) if ids else []
    if not isinstance(fetched, list):
        fetched = [fetched]
    in_group = {m.id for m in fetched if m is not None}

    cross_peer, intra, external = [], [], []
    for m in replies:
        rt = m.reply_to
        if getattr(rt, "reply_to_peer_id", None) is not None:
            cross_peer.append(m)
        elif getattr(rt, "reply_to_msg_id", None) in in_group:
            intra.append(m)
        else:
            external.append(m)

    print(f"    · 带 reply_to_peer_id（跨 peer 回复）      ：{len(cross_peer)} 条")
    print(f"    · 群内互相回复（父消息在本群）            ：{len(intra)} 条")
    print(f"    · 父消息不在本群（疑似指向频道原帖）      ：{len(external)} 条")

    print("\n    前 8 条的 reply_to 明细：")
    for m in replies[:8]:
        rt = m.reply_to
        print(f"      #{m.id} msg_id={rt.reply_to_msg_id} "
              f"peer_id={short(rt.reply_to_peer_id)} "
              f"top_id={getattr(rt, 'reply_to_top_id', None)} "
              f"media={'有' if getattr(m, 'media', None) else '无'}")

    candidates = [sample_comment] if sample_comment is not None else (
        cross_peer or external)
    if not candidates:
        print("\n    ⚠️ 这一批里没有「指向频道原帖」的评论——换个时间窗或加 limit 再试")
        return linked_id

    sample = candidates[0]
    print(f"\n【4】拿 #{sample.id} 验证取父路径（候选来源："
          f"{'reply_to_peer_id' if cross_peer else '父消息不在本群'}）")
    describe(sample)
    dump_reply_to(sample)
    dump_fwd_from(sample)

    rt = sample.reply_to
    msg_id = rt.reply_to_msg_id
    peer = getattr(rt, "reply_to_peer_id", None)

    print("\n  (a) get_reply_message()（内部 InputMessageReplyTo，服务端解析跨 peer）")
    try:
        parent = await asyncio.wait_for(sample.get_reply_message(), timeout=30)
        if parent is None:
            print("      ❌ 返回 None")
        else:
            describe(parent, indent="      ")
            pcid = getattr(getattr(parent, "peer_id", None), "channel_id", None)
            print(f"      父消息所在 channel_id = {pcid}"
                  f"（{'= 频道 A 的帖子' if pcid else '拿不到 channel_id'}）")
    except Exception as e:
        print(f"      ❌ {type(e).__name__}: {e}")

    print(f"\n  (b) reply_to_peer_id + reply_to_msg_id"
          f"（peer={short(peer)} id={msg_id}）")
    if peer is None or msg_id is None:
        print("      ⚠️ reply_to_peer_id 为 None——这条路径在这条消息上不可用")
    else:
        try:
            from telethon.utils import get_peer_id
            resolved = get_peer_id(peer)
            print(f"      解析成 marked id = {resolved}")
            parent = await asyncio.wait_for(
                client.get_messages(resolved, ids=msg_id), timeout=30)
            describe(parent, indent="      ")
        except Exception as e:
            print(f"      ❌ {type(e).__name__}: {e}")

    print(f"\n  (c) 用「讨论组的 linked_chat_id」反查 A，取 ids={msg_id}")
    try:
        parent = await asyncio.wait_for(
            client.get_messages(source_chat_id, ids=msg_id), timeout=30)
        if parent is None:
            print("      ❌ 返回 None（说明这个 id 不是频道 A 的帖子号）")
        else:
            describe(parent, indent="      ")
            print("      ✅ 这条反查路径可用——不依赖 reply_to_peer_id")
    except Exception as e:
        print(f"      ❌ {type(e).__name__}: {e}")
    return linked_id


async def probe_saved_copies(client, linked_id):
    """0b：收藏夹里从讨论组转发来的副本，能不能回源取到 B 的原消息。"""
    print("\n【5】收藏夹里的转发副本 → 能否回源取到原消息（0b）")
    from telethon.utils import get_peer_id
    # linked_chat_id 是**裸** id，get_peer_id() 返回的是 -100… 标记 id，必须对齐
    # 了再比——否则永远不相等，「收藏夹里没有副本」会是假结论（第一版就踩了）。
    group_marked = get_peer_id(await client.get_input_entity(linked_id))
    print(f"    讨论组标记 id = {group_marked}")

    me = await client.get_entity("me")
    messages = await client.get_messages(me, limit=300)
    print(f"    收藏夹近 {len(messages)} 条")

    copies = []
    for m in messages:
        fwd = getattr(m, "fwd_from", None)
        if not fwd or not getattr(fwd, "from_id", None):
            continue
        try:
            if get_peer_id(fwd.from_id) == group_marked:
                copies.append(m)
        except Exception:
            continue

    if not copies:
        print("    ⚠️ 收藏夹里没有从该讨论组转发来的副本")
        print("       请手动从讨论组转发一条评论到收藏夹，然后重跑本脚本")
        return
    print(f"    其中来自讨论组 {linked_id} 的副本：{len(copies)} 条")

    sample = copies[0]
    print("\n    样本副本：")
    describe(sample)
    dump_fwd_from(sample)

    fwd = sample.fwd_from
    from_id = getattr(fwd, "from_id", None)
    post = getattr(fwd, "channel_post", None)
    saved_peer = getattr(fwd, "saved_from_peer", None)
    saved_id = getattr(fwd, "saved_from_msg_id", None)

    candidates = []
    if from_id is not None and post:
        candidates.append(("fwd_from.from_id + channel_post", from_id, post))
    if saved_peer is not None and saved_id:
        candidates.append(("fwd_from.saved_from_peer + saved_from_msg_id",
                           saved_peer, saved_id))

    if not candidates:
        print("\n    ❌ fwd_from 里没有可用的「原消息 id」字段——回源不可行")
        print("       （只有 from_id 能定位聊天，定不到具体消息）")
        return

    print("\n    回源尝试：")
    for label, peer, mid in candidates:
        print(f"  · {label}（ids={mid}）")
        try:
            original = await asyncio.wait_for(
                client.get_messages(peer, ids=mid), timeout=30)
        except Exception as e:
            print(f"      ❌ {type(e).__name__}: {e}")
            continue
        if original is None:
            print("      ❌ 返回 None")
            continue
        describe(original, indent="      ")
        print("      ↓ 回源到的原消息的 reply_to（能否链到频道原帖 A）：")
        dump_reply_to(original, indent="        ")
        rt = getattr(original, "reply_to", None)
        if rt is not None and getattr(rt, "reply_to_peer_id", None):
            print("      ✅ 可以继续链到 A ——「手动转发」路径可行")
        elif rt is not None:
            print("      ⚠️ 有 reply_to 但缺 reply_to_peer_id ——需要 A 的讨论组反查兜底")
        else:
            print("      ❌ 原消息不是回复——这条副本不属于任何频道帖子")


async def main():
    source_chat_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
    if source_chat_id is None:
        listen_path = find_listen_json()
        print(f"（未传参数，读 {listen_path}）")
        with open(listen_path, encoding="utf-8") as f:
            rules = json.load(f).get("listeners") or []
        if not rules:
            print("listen.json 里没有规则，请显式传入源聊天 id")
            return
        source_chat_id = int(rules[0]["source_chat_id"])
        print(f"（未传参数，取 listen.json 第一条规则的来源：{source_chat_id}）")

    client = await make_client()
    try:
        print(f"✅ 已连接（只读 session、不接收更新）dc={client.session.dc_id}")
        linked_id = await probe_linked_group(client, source_chat_id)
        if linked_id:
            await probe_saved_copies(client, linked_id)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
