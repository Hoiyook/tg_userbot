"""Runtime Reporter（reporter.py）单元测试。

Reporter 是**只读观察者**：本文件只验证「能否正确观察与展示既有系统产生的
状态」，不重测下载/队列/worker 自身的算法（那是各自模块测试的事）。

分层（规格 §46）：
- Level 1 纯函数：format_bytes/duration/speed/eta/ago、build_status_text、redact
- Level 2 Reporter 单元：Mock Telegram Client + 注入 state.stats 快照

不联网：所有 Telegram 调用都被 mock 掉。
运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_reporter_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

from telethon.errors import (  # noqa: E402
    FloodWaitError,
    MessageIdInvalidError,
    MessageNotModifiedError,
    RPCError,
)

from tg_userbot import app  # noqa: E402
from tg_userbot import config, state  # noqa: E402
from tg_userbot import reporter  # noqa: E402


def _snap(**kw):
    """造一份最小快照，供 build_status_text 用。"""
    s = {
        "state": "RUNNING",
        "started_wall": datetime(2026, 9, 10, 13, 20, 15),
        "uptime_seconds": 4 * 3600 + 36 * 60,
        "now_wall": datetime(2026, 9, 10, 17, 56, 21),
        "queue": {"pending": 3, "running": 2, "retry": 1},
        "downloads": [],
        "workers": [],
        "workers_total": 3,
        "workers_busy": 0,
        "workers_unhealthy": 0,
        "clients": {"main": True, "bot": True},
        "stats": {},
        "last_activity_ago": 12.0,
        "degraded_reason": None,
    }
    s.update(kw)
    return s


class PureFormatterTest(unittest.TestCase):
    """Level 1：纯函数。拿不到数据一律 `--`，绝不猜（规格 §13/§1513）。"""

    def test_format_bytes(self):
        f = reporter.format_bytes
        self.assertEqual(f(None), "--")
        self.assertEqual(f(0), "0 B")
        self.assertEqual(f(999), "999 B")
        self.assertEqual(f(2048), "2.0 KB")
        self.assertEqual(f(5 * 1024 ** 2), "5.0 MB")
        self.assertEqual(f(int(1.32 * 1024 ** 3)), "1.32 GB")

    def test_format_duration(self):
        f = reporter.format_duration
        self.assertEqual(f(None), "--")
        self.assertEqual(f(-5), "--")
        self.assertEqual(f(21), "21s")
        self.assertEqual(f(3 * 60 + 21), "03m 21s")
        self.assertEqual(f(4 * 3600 + 36 * 60), "04h 36m")
        self.assertEqual(f(2 * 86400 + 3600), "2d 01h")

    def test_format_speed(self):
        f = reporter.format_speed
        self.assertEqual(f(None), "--")
        self.assertEqual(f(0), "--")
        self.assertEqual(f(-1), "--")
        self.assertEqual(f(8.4 * 1024 ** 2), "8.40 MB/s")

    def test_format_eta(self):
        f = reporter.format_eta
        self.assertEqual(f(None, 100), "--")          # 大小未知
        self.assertEqual(f(1000, None), "--")         # 速度未知
        self.assertEqual(f(1000, 0), "--")            # speed<=0 不给 ETA
        self.assertEqual(f(1000, -3), "--")
        self.assertEqual(f(1000, 100), "00:10")
        self.assertEqual(f(3725, 1), "1:02:05")

    def test_format_ago(self):
        f = reporter.format_ago
        self.assertEqual(f(None), "--")
        self.assertEqual(f(0), "刚刚")
        self.assertEqual(f(12), "12 秒前")
        self.assertEqual(f(125), "2 分钟前")
        self.assertEqual(f(3 * 3600 + 100), "3 小时前")

    def test_redact_strips_secrets(self):
        """汇报文本不得带 token / api hash / cookie / 本机绝对路径（规格 §41）。"""
        raw = ("bot_token=123456:AAHsecretvalue here "
               "api_hash 0123456789abcdef0123456789abcdef "
               "cookie sessionid=deadbeefcafe1234567890 "
               "path /Users/someone/Downloads/x.mp4")
        out = reporter.redact(raw)
        self.assertNotIn("AAHsecretvalue", out)
        self.assertNotIn("0123456789abcdef0123456789abcdef", out)
        self.assertNotIn("deadbeefcafe1234567890", out)
        self.assertNotIn("/Users/someone", out)
        self.assertIn("x.mp4", out)

    def test_redact_leaves_ordinary_text_alone(self):
        self.assertEqual(reporter.redact("普通文件名 a.mp4"),
                         "普通文件名 a.mp4")


class BuildStatusTextTest(unittest.TestCase):
    """Level 1：面板文本。"""

    def test_contains_all_sections(self):
        text = reporter.build_status_text(_snap())
        for token in ("🤖 Userbot Runtime", "🟢 RUNNING", "04h 36m",
                      "13:20:15", "队列：3", "下载中：2", "重试中：1",
                      "Workers", "Connections", "Main Client", "Bot Client",
                      "最后活动", "12 秒前", "17:56:21"):
            self.assertIn(token, text)

    def test_state_icons(self):
        self.assertIn("🟢 RUNNING",
                      reporter.build_status_text(_snap(state="RUNNING")))
        self.assertIn("🟡 DEGRADED",
                      reporter.build_status_text(_snap(state="DEGRADED")))
        self.assertIn("🔴 ERROR",
                      reporter.build_status_text(_snap(state="ERROR")))

    def test_no_downloads_shows_placeholder(self):
        text = reporter.build_status_text(_snap())
        self.assertIn("当前下载", text)
        self.assertIn("--", text)

    def test_download_line_fields(self):
        snap = _snap(downloads=[{
            "filename": "example.mp4", "percent": 63,
            "downloaded": 820 * 1024 ** 2, "total": int(1.3 * 1024 ** 3),
            "speed": 8.4 * 1024 ** 2, "eta": "01:02",
            "worker": "#2", "elapsed": 96.0,
        }])
        text = reporter.build_status_text(snap)
        self.assertIn("example.mp4", text)
        self.assertIn("63%", text)
        self.assertIn("8.40 MB/s", text)
        self.assertIn("ETA 01:02", text)
        self.assertIn("#2", text)

    def test_unknown_download_fields_render_dash(self):
        """worker 拿不到（池禁用）时显示 --，不猜。"""
        snap = _snap(downloads=[{
            "filename": "a.mp4", "percent": None, "downloaded": 0,
            "total": None, "speed": None, "eta": "--",
            "worker": None, "elapsed": None,
        }])
        self.assertIn("--", reporter.build_status_text(snap))

    def test_many_downloads_truncated_with_remainder(self):
        """消息长度受控（规格 §14）：只列前 N 个，其余折叠成一行且保留总数。"""
        n = config.REPORT_MAX_DOWNLOADS_SHOWN
        downloads = [{"filename": f"f{i}.mp4", "percent": 1,
                      "downloaded": 1, "total": 100, "speed": 1.0,
                      "eta": "00:01", "worker": None, "elapsed": 1.0}
                     for i in range(n + 7)]
        text = reporter.build_status_text(_snap(downloads=downloads))
        self.assertIn(f"f{n - 1}.mp4", text)
        self.assertNotIn(f"f{n}.mp4", text)
        self.assertIn("还有 7 个下载任务", text)

    def test_workers_block_is_compact_counts_plus_alerts(self):
        """Workers 只给计数 + 异常明细：20 条 worker 逐行罗列会把消息顶到 4096。"""
        snap = _snap(
            workers=[{"label": f"#{i}", "state": "IDLE", "health": "HEALTHY",
                      "reason": None} for i in range(1, 21)],
            workers_total=20, workers_busy=3, workers_unhealthy=0,
        )
        text = reporter.build_status_text(snap)
        self.assertIn("20 条：3 忙碌 / 17 空闲", text)
        self.assertNotIn("#1 IDLE", text, "正常 worker 不该逐条罗列")

    def test_workers_block_lists_unhealthy_with_reason(self):
        snap = _snap(
            workers=[
                {"label": "#1", "state": "BUSY", "health": "HEALTHY",
                 "reason": None},
                {"label": "#2", "state": "IDLE", "health": "UNHEALTHY",
                 "reason": "OSError: 网络不可达"},
            ],
            workers_total=2, workers_busy=1, workers_unhealthy=1,
        )
        text = reporter.build_status_text(snap)
        self.assertIn("2 条：1 忙碌 / 0 空闲 / 1 异常", text)
        self.assertIn("#2 UNHEALTHY", text)
        self.assertIn("网络不可达", text)

    def test_clients_disconnected_rendered(self):
        snap = _snap(clients={"main": True, "bot": False})
        text = reporter.build_status_text(snap)
        self.assertIn("Main Client：🟢", text)
        self.assertIn("Bot Client：🔴", text)

    def test_stats_section_rendered(self):
        snap = _snap(stats={"received": 18, "success": 13, "failed_final": 1,
                            "retries": 4, "auto_replay": 3, "dedup_hit": 2,
                            "cancelled": 0})
        text = reporter.build_status_text(snap)
        self.assertIn("收到：18", text)
        self.assertIn("成功：13", text)
        self.assertIn("自动重放：3", text)

    def test_missing_stats_render_dash(self):
        """统计拿不到（事件口径不可用）时显示 --，不编数字。"""
        snap = _snap(stats=None)
        self.assertIn("收到：--", reporter.build_status_text(snap))


class _FakeClient:
    """假 Telegram 客户端：记录 send_message / edit_message 调用与序号。"""

    def __init__(self):
        self.sent = []
        self.edited = []
        self._next_id = 100
        self.send_error = None
        self.edit_error = None

    def is_connected(self):
        return True

    async def send_message(self, target, text):
        if self.send_error:
            raise self.send_error
        self.sent.append((target, text))
        self._next_id += 1
        return mock.MagicMock(id=self._next_id)

    async def edit_message(self, target, message, text):
        if self.edit_error:
            raise self.edit_error
        self.edited.append((target, message, text))


class StatusPanelTest(unittest.IsolatedAsyncioTestCase):
    """Level 2：Status Panel 生命周期（规格 §7/§34/§35/§38）。"""

    async def asyncSetUp(self):
        self.fake = _FakeClient()
        self.rep = reporter.Reporter(client=self.fake)
        self.rep._started_at = 0.0

    async def test_first_update_creates_then_edits(self):
        await self.rep.update_status()
        self.assertEqual(len(self.fake.sent), 1)
        self.assertEqual(self.fake.edited, [])
        self.assertIsNotNone(self.rep.status_message_id)

        await self.rep.update_status()
        self.assertEqual(len(self.fake.sent), 1, "不得重复创建面板消息")
        self.assertEqual(len(self.fake.edited), 1)

    async def test_message_not_modified_is_normal(self):
        """内容没变时 Telegram 报 MessageNotModified——视为正常，不算错误、
        也不重建消息（规格 §34）。"""
        await self.rep.update_status()
        self.fake.edit_error = MessageNotModifiedError(request=None)
        before = self.rep.status_message_id
        await self.rep.update_status()
        self.assertEqual(self.rep.status_message_id, before)
        self.assertEqual(len(self.fake.sent), 1)

    async def test_message_id_invalid_recreates(self):
        """面板被清理掉了 → 下一轮重新创建，而不是永久失效（规格 §35）。"""
        await self.rep.update_status()
        self.fake.edit_error = MessageIdInvalidError(request=None)
        await self.rep.update_status()
        self.assertIsNone(self.rep.status_message_id) or None
        self.fake.edit_error = None
        await self.rep.update_status()
        self.assertEqual(len(self.fake.sent), 2, "应重新创建一条新面板")

    async def test_floodwait_skips_without_tight_loop(self):
        """FloodWait 记日志跳过本轮，不进入紧密重试循环（规格 §36）。"""
        self.fake.send_error = FloodWaitError(request=None, capture=42)
        await self.rep.update_status()
        self.assertEqual(len(self.fake.sent), 0)
        self.assertIsNone(self.rep.status_message_id)

    async def test_rpc_error_does_not_raise(self):
        self.fake.send_error = RPCError(request=None, message="boom")
        await self.rep.update_status()      # 不抛


class MessageLengthBudgetTest(unittest.TestCase):
    """消息长度硬约束：超 4096 会直接发不出去，而失败被静默吞成「没反应」。"""

    def _download(self, i, name_len=300):
        return {"filename": ("长" * name_len) + f"-{i}.mp4", "percent": 63,
                "downloaded": 820 * 1024 ** 2, "total": int(1.3 * 1024 ** 3),
                "speed": 8.4 * 1024 ** 2, "eta": "01:02",
                "worker": "#2", "elapsed": 96.0}

    def test_never_exceeds_hard_cap_with_long_names(self):
        snap = _snap(downloads=[self._download(i) for i in range(30)])
        text = reporter.build_status_text(snap)
        self.assertLessEqual(len(text), config.REPORT_MAX_MESSAGE_CHARS)

    def test_long_filename_clipped_keeping_tail(self):
        """保尾裁剪：区分性最强的原始文件名在末尾。"""
        snap = _snap(downloads=[self._download(0, name_len=300)])
        text = reporter.build_status_text(snap)
        self.assertIn("-0.mp4", text)
        self.assertNotIn("长" * 60, text, "超长文件名应被裁剪")

    def test_budget_shrinks_download_list_before_hard_truncation(self):
        """优先「少显示几条」而不是把消息硬切成半截。"""
        snap = _snap(downloads=[self._download(i) for i in range(30)])
        text = reporter.build_status_text(snap)
        self.assertIn("…还有", text)
        self.assertIn("更新时间", text, "硬截断不该吃掉收尾区块")

    def test_short_message_untouched(self):
        snap = _snap()
        self.assertLess(len(reporter.build_status_text(snap)), 2000)


class TargetResolutionTest(unittest.TestCase):
    """汇报目标：bot 私聊优先，bot 未就绪回落收藏夹（绝不发丢）。"""

    def setUp(self):
        self.old_bot_id = state.BOT_ID

    def tearDown(self):
        state.BOT_ID = self.old_bot_id

    def test_uses_bot_chat_when_available(self):
        state.BOT_ID = 8942353476
        with mock.patch.object(reporter, "REPORT_TO_BOT_CHAT", True):
            self.assertEqual(reporter.Reporter().target, 8942353476)

    def test_falls_back_when_bot_missing(self):
        state.BOT_ID = None
        with mock.patch.object(reporter, "REPORT_TO_BOT_CHAT", True):
            self.assertEqual(reporter.Reporter().target,
                             config.REPORT_FALLBACK_TARGET)

    def test_can_be_pinned_to_saved_messages(self):
        state.BOT_ID = 8942353476
        with mock.patch.object(reporter, "REPORT_TO_BOT_CHAT", False):
            self.assertEqual(reporter.Reporter().target,
                             config.REPORT_FALLBACK_TARGET)


class StatsCacheTest(unittest.TestCase):
    """统计缓存：事件流没新增时不重算（全量扫描 3 万行约 116ms 同步阻塞）。"""

    def setUp(self):
        self.calls = []
        self._p = mock.patch.object(reporter.stats, "load_events",
                                    lambda *a, **k: self.calls.append(1) or [])
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_reuses_cached_stats_within_ttl(self):
        rep = reporter.Reporter(client=None)
        rep._stats_dirty = False
        rep.collect_stats(now=100.0)          # 首次：算
        rep.collect_stats(now=110.0)          # 10 秒后：复用
        rep.collect_stats(now=200.0)
        self.assertEqual(len(self.calls), 1, "缓存期内不该重复全量扫描")

    def test_recomputes_after_ttl(self):
        rep = reporter.Reporter(client=None)
        rep._stats_dirty = False
        rep.collect_stats(now=100.0)
        rep.collect_stats(now=100.0 + config.REPORT_STATS_CACHE_SECONDS + 1)
        self.assertEqual(len(self.calls), 2)

    def test_dirty_flag_forces_recompute(self):
        rep = reporter.Reporter(client=None)
        rep._stats_dirty = False
        rep.collect_stats(now=100.0)
        rep._stats_dirty = True               # poll_events 读到新事件
        rep.collect_stats(now=101.0)
        self.assertEqual(len(self.calls), 2, "事件有新增就该重算")


class TickCadenceTest(unittest.IsolatedAsyncioTestCase):
    """tick 的刷新节奏：启动后第一跳必须立刻建面板。

    回归（2026-09-10 真机踩到）：macOS 上 `time.monotonic()` 从 ~0 起步（实测
    首次调用返回 0.006，而非开机至今秒数）。若用 `0.0` 当作「从未刷新过」的
    哨兵，`now - 0.0 >= 间隔` 恒为 False → **面板永远不出现**，而且不报任何错
    （既不 send 也不 log），只能靠「日志里没有面板创建行」发现。
    钉死：把时钟钉在接近 0 的位置，首跳仍必须发出面板。
    """

    async def asyncSetUp(self):
        self.fake = _FakeClient()

    async def test_first_tick_creates_panel_with_clock_near_zero(self):
        rep = reporter.Reporter(client=self.fake)
        with mock.patch.object(reporter.time, "monotonic",
                               lambda: 0.006):
            await rep.tick()
        self.assertEqual(len(self.fake.sent), 1,
                         "时钟从 ~0 起步时，首跳也必须立刻建面板")
        self.assertIsNotNone(rep.status_message_id)

    async def test_second_tick_edits_instead_of_recreating(self):
        rep = reporter.Reporter(client=self.fake)
        with mock.patch.object(reporter.time, "monotonic",
                               lambda: 1.0):
            await rep.tick()
            await rep.tick()
        self.assertEqual(len(self.fake.sent), 1, "不得重复创建面板")
        self.assertEqual(len(self.fake.edited), 0,
                         "第二次 tick 距首次不足间隔 → 不该刷新")


class EventPollTest(unittest.IsolatedAsyncioTestCase):
    """Level 2：事件流增量读取与通知（规格 §20/§22/§39）。"""

    async def asyncSetUp(self):
        self.fake = _FakeClient()
        self.rep = reporter.Reporter(client=self.fake)
        self.ev_file = os.path.join(_TMP, "reporter_events.jsonl")
        with open(self.ev_file, "w", encoding="utf-8") as f:
            f.write("")                     # 空文件
        self._p = mock.patch.object(reporter, "TASK_EVENTS_FILE", self.ev_file)
        self._p.start()

    async def asyncTearDown(self):
        self._p.stop()
        if os.path.exists(self.ev_file):
            os.remove(self.ev_file)

    def _append(self, **rec):
        import json
        with open(self.ev_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    async def test_history_before_start_is_not_replayed(self):
        """启动前就存在的事件不得被重新通知给用户（规格 §39）。"""
        self._append(ts="2026-09-10 10:00:00", ev="AUTO_REPLAY", id="old",
                     label="old.mp4", attempts=1)
        self.rep.mark_event_cursor()        # 记录起点 = 当前 EOF
        self._append(ts="2026-09-10 11:00:00", ev="AUTO_REPLAY", id="new1",
                     label="new.mp4", attempts=1)
        await self.rep.poll_events()
        labels = [t for _, t in self.fake.sent]
        self.assertTrue(any("new.mp4" in t for t in labels))
        self.assertFalse(any("old.mp4" in t for t in labels))

    async def test_new_events_only_reported_once(self):
        self.rep.mark_event_cursor()
        self._append(ts="2026-09-10 11:00:00", ev="AUTO_REPLAY", id="n1",
                     label="n1.mp4", attempts=1)
        await self.rep.poll_events()
        n_after_first = len(self.fake.sent)
        await self.rep.poll_events()
        self.assertEqual(len(self.fake.sent), n_after_first,
                         "同一事件被重复通知")

    async def test_auto_replay_aggregated_into_one_message(self):
        """一轮重放 N 条 → 一条聚合通知，而不是 N 条刷屏。"""
        self.rep.mark_event_cursor()
        for i in range(5):
            self._append(ts="2026-09-10 11:00:00", ev="AUTO_REPLAY",
                         id=f"t{i}", label=f"t{i}.mp4", attempts=2)
        await self.rep.poll_events()
        auto = [t for _, t in self.fake.sent if "自动重放" in t]
        self.assertEqual(len(auto), 1, "自动重放应聚合成一条")
        self.assertIn("5", auto[0])

    async def test_auto_replay_notification_can_be_disabled(self):
        with mock.patch.object(reporter, "REPORT_AUTO_REPLAY", False):
            self.rep.mark_event_cursor()
            self._append(ts="2026-09-10 11:00:00", ev="AUTO_REPLAY",
                         id="t1", label="a.mp4", attempts=1)
            await self.rep.poll_events()
        self.assertEqual([t for _, t in self.fake.sent if "自动重放" in t], [])

    async def test_cursor_survives_file_replacement(self):
        """事件文件被原子重写（trim）后变小 → 游标必须重置，不得读出错位内容。"""
        self.rep.mark_event_cursor()
        with open(self.ev_file, "w", encoding="utf-8") as f:
            f.write("")                      # 重写成更短
        self._append(ts="2026-09-10 11:00:00", ev="AUTO_REPLAY", id="n1",
                     label="after-trim.mp4", attempts=1)
        await self.rep.poll_events()         # 不抛
        self.assertTrue(any("after-trim.mp4" in t for _, t in self.fake.sent))


class ErrorDedupTest(unittest.TestCase):
    """Level 2：异常去重（规格 §26）。同一异常只通知一次，恢复再通知一次。"""

    def setUp(self):
        self.rep = reporter.Reporter(client=_FakeClient())

    def test_same_error_reported_once(self):
        self.assertTrue(self.rep.note_error("worker", "#2", "Connection error"))
        self.assertFalse(self.rep.note_error("worker", "#2", "Connection error"))
        self.assertFalse(self.rep.note_error("worker", "#2", "Connection error"))

    def test_recovery_reported_once(self):
        self.rep.note_error("worker", "#2", "Connection error")
        self.assertTrue(self.rep.note_recovery("worker", "#2"))
        self.assertFalse(self.rep.note_recovery("worker", "#2"))

    def test_different_component_is_a_different_fingerprint(self):
        self.assertTrue(self.rep.note_error("worker", "#2", "Connection error"))
        self.assertTrue(self.rep.note_error("worker", "#3", "Connection error"))

    def test_normalized_message_ignores_volatile_bits(self):
        """fingerprint 不能含易变片段（端口/时长/内存地址），否则每次都算新错误。"""
        self.assertTrue(self.rep.note_error(
            "worker", "#2", "Connection reset to 1.2.3.4:443 after 12 秒"))
        self.assertFalse(self.rep.note_error(
            "worker", "#2", "Connection reset to 9.9.9.9:8443 after 47 秒"))

    def test_plain_state_reflects_health(self):
        self.assertEqual(self.rep.error_state("worker", "#2"), "NORMAL")
        self.rep.note_error("worker", "#2", "boom")
        self.assertEqual(self.rep.error_state("worker", "#2"), "ERROR")
        self.rep.note_recovery("worker", "#2")
        self.assertEqual(self.rep.error_state("worker", "#2"), "NORMAL")


class ExceptionIsolationTest(unittest.IsolatedAsyncioTestCase):
    """Level 2：Reporter 自身故障不得打死自己、也不得影响核心（规格 §32）。"""

    async def test_run_loop_survives_cycle_errors(self):
        fake = _FakeClient()
        rep = reporter.Reporter(client=fake)
        rep._started_at = 0.0
        ticks = []

        async def boom():
            ticks.append(1)
            raise ValueError("boom")

        with mock.patch.object(rep, "tick", boom), \
                mock.patch.object(reporter, "REPORT_EVENT_POLL_SECONDS", 0.01), \
                mock.patch.object(reporter, "REPORT_INTERVAL_SECONDS", 0.01):
            task = asyncio.create_task(rep.run())
            await asyncio.sleep(0.06)
            cancelled = task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertGreaterEqual(len(ticks), 2, "一次异常就把主循环打死了")
        self.assertTrue(cancelled)

    async def test_cancel_propagates(self):
        rep = reporter.Reporter(client=_FakeClient())
        with mock.patch.object(reporter, "REPORT_EVENT_POLL_SECONDS", 0.01), \
                mock.patch.object(reporter, "REPORT_INTERVAL_SECONDS", 0.01):
            task = asyncio.create_task(rep.run())
            await asyncio.sleep(0.03)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


class SnapshotTest(unittest.TestCase):
    """Level 2：快照采集只读既有 state，不创建第二套状态（规格 §2.2）。"""

    def setUp(self):
        self.old = (state.ACTIVE_DOWNLOADS, state.QUEUE, state.EXECUTING,
                    state.client, state.bot_client, state.DOWNLOAD_WORKERS)
        state.ACTIVE_DOWNLOADS = {}
        state.QUEUE = {"tasks": [], "retry": []}
        state.EXECUTING = set()
        state.DOWNLOAD_WORKERS = []
        self.addCleanup(self._restore)

    def _restore(self):
        (state.ACTIVE_DOWNLOADS, state.QUEUE, state.EXECUTING,
         state.client, state.bot_client, state.DOWNLOAD_WORKERS) = self.old

    def test_downloads_snapshot_copies_and_derives_speed(self):
        rep = reporter.Reporter(client=None)
        state.ACTIVE_DOWNLOADS[1] = {
            "filename": "a.mp4", "total": 1000, "downloaded": 100,
            "percent": 10, "worker": "#1", "started_at": 0.0,
        }
        first = rep.collect_downloads(now=100.0)
        self.assertEqual(first[0]["speed"], None, "首次采样没有速度")
        state.ACTIVE_DOWNLOADS[1]["downloaded"] = 300
        second = rep.collect_downloads(now=102.0)
        self.assertAlmostEqual(second[0]["speed"], 100.0, places=3)
        self.assertIn(second[0]["eta"], ("00:07", "00:06", "00:08"))

    def test_snapshot_does_not_mutate_state(self):
        rep = reporter.Reporter(client=None)
        state.ACTIVE_DOWNLOADS[1] = {
            "filename": "a.mp4", "total": 10, "downloaded": 1,
            "percent": 10, "worker": None, "started_at": 0.0,
        }
        before = dict(state.ACTIVE_DOWNLOADS[1])
        rep.snapshot()
        self.assertEqual(state.ACTIVE_DOWNLOADS[1], before)

    def test_queue_counts_pending(self):
        rep = reporter.Reporter(client=None)
        state.QUEUE = {"tasks": [{"id": "a"}, {"id": "b"}],
                       "retry": [{"id": "c"}]}
        snap = rep.snapshot()
        self.assertEqual(snap["queue"]["pending"], 2)
        self.assertEqual(snap["queue"]["retry"], 1)

    def test_no_pool_reports_error_state(self):
        """池禁用 + 主客户端未连接 → ERROR；主客户端正常但 worker 异常 → DEGRADED。"""
        rep = reporter.Reporter(client=None)
        snap = rep.snapshot()
        self.assertEqual(snap["state"], "ERROR")

    def test_bot_down_is_degraded_not_error(self):
        rep = reporter.Reporter(client=_FakeClient())
        state.bot_client = None
        snap = rep.snapshot()
        self.assertIn(snap["state"], ("RUNNING", "DEGRADED"))

    def test_unhealthy_worker_is_degraded(self):
        rep = reporter.Reporter(client=_FakeClient())
        state.DOWNLOAD_WORKERS = [object(), object()]

        def fake_snapshot():
            return [
                {"label": "#1", "state": "IDLE", "health": "HEALTHY",
                 "reason": None},
                {"label": "#2", "state": "BUSY", "health": "UNHEALTHY",
                 "reason": "Connection error"},
            ]
        with mock.patch.object(reporter.workers, "worker_snapshot",
                               fake_snapshot):
            snap = rep.snapshot()
        self.assertEqual(snap["state"], "DEGRADED")
        self.assertEqual(snap["workers_unhealthy"], 1)


class StartReporterWiringTest(unittest.IsolatedAsyncioTestCase):
    """app._start_reporter 的接线：防「函数写了但没人用」与「关了还起」。"""

    async def test_disabled_returns_nothing(self):
        with mock.patch.object(app, "REPORT_ENABLED", False):
            inst, task = await app._start_reporter()
        self.assertIsNone(inst)
        self.assertIsNone(task)

    async def test_enabled_starts_and_creates_task(self):
        started = []

        class FakeReporter:
            async def start(self):
                started.append(1)

            async def run(self):
                await asyncio.sleep(3600)

        with mock.patch.object(app.reporter, "Reporter", FakeReporter), \
                mock.patch.object(app, "REPORT_ENABLED", True):
            inst, task = await app._start_reporter()
        self.assertIsInstance(inst, FakeReporter)
        self.assertEqual(len(started), 1, "入口没调 start()")
        self.assertIsNotNone(task)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_start_failure_does_not_break_startup(self):
        """Reporter 起不来（如 Telegram 不可用）绝不能影响主程序启动。"""

        class BoomReporter:
            async def start(self):
                raise RuntimeError("网络不可达")

            async def run(self):
                await asyncio.sleep(3600)

        with mock.patch.object(app.reporter, "Reporter", BoomReporter), \
                mock.patch.object(app, "REPORT_ENABLED", True):
            inst, task = await app._start_reporter()      # 不抛
        self.assertIsNotNone(inst)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()
