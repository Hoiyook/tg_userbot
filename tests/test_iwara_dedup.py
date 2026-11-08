"""iwara URL 判重（2026-10-26 定档：成功过才拦截）的单元测试。"""
import atexit
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_iwara_dedup_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import chrome_client, iwara  # noqa: E402


def _req(task_id, url, **kw):
    return {"task_id": task_id, "url": url,
            "created_at": kw.pop("created_at", "2026-10-26 20:00:00"),
            **kw}


class MarkNotifiedTest(unittest.TestCase):
    """终态回写请求台账：outcome + filename（判重的持久层）。"""

    def test_outcome_and_filename_written(self):
        path = os.path.join(_TMP, "req1.json")
        chrome_client.save_requests(
            [_req("t1", "https://www.iwara.tv/video/abc")], path)
        chrome_client.mark_notified("t1", path=path, outcome="SUCCESS",
                                    filename="video.mp4")
        rec = chrome_client.get_request("t1", path)
        self.assertEqual(rec["outcome"], "SUCCESS")
        self.assertEqual(rec["filename"], "video.mp4")
        self.assertTrue(rec["notified_at"])
        # 不传 outcome 不覆盖已有值（幂等重放安全）
        chrome_client.mark_notified("t1", path=path)
        self.assertEqual(chrome_client.get_request("t1", path)["outcome"],
                         "SUCCESS")


class FindPreviousSuccessTest(unittest.TestCase):
    def setUp(self):
        self.rp = os.path.join(_TMP, "req2.json")
        self.tp = os.path.join(_TMP, "tasks.json")
        with open(self.tp, "w", encoding="utf-8") as f:
            json.dump({"tasks": []}, f)

    def _seed(self, reqs, tasks=None):
        chrome_client.save_requests(reqs, self.rp)
        with open(self.tp, "w", encoding="utf-8") as f:
            json.dump({"tasks": tasks or []}, f)

    def test_new_persistent_success_blocks(self):
        self._seed([_req("t1", "https://www.iwara.tv/video/abc",
                         outcome="SUCCESS", filename="a.mp4",
                         notified_at="2026-10-26 20:01:00")])
        hit = iwara.find_previous_success("abc", self.rp, self.tp)
        self.assertIsNotNone(hit)
        self.assertIn("a.mp4", iwara.duplicate_text(hit))
        self.assertIn("拦截", iwara.duplicate_text(hit))

    def test_url_variant_same_video_id(self):
        self._seed([_req("t1", "https://iwara.tv/video/abc",
                         outcome="SUCCESS")])
        self.assertIsNotNone(
            iwara.find_previous_success("abc", self.rp, self.tp))

    def test_failed_never_blocks(self):
        self._seed([_req("t1", "https://www.iwara.tv/video/abc",
                         outcome="FAILED", notified_at="2026-10-26 20:01"),
                    _req("t2", "https://www.iwara.tv/video/abc",
                         outcome="CANCELLED")])
        self.assertIsNone(
            iwara.find_previous_success("abc", self.rp, self.tp))

    def test_legacy_success_via_task_window_fallback(self):
        # 上线前的老请求（无 outcome），任务窗口内 status=SUCCESS 兜底
        self._seed(
            [_req("t9", "https://www.iwara.tv/video/old",
                  notified_at="2026-09-01 10:00:00")],
            tasks=[{"task_id": "t9", "status": "SUCCESS",
                    "filename": "old.mp4"}])
        hit = iwara.find_previous_success("old", self.rp, self.tp)
        self.assertIsNotNone(hit)

    def test_legacy_unknown_out_window_does_not_block(self):
        # 无 outcome 且任务已出窗（无法证明成功）→ 不拦（宁可重下不误拦）
        self._seed([_req("t9", "https://www.iwara.tv/video/old",
                         notified_at="2026-09-01 10:00:00")],
                   tasks=[])
        self.assertIsNone(
            iwara.find_previous_success("old", self.rp, self.tp))

    def test_different_video_not_blocked(self):
        self._seed([_req("t1", "https://www.iwara.tv/video/abc",
                         outcome="SUCCESS")])
        self.assertIsNone(
            iwara.find_previous_success("zzz", self.rp, self.tp))

    def test_missing_files_safe(self):
        self.assertIsNone(
            iwara.find_previous_success(
                "abc", os.path.join(_TMP, "nope1.json"),
                os.path.join(_TMP, "nope2.json")))


if __name__ == "__main__":
    unittest.main()
