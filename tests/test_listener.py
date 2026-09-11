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

架构（2026-09-11 起）：Scanner 只把匹配结果落成 SQLite 任务，**不转发**；
转发由 listener_worker 受控执行（其单测见 test_listener_worker.py）。因此本文件
的扫描用例断言的是「DB 里建出了什么任务 / checkpoint 推到哪」，而不是「调了几次
forward」。

不联网：FakeClient 记录全部调用；DB 与配置全部落在进程级临时目录。
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
from tg_userbot import runtime_db  # noqa: E402

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
                       state.LISTEN_INTERVAL_MINUTES)
        self.addCleanup(self._restore)

    def _restore(self):
        (state.LISTEN_RULES, state.LISTEN_ENABLED,
         state.LISTEN_INTERVAL_MINUTES) = self._saved

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
# ============================================================
# 6. 扫描（规格书 §27 测试 A-G）
# ============================================================
class ScanTest(unittest.IsolatedAsyncioTestCase):
    """Scanner：扫出匹配 → 落成持久化任务（**不转发**）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="scan_", dir=_TMP)
        self.db_path = os.path.join(self.dir, "tg_userbot.db")
        self.cfg_path = os.path.join(self.dir, "listen.json")
        self.legacy_path = os.path.join(self.dir, "listen_state.json")
        self._p1 = mock.patch.object(config, "RUNTIME_DB_FILE", self.db_path)
        self._p2 = mock.patch.object(config, "LISTEN_CONFIG_FILE",
                                     self.cfg_path)
        self._p3 = mock.patch.object(config, "LISTEN_STATE_FILE",
                                     self.legacy_path)
        for patch in (self._p1, self._p2, self._p3):
            patch.start()
            self.addCleanup(patch.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        listener._LAST_CONFIG_MTIME = 0.0

        self._saved = (list(state.LISTEN_RULES), state.LISTEN_ENABLED,
                       dict(state.WHITELIST_CHATS), state.MY_ID, state.client,
                       state.LISTEN_LAST_SCAN)
        self.addCleanup(self._restore)
        state.MY_ID = ME
        state.WHITELIST_CHATS = {}
        state.LISTEN_ENABLED = True
        state.LISTEN_LAST_SCAN = None

    def _restore(self):
        (state.LISTEN_RULES, state.LISTEN_ENABLED, state.WHITELIST_CHATS,
         state.MY_ID, state.client, state.LISTEN_LAST_SCAN) = self._saved
        runtime_db.close_db()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _install(self, client, rules, checkpoint=100):
        state.client = client
        state.LISTEN_RULES = rules
        for rule in rules:
            runtime_db.set_listener_checkpoint(
                int(rule["source_chat_id"]), checkpoint)

    def _tasks(self, status=None):
        return runtime_db.list_listener_tasks(status=status)

    def _targets(self):
        """已建任务的 (target_type, target_chat_id) 集合。"""
        return {(t["target_type"], t["target_chat_id"]) for t in self._tasks()}

    # ---------- 规格书 §27 测试 A–G ----------
    async def test_A_listener_chat_not_in_download_whitelist(self):
        """监听聊天不在 /wl 里也必须正常工作（两套白名单独立的证明）。"""
        msg = FakeMessage(101, text="hello #01musume")
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule()])
        self.assertEqual(state.WHITELIST_CHATS, {})   # 下载白名单里没有 SRC

        summary = await listener.scan_all()
        self.assertEqual(summary["matched"], 1)
        self.assertEqual(summary["created"], 2)
        self.assertEqual(self._targets(),
                         {("saved_messages", None), ("chat", TARGET_CHAT)})
        # 扫描阶段绝不转发：forward 只由 Worker 发
        self.assertEqual(client.forward_calls, [])

    async def test_B_source_also_in_download_whitelist(self):
        """§14：实时链路已转发+下载 → 不为收藏夹建任务，其余目标照建。"""
        msg = FakeMessage(101, text="hello #01musume")
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule()])
        state.WHITELIST_CHATS = {SRC: "Source Channel"}

        await listener.scan_all()
        self.assertEqual(self._targets(), {("chat", TARGET_CHAT)})
        for task in self._tasks():
            self.assertFalse(task["download"], "白名单重叠时不该再触发下载")

    async def test_C_tag_mismatch_does_nothing(self):
        msg = FakeMessage(101, text="hello #02musume")
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule(tag="#01musume")])
        summary = await listener.scan_all()
        self.assertEqual(summary["matched"], 0)
        self.assertEqual(self._tasks(), [])
        # 未命中也推进 checkpoint（这条已经检查过了）
        self.assertEqual(listener.get_checkpoint(SRC), 101)

    async def test_D_same_chat_scanned_once_for_all_rules(self):
        msgs = [FakeMessage(101, text="#a"), FakeMessage(102, text="#b")]
        client = FakeClient({SRC: msgs})
        self._install(client, [_rule(tag="#a"), _rule(tag="#b")])

        await listener.scan_all()
        chat_fetches = [c for c in client.get_messages_calls
                        if c[0] == SRC and "ids" not in c[1]
                        and c[1].get("limit") != 1]
        self.assertEqual(len(chat_fetches), 1, "按 chat 扫一次，不按规则重复请求")

    async def test_D2_same_message_two_tags_one_task_per_target(self):
        """同一消息命中两条规则、目标相同 → 每个目标仍只有一条任务。"""
        msg = FakeMessage(101, text="看 #a 和 #b")
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule(tag="#a"), _rule(tag="#b")])
        summary = await listener.scan_all()
        self.assertEqual(summary["matched"], 1)
        self.assertEqual(len(self._tasks()), 2)      # 收藏夹 + 目标频道，各一条

    async def test_D3_union_of_targets_across_rules(self):
        msg = FakeMessage(101, text="看 #a 和 #b")
        only_me = [{"type": "saved_messages"}]
        only_tg = [{"type": "chat", "chat_id": TARGET_CHAT, "name": "S"}]
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule(tag="#a", targets=only_me, download=False),
                               _rule(tag="#b", targets=only_tg,
                                     download=False)])
        await listener.scan_all()
        self.assertEqual(self._targets(),
                         {("saved_messages", None), ("chat", TARGET_CHAT)})

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
        self.assertEqual(listener.get_checkpoint(SRC), 12)

        summary = await listener.scan_all()
        self.assertEqual(summary["matched"], 0)      # 一条历史都不处理
        self.assertEqual(self._tasks(), [])

    async def test_E_re_add_keeps_existing_checkpoint(self):
        client = FakeClient({}, newest={SRC: 999})
        state.client = client
        state.LISTEN_RULES = []
        runtime_db.set_listener_checkpoint(SRC, 500)
        await listener.add_listener(_rule())
        self.assertEqual(listener.get_checkpoint(SRC), 500)

    async def test_F_checkpoint_only_processes_new(self):
        msgs = [FakeMessage(101, text="#01musume"),
                FakeMessage(102, text="#01musume"),
                FakeMessage(103, text="#01musume")]
        client = FakeClient({SRC: msgs})
        self._install(client, [_rule()], checkpoint=100)

        summary = await listener.scan_all()
        self.assertEqual(summary["matched"], 3)
        self.assertEqual(listener.get_checkpoint(SRC), 103)
        before = len(self._tasks())

        # 第二次扫描：没有新消息 → 不重复建任务
        summary = await listener.scan_all()
        self.assertEqual(summary["matched"], 0)
        self.assertEqual(summary["created"], 0)
        self.assertEqual(len(self._tasks()), before)

    async def test_G_multi_target_creates_independent_tasks(self):
        """多目标各自独立成任务：一条失败不牵连另一条（§15）。"""
        msg = FakeMessage(101, text="#01musume")
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule()])
        await listener.scan_all()

        tasks = self._tasks()
        self.assertEqual(len(tasks), 2)
        by_target = {(t["target_type"], t["target_chat_id"]): t for t in tasks}
        me_task = by_target[("saved_messages", None)]
        chat_task = by_target[("chat", TARGET_CHAT)]

        # 目标频道那条失败，收藏夹那条不受影响（两条任务互不牵连）
        runtime_db.fail_listener_task(chat_task["id"], error="ChatWriteForbidden")
        self.assertEqual(runtime_db.get_listener_task(chat_task["id"])["status"],
                         "FAILED")
        self.assertEqual(runtime_db.get_listener_task(me_task["id"])["status"],
                         "PENDING")
        self.assertTrue(me_task["download"])

    # ---------- 队列上限 / 事务（§30 / Case E） ----------
    async def test_queue_cap_stops_and_holds_checkpoint(self):
        """队列触顶：不建新任务，且 checkpoint **绝不推进**（否则丢消息）。"""
        msgs = [FakeMessage(101, text="#01musume"),
                FakeMessage(102, text="#01musume")]
        client = FakeClient({SRC: [msgs[0], msgs[1]]})
        self._install(client, [_rule()], checkpoint=100)

        with mock.patch.object(config, "LISTEN_MAX_PENDING_TASKS", 0):
            summary = await listener.scan_all()
        self.assertEqual(summary["created"], 0)
        self.assertEqual(summary["capped"], 1)
        self.assertEqual(listener.get_checkpoint(SRC), 100,
                         "触顶时 checkpoint 必须停在原地")
        self.assertEqual(self._tasks(), [])

        # 容量恢复后重扫 → 原来的消息一条不丢
        summary = await listener.scan_all()
        self.assertEqual(summary["matched"], 2)
        self.assertEqual(listener.get_checkpoint(SRC), 102)

    async def test_cap_counts_existing_pending(self):
        msg = FakeMessage(101, text="#01musume")
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule()], checkpoint=100)
        # 先塞满队列（留 1 个空位 → 只够建 1 条任务）
        runtime_db.enqueue_listener_tasks(SRC, [{
            "message_id": 1, "grouped_id": None, "target_type": "chat",
            "target_chat_id": -1, "download": False, "payload": None,
        }], checkpoint=None)      # 不动 checkpoint：本用例要验的是扫描时它不动
        with mock.patch.object(config, "LISTEN_MAX_PENDING_TASKS", 2):
            summary = await listener.scan_all()
        self.assertEqual(summary["capped"], 1)
        self.assertEqual(listener.get_checkpoint(SRC), 100)
        self.assertEqual(summary["created"], 0)

    async def test_db_failure_does_not_advance_checkpoint(self):
        """DB 写不进去 = 本轮没做成：checkpoint 原地不动（Case B）。"""
        msg = FakeMessage(101, text="#01musume")
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule()], checkpoint=100)
        with mock.patch.object(runtime_db, "enqueue_listener_tasks",
                               side_effect=runtime_db.DbUnavailable("boom")):
            summary = await listener.scan_all()
        self.assertEqual(summary["failed_chats"], 1)
        self.assertEqual(listener.get_checkpoint(SRC), 100)
        self.assertEqual(self._tasks(), [])

    # ---------- 相册（§16） ----------
    async def test_album_one_task_per_target_with_all_members(self):
        a1 = FakeMessage(101, grouped_id=77, fname="a.mp4")
        a2 = FakeMessage(102, grouped_id=77, text="#01musume 相册说明",
                         fname="b.mp4")
        a3 = FakeMessage(103, grouped_id=77, fname="c.mp4")
        client = FakeClient({SRC: [a1, a2, a3]})
        self._install(client, [_rule()])

        summary = await listener.scan_all()
        self.assertEqual(summary["matched"], 1, "一组算一条命中")
        self.assertEqual(len(self._tasks()), 2, "每个目标一条任务")
        for task in self._tasks():
            self.assertEqual(task["message_id"], 101, "锚点 = 组内最小成员 id")
            self.assertEqual(task["grouped_id"], 77)
            self.assertEqual(task["payload"]["member_ids"], [101, 102, 103])
            self.assertEqual(task["payload"]["caption"], "#01musume 相册说明")
        self.assertEqual(listener.get_checkpoint(SRC), 103)

    async def test_album_cut_by_scan_limit_is_completed(self):
        """相册骑在一轮条数上限上时，边界成员必须补齐（否则转发半个相册）。"""
        a1 = FakeMessage(101, grouped_id=77, fname="a.mp4")
        a2 = FakeMessage(102, grouped_id=77, text="#01musume", fname="b.mp4")
        a3 = FakeMessage(103, grouped_id=77, fname="c.mp4")
        client = FakeClient({SRC: [a1, a2, a3]})
        self._install(client, [_rule()])

        with mock.patch.object(config, "LISTEN_MAX_MESSAGES_PER_SCAN", 2):
            await listener.scan_all()
        task = [t for t in self._tasks()
                if t["target_type"] == "saved_messages"][0]
        self.assertEqual(task["payload"]["member_ids"], [101, 102, 103],
                         "被上限切开的相册要补齐整组")

    async def test_non_media_matched_message_is_ignored(self):
        """纯文本命中标签 → 不建任务（避免收藏夹标注污染）。"""
        msg = FakeMessage(101, text="#01musume 公告", is_media=False)
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule()])
        summary = await listener.scan_all()
        self.assertEqual(summary["matched"], 0)
        self.assertEqual(self._tasks(), [])

    async def test_download_false_with_me_target_has_no_download_flag(self):
        msg = FakeMessage(101, text="#01musume")
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule(targets=[{"type": "saved_messages"}],
                                     download=False)])
        await listener.scan_all()
        tasks = self._tasks()
        self.assertEqual(len(tasks), 1)
        self.assertFalse(tasks[0]["download"])

    async def test_download_true_implies_me_target(self):
        """download=true 但 targets 里没有 me → 仍为收藏夹建任务以触发下载。"""
        msg = FakeMessage(101, text="#01musume")
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule(targets=[{"type": "chat",
                                               "chat_id": TARGET_CHAT,
                                               "name": "S"}],
                                     download=True)])
        await listener.scan_all()
        self.assertIn(("saved_messages", None), self._targets())
        me_task = [t for t in self._tasks()
                   if t["target_type"] == "saved_messages"][0]
        self.assertTrue(me_task["download"])

    # ---------- 单聊天隔离 / 守卫 / 快照 ----------
    async def test_one_chat_failure_does_not_stop_others(self):
        """单个聊天读失败不影响其他监听聊天（§23）。"""
        bad = FakeMessage(101, text="#01musume")
        good = FakeMessage(201, text="#01musume")
        client = FakeClient({SRC: [bad], OTHER_SRC: [good]},
                            fail_get_peers={SRC})
        self._install(client, [_rule(chat=SRC), _rule(chat=OTHER_SRC)])

        summary = await listener.scan_all()
        self.assertEqual(summary["chats"], 2)
        self.assertEqual(summary["failed_chats"], 1)
        self.assertIsNotNone(listener.get_checkpoint(OTHER_SRC))

    async def test_disabled_does_nothing(self):
        msg = FakeMessage(101, text="#01musume")
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule()])
        state.LISTEN_ENABLED = False
        summary = await listener.scan_all()
        self.assertEqual(summary["matched"], 0)
        self.assertEqual(self._tasks(), [])

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
        self.assertIn("queue", state.LISTEN_LAST_SCAN)

    async def test_scan_emits_stats_event(self):
        """台账事件：LISTEN_SCAN 带上 created 口径（Scanner 只建任务）。"""
        msg = FakeMessage(101, text="#01musume")
        client = FakeClient({SRC: [msg]})
        self._install(client, [_rule()])
        with mock.patch.object(listener.stats, "emit_event") as emit:
            await listener.scan_all()
        calls = [c for c in emit.call_args_list
                 if c[0] and c[0][0] == "LISTEN_SCAN"]
        self.assertTrue(calls)
        kwargs = calls[0][1]
        self.assertEqual(kwargs["created"], 2)
        self.assertNotIn("forwarded", kwargs)

    # ---------- 旧状态迁移（§38） ----------
    async def test_legacy_state_migration(self):
        with open(self.legacy_path, "w", encoding="utf-8") as f:
            json.dump({
                str(SRC): {
                    "last_message_id": 500,
                    "pending": {
                        "490": {"ids": [490, 491],
                                "work": ["saved_messages",
                                         f"chat:{TARGET_CHAT}"],
                                "dl": True, "cap": "#a 相册说明"},
                    },
                },
            }, f, ensure_ascii=False)

        out = listener.migrate_legacy_state()
        self.assertEqual(out["chats"], 1)
        self.assertEqual(out["tasks"], 2, "pending 的每个工作项都要变成任务")
        self.assertTrue(out["moved"])
        self.assertFalse(os.path.exists(self.legacy_path))
        self.assertTrue(os.path.exists(self.legacy_path + ".migrated"))

        self.assertEqual(listener.get_checkpoint(SRC), 500)
        tasks = self._tasks()
        self.assertEqual(self._targets(),
                         {("saved_messages", None), ("chat", TARGET_CHAT)})
        me_task = [t for t in tasks if t["target_type"] == "saved_messages"][0]
        self.assertEqual(me_task["message_id"], 490, "锚点 = 组内最小成员 id")
        self.assertEqual(me_task["payload"]["member_ids"], [490, 491])
        self.assertTrue(me_task["download"])
        self.assertTrue(all(t["status"] == "PENDING" for t in tasks))

    async def test_legacy_migration_does_not_rewind_newer_checkpoint(self):
        """DB 里已有更新的游标时，旧文件不得把它倒回去（否则重复扫一大段）。"""
        runtime_db.set_listener_checkpoint(SRC, 900)
        with open(self.legacy_path, "w", encoding="utf-8") as f:
            json.dump({str(SRC): {"last_message_id": 500, "pending": {}}}, f)
        out = listener.migrate_legacy_state()
        self.assertEqual(out["chats"], 0)
        self.assertEqual(out["skipped"], 1)
        self.assertEqual(listener.get_checkpoint(SRC), 900)

    async def test_legacy_migration_is_noop_without_file(self):
        out = listener.migrate_legacy_state()
        self.assertEqual(out, {"chats": 0, "tasks": 0, "skipped": 0,
                               "moved": False})

    async def test_legacy_migration_survives_corrupt_file(self):
        with open(self.legacy_path, "w", encoding="utf-8") as f:
            f.write("{ 半截")
        out = listener.migrate_legacy_state()
        self.assertEqual(out["tasks"], 0)
        self.assertFalse(out["moved"])
        self.assertTrue(os.path.exists(self.legacy_path), "坏文件保持原名")


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
