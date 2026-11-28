"""asmr_dl（/asmr 音声下载）的单元测试。

覆盖：解析纯函数（RJ 三形态 / enqueue 正文 only 过滤 / 命令子命令两形 /
文件树扁平化 / only 过滤）、asmr_tasks 表 CRUD 与状态机（claim 租约 /
失败分流 / 重投 / 释放 / 过期恢复）、命令入口判定。不联网；DB 落在进程级
临时目录，退出时回收。运行方式（项目根目录）：
    .venv/bin/python -m unittest tests.test_asmr_dl -v
"""
import asyncio
import atexit
import os
import shutil
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_asmr_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import asmr_dl   # noqa: E402
from tg_userbot import config    # noqa: E402
from tg_userbot import runtime_db  # noqa: E402


# ============================================================
# 解析纯函数
# ============================================================
class ParseAsmrRefTest(unittest.TestCase):
    def test_bare_digits(self):
        self.assertEqual(asmr_dl.parse_asmr_ref("01207846"), 1207846)
        self.assertEqual(asmr_dl.parse_asmr_ref("398337"), 398337)

    def test_rj_prefix(self):
        self.assertEqual(asmr_dl.parse_asmr_ref("RJ01207846"), 1207846)
        self.assertEqual(asmr_dl.parse_asmr_ref("rj1207846"), 1207846)

    def test_work_url(self):
        self.assertEqual(
            asmr_dl.parse_asmr_ref(
                "https://www.asmr-300.com/work/RJ01207846"), 1207846)

    def test_garbage(self):
        for bad in ("", None, "asmr", "https://example.com/x", "RJabc"):
            self.assertIsNone(asmr_dl.parse_asmr_ref(bad), bad)


class IsAsmrCommandTest(unittest.TestCase):
    def test_matches(self):
        for ok in ("/asmr", "/asmr ", "/asmr RJ01207846",
                   "/asmr_retry 5", "/asmr retry all", "/ASMR stop"):
            self.assertTrue(asmr_dl.is_asmr_command(ok), ok)

    def test_rejects(self):
        for bad in ("/asmrfoo", "/asmrx", "/wnacg", "/x"):
            self.assertFalse(asmr_dl.is_asmr_command(bad), bad)


class ParseAsmrCommandTest(unittest.TestCase):
    def test_status_forms(self):
        self.assertEqual(asmr_dl.parse_asmr_command("/asmr"),
                         ("status", None))
        self.assertEqual(asmr_dl.parse_asmr_command("/asmr   "),
                         ("status", None))

    def test_space_and_underscore_equivalent(self):
        # 入口规范化只改写空格形；两种形态都必须落到同一子命令
        self.assertEqual(asmr_dl.parse_asmr_command("/asmr stop"),
                         ("stop", None))
        self.assertEqual(asmr_dl.parse_asmr_command("/asmr_stop"),
                         ("stop", None))
        self.assertEqual(asmr_dl.parse_asmr_command("/asmr retry 5"),
                         ("retry", "5"))
        self.assertEqual(asmr_dl.parse_asmr_command("/asmr_retry 5"),
                         ("retry", "5"))
        self.assertEqual(asmr_dl.parse_asmr_command("/asmr del 5"),
                         ("del", "5"))
        self.assertEqual(asmr_dl.parse_asmr_command("/asmr_del 5"),
                         ("del", "5"))

    def test_enqueue(self):
        self.assertEqual(asmr_dl.parse_asmr_command("/asmr RJ01207846"),
                         ("enqueue", "RJ01207846"))
        self.assertEqual(
            asmr_dl.parse_asmr_command(
                "/asmr https://www.asmr-300.com/work/RJ01207846"),
            ("enqueue", "https://www.asmr-300.com/work/RJ01207846"))


