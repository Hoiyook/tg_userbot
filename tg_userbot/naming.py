"""命名 / 文件名相关纯函数。

全部无 I/O、不依赖运行态：sanitize/去重/后缀推断/兜底文件名，
以及共享的最终落盘名计算 compute_final_filename（download_file 与
队列展示共用）。UUID_FILENAME_PATTERN 等只读常量来自 config。
"""
import os
import re
import mimetypes
from datetime import datetime

from .config import MAX_FILENAME_BYTES, UUID_FILENAME_PATTERN
from .log import logger


def sanitize_filename(name: str) -> str:
    """清理 Android / Windows 不适合出现在文件名中的字符。"""
    if not name:
        return "未命名文件"

    name = str(name).strip()

    # Android 常见非法/不安全字符
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)

    # 去掉连续空格
    name = re.sub(r"\s+", " ", name).strip()

    # 避免文件名末尾出现空格或点
    name = name.rstrip(" .")

    return name or "未命名文件"


def unique_path(path: str) -> str:
    """文件重名时自动增加 (1)、(2)..."""
    if not os.path.exists(path):
        return path

    base, ext = os.path.splitext(path)
    index = 1

    while True:
        new_path = f"{base} ({index}){ext}"
        if not os.path.exists(new_path):
            return new_path
        index += 1


def truncate_filename(name: str, max_bytes: int = None) -> str:
    """把文件名（可含扩展名）按 UTF-8 字节数整字截短到 ≤ max_bytes。

    macOS/Android 单文件名上限为 255 字节，超长标题/说明拼进文件名会抛
    OSError(Errno 63 File name too long)。这里只截「主体」，保留扩展名，
    并在字节边界处整字截断（不会留下半个 UTF-8 字符）。不超过上限时原样返回。
    """
    if max_bytes is None:
        max_bytes = MAX_FILENAME_BYTES

    name = str(name or "")
    if len(name.encode("utf-8")) <= max_bytes:
        return name

    stem, ext = os.path.splitext(name)
    ext_bytes = len(ext.encode("utf-8"))
    keep = max_bytes - ext_bytes
    if keep <= 0:
        # 极端情况：仅扩展名就已超限，直接按字节硬截。
        return ext.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")

    # 按字节截主体，errors="ignore" 会丢弃末尾半个字符，保证结果是合法 UTF-8
    cut = stem.encode("utf-8")[:keep].decode("utf-8", errors="ignore")
    return cut + ext


def format_size(size):
    if size is None:
        return "未知大小"

    size = float(size)
    units = ["B", "KB", "MB", "GB", "TB"]

    for unit in units:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024

    return f"{size:.2f} PB"


def is_meaningless_filename(name: str) -> bool:
    """判断文件名是否无意义（空、未命名、随机 UUID/hex 等）。"""
    if not name:
        return True

    base = os.path.splitext(name.strip())[0]
    if base in ("未命名文件", "未命名"):
        return True

    return bool(UUID_FILENAME_PATTERN.match(name.strip()))


def generate_fallback_filename(message, kind: str) -> str:
    """用媒体类型 + 消息时间生成兜底文件名，如 video_20260904_021530。"""
    try:
        if message.date:
            stamp = message.date.strftime("%Y%m%d_%H%M%S")
        else:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    except Exception:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{kind}_{stamp}"


def get_original_filename(message) -> str:
    """尽可能完整地获取 Telegram 原始文件名。"""
    try:
        if message.file:
            name = message.file.name
            if name:
                return name

        document = getattr(message, "document", None)
        if document:
            for attr in getattr(document, "attributes", []) or []:
                name = getattr(attr, "file_name", None)
                if name:
                    return name
    except Exception:
        logger.exception("获取原始文件名失败")

    return "未命名文件"


