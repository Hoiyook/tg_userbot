"""UX Round 2 三件套的单元测试（2026-09-24）：

1. Pawchive Cookie 校验（validate_cookie_blocking / cookie_check_cached /
   /paw status 的 Cookie 行 / 🧪 按钮 handler）——失效与网络失败必须区分，
   网络抖动不得把会话误判成失效。
2. 失败帖画像（runtime_db.classify_pawchive_failed）——与
   retry_pawchive_posts 的「可恢复」判定同源：画像里的可重投数就是
   /paw retry all 会真正动的数量。
3. 115 备份对账（cd2.reconcile_local_backup / reconcile_text）——
   虚拟/本地路径互转、滞留×备份日志交叉、报告渲染。

不联网：HTTP 全部 monkeypatch；DB 落进程级临时目录。
"""
import asyncio
import atexit
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_ux2_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import cd2, config, pawchive, runtime_db, state  # noqa: E402


# ============================================================
# 1) Cookie 校验
# ============================================================
class _CookieBase(unittest.TestCase):
    def setUp(self):
        self._p = mock.patch.object(
            config, "PAWCHIVE_COOKIE", "sessionid=ABC")
        self._p.start()
        self.addCleanup(self._p.stop)
        saved = dict(state.PAW_COOKIE_CHECK)
        self.addCleanup(state.PAW_COOKIE_CHECK.update, saved)
        # status_text 依赖 runtime DB 就绪（计数字段），给独立临时库
        self.db_dir = tempfile.mkdtemp(prefix="ux2_ckdb_", dir=_TMP)
        self._db = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.db_dir, "db.sqlite"))
        self._db.start()
        self.addCleanup(self._db.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)

    def _run(self, coro):
        return self.loop.run_until_complete(coro)


class CookieValidateTest(_CookieBase):
    """validate_cookie_blocking：结论口径（有效 / 已失效 / 校验失败）。"""

    def _patch_http(self, fn):
        return mock.patch.object(pawchive, "_http_get_json", fn)

    def test_no_cookie(self):
        with mock.patch.object(config, "PAWCHIVE_COOKIE", ""):
            ok, detail = pawchive.validate_cookie_blocking()
        self.assertFalse(ok)
        self.assertIn("未配置", detail)

    def test_valid_empty_favs(self):
        with self._patch_http(lambda url, **kw: []):
            ok, detail = pawchive.validate_cookie_blocking()
        self.assertTrue(ok)
        self.assertIn("收藏 0 条", detail)

    def test_valid_with_favs(self):
        with self._patch_http(lambda url, **kw: [{"id": 1}, {"id": 2}]):
            ok, detail = pawchive.validate_cookie_blocking()
        self.assertTrue(ok)
        self.assertIn("收藏 2 条", detail)

    def test_http_401_is_invalid(self):
        def raise_401(url, **kw):
            raise RuntimeError(f"HTTP 401: {url}")
        with self._patch_http(raise_401):
            ok, detail = pawchive.validate_cookie_blocking()
        self.assertFalse(ok)
        self.assertIn("已失效", detail)

    def test_login_wall_html_is_invalid(self):
        """登录墙返回 HTML → json.loads 抛 JSONDecodeError（ValueError）。"""
        def html_wall(url, **kw):
            raise json.JSONDecodeError("Expecting value", "<html>", 0)
        with self._patch_http(html_wall):
            ok, detail = pawchive.validate_cookie_blocking()
        self.assertFalse(ok)
        self.assertIn("已失效", detail)

    def test_network_error_not_invalid(self):
        """网络抖动 ≠ Cookie 失效：detail 必须落在「校验失败」口径。"""
        def flaky(url, **kw):
            raise RuntimeError("重试 2 次仍失败：url（TimeoutError）")
        with self._patch_http(flaky):
            ok, detail = pawchive.validate_cookie_blocking()
        self.assertFalse(ok)
        self.assertIn("校验失败", detail)
        self.assertNotIn("已失效", detail)


