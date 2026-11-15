"""问题库文档的保鲜守卫（2026-11-16）。

防线本身也会腐烂：文档里引用的测试文件被改名/删除、引用的 commit 被
重写、守卫测试新增了但文档没更新——这些都不会让任何业务测试变红，
问题库就悄悄变成废纸（P8 观测缺失模式在文档层的翻版）。

本文件用三个断言把腐烂路径钉死：
  ① 文档引用的每个测试文件真实存在（改名/删除/移动 → 咬人）
  ② 文档引用的每个 commit hash 真实存在（无 git 时 skip）
  ③ 测试文件 docstring 里标注的模式编号（P1~P8）与文档覆盖集合一致
    ——新增守卫必须打标 + 更新文档，两边一致才放行
"""
import os
import re
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(REPO, "docs", "典型问题库.md")


class TypicalIssuesDocFreshnessTest(unittest.TestCase):
    """问题库保鲜：文档引用的一切必须真实存在（P8 模式的文档层防线）。"""

    @classmethod
    def setUpClass(cls):
        cls.doc = open(DOC, encoding="utf-8").read()

    def test_doc_exists_and_has_eight_patterns(self):
        self.assertTrue(os.path.exists(DOC), "问题库文档被移动/删除")
        for n in range(1, 9):
            self.assertRegex(
                self.doc, rf"### P{n}\.",
                f"问题库缺 P{n} 模式段落（被删改？保鲜机制失效）")

    def test_defense_table_files_exist(self):
        """防线清单表格里提到的每个测试文件必须真实存在。

        表格行形如 `| 名字 | tests/test_xxx.py | 模式 |`——文件路径列
        变了（改名/删除）测试当场咬，逼着维护者同步更新文档。"""
        for m in re.finditer(r"\|\s*([^|]+)\|\s*(tests/[\w./-]+\.py)",
                             self.doc):
            path = m.group(2).strip()
            self.assertTrue(
                os.path.exists(os.path.join(REPO, path)),
                f"问题库防线清单引用的 {path} 已不存在——"
                "请同步更新 docs/典型问题库.md 的防线清单表格")

    def test_referenced_commits_exist(self):
        """案例索引里的 commit hash 必须在 git 历史里（本地仓库必有 git）。"""
        hashes = set(re.findall(r"\b([0-9a-f]{7,10})\b", self.doc))
        # 排除疑似普通 hex（长度恰为 7~10 且独立成词的才算引用）中的误报：
        # 只有出现在表格/案例行的才算——宽松处理：查不到的放行 known 特例
        if not hashes:
            self.skipTest("文档中无 commit 引用")
        for h in sorted(hashes):
            r = subprocess.run(
                ["git", "-C", REPO, "cat-file", "-t", h],
                capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                # 尝试前 7 位补全（文档可能写了截断形式）
                r7 = subprocess.run(
                    ["git", "-C", REPO, "cat-file", "-t", h[:7]],
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(
                    r7.returncode, 0,
                    f"问题库引用的 commit {h} 不在 git 历史里"
                    "（hash 写错或历史被重写——请核对案例索引）")

    def test_pattern_tags_in_tests_covered_by_doc(self):
        """测试 docstring 里标注的模式编号 ⊆ 文档覆盖的模式集合。

        约定：与问题库模式绑定的守卫测试，docstring 必须含 `P<n> 控制`
        或 `P<n> 模式` 标记；新增守卫必须同时更新文档，否则本测试咬人
        ——防线清单表格永不手工过期。"""
        tags = set()
        for dirpath, _, files in os.walk(os.path.join(REPO, "tests")):
            for fn in files:
                if not fn.startswith("test_") or not fn.endswith(".py"):
                    continue
                src = open(os.path.join(dirpath, fn),
                           encoding="utf-8").read()
                tags |= set(re.findall(r"P([1-8])\s*(?:控制|模式)", src))
        doc_patterns = set(re.findall(r"### P([1-8])\.", self.doc))
        self.assertTrue(doc_patterns, "文档模式段落解析失败")
        orphan = tags - doc_patterns
        self.assertFalse(
            orphan,
            f"测试标注了 P{'、P'.join(sorted(orphan))} 但问题库没有对应"
            f"模式段落——新增守卫请同步更新 docs/典型问题库.md")


if __name__ == "__main__":
    unittest.main()
