"""监听规则持久层（listener_rules，schema v24）的守卫测试。

背景（2026-11-16 事故，典型问题库 P7）：listen.json 曾是唯一真相源，
被测试进程 save_listen_config 覆写成 [] 后规则全丢、评论区跟进停摆
6 天且不可恢复。入库后 DB 是真相源、JSON 是导出物——本文件钉住三个
契约：
  ① write-through：save_listen_config 必须落 DB（表 = state 镜像）；
  ② 装载优先级：表非空 → 表覆盖内存（JSON 被清空也不再丢规则）；
  ③ 首次升级导入：表空 + JSON 非空 → 导入并落表（旧部署升级路径）。
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

# 注意：TG_SAVE_FOLDER 已由 conftest.py / 更早 import 的测试文件设定并冻结
# （config.LISTEN_CONFIG_FILE 指向那个目录的 runtime/listen.json）——本文件
# 不再覆盖 env（覆盖无效且会让人误以为路径可换），JSON 路径一律用
# listener._config_path() 动态取，保证与被测代码读写同一个文件。
from tg_userbot import config  # noqa: E402
from tg_userbot import listener, runtime_db, state  # noqa: E402


def _rule(tag, chat=-1001719225045):
    return {"source_chat_id": chat, "tag": tag,
            "targets": [{"type": "saved_messages"}], "download": True,
            "source_name": "祂录（3D区）", "source_username": "talu3D"}


class ListenerRulesDbTest(unittest.TestCase):
    """规则入库三契约（2026-11-16 事故防线）。"""

    def setUp(self):
        # JSON 路径 = 被测代码实际读写的 listen.json（动态取，见文件头注释）
        self.json_path = listener._config_path()
        os.makedirs(os.path.dirname(self.json_path), exist_ok=True)
        for p in (self.json_path, self.json_path + ".tmp"):
            if os.path.exists(p):
                os.remove(p)
        self._old = (list(state.LISTEN_RULES), state.LISTEN_ENABLED,
                     state.LISTEN_INTERVAL_MINUTES)
        self._old_mtime = listener._LAST_CONFIG_MTIME
        state.LISTEN_RULES = []
        state.LISTEN_ENABLED = True
        # 独立 DB 连接（不能复用进程内连接：check_same_thread / 跨测试污染）
        self._saved_conn = runtime_db._CONN
        runtime_db._CONN = None
        self._db_path = os.path.join(
            tempfile.mkdtemp(prefix="listen_rules_db_"), "tg.db")
        assert runtime_db.init_db(self._db_path)
        runtime_db._rules_replace([])

    def tearDown(self):
        runtime_db.close_db()
        runtime_db._CONN = self._saved_conn
        (state.LISTEN_RULES, state.LISTEN_ENABLED,
         state.LISTEN_INTERVAL_MINUTES) = self._old
        listener._LAST_CONFIG_MTIME = self._old_mtime
        shutil.rmtree(os.path.dirname(self._db_path), ignore_errors=True)
        for p in (self.json_path, self.json_path + ".tmp"):
            if os.path.exists(p):
                os.remove(p)

    def test_save_writes_through_to_db(self):
        """① save_listen_config 必须落 DB：表是 state 的精确镜像。"""
        state.LISTEN_RULES = [_rule("#水水"), _rule("#腿玩年")]
        self.assertTrue(listener.save_listen_config())
        rows = runtime_db.rules_load()
        self.assertEqual([r["tag"] for r in rows], ["#水水", "#腿玩年"])
        # JSON 同步导出（人可读快照仍存在）
        with open(self.json_path, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["listeners"]), 2)

    def test_db_overrides_emptied_json_on_load(self):
        """② 表非空 → 表覆盖内存：JSON 被清空不再丢规则（事故核心场景）。"""
        # 先正常保存 2 条（DB + JSON 双层都有）
        state.LISTEN_RULES = [_rule("#水水"), _rule("#腿玩年")]
        listener.save_listen_config()
        # 模拟事故：外部进程把 JSON 清空（DB 不动）
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump({"enabled": True, "interval_minutes": 30,
                       "listeners": []}, f)
        n = listener.load_listen_config()
        self.assertEqual(n, 2, "JSON 被清空后装载必须从 DB 恢复")
        self.assertEqual([r["tag"] for r in state.LISTEN_RULES],
                         ["#水水", "#腿玩年"])
        # 且用 DB 覆写回导出物（JSON 不再是 0 条）
        with open(self.json_path, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["listeners"]), 2)

    def test_first_upgrade_imports_json_into_db(self):
        """③ 表空 + JSON 非空 → 一次性导入落表（旧部署升级路径）。"""
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump({"enabled": True, "interval_minutes": 30,
                       "listeners": [_rule("#01musume")]}, f)
        n = listener.load_listen_config()
        self.assertEqual(n, 1)
        self.assertEqual([r["tag"] for r in runtime_db.rules_load()],
                         ["#01musume"], "导入必须落表（此后 DB 是真相源）")

    def test_roundtrip_preserves_optional_fields(self):
        """exclude_ext / source_name 等可选字段 round-trip 无损。"""
        rule = _rule("#VikLodn")
        rule["exclude_ext"] = [".webp", ".jpg"]
        state.LISTEN_RULES = [rule]
        listener.save_listen_config()
        state.LISTEN_RULES = []
        listener.load_listen_config()
        self.assertEqual(state.LISTEN_RULES[0].get("exclude_ext"),
                         [".jpg", ".webp"])
        self.assertEqual(state.LISTEN_RULES[0].get("source_name"),
                         "祂录（3D区）")


if __name__ == "__main__":
    unittest.main()