class CookieCacheTest(_CookieBase):
    """cookie_check_cached：TTL 内复用结论，force 绕过。"""

    def test_ttl_reuse_and_force(self):
        calls = []

        def fake_validate():
            calls.append(1)
            return True, "有效（收藏 0 条）"

        with mock.patch.object(pawchive, "validate_cookie_blocking",
                               fake_validate):
            state.PAW_COOKIE_CHECK.update({"ts": 0.0, "ok": None,
                                           "detail": ""})
            self._run(pawchive.cookie_check_cached(force=True))
            # 刚校验过（ts=now）→ TTL 内直接回缓存
            self._run(pawchive.cookie_check_cached())
            self.assertEqual(len(calls), 1)
            # ts 推回 20 分钟前 → 过期；force 也应现打
            state.PAW_COOKIE_CHECK["ts"] = time.monotonic() - 1200
            self._run(pawchive.cookie_check_cached())
            self._run(pawchive.cookie_check_cached(force=True))
            self.assertEqual(len(calls), 3)

    def test_no_cache_when_never_checked(self):
        calls = []

        def fake_validate():
            calls.append(1)
            return False, "已失效（HTTP 401）"

        with mock.patch.object(pawchive, "validate_cookie_blocking",
                               fake_validate):
            state.PAW_COOKIE_CHECK.update({"ts": 0.0, "ok": None,
                                           "detail": ""})
            self._run(pawchive.cookie_check_cached())
            self.assertEqual(len(calls), 1)
            self.assertEqual(state.PAW_COOKIE_CHECK["detail"],
                             "已失效（HTTP 401）")


class StatusTextCookieLineTest(_CookieBase):
    """/paw status 的 Cookie 行：只读缓存展示，绝不触发网络。"""

    def test_shows_cached_result(self):
        state.PAW_COOKIE_CHECK.update(
            {"ts": time.monotonic(), "ok": False,
             "detail": "已失效（HTTP 401）"})
        text = pawchive.status_text()
        self.assertIn("⚠️ Cookie：已失效（HTTP 401）", text)

    def test_shows_unchecked_hint(self):
        state.PAW_COOKIE_CHECK.update({"ts": 0.0, "ok": None, "detail": ""})
        text = pawchive.status_text()
        self.assertIn("🧪 校验", text)

    def test_hidden_when_no_cookie(self):
        state.PAW_COOKIE_CHECK.update(
            {"ts": time.monotonic(), "ok": True, "detail": "有效"})
        with mock.patch.object(config, "PAWCHIVE_COOKIE", ""):
            text = pawchive.status_text()
        self.assertNotIn("Cookie：", text)


class CookieCheckButtonTest(_CookieBase):
    """🧪 校验按钮的 bot handler 接线。"""

    def test_action_handled_and_registered(self):
        self.assertIn("paw_cookie_check", config.MENU_ACTIONS)

    def test_handler_replies_with_result(self):
        import asyncio
        from tg_userbot import bot

        async def run():
            with mock.patch.object(
                    pawchive, "cookie_check_cached",
                    mock.AsyncMock(return_value=(False, "已失效（HTTP 401）"))):
                reply, buttons = await bot.handle_menu_action(
                    "paw_cookie_check", None, mock.MagicMock())
            return reply, buttons

        reply, buttons = asyncio.new_event_loop().run_until_complete(run())
        self.assertIn("已失效（HTTP 401）", reply)
        self.assertIn("更新", reply)
        self.assertTrue(buttons)


