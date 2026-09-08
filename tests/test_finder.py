"""媒体下落查询（finder.py）的单元测试：/find 关键字 → 命中各终态。

契约：/find <关键字> 在三个数据源里做忽略大小写的完整字段子串搜索——
① 当前队列（tasks/retry 的 final_name/label）；② download_history.txt
全量（已下载）；③ download.log 近 7 天窗口（含轮转；按 [T=] 聚合成任务，
无 trace 的行独立成事件）。回复给每条命中一个终态（待处理/待重试/已下载/
去重跳过/已移除/原消息删除/中断），统一前缀「🔍 查询」进自动清理白名单。

路径/基准日/队列可注入（纯函数）；日志/历史文件写临时目录。
"""
import os
import tempfile
import unittest
from datetime import date

_TMP = tempfile.mkdtemp(prefix="tg_userbot_finder_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import finder  # noqa: E402

TODAY = date(2026, 9, 8)
YESTERDAY = date(2026, 9, 7)
KW = "bl44"

LOG_LINES = [
    # 今天：收到事件（📦 无 trace，文件名带关键字变体「专属44」不带「bl44」）
    f"{TODAY} 01:26:22 | INFO | 📨 Saved Messages 收到消息 | ID=28291 "
    "| media=Video | grouped=1 | file=bl专属44a.mp4 | has_document=yes",
    f"{TODAY} 01:26:22 | INFO | 📦 检测到可下载媒体 | 类型=Video "
    "| 文件=bl专属44a.mp4",
    # 今天：trace 任务——header 带完整 caption（含「bl44专属」），失败到上限
    f"{TODAY} 01:26:24 | INFO | [T=b44aaa01] ▶️ 队列任务开始：bl专属44a.mp4",
    f"{TODAY} 01:26:25 | INFO | [T=b44aaa01] 最终文件名：24-04-02 作者：#腿玩年"
    "_期数：bl44专属a+b+c_角色：#弱音 - bl专属44a.mp4",
    f"{TODAY} 01:27:00 | ERROR | [T=b44aaa01] ❌ 已达到最大重试次数，下载失败",
    # 今天：成功任务（✅——历史里也有同一条，trace 组应让位于队列/历史不重复）
    f"{TODAY} 02:00:00 | INFO | [T=b44bbb02] ▶️ 队列任务开始：bl44ok.mp4",
    f"{TODAY} 02:01:00 | INFO | [T=b44bbb02] ✅ 下载完成",
    # 今天：去重跳过（⏭️ 无 trace）
    f"{TODAY} 03:00:00 | INFO | ⏭️ 重复媒体跳过入队（消息 28301）"
    " bl44专属a+b+c",
    # 今天：手动移除（🗑 无 trace）
    f"{TODAY} 03:30:00 | INFO | 🗑 手动移除队列任务（在途已取消）：bl44gone.mp4",
    # 今天：与关键字无关的行
    f"{TODAY} 04:00:00 | INFO | 📦 检测到可下载媒体 | 类型=Photo | 文件=猫.jpg",
    # 今天：trace id8 与队列 retry 记录相同（id="b44retry"+"1"*24）——
    # 该组应被队列条目吸收，不再单列
    f"{TODAY} 05:00:00 | INFO | [T=b44retry] ▶️ 队列任务开始：LOGDUP01.mp4",
    f"{TODAY} 05:00:30 | ERROR | [T=b44retry] ❌ 已达到最大重试次数，下载失败",
    # 昨天：命中（只应进 days>=2 的窗口；finder 恒查满窗口）
    f"{YESTERDAY} 10:00:00 | INFO | [T=b44ccc03] ▶️ 队列任务开始：bl44old.mp4",
    f"{YESTERDAY} 10:05:00 | ERROR | [T=b44ccc03] ❌ 已达到最大重试次数，"
    "下载失败",
]

HISTORY_LINES = [
    f"{TODAY} 02:01:05 | 普通 | 24-04-02 期数：bl44专属ok.mp4 | 1.00 GB "
    "| 来源：祂录（3D区）",
    f"{YESTERDAY} 23:00:00 | 普通 | 无关文件.mp4 | 1.00 KB | 来源：旧",
]

QUEUE = {
    "tasks": [
        {"id": "b44task01" + "0" * 24, "kind": "media",
         "final_name": "24-04-02 期数：bl44专属pending.mp4",
         "label": "bl44pending.mp4"},
    ],
    "retry": [
        {"id": "b44retry" + "1" * 24, "kind": "media",
         "final_name": "24-04-02 作者：#腿玩年_期数：bl44专属a+b+c"
         "（2024.02.16_23_29）_角色：#弱音_文件大小：1191M；1158M；1201M"
         "_i站视频预览地址_专属a【 https___www.iwara.tv_video_x 】"
         " - bl专属44a.mp4",
         "label": "bl专属44a.mp4", "attempts": 3},
    ],
}


def _write():
    log_path = os.path.join(_TMP, "finder.log")
    history_path = os.path.join(_TMP, "finder_history.txt")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(LOG_LINES) + "\n")
    with open(history_path, "w", encoding="utf-8") as f:
        f.write("\n".join(HISTORY_LINES) + "\n")
    return log_path, history_path


