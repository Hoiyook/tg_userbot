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

from tg_userbot import bot, cd2, commands, config, pawchive, runtime_db, state  # noqa: E402
from tg_userbot import pawchive_worker  # noqa: E402


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


# ============================================================
# 7) CDN 无扩展名 404：URL 自愈（2026-09-24 Caught Behemoth 排查产物）
# ============================================================
class FileEntryExtensionTest(unittest.TestCase):
    """_file_entry：API path 缺扩展名时从文件名补上。"""

    def test_path_without_ext_gets_ext_from_name(self):
        att = {"path": "/82/3a/hash123", "name": "bh_px.png"}
        entry = pawchive._file_entry(att)
        self.assertIn("/data/82/3a/hash123.png?f=bh_px.png", entry["url"])

    def test_path_with_ext_untouched(self):
        att = {"path": "/82/3a/hash123.png", "name": "bh_px.png"}
        entry = pawchive._file_entry(att)
        self.assertIn("/hash123.png?", entry["url"])

    def test_name_without_ext_leaves_path(self):
        att = {"path": "/82/3a/hash123", "name": "无名"}
        entry = pawchive._file_entry(att)
        self.assertIn("/data/82/3a/hash123?", entry["url"])


class RepairUrlTest(unittest.TestCase):
    """_repair_url 纯函数：补扩展名位置与守卫。"""

    def test_appends_ext_before_query(self):
        url = "https://f.pw/data/82/3a/hash?f=bh_px.png"
        self.assertEqual(
            pawchive_worker._repair_url(url, "bh_px.png"),
            "https://f.pw/data/82/3a/hash.png?f=bh_px.png")

    def test_no_query_case(self):
        self.assertEqual(
            pawchive_worker._repair_url("https://f.pw/data/x/hash", "a.zip"),
            "https://f.pw/data/x/hash.zip")

    def test_already_has_ext_returns_none(self):
        self.assertIsNone(pawchive_worker._repair_url(
            "https://f.pw/data/x/hash.png", "a.png"))

    def test_bad_name_returns_none(self):
        self.assertIsNone(pawchive_worker._repair_url(
            "https://f.pw/data/x/hash", "2024.09.22"))
        self.assertIsNone(pawchive_worker._repair_url(
            "https://f.pw/data/x/hash", "无扩展名"))


class HeadDeadIdsRepairTest(unittest.TestCase):
    """404 时先试补扩展名再判死；补活返回自愈映射。"""

    def setUp(self):
        self.targets = [(
            {"id": 7, "filename": "bh_px.png"},
            "https://f.pw/data/82/3a/hash?f=bh_px.png")]

    def test_repair_alive_not_dead(self):
        calls = []

        def fake_head(url, timeout=15):
            calls.append(url)
            # 第一轮：原 URL 404；第二轮：补扩展名 200
            return 404 if url.endswith("hash?f=bh_px.png") else 200

        with mock.patch.object(pawchive_worker, "_head_status",
                               side_effect=fake_head):
            dead, repaired = pawchive_worker._head_dead_ids(self.targets)
        self.assertEqual(dead, set(), "补活后绝不能标死")
        self.assertEqual(repaired.get(7),
                         "https://f.pw/data/82/3a/hash.png?f=bh_px.png")

    def test_repair_still_404_is_dead(self):
        with mock.patch.object(pawchive_worker, "_head_status",
                               return_value=404):
            dead, repaired = pawchive_worker._head_dead_ids(self.targets)
        self.assertEqual(dead, {7})
        self.assertEqual(repaired, {})

    def test_healthy_url_untouched(self):
        with mock.patch.object(pawchive_worker, "_head_status",
                               return_value=200):
            dead, repaired = pawchive_worker._head_dead_ids(self.targets)
        self.assertEqual((dead, repaired), (set(), {}))


