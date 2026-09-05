"""日志模块（[T=] 追踪 + 按天轮转）的单元测试。

刻意不调 log.configure() 改全局 logger 的 handler（会干扰同进程其它测试模块）；
改而直接构造 _TraceFormatter / _make_file_handler 验证，或只读 current_log_path()。
"""
import logging
import os
import tempfile
import unittest

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_log_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import log as logmod  # noqa: E402


def _record(msg):
    return logging.LogRecord(
        "tg_userbot", logging.INFO, __file__, 1, msg, None, None
    )


class TraceFormatterTest(unittest.TestCase):
    """_TraceFormatter：有 trace 时在正文前插 [T=xxxx]，无 trace 时原样。"""

    def setUp(self):
        self.fmt = logmod._make_formatter()

    def test_inserts_trace_when_set(self):
        logmod.set_trace("abc12345")
        try:
            line = self.fmt.format(_record("开始下载"))
        finally:
            logmod.clear_trace()
        self.assertIn("[T=abc12345] 开始下载", line)

    def test_omits_trace_when_empty(self):
        logmod.clear_trace()
        line = self.fmt.format(_record("开始下载"))
        self.assertNotIn("[T=", line)
        self.assertTrue(line.endswith("开始下载"))

    def test_trace_scoped_to_current_context(self):
        # 模拟并发两任务：第二个任务的 context 里 set_trace 不影响第一个
        logmod.set_trace("aaaabbbb")
        try:
            line1 = self.fmt.format(_record("任务A"))
            ctx = logmod._trace_var  # 真实并发下每个 asyncio 任务一份 context
            tok = ctx.set("ccccdddd")
            try:
                line2 = self.fmt.format(_record("任务B"))
            finally:
                ctx.reset(tok)
            line3 = self.fmt.format(_record("任务A又一条"))
        finally:
            logmod.clear_trace()
        self.assertIn("[T=aaaabbbb]", line1)
        self.assertIn("[T=ccccdddd]", line2)
        self.assertIn("[T=aaaabbbb]", line3)  # 外层 context 未被内层污染


class RotationHandlerTest(unittest.TestCase):
    """_make_file_handler：每日零点轮转 + 只保留 N 天。"""

    def test_handler_parameters(self):
        path = os.path.join(_TMP, "runtime", "download.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler = logmod._make_file_handler(path, retention_days=7)
        try:
            self.assertEqual(handler.when, "MIDNIGHT")
            self.assertEqual(handler.backupCount, 7)
            self.assertEqual(handler.baseFilename, path)
            self.assertEqual(
                handler.suffix, "%Y-%m-%d"
            )  # 轮转后 download.log.<日期>
        finally:
            handler.close()

    def test_default_retention_seven(self):
        path = os.path.join(_TMP, "runtime", "download2.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler = logmod._make_file_handler(path)
        try:
            self.assertEqual(handler.backupCount, 7)
        finally:
            handler.close()


class CurrentLogPathTest(unittest.TestCase):
    """current_log_path() 指向 config 实际写入的 runtime/download.log。"""

    def test_points_to_runtime_log(self):
        path = logmod.current_log_path()
        self.assertIsNotNone(path)
        self.assertTrue(path.endswith(os.path.join("runtime", "download.log")))


if __name__ == "__main__":
    unittest.main()
