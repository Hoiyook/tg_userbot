"""指令回复统一代码块化（text.with_code_block）测试。

契约：多行回复 → 首行（前缀行，自动清理白名单 startswith 依赖它）留在
围栏外，其余正文包进 ``` 围栏；已含围栏（/sh 输出）与单行回复原样不动。

    .venv/bin/python -m unittest tests.test_reply_format -v
"""
import os
import shutil
import tempfile
import unittest

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录（config import 期
# 有真实副作用，见 test_config 的基座说明）。
_TMP = tempfile.mkdtemp(prefix="tg_userbot_reply_format_test_")
import atexit  # noqa: E402
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import text  # noqa: E402


class TestWithCodeBlock(unittest.TestCase):
    def test_multiline_wrapped_first_line_outside(self):
        src = "📥 下载队列（共 2 条）：\n\n1. 视频.mp4\n2. 音频.mp3"
        out = text.with_code_block(src)
        self.assertTrue(out.startswith("📥 下载队列（共 2 条）：\n```"))
        self.assertTrue(out.endswith("```"))
        self.assertIn("1. 视频.mp4", out)

    def test_single_line_unchanged(self):
        self.assertEqual(text.with_code_block("✅ 已从白名单移除"),
                         "✅ 已从白名单移除")

    def test_already_fenced_unchanged(self):
        src = "$ df -h\n```\nFilesystem ...\n```"
        self.assertEqual(text.with_code_block(src), src)

    def test_empty_unchanged(self):
        self.assertEqual(text.with_code_block(""), "")

    def test_prefix_still_startswith_after_wrap(self):
        """自动清理白名单按 startswith 匹配——包装后必须依然成立。"""
        src = "📋 下载白名单（2 个）：\n- chat A\n- chat B"
        out = text.with_code_block(src)
        self.assertTrue(out.startswith("📋 下载白名单"))


if __name__ == "__main__":
    unittest.main()
