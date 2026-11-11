"""凭据健康度巡检 + 备份完整性验证（2026-11-12 ops-hardening R3/R4）。"""
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="cred_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import credentials, maintenance, runtime_db, state  # noqa: E402


class CredentialsCheckTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.mkdtemp(prefix="cred_db_")
        self._saved_conn = runtime_db._CONN
        runtime_db._CONN = None
        runtime_db.init_db(os.path.join(self._td, "tg.db"))
        self.addCleanup(lambda: (
            runtime_db.close_db(),
            setattr(runtime_db, "_CONN", self._saved_conn),
            shutil.rmtree(self._td, ignore_errors=True),
        ))

    def _set(self, attr, value):
        from tg_userbot import config
        self.addCleanup(setattr, config, attr,
                        getattr(config, attr))
        setattr(config, attr, value)
        return value

    def test_missing_cookie_is_ok(self):
        self._set("DOUYIN_COOKIE", "")
        r = credentials.check_all()
        dy = next(x for x in r if x["site"] == "douyin")
        self.assertTrue(dy["ok"])
        self.assertFalse(dy["configured"])

    def test_intact_cookie_is_ok(self):
        self._set("DOUYIN_COOKIE",
                  "ttwid=1|x; sessionid=abc; sessionid_ss=abc2; mstoken=x")
        r = credentials.check_all()
        dy = next(x for x in r if x["site"] == "douyin")
        self.assertTrue(dy["ok"])
        self.assertIn("字段完整", dy["detail"])

    def test_mutilated_cookie_fails(self):
        self._set("DOUYIN_COOKIE", "ttwid=1; mstoken=x")
        r = credentials.check_all()
        dy = next(x for x in r if x["site"] == "douyin")
        self.assertFalse(dy["ok"], "缺 sessionid 应判残缺")
        self.assertIn("残缺", dy["detail"])

    def test_ehentai_requires_ipb(self):
        self._set("EHENTAI_COOKIE", "foo=1")
        r = credentials.check_all()
        eh = next(x for x in r if x["site"] == "ehentai")
        self.assertFalse(eh["ok"])

    def test_daily_check_alerts_on_new_bad(self):
        from tg_userbot import config
        self._set("DOUYIN_COOKIE", "ttwid=1")   # 残缺
        calls = []
        async def fake_notify(text, **kw):
            calls.append(text)
            return True
        runtime_db.init_db(os.path.join(self._td, "tg.db"))
        self.addCleanup(runtime_db.close_db)
        results, alert = credentials.daily_check_and_alert(
            notify_coro=fake_notify)
        self.assertIsNotNone(alert)
        self.assertEqual(len(calls), 1)
        self.assertIn("失效", alert)
        # 第二轮同状态：不重复告警
        results, alert2 = credentials.daily_check_and_alert(
            notify_coro=fake_notify)
        self.assertIsNone(alert2)
        self.assertEqual(len(calls), 1)
        # 恢复后再次失效 → 再告警
        self._set("DOUYIN_COOKIE",
                  "ttwid=1; sessionid=ok; sessionid_ss=ok")
        credentials.daily_check_and_alert(notify_coro=fake_notify)
        self._set("DOUYIN_COOKIE", "ttwid=1")
        results, alert3 = credentials.daily_check_and_alert(
            notify_coro=fake_notify)
        self.assertIsNotNone(alert3)
        self.assertEqual(len(calls), 2)

    def test_health_line_lists_bad_only(self):
        from tg_userbot import config
        self._set("EHENTAI_COOKIE", "foo=1")
        line = credentials.health_line()
        self.assertIsNotNone(line)
        self.assertIn("ehentai", line)


class VerifyBackupTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.mkdtemp(prefix="vb_test_")
        self._saved_conn = runtime_db._CONN
        runtime_db._CONN = None
        db = os.path.join(self._td, "tg.db")
        runtime_db.init_db(db)
        # backup_db 经 db_path() 找源库——patch 到测试库（与 init 同源）
        self._saved_db_path = runtime_db.db_path
        runtime_db.db_path = lambda: db
        self.addCleanup(lambda: (
            runtime_db.close_db(),
            setattr(runtime_db, "_CONN", self._saved_conn),
            setattr(runtime_db, "db_path", self._saved_db_path),
            shutil.rmtree(self._td, ignore_errors=True),
        ))

    def test_good_backup_passes(self):
        path = maintenance.backup_db(self._td)
        ok, detail = maintenance.verify_backup(path)
        self.assertTrue(ok, detail)
        self.assertIn("listener=", detail)

    def test_truncated_file_fails(self):
        bad = os.path.join(self._td, "tg_userbot.20990101.db")
        open(bad, "wb").close()   # 0 字节截断
        ok, detail = maintenance.verify_backup(bad)
        self.assertFalse(ok)

    def test_corrupt_db_fails(self):
        bad = os.path.join(self._td, "tg_userbot.20990102.db")
        with open(bad, "wb") as f:
            f.write(b"not a sqlite file at all" * 100)
        ok, detail = maintenance.verify_backup(bad)
        self.assertFalse(ok)
