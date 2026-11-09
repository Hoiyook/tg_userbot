# -*- coding: utf-8 -*-
"""把本地文件上传到 115 网盘的 temp 目录（经 CD2 挂载 + gRPC 对账）。

用法（仓库根目录）：
    .venv/bin/python scripts/upload_to_115_temp.py <本地文件> [远端文件名]

默认目标：/115open/temp（CD2 挂载 = /Volumes/CloudDrive/temp）。
远端文件名默认取本地 basename；重名会覆盖，想保留旧版请显式传新名字。
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tg_userbot import cd2_api  # noqa: E402
from tg_userbot.log import logger  # noqa: E402

TARGET = "/115open/temp"


def main():
    if len(sys.argv) < 2:
        print("用法：upload_to_115_temp.py <本地文件> [远端文件名]")
        return 2
    src = os.path.abspath(sys.argv[1])
    if not os.path.isfile(src):
        print(f"❌ 本地文件不存在：{src}")
        return 2
    remote_name = (sys.argv[2] if len(sys.argv) > 2
                   else os.path.basename(src)).strip().replace("/", "_")
    if not remote_name:
        print("❌ 远端文件名为空")
        return 2

    tmpdir = None
    upload_path = src
    if os.path.basename(src) != remote_name:
        tmpdir = tempfile.mkdtemp(prefix="upload115_")
        upload_path = os.path.join(tmpdir, remote_name)
        shutil.copy2(src, upload_path)
        logger.info(f"📤 上传副本：{upload_path}（{os.path.getsize(upload_path)} 字节）")

    try:
        # 大文件（APK 几十 MB）给足 CD2 后台上传与对账时间
        ok, note = cd2_api.upload_to_115(
            [upload_path], TARGET,
            verify_tries=60, verify_gap_s=3.0,
            upload_wait_tries=10, upload_wait_gap_s=3.0,
            name_prefix=remote_name)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

    if ok:
        print(f"✅ 已上传：{TARGET}/{remote_name}")
        print(f"   本地：{src}")
        return 0
    print(f"❌ 上传未确认：{note}")
    print(f"   目标：{TARGET}/{remote_name}")
    print("   如果是“已写挂载但 gRPC 对账未见”，CD2 可能仍在后台上传，"
          "稍后在 115 里刷新查看。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
