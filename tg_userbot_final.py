import os
import json
import re
import uuid
import asyncio
import logging
import mimetypes
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime
from collections import deque

from telethon import TelegramClient, events, Button
from telethon.errors import RPCError
from telethon.network import ConnectionTcpFull, ConnectionTcpObfuscated
from telethon.utils import get_peer_id

# ============================================================
# 配置区：只需要修改这里
# ============================================================

# ------------------------------------------------------------
# 敏感配置：api_id / api_hash / bot_token 等真实密钥不在代码里硬编码，
# 而是从本地配置文件 tg_secrets.json 读取（与脚本同目录，可用环境变量
# TG_SECRETS_FILE 覆盖路径）。该文件含真实密钥，已被 .gitignore 排除，
# 不会提交到仓库；缺失时按“未配置”处理，启动阶段会给出中文提示。
# 字段格式见仓库内模板 tg_secrets.example.json。
# ------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS_FILE = os.environ.get(
    "TG_SECRETS_FILE", os.path.join(SCRIPT_DIR, "tg_secrets.json")
)


def load_secret_config() -> dict:
    """读取敏感配置。文件缺失 / 损坏时返回空 dict（不抛异常，启动时再提示）。"""
    try:
        with open(SECRETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


_SECRET_CONFIG = load_secret_config()

# Telegram API 凭据（在 my.telegram.org 创建应用获取）
API_ID = _SECRET_CONFIG.get("api_id")
API_HASH = _SECRET_CONFIG.get("api_hash", "")


# ------------------------------------------------------------
# 跨平台支持：自动区分 Termux（Android）与 macOS/桌面
# Termux 分支保持原样，不影响安卓运行。
# ------------------------------------------------------------
def is_termux() -> bool:
    """检测是否运行在 Termux（Android）环境。"""
    return bool(os.environ.get("TERMUX_VERSION")) or os.path.isdir(
        "/data/data/com.termux"
    )


IS_TERMUX = is_termux()

# 会话文件：两个平台都用 ~/tg_downloader。
# 每台设备首次运行各自独立登录，互不影响。
# 注意：不要把同一台设备的 .session 文件复制到另一台同时运行。
SESSION_NAME = os.path.expanduser("~/tg_downloader")

# 保存目录：
#   Termux → /storage/emulated/0/Download/Nagram（手机存储）
#   macOS  → ~/Downloads/Nagram
# 也可以用环境变量 TG_SAVE_FOLDER 覆盖。
if IS_TERMUX:
    DEFAULT_SAVE_FOLDER = "/storage/emulated/0/Download/Nagram"
else:
    DEFAULT_SAVE_FOLDER = str(Path.home() / "Downloads" / "Nagram")

SAVE_FOLDER = os.environ.get("TG_SAVE_FOLDER", DEFAULT_SAVE_FOLDER)
LOG_FILE = os.path.join(SAVE_FOLDER, "download.log")


# ------------------------------------------------------------
# 代理支持（可选，默认直连）
# ------------------------------------------------------------
# 国内网络访问 Telegram 需要代理时，用环境变量 TG_PROXY 指定：
#   TG_PROXY=socks5://127.0.0.1:7890           （Clash 等常用）
#   TG_PROXY=socks5://user:pass@127.0.0.1:7890
#   TG_PROXY=http://127.0.0.1:7890
# 不设置 TG_PROXY 则直连；Termux/安卓上不设置即保持原行为不变。
def parse_proxy(value):
    """把 TG_PROXY 解析成 Telethon 的 proxy 参数。

    支持 socks5 / socks4 / http 协议，可带用户名密码。
    解析失败返回 None（静默，启动横幅会显示代理状态）。
    """
    if not value:
        return None

    value = value.strip()
    if not value:
        return None

    try:
        parsed = urlparse(value)
    except ValueError:
        return None

    scheme = (parsed.scheme or "").lower()

    # Telethon/python_socks 的代理协议名：socks5 / socks4 / http
    if scheme in ("socks5", "socks5h"):
        proxy_type = "socks5"
    elif scheme == "socks4":
        proxy_type = "socks4"
    elif scheme in ("http", "https"):
        proxy_type = "http"
    else:
        return None

    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        return None

    # Telethon 代理参数格式：(type, host, port, rdns, username, password)
    if parsed.username:
        return (proxy_type, host, port, True, parsed.username, parsed.password or "")
    return (proxy_type, host, port, True)


PROXY = parse_proxy(os.environ.get("TG_PROXY"))


# ------------------------------------------------------------
# 连接传输方式（可选）
# ------------------------------------------------------------
# TG_CONNECTION=full       → ConnectionTcpFull（TLS 传输，Telethon 默认）
# TG_CONNECTION=obfuscated → ConnectionTcpObfuscated（MTProto 混淆传输，无 TLS）
# 默认规则：设置了代理时用 obfuscated —— TLS 传输的握手是同步阻塞的，
# 代理节点一旦卡住会冻结整个事件循环且超时无效；混淆传输全程异步，
# 卡住时超时/自动重试可以正常工作。不设代理时保持 full（安卓原行为不变）。
def pick_connection_type():
    value = os.environ.get("TG_CONNECTION", "").strip().lower()
    if value in ("full", "tls", "tcpfull"):
        return "full"
    if value in ("obfuscated", "obf", "tcpobfuscated"):
        return "obfuscated"
    return "obfuscated" if PROXY else "full"


CONNECTION_TYPE = pick_connection_type()

# ------------------------------------------------------------
# 下载与历史
# ------------------------------------------------------------
# 下载失败自动重试次数
DOWNLOAD_RETRIES = 3

# 进度日志间隔（百分比）
PROGRESS_STEP = 5

# 下载历史记录文件（SAVE_FOLDER 下），每行一条已完成下载：
# 时间 | 类型(普通/抖音) | 文件名 | 大小 | 来源
DOWNLOAD_HISTORY_FILE = os.path.join(SAVE_FOLDER, "download_history.txt")
DONE_DEFAULT_LINES = 10  # /done 默认显示行数
DONE_MAX_LINES = 50      # /done 允许的最大行数

# 无意义文件名模式：带横线的 UUID、不带横线的 32 位随机 hex（均可带扩展名）
UUID_FILENAME_PATTERN = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[0-9a-f]{32})(\.[A-Za-z0-9]{1,8})?$",
    re.IGNORECASE,
)

# ============================================================
# 抖音链接自动解析
# ============================================================
# 在 Saved Messages（收藏夹）发送抖音链接后：
# Userbot 会自动发送给 @DouYintg_bot，收到视频后下载到手机。
DOUYIN_BOT_USERNAME = "@DouYintg_bot"
DOUYIN_BOT_TIMEOUT = 120
DOUYIN_SAVE_SUBFOLDER = "Douyin"
DOUYIN_MAX_RESPONSES = 12

# ============================================================
# Instagram 链接自动解析
# ============================================================
# 复用同一个解析机器人（@DouYintg_bot），流程与抖音一致，
# 但下载目录分开存放。
INSTAGRAM_BOT_USERNAME = "@DouYintg_bot"
INSTAGRAM_BOT_TIMEOUT = 120
INSTAGRAM_SAVE_SUBFOLDER = "Instagram"
INSTAGRAM_MAX_RESPONSES = 12

# 平台配f置表：抖音 / Instagram 共用同一套解析下载流程
PLATFORM_CONFIG = {
    "douyin": {
        "bot": DOUYIN_BOT_USERNAME,
        "subfolder": DOUYIN_SAVE_SUBFOLDER,
        "timeout": DOUYIN_BOT_TIMEOUT,
        "max_responses": DOUYIN_MAX_RESPONSES,
        "label": "抖音",
        "emoji": "🎬",
    },
    "instagram": {
        "bot": INSTAGRAM_BOT_USERNAME,
        "subfolder": INSTAGRAM_SAVE_SUBFOLDER,
        "timeout": INSTAGRAM_BOT_TIMEOUT,
        "max_responses": INSTAGRAM_MAX_RESPONSES,
        "label": "Instagram",
        "emoji": "📸",
    },
}

# 平台链接匹配正则（提取消息中的抖音 / Instagram 链接）
DOUYIN_URL_PATTERN = re.compile(
    r"https?://(?:v\.douyin\.com|www\.douyin\.com|m\.douyin\.com|douyin\.com)/[^\s<>\"]+",
    re.IGNORECASE,
)

INSTAGRAM_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:instagram\.com|instagr\.am)/[^\s<>\"]+",
    re.IGNORECASE,
)

# ============================================================
# 下载并发
# ============================================================
# 同时进行的下载数量（普通下载与抖音/Instagram 视频下载共享并发池）。
# 可通过 /thread 指令运行时调整（1-10），并持久化到 thread_config.json。
DOWNLOAD_CONCURRENCY = 3
DOWNLOAD_CONCURRENCY_MIN = 1
DOWNLOAD_CONCURRENCY_MAX = 10
THREAD_CONFIG_FILE = os.path.join(SAVE_FOLDER, "thread_config.json")

# 下载白名单：除 Saved Messages 外，白名单内的 chat 收到媒体消息也会
# 自动下载（保存到 SAVE_FOLDER/<chat标题>/）。通过 /wl 指令运行时管理，
# 持久化到 whitelist_config.json。
WHITELIST_FILE = os.path.join(SAVE_FOLDER, "whitelist_config.json")
WHITELIST_CHATS = {}  # {chat_id(带符号): 标题}，main() 启动时加载

# ------------------------------------------------------------
# bot 按钮菜单（可选）：用一个 bot 账号在私聊里提供可点击的按钮菜单。
# 按钮/键盘是 bot 账号的专属能力，userbot 账号发不出按钮。
# 给 bot 发任意消息（或 /start）显示主菜单；token 从 BotFather 获取（/token）。
# 只响应 owner 的消息与回调。token 属敏感信息，从 tg_secrets.json 读取，
# 不硬编码在代码里、也不会提交到仓库；留空则按钮菜单不启用。
# ------------------------------------------------------------
BOT_TOKEN = _SECRET_CONFIG.get("bot_token", "")
BOT_USERNAME = _SECRET_CONFIG.get("bot_username", "")
BOT_SESSION_NAME = SESSION_NAME + "_bot"

# 菜单回调 action 全集（encode_menu_data 生成 m:<action>[:<arg>] 载荷）
MENU_ACTIONS = (
    "home", "status", "progress", "done", "wl", "wl_add",
    "wl_del", "thread", "clean", "back",
    "queue", "queue_del", "retry", "retry_run", "retry_del",
)

# ------------------------------------------------------------
# 持久化下载队列：任务先入队（媒体存消息引用、平台链接存完整 URL），
# 重启后自动恢复执行。失败任务移入 retry 列表停靠，由用户手动重试。
# ------------------------------------------------------------
QUEUE_FILE = os.path.join(SAVE_FOLDER, "download_queue.json")

# Telethon 的请求没有读超时：代理节点卡住时 get_messages 等请求会永久挂起，
# 把信号量槽位占满、整条队列堵死。取消息步骤必须加外部超时。
QUEUE_FETCH_TIMEOUT = 30

QUEUE_KIND_LABELS = {"media": "媒体", "douyin": "抖音", "instagram": "Instagram"}

# ------------------------------------------------------------
# Saved Messages 消息清理
# ------------------------------------------------------------
# 是否自动清理 Saved Messages 中的程序指令、抖音链接和程序通知
AUTO_CLEAN_SAVED_MESSAGES = True

# 消息至少存在多少分钟后才允许删除
CLEAN_MESSAGE_AGE_MINUTES = 1

# 自动清理执行间隔，默认 1 分钟，可通过 /setcleartime 修改
DEFAULT_CLEAR_INTERVAL_SECONDS = 60
CLEAR_TIME_CONFIG_FILE = os.path.join(SAVE_FOLDER, "clear_time.json")
CLEAR_INTERVAL_SECONDS = DEFAULT_CLEAR_INTERVAL_SECONDS

# 需要自动清理的命令（精确匹配）
CLEAN_COMMANDS = {
    "/status",
    "/folder",
    "/logpath",
    "/help",
    "/clean",
    "/clearmsg",
    "/done",
    "/progress",
    "/downloading",
}

# 程序通知回复的前缀（以此开头的消息会被自动清理）
CLEAN_NOTIFICATION_PREFIXES = (
    "🟢 TG Userbot 状态正常",
    "📁 保存目录：",
    "📋 日志文件：",
    "📖 TG Userbot 命令",
    "🧹 清理完成，共删除",
    "📥 开始下载",
    "✅ 下载完成",
    "❌ 文件下载失败",
    "🎬 开始下载抖音视频",
    "🎬 抖音视频下载完成",
    "❌ 抖音视频下载失败",
    "❌ 抖音链接处理失败",
    # /setcleartime 的回复
    "✅ 自动清理间隔已设置为",
    "⏸ 自动清理已关闭。",
    "⏱ 自动清理当前间隔：",
    "❌ 格式错误",
    # /done 的回复
    "📜 下载记录：暂无记录",
    "📜 最近",
    "📜 匹配",
    "📜 没有匹配",
    # /progress 的回复
    "📊 当前没有进行中的下载",
    "📊 当前下载进度",
    # /thread 的回复
    "🧵 当前并发下载数",
    "✅ 并发下载数已设置为",
    # /wl 的回复
    "📋 下载白名单",
    "✅ 已加入白名单",
    "✅ 已从白名单移除",
    "✅ 该 chat 已在白名单",
    "✅ Saved Messages 始终生效",
    "❌ /wl",
    # /queue、/retry 的回复
    "📥 下载队列",
    "🔁 待重试列表",
    "✅ 已从队列移除",
    "✅ 已从待重试列表移除",
    "▶️ 已重新执行",
    "⏳ 该任务正在执行中",
    "❌ 队列任务原消息已被删除",
    "❌ 任务已不存在",
    "❌ /queue",
    "❌ /retry",
    # Instagram 的通知
    "📸 开始下载Instagram视频",
    "📸 Instagram视频下载完成",
    "❌ Instagram视频下载失败",
    "❌ Instagram链接处理失败",
)

