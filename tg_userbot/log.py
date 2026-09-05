"""共享日志模块：console + 每日轮转文件双输出 + 全链路 [T=] 追踪标记。

全包共用同一个 `tg_userbot` logger。configure() 在 config.py 末尾（import 时）
调用一次；先清空旧 handler 再挂新，保证重复 import / 测试进程内多次加载不会重复
叠加 handler，幂等。

- 文件输出用 TimedRotatingFileHandler：每天零点把当天内容滚成
  download.log.<日期>（同一 runtime 目录），只保留最近 LOG_RETENTION_DAYS 天。
- 追踪：每个队列任务在 execute_queued_task 入口用 set_trace() 把
  <队列记录 id 前 8 位> 写进 contextvar，随协程传导到 download_file/通知等全部
  下游日志；_TraceFormatter 在有 trace 时把它以 [T=xxxx] 插到正文之前，同一任务
  的多条日志据此串起来排查（配合 download_history 的时间戳即全链路）。
"""
import contextvars
import logging
from logging.handlers import TimedRotatingFileHandler

logger = logging.getLogger("tg_userbot")

# 全链路 trace id（contextvar）：任务级生效，随 await 自动传导，不污染全局。
# 默认空串 = 无 trace，普通消息不出现 [T=]。
_trace_var = contextvars.ContextVar("trace_id", default="")

_active_log_file = None  # 当前写入的日志文件路径（rotate 后仍指向 base 文件）


def set_trace(trace_id: str) -> None:
    """为当前协程（及它 await 出来的下游）设置追踪标记，空串清除。"""
    _trace_var.set(trace_id or "")


def clear_trace() -> None:
    _trace_var.set("")


def current_log_path():
    """当前日志文件绝对路径（/logpath 等展示用）。configure 前为 None。"""
    return _active_log_file


class _TraceFormatter(logging.Formatter):
    """在「时间 | 级别 | 」之后、正文之前插入 [T=xxxx]（有 trace 才插）。

    每次 format 重新读 contextvar，因此同一 record 被 console/file 两个 handler
    各格式化一次也不会重复叠加；多 handler 共用同一实例是安全的。异常文本仍整体
    附在正文之后。
    """

    def format(self, record):
        message = record.getMessage()
        if self.usesTime():
            record.asctime = self.formatTime(record, self.datefmt)
        trace = _trace_var.get()
        body = f"[T={trace}] {message}" if trace else message
        line = self._fmt % {**record.__dict__, "message": body}
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            line = line.rstrip() + "\n" + record.exc_text
        return line


def _make_formatter():
    return _TraceFormatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _make_file_handler(log_file: str, retention_days: int = 7):
    """构建每日轮转的文件 handler（默认只保留最近 7 天）。"""
    handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        backupCount=retention_days,
        encoding="utf-8",
    )
    handler.setFormatter(_make_formatter())
    return handler


def configure(log_file: str, retention_days: int = 7) -> None:
    """配置 logger：级别 INFO，清空旧 handler 后挂「每日轮转文件 + console」。

    log_file 为当天活动的 base 路径（runtime/download.log）；零点后旧内容滚成
    download.log.<日期>，本路径继续指当天的活动文件，故 current_log_path() 稳定。
    retention_days 由 config 传入（LOG_RETENTION_DAYS，默认 7），保持 log 为叶子。
    """
    global _active_log_file
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    file_handler = _make_file_handler(log_file, retention_days)
    logger.addHandler(file_handler)
    _active_log_file = log_file

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(_make_formatter())
    logger.addHandler(console_handler)