class UpdateFileUrlTest(unittest.TestCase):
    """update_pawchive_file_url：自愈回写存量行。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ux2_urlfix_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        runtime_db.enqueue_pawchive_posts(
            "patreon", "1", "作者", [{
                "post_id": "p1", "title": "t",
                "published": "2026-09-24T00:00:00",
                "post_url": "https://x/p1", "subdir": "Pawchive/A/p1",
                "files": [{"url": "https://f.pw/data/x/hash",
                           "filename": "a.png"}],
                "ext_links": [],
            }], scan_batch="t")

    def test_url_updated(self):
        f = runtime_db.list_pawchive_files(
            runtime_db.claim_next_pawchive_post(now=1000)["id"])[0]
        n = runtime_db.update_pawchive_file_url(
            f["id"], "https://f.pw/data/x/hash.png")
        self.assertEqual(n, 1)
        rows = runtime_db.list_pawchive_files(f["post_row"])
        self.assertEqual(rows[0]["url"], "https://f.pw/data/x/hash.png")


# ============================================================
# 8) 监听扫描断连兜底：等重连 + 失败文案带自愈说明
# ============================================================
class WaitReconnectTest(unittest.IsolatedAsyncioTestCase):
    """_wait_reconnect：在线即返；断连等到重连；超时返回 False。"""

    def setUp(self):
        from tg_userbot import listener
        self.listener = listener
        self._saved = (state.client,)

    def tearDown(self):
        state.client = self._saved[0]

    async def test_online_returns_true_immediately(self):
        cli = mock.MagicMock()
        cli.is_connected = lambda: True
        state.client = cli
        self.assertTrue(await self.listener._wait_reconnect(timeout=5))

    async def test_no_client_returns_false_without_waiting(self):
        """client 缺席无从等待：立即 False（否则测试/异常态空转满 60s）。"""
        state.client = None
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)

        with mock.patch.object(self.listener.asyncio, "sleep", fake_sleep):
            ok = await self.listener._wait_reconnect(timeout=60, poll=3)
        self.assertFalse(ok)
        self.assertEqual(sleeps, [], "没有 client 绝不该进入等待循环")

    async def test_fake_client_without_is_connected_treated_online(self):
        """无 is_connected 接口的假客户端（测试桩）：视为在线直接扫。"""
        state.client = object()   # object 没有 is_connected
        self.assertTrue(await self.listener._wait_reconnect(timeout=5))

    async def test_reconnects_within_timeout(self):
        cli = mock.MagicMock()
        cli.is_connected = lambda: self.ticks > 1      # 第二轮探测才在线
        self.ticks = 0
        state.client = cli
        with mock.patch.object(self.listener.asyncio, "sleep",
                               mock.AsyncMock(side_effect=lambda s: setattr(
                                   self, "ticks", self.ticks + 1))):
            ok = await self.listener._wait_reconnect(timeout=60, poll=3)
        self.assertTrue(ok)

    async def test_timeout_returns_false(self):
        cli = mock.MagicMock()
        cli.is_connected = lambda: False
        state.client = cli
        with mock.patch.object(self.listener.asyncio, "sleep",
                               mock.AsyncMock()):
            ok = await self.listener._wait_reconnect(timeout=0.1, poll=3)
        self.assertFalse(ok)


class FetchNewFailureWordingTest(unittest.TestCase):
    """断连导致的读取失败必须带「自动补扫、不会丢」说明。"""

    def test_source_contains_reassurance(self):
        import inspect
        from tg_userbot import listener
        src = inspect.getsource(listener)
        self.assertIn("下轮自动补扫", src)
        self.assertIn("消息不会丢", src)


# ============================================================
# 9) /paw pr 帖子执行详情报告（2026-09-24）
# ============================================================
class PostReportTest(unittest.TestCase):
    """post_report_text：统计头 / 逐附件（失败排前）/ 外链 / 落盘实况。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ux2_pr_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        self._dl = tempfile.mkdtemp(prefix="ux2_prdl_", dir=_TMP)
        self._dlp = mock.patch.object(config, "DOWNLOAD_DIR", self._dl)
        self._dlp.start()
        self.addCleanup(self._dlp.stop)
        runtime_db.enqueue_pawchive_posts(
            "patreon", "32091060", "ruberule", [{
                "post_id": "112544981", "title": "Caught Behemoth",
                "published": "2024-09-22T10:00:00",
                "post_url": "https://pawchive.pw/patreon/user/32091060/post/112544981",
                "subdir": "Pawchive/ruberule/2024-09-22_112544981_Caught Behemoth",
                "files": [
                    {"url": "https://f.pw/data/1.png", "filename": "bh_01.png"},
                    {"url": "https://f.pw/data/2.png", "filename": "bh_px.png"},
                ],
                "ext_links": [{"domain": "mega.nz",
                               "url": "https://mega.nz/folder/x"}],
            }], scan_batch="2026-09-24 08:00:00")
        post = runtime_db.claim_next_pawchive_post(now=1000)
        files = runtime_db.list_pawchive_files(post["id"])
        runtime_db.mark_pawchive_file_done(files[0]["id"], size_bytes=2048)
        runtime_db.mark_pawchive_file_failed(
            files[1]["id"], error="站点缺文件(404) HTTP 404")
        runtime_db.finalize_pawchive_post(
            post["id"], runtime_db.PAW_POST_FAILED)
        self.row_id = post["id"]

    def test_report_by_row_id(self):
        text = pawchive.post_report_text(str(self.row_id))
        self.assertIn("📋 帖子报告", text)
        self.assertIn("ruberule", text)
        self.assertIn("附件 2 个", text)
        self.assertIn("✅ 完成 1", text)
        self.assertIn("未完成 1", text)
        self.assertIn("共 2.00 KB", text)
        # 失败的排前面
        self.assertLess(text.index("bh_px.png"), text.index("bh_01.png"))
        self.assertIn("站点缺文件(404)", text)
        self.assertIn("🌐 外链 1 条", text)
        self.assertIn("mega.nz", text)
        self.assertIn("目录不在本地", text)   # tmp DOWNLOAD_DIR 无此目录
        self.assertIn("扫描批次：2026-09-24 08:00:00", text)

    def test_report_by_post_url(self):
        text = pawchive.post_report_text(
            "https://pawchive.pw/patreon/user/32091060/post/112544981")
        self.assertIn("📋 帖子报告", text)
        self.assertIn("Caught Behemoth", text)

    def test_local_dir_presence_shown(self):
        os.makedirs(os.path.join(
            self._dl, "Pawchive/ruberule/2024-09-22_112544981_Caught Behemoth"),
            exist_ok=True)
        text = pawchive.post_report_text(str(self.row_id))
        self.assertIn("个文件在盘上", text)

    def test_not_found(self):
        text = pawchive.post_report_text("999999")
        self.assertIn("找不到帖子", text)

    def test_multi_match_disambiguation(self):
        """同 post_id 不同创作者 → 消歧清单（att/pr 共用解析）。"""
        runtime_db.enqueue_pawchive_posts(
            "fanbox", "99", "another", [{
                "post_id": "112544981", "title": "同名帖",
                "published": "2024-09-22T10:00:00", "post_url": "https://x/2",
                "subdir": "Pawchive/another/p",
                "files": [{"url": "https://f.pw/data/9.png",
                           "filename": "z.png"}],
                "ext_links": [],
            }], scan_batch="t")
        text = pawchive.post_report_text("112544981")
        self.assertIn("对应多条记录", text)
        self.assertIn("ruberule", text)
        self.assertIn("another", text)

    def test_command_and_panel_wiring(self):
        self.assertEqual(pawchive.parse_paw_command("/paw pr 1260"),
                         ("pr", "1260"))
        self.assertIn("paw_pr", config.MENU_ACTIONS)
        texts = [b.text for row in pawchive.menu_buttons() for b in row]
        self.assertIn("📋 帖子报告", texts)


