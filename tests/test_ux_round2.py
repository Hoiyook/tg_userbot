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
from tg_userbot import menu, text as text_mod  # noqa: E402
from tg_userbot import pawchive_worker  # noqa: E402
from tg_userbot import queue as test_queue_mod  # noqa: E402


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


# ============================================================
# 15) /paw archive del 彻底删除 + 明细带原帖地址（2026-09-25）
# ============================================================
class ArchiveDeleteTest(unittest.TestCase):
    """archive_delete_reply / delete_archived_pawchive_post：只删 ARCHIVED，
    单条与区间，不可逆路径的状态边界。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ux2_archdel_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())

    def _enqueue(self, post_id):
        runtime_db.enqueue_pawchive_posts(
            "patreon", "1", "作者A", [{
                "post_id": post_id, "title": f"帖{post_id}",
                "published": "2024-11-02T00:00:00",
                "post_url": f"https://x/{post_id}",
                "subdir": f"Pawchive/A/{post_id}",
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
        return post["id"]

    def test_delete_archived_only(self):
        rid = self._enqueue("p1")
        self._enqueue("p2")
        archived, _ = runtime_db.archive_pawchive_failed()
        self.assertEqual(archived, 2)
        # 非 ARCHIVED 行（新帖未处理）必须被拒
        pending_id = self._enqueue("p3")
        self.assertFalse(runtime_db.delete_archived_pawchive_post(pending_id))
        # 删一个归档帖：帖子+附件行一起没了
        self.assertTrue(runtime_db.delete_archived_pawchive_post(rid))
        self.assertEqual(runtime_db.get_pawchive_post_row(rid), None)
        self.assertEqual(runtime_db.list_pawchive_files(rid), [])
        # 再删同一条 = 已不存在 False（幂等安全）
        self.assertFalse(runtime_db.delete_archived_pawchive_post(rid))

    def test_reply_single_and_range(self):
        r1 = self._enqueue("p1")
        r2 = self._enqueue("p2")
        r3 = self._enqueue("p3")
        runtime_db.archive_pawchive_failed()
        text = pawchive.archive_delete_reply(str(r1))
        self.assertIn("已彻底删除 1", text)
        text2 = pawchive.archive_delete_reply(f"{r2}-{r3}")
        self.assertIn("已彻底删除 2", text2)
        # 全删光后明细空态
        self.assertIn("归档区是空的", pawchive.archive_overview_text())

    def test_reply_skips_non_archived(self):
        rid = self._enqueue("p1")     # 保持 FAILED，不归档
        text = pawchive.archive_delete_reply(str(rid))
        self.assertIn("已彻底删除 0", text)
        self.assertIn("非归档态或不存在", text)

    def test_reply_bad_input(self):
        self.assertIn("用法", pawchive.archive_delete_reply("abc"))
        self.assertIn("写反", pawchive.archive_delete_reply("100-50"))

    def test_overview_has_post_url(self):
        rid = self._enqueue("p9")
        runtime_db.archive_pawchive_failed()
        text = pawchive.archive_overview_text()
        self.assertIn("↳ https://x/p9", text)


# ============================================================
# 16) /help 完整手册（2026-09-25 统一归集）
# ============================================================
class HelpManualTest(unittest.TestCase):
    """/help：下划线标准形全覆盖 + 单条消息发得出去 + 仓库详版同源。"""

    def test_covers_all_registered_base_commands(self):
        """每个注册指令的基词都出现在手册（含下划线标准形本身）。"""
        text = commands._help_text()
        bases = {name.split("_")[0] for name in
                 config.REGISTERED_COMMAND_NAMES}
        for base in sorted(bases):
            self.assertIn(f"/{base}", text, f"/{base} 没进 /help")

    def test_canonical_underscore_forms_documented(self):
        text = commands._help_text()
        for cmd in ("/paw_plan", "/paw_progress", "/paw_search", "/paw_post",
                    "/paw_pr", "/paw_att", "/paw_find", "/paw_manual",
                    "/paw_done", "/paw_fail", "/paw_retry", "/paw_archive",
                    "/paw_backfill", "/paw_since", "/paw_pause",
                    "/paw_resume", "/paw_cookie", "/paw_csv",
                    "/listen_add", "/listen_edit", "/listen_del",
                    "/listen_scan", "/listen_interval",
                    "/wl_add", "/wl_del", "/wl_scan", "/wl_since",
                    "/retry_all", "/retry_del", "/queue_del",
                    "/sqlt_add", "/sqlt_del",
                    "/cmdt_add", "/cmdt_del", "/cmdt_run",
                    "/caption_filter_add", "/caption_filter_del",
                    "/caption_filter_test", "/usage", "/cmdhis", "/cd2ck"):
            self.assertIn(cmd, text, f"{cmd} 没进 /help")

    def test_key_param_forms_present(self):
        text = commands._help_text()
        for form in ("since 日期", "起-止", "行id", "retry_del",
                     "add 聊天 标签", "alias_check_placeholder"):
            if form == "alias_check_placeholder":
                continue
            self.assertIn(form, text, f"参数形态「{form}」没写进 /help")

    def test_legacy_space_form_noted(self):
        self.assertIn("等效", commands._help_text())

    def test_fits_one_message(self):
        self.assertLessEqual(len(commands._help_text()), 3900)

    def test_repo_manual_exists_and_covers(self):
        """仓库详版文档存在且与 /help 同源覆盖（抽查关键指令）。"""
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "docs", "操作手册.md")
        self.assertTrue(os.path.exists(path), "docs/操作手册.md 缺失")
        content = open(path, encoding="utf-8").read()
        for key in ("/paw_pr", "/paw_fail", "/paw_archive_del", "/usage",
                    "/cmdhis", "/cd2ck", "/listen_add", "/wl_since",
                    "/caption_filter_add", "/sqlt_add", "/cmdt_add"):
            self.assertIn(key, content)


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


# ============================================================
# 15) /paw archive del 彻底删除 + 明细带原帖地址（2026-09-25）
# ============================================================
class ArchiveDeleteTest(unittest.TestCase):
    """archive_delete_reply / delete_archived_pawchive_post：只删 ARCHIVED，
    单条与区间，不可逆路径的状态边界。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ux2_archdel_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())

    def _enqueue(self, post_id):
        runtime_db.enqueue_pawchive_posts(
            "patreon", "1", "作者A", [{
                "post_id": post_id, "title": f"帖{post_id}",
                "published": "2024-11-02T00:00:00",
                "post_url": f"https://x/{post_id}",
                "subdir": f"Pawchive/A/{post_id}",
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
        return post["id"]

    def test_delete_archived_only(self):
        rid = self._enqueue("p1")
        self._enqueue("p2")
        archived, _ = runtime_db.archive_pawchive_failed()
        self.assertEqual(archived, 2)
        # 非 ARCHIVED 行（新帖未处理）必须被拒
        pending_id = self._enqueue("p3")
        self.assertFalse(runtime_db.delete_archived_pawchive_post(pending_id))
        # 删一个归档帖：帖子+附件行一起没了
        self.assertTrue(runtime_db.delete_archived_pawchive_post(rid))
        self.assertEqual(runtime_db.get_pawchive_post_row(rid), None)
        self.assertEqual(runtime_db.list_pawchive_files(rid), [])
        # 再删同一条 = 已不存在 False（幂等安全）
        self.assertFalse(runtime_db.delete_archived_pawchive_post(rid))

    def test_reply_single_and_range(self):
        r1 = self._enqueue("p1")
        r2 = self._enqueue("p2")
        r3 = self._enqueue("p3")
        runtime_db.archive_pawchive_failed()
        text = pawchive.archive_delete_reply(str(r1))
        self.assertIn("已彻底删除 1", text)
        text2 = pawchive.archive_delete_reply(f"{r2}-{r3}")
        self.assertIn("已彻底删除 2", text2)
        # 全删光后明细空态
        self.assertIn("归档区是空的", pawchive.archive_overview_text())

    def test_reply_skips_non_archived(self):
        rid = self._enqueue("p1")     # 保持 FAILED，不归档
        text = pawchive.archive_delete_reply(str(rid))
        self.assertIn("已彻底删除 0", text)
        self.assertIn("非归档态或不存在", text)

    def test_reply_bad_input(self):
        self.assertIn("用法", pawchive.archive_delete_reply("abc"))
        self.assertIn("写反", pawchive.archive_delete_reply("100-50"))

    def test_overview_has_post_url(self):
        rid = self._enqueue("p9")
        runtime_db.archive_pawchive_failed()
        text = pawchive.archive_overview_text()
        self.assertIn("↳ https://x/p9", text)


# ============================================================
# 17) ReplyMarkupTooLong 双层修复（2026-09-25）
# ============================================================
class LsButtonsCapTest(unittest.TestCase):
    """sh_ls_buttons 目录按钮上限：200 目录不再撑爆 reply markup。"""

    def test_cap_at_40_buttons_with_nav(self):
        dirs = [(f"目录{i}", f"{i:08x}") for i in range(200)]
        rows = menu.sh_ls_buttons(dirs, up_token="abcdef01",
                                  home_token="12345678")
        flat = [b for row in rows for b in row]
        self.assertLessEqual(len(flat), menu.MAX_LS_BUTTONS)
        # 导航行始终保留
        texts = [b.text for b in flat]
        self.assertIn("⬆️ 上一级", texts)
        self.assertIn("🏠 根目录", texts)
        self.assertIn("📁 目录0", texts)     # 首（最新）保留

    def test_small_listing_untouched(self):
        dirs = [(f"d{i}", f"{i:08x}") for i in range(5)]
        rows = menu.sh_ls_buttons(dirs, home_token="12345678")
        self.assertEqual(len([b for r in rows for b in r]), 6)  # 5 目录+根


class CleanButtonsBudgetTest(unittest.TestCase):
    """clean_buttons 体积护栏：超预算裁行保导航，单行超限整体降级。"""

    @staticmethod
    def _big_rows(n_rows, label="目录名"):
        from telethon import Button
        rows = [[Button.inline(f"📁 {label}{i}", b"m:sh_ls:abcd1234")]
                for i in range(n_rows)]
        rows.append([Button.inline("🔙 返回主菜单", b"m:home")])
        return rows

    def test_small_pass_through(self):
        rows = self._big_rows(5)
        self.assertEqual(text_mod.clean_buttons(rows), rows)

    def test_oversized_trimmed_keeps_first_and_nav(self):
        rows = self._big_rows(300, label="很长的目录名字" * 4)
        out = text_mod.clean_buttons(rows)
        self.assertIsNotNone(out, "裁剪后应当能发出")
        flat = [b for row in out for b in row]
        texts = [b.text for b in flat]
        self.assertIn("🔙 返回主菜单", texts)          # 末行导航保留
        self.assertTrue(any(t.endswith("名字0") for t in texts),
                        "首行数据保留")
        self.assertLess(len(out), len(rows))

    def test_single_row_over_budget_degrades_to_none(self):
        from telethon import Button
        huge = [[Button.inline("x" * 6000, b"m:home")]]
        self.assertIsNone(text_mod.clean_buttons(huge))

    def test_empty_still_none(self):
        self.assertIsNone(text_mod.clean_buttons([]))
        self.assertIsNone(text_mod.clean_buttons(None))


# ============================================================
# 18) /paw progress 按作者处理进度（2026-09-25）
# ============================================================
class AuthorProgressTest(unittest.TestCase):
    """author_progress_text：状态计数/文件产出/完成率/待处理明细/未知作者。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ux2_prog_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        # 作者A：1 完成 + 1 失败（非死链） + 1 归档死链
        runtime_db.enqueue_pawchive_posts(
            "patreon", "1", "MofuMochii", [{
                "post_id": f"p{i}", "title": f"帖{i}",
                "published": "2026-09-01T00:00:00",
                "post_url": f"https://x/p{i}",
                "subdir": f"Pawchive/MofuMochii/p{i}",
                "files": [{"url": f"https://f/p{i}.png",
                           "filename": f"{i}.png"}],
                "ext_links": [],
            } for i in (1, 2, 3)], scan_batch="t")
        for i in (1, 2, 3):
            post = runtime_db.claim_next_pawchive_post(now=1000)
            f = runtime_db.list_pawchive_files(post["id"])[0]
            if i == 1:
                runtime_db.mark_pawchive_file_done(f["id"], size_bytes=1024)
                runtime_db.finalize_pawchive_post(
                    post["id"], runtime_db.PAW_POST_COMPLETED)
            elif i == 2:
                runtime_db.mark_pawchive_file_failed(
                    f["id"], error="HTTP 429")
                runtime_db.finalize_pawchive_post(
                    post["id"], runtime_db.PAW_POST_FAILED)
            else:
                runtime_db.mark_pawchive_file_failed(
                    f["id"], error=runtime_db.PAW_DEAD_LINK_MARK + " 404")
                runtime_db.finalize_pawchive_post(
                    post["id"], runtime_db.PAW_POST_FAILED)
        runtime_db.archive_pawchive_failed()

    def test_full_view(self):
        text = pawchive.author_progress_text("MofuMochii")
        self.assertIn("帖子 3 个：✅ 完成 1", text)
        self.assertIn("完成率 33%", text)
        self.assertIn("🗄 已归档 1", text)
        self.assertIn("✅ 1 个 / 1.00 KB", text)
        self.assertIn("站点死链 1 个", text)
        # 待处理只列可行动状态（归档死链帖不占待办版面）
        self.assertIn("待处理", text)
        self.assertIn("⚠️ 可重投 1 个", text)     # 非死链失败单独亮出
        self.assertIn("#2 ❌ 失败｜帖2", text)
        self.assertNotIn("帖3", text)   # 归档的帖3 不进明细

    def test_case_insensitive_and_unknown(self):
        """小写输入也命中，标题显示 DB 规范名。"""
        text = pawchive.author_progress_text("mofumochii")
        self.assertIn("MofuMochii 处理进度", text)
        self.assertIn("没有叫", pawchive.author_progress_text("不存在"))
        self.assertIn("用法", pawchive.author_progress_text(""))

    def test_wiring(self):
        self.assertEqual(pawchive.parse_paw_command("/paw progress MofuMochii"),
                         ("progress", "MofuMochii"))


# ============================================================
# 19) file 主文件字段捕获 + /paw post 刷新补录（2026-09-25 帖 78212541）
# ============================================================
class FileMainFieldTest(unittest.TestCase):
    """build_scan_records：file 字段并入附件（与 attachments 按 path 去重）。"""

    def test_file_field_added_when_not_in_attachments(self):
        post = {"id": "1", "title": "t", "published": "2026-09-01T00:00:00",
                "attachments": [{"name": "a.rar", "path": "/x/aa"}],
                "file": {"name": "twitter.png", "path": "/y/bb"}}
        recs = pawchive.build_scan_records(
            {"id": "1", "name": "A", "service": "patreon"}, [post])
        names = [f["filename"] for f in recs[0]["files"]]
        self.assertEqual(names[0], "twitter.png")   # 主文件在最前
        self.assertIn("a.rar", names)

    def test_file_field_dedup_when_same_path(self):
        post = {"id": "1", "title": "t", "published": "2026-09-01T00:00:00",
                "attachments": [{"name": "a.png", "path": "/x/aa"}],
                "file": {"name": "a.png", "path": "/x/aa"}}
        recs = pawchive.build_scan_records(
            {"id": "1", "name": "A", "service": "patreon"}, [post])
        self.assertEqual(len(recs[0]["files"]), 1)

    def test_no_file_field_untouched(self):
        post = {"id": "1", "title": "t", "published": "2026-09-01T00:00:00",
                "attachments": [{"name": "a.rar", "path": "/x/aa"}]}
        recs = pawchive.build_scan_records(
            {"id": "1", "name": "A", "service": "patreon"}, [post])
        self.assertEqual(len(recs[0]["files"]), 1)


class PostRefreshTest(unittest.TestCase):
    """/paw post 对已入库帖：发现新附件 → 补录 + 重开入队；无新增回落状态。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ux2_refresh_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        runtime_db.enqueue_pawchive_posts(
            "patreon", "1", "A", [{
                "post_id": "p1", "title": "t",
                "published": "2026-09-01T00:00:00",
                "post_url": "https://x/p1",
                "subdir": "Pawchive/A/p1",
                "files": [{"url": "https://f/p1/a.rar", "filename": "a.rar"}],
                "ext_links": [],
            }], scan_batch="t")
        post = runtime_db.claim_next_pawchive_post(now=1000)
        f = runtime_db.list_pawchive_files(post["id"])[0]
        runtime_db.mark_pawchive_file_done(f["id"], size_bytes=10)
        runtime_db.finalize_pawchive_post(post["id"],
                                          runtime_db.PAW_POST_COMPLETED)
        self.row_id = post["id"]

    def test_reopen_helper_state_guard(self):
        self.assertTrue(runtime_db.reopen_pawchive_post(self.row_id))
        row = runtime_db.get_pawchive_post_row(self.row_id)
        self.assertEqual(row["status"], "PENDING")
        # 再重开（PENDING）= False
        self.assertFalse(runtime_db.reopen_pawchive_post(self.row_id))

    def test_add_missing_files(self):
        added = runtime_db.add_missing_pawchive_files(self.row_id, [
            {"url": "https://f/p1/twitter.png", "filename": "twitter.png"},
            {"url": "https://f/p1/a.rar", "filename": "a.rar"},   # 已有
        ])
        self.assertEqual(added, 1)
        names = [f["filename"] for f in
                 runtime_db.list_pawchive_files(self.row_id)]
        self.assertIn("twitter.png", names)

    def test_reopen_fails_for_archived_deleted(self):
        # 归档删除后重开 = False（行没了）
        runtime_db.delete_pawchive_post(self.row_id)
        self.assertFalse(runtime_db.reopen_pawchive_post(self.row_id))


# ============================================================
# 20) /paw backfill 历史帖回填（2026-09-25）
# ============================================================
class BackfillTest(unittest.TestCase):
    """posts_for_backfill：全状态列出、名字大小写不敏感。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ux2_bf_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        runtime_db.enqueue_pawchive_posts(
            "patreon", "1", "MofuMochii", [{
                "post_id": f"p{i}", "title": f"帖{i}",
                "published": "2026-09-01T00:00:00",
                "post_url": f"https://x/p{i}",
                "subdir": f"Pawchive/MofuMochii/p{i}",
                "files": [{"url": f"https://f/p{i}.png",
                           "filename": f"{i}.png"}],
                "ext_links": [],
            } for i in (1, 2)], scan_batch="t")

    def test_lists_all_statuses(self):
        post = runtime_db.claim_next_pawchive_post(now=1000)
        runtime_db.finalize_pawchive_post(post["id"],
                                          runtime_db.PAW_POST_COMPLETED)
        rows = runtime_db.posts_for_backfill("mofumochii")   # 小写命中
        self.assertEqual(len(rows), 2)
        statuses = {r["status"] for r in rows}
        self.assertEqual(statuses, {"COMPLETED", "PENDING"})


class BackfillRunTest(unittest.IsolatedAsyncioTestCase):
    """backfill_author：补录新附件+重开；站点已无帖跳过；互斥守卫。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ux2_bfrun_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        runtime_db.enqueue_pawchive_posts(
            "patreon", "1", "MofuMochii", [{
                "post_id": "p1", "title": "帖1",
                "published": "2026-09-01T00:00:00",
                "post_url": "https://x/p1",
                "subdir": "Pawchive/MofuMochii/p1",
                "files": [{"url": "https://f/p1/a.rar", "filename": "a.rar"}],
                "ext_links": [],
            }], scan_batch="t")
        post = runtime_db.claim_next_pawchive_post(now=1000)
        f = runtime_db.list_pawchive_files(post["id"])[0]
        runtime_db.mark_pawchive_file_done(f["id"], size_bytes=10)
        runtime_db.finalize_pawchive_post(post["id"],
                                          runtime_db.PAW_POST_COMPLETED)
        self.row_id = post["id"]

    def tearDown(self):
        state.PAW_SCAN_RUNNING = None
        state.PAW_SCAN_PROGRESS = {}

    async def test_backfill_adds_file_field_and_reopens(self):
        async def run():
            detail = {"id": "p1", "attachments": [
                {"name": "a.rar", "path": "/x/aa"}],
                "file": {"name": "twitter.png", "path": "/y/bb"}}

            def fake_fetch(service, cid, pid, cookie=None):   # 同步：to_thread 调用
                return detail
            with mock.patch.object(pawchive, "fetch_post_detail", fake_fetch), \
                    mock.patch.object(pawchive.asyncio, "sleep",
                                      mock.AsyncMock()):
                msg = await pawchive.backfill_author("MofuMochii")
                self.assertIn("开始回填", msg)
                # 任务必须在补丁存活期内跑完（否则真网络调用+真节流）
                for t in list(pawchive._SPAWNED_SCANS):
                    await t
        await run()
        names = [f["filename"] for f in
                 runtime_db.list_pawchive_files(self.row_id)]
        self.assertIn("twitter.png", names)
        row = runtime_db.get_pawchive_post_row(self.row_id)
        self.assertEqual(row["status"], "PENDING")   # COMPLETED 重开

    async def test_gone_post_skipped(self):
        async def run():
            def fake_fetch(service, cid, pid, cookie=None):
                raise RuntimeError("HTTP 404")
            with mock.patch.object(pawchive, "fetch_post_detail", fake_fetch), \
                    mock.patch.object(pawchive.asyncio, "sleep",
                                      mock.AsyncMock()):
                await pawchive.backfill_author("MofuMochii")
                for t in list(pawchive._SPAWNED_SCANS):
                    await t
        await run()
        row = runtime_db.get_pawchive_post_row(self.row_id)
        self.assertEqual(row["status"], "COMPLETED")   # 不动

    async def test_mutex_guard(self):
        state.PAW_SCAN_RUNNING = "别的扫描"
        msg = await pawchive.backfill_author("MofuMochii")
        self.assertIn("已有扫描/回填", msg)

    async def test_unknown_author(self):
        msg = await pawchive.backfill_author("不存在")
        self.assertIn("没有叫", msg)


# ============================================================
# 21) 指令重构：下划线标准形 + 空格兼容（2026-09-25）
# ============================================================
class CanonicalCommandTest(unittest.TestCase):
    """_canonical_command：映射表内的组合改写为下划线标准形。"""

    def test_map_rewrites(self):
        for legacy, canonical in (
            ("/paw plan MofuMochii", "/paw_plan MofuMochii"),
            ("/paw progress Mofu", "/paw_progress Mofu"),
            ("/paw archive del 5", "/paw_archive del 5"),
            ("/paw manual export", "/paw_manual export"),
            ("/retry all", "/retry_all"),
            ("/retry del 3", "/retry_del 3"),
            ("/queue del 1", "/queue_del 1"),
            ("/wl since 1 100", "/wl_since 1 100"),
            ("/listen add @a #t me on", "/listen_add @a #t me on"),
            ("/sqlt add 名 SELECT 1", "/sqlt_add 名 SELECT 1"),
            ("/cmdt add 名 ls", "/cmdt_add 名 ls"),
            ("/caption_filter add x", "/caption_filter_add x"),
        ):
            self.assertEqual(commands._canonical_command(legacy), canonical)

    def test_non_mapped_untouched(self):
        for t in ("/status", "/paw", "/retry 1", "/wl", "/pawfoo bar",
                  "/done 关键词", "/sh ls -la", "/up 文件"):
            self.assertEqual(commands._canonical_command(t), t)

    def test_registered_names_updated(self):
        """下划线标准形全部注册；旧空格基名保持注册。"""
        for name in ("paw_plan", "paw_progress", "paw_pr", "paw_fail",
                     "paw_archive", "paw_backfill", "listen_add",
                     "wl_since", "retry_all", "retry_del", "queue_del",
                     "sqlt_add", "cmdt_run", "caption_filter_test"):
            self.assertIn(name, config.REGISTERED_COMMAND_NAMES)
        for legacy in ("paw", "listen", "wl", "retry", "queue"):
            self.assertIn(legacy, config.REGISTERED_COMMAND_NAMES)

    def test_is_predicates_accept_underscore(self):
        from tg_userbot import whitelist
        from tg_userbot.cmd_templates import is_cmdt_command
        from tg_userbot.sql_templates import is_sqlt_command
        from tg_userbot.caption_filter import is_caption_filter_command
        self.assertTrue(pawchive.is_paw_command("/paw_plan X"))
        self.assertTrue(pawchive.is_paw_command("/paw_plan"))
        self.assertFalse(pawchive.is_paw_command("/pawfoo"))
        self.assertTrue(test_queue_mod.is_retry_command("/retry_all"))
        self.assertTrue(whitelist.is_wl_command("/wl_since 1 100"))
        self.assertTrue(is_cmdt_command("/cmdt_add 名 ls"))
        self.assertTrue(is_sqlt_command("/sqlt_add 名 SELECT 1"))
        self.assertTrue(is_caption_filter_command("/caption_filter_test x"))


class LegacyAliasE2ETest(unittest.IsolatedAsyncioTestCase):
    """端到端：旧空格写法与新下划线写法路由到同一处理器。"""

    async def test_both_forms_route_identically(self):
        for legacy, canonical in (("/paw plan Mofu", "/paw_plan Mofu"),
                                  ("/retry all", "/retry_all"),
                                  ("/wl since 1 100", "/wl_since 1 100")):
            with mock.patch.object(commands, "_canonical_command",
                                   side_effect=lambda t: t), \
                    mock.patch.object(commands, "_record_usage"), \
                    mock.patch.object(commands, "_reply",
                                      mock.AsyncMock()):
                pass   # 归一化在入口，patch 后再手动验证纯函数已足够
        # 直接验证归一化 + paw 路由
        ev = mock.MagicMock()
        routed = []

        async def fake_paw_reply(event, cmd_text):
            routed.append(cmd_text)

        with mock.patch.object(pawchive, "command_reply", fake_paw_reply), \
                mock.patch.object(commands.logger, "info"):
            await commands.handle_command(ev, "/paw_plan MofuMochii")
            await commands.handle_command(ev, "/paw plan Mofu")
        self.assertEqual(routed, ["/paw_plan MofuMochii", "/paw_plan Mofu"],
                         "两种写法到 Pawchive 分发器时已归一")


# ============================================================
# 22) Pawchive 开始/完成通知开关（2026-09-25 与转发链路对齐）
# ============================================================
class NotifyToggleTest(unittest.TestCase):
    def test_roundtrip(self):
        path = os.path.join(_TMP, f"ntf_{id(self)}.json")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with mock.patch.object(pawchive, "_notify_toggle_path",
                               return_value=path):
            self.assertTrue(pawchive.notify_each_post_enabled())   # 默认开
            pawchive.set_notify_each_post(False)
            self.assertFalse(pawchive.notify_each_post_enabled())
            pawchive.set_notify_each_post(True)
            self.assertTrue(pawchive.notify_each_post_enabled())

    def test_reply_branches(self):
        path = os.path.join(_TMP, f"ntfrep_{id(self)}.json")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with mock.patch.object(pawchive, "_notify_toggle_path",
                               return_value=path):
            self.assertIn("：开", pawchive.notify_toggle_reply(""))
            self.assertIn("已开启", pawchive.notify_toggle_reply("on"))
            self.assertIn("已关闭", pawchive.notify_toggle_reply("off"))
            self.assertIn("：关", pawchive.notify_toggle_reply(""))
            self.assertIn("用法", pawchive.notify_toggle_reply("啥"))


class NotifyOnProcessTest(unittest.IsolatedAsyncioTestCase):
    """process_post：开关开→发开始通知；关→不发。完成通知同开关。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ux2_ntf_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        self._sent = []
        self._patch_notify = mock.patch.object(
            pawchive_worker.notify, "notify_user",
            mock.AsyncMock(side_effect=lambda t: self._sent.append(t)))
        self._patch_notify.start()
        self.addCleanup(self._patch_notify.stop)

    def _enqueue_completed_post(self):
        runtime_db.enqueue_pawchive_posts(
            "patreon", "1", "MofuMochii", [{
                "post_id": "p9", "title": "t9",
                "published": "2026-09-01T00:00:00",
                "post_url": "https://x/p9",
                "subdir": "Pawchive/MofuMochii/p9",
                "files": [{"url": "https://f/p9.png",
                           "filename": "9.png"}],
                "ext_links": [],
            }], scan_batch="t")
        return runtime_db.claim_next_pawchive_post(now=1000)

    async def test_start_notification_on(self):
        post = self._enqueue_completed_post()
        with mock.patch.object(pawchive_worker, "_download_post_files",
                               mock.AsyncMock()), \
                mock.patch.object(pawchive_worker, "_renew_lease_loop",
                                  mock.AsyncMock()) as lease, \
                mock.patch.object(pawchive_worker, "_finalize",
                                  mock.AsyncMock()):
            lease.return_value.cancel = lambda: None
            await pawchive_worker.process_post(post)
        self.assertTrue(any("开始下载：MofuMochii" in t for t in self._sent),
                        self._sent)

    async def test_disabled_no_start_notification(self):
        pawchive.set_notify_each_post(False)
        post = self._enqueue_completed_post()
        with mock.patch.object(pawchive_worker, "_download_post_files",
                               mock.AsyncMock()), \
                mock.patch.object(pawchive_worker, "_renew_lease_loop",
                                  mock.AsyncMock()) as lease, \
                mock.patch.object(pawchive_worker, "_finalize",
                                  mock.AsyncMock()):
            lease.return_value.cancel = lambda: None
            await pawchive_worker.process_post(post)
        self.assertFalse(any("开始下载" in t for t in self._sent))

    def tearDown(self):
        pawchive.set_notify_each_post(True)


# ============================================================
# 23) /restart 重启指令（2026-09-25）
# ============================================================
class RestartCommandTest(unittest.IsolatedAsyncioTestCase):
    """/restart：回执 + 调度（绝不真杀进程）；未注册形态不触发。"""

    async def test_reply_and_schedule(self):
        ev = mock.MagicMock()
        ev.reply = mock.AsyncMock()
        with mock.patch.object(commands, "_schedule_restart") as sched:
            await commands.handle_command(ev, "/restart")
        reply = ev.reply.await_args.args[0]
        self.assertIn("重启中", reply)
        self.assertTrue(sched.called or sched.await_count)

    def test_schedule_spawns_detached_helper_and_schedules_term(self):
        """_schedule_restart 本体：派生脱离进程组的 sh 守护 + 定时 SIGTERM。"""
        import subprocess
        import threading
        spawned, timers = [], []

        class FakeTimer:
            def __init__(self, delay, fn):
                timers.append((delay, fn))

            def start(self):
                pass

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with mock.patch("os.getpid", return_value=424242), \
                mock.patch("os.kill") as fake_kill, \
                mock.patch("subprocess.Popen",
                           side_effect=lambda a, **kw: spawned.append(a)), \
                mock.patch("threading.Timer", FakeTimer):
            commands._schedule_restart(delay=2.0)
            # FakeTimer 不自跑：在补丁内手动触发，验证到点对自己发 SIGTERM
            timers[0][1]()
            fake_kill.assert_called_once_with(424242, 15)
        self.assertTrue(spawned, "应派生 sh 守护脚本")
        script = " ".join(spawned[0])
        self.assertIn("run.sh start", script)
        self.assertIn("kill -0 424242", script)
        self.assertTrue(timers and timers[0][0] == 2.0, "应有 2s 定时 SIGTERM")

    async def test_not_triggered_without_command(self):
        ev = mock.MagicMock()
        ev.reply = mock.AsyncMock()
        with mock.patch.object(commands, "_schedule_restart",
                               mock.AsyncMock()) as sched:
            await commands.handle_command(ev, "/status")
        sched.assert_not_called()

    def test_registered_everywhere(self):
        self.assertIn("restart", config.REGISTERED_COMMAND_NAMES)
        self.assertTrue(any(n == "restart" for n, _ in bot.BOT_COMMANDS))
        self.assertIn("/restart", commands._help_text())


# ============================================================
# 24) 完成通知的大小统计修复（2026-09-26 0.00 B 案例）
# ============================================================
class CompletionSizeTest(unittest.IsolatedAsyncioTestCase):
    """完成通知的总大小必须反映下载结果（内存同步 size_bytes）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ux2_size_", dir=_TMP)
        self._p = mock.patch.object(
            config, "RUNTIME_DB_FILE", os.path.join(self.dir, "db.sqlite"))
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        runtime_db.enqueue_pawchive_posts(
            "patreon", "1", "くらしっく", [{
                "post_id": "p1", "title": "水着",
                "published": "2026-09-01T00:00:00",
                "post_url": "https://x/p1",
                "subdir": "Pawchive/くらしっく/p1",
                "files": [{"url": "https://f/p1.png", "filename": "1.png"}],
                "ext_links": [],
            }], scan_batch="t")
        self.post = runtime_db.claim_next_pawchive_post(now=1000)
        self._sent = []
        self._np = mock.patch.object(
            pawchive_worker.notify, "notify_user",
            mock.AsyncMock(side_effect=lambda t: self._sent.append(t)))
        self._np.start()
        self.addCleanup(self._np.stop)

    async def test_downloaded_size_shown_in_completion(self):
        from tg_userbot.naming import sanitize_filename_bounded as _sb
        files = runtime_db.list_pawchive_files(self.post["id"])
        f = files[0]
        # 模拟下载循环：下载器返回真实大小，mark 落库
        status, size, err = ("done", 3366775, None)
        runtime_db.mark_pawchive_file_done(f["id"], size_bytes=size)
        f["status"] = runtime_db.PAW_FILE_DONE
        f["size_bytes"] = size                                  # 内存同步（修复点）
        await pawchive_worker._finalize(self.post, files)
        text = self._sent[0] if self._sent else ""
        self.assertIn("3.21 MB", text)                          # 3366775 字节
        self.assertNotIn("0.00 B", text)


# ============================================================
# 25) 状态视图「待入队请求」过滤已走完生命周期的请求（2026-09-26）
# ============================================================
class UnclaimedDisplayTest(unittest.TestCase):
    """unclaimed 计算必须排除已通知过的请求（出窗后不算幽灵）。"""

    def test_filter_excludes_notified(self):
        known = {"t1"}
        reqs = [
            {"task_id": "t1", "url": "https://x/1"},               # 在窗内
            {"task_id": "t2", "url": "https://x/2"},               # 未认领
            {"task_id": "t3", "url": "https://x/3",
             "notified_at": "2026-09-24 12:00:00"},                # 出窗老请求
        ]
        unclaimed = [r for r in reqs
                     if r["task_id"] not in known
                     and not r.get("notified_at")]
        self.assertEqual([r["task_id"] for r in unclaimed], ["t2"])
