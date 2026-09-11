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

# bot 菜单「🍪 抖音Cookie」的等待输入标记：monotonic 时间戳，超过即窗口关闭
COOKIE_INPUT_UNTIL = 0.0
FIND_INPUT_UNTIL = 0.0
# bot 菜单「🧹 Caption 清洗」的等待输入标记 + 这次输入当什么用：
# "add" = 下一条文本是一条规则，"del" = 规则编号，"test" = 要试清洗的原文。
# 三个输入窗口（cookie / 查询 / Caption 清洗）互斥，由 bot.open_input_window
# 统一开关，避免先开的窗口把本该给后开窗口的文本吃掉。
CAPTION_INPUT_UNTIL = 0.0
CAPTION_INPUT_MODE = ""

MY_ID = None  # 本人（owner）用户 id，登录后填充

# 进行中下载注册表（/progress 指令用）
ACTIVE_DOWNLOADS = {}
_download_seq = 0

# /setcleartime 唤醒清理循环的事件
CLEAR_INTERVAL_SECONDS = config.DEFAULT_CLEAR_INTERVAL_SECONDS
CLEAR_TIME_CHANGED = None  # asyncio.Event，main() 里创建（事件循环规则）

# 稳态停止事件：SIGINT/SIGTERM 信号处理器 set()，main() 挂起等它再收尾退出
STOP_EVENT = None  # asyncio.Event，main() 里创建（事件循环规则）

# 下载白名单 {chat_id(带符号): 标题}，main() 启动时 load_whitelist() 加载
WHITELIST_CHATS = {}

# 重复媒体去重：{判重键: {"date":…, "filename":…}}，main() 启动时
# dedup.load_index() 载入（尾部 ≤ DEDUP_MAX_ENTRIES 条）；DEDUP_ENABLED 由
# dedup.load_dedup_config() 从 dedup_config.json 还原（/dedup off|on 切换）
DEDUP_INDEX = {}
DEDUP_ENABLED = True

# Caption 清洗规则当前值（列表，顺序=用户看到的顺序）。初值是 config 里的
# 默认规则；启动时 caption_filter.load_caption_filter_config() 从
# caption_filter.json 还原（/caption_filter 增删改实时生效并持久化）。
CAPTION_FILTER_RULES = list(config.DEFAULT_CAPTION_FILTER_RULES)

# ============================================================
# 标签监听（listener.py，与下载白名单完全独立的一套配置）
# ============================================================
# 运行态全部在这里，逻辑在 listener.py —— 与 WHITELIST_CHATS / DEDUP_ENABLED /
# CAPTION_FILTER_RULES 同款模式：reporter（只读观察者）与 bot 菜单都只读
# state.*，不碰文件、不做网络请求。main() 启动时 load_listen_config()/
# load_listen_state() 从 runtime/listen.json 与 runtime/listen_state.json 还原
# （/listen 命令与菜单增删改实时生效并持久化）。
LISTEN_ENABLED = True          # 总开关（listen.json 的 enabled）
LISTEN_INTERVAL_MINUTES = config.LISTEN_DEFAULT_INTERVAL_MINUTES
LISTEN_RULES = []              # 规则列表（每项含 source_chat_id/tag/targets/…）
# 扫描游标：{source_chat_id 字符串: {"last_message_id": int,
#   "pending": {消息单元键: {"ids": [...], "work": [未完成工作项]}}}}
# 按 chat_id 存（username/名称都会变，chat_id 不会）。
LISTEN_STATE = {}
# 上轮扫描快照（menu / reporter 只读展示）：{"ts","scanned","matched",
# "forwarded","failed","chats","failed_chats"}；未扫过为 None。
LISTEN_LAST_SCAN = None

# bot 菜单「添加/修改监听」的多步向导：等待下一条文本的窗口 + 这一步在等什么
# （"chat" 来源聊天 / "tag" 标签 / "target" 目标聊天）。四个输入窗口
# （cookie / 查询 / Caption 清洗 / 标签监听）互斥，由 bot.open_input_window
# 统一开关——否则先开的 cookie 窗口会把一段标签当 cookie 存进 tg_secrets.json。
LISTEN_INPUT_UNTIL = 0.0
LISTEN_INPUT_STEP = ""         # chat | tag | target

# CD2 进程句柄（仅防 GC 回收后台进程，无人读取）
_CD2_PROC = None