# ============================================================
# 10) 功能使用审计（feature_usage / /usage）
# ============================================================
class FeatureUsageDbTest(unittest.TestCase):
    """feature_usage_bump/top：按天聚合、近 7 天口径、排行。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ux2_usage_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())

    def test_bump_aggregates(self):
        now = time.time()
        runtime_db.feature_usage_bump("/folder", now=now)
        runtime_db.feature_usage_bump("/folder", now=now)
        runtime_db.feature_usage_bump("menu:home", now=now)
        rows = runtime_db.feature_usage_top(10, now=now)
        by_name = {r["name"]: r for r in rows}
        self.assertEqual(by_name["/folder"]["total"], 2)
        self.assertEqual(by_name["/folder"]["recent7"], 2)
        self.assertEqual(by_name["menu:home"]["total"], 1)
        self.assertEqual(by_name["/folder"]["last_at"], int(now))

    def test_recent7_excludes_old_days(self):
        now = time.time()
        runtime_db.feature_usage_bump("/old", now=now - 30 * 86400)
        runtime_db.feature_usage_bump("/old", now=now)
        rows = runtime_db.feature_usage_top(10, now=now)
        r = rows[0]
        self.assertEqual(r["total"], 2)
        self.assertEqual(r["recent7"], 1)
        self.assertEqual(r["first_day"], time.strftime(
            "%Y-%m-%d", time.localtime(now - 30 * 86400)))


class UsageNameTest(unittest.TestCase):
    """usage_name：子命令带名字、参数绝不进名字。"""

    def test_multiword_and_guard(self):
        self.assertEqual(commands.usage_name("/paw plan MofuMochii"),
                         "/paw plan")
        self.assertEqual(commands.usage_name("/CHROME https://x/y"),
                         "/chrome")
        self.assertEqual(commands.usage_name("/retry all"), "/retry")
        self.assertEqual(commands.usage_name(""), "")


class CommandUsageRecordTest(unittest.TestCase):
    """handle_command 埋点：已注册指令落库、未注册不落、炸不了命令。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ux2_cusage_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())

    def test_registered_command_recorded(self):
        async def run():
            ev = mock.MagicMock()
            ev.reply = mock.AsyncMock()
            await commands.handle_command(ev, "/folder")
        asyncio.new_event_loop().run_until_complete(run())
        rows = runtime_db.feature_usage_top(10)
        self.assertEqual([r["name"] for r in rows], ["/folder"])

    def test_unregistered_not_recorded(self):
        async def run():
            ev = mock.MagicMock()
            ev.reply = mock.AsyncMock()
            handled = await commands.handle_command(ev, "/not_a_cmd")
            self.assertFalse(handled)
        asyncio.new_event_loop().run_until_complete(run())
        self.assertEqual(runtime_db.feature_usage_top(10), [])

    def test_db_failure_never_breaks_command(self):
        async def run():
            ev = mock.MagicMock()
            ev.reply = mock.AsyncMock()
            with mock.patch.object(runtime_db, "feature_usage_bump",
                                   side_effect=RuntimeError("db down")):
                handled = await commands.handle_command(ev, "/folder")
            self.assertTrue(handled)
        asyncio.new_event_loop().run_until_complete(run())


