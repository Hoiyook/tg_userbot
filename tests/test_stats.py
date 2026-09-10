"""台账/对账（stats.py）的单元测试：任务事件层 + task_id 生命周期重建 + 渲染。

stats 的契约：窗口内有任务事件（runtime/task_events.jsonl）→ 按 task_id
严格重建（rebuild_stats：任务集按窗口内最后终态分区，对账恒等式恒成立）；
窗口内无事件（功能上线前的老日子）→ 回落 download.log/download_history.txt
关键词口径并注明估算。日期窗口以「注入的 today」为基准（测试可定死日期），
days=1 只看 today 当天，days=N 看 today 起往前的 N 个自然日。

运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import atexit
import os
import shutil
import tempfile
import unittest
from datetime import date
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_stats_test_")
# 退出时回收临时目录（测试跑完就地删，别让 /var/folders 越堆越多）
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import state  # noqa: E402
from tg_userbot import stats  # noqa: E402

# 统一用这个「今天」，让窗口断言与真实日期解耦
TODAY = date(2026, 9, 7)
YESTERDAY = date(2026, 9, 6)

LOG_LINES = [
    # 昨天：一条收藏夹媒体（只应进 days>=2 的窗口）
    f"{YESTERDAY} 23:59:01 | INFO | 📨 Saved Messages 收到消息 | ID=1 "
    "| media=Video | grouped=None | file=old.mp4 | has_document=yes",
    f"{YESTERDAY} 23:59:02 | INFO | 📦 检测到可下载媒体 | 类型=Video "
    "| 文件=old.mp4",
    # 今天：纯文本消息（media=None，不计媒体）
    f"{TODAY} 08:00:01 | INFO | 📨 Saved Messages 收到消息 | ID=2 "
    "| media=None | grouped=None | file=None",
    # 今天：网页预览（MessageMediaWebPage 不是可下载媒体，不计）
    f"{TODAY} 08:00:02 | INFO | 📨 Saved Messages 收到消息 | ID=3 "
    "| media=MessageMediaWebPage | grouped=None | file=None",
    # 今天：收藏夹 1 条媒体 + 白名单中转 1 条媒体
    f"{TODAY} 09:19:35 | INFO | 📨 Saved Messages 收到消息 | ID=4 "
    "| media=Video | grouped=1 | file=a.mp4",
    f"{TODAY} 09:19:36 | INFO | 📨 白名单 chat（解析bot） 收到消息 | ID=5 "
    "| media=Video | grouped=None | file=b.mp4",
    f"{TODAY} 09:19:37 | INFO | 📦 检测到可下载媒体 | 类型=Video | 文件=a.mp4",
    f"{TODAY} 09:19:38 | INFO | 📦 检测到可下载媒体 | 类型=Video | 文件=b.mp4",
    # 今天：解析一本地一中转
    f"{TODAY} 09:19:39 | INFO | 🛠 抖音链接已本地解析并入队下载：xxx.mp4",
    f"{TODAY} 09:19:40 | INFO | 📤 已把抖音链接发给解析 bot（@DouYintg_bot），"
    "回复视频将自动转发进收藏夹下载",
    # 今天：失败一次 → 重试 → 再失败 → 最终失败
    f"{TODAY} 09:20:00 | ERROR | [T=abc] ❌ 下载失败，尝试第 1 次（上限 3）：x",
    f"{TODAY} 09:20:10 | ERROR | [T=abc] ❌ 已达到最大重试次数，下载失败",
    # 今天：三条「收了但两不属于成功/待重试」的出口（勾稽桶）
    f"{TODAY} 09:21:00 | INFO | 🗑 手动移除队列任务（在途已取消）：x.mp4",
    f"{TODAY} 09:21:01 | INFO | 队列任务原消息已被删除：y.mp4",
    f"{TODAY} 09:21:02 | INFO | ⏭️ 重复媒体跳过入队（消息 88）",
    # 昨天：手动移除（只应进 days>=2 的窗口）
    f"{YESTERDAY} 23:58:00 | INFO | 🗑 手动移除队列任务（未在途）：old.mp4",
]

HISTORY_LINES = [
    f"{TODAY} 09:33:45 | 普通 | a.mp4 | 1.00 GB | 来源：祂录（3D区）",
    f"{TODAY} 09:35:33 | 抖音 | b.mp4 | 512.00 MB | 来源：本地解析",
    f"{YESTERDAY} 23:00:00 | 普通 | old.mp4 | 1.00 KB | 来源：旧",
]


def _write(paths):
    log_path = os.path.join(_TMP, "stats.log")
    history_path = os.path.join(_TMP, "stats_history.txt")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(LOG_LINES) + "\n")
    with open(history_path, "w", encoding="utf-8") as f:
        f.write("\n".join(HISTORY_LINES) + "\n")
    return log_path, history_path


class ParseSizeTest(unittest.TestCase):
    """parse_size：format_size 输出的反解（台账合计字节数用）。"""

    def test_units(self):
        self.assertEqual(stats.parse_size("123.00 B"), 123)
        self.assertEqual(stats.parse_size("1.00 KB"), 1024)
        self.assertEqual(stats.parse_size("512.00 MB"), 512 * 1024 * 1024)
        self.assertEqual(stats.parse_size("1.00 GB"), 1024 * 1024 * 1024)

    def test_garbage_returns_zero(self):
        self.assertEqual(stats.parse_size("未知大小"), 0)
        self.assertEqual(stats.parse_size(""), 0)


class CollectStatsTest(unittest.TestCase):
    """collect_stats：按自然日窗口从日志+历史文件计数。"""

    def setUp(self):
        self.log_path, self.history_path = _write(None)

    def test_today_window_counts_only_today(self):
        s = stats.collect_stats(
            1, today=TODAY, log_path=self.log_path,
            history_path=self.history_path,
        )
        self.assertEqual(s["media_total"], 2)      # 两条 📦 都在今天
        self.assertEqual(s["media_me"], 1)         # 收藏夹媒体（排除 None/网页）
        self.assertEqual(s["media_wl"], 1)         # 白名单中转媒体
        self.assertEqual(s["parse_local"], 1)
        self.assertEqual(s["parse_relay"], 1)
        self.assertEqual(s["fail_attempts"], 1)
        self.assertEqual(s["fail_final"], 1)
        self.assertEqual(s["success_count"], 2)    # 1.00 GB + 512.00 MB
        self.assertEqual(
            s["success_bytes"],
            1024 ** 3 + 512 * 1024 * 1024,
        )
        self.assertEqual(s["manual_del"], 1)       # 今日手动移除（昨天那条不进）
        self.assertEqual(s["msg_deleted"], 1)      # 原消息被删 → 移除出口
        self.assertEqual(s["dedup_skip"], 1)       # 去重跳过出口

    def test_two_day_window_includes_yesterday(self):
        s = stats.collect_stats(
            2, today=TODAY, log_path=self.log_path,
            history_path=self.history_path,
        )
        self.assertEqual(s["media_total"], 3)      # 昨天那条也进来
        self.assertEqual(s["media_me"], 2)
        self.assertEqual(s["success_count"], 3)
        self.assertEqual(s["success_bytes"], 1024 ** 3 + 512 * 1024 * 1024 + 1024)
        self.assertEqual(s["manual_del"], 2)       # 今天 + 昨天各一条

    def test_content_level_hit_counts_into_dedup_bucket(self):
        """内容级拦截（下载后、落盘前）也进「去重」桶：该出口的任务既无
        成功 history 行也不转 retry，不计数则勾稽恒等式破。"""
        log_path = os.path.join(_TMP, "stats_content.log")
        lines = [
            f"{TODAY} 10:00:00 | INFO | 📨 Saved Messages 收到消息 | ID=9 "
            "| media=Video | grouped=None | file=dup.mp4",
            f"{TODAY} 10:00:01 | INFO | 📦 检测到可下载媒体 | 类型=Video "
            "| 文件=dup.mp4",
            f"{TODAY} 10:00:05 | INFO | ⏭️ 内容重复已拦截落盘"
            "（与已下载文件字节相同）：dup.mp4",
        ]
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        # 空 history：隔离 setUp 夹具里的今日成功行，只看内容拦截这一个出口
        history_path = os.path.join(_TMP, "stats_content_history.txt")
        with open(history_path, "w", encoding="utf-8") as f:
            f.write("")
        s = stats.collect_stats(
            1, today=TODAY, log_path=log_path,
            history_path=history_path,
        )
        self.assertEqual(s["media_total"], 1)
        self.assertEqual(s["success_count"], 0)    # 没落盘 → 无 history 行
        self.assertEqual(s["dedup_skip"], 1)       # 计入去重桶，勾稽才平


class StatsTextTest(unittest.TestCase):
    """stats_text：渲染出台账文本；在途/待处理/待重试读 state。"""

    def setUp(self):
        self.log_path, self.history_path = _write(None)
        self.old = {
            "queue": state.QUEUE,
            "active": state.ACTIVE_DOWNLOADS,
        }

    def tearDown(self):
        state.QUEUE = self.old["queue"]
        state.ACTIVE_DOWNLOADS = self.old["active"]

    def test_today_text_contains_counts(self):
        state.QUEUE = {"tasks": [{"id": "x"}], "retry": [{"id": "y"}]}
        state.ACTIVE_DOWNLOADS = {1: {}, 2: {}}
        text = stats.stats_text(
            1, today=TODAY, log_path=self.log_path,
            history_path=self.history_path,
        )
        self.assertIn("台账", text)
        self.assertIn("媒体：2 条", text)        # 收到媒体总数
        self.assertIn("收藏 1", text)
        self.assertIn("中转 1", text)
        self.assertIn("本地 1", text)            # 解析
        self.assertIn("2 个", text)              # 下载成功
        self.assertIn("最终失败 1", text)
        self.assertIn("重试 1 次", text)
        self.assertIn("在途 2", text)
        self.assertIn("待处理 1", text)
        self.assertIn("待重试 1", text)
        # 勾稽两出口 + 勾稽行（fixture 不平衡：收到 2 ≠ 2+1+2+1+2+1 → 差 -7）
        self.assertIn("移除：手动 1", text)
        self.assertIn("原消息删除 1", text)
        self.assertIn("去重跳过：1 条", text)
        self.assertIn("勾稽：", text)
        self.assertIn("差 -7", text)

    def test_reconciliation_balanced_when_identity_holds(self):
        """收到 = 成功 + 待重试 + 移除 + 去重 + 在途/待处理 时勾稽打 ✓。"""
        log_path = os.path.join(_TMP, "stats_bal.log")
        history_path = os.path.join(_TMP, "stats_bal_history.txt")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join([
                f"{TODAY} 10:00:00 | INFO | 📦 检测到可下载媒体 | 类型=Video "
                "| 文件=a",
                f"{TODAY} 10:00:01 | INFO | 📦 检测到可下载媒体 | 类型=Video "
                "| 文件=b",
                f"{TODAY} 10:00:02 | INFO | 📦 检测到可下载媒体 | 类型=Photo "
                "| 文件=c",
                f"{TODAY} 10:00:03 | INFO | ⏭️ 重复媒体跳过入队（消息 88）",
                f"{TODAY} 10:00:04 | INFO | 🗑 手动移除队列任务（在途已取消）：b",
            ]) + "\n")
        with open(history_path, "w", encoding="utf-8") as f:
            f.write(f"{TODAY} 10:01:00 | 普通 | a.mp4 | 1.00 KB | 来源：x\n")
        state.QUEUE = {"tasks": [], "retry": []}
        state.ACTIVE_DOWNLOADS = {}
        text = stats.stats_text(
            1, today=TODAY, log_path=log_path, history_path=history_path,
        )
        # 3 = 1 成功 + 0 待重试 + 1 移除 + 1 去重 + 0 在途/待处理
        self.assertIn("= 收到 3 ✓", text)

    def test_multi_day_header(self):
        text = stats.stats_text(
            3, today=TODAY, log_path=self.log_path,
            history_path=self.history_path,
        )
        self.assertIn("最近 3 天", text)
        self.assertIn("09-05", text)
        self.assertIn("09-07", text)

    def test_single_day_header(self):
        text = stats.stats_text(
            1, today=TODAY, log_path=self.log_path,
            history_path=self.history_path,
        )
        self.assertIn("09-07", text)


class IsStatsCommandTest(unittest.TestCase):
    """is_stats_command：/stats、/stats 3 算；其它不算。"""

    def test_matches(self):
        self.assertTrue(stats.is_stats_command("/stats"))
        self.assertTrue(stats.is_stats_command("/stats 3"))
        self.assertTrue(stats.is_stats_command("  /STATS 7  "))

    def test_rejects(self):
        self.assertFalse(stats.is_stats_command("/statsx"))
        self.assertFalse(stats.is_stats_command("/status"))
        self.assertFalse(stats.is_stats_command("看看台账"))


def _ev(day, hhmmss, ev, task_id=None, **extra):
    """造一条合成事件（day: 6=昨天 7=今天，对应 YESTERDAY/TODAY）。"""
    rec = {"ts": f"2026-09-{day:02d} {hhmmss}", "ev": ev}
    if task_id:
        rec["id"] = task_id
    rec.update(extra)
    return rec


class RebuildStatsTest(unittest.TestCase):
    """按 task_id 重建统计：一个任务一条生命线，retry 不产生新任务，
    白名单转发只有一个任务，去重跳过不产生任务，bytes 精确累计。"""

    def _rebuild(self, events, days=1):
        return stats.rebuild_stats(events, days=days, today=TODAY)

    def test_auto_replay_event_does_not_perturb_reconciliation(self):
        """新增的 AUTO_REPLAY 事件（队列自动放行时发）不得改变任何统计口径：
        它既不是终态、也不产生新任务，只给已有 task 的生命线多一条痕。"""
        t = "a" * 32
        base = [
            _ev(7, "10:00:00", "QUEUED", t, kind="media", label="x.mp4"),
            _ev(7, "10:00:01", "RUNNING", t),
            _ev(7, "10:00:02", "SUCCESS", t, bytes=1000),
        ]
        with_auto = base + [_ev(7, "09:00:00", "AUTO_REPLAY", t,
                                label="x.mp4", attempts=3)]
        self.assertEqual(self._rebuild(with_auto), self._rebuild(base))

    def test_retry_three_times_then_success(self):
        t = "a" * 32
        events = [
            _ev(7, "10:00:00", "RECEIVED", t, src="me"),
            _ev(7, "10:00:01", "QUEUED", t, kind="media", label="x.mp4"),
            _ev(7, "10:00:02", "RUNNING", t),
            _ev(7, "10:00:03", "RETRY", t, attempts=1),
            _ev(7, "10:00:03", "FAILED", t),
            _ev(7, "10:01:00", "RUNNING", t),
            _ev(7, "10:01:01", "RETRY", t, attempts=2),
            _ev(7, "10:01:01", "FAILED", t),
            _ev(7, "10:02:00", "RUNNING", t),
            _ev(7, "10:02:01", "RETRY", t, attempts=3),
            _ev(7, "10:02:01", "FAILED", t),
            _ev(7, "10:03:00", "RUNNING", t),
            _ev(7, "10:03:01", "SUCCESS", t, bytes=1000),
        ]
        s = self._rebuild(events)
        self.assertEqual(s["received"], 1)
        self.assertEqual(s["queued_new"], 1)
        self.assertEqual(s["success"], 1)          # 一个 task 只算一次成功
        self.assertEqual(s["failed_final"], 0)     # 最终成功了就不算失败
        self.assertEqual(s["retries"], 3)          # 失败尝试单独计次
        self.assertEqual(s["success_bytes"], 1000)

    def test_fail_twice_parks_as_final_fail(self):
        t = "b" * 32
        events = [
            _ev(7, "10:00:00", "RECEIVED", t),
            _ev(7, "10:00:01", "QUEUED", t, kind="media"),
            _ev(7, "10:00:02", "RUNNING", t),
            _ev(7, "10:00:03", "RETRY", t, attempts=1),
            _ev(7, "10:00:03", "FAILED", t),
            _ev(7, "10:01:00", "RUNNING", t),      # 手动重放
            _ev(7, "10:01:03", "RETRY", t, attempts=2),
            _ev(7, "10:01:03", "FAILED", t),       # 停在 retry 列表
        ]
        s = self._rebuild(events)
        self.assertEqual(s["queued_new"], 1)
        self.assertEqual(s["success"], 0)
        self.assertEqual(s["failed_final"], 1)     # 一个任务最终失败一次
        self.assertEqual(s["retries"], 2)
        self.assertEqual(s["active"], 0)           # FAILED 是终态（暂时的）

    def test_removed_manual_source_deleted_and_cancelled(self):
        t1, t2, t3 = "c" * 32, "d" * 32, "e" * 32
        events = [
            _ev(7, "10:00:00", "QUEUED", t1, kind="media"),
            _ev(7, "10:00:01", "RUNNING", t1),
            _ev(7, "10:00:02", "REMOVED", t1, why="manual"),
            _ev(7, "10:01:00", "QUEUED", t2, kind="media"),
            _ev(7, "10:01:01", "RUNNING", t2),
            _ev(7, "10:01:02", "REMOVED", t2, why="source_deleted"),
            _ev(7, "10:02:00", "QUEUED", t3, kind="media"),
            _ev(7, "10:02:01", "RUNNING", t3),
            _ev(7, "10:02:02", "CANCELLED", t3),
        ]
        s = self._rebuild(events)
        self.assertEqual(s["queued_new"], 3)
        self.assertEqual(s["removed"], 3)          # 移除桶含取消
        self.assertEqual(s["removed_manual"], 1)
        self.assertEqual(s["removed_source"], 1)
        self.assertEqual(s["cancelled"], 1)
        self.assertEqual(s["active"], 0)

    def test_dedup_skip_produces_no_task(self):
        events = [
            _ev(7, "10:00:00", "DEDUP_SKIPPED"),
            _ev(7, "10:00:01", "DEDUP_SKIPPED"),
            _ev(7, "10:00:02", "RECEIVED", "f" * 32),
            _ev(7, "10:00:03", "QUEUED", "f" * 32, kind="media"),
        ]
        s = self._rebuild(events)
        self.assertEqual(s["dedup_skipped"], 2)    # 收到但未成任务
        self.assertEqual(s["queued_new"], 1)
        self.assertEqual(s["task_total"], 1)       # 任务集只有一个

    def test_content_hit_is_its_own_terminal(self):
        t = "1" * 32
        events = [
            _ev(7, "10:00:00", "QUEUED", t, kind="media"),
            _ev(7, "10:00:01", "RUNNING", t),
            _ev(7, "10:00:02", "DEDUP_HIT", t),
        ]
        s = self._rebuild(events)
        self.assertEqual(s["dedup_hit"], 1)
        self.assertEqual(s["success"], 0)          # 没落盘不算成功
        self.assertEqual(s["failed_final"], 0)

    def test_cross_day_task(self):
        t = "2" * 32
        events = [
            _ev(6, "23:59:00", "RECEIVED", t),
            _ev(6, "23:59:01", "QUEUED", t, kind="media"),
            _ev(7, "00:00:30", "RUNNING", t),
            _ev(7, "00:01:00", "SUCCESS", t, bytes=555),
        ]
        today_only = self._rebuild(events, days=1)
        self.assertEqual(today_only["queued_new"], 0)   # 新建在昨天
        self.assertEqual(today_only["success"], 1)      # 终态在今天
        self.assertEqual(today_only["success_bytes"], 555)
        two_days = self._rebuild(events, days=2)
        self.assertEqual(two_days["queued_new"], 1)
        self.assertEqual(two_days["success"], 1)
        self.assertEqual(two_days["task_total"], 1)     # 跨日仍是一个任务

    def test_bytes_accumulated_exactly(self):
        events = [
            _ev(7, "10:00:00", "QUEUED", "3" * 32, kind="media"),
            _ev(7, "10:00:01", "SUCCESS", "3" * 32, bytes=1234567891),
            _ev(7, "10:01:00", "QUEUED", "4" * 32, kind="url"),
            _ev(7, "10:01:01", "SUCCESS", "4" * 32, bytes=2),
        ]
        s = self._rebuild(events)
        self.assertEqual(s["success_bytes"], 1234567891 + 2)  # 无舍入

    def test_running_task_without_terminal_is_active(self):
        t = "5" * 32
        events = [
            _ev(7, "10:00:00", "QUEUED", t, kind="media"),
            _ev(7, "10:00:01", "RUNNING", t),
        ]
        s = self._rebuild(events)
        self.assertEqual(s["active"], 1)
        self.assertEqual(s["task_total"], 1)


class StatsTextEventModeTest(unittest.TestCase):
    """事件模式下 stats_text 的分节渲染（用户约定格式）。"""

    def setUp(self):
        self.ev_file = os.path.join(_TMP, "task_events_render.jsonl")
        if os.path.exists(self.ev_file):
            os.remove(self.ev_file)
        self.old = {"queue": state.QUEUE, "active": state.ACTIVE_DOWNLOADS}
        state.QUEUE = {"tasks": [], "retry": []}
        state.ACTIVE_DOWNLOADS = {}

    def tearDown(self):
        state.QUEUE = self.old["queue"]
        state.ACTIVE_DOWNLOADS = self.old["active"]
        if os.path.exists(self.ev_file):
            os.remove(self.ev_file)

    def _write_events(self, events):
        import json as _json
        with open(self.ev_file, "w", encoding="utf-8") as f:
            for e in events:
                f.write(_json.dumps(e, ensure_ascii=False) + "\n")

    def test_renders_task_lifecycle_sections(self):
        self._write_events([
            _ev(7, "10:00:00", "RECEIVED", "a" * 32, src="me"),
            _ev(7, "10:00:01", "DEDUP_SKIPPED"),
            _ev(7, "10:00:02", "QUEUED", "a" * 32, kind="media"),
            _ev(7, "10:00:03", "RUNNING", "a" * 32),
            _ev(7, "10:00:04", "RETRY", "a" * 32, attempts=1),
            _ev(7, "10:00:05", "FAILED", "a" * 32),
            _ev(7, "10:01:00", "RUNNING", "a" * 32),
            _ev(7, "10:01:01", "SUCCESS", "a" * 32, bytes=1234),
        ])
        text = stats.stats_text(
            1, today=TODAY, log_path=os.path.join(_TMP, "nope.log"),
            history_path=os.path.join(_TMP, "nope_history.txt"),
            events_path=self.ev_file,
        )
        self.assertIn("📥 输入事件", text)
        self.assertIn("收到媒体：1 条", text)
        self.assertIn("去重跳过：1 条", text)
        self.assertIn("📦 下载任务", text)
        self.assertIn("新建任务：1", text)
        self.assertIn("成功任务：1", text)
        self.assertIn("最终失败任务：0", text)
        self.assertIn("移除任务：0", text)
        self.assertIn("🔄 执行情况", text)
        self.assertIn("重试次数：1", text)
        self.assertIn("⏳ 当前存量", text)
        self.assertIn("下载中 0", text)
        self.assertIn("💾 成功容量", text)
        self.assertIn("1234 bytes", text)          # 精确 bytes 展示
        self.assertIn("🧮 对账", text)
        self.assertIn("✓", text)                   # 严格分区恒等

    def test_legacy_fallback_when_no_events_in_window(self):
        """窗口内无事件（功能上线前的老日子）→ 回落关键词口径并注明。"""
        log_path, history_path = _write(None)
        text = stats.stats_text(
            1, today=TODAY, log_path=log_path,
            history_path=history_path,
            events_path=self.ev_file,              # 空事件文件
        )
        self.assertIn("关键词估算", text)
        self.assertIn("媒体：2 条", text)           # 旧口径内容仍在


class EventLogTest(unittest.TestCase):
    """任务事件日志（task_events.jsonl）：JSONL append-only 单行追加，
    与 dedup 索引/history 同款纪律——写失败仅告警、绝不影响下载。"""

    def setUp(self):
        self.ev_file = os.path.join(_TMP, "task_events_test.jsonl")
        if os.path.exists(self.ev_file):
            os.remove(self.ev_file)

    def tearDown(self):
        if os.path.exists(self.ev_file):
            os.remove(self.ev_file)

    def test_emit_appends_readable_json_line(self):
        with mock.patch.object(stats, "TASK_EVENTS_FILE", self.ev_file):
            stats.emit_event("QUEUED", task_id="a" * 32, label="视频.mp4")
        events = stats.load_events(self.ev_file)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["ev"], "QUEUED")
        self.assertEqual(events[0]["id"], "a" * 32)
        self.assertEqual(events[0]["label"], "视频.mp4")
        self.assertEqual(events[0]["ts"][:2], "20")  # 有时间戳

    def test_emit_extra_fields_and_label_sanitized(self):
        with mock.patch.object(stats, "TASK_EVENTS_FILE", self.ev_file):
            stats.emit_event("SUCCESS", task_id="b", bytes=123,
                             label="a\tb\nc")
        ev = stats.load_events(self.ev_file)[0]
        self.assertEqual(ev["bytes"], 123)
        self.assertEqual(ev["label"], "a b c")  # 制表/换行压空格防拆行

    def test_emit_failure_never_raises(self):
        # 目标路径是目录 → open 必炸；emit 必须吞掉（台账绝不影响下载）
        with mock.patch.object(stats, "TASK_EVENTS_FILE", _TMP):
            stats.emit_event("QUEUED", task_id="x")  # 不应抛

    def test_load_missing_or_bad_lines(self):
        self.assertEqual(stats.load_events(self.ev_file), [])
        with open(self.ev_file, "w", encoding="utf-8") as f:
            f.write("not-json\n")
            f.write('{"ev": "QUEUED", "ts": "2026-09-09 10:00:00"}\n')
            f.write("[1, 2]\n")  # JSON 但不是 dict
            f.write("\n")
        events = stats.load_events(self.ev_file)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["ev"], "QUEUED")

    def test_trim_keeps_tail_atomically(self):
        with mock.patch.object(stats, "TASK_EVENTS_FILE", self.ev_file):
            for i in range(5):
                stats.emit_event("QUEUED", task_id=str(i))
            stats.trim_event_file(self.ev_file, max_events=3)
        events = stats.load_events(self.ev_file)
        self.assertEqual([e["id"] for e in events], ["2", "3", "4"])
        self.assertFalse(os.path.exists(self.ev_file + ".tmp"))


if __name__ == "__main__":
    unittest.main()
