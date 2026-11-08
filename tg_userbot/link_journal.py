# -*- coding: utf-8 -*-
"""链接提交日志：所有入口写同一份，三个视图读同一份（2026-11-08）。

此前 ``paste_log.jsonl`` 只有看板 ``dispatch_link_text`` 写入，bot 对话 /
收藏夹发的链接根本不在这里；台账页也没有链接提交记录。现在：

- **唯一写点** = ``routing.execute``（收藏夹 / bot / 看板 / PWA / App
  分享都经过它），记录 ``surface``（me/bot/api/app）与关联 ``ref``；
- **唯一读点** = 本模块；发现页「链接记录」、台账页、队列页都读它。

文件沿用 ``RUNTIME_DIR/paste_log.jsonl``（老记录没有 ``surface``，读取
原样保留；键名 ``routed`` 也保持兼容）。写入是单行 append（事件循环内
原子，不需要锁）；读取时回看最近 ``MAX_ENTRIES`` 条，按新→旧返回。
"""
import json
import os
import time

from . import config
from .log import logger

PATH = os.path.join(config.RUNTIME_DIR, "paste_log.jsonl")
MAX_ENTRIES = 500


def record(text, url, routed, *, ref=None, surface="unknown",
           chat_id=None, message_id=None, message=None, path=None):
    """追加一条链接提交记录；失败仅告警，绝不影响主链路。"""
    path = path or PATH
    entry = {
        "ts": time.time(),
        "text": str(text or "")[:200],
        "url": url,
        "routed": routed,
        "ref": ref,
        "surface": surface,
    }
    if chat_id is not None:
        entry["chat_id"] = chat_id
    if message_id is not None:
        entry["message_id"] = message_id
    if isinstance(message, str) and message:
        entry["message"] = message[:200]
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"链接日志落盘失败：{e}")


def read(n=50, path=None):
    """读最近 n 条（新→旧），最多回看 MAX_ENTRIES 条。"""
    path = path or PATH
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning(f"链接日志读取失败：{e}")
        return []
    try:
        n = max(1, int(n))
    except (TypeError, ValueError):
        n = 50
    items = []
    for line in lines[-MAX_ENTRIES:][-n:]:
        try:
            items.append(json.loads(line))
        except ValueError:
            continue
    return list(reversed(items))    # 新→旧