class MenuButtonUsageRecordTest(unittest.IsolatedAsyncioTestCase):
    """面板按钮埋点：menu:<action> 落库。"""

    async def test_status_button_recorded(self):
        self.dir = tempfile.mkdtemp(prefix="ux2_musage_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        saved_me = state.MY_ID
        state.MY_ID = 123
        ev = mock.MagicMock()
        ev.chat_id = 123
        ev.data = b"m:status"
        ev.answer = mock.AsyncMock()
        ev.edit = mock.AsyncMock()
        try:
            with mock.patch.object(bot.logger, "info"), \
                    mock.patch.object(bot.logger, "exception"):
                await bot.bot_callback_handler(ev)
        finally:
            state.MY_ID = saved_me
        names = [r["name"] for r in runtime_db.feature_usage_top(10)]
        self.assertIn("menu:status", names)


class UsageCommandTest(unittest.TestCase):
    """/usage 输出格式。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ux2_ucmd_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())

    def test_empty_and_rows(self):
        async def run():
            ev = mock.MagicMock()
            ev.reply = mock.AsyncMock()
            await commands.handle_command(ev, "/usage")
            return ev.reply.await_args.args[0]
        loop = asyncio.new_event_loop()
        # /usage 自身也会被记录（进命令入口即计数）——首查只有它自己
        first = loop.run_until_complete(run())
        self.assertIn("/usage", first)
        runtime_db.feature_usage_bump("/paw plan")
        with_data = loop.run_until_complete(run())
        loop.close()
        self.assertIn("/paw plan", with_data)
        self.assertIn("近7天", with_data)
        # 空态文案单独验：无任何记录时提示
        with mock.patch.object(runtime_db, "feature_usage_top",
                               return_value=[]):
            ev = mock.MagicMock()
            ev.reply = mock.AsyncMock()
            loop2 = asyncio.new_event_loop()
            empty = loop2.run_until_complete(run())
            loop2.close()
        self.assertIn("还没有记录", empty)


# ============================================================
# 11) /paw since 持久化时间下限
# ============================================================
class PawSinceTest(unittest.TestCase):
    """set/get roundtrip + since_reply 各分支 + plan 默认应用。"""

    def setUp(self):
        self.path = os.path.join(_TMP, f"since_{id(self)}.json")
        self._p = mock.patch.object(pawchive, "_since_path",
                                    return_value=self.path)
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_roundtrip_and_clear(self):
        self.assertIsNone(pawchive.get_default_since())
        pawchive.set_default_since("2026-01-01")
        self.assertEqual(pawchive.get_default_since(), "2026-01-01")
        pawchive.set_default_since(None)
        self.assertIsNone(pawchive.get_default_since())

    def test_reply_branches(self):
        self.assertIn("未设置", pawchive.since_reply(""))
        self.assertIn("已设置", pawchive.since_reply("2026-03-01"))
        self.assertIn("2026-03-01", pawchive.since_reply(""))
        self.assertIn("日期格式", pawchive.since_reply("瞎写的"))
        self.assertIn("已清除", pawchive.since_reply("off"))
        self.assertIn("未设置", pawchive.since_reply(""))

    async def test_plan_applies_default_since(self):
        state.PAW_SCAN_RUNNING = None
        captured = {}

        async def fake_start(creator, scope="notfaved", since=None):
            captured["since"] = since
            return "ok"

        async def fake_resolve(name):
            return {"id": "1", "name": name, "service": "patreon"}

        pawchive.set_default_since("2026-02-02")
        ev = mock.MagicMock()
        ev.reply = mock.AsyncMock()
        with mock.patch.object(pawchive, "start_scan", fake_start), \
                mock.patch.object(pawchive, "resolve_creator_async",
                                  fake_resolve), \
                mock.patch.object(pawchive, "creators_cache_stale",
                                  return_value=False):
            await pawchive._reply_plan(ev, "MofuMochii")
        self.assertEqual(captured["since"], "2026-02-02",
                         "未显式给 since 时必须应用持久化默认")
        # 显式 since 覆盖默认
        captured.clear()
        with mock.patch.object(pawchive, "start_scan", fake_start), \
                mock.patch.object(pawchive, "resolve_creator_async",
                                  fake_resolve), \
                mock.patch.object(pawchive, "creators_cache_stale",
                                  return_value=False):
            await pawchive._reply_plan(ev, "MofuMochii since 2026-05-01")
        self.assertEqual(captured["since"], "2026-05-01")


# ============================================================
# 12) 巡检修复（2026-09-25）：按钮嵌套行 + 文件名超长
# ============================================================
class InputCancelButtonShapeTest(unittest.TestCase):
    """input_cancel_buttons 每行每元素必须是 Button 实例（禁嵌套行）。

    曾把 back_home_buttons()（行的列表）整坨当一行塞进去 →
    [[❌], [[🔙]]]：Telethon 把内层 list 当普通按钮，渲染抛
    'You cannot mix inline with normal buttons'。旧测试在含 ❌ 的第 0 行
    就 return，嵌套行永远没被走到——这里必须**全量**遍历。"""

    def test_all_rows_flat_buttons(self):
        from telethon.tl.types import KeyboardInlineButton
        rows = bot.input_cancel_buttons()
        self.assertTrue(rows)
        for row in rows:
            self.assertIsInstance(row, list, "行必须是 list")
            for b in row:
                self.assertIsInstance(
                    b, KeyboardInlineButton,
                    f"按钮必须是 KeyboardInlineButton，实际 {type(b)}")

    def test_has_cancel_and_home(self):
        rows = bot.input_cancel_buttons()
        flat = [b for row in rows for b in row]
        texts = [b.text for b in flat]
        self.assertIn("❌ 取消", texts)
        self.assertIn("🔙 返回主菜单", texts)


class SanitizeFilenameBudgetTest(unittest.TestCase):
    """sanitize_filename_bounded 字节截断：保扩展名、CJK 不切半、短名不动；
    sanitize_filename 本体保持无预算契约（compute_final_filename 依赖）。"""

    def test_short_name_untouched(self):
        from tg_userbot.naming import sanitize_filename_bounded as sb
        self.assertEqual(sb("bh_01.png"), "bh_01.png")
        self.assertEqual(sb("Gorizia Default.rar"), "Gorizia Default.rar")

    def test_sanitize_itself_never_truncates(self):
        """契约钉死：无预算版绝不裁（compute_final_filename max_bytes=None
        的「整段 caption 保留」语义依赖它）。"""
        from tg_userbot.naming import sanitize_filename
        self.assertEqual(len(sanitize_filename("汉" * 300)), 300)

    def test_url_name_truncated_with_ext(self):
        from tg_userbot.naming import sanitize_filename_bounded as sb
        url = ("https___www.patreon.com_media-u_Z0FBQUFBQmtpYm5ONDhNdzhYWklQ"
               "REZxRTNoVTlvd2ZaLXZHamZaSzdSdHpxMDNQZHFkMy1aWkw1WjZLSlBNSW5N"
               "dlpDQUNPUGVMUlZ3OGYxbTFPTWZvRVZuSWwweVRYZUpkbDBobkJuakZsUUtT"
               "bVJFUXFfdlhIZkh2ZVpjOGtqY19pdmJ3UUt5V3ZvY3kyMmdjR2VjcW1GQ3Ft"
               "dl8wOFRRPT0=#205596539_.part")
        out = sb(url)
        self.assertTrue(out)
        self.assertLessEqual(len(out.encode("utf-8")), 200)

    def test_cjk_truncation_no_half_char(self):
        from tg_userbot.naming import sanitize_filename_bounded as sb
        long_cjk = "新" * 300 + ".zip"
        out = sb(long_cjk)
        self.assertTrue(out.endswith(".zip"))
        self.assertLessEqual(len(out.encode("utf-8")), 200)
        out.encode("utf-8")   # 不抛 = 没切半个字符

    def test_no_valid_ext(self):
        from tg_userbot.naming import sanitize_filename_bounded as sb
        out = sb("x" * 300 + ".超级长扩展名")
        self.assertLessEqual(len(out.encode("utf-8")), 200)

    def test_pawchive_target_path_now_safe(self):
        """端到端：worker 的 _target_path 不再因超长名炸 OSError 63。"""
        from tg_userbot.pawchive_worker import _target_path
        post = {"subdir": "Pawchive/MofuMochii/2023-05-26_83579000_t"}
        name = "https___" + "Z" * 400 + "_.part"
        path = _target_path(post, name)
        import os as _os
        self.assertLessEqual(
            len(_os.path.basename(path).encode("utf-8")), 200 + 5,
            "落盘文件名必须在字节预算内（.part 后缀留量）")


# ============================================================
# 13) /paw fail 非死链失败明细（2026-09-25）
# ============================================================
class PawFailTest(unittest.TestCase):
    """fail_text：死链不占版面；非死链失败带错误聚合与行 id。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ux2_fail_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())

    def _enqueue_post(self, post_id, files_errors):
        runtime_db.enqueue_pawchive_posts(
            "patreon", "1", "作者A", [{
                "post_id": post_id, "title": f"帖{post_id}",
                "published": "2026-09-25T00:00:00",
                "post_url": f"https://x/{post_id}",
                "subdir": f"Pawchive/A/{post_id}",
                "files": [{"url": f"https://f/{post_id}_{i}.mp4",
                           "filename": f"{i}.mp4"}
                          for i in range(len(files_errors))],
                "ext_links": [],
            }], scan_batch="t")
        post = runtime_db.claim_next_pawchive_post(now=1000)
        for f, err in zip(runtime_db.list_pawchive_files(post["id"]),
                          files_errors):
            if err is None:
                runtime_db.mark_pawchive_file_done(f["id"], size_bytes=10)
            elif err.startswith(runtime_db.PAW_DEAD_LINK_MARK):
                runtime_db.mark_pawchive_file_failed(f["id"], error=err)
            else:
                runtime_db.mark_pawchive_file_failed(f["id"], error=err)
        runtime_db.finalize_pawchive_post(post["id"],
                                          runtime_db.PAW_POST_FAILED)

    def test_all_dead_shows_archive_hint(self):
        self._enqueue_post("p1", [runtime_db.PAW_DEAD_LINK_MARK + " HTTP 404"])
        text = pawchive.fail_text()
        self.assertIn("全部是 404 死链", text)
        self.assertIn("/paw archive", text)

    def test_non_dead_listed_with_errors_and_row_id(self):
        self._enqueue_post("p2", [
            "HTTP 429 too many requests",
            None,   # 这个成功
            "ReadTimeout: timed out",
        ])
        text = pawchive.fail_text()
        self.assertIn("#1 作者A", text)
        self.assertIn("HTTP 429", text)
        self.assertIn("ReadTimeout", text)
        self.assertIn("✅1", text)          # 部分成功标记
        self.assertIn("非死链 2 / 死链 0", text)
        self.assertIn("/paw retry", text)
        # 死链错误绝不进明细行
        self._enqueue_post("p3", [runtime_db.PAW_DEAD_LINK_MARK + " HTTP 404"])
        text2 = pawchive.fail_text()
        self.assertIn("#1", text2)          # p2 仍列出
        self.assertNotIn("站点缺文件", text2.split("#1")[1].split("失败文件")[0])

    def test_empty_db(self):
        self.assertIn("没有失败记录", pawchive.fail_text())