# ------------------------------------------------------------
# 登录看门狗（start_with_retry 的超时与重试）
# ------------------------------------------------------------
LOGIN_TIMEOUT_SECONDS = 120
LOGIN_RETRIES = 10

# ============================================================
# 运行时全局状态（main() 内创建/填充，模块导入时保持默认值）
# ============================================================
# 注意：client 和 asyncio 锁必须在 main()（事件循环内部）创建，
# 不能在模块导入时创建。Python 3.9 下 asyncio 原语会在构造时绑定
# 当时的默认事件循环，而 asyncio.run() 会新建循环——绑定错循环后
# 连接能建立但所有请求永久挂起、无任何报错（经典陷阱）。
# 手机上能跑是因为 Termux 的 Python 3.10+ 原语改为惰性绑定。
client = None
bot_client = None
BOT_ID = None  # bot 账号的用户 id（bot 登录后填充，清理 bot 对话时用）
QUEUE = {"tasks": [], "retry": []}  # 内存队列，main() 启动时从 QUEUE_FILE 加载
QUEUE_LOCK = None  # asyncio.Lock，main() 里创建（事件循环规则）
EXECUTING = set()  # 正在执行的任务 id（防重复触发）
DOWNLOAD_SEMAPHORE = None
DOUYIN_LOCK = None
PROCESSING_DOUYIN_IDS = set()
MY_ID = None

# 进行中下载注册表（/progress 指令用）
ACTIVE_DOWNLOADS = {}
_download_seq = 0

CLEAR_TIME_CHANGED = None  # asyncio.Event，main() 里创建（/setcleartime 唤醒清理循环）


class AdjustableSemaphore:
    """并发上限可动态调整的信号量（asyncio.Semaphore 创建后不可改值）。"""

    def __init__(self, limit: int):
        self._limit = max(1, int(limit))
        self._in_use = 0
        self._waiters = deque()

    async def acquire(self):
        while self._in_use >= self._limit:
            fut = asyncio.get_running_loop().create_future()
            self._waiters.append(fut)
            try:
                await fut
            except asyncio.CancelledError:
                try:
                    self._waiters.remove(fut)
                except ValueError:
                    pass
                raise
        self._in_use += 1

    def release(self):
        if self._in_use > 0:
            self._in_use -= 1
        self._wake_waiters()

    def set_limit(self, limit: int):
        self._limit = max(1, int(limit))
        self._wake_waiters()

    def _wake_waiters(self):
        while self._waiters and self._in_use < self._limit:
            fut = self._waiters.popleft()
            if not fut.done():
                fut.set_result(None)

    @property
    def limit(self):
        return self._limit

    async def __aenter__(self):
        await self.acquire()

    async def __aexit__(self, exc_type, exc, tb):
        self.release()


def load_thread_config():
    global DOWNLOAD_CONCURRENCY
    try:
        if os.path.exists(THREAD_CONFIG_FILE):
            with open(THREAD_CONFIG_FILE, "r", encoding="utf-8") as f:
                value = int(json.load(f).get("concurrency", DOWNLOAD_CONCURRENCY))
            if not (DOWNLOAD_CONCURRENCY_MIN <= value <= DOWNLOAD_CONCURRENCY_MAX):
                value = DOWNLOAD_CONCURRENCY
            DOWNLOAD_CONCURRENCY = value
    except Exception as e:
        DOWNLOAD_CONCURRENCY = DOWNLOAD_CONCURRENCY
        logger.warning(f"读取并发配置失败，使用默认 {DOWNLOAD_CONCURRENCY}：{e}")


def save_thread_config(concurrency):
    try:
        with open(THREAD_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"concurrency": int(concurrency)}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存并发配置失败：{e}")


def load_whitelist(path=None):
    """读取下载白名单 JSON，返回 {chat_id: 标题}；文件缺失/损坏返回空。"""
    path = path or WHITELIST_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        chats = {}
        for chat_id, title in data.get("chats", []):
            chats[int(chat_id)] = str(title)
        return chats
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"读取白名单配置失败，使用空白名单：{e}")
        return {}


def save_whitelist(chats, path=None):
    """把白名单写入 JSON（按 ID 排序，输出稳定）。"""
    path = path or WHITELIST_FILE
    try:
        pairs = sorted(chats.items())
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"chats": [[int(cid), str(title)] for cid, title in pairs]},
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        logger.warning(f"保存白名单配置失败：{e}")

# ============================================================
# 下载历史记录
# ============================================================


def append_history(record: str):
    """向下载历史文件追加一行记录（失败仅记日志，不影响下载）。"""
    try:
        with open(DOWNLOAD_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(record + "\n")
    except Exception as e:
        logger.warning(f"写入下载历史失败：{e}")


def get_history_lines(n=None):
    """读取历史文件最后 n 行（n 为 None 时读全部），文件不存在返回空列表。"""
    try:
        with open(DOWNLOAD_HISTORY_FILE, "r", encoding="utf-8") as f:
            lines = [line.rstrip("\n") for line in f.readlines()]
        if n is None:
            return lines
        return lines[-n:]
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning(f"读取下载历史失败：{e}")
        return []


# ============================================================
# 进行中下载注册表（/progress 指令用）
# ============================================================


def register_download(label, filename, total, link=None):
    """登记一个开始下载的任务，返回下载 ID。link 为来源消息链接（可选）。"""
    global _download_seq
    _download_seq += 1
    did = _download_seq
    ACTIVE_DOWNLOADS[did] = {
        "label": label,
        "filename": filename,
        "total": total,
        "downloaded": 0,
        "percent": 0,
        "link": link,
    }
    return did


def update_download(did, current, total):
    """更新下载进度（由 progress_callback 调用）。"""
    info = ACTIVE_DOWNLOADS.get(did)
    if info is None:
        return
    info["downloaded"] = current
    if total:
        info["total"] = total
        info["percent"] = min(int(current * 100 / total), 100)
    else:
        info["percent"] = None


def unregister_download(did):
    ACTIVE_DOWNLOADS.pop(did, None)


def is_done_command(text):
    # /done、/done 10、/done 关键词、/done 10 关键词...
    return bool(re.fullmatch(r"/done(?:\s+\S+)*", text.strip(), re.IGNORECASE))


def is_thread_command(text):
    return bool(re.fullmatch(r"/thread(?:\s+\d+)?", text.strip(), re.IGNORECASE))


# ============================================================
# 初始化目录
# ============================================================
os.makedirs(SAVE_FOLDER, exist_ok=True)

# ============================================================
# 日志
# ============================================================
logger = logging.getLogger("tg_userbot")
logger.setLevel(logging.INFO)
logger.handlers.clear()

formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# ============================================================
# Telegram Client
# ============================================================
def create_client() -> TelegramClient:
    return TelegramClient(
        SESSION_NAME,
        API_ID,
        API_HASH,
        connection=(
            ConnectionTcpObfuscated
            if CONNECTION_TYPE == "obfuscated"
            else ConnectionTcpFull
        ),
        connection_retries=10,
        retry_delay=3,
        auto_reconnect=True,
        proxy=PROXY,
    )


def create_bot_client() -> TelegramClient:
    """创建 bot 账号客户端（按钮菜单用），连接配置与 userbot 一致。"""
    return TelegramClient(
        BOT_SESSION_NAME,
        API_ID,
        API_HASH,
        connection=(
            ConnectionTcpObfuscated
            if CONNECTION_TYPE == "obfuscated"
            else ConnectionTcpFull
        ),
        connection_retries=10,
        retry_delay=3,
        auto_reconnect=True,
        proxy=PROXY,
    )

# ============================================================
# 工具函数
# ============================================================
def sanitize_filename(name: str) -> str:
    """清理 Android / Windows 不适合出现在文件名中的字符。"""
    if not name:
        return "未命名文件"

    name = str(name).strip()

    # Android 常见非法/不安全字符
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)

    # 去掉连续空格
    name = re.sub(r"\s+", " ", name).strip()

    # 避免文件名末尾出现空格或点
    name = name.rstrip(" .")

    return name or "未命名文件"


def unique_path(path: str) -> str:
    """文件重名时自动增加 (1)、(2)..."""
    if not os.path.exists(path):
        return path

    base, ext = os.path.splitext(path)
    index = 1

    while True:
        new_path = f"{base} ({index}){ext}"
        if not os.path.exists(new_path):
            return new_path
        index += 1


def format_size(size):
    if size is None:
        return "未知大小"

    size = float(size)
    units = ["B", "KB", "MB", "GB", "TB"]

    for unit in units:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024

    return f"{size:.2f} PB"


def is_meaningless_filename(name: str) -> bool:
    """判断文件名是否无意义（空、未命名、随机 UUID/hex 等）。"""
    if not name:
        return True

    base = os.path.splitext(name.strip())[0]
    if base in ("未命名文件", "未命名"):
        return True

    return bool(UUID_FILENAME_PATTERN.match(name.strip()))


def generate_fallback_filename(message, kind: str) -> str:
    """用媒体类型 + 消息时间生成兜底文件名，如 video_20260904_021530。"""
    try:
        if message.date:
            stamp = message.date.strftime("%Y%m%d_%H%M%S")
        else:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    except Exception:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{kind}_{stamp}"


def get_original_filename(message) -> str:
    """尽可能完整地获取 Telegram 原始文件名。"""
    try:
        if message.file:
            name = message.file.name
            if name:
                return name

        document = getattr(message, "document", None)
        if document:
            for attr in getattr(document, "attributes", []) or []:
                name = getattr(attr, "file_name", None)
                if name:
                    return name
    except Exception:
        logger.exception("获取原始文件名失败")

    return "未命名文件"


def get_file_extension(message, filename: str) -> str:
    """原文件名没有后缀时，根据媒体/MIME 类型自动补后缀。"""
    if filename and os.path.splitext(filename)[1]:
        return ""

    try:
        if message.photo:
            return ".jpg"

        mime = getattr(message.file, "mime_type", None) if message.file else None

        if message.voice:
            return ".ogg"

        if message.video:
            known = {
                "video/mp4": ".mp4",
                "video/webm": ".webm",
                "video/x-matroska": ".mkv",
                "video/quicktime": ".mov",
                "video/x-msvideo": ".avi",
            }
            return known.get(mime) or mimetypes.guess_extension(mime or "") or ".mp4"

        if message.audio:
            known = {
                "audio/mpeg": ".mp3",
                "audio/mp4": ".m4a",
                "audio/x-m4a": ".m4a",
                "audio/ogg": ".ogg",
                "audio/wav": ".wav",
                "audio/x-wav": ".wav",
                "audio/flac": ".flac",
            }
            return known.get(mime) or mimetypes.guess_extension(mime or "") or ".mp3"

        if mime:
            return mimetypes.guess_extension(mime) or ""

    except Exception as e:
        logger.warning(f"自动判断文件后缀失败：{e}")

    return ""


def get_caption(message) -> str:
    """获取 Caption / 消息文字；无 caption 返回空串。

    注意不能用 sanitize_filename("") 的兜底值「未命名文件」——
    那会把所有无说明文件的文件名都加上「未命名文件 - 」前缀。
    """
    try:
        text = (message.message or "").strip()
        return sanitize_filename(text) if text else ""
    except Exception:
        return ""


def compute_final_filename(message) -> str:
    """根据消息计算最终落盘文件名（download_file 与队列展示共用）。

    规则：有 caption 用 caption 拼接原名；无意义文件名（未命名/UUID）
    用 caption 或 媒体类型_时间戳 兜底；原名缺后缀时按 MIME 推断。
    """
    original_filename = sanitize_filename(get_original_filename(message))
    caption = get_caption(message)

    extension = get_file_extension(message, original_filename)
    if extension and not os.path.splitext(original_filename)[1]:
        original_filename += extension

    if is_meaningless_filename(original_filename):
        ext = os.path.splitext(original_filename)[1] or extension or ""
        if caption:
            return sanitize_filename(caption + ext)

        if message.voice:
            kind = "voice"
        elif message.video:
            kind = "video"
        elif message.photo:
            kind = "photo"
        elif message.audio:
            kind = "audio"
        else:
            kind = "file"
        return generate_fallback_filename(message, kind) + ext

    if caption:
        return sanitize_filename(f"{caption} - {original_filename}")
    return original_filename


