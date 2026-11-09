# -*- coding: utf-8 -*-
"""Android 核心启动入口（ChaquopyProbe）。

核心 = Telethon 用户客户端 + Saved Messages 处理器 + 下载队列 + worker 池
+ 白名单/标签监听的 Scanner & Worker（解析 bot 回复的转发/下载靠它）。

与 app.main() 的差异（刻意）：
- 不启动 Chrome Agent / CD2 / Pawchive / 种子 / B站 / X / Pixiv / 115 相关；
- 不启动 bot 账号菜单（后续再加）；
- 不启动 reporter（看板/API 自身承担状态展示）。
"""
import asyncio
import json
import os
import threading
import time
import traceback

import android_paths

_STOP = threading.Event()
_THREAD = None
_STATUS = {
    "state": "idle",
    "user": "",
    "error": "",
    "queue": 0,
    "retry": 0,
    "api": "off",
    "started_at": 0,
}


def _set_env(data_dir):
    os.environ["HOME"] = data_dir
    os.environ["TG_SECRETS_FILE"] = os.path.join(data_dir, "tg_secrets.json")
    os.environ["TG_DATA_ROOT"] = data_dir
    dl = android_paths.download_dir(data_dir)
    os.environ["TG_DOWNLOAD_DIR"] = dl
    os.environ["TG_RUNTIME_DIR"] = os.path.join(data_dir, "runtime")
    os.environ["TG_RUNTIME_DB"] = os.path.join(
        data_dir, "runtime", "tg_userbot.db")
    os.environ["TG_RESOLVER"] = "off"
    os.environ["TG_ANDROID"] = "1"
    env_path = os.path.join(data_dir, "android_env.json")
    if os.path.exists(env_path):
        try:
            with open(env_path, encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    os.environ.setdefault(str(k), str(v))
        except Exception:
            pass
    os.makedirs(dl, exist_ok=True)
    os.makedirs(os.environ["TG_RUNTIME_DIR"], exist_ok=True)


def start(data_dir):
    """Kotlin 前台服务调用：启动核心（幂等）。"""
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return "already running (%s)" % _STATUS.get("state")
    _set_env(data_dir)
    _STOP.clear()
    _STATUS.update({"state": "starting", "error": "", "user": "",
                    "queue": 0, "retry": 0, "api": "off",
                    "started_at": time.time()})
    _THREAD = threading.Thread(target=_thread_main, daemon=True,
                               name="tg-core")
    _THREAD.start()
    return "core starting"


def _thread_main():
    try:
        asyncio.run(_run())
    except Exception:
        _STATUS.update({"state": "error", "error": traceback.format_exc()})


async def _run():
    from telethon import events
    from tg_userbot import app as app_mod
    from tg_userbot import (api_server, caption_filter, cleanup, dedup,
                            listener, listener_worker, queue, runtime_db,
                            state, thread, whitelist, workers)
    from tg_userbot.config import AdjustableSemaphore
    from tg_userbot.log import logger

    client = None
    cleanup_task = sweeper_task = api_task = None
    listener_task = wl_scan_task = listener_worker_task = None
    history_recheck_task = None
    try:
        # 与 app.main() 对齐的最小初始化顺序
        thread.load_thread_config()
        dedup.load_dedup_config()
        caption_filter.load_caption_filter_config()
        listener.load_listen_config()
        # init_db 只建连接、返回是否可用；RUNTIME_DB_READY 是队列/去重/
        # 监听选择 DB 模式的开关，必须在这里显式落位（v0.16 前漏了，
        # 导致全程回落 JSON/文件模式）。
        state.RUNTIME_DB_READY = bool(runtime_db.init_db())
        if not state.RUNTIME_DB_READY:
            logger.error("🗄 Runtime DB 不可用：队列/去重回落文件，监听不启动")
        else:
            try:
                listener.migrate_legacy_state()
            except Exception as e:
                logger.warning(f"📡 旧监听状态迁移失败（忽略）：{e}")
        dedup.load_index()
        state.WHITELIST_CHATS = whitelist.load_whitelist()
        state.DOWNLOAD_SEMAPHORE = AdjustableSemaphore(
            state.DOWNLOAD_CONCURRENCY)
        state.QUEUE_LOCK = asyncio.Lock()
        state.QUEUE = queue.load_queue_any()
        state.CLEAR_TIME_CHANGED = asyncio.Event()
        state.STOP_EVENT = asyncio.Event()

        _STATUS["state"] = "connecting"
        client = app_mod.create_client()
        await app_mod.start_with_retry(client)
        state.client = client
        me = await client.get_me()
        state.MY_ID = me.id
        client.add_event_handler(app_mod.new_message_handler,
                                 events.NewMessage())
        await workers.spawn_pool(state.DOWNLOAD_CONCURRENCY)
        cleanup.load_clear_interval()
        cleanup_task = asyncio.create_task(cleanup.cleanup_loop())
        sweeper_task = asyncio.create_task(app_mod._retry_sweeper())

        # 白名单/监听：没有 Worker 时，解析 bot 回复的视频只会停在
        # listener_tasks 表里，永远不会转发进收藏夹/下载。
        if state.RUNTIME_DB_READY:
            try:
                recovered = listener_worker.recover_expired()
                if recovered:
                    logger.warning(
                        f"🗄 启动恢复：{recovered} 条租约过期的监听任务已回到待执行")
            except Exception as e:
                logger.warning(f"🗄 监听任务租约恢复失败（忽略）：{e}")
            listener_worker_task = listener_worker.start_worker()
            try:
                listener.resume_interrupted_history_scans()
            except Exception as e:
                logger.warning(f"📡 历史回扫续扫失败（忽略）：{e}")
            wl_scan_task = asyncio.create_task(
                app_mod._whitelist_scan_loop())
            listener_task = asyncio.create_task(app_mod._listener_loop())
            history_recheck_task = asyncio.create_task(
                listener.history_recheck_loop())

        # 探针页让位：停探针 HTTP，把 8765 交给真正的看板 API。
        # 强制绑 127.0.0.1（不沿用 Mac 的 0.0.0.0），token 仍来自 secrets。
        try:
            import probe_main
            probe_main.stop()
        except Exception:
            pass
        await asyncio.sleep(1.0)
        api_task = asyncio.create_task(
            api_server.start_api(bind="127.0.0.1"))
        _STATUS["api"] = "starting"
        await asyncio.sleep(1.5)
        _STATUS["api"] = "failed" if api_task.done() else "running"

        user = getattr(me, "username", None) or str(me.id)
        _STATUS.update({"state": "connected", "user": user, "error": ""})
        while not _STOP.is_set():
            try:
                q = state.QUEUE or {}
                _STATUS["queue"] = len(q.get("tasks") or [])
                _STATUS["retry"] = len(q.get("retry") or [])
            except Exception:
                pass
            await asyncio.sleep(1)
    except Exception:
        _STATUS.update({"state": "error", "error": traceback.format_exc()})
    finally:
        for t in (cleanup_task, sweeper_task, api_task,
                  listener_task, wl_scan_task, listener_worker_task,
                  history_recheck_task):
            if t is not None:
                t.cancel()
        try:
            await workers.shutdown()
        except Exception:
            pass
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        if _STATUS.get("state") != "error":
            _STATUS["state"] = "stopped"


def dashboard_url():
    """给 Kotlin WebView 的看板地址（带 token；token 不打印到界面）。"""
    from urllib.parse import quote
    from tg_userbot import config
    token = getattr(config, "API_TOKEN", "") or ""
    port = getattr(config, "API_PORT", 8765) or 8765
    url = f"http://127.0.0.1:{port}/"
    if token:
        url += "?token=" + quote(token)
    return url


def status():
    return dict(_STATUS)


def stop():
    _STOP.set()
    return "stopping"
