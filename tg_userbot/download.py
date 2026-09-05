"""普通媒体下载：download_file + 进行中下载注册表（/progress 用）。

下载模式：写到 final_path + ".download"，成功后 os.replace 原子改名；
重试 DOWNLOAD_RETRIES 次、间隔 3 秒，必要时重连。注册表走 state.*
（ACTIVE_DOWNLOADS / _download_seq / DOWNLOAD_SEMAPHORE）。
无环依赖：download 只引用叶子模块（naming/sources/history/config/log/state）。
"""
import os
import asyncio
from datetime import datetime

from telethon.errors import RPCError

from . import state
from .config import DOWNLOAD_RETRIES, PROGRESS_STEP, SAVE_FOLDER
from .log import logger
from .history import append_history
from .naming import (
    compute_final_filename,
    format_size,
    get_caption,
    get_original_filename,
    sanitize_filename,
    truncate_filename,
)
from .sources import message_source_link, resolve_download_source

# 已占用的最终下载路径（进程内）：download_file 在写 .download 前占位，避免
# 同 basename 的并发任务（如同一相册的多张同标题图片）共用一条 final_path，
# 写完 finally 释放。与磁盘 os.path.exists 去重互补。
_RESERVED_FINAL_PATHS = set()


def _reserve_final_path(folder, filename):
    """挑选并占位一条最终下载路径（并发安全 + 磁盘重名去重）。

    逻辑与 naming.unique_path 相同（已存在则依次加 (1)(2)...），但额外把
    选中的路径登记进进程内集合，同一时刻不同任务不会选出同一条路径。
    """
    base = os.path.join(folder, filename)
    path = base
    index = 0
    while path in _RESERVED_FINAL_PATHS or os.path.exists(path):
        index += 1
        stem, ext = os.path.splitext(base)
        path = f"{stem} ({index}){ext}"
    _RESERVED_FINAL_PATHS.add(path)
    return path


def register_download(label, filename, total, link=None):
    """登记一个开始下载的任务，返回下载 ID。link 为来源消息链接（可选）。"""
    state._download_seq += 1
    did = state._download_seq
    state.ACTIVE_DOWNLOADS[did] = {
        "label": label,
        "filename": filename,
        "total": total,
        "downloaded": 0,
        "percent": 0,
        "link": link,
    }
    return did


def update_download(did, current, total):
    """更新下载进度（由 progress_callback 调用）。"""
    info = state.ACTIVE_DOWNLOADS.get(did)
    if info is None:
        return
    info["downloaded"] = current
    if total:
        info["total"] = total
        info["percent"] = min(int(current * 100 / total), 100)
    else:
        info["percent"] = None


def unregister_download(did):
    state.ACTIVE_DOWNLOADS.pop(did, None)


