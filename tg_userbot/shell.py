"""/sh 命令行执行器：在 Saved Messages 里远程跑 shell 命令。

owner-only（handle_command 只在 Saved Messages 触发）。任意命令 + 黑名单：
sudo/rm/mkfs/dd/kill 等高危命令直接拒绝；写 /dev/* 的重定向同样拦。
每条命令在独立子进程里跑（create_subprocess_shell，管道/通配符/重定向照常
可用），超时强杀；stdout+stderr 合并捕获，超长按 Telegram 4096 上限截断
（保头保尾）。

「当前目录」是持久化的：/sh cd 切换后写 runtime/shell_state.json，启动时
load_shell_cwd() 恢复——与 cleanup.load_clear_interval 同款模式。cd 在子进程
里跑不留痕，所以在这里拦截处理而不是丢给 shell。
"""
import asyncio
import json
import os
import re
import shlex

from . import config
from . import state
from .log import logger

# 高危命令黑名单（token 精确匹配）：想放开/收紧改这一个集合
BLACKLIST_COMMANDS = frozenset({
    "sudo", "rm", "mkfs", "dd", "shutdown", "reboot", "halt",
    "kill", "pkill", "killall", "launchctl", "diskutil", "csrutil",
})
# 写块设备：>/dev/sda、> /dev/disk0 …（token 拆不干净，按原文正则兜底）
_DEV_REDIRECT_RE = re.compile(r">\s*/dev/")

# 输出截断预算：Telegram 单条消息上限 4096，头部留 $ 命令回显/围栏/退出码，
# 正文保头保尾各 1600 字符
_OUTPUT_KEEP_HEAD = 1600
_OUTPUT_KEEP_TAIL = 1600

USAGE_TEXT = (
    "🖥 /sh 命令行（owner-only，黑名单拦截高危命令）\n\n"
    "用法：/sh <命令>\n"
    "例：/sh ls\n"
    "    /sh pwd\n"
    "    /sh df -h\n"
    "    /sh cd 目录 - 切换工作目录（会记住）\n"
    "    /sh cd - 查看当前工作目录\n"
    f"超时 {config.SHELL_TIMEOUT_SECONDS} 秒强杀；输出超长自动截断（保头保尾）。"
)

# 菜单 🖥 命令行视图的预设按钮（键 → 命令）：选 macOS / Termux 都安全的
PRESET_COMMANDS = {
    "ls": "ls -la",
    "df": "df -h .",
    "uptime": "uptime",
    # find 系列（2026-09-15 用户要求：终端命令行按名称查当前目录数据）
    "find . -maxdepth 3 -type f | head -60": "🔎 find 当前目录文件",
    "find . -maxdepth 3 -type d | head -40": "📁 find 子目录",
}


def sh_view_text():
    """🖥 命令行视图正文：当前工作目录 + 用法提示。"""
    return (
        "🖥 命令行\n\n"
        f"📂 当前工作目录：\n{state.SHELL_CWD}\n\n"
        "点按钮执行预设命令，或 ✏️ 输入自定义命令。\n"
        "按名称搜文件（✏️ 里发，替换关键词）：\n"
        'find . -iname \"*关键词\"\n'
        "（任意命令请直接在收藏夹发 /sh <命令>）"
    )


def is_shell_command(cmd_text):
    return cmd_text == "/sh" or cmd_text.startswith("/sh ")


def truncate_output(output):
    """超长输出保头保尾，中间标注省略的字符数。"""
    keep = _OUTPUT_KEEP_HEAD + _OUTPUT_KEEP_TAIL
    if len(output) <= keep:
        return output
    omitted = len(output) - keep
    return (
        output[:_OUTPUT_KEEP_HEAD]
        + f"\n…（中间省略 {omitted} 字符）…\n"
        + output[-_OUTPUT_KEEP_TAIL:]
    )


def _blacklisted_reason(raw, tokens):
    """返回命中黑名单的描述；未命中返回 None。"""
    for tok in tokens:
        if tok in BLACKLIST_COMMANDS:
            return f"禁用命令「{tok}」"
        if tok.startswith("/dev/"):
            return f"写块设备「{tok}」"
    match = _DEV_REDIRECT_RE.search(raw)
    if match:
        return "重定向写入 /dev/*"
    return None


async def _reply_for(raw, tokens):
    """组装一条 /sh 命令的回复文本（执行或拒绝）。"""
    if tokens[0] == "cd":
        return _handle_cd(tokens)
    reason = _blacklisted_reason(raw, tokens)
    if reason:
        return f"❌ /sh：命令包含{reason}，已拒绝执行。"
    return await _execute(raw)


def _handle_cd(tokens):
    if len(tokens) == 1:
        return (
            f"📂 当前工作目录：\n{state.SHELL_CWD}\n"
            "用法：/sh cd <目录>"
        )
    if not change_cwd(tokens[1]):
        return f"❌ /sh cd：目录不存在：{tokens[1]}"
    return f"📂 工作目录已切换：\n{state.SHELL_CWD}"


