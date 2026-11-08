#!/usr/bin/env python3
"""Download 标签归档小工具（Termux / Android 手机用）

浏览器等 App 只能下到 Download 根目录；本工具配合「先改名再触发」的手法归档：
把 Download 根目录下按「#作者#日期#原名.ext」格式改名过的文件，移动到
Download/画集Temp/<作者>/<日期>/ 下，并还原为原文件名。

  #仓鼠#2026-10-27#A-12 #hello .mp4 → 画集Temp/仓鼠/2026-10-27/A-12 #hello .mp4
  #仓鼠#A-12 #hello .mp4            → 画集Temp/仓鼠/_未注明日期/A-12 #hello .mp4

解析规则（宁可不動，不误伤）:
  - 原名里的 # 不影响解析：只取前两个标签，其余整体视为原名（split 限 3 刀）。
  - 两个标签时第二段必须是日期（2026-10-27 / 2026.10.7 / 2026_10_27 / 20261027
    均可，统一归一为 YYYY-MM-DD），否则视为标签不合规，跳过不动。
  - 不以 # 开头的文件完全忽略；下载未完成（.crdownload/.part 等后缀，或
    30 秒内仍在写）的文件跳过；目标重名自动加 (1)(2) 序号，绝不覆盖。

用法:
  python dl_sort.py [目录] [--dest 目标根目录] [--dry]
    目录    默认 /storage/emulated/0/Download
    --dest  默认 <目录>/画集Temp
    --dry   只预览要做什么，不实际移动

手机安装（Termux 需 F-Droid/GitHub 版）:
  pkg install python
  termux-setup-storage        # 弹窗授权，一次即可
  python ~/dl_sort.py --dry   # 先预览
  python ~/dl_sort.py         # 执行

桌面一键（装 Termux:Widget 后）:
  mkdir -p ~/.shortcuts
  printf '#!/data/data/com.termux/files/usr/bin/sh\npython ~/dl_sort.py\necho\necho 按回车关闭\nread x\n' > ~/.shortcuts/画集归类
  chmod +x ~/.shortcuts/画集归类
  桌面添加 Termux:Widget 小组件 → 点「画集归类」。
"""

import argparse
import os
import shutil
import sys
import time
from datetime import datetime

IN_PROGRESS_SUFFIXES = (".crdownload", ".download", ".part", ".tmp", ".opdownload")
DATE_FORMATS = ("%Y-%m-%d", "%Y.%m.%d", "%Y_%m_%d", "%Y%m%d")
NO_DATE_DIR = "_未注明日期"
DEST_NAME = "画集Temp"
STABLE_SECONDS = 30  # 出现不足 30 秒的文件视为仍在下载/写入


def parse_date(text):
    """常见日期写法归一为 YYYY-MM-DD；不是日期返回 None。"""
    text = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_tags(name):
    """#作者#日期#原名 或 #作者#原名 → (作者, 日期|None, 原名)；不合规返回 None。"""
    if not name.startswith("#"):
        return None
    parts = name.split("#", 3)  # 限 3 刀：原名里的 # 全部保留在最后一段
    if len(parts) == 4:
        author, date_text, original = parts[1].strip(), parts[2], parts[3]
        date = parse_date(date_text)
        if date is None:
            return None  # 第二段不是日期 → 标签格式存疑，不动
    elif len(parts) == 3:
        author, date, original = parts[1].strip(), None, parts[2]
    else:
        return None
    if not author or author in (".", "..") or not original.strip():
        return None
    return author, date, original


def unique_path(dest):
    """目标已存在时加 (1)(2) 序号，绝不覆盖。"""
    if not os.path.exists(dest):
        return dest
    root, ext = os.path.splitext(dest)
    i = 1
    while True:
        cand = "%s (%d)%s" % (root, i, ext)
        if not os.path.exists(cand):
            return cand
        i += 1


def main():
    ap = argparse.ArgumentParser(
        description="Download 标签归档：#作者#日期#原名 → 画集Temp/作者/日期/原名")
    ap.add_argument("dir", nargs="?", default="/storage/emulated/0/Download",
                    help="要整理的目录（默认 Android Download）")
    ap.add_argument("--dest", default=None,
                    help="目标根目录（默认 <目录>/%s）" % DEST_NAME)
    ap.add_argument("--dry", action="store_true", help="只预览不移动")
    args = ap.parse_args()

    src = os.path.abspath(args.dir)
    if not os.path.isdir(src):
        sys.exit("目录不存在: %s" % src)
    dest_root = os.path.abspath(args.dest) if args.dest else os.path.join(src, DEST_NAME)

    done = skipped = 0
    for name in sorted(os.listdir(src)):
        path = os.path.join(src, name)
        if not os.path.isfile(path):
            continue  # 子目录不碰（画集Temp 本身就在里面）
        parsed = parse_tags(name)
        if parsed is None:
            if name.startswith("#"):
                print("· 跳过（标签不合规，需 #作者#日期#原名 或 #作者#原名，日期段须为日期）: %s" % name)
                skipped += 1
            continue  # 不以 # 开头的文件：静默忽略
        if name.lower().endswith(IN_PROGRESS_SUFFIXES):
            print("· 跳过（下载中后缀）: %s" % name)
            skipped += 1
            continue
        if time.time() - os.path.getmtime(path) < STABLE_SECONDS:
            print("· 跳过（30 秒内仍在写）: %s" % name)
            skipped += 1
            continue

        author, date, original = parsed
        dest_dir = os.path.join(dest_root, author, date or NO_DATE_DIR)
        dest = unique_path(os.path.join(dest_dir, original))
        rel = os.path.relpath(dest, dest_root)
        if args.dry:
            print("[预览] %s  →  %s" % (name, rel))
            done += 1
            continue
        os.makedirs(dest_dir, exist_ok=True)
        shutil.move(path, dest)  # 同盘即 rename，秒完成
        print("✓ %s  →  %s" % (name, rel))
        done += 1

    print("\n%s：移动 %d 个，跳过 %d 个。" % ("预览" if args.dry else "完成", done, skipped))
    if args.dry:
        print("（--dry 预览模式，未实际移动）")


if __name__ == "__main__":
    main()