async def download_file(message, source_override=None, caption_override=None):
    async with state.DOWNLOAD_SEMAPHORE:
        source = await resolve_download_source(message, source_override)
        # 命名用 caption：消息自带文字优先；否则用调用方继承的相册同组说明
        # （转发副本无 caption，图片名靠它避免落到 媒体类型_时间戳 兜底名）
        own_caption = get_caption(message)
        caption = own_caption or (caption_override or "")
        # 超长标题/说明会拼出超 255 字节的文件名（Errno 63），按字节整字截短
        final_filename = truncate_filename(
            compute_final_filename(message, caption=caption or None)
        )
        # 日志展示用（与 final_filename 的计算共用同一套规则）
        original_filename = sanitize_filename(get_original_filename(message))

        folder = os.path.join(SAVE_FOLDER, source)
        os.makedirs(folder, exist_ok=True)

        # 最终路径做「进程内占位 + 磁盘重名」双保险：相册里多张图片共享同一
        # 继承标题、原名又都无意义时，各任务算出相同的 basename——并发下载会
        # 撞上同一个 final_path/.download（一方 os.replace 后，另一方报
        # 「临时文件不存在」）。先占位再下载可避免；串行时 os.path.exists
        # 兜底加 (1)(2)，语义与 naming.unique_path 一致。
        final_path = _reserve_final_path(folder, final_filename)
        temp_path = final_path + ".download"

        size = None
        try:
            size = message.file.size if message.file else None
        except Exception:
            pass

        # 登记进行中下载（/progress 可见，带来源链接）
        did = register_download(
            "普通",
            os.path.basename(final_path),
            size,
            link=message_source_link(message, message.chat_id),
        )

        try:
            logger.info("=" * 60)
            logger.info("📥 开始下载")
            logger.info(f"消息 ID：{message.id}")
            logger.info(f"来源：{source}")
            logger.info(f"Caption：{caption or '(无)'}")
            logger.info(f"原始文件名：{original_filename}")
            logger.info(f"最终文件名：{os.path.basename(final_path)}")
            logger.info(f"文件大小：{format_size(size)}")
            logger.info(f"保存目录：{folder}")

            # 开始下载即发通知（程序消息稍后会被自动清理）
            try:
                await state.client.send_message(
                    "me",
                    "📥 开始下载\n\n"
                    f"来源：{source}\n"
                    f"文件：{os.path.basename(final_path)}\n"
                    f"大小：{format_size(size)}",
                )
            except Exception as e:
                logger.warning(f"发送下载开始通知失败：{e}")

            for attempt in range(1, DOWNLOAD_RETRIES + 1):
                try:
                    # 清理上一次失败留下的临时文件
                    if os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass

                    last_percent = -1

                    def progress(current, total):
                        nonlocal last_percent
                        update_download(did, current, total)

                        if not total:
                            return

                        percent = int(current * 100 / total)
                        percent = min(percent, 100)

                        if percent >= last_percent + PROGRESS_STEP or percent == 100:
                            last_percent = percent
                            logger.info(
                                f"⬇️ 下载进度：{percent}% "
                                f"({format_size(current)}/{format_size(total)})"
                                f" | {os.path.basename(final_path)}"
                            )

                    logger.info(
                        f"⬇️ 下载尝试 {attempt}/{DOWNLOAD_RETRIES}"
                        f" | {os.path.basename(final_path)}"
                    )

                    result = await message.download_media(
                        file=temp_path,
                        progress_callback=progress,
                    )

                    if not result or not os.path.exists(temp_path):
                        raise RuntimeError("Telegram 返回下载结果，但临时文件不存在")

                    actual_size = os.path.getsize(temp_path)
                    if actual_size <= 0:
                        raise RuntimeError("下载完成，但文件大小为 0")

                    # 大小一致性校验：实际大小明显小于消息声明大小时打警告，
                    # 便于排查「客户端显示大、下载却小」的问题。
                    if size and actual_size < size * 0.98:
                        logger.warning(
                            f"⚠️ 实际下载大小（{format_size(actual_size)}）"
                            f"小于消息声明大小（{format_size(size)}），"
                            "可能是客户端显示的是原始大小，而 Telegram "
                            "存储的是转码后的文件"
                        )

                    os.replace(temp_path, final_path)

                    # 记录下载历史（每行一条）
                    append_history(
                        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 普通 | "
                        f"{os.path.basename(final_path)} | {format_size(actual_size)}"
                        f" | 来源：{source}"
                    )

                    logger.info("✅ 下载完成")
                    logger.info(f"文件：{final_path}")
                    logger.info(f"实际大小：{format_size(actual_size)}")
                    logger.info("=" * 60)

                    try:
                        await state.client.send_message(
                            "me",
                            "✅ 下载完成\n\n"
                            f"来源：{source}\n"
                            f"文件：{os.path.basename(final_path)}\n"
                            f"大小：{format_size(actual_size)}",
                        )
                    except Exception as e:
                        logger.warning(f"发送完成通知失败：{e}")

                    return True

                except (ConnectionError, TimeoutError, OSError, RPCError) as e:
                    logger.exception(
                        f"❌ 下载失败，尝试 {attempt}/{DOWNLOAD_RETRIES}：{e}"
                    )

                    if attempt < DOWNLOAD_RETRIES:
                        logger.info("🔄 3 秒后重试下载...")
                        await asyncio.sleep(3)

                        try:
                            if not state.client.is_connected():
                                logger.info("🔌 Telegram 连接已断开，正在重新连接...")
                                await state.client.connect()
                                logger.info("✅ Telegram 重新连接成功")
                        except Exception as reconnect_error:
                            logger.exception(
                                f"重新连接 Telegram 失败：{reconnect_error}"
                            )

                except Exception as e:
                    logger.exception(
                        f"❌ 下载出现未预期错误，尝试 {attempt}/{DOWNLOAD_RETRIES}：{e}"
                    )

                    if attempt < DOWNLOAD_RETRIES:
                        logger.info("🔄 3 秒后重试下载...")
                        await asyncio.sleep(3)

            logger.error("❌ 已达到最大重试次数，下载失败")

            try:
                await state.client.send_message(
                    "me",
                    "❌ 文件下载失败\n\n"
                    f"来源：{source}\n"
                    f"文件：{os.path.basename(final_path)}\n"
                    f"请查看 download.log",
                )
            except Exception:
                pass

            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

            return False

        finally:
            unregister_download(did)
            _RESERVED_FINAL_PATHS.discard(final_path)
