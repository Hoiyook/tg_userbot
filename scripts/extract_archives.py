#!/usr/bin/env python3
"""递归解压指定目录下的压缩包（独立 CLI，核心在 tg_userbot/extract_dir.py）。

规则（与 bot 内 Pawchive 自动解压同一套安全纪律）:
  * 递归遍历目录树，压缩包 → 解压到**同名目录**（foo.zip → foo/）
  * **解压成功才删压缩包**；失败/需密码/损坏一律原样保留
  * 同名目录已存在且有内容 → 跳过（幂等）；zip 炸弹防护；加密跳过
  * --dry 预览模式；单包失败不中断整轮

用法:
  python3 scripts/extract_archives.py [目录] [--dry] [--timeout 秒]

注意: 本壳 import tg_userbot.extract_dir（依赖仓库检出）。核心逻辑
（含 /extract 命令、app 指令中心）单一来源在 tg_userbot/extract_dir.py。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tg_userbot.extract_dir import extract_directory   # noqa: E402


def main():
    ap = argparse.ArgumentParser(
        description="递归解压目录下的压缩包到同名目录，成功才删压缩包")
    ap.add_argument("directory", nargs="?", default=".",
                    help="要处理的目录（默认当前目录，含递归）")
    ap.add_argument("--dry", action="store_true", help="只预览，不动文件")
    ap.add_argument("--timeout", type=int, default=3600,
                    help="单包解压硬顶秒数（默认 3600）")
    args = ap.parse_args()

    lines, stats = extract_directory(
        os.path.abspath(args.directory), dry=args.dry, timeout=args.timeout)
    for line in lines:
        print(line)
    if args.dry:
        print("（--dry 预览模式，未实际解压/删除）")


if __name__ == "__main__":
    main()