def message_link(chat_id, msg_id):
    """生成可跳转的 Telegram 消息链接。

    频道/超级群组（-100 前缀的负 id）→ https://t.me/c/<id>/<msg_id>；
    私聊与 Saved Messages（正 id）没有链接格式 → None。
    """
    if chat_id is None or msg_id is None:
        return None
    try:
        chat_id = int(chat_id)
        msg_id = int(msg_id)
    except (TypeError, ValueError):
        return None
    if chat_id < 0 and str(chat_id).startswith("-100"):
        return f"https://t.me/c/{str(chat_id)[4:]}/{msg_id}"
    return None


def message_source_link(message, fallback_chat_id=None):
    """消息的来源链接：转发消息优先链到原频道消息（fwd_from.channel_post），
    否则用消息自身的 chat 生成；私聊/Saved Messages 返回 None。"""
    fwd = getattr(message, "fwd_from", None)
    if fwd:
        from_id = getattr(fwd, "from_id", None)
        post = getattr(fwd, "channel_post", None)
        if from_id and post:
            try:
                link = message_link(get_peer_id(from_id), post)
                if link:
                    return link
            except Exception:
                pass
    return message_link(fallback_chat_id, getattr(message, "id", None))


def entity_display_name(entity) -> str:
    """实体 → 可读名称：频道用标题，用户用姓名，都没有用用户名。"""
    try:
        title = getattr(entity, "title", None)
        if title:
            return str(title)

        first_name = getattr(entity, "first_name", "") or ""
        last_name = getattr(entity, "last_name", "") or ""
        username = getattr(entity, "username", "") or ""

        full_name = f"{first_name} {last_name}".strip()
        if full_name:
            return full_name

        return username
    except Exception:
        return ""


async def get_forward_source(message) -> str:
    """获取 Forward From 名称，用作目录名。"""
    try:
        forward = message.fwd_from
        if not forward:
            return "未分类"

        from_id = getattr(forward, "from_id", None)
        if not from_id:
            return "未分类"

        entity = await client.get_entity(from_id)

        name = entity_display_name(entity)
        if name:
            return sanitize_filename(name)

    except Exception as e:
        logger.warning(f"获取 Forward From 失败：{e}")

    return "未分类"


def get_media_type(message) -> str:
    """仅用于日志显示。"""
    if not message.media:
        return "None"

    try:
        if message.photo:
            return "Photo"
        if message.video:
            return "Video"
        if message.audio:
            return "Audio"
        if message.voice:
            return "Voice"
        if message.document:
            return "Document"
    except Exception:
        pass

    return type(message.media).__name__


def is_downloadable(message) -> bool:
    """判断消息是否包含可下载媒体。"""
    try:
        # Telethon 对文件、图片、视频等通常都会提供 message.file
        if message.file:
            return True

        # 某些媒体情况下 file 属性可能暂时无法判断，再检查这些属性
        if message.photo or message.document or message.video or message.audio:
            return True

    except Exception:
        pass

    return False

# ============================================================
# 链接自动解析（抖音 / Instagram）
# ============================================================
def extract_urls_by_pattern(text: str, pattern):
    """用指定正则从文字中提取链接，去重并补全协议。"""
    if not text:
        return []

    urls = []
    for match in pattern.finditer(text):
        url = match.group(0).strip().rstrip(".,，。！？；;)")
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url

        if url not in urls:
            urls.append(url)

    return urls


def extract_douyin_urls(text: str):
    """从消息文字中提取抖音链接。"""
    return extract_urls_by_pattern(text, DOUYIN_URL_PATTERN)


def extract_instagram_urls(text: str):
    """从消息文字中提取 Instagram 链接。"""
    return extract_urls_by_pattern(text, INSTAGRAM_URL_PATTERN)


def is_media_message(message) -> bool:
    """判断解析机器人回复是否包含可以下载的媒体。"""
    try:
        if message.file:
            return True
        if message.video or message.document or message.photo or message.audio:
            return True
    except Exception:
        pass
    return False


def get_platform_save_folder(kind: str) -> str:
    folder = os.path.join(SAVE_FOLDER, PLATFORM_CONFIG[kind]["subfolder"])
    os.makedirs(folder, exist_ok=True)
    return folder


def get_bot_video_filename(message, kind: str) -> str:
    """
    获取解析机器人返回视频的文件名（抖音 / Instagram 通用）。
    优先使用机器人 Caption 里的标题，例如：
    “标题：可爱的小狐狸 #游戏破壁计划 #鸣潮 #心月狐”
    → “可爱的小狐狸.mp4”
    如果没有标题，再使用 Telegram 原始文件名。
    """
    try:
        caption = (message.message or "").strip()

        if caption:
            # 机器人截图中的格式通常为：
            # 标题：可爱的小狐狸 #游戏破壁计划 #鸣潮 #心月狐
            title_match = re.search(
                r"(?:^|\n)\s*标题\s*[：:]\s*(.+)",
                caption,
                re.IGNORECASE,
            )

            if title_match:
                title = title_match.group(1).strip()

                # 如果标题后还有换行，只取标题第一行。
                title = title.splitlines()[0].strip()

                # 保留标题中的全部 Hashtag。
                title = sanitize_filename(title)

                if title:
                    return title + ".mp4"

            # 没有“标题：”时，尝试使用第一行文本作为标题。
            first_line = caption.splitlines()[0].strip()
            first_line = re.sub(r"^标题\s*[：:]\s*", "", first_line)
            # 保留标题中的全部 Hashtag。
            first_line = first_line.strip()

            if first_line and len(first_line) <= 100:
                first_line = sanitize_filename(first_line)
                if first_line:
                    return first_line + ".mp4"

    except Exception as e:
        logger.warning(f"读取视频标题失败：{e}")

    # 没有标题时再使用 Telegram 原始文件名（无意义名/UUID 除外）
    try:
        if message.file and message.file.name:
            original = sanitize_filename(message.file.name)
            if original and not is_meaningless_filename(original):
                return original
    except Exception:
        pass

    # 兜底：文件名缺失或为 UUID 时用 平台_时间戳 命名
    return generate_fallback_filename(message, kind) + ".mp4"


async def download_bot_video(message, source_url: str, kind: str) -> bool:
    """把解析机器人返回的视频下载到本地（抖音 / Instagram 通用）。"""
    cfg = PLATFORM_CONFIG[kind]
    label = cfg["label"]
    emoji = cfg["emoji"]
    subfolder = cfg["subfolder"]

    async with DOWNLOAD_SEMAPHORE:
        folder = get_platform_save_folder(kind)
        filename = get_bot_video_filename(message, kind)

        if not os.path.splitext(filename)[1]:
            filename += get_file_extension(message, filename) or ".mp4"

        final_path = unique_path(os.path.join(folder, filename))
        temp_path = final_path + ".download"

        size = None
        try:
            size = message.file.size if message.file else None
        except Exception:
            pass

        # 登记进行中下载（/progress 可见）
        did = register_download(label, os.path.basename(final_path), size)

        try:
            logger.info("=" * 60)
            logger.info(f"{emoji} 开始下载{label}视频")
            logger.info(f"{label}链接：{source_url}")
            logger.info(f"返回消息 ID：{message.id}")
            logger.info(f"文件名：{os.path.basename(final_path)}")
            logger.info(f"文件大小：{format_size(size)}")
            logger.info(f"保存目录：{folder}")

            # 开始下载即发通知（程序消息稍后会被自动清理）
            try:
                await client.send_message(
                    "me",
                    f"{emoji} 开始下载{label}视频\n\n"
                    f"文件：{os.path.basename(final_path)}\n"
                    f"大小：{format_size(size)}\n"
                    f"来源：{source_url}",
                )
            except Exception as e:
                logger.warning(f"发送{label}下载开始通知失败：{e}")

            for attempt in range(1, DOWNLOAD_RETRIES + 1):
                try:
                    if os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass

                    last_percent = -1

                    def progress(current, total):
                        nonlocal last_percent
                        update_download(did, current, total)
                        if not total:
                            return
                        percent = min(int(current * 100 / total), 100)
                        if percent >= last_percent + PROGRESS_STEP or percent == 100:
                            last_percent = percent
                            logger.info(
                                f"{emoji} {label}下载进度：{percent}% "
                                f"({format_size(current)}/{format_size(total)})"
                                f" | {os.path.basename(final_path)}"
                            )

                    logger.info(
                        f"⬇️ {label}下载尝试 {attempt}/{DOWNLOAD_RETRIES}"
                        f" | {os.path.basename(final_path)}"
                    )

                    result = await message.download_media(
                        file=temp_path,
                        progress_callback=progress,
                    )

                    if not result or not os.path.exists(temp_path):
                        raise RuntimeError("Telegram 返回下载结果，但临时文件不存在")

                    actual_size = os.path.getsize(temp_path)
                    if actual_size <= 0:
                        raise RuntimeError("下载完成，但文件大小为 0")

                    os.replace(temp_path, final_path)

                    # 记录下载历史（每行一条）
                    append_history(
                        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {label} | "
                        f"{os.path.basename(final_path)} | {format_size(actual_size)}"
                        f" | {source_url}"
                    )

                    logger.info(f"✅ {label}视频下载完成")
                    logger.info(f"文件：{final_path}")
                    logger.info(f"实际大小：{format_size(actual_size)}")
                    logger.info("=" * 60)

                    try:
                        await client.send_message(
                            "me",
                            f"{emoji} {label}视频下载完成\\n\\n"
                            f"文件：{os.path.basename(final_path)}\\n"
                            f"大小：{format_size(actual_size)}\\n"
                            f"保存位置：{subfolder}/\\n"
                            f"来源：{source_url}",
                        )
                    except Exception as e:
                        logger.warning(f"发送{label}完成通知失败：{e}")

                    return True

                except (ConnectionError, TimeoutError, OSError, RPCError) as e:
                    logger.exception(
                        f"❌ {label}视频下载失败，尝试 {attempt}/{DOWNLOAD_RETRIES}：{e}"
                    )
                    if attempt < DOWNLOAD_RETRIES:
                        await asyncio.sleep(3)
                        try:
                            if not client.is_connected():
                                await client.connect()
                        except Exception as reconnect_error:
                            logger.exception(f"重新连接 Telegram 失败：{reconnect_error}")

                except Exception as e:
                    logger.exception(
                        f"❌ {label}视频下载出现未预期错误，"
                        f"尝试 {attempt}/{DOWNLOAD_RETRIES}：{e}"
                    )
                    if attempt < DOWNLOAD_RETRIES:
                        await asyncio.sleep(3)

            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

            try:
                await client.send_message(
                    "me",
                    f"❌ {label}视频下载失败\\n\\n"
                    f"来源：{source_url}\\n"
                    f"请查看：{LOG_FILE}",
                )
            except Exception:
                pass

            return False

        finally:
            unregister_download(did)


async def click_first_useful_button(message) -> bool:
    """优先点击“下载 MP4 HD”，其次尝试其它 MP4/无水印视频按钮。"""
    try:
        if not message.buttons:
            return False

        buttons = []
        for row_index, row in enumerate(message.buttons):
            for col_index, button in enumerate(row):
                button_text = (getattr(button, "text", "") or "").strip()
                buttons.append((row_index, col_index, button_text))
                logger.info(
                    f"🔘 解析机器人按钮：[{row_index},{col_index}] {button_text}"
                )

        hd_keywords = (
            "下载 MP4 HD", "下载MP4 HD", "MP4 HD",
            "高清 MP4", "高清MP4",
        )
        for row_index, col_index, button_text in buttons:
            if any(k.lower() in button_text.lower() for k in hd_keywords):
                await message.click(row_index, col_index)
                logger.info(f"⭐ 已优先点击高清 MP4：{button_text}")
                return True

        video_keywords = (
            "下载 MP4", "下载MP4", "无水印", "原视频",
            "download", "video",
        )
        for row_index, col_index, button_text in buttons:
            if "mp3" in button_text.lower():
                continue
            if any(k.lower() in button_text.lower() for k in video_keywords):
                await message.click(row_index, col_index)
                logger.info(f"🖱️ 已点击视频下载按钮：{button_text}")
                return True

        return False
    except Exception as e:
        logger.warning(f"点击解析机器人按钮失败：{e}")
        return False


