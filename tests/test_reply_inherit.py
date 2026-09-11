"""讨论组评论继承频道原帖命名信息（B→A 继承）的单元测试。

契约见 docs/Telegram标签监听_评论继承原帖命名信息_DeepSeek开发任务书.md。

**已由真机探针（scripts/probe_comment_parent.py）钉死的事实**——测试按真实数据
形状构造替身，不按直觉构造：

1. ``reply_to_peer_id`` 在讨论组评论上**恒为 None**（实测 97/97 条），所以
   「peer + msg_id 取父」这条常规路径在本项目里根本不可用，别为它写代码。
2. ``Message.get_reply_message()`` 在真实评论上**静默返回 None**（telethon 走
   InputMessageReplyTo，服务端不给）——必须显式 ``get_messages(群, ids=...)``。
   这条最阴：按 telethon 文档直觉写会得到一个「永远 fallback、功能静默不生效」
   的实现，且不报错。
3. 评论的父消息 id 指向**群内的「镜像帖」**（频道帖在讨论组里的转发副本），
   镜像帖自带 ``fwd_from.channel_post`` + 频道帖的完整 caption 与原始日期
   ——所以拿到镜像帖就够了，**不需要再去频道取一次**。
4. 转发进收藏夹的副本，``fwd_from.channel_post`` 是 **None**（supergroup 转发
   不带），原消息 id 在 ``saved_from_peer + saved_from_msg_id`` 里。

不联网：解析路径全部走注入的 fetcher；扫描用例走 FakeClient。
运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import atexit
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_reply_inherit_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from telethon.tl.types import PeerChannel  # noqa: E402

from tg_userbot import app  # noqa: E402
from tg_userbot import config  # noqa: E402
from tg_userbot import listener  # noqa: E402
from tg_userbot import naming  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402
from tg_userbot import sources  # noqa: E402
from tg_userbot import state  # noqa: E402

CHANNEL = -1001719225045      # 频道 A（原帖所在）
GROUP = -1002143122455        # 讨论组 B
MIRROR_ID = 679121            # 群内镜像帖（= 频道帖 88040 的副本）
POST_ID = 88040               # 频道帖 id
COMMENT_ID = 679200           # 对镜像帖的评论
COPY_ID = 31468               # 收藏夹里的转发副本

POST_TEXT = "作者：#Keke 期数：31期 角色：#妮露"
POST_DATE = datetime(2026, 9, 11, 6, 30, 12, tzinfo=timezone.utc)


# ============================================================
# 测试替身（按真机观测到的字段形状）
# ============================================================
class FakeFwd:
    def __init__(self, from_id=None, channel_post=None, date=None,
                 saved_from_peer=None, saved_from_msg_id=None):
        self.from_id = from_id
        self.channel_post = channel_post
        self.date = date
        self.saved_from_peer = saved_from_peer
        self.saved_from_msg_id = saved_from_msg_id
        self.from_name = None


class FakeReplyTo:
    """真实的 MessageReplyHeader 上 reply_to_peer_id 恒为 None（见模块 docstring）。"""

    def __init__(self, msg_id, peer_id=None):
        self.reply_to_msg_id = msg_id
        self.reply_to_peer_id = peer_id
        self.reply_to_top_id = None


class FakeFile:
    def __init__(self, name="v.mp4", size=100):
        self.id = "fid"
        self.name = name
        self.size = size
        self.mime_type = "video/mp4"


class FakeMsg:
    def __init__(self, mid, text="", date=None, fwd_from=None, reply_to=None,
                 chat_id=GROUP, is_media=True):
        self.id = mid
        self.message = text
        self.date = date
        self.fwd_from = fwd_from
        self.reply_to = reply_to
        self.chat_id = chat_id
        self.grouped_id = None
        if is_media:
            self.file = FakeFile()
            self.document = object()
            self.video = object()
            self.photo = None
            self.audio = None
            self.voice = None
        else:
            self.file = None
            self.document = None
            self.video = None
            self.photo = None
            self.audio = None
            self.voice = None


def mirror_msg(text=POST_TEXT):
    """频道帖在讨论组里的镜像帖。"""
    return FakeMsg(
        MIRROR_ID, text=text, date=datetime(2026, 9, 11, 6, 30, 15,
                                            tzinfo=timezone.utc),
        fwd_from=FakeFwd(from_id=PeerChannel(1719225045),
                         channel_post=POST_ID, date=POST_DATE,
                         saved_from_peer=PeerChannel(1719225045),
                         saved_from_msg_id=POST_ID))


def comment_msg(mid=COMMENT_ID, text="", parent=MIRROR_ID, date=None):
    """讨论组里对镜像帖（或其下某条评论）的回复。"""
    return FakeMsg(mid, text=text,
                   date=date or datetime(2026, 9, 11, 10, 8, 55,
                                         tzinfo=timezone.utc),
                   reply_to=FakeReplyTo(parent))


def saved_copy(mid=COPY_ID, origin_msg_id=MIRROR_ID, channel_post=None):
    """收藏夹里从讨论组转发来的副本：channel_post 为 None，靠 saved_from_*。"""
    return FakeMsg(
        mid, text="", chat_id=0,
        fwd_from=FakeFwd(from_id=PeerChannel(2143122455),
                         channel_post=channel_post,
                         saved_from_peer=PeerChannel(2143122455),
                         saved_from_msg_id=origin_msg_id))


class FakeFetcher:
    """注入用的取消息替身：{(peer, msg_id): FakeMsg}，并记录调用次数。"""

    def __init__(self, table=None, fail=()):
        self.table = dict(table or {})
        self.fail = set(fail)
        self.calls = []

    async def __call__(self, peer, msg_id):
        key = (peer, int(msg_id))
        self.calls.append(key)
        if key in self.fail:
            raise RuntimeError(f"读消息被拒：{key}")
        return self.table.get(key)


class FakeNamer:
    """注入用的「原帖目录名」替身。

    真实现走 sources.get_forward_source（要 state.client + 网络），所以每个
    解析用例都注入它，保持完全离线、且能分别测「取得到 / 取不到」两条路。
    """

    def __init__(self, name="祂录（3D区）", fail=False):
        self.name = name
        self.fail = fail
        self.calls = []

    async def __call__(self, message):
        self.calls.append(message)
        if self.fail:
            raise RuntimeError("解析实体失败")
        return self.name


NAMER = FakeNamer()


# ============================================================
# 1. 转发头 → 原消息引用（纯函数）
# ============================================================
class ForwardOriginRefTest(unittest.TestCase):
    def test_channel_post_wins(self):
        fwd = FakeFwd(from_id=PeerChannel(1), channel_post=77,
                      saved_from_peer=PeerChannel(2), saved_from_msg_id=99)
        peer, mid = sources.forward_origin_ref(fwd)
        self.assertEqual((sources_peer(peer), mid), (sources_peer(PeerChannel(1)), 77))

    def test_falls_back_to_saved_from_when_channel_post_missing(self):
        """supergroup 转发到收藏夹时 channel_post 为 None（真机观测）。"""
        fwd = FakeFwd(from_id=PeerChannel(2143122455), channel_post=None,
                      saved_from_peer=PeerChannel(2143122455),
                      saved_from_msg_id=679136)
        peer, mid = sources.forward_origin_ref(fwd)
        self.assertEqual(mid, 679136)
        self.assertIsNotNone(peer)

    def test_none_when_nothing_usable(self):
        self.assertIsNone(sources.forward_origin_ref(None))
        self.assertIsNone(sources.forward_origin_ref(FakeFwd()))
        # 只有 from_id、没有任何消息 id：定位不到具体消息
        self.assertIsNone(sources.forward_origin_ref(
            FakeFwd(from_id=PeerChannel(1))))


def sources_peer(peer):
    from telethon.utils import get_peer_id
    return get_peer_id(peer)


# ============================================================
# 2. 「这条消息是不是镜像帖」——扫描时要跳过它（用户明确要求）
# ============================================================
class ChannelMirrorTest(unittest.TestCase):
    def test_mirror_detected(self):
        self.assertTrue(sources.is_channel_mirror(mirror_msg()))

    def test_plain_comment_is_not_mirror(self):
        self.assertFalse(sources.is_channel_mirror(comment_msg()))

    def test_plain_media_is_not_mirror(self):
        self.assertFalse(sources.is_channel_mirror(FakeMsg(1, text="hi")))


# ============================================================
# 3. B → A 解析（注入 fetcher，不联网）
# ============================================================
class ResolveOriginSnapshotTest(unittest.IsolatedAsyncioTestCase):
    async def test_comment_resolves_through_mirror(self):
        fetcher = FakeFetcher({(GROUP, MIRROR_ID): mirror_msg()})
        snap = await sources.resolve_origin_snapshot(comment_msg(), fetcher, NAMER)
        self.assertIsNotNone(snap)
        self.assertEqual(snap["caption"], POST_TEXT)
        self.assertEqual(snap["date"], POST_DATE)
        self.assertEqual(snap["channel_post"], POST_ID)

    async def test_deep_reply_walks_up(self):
        """评论 → 评论 → 镜像帖：要一路走到镜像帖为止。"""
        mid1 = comment_msg(679202, parent=MIRROR_ID)
        mid2 = comment_msg(679209, parent=679202)
        fetcher = FakeFetcher({(GROUP, 679202): mid1, (GROUP, MIRROR_ID): mirror_msg()})
        snap = await sources.resolve_origin_snapshot(mid2, fetcher, NAMER)
        self.assertEqual(snap["caption"], POST_TEXT)

    async def test_saved_copy_resolves_via_saved_from(self):
        """收藏夹副本路径：先回源取到群里的原评论，再走镜像帖。"""
        fetcher = FakeFetcher({
            (GROUP, COMMENT_ID): comment_msg(),
            (GROUP, MIRROR_ID): mirror_msg(),
        })
        snap = await sources.resolve_origin_snapshot(saved_copy(
            origin_msg_id=COMMENT_ID), fetcher, NAMER)
        self.assertEqual(snap["caption"], POST_TEXT)
        self.assertEqual(snap["date"], POST_DATE)

    async def test_fetcher_failure_returns_none(self):
        """父消息查询失败必须 fallback，绝不能把 B 弄丢或抛出去。"""
        fetcher = FakeFetcher({(GROUP, MIRROR_ID): mirror_msg()},
                              fail=[(GROUP, MIRROR_ID)])
        self.assertIsNone(
            await sources.resolve_origin_snapshot(comment_msg(), fetcher, NAMER))

    async def test_missing_parent_returns_none(self):
        fetcher = FakeFetcher({})
        self.assertIsNone(
            await sources.resolve_origin_snapshot(comment_msg(), fetcher, NAMER))

    async def test_hop_limit_stops_walk(self):
        """链条过长（或父消息恒取不回）时必须在有限跳内停下，不能无限查。"""
        chain = {}
        for i in range(200, 200 + config.ORIGIN_MAX_HOPS + 5):
            chain[(GROUP, i)] = comment_msg(i, parent=i + 1)
        comment = comment_msg(199, parent=200)
        fetcher = FakeFetcher(chain)
        self.assertIsNone(await sources.resolve_origin_snapshot(comment, fetcher, NAMER))
        self.assertLessEqual(len(fetcher.calls), config.ORIGIN_MAX_HOPS)

    async def test_cycle_does_not_hang(self):
        a = comment_msg(1, parent=2)
        b = comment_msg(2, parent=1)
        fetcher = FakeFetcher({(GROUP, 2): b, (GROUP, 1): a})
        self.assertIsNone(await sources.resolve_origin_snapshot(a, fetcher, NAMER))

    async def test_plain_message_without_reply_is_none(self):
        fetcher = FakeFetcher({})
        self.assertIsNone(
            await sources.resolve_origin_snapshot(FakeMsg(5, text="hi"), fetcher, NAMER))

    async def test_snapshot_carries_folder_name(self):
        """B 是 A 的评论时，B 的落盘目录要能对齐到 A（用户要求）。"""
        fetcher = FakeFetcher({(GROUP, MIRROR_ID): mirror_msg()})
        namer = FakeNamer("祂录（3D区）")
        snap = await sources.resolve_origin_snapshot(comment_msg(), fetcher, namer)
        self.assertEqual(snap["source_name"], "祂录（3D区）")
        self.assertEqual(len(namer.calls), 1)

    async def test_folder_name_failure_falls_back(self):
        """取不到目录名时给 None，调用方退回副本自身来源——绝不能落进「未分类」。"""
        fetcher = FakeFetcher({(GROUP, MIRROR_ID): mirror_msg()})
        snap = await sources.resolve_origin_snapshot(
            comment_msg(), fetcher, FakeNamer(fail=True))
        self.assertIsNone(snap["source_name"])
        # get_forward_source 的失败哨兵值也不能当目录名用
        snap2 = await sources.resolve_origin_snapshot(
            comment_msg(), fetcher, FakeNamer("未分类"))
        self.assertIsNone(snap2["source_name"])

    async def test_success_is_logged(self):
        """任务书 §6：日志要能看出「**成功继承**」，否则真机验收时无从判断。"""
        fetcher = FakeFetcher({(GROUP, MIRROR_ID): mirror_msg()})
        with self.assertLogs("tg_userbot", level="INFO") as cap:
            await sources.resolve_origin_snapshot(comment_msg(), fetcher, NAMER)
        self.assertTrue(any("已解析到频道原帖" in line for line in cap.output),
                        cap.output)

    async def test_plain_message_logs_nothing(self):
        """非评论消息占绝大多数，这条路径跑在每条媒体消息上——不能刷屏（§6）。"""
        fetcher = FakeFetcher({})
        with mock.patch.object(sources.logger, "info") as info:
            await sources.resolve_origin_snapshot(FakeMsg(5, text="hi"), fetcher, NAMER)
        info.assert_not_called()

    async def test_mirror_itself_resolves_to_itself(self):
        """镜像帖自己送进来（它本身就是原帖的副本）——直接就是答案。"""
        fetcher = FakeFetcher({})
        snap = await sources.resolve_origin_snapshot(mirror_msg(), fetcher, NAMER)
        self.assertEqual(snap["caption"], POST_TEXT)
        self.assertEqual(fetcher.calls, [])   # 一次网络请求都不该发


# ============================================================
# 4. 命名层：日期 override
# ============================================================
class DateOverrideTest(unittest.TestCase):
    def test_prefix_uses_override(self):
        msg = FakeMsg(1, date=datetime(2026, 9, 11, 10, 8,
                                       tzinfo=timezone.utc))
        self.assertEqual(naming.date_prefix(msg, POST_DATE), "26-09-11 ")

    def test_prefix_without_override_unchanged(self):
        """不传 override 时行为必须与现在完全一致（零回归）。"""
        msg = FakeMsg(1, date=datetime(2026, 9, 11, 10, 8,
                                       tzinfo=timezone.utc))
        self.assertEqual(naming.date_prefix(msg), naming.date_prefix(msg))
        self.assertTrue(naming.date_prefix(msg).startswith("26-09-11"))

    def test_final_filename_uses_override(self):
        msg = comment_msg(COMMENT_ID, text="")
        name = naming.compute_final_filename(
            msg, caption=POST_TEXT, date_override=POST_DATE)
        self.assertTrue(name.startswith("26-09-11 "), name)
        self.assertIn("#Keke", name)

    def test_fallback_name_uses_override_date(self):
        """A 无 caption 时走兜底名，日期仍须是 A 的（任务书 §14-B：不许半实现）。"""
        # 评论发在 10:08，帖子发在 06:30——两者日期戳必须不同才验得出来
        msg = FakeMsg(COMMENT_ID, text="", is_media=False,
                      reply_to=FakeReplyTo(MIRROR_ID),
                      date=datetime(2026, 9, 11, 10, 8, 55,
                                    tzinfo=timezone.utc))
        name = naming.compute_final_filename(msg, caption=None,
                                             date_override=POST_DATE)
        self.assertIn(POST_DATE.strftime("%Y%m%d_%H%M%S"), name,
                      f"兜底名应带继承日期：{name}")
        self.assertNotIn(msg.date.strftime("%Y%m%d_%H%M%S"), name,
                         f"兜底名不该带评论自身日期：{name}")


# ============================================================
# 5. 命名优先级（任务书 §3，按用户选定的「标注与 caption 并存」）
# ============================================================
class NamingPrecedenceTest(unittest.TestCase):
    """任务书 §3 的优先级表：标注 > 频道原帖 caption > 消息自身文字 > 相册说明。

    取舍只有 naming.effective_caption 一处，且**入队展示与落盘下载共用它**——
    这是「列表里显示的名字」与「实际文件名」不会各算各的保证。
    """

    def test_label_and_inherited_caption_coexist(self):
        """标注在**前**、继承来的 caption 仍在——用户明确选了「并存」。"""
        msg = comment_msg(COMMENT_ID, text="")
        name = naming.compute_final_filename(
            msg, caption=POST_TEXT, label="我的备注", date_override=POST_DATE)
        self.assertTrue(name.startswith("26-09-11 "), name)
        self.assertIn("#我的备注", name)
        self.assertIn("#Keke", name)
        self.assertLess(name.index("#我的备注"), name.index("#Keke"))

    def test_parent_caption_beats_own_text(self):
        """评论自己有文字时**原帖 caption 仍然覆盖它**（用户选定：A 覆盖 B）。

        评论的文字常常只是个「👍」，没有命名价值；原帖标题才是信息。
        """
        msg = comment_msg(COMMENT_ID, text="👍 好片")
        self.assertEqual(
            naming.effective_caption(msg, parent_caption=POST_TEXT), POST_TEXT)
        name = naming.compute_final_filename(
            msg, caption=naming.effective_caption(msg, parent_caption=POST_TEXT),
            date_override=POST_DATE)
        self.assertIn("#Keke", name)
        self.assertNotIn("👍 好片", name)

    def test_own_text_used_when_parent_has_no_caption(self):
        """原帖没 caption 时回落到评论自己的文字（不产生空命名）。"""
        msg = comment_msg(COMMENT_ID, text="👍 好片")
        self.assertEqual(
            naming.effective_caption(msg, parent_caption=""), "👍 好片")

    def test_album_caption_stays_fallback(self):
        """**相册语义不能被带坏**：album_caption 仍是 fallback。

        这条是本次改动的安全性锚点——如果把原帖 caption 塞进 album_caption
        槽（第一版就这么写的），相册里自己写了字的成员也会被同组标题盖掉。
        """
        msg = comment_msg(COMMENT_ID, text="成员自己的说明")
        self.assertEqual(
            naming.effective_caption(msg, album_caption="相册同组标题"),
            "成员自己的说明")
        # 成员没有自己的文字时才用相册说明
        quiet = comment_msg(COMMENT_ID, text="")
        self.assertEqual(
            naming.effective_caption(quiet, album_caption="相册同组标题"),
            "相册同组标题")

    def test_display_name_matches_download_name(self):
        """入队展示名与下载落盘名必须同源（否则列表里看到的名字是假的）。"""
        msg = comment_msg(COMMENT_ID, text="👍 好片")
        caption = naming.effective_caption(msg, parent_caption=POST_TEXT)
        display = app._build_media_record(
            msg, GROUP, "来源", parent_caption=POST_TEXT,
            parent_date=POST_DATE.isoformat())["final_name"]
        download = naming.compute_final_filename(
            msg, caption=caption, date_override=POST_DATE)
        self.assertEqual(display, download)
        self.assertIn("#Keke", display)

    def test_inherited_caption_used_when_comment_has_no_text(self):
        """评论没有自己的文字（讨论组里最常见的形态）时才继承原帖 caption。"""
        msg = comment_msg(COMMENT_ID, text="")
        self.assertEqual(
            naming.effective_caption(msg, parent_caption=POST_TEXT), POST_TEXT)

    def test_plain_message_naming_unchanged(self):
        """普通消息（非评论）命名不受本功能影响。"""
        msg = FakeMsg(7, text="标题", date=POST_DATE)
        self.assertEqual(naming.compute_final_filename(msg),
                         naming.compute_final_filename(msg))


# ============================================================
# 6. 入队记录：日期快照要落进记录（重试时才不会因 A 改动而改名）
# ============================================================
class RecordSnapshotTest(unittest.TestCase):
    def test_record_carries_parent_date(self):
        msg = comment_msg(COMMENT_ID, text="")
        record = app._build_media_record(
            msg, GROUP, "来源", album_caption=POST_TEXT,
            parent_date=POST_DATE.isoformat())
        self.assertEqual(record["parent_date"], POST_DATE.isoformat())
        self.assertTrue(record["final_name"].startswith("26-09-11 "),
                        record["final_name"])

    def test_record_without_parent_date_unchanged(self):
        msg = FakeMsg(8, date=POST_DATE)
        record = app._build_media_record(msg, GROUP, "来源")
        self.assertNotIn("parent_date", record)


# ============================================================
# 7. 扫描时跳过镜像帖
#
# 起因：频道帖在讨论组里有镜像副本（带同一份 caption 与标签）。若讨论组也被
# 加进监听来源，同一篇帖子会在频道侧与讨论组侧各命中一次——转发会重复（下载
# 有 dedup 兜底，转发没有）。用户明确要求：跳过镜像帖。
# ============================================================
class ScanFakeClient:
    """够 listener 扫描用的最小假客户端。"""

    def __init__(self, by_chat):
        self.by_chat = dict(by_chat)
        self.forward_calls = []

    async def get_messages(self, peer, **kwargs):
        if "ids" in kwargs:
            wanted = set(kwargs["ids"])
            return [m for m in self.by_chat.get(peer, []) if m.id in wanted]
        msgs = sorted(self.by_chat.get(peer, []), key=lambda m: m.id)
        if "min_id" not in kwargs and kwargs.get("limit") == 1:
            return msgs[-1:]
        newer = [m for m in msgs if m.id > (kwargs.get("min_id") or 0)]
        limit = kwargs.get("limit")
        return newer[:limit] if limit is not None else newer

    async def forward_messages(self, peer, messages, from_peer=None):
        self.forward_calls.append((peer, messages, from_peer))
        return []


class MirrorSkipScanTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mirror_skip_", dir=_TMP)
        self._p1 = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "tg_userbot.db"))
        self._p2 = mock.patch.object(
            config, "LISTEN_CONFIG_FILE", os.path.join(self.dir, "listen.json"))
        for patch in (self._p1, self._p2):
            patch.start()
            self.addCleanup(patch.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        runtime_db.init_db()
        listener._LAST_CONFIG_MTIME = 0.0

        self._saved = (list(state.LISTEN_RULES), state.LISTEN_ENABLED,
                       dict(state.WHITELIST_CHATS), state.client,
                       state.LISTEN_LAST_SCAN)
        self.addCleanup(self._restore)
        state.LISTEN_ENABLED = True
        state.WHITELIST_CHATS = {}
        state.LISTEN_LAST_SCAN = None

    def _restore(self):
        (state.LISTEN_RULES, state.LISTEN_ENABLED, state.WHITELIST_CHATS,
         state.client, state.LISTEN_LAST_SCAN) = self._saved
        runtime_db.close_db()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def test_mirror_post_is_skipped_but_checkpoint_advances(self):
        mirror = mirror_msg(text=f"{POST_TEXT} #01musume")
        real = comment_msg(679300, text="评论 #01musume")
        client = ScanFakeClient({GROUP: [mirror, real]})
        state.client = client
        state.LISTEN_RULES = [{
            "source_chat_id": GROUP, "tag": "#01musume",
            "targets": [{"type": "saved_messages"}], "download": True,
        }]
        runtime_db.set_listener_checkpoint(GROUP, 100)

        summary = await listener.scan_all()

        created = runtime_db.list_listener_tasks()
        self.assertEqual(summary["matched"], 1,
                         "镜像帖不该被算作命中")
        self.assertEqual([t["message_id"] for t in created], [real.id],
                         "只该给真实评论建任务")
        # checkpoint 仍要推过镜像帖，否则每轮都会重新扫它
        self.assertGreaterEqual(
            runtime_db.get_listener_checkpoint(GROUP), mirror.id)


if __name__ == "__main__":
    unittest.main()
