"""控制面板前端的自动化守卫（典型问题库 P1/P2 的落地）。

背景（2026-11-16 问题库整理）：看板 JS 曾两次整页静默坏死——gum 轮次
遗留的语法错误（async async function / 非 async 内 await）让全部按钮
"点了没反应"，直到 owner 反馈才发现；_sseOK 标志只在 onopen 置 true、
断开不清，SSE 断连后页面冻结在旧数据（owner 看到早已变 SUCCESS 的
任务还挂着 RETRY_WAIT）。

控制措施：
  ① node --check 全 script 语法校验——语法错误再也进不了主干；
  ② 关键"状态标志"语义断言——onerror 必须清 _sseOK（防止兜底轮询
    被跳过、页面冻结这一模式复发）。

node 不存在（CI/极简环境）时 skip——守卫只在有 node 的开发机上咬人。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

_HTML = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tg_userbot", "api_dashboard.html")

_HAS_NODE = shutil.which("node") is not None


@unittest.skipUnless(_HAS_NODE, "node 不存在（跳过 JS 语法守卫）")
class DashboardJsSyntaxTest(unittest.TestCase):
    """P1 控制：看板 JS 语法校验——语法错误不得进主干。

    整页静默坏死的两次事故（87dca05 / 80ec775 修复）都是语法级错误，
    任何一个静态语法检查都能当场拦下。"""

    def test_all_script_blocks_pass_node_check(self):
        html = open(_HTML, encoding="utf-8").read()
        blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
        self.assertTrue(blocks, "找不到 <script> 块（HTML 结构异常）")
        for i, block in enumerate(blocks):
            with tempfile.NamedTemporaryFile("w", suffix=".js",
                                             delete=False) as f:
                f.write(block)
                path = f.name
            try:
                r = subprocess.run(["node", "--check", path],
                                   capture_output=True, text=True, timeout=30)
            finally:
                os.unlink(path)
            self.assertEqual(
                r.returncode, 0,
                f"script 块 {i} 语法错误（整页静默坏死模式）：\n"
                f"{r.stderr[:600]}")


class DashboardJsStateFlagGuardTest(unittest.TestCase):
    """P2 控制：SSE 连接标志语义——断开必须清 _sseOK。

    冻结假状态模式（57f9a06 修复）：标志只置不清 → batch 轮询兜底被
    跳过 → 页面停在旧数据上给 owner 看假状态。此断言钉住三件事：
    onopen 置位、onerror 清位、_refreshLoop 以 _sseOK 决定是否兜底。"""

    def setUp(self):
        self.js = open(_HTML, encoding="utf-8").read()

    def test_sse_ok_set_on_open_and_cleared_on_error(self):
        self.assertIn("_sseOK = true", self.js, "onopen 必须置位 _sseOK")
        self.assertRegex(
            self.js, r"onerror[^{]*\{[^}]*_sseOK\s*=\s*false",
            "onerror 必须清 _sseOK（否则 SSE 断连后兜底轮询被跳过，"
            "页面冻结在旧数据——57f9a06 修复的模式不得复发）")

    def test_batch_polling_gated_by_sse_ok(self):
        self.assertRegex(
            self.js, r"if\s*\(!\s*window\._sseOK\s*\)",
            "batch 轮询必须以 _sseOK 为闸（断连时接管刷新）")

    def test_disconnect_banner_wiring(self):
        self.assertIn('id="connBanner"', self.js, "断连横幅元素缺失")
        self.assertIn("_connBanner(true)", self.js,
                      "onerror 路径必须亮断连横幅")

    def test_action_refresh_hooked(self):
        """操作成功后必须即时刷新（UX-4 模式：点了没反应感）。"""
        self.assertIn("refreshActiveView()", self.js)
        self.assertRegex(
            self.js, r"if\s*\(\s*d\.ok\s*\)\s*refreshActiveView\(\)",
            "runCmdDirect 成功路径必须调 refreshActiveView")


class DashboardActionWhitelistGuardTest(unittest.TestCase):
    """P4 控制（双入口漂移）：hint 命令模板必须指向真实注册的命令。

    x/pixiv 的 hint 曾因注释过时（『站点侧没有重试命令』）长期缺失，
    而 /x retry 早已存在——面板弱于 bot。此断言钉住四段视图 hint
    模板里的命令名都是 command_registry 注册过的。"""

    def test_qd_hint_commands_are_registered(self):
        # __file__ = <repo>/tests/test_dashboard_js.py → repo 是上两级
        repo = os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))
        sys.path.insert(0, repo)
        from tg_userbot import command_registry
        src = open(os.path.join(repo, "tg_userbot", "api_server.py"),
                   encoding="utf-8").read()
        # 提取 _qd_* 里 hint 模板中的命令（/xxx retry {key} 形态）
        hints = re.findall(r'"(/(?:[a-z_]+))\s+(?:retry|del)\s*\{key\}"',
                           src)
        self.assertTrue(hints, "未找到 queue_detail hint 模板（结构变动？）")
        reg = {c.name for c in command_registry.ALL}
        for h in hints:
            self.assertIn(h.lstrip("/"), reg,
                          f"hint 模板命令 {h} 未在 command_registry 注册"
                          "（双入口漂移：面板指向了不存在的命令）")


if __name__ == "__main__":
    unittest.main()