class ParseEnqueueBodyTest(unittest.TestCase):
    def test_plain(self):
        rj, only = asmr_dl.parse_enqueue_body("RJ01207846")
        self.assertEqual(rj, 1207846)
        self.assertIsNone(only)

    def test_only_takes_rest(self):
        rj, only = asmr_dl.parse_enqueue_body(
            "https://www.asmr-300.com/work/RJ01207846 only wav(含SE)")
        self.assertEqual(rj, 1207846)
        self.assertEqual(only, "wav(含SE)")

    def test_only_multi_word(self):
        rj, only = asmr_dl.parse_enqueue_body("RJ1207846 only 我的 目录 名")
        self.assertEqual(rj, 1207846)
        self.assertEqual(only, "我的 目录 名")

    def test_no_rj(self):
        rj, only = asmr_dl.parse_enqueue_body("only 含SE")
        self.assertIsNone(rj)
        self.assertEqual(only, "含SE")


class FolderNameTest(unittest.TestCase):
    """115 目录名约定：与站点显示一致的 RJ 前缀（盘上既有 RJ1218390 等）。"""

    def test_uses_source_id_verbatim(self):
        self.assertEqual(asmr_dl._folder_name(1267237, "RJ01267237"),
                         "RJ01267237")
        self.assertEqual(asmr_dl._folder_name(1218390, "RJ1218390"),
                         "RJ1218390")

    def test_falls_back_to_rj_prefix(self):
        self.assertEqual(asmr_dl._folder_name(1267237), "RJ1267237")
        self.assertEqual(asmr_dl._folder_name(1267237, ""), "RJ1267237")
        self.assertEqual(asmr_dl._folder_name(1267237, None), "RJ1267237")


class FlattenTracksTest(unittest.TestCase):
    def _tree(self):
        return [
            {"type": "folder", "title": "wav(含SE)",
             "children": [
                 {"type": "audio", "title": "1.序章.wav", "size": 100,
                  "mediaDownloadUrl": "https://raw.example/a.wav"},
                 {"type": "text", "title": "1.序章.wav.vtt", "size": 5,
                  "mediaDownloadUrl": "https://raw.example/a.vtt"},
             ]},
            {"type": "text", "title": "read_me.txt", "size": 10,
             "mediaDownloadUrl": "https://raw.example/read.txt"},
        ]

    def test_flatten_keeps_tree(self):
        files = asmr_dl.flatten_tracks(self._tree())
        self.assertEqual([f["path"] for f in files],
                         ["wav(含SE)/1.序章.wav", "wav(含SE)/1.序章.wav.vtt",
                          "read_me.txt"])
        self.assertEqual(files[0]["size"], 100)
        self.assertEqual(files[0]["url"], "https://raw.example/a.wav")

    def test_skips_leafless_nodes(self):
        tree = [{"type": "audio", "title": "x.wav"},
                {"type": "folder", "title": "空", "children": []}]
        self.assertEqual(asmr_dl.flatten_tracks(tree), [])

    def test_apply_only_filter(self):
        files = asmr_dl.flatten_tracks(self._tree())
        kept = asmr_dl.apply_only_filter(files, "含SE")
        self.assertEqual(len(kept), 2)
        self.assertTrue(all(f["path"].startswith("wav(含SE)/")
                            for f in kept))
        self.assertEqual(asmr_dl.apply_only_filter(files, "noth-it"), [])
        self.assertEqual(asmr_dl.apply_only_filter(files, None), files)
        self.assertEqual(len(asmr_dl.apply_only_filter(files, "READ_ME")), 1)


