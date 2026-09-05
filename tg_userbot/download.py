"""普通媒体下载：download_file + 进行中下载注册表（/progress 用）。

下载模式：写到 final_path + ".download"，成功后 os.replace 原子改名；
重试间隔 3 秒、必要时重连，次数按错误类型封顶：普通失败 DOWNLOAD_RETRIES 次，
跨 DC 导出竞态（AuthBytesInvalidError）放宽到 + EXPORT_RACE_EXTRA_RETRIES。
注册表走 state.*（ACTIVE_DOWNLOADS / _download_seq / DOWNLOAD_SEMAPHORE）。
无环依赖：download 只引用叶子模块（naming/sources/history/config/log/state）。
"""
import os
import time
import asyncio
from datetime import datetime
from itertools import count

from telethon.errors import AuthBytesInvalidError, RPCError

from . import state
from . import workers
from .config import (
    DOWNLOAD_IDLE_TIMEOUT,
    DOWNLOAD_RETRIES,
    EXPORT_RACE_EXTRA_RETRIES,
    PROGRESS_STEP,
    SAVE_FOLDER,
)
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


async def _sleep_and_reconnect(worker):
    """重试前的固定 3 秒等待；传字节的连接（worker 或主客户端）断开则重连。"""
    logger.info("🔄 3 秒后重试下载...")
    await asyncio.sleep(3)
    transfer = worker or state.client
    try:
        if not transfer.is_connected():
            logger.info("🔌 Telegram 连接已断开，正在重新连接...")
            await transfer.connect()
            logger.info("✅ Telegram 重新连接成功")
    except Exception as e:
        logger.exception(f"重新连接 Telegram 失败：{e}")


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

        worker = None
        try:
            # 借一条下载专用连接（多 worker 池启用时）。信号量先于借 worker、
            # 并发下载数恒不大于存活 worker 数 → 不会饿死；None = 池禁用，
            # 照旧走主客户端单连接（原行为）。
            worker = await workers.borrow()
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

            # 重试不是固定 range(DOWNLOAD_RETRIES)，而是「成功即出、上限按错误
            # 类型给」的手数循环：AuthBytesInvalidError（跨 DC 首次导出竞态，见
            # config 的 EXPORT_RACE_EXTRA_RETRIES）秒级失败、可多试几次等竞争消散；
            # 其余失败仍按 DOWNLOAD_RETRIES 封顶。任何一次尝试成功都直接 return，
            # 到达各自上限的失败 break 到下方统一「达到最大重试次数」处理。
            for attempt in count(1):
                try:
                    # 清理上一次失败留下的临时文件
                    if os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass

                    last_percent = -1
                    last_activity = time.monotonic()  # 无进度看门狗的心跳

                    def progress(current, total):
                        nonlocal last_percent, last_activity
                        last_activity = time.monotonic()
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
                        f"⬇️ 下载尝试第 {attempt} 次"
                        f" | {os.path.basename(final_path)}"
                    )

                    # 传字节的连接：池启用时用 worker（独立 socket，避开主客户端
                    # 单 socket 的聚合瓶颈）；否则沿用消息自带客户端（原行为）。
                    # 消息 media 的 dc_id/access_hash/file_reference 都内嵌在消息
                    # 里，worker.download_media(message) 无需解析实体即可拉取。
                    # 调用包成 Task 再 await，外套「无进度看门狗」：请求没有读超时，
                    # 连接僵死时既不报错也不出数据，会永远占住信号量槽；超过
                    # DOWNLOAD_IDLE_TIMEOUT 无进度回调即取消本次、抛 TimeoutError
                    # （走重试分支重连重下）。
                    if worker is not None:
                        dl = worker.download_media(
                            message,
                            file=temp_path,
                            progress_callback=progress,
                        )
                    else:
                        dl = message.download_media(
                            file=temp_path,
                            progress_callback=progress,
                        )
                    dl_task = asyncio.ensure_future(dl)
                    try:
                        while not dl_task.done():
                            await asyncio.wait({dl_task}, timeout=1.0)
                            if dl_task.done():
                                break
                            if time.monotonic() - last_activity > DOWNLOAD_IDLE_TIMEOUT:
                                logger.warning(
                                    f"⏰ 下载超过 {DOWNLOAD_IDLE_TIMEOUT}s 无任何进度，"
                                    f"判定连接僵死，取消本次尝试（将重试）："
                                    f"{os.path.basename(final_path)}"
                                )
                                dl_task.cancel()
                                try:
                                    await dl_task
                                except asyncio.CancelledError:
                                    pass
                                raise TimeoutError(
                                    f"下载无进度超过 {DOWNLOAD_IDLE_TIMEOUT}s，已取消"
                                )
                        # 子任务结局一律经 result() 取：成功返回路径；若它以
                        # CancelledError 收场（网络层 future.cancel()），这里就地转成
                        # 可重试的 ConnectionError —— 不把网络层取消冒充成父任务取消
                        # 往上抛（那会让 except asyncio.CancelledError 无从分辨真假）。
                        try:
                            result = dl_task.result()
                        except asyncio.CancelledError as exc:
                            raise ConnectionError(
                                "下载被底层连接取消（网络层 future.cancel()）"
                            ) from exc
                    finally:
                        # 兜底：任何离开路径（含外层被真取消、异常打断 while）都要让
                        # 传字节的子任务先结束再归还 worker —— 否则会有一条游离下载
                        # 继续占用已还回空闲池的连接。
                        if not dl_task.done():
                            dl_task.cancel()
                            try:
                                await dl_task
                            except asyncio.CancelledError:
                                pass

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

                except asyncio.CancelledError:
                    # 能作为 CancelledError 到这里的只剩「父任务被真取消」：网络层的
                    # future.cancel() 已在 result() 处转成 ConnectionError。真取消 =
                    # 进程退出/外部主动中断 → 原样放行（让任务以取消收尾，队列记录
                    # 留在原处、重启后由 recover 重新执行）；finally 兜底清理临时文件。
                    raise

                except (ConnectionError, TimeoutError, OSError, RPCError) as e:
                    if isinstance(e, AuthBytesInvalidError):
                        # 跨 DC 首次授权导出竞态：失败在首字节前、秒级返回，重试极
                        # 便宜；上限放宽到 DOWNLOAD_RETRIES + EXPORT_RACE_EXTRA_
                        # RETRIES，等同一 DC 的竞争随成功者退出而消散后总有一次能赢。
                        cap = DOWNLOAD_RETRIES + EXPORT_RACE_EXTRA_RETRIES
                        logger.exception(
                            f"❌ 下载失败（跨 DC 授权导出竞态），尝试第 {attempt} 次"
                            f"（上限 {cap}）：{e}"
                        )
                    else:
                        cap = DOWNLOAD_RETRIES
                        logger.exception(
                            f"❌ 下载失败，尝试第 {attempt} 次（上限 {cap}）：{e}"
                        )
                    if attempt >= cap:
                        break
                    await _sleep_and_reconnect(worker)

                except Exception as e:
                    logger.exception(
                        f"❌ 下载出现未预期错误，尝试第 {attempt} 次"
                        f"（上限 {DOWNLOAD_RETRIES}）：{e}"
                    )
                    if attempt >= DOWNLOAD_RETRIES:
                        break
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

            return False

        finally:
            # 所有退出路径都清理 .download 半成品：成功路径 os.replace 已把它改名
            # 为最终文件（此处已不存在，no-op）；失败/取消路径由这里兜底删除，杜绝
            # 异常退出（例如 CancelledError 曾直接打出函数体）残留孤儿临时文件。
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            if worker is not None:
                await workers.release(worker)
            unregister_download(did)
            _RESERVED_FINAL_PATHS.discard(final_path)
