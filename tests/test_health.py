"""health 模块测试：快照计数 / 卡死信号判定 / 视图渲染。跑法：
    .venv/bin/python -m unittest tests.test_health -v
"""
import asyncio
import atexit
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest as _btn_shim  # noqa: F401,E402

_TMP = tempfile.mkdtemp(prefix="tg_userbot_health_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ.setdefault("TG_SAVE_FOLDER", _TMP)

from tg_userbot import config, health, runtime_db, state  # noqa: E402


def _blk(counts=None, due=0, expired=0, recent=0):
    return {"counts": counts or {}, "due": due, "expired": expired,
            "recent_done": recent}


class CountsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hp_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())

    def test_queue_health_counts_shape(self):
        runtime_db.enqueue_wnacg_task(1)
        snap = runtime_db.queue_health_counts()
        self.assertEqual(snap["wnacg"]["due"], 1)
        self.assertEqual(snap["wnacg"]["counts"].get("PENDING"), 1)
        self.assertEqual(snap["recent_dl_history"], 0)
        self.assertEqual(snap["dup_running_long"], 0)

    def test_lease_expired_detection(self):
        runtime_db.enqueue_wnacg_task(2)
        runtime_db.claim_next_wnacg_task(now=1000, lease_seconds=60)
        snap = runtime_db.queue_health_counts(now=2000)
        self.assertEqual(snap["wnacg"]["expired"], 1)


class StallSignalsTest(unittest.TestCase):
    """纯函数判定：有 due 无完成=卡死；租约过期；查重超长。"""

    def _snap(self, due=0, expired=0, recent=1, dup_long=0):
        blk = lambda d, e: _blk(due=d, expired=e, recent=0)  # noqa: E731
        return {
            "extract": _blk(due=due, expired=1 if expired else 0, recent=0),
            "pawchive": _blk(recent=recent), "listener": _blk(),
            "wnacg": _blk(),
            "download": {"counts": {}},
            "recent_dl_history": 0, "dup_running_long": dup_long,
        }

    def test_due_without_completions_signals_stall(self):
        sigs = health.decide_stall_signals(self._snap(due=5, recent=0))
        keys = [s["key"] for s in sigs]
        self.assertIn("stall", keys)

    def test_due_with_recent_completion_is_quiet(self):
        sigs = health.decide_stall_signals(self._snap(due=5, recent=2))
        keys = [s["key"] for s in sigs]
        self.assertNotIn("stall", keys)

    def test_no_due_is_quiet(self):
        self.assertEqual(health.decide_stall_signals(self._snap()), [])

    def test_lease_and_dup_signals(self):
        sigs = health.decide_stall_signals(self._snap(dup_long=1, expired=1))
        keys = [s["key"] for s in sigs]
        self.assertIn("lease", keys)
        self.assertIn("dup", keys)


class RenderTest(unittest.TestCase):
    def test_render_now_smoke(self):
        # 模块态兜底：任何子模块缺状态都不炸
        with mock.patch.object(runtime_db, "queue_health_counts",
                               return_value={
                                   "extract": _blk(), "pawchive": _blk(),
                                   "listener": _blk(), "wnacg": _blk(),
                                   "download": {"counts": {"RETRY": 2}},
                                   "recent_dl_history": 1,
                                   "dup_running_long": 0}):
            state.ACTIVE_DOWNLOADS = {}
            text = health.render_now()
        self.assertIn("现在", text)
        self.assertIn("115解压", text)
        self.assertIn("待重试 2", text)
        self.assertLess(len(text), 3900)

    def test_render_health_smoke(self):
        with mock.patch.object(runtime_db, "queue_health_counts",
                               side_effect=RuntimeError("db down")):
            text = health.render_health()
        self.assertIn("巡检", text)
        self.assertIn("异常", text)

    def test_proxy_probe_no_config_ok(self):
        os.environ.pop("TG_PROXY", None)
        ok, desc = health._proxy_probe()
        self.assertTrue(ok)
        self.assertIn("直连", desc)


class NotifyCooldownTest(unittest.TestCase):
    def test_cooldown_suppresses_repeat(self):
        health._last_notify.clear()

        async def run():
            with mock.patch("tg_userbot.notify.notify_user",
                            new=mock.AsyncMock()) as m:
                first = await health._notify_signal("k1", "x")
                second = await health._notify_signal("k1", "x")
                return first, second, m.call_count
        first, second, calls = asyncio.run(run())
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