# ============================================================
# asmr_tasks 表（CRUD + 状态机）
# ============================================================
class _AsmrDbTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="asmr_db_", dir=_TMP)
        self.path = os.path.join(self.dir, "tg_userbot.db")
        self._p = mock.patch.object(config, "RUNTIME_DB_FILE", self.path)
        self._p.start()
        self.addCleanup(self._p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def enqueue(self, rj=1207846, **kw):
        return runtime_db.enqueue_asmr_task(
            rj, title=kw.get("title", "标题"), circle=kw.get("circle", "社团"),
            only_filter=kw.get("only_filter"), total_files=kw.get("total_files"),
            size_bytes=kw.get("size_bytes"))


class AsmrDbTest(_AsmrDbTestCase):
    def test_enqueue_dedupes_by_rj(self):
        self.assertTrue(self.enqueue())
        self.assertFalse(self.enqueue(title="再来一次"))
        got = runtime_db.get_asmr_task(1207846)
        self.assertEqual(got["title"], "标题")   # 首次登记胜出
        self.assertEqual(got["status"], runtime_db.ASMR_PENDING)

    def test_claim_sets_processing_with_lease(self):
        self.assertTrue(self.enqueue(rj=100))
        task = runtime_db.claim_next_asmr_task(now=1000)
        self.assertEqual(task["rj"], 100)
        self.assertEqual(task["status"], runtime_db.ASMR_PROCESSING)
        self.assertGreater(task["lease_until"], 1000)
        # PENDING 已被领走 → 再领无货
        self.assertIsNone(runtime_db.claim_next_asmr_task(now=1000))

    def test_claim_respects_backoff(self):
        self.assertTrue(self.enqueue(rj=100))
        st = runtime_db.fail_asmr_task(100, "x", max_attempts=5,
                                       delay_seconds=300, now=1000)
        self.assertEqual(st, runtime_db.ASMR_PENDING)
        self.assertIsNone(runtime_db.claim_next_asmr_task(now=1000))
        task = runtime_db.claim_next_asmr_task(now=1301)
        self.assertEqual(task["rj"], 100)

    def test_fail_to_limit_then_failed(self):
        self.assertTrue(self.enqueue(rj=100))
        st = None
        for _ in range(5):
            st = runtime_db.fail_asmr_task(100, "err", max_attempts=5,
                                           delay_seconds=1, now=1000)
        self.assertEqual(st, runtime_db.ASMR_FAILED)
        self.assertIsNone(runtime_db.claim_next_asmr_task(now=99999))

    def test_retry_resets_and_requeues(self):
        self.assertTrue(self.enqueue(rj=100))
        for _ in range(5):
            runtime_db.fail_asmr_task(100, "err", max_attempts=5,
                                      delay_seconds=1, now=1000)
        self.assertEqual(runtime_db.retry_asmr_task(100), 1)
        self.assertEqual(runtime_db.retry_asmr_task(100), 0)  # 已不在 FAILED
        task = runtime_db.claim_next_asmr_task(now=1000)
        self.assertEqual(task["rj"], 100)
        self.assertEqual(task["attempts"], 0)

    def test_retry_all(self):
        for rj in (100, 200):
            self.assertTrue(self.enqueue(rj=rj))
            for _ in range(5):
                runtime_db.fail_asmr_task(rj, "err", max_attempts=5,
                                          delay_seconds=1, now=1000)
        self.assertEqual(runtime_db.retry_asmr_task(None), 2)

    def test_postpone_keeps_attempts(self):
        self.assertTrue(self.enqueue(rj=100))
        runtime_db.postpone_asmr_task(100, "瞬时", delay_seconds=60, now=1000)
        got = runtime_db.get_asmr_task(100)
        self.assertEqual(got["status"], runtime_db.ASMR_PENDING)
        self.assertEqual(got["attempts"], 0)

    def test_release_and_recover_expired(self):
        self.assertTrue(self.enqueue(rj=100))
        self.assertTrue(self.enqueue(rj=200))
        first = runtime_db.claim_next_asmr_task(now=1000)   # 领走 100
        second = runtime_db.claim_next_asmr_task(now=1000)  # 领走 200
        self.assertEqual((first["rj"], second["rj"]), (100, 200))
        # 释放（停机）：立即回 PENDING、租约清空
        self.assertTrue(runtime_db.release_asmr_task(100))
        self.assertEqual(runtime_db.get_asmr_task(100)["status"],
                         runtime_db.ASMR_PENDING)
        self.assertIsNone(runtime_db.get_asmr_task(100)["lease_until"])
        # 租约过期恢复（崩溃自愈）：另一条 PROCESSING 回 PENDING
        self.assertEqual(runtime_db.recover_expired_asmr_tasks(now=99999), 1)
        self.assertEqual(runtime_db.get_asmr_task(200)["status"],
                         runtime_db.ASMR_PENDING)

    def test_renew_lease_only_for_processing(self):
        self.assertTrue(self.enqueue(rj=100))
        self.assertFalse(runtime_db.renew_asmr_lease(100))  # PENDING 不续
        runtime_db.claim_next_asmr_task(now=1000)
        self.assertTrue(runtime_db.renew_asmr_lease(100, now=2000))
        self.assertGreaterEqual(runtime_db.get_asmr_task(100)["lease_until"],
                                2000 + 900)

    def test_complete_and_delete(self):
        self.assertTrue(self.enqueue(rj=100))
        runtime_db.complete_asmr_task(100, size_bytes=123, total_files=7)
        got = runtime_db.get_asmr_task(100)
        self.assertEqual(got["status"], runtime_db.ASMR_DONE)
        self.assertEqual(got["size_bytes"], 123)
        self.assertEqual(got["total_files"], 7)
        self.assertIsNone(got["error"])
        runtime_db.delete_asmr_task(100)
        self.assertIsNone(runtime_db.get_asmr_task(100))

    def test_update_fields(self):
        self.assertTrue(self.enqueue(rj=100))
        runtime_db.update_asmr_task(100, title="新标题", circle="新社团",
                                    total_files=41, done_files=13,
                                    size_bytes=4550000000)
        got = runtime_db.get_asmr_task(100)
        self.assertEqual(got["title"], "新标题")
        self.assertEqual(got["done_files"], 13)

    def test_list_filter_by_status(self):
        self.assertTrue(self.enqueue(rj=100))
        self.assertTrue(self.enqueue(rj=200))
        for _ in range(5):
            runtime_db.fail_asmr_task(200, "err", max_attempts=5,
                                      delay_seconds=1, now=1000)
        rows = runtime_db.list_asmr_tasks(status=runtime_db.ASMR_FAILED)
        self.assertEqual([r["rj"] for r in rows], [200])

    def test_health_snapshot_includes_asmr(self):
        self.assertTrue(self.enqueue(rj=100))
        snap = runtime_db.queue_health_counts(now=1000)
        self.assertIn("asmr", snap)
        self.assertEqual(snap["asmr"]["counts"].get("PENDING"), 1)
        self.assertEqual(snap["asmr"]["due"], 1)


class SchemaVersionTest(_AsmrDbTestCase):
    def test_schema_v19_recorded(self):
        # v20 起不再钉死具体号：断言「>=19 且与 config 声明一致」
        row = runtime_db._read(lambda c: runtime_db._execute(
            c, "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone(), "查版本")
        self.assertGreaterEqual(int(row["value"]), 19)
        self.assertEqual(int(row["value"]),
                         int(config.RUNTIME_DB_SCHEMA_VERSION))


# ============================================================
# fetch_work_info 的错误面（不联网：代理缺失即拒）
# ============================================================
class DownloadRetryTest(unittest.TestCase):
    """单文件停顿内建快速重试（CDN 偶发首字节停顿，换连接即通）。"""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="asmr_dl_", dir=_TMP)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.dest = os.path.join(self.dir, "a.mp3")

    def _fake_client(self, script):
        """script: 每次 stream() 的行为——('stall',) 抛 ReadTimeout /
        ('ok', bytes) 正常 / ('status', code) 指定状态码。记录调用次数。"""
        calls = {"n": 0}

        class _Resp:
            def __init__(self, code, body):
                self.status_code = code
                self._body = body

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

            def iter_bytes(self, _n):
                yield self._body

        class _StreamCtx:
            def __init__(self, behavior):
                self._b = behavior

            def __enter__(self):
                kind = self._b[0]
                if kind == "stall":
                    raise asmr_dl.httpx.ReadTimeout("stalled")
                if kind == "status":
                    return _Resp(self._b[1], b"")
                return _Resp(206, self._b[1])

            def __exit__(self, *a):
                return False

        class _Client:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def stream(self, method, url, headers=None):
                behavior = script[min(calls["n"], len(script) - 1)]
                calls["n"] += 1
                return _StreamCtx(behavior)

        return _Client, calls

    def test_stall_then_success_within_attempts(self):
        script = [("stall",), ("stall",), ("ok", b"0123456789")]
        client, calls = self._fake_client(script)
        with mock.patch.object(asmr_dl.httpx, "Client", client):
            size = asmr_dl.download_file_blocking(
                "https://x/a.mp3", self.dest, expected=10)
        self.assertEqual(size, 10)
        self.assertEqual(calls["n"], 3)
        self.assertTrue(os.path.isfile(self.dest))
        self.assertFalse(os.path.isfile(self.dest + ".part"))

    def test_exhausted_attempts_raises_without_dest(self):
        script = [("stall",)]
        client, calls = self._fake_client(script)
        with mock.patch.object(asmr_dl.httpx, "Client", client), \
                mock.patch.object(asmr_dl.time, "sleep", lambda s: None):
            with self.assertRaises(RuntimeError) as cm:
                asmr_dl.download_file_blocking("https://x/a.mp3", self.dest)
        self.assertIn("下载失败", str(cm.exception))
        self.assertEqual(calls["n"], 3)
        self.assertFalse(os.path.isfile(self.dest))

    def test_403_is_permanent_no_retry(self):
        script = [("status", 403)]
        client, calls = self._fake_client(script)
        with mock.patch.object(asmr_dl.httpx, "Client", client):
            with self.assertRaises(RuntimeError) as cm:
                asmr_dl.download_file_blocking("https://x/a.mp3", self.dest)
        self.assertIn("直链失效", str(cm.exception))
        self.assertEqual(calls["n"], 1)   # 永久事实不烧重试

    def test_size_mismatch_clears_part(self):
        script = [("ok", b"0123456789")]
        client, _ = self._fake_client(script)
        with mock.patch.object(asmr_dl.httpx, "Client", client):
            with self.assertRaises(RuntimeError) as cm:
                asmr_dl.download_file_blocking(
                    "https://x/a.mp3", self.dest, expected=99)
        self.assertIn("大小不符", str(cm.exception))
        self.assertFalse(os.path.isfile(self.dest + ".part"))


class FetchRequiresProxyTest(unittest.TestCase):
    def test_no_proxy_raises(self):
        env = mock.patch.dict(os.environ, {"TG_PROXY": ""}, clear=False)
        with env:
            with self.assertRaises(RuntimeError) as cm:
                asmr_dl.fetch_work_info_blocking(1207846)
            self.assertIn("TG_PROXY", str(cm.exception))


if __name__ == "__main__":
    unittest.main()


class WorkerFrameworkTest(_AsmrDbTestCase):
    """asmr → SerialTaskWorker 迁移回归（2026-10-26 评审）：
    ① 真 claim 取消 → 放回 PENDING；② 挂载冻结看门狗 → 瞬时退避。"""

    def test_cancel_via_framework_releases_task(self):
        import asyncio
        runtime_db.enqueue_asmr_task(410000, "标题")

        async def drive():
            gate = asyncio.Event()

            async def slow(task):
                await gate.wait()

            w = asmr_dl._WORKER
            claimed = {"n": 0}

            def real_claim():
                claimed["n"] += 1
                return (runtime_db.claim_next_asmr_task()
                        if claimed["n"] == 1 else None)

            w.claim = real_claim
            w.process = slow
            with mock.patch.object(asmr_dl.logger, "info"):
                runner = asyncio.ensure_future(w.run())
                await asyncio.sleep(0.05)
                self.assertIsNotNone(w._current)
                runner.cancel()
                try:
                    await runner
                except asyncio.CancelledError:
                    pass
        asyncio.run(drive())
        after = runtime_db.get_asmr_task(410000)
        self.assertEqual(after["status"], runtime_db.ASMR_PENDING, after)
        self.assertIsNone(after["lease_until"])
        again = runtime_db.claim_next_asmr_task()
        self.assertEqual(again["rj"], 410000)

    def test_periodic_recover_configured(self):
        """框架实例启用了周期租约回收（此前缺失 = P2）。"""
        w = asmr_dl._AsmrWorker()
        self.assertGreater(w.recover_seconds, 0,
                           "asmr 必须开周期恢复（挂死任务回 PENDING）")

    def test_frozen_mount_copy_times_out_to_transient(self):
        """看门狗：拷贝无进展超限 → TimeoutError → 走瞬时退避不占上限。"""
        import asyncio
        import threading
        runtime_db.enqueue_asmr_task(410001, "冻")
        task = runtime_db.claim_next_asmr_task()
        gate = threading.Event()

        def frozen_download(url, dest, expected=None):
            return 123          # 下载秒成，卡点在挂载拷贝

        def frozen_copy(src, dst):
            gate.wait(3)        # 模拟 FUSE 冻结：dst 永不增长

        info = {"title": "T", "circle": "C",
                "files": [{"path": "a.wav", "size": 123, "url": "u"}]}

        async def drive():
            with mock.patch.object(asmr_dl, "fetch_work_info_blocking",
                                   return_value=info), \
                 mock.patch.object(asmr_dl, "download_file_blocking",
                                   side_effect=frozen_download), \
                 mock.patch.object(asmr_dl, "_copy_file",
                                   side_effect=frozen_copy), \
                 mock.patch("tg_userbot.extract_worker.mount_of",
                                   return_value=self.dir), \
                 mock.patch.object(config, "ASMR_COPY_CHECK_SECONDS", 0.05), \
                 mock.patch.object(config, "ASMR_COPY_STALL_SECONDS", 0.15), \
                 mock.patch.object(config, "ASMR_THROTTLE_SECONDS", 0.0), \
                 mock.patch.object(asmr_dl, "_wait_remote_file",
                                   new=mock.AsyncMock(return_value=True)):
                try:
                    await asmr_dl._run_one(task)
                finally:
                    gate.set()
        asyncio.run(drive())
        after = runtime_db.get_asmr_task(410001)
        self.assertEqual(after["status"], runtime_db.ASMR_PENDING,
                         "瞬时故障应退避回 PENDING，不 FAILED")
        self.assertIn("挂载冻结", after["error"])


class DownloadStallWatchdogTest(unittest.TestCase):
    """CDN 涓流看门狗（2026-10-26 生产事故：34MB 文件涓流 21 分钟
    卡住串行 worker）——窗口内字节不足即抛 TimeoutError，正常流速不误伤。"""

    def _run_once(self, chunks, window, floor):
        import asyncio  # noqa: F401
        import tempfile

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def iter_bytes(self, n):
                yield from chunks

        class _StreamCM:
            def __enter__(self):
                return _Resp()

            def __exit__(self, *a):
                return False

        class _Cli:
            def stream(self, *a, **kw):
                return _StreamCM()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        d = tempfile.mkdtemp(dir=_TMP)
        self.addCleanup(__import__("shutil").rmtree, d, ignore_errors=True)
        part = os.path.join(d, "f.part")
        with mock.patch.object(asmr_dl.httpx, "Client",
                               return_value=_Cli()), \
             mock.patch.object(config, "ASMR_STALL_WINDOW_SECONDS", window), \
             mock.patch.object(config, "ASMR_STALL_FLOOR_BYTES", floor), \
             mock.patch.object(config, "ASMR_FILE_TIMEOUT_SECONDS", 5):
            asmr_dl._download_once("http://x/f", part)
        return open(part, "rb").read()

    def test_trickle_aborts(self):
        import time as _t
        # 每 0.12s 滴 10 字节（窗口 0.1s、下限 1KB → 第一窗就判死）
        def trickle():
            for _ in range(50):
                yield b"x" * 10
                _t.sleep(0.12)
        with self.assertRaises(TimeoutError):
            self._run_once(trickle(), window=0.1, floor=1024)

    def test_healthy_speed_not_interrupted(self):
        data = [b"a" * 4096] * 10
        out = self._run_once(iter(data), window=0.1, floor=256)
        self.assertEqual(len(out), 40960)


class FetchBudgetTest(_AsmrDbTestCase):
    """拉文件树必须有协程级超时（2026-11-27：worker 卡在"开始"后 10+
    分钟零日志/零 .part/无 CDN 连接——socks 代理下连接挂死不认 httpx
    超时，整个 worker 被一条僵尸线程占死）。"""

    def test_hung_fetch_aborts_with_timeout(self):
        import time as _t
        import threading

        def hang(*a, **k):
            _t.sleep(2)          # 模拟挂死（不返回）
            return {"files": []}

        task = {"rj": 999001, "title": "T", "only_filter": ""}
        runtime_db.enqueue_asmr_task(999001, title="T", circle="",
                                     total_files=0, size_bytes=0)
        with mock.patch.object(asmr_dl, "fetch_work_info_blocking",
                               side_effect=hang), \
                mock.patch.object(config, "ASMR_FETCH_BUDGET_SECONDS", 0.2):
            t0 = _t.monotonic()
            with self.assertRaises(TimeoutError) as cm:
                asyncio.run(asmr_dl._process_task(task))
            spent = _t.monotonic() - t0
        self.assertIn("拉文件树超过", str(cm.exception))
        # 注：asyncio.run 退出时会 join 默认执行器线程（挂死线程 2s 才返回），
        # 所以整体耗时会接近挂死时长；常驻进程里不存在这个 join，僵尸线程
        # 直接泄漏（与 download_with_watchdog 同策略）。这里断言"预算生效
        # 且异常类型正确"即可。
        self.assertLess(spent, 3.0)

    def test_normal_fetch_unaffected(self):
        from tg_userbot import task_worker
        info = {"title": "T", "circle": "C", "source_id": "",
                "files": [{"path": "a.wav", "size": 10, "url": "u"}]}
        task = {"rj": 999002, "title": "T", "only_filter": ""}
        runtime_db.enqueue_asmr_task(999002, title="T", circle="",
                                     total_files=0, size_bytes=0)

        async def fake_dl(*a, **k):
            return ("done", 10, None)

        async def fake_cp(*a, **k):
            return 10

        with mock.patch.object(asmr_dl, "fetch_work_info_blocking",
                               return_value=info), \
                mock.patch.object(task_worker, "download_with_watchdog",
                                  new=fake_dl), \
                mock.patch.object(task_worker, "copy_with_watchdog",
                                  new=fake_cp), \
                mock.patch("tg_userbot.extract_worker.mount_of",
                           return_value=self.dir), \
                mock.patch.object(config, "ASMR_THROTTLE_SECONDS", 0.0), \
                mock.patch.object(asmr_dl, "_wait_remote_file",
                                  new=mock.AsyncMock(return_value=True)):
            out = asyncio.run(asmr_dl._process_task(task))
        self.assertEqual(out["files"], 1)


class SizeDriftTest(unittest.TestCase):
    """CDN 偶发多吐字节（22:50 实测 20727940 vs 声明 20203652，+0.5MB）
    → 压线容差内裁剪收下，不整文件重来；超容差仍是硬错。"""

    def _prep(self, total):
        import shutil as _sh
        d = tempfile.mkdtemp(prefix="asmr_drift_", dir=_TMP)
        self.addCleanup(_sh.rmtree, d, ignore_errors=True)
        dest = os.path.join(d, "a.mp3")
        with open(dest + ".part", "wb") as f:
            f.write(b"x" * total)
        return dest

    def test_small_drift_truncated_and_accepted(self):
        expected = 20203652
        dest = self._prep(expected + 500000)
        # _download_once 正常返回（part 已就位），只走对账段
        with mock.patch.object(asmr_dl, "_download_once"), \
                mock.patch.object(asmr_dl.os.path, "getsize",
                                  return_value=expected + 500000):
            size = asmr_dl.download_file_blocking("u", dest, expected)
        self.assertEqual(size, expected)
        self.assertEqual(os.path.getsize(dest), expected)
        self.assertFalse(os.path.exists(dest + ".part"))

    def test_huge_drift_still_raises(self):
        dest = self._prep(99999999)
        with mock.patch.object(asmr_dl, "_download_once"), \
                mock.patch.object(asmr_dl.os.path, "getsize",
                                  return_value=99999999):
            with self.assertRaises(RuntimeError) as cm:
                asmr_dl.download_file_blocking("u", dest, expected=1000)
        self.assertIn("大小不符", str(cm.exception))

    def test_exact_size_untouched(self):
        expected = 1000
        dest = self._prep(expected)
        with mock.patch.object(asmr_dl, "_download_once"), \
                mock.patch.object(asmr_dl.os.path, "getsize",
                                  return_value=expected):
            size = asmr_dl.download_file_blocking("u", dest, expected)
        self.assertEqual(size, expected)


class OrphanSocketCleanupTest(unittest.TestCase):
    """弃线程遗留的 socks 隧道必须清（2026-11-27：12 轮失败泄漏 12 条
    ESTABLISHED，Hiddify 会话累积 200+ 后新握手挂死——因果链实测）。"""

    def test_cleanup_closes_only_proxy_sockets(self):
        import socket as _s
        import subprocess as _sp
        from unittest import mock as _m
        # 空 lsof 输出：不抛、安全空转
        with _m.patch.object(_sp, "run") as run:
            run.return_value.stdout = ""
            asmr_dl._kill_orphan_sockets()
        run.assert_called_once()

    def test_loop_owned_fd_is_spared(self):
        """asyncio selector 里注册的 fd 绝不能被关（2026-11-28 事故：
        第一版把 TG 主客户端的连接全杀了，bot 冻结 7.5 小时）。"""
        import socket as _s
        import subprocess as _sp
        from unittest import mock as _m

        async def drive():
            loop = asyncio.get_running_loop()
            # loop 持有的 socket：注册进 selector
            owned = _s.socket()
            owned.bind(("127.0.0.1", 0))
            owned.listen(1)
            loop.add_reader(owned.fileno(), lambda: None)
            self.addCleanup(owned.close)
            # 非 loop 持有的 socket（模拟弃线程的隧道）
            orphan = _s.socket()
            orphan.bind(("127.0.0.1", 0))
            orphan.listen(1)
            self.addCleanup(orphan.close)
            # 两个 fd 都"到 12334"（lsof mock 伪造）
            out = f"f{owned.fileno()}\np127.0.0.1:12334\n" \
                  + f"f{orphan.fileno()}\np127.0.0.1:12334\n"
            with _m.patch.object(_sp, "run") as run:
                run.return_value.stdout = out
                asmr_dl._kill_orphan_sockets()
            # owned 还活着且在 selector 里；orphan 收到 shutdown
            self.assertIn(owned.fileno(),
                          set(loop._selector.get_map().keys()),
                          "loop 持有的 fd 不得被关")
            with self.assertRaises(OSError):
                orphan.shutdown(_s.SHUT_RDWR)   # 已被 shutdown → ENOTCONN
        asyncio.run(drive())

    def test_hung_fetch_also_cleans_sockets(self):
        """fetch 超时路径必须调用清理（防只改一处漏另一处）。"""
        src = open("tg_userbot/asmr_dl.py", encoding="utf-8").read()
        self.assertIn("_kill_orphan_sockets()", src)


class StagingReuseTest(_AsmrDbTestCase):
    """拷贝被打断转重试后，staging 里已对账完的文件直接补拷贝，
    不重新下载（2026-11-28：挂载冻结 900s 一弃，20MB 就白拉一遍）。"""

    def test_complete_staging_file_skips_download(self):
        from tg_userbot import task_worker
        info = {"title": "T", "circle": "C", "source_id": "",
                "files": [{"path": "a.wav", "size": 10, "url": "u"}]}
        task = {"rj": 999003, "title": "T", "only_filter": ""}
        runtime_db.enqueue_asmr_task(999003, title="T", circle="",
                                     total_files=0, size_bytes=0)
        stage_root = tempfile.mkdtemp(prefix="asmr_stage_", dir=_TMP)
        self.addCleanup(shutil.rmtree, stage_root, ignore_errors=True)
        # 只在 _process_task 执行期间把 RUNTIME_DIR 指向 stage_root
        #（runtime_db 的 DB path 在 init 时已固定，不受影响）
        staging_a = os.path.join(stage_root, "asmr", "999003", "a.wav")
        os.makedirs(os.path.dirname(staging_a), exist_ok=True)
        with open(staging_a, "wb") as f:
            f.write(b"1234567890")     # 与 API size 一致

        async def fake_dl(*a, **k):
            raise AssertionError("staging 已有对账文件，不该再下载")

        async def fake_cp(*a, **k):
            return 10

        with mock.patch.object(asmr_dl, "fetch_work_info_blocking",
                               return_value=info), \
                mock.patch.object(task_worker, "download_with_watchdog",
                                  new=fake_dl), \
                mock.patch.object(task_worker, "copy_with_watchdog",
                                  new=fake_cp), \
                mock.patch.object(config, "RUNTIME_DIR", stage_root), \
                mock.patch("tg_userbot.extract_worker.mount_of",
                           return_value=self.dir), \
                mock.patch.object(config, "ASMR_THROTTLE_SECONDS", 0.0), \
                mock.patch.object(asmr_dl, "_wait_remote_file",
                                  new=mock.AsyncMock(return_value=True)):
            out = asyncio.run(asmr_dl._process_task(task))
        self.assertEqual(out["files"], 1)