class IsFindCommandTest(unittest.TestCase):
    """is_find_command：/find、/find 关键字 算；其它不算。"""

    def test_matches(self):
        self.assertTrue(finder.is_find_command("/find"))
        self.assertTrue(finder.is_find_command("/find bl44"))
        self.assertTrue(finder.is_find_command("  /FIND abc 7 "))

    def test_rejects(self):
        self.assertFalse(finder.is_find_command("/findx"))
        self.assertFalse(finder.is_find_command("/done bl44"))
        self.assertFalse(finder.is_find_command("查一下 bl44"))


class FindMediaTest(unittest.TestCase):
    """find_media：三个数据源汇聚成一份带终态的回复。"""

    def setUp(self):
        self.log_path, self.history_path = _write()
        self.text = finder.find_media(
            KW, today=TODAY, log_path=self.log_path,
            history_path=self.history_path, queue=QUEUE,
        )

    def test_prefix_and_count(self):
        # 统一前缀（自动清理白名单）+ 头部带关键字与命中总数
        self.assertIn(finder.FIND_TEXT_PREFIX, self.text)
        self.assertIn(KW, self.text)
        self.assertIn("匹配", self.text)

    def test_queue_hits(self):
        # 待处理 1 + 待重试 1（带失败次数）
        self.assertIn("⏳ 待处理", self.text)
        self.assertIn("🔁 待重试", self.text)
        self.assertIn("失败 3 次", self.text)

    def test_long_name_keeps_match_context_and_tail(self):
        # 长名截断时：关键字上下文在头部、区分性真文件名在尾部，两头都要保
        # （只断言队列那条：日志 trace 条目名字短、天然完整，断言会被它满足）
        line = next(l for l in self.text.splitlines() if "🔁 待重试" in l)
        self.assertIn("期数：bl44专属a+b+c", line)
        self.assertIn("- bl专属44a.mp4", line)

    def test_history_hit(self):
        # 已下载（历史文件，含大小）
        self.assertIn("✅ 已下载", self.text)
        self.assertIn("1.00 GB", self.text)

    def test_log_events(self):
        # 无 trace 事件：去重跳过 / 手动移除
        self.assertIn("⏭️ 去重跳过", self.text)
        self.assertIn("🗑 已移除", self.text)
        # 失败 trace 组（昨天的，满窗口可查），终态判定正确
        self.assertIn("b44ccc03", self.text)
        self.assertIn("失败进待重试", self.text)
        # 成功 trace 组不单列（历史里已有同一份，避免双计）
        self.assertNotIn("b44bbb02", self.text)
        # trace id8 与队列记录相同 → 被队列条目吸收，不重复列
        self.assertNotIn("LOGDUP01", self.text)

    def test_case_insensitive(self):
        text = finder.find_media(
            "BL44", today=TODAY, log_path=self.log_path,
            history_path=self.history_path, queue=QUEUE,
        )
        self.assertIn("✅ 已下载", text)

    def test_variant_filename_only_matches_untraced_received(self):
        # 「专属44」是文件名里的变体写法：📦 收到事件应命中
        text = finder.find_media(
            "专属44", today=TODAY, log_path=self.log_path,
            history_path=self.history_path, queue=QUEUE,
        )
        self.assertIn("📨 收到", text)

    def test_no_match(self):
        text = finder.find_media(
            "不存在的关键字xyz", today=TODAY, log_path=self.log_path,
            history_path=self.history_path, queue=QUEUE,
        )
        self.assertIn("无匹配", text)

    def test_short_keyword_hint(self):
        # 2 字符以下给用法提示
        self.assertIn("用法", finder.find_media(
            "b", today=TODAY, log_path=self.log_path,
            history_path=self.history_path, queue=QUEUE,
        ))

    def test_limit_truncates(self):
        text = finder.find_media(
            KW, today=TODAY, log_path=self.log_path,
            history_path=self.history_path, queue=QUEUE, limit=2,
        )
        self.assertIn("仅显示前 2 条", text)


if __name__ == "__main__":
    unittest.main()
