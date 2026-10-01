"""解压工具：pawchive 与 115 解压回传共用的压缩包解压核心。

2026-09-30 从 pawchive_worker 抽出并参数化（dest / delete_source），行为
与原版保持一致（zip 用 stdlib：加密位检测 + 炸弹守卫 + staging 改名；
rar/7z 等用 bsdtar：先列表探测，失败=需密码/损坏）。差异增强：

* cp437→GBK 文件名修复：Windows 中文 zip 不带 UTF-8 标志位（flag_bits
  & 0x800 未置），stdlib 按 cp437 解出乱码——roundtrip 转回 GBK 成功即替换。
* zip slip 防护：成员路径含 ``..`` / 绝对路径 / 盘符的一律跳过（115x 的
  包来自任意来源，不像 pawchive 是站点受信内容）。

阻塞函数，调用方放线程（pawchive 的串行锁语义保留在 pawchive_worker）。
"""
import os
import re
import shutil

from . import config
from .log import logger


def _archive_dest(archive_path):
    """压缩包 → 解压目标文件夹（同名去扩展名）。"""
    return os.path.splitext(archive_path)[0]


def _safe_member_path(dest, name):
    """zip 成员名 → dest 下的安全路径；不可信（穿越/空落点）返回 None。

    Windows 压缩包常见 ``\\`` 分隔与 ``C:\\`` 前缀，先归一再拆分；绝对路径
    与盘符被钳制进 dest 内（保留数据），``..`` 穿越**拒绝**（静默丢弃会让
    ``a/../x`` 与 ``b/../x`` 碰撞同名，后者覆盖前者——丢数据不可接受）。
    """
    name = str(name).replace("\\", "/")
    name = re.sub(r"^[A-Za-z]:", "", name)
    if any(p == ".." for p in name.split("/")):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts:
        return None
    return os.path.join(dest, *parts)


def _fix_zip_name(zi):
    """非 UTF-8 标志的成员名按 cp437 读出，roundtrip 转 GBK 成功即修复。"""
    if zi.flag_bits & 0x800:
        return zi.filename
    try:
        fixed = zi.filename.encode("cp437").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return zi.filename
    return fixed if fixed != zi.filename else zi.filename


def extract_archive(archive_path, dest=None, delete_source=False):
    """解压单个压缩包（阻塞，线程内执行）。返回 (status, detail, files)。

    status: "extracted"（成功）/ "password"（需密码，保留）/
            "failed"（解压出错，保留）/ "no-space"（磁盘不足，保留）
    detail: 人读的原因/摘要；files: 成功时解压产物的绝对路径列表。
    dest 缺省 = 同名去扩展名目录（原 pawchive 行为）；delete_source=True
    时成功后删除压缩包（pawchive 场景；115x 的本地副本保留供重试幂等）。
    """
    import zipfile
    lower = archive_path.lower()
    if dest is None:
        dest = _archive_dest(archive_path)
    free = shutil.disk_usage(os.path.dirname(dest) or ".").free
    margin = int(config.PAWCHIVE_MIN_FREE_GB) * 1024 ** 3

    if lower.endswith(".zip"):
        try:
            with zipfile.ZipFile(archive_path) as z:
                infos = z.infolist()
                if any(zi.flag_bits & 0x1 for zi in infos):
                    return "password", "zip 成员加密", []
                total_unc = sum(zi.file_size for zi in infos)
                total_comp = sum(zi.compress_size for zi in infos)
                if len(infos) > config.PAWCHIVE_ZIP_MAX_ENTRIES:
                    return ("failed",
                            f"zip 成员数 {len(infos)} 超限"
                            f"（>{config.PAWCHIVE_ZIP_MAX_ENTRIES}，疑似炸弹）",
                            [])
                biggest = max((zi.file_size for zi in infos), default=0)
                if biggest > config.PAWCHIVE_ZIP_MAX_MEMBER_BYTES:
                    return ("failed",
                            f"zip 单成员 {biggest/1e9:.1f}GB 超限"
                            f"（>{config.PAWCHIVE_ZIP_MAX_MEMBER_BYTES/1e9:.0f}GB）",
                            [])
                if total_comp and total_unc / total_comp > \
                        config.PAWCHIVE_ZIP_MAX_RATIO:
                    return ("failed",
                            f"压缩比 {total_unc // max(total_comp,1)}x 超限"
                            f"（>{config.PAWCHIVE_ZIP_MAX_RATIO}x，疑似炸弹）",
                            [])
                if total_unc and total_unc > free - margin:
                    return ("no-space",
                            f"解压需 {total_unc/1e9:.1f}GB 超出磁盘余量", [])
                # staging 目录解压，成功后整体改名——失败留下的半成品不会
                # 混进正式目录（P1-8 语义）
                staging = dest + ".extracting"
                if os.path.isdir(staging):
                    shutil.rmtree(staging, ignore_errors=True)
                os.makedirs(staging, exist_ok=True)
                for zi in infos:
                    target = _safe_member_path(staging, _fix_zip_name(zi))
                    if target is None:
                        logger.warning(f"🗜 跳过不可信 zip 成员：{zi.filename!r}")
                        continue
                    if zi.is_dir():
                        os.makedirs(target, exist_ok=True)
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with z.open(zi) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
                os.replace(staging, dest)
        except RuntimeError as e:
            # stdlib 读到加密成员时的典型异常
            return "password", str(e)[:120], []
        except Exception as e:
            return "failed", f"{type(e).__name__}: {e}", []
    else:  # .rar/.7z/.tar 系 → bsdtar（macOS/Termux 自带；探测失败=需密码/损坏）
        import subprocess
        try:
            probe = subprocess.run(
                ["bsdtar", "-tf", archive_path],
                capture_output=True, timeout=120,
                stdin=subprocess.DEVNULL)
            if probe.returncode != 0:
                # 加密/损坏压缩包无法列出成员 → 需密码，原样保留
                return "password", (probe.stderr.decode("utf-8", "replace")
                                    or "list failed")[:120], []
        except subprocess.TimeoutExpired:
            return "password", "list 超时（疑似加密卷）", []
        except FileNotFoundError:
            return "failed", "bsdtar 不可用", []

        try:
            staging = dest + ".extracting"
            if os.path.isdir(staging):
                shutil.rmtree(staging, ignore_errors=True)
            os.makedirs(staging, exist_ok=True)
            p = subprocess.run(
                ["bsdtar", "-xf", archive_path, "-C", staging],
                capture_output=True, timeout=600,
                stdin=subprocess.DEVNULL)
            if p.returncode != 0:
                return "failed", \
                    p.stderr.decode("utf-8", "replace")[:120], []
            os.replace(staging, dest)
        except subprocess.TimeoutExpired:
            return "failed", "bsdtar 解压超时", []
        except FileNotFoundError:
            return "failed", "bsdtar 不可用", []

    # 成功判定：目标目录非空
    files = []
    for root, _dirs, names in os.walk(dest):
        for n in names:
            files.append(os.path.join(root, n))
    if not files:
        return "failed", "解压后目录为空", []
    if delete_source:
        try:
            os.remove(archive_path)
        except OSError as e:
            logger.warning(f"🗜 解压成功但删除压缩包失败：{e}")
    return "extracted", f"{len(files)} 个文件", files
