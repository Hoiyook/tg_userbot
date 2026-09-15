"""评论来源解析的准确性保障（sources.py 2026-09-15 重构）的单元测试。

覆盖：
1. 取父消息失败（超时/None）→ 退避重试，恢复后成功拿到快照；
2. 重试耗尽 → 返回 None + **失败账本落盘** + 通知发出（用户要求：失败必须可见可溯源）；
3. 确证无关联（原消息不是评论）→ 返回 None 但**不记账**（那不是失败）；
4. 副本回源失败不再退回副本自身走 reply 链（旧行为会顺着转发批次的副本串扰
   耗尽跳数后静默降级——2026-09-15 事故路径）；
5. /origin 视图渲染与损坏行容错。

不联网：fetcher/namer 全注入替身；重试延迟 patch asyncio.sleep。
运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import asyncio
import atexit
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_origin_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import sources  # noqa: E402


def _copy_message(msg_id=42116, text="掉装备/不掉装备", saved_peer=-1002143122455,
                  saved_msg_id=642375, reply_to=None, fwd=None):
    """最小消息替身：形状对齐 sources.walk 用到的字段。"""
    m = mock.MagicMock()
    m.id = msg_id
    m.chat_id = saved_peer if fwd else 5452449426   # 副本在收藏夹
    m.message = text
    m.fwd_from = fwd
    m.reply_to = reply_to
    file = mock.MagicMock()
    file.name = f"【04-NSFW】N{msg_id}.mp4"
    m.file = file
    return m


def _fwd(from_peer=-1002143122455, saved_peer=-1002143122455, saved_msg_id=642375,
         channel_post=None):
    f = mock.MagicMock()
    f.from_id = from_peer
    f.channel_post = channel_post
    f.saved_from_peer = saved_peer
    f.saved_from_msg_id = saved_msg_id
    f.date = None
    f.from_name = None
    return f


class _Base(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # 账本文件指到本用例临时目录
        self.dir = tempfile.mkdtemp(prefix="origin_", dir=_TMP)
        self._p = mock.patch.object(
            config, "ORIGIN_FAILURES_FILE", os.path.join(self.dir, "of.jsonl"))
        self._p.start()
        self.addCleanup(self._p.stop)
        # 重试退避立即返回（不 patch asyncio.sleep 全局——续租循环等不在此路径）
        self._sleeps = []

        async def fake_sleep(sec):
            self._sleeps.append(sec)

        self._s = mock.patch.object(sources.asyncio, "sleep", side_effect=fake_sleep)
        self._s.start()
        self.addCleanup(self._s.stop)
        # 通知捕获
        self._notifies = []

        async def fake_notify(text):
            self._notifies.append(text)

        self._n = mock.patch("tg_userbot.notify.notify_user", side_effect=fake_notify)
        self._n.start()
        self.addCleanup(self._n.stop)

    def _mirror(self, channel_peer=-1001719225045, post=85397, text="作者：#77T 期数：2026.08.02"):
        """镜像帖替身：fwd 带 channel_post；namer 会拿到它。"""
        m = mock.MagicMock()
        m.id = 642363
        m.chat_id = -1002143122455
        m.message = text
        m.fwd_from = _fwd(from_peer=channel_peer, saved_msg_id=post,
                          channel_post=post)
        m.reply_to = None
        return m


class RetryUntilSuccessTest(_Base):
    """前 N 次取消息失败（None）→ 重试 → 恢复后成功。"""

    async def test_retry_then_success(self):
        copy = _copy_message(fwd=_fwd())
        mirror = self._mirror()
        calls = {"n": 0}

        async def fetch(peer, msg_id):
            calls["n"] += 1
            if calls["n"] <= 2:      # 回源 + 第一跳都失败一次
                return None
            if msg_id == 642375:
                return _copy_message(msg_id=642375, saved_peer=-1002143122455,
                                     reply_to=mock.MagicMock(
                                         reply_to_msg_id=642363))
            if msg_id == 642363:
                return mirror
            return None

        async def namer(m):
            return "祂录（3D区）"

        snap = await sources.resolve_origin_snapshot(copy, fetcher=fetch, namer=namer)
        self.assertIsNotNone(snap)
        self.assertEqual(snap["source_name"], "祂录（3D区）")
        self.assertEqual(snap["channel_post"], 85397)
        self.assertGreaterEqual(calls["n"], 3)
        # 有重试间隔
        self.assertTrue(any(s == config.ORIGIN_RETRY_DELAY_SECONDS for s in self._sleeps))
        # 成功不记账、不通知
        self.assertEqual(sources.read_origin_failures(), [])
        self.assertEqual(self._notifies, [])


class ExhaustedRetryTest(_Base):
    """重试耗尽 → None + 账本 + 通知（数据准确性要求：失败必须可见可溯源）。"""

    async def test_exhausted_records_and_notifies(self):
        copy = _copy_message(fwd=_fwd())

        async def fetch(peer, msg_id):
            return None     # 回源永远失败（模拟限流超时被收口成 None）

        snap = await sources.resolve_origin_snapshot(copy, fetcher=fetch)
        self.assertIsNone(snap)
        # 账本落盘且字段齐全
        rows = sources.read_origin_failures()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["saved_msg_id"], 42116)
        self.assertEqual(r["saved_from_msg_id"], 642375)
        self.assertIn("回源取原消息失败", r["reason"])
        self.assertEqual(r["attempts"], config.ORIGIN_RETRY_ATTEMPTS)
        # 通知发出且带溯源指引
        self.assertEqual(len(self._notifies), 1)
        self.assertIn("来源解析失败", self._notifies[0])
        self.assertIn("/origin", self._notifies[0])
        # 重试次数-1 个间隔（3 次尝试 2 个间隔）
        self.assertEqual(len(self._sleeps), config.ORIGIN_RETRY_ATTEMPTS - 1)


class DefinitiveNoLinkTest(_Base):
    """确证无关联：原消息不是评论 → None 但不记账不重试。"""

    async def test_no_reply_no_ledger(self):
        copy = _copy_message(fwd=_fwd())

        async def fetch(peer, msg_id):
            # 回源成功，但群里的原消息不是评论（无 reply_to、无 channel_post）
            return _copy_message(msg_id=642375, saved_peer=-1002143122455,
                                 reply_to=None)

        calls = {"n": 0}

        async def counting_fetch(peer, msg_id):
            calls["n"] += 1
            return await fetch(peer, msg_id)

        snap = await sources.resolve_origin_snapshot(copy, fetcher=counting_fetch)
        self.assertIsNone(snap)
        self.assertEqual(calls["n"], 1)          # 没有重试
        self.assertEqual(sources.read_origin_failures(), [])  # 没有记账
        self.assertEqual(self._notifies, [])


class NoCopyChainWalkTest(_Base):
    """副本回源失败时不许再顺着副本自身的 reply_to 走（旧事故路径）。"""

    async def test_copy_reply_to_not_followed_on_fetch_failure(self):
        # 副本带 reply_to（指向另一条收藏夹副本 42104）——旧版会在回源失败后
        # 顺着它走进收藏夹副本链并耗尽跳数；新版应直接判可重试失败。
        rt = mock.MagicMock()
        rt.reply_to_msg_id = 42104
        rt.reply_to_peer_id = None
        copy = _copy_message(fwd=_fwd(), reply_to=rt)

        async def fetch(peer, msg_id):
            return None

        snap = await sources.resolve_origin_snapshot(copy, fetcher=fetch)
        self.assertIsNone(snap)
        rows = sources.read_origin_failures()
        self.assertEqual(len(rows), 1)
        # 失败原因是「回源失败」，而不是走进副本链后的跳数耗尽
        self.assertIn("回源取原消息失败", rows[0]["reason"])


class LedgerRenderTest(_Base):

    def test_render_and_corrupt_line_tolerance(self):
        entry = {
            "ts": "2026-09-15 09:02:28", "epoch": 1789434148,
            "saved_chat": "5452449426", "saved_msg_id": 42116,
            "filename": "【04-NSFW】N60731.mp4", "text": "掉装备/不掉装备",
            "from_peer": "-1002143122455", "saved_from_peer": "-1002143122455",
            "saved_from_msg_id": 642375, "reason": "回源取原消息失败",
            "attempts": 3,
        }
        with open(config.ORIGIN_FAILURES_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.write("not-json-garbage\n")            # 损坏行要被跳过
        rows = sources.read_origin_failures()
        self.assertEqual(len(rows), 1)
        text = sources.origin_failures_text()
        self.assertIn("N60731", text)
        self.assertIn("回源取原消息失败", text)
        self.assertIn("642375", text)

    def test_render_empty(self):
        self.assertIn("没有来源解析失败记录", sources.origin_failures_text())


if __name__ == "__main__":
    unittest.main()
