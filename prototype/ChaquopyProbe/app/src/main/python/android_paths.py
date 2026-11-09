# -*- coding: utf-8 -*-
"""Android 端媒体目录选择。

Kotlin 在用户授予「所有文件访问」后写 `files/public_download_dir.txt`；
Python 侧优先用它（/storage/emulated/0/Download/Nagram），否则回落到
App 私有 `files/downloads`。
"""
import os


def download_dir(data_dir):
    flag = os.path.join(data_dir, "public_download_dir.txt")
    try:
        if os.path.isfile(flag):
            with open(flag, encoding="utf-8") as f:
                path = f.read().strip()
            if path:
                return path
    except Exception:
        pass
    return os.path.join(data_dir, "downloads")