async def process_platform_url(source_url: str, saved_message_id: int, kind: str):
    """把链接交给解析机器人并下载返回的视频，返回是否成功。

    saved_message_id 仅用于日志；队列恢复时可为 None。
    """
    cfg = PLATFORM_CONFIG[kind]
    label = cfg["label"]
    bot_username = cfg["bot"]
    bot_timeout = cfg["timeout"]
    max_responses = cfg["max_responses"]

    async with DOUYIN_LOCK:
        logger.info("=" * 60)
        logger.info(f"🔎 检测到{label}链接")
        logger.info(
            f"消息 ID：{saved_message_id if saved_message_id is not None else '无（队列恢复）'}"
        )
        logger.info(f"链接：{source_url}")
        logger.info(f"解析机器人：{bot_username}")

        try:
            bot_entity = await client.get_entity(bot_username)

            async with client.conversation(
                bot_entity,
                timeout=bot_timeout,
                exclusive=False,
            ) as conv:
                fallback_response = None
                logger.info(f"📤 正在把{label}链接发送给解析机器人...")
                await conv.send_message(source_url)

                for response_index in range(1, max_responses + 1):
                    try:
                        response = await conv.get_response(
                            timeout=bot_timeout
                        )
                    except asyncio.TimeoutError:
                        logger.error(
                            f"⏰ 等待解析机器人超时：{bot_timeout} 秒"
                        )
                        if fallback_response is not None:
                            logger.warning(
                                "⚠️ 高清 MP4 未返回，改用最初返回的视频"
                            )
                            await download_bot_video(
                                fallback_response, source_url, kind
                            )
                            return True
                        break

                    response_text = (response.message or "").strip()

                    logger.info(
                        f"📩 收到解析机器人回复 {response_index}/"
                        f"{max_responses} | ID={response.id} | "
                        f"media={get_media_type(response)} | "
                        f"text={response_text[:200] or '(无)'}"
                    )

                    # 机器人可能先返回普通视频，并附带“下载 MP4 HD”等按钮。
                    # 优先请求 HD；普通视频保存为兜底。
                    if is_media_message(response):
                        logger.info("🎬 检测到机器人返回直接视频")

                        clicked = await click_first_useful_button(response)
                        if clicked:
                            fallback_response = response
                            logger.info(
                                "⭐ 已请求高清 MP4；当前直接返回的视频作为兜底"
                            )
                            continue

                        logger.info("⬇️ 没有可用下载按钮，直接下载返回视频")
                        await download_bot_video(response, source_url, kind)
                        return True

                    clicked = await click_first_useful_button(response)
                    if clicked:
                        logger.info("⭐ 已点击高清/视频下载按钮，等待高清媒体")
                        continue

                    error_keywords = (
                        "解析失败", "下载失败", "链接无效", "无法解析",
                        "不存在", "失效", "error", "failed", "invalid"
                    )
                    if any(k.lower() in response_text.lower()
                           for k in error_keywords):
                        logger.error(
                            f"❌ 解析机器人返回失败信息：{response_text[:500]}"
                        )
                        break

        except Exception as e:
            logger.exception(f"❌ {label}链接处理失败：{source_url} | {e}")

        # conversation 正常结束但没有拿到高清结果时，用普通视频兜底。
        if fallback_response is not None:
            logger.warning("⚠️ 未拿到高清 MP4，使用普通视频兜底下载")
            try:
                if await download_bot_video(fallback_response, source_url, kind):
                    return True
            except Exception as e:
                logger.exception(f"兜底下载普通{label}视频失败：{e}")

        try:
            await client.send_message(
                "me",
                f"❌ {label}链接处理失败\\n\\n"
                f"链接：{source_url}\\n"
                f"请查看：{LOG_FILE}",
            )
        except Exception:
            pass

        logger.info("=" * 60)

    return False


async def handle_media_links(message, douyin_urls, instagram_urls):
    """处理一条 Saved Messages 消息中的抖音/Instagram 链接。"""
    if not douyin_urls and not instagram_urls:
        return

    if message.id in PROCESSING_DOUYIN_IDS:
        logger.info(f"⏭️ 消息 ID={message.id} 已在处理中，跳过重复触发")
        return

    PROCESSING_DOUYIN_IDS.add(message.id)

    try:
        for url in douyin_urls:
            await enqueue_and_start({
                "kind": "douyin",
                "url": url,
                "msg_id": message.id,
                "label": url,
            })
        for url in instagram_urls:
            await enqueue_and_start({
                "kind": "instagram",
                "url": url,
                "msg_id": message.id,
                "label": url,
            })
    finally:
        PROCESSING_DOUYIN_IDS.discard(message.id)


# ============================================================
# 下载
# ============================================================
async def resolve_download_source(message, source_override=None):
    """下载来源目录名：白名单 chat 用 chat 标题；否则沿用转发来源。"""
    if source_override:
        return source_override
    return await get_forward_source(message)


async def download_file(message, source_override=None):
    async with DOWNLOAD_SEMAPHORE:
        source = await resolve_download_source(message, source_override)
        final_filename = compute_final_filename(message)
        # 日志展示用（与 final_filename 的计算共用同一套规则）
        caption = get_caption(message)
        original_filename = sanitize_filename(get_original_filename(message))

        folder = os.path.join(SAVE_FOLDER, source)
        os.makedirs(folder, exist_ok=True)

        final_path = unique_path(os.path.join(folder, final_filename))
        temp_path = final_path + ".download"

        size = None
        try:
            size = message.file.size if message.file else None
        except Exception:
            pass

        # 登记进行中下载（/progress 可见，带来源链接）
        did = register_download(
            "普通",
            os.path.basename(final_path),
            size,
            link=message_source_link(message, message.chat_id),
        )

        try:
            logger.info("=" * 60)
            logger.info("📥 开始下载")
            logger.info(f"消息 ID：{message.id}")
            logger.info(f"来源：{source}")
            logger.info(f"Caption：{caption or '(无)'}")
            logger.info(f"原始文件名：{original_filename}")
            logger.info(f"最终文件名：{os.path.basename(final_path)}")
            logger.info(f"文件大小：{format_size(size)}")
            logger.info(f"保存目录：{folder}")

            # 开始下载即发通知（程序消息稍后会被自动清理）
            try:
                await client.send_message(
                    "me",
                    "📥 开始下载\n\n"
                    f"来源：{source}\n"
                    f"文件：{os.path.basename(final_path)}\n"
                    f"大小：{format_size(size)}",
                )
            except Exception as e:
                logger.warning(f"发送下载开始通知失败：{e}")

            for attempt in range(1, DOWNLOAD_RETRIES + 1):
                try:
                    # 清理上一次失败留下的临时文件
                    if os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass

                    last_percent = -1

                    def progress(current, total):
                        nonlocal last_percent
                        update_download(did, current, total)

                        if not total:
                            return

                        percent = int(current * 100 / total)
                        percent = min(percent, 100)

                        if percent >= last_percent + PROGRESS_STEP or percent == 100:
                            last_percent = percent
                            logger.info(
                                f"⬇️ 下载进度：{percent}% "
                                f"({format_size(current)}/{format_size(total)})"
                                f" | {os.path.basename(final_path)}"
                            )

                    logger.info(
                        f"⬇️ 下载尝试 {attempt}/{DOWNLOAD_RETRIES}"
                        f" | {os.path.basename(final_path)}"
                    )

                    result = await message.download_media(
                        file=temp_path,
                        progress_callback=progress,
                    )

                    if not result or not os.path.exists(temp_path):
                        raise RuntimeError("Telegram 返回下载结果，但临时文件不存在")

                    actual_size = os.path.getsize(temp_path)
                    if actual_size <= 0:
                        raise RuntimeError("下载完成，但文件大小为 0")

                    # 大小一致性校验：实际大小明显小于消息声明大小时打警告，
                    # 便于排查「客户端显示大、下载却小」的问题。
                    if size and actual_size < size * 0.98:
                        logger.warning(
                            f"⚠️ 实际下载大小（{format_size(actual_size)}）"
                            f"小于消息声明大小（{format_size(size)}），"
                            "可能是客户端显示的是原始大小，而 Telegram "
                            "存储的是转码后的文件"
                        )

                    os.replace(temp_path, final_path)

                    # 记录下载历史（每行一条）
                    append_history(
                        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 普通 | "
                        f"{os.path.basename(final_path)} | {format_size(actual_size)}"
                        f" | 来源：{source}"
                    )

                    logger.info("✅ 下载完成")
                    logger.info(f"文件：{final_path}")
                    logger.info(f"实际大小：{format_size(actual_size)}")
                    logger.info("=" * 60)

                    try:
                        await client.send_message(
                            "me",
                            "✅ 下载完成\n\n"
                            f"来源：{source}\n"
                            f"文件：{os.path.basename(final_path)}\n"
                            f"大小：{format_size(actual_size)}",
                        )
                    except Exception as e:
                        logger.warning(f"发送完成通知失败：{e}")

                    return True

                except (ConnectionError, TimeoutError, OSError, RPCError) as e:
                    logger.exception(
                        f"❌ 下载失败，尝试 {attempt}/{DOWNLOAD_RETRIES}：{e}"
                    )

                    if attempt < DOWNLOAD_RETRIES:
                        logger.info("🔄 3 秒后重试下载...")
                        await asyncio.sleep(3)

                        try:
                            if not client.is_connected():
                                logger.info("🔌 Telegram 连接已断开，正在重新连接...")
                                await client.connect()
                                logger.info("✅ Telegram 重新连接成功")
                        except Exception as reconnect_error:
                            logger.exception(
                                f"重新连接 Telegram 失败：{reconnect_error}"
                            )

                except Exception as e:
                    logger.exception(
                        f"❌ 下载出现未预期错误，尝试 {attempt}/{DOWNLOAD_RETRIES}：{e}"
                    )

                    if attempt < DOWNLOAD_RETRIES:
                        logger.info("🔄 3 秒后重试下载...")
                        await asyncio.sleep(3)

            logger.error("❌ 已达到最大重试次数，下载失败")

            try:
                await client.send_message(
                    "me",
                    "❌ 文件下载失败\n\n"
                    f"来源：{source}\n"
                    f"文件：{os.path.basename(final_path)}\n"
                    f"请查看 download.log",
                )
            except Exception:
                pass

            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

            return False

        finally:
            unregister_download(did)

# ============================================================
# Saved Messages 自动清理
# ============================================================
# 只删除程序自己产生的指令、抖音链接和程序通知，
# 不删除普通文件/图片/视频消息。
def format_clear_interval(seconds):
    seconds = int(seconds)
    if seconds % 3600 == 0:
        return f"{seconds // 3600}小时"
    if seconds % 60 == 0:
        return f"{seconds // 60}分钟"
    return f"{seconds}秒"


def parse_clear_interval(value):
    value = value.strip().lower()
    if value in {"off", "0", "关闭"}:
        return 0
    match = re.fullmatch(r"(\d+)(s|m|h)", value)
    if not match:
        raise ValueError("格式错误")
    seconds = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]
    if seconds < 10:
        raise ValueError("清理间隔不能小于 10 秒")
    return seconds


def load_clear_interval():
    global CLEAR_INTERVAL_SECONDS
    try:
        if os.path.exists(CLEAR_TIME_CONFIG_FILE):
            with open(CLEAR_TIME_CONFIG_FILE, "r", encoding="utf-8") as f:
                value = int(json.load(f).get("interval_seconds", DEFAULT_CLEAR_INTERVAL_SECONDS))
            if value != 0 and value < 10:
                value = DEFAULT_CLEAR_INTERVAL_SECONDS
            CLEAR_INTERVAL_SECONDS = value
    except Exception as e:
        CLEAR_INTERVAL_SECONDS = DEFAULT_CLEAR_INTERVAL_SECONDS
        logger.warning(
            f"读取自动清理配置失败，使用默认 "
            f"{DEFAULT_CLEAR_INTERVAL_SECONDS // 60} 分钟：{e}"
        )


def save_clear_interval(seconds):
    try:
        with open(CLEAR_TIME_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"interval_seconds": int(seconds)}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存自动清理配置失败：{e}")


def is_setcleartime_command(text):
    return bool(re.fullmatch(r"/setcleartime(?:\s+.*)?", text.strip(), re.IGNORECASE))


# 抖音链接原始指令消息
# 程序处理完成后，按照和其它程序消息相同的定时清理规则删除。


def is_cleanup_message(message) -> bool:
    """判断 Saved Messages 中的消息是否属于程序指令/通知。"""
    try:
        text = (message.message or "").strip()
        if text in CLEAN_COMMANDS:
            return True

        if is_setcleartime_command(text):
            return True

        # /done 指令（/done 或 /done 数字）
        if is_done_command(text):
            return True

        # /thread 指令（/thread 或 /thread 数字）
        if is_thread_command(text):
            return True

        # /wl 指令（/wl、/wl add @xxx、/wl del 123 ...）
        if is_wl_command(text):
            return True

        # /queue、/retry 指令（含子命令）
        if is_queue_command(text) or is_retry_command(text):
            return True

        # 程序自己发送/产生的链接指令：
        # 只要消息中包含抖音 / Instagram URL，就视为下载指令，纳入定时清理。
        if extract_douyin_urls(text) or extract_instagram_urls(text):
            return True

        return any(text.startswith(prefix) for prefix in CLEAN_NOTIFICATION_PREFIXES)
    except Exception:
        return False


async def cleanup_saved_messages_once():
    """清理一段时间以前的程序指令和通知。"""
    if not MY_ID:
        return

    try:
        from datetime import datetime, timezone, timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(
            minutes=CLEAN_MESSAGE_AGE_MINUTES
        )

        delete_ids = []

        # Saved Messages 通常不会很多，逐页检查即可。
        async for message in client.iter_messages("me", limit=300):
            if not is_cleanup_message(message):
                continue

            msg_date = message.date
            if msg_date and msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)

            if msg_date and msg_date < cutoff:
                delete_ids.append(message.id)

        if delete_ids:
            await client.delete_messages("me", delete_ids)
            logger.info(
                f"🧹 自动清理 Saved Messages：删除 {len(delete_ids)} 条程序消息"
            )
        else:
            logger.info("⏱ 自动清理检查完成：没有超过时限的程序消息")

    except Exception as e:
        logger.exception(f"自动清理 Saved Messages 失败：{e}")


