"""手工转发「评论 + 紧跟媒体」前置标注的待关联窗口行为。

关注点：一条评论后连续到达的 N 条媒体都继承同一条标注（取用不清空，靠时间
自然过期）；过了关联窗口后不再被认领。纯内存状态、无 I/O、无 Telegram。
另覆盖 _enqueue_me 的「文本在后」宽限：评论的事件可能落在媒体之后（实测两
种顺序都会出现），入队前应等一小段宽限再取一次标注。
"""
import asyncio
import atexit
import os
import shutil
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录：config 的 import
# 期有真实副作用（mkdir + 旧数据根迁移闸门），零配置裸 import 会以「桌面默认
# 部署」形态创建真实 /Volumes/V1 子目录、甚至迁移真实 ~/Downloads/Nagram。
_TMP = tempfile.mkdtemp(prefix="tg_userbot_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import app


def _reset():
    app._ME_PENDING_LABEL = None
    app._ME_PENDING_LABEL_AT = 0.0


class MeLabelCaptureTest(unittest.TestCase):
    """_record_me_label / _take_me_label 的窗口语义。"""

    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def test_consume_does_not_clear_same_label_for_burst(self):
        # 一条评论 + 连续 N 条媒体：多次认领都拿到同一条（不清空）
        app._record_me_label("自存")
        self.assertEqual(app._take_me_label(), "自存")
        self.assertEqual(app._take_me_label(), "自存")  # 第二条媒体同样继承

    def test_expires_after_window(self):
        # 过了窗口：不再返回，且待关联清空（下次 None）
        base = 1000.0
        app._record_me_label("自存")
        app._ME_PENDING_LABEL_AT = base
        with mock.patch.object(app.time, "monotonic", return_value=base + 3):
            self.assertEqual(app._take_me_label(), "自存")  # 窗口内
        with mock.patch.object(app.time, "monotonic", return_value=base + 10):
            self.assertIsNone(app._take_me_label())  # 已过期
        self.assertIsNone(app._take_me_label())  # 已清空

    def test_later_comment_overwrites(self):
        app._record_me_label("第一条")
        app._record_me_label("第二条")  # 紧跟的媒体属于后一条评论
        self.assertEqual(app._take_me_label(), "第二条")

    def test_none_when_nothing_recorded(self):
        self.assertIsNone(app._take_me_label())


def _fake_msg(mid=1):
    msg = mock.Mock()
    msg.id = mid
    return msg


class _EnqueueMeHarness:
    """把 _enqueue_me 的协程依赖（相册说明、入队）换成内存假件，记录 user_label。"""

    def __init__(self):
        self.labels = []
        self.subdirs = []

    def patch(self, grace):
        async def fake_enqueue(message, chat_id, source_override,
                               source_link=None, album_caption=None,
                               user_label=None, src=None, parent_date=None,
                               parent_caption=None, source_subdir=None):
            self.labels.append(user_label)
            self.subdirs.append(source_subdir)

        return mock.patch.multiple(
            "tg_userbot.app",
            ME_LABEL_GRACE_SECONDS=grace,
            _maybe_album_caption=mock.AsyncMock(return_value=""),
            enqueue_media=fake_enqueue,
        )


class MeLabelGraceTest(unittest.IsolatedAsyncioTestCase):
    """_enqueue_me 的「文本在后」宽限：评论事件可能落在媒体之后。"""

    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    async def test_trailing_comment_within_grace_attaches(self):
        # 媒体先到（此刻无评论）→ 宽限窗口内评论落地 → 应等到并继承
        h = _EnqueueMeHarness()
        with h.patch(0.05):
            task = asyncio.create_task(app._enqueue_me(_fake_msg(1)))
            await asyncio.sleep(0.01)      # 媒体已入取标点、评论尚未到达
            app._record_me_label("自存")   # 尾随评论落地
            await task
        self.assertEqual(h.labels, ["自存"])

    async def test_pending_label_takes_immediately_without_waiting(self):
        # 已有待关联标注时不等宽限，立即继承入队（宽限被故意放大以暴露等待）
        app._record_me_label("自存")
        h = _EnqueueMeHarness()
        with h.patch(50):
            await asyncio.wait_for(
                app._enqueue_me(_fake_msg(2)), timeout=1
            )
        self.assertEqual(h.labels, ["自存"])


if __name__ == "__main__":
    unittest.main()


class SubdirLabelSplitTest(unittest.TestCase):
    """目录模式标注：/A#标注 与 /A →（标注, 子目录）拆分。

    解析规则（2026-09-16 用户需求）：
      /A#标注 → 标注部分原样拼文件名（照常过 Caption 清洗），文件落
                原目录/A/；
      /A      → 无标注（不拼 #），文件落 原目录/A/；
      /A/B    → 多级子目录 原目录/A/B/（B 在 A 下）；
      普通评论 → 原语义不变（整条拼 #标注，落原目录）。
    """

    def test_dir_with_label(self):
        self.assertEqual(app.split_label_subdir("/A#标注"),
                         ("标注", "A"))

    def test_dir_only(self):
        self.assertEqual(app.split_label_subdir("/A"), ("", "A"))

    def test_nested_dirs(self):
        self.assertEqual(app.split_label_subdir("/A/B#x"),
                         ("x", "A/B"))
        self.assertEqual(app.split_label_subdir("/A/B"),
                         ("", "A/B"))

    def test_plain_label_unchanged(self):
        """普通评论：返回 (原文, None)——None 表示不覆盖子目录。"""
        self.assertEqual(app.split_label_subdir("自存"), ("自存", None))
        self.assertEqual(app.split_label_subdir("#tag 标注"),
                         ("#tag 标注", None))

    def test_slash_only_is_plain(self):
        """裸 '/'（无目录名）：不是目录模式，按普通评论处理。"""
        self.assertEqual(app.split_label_subdir("/"), ("/", None))

    def test_slash_dirname_sanitized(self):
        """目录名里的非法字符清洗掉（/ 作分隔符保留，其余非法字符清）。"""
        label, sub = app.split_label_subdir("/A:B#tag")
        self.assertEqual(label, "tag")
        self.assertEqual(sub, "AB".replace("AB", "AB") if False else
                         app.sanitize_dirname("A:B"))
        self.assertNotIn(":", sub)

    def test_empty_dirname_is_plain(self):
        """/#标注：目录名为空 → 不是目录模式（否则文件落原地且丢标注）。"""
        self.assertEqual(app.split_label_subdir("/#标注"),
                         ("/#标注", None))


class SubdirEnqueueTest(unittest.IsolatedAsyncioTestCase):
    """_enqueue_me：目录模式标注 → record.source_subdir + 干净的 user_label。"""

    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    async def test_record_gets_source_subdir(self):
        captured = {}

        async def fake_enqueue(message, chat_id, source_override,
                               source_link=None, album_caption=None,
                               user_label=None, src=None, parent_date=None,
                               parent_caption=None, source_subdir=None):
            captured["user_label"] = user_label
            captured["subdir"] = source_subdir

        app._record_me_label("/ wallpapers#城市夜景")
        with mock.patch.multiple(
                "tg_userbot.app",
                ME_LABEL_GRACE_SECONDS=0,
                _maybe_album_caption=mock.AsyncMock(return_value=""),
                enqueue_media=fake_enqueue):
            await app._enqueue_me(_fake_msg(2))
        self.assertEqual(captured["user_label"], "城市夜景")
        self.assertEqual(captured["subdir"], "wallpapers")


class RegisteredCommandGuardTest(unittest.TestCase):
    """评论捕获守卫：/ 开头的目录模式要放行，真命令要拦。"""

    def test_dir_mode_not_treated_as_command(self):
        self.assertFalse(app._is_registered_command("/wallpapers#城市"))
        self.assertFalse(app._is_registered_command("/wallpapers"))

    def test_real_commands_still_excluded(self):
        for cmd in ("/wl", "/status", "/help", "/paw plan X", "/start"):
            self.assertTrue(app._is_registered_command(cmd),
                            f"{cmd} 应被判为注册命令")


class HandlerWiringContractTest(unittest.TestCase):
    """handler 层接线契约：评论捕获分支必须真的调用 _record_me_label。

    2026-09-16 事故：调用行被并行编辑误删，handler 只剩日志行——
    「记录待关联转发评论」照打但标注从未写入，整条目录模式/标注链静默
    失效。用源码断言钉死「日志与调用必须同在」。

    """

    def test_capture_branch_calls_record(self):
        import inspect
        src = inspect.getsource(app)
        self.assertIn("_record_me_label(text)", src)
        # 日志行与调用行必须相邻出现（调用在日志之前）
        self.assertLess(
            src.index("_record_me_label(text)"),
            src.index("记录待关联转发评论"))
