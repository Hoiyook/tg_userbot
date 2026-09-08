"""台账/对账（stats.py）的单元测试：日志+历史文件的窗口统计与文本渲染。

stats 的契约：查询时解析 download.log（7 天轮转）与 download_history.txt，
不新增任何持久化；日期窗口以「注入的 today」为基准（测试可定死日期），
days=1 只看 today 当天，days=N 看 today 起往前的 N 个自然日。

运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import os
import tempfile
import unittest
from datetime import date

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_stats_test_")
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


if __name__ == "__main__":
    unittest.main()
