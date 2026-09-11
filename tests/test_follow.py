"""评论跟进（关注列表）：命中标签的帖子 → 之后按天跟进它的评论区。

需求（2026-09-12 用户定的一版参数）：
* 标签命中的帖子进关注列表，**有效期 15 天**；
* **跟进周期 1 天一次**——不是每轮扫描（30 分钟）都去查；
* 关注列表**上限 500 条**（背压：满了不再新增，但帖子本身的下载任务照建）；
* 单帖单次最多取 **100 条评论**；
* **过期置为「失效」而不是删除**（留下「到底有没有等到讨论串」的证据）；
* 每次真发现可下载内容，**必须走去重并加入索引**——这条由现有机制覆盖：
  重复评论被 listener_tasks 的唯一索引挡住，下载层被 dedup 的判重键挡住。

为什么要有这个功能：频道主常把差分图放在**评论区**，而评论区在讨论组里、
监听频道的 Scanner 看不到；把整个群加白名单又会全盘接收（风控 + 不需要）。
所以只跟进「命中标签的那几条帖子」。

不联网：取评论区走注入的 fetcher；扫描用例走 FakeClient。
运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import atexit
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_follow_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import listener  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402
from tg_userbot import state  # noqa: E402

CHANNEL = -1001719225045
GROUP = -1002143122455
POST_ID = 88032
POST_TEXT = "作者：#Keke 期数：33期 角色：#芙宁娜"
POST_DATE = "2026-09-11T06:20:12+00:00"
SOURCE_NAME = "祂录（3D区）"


class FakeFile:
    def __init__(self, name="v.mp4"):
        self.id = "fid"
        self.name = name
        self.size = 100
        self.mime_type = "video/mp4"


class FakeMsg:
    def __init__(self, mid, text="", is_media=True, grouped_id=None):
        self.id = mid
        self.message = text
        self.grouped_id = grouped_id
        self.chat_id = GROUP
        self.date = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
        self.fwd_from = None
        self.reply_to = None
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


class FakeReplies:
    """取评论区的替身。

    契约与 ``listener._fetch_replies`` 一致：返回 ``(评论列表 或 None, 失败原因
    或 None)``。``fail`` 里的键则直接抛异常——用来验「单帖抛错不打断整个循环」。
    """

    def __init__(self, table=None, fail=()):
        self.table = dict(table or {})
        self.fail = set(fail)
        self.calls = []

    async def __call__(self, channel_id, post_id, limit):
        key = (int(channel_id), int(post_id))
        self.calls.append(key)
        if key in self.fail:
            raise RuntimeError(f"取评论区失败：{key}")
        if key not in self.table:
            return None, "尚无讨论串"
        return self.table[key]


class FollowBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="follow_", dir=_TMP)
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

    def _add(self, post_id=POST_ID, **kw):
        args = dict(channel_id=CHANNEL, post_id=post_id,
                    source_chat_id=GROUP, caption=POST_TEXT,
                    post_date=POST_DATE, source_name=SOURCE_NAME)
        args.update(kw)
        return runtime_db.add_listener_follow(**args)

    def _tasks(self):
        return runtime_db.list_listener_tasks()


# ============================================================
# 1. 关注列表存储（runtime_db）
# ============================================================
class FollowStoreTest(FollowBase):
    def test_add_returns_id_and_snapshot(self):
        fid = self._add()
        self.assertIsNotNone(fid)
        row = runtime_db.list_listener_follows()[0]
        self.assertEqual(row["status"], runtime_db.FOLLOW_ACTIVE)
        self.assertEqual(row["caption"], POST_TEXT)
        self.assertEqual(row["post_date"], POST_DATE)
        self.assertEqual(row["source_name"], SOURCE_NAME)

    def test_same_post_added_once(self):
        self.assertIsNotNone(self._add())
        self.assertIsNone(self._add(), "同一个帖子不该被关注两次")
        self.assertEqual(len(runtime_db.list_listener_follows()), 1)

    def test_cap_blocks_new_follows(self):
        with mock.patch.object(config, "LISTEN_FOLLOW_MAX", 2):
            self.assertIsNotNone(self._add(post_id=1))
            self.assertIsNotNone(self._add(post_id=2))
            self.assertIsNone(self._add(post_id=3), "满了不该再新增")
        self.assertEqual(runtime_db.count_listener_follows(
            runtime_db.FOLLOW_ACTIVE), 2)

    def test_cap_counts_only_active(self):
        """失效的不占额度——否则用满 500 条之后永远加不进新的。"""
        with mock.patch.object(config, "LISTEN_FOLLOW_MAX", 1):
            self.assertIsNotNone(self._add(post_id=1))
            self.assertIsNone(self._add(post_id=2))
            runtime_db.expire_listener_follows(now=10 ** 12)
            self.assertIsNotNone(self._add(post_id=3))

    def test_due_selection_respects_interval(self):
        self._add()
        now = 1_000_000
        self.assertEqual(len(runtime_db.list_due_follows(
            interval_seconds=3600, now=now)), 1, "从没查过 → 到期")
        runtime_db.touch_listener_follow(1, now=now)
        self.assertEqual(len(runtime_db.list_due_follows(
            interval_seconds=3600, now=now + 60)), 0, "间隔内不该到期")
        self.assertEqual(len(runtime_db.list_due_follows(
            interval_seconds=3600, now=now + 3601)), 1, "超过间隔又到期")

    def test_expire_marks_not_deletes(self):
        self._add()
        runtime_db.expire_listener_follows(now=10 ** 12)
        rows = runtime_db.list_listener_follows()
        self.assertEqual(len(rows), 1, "过期是置失效，不是删除")
        self.assertEqual(rows[0]["status"], runtime_db.FOLLOW_EXPIRED)
        self.assertEqual(runtime_db.count_listener_follows(
            runtime_db.FOLLOW_ACTIVE), 0)

    def test_trim_expired_keeps_newest(self):
        for i in range(1, 6):
            self._add(post_id=i)
        runtime_db.expire_listener_follows(now=10 ** 12)
        runtime_db.trim_expired_follows(keep=2)
        rows = runtime_db.list_listener_follows()
        self.assertEqual([r["post_id"] for r in rows], [5, 4])


# ============================================================
# 2. 跟进扫描（listener.follow_scan）
# ============================================================
class FollowScanTest(FollowBase):
    async def _scan(self, fetcher, sleep=None):
        calls = []

        async def fake_sleep(seconds):
            calls.append(seconds)

        result = await listener.follow_scan(
            fetcher=fetcher, sleep=(sleep or fake_sleep))
        return result, calls

    async def test_downloadable_comments_become_tasks(self):
        self._add()
        comment = FakeMsg(679500)
        fetcher = FakeReplies({(CHANNEL, POST_ID): ([comment], None)})
        result, _ = await self._scan(fetcher)

        self.assertEqual(result["created"], 1)
        tasks = self._tasks()
        self.assertEqual(len(tasks), 1)
        t = tasks[0]
        self.assertEqual(t["source_chat_id"], GROUP)
        self.assertEqual(t["message_id"], comment.id)
        self.assertEqual(t["target_type"], "saved_messages")
        self.assertTrue(t["download"])

    async def test_comment_task_carries_post_snapshot(self):
        """评论任务必须带原帖的 caption/日期/目录——命名继承靠它。"""
        self._add()
        fetcher = FakeReplies({(CHANNEL, POST_ID): ([FakeMsg(679501)], None)})
        await self._scan(fetcher)
        payload = self._tasks()[0]["payload"]
        self.assertEqual(payload.get("parent_caption"), POST_TEXT)
        self.assertEqual(payload.get("parent_date"), POST_DATE)
        self.assertEqual(payload.get("source_name"), SOURCE_NAME)

    async def test_non_media_comments_ignored(self):
        self._add()
        fetcher = FakeReplies({(CHANNEL, POST_ID): (
            [FakeMsg(1, text="沙发", is_media=False), FakeMsg(2)], None)})
        result, _ = await self._scan(fetcher)
        self.assertEqual(result["created"], 1)
        self.assertEqual([t["message_id"] for t in self._tasks()], [2])

    async def test_repeated_scan_does_not_duplicate_tasks(self):
        """重复扫描靠唯一索引挡重复：同一条评论只建一次任务。"""
        self._add()
        fetcher = FakeReplies({(CHANNEL, POST_ID): ([FakeMsg(679502)], None)})
        await self._scan(fetcher)
        # 让关注再次到期，再扫一遍
        runtime_db.touch_listener_follow(1, now=0)
        result, _ = await self._scan(fetcher)
        self.assertEqual(result["created"], 0)
        self.assertEqual(len(self._tasks()), 1, "同一条评论不该建第二条任务")

    async def test_missing_thread_is_tolerated(self):
        """还没生成讨论串：不建任务、不崩、记下错误等下一轮。"""
        self._add()
        fetcher = FakeReplies({(CHANNEL, POST_ID): (None, "尚无讨论串")})
        result, _ = await self._scan(fetcher)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["no_thread"], 1)
        self.assertEqual(self._tasks(), [])
        row = runtime_db.list_listener_follows()[0]
        self.assertEqual(row["checks"], 1, "失败也要记一次检查次数")
        self.assertIsNotNone(row["last_error"])

    async def test_fetcher_exception_does_not_break_scan(self):
        """单帖取评论区抛错不能打断整个跟进循环（稳定性 §23）。"""
        self._add(post_id=1)
        self._add(post_id=2)
        fetcher = FakeReplies({(CHANNEL, 2): ([FakeMsg(679600)], None)},
                              fail=[(CHANNEL, 1)])
        result, _ = await self._scan(fetcher)
        self.assertEqual(result["created"], 1, "后面那条帖子照常处理")

    async def test_throttle_between_posts(self):
        """每帖之间要节流：500 条连起来发就是个突发。"""
        self._add(post_id=1)
        self._add(post_id=2)
        self._add(post_id=3)
        fetcher = FakeReplies({(CHANNEL, i): ([], None) for i in (1, 2, 3)})
        _, sleeps = await self._scan(fetcher)
        self.assertEqual(len(sleeps), 2, "3 条关注之间只该有 2 次间隔")
        self.assertTrue(all(s >= config.LISTEN_FOLLOW_MIN_INTERVAL_SECONDS
                            for s in sleeps))

    async def test_expired_follows_not_scanned(self):
        self._add()
        runtime_db.expire_listener_follows(now=10 ** 12)
        fetcher = FakeReplies({(CHANNEL, POST_ID): ([FakeMsg(679700)], None)})
        result, _ = await self._scan(fetcher)
        self.assertEqual(fetcher.calls, [], "失效的帖子不该再发请求")
        self.assertEqual(result["created"], 0)

    async def test_scan_expires_due_follows(self):
        """扫描每轮都要先把到期的置失效（不删）。"""
        self._add()
        result, _ = await self._scan(FakeReplies({}), )
        self.assertEqual(result["expired"], 0)
        self.assertEqual(
            runtime_db.count_listener_follows(runtime_db.FOLLOW_ACTIVE), 1)

    async def test_group_in_download_whitelist_is_skipped(self):
        """源群已在下载白名单时实时链路已经转发过，跟进不再重复建任务（§14）。"""
        state.WHITELIST_CHATS = {GROUP: "祂录（3D区）群组"}
        self._add()
        fetcher = FakeReplies({(CHANNEL, POST_ID): ([FakeMsg(679800)], None)})
        result, _ = await self._scan(fetcher)
        self.assertEqual(result["created"], 0)
        self.assertEqual(self._tasks(), [])


# ============================================================
# 3. Scanner 命中标签时把帖子写进关注列表
# ============================================================
class FollowOnHitTest(FollowBase):
    def _rule(self):
        return {"source_chat_id": CHANNEL, "tag": "#01musume",
                "targets": [{"type": "saved_messages"}], "download": True}

    def _client(self, msgs):
        class _C:
            def __init__(self):
                self.forward_calls = []

            async def get_messages(self, peer, **kw):
                if "ids" in kw:
                    want = set(kw["ids"])
                    return [m for m in msgs if m.id in want]
                if "min_id" not in kw and kw.get("limit") == 1:
                    return msgs[-1:]
                newer = [m for m in msgs if m.id > (kw.get("min_id") or 0)]
                lim = kw.get("limit")
                return newer[:lim] if lim is not None else newer

            async def forward_messages(self, peer, messages, from_peer=None):
                self.forward_calls.append((peer, messages, from_peer))
                return []

        return _C()

    async def _scan(self, msgs, channel=CHANNEL):
        state.client = self._client(msgs)
        state.LISTEN_RULES = [dict(self._rule(), source_chat_id=channel)]
        runtime_db.set_listener_checkpoint(channel, 100)
        with mock.patch.object(listener, "resolve_origin_snapshot",
                               new=mock.AsyncMock(return_value=None)):
            return await listener.scan_all()

    async def test_hit_adds_follow_with_snapshot(self):
        msg = FakeMsg(200, text="x #01musume")
        msg.chat_id = CHANNEL
        msg.date = datetime(2026, 9, 11, 6, 20, 12, tzinfo=timezone.utc)
        await self._scan([msg])
        rows = runtime_db.list_listener_follows()
        self.assertEqual(len(rows), 1, "命中标签的帖子要进关注列表")
        self.assertEqual(rows[0]["channel_id"], CHANNEL)
        self.assertEqual(rows[0]["post_id"], 200)
        self.assertEqual(rows[0]["caption"], "x #01musume")
        self.assertEqual(rows[0]["status"], runtime_db.FOLLOW_ACTIVE)

    async def test_only_hit_posts_are_followed(self):
        hit = FakeMsg(200, text="x #01musume")
        miss = FakeMsg(201, text="无关内容")
        for m in (hit, miss):
            m.chat_id = CHANNEL
        await self._scan([hit, miss])
        self.assertEqual([r["post_id"] for r in runtime_db.list_listener_follows()],
                         [200])

    async def test_cap_does_not_break_normal_tasks(self):
        """关注列表满了只是不再跟进，帖子本身的下载任务照建（背压不回退主链路）。"""
        with mock.patch.object(config, "LISTEN_FOLLOW_MAX", 0):
            msg = FakeMsg(200, text="x #01musume")
            msg.chat_id = CHANNEL
            await self._scan([msg])
        self.assertEqual(runtime_db.count_listener_follows(), 0)
        self.assertEqual(len(self._tasks()), 1, "主链路的任务不受影响")


if __name__ == "__main__":
    unittest.main()
