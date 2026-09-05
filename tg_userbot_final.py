#!/usr/bin/env python3
"""TG Userbot v2.9 —— 运行薄壳。

代码已重构进 tg_userbot/ 扁平包（config/log/state + 各子系统模块，见
CLAUDE.md「模块索引」）。本文件只负责运行入口：起事件循环、调用
app.main()、处理退出信号。运行命令、`.claude` pkill 管理规则等都
key 在这个文件名上，保持不变（同目录有单文件时代备份 tg_userbot_final.py.bak_*）。
"""
import sys
import asyncio

from tg_userbot import app
from tg_userbot.log import logger

if __name__ == "__main__":
    try:
        asyncio.run(app.main())
    except KeyboardInterrupt:
        logger.info("🛑 TG Userbot 已停止")
    except Exception as e:
        logger.exception(f"❌ 程序退出：{e}")
