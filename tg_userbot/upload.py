"""文件上传到收藏夹：/up 命令与菜单 ⬆️ 上传视图共用的核心逻辑。

路径原样处理（不拆词，带空格不用加引号）：~ 展开，相对路径基于 /sh 的
工作目录（state.SHELL_CWD，与 /sh cd 联动）。上传走主客户端 send_file
("me")——文件落在收藏夹，任何设备都能取；进度按固定间隔原地编辑一条
消息（编辑太频繁会撞 Telegram 限流），完成后由调用方把最终回执写上。
"""
import asyncio
import os
import time

from . import config
from . import state
from .log import logger

USAGE_TEXT = (
    "⬆️ /up 文件上传（owner-only）\n\n"
    "用法：/up <文件路径>\n"
    "例：/up photo.jpg（相对路径，基于 /sh 工作目录）\n"
    "    /up ~/Desktop/report.pdf\n"
    "    /up /Volumes/V1/downloads/a.mp4\n"
    f"单文件上限 2GB；路径带空格不必加引号。"
)


def resolve_path(raw):
    """原样路径 → 规整绝对路径：strip、~ 展开、相对基于 /sh 工作目录。"""
    p = os.path.expanduser(raw.strip())
    if not os.path.isabs(p):
        p = os.path.join(state.SHELL_CWD, p)
    return os.path.abspath(p)


def validate_file(path):
    """→ (ok, 错误消息)。存在、是文件、不超单文件上限。"""
    if not os.path.exists(path):
        return False, f"❌ 文件不存在：{path}"
    if os.path.isdir(path):
        return False, f"❌ 这是目录，/up 只支持单个文件：{path}"
    size = os.path.getsize(path)
    if size > config.TG_UPLOAD_MAX_BYTES:
        return False, (
            f"❌ 文件超出 Telegram 单文件上限 2GB：\n{path}\n"
            f"（{size / 1024 ** 3:.2f} GB）"
        )
    return True, ""


def recent_files(directory, limit=None):
    """目录下最近修改的普通文件（不含子目录），新 → 旧。目录打不开返回 []。"""
    if limit is None:
        limit = config.UPLOAD_LIST_LIMIT
    try:
        entries = [
            os.path.join(directory, n) for n in os.listdir(directory)
        ]
        files = [p for p in entries if os.path.isfile(p)]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return files[:limit]
    except OSError as e:
        logger.warning(f"列出 {directory} 失败：{e}")
        return []


def progress_text(path, done, total):
    """上传中的进度文案（调用方拿去原地编辑）。"""
    name = os.path.basename(path)
    pct = int(done * 100 / total) if total else 0
    return f"⬆️ 正在上传：{name}\n{pct}%（{done}/{total} 字节）"


def receipt_text(path, size, elapsed):
    """完成回执文案。"""
    name = os.path.basename(path)
    return (
        f"✅ 已上传到收藏夹\n"
        f"📄 {name}\n"
        f"📦 {size} 字节\n"
        f"⏱ {elapsed:.1f} 秒"
    )


async def upload_with_progress(client, path, update):
    """上传单文件到收藏夹，期间按间隔调用 update(进度文案)。

    update 的首次调用延迟到第一个间隔之后（调用方已先展示 0% 状态，避免
    同文编辑触发 MessageNotModified）。返回 (ok, 最终文案)：成功是回执、
    失败是错误说明——无论成败都由调用方负责把最终文案写上。
    """
    total = os.path.getsize(path)
    box = {"done": 0}
    finished = False

    def _on_progress(done, _total):
        box["done"] = done

    async def _reporter():
        while True:
            await asyncio.sleep(config.UPLOAD_PROGRESS_EDIT_SECONDS)
            if finished:
                return
            await update(progress_text(path, box["done"], total))

    reporter = asyncio.ensure_future(_reporter())
    start = time.monotonic()
    try:
        await client.send_file(
            "me", path, progress_callback=_on_progress
        )
    except Exception as e:
        logger.warning(f"/up 上传失败 {path}：{e}")
        finished = True
        reporter.cancel()
        await asyncio.gather(reporter, return_exceptions=True)
        return False, f"❌ 上传失败：{e}"
    # 成功路径同样要先取消 reporter 再收尾：否则它在 sleep 间隔里醒不过来，
    # gather 会白等一个完整周期（回执耗时凭空多出几秒）
    finished = True
    reporter.cancel()
    await asyncio.gather(reporter, return_exceptions=True)
    elapsed = time.monotonic() - start
    return True, receipt_text(path, total, elapsed)


async def command_reply(event, cmd_text):
    """/up 命令完整流程（handle_command 调用）。

    无参数回用法；路径非法回错误；否则发一条「⬆️ 上传中」状态消息，上传
    期间原地编辑进度，完成后原地替换为最终回执。
    """
    raw = cmd_text[len("/up"):].strip()
    if not raw:
        await event.reply(USAGE_TEXT)
        return
    path = resolve_path(raw)
    ok, err = validate_file(path)
    if not ok:
        await event.reply(err)
        return
    total = os.path.getsize(path)
    logger.info(f"执行命令：/up {path}")
    status = await event.reply(progress_text(path, 0, total))

    async def _update(text):
        try:
            await status.edit(text)
        except Exception as e:
            logger.warning(f"/up 进度更新失败（忽略）：{e}")

    _done, final = await upload_with_progress(state.client, path, _update)
    await status.edit(final)


def candidate_at(arg):
    """菜单 up_file 序号 → 文件路径；失效返回错误说明。

    回调数据只有序号：快照是打开视图那一刻的 state.UP_CANDIDATES，
    上传前必须确认文件仍在（列表可能已过时）。
    """
    try:
        idx = int(arg)
    except (TypeError, ValueError):
        return None, "❌ 无效的文件序号"
    candidates = state.UP_CANDIDATES or []
    if not 0 <= idx < len(candidates):
        return None, "❌ 文件列表已变动，请 🔄 刷新后重试"
    path = candidates[idx]
    if not os.path.isfile(path):
        return None, "❌ 文件已不存在，请 🔄 刷新后重试"
    return path, ""
