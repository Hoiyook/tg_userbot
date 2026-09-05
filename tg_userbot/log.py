"""共享日志模块。

全包共用同一个 `tg_userbot` logger：console + 文件（download.log）双输出。
configure() 在 config.py 末尾（import 时）调用一次；先清空旧 handler 再挂新，
保证重复 import / 测试进程内多次加载不会重复叠加 handler，幂等。
"""
import logging

logger = logging.getLogger("tg_userbot")


def configure(log_file: str) -> None:
    """配置 logger：级别 INFO，清空已有 handler 后挂 File + Stream 双 handler。"""
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
