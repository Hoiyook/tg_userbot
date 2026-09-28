#!/usr/bin/env python3
"""Pawchive 历史死链缩略图追回（独立进程，避开 bot 的在途帖）。

原理：死链文件（原图 404）的缩略图仍挂在 img.pawchive.pw（实测 ~100% 残留）。
按 id 游标分批抓回落盘为 <名>.thumb.jpg，标 DONE+备注，帖内全补齐翻 COMPLETED。

与运行中的 bot 共存：
  - 读：独立 sqlite3 只读连接（自己分批）
  - 写：runtime_db 公共助手（跨进程 WAL + busy_timeout）
  - 只处理非 PENDING/PROCESSING 帖的文件——绝不与 bot worker 抢在途帖
  - GET 抓取（DDoS-Guard 拦 HEAD）；直连不走代理
  - 断点续跑：游标存 state 文件，重启自动续

用法（后台）：
    TG_DATA_ROOT=/Volumes/V1 nohup .venv/bin/python \\
        scripts/recover_thumbs_backlog.py > /Volumes/V1/runtime/thumb_recovery.log 2>&1 &
"""
import concurrent.futures
import json
import os
import sqlite3
import sys
import time
import urllib.request

os.environ.setdefault("TG_DATA_ROOT", "/Volumes/V1")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tg_userbot import runtime_db, config  # noqa: E402
from tg_userbot.log import logger  # noqa: E402

assert runtime_db.init_db(), "runtime_db 初始化失败"

THUMB_BASE = "https://img.pawchive.pw/thumbnail/data"
STATE_FILE = os.path.join(config.RUNTIME_DIR, "thumb_backlog_state.json")
WORKERS = 8
BATCH = 500
SKIP_POST_STATUS = ("PENDING", "PROCESSING")
DEAD_MARK = "站点缺文件"


def load_cursor():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f).get("last_id", 0)
    except Exception:
        return 0


def save_cursor(last_id):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"last_id": last_id}, f)
    os.replace(tmp, STATE_FILE)


def fetch_batch(last_id, size=BATCH):
    """下一批死链文件（跳过 bot 在途帖）。返回 (rows, max_id)。"""
    conn = sqlite3.connect(f"file:{config.RUNTIME_DB_FILE}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT f.id, f.url, f.filename, f.post_row, p.subdir "
            "FROM pawchive_files f JOIN pawchive_posts p ON p.id=f.post_row "
            "WHERE f.id > ? AND f.status='FAILED' "
            "AND f.error LIKE ? AND p.status NOT IN (?,?) "
            "ORDER BY f.id LIMIT ?",
            (last_id, DEAD_MARK + "%", *SKIP_POST_STATUS, size),
        ).fetchall()
        return [dict(r) for r in rows], (rows[-1]["id"] if rows else last_id)
    finally:
        conn.close()


def fetch_thumb_bytes(url):
    i = url.find("/data")
    if i < 0:
        return None
    thumb = "https://img.pawchive.pw/thumbnail/data" + url[i + 5:].split("?")[0]
    try:
        req = urllib.request.Request(thumb)
        req.add_header("User-Agent", "Mozilla/5.0 tg-userbot-pawchive")
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status == 200:
                return r.read()
    except Exception:
        return None
    return None


def main():
    from tg_userbot import notify  # 延迟导入：避免 import 期碰 telegram
    cursor = load_cursor()
    logger.info(f"🖼 追回启动：游标 {cursor}，每批 {BATCH}，并发 {WORKERS}")
    total_done = total_miss = 0
    try:
        while True:
            rows, max_id = fetch_batch(cursor)
            if not rows:
                logger.info("🖼 追回完成：没有更多死链文件")
                break
            with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
                payload = list(ex.map(
                    lambda r: (r, fetch_thumb_bytes(r["url"])), rows))
            done = miss = 0
            touched_posts = set()
            for row, data in payload:
                cursor = max(cursor, row["id"])
                if not data:
                    miss += 1
                    continue
                target = os.path.join(config.DOWNLOAD_DIR, row["subdir"],
                                      os.path.splitext(row["filename"])[0]
                                      + ".thumb.jpg")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                if not os.path.isfile(target):
                    with open(target, "wb") as fh:
                        fh.write(data)
                try:
                    runtime_db.mark_pawchive_file_done(
                        row["id"], size_bytes=len(data),
                        note="缩略图追回(原图404)")
                    done += 1
                    touched_posts.add(row["post_row"])
                except Exception as e:
                    logger.warning(f"🖼 记账失败（忽略）：{e}")
            total_done += done
            total_miss += miss
            logger.info(
                f"🖼 批次完成：+{done} 张缩略图（无缩略图 {miss}）｜"
                f"累计 {total_done}")
            save_cursor(cursor)
            # 帖内全补齐 → 翻 COMPLETED
            for pr in touched_posts:
                try:
                    if runtime_db.complete_pawchive_post_if_all_done(pr):
                        logger.info(f"🖼 死链帖 #{pr} 缩略图追回补齐 → COMPLETED")
                except Exception as e:
                    logger.warning(f"🖼 帖子翻转失败（忽略）：{e}")
            if total_done and total_done % 2000 == 0:
                try:
                    asyncio_run_notify(
                        f"🖼 缩略图追回进度：已追回 {total_done} 张"
                        f"（无缩略图 {total_miss}）")
                except Exception:
                    pass
    except KeyboardInterrupt:
        logger.info("🖼 追回被手动中断")
    finally:
        logger.info(
            f"🖼 追回结束：追回 {total_done}，无缩略图 {total_miss}，"
            f"游标 {cursor}")
        save_cursor(cursor)


def asyncio_run_notify(text):
    import asyncio
    from tg_userbot import notify

    async def _go():
        try:
            await notify.notify_user(text)
        except Exception as e:
            logger.warning(f"通知失败（忽略）：{e}")

    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.run_until_complete(_go())


if __name__ == "__main__":
    main()
