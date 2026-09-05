"""配置与平台探测（不可变常量 + import 期一次性启动）。

拆包前这些都在单文件 tg_userbot_final.py 顶部。这里保持同样的求值顺序：
secrets → 平台探测 → 保存目录/日志 → 代理/传输 → 其余常量 → AdjustableSemaphore，
模块末尾才做 import 期的两个文件系统副作用：mkdir(SAVE_FOLDER) 与
log.configure(LOG_FILE)。

可变的运行态全局不在这里（见 state.py）；本模块导出的都是只读常量，
consumer 模块可用 `from .config import SAVE_FOLDER` 别名（值永不变化）。
唯一例外：cd2_config / cd2_log_dir 需在调用时读 config._SECRET_CONFIG
（模块对象属性），供测试 monkeypatch —— 不能 from-import 成别名。
"""
import os
import json
import re
import asyncio
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

from . import log

# ============================================================
# 敏感配置：api_id / api_hash / bot_token 等真实密钥不在代码里硬编码，
# 而是从本地配置文件 tg_secrets.json 读取。该文件含真实密钥，已被
# .gitignore 排除，不会提交到仓库；缺失时按“未配置”处理，启动阶段提示。
# 字段格式见仓库内模板 tg_secrets.example.json。
#
# 定位修复：单文件时代默认路径是“脚本所在目录”（=仓库根）；代码搬进
# tg_userbot/ 包后 __file__ 会变成 <仓库根>/tg_userbot/config.py，若沿用
# 会把查找点悄然挪到包内。故显式以 REPO_ROOT（包目录的父目录）为准。
# 环境变量 TG_SECRETS_FILE 仍可覆盖。
# ------------------------------------------------------------
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(PACKAGE_DIR)
SECRETS_FILE = os.environ.get(
    "TG_SECRETS_FILE", os.path.join(REPO_ROOT, "tg_secrets.json")
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

# 文件名按 UTF-8 字节上限截断。macOS(APFS/HFS+) 与 Android(ext4) 的单文件名
# 上限都是 255 字节；这里留出 ".download" 临时后缀与重名 " (n)" 的余量。
# 只影响超限的罕见超长标题/说明，正常文件名原样保留。
MAX_FILENAME_BYTES = 200

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
# 抖音 / Instagram 链接解析
# ============================================================
# 在 Saved Messages（收藏夹）发送抖音 / Instagram 链接后：Userbot 把链接原文
# 转发给解析机器人（两个平台共用 @DouYintg_bot），bot 回复的首条直发视频因
# 解析 bot 本身在下载白名单上，会被自动转发进收藏夹 → 走统一媒体下载（不再
# 由平台流自下、也不再写入 Douyin/Instagram 子目录）。链接类型只用于选择
# 转发目标与日志标签；命名与落盘目录不再区分平台。
DOUYIN_BOT_USERNAME = "@DouYintg_bot"
INSTAGRAM_BOT_USERNAME = "@DouYintg_bot"

# kind → (解析 bot 用户名, 日志标签)：relay 用（douyin/instagram 同 bot）
PLATFORM_LINKS = {
    "douyin": {"bot": DOUYIN_BOT_USERNAME, "label": "抖音"},
    "instagram": {"bot": INSTAGRAM_BOT_USERNAME, "label": "Instagram"},
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
# 默认值（当前值存 state.DOWNLOAD_CONCURRENCY，运行时可改）。
DOWNLOAD_CONCURRENCY = 3
DOWNLOAD_CONCURRENCY_MIN = 1
DOWNLOAD_CONCURRENCY_MAX = 10
THREAD_CONFIG_FILE = os.path.join(SAVE_FOLDER, "thread_config.json")

# 下载白名单：除 Saved Messages 外，白名单内的 chat 收到媒体消息也会
# 自动下载（保存到 SAVE_FOLDER/<chat标题>/）。通过 /wl 指令运行时管理，
# 持久化到 whitelist_config.json。
WHITELIST_FILE = os.path.join(SAVE_FOLDER, "whitelist_config.json")

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
    "cd2", "cd2_stop", "bak",
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


class AdjustableSemaphore:
    """并发上限可动态调整的信号量（asyncio.Semaphore 创建后不可改值）。

    纯类型：import 时不构造实例、不碰事件循环；acquire() 里
    asyncio.get_running_loop() 只在运行期调用，满足 py3.9 事件循环规则。
    实例（DOWNLOAD_SEMAPHORE）只在 app.main() 里创建。
    """

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


# ============================================================
# import 期一次性副作用（保持单文件时的时机：先建目录、再配日志）
# ============================================================
os.makedirs(SAVE_FOLDER, exist_ok=True)
log.configure(LOG_FILE)