# ============================================================
# 14) /paw archive 明细视图（2026-09-25）
# ============================================================
class ArchiveOverviewTest(unittest.TestCase):
    """archive_overview_text：总数/按作者/最近条目；空态指引。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ux2_arch_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())

    def _enqueue(self, post_id, creator="作者A"):
        runtime_db.enqueue_pawchive_posts(
            "patreon", "1", creator, [{
                "post_id": post_id, "title": f"帖{post_id}",
                "published": "2024-11-02T00:00:00",
                "post_url": f"https://x/{post_id}",
                "subdir": f"Pawchive/{creator}/{post_id}",
                "files": [{"url": f"https://f/{post_id}.png",
                           "filename": f"{post_id}.png"}],
                "ext_links": [],
            }], scan_batch="t")
        post = runtime_db.claim_next_pawchive_post(now=1000)
        f = runtime_db.list_pawchive_files(post["id"])[0]
        runtime_db.mark_pawchive_file_failed(
            f["id"], error=runtime_db.PAW_DEAD_LINK_MARK + " HTTP 404")
        runtime_db.finalize_pawchive_post(post["id"],
                                          runtime_db.PAW_POST_FAILED)

    def test_empty_state(self):
        text = pawchive.archive_overview_text()
        self.assertIn("归档区是空的", text)
        self.assertIn("/paw archive failed", text)

    def test_overview_with_data(self):
        self._enqueue("p1", "作者A")
        self._enqueue("p2", "作者A")
        self._enqueue("p3", "作者B")
        # 把两个 FAILED 死链帖归档
        archived, kept = runtime_db.archive_pawchive_failed()
        self.assertEqual((archived, kept), (3, 0))
        text = pawchive.archive_overview_text()
        self.assertIn("共 3 帖", text)
        self.assertIn("作者A 2", text)
        self.assertIn("作者B 1", text)
        self.assertIn("#3 作者B", text)     # 最近条目按 id 倒序
        self.assertIn("/paw pr", text)

    def test_dispatch_list_form(self):
        """无参/`list` 出明细；`failed` 保持执行语义（归档空库 → 提示）。"""
        async def run():
            ev = mock.MagicMock()
            ev.reply = mock.AsyncMock()
            await pawchive.command_reply(ev, "/paw archive")
            first = ev.reply.await_args.args[0]
            await pawchive.command_reply(ev, "/paw archive failed")
            second = ev.reply.await_args.args[0]
            return first, second
        first, second = asyncio.new_event_loop().run_until_complete(run())
        self.assertIn("归档明细", first)
        self.assertIn("没有可归档", second)   # 空库执行 = 幂等提示
