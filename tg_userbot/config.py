"""配置与平台探测（不可变常量 + import 期一次性启动）。

拆包前这些都在单文件 tg_userbot_final.py 顶部。这里保持同样的求值顺序：
secrets → 平台探测 → 保存目录/日志 → 代理/传输 → 其余常量 → AdjustableSemaphore，
模块末尾才做 import 期的文件系统副作用：mkdir(SAVE_FOLDER) 与
mkdir(RUNTIME_DIR)、把旧版散在根目录的运行时文件迁入 runtime/、然后
log.configure(LOG_FILE, LOG_RETENTION_DAYS)（download.log 按天轮转、
只保留最近 7 天）。

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

# 运行时文件（非媒体的配置/日志/队列）统一归集到 SAVE_FOLDER/runtime/ 子目录：
# download.log（按天轮转）、download_history.txt、4 个运行时 JSON、cd2_launch.log。
# 历史版本散在 SAVE_FOLDER 根下的同名文件在启动时自动迁入（_migrate_runtime_files）。
# 媒体仍存 SAVE_FOLDER/<来源>/（递归范围被 CD2 白名单搬到 115）；运行时文件因为
# 不是媒体扩展名、天然不被 CD2 备份/删除规则碰（与归集前等价），归集只为了收拢
# 目录、避免根目录越来越杂。该目录与媒体都在同一 SAVE_FOLDER 里，无跨盘问题。
RUNTIME_DIR = os.path.join(SAVE_FOLDER, "runtime")
LOG_RETENTION_DAYS = 7  # download.log 按天轮转，只保留最近 7 天
LOG_FILE = os.path.join(RUNTIME_DIR, "download.log")
CD2_LAUNCH_LOG = os.path.join(RUNTIME_DIR, "cd2_launch.log")


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
# 默认规则：一律 full（2026-09-05 起，此前是「设了代理就用 obfuscated」）。
# 改默认的原因（实测）：obfuscated 传输对每一条连接的每一帧都做 AES-CTR 加密，
# 而 telethon 1.44 的 AESModeCTR（crypto/aesctr.py，注释还留着 TODO Use libssl）
# 只走纯 Python pyaes、单事件循环线程 → 单核 ~274KB/s，是聚合吞吐的真天花板，
# 多 worker 并行连接也绕不开（实测 10 worker 仍 ~213KB/s）。full 无此每帧加密，
# 配合多 worker 才真正并行（实测 10 worker → ~2.3-2.6MB/s）。代价：full 的
# TLS 握手是同步阻塞的，代理节点挂起时可能不可取消，靠 start_with_retry 看门狗
# 与重启兜底。需要抗封锁/混淆（或遇代理不稳定）时用 TG_CONNECTION=obfuscated
# 显式切回，两值对主客户端与下载 worker（_connection_class）同生效。
def pick_connection_type():
    value = os.environ.get("TG_CONNECTION", "").strip().lower()
    if value in ("full", "tls", "tcpfull"):
        return "full"
    if value in ("obfuscated", "obf", "tcpobfuscated"):
        return "obfuscated"
    return "full"


CONNECTION_TYPE = pick_connection_type()

# ------------------------------------------------------------
# 下载与历史
# ------------------------------------------------------------
# 下载失败自动重试次数
DOWNLOAD_RETRIES = 3

# 跨 DC 首次授权导出「竞态」的专用加量重试：多 worker 池里每条连接都是独立
# 客户端、各自首次跨 DC 下载都要做一次 auth.exportAuthorization（导出成功结果
# 按 DC 缓存在该 worker 上、此后不再导出）。同一账号的 N 条新连接同时对同一 DC
# 做首次导出时，Telegram 只让极少数成功（AuthBytesInvalidError，谁先落地谁赢；
# 实测 6 路并发就有 5/6 首轮失败）。该错误秒级失败、发生在任何字节写出之前，
# 不像断流要等 DOWNLOAD_IDLE_TIMEOUT——重试代价极低。且竞争会随「导出成功者
# 退出」自然消散（成功 worker 带热缓存回池，后续文件借到它直接免导出），所以只
# 对这一类快速失败放宽上限即可收敛，普通网络/其他 RPC 失败仍按 DOWNLOAD_RETRIES
# 兜底，绝不无限重试。
EXPORT_RACE_EXTRA_RETRIES = 6

# 进度日志间隔（百分比）
PROGRESS_STEP = 5

# 下载「无进度」看门狗（秒）：Telethon 请求没有读超时，连接僵死时既不报错也不
# 出数据，会永远占住信号量槽位（另一个静默卡死的来源）。单次下载尝试超过该秒数
# 没有任何进度回调即判定僵死：取消本次、抛 TimeoutError，走重试分支重连重下。
# 实际数据分块回调远密于该阈值（单 worker 满速 ~0.3MB/s，1MB 分块约 3-4 秒一次
# 进度），只在代理节点彻底断流时才触发；误判顶多浪费一次尝试、重下即可。
DOWNLOAD_IDLE_TIMEOUT = 120

# 抖音直链签名寿命的「提前刷新」边际（秒）：douyinvod 直链内嵌过期 unix 戳，
# 实测 = 解析时刻 +3 小时。本地解析链入队的 url 任务在深队列里排队太久会拿
# 着过期直链开下（403 白烧重试、任务救不回）。download_url_media 开下前解码
# 该戳，剩余寿命低于此边际就先用原始分享链接重新解析拿新直链。
DIRECT_URL_REFRESH_MARGIN_SECONDS = 15 * 60

# 文件名按 UTF-8 字节上限截断。macOS(APFS/HFS+) 与 Android(ext4) 的单文件名
# 上限都是 255 字节；这里留出 ".download" 临时后缀与重名 " (n)" 的余量。
# 只影响超限的罕见超长标题/说明，正常文件名原样保留。
MAX_FILENAME_BYTES = 200

# 手工转发「评论 + 紧跟媒体」的前置标注关联窗口（秒）：在收藏夹收到一条用户
# 纯文本评论后，窗口内到达的媒体都继承该文本为命名标注（代码加 # 前缀拼到
# 文件名最前，见 naming 的 label 参数）。窗口不消费、不因取用而清空——一条
# 评论后连续转发 N 条媒体都拼上同一条标注，靠时间自然过期。
ME_LABEL_WINDOW_SECONDS = 5

# 重复媒体去重（2026-09-07）：判重索引 append-only 追加（runtime/dedup_index.txt，
# 一行一条「键\t日期\t文件名」，与 download_history.txt 同款单行原子纪律，
# 永不全量重写），启动时载入内存并只在启动裁剪到 DEDUP_MAX_ENTRIES 条
# （保尾部，超限原子重写一次）。键：tg:<file_unique_id> / dyc:<aweme_id>。
DEDUP_INDEX_FILE = os.path.join(RUNTIME_DIR, "dedup_index.txt")
DEDUP_CONFIG_FILE = os.path.join(RUNTIME_DIR, "dedup_config.json")
DEDUP_MAX_ENTRIES = 10000

# 任务生命周期事件日志（JSONL，append-only 单行追加，写失败仅告警）：
# 台账按 task_id 重建统计的数据源。每行一个事件
# {"ts","ev","id","label",...}，ev ∈ RECEIVED/QUEUED/RUNNING/RETRY/FAILED/
# SUCCESS/CANCELLED/REMOVED/DEDUP_SKIPPED/DEDUP_HIT。只在启动裁剪到
# TASK_EVENTS_MAX_EVENTS 条（保尾部，超限原子重写一次）。
TASK_EVENTS_FILE = os.path.join(RUNTIME_DIR, "task_events.jsonl")
TASK_EVENTS_MAX_EVENTS = 30000

# 「文本在后」宽限（秒）：实测转发+评论时评论的事件可能落在媒体之后（事件循环
# 调度顺序不定），媒体到达时若还没有待关联标注，先等这么多秒再取一次，给尾部
# 评论一个落地机会。已有待关联标注时不等待、立即继承。0 = 关闭宽限。
ME_LABEL_GRACE_SECONDS = 2

# 下载历史记录文件（RUNTIME_DIR 下），每行一条已完成下载：
# 时间 | 类型(普通/抖音) | 文件名 | 大小 | 来源
DOWNLOAD_HISTORY_FILE = os.path.join(RUNTIME_DIR, "download_history.txt")
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

# ------------------------------------------------------------
# 本地解析链（桌面端）：f2 库优先 + 解析 bot 兜底
# ------------------------------------------------------------
# 抖音链接默认先尝试 f2 本地解析（不依赖第三方解析 bot）；任何失败
# （f2 未安装 / import 失败 / 签名过期 / 网络断 / 超时）都静默降级回
# 原有 bot 中转路径，最坏情况 = 维持现状。Termux 上强制走 bot 路径
# （f2 依赖较重，且手机端维持原行为）；TG_RESOLVER=off 可在桌面端
# 强制关闭本地解析。
RESOLVER_ENABLED = (
    not IS_TERMUX
    and os.environ.get("TG_RESOLVER", "").strip().lower() != "off"
)

# 本地解析整体超时：分享链接展开 + 作品详情接口请求共用一个预算。
RESOLVER_TIMEOUT_SECONDS = 30

# 抖音 Web cookie：f2 调作品详情接口必需（游客 cookie 大概率也能用，
# 但登录 cookie 更稳）。从 tg_secrets.json 的 douyin_cookie 字段读取，
# 缺省空串 → f2 请求大概率失败 → 自动降级 bot，不硬性要求配置。
DOUYIN_COOKIE = _SECRET_CONFIG.get("douyin_cookie", "")

# 抖音 Web 端通用请求头（f2 详情接口 + CDN 直链下载共用）。CDN 直链对
# UA/Referer 敏感：裸请求（无 UA）会被 douyinvod 拒绝 403，下载必须带。
DOUYIN_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
DOUYIN_HEADERS = {
    "User-Agent": DOUYIN_UA,
    "Referer": "https://www.douyin.com/",
}

# bot 菜单更新 cookie 的等待输入窗口（秒）：超时后 bot 对话里的普通文本
# 不再当作 cookie 内容，需重新点【✏️ 更新】
COOKIE_INPUT_WINDOW_SECONDS = 120
# 【🔍 查询】按钮的等待输入窗口（按下后发关键字，同 cookie 模式）
FIND_INPUT_WINDOW_SECONDS = 120

# ============================================================
# 下载并发
# ============================================================
# 同时进行的下载数量 = 并行下载连接数（worker）：每条下载各占一条独立
# Telethon 连接（须 full 传输才真正并行；obfuscated 有纯 Python AES-CTR
# 单核天花板，见 pick_connection_type）。实测（2026-09-05）：单条 ~0.3MB/s，
# n 路聚合先随 n 涨、后到墙 —— 本机经当时 Hiddify 节点到 TG DC 聚合封顶
# ~2.3–2.6MB/s（~21Mbps），10 路与 24 路同速；墙在 ~8–10 路附近，位置取决于
# 线路/代理节点/TG 路径。上限保留到 25（2026-09-05 起按用户要求，勿收窄到
# 10）：Hiddify 节点带宽时快时慢，瓶颈在节点总带宽而不在 bot，节点快时把
# /thread 开过 10 能吃到该节点更高的聚合（10 只是当时节点的实测墙）；何时用
# 10 还是 25 由用户在运行时自行决定，慢节点开多只摊薄单文件速度、不加聚合。
# 普通下载与抖音/IG 视频共享并发池。可通过 /thread 指令运行时调整（1-25），
# 并持久化到 thread_config.json。
# 默认值（当前值存 state.DOWNLOAD_CONCURRENCY，运行时可改）。
DOWNLOAD_CONCURRENCY = 3
DOWNLOAD_CONCURRENCY_MIN = 1
DOWNLOAD_CONCURRENCY_MAX = 25
THREAD_CONFIG_FILE = os.path.join(RUNTIME_DIR, "thread_config.json")

# 下载白名单：除 Saved Messages 外，白名单内的 chat 收到媒体消息也会
# 自动下载（保存到 SAVE_FOLDER/<chat标题>/）。通过 /wl 指令运行时管理，
# 持久化到 whitelist_config.json。
WHITELIST_FILE = os.path.join(RUNTIME_DIR, "whitelist_config.json")

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
    "queue", "queue_del", "retry", "retry_run", "retry_del", "retry_all",
    "cd2", "cd2_stop", "bak", "stats",
    "cookie", "cookie_set", "cookie_clear", "cookie_imp",
    "dedup", "dedup_toggle",
    "find",
)


def mask_douyin_cookie(cookie):
    """把 cookie 掩码成可展示片段：前 12 字符 + … + 后 4 字符（纯函数）。"""
    raw = str(cookie or "").strip()
    if not raw:
        return ""
    if len(raw) <= 20:
        return raw[:4] + "…"
    return f"{raw[:12]}…{raw[-4:]}"


def save_douyin_cookie(value, path=None):
    """把抖音 cookie 写入 tg_secrets.json（原子替换）并更新内存值。

    实时生效的关键：resolver._douyin_kwargs 每次解析都动态读
    config.DOUYIN_COOKIE 属性，这里同步更新后立即作用于下一次解析。
    保留文件里其它字段（api_id/bot_token/cd2 等）。返回错误文案，None=成功。
    """
    global DOUYIN_COOKIE
    target = path or SECRETS_FILE
    # 直接重新读盘，拿文件里最新的其它字段
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except FileNotFoundError:
        data = {}
    except Exception:
        return f"tg_secrets.json 解析失败，拒绝覆盖（请手工修复该文件）"

    value = str(value or "").strip()
    data["douyin_cookie"] = value

    tmp_path = target + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, target)
    except Exception as e:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return f"写入失败：{type(e).__name__}: {e}"

    DOUYIN_COOKIE = value
    return None

# ------------------------------------------------------------
# 持久化下载队列：任务先入队（媒体存消息引用、平台链接存完整 URL），
# 重启后自动恢复执行。失败任务移入 retry 列表停靠，由用户手动重试。
# ------------------------------------------------------------
QUEUE_FILE = os.path.join(RUNTIME_DIR, "download_queue.json")

# Telethon 的请求没有读超时：代理节点卡住时 get_messages 等请求会永久挂起，
# 把信号量槽位占满、整条队列堵死。取消息步骤必须加外部超时。
QUEUE_FETCH_TIMEOUT = 30

QUEUE_KIND_LABELS = {
    "media": "媒体",
    "url": "链接",
    "douyin": "抖音",
    "instagram": "Instagram",
}

# ------------------------------------------------------------
# Saved Messages 消息清理
# ------------------------------------------------------------
# 是否自动清理 Saved Messages 中的程序指令、抖音链接和程序通知
AUTO_CLEAN_SAVED_MESSAGES = True

# 消息至少存在多少分钟后才允许删除
CLEAN_MESSAGE_AGE_MINUTES = 1

# 自动清理执行间隔，默认 1 分钟，可通过 /setcleartime 修改
DEFAULT_CLEAR_INTERVAL_SECONDS = 60
CLEAR_TIME_CONFIG_FILE = os.path.join(RUNTIME_DIR, "clear_time.json")

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
    # 本地解析链（url 任务）的通知
    "🛠 本地解析成功",
    "❌ 直链下载失败",
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
    # /stats（台账）的回复
    "📊 台账",
    # /find（媒体下落查询）的命令与回复
    "🔍 查询",
    # /thread 的回复
    "🧵 当前并发下载数",
    "✅ 并发下载数已设置为",
    # 去重命中的跳过通知
    "⏭️ 重复媒体已跳过下载",
    "⏭️ 相同媒体已在下载队列",
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


# ------------------------------------------------------------
# 稳态连接守护（app 的 _main_serve / _bot_keepalive）
# ------------------------------------------------------------
# telethon 内建自动重连：置 False，由 app.main 的稳态守护接管重连。内建重连在
# 「连接成功后立刻再失败」（例如代理持续回 HTTP 429）时进入无界递归风暴
# （get_me 验证失败又触发重连，曾叠上千层把事件循环拖死、进程退出）；关闭后
# 断线会及时让 run_until_disconnected 返回/抛错，交给上层有界重连。
TELEGRAM_AUTO_RECONNECT = False

# 主客户端稳态守护：掉线自动重连，指数退避 5s → … → 120s 上限。
SERVE_RECONNECT_BASE_DELAY = 5
SERVE_RECONNECT_MAX_DELAY = 120

# bot 菜单连接守护的探活间隔（秒）。
BOT_KEEPALIVE_INTERVAL = 15

# 清理周期拉取/删除消息的超时（秒）：连接半死（无读超时）时不能让清理周期
# 无限卡住。拉取/删除都只记警告跳过本轮，清理本就是 best-effort。
CLEANUP_FETCH_TIMEOUT = 90
CLEANUP_DELETE_TIMEOUT = 30


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


def _migrate_runtime_files(save_folder=None, runtime_dir=None):
    """把历史版本散在 SAVE_FOLDER 根下的运行时文件迁入 runtime/（幂等）。

    save_folder/runtime_dir 可显式传入（供单测用临时目录）；默认取模块常量。
    仅当 runtime/ 下尚不存在同名文件时才移动：重复 import / 进程已在跑新版本
    都 no-op。返回本次实际迁入的文件名列表。运行时文件本来就是根目录里「非媒体
    扩展名」的那一撮，不会被 CD2 的备份/删除规则碰（按媒体扩展名白名单过滤），
    搬进子目录后依然免疫，故移动安全。只处理下面 7 个确切 basename，绝不误伤
    其它用户文件。失败不阻塞启动（下轮启动或手动处理）。
    """
    if save_folder is None:
        save_folder = SAVE_FOLDER
    if runtime_dir is None:
        runtime_dir = RUNTIME_DIR
    basenames = (
        "download.log", "download_history.txt", "thread_config.json",
        "whitelist_config.json", "download_queue.json", "clear_time.json",
        "cd2_launch.log",
    )
    moved = []
    for name in basenames:
        src = os.path.join(save_folder, name)
        dst = os.path.join(runtime_dir, name)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                os.replace(src, dst)
                moved.append(name)
            except OSError:
                pass
    return moved


# ============================================================
# import 期一次性副作用（保持单文件时的时机：先建目录、再配日志）
# ============================================================
os.makedirs(SAVE_FOLDER, exist_ok=True)
os.makedirs(RUNTIME_DIR, exist_ok=True)
_migrated_runtime_files = _migrate_runtime_files()
log.configure(LOG_FILE, LOG_RETENTION_DAYS)
if _migrated_runtime_files:
    log.logger.info(
        "已把历史运行时文件迁入 runtime/ 目录："
        + "、".join(_migrated_runtime_files)
    )
