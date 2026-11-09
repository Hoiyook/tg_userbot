# -*- coding: utf-8 -*-
"""手机号 / 验证码 / 2FA 登录（ChaquopyProbe onboarding）。

这里是**新建独立 session**（不是复制 Mac 的 auth key），可与 Mac bot
同时在线。只依赖 Telethon；所有调用通过一个常驻 asyncio 循环串行执行，
避免 Telethon client 跨事件循环。
"""
import asyncio
import json
import os
import threading
import traceback

import android_paths

_LOOP = None
_CLIENT = None
_PHONE = ""


def _cfg(data_dir):
    """设置 Android 路径/代理环境后 import tg_userbot.config（一次）。"""
    os.environ["HOME"] = data_dir
    os.environ["TG_DATA_ROOT"] = data_dir
    dl = android_paths.download_dir(data_dir)
    os.environ["TG_DOWNLOAD_DIR"] = dl
    os.environ["TG_RUNTIME_DIR"] = os.path.join(data_dir, "runtime")
    os.environ["TG_RUNTIME_DB"] = os.path.join(
        data_dir, "runtime", "tg_userbot.db")
    os.environ["TG_SECRETS_FILE"] = os.path.join(
        data_dir, "tg_secrets.json")
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
    from tg_userbot import config
    return config


def _ensure_loop():
    global _LOOP
    if _LOOP is not None:
        return
    ready = threading.Event()

    def run():
        global _LOOP
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
        ready.set()
        _LOOP.run_forever()

    threading.Thread(target=run, daemon=True, name="login-loop").start()
    ready.wait(5)


def _run(coro, timeout=150):
    _ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, _LOOP)
    try:
        return fut.result(timeout)
    except Exception:
        return "error:" + traceback.format_exc()


async def _send_code(data_dir, phone):
    global _CLIENT, _PHONE
    from telethon import TelegramClient
    config = _cfg(data_dir)
    if _CLIENT is not None:
        try:
            await _CLIENT.disconnect()
        except Exception:
            pass
    _CLIENT = TelegramClient(
        config.SESSION_NAME, config.API_ID, config.API_HASH,
        proxy=config.PROXY)
    await _CLIENT.connect()
    if await _CLIENT.is_user_authorized():
        await _CLIENT.disconnect()
        _CLIENT = None
        return "already"
    phone = str(phone or "").strip()
    if not phone:
        return "error:手机号为空"
    await _CLIENT.send_code_request(phone)
    _PHONE = phone
    return "code_sent"


async def _sign_in(data_dir, code):
    global _CLIENT
    from telethon.errors import (FloodWaitError, PhoneCodeExpiredError,
                                 PhoneCodeInvalidError,
                                 SessionPasswordNeededError)
    if _CLIENT is None:
        return "error:请先发送验证码"
    try:
        await _CLIENT.sign_in(_PHONE, str(code or "").strip())
    except SessionPasswordNeededError:
        return "password_needed"
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
        return "code_invalid:" + e.__class__.__name__
    except FloodWaitError as e:
        return f"flood:{e.seconds}"
    await _CLIENT.disconnect()
    _CLIENT = None
    return "ok"


async def _sign_in_password(data_dir, password):
    global _CLIENT
    from telethon.errors import FloodWaitError, PasswordHashInvalidError
    if _CLIENT is None:
        return "error:请先发送验证码"
    try:
        await _CLIENT.sign_in(password=str(password or ""))
    except PasswordHashInvalidError:
        return "password_invalid"
    except FloodWaitError as e:
        return f"flood:{e.seconds}"
    await _CLIENT.disconnect()
    _CLIENT = None
    return "ok"


def send_code(data_dir, phone):
    return _run(_send_code(data_dir, phone), 150)


def sign_in(data_dir, code):
    return _run(_sign_in(data_dir, code), 150)


def sign_in_password(data_dir, password):
    return _run(_sign_in_password(data_dir, password), 150)


def reset():
    """放弃未完成的登录（不删除已成功的 session）。"""
    global _CLIENT

    async def _reset():
        global _CLIENT
        if _CLIENT is not None:
            try:
                await _CLIENT.disconnect()
            except Exception:
                pass
            _CLIENT = None

    _run(_reset(), 30)
    return "reset"
