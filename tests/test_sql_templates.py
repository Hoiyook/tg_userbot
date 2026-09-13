"""SQL 模板（sql_templates.py）的单元测试：存取/名字校验/解析/执行接线。

契约：模板存 runtime/sql_templates.json（配置归 JSON 层）；名字 1-16 字符
（中文/字母/数字/下划线，保留字 add/del/list 不可用）；同名 upsert 即改；
执行走 runtime_db.execute_user_sql（权限全放开、错误当结果）。
"""
import atexit
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_sqlt_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402
from tg_userbot import sql_templates as st  # noqa: E402
from tg_userbot import state  # noqa: E402


class StorageTest(unittest.TestCase):

    def setUp(self):
        state.SQL_TEMPLATES = {}
        self.addCleanup(setattr, state, "SQL_TEMPLATES", {})

    def test_roundtrip_and_overwrite(self):
        ok, msg = st.upsert("待执行", "SELECT 1")
        self.assertTrue(ok)
        ok, msg = st.upsert("待执行", "SELECT 2")
        self.assertTrue(ok)
        self.assertIn("覆盖", msg)
        self.assertEqual(state.SQL_TEMPLATES["待执行"], "SELECT 2")
        self.assertEqual(st.get("待执行"), "SELECT 2")

    def test_persist_and_load(self):
        st.upsert("a", "SELECT 1")
        st.upsert("b", "SELECT 2")
        state.SQL_TEMPLATES = {}
        self.assertEqual(st.load_sql_templates(), 2)
        self.assertEqual(st.get("a"), "SELECT 1")

    def test_name_validation(self):
        for bad in ("", "x" * 17, "a b", "add", "del", "list"):
            ok, _ = st.validate_name(bad)
            self.assertFalse(ok, f"{bad!r} 不该通过")
        ok, _ = st.validate_name("模板_1")
        self.assertTrue(ok)

    def test_delete(self):
        st.upsert("a", "SELECT 1")
        ok, _ = st.delete("a")
        self.assertTrue(ok)
        self.assertIsNone(st.get("a"))
        ok, _ = st.delete("a")
        self.assertFalse(ok)

    def test_empty_sql_rejected(self):
        ok, _ = st.upsert("a", "   ")
        self.assertFalse(ok)

    def test_list_text_shows_names_and_sql(self):
        st.upsert("待执行", "SELECT status FROM listener_tasks")
        text = st.list_text()
        self.assertIn("待执行", text)
        self.assertIn("SELECT status", text)
        self.assertIn("sqlt add", text)   # 用法提示


class ParseTest(unittest.TestCase):

    def test_parse_shapes(self):
        self.assertEqual(st.parse_sqlt_command("/sqlt"), ("list", None))
        self.assertEqual(st.parse_sqlt_command("/sqlt list"), ("list", None))
        self.assertEqual(st.parse_sqlt_command("/sqlt 待执行"), ("run", "待执行"))
        self.assertEqual(st.parse_sqlt_command("/sqlt add n SELECT 1"),
                         ("add", "n SELECT 1"))
        self.assertEqual(st.parse_sqlt_command("/sqlt del n"), ("del", "n"))

    def test_is_sqlt_command(self):
        self.assertTrue(st.is_sqlt_command("/sqlt"))
        self.assertTrue(st.is_sqlt_command("/sqlt x"))
        self.assertFalse(st.is_sqlt_command("/sqltfoo"))
        self.assertFalse(st.is_sqlt_command("/sql SELECT 1"))


class ExecuteTemplateTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sqltexec_", dir=_TMP)
        p = mock.patch.object(config, "RUNTIME_DB_FILE",
                              os.path.join(self.dir, "db.sqlite"))
        p.start()
        self.addCleanup(p.stop)
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        state.SQL_TEMPLATES = {"查任务": "SELECT COUNT(*) AS n FROM listener_tasks"}
        self.addCleanup(setattr, state, "SQL_TEMPLATES", {})

    def test_execute_returns_result_dict(self):
        ok, result = st.execute_template("查任务")
        self.assertTrue(ok)
        self.assertEqual(result["kind"], "rows")
        self.assertEqual(result["columns"], ["n"])

    def test_execute_unknown_template(self):
        ok, msg = st.execute_template("没有的")
        self.assertFalse(ok)
        self.assertIn("没有", msg)

    def test_execute_bad_sql_is_result(self):
        st.upsert("坏的", "SELE 1")
        ok, result = st.execute_template("坏的")
        self.assertTrue(ok)          # 执行本身成功
        self.assertEqual(result["kind"], "error")   # 错误是结果


if __name__ == "__main__":
    unittest.main()
