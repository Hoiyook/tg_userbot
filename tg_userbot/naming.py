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
    (1)(2)；前缀消息日期可天然防撞并带时间序。

    转发副本取「最初日期」：优先 fwd_from.date（转发来源那条消息的发送日
    期，如频道原帖日期），缺失才退回 message.date（转发时间）。Telegram 的
    日期是 UTC 感知时间，按本地时区渲染（= 客户端里看到的日期），避免晚间
    发布的内容差一天。日期缺失/异常时返回空串（不强加前缀）。纯函数、无 I/O。
    """
    try:
        fwd = getattr(message, "fwd_from", None)
        d = getattr(fwd, "date", None) if fwd is not None else None
        if d is None:
            d = getattr(message, "date", None)
        if d is None:
            return ""
        if d.tzinfo is not None:
            d = d.astimezone()
        return d.strftime("%y-%m-%d ")
    except Exception:
        return ""


def _label_piece(label) -> str:
    """把「手工转发标注」整理成带 # 前缀的标签段（无有效文本返回空串）。

    代码统一加 '#' 作标注前缀，与后接的原 caption 空格分隔（如
    '#自存 标题…'）。用户已自打 '#' 开头时去掉，避免 '##xx'；空串/只含
    '#' 等清理后无字可用的都返回空串——不能交给 sanitize_filename，它把
    空串兜成「未命名文件」，会让空标注误成 '#未命名文件'。
    """
    if label is None:
        return ""
    raw = str(label).strip()
    if not raw:
        return ""
    piece = sanitize_filename(raw).lstrip("#")
    return f"#{piece}" if piece else ""


def _fit_text_parts(label_piece, caption, budget_bytes):
    """把 label 段与 caption 段拼进 ≤ budget_bytes 的字节预算，返回文字段。

    裁剪优先级：先保 #标注（用户手工打的、通常很短）→ 从 caption 尾部整字
    裁（超长的是 douyin/IG 那一大段原说明）→ 标注自身也放不下时才最后裁标注。
    budget_bytes ≤ 0 或两者皆无时返回空串。
    """
    if budget_bytes <= 0:
        return ""
    if not caption:
        return _truncate_utf8_bytes(label_piece, budget_bytes)
    head = f"{label_piece} " if label_piece else ""
    if len(head.encode("utf-8")) + len(caption.encode("utf-8")) <= budget_bytes:
        return head + caption
    caption_budget = budget_bytes - len(head.encode("utf-8"))
    if caption_budget > 0:
        return head + _truncate_utf8_bytes(caption, caption_budget)
    # 标注本身已超出剩余预算：最后才动标注（仍整字截）
    return _truncate_utf8_bytes(label_piece, budget_bytes)


def _truncate_utf8_bytes(text, max_bytes):
    """把整段文本按 UTF-8 字节整字截短到 ≤ max_bytes（不丢半个字符）。"""
    if not text or max_bytes <= 0:
        return ""
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def compute_final_filename(message, caption=None, label=None, max_bytes=None) -> str:
    """根据消息计算最终落盘文件名（download_file 与队列展示共用）。

    规则：文件名开头加原消息日期前缀（'YY-MM-DD '，见 date_prefix）；有
    caption 用 caption 拼接原名；无意义文件名（未命名/UUID）用 caption 或
    媒体类型_时间戳 兜底；原名缺后缀时按 MIME 推断。唯一例外：纯兜底名
    媒体类型_时间戳 已含消息日期，不再前缀以免冗余。

    label 参数：手工转发时在输入框里打的评论（见 app.py 的待关联标注），代码
    加 '#' 前缀后与 caption 空格拼接，排在最前：'<date> #<label> <caption>…'。
    通常很短；真超长时随预算一起被裁（见 max_bytes）。

    caption 参数：显式传入覆盖「消息自身文字」作为命名用说明（默认 None =
    取消息自带 caption）。相册的转发副本无法补 caption，调用方把从源 chat 读
    到的同组说明传进来，无文字图片即可沿用相册标题命名而非媒体类型_时间戳。

    max_bytes 参数：非 None 时开启字节预算（download_file 传 MAX_FILENAME_BYTES），
    拼出超限名时按用户约定的优先级裁剪——文件名/日期前缀最后才动、先裁原
    caption、然后才裁 #标注。None（缺省，队列展示/单测用）不裁剪、照原样拼。
    """
    original_filename = sanitize_filename(get_original_filename(message))
    if caption is None:
        caption = get_caption(message)
    else:
        raw = str(caption or "").strip()
        caption = sanitize_filename(raw) if raw else ""

    label_piece = _label_piece(label)  # '#xxx' 或 ''

    extension = get_file_extension(message, original_filename)
    if extension and not os.path.splitext(original_filename)[1]:
        original_filename += extension

    prefix = date_prefix(message)
    meaningful = not is_meaningless_filename(original_filename)
    # 无意义文件名时的扩展名：原名带出的优先，否则按 MIME 推断的
    m_ext = os.path.splitext(original_filename)[1] or extension or ""

    if meaningful:
        base = original_filename

        def assemble(text_block):
            if text_block:
                return prefix + text_block + " - " + base
            return prefix + base
    else:
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

        def assemble(text_block):
            if text_block:
                return prefix + text_block + m_ext
            # 兜底名已含 媒体类型_时间戳（同为消息日期），不再前缀
            return generate_fallback_filename(message, kind) + m_ext

    text_block = " ".join(p for p in (label_piece, caption) if p)
    if max_bytes is None:
        return assemble(text_block)

    # ---- 字节预算版：先算「文字段」可用预算，保 日期前缀+文件名/后缀 ----
    if meaningful:
        overhead = len((prefix + " - " + base).encode("utf-8"))
    else:
        overhead = len((prefix + m_ext).encode("utf-8"))
    allowed_text = max_bytes - overhead
    if allowed_text > 0:
        return assemble(_fit_text_parts(label_piece, caption, allowed_text))
    # 文字段一点预算都分不到（极罕见：光原名就已超限）→ 退回无文字命名
    name = assemble("")
    if meaningful:
        # 原名自身也可能超限 → 最后手段才截文件名（保扩展名）
        return truncate_filename(name, max_bytes)
    return name


def compute_url_filename(title, created_at=None, label=None,
                         max_bytes=MAX_FILENAME_BYTES) -> str:
    """平台链接任务（本地解析链）的最终落盘文件名。

    与 compute_final_filename 同一套视觉规则：'<日期前缀> [#标注] <标题>.mp4'。
    差异：链接任务没有 Telegram 消息，没有「原始文件名」概念——
      * 标题（解析元数据的 desc）就是 caption 源；为空时兜底 '视频_时间戳'；
      * 日期前缀取任务创建时间（created_at，本地时区），非消息日期；
      * 后缀固定 .mp4（当前解析链只下载抖音视频）。
    max_bytes 字节预算语义一致：先裁标题、再裁 #标注、最后才动前缀/兜底名。
    纯函数、无 I/O，入队展示与实际下载命名共用（final_name 在入队时算好）。
    """
    prefix = ""
    if created_at is not None:
        prefix = created_at.strftime("%y-%m-%d ")

    raw = str(title or "").strip()
    caption = sanitize_filename(raw) if raw else ""
    label_piece = _label_piece(label)

    if not caption:
        # 兜底名：与媒体流的 媒体类型_时间戳 风格一致，已含时间不再前缀
        stamp = (created_at or datetime.now()).strftime("%Y%m%d_%H%M%S")
        return f"视频_{stamp}.mp4"

    base = ".mp4"

    def assemble(text_block):
        if text_block:
            return prefix + text_block + base
        # 无文字段（极罕见）时去掉日期前缀的尾空格：'26-09-06.mp4'
        return prefix.rstrip() + base

    text_block = " ".join(p for p in (label_piece, caption) if p)
    if max_bytes is None:
        return assemble(text_block)

    overhead = len((prefix + base).encode("utf-8"))
    allowed_text = max_bytes - overhead
    if allowed_text > 0:
        return assemble(_fit_text_parts(label_piece, caption, allowed_text))
    # 标题+标注一点预算都分不到（极罕见）→ 至少保留日期前缀，按预算截
    return _truncate_utf8_bytes(assemble("").rstrip(), max_bytes)