async def cleanup_loop():
    """后台定时清理任务。"""
    while True:
        try:
            if CLEAR_INTERVAL_SECONDS <= 0:
                if CLEAR_TIME_CHANGED is not None:
                    await CLEAR_TIME_CHANGED.wait()
                    CLEAR_TIME_CHANGED.clear()
                else:
                    await asyncio.sleep(60)
                continue

            interval = CLEAR_INTERVAL_SECONDS
            if CLEAR_TIME_CHANGED is not None:
                try:
                    await asyncio.wait_for(CLEAR_TIME_CHANGED.wait(), timeout=interval)
                    CLEAR_TIME_CHANGED.clear()
                    continue
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(interval)

            if CLEAR_INTERVAL_SECONDS != interval:
                continue
            await cleanup_saved_messages_once()
            await cleanup_bot_chat_once()
        except asyncio.CancelledError:
            logger.info("🛑 Saved Messages 自动清理任务已停止")
            raise
        except Exception as e:
            logger.exception(f"自动清理循环异常：{e}")
            await asyncio.sleep(30)


# ============================================================
# /clearmsg：手动清理程序相关消息
# ============================================================
async def clear_program_messages():
    """
    删除 Saved Messages 中：
    1. 抖音链接消息
    2. Userbot 程序通知
    3. 程序命令
    不删除普通收藏内容、普通文件、图片、视频。
    """
    delete_ids = []

    async for message in client.iter_messages("me", limit=3000):
        if is_cleanup_message(message):
            delete_ids.append(message.id)

    if delete_ids:
        await client.delete_messages("me", delete_ids)

    return len(delete_ids)


# ============================================================
# 命令处理
# ============================================================
async def handle_command(event, text):
    global MY_ID, CLEAR_INTERVAL_SECONDS, DOWNLOAD_CONCURRENCY, DOWNLOAD_SEMAPHORE

    if text == "/status":
        await event.reply(status_text())
        logger.info("执行命令：/status")
        return True

    if text == "/folder":
        await event.reply(f"📁 保存目录：\n{SAVE_FOLDER}")
        logger.info("执行命令：/folder")
        return True

    if text == "/logpath":
        await event.reply(f"📋 日志文件：\n{LOG_FILE}")
        logger.info("执行命令：/logpath")
        return True

    if text == "/done" or text.startswith("/done "):
        # 用法：/done、/done 10、/done 关键词、/done 10 关键词
        parts = text.split(maxsplit=2)
        n = DONE_DEFAULT_LINES
        keyword = None
        if len(parts) >= 2 and parts[1]:
            if parts[1].isdigit():
                n = int(parts[1])
                if len(parts) == 3 and parts[2]:
                    keyword = parts[2].strip().lower()
            else:
                keyword = parts[1].strip().lower()
                n = DONE_MAX_LINES
        if n < 1:
            n = 1
        elif n > DONE_MAX_LINES:
            n = DONE_MAX_LINES

        if keyword is not None:
            # 模糊匹配：大小写不敏感的子串匹配，扫描全部历史
            await event.reply(done_reply_text(n, keyword))
            logger.info(f"执行命令：/done 关键词「{keyword}」")
            return True

        await event.reply(done_reply_text(n))
        logger.info(f"执行命令：/done {n}")
        return True

    if text == "/progress" or text == "/downloading":
        await event.reply(progress_text())
        logger.info(f"执行命令：/progress | 进行中 {len(ACTIVE_DOWNLOADS)} 个")
        return True

    if text == "/thread" or text.startswith("/thread "):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            await event.reply(
                f"🧵 当前并发下载数：{DOWNLOAD_CONCURRENCY}\n"
                f"用法：/thread 3（{DOWNLOAD_CONCURRENCY_MIN}-{DOWNLOAD_CONCURRENCY_MAX}）"
            )
            logger.info("执行命令：/thread（查询）")
            return True
        ok, msg = apply_thread_limit(parts[1])
        await event.reply(msg)
        logger.info(f"执行命令：/thread {parts[1]} 成功={ok}")
        return True

    parsed_wl = parse_wl_command(text)
    if parsed_wl is not None:
        action, arg = parsed_wl
        logger.info(f"执行命令：/wl {action}")

        if action == "list":
            await event.reply(wl_list_text())
            return True

        if action == "add":
            try:
                if arg:
                    try:
                        target = int(arg)
                    except ValueError:
                        target = arg
                    chat_id, title = await resolve_wl_target(client, target)
                else:
                    # 不带参数：从回复的转发消息里取来源 chat。
                    reply_id = event.message.reply_to_msg_id
                    if not reply_id:
                        await event.reply(
                            "❌ /wl：请带参数（ID 或 @用户名），或回复一条"
                            "从目标 chat 转发的消息后发送 /wl add"
                        )
                        return True
                    reply_msg = await client.get_messages("me", ids=reply_id)
                    if not reply_msg or not getattr(reply_msg, "fwd_from", None):
                        await event.reply(
                            "❌ /wl：回复的消息不是转发的，取不到来源 chat"
                        )
                        return True
                    chat_id, title = await resolve_wl_target(
                        client, None, reply_msg.fwd_from
                    )

                if chat_id is None:
                    await event.reply(
                        f"❌ /wl：无法找到该 chat：{arg or '转发来源'}"
                    )
                    return True
                ok, msg = add_to_whitelist(chat_id, title)
                await event.reply(msg)
            except Exception as e:
                logger.warning(f"/wl add 失败：{e}")
                await event.reply(
                    f"❌ /wl：无法找到该 chat：{arg or '转发来源'}"
                )
            return True

        if action == "del":
            ok, msg = del_from_whitelist(arg or "")
            await event.reply(msg)
            return True

        await event.reply(
            "❌ 用法：/wl list | /wl add <ID或@用户名> | /wl del <ID或序号>"
        )
        return True

    if is_queue_command(text):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            await event.reply(
                format_queue_text(QUEUE), link_preview=False
            )
        elif parts[1].startswith("del "):
            try:
                idx = int(parts[1].split(None, 1)[1])
            except (IndexError, ValueError):
                await event.reply("❌ /queue del 用法：/queue del <序号>")
                return True
            async with QUEUE_LOCK:
                ok, removed = queue_remove(QUEUE, "tasks", idx)
                if ok:
                    save_queue(QUEUE)
            if ok:
                await event.reply(
                    f"✅ 已从队列移除：{removed.get('label', '')}"
                )
            else:
                await event.reply("❌ /queue：序号无效，用 /queue 查看列表")
        else:
            await event.reply("❌ 用法：/queue | /queue del <序号>")
        logger.info(f"执行命令：/queue {parts[1] if len(parts) > 1 else ''}")
        return True

    if is_retry_command(text):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            await event.reply(
                format_retry_text(QUEUE), link_preview=False
            )
        elif parts[1].startswith("del "):
            try:
                idx = int(parts[1].split(None, 1)[1])
            except (IndexError, ValueError):
                await event.reply("❌ /retry del 用法：/retry del <序号>")
                return True
            async with QUEUE_LOCK:
                ok, removed = queue_remove(QUEUE, "retry", idx)
                if ok:
                    save_queue(QUEUE)
            if ok:
                await event.reply(
                    f"✅ 已从待重试列表移除：{removed.get('label', '')}"
                )
            else:
                await event.reply(
                    "❌ /retry：序号无效，用 /retry 查看列表"
                )
        else:
            try:
                idx = int(parts[1])
            except ValueError:
                await event.reply(
                    "❌ 用法：/retry | /retry <序号> | /retry del <序号>"
                )
                return True
            async with QUEUE_LOCK:
                retry_list = QUEUE["retry"]
                record = (
                    retry_list[idx - 1] if 1 <= idx <= len(retry_list) else None
                )
            if record is None:
                await event.reply("❌ /retry：序号无效，用 /retry 查看列表")
                return True
            if record["id"] in EXECUTING:
                await event.reply("⏳ 该任务正在执行中")
                return True
            asyncio.create_task(execute_queued_task(record))
            await event.reply(f"▶️ 已重新执行：{record.get('label', '')}")
        logger.info(f"执行命令：/retry {parts[1] if len(parts) > 1 else ''}")
        return True

    if text == "/help":
        await event.reply(
            "📖 TG Userbot 命令\n\n"
            "/status - 查看运行状态\n"
            "/folder - 查看保存目录\n"
            "/logpath - 查看日志路径\n"
            "/done - 查看最近下载记录（默认 10 条）\n"
            "/done 20 - 查看最近 20 条下载记录\n"
            "/done 关键词 - 模糊匹配下载记录\n"
            "/done 20 关键词 - 最近 20 条中模糊匹配\n"
            "/progress - 查看进行中下载的进度\n"
            "/thread - 查看当前并发下载数\n"
            "/thread 5 - 设置并发下载数为 5（1-10）\n"
            "/wl - 查看下载白名单\n"
            "/wl add @用户名 - 加入白名单（也可回复转发消息后 /wl add）\n"
            "/wl del ID或序号 - 移出白名单\n"
            "/queue - 查看下载队列\n"
            "/queue del 序号 - 从队列移除任务\n"
            "/retry - 查看待重试列表\n"
            "/retry 序号 - 重新执行某条失败任务\n"
            "/retry del 序号 - 从待重试列表移除\n"
            "/clean - 清理 .download 临时文件\n"
            "/clearmsg - 清理程序产生的命令、通知和链接指令\n"
            "/setcleartime 1m - 设置自动清理间隔\n"
            "/setcleartime off - 关闭自动清理\n"
            "/help - 查看帮助\n\n"
            "🎬 抖音：发送抖音链接到 Saved Messages，自动解析并优先下载 MP4 HD。\n"
            "📸 Instagram：发送 Instagram 链接到 Saved Messages，自动解析下载。\n"
            "📥 普通文件、图片、视频发送到 Saved Messages 会自动下载。\n"
            "📥 白名单 chat 里的媒体也会自动下载到对应子目录。\n"
            f"🤖 按钮菜单：给 {BOT_USERNAME} 发任意消息，用按钮操作。"
        )
        logger.info("执行命令：/help")
        return True

    if text.startswith("/setcleartime"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            current = "已关闭" if CLEAR_INTERVAL_SECONDS <= 0 else format_clear_interval(CLEAR_INTERVAL_SECONDS)
            await event.reply(f"⏱ 自动清理当前间隔：{current}\n用法：/setcleartime 1m\n支持：30s、1m、2m、1h\n关闭：/setcleartime off")
            return True
        try:
            seconds = parse_clear_interval(parts[1])
        except ValueError:
            await event.reply("❌ 格式错误。示例：/setcleartime 1m、/setcleartime 2m、/setcleartime 1h、/setcleartime off")
            return True
        CLEAR_INTERVAL_SECONDS = seconds
        save_clear_interval(seconds)
        if CLEAR_TIME_CHANGED is not None:
            CLEAR_TIME_CHANGED.set()
        await event.reply("⏸ 自动清理已关闭。" if seconds == 0 else f"✅ 自动清理间隔已设置为 {format_clear_interval(seconds)}。")
        return True

    if text == "/clearmsg":
        logger.info("执行命令：/clearmsg")
        try:
            # 先统计并删除其它程序消息；当前 /clearmsg 留到最后删除。
            delete_ids = []

            async for message in client.iter_messages("me", limit=3000):
                if message.id == event.message.id:
                    continue

                if is_cleanup_message(message):
                    delete_ids.append(message.id)

            count = len(delete_ids) + 1

            # 先反馈结果，避免当前 /clearmsg 被删除后无法回复。
            await event.reply(
                f"🧹 清理完成，共删除 {count} 条程序相关消息\\n\\n"
                "已清理：抖音链接、程序通知、程序命令、/clearmsg 指令\\n"
                "普通收藏内容不会删除。"
            )

            # 删除其它消息 + 当前 /clearmsg 指令。
            if delete_ids:
                await client.delete_messages("me", delete_ids)

            await client.delete_messages("me", [event.message.id])

            logger.info(
                f"执行命令：/clearmsg | 删除 {count} 条程序相关消息"
            )

        except Exception as e:
            logger.exception(f"/clearmsg 执行失败：{e}")
            # 如果清理过程中失败，至少尝试保留错误信息。
            try:
                await event.reply(f"❌ 清理失败：{e}")
            except Exception:
                pass

        return True

    if text == "/clean":
        count = clean_temp_files()
        await event.reply(f"🧹 清理完成，共删除 {count} 个临时文件")
        logger.info(f"执行命令：/clean | 删除 {count} 个临时文件")
        return True

    return False

# ============================================================
# 下载白名单
# ============================================================
# 除 Saved Messages（"me"）外，白名单内的 chat 收到媒体消息也会自动下载，
# 保存到 SAVE_FOLDER/<chat标题>/。/wl 命令只在 "me" 生效。
def classify_message_chat(chat_id, my_id, whitelist):
    """决定一条消息属于哪个来源：
    ("me", None)    — 自己的 Saved Messages，全功能（命令/链接解析/下载）
    ("chat", 标题)  — 白名单 chat，仅下载媒体
    (None, None)    — 忽略
    """
    if chat_id == my_id:
        return ("me", None)
    if chat_id in (whitelist or {}):
        return ("chat", whitelist[chat_id])
    return (None, None)


def is_wl_command(text):
    # /wl、/wl list、/wl add @xxx、/wl del 123 ...
    return bool(re.fullmatch(r"/wl(?:\s+\S+)*", text.strip(), re.IGNORECASE))


def is_queue_command(text):
    # /queue、/queue del 1 ...
    return bool(re.fullmatch(r"/queue(?:\s+\S+)*", text.strip(), re.IGNORECASE))


def is_retry_command(text):
    # /retry、/retry 1、/retry del 1 ...
    return bool(re.fullmatch(r"/retry(?:\s+\S+)*", text.strip(), re.IGNORECASE))


def parse_wl_command(text):
    """解析 /wl 命令，返回 (动作, 参数)；非 /wl 命令返回 None。

    动作：list / add / del / invalid
    add 参数：@用户名、数字 ID（可负数），空字符串表示配合回复的转发消息。
    """
    if not is_wl_command(text):
        return None
    parts = text.split(maxsplit=1)
    if len(parts) == 1:
        return ("list", None)
    sub = parts[1].strip()
    if sub == "list":
        return ("list", None)
    if sub.startswith("add"):
        return ("add", sub[len("add"):].strip())
    if sub.startswith("del"):
        return ("del", sub[len("del"):].strip())
    return ("invalid", None)


def resolve_wl_del_key(key, whitelist):
    """/wl del 参数 → 要删除的 chat id：先按 ID 匹配，再按列表序号。"""
    try:
        numeric = int(key)
    except (TypeError, ValueError):
        return None
    if numeric in whitelist:
        return numeric
    if numeric > 0 and numeric <= len(whitelist):
        return sorted(whitelist)[numeric - 1]
    return None


# ============================================================
# 持久化下载队列
# ============================================================
# 任务先入队再执行：媒体任务存消息引用（chat_id + msg_id，Telegram 服务器
# 上媒体永久在，重启后按 id 取回继续下载）；平台链接任务存完整 URL（原
# 消息即使被自动清理也不影响）。失败任务移入 retry 列表停靠，不自动重试，
# 由用户通过 /retry 或按钮手动触发。文件顺序即执行顺序（FIFO）。
def load_queue(path=None):
    """读取队列文件，返回 {"tasks": [...], "retry": [...]}；缺失/损坏返回空。"""
    path = path or QUEUE_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "tasks": list(data.get("tasks", [])),
            "retry": list(data.get("retry", [])),
        }
    except FileNotFoundError:
        return {"tasks": [], "retry": []}
    except Exception as e:
        logger.warning(f"读取下载队列失败，使用空队列：{e}")
        return {"tasks": [], "retry": []}


