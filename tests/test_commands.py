"""handle_command（Saved Messages 文本命令分发）的回归测试。

守护点：handle_command 的形参曾与模块同名（text 模块被消息文本字符串遮蔽），
凡走到 text.status_text()/done_reply_text()/progress_text() 分支的命令都会抛
'str' object has no attribute '...'。本测试用假 event 直接调各命令，断言
不抛异常、正确分发（返回 True）、回复非空字符串，且不带遮蔽报错特征。

运行方式（在项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_commands_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import commands  # noqa: E402


class FakeEvent:
    """仅实现 handle_command 用到的 reply：捕获回复文本，不碰网络。"""

    def __init__(self):
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)
        return None


class TextCommandRegressionTest(unittest.TestCase):
    def _run(self, cmd):
        ev = FakeEvent()
        ok = asyncio.run(commands.handle_command(ev, cmd))
        return ok, ev.replies

    def test_text_module_commands_dispatch(self):
        # 命中 text.* 模块（遮蔽 bug 原爆发点）的命令
        for cmd in ["/status", "/progress", "/done", "/done 5 古风"]:
            with self.subTest(cmd=cmd):
                ok, replies = self._run(cmd)
                self.assertTrue(ok, f"{cmd} 应被识别为命令")
                self.assertTrue(replies, f"{cmd} 应有回复")
                reply = replies[0]
                self.assertIsInstance(reply, str)
                self.assertTrue(reply.strip())
                self.assertNotIn("AttributeError", reply)
                self.assertNotIn("object has no attribute", reply)

    def test_plain_string_commands_dispatch(self):
        # 纯字符串路径（不走 text 模块）
        for cmd in ["/folder", "/logpath", "/help"]:
            with self.subTest(cmd=cmd):
                ok, replies = self._run(cmd)
                self.assertTrue(ok, f"{cmd} 应被识别为命令")
                self.assertTrue(replies)
                self.assertIsInstance(replies[0], str)

    def test_unknown_command_falls_through(self):
        # 未知文本命令回落 False（交由媒体逻辑）
        ok, replies = self._run("/not_a_real_command")
        self.assertFalse(ok)
        self.assertFalse(replies)

    def test_dedup_command_dispatch(self):
        """/dedup（查询）、/dedup off|on（切换）、非法参数回落。"""
        from tg_userbot import state

        old_enabled = state.DEDUP_ENABLED
        try:
            ok, replies = self._run("/dedup")
            self.assertTrue(ok)
            self.assertIn("去重", replies[0])

            ok, replies = self._run("/dedup off")
            self.assertTrue(ok)
            self.assertIn("关闭", replies[0])
            self.assertFalse(state.DEDUP_ENABLED)

            ok, replies = self._run("/dedup on")
            self.assertTrue(ok)
            self.assertIn("开启", replies[0])
            self.assertTrue(state.DEDUP_ENABLED)

            # 非法参数不被当命令吃掉（is_dedup_command 只认 on/off/无参）
            ok, _ = self._run("/dedup now")
            self.assertFalse(ok)
        finally:
            state.DEDUP_ENABLED = old_enabled

    def test_queue_del_executing_cancels(self):
        """/queue del 命中执行中的任务：真正取消在途下载并移除记录。"""
        import asyncio as _asyncio
        from unittest import mock
        from tg_userbot import state, queue

        old = (state.QUEUE, state.QUEUE_LOCK, state.EXECUTING,
               state.DOWNLOAD_SEMAPHORE)
        state.DOWNLOAD_SEMAPHORE = None

        async def run():
            # 锁等原语必须在事件循环内创建（py3.9 构造即绑环）
            state.QUEUE = {"tasks": [], "retry": []}
            state.QUEUE_LOCK = _asyncio.Lock()
            state.EXECUTING = set()
            started = _asyncio.Event()

            async def hang(record):
                started.set()
                await _asyncio.sleep(3600)

            rec = queue.queue_enqueue(
                state.QUEUE, {"kind": "media", "chat_id": 1, "msg_id": 1,
                              "label": "在途大文件.mp4", "id": "cancel-me"}
            )
            rec["id"] = "cancel-me"
            state.EXECUTING.add("cancel-me")
            with mock.patch.object(queue, "execute_queued_task",
                                   side_effect=hang):
                queue.spawn_execute(rec)
            await started.wait()

            ev = FakeEvent()
            ok = await commands.handle_command(ev, "/queue del 1")
            self.assertTrue(ok)
            self.assertTrue(
                any("取消" in r for r in ev.replies), ev.replies
            )
            self.assertEqual(state.QUEUE["tasks"], [])

        try:
            _asyncio.run(run())
        finally:
            state.QUEUE, state.QUEUE_LOCK, state.EXECUTING, \
                state.DOWNLOAD_SEMAPHORE = old
            queue._RUNNING_TASKS.clear()
            queue._SPAWNED_TASKS.clear()


if __name__ == "__main__":
    unittest.main()


class FindCommandTest(unittest.TestCase):
    """/find <关键字>：媒体下落查询（finder.find_media 的命令分发）。"""

    def test_find_dispatches_and_replies(self):
        ev = FakeEvent()
        ok = asyncio.run(commands.handle_command(ev, "/find bl44"))
        self.assertTrue(ok)
        self.assertTrue(ev.replies)
        self.assertIn("🔍 查询", ev.replies[0])

    def test_find_short_keyword_shows_usage(self):
        ev = FakeEvent()
        ok = asyncio.run(commands.handle_command(ev, "/find b"))
        self.assertTrue(ok)
        self.assertIn("用法", ev.replies[0])

    def test_find_no_match(self):
        ev = FakeEvent()
        ok = asyncio.run(commands.handle_command(ev, "/find 无此关键字zz"))
        self.assertTrue(ok)
        self.assertIn("无匹配", ev.replies[0])


class ClearmsgEntryTest(unittest.IsolatedAsyncioTestCase):
    """/clearmsg 双入口：bot 对话与收藏夹的消息 id 是两个空间。

    bot 路径曾按 bot 对话消息 id 去盲删收藏夹消息（2026-09-09 发现，
    可能误删用户保留的内容）；收藏夹路径要跳过并最后删除命令自身。
    """

    def _msg(self, mid, text):
        from types import SimpleNamespace
        return SimpleNamespace(id=mid, message=text, file=None,
                               document=None, video=None, photo=None,
                               audio=None, voice=None, media=None)

    class _FakeClient:
        def __init__(self, msgs, deleted):
            self._msgs = msgs
            self.deleted = deleted

        async def iter_messages(self, peer, limit=None):
            for m in self._msgs:
                yield m

        async def delete_messages(self, peer, ids):
            self.deleted.append(list(ids))

    async def asyncSetUp(self):
        self.old = (commands.state.client, commands.state.MY_ID)
        commands.state.MY_ID = 545
        self.deleted = []
        self.msgs = [
            self._msg(100, "/status"),
            self._msg(500, "普通收藏内容，用户自己存的"),
            self._msg(501, "🤖 Chrome 下载任务已提交"),
        ]
        self.fake_client = self._FakeClient(self.msgs, self.deleted)
        commands.state.client = self.fake_client
        self.bot_client_sentinel = object()  # ≠ state.client

    async def asyncTearDown(self):
        commands.state.client, commands.state.MY_ID = self.old

    def _event(self, client, msg_id):
        replies = []
        owner_client = client

        class _Ev:
            client = owner_client  # handle_command 以 event.client is state.client 甄别入口
            chat_id = 545
            message = SimpleNamespace(id=msg_id)

            async def reply(self, text, **kwargs):
                replies.append(text)

        return _Ev(), replies

    def _flat_deleted(self):
        return [i for batch in self.deleted for i in batch]

    async def test_bot_path_never_touches_me_id_500(self):
        ev, replies = self._event(self.bot_client_sentinel, 500)
        handled = await commands.handle_command(ev, "/clearmsg")
        self.assertTrue(handled)
        flat = self._flat_deleted()
        self.assertIn(100, flat)
        self.assertIn(501, flat)
        self.assertNotIn(500, flat)  # 500 是 bot 对话 id，收藏夹同 id 不得动
        self.assertTrue(any("正在扫描" in r for r in replies))
        self.assertTrue(any("清理完成" in r for r in replies))

    async def test_saved_path_skips_then_deletes_own_command(self):
        ev, _ = self._event(self.fake_client, 500)
        handled = await commands.handle_command(ev, "/clearmsg")
        self.assertTrue(handled)
        flat = self._flat_deleted()
        self.assertIn(100, flat)
        self.assertIn(501, flat)
        self.assertIn(500, flat)  # 收藏夹路径：最后删除命令自身
        # 自身命令必须是最后一个删除批次
        self.assertEqual(self.deleted[-1], [500])


if __name__ == "__main__":
    unittest.main()