def get_file_extension(message, filename: str) -> str:
    """原文件名没有后缀时，根据媒体/MIME 类型自动补后缀。"""
    if filename and os.path.splitext(filename)[1]:
        return ""

    try:
        if message.photo:
            return ".jpg"

        mime = getattr(message.file, "mime_type", None) if message.file else None

        if message.voice:
            return ".ogg"

        if message.video:
            known = {
                "video/mp4": ".mp4",
                "video/webm": ".webm",
                "video/x-matroska": ".mkv",
                "video/quicktime": ".mov",
                "video/x-msvideo": ".avi",
            }
            return known.get(mime) or mimetypes.guess_extension(mime or "") or ".mp4"

        if message.audio:
            known = {
                "audio/mpeg": ".mp3",
                "audio/mp4": ".m4a",
                "audio/x-m4a": ".m4a",
                "audio/ogg": ".ogg",
                "audio/wav": ".wav",
                "audio/x-wav": ".wav",
                "audio/flac": ".flac",
            }
            return known.get(mime) or mimetypes.guess_extension(mime or "") or ".mp3"

        if mime:
            return mimetypes.guess_extension(mime) or ""

    except Exception as e:
        logger.warning(f"自动判断文件后缀失败：{e}")

    return ""


def get_caption(message) -> str:
    """获取 Caption / 消息文字；无 caption 返回空串。

    注意不能用 sanitize_filename("") 的兜底值「未命名文件」——
    那会把所有无说明文件的文件名都加上「未命名文件 - 」前缀。
    """
    try:
        text = (message.message or "").strip()
        return sanitize_filename(text) if text else ""
    except Exception:
        return ""


def pick_group_caption_text(messages, grouped_id) -> str:
    """在相册的兄弟消息里挑出带文字的成员文本（无则空串）。

    Telegram 相册的说明文字只挂在其中一个成员上（通常是视频/最后一张），
    其余成员（如图片）本身没有 caption，而转发副本无法补 caption。这里给
    「同 grouped_id 且有文字」的兄弟取文本，供调用方继承到无文字成员上，
    下载命名时套用，让整组文件共用可读标题而不落 媒体类型_时间戳 兜底名。
    纯函数、无 I/O。
    """
    if not grouped_id or not messages:
        return ""
    for m in messages:
        try:
            if getattr(m, "grouped_id", None) == grouped_id:
                text = (getattr(m, "message", None) or "").strip()
                if text:
                    return text
        except Exception:
            continue
    return ""


def date_prefix(message) -> str:
    """原消息日期前缀，形如 '26-09-05 '（`%y-%m-%d `），供文件名开头排序/防重名。

    统一下载链路后抖音/IG 视频等都用通用命名落同来源目录，同名会撞出
    (1)(2)；前缀消息日期可天然防撞并带时间序。message.date 缺失/异常时
    返回空串（不强加前缀）。纯函数、无 I/O。
    """
    try:
        d = getattr(message, "date", None)
        if d is None:
            return ""
        return d.strftime("%y-%m-%d ")
    except Exception:
        return ""


def compute_final_filename(message, caption=None) -> str:
    """根据消息计算最终落盘文件名（download_file 与队列展示共用）。

    规则：文件名开头加原消息日期前缀（'YY-MM-DD '，见 date_prefix）；有
    caption 用 caption 拼接原名；无意义文件名（未命名/UUID）用 caption 或
    媒体类型_时间戳 兜底；原名缺后缀时按 MIME 推断。唯一例外：纯兜底名
    媒体类型_时间戳 已含消息日期，不再前缀以免冗余。

    caption 参数：显式传入覆盖「消息自身文字」作为命名用说明（默认 None =
    取消息自带 caption）。相册的转发副本无法补 caption，调用方把从源 chat 读
    到的同组说明传进来，无文字图片即可沿用相册标题命名而非媒体类型_时间戳。
    """
    original_filename = sanitize_filename(get_original_filename(message))
    if caption is None:
        caption = get_caption(message)
    else:
        caption = sanitize_filename(str(caption or "").strip()) if str(caption or "").strip() else ""

    extension = get_file_extension(message, original_filename)
    if extension and not os.path.splitext(original_filename)[1]:
        original_filename += extension

    prefix = date_prefix(message)

    if is_meaningless_filename(original_filename):
        ext = os.path.splitext(original_filename)[1] or extension or ""
        if caption:
            return prefix + sanitize_filename(caption + ext)

        if message.voice:
            kind = "voice"
        elif message.video:
            kind = "video"
        elif message.photo:
            kind = "photo"
        elif message.audio:
            kind = "audio"
        else:
            kind = "file"
        # 兜底名已含 媒体类型_时间戳（同为消息日期），不再前缀
        return generate_fallback_filename(message, kind) + ext

    if caption:
        return prefix + sanitize_filename(f"{caption} - {original_filename}")
    return prefix + original_filename