def save_queue(queue, path=None):
    """原子写入队列文件（temp + os.replace）。"""
    path = path or QUEUE_FILE
    try:
        temp_path = path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    except Exception as e:
        logger.warning(f"保存下载队列失败：{e}")


def queue_enqueue(queue, record):
    """把任务追加到活跃队列末尾，补上 id/attempts 字段，返回该记录。"""
    record = dict(record)
    record.setdefault("id", uuid.uuid4().hex)
    record.setdefault("attempts", 0)
    queue["tasks"].append(record)
    return record


def queue_fail_to_retry(queue, record):
    """执行失败：从 tasks 移除该记录（按 id），attempts+1，追加到 retry 末尾。"""
    for i, r in enumerate(queue["tasks"]):
        if r.get("id") == record.get("id"):
            moved = queue["tasks"].pop(i)
            moved["attempts"] = moved.get("attempts", 0) + 1
            queue["retry"].append(moved)
            return


def queue_remove(queue, list_key, index):
    """按 1 起始序号从指定列表（tasks/retry）移除，返回 (是否成功, 被移除记录)。"""
    lst = queue.get(list_key)
    if not lst or not isinstance(index, int) or index < 1 or index > len(lst):
        return False, None
    return True, lst.pop(index - 1)


def queue_retry_success(queue, record):
    """手动重试成功：从 retry 移除（按 id）。"""
    for i, r in enumerate(queue["retry"]):
        if r.get("id") == record.get("id"):
            queue["retry"].pop(i)
            return


def queue_retry_failed(queue, record):
    """手动重试失败：attempts+1，保持 retry 中的位置不变。"""
    for r in queue["retry"]:
        if r.get("id") == record.get("id"):
            r["attempts"] = r.get("attempts", 0) + 1
            return


def _queue_record_display(record):
    kind_label = QUEUE_KIND_LABELS.get(record.get("kind"), record.get("kind"))
    if record.get("kind") == "media" and record.get("final_name"):
        label = record["final_name"]
    else:
        label = record.get("label") or record.get("url") or "(无)"
    lines = [f"[{kind_label}] {label}"]
    source = _queue_record_source(record)
    if source:
        lines.append(f"来源：{source}")
    return "\n".join(lines)


def _queue_record_source(record):
    """任务来源展示：有链接（频道/转发原频道）优先，否则 #消息ID。"""
    link = record.get("source_link") or message_link(
        record.get("chat_id"), record.get("msg_id")
    )
    if link:
        return link
    if record.get("msg_id") is not None:
        return f"#{record['msg_id']}"
    return ""


def format_queue_text(queue):
    """活跃队列列表文本。"""
    tasks = queue.get("tasks", [])
    if not tasks:
        return "📥 下载队列：空"
    lines = [
        f"{i}. {_queue_record_display(r)}"
        for i, r in enumerate(tasks, start=1)
    ]
    return f"📥 下载队列（等待中 {len(tasks)} 条）：\n\n" + "\n".join(lines)


def format_retry_text(queue):
    """待重试列表文本。"""
    retry = queue.get("retry", [])
    if not retry:
        return "🔁 待重试列表：空"
    lines = [
        f"{i}. {_queue_record_display(r)}（已尝试 {r.get('attempts', 0)} 次）"
        for i, r in enumerate(retry, start=1)
    ]
    return f"🔁 待重试列表（{len(retry)} 条）：\n\n" + "\n".join(lines)


# ------------------------------------------------------------
# 队列执行（异步部分）
# ------------------------------------------------------------

async def enqueue_and_start(record):
    """任务入队（持久化）并立即触发执行。"""
    async with QUEUE_LOCK:
        # queue_enqueue 返回带 id 的副本，执行必须用这份副本，
        # 否则 execute_queued_task 按 id 收尾时对不上队列里的记录。
        record = queue_enqueue(QUEUE, record)
        save_queue(QUEUE)
    asyncio.create_task(execute_queued_task(record))


