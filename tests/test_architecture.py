"""架构守卫（棘轮）：后台任务必须被引用。

背景（2026-11-05 立规，2026-11-08 补守卫）：CLAUDE.md 第一条工程纪律要求
后台任务一律用 ``spawn(coro, what)`` —— 裸 ``asyncio.create_task(...)`` 的
返回值若无人持有，事件循环对 Task 只持弱引用，任务可能在执行中途被 GC，
异常也只会等到 GC 时才打一条 "Task exception was never retrieved"（实测
``_enqueue_me`` 静默死 = 收藏夹媒体不下载且无任何报错）。

本文件先用最小棘轮：``app.py`` 里不允许出现「作为独立表达式语句、返回值
无人持有」的 ``create_task()``。后续可把同一规则扩到全部模块（届时存量
站点先进 allowlist，只许减不许增）。
"""
import ast
import os
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "tg_userbot")


def _is_create_task(func):
    """匹配 ``asyncio.create_task(...)`` / ``create_task(...)``。"""
    if isinstance(func, ast.Attribute) and func.attr == "create_task":
        return True
    return isinstance(func, ast.Name) and func.id == "create_task"


def unreferenced_create_task_lines(path):
    """返回文件里返回值无人持有的 create_task 行号（升序）。"""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue                 # 只要独立语句；赋值/return/参数都算持有
        call = node.value
        if isinstance(call, ast.Call) and _is_create_task(call.func):
            hits.append(node.lineno)
    return sorted(hits)


class BackgroundTaskDisciplineTest(unittest.TestCase):
    def _check(self, filename):
        path = os.path.join(PKG, filename)
        hits = unreferenced_create_task_lines(path)
        self.assertEqual(
            hits, [],
            f"{filename} 存在返回值无人持有的 asyncio.create_task（行号 "
            f"{hits}）：任务可能被 GC 静默杀死，异常也无人可见。"
            "请改用 spawn() 或保存返回值并纳入停机取消列表。")

    def test_app_has_no_unreferenced_create_task(self):
        self._check("app.py")

    def test_commands_has_no_unreferenced_create_task(self):
        # /dyu 曾在这里裸 create_task：枚举任务被 GC 后枚举锁一直占着，
        # 后续 /dyu 只回「已有一个枚举在进行中」，用户侧就是「执行不通」。
        self._check("commands.py")


# 链接分流只允许 routing.py 调用这些服务入口；三个文本入口（收藏夹 /
# bot 对话 / 看板）必须经 routing.classify + routing.execute。此前三份
# 手写分流已实际漏过（iwara / X 掉台账），这里用 AST 钉死「不再抄回去」。
_ROUTING_ONLY_CALLS = {"relay_platform_links", "handle_torrent_url",
                       "auto_reply", "observe"}
_ENTRY_MODULES = ("app.py", "bot.py", "api_server.py")


def direct_routing_calls(path):
    """入口模块里直接调用分流服务函数的 (行号, 函数名) 列表。"""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = None
        if isinstance(fn, ast.Attribute) and fn.attr in _ROUTING_ONLY_CALLS:
            name = fn.attr
        elif isinstance(fn, ast.Name) and fn.id in _ROUTING_ONLY_CALLS:
            name = fn.id
        if name:
            hits.append((node.lineno, name))
    return hits


class LinkRoutingSingleImplementationTest(unittest.TestCase):
    def test_entry_modules_delegate_to_routing(self):
        for name in _ENTRY_MODULES:
            hits = direct_routing_calls(os.path.join(PKG, name))
            self.assertEqual(
                hits, [],
                f"{name} 又直接调用了链接分流服务 {hits}——"
                "分流只允许在 routing.py 里实现，入口只负责交付方式。")


# ── 第三道守卫：SQL 只允许从 runtime_db 出去 ────────────────────────
# 存量 allowlist 只许减不许增：runtime_db 是全项目唯一 SQL 出口；
# maintenance 用 sqlite3 backup API（备份/整理必须原生连接）；
# inspect 是只读诊断（schema 自省）。其余模块一律走 runtime_db。
SQLITE_ALLOWLIST = {
    "runtime_db.py",
    "maintenance.py",
    "inspect.py",
}


# ── 第四道守卫：下层模块不得 import 入口模块（反向依赖）────────────
# 存量 allowlist：这四条是既有循环依赖（都写在函数内避免 import 期成环），
# 短期保留，**只许减不许增**。新增依赖请抽共享件，不要 import 入口。
# menu 不算入口层（它是按钮编码工具，被业务模块广泛复用）。
SURFACE_MODULES = {"app", "bot", "commands", "api_server"}
REVERSE_IMPORT_ALLOWLIST = {
    ("listener_worker.py", "app"),   # Worker 转发后复用 app.enqueue_media
    ("tme.py", "app"),               # t.me 直下复用 app._enqueue_me
    ("cmdq.py", "commands"),         # 指令回放必须回到同一 dispatcher
    ("pawchive.py", "bot"),          # 菜单复用 bot.open_input_window
}


def imported_modules(path):
    """返回文件 import 的模块名集合（含函数内延迟导入；去包名前缀）。"""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                names.add(parts[0])
                if parts[0] == "tg_userbot" and len(parts) > 1:
                    names.add(parts[1])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                parts = node.module.split(".")
                names.add(parts[0])
                if parts[0] == "tg_userbot" and len(parts) > 1:
                    names.add(parts[1])
            elif node.level and node.names:
                # from . import app
                names.update(a.name.split(".")[0] for a in node.names)
    return names


def modules_importing(pkg_dir, target):
    """目录下 import 了 ``target`` 的模块文件名（升序）。"""
    hits = []
    for name in sorted(os.listdir(pkg_dir)):
        if not name.endswith(".py"):
            continue
        if target in imported_modules(os.path.join(pkg_dir, name)):
            hits.append(name)
    return hits


