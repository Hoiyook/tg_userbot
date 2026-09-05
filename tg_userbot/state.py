"""运行时全局状态。

单文件时代这些名字是模块级全局（client、各种锁、队列、白名单等），分散在
十几个子系统里被读写。拆包后统一收进本模块：**任何读写都走 state.X**（模块
对象属性访问），禁止 `from .state import X` —— main() 在事件循环里对 state.X
的赋值才能被所有调用方看到。

事件循环规则（Python 3.9，.venv 就是 3.9.6，实打实）：client 与 asyncio 原语
（Lock/Event/AdjustableSemaphore）只能在 app.main()（asyncio.run 内）构造，
此处一律 None/占位；Python ≤3.9 的原语在构造时绑定当时的默认事件循环，若在
模块导入期创建会绑定错循环——连接能建立但所有请求永久挂起、无任何报错。
Termux 的 Python 3.10+ 原语惰性绑定才不受影响。
"""
from . import config

# Telegram 客户端（main() 里 create_client() 后赋值）
client = None
bot_client = None
BOT_ID = None  # bot 账号的用户 id（bot 登录后填充，清理 bot 对话时用）

# 持久化下载队列的内存形态：{"tasks": [...], "retry": [...]}
# main() 启动时从 QUEUE_FILE 加载；QUEUE_LOCK 由其序列化并发修改
QUEUE = {"tasks": [], "retry": []}
QUEUE_LOCK = None  # asyncio.Lock，main() 里创建（事件循环规则）
EXECUTING = set()  # 正在执行的任务 id（防重复触发）

# 下载并发信号量（可动态调限）与当前并发数
DOWNLOAD_SEMAPHORE = None  # config.AdjustableSemaphore，main() 里创建
DOWNLOAD_CONCURRENCY = config.DOWNLOAD_CONCURRENCY  # 当前并发数（默认 3）

# 多 worker 下载池（见 workers.py）：DOWNLOAD_CONCURRENCY 条独立连接并发拉文件
DOWNLOAD_WORKERS = []       # 存活 worker 客户端列表（main() spawn_pool 后填充）
DOWNLOAD_WORKER_QUEUE = None  # asyncio.Queue 空闲 worker，main() 里创建；None=池禁用
DOWNLOAD_WORKER_TARGET = 0    # 目标存活 worker 数（/thread 与 spawn 时更新）

# 平台链接（抖音/Instagram）已投递给解析 bot 的消息 id（防重复触发）
PROCESSING_DOUYIN_IDS = set()

MY_ID = None  # 本人（owner）用户 id，登录后填充

# 进行中下载注册表（/progress 指令用）
ACTIVE_DOWNLOADS = {}
_download_seq = 0

# /setcleartime 唤醒清理循环的事件
CLEAR_INTERVAL_SECONDS = config.DEFAULT_CLEAR_INTERVAL_SECONDS
CLEAR_TIME_CHANGED = None  # asyncio.Event，main() 里创建（事件循环规则）

# 下载白名单 {chat_id(带符号): 标题}，main() 启动时 load_whitelist() 加载
WHITELIST_CHATS = {}

# CD2 进程句柄（仅防 GC 回收后台进程，无人读取）
_CD2_PROC = None
