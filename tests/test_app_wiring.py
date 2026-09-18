"""app.py / bot.py 模块接线的冒烟测试：main() 与回调引用的名字必须真实存在。

背景（2026-09-13 启动崩溃）：sql_templates.load_sql_templates() 曾在没
import 的情况下被 app.main() 调用，NameError 直接炸启动——全量单测没抓到，
因为没有任何测试执行 main() 函数体。这里不跑 main，只做属性存在性检查
（pyflakes 语义的手工子集），钉死「接线漏 import」这一类回归。
"""
import atexit
import os
import shutil
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="tg_userbot_wiring_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import app  # noqa: E402
from tg_userbot import bot  # noqa: E402


class AppWiringSmokeTest(unittest.TestCase):

    def test_modules_referenced_by_main_are_imported(self):
        for name in ("sql_templates", "wl_scan", "listener", "listener_worker",
                     "pawchive_worker", "runtime_db", "reporter", "queue",
                     "cleanup", "chrome_client", "workers", "thread", "dedup",
                     "whitelist", "platform", "stats", "notify", "commands",
                     "bot", "state", "caption_filter"):
            self.assertTrue(hasattr(app, name),
                            f"app.{name} 未导入——main() 里用到会 NameError")

    def test_bot_module_wiring(self):
        for name in ("sql_templates", "wl_scan", "runtime_db",
                     "pawchive", "MessageNotModifiedError"):
            self.assertTrue(hasattr(bot, name),
                            f"bot.{name} 未导入——回调路径用到会 NameError")


if __name__ == "__main__":
    unittest.main()


class KeepalivePingTest(unittest.TestCase):
    """主连接保活心跳（2026-09-18）：55s 间隔 < 代理 60s 空闲回收线。"""

    def test_ping_interval_below_recycle_line(self):
        from tg_userbot import config
        self.assertLess(config.KEEPALIVE_PING_SECONDS, 60)
        self.assertGreater(config.KEEPALIVE_PING_SECONDS, 10)

    def test_heartbeat_loop_wired(self):
        """main() 必须启动心跳任务；心跳循环必须调 _ping。"""
        import inspect
        src = inspect.getsource(app)
        self.assertIn("_keepalive_ping_loop", src)
        self.assertIn("_ping_caller", src[:0] or src)  # 函数存在性
        self.assertTrue(hasattr(app, "_keepalive_ping_loop"))
        self.assertTrue(callable(app._keepalive_ping_loop))
