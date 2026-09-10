"""Caption 命名清洗（caption_filter.py）的单元测试。

背景：转发说明常带结构化字段（`作者：#腿玩年 期数：bl11 …`）、URL、括号，
全塞进文件名又长又噪。清洗按可配置规则把标签/URL 去掉，只留值，结果再交给
现有命名逻辑（超长仍由 naming.truncate_filename 负责）。

守护点：
1. 规则解析：4 种前缀（exact/contains/regex/field），只切第一个冒号
   （regex 里含 `:`）；非法前缀 / 空 pattern / 非法 regex → ValueError。
2. clean_caption：默认规则能复现文档验收输出；field 剥标签留值、
   字段顺序不固定、值可含空格、中英文冒号、换行字段；§10 五条防误删；
   空白归一但不破坏 `#标签`。
3. 清洗不做截断（文件名长度是 naming 的职责）。
4. 非法/未知规则当场跳过，绝不抛异常拖垮下载。
5. 配置持久化与服务函数（rules_text/add_rule/del_rule/clear_rules/
   reset_rules/test_text）——规则是配置项，改完实时生效且重启不丢。

不联网；文件全部落在进程级临时 SAVE_FOLDER。
运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import atexit
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_caption_filter_test_")
# 退出时回收临时目录（测试跑完就地删，别让 /var/folders 越堆越多）
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import state  # noqa: E402
from tg_userbot import caption_filter  # noqa: E402
from tg_userbot import naming  # noqa: E402


REAL_CAPTION = (
    "作者：#腿玩年 期数：bl11 角色：#弱音 "
    "i站地址【 https://ecchi.iwara.tv/videos/jprxauwjbqfepebnb 】 "
    "标签：#MMD #掉装备"
)
REAL_EXPECTED = "#腿玩年 bl11 #弱音 #MMD #掉装备"


class ParseRuleTest(unittest.TestCase):
    """parse_caption_filter_rule：前缀 → (type, pattern)。"""

    def test_four_prefixes(self):
        cases = {
            "field:作者": ("field", "作者"),
            "exact:作者：": ("exact", "作者："),
            "contains:广告": ("contains", "广告"),
            r"regex:https?://\S+": ("regex", r"https?://\S+"),
        }
        for rule, (rtype, pattern) in cases.items():
            with self.subTest(rule=rule):
                parsed = caption_filter.parse_caption_filter_rule(rule)
                self.assertEqual(parsed["type"], rtype)
                self.assertEqual(parsed["pattern"], pattern)

    def test_regex_pattern_keeps_its_colons(self):
        # 只切第一个冒号：regex 正文里的 ':' 必须原样保留
        parsed = caption_filter.parse_caption_filter_rule(r"regex:https?://\S+")
        self.assertEqual(parsed["pattern"], r"https?://\S+")

    def test_unknown_prefix_rejected(self):
        with self.assertRaises(ValueError):
            caption_filter.parse_caption_filter_rule("abc:作者")

    def test_empty_pattern_rejected(self):
        for rule in ("field:", "exact:", "contains:", "regex:"):
            with self.subTest(rule=rule):
                with self.assertRaises(ValueError):
                    caption_filter.parse_caption_filter_rule(rule)

    def test_invalid_regex_rejected(self):
        with self.assertRaises(ValueError):
            caption_filter.parse_caption_filter_rule("regex:(abc")

    def test_plain_text_without_prefix_rejected(self):
        with self.assertRaises(ValueError):
            caption_filter.parse_caption_filter_rule("作者")


class CleanCaptionTest(unittest.TestCase):
    """clean_caption：按规则清洗（默认规则见 config.DEFAULT_CAPTION_FILTER_RULES）。"""

    def clean(self, text, rules):
        return caption_filter.clean_caption(text, rules)

    def test_default_rules_are_the_documented_seven(self):
        self.assertEqual(
            config.DEFAULT_CAPTION_FILTER_RULES,
            [
                "field:作者",
                "field:期数",
                "field:角色",
                "field:i站地址",
                "field:标签",
                r"regex:https?://\S+",
                r"regex:【.*?】",
            ],
        )

    # Test 1：真实 Caption（文档 §46 验收输出）
    def test_real_caption_with_default_rules(self):
        self.assertEqual(
            self.clean(REAL_CAPTION, config.DEFAULT_CAPTION_FILTER_RULES),
            REAL_EXPECTED,
        )

    # Test 2：field 防误删（文档 §10 五条全部不得改动）
    def test_field_does_not_touch_natural_language(self):
        rules = ["field:作者"]
        for text in (
            "这个作者真的很厉害。",
            "我很喜欢这个作者。",
            "作者真的很厉害。",
            "这个作者：真的很厉害。",
            "我认识一个作者：张三。",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.clean(text, rules), text)

    # Test 3：exact
    def test_exact(self):
        self.assertEqual(
            self.clean("作者：#腿玩年", ["exact:作者："]), "#腿玩年"
        )

    # Test 4：contains（有意宽匹配）
    def test_contains(self):
        self.assertEqual(
            self.clean("这是广告内容", ["contains:广告"]), "这是内容"
        )

    # Test 5：regex URL
    def test_regex_url(self):
        self.assertEqual(
            self.clean(
                "测试 https://example.com/video abc", [r"regex:https?://\S+"]
            ),
            "测试 abc",
        )

    # Test 6：中文 / 英文冒号 + 冒号附近空格
    def test_field_colon_variants(self):
        rules = ["field:作者"]
        for text in ("作者：张三", "作者:张三", "作者 : 张三", "作者 ： 张三"):
            with self.subTest(text=text):
                self.assertEqual(self.clean(text, rules), "张三")

    # Test 7：换行分隔的字段
    def test_newline_separated_fields(self):
        text = "作者：#腿玩年\n期数：bl11\n角色：#弱音\n标签：#MMD #掉装备"
        self.assertEqual(
            self.clean(text, config.DEFAULT_CAPTION_FILTER_RULES), REAL_EXPECTED
        )

    # Test 8：清洗不做截断（长度控制仍归 naming.truncate_filename）
    def test_clean_caption_does_not_truncate(self):
        value = "x" * (config.MAX_FILENAME_BYTES * 3)
        result = self.clean(f"标签：{value}", ["field:标签"])
        self.assertEqual(result, value)
        self.assertGreater(len(result.encode("utf-8")), config.MAX_FILENAME_BYTES)

    def test_field_value_keeps_spaces(self):
        # 值一直持续到下一个字段起点，不是下一个空格
        self.assertEqual(
            self.clean(
                "作者：John Smith 期数：第12期 标签：#MMD #test",
                ["field:作者", "field:期数", "field:标签"],
            ),
            "John Smith 第12期 #MMD #test",
        )

    def test_field_order_is_not_fixed(self):
        rules = ["field:作者", "field:角色", "field:标签"]
        self.assertEqual(self.clean("角色：C 作者：A 标签：D", rules), "C A D")
        self.assertEqual(self.clean("标签：D 作者：A", rules), "D A")

    def test_field_name_needs_boundary(self):
        # 字段名必须在开头或空白之后：'第2作者：x' 不是结构化字段
        self.assertEqual(
            self.clean("第2作者：x", ["field:作者"]), "第2作者：x"
        )
        self.assertEqual(self.clean("作者：x", ["field:作者"]), "x")

    def test_field_name_with_bracket_separator(self):
        # i站地址【…】没有冒号 → 名字后紧跟左括号也算字段起点（剥名字留内容）
        self.assertEqual(
            self.clean(
                "i站地址【 https://x.test/v/1 】 标签：#MMD",
                ["field:i站地址", "field:标签", r"regex:https?://\S+",
                 r"regex:【.*?】"],
            ),
            "#MMD",
        )

    def test_whitespace_normalized_without_joining_hashtags(self):
        # §39/§40：连续空白 → 一个空格；'#MMD #掉装备' 不能变成 '#MMD#掉装备'
        self.assertEqual(
            self.clean("标签：#MMD     #掉装备", ["field:标签"]),
            "#MMD #掉装备",
        )
        self.assertEqual(
            self.clean("标签：#MMD\n\n#掉装备", ["field:标签"]),
            "#MMD #掉装备",
        )

    def test_empty_caption_stays_empty(self):
        self.assertEqual(self.clean("", config.DEFAULT_CAPTION_FILTER_RULES), "")
        self.assertEqual(
            self.clean("   ", config.DEFAULT_CAPTION_FILTER_RULES), ""
        )

    def test_fully_cleaned_caption_is_empty(self):
        # 全被清掉 → 空串（不得兜成「未命名文件」，fallback 名不归清洗管）
        self.assertEqual(self.clean("作者：#腿玩年", ["regex:.+"]), "")

    def test_field_keeps_value(self):
        # field 是「剥标签留值」，不是整字段删除（文档 §46 验收口径）
        self.assertEqual(
            self.clean("作者：#腿玩年", ["field:作者"]), "#腿玩年"
        )

    def test_no_match_returns_caption_unchanged(self):
        self.assertEqual(
            self.clean("普通标题 abc", config.DEFAULT_CAPTION_FILTER_RULES),
            "普通标题 abc",
        )

    def test_field_runs_before_contains_even_if_listed_after(self):
        # field 必须先于 contains 执行，否则 contains 会先破坏字段结构
        self.assertEqual(
            self.clean("作者：#腿玩年", ["contains:作者", "field:作者"]),
            "#腿玩年",
        )

    def test_invalid_rules_are_skipped_not_raised(self):
        rules = ["bogus:x", "field:", "regex:(abc", "exact:abc"]
        with mock.patch.object(caption_filter.logger, "warning") as warn:
            self.assertEqual(self.clean("abcxyz", rules), "xyz")
        # 坏规则跳过 + 留一条可查的日志，绝不抛
        self.assertEqual(warn.call_count, 3)

    def test_rules_none_falls_back_to_current_state_rules(self):
        with unittest.mock.patch.object(
            state, "CAPTION_FILTER_RULES", ["field:作者"]
        ):
            self.assertEqual(
                caption_filter.clean_caption("作者：#腿往"), "#腿往"
            )

    def test_field_name_is_escaped(self):
        # 字段名可能含正则元字符：re.escape 后按字面匹配
        self.assertEqual(self.clean("A+B：值", ["field:A+B"]), "值")


class ConfigServiceTest(unittest.TestCase):
    """规则是配置项：增删改清空恢复都要持久化，且改完立即生效（无需重启）。"""

    def setUp(self):
        self.path = os.path.join(_TMP, "caption_filter_service_test.json")
        self._patch = mock.patch.object(
            config, "CAPTION_FILTER_CONFIG_FILE", self.path
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(
            lambda: os.path.exists(self.path) and os.remove(self.path)
        )
        self._saved = state.CAPTION_FILTER_RULES
        state.CAPTION_FILTER_RULES = list(config.DEFAULT_CAPTION_FILTER_RULES)
        self.addCleanup(self._restore_rules)

    def _restore_rules(self):
        state.CAPTION_FILTER_RULES = self._saved

    def _written(self):
        import json
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)["rules"]

    def test_load_without_file_keeps_defaults(self):
        caption_filter.load_caption_filter_config()
        self.assertEqual(
            state.CAPTION_FILTER_RULES, config.DEFAULT_CAPTION_FILTER_RULES
        )

    def test_save_then_load_roundtrip(self):
        caption_filter.save_caption_filter_config(["field:测试"])
        state.CAPTION_FILTER_RULES = []
        caption_filter.load_caption_filter_config()
        self.assertEqual(state.CAPTION_FILTER_RULES, ["field:测试"])

    def test_load_corrupt_file_keeps_defaults(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        with mock.patch.object(caption_filter.logger, "warning"):
            caption_filter.load_caption_filter_config()
        self.assertEqual(
            state.CAPTION_FILTER_RULES, config.DEFAULT_CAPTION_FILTER_RULES
        )

    def test_load_drops_invalid_rules(self):
        import json
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"rules": ["bogus:x", "field:作者"]}, f)
        with mock.patch.object(caption_filter.logger, "warning"):
            caption_filter.load_caption_filter_config()
        self.assertEqual(state.CAPTION_FILTER_RULES, ["field:作者"])

    def test_add_rule_persists_and_takes_effect_immediately(self):
        ok, msg = caption_filter.add_rule("field:测试")
        self.assertTrue(ok)
        self.assertIn("field:测试", msg)
        self.assertIn("field:测试", state.CAPTION_FILTER_RULES)
        self.assertIn("field:测试", self._written())
        # 无需重启：下一条消息就用新规则
        self.assertEqual(
            caption_filter.clean_caption("测试：内容"), "内容"
        )

    def test_add_rejects_unknown_prefix(self):
        ok, msg = caption_filter.add_rule("abc:作者")
        self.assertFalse(ok)
        self.assertIn("不支持的规则类型", msg)
        self.assertNotIn("abc:作者", state.CAPTION_FILTER_RULES)
        self.assertFalse(os.path.exists(self.path))

    def test_add_rejects_invalid_regex(self):
        ok, msg = caption_filter.add_rule("regex:(abc")
        self.assertFalse(ok)
        self.assertIn("正则表达式无效", msg)
        self.assertFalse(os.path.exists(self.path))

    def test_add_rejects_empty_pattern(self):
        ok, _ = caption_filter.add_rule("field:")
        self.assertFalse(ok)

    def test_del_rule_by_number(self):
        state.CAPTION_FILTER_RULES = ["field:作者", "field:期数", "field:角色"]
        ok, msg = caption_filter.del_rule(2)
        self.assertTrue(ok)
        self.assertIn("field:期数", msg)
        self.assertEqual(
            state.CAPTION_FILTER_RULES, ["field:作者", "field:角色"]
        )
        self.assertEqual(self._written(), ["field:作者", "field:角色"])

    def test_del_rule_out_of_range(self):
        ok, msg = caption_filter.del_rule(99)
        self.assertFalse(ok)
        self.assertIn("规则编号不存在", msg)
        self.assertEqual(
            len(state.CAPTION_FILTER_RULES),
            len(config.DEFAULT_CAPTION_FILTER_RULES),
        )

    def test_clear_rules(self):
        ok, msg = caption_filter.clear_rules()
        self.assertTrue(ok)
        self.assertIn("已清空", msg)
        self.assertEqual(state.CAPTION_FILTER_RULES, [])
        self.assertEqual(self._written(), [])

    def test_reset_restores_defaults_not_current(self):
        caption_filter.clear_rules()
        ok, msg = caption_filter.reset_rules()
        self.assertTrue(ok)
        self.assertIn("默认", msg)
        self.assertEqual(
            state.CAPTION_FILTER_RULES, config.DEFAULT_CAPTION_FILTER_RULES
        )
        self.assertEqual(
            self._written(), config.DEFAULT_CAPTION_FILTER_RULES
        )

    def test_rules_text_lists_numbered(self):
        out = caption_filter.rules_text()
        self.assertIn("1. field:作者", out)
        self.assertIn("7. regex:【.*?】", out)
        self.assertIn("共 7 条", out)

    def test_rules_text_when_empty(self):
        caption_filter.clear_rules()
        self.assertIn("当前没有任何规则", caption_filter.rules_text())

    def test_test_text_runs_real_clean_caption(self):
        out = caption_filter.test_text("作者：#腿玩年 标签：#MMD")
        self.assertIn("🧪 Caption 清洗测试", out)
        self.assertIn("作者：#腿玩年 标签：#MMD", out)
        self.assertIn("#腿玩年 #MMD", out)

    def test_test_text_when_fully_cleaned(self):
        out = caption_filter.test_text("作者：")
        self.assertIn("(空)", out)


class CommandParseTest(unittest.TestCase):
    """/caption_filter 的子命令解析（命令与菜单共用同一套服务函数）。"""

    def parse(self, text):
        return caption_filter.parse_caption_filter_command(text)

    def test_bare_command_lists(self):
        self.assertEqual(self.parse("/caption_filter"), ("list", None))

    def test_add(self):
        self.assertEqual(
            self.parse("/caption_filter add field:作者"), ("add", "field:作者")
        )
        self.assertEqual(
            self.parse("/caption_filter add regex:https?://\\S+"),
            ("add", "regex:https?://\\S+"),
        )

    def test_del(self):
        self.assertEqual(self.parse("/caption_filter del 3"), ("del", "3"))

    def test_clear_and_reset(self):
        self.assertEqual(self.parse("/caption_filter clear"), ("clear", None))
        self.assertEqual(self.parse("/caption_filter reset"), ("reset", None))

    def test_test_keeps_full_text(self):
        self.assertEqual(
            self.parse("/caption_filter test 作者：A 标签：B"),
            ("test", "作者：A 标签：B"),
        )

    def test_unknown_subcommand_is_usage(self):
        self.assertEqual(self.parse("/caption_filter wat"), ("usage", "wat"))

    def test_is_command_matches_only_prefix(self):
        self.assertTrue(caption_filter.is_caption_filter_command("/caption_filter"))
        self.assertTrue(
            caption_filter.is_caption_filter_command("/caption_filter del 1")
        )
        self.assertFalse(
            caption_filter.is_caption_filter_command("/caption_filters")
        )
        self.assertFalse(caption_filter.is_caption_filter_command("/dedup"))

    def test_parse_returns_none_for_other_text(self):
        self.assertIsNone(self.parse("/dedup"))
        self.assertIsNone(self.parse("作者：#腿玩年"))


class NamingIntegrationTest(unittest.TestCase):
    """命名链路接入：清洗跑在 sanitize **之前**，且全链路只清洗一次。

    清洗必须作用于原始 message.message——sanitize_filename 会把换行和 ASCII
    冒号换成 '_'，之后再清洗就认不出字段边界了（这两条用例正是钉死顺序的）。
    """

    D = datetime(2026, 9, 10, 12, 0, 0)

    def fake(self, filename, caption="", **flags):
        m = mock.MagicMock()
        m.id = 1
        m.file.name = filename
        m.file.size = 100
        m.fwd_from = None
        m.message = caption
        m.date = self.D
        for attr in ("photo", "video", "audio", "voice", "document", "media"):
            setattr(m, attr, flags.get(attr, None))
        return m

    def test_real_caption_cleaned_into_filename(self):
        m = self.fake("a.mp4", REAL_CAPTION)
        self.assertEqual(
            naming.compute_final_filename(m),
            f"26-09-10 {REAL_EXPECTED} - a.mp4",
        )

    def test_cleaning_runs_before_sanitize_newline_fields(self):
        # sanitize 会把换行变成 '_'，那样 '期数' 就不再是字段起点
        m = self.fake("a.mp4", "作者：#腿玩年\n期数：bl11")
        self.assertEqual(
            naming.compute_final_filename(m), "26-09-10 #腿玩年 bl11 - a.mp4"
        )

    def test_cleaning_runs_before_sanitize_ascii_colon(self):
        # sanitize 会把 ASCII ':' 换成 '_'，那样 field:作者 永远匹配不上
        m = self.fake("a.mp4", "作者:张三")
        self.assertEqual(
            naming.compute_final_filename(m), "26-09-10 张三 - a.mp4"
        )

    def test_album_caption_override_is_cleaned(self):
        m = self.fake(
            "c80d0ff8-4fdb-4762-8b97-800612217e4c.jpg", "", photo=True
        )
        self.assertEqual(
            naming.compute_final_filename(m, caption=REAL_CAPTION),
            f"26-09-10 {REAL_EXPECTED}.jpg",
        )

    def test_captionless_message_unchanged(self):
        m = self.fake("a.mp4")
        result = naming.compute_final_filename(m)
        self.assertEqual(result, "26-09-10 a.mp4")
        self.assertNotIn("未命名文件", result)

    def test_caption_that_cleans_to_nothing_falls_back(self):
        # 只有标签、没有值的说明 → 清洗成空 → 仍走原有的 媒体类型_时间戳 兜底
        m = self.fake(
            "c80d0ff8-4fdb-4762-8b97-800612217e4c.jpg", "标签：", photo=True
        )
        result = naming.compute_final_filename(m)
        self.assertRegex(result, r"^photo_\d{8}_\d{6}\.jpg$")
        self.assertNotIn("未命名文件", result)

    def test_long_cleaned_caption_still_truncated_by_budget(self):
        # 清洗不做截断：超长仍由既有的字节预算逻辑收敛
        m = self.fake("a.mp4", "标签：" + "x" * 400)
        result = naming.compute_final_filename(
            m, max_bytes=config.MAX_FILENAME_BYTES
        )
        self.assertLessEqual(
            len(result.encode("utf-8")), config.MAX_FILENAME_BYTES
        )
        self.assertTrue(result.startswith("26-09-10 "))
        self.assertTrue(result.endswith(" - a.mp4"))

    def test_get_caption_returns_cleaned_text(self):
        m = self.fake("a.mp4", REAL_CAPTION)
        self.assertEqual(naming.get_caption(m), REAL_EXPECTED)

    def test_url_task_title_is_not_cleaned(self):
        # 抖音本地解析链的标题不走清洗（只清 Telegram caption）
        self.assertEqual(
            naming.compute_url_filename("作者：标题", created_at=self.D),
            "26-09-10 作者：标题.mp4",
        )

    def test_new_rules_take_effect_without_restart(self):
        m = self.fake("a.mp4", "作者：#腿玩年 标签：#MMD")
        self.assertEqual(
            naming.compute_final_filename(m), "26-09-10 #腿玩年 #MMD - a.mp4"
        )
        with mock.patch.object(
            state, "CAPTION_FILTER_RULES", ["field:标签"]
        ):
            self.assertEqual(
                naming.compute_final_filename(m), "26-09-10 作者：#腿玩年 #MMD - a.mp4"
            )


if __name__ == "__main__":
    unittest.main()