def reverse_surface_imports(pkg_dir):
    """下层模块 → 入口模块的 import 边集合 {(文件名, 入口模块)}。"""
    hits = set()
    for name in sorted(os.listdir(pkg_dir)):
        if not name.endswith(".py") or name[:-3] in SURFACE_MODULES:
            continue
        imported = imported_modules(os.path.join(pkg_dir, name))
        for target in imported & SURFACE_MODULES:
            hits.add((name, target))
    return hits


class GuardSelfTest(unittest.TestCase):
    """守卫自身的灵敏度：注入一条新违规，扫描器必须报出来。"""

    def test_scanner_flags_direct_sqlite_and_reverse_import(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad_module.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write("import sqlite3\nfrom . import bot\n")
            self.assertIn("bad_module.py", modules_importing(tmp, "sqlite3"))
            self.assertEqual(reverse_surface_imports(tmp),
                             {("bad_module.py", "bot")})


class SqliteSingleExitTest(unittest.TestCase):
    def test_only_allowlisted_modules_touch_sqlite3(self):
        offenders = set(modules_importing(PKG, "sqlite3")) - SQLITE_ALLOWLIST
        self.assertEqual(
            offenders, set(),
            "以下模块直接 import sqlite3，绕过 runtime_db 的唯一 SQL 出口："
            f"{sorted(offenders)}。请改走 runtime_db；确需原生连接（备份/"
            "诊断）的加入 SQLITE_ALLOWLIST，只许减不许增。")


class ImportDirectionTest(unittest.TestCase):
    def test_lower_layers_do_not_import_surfaces(self):
        new_edges = reverse_surface_imports(PKG) - REVERSE_IMPORT_ALLOWLIST
        self.assertEqual(
            new_edges, set(),
            f"出现新的反向依赖（下层 → 入口模块）：{sorted(new_edges)}。"
            "入口模块只允许彼此调用；新增依赖请抽共享件，不要 import 入口。")


if __name__ == "__main__":
    unittest.main()


# ── 第六道守卫：命令清单单一事实源 ─────────────────────────────────
# 同一份命令知识此前抄了 ≥3 份（config.REGISTERED_COMMAND_NAMES /
# bot.BOT_COMMANDS / _handle_command_inner 的分发字面量），已实际漂移
# （extract/now/health 等 10 个命令处理着、展示着却不进审计名单，
# 2026-11-10 补登记）。本守卫钉住三份的一致性：新增命令漏登记时，
# 守护测试立刻红，而不是等审计对不上账才发现。

def _command_literals_in_dispatcher():
    """AST 抽 _handle_command_inner 里 cmd_text ==/in/startswith 的
    "/..." 字面量（分发器实际认得的命令）。"""
    path = os.path.join(PKG, "commands.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    lits = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name)
                and node.left.id == "cmd_text"):
            continue
        comps = list(node.comparators)
        for op, comp in zip(node.ops, comps):
            if isinstance(op, ast.In) and isinstance(
                    comp, (ast.Tuple, ast.List)):
                comps.extend(comp.elts)
                continue
            if isinstance(comp, ast.Call):
                comps.extend(comp.args)
                continue
            if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                lits.add(comp.value)
    return {l for l in lits if l.startswith("/")}


def _runtime_panel_and_registry():
    """运行时取面板清单与 registry（registry 化后清单是派生物，
    AST 抽字面量已不适用——不变式升级为「运行时同源一致」）。"""
    import sys
    sys.path.insert(0, REPO)
    os.environ.setdefault("TG_SAVE_FOLDER",
                          tempfile.gettempdir() + "/tg_arch_guard")
    from tg_userbot import bot, command_registry
    panel = {name for name, _ in bot.BOT_COMMANDS}
    derived = {n for n, _ in command_registry.panel_commands()}
    return panel, derived


class CommandRegistryConsistencyTest(unittest.TestCase):
    """命令审计名单（config）↔ bot 面板 ↔ 分发器字面量 三方一致。"""

    def test_dispatcher_literals_are_registered(self):
        import importlib
        import sys
        sys.path.insert(0, REPO)
        os.environ.setdefault("TG_SAVE_FOLDER",
                              tempfile.gettempdir() + "/tg_arch_guard")
        from tg_userbot import config
        reg = {n.lstrip("/") for n in config.REGISTERED_COMMAND_NAMES}
        disp = {c.lstrip("/") for c in _command_literals_in_dispatcher()}
        missing = sorted(disp - reg)
        self.assertEqual(
            missing, [],
            f"分发器处理了这些命令但没进 REGISTERED_COMMAND_NAMES（审计"
            f"漏记账）：{missing}。请补登记进 config.py 的清单。")

    def test_bot_panel_commands_are_registered(self):
        import importlib
        import sys
        sys.path.insert(0, REPO)
        os.environ.setdefault("TG_SAVE_FOLDER",
                              tempfile.gettempdir() + "/tg_arch_guard")
        from tg_userbot import config
        reg = {n.lstrip("/") for n in config.REGISTERED_COMMAND_NAMES}
        panel, derived = _runtime_panel_and_registry()
        # registry 化后：面板必须与 registry 派生同源（结构性不变式）
        self.assertEqual(panel, derived,
                         "bot 面板与 command_registry 派生不一致——"
                         "BOT_COMMANDS 必须由 panel_commands() 派生")
        missing = sorted(panel - reg)
        self.assertEqual(
            missing, [],
            f"bot 命令面板注册了这些命令但没进 REGISTERED_COMMAND_NAMES："
            f"{missing}。面板展示与审计名单漂移，请补登记或下架面板项。")