# ============================================================
# 2) 失败帖画像
# ============================================================
class _PawDbCase(unittest.TestCase):
    """单用例独立 DB（沿用 test_pawchive_db 的模式）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ux2_pawdb_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())

    def _enqueue(self, post_id="111", n_files=2):
        return runtime_db.enqueue_pawchive_posts(
            "patreon", "1", "作者A",
            [{
                "post_id": post_id,
                "title": "t",
                "published": "2026-09-13T04:57:26",
                "post_url": f"https://x/post/{post_id}",
                "subdir": f"Pawchive/A/{post_id}",
                "files": [{"url": f"https://f/{post_id}_{i}.mp4",
                           "filename": f"{i}.mp4"}
                          for i in range(n_files)],
                "ext_links": [],
            }],
            scan_batch="test")

    def _fail_post_files(self, row_id, errors):
        """把帖子的文件按 errors 列表逐个标 FAILED（None 项保持 PENDING）。"""
        files = runtime_db.list_pawchive_files(row_id)
        for f, err in zip(files, errors):
            if err is None:
                continue
            runtime_db.mark_pawchive_file_failed(f["id"], error=err)


class ClassifyFailedTest(_PawDbCase):
    """classify_pawchive_failed 与 retry 判定同源。"""

    def test_dead_vs_recoverable(self):
        created = self._enqueue(post_id="A", n_files=2)
        self.assertEqual(created, (1, 0))
        post = runtime_db.claim_next_pawchive_post(now=1000)
        self._fail_post_files(post["id"], [
            runtime_db.PAW_DEAD_LINK_MARK + " HTTP 404"] * 2)
        runtime_db.finalize_pawchive_post(post["id"],
                                          runtime_db.PAW_POST_FAILED)
        # （直接造第二帖：不同 post_id 各占一行）
        # 直接造第二帖：不同 post_id
        runtime_db.enqueue_pawchive_posts(
            "patreon", "1", "作者A",
            [{"post_id": "B", "title": "t2",
              "published": "2026-09-14T04:57:26",
              "post_url": "https://x/post/B",
              "subdir": "Pawchive/A/B",
              "files": [{"url": "https://f/b.mp4", "filename": "b.mp4"}],
              "ext_links": []}],
            scan_batch="test")
        post2 = runtime_db.claim_next_pawchive_post(now=1001)
        self._fail_post_files(post2["id"], [
            "HTTP 429 too many requests", None])
        runtime_db.finalize_pawchive_post(post2["id"],
                                          runtime_db.PAW_POST_FAILED)

        recoverable, dead = runtime_db.classify_pawchive_failed()
        self.assertEqual((recoverable, dead), (1, 1))

        # 与 retry 的动作量一致：retry all 应重投 1 跳过 1
        requeued, skipped = runtime_db.retry_pawchive_posts()
        self.assertEqual((requeued, skipped), (1, 1))

    def test_status_text_has_profile_line(self):
        self._enqueue(post_id="C", n_files=1)
        post = runtime_db.claim_next_pawchive_post(now=1000)
        self._fail_post_files(post["id"], [
            runtime_db.PAW_DEAD_LINK_MARK + " HTTP 404"])
        runtime_db.finalize_pawchive_post(post["id"],
                                          runtime_db.PAW_POST_FAILED)
        text = pawchive.status_text()
        self.assertIn("失败帖画像", text)
        self.assertIn("♻️ 可重投 0 / 🗄 纯死链 1", text)


# ============================================================
# 3) 115 备份对账
# ============================================================
class _ReconCase(unittest.TestCase):
    """tmp 下载树 + tmp 备份日志目录。"""

    def setUp(self):
        self.dl = tempfile.mkdtemp(prefix="ux2_dl_", dir=_TMP)
        self.logs = tempfile.mkdtemp(prefix="ux2_logs_", dir=_TMP)
        self._p1 = mock.patch.object(config, "DOWNLOAD_DIR", self.dl)
        self._p2 = mock.patch.object(cd2, "cd2_log_dir", lambda: self.logs)
        self._p1.start()
        self._p2.start()
        self.addCleanup(self._p1.stop)
        self.addCleanup(self._p2.stop)
        # tmp 目录不在 /Volumes 数据卷上，真实卷名推导拿不到虚拟路径——
        # 打一个等价映射桩（dl 前缀 → /V1/downloads），互转纯函数另测。
        self._p3 = mock.patch.object(
            cd2, "_local_to_virtual",
            lambda p: ("/V1/downloads/" + os.path.relpath(p, self.dl))
            if p.startswith(self.dl) else None)
        self._p3.start()
        self.addCleanup(self._p3.stop)

    def _write_log(self, day_offset, virtual_paths):
        d = time.strftime("%Y-%m-%d", time.localtime(
            time.time() - day_offset * 86400))
        lines = []
        for vp in virtual_paths:
            lines.append(
                f"2026-09-20 10:00:00.000  INFO  cloudapi::backup_manager: "
                f"handle_localfs_notify: delete file and remove from all "
                f'dests "{vp}"')
        with open(os.path.join(self.logs, f"backup.{d}.log"),
                  "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


class ReconcilePathHelpersTest(unittest.TestCase):
    """虚拟/本地路径互转（纯函数，fake 卷名）。"""

    def test_roundtrip(self):
        with mock.patch.object(config, "DOWNLOAD_DIR", "/Volumes/V1/downloads"):
            self.assertEqual(cd2._volume_name(), "V1")
            self.assertEqual(
                cd2._local_to_virtual("/Volumes/V1/downloads/Pawchive/a.mp4"),
                "/V1/downloads/Pawchive/a.mp4")
            self.assertEqual(
                cd2._virtual_to_local("/V1/downloads/Pawchive/a.mp4"),
                "/Volumes/V1/downloads/Pawchive/a.mp4")
            self.assertIsNone(cd2._local_to_virtual("/Users/x/a.mp4"))
            self.assertIsNone(cd2._virtual_to_local("/OtherV1/downloads/a"))


class ReconcileScanTest(_ReconCase):
    """滞留判定 × 备份日志交叉。"""

    def _mk(self, rel, age_hours):
        path = os.path.join(self.dl, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"x" * 1024)
        ts = time.time() - age_hours * 3600
        os.utime(path, (ts, ts))
        return path

    def test_fresh_ignored_missed_and_reappeared(self):
        fresh = self._mk("Pawchive/A/new.mp4", age_hours=1)      # 太新：忽略
        missed = self._mk("Pawchive/A/lost.mp4", age_hours=48)   # 日志没有：漏备
        again = self._mk("Pawchive/B/again.zip", age_hours=48)   # 日志有：重现
        self._write_log(1, ["/V1/downloads/Pawchive/B/again.zip"])
        self._write_log(2, ["/V1/downloads/Pawchive/B/again.zip"])

        r = cd2.reconcile_local_backup()
        self.assertEqual(r["stale_n"], 2)
        self.assertEqual(r["stale_bytes"], 2048)
        self.assertEqual(r["missed_n"], 1)
        self.assertEqual(r["reappeared_n"], 1)
        self.assertTrue(r["log_available"])
        self.assertEqual(r["dirs"][0][0], "Pawchive/A")

    def test_non_media_and_unreadable_log(self):
        self._mk("Pawchive/A/note.txt", age_hours=48)             # 非媒体
        self._mk("Pawchive/A/big.iso", age_hours=48)              # 白名单外
        self._write_log(1, [])
        shutil.rmtree(self.logs)                                   # 日志没了
        r = cd2.reconcile_local_backup()
        self.assertEqual(r["stale_n"], 0)
        self.assertFalse(r["log_available"])

    def test_text_clean_and_dirty(self):
        self.assertIn("✅ 对账干净", cd2.reconcile_text())
        self._mk("Pawchive/A/lost.mp4", age_hours=48)
        text = cd2.reconcile_text()
        self.assertIn("本地滞留媒体：1 个", text)
        self.assertIn("漏备候选", text)
        self.assertIn("建议", text)


if __name__ == "__main__":
    unittest.main()


# ============================================================
# 4) 创作者缓存过期即时提示（2026-09-24 /paw plan 静默无反应修复）
# ============================================================
class CreatorsCacheStaleTest(unittest.TestCase):
    """creators_cache_stale：新鲜 False / 过期 True / 缺失 True。"""

    def setUp(self):
        self.cache = os.path.join(_TMP, f"creators_{id(self)}.json")

    def tearDown(self):
        if os.path.exists(self.cache):
            os.remove(self.cache)

    def test_fresh_cache_not_stale(self):
        with open(self.cache, "w") as f:
            f.write("[]")
        with mock.patch.object(pawchive, "_cache_path",
                               return_value=self.cache):
            self.assertFalse(pawchive.creators_cache_stale())

    def test_expired_and_missing_are_stale(self):
        with open(self.cache, "w") as f:
            f.write("[]")
        old = time.time() - config.PAWCHIVE_CREATORS_CACHE_TTL - 60
        os.utime(self.cache, (old, old))
        with mock.patch.object(pawchive, "_cache_path",
                               return_value=self.cache):
            self.assertTrue(pawchive.creators_cache_stale())
        with mock.patch.object(pawchive, "_cache_path",
                               return_value=self.cache + ".nope"):
            self.assertTrue(pawchive.creators_cache_stale())


class StaleAckTest(unittest.IsolatedAsyncioTestCase):
    """ack_stale_creators：过期才提示，新鲜不发。"""

    async def test_ack_sent_when_stale(self):
        sent = []

        async def reply(text):
            sent.append(text)

        with mock.patch.object(pawchive, "creators_cache_stale",
                               return_value=True):
            await pawchive.ack_stale_creators(reply)
        self.assertEqual(len(sent), 1)
        self.assertIn("缓存已过期", sent[0])

    async def test_no_ack_when_fresh(self):
        sent = []

        async def reply(text):
            sent.append(text)

        with mock.patch.object(pawchive, "creators_cache_stale",
                               return_value=False):
            await pawchive.ack_stale_creators(reply)
        self.assertEqual(sent, [])


# ============================================================
# 5) 扫描实时进度（/paw status · 🐾 面板，2026-09-24）
# ============================================================
class ScanProgressTest(unittest.IsolatedAsyncioTestCase):
    """扫描各阶段写进度状态；status/面板渲染；结束清空。"""

    def setUp(self):
        self.saved = (state.PAW_SCAN_RUNNING, state.PAW_SCAN_PROGRESS,
                      state.PAW_LAST_SCAN)
        state.PAW_SCAN_RUNNING = None
        state.PAW_SCAN_PROGRESS = {}
        state.PAW_LAST_SCAN = None
        self.db_dir = tempfile.mkdtemp(prefix="ux2_scan_", dir=_TMP)
        self._db = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.db_dir, "db.sqlite"))
        self._db.start()
        self.addCleanup(self._db.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())

    def tearDown(self):
        state.PAW_SCAN_RUNNING, state.PAW_SCAN_PROGRESS, \
            state.PAW_LAST_SCAN = self.saved

    def test_stage_callback_updates_state(self):
        cb = pawchive._scan_stage("拉取帖子")
        cb()
        self.assertEqual(state.PAW_SCAN_PROGRESS["stage"], "拉取帖子")
        cb("已拉取 450 条")
        self.assertEqual(state.PAW_SCAN_PROGRESS["detail"], "已拉取 450 条")

    async def test_scan_lifecycle_sets_and_clears_progress(self):
        creator = {"id": "46802018", "name": "MofuMochii", "service": "patreon"}

        def fake_fetch(service, cid, cookie=None, progress=None,
                       known_ids=None):
            if progress:
                progress("已拉取 120 条")
            return [{"id": 1, "title": "t"}]

        notifies = []

        async def fake_notify(text):
            notifies.append(text)

        def fake_faved(cookie):
            return {"1"}

        self.assertNotEqual(state.PAW_SCAN_RUNNING, None) if False else None
        reply_holder = {}

        async def fake_start(creator, scope="notfaved", since=None):
            pass

        with mock.patch.object(pawchive, "fetch_creator_posts", fake_fetch), \
             mock.patch.object(pawchive, "fetch_favorited_ids", fake_faved), \
             mock.patch.object(pawchive, "notify_user", fake_notify), \
             mock.patch.object(pawchive, "build_scan_records",
                               return_value=[]):
            msg = await pawchive.start_scan(creator)
            self.assertIn("开始扫描", msg)
            self.assertEqual(state.PAW_SCAN_RUNNING, "MofuMochii")
            # 等后台扫描任务跑完
            for t in list(pawchive._SPAWNED_SCANS):
                await t
        self.assertEqual(state.PAW_SCAN_RUNNING, None,
                         "扫描结束必须清 RUNNING")
        self.assertEqual(state.PAW_SCAN_PROGRESS, {},
                         "扫描结束必须清进度")
        self.assertEqual(state.PAW_LAST_SCAN["creator"], "MofuMochii")
        self.assertTrue(any("扫描完成" in n for n in notifies))

    async def test_status_text_renders_progress(self):
        state.PAW_SCAN_RUNNING = "MofuMochii"
        # 固定时钟：started=1000，now=1090 → 恰好 90 秒，断言才确定
        state.PAW_SCAN_PROGRESS = {
            "started": 1000.0,
            "stage": "拉取帖子",
            "detail": "已拉取 450 条",
        }
        with mock.patch.object(pawchive.time, "monotonic",
                               return_value=1090.0):
            text = pawchive.status_text()
        self.assertIn("🔄 正在扫描：MofuMochii", text)
        self.assertIn("拉取帖子", text)
        self.assertIn("已拉取 450 条", text)
        self.assertIn("已进行 1 分", text)

    async def test_panel_renders_scan_line(self):
        from tg_userbot import pawchive_worker
        state.PAW_SCAN_RUNNING = "MofuMochii"
        state.PAW_SCAN_PROGRESS = {"started": time.monotonic(),
                                   "stage": "收藏对比", "detail": "共 120 帖"}
        with mock.patch.object(pawchive_worker.runtime_db,
                               "pawchive_status_counts",
                               return_value={}), \
                mock.patch.object(pawchive_worker, "_panel_progress_lines",
                                  return_value=[]), \
                mock.patch.object(pawchive_worker, "_disk_free_gb",
                                  return_value=None):
            text = pawchive_worker.build_progress_text()
        self.assertIn("🔎 扫描中：MofuMochii", text)
        self.assertIn("收藏对比", text)


# ============================================================
# 6) Cookie 失效降级：不跳帖（去重交给入库唯一索引）+ 通知只对真失效
# ============================================================
class CookieInvalidNoSkipTest(unittest.TestCase):
    """Cookie 拿不到收藏清单（失效/网络失败）→ faved_ids=None →
    范围过滤整体跳过、全量处理——绝不能因为 Cookie 问题漏帖；
    重复入队由 UNIQUE(service, creator_id, post_id) 去重兜底。"""

    POSTS = [
        {"id": 1, "title": "会收藏的帖", "published": "2026-09-01T10:00:00",
         "attachments": [{"path": "a/1.mp4", "name": "1.mp4"}]},
        {"id": 2, "title": "新帖", "published": "2026-09-02T10:00:00",
         "attachments": [{"path": "a/2.mp4", "name": "2.mp4"}]},
    ]
    CREATOR = {"id": "46802018", "name": "MofuMochii", "service": "patreon"}

    def test_valid_cookie_notfaved_scope_skips_faved(self):
        recs = pawchive.build_scan_records(
            self.CREATOR, self.POSTS, faved_ids={"1"}, scope="notfaved")
        self.assertEqual([r["post_id"] for r in recs], ["2"])

    def test_invalid_cookie_processes_all_posts(self):
        """faved_ids=None（Cookie 失效/校验失败降级）→ 一帖不少。"""
        recs = pawchive.build_scan_records(
            self.CREATOR, self.POSTS, faved_ids=None, scope="notfaved")
        self.assertEqual([r["post_id"] for r in recs], ["1", "2"])


class DailyCookieNotifyOnlyOnRealInvalidTest(unittest.IsolatedAsyncioTestCase):
    """每日体检：网络抖动（校验失败）不通知；确认失效才通知。"""

    async def _run_check(self, ok, detail):
        from tg_userbot import app as app_mod
        from tg_userbot import maintenance, notify, pawchive
        sent = []

        async def fake_notify(text):
            sent.append(text)

        async def fake_daily():
            return None

        async def fake_check(force=False):
            return ok, detail

        async def fake_sleep(seconds):
            raise asyncio.CancelledError

        with mock.patch.object(maintenance, "daily_maintenance", fake_daily), \
             mock.patch.object(pawchive, "cookie_check_cached", fake_check), \
             mock.patch.object(notify, "notify_user", fake_notify), \
             mock.patch.object(config, "PAWCHIVE_COOKIE", "sessionid=X"), \
             mock.patch.object(app_mod.asyncio, "sleep", fake_sleep):
            task = asyncio.ensure_future(app_mod._maintenance_loop())
            with self.assertRaises(asyncio.CancelledError):
                await task
        return sent

    async def test_network_failure_not_notified(self):
        sent = await self._run_check(False, "校验失败（重试 2 次仍失败）")
        self.assertEqual(sent, [], "网络抖动不是失效，不该打扰用户")

    async def test_real_invalid_notified_with_dedup_note(self):
        sent = await self._run_check(False, "已失效（HTTP 401）")
        self.assertEqual(len(sent), 1)
        self.assertIn("已失效", sent[0])
        self.assertIn("不会重复下载", sent[0])