async def _run_subprocess(raw):
    """跑子进程，返回 (超时?, 退出码, 输出文本)。"""
    proc = await asyncio.create_subprocess_shell(
        raw,
        cwd=state.SHELL_CWD,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(), timeout=config.SHELL_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return True, None, ""
    return False, proc.returncode, out.decode("utf-8", errors="replace")


async def _execute(raw):
    try:
        timed_out, returncode, output = await _run_subprocess(raw)
    except ValueError as e:
        # shlex 引号不闭合等解析错误
        return f"❌ /sh：命令解析失败：{e}"
    except OSError as e:
        return f"❌ /sh：执行失败：{e}"

    lines = [f"$ {raw}"]
    if timed_out:
        lines.append(
            f"⏱ 执行超时（上限 {config.SHELL_TIMEOUT_SECONDS} 秒），"
            "已强制终止。"
        )
    elif returncode != 0:
        lines.append(f"（退出码 {returncode}）")

    body = truncate_output(output.rstrip("\n"))
    lines.append("```")
    lines.append(body if body else "（无输出）")
    lines.append("```")
    return "\n".join(lines)


async def command_reply(cmd_text):
    """handle_command 的入口：/sh <…> → 回复文本。"""
    raw = cmd_text[len("/sh"):].strip()
    if not raw:
        return USAGE_TEXT
    try:
        tokens = shlex.split(raw)
    except ValueError as e:
        return f"❌ /sh：命令解析失败：{e}"
    if not tokens:
        return USAGE_TEXT
    return await _reply_for(raw, tokens)


def load_shell_cwd():
    """启动时从 shell_state.json 恢复工作目录到 state.SHELL_CWD。

    文件缺失/损坏/目录已失效一律回退默认（REPO_ROOT）。
    """
    cwd = None
    try:
        if os.path.exists(config.SHELL_STATE_FILE):
            with open(config.SHELL_STATE_FILE, "r", encoding="utf-8") as f:
                cwd = json.load(f).get("cwd")
    except Exception as e:
        logger.warning(f"读取 /sh 工作目录失败，使用默认：{e}")
    if isinstance(cwd, str) and os.path.isdir(cwd):
        state.SHELL_CWD = cwd
    else:
        state.SHELL_CWD = config.REPO_ROOT


def _save_shell_cwd():
    try:
        with open(config.SHELL_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"cwd": state.SHELL_CWD}, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"保存 /sh 工作目录失败：{e}")


# ============================================================
# ls 文件夹浏览器（2026-09-14）：ls 输出里的目录渲染成可点击按钮，
# 点击 = change_cwd 进入 + 重新 ls——跑在 Telegram 里的迷你文件浏览器。
# 回调数据 ≤64 字节装不下长路径：按钮只带 hash8 键，真实路径存
# state.LS_PATHS（FIFO 淘汰）。
# ============================================================
_LS_PERM_RE = re.compile(r"^[dcb\-lps][rwxsStT\-+@]{9}")


def parse_ls_entries(output):
    """ls -la 输出 → 目录名列表（保序；文件/符号链接/坏行/./.. 排除）。"""
    dirs = []
    for line in output.splitlines():
        if not _LS_PERM_RE.match(line):
            continue
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        name = parts[8]
        if line[0] == "d" and name not in (".", ".."):
            dirs.append(name)
    return dirs


def extract_ls_dirs(cmd_text, reply_text, cwd):
    """命令是简单 ls → 从回复的围栏体解析目录并转绝对路径；否则 []。

    base 目录：命令带路径操作数（如 ls -la /x）用它，否则用 cwd；
    相对操作数按 cwd 拼。flags（-la -A…）不影响 base。"""
    try:
        tokens = shlex.split(cmd_text)
    except ValueError:
        return []
    if not tokens or tokens[0] != "ls":
        return []
    operands = [t for t in tokens[1:] if not t.startswith("-")]
    if operands:
        base = os.path.expanduser(operands[-1])
        if not os.path.isabs(base):
            base = os.path.join(cwd, base)
    else:
        base = cwd
    if reply_text.count("```") < 2:
        return []
    body = reply_text.split("```")[1].strip("\n")
    return [
        name if os.path.isabs(name) else os.path.join(base, name)
        for name in parse_ls_entries(body)
    ]


def register_ls_paths(paths):
    """路径 → hash8 回调键（FIFO 淘汰，上限 128），返回与入参对齐的键表。"""
    import hashlib
    tokens = []
    for p in paths:
        token = hashlib.md5(p.encode("utf-8")).hexdigest()[:8]
        while len(state.LS_PATHS) >= 128:
            state.LS_PATHS.pop(next(iter(state.LS_PATHS)))
        state.LS_PATHS[token] = p
        tokens.append(token)
    return tokens


def resolve_ls_dir(token):
    """hash8 → 绝对路径；未注册或目录已不存在返回 None。"""
    path = state.LS_PATHS.get(str(token or ""))
    if path and os.path.isdir(path):
        return path
    return None


def change_cwd(path):
    """切换并持久化工作目录；目录不存在返回 False（不改动现状）。

    相对路径基于当前 SHELL_CWD 解析（与 /sh cd 同语义）。"""
    p = os.path.expanduser(str(path or ""))
    if not os.path.isabs(p):
        p = os.path.join(state.SHELL_CWD, p)
    target = os.path.realpath(p)
    if not os.path.isdir(target):
        return False
    state.SHELL_CWD = target
    _save_shell_cwd()
    return True
