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


if __name__ == "__main__":
    unittest.main()