async def _run_queued_task(record):
    """执行单个队列任务，返回是否成功。

    媒体任务原消息已被删除时返回 True（视为终结，直接移除并通知）。
    """
    kind = record.get("kind")
    if kind == "media":
        try:
            message = await asyncio.wait_for(
                client.get_messages(
                    record["chat_id"], ids=record["msg_id"]
                ),
                timeout=QUEUE_FETCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error(
                f"⏰ 队列任务取消息超时（{QUEUE_FETCH_TIMEOUT}s）："
                f"{record.get('label') or '(无)'}"
            )
            return False
        except Exception as e:
            logger.warning(f"队列任务取消息失败：{e}")
            return False
        if not message:
            label = record.get("label") or ""
            logger.warning(f"队列任务原消息已被删除：{label}")
            try:
                await client.send_message(
                    "me",
                    f"❌ 队列任务原消息已被删除，已移除：\n{label}",
                )
            except Exception:
                pass
            return True
        return await download_file(
            message, record.get("source_override")
        )
    if kind in ("douyin", "instagram"):
        return await process_platform_url(
            record["url"], record.get("msg_id"), kind
        )
    logger.warning(f"未知队列任务类型：{kind}，直接移除")
    return True


async def execute_queued_task(record):
    """执行队列任务并更新持久化状态：
    成功 → 移除；失败 → 移入 retry（已在 retry 的手动重试失败则留原处）。
    """
    EXECUTING.add(record["id"])
    logger.info(
        f"▶️ 队列任务开始：{record.get('label') or record.get('url') or '(无)'}"
    )
    try:
        try:
            # 注意：这里不能再拿 DOWNLOAD_SEMAPHORE —— download_file 与
            # download_bot_video 内部各自 acquire 同一个信号量，队列层再包
            # 一层会嵌套死锁（并发 ≥2 时槽位互相等待）。真正的下载并发仍由
            # 内部信号量约束，抖音解析流程由 DOUYIN_LOCK 串行。
            success = await _run_queued_task(record)
        except Exception as e:
            logger.exception(f"队列任务执行异常：{e}")
            success = False

        async with QUEUE_LOCK:
            in_retry = any(
                r.get("id") == record["id"] for r in QUEUE["retry"]
            )
            if success:
                if in_retry:
                    queue_retry_success(QUEUE, record)
                else:
                    QUEUE["tasks"] = [
                        r for r in QUEUE["tasks"]
                        if r.get("id") != record["id"]
                    ]
            else:
                if in_retry:
                    queue_retry_failed(QUEUE, record)
                else:
                    queue_fail_to_retry(QUEUE, record)
            save_queue(QUEUE)
    finally:
        EXECUTING.discard(record["id"])


def recover_queue_tasks():
    """启动时重新触发活跃队列中的任务（失败/中断的任务重启后自动重来）。"""
    for record in list(QUEUE["tasks"]):
        asyncio.create_task(execute_queued_task(record))


# ============================================================
# bot 按钮菜单
# ============================================================
# 按钮/键盘是 bot 账号的专属能力（实测 userbot 账号发的按钮官方客户端不渲染）。
# 用一个独立 bot 账号（BOT_TOKEN）与 userbot 同进程运行：bot 私聊提供
# 按钮菜单，回调触发后复用下方的服务函数执行真实操作，并原地更新菜单消息。
# bot 只响应 owner（MY_ID）的消息与回调。
def encode_menu_data(action, arg=None):
    """把菜单动作编码成回调数据（Telegram 限制 ≤64 字节）。"""
    data = f"m:{action}"
    if arg is not None:
        data += f":{arg}"
    return data.encode("utf-8")


def parse_menu_data(data):
    """解析回调数据，返回 (动作, 参数)；无法识别返回 ("unknown", None)。"""
    try:
        parts = data.decode("utf-8").split(":")
        if len(parts) < 2 or len(parts) > 3 or parts[0] != "m":
            return ("unknown", None)
        action = parts[1]
        arg = parts[2] if len(parts) == 3 else None
        if action not in MENU_ACTIONS:
            return ("unknown", None)
        return (action, arg)
    except Exception:
        return ("unknown", None)


def build_main_menu_text():
    return (
        "🤖菜单\n\n"
        "点击按钮操作，结果会更新在这条消息里。\n"
        "【我的收藏】 里的命令照常可用。"
    )


def main_menu_buttons():
    return [
        [Button.inline("📊 状态", encode_menu_data("status")),
         Button.inline("📈 进度", encode_menu_data("progress"))],
        [Button.inline("📜 下载记录", encode_menu_data("done")),
         Button.inline("📋 白名单", encode_menu_data("wl"))],
        [Button.inline("📥 队列", encode_menu_data("queue")),
         Button.inline("🔁 待重试", encode_menu_data("retry"))],
        [Button.inline("🧵 并发", encode_menu_data("thread")),
         Button.inline("🧹 清理", encode_menu_data("clean"))],
    ]


def queue_menu_buttons():
    rows = []
    for i, r in enumerate(QUEUE["tasks"], start=1):
        label = (r.get("label") or "(无)")[:20]
        rows.append(
            [Button.inline(
                f"❌ {i} {label}", encode_menu_data("queue_del", r["id"])
            )]
        )
    rows.append([Button.inline("🔙 返回主菜单", encode_menu_data("home"))])
    return rows


def retry_menu_buttons():
    rows = []
    for i, r in enumerate(QUEUE["retry"], start=1):
        label = (r.get("label") or "(无)")[:14]
        rows.append(
            [
                Button.inline(
                    f"▶️ {i}", encode_menu_data("retry_run", r["id"])
                ),
                Button.inline(
                    f"❌ {i} {label}", encode_menu_data("retry_del", r["id"])
                ),
            ]
        )
    rows.append([Button.inline("🔙 返回主菜单", encode_menu_data("home"))])
    return rows


def back_home_buttons():
    return [[Button.inline("🔙 返回主菜单", encode_menu_data("home"))]]


def wl_menu_buttons():
    rows = [[Button.inline("➕ 添加", encode_menu_data("wl_add"))]]
    for cid, title in sorted(WHITELIST_CHATS.items()):
        rows.append(
            [Button.inline(f"➖ {title}", encode_menu_data("wl_del", str(cid)))]
        )
    rows.append([Button.inline("🔙 返回主菜单", encode_menu_data("home"))])
    return rows


def thread_menu_buttons():
    return [
        [Button.inline("3", encode_menu_data("thread", "3")),
         Button.inline("5", encode_menu_data("thread", "5")),
         Button.inline("10", encode_menu_data("thread", "10"))],
        [Button.inline("🔙 返回主菜单", encode_menu_data("home"))],
    ]


# ============================================================
# 命令服务函数（Saved Messages 命令与 bot 按钮菜单共用）
# ============================================================
def status_text():
    """生成 /status 回复文本。"""
    try:
        connected = bool(client and client.is_connected())
    except Exception:
        connected = False
    return (
        "🟢 TG Userbot 状态正常\n\n"
        f"连接：{'正常' if connected else '断开'}\n"
        f"用户 ID：{MY_ID}\n"
        f"保存目录：{SAVE_FOLDER}\n"
        f"日志：{LOG_FILE}"
    )


def progress_text():
    """生成 /progress 回复文本。"""
    if not ACTIVE_DOWNLOADS:
        return "📊 当前没有进行中的下载"
    lines = []
    for info in sorted(ACTIVE_DOWNLOADS.values(), key=lambda x: x["filename"]):
        if info["percent"] is None:
            prog = f"{format_size(info['downloaded'])}/未知大小"
        else:
            prog = (
                f"{info['percent']}% "
                f"({format_size(info['downloaded'])}/{format_size(info['total'])})"
            )
        line = f"[{info['label']}] {info['filename']} - {prog}"
        if info.get("link"):
            line += f"\n  来源：{info['link']}"
        lines.append(line)
    return "📊 当前下载进度：\n\n" + "\n".join(lines)


def done_reply_text(n, keyword=None):
    """生成 /done 回复文本（命令与 bot 菜单共用）。"""
    if keyword is not None:
        matched = [
            line for line in get_history_lines(None)
            if keyword in line.lower()
        ]
        lines = matched[-n:]
        if not lines:
            return f"📜 没有匹配「{keyword}」的下载记录"

        # Telegram 单条消息上限 4096 字符，从最新往前凑到约 3800
        shown = []
        total = 0
        for line in reversed(lines):
            if total + len(line) + 1 > 3800:
                break
            shown.append(line)
            total += len(line) + 1
        shown.reverse()

        if not shown:
            shown = [lines[-1][:3800]]

        return (
            f"📜 匹配「{keyword}」的下载记录（最近 {len(shown)} 条）：\n\n"
            + "\n".join(shown)
        )

    lines = get_history_lines(n)
    if not lines:
        return "📜 下载记录：暂无记录"

    # Telegram 单条消息上限 4096 字符，从最新往前凑到约 3800
    shown = []
    total = 0
    for line in reversed(lines):
        if total + len(line) + 1 > 3800:
            break
        shown.append(line)
        total += len(line) + 1
    shown.reverse()

    if not shown:
        shown = [lines[-1][:3800]]

    return f"📜 最近 {len(shown)} 条下载记录：\n\n" + "\n".join(shown)


def wl_list_text(chats=None):
    """生成白名单列表文本（命令与 bot 菜单共用）。"""
    chats = WHITELIST_CHATS if chats is None else chats
    if not chats:
        return (
            "📋 下载白名单：空\n\n"
            "用法：/wl add <ID或@用户名>，或回复一条从目标 chat "
            "转发的消息后发送 /wl add"
        )
    lines = [
        f"{i}. {title} ({cid})"
        for i, (cid, title) in enumerate(sorted(chats.items()), start=1)
    ]
    return "📋 下载白名单：\n\n" + "\n".join(lines)


def apply_thread_limit(value):
    """设置并发下载数，返回 (是否成功, 提示文本)。命令与 bot 菜单共用。"""
    global DOWNLOAD_CONCURRENCY
    try:
        n = int(value)
    except (TypeError, ValueError):
        return False, (
            f"❌ 格式错误。用法：/thread 3"
            f"（{DOWNLOAD_CONCURRENCY_MIN}-{DOWNLOAD_CONCURRENCY_MAX}）"
        )
    if not (DOWNLOAD_CONCURRENCY_MIN <= n <= DOWNLOAD_CONCURRENCY_MAX):
        return False, (
            f"❌ 格式错误。并发数需在 "
            f"{DOWNLOAD_CONCURRENCY_MIN}-{DOWNLOAD_CONCURRENCY_MAX} 之间"
        )
    DOWNLOAD_CONCURRENCY = n
    if DOWNLOAD_SEMAPHORE is not None:
        DOWNLOAD_SEMAPHORE.set_limit(n)
    save_thread_config(n)
    return True, f"✅ 并发下载数已设置为 {n}"


def clean_temp_files(root=None):
    """删除 .download 临时文件，返回删除数量。命令与 bot 菜单共用。"""
    root = root or SAVE_FOLDER
    count = 0
    for dirpath, dirs, files in os.walk(root):
        for filename in files:
            if filename.endswith(".download"):
                path = os.path.join(dirpath, filename)
                try:
                    os.remove(path)
                    count += 1
                except Exception as e:
                    logger.warning(f"清理失败：{path} | {e}")
    return count


def forward_source_info(fwd_from):
    """从转发头提取 (chat_id, 标题)，不依赖网络解析。

    from_name 是 Telegram 在转发来源不可访问（如私密频道）时附带在
    转发头里的标题；有它就不需要 get_entity（bot 账号无权限解析私密频道）。
    返回 (None, None) 表示没有可用的转发来源。
    """
    from_id = getattr(fwd_from, "from_id", None)
    if not from_id:
        return None, None
    chat_id = get_peer_id(from_id)
    name = getattr(fwd_from, "from_name", None) or ""
    return chat_id, name


async def resolve_wl_target(cli, target, fwd_from=None):
    """把 /wl add 的目标解析成 (chat_id, 标题)，返回 (None, None) 表示失败。

    target：数字 ID / @用户名 / Peer 对象。
    fwd_from：转发消息的 MessageFwdHeader。带 from_name 时优先用它，
    避免对私密频道调用 get_entity 被服务端拒绝。
    """
    if fwd_from is not None:
        chat_id, name = forward_source_info(fwd_from)
        if chat_id is not None:
            if name:
                return chat_id, sanitize_filename(name)
            try:
                entity = await cli.get_entity(fwd_from.from_id)
                return chat_id, sanitize_filename(
                    entity_display_name(entity) or f"chat_{chat_id}"
                )
            except Exception:
                return chat_id, f"chat_{chat_id}"
        return None, None

    try:
        entity = await cli.get_entity(target)
    except Exception:
        return None, None
    chat_id = get_peer_id(entity)
    return chat_id, sanitize_filename(
        entity_display_name(entity) or f"chat_{chat_id}"
    )


def add_to_whitelist(chat_id, title):
    """把 chat 加入下载白名单并持久化，返回 (是否成功, 提示文本)。"""
    if chat_id == MY_ID:
        return False, "✅ Saved Messages 始终生效，无需加入白名单"
    if chat_id in WHITELIST_CHATS:
        return False, (
            f"✅ 该 chat 已在白名单：{WHITELIST_CHATS[chat_id]} ({chat_id})"
        )
    WHITELIST_CHATS[chat_id] = title
    save_whitelist(WHITELIST_CHATS)
    return True, f"✅ 已加入白名单：{title} ({chat_id})"


def del_from_whitelist(key):
    """从下载白名单移除 chat 并持久化，返回 (是否成功, 提示文本)。"""
    key_id = resolve_wl_del_key(key or "", WHITELIST_CHATS)
    if key_id is None:
        return False, "❌ /wl：白名单中没有该 ID/序号。用 /wl 查看列表"
    title = WHITELIST_CHATS.pop(key_id)
    save_whitelist(WHITELIST_CHATS)
    return True, f"✅ 已从白名单移除：{title} ({key_id})"


def plan_bot_chat_cleanup(messages, age_limit):
    """bot 菜单对话清理决策：删除超过时限的消息，但始终保留最新一条带按钮的菜单。

    messages: 按时间从新到旧排列的 [{"id", "age_minutes", "has_buttons"}, ...]
    返回 (要删除的 id 列表, 要保留的 id 集合)
    """
    keep = set()
    delete = []
    for m in messages:
        if m["has_buttons"] and not keep:
            keep.add(m["id"])
            continue
        if m["age_minutes"] > age_limit:
            delete.append(m["id"])
    return delete, keep


# ============================================================
# bot 菜单处理器（bot_client 在 main() 中创建并注册）
# ============================================================
async def handle_menu_action(action, arg, event):
    """按按钮动作执行并返回 (新文本, 新按钮)；返回 (None, None) 表示不改动消息。"""
    if action == "home":
        return build_main_menu_text(), main_menu_buttons()
    if action == "status":
        return status_text(), back_home_buttons()
    if action == "progress":
        return progress_text(), back_home_buttons()
    if action == "done":
        return done_reply_text(DONE_DEFAULT_LINES), back_home_buttons()
    if action == "wl":
        return wl_list_text(), wl_menu_buttons()
    if action == "wl_add":
        if arg is None:
            return (
                "📋 添加白名单：\n\n"
                "转发一条来自目标 chat 的消息到本对话，"
                "我会读取转发来源并请你确认添加。",
                back_home_buttons(),
            )
        try:
            entity = await bot_client.get_entity(int(arg))
            chat_id = get_peer_id(entity)
            title = sanitize_filename(
                entity_display_name(entity) or f"chat_{chat_id}"
            )
            ok, msg = add_to_whitelist(chat_id, title)
            return msg, back_home_buttons()
        except Exception as e:
            logger.warning(f"bot 菜单添加白名单失败：{e}")
            return "❌ 添加失败，无法找到该 chat", back_home_buttons()
    if action == "wl_del":
        ok, msg = del_from_whitelist(arg or "")
        return msg, back_home_buttons()
    if action == "thread":
        if arg is None:
            return (
                f"🧵 当前并发下载数：{DOWNLOAD_CONCURRENCY}\n选择新值：",
                thread_menu_buttons(),
            )
        ok, msg = apply_thread_limit(arg)
        return msg, back_home_buttons()
    if action == "clean":
        count = clean_temp_files()
        return f"🧹 清理完成，共删除 {count} 个临时文件", back_home_buttons()
    if action == "queue":
        return format_queue_text(QUEUE), queue_menu_buttons()
    if action == "queue_del":
        async with QUEUE_LOCK:
            before = len(QUEUE["tasks"])
            QUEUE["tasks"] = [
                r for r in QUEUE["tasks"] if r.get("id") != arg
            ]
            removed_any = len(QUEUE["tasks"]) != before
            if removed_any:
                save_queue(QUEUE)
        return (
            ("✅ 已从队列移除" if removed_any else "❌ 任务已不存在"),
            back_home_buttons(),
        )
    if action == "retry":
        return format_retry_text(QUEUE), retry_menu_buttons()
    if action == "retry_run":
        async with QUEUE_LOCK:
            record = next(
                (r for r in QUEUE["retry"] if r.get("id") == arg), None
            )
        if record is None:
            return "❌ 任务已不存在", back_home_buttons()
        if record["id"] in EXECUTING:
            return "⏳ 该任务正在执行中", back_home_buttons()
        asyncio.create_task(execute_queued_task(record))
        return (
            f"▶️ 已重新执行：{record.get('label', '')}",
            back_home_buttons(),
        )
    if action == "retry_del":
        async with QUEUE_LOCK:
            before = len(QUEUE["retry"])
            QUEUE["retry"] = [
                r for r in QUEUE["retry"] if r.get("id") != arg
            ]
            removed_any = len(QUEUE["retry"]) != before
            if removed_any:
                save_queue(QUEUE)
        return (
            ("✅ 已从待重试列表移除" if removed_any else "❌ 任务已不存在"),
            back_home_buttons(),
        )
    if action == "back":
        return build_main_menu_text(), main_menu_buttons()
    return None, None


async def bot_message_handler(event):
    """bot 账号收到 owner 私聊消息：转发消息走确认添加，其余显示主菜单。"""
    if event.out or MY_ID is None or event.chat_id != MY_ID:
        return
    message = event.message
    text = (message.message or "").strip()
    fwd = getattr(message, "fwd_from", None)
    from_id = getattr(fwd, "from_id", None) if fwd else None

    if from_id:
        chat_id, title = await resolve_wl_target(bot_client, None, fwd)
        if chat_id is None:
            logger.warning("bot 菜单解析转发来源失败")
            await bot_client.send_message(MY_ID, "❌ 无法解析转发来源")
            return
        if chat_id == MY_ID:
            await bot_client.send_message(
                MY_ID, "✅ Saved Messages 始终生效，无需加入白名单"
            )
        elif chat_id in WHITELIST_CHATS:
            await bot_client.send_message(
                MY_ID,
                f"✅ 该 chat 已在白名单：{WHITELIST_CHATS[chat_id]} ({chat_id})",
            )
        else:
            await bot_client.send_message(
                MY_ID,
                f"检测到转发来源：{title} ({chat_id})\n\n是否加入下载白名单？",
                buttons=[
                    [Button.inline(
                        "✅ 添加", encode_menu_data("wl_add", str(chat_id))
                    )],
                    [Button.inline("❌ 取消", encode_menu_data("home"))],
                ],
            )
        return

    # 任意文本（含 /start）→ 主菜单
    logger.info(f"🤖 bot 菜单：owner 发送 {text[:30]!r}，显示主菜单")
    await bot_client.send_message(
        MY_ID, build_main_menu_text(), buttons=main_menu_buttons()
    )

"""按钮回调功能"""
async def bot_callback_handler(event):
    """bot 按钮回调：解析动作、执行、原地更新消息。"""
    if MY_ID is None or event.chat_id != MY_ID:
        return
    try:
        await event.answer()
    except Exception:
        pass
    action, arg = parse_menu_data(event.data)
    logger.info(f"🤖 bot 菜单回调：{action} {arg or ''}")
    try:
        text, buttons = await handle_menu_action(action, arg, event)
        if text is not None:
            await event.edit(
                text, buttons=buttons, link_preview=False
            )
    except Exception as e:
        logger.exception(f"bot 菜单处理失败：{e}")
        try:
            await event.edit("❌ 操作失败，请查看日志", buttons=back_home_buttons())
        except Exception:
            pass


async def cleanup_bot_chat_once():
    """清理 bot 菜单对话：删除超时消息，保留最新一条带按钮的菜单。

    注意：必须用 userbot 账号（client）读写这个对话，peer 是 bot 的
    用户 id（BOT_ID）——bot 账号调用 messages.getHistory 会被服务端
    拒绝（bot API 限制），且从 userbot 视角 bot 对话的 peer 不是 MY_ID。
    """
    if not MY_ID or not bot_client or not BOT_ID:
        return
    try:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        infos = []
        async for m in client.iter_messages(BOT_ID, limit=300):
            if m.date:
                if m.date.tzinfo is None:
                    m.date = m.date.replace(tzinfo=timezone.utc)
                age = (now - m.date).total_seconds() / 60.0
            else:
                age = 0.0
            infos.append(
                {"id": m.id, "age_minutes": age, "has_buttons": bool(m.buttons)}
            )
        del_ids, _ = plan_bot_chat_cleanup(infos, CLEAN_MESSAGE_AGE_MINUTES)
        if del_ids:
            await client.delete_messages(BOT_ID, del_ids)
            logger.info(
                f"🧹 自动清理 bot 菜单对话：删除 {len(del_ids)} 条消息"
            )
        else:
            logger.info("⏱ bot 菜单对话清理检查完成：没有需要删除的消息")
    except Exception as e:
        logger.exception(f"自动清理 bot 菜单对话失败：{e}")


# ============================================================
# 核心监听器
# ============================================================
# 注意：事件处理器在 main() 里注册（client.add_event_handler），
# 因为 client 在 main() 里才被创建。
async def new_message_handler(event):
    global MY_ID

    try:
        message = event.message

        # 只处理 Saved Messages（"me"）与白名单 chat
        chat_kind, source_override = classify_message_chat(
            event.chat_id, MY_ID, WHITELIST_CHATS
        )
        if chat_kind is None:
            return
        is_me = chat_kind == "me"

        media_type = get_media_type(message)
        file_name = None

        try:
            file_name = message.file.name if message.file else None
        except Exception:
            pass

        chat_label = (
            "Saved Messages" if is_me else f"白名单 chat（{source_override}）"
        )
        logger.info(
            f"📨 {chat_label} 收到消息 | "
            f"ID={message.id} | "
            f"media={media_type} | "
            f"file={file_name or 'None'} | "
            f"has_document={'yes' if message.document else 'no'} | "
            f"has_photo={'yes' if message.photo else 'no'}"
        )

        text = (message.message or "").strip()

        # 处理命令（只在 Saved Messages 生效）
        if is_me and text.startswith("/"):
            handled = await handle_command(event, text)
            if handled:
                return

        # ========================================================
        # 抖音 / Instagram 链接：即使消息本身没有媒体，也要检查文字 URL
        # 链接解析只在 Saved Messages 生效（白名单 chat 仅下载媒体）。
        # ========================================================
        if is_me:
            douyin_urls = extract_douyin_urls(text)
            instagram_urls = extract_instagram_urls(text)

            # Telegram 可能把链接显示成 WebPage 预览（MessageMediaWebPage）。
            # 即使 message.file=None，也必须按文字中的 URL 继续处理。
            if not douyin_urls or not instagram_urls:
                try:
                    for entity in getattr(message, "entities", None) or []:
                        url = getattr(entity, "url", None)
                        if not url:
                            continue
                        if not douyin_urls:
                            douyin_urls.extend(extract_douyin_urls(url))
                        if not instagram_urls:
                            instagram_urls.extend(extract_instagram_urls(url))
                except Exception as e:
                    logger.warning(f"读取 Telegram URL entity 失败：{e}")

            # 去重
            douyin_urls = list(dict.fromkeys(douyin_urls))
            instagram_urls = list(dict.fromkeys(instagram_urls))

            if douyin_urls or instagram_urls:
                logger.info(
                    f"🔎 检测到 {len(douyin_urls)} 个抖音链接、"
                    f"{len(instagram_urls)} 个 Instagram 链接"
                )
                asyncio.create_task(
                    handle_media_links(message, douyin_urls, instagram_urls)
                )
                return

        # 判断是否是可下载媒体
        if not is_downloadable(message):
            logger.info(
                f"消息 ID={message.id} 没有检测到可下载媒体，忽略"
            )
            return

        logger.info(
            f"📦 检测到可下载媒体 | 类型={media_type} | "
            f"文件={file_name or '无文件名'}"
        )

        # 入队持久化下载（重启不丢任务），不阻塞消息监听
        label = file_name or (text[:50] if text else f"消息 {message.id}")
        asyncio.create_task(enqueue_and_start({
            "kind": "media",
            "chat_id": event.chat_id,
            "msg_id": message.id,
            "source_override": source_override,
            "label": label,
            # 入队时算好最终文件名，列表展示与实际下载命名保持一致
            "final_name": compute_final_filename(message),
            # 转发消息链到原频道消息；否则用消息自身 chat 生成
            "source_link": message_source_link(message, event.chat_id),
        }))

    except Exception as e:
        logger.exception(f"❌ 消息处理异常：{e}")

# ============================================================
# 主程序
# ============================================================


async def start_with_retry(cli, bot_token=None):
    """
    带超时看门狗地执行 cli.start()。

    Telethon 的连接/收包路径没有读超时，代理节点卡住时 client.start()
    会无限期挂起。这里用 wait_for 加外部超时（混淆传输全程异步、
    可以被取消），超时后断开重试，直到成功或重试次数用尽。
    bot_token 非空时按 bot 账号登录（按钮菜单客户端）。
    """
    last_error = None
    for attempt in range(1, LOGIN_RETRIES + 1):
        try:
            if bot_token:
                await asyncio.wait_for(
                    cli.start(bot_token=bot_token),
                    timeout=LOGIN_TIMEOUT_SECONDS,
                )
            else:
                await asyncio.wait_for(
                    cli.start(), timeout=LOGIN_TIMEOUT_SECONDS
                )
            return
        except Exception as e:
            last_error = e
            logger.error(
                f"❌ 连接/登录失败，尝试 {attempt}/{LOGIN_RETRIES}："
                f"{type(e).__name__}: {e}"
            )
            if attempt < LOGIN_RETRIES:
                try:
                    await cli.disconnect()
                except Exception:
                    pass
                logger.info("🔄 5 秒后重试连接...")
                await asyncio.sleep(5)

    raise last_error


async def main():
    global MY_ID, client, bot_client, QUEUE, QUEUE_LOCK, DOWNLOAD_SEMAPHORE, DOUYIN_LOCK, DOWNLOAD_CONCURRENCY, WHITELIST_CHATS

    # 敏感配置检查：api_id / api_hash 缺失时给出明确提示（否则 create_client
    # 会用空凭据报出晦涩错误），bot_token 缺失仅禁用按钮菜单。
    if not API_ID or not API_HASH:
        logger.error(
            "❌ 未配置 Telegram API 凭据：请参照 tg_secrets.example.json "
            "在 %s 中填写 api_id / api_hash 后重新运行。",
            SECRETS_FILE,
        )
        return
    if not BOT_TOKEN:
        logger.warning(
            "ℹ️ 未配置 bot_token：按钮菜单不可用，下载等其余功能正常。"
            "如需菜单请在 %s 中补充 bot_token / bot_username。",
            SECRETS_FILE,
        )

    # 必须在事件循环内创建（见上方“Telegram Client”一节说明）
    client = create_client()
    bot_client = None
    load_thread_config()
    WHITELIST_CHATS = load_whitelist()
    DOWNLOAD_SEMAPHORE = AdjustableSemaphore(DOWNLOAD_CONCURRENCY)
    DOUYIN_LOCK = asyncio.Lock()
    QUEUE_LOCK = asyncio.Lock()
    QUEUE = load_queue()
    client.add_event_handler(new_message_handler, events.NewMessage())

    logger.info("=====================")
    logger.info("🚀 TG Userbot v2.9 正在启动")
    logger.info(f"运行平台：{'Termux/Android' if IS_TERMUX else 'macOS/桌面'}")
    if PROXY:
        logger.info(f"代理：已启用（{PROXY[0]} {PROXY[1]}:{PROXY[2]}）")
    else:
        logger.info("代理：未启用（直连）")
    logger.info(
        "传输方式：{}".format(
            "混淆(Obfuscated)" if CONNECTION_TYPE == "obfuscated" else "TLS(Full)"
        )
    )
    logger.info(f"保存目录：{SAVE_FOLDER}")
    logger.info(f"日志文件：{LOG_FILE}")
    logger.info(
        f"监听范围：Saved Messages + 白名单 {len(WHITELIST_CHATS)} 个 chat"
    )
    for cid, title in sorted(WHITELIST_CHATS.items()):
        logger.info(f"  - 白名单：{title} ({cid})")
    logger.info(
        f"📥 下载队列：{len(QUEUE['tasks'])} 个任务 | "
        f"待重试 {len(QUEUE['retry'])} 个"
    )
    if BOT_TOKEN:
        logger.info(f"🤖 bot 菜单：{BOT_USERNAME}")
    logger.info("自动重试：3 次")
    logger.info(f"并发下载数：{DOWNLOAD_CONCURRENCY}（/thread 可调整）")
    logger.info(f"抖音解析机器人：{DOUYIN_BOT_USERNAME}")
    logger.info(f"抖音保存目录：{get_platform_save_folder('douyin')}")
    logger.info(f"Instagram 解析机器人：{INSTAGRAM_BOT_USERNAME}")
    logger.info(f"Instagram 保存目录：{get_platform_save_folder('instagram')}")
    logger.info("============================================")

    await start_with_retry(client)

    me = await client.get_me()
    MY_ID = me.id

    logger.info(
        f"✅ 登录成功 | 用户：{me.first_name or ''} "
        f"{me.last_name or ''} | ID={MY_ID}"
    )

    # 恢复持久化队列：重启前没跑完的任务自动重新执行
    if QUEUE["tasks"]:
        logger.info(f"📥 恢复下载队列：{len(QUEUE['tasks'])} 个任务")
        recover_queue_tasks()
    if QUEUE["retry"]:
        logger.info(f"🔁 待重试列表：{len(QUEUE['retry'])} 个任务（手动重试）")

    # bot 按钮菜单：登录失败只影响菜单，不影响主功能
    global BOT_ID
    if BOT_TOKEN:
        try:
            bot_client = create_bot_client()
            await start_with_retry(bot_client, bot_token=BOT_TOKEN)
            bot_me = await bot_client.get_me()
            BOT_ID = bot_me.id
            bot_client.add_event_handler(
                bot_message_handler, events.NewMessage()
            )
            bot_client.add_event_handler(
                bot_callback_handler, events.CallbackQuery()
            )
            logger.info(f"✅ bot 菜单已启用：{BOT_USERNAME}（ID={BOT_ID}）")
        except Exception as e:
            logger.error(f"❌ bot 菜单启动失败（不影响主功能）：{e}")
            bot_client = None
            BOT_ID = None

    if os.access(SAVE_FOLDER, os.W_OK):
        logger.info("✅ 保存目录可访问")
    else:
        if IS_TERMUX:
            logger.error("❌ 保存目录不可写，请检查 Termux 存储权限")
        else:
            logger.error("❌ 保存目录不可写，请检查目录权限")

    logger.info(
        "🟢 TG Userbot v2.9 已启动，等待 Saved Messages / 白名单 chat 的媒体与抖音链接"
    )
    logger.info("💡 测试：在 Saved Messages 发送 /status")
    logger.info("💡 下载：把文件转发到 Saved Messages")
    logger.info("💡 抖音：把抖音链接发送到 Saved Messages")
    logger.info("💡 Instagram：把 Instagram 链接发送到 Saved Messages")
    logger.info("💡 诊断：发送消息后，日志必须出现‘📨 Saved Messages 收到消息’")

    # 启动自动清理任务
    global CLEAR_TIME_CHANGED
    load_clear_interval()
    CLEAR_TIME_CHANGED = asyncio.Event()

    cleanup_task = None
    if AUTO_CLEAN_SAVED_MESSAGES:
        cleanup_task = asyncio.create_task(cleanup_loop())
        # 启动时先清理一次已经过期的程序消息（Saved Messages 与 bot 对话）
        await cleanup_saved_messages_once()
        await cleanup_bot_chat_once()

    try:
        await client.run_until_disconnected()
    finally:
        if cleanup_task:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 TG Userbot 已停止")
    except Exception as e:
        logger.exception(f"❌ 程序退出：{e}")
