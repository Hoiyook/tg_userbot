"""标签监听（listener.py）的单元测试。

契约（规格书 docs/Telegram标签监听功能_DeepSeek开发任务书.md）：

1. **两套白名单完全独立**：监听来源来自独立的 listen.json，绝不从
   state.WHITELIST_CHATS 推导；一个聊天可以只在下载白名单、只在监听、
   两边都在或都不在。
2. 标签匹配有前后边界、大小写不敏感，不用朴素的 `tag in text`
   （`#01musume2` / `x#01musume` / `##01musume` 都不算命中）。
3. 同一消息命中多个标签/多条规则 → **先匹配合并再执行**，同一
   (消息, 目标) 只转发一次。
4. 按 chat 扫描一次，再匹配该 chat 下的全部规则（测试 D）。
5. 首次启用以当前最新消息 id 作 checkpoint，绝不扫历史（测试 E）。
6. checkpoint 只推进、不重复处理；失败的目标留在 pending 精确续做，
   不因为一个目标失败就丢消息或重复转发已成功的目标（测试 G）。
7. download=true 的实现 = 转发到收藏夹 + 入队那份转发副本（复用现有
   enqueue_media → 队列 → dedup → 命名链路，不新增第二套下载系统）。
   download=false 且 targets 显式含 me 时只转发不入队。
8. 源聊天同时在下载白名单时（§14）：跳过 me 转发与下载（实时链路已做），
   其余目标照常转发。

不联网：FakeClient 记录全部调用；文件全部落在进程级临时 SAVE_FOLDER。
运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import atexit
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_listener_test_")
# 退出时回收临时目录（测试跑完就地删，别让 /var/folders 越堆越多）
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import state  # noqa: E402
from tg_userbot import listener  # noqa: E402

ME = 42                      # owner 自己的 id
SRC = -1001234567890         # 监听来源聊天
TARGET_CHAT = -1009876543210  # 普通转发目标
OTHER_SRC = -1005555555555


# ============================================================
# 测试替身
# ============================================================
class FakeFile:
    def __init__(self, fid="fileid-1", size=100, name="v.mp4"):
        self.id = fid
        self.size = size
        self.name = name
        self.mime_type = "video/mp4"


class FakeMessage:
    """够 listener 与 is_downloadable 用的最小消息。"""

    def __init__(self, mid, text="", grouped_id=None, chat_id=SRC,
                 is_media=True, fname="v.mp4"):
        self.id = mid
        self.message = text
        self.grouped_id = grouped_id
        self.chat_id = chat_id
        self.date = None
        self.fwd_from = None
        self._is_media = is_media
        if is_media:
            self.file = FakeFile(fid=f"fileid-{mid}", name=fname)
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


class FakeCopy(FakeMessage):
    """转发返回的副本：新 id、无 grouped_id 传染，但保留 caption。"""


class FakeClient:
    """记录调用的假客户端：只需 get_messages / forward_messages。"""

    def __init__(self, messages_by_chat=None, newest=None, fail_peers=(),
                 fail_get_peers=()):
        # messages_by_chat: {chat_id: [FakeMessage, ...]}（按 id 升序）
        self.messages_by_chat = dict(messages_by_chat or {})
        self.newest = dict(newest or {})
        self.fail_peers = set(fail_peers)
        self.fail_get_peers = set(fail_get_peers)
        self.get_messages_calls = []   # (peer, kwargs)
        self.forward_calls = []        # (peer, [msg_id...], from_peer)
        self._next_id = 90000

    async def get_messages(self, peer, **kwargs):
        self.get_messages_calls.append((peer, kwargs))
        if peer in self.fail_get_peers:
            raise RuntimeError(f"读消息被拒：{peer}")
        if "ids" in kwargs:
            wanted = set(kwargs["ids"])
            return [m for m in self.messages_by_chat.get(peer, [])
                    if m.id in wanted]
        chat_msgs = sorted(self.messages_by_chat.get(peer, []),
                           key=lambda m: m.id)
        # checkpoint 初始化：limit=1 且不带 min_id → 取最新一条
        if "min_id" not in kwargs and kwargs.get("limit") == 1:
            newest = self.newest.get(peer)
            if newest is not None:
                return [FakeMessage(newest, chat_id=peer)]
            return chat_msgs[-1:]
        min_id = kwargs.get("min_id") or 0
        limit = kwargs.get("limit")
        newer = [m for m in chat_msgs if m.id > min_id]
        if limit is not None:
            newer = newer[:limit]
        return newer

    async def forward_messages(self, peer, messages, from_peer=None):
        ids = [getattr(m, "id", m) for m in messages]
        self.forward_calls.append((peer, ids, from_peer))
        if peer in self.fail_peers:
            raise RuntimeError(f"转发被拒：{peer}")
        out = []
        for m in messages:
            self._next_id += 1
            out.append(FakeCopy(self._next_id, text=getattr(m, "message", ""),
                                chat_id=ME))
        return out


def _rule(chat=SRC, tag="#01musume", targets=None, download=True):
    return {
        "source_chat_id": chat,
        "source_name": "Source Channel",
        "source_username": "source_channel",
        "tag": tag,
        "targets": (targets if targets is not None
                    else [{"type": "saved_messages"},
                          {"type": "chat", "chat_id": TARGET_CHAT,
                           "name": "SpeedLearn"}]),
        "download": download,
    }


# ============================================================
# 1. 标签匹配（规格书 §10）
# ============================================================
class TagMatchTest(unittest.TestCase):
    def test_plain_hit(self):
        self.assertTrue(listener.tag_matches("这是 #01musume 的新视频",
                                             "#01musume"))

    def test_prefix_by_space_or_fullwidth_colon(self):
        self.assertTrue(listener.tag_matches("作者：#01musume", "#01musume"))

    def test_case_insensitive(self):
        self.assertTrue(listener.tag_matches("看 #01MUSUME", "#01musume"))
        self.assertTrue(listener.tag_matches("看 #01musume", "#01MUSUME"))

    def test_trailing_word_char_is_a_different_tag(self):
        # #01musume2 / #01musume_x 是另一个标签，不能命中
        self.assertFalse(listener.tag_matches("hello #01musume2", "#01musume"))
        self.assertFalse(listener.tag_matches("hello #01musume_x", "#01musume"))

    def test_trailing_chinese_is_a_different_tag(self):
        self.assertFalse(listener.tag_matches("hello #01musume的", "#01musume"))

    def test_leading_word_char_or_hash_blocks(self):
        self.assertFalse(listener.tag_matches("hello x#01musume", "#01musume"))
        self.assertFalse(listener.tag_matches("hello ##01musume", "#01musume"))

    def test_leading_chinese_allows(self):
        # 中文后面直接跟标签是常见的「不加空格」写法
        self.assertTrue(listener.tag_matches("这是#01musume", "#01musume"))

    def test_empty_text_or_tag(self):
        self.assertFalse(listener.tag_matches("", "#01musume"))
        self.assertFalse(listener.tag_matches("#01musume", ""))
        self.assertFalse(listener.tag_matches(None, "#01musume"))

    def test_matched_tags_returns_all_hits(self):
        text = "视频 #MMD #01musume 另外 #其他"
        self.assertEqual(
            listener.matched_tags(text, ["#MMD", "#01musume", "#没有"]),
            {"#MMD", "#01musume"},
        )


# ============================================================
# 2. 目标身份（规格书 §22）
# ============================================================
class TargetKeyTest(unittest.TestCase):
    def test_saved_messages_key(self):
        self.assertEqual(listener.target_key({"type": "saved_messages"}),
                         ("saved_messages", None))

    def test_chat_key_uses_chat_id(self):
        self.assertEqual(
            listener.target_key({"type": "chat", "chat_id": TARGET_CHAT,
                                 "name": "改名了也无所谓"}),
            ("chat", TARGET_CHAT),
        )

    def test_names_do_not_affect_identity(self):
        a = listener.target_key({"type": "chat", "chat_id": TARGET_CHAT,
                                 "name": "旧名"})
        b = listener.target_key({"type": "chat", "chat_id": TARGET_CHAT,
                                 "name": "新名"})
        self.assertEqual(a, b)

    def test_normalize_rejects_garbage(self):
        self.assertIsNone(listener.normalize_target(None))
        self.assertIsNone(listener.normalize_target({"type": "chat"}))
        self.assertIsNone(listener.normalize_target({"type": "nope"}))

    def test_normalize_me_aliases(self):
        self.assertEqual(listener.normalize_target("me"),
                         {"type": "saved_messages"})
        self.assertEqual(listener.normalize_target("saved_messages"),
                         {"type": "saved_messages"})

    def test_normalize_chat_dict_coerces_int(self):
        out = listener.normalize_target(
            {"type": "chat", "chat_id": "-1009876543210"})
        self.assertEqual(out["chat_id"], TARGET_CHAT)

    def test_target_label(self):
        self.assertIn("收藏", listener.target_label({"type": "saved_messages"}))
        self.assertIn("SpeedLearn",
                      listener.target_label({"type": "chat",
                                             "chat_id": TARGET_CHAT,
                                             "name": "SpeedLearn"}))


# ============================================================
# 3. 相册分桶与规则校验
# ============================================================
class AlbumGroupingTest(unittest.TestCase):
    def test_non_album_messages_are_singletons(self):
        msgs = [FakeMessage(1), FakeMessage(2)]
        units = listener.group_by_album(msgs)
        self.assertEqual([[m.id for m in u] for u in units], [[1], [2]])

    def test_same_grouped_id_forms_one_unit_sorted(self):
        msgs = [FakeMessage(3, grouped_id=77), FakeMessage(1, grouped_id=77),
                FakeMessage(2)]
        units = listener.group_by_album(msgs)
        got = sorted(sorted(m.id for m in u) for u in units)
        self.assertEqual(got, [[1, 3], [2]])

    def test_empty(self):
        self.assertEqual(listener.group_by_album([]), [])


class RuleValidationTest(unittest.TestCase):
    def test_valid_rule(self):
        ok, _ = listener.validate_rule(_rule())
        self.assertTrue(ok)

    def test_missing_source_chat_id(self):
        bad = _rule()
        bad.pop("source_chat_id")
        ok, msg = listener.validate_rule(bad)
        self.assertFalse(ok)
        self.assertIn("来源", msg)

    def test_tag_must_start_with_hash(self):
        ok, msg = listener.validate_rule(_rule(tag="01musume"))
        self.assertFalse(ok)
        self.assertIn("#", msg)

    def test_empty_targets_and_no_download_is_allowed(self):
        # 只转发到收藏夹（download=False）也要能保存
        ok, _ = listener.validate_rule(
            _rule(targets=[{"type": "saved_messages"}], download=False))
        self.assertTrue(ok)

    def test_interval_validation(self):
        self.assertTrue(listener.validate_interval(1440)[0])
        self.assertFalse(listener.validate_interval(0)[0])
        self.assertFalse(listener.validate_interval(-5)[0])
        self.assertFalse(listener.validate_interval("abc")[0])


# ============================================================
# 4. 配置持久化（listen.json）
# ============================================================
class ConfigStoreTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(_TMP, "listen_cfg_test.json")
        self.state_path = os.path.join(_TMP, "listen_state_test.json")
        self._p1 = mock.patch.object(config, "LISTEN_CONFIG_FILE", self.path)
        self._p2 = mock.patch.object(config, "LISTEN_STATE_FILE",
                                     self.state_path)
        self._p1.start()
        self._p2.start()
        self.addCleanup(self._p1.stop)
        self.addCleanup(self._p2.stop)
        for p in (self.path, self.state_path):
            if os.path.exists(p):
                os.remove(p)
        # 每个用例从「文件不存在」开始，且忘掉上个用例记住的 mtime
        listener._LAST_CONFIG_MTIME = 0.0
        self._saved = (list(state.LISTEN_RULES), state.LISTEN_ENABLED,
                       state.LISTEN_INTERVAL_MINUTES, dict(state.LISTEN_STATE))
        self.addCleanup(self._restore)

    def _restore(self):
        (state.LISTEN_RULES, state.LISTEN_ENABLED,
         state.LISTEN_INTERVAL_MINUTES,
         state.LISTEN_STATE) = (self._saved[0], self._saved[1],
                                self._saved[2], self._saved[3])

    def test_load_missing_file_keeps_defaults(self):
        state.LISTEN_RULES = []
        state.LISTEN_ENABLED = True
        self.assertEqual(listener.load_listen_config(), 0)
        self.assertEqual(state.LISTEN_RULES, [])

    def test_save_then_load_roundtrip(self):
        state.LISTEN_ENABLED = False
        state.LISTEN_INTERVAL_MINUTES = 30
        state.LISTEN_RULES = [_rule()]
        self.assertTrue(listener.save_listen_config())

        state.LISTEN_RULES = []
        state.LISTEN_ENABLED = True
        state.LISTEN_INTERVAL_MINUTES = 1440
        loaded = listener.load_listen_config()
        self.assertEqual(loaded, 1)
        self.assertFalse(state.LISTEN_ENABLED)
        self.assertEqual(state.LISTEN_INTERVAL_MINUTES, 30)
        self.assertEqual(state.LISTEN_RULES[0]["source_chat_id"], SRC)
        self.assertEqual(state.LISTEN_RULES[0]["tag"], "#01musume")

    def test_corrupt_json_does_not_raise(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ 这不是 json")
        state.LISTEN_RULES = [_rule()]
        self.assertEqual(listener.load_listen_config(), 0)
        self.assertEqual(state.LISTEN_RULES, [])
        self.assertTrue(state.LISTEN_ENABLED)   # 回落默认开启

    def test_bad_rules_are_dropped_one_by_one(self):
        good = _rule()
        bad_tag = _rule(tag="no-hash")
        bad_src = _rule()
        bad_src.pop("source_chat_id")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"enabled": True, "interval_minutes": 60,
                       "listeners": [good, bad_tag, bad_src]}, f,
                      ensure_ascii=False)
        self.assertEqual(listener.load_listen_config(), 1)
        self.assertEqual(state.LISTEN_RULES[0]["tag"], "#01musume")
        self.assertEqual(state.LISTEN_INTERVAL_MINUTES, 60)

    def test_illegal_interval_falls_back_to_default(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"enabled": True, "interval_minutes": -1,
                       "listeners": []}, f)
        listener.load_listen_config()
        self.assertEqual(state.LISTEN_INTERVAL_MINUTES,
                         config.LISTEN_DEFAULT_INTERVAL_MINUTES)

    def test_del_listener(self):
        state.LISTEN_RULES = [_rule(tag="#a"), _rule(tag="#b")]
        ok, _ = listener.del_listener(1)
        self.assertTrue(ok)
        self.assertEqual([r["tag"] for r in state.LISTEN_RULES], ["#b"])
        ok, msg = listener.del_listener(9)
        self.assertFalse(ok)
        self.assertIn("序号", msg)

    def test_reload_applies_external_edit(self):
        """手工改了 listen.json（mtime 变了）→ 扫描前按需重读，无需重启。"""
        state.LISTEN_RULES = []
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"enabled": True, "interval_minutes": 15,
                       "listeners": [_rule()]}, f, ensure_ascii=False)
        os.utime(self.path, (1, 1))          # 确保 mtime 与记录值不同
        self.assertTrue(listener.reload_listen_config())
        self.assertEqual(state.LISTEN_INTERVAL_MINUTES, 15)
        self.assertEqual(len(state.LISTEN_RULES), 1)
        # 内容没变 → 不再重复读
        self.assertFalse(listener.reload_listen_config())

    def test_reload_keeps_rules_when_file_corrupt(self):
        """半截/损坏的文件绝不能把正在生效的规则清空（否则下次保存就真没了）。"""
        state.LISTEN_RULES = [_rule()]
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ 半截")
        os.utime(self.path, (1, 1))
        self.assertFalse(listener.reload_listen_config())
        self.assertEqual(len(state.LISTEN_RULES), 1)   # 规则还在

    def test_set_enabled_and_interval_persist(self):
        state.LISTEN_RULES = []
        self.assertIn("关闭", listener.set_enabled(False))
        self.assertFalse(state.LISTEN_ENABLED)
        ok, msg = listener.set_interval(30)
        self.assertTrue(ok)
        self.assertEqual(state.LISTEN_INTERVAL_MINUTES, 30)
        with open(self.path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["interval_minutes"], 30)
        self.assertFalse(saved["enabled"])

        ok, msg = listener.set_interval(0)
        self.assertFalse(ok)
        self.assertIn("周期", msg)


# ============================================================
# 5. 扫描状态（listen_state.json）
# ============================================================
class StateStoreTest(unittest.TestCase):
    def setUp(self):
        self.state_path = os.path.join(_TMP, "listen_state_test2.json")
        self.cfg_path = os.path.join(_TMP, "listen_cfg_test2.json")
        self._p1 = mock.patch.object(config, "LISTEN_CONFIG_FILE",
                                     self.cfg_path)
        self._p2 = mock.patch.object(config, "LISTEN_STATE_FILE",
                                     self.state_path)
        self._p1.start()
        self._p2.start()
        self.addCleanup(self._p1.stop)
        self.addCleanup(self._p2.stop)
        self._saved = dict(state.LISTEN_STATE)
        self.addCleanup(setattr, state, "LISTEN_STATE", self._saved)

    def test_save_then_load(self):
        state.LISTEN_STATE = {
            str(SRC): {"last_message_id": 500,
                       "pending": {"490": {"ids": [490],
                                           "work": ["saved_messages"]}}}
        }
        self.assertTrue(listener.save_listen_state())
        state.LISTEN_STATE = {}
        self.assertEqual(listener.load_listen_state(), 1)
        entry = state.LISTEN_STATE[str(SRC)]
        self.assertEqual(entry["last_message_id"], 500)
        self.assertEqual(entry["pending"]["490"]["work"], ["saved_messages"])

    def test_corrupt_state_falls_back_to_empty(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            f.write("]]] not json")
        state.LISTEN_STATE = {str(SRC): {"last_message_id": 1}}
        self.assertEqual(listener.load_listen_state(), 0)
        self.assertEqual(state.LISTEN_STATE, {})


# ============================================================
# 6. 扫描（规格书 §27 测试 A-G）
# ============================================================
class ScanTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg_path = os.path.join(_TMP, "listen_scan_cfg.json")
        self.state_path = os.path.join(_TMP, "listen_scan_state.json")
        self._p1 = mock.patch.object(config, "LISTEN_CONFIG_FILE",
                                     self.cfg_path)
        self._p2 = mock.patch.object(config, "LISTEN_STATE_FILE",
                                     self.state_path)
        self._p1.start()
        self._p2.start()
        self.addCleanup(self._p1.stop)
        self.addCleanup(self._p2.stop)
        # 每个用例从「配置文件不存在」开始，且忘掉上个用例记住的 mtime
        for p in (self.cfg_path, self.state_path):
            if os.path.exists(p):
                os.remove(p)
        listener._LAST_CONFIG_MTIME = 0.0

        self._saved = (list(state.LISTEN_RULES), state.LISTEN_ENABLED,
                       dict(state.LISTEN_STATE), dict(state.WHITELIST_CHATS),
                       state.MY_ID, state.client, state.LISTEN_LAST_SCAN)
        self.addCleanup(self._restore)
        state.MY_ID = ME
        state.WHITELIST_CHATS = {}
        state.LISTEN_ENABLED = True
        state.LISTEN_LAST_SCAN = None
        state.LISTEN_STATE = {}

        self.enqueued = []

        async def fake_enqueue(copy, source_link, album_caption, src):
            self.enqueued.append((copy.id, source_link, album_caption, src))

        self._p3 = mock.patch.object(listener, "_enqueue_copy", fake_enqueue)
        self._p3.start()
        self.addCleanup(self._p3.stop)

    def _restore(self):
        (state.LISTEN_RULES, state.LISTEN_ENABLED, state.LISTEN_STATE,
         state.WHITELIST_CHATS, state.MY_ID, state.client,
         state.LISTEN_LAST_SCAN) = self._saved

    def _install(self, client, rules, checkpoint=100):
        state.client = client
        state.LISTEN_RULES = rules
        state.LISTEN_STATE = {
            str(r["source_chat_id"]): {"last_message_id": checkpoint,
                                       "pending": {}}
            for r in rules
        }

    async def test_A_listener_chat_not_in_download_whitelist(self):
        """监听聊天不在 /wl 里也必须正常工作（两套白名单独立的证明）。"""
        msg = FakeMessage(101, text="hello #01musume")
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule()])
        self.assertEqual(state.WHITELIST_CHATS, {})   # 下载白名单里没有 SRC

        summary = await listener.scan_all()
        self.assertEqual(summary["matched"], 1)
        peers = [c[0] for c in client.forward_calls]
        self.assertIn("me", peers)               # 转发进收藏夹
        self.assertIn(TARGET_CHAT, peers)        # 以及目标频道
        self.assertEqual(len(self.enqueued), 1)  # download=true → 入队副本
        self.assertEqual(self.enqueued[0][3], "listen")

    async def test_B_source_also_in_download_whitelist(self):
        """§14：实时链路已转发+下载 → 跳过 me 与 download，其余目标照转。"""
        msg = FakeMessage(101, text="hello #01musume")
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule()])
        state.WHITELIST_CHATS = {SRC: "Source Channel"}

        await listener.scan_all()
        peers = [c[0] for c in client.forward_calls]
        self.assertNotIn("me", peers)            # 不再转发收藏夹
        self.assertEqual(peers, [TARGET_CHAT])   # 只跑其余目标
        self.assertEqual(self.enqueued, [])      # 不重复下载

    async def test_C_tag_mismatch_does_nothing(self):
        msg = FakeMessage(101, text="hello #02musume")
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule(tag="#01musume")])
        summary = await listener.scan_all()
        self.assertEqual(summary["matched"], 0)
        self.assertEqual(client.forward_calls, [])
        self.assertEqual(self.enqueued, [])
        # 未命中也推进 checkpoint（这条已经检查过了）
        self.assertEqual(
            state.LISTEN_STATE[str(SRC)]["last_message_id"], 101)

    async def test_D_same_chat_scanned_once_for_all_rules(self):
        msgs = [FakeMessage(101, text="#a"), FakeMessage(102, text="#b")]
        client = FakeClient({SRC: [msgs[0], msgs[1]]})
        self._install(client, [_rule(tag="#a"), _rule(tag="#b")])

        await listener.scan_all()
        chat_fetches = [c for c in client.get_messages_calls
                        if c[0] == SRC and "ids" not in c[1]]
        self.assertEqual(len(chat_fetches), 1)   # 按 chat 扫一次

    async def test_D_same_message_matching_two_tags_forwards_once(self):
        """同一消息命中两条规则、目标都含 me → 收藏夹只转发一次。"""
        msg = FakeMessage(101, text="看 #a 和 #b")
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule(tag="#a"), _rule(tag="#b")])
        await listener.scan_all()
        me_forwards = [c for c in client.forward_calls if c[0] == "me"]
        self.assertEqual(len(me_forwards), 1)
        self.assertEqual(len(self.enqueued), 1)

    async def test_D2_union_of_targets_across_rules(self):
        """两条规则目标不同 → 合并执行，各目标各一次。"""
        msg = FakeMessage(101, text="看 #a 和 #b")
        only_me = [{"type": "saved_messages"}]
        only_tg = [{"type": "chat", "chat_id": TARGET_CHAT, "name": "S"}]
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule(tag="#a", targets=only_me, download=False),
                               _rule(tag="#b", targets=only_tg,
                                     download=False)])
        await listener.scan_all()
        peers = sorted(str(c[0]) for c in client.forward_calls)
        self.assertEqual(peers, sorted(["me", str(TARGET_CHAT)]))

    async def test_E_first_enable_does_not_scan_history(self):
        """聊天里已有 3 条历史，add_listener 后 checkpoint = 最新 id。"""
        history = [FakeMessage(10, text="#01musume"),
                   FakeMessage(11, text="#01musume"),
                   FakeMessage(12, text="#01musume")]
        client = FakeClient({SRC: history}, newest={SRC: 12})
        state.client = client
        state.LISTEN_RULES = []

        ok, msg = await listener.add_listener(_rule())
        self.assertTrue(ok, msg)
        self.assertEqual(state.LISTEN_STATE[str(SRC)]["last_message_id"], 12)

        summary = await listener.scan_all()
        self.assertEqual(summary["matched"], 0)      # 一条历史都不处理
        self.assertEqual(client.forward_calls, [])

    async def test_E_re_add_keeps_existing_checkpoint(self):
        client = FakeClient({}, newest={SRC: 999})
        state.client = client
        state.LISTEN_RULES = []
        state.LISTEN_STATE = {str(SRC): {"last_message_id": 500,
                                         "pending": {}}}
        await listener.add_listener(_rule())
        self.assertEqual(state.LISTEN_STATE[str(SRC)]["last_message_id"], 500)

    async def test_F_checkpoint_only_processes_new(self):
        msgs = [FakeMessage(101, text="#01musume"),
                FakeMessage(102, text="#01musume"),
                FakeMessage(103, text="#01musume")]
        client = FakeClient({SRC: msgs})
        self._install(client, [_rule()], checkpoint=100)

        summary = await listener.scan_all()
        self.assertEqual(summary["matched"], 3)
        self.assertEqual(state.LISTEN_STATE[str(SRC)]["last_message_id"], 103)

        # 第二次扫描：没有新消息 → 一条都不重复处理
        client.forward_calls.clear()
        self.enqueued.clear()
        summary = await listener.scan_all()
        self.assertEqual(summary["matched"], 0)
        self.assertEqual(client.forward_calls, [])
        self.assertEqual(self.enqueued, [])

    async def test_G_failed_target_keeps_pending_and_retries(self):
        """目标频道转发失败：不影响收藏夹、不丢消息、下轮只补失败的那一项。"""
        msg = FakeMessage(101, text="#01musume")
        client = FakeClient({SRC: [msg]}, fail_peers={TARGET_CHAT})
        self._install(client, [_rule()], checkpoint=100)

        await listener.scan_all()
        # 收藏夹那条成功 → 立即入队，不因另一个目标失败而回滚
        self.assertEqual(len(self.enqueued), 1)
        entry = state.LISTEN_STATE[str(SRC)]
        self.assertEqual(entry["last_message_id"], 101)
        self.assertIn("101", entry["pending"])
        self.assertEqual(entry["pending"]["101"]["work"],
                         [f"chat:{TARGET_CHAT}"])

        # 下一轮：网络恢复 → 只补目标频道，收藏夹不再转发、不再重复下载
        client.fail_peers.clear()
        client.forward_calls.clear()
        self.enqueued.clear()
        await listener.scan_all()
        peers = [c[0] for c in client.forward_calls]
        self.assertEqual(peers, [TARGET_CHAT])
        self.assertEqual(self.enqueued, [])          # 不再重复下载
        self.assertEqual(state.LISTEN_STATE[str(SRC)]["pending"], {})

    async def test_G_one_chat_failure_does_not_stop_others(self):
        """单个聊天读失败不影响其他监听聊天（§23）。"""
        bad = FakeMessage(101, text="#01musume")
        good = FakeMessage(201, text="#01musume")
        client = FakeClient({SRC: [bad], OTHER_SRC: [good]},
                            fail_get_peers={SRC})
        self._install(client, [_rule(chat=SRC), _rule(chat=OTHER_SRC)])

        summary = await listener.scan_all()
        self.assertEqual(summary["chats"], 2)
        self.assertEqual(summary["failed_chats"], 1)
        # 另一个聊天照常走完转发
        peers = [c[0] for c in client.forward_calls]
        self.assertIn("me", peers)
        self.assertEqual(len(self.enqueued), 1)

    async def test_disabled_does_nothing(self):
        msg = FakeMessage(101, text="#01musume")
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule()])
        state.LISTEN_ENABLED = False
        summary = await listener.scan_all()
        self.assertEqual(summary["matched"], 0)
        self.assertEqual(client.forward_calls, [])

    async def test_download_false_with_me_target_only_forwards(self):
        """download=false 且目标显式含 me → 只转发收藏夹，不入队。"""
        msg = FakeMessage(101, text="#01musume")
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule(targets=[{"type": "saved_messages"}],
                                     download=False)])
        await listener.scan_all()
        self.assertEqual([c[0] for c in client.forward_calls], ["me"])
        self.assertEqual(self.enqueued, [])

    async def test_download_true_implies_me_target(self):
        """download=true 但 targets 里没有 me → 隐式转发收藏夹以触发下载。"""
        msg = FakeMessage(101, text="#01musume")
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule(targets=[{"type": "chat",
                                               "chat_id": TARGET_CHAT,
                                               "name": "S"}],
                                     download=True)])
        await listener.scan_all()
        peers = sorted(str(c[0]) for c in client.forward_calls)
        self.assertEqual(peers, sorted(["me", str(TARGET_CHAT)]))
        self.assertEqual(len(self.enqueued), 1)

    async def test_album_group_matched_via_one_member(self):
        """标签只挂在一个成员上 → 整组转发（一次调用）、逐个入队。"""
        a1 = FakeMessage(101, grouped_id=77, text="", fname="a.mp4")
        a2 = FakeMessage(102, grouped_id=77, text="#01musume 相册说明",
                         fname="b.mp4")
        a3 = FakeMessage(103, grouped_id=77, text="", fname="c.mp4")
        client = FakeClient({SRC: [a1, a2, a3]})
        self._install(client, [_rule(download=True)])

        summary = await listener.scan_all()
        self.assertEqual(summary["matched"], 1)          # 一组算一条
        me_fwd = [c for c in client.forward_calls if c[0] == "me"]
        self.assertEqual(len(me_fwd), 1)                 # 整组一次调用
        self.assertEqual(me_fwd[0][1], [101, 102, 103])
        self.assertEqual(len(self.enqueued), 3)          # 三个副本各自入队
        self.assertEqual(self.enqueued[0][2], "#01musume 相册说明")

    async def test_non_media_matched_message_is_ignored(self):
        """纯文本命中标签 → 不转发（避免收藏夹标注污染）。"""
        msg = FakeMessage(101, text="#01musume 公告", is_media=False)
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule()])
        summary = await listener.scan_all()
        self.assertEqual(summary["matched"], 0)
        self.assertEqual(client.forward_calls, [])

    async def test_scan_guard_prevents_overlap(self):
        self.assertFalse(listener.is_scanning())
        listener._SCANNING = True
        try:
            summary = await listener.scan_all()
            self.assertTrue(summary.get("skipped"))
        finally:
            listener._SCANNING = False

    async def test_last_scan_snapshot_written(self):
        msg = FakeMessage(101, text="#01musume")
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule()])
        await listener.scan_all()
        self.assertIsNotNone(state.LISTEN_LAST_SCAN)
        self.assertEqual(state.LISTEN_LAST_SCAN["matched"], 1)
        self.assertIn("ts", state.LISTEN_LAST_SCAN)


# ============================================================
# 7. 命令与视图
# ============================================================
class ListenCommandTest(unittest.TestCase):
    def test_is_listen_command(self):
        self.assertTrue(listener.is_listen_command("/listen"))
        self.assertTrue(listener.is_listen_command("/listen on"))
        self.assertTrue(listener.is_listen_command("/LISTEN off"))
        self.assertFalse(listener.is_listen_command("/listeners"))
        self.assertFalse(listener.is_listen_command("/wl"))

    def test_parse_bare_and_subcommands(self):
        self.assertEqual(listener.parse_listen_command("/listen"),
                         ("list", None))
        self.assertEqual(listener.parse_listen_command("/listen on"),
                         ("on", None))
        self.assertEqual(listener.parse_listen_command("/listen off"),
                         ("off", None))
        self.assertEqual(listener.parse_listen_command("/listen scan"),
                         ("scan", None))
        self.assertEqual(listener.parse_listen_command("/listen del 2"),
                         ("del", "2"))
        self.assertEqual(listener.parse_listen_command("/listen interval 30"),
                         ("interval", "30"))
        self.assertIsNone(listener.parse_listen_command("/wl"))

    def test_parse_add_form(self):
        action, arg = listener.parse_listen_command(
            "/listen add @source #01musume me,@speedlearnnn on")
        self.assertEqual(action, "add")
        self.assertEqual(arg, "@source #01musume me,@speedlearnnn on")

    def test_view_text_shows_state_and_rules(self):
        state.LISTEN_ENABLED = True
        state.LISTEN_INTERVAL_MINUTES = 1440
        state.LISTEN_RULES = [_rule()]
        text = listener.view_text()
        self.assertIn("📡 标签监听", text)
        self.assertIn("#01musume", text)
        self.assertIn("收藏", text)
        self.assertIn("24", text)


if __name__ == "__main__":
    unittest.main()
