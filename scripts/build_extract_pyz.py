#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建独立解压工具 extract.pyz（手机 Termux / 任意机器跑）。

把 tg_userbot 的解压核心（extract_dir.py + extract_util.py，**逐字复制**，
单一真相源在项目里——改完源码重跑本脚本重新出包）打包成零依赖单文件
zipapp。垫片只补两个包内依赖：

* ``log``    → 薄版（logging 输出 stderr；项目版有轮转文件/追踪，CLI 不需要）
* ``config`` → 薄版（extract_util 引用的 6 个守卫常量内置默认值，
               可用环境变量覆盖）

用法（构建）::
    python3 scripts/build_extract_pyz.py          # 产出 dist/extract.pyz

用法（手机/任意机器）::
    pkg install python p7zip libarchive           # Termux 依赖
    python3 extract.pyz <目录> [--dry] [--timeout 3600]
"""
import os
import shutil
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "tg_userbot")
OUT = os.path.join(ROOT, "dist", "extract.pyz")

# 包版本：每次出包手动 +1（手机排查「跑的是哪版」全靠运行首行横幅——
# 2026-11-07 Alicexi 排障实测：旧包覆盖失败连跑三轮同款报错）。
#   1 初版（zip+bsdtar）
#   2 魔数嗅探（伪装 .zip 按 rar/7z 签名分流）
#   3 SFX 深扫描（前 4MB 搜内嵌签名）+ 7z 成功删原包
VERSION = "3"

# extract_util 引用的守卫常量：内置默认（与项目 config.py 现值一致），
# 环境变量 EXTRACT_* 可覆盖（手机上想放宽守卫时不用改脚本）
CONFIG_STUB = '''"""薄版 config（zipapp 垫片）：只提供 extract_util 用到的守卫常量。

默认值与项目 config.py 现值一致；环境变量覆盖：
  EXTRACT_MIN_FREE_GB / EXTRACT_ZIP_MAX_ENTRIES /
  EXTRACT_ZIP_MAX_MEMBER_BYTES / EXTRACT_ZIP_MAX_RATIO
"""
import os as _os

PAWCHIVE_MIN_FREE_GB = float(
    _os.environ.get("EXTRACT_MIN_FREE_GB", 2.0))
PAWCHIVE_ZIP_MAX_ENTRIES = int(
    _os.environ.get("EXTRACT_ZIP_MAX_ENTRIES", 20000))
PAWCHIVE_ZIP_MAX_MEMBER_BYTES = int(
    _os.environ.get("EXTRACT_ZIP_MAX_MEMBER_BYTES", 8 * 1024 ** 3))
PAWCHIVE_ZIP_MAX_RATIO = int(
    _os.environ.get("EXTRACT_ZIP_MAX_RATIO", 500))
'''

LOG_STUB = '''"""薄版 log（zipapp 垫片）：logging 输出 stderr。

项目版有每日轮转文件 + 队列任务追踪标记——CLI 单发场景用不上，垫片
保持同名 logger 接口（logger.info/warning/...）让 extract_dir /
extract_util 的源码零改动。"""
import logging
import sys

logger = logging.getLogger("extract_cli")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s",
                                      "%m-%d %H:%M:%S"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
'''

MAIN_STUB = '''"""extract.pyz 入口：/extract 命令的同款语义（递归解压目录）。"""
import sys

from _extract_pkg.version import VERSION
from _extract_pkg.extract_dir import extract_directory


def main(argv):
    print(f"extract.pyz v{VERSION}")   # 排障横幅：一眼确认手机跑的哪版
    arg = " ".join(a for a in argv if not a.startswith("--"))
    dry = "--dry" in argv
    timeout = 3600
    for i, a in enumerate(argv):
        if a == "--timeout" and i + 1 < len(argv):
            timeout = int(argv[i + 1])
    if not arg.strip():
        print("用法：extract.pyz <目录> [--dry] [--timeout 秒]")
        print("  递归解压目录下的压缩包到同名目录（成功才删压缩包；")
        print("  密码自动取：文件名 # 段 / 密码*.txt 提示；rar 走 7z）")
        return 2
    lines, st = extract_directory(arg.strip(), dry=dry, timeout=timeout)
    for line in lines:
        print(line)
    return 0 if st["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def main():
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("__main__.py", MAIN_STUB)
        z.writestr("_extract_pkg/version.py", f'VERSION = "{VERSION}"\n')
        z.writestr("_extract_pkg/__init__.py", "")
        # 源模块逐字复制（单一真相源）
        for name in ("extract_dir.py", "extract_util.py"):
            with open(os.path.join(PKG, name), encoding="utf-8") as f:
                z.writestr(f"_extract_pkg/{name}", f.read())
        z.writestr("_extract_pkg/log.py", LOG_STUB)
        z.writestr("_extract_pkg/config.py", CONFIG_STUB)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "wb") as f:
        f.write(b"#!/usr/bin/env python3\n")
        f.write(buf.getvalue())
    os.chmod(OUT, 0o755)
    print(f"✓ {OUT}（{os.path.getsize(OUT)} 字节）")
    print("  手机（Termux）：pkg install python p7zip libarchive"
          " → python3 extract.pyz <目录>")


if __name__ == "__main__":
    main()
