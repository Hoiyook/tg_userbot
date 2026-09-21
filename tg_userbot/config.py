"""配置与平台探测（不可变常量 + import 期一次性启动）。

拆包前这些都在单文件 tg_userbot_final.py 顶部。这里保持同样的求值顺序：
secrets → 平台探测 → 数据根/下载根/Runtime 根 → 代理/传输 → 其余常量 →
AdjustableSemaphore，模块末尾才做 import 期的文件系统副作用：mkdir(DOWNLOAD_DIR)
与 mkdir(RUNTIME_DIR)、把旧版散在下载根的运行时文件迁入 runtime/、把旧默认
数据根（~/Downloads/Nagram）整体迁入新根（仅零配置桌面部署，见
_legacy_migration_root）、然后 log.configure(LOG_FILE, LOG_RETENTION_DAYS)
（download.log 按天轮转、只保留最近 7 天）。

可变的运行态全局不在这里（见 state.py）；本模块导出的都是只读常量，
consumer 模块可用 `from .config import SAVE_FOLDER` 别名（值永不变化）。
唯一例外：cd2_config / cd2_log_dir 需在调用时读 config._SECRET_CONFIG
（模块对象属性），供测试 monkeypatch —— 不能 from-import 成别名。
"""
import os
import json
import re
import shutil
import asyncio
from collections import deque, namedtuple
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

# ------------------------------------------------------------
# 数据根目录与路径解耦（2026-09-13）：
#   DATA_ROOT    正式数据根（桌面默认 /Volumes/V1；Termux 沿用旧根，行为不变）
#   DOWNLOAD_DIR 媒体下载根（新代码用这个；<根>/<来源>/ 的布局不变）
#   RUNTIME_DIR  运行数据根（db/JSON/日志/PID；旧版是 SAVE_FOLDER/runtime，强耦合）
# SAVE_FOLDER 保留为 DOWNLOAD_DIR 的旧别名：既有媒体代码与展示文案不改也对，
# 新代码一律用 DOWNLOAD_DIR。优先级（高→低）：
#   DOWNLOAD_DIR: TG_DOWNLOAD_DIR → TG_SAVE_FOLDER(旧变量) → DATA_ROOT/downloads
#   RUNTIME_DIR : TG_RUNTIME_DIR → 仅设旧 TG_SAVE_FOLDER 时与其同目录 → DATA_ROOT/runtime
# 解析逻辑独立成纯函数 _resolve_paths：import 期的 mkdir/迁移副作用只在真实
# 环境跑一次；单测直接喂 env dict，不 reload 模块、不触碰真实文件系统。
# ------------------------------------------------------------
ResolvedPaths = namedtuple(
    "ResolvedPaths",
    "data_root download_dir runtime_dir runtime_db_file")


def _resolve_paths(env, is_termux):
    """由环境变量与平台分支解析四个路径常量（纯函数，可单测）。

    Termux 不引入新布局：数据根沿用旧默认（/storage/.../Nagram）且
    DOWNLOAD_DIR == DATA_ROOT——手机端行为与解耦前逐字节一致，桌面才把
    媒体收进 DATA_ROOT/downloads。只设旧 TG_SAVE_FOLDER（测试基座与老部署
    的形态）时 runtime 仍与其同目录：不能让只动了旧变量的部署得到一个
    突兀的 DATA_ROOT/runtime；显式设了 TG_DATA_ROOT/TG_RUNTIME_DIR 即视为
    新式配置，旧变量的耦合不再生效。
    """
    env = env or {}
    if is_termux:
        data_root = env.get("TG_DATA_ROOT",
                            "/storage/emulated/0/Download/Nagram")
    else:
        data_root = env.get("TG_DATA_ROOT", "/Volumes/V1")
    legacy_save = env.get("TG_SAVE_FOLDER")
    default_download = data_root if is_termux else os.path.join(
        data_root, "downloads")
    download_dir = env.get("TG_DOWNLOAD_DIR", legacy_save or default_download)
    if env.get("TG_RUNTIME_DIR"):
        runtime_dir = env["TG_RUNTIME_DIR"]
    elif legacy_save and not env.get("TG_DATA_ROOT"):
        runtime_dir = os.path.join(legacy_save, "runtime")
    else:
        runtime_dir = os.path.join(data_root, "runtime")
    runtime_db = env.get("TG_RUNTIME_DB",
                         os.path.join(runtime_dir, "tg_userbot.db"))
    return ResolvedPaths(data_root, download_dir, runtime_dir, runtime_db)


DATA_ROOT, DOWNLOAD_DIR, RUNTIME_DIR, RUNTIME_DB_FILE = _resolve_paths(
    os.environ, IS_TERMUX)
# legacy compatibility：下载根旧别名，值永不变化。
SAVE_FOLDER = DOWNLOAD_DIR

# 运行时文件（非媒体的配置/日志/队列）统一归集到 RUNTIME_DIR：
# download.log（按天轮转）、download_history.txt、各运行时 JSON、cd2_launch.log。
# 历史版本散在下载根下的同名文件在启动时自动迁入（_migrate_runtime_files）。
# 运行时文件不是媒体扩展名、天然不被 CD2 备份/删除规则碰；独立成根后与
# 媒体目录彻底解耦（旧版 SAVE_FOLDER/runtime 的强耦合到此为止）。
LOG_RETENTION_DAYS = 7  # download.log 按天轮转，只保留最近 7 天
LOG_FILE = os.path.join(RUNTIME_DIR, "download.log")
# Chrome Agent 是独立进程，必须写自己的日志文件：两个进程各持一个
# TimedRotatingFileHandler 写同一文件时，午夜各自轮转，POSIX rename 静默替换
# → 后轮转者覆盖先轮转者刚归档的内容，而先轮转者的句柄仍绑在已被改名的 inode
# 上、此后持续写进归档名文件。2026-09-10 实测：主进程 9/9 全天日志被覆盖丢失，
# 其日志此后全灌进 download.log.2026-09-09（issues/001）。Agent 日志语义本就
# 独立（自己的任务状态机 trace），分开正是所需的隔离。
CHROME_AGENT_LOG_FILE = os.path.join(RUNTIME_DIR, "chrome_agent.log")
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

# ------------------------------------------------------------
# retry 榜到期自动重放（网络恢复韧性，2026-09-10；issues/002）
# ------------------------------------------------------------
# 背景：链路分钟级抖动时，在跑任务 3 次尝试必然全撞上，快速烧完预算入榜后
# 「永久停靠」等人肉 /retry all。以下机制让自动路径按指数退避逐次重放：
# 失败入榜时记 next_retry_at，后台每 AUTO_RETRY_SWEEP_SECONDS 扫一次，
# 到期且未在执行的重放。手动 /retry、/retry all 不受这些约束（立即执行）。
AUTO_RETRY_SWEEP_SECONDS = 60      # 后台扫描间隔（秒）
AUTO_RETRY_BASE_DELAY = 60         # 首次退避（秒）
AUTO_RETRY_MAX_DELAY = 1800        # 退避封顶（30 分钟）——坏窗口内不至于空转
# 自动重试次数上限已移除（2026-09-20 用户决策）：死文件由
# AUTO_REPLAY_FUSE_SECONDS 时间熔断兜底（每 6h 至多一次自动重放），
# 流量代价可控，无需按次数封顶。
# 死文件熔断（2026-09-18）：任务「快速失败」（起步即败、无任何字节进账，
# 如 Request unsuccessful / 文件对象损坏）后在此窗口内不再被 AUTO_REPLAY
# 自动重放——死文件重试多少次都一样，只会烧流量与日志。冷却过后恢复自动
# 重放（也许上游恢复了）；/retry <序号> 单条强救不受熔断影响。
AUTO_REPLAY_FUSE_SECONDS = 6 * 3600

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
# DB 写失败时的判重键暂存（P1-1，2026-09-15）：remember 失败先落这里，
# 启动 load_index 时补写进 DB（防「文件已下载、键没落库」的重启重复下载窗口）
DEDUP_PENDING_FILE = os.path.join(RUNTIME_DIR, "dedup_pending.jsonl")
# 命令模板（cmd_templates.py，/cmdt）：名字 → shell 命令原文
CMD_TEMPLATES_FILE = os.path.join(RUNTIME_DIR, "command_templates.json")
DEDUP_CONFIG_FILE = os.path.join(RUNTIME_DIR, "dedup_config.json")
DEDUP_MAX_ENTRIES = 10000

# ------------------------------------------------------------
# Caption 命名清洗（2026-09-10）：把转发说明里的字段标签、URL 等噪音从
# **文件名**里去掉。规则是普通字符串列表，4 种前缀：
#   exact:<串>     删除这个确切子串
#   contains:<串>  删除所有出现的该子串（有意宽匹配）
#   regex:<正则>   按正则删除（非法正则跳过，不影响其它规则）
#   field:<字段名> 剥掉「字段名+冒号」，保留字段值；字段名后紧跟 【（[ 也算
#                  字段起点（i站地址【 https://… 】 这类没有冒号的写法）
# 默认值 = DEFAULT_CAPTION_FILTER_RULES；当前值存 state.CAPTION_FILTER_RULES，
# 持久化在 CAPTION_FILTER_CONFIG_FILE，由 bot 命令/菜单增删改（实时生效）。
# 清洗只作用于 Telegram caption 那条命名链（compute_final_filename），
# 本地解析链的抖音 url 任务标题不走清洗。
DEFAULT_CAPTION_FILTER_RULES = [
    "field:作者",
    "field:期数",
    "field:角色",
    "field:i站地址",
    "field:标签",
    r"regex:https?://\S+",
    r"regex:【.*?】",
]
CAPTION_FILTER_CONFIG_FILE = os.path.join(RUNTIME_DIR, "caption_filter.json")

# ------------------------------------------------------------
# 标签监听（2026-09-11，V1）：按周期主动扫描指定聊天，命中配置的标签后
# 转发到目标（收藏夹/频道）并按需触发下载。
#
# **与下载白名单完全独立**：监听来源存在自己的 listen.json + 独立的
# state.LISTEN_RULES，绝不从 state.WHITELIST_CHATS 推导，也绝不要求监听
# 聊天必须加入 /wl。一个聊天可以只在下载白名单、只在标签监听、两边都在、
# 或两边都不在。唯一一次「读」白名单是 §14 的重叠判定——源聊天已在下载
# 白名单时，实时链路已经转发+下载过，监听就跳过 me 目标与下载，只跑其余
# 目标（只读，不改其语义）。
#
# download=true 的实现 = 转发到收藏夹 + 入队那份转发副本，复用现有
# enqueue_media → 下载队列 → dedup → 命名的整条链路，不新增第二套下载器。
# ------------------------------------------------------------
LISTEN_CONFIG_FILE = os.path.join(RUNTIME_DIR, "listen.json")
SQL_TEMPLATES_FILE = os.path.join(RUNTIME_DIR, "sql_templates.json")
LISTEN_STATE_FILE = os.path.join(RUNTIME_DIR, "listen_state.json")
LISTEN_DEFAULT_INTERVAL_MINUTES = 1440     # 默认每天扫一次
LISTEN_MIN_INTERVAL_MINUTES = 1
LISTEN_MAX_INTERVAL_MINUTES = 10080        # 上限 7 天
# 单轮单聊天最多处理多少条新消息：防止长时间停机后一次性爆发（剩余的下轮再取；
# checkpoint 只推进到本轮真正处理完的那条）。
LISTEN_MAX_MESSAGES_PER_SCAN = 200
# 补相册边界时向「更新一侧」多探几条：limit 是从最新一侧切的，正好骑在边界上
# 的相册会有较新成员被切在外面，导致整组转发退化成半个相册。Telegram 相册
# 最多 10 个媒体且 id 连续，探 10 条足够覆盖。
LISTEN_ALBUM_SIBLING_RANGE = 10
# 每个聊天最多挂多少条「未处理完」的待续做记录（目标频道长期不可达时兜底，
# 超出丢最旧并告警——绝不无限增长）。
LISTEN_MAX_PENDING = 200
# 单轮单聊天最多续做几个待办单元：一条 get_messages 的 ids 数组不宜过大
#（200 个单元 × 最多 10 个相册成员 = 2000 个 id）。没轮到的下轮继续，不会丢。
LISTEN_MAX_RETRY_UNITS = 20
# 读消息 / 转发的网络超时（秒）：telethon 请求没有读超时，僵死连接会挂住循环。
LISTEN_FETCH_TIMEOUT_SECONDS = 60
LISTEN_FORWARD_TIMEOUT_SECONDS = 120
# bot 菜单「添加/修改监听」的等待输入窗口（秒），与 cookie/查询/Caption 同款。
LISTEN_INPUT_WINDOW_SECONDS = 120
# 启动后首次扫描的延迟（秒）：等登录、worker 池、队列恢复都就位再开扫。
LISTEN_STARTUP_DELAY_SECONDS = 20
# 进自动清理白名单的回复/通知前缀（/listen 命令回复与扫描通知都以此开头）。
# 评论继承原帖命名信息（2026-09-11）：讨论组评论顺着 reply_to 上溯到「频道帖
# 在群里的镜像副本」，每次上溯最多查几条消息。评论可以回复评论，但同一帖子
# 的评论区深度有限；限死是为了「父消息恒取不回 / 成环」时不会无限查下去。
ORIGIN_MAX_HOPS = 5
# 取父消息的网络超时（秒）：telethon 请求没有读超时，僵死连接会挂住入队路径。
ORIGIN_FETCH_TIMEOUT_SECONDS = 30
# ---- 评论继承的准确性保障（2026-09-15，突发转发限流事故）----
# 实测：一次转发 ~40 个文件时，并发回源请求触发 Telegram 限流，16 个取消息
# 超过 30s 保护超时 → 静默降级落错目录（祂录（3D区）群组/）。三层防线：
# 1) 取消息全局限速（并发闸 + 最小间隔），从源头避免限流；
# 2) 解析失败退避重试（数据准确性优先——宁可迟到下载也不落错目录）；
# 3) 重试耗尽才降级，且**必须**写失败账本（origin_failures.jsonl）+ 通知，
#    /origin 命令可查、可溯源。
ORIGIN_FETCH_CONCURRENCY = 3          # 回源取消息的全局并发闸
ORIGIN_FETCH_PACE_SECONDS = 0.4       # 相邻两次取消息的最小间隔（秒）
ORIGIN_RETRY_ATTEMPTS = 3             # 整条上溯链的最大尝试次数
ORIGIN_RETRY_DELAY_SECONDS = 30       # 相邻两次尝试的退避（秒）
ORIGIN_FAILURES_FILE = os.path.join(RUNTIME_DIR, "origin_failures.jsonl")
# 执行进度主动汇报（2026-09-15 用户要求）：专用进度面板（主账号发 bot 对话、
# 原地编辑，同 Reporter 模式）每 N 秒刷新一次；每完成 M 帖发一次里程碑汇总
PAWCHIVE_PANEL_INTERVAL_SECONDS = 60
# 多帖并发：worker 同时处理的帖子数上限（帖内文件并发仍由 /thread 信号量总控）
PAWCHIVE_MAX_INFLIGHT_POSTS = 8  # 4→8（2026-09-18：CDN 每 53s 掐断流，更多并发流摊平吞吐）
PAWCHIVE_MILESTONE_POSTS = 50
# --- 评论跟进（2026-09-12）---
# 命中标签的帖子会进「关注列表」，之后按天跟进它的评论区并取回新出现的媒体
# 评论（频道主/成员常把差分放在评论区）。**不是**扫群全量：每帖每次只发 1 次
# GetReplies 请求，只为命中标签的那几条帖子服务。
LISTEN_FOLLOW_INTERVAL_SECONDS = 24 * 3600   # 跟进周期：一天一次
LISTEN_FOLLOW_TTL_SECONDS = 15 * 24 * 3600   # 关注有效期：15 天
LISTEN_FOLLOW_MAX = 500                      # 关注列表上限（满了不再新增并告警）
LISTEN_FOLLOW_COMMENTS_LIMIT = 100           # 单帖单次最多取多少条评论
# 每帖之间的节流（秒）：500 条关注连起来发就是个突发，攒成「一天一次」的
# 本意是**摊开**，不能变成「一分钟 500 次」。3 秒是用户定的保守值——满额
# 500 条一轮 ≈ 25 分钟，对每天一次的任务毫无压力，但把峰值压到 20 次/分钟。
LISTEN_FOLLOW_MIN_INTERVAL_SECONDS = 3.0
LISTEN_FOLLOW_KEEP_EXPIRED = 2000            # 失效记录保留条数（裁剪，防无限增长）
LISTEN_NOTIFY_PREFIX = "📡 标签监听"
LISTEN_MATCH_PREFIX = "🏷 标签监听"
# 汇报开关：标签监听扫描命中/失败的事件通知（空扫不发，避免定期刷屏）。
REPORT_LISTEN = True

# ------------------------------------------------------------
# 标签监听的 Producer/Consumer 执行架构（2026-09-11）
#
# 扫描 ≠ 执行：Scanner 只负责「扫出匹配 → 落成持久化任务」，Worker 常驻受控
# 执行（forward / 触发下载）。任务与 checkpoint 在**同一个 SQLite 事务**里
# 提交，故 checkpoint 的语义是「此位置之前需要建的任务都已落盘」，与「任务是否
# 已发送成功」解耦（前者是 Scanner 的事，后者是 Worker 的事）。
#
# 数据分层（规格 §4）：**配置 → JSON（listen.json 仍是唯一真相）；业务运行状态
# → SQLite（runtime/tg_userbot.db，第一阶段只有 listener 这三张表）；技术日志
# → 文件（download.log 等原样不动）**。
# ------------------------------------------------------------
# Runtime DB 路径已在模块头部由 _resolve_paths 统一解析：默认放 RUNTIME_DIR
#（规格 §5：禁止各模块自造 runtime 路径），TG_RUNTIME_DB 单文件覆盖保留。
# 覆盖的用途：Termux 上 SAVE_FOLDER 落在 /storage/emulated/0（FUSE 外部存储），
# WAL 依赖 mmap 共享内存、在该文件系统上可能不可用；真机上若探测到 WAL 回落，
# 可把 DB 挪到应用私有目录。注意：**只允许主进程写**，Chrome Agent 进程绝不
# 能开连接（见 runtime_db 的「不做 import 期连接」）。
RUNTIME_DB_SCHEMA_VERSION = 10  # v10：manual_links 加 note（链接备注）
# 单条写事务等锁的上限（毫秒）与 SQLITE_BUSY/LOCKED 的有限重试（规格 §39：
# 记日志 → 短暂等待 → 有限次数重试，绝不无限循环、绝不因此崩掉主进程）。
RUNTIME_DB_BUSY_TIMEOUT_MS = 5000
RUNTIME_DB_BUSY_RETRIES = 5
RUNTIME_DB_BUSY_RETRY_DELAY_SECONDS = 0.2
# 落盘强度（规格 §5「优先保证 Runtime DB 持久性」）：FULL 每个事务 fsync，
# 崩溃/断电都不会丢已提交的任务。代价是每任务两次提交 = 两次 fsync，安卓
# 外部存储上单次可能几十毫秒——真机若卡顿可用 TG_DB_SYNCHRONOUS=NORMAL 降级
#（WAL 下 NORMAL 仍是崩溃安全的，只是断电可能丢最后几个事务）。
RUNTIME_DB_SYNCHRONOUS = (
    os.environ.get("TG_DB_SYNCHRONOUS", "FULL").strip().upper() or "FULL"
)

# ---- Listener Worker（规格 §27/§31/§32）----
# 第一版并发固定 1：一次只发一条，配合下面的最小间隔做保守节流。
LISTEN_WORKER_CONCURRENCY = 1
# 转发副本入队失败后，对账器补建的间隔（秒）（P0-2，2026-09-15）
LISTEN_ENQUEUE_RETRY_DELAY_SECONDS = 60
# 没有可执行任务时的轮询间隔（秒）
LISTEN_WORKER_POLL_SECONDS = 2.0
# 两条转发之间的最小间隔（秒）。**这不是 Telegram 官方安全阈值**，只是保守
# 节流；真正的限流以服务端 FloodWait 返回值为最高优先级（规格 §32）。
# 2026-09-13 从 1.5 调到 5：/wl since 回补了 ~5600 条历史任务（解析 bot 聊天），
# 40 条/分钟持续数小时大概率吃 FloodWait；12 条/分钟更稳（用户定的）。
LISTEN_WORKER_MIN_FORWARD_INTERVAL_SECONDS = 5.0
# 任务租约：claim 后多久没写完结果就视为 Worker 崩溃，可被恢复重跑（规格 §24）。
# 必须显著大于单次转发的正常耗时（含 FloodWait 等待之外的部分）。
LISTEN_WORKER_LEASE_SECONDS = 600
# 临时错误的最大自动重试次数（attempts 上限），超过转 FAILED 等人看。
LISTEN_WORKER_MAX_ATTEMPTS = 8
# 重试退避：与 queue._backoff_delay 同款「base × 2^(attempts-1) 封顶 max」，
# 复用同一风格而不是另设计一套（规格 §26）。
LISTEN_WORKER_BACKOFF_BASE_SECONDS = 60
LISTEN_WORKER_BACKOFF_MAX_SECONDS = 1800
# 待执行任务上限（规格 §30）：达到上限 Scanner 停止创建新任务，且**不推进
# checkpoint**（没入队的消息不能被跳过）。是配置项不是硬编码。
LISTEN_MAX_PENDING_TASKS = 1000
# FloodWait 的服务端等待值上限保护：正常直接采信服务端返回值（规格 §27 明确
# 不许固定等 60 秒）；只有离谱到超过这个上限（默认 6 小时）才截断并显著告警，
# 免得一个异常返回值把 Worker 挂死且不留痕。
LISTEN_FLOODWAIT_MAX_WAIT_SECONDS = 6 * 3600

# ── 白名单扫描生产者（下载白名单的停机补漏链，2026-09-13）──
# 白名单改成「事件生产者 + 扫描生产者 → 任务表 → Worker」双通道后，扫描链的
# 节奏参数。在线时事件链兜实时，扫描只管补漏，周期可以放宽。
WHITELIST_SCAN_INTERVAL_SECONDS = 300     # 扫描周期（秒）
WHITELIST_SCAN_PAGES_PER_ROUND = 10       # 每轮每聊天最多页数（页大小复用 LISTEN_MAX_MESSAGES_PER_SCAN）
WHITELIST_SCAN_PAGE_SLEEP_SECONDS = 5.0   # 页间小睡：读请求摊开（风控）

# /sql 诊断控制台（owner-only）的输出上限
SQL_CONSOLE_MAX_ROWS = 20                 # 查询最多返回行数（超出提示用 LIMIT）
SQL_CONSOLE_CELL_LIMIT = 48               # 单元格字符上限（截断带省略号）

# /sh 命令行执行器（owner-only）：工作目录记忆文件 + 子进程超时
SHELL_STATE_FILE = os.path.join(RUNTIME_DIR, "shell_state.json")
SHELL_TIMEOUT_SECONDS = 30

# /up 文件上传（owner-only）：单文件上限、进度编辑间隔、上传视图文件数
TG_UPLOAD_MAX_BYTES = 2 * 1024 ** 3
UPLOAD_PROGRESS_EDIT_SECONDS = 5.0
UPLOAD_LIST_LIMIT = 8

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

# Pawchive（pawchive.pw）会话 cookie：/paw plan 对比收藏必需（帖子/文件
# 直链本身公开、不需要它）。从 tg_secrets.json 的 pawchive_cookie 字段
# 读取，缺省空串 → 清单照常生成、faved 全为 None，不硬性要求配置。
PAWCHIVE_COOKIE = _SECRET_CONFIG.get("pawchive_cookie", "")

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
# 【🧹 Caption 清洗】按钮的等待输入窗口（按下「添加规则」/「测试清洗」后
# 发一条文本，同 cookie/查询模式；窗口内文本按 state.CAPTION_INPUT_MODE
# 决定当规则还是当待清洗文本）
CAPTION_INPUT_WINDOW_SECONDS = 120

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
    "capf", "capf_add", "capf_del", "capf_test", "capf_reset", "capf_clear",
    # Chrome 任务（/chrome_tasks 的只读视图 + 每条任务一个 🛑 取消按钮）
    # 外加 Agent 启停与状态（此前只在命令面板里，按钮菜单够不着）
    "chrome_tasks", "chrome_cancel", "chrome_start", "chrome_stop",
    "chrome_status",
    # 标签监听：视图 / 增删改 / 立即扫描 / 周期 / 总开关 + 添加向导的
    # 目标勾选（listen_tgt 带目标键）、加目标、下载开关、保存、取消
    "listen", "listen_add", "listen_edit", "listen_del", "listen_scan",
    "listen_interval", "listen_interval_set", "listen_toggle",
    "listen_tgt", "listen_tgtadd", "listen_dl", "listen_save",
    "listen_cancel",
    # SQL 模板：视图 / ➕ 新增（同名即覆盖）/ ▶️ 执行 / 🗑 删除
    "sqlt", "sqlt_add", "sqlt_run", "sqlt_del",
    # 命令行（sh：视图 / 预设执行 / ✏️ 输入命令）与文件上传
    # （up：视图 / 📄 最近文件带序号 / ✏️ 输入路径）
    "sh", "sh_run", "sh_input", "sh_ls",
    "up", "up_file", "up_input",
    # Pawchive：视图（含状态） / 搜作者 / Cookie / 暂停 / 恢复 / 待人工 /
    # 导出 CSV / 重投全部失败 / 搜索候选按钮（paw_pick 带序号）
    "paw", "paw_status", "paw_search", "paw_cookie", "paw_pause",
    "paw_resume", "paw_manual", "paw_done", "paw_archive", "paw_pick",
    "paw_csv",
    "mlink_done", "mlink_open", "mlink_view", "mlink_paw_done",
    "mlink_paw_del", "paw_done_view",
    "input_cancel",
    "paw_retry_all",
    "paw_post", "paw_find",
    "cmdt", "cmdt_add", "cmdt_run", "cmdt_del",
    # CD2 子菜单视图与「命令行/上传」合并工具箱视图
    "cd2_menu", "tools",
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


def save_pawchive_cookie(value, path=None):
    """把 Pawchive 会话 cookie 写入 tg_secrets.json（原子替换）并更新内存值。

    与 save_douyin_cookie 同款纪律：重新读盘保留其它字段；运行时动态读
    config.PAWCHIVE_COOKIE 属性，同步更新后立即作用于下一次扫描。
    返回错误文案，None=成功。
    """
    global PAWCHIVE_COOKIE
    target = path or SECRETS_FILE
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except FileNotFoundError:
        data = {}
    except Exception:
        return "tg_secrets.json 解析失败，拒绝覆盖（请手工修复该文件）"

    value = str(value or "").strip()
    data["pawchive_cookie"] = value

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

    PAWCHIVE_COOKIE = value
    return None

# ------------------------------------------------------------
# 持久化下载队列：任务先入队（媒体存消息引用、平台链接存完整 URL），
# 重启后自动恢复执行。失败任务移入 retry 列表停靠，由用户手动重试。
# ------------------------------------------------------------
QUEUE_FILE = os.path.join(RUNTIME_DIR, "download_queue.json")
# 队列持久化层（2026-09-13，任务书：下载队列 SQLite 化）：
#   "sqlite" → Runtime DB 的 download_tasks 表，单行事务 write-through（默认）
#   "json"   → 旧路径 download_queue.json 全量重写（一键回滚开关，不改代码）
# 内存字典（state.QUEUE）在两种模式下都是唯一工作副本与读取面，区别只在
# 「怎么存」；sqlite 模式下 DB 不可用自动回落 json 路径（见 queue._persist）。
QUEUE_STORE = os.environ.get("TG_QUEUE_STORE", "sqlite").strip().lower() or "sqlite"

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

# bot 控制面板对话里额外保留的「最近消息」条数（除最新菜单与最新状态面板外）。
# 2026-09-11 起程序主动通知都发到这个对话，若照旧「超时即删」，面板就成不了
# 可回溯的时间线（打开只剩菜单和面板两行）。保留最新 N 条 + 其余按超时删除 =
# 时间线有界（≈N+2 条封顶），既不刷屏也不会越攒越多。
BOT_CHAT_KEEP_NOTIFICATIONS = 20


# 自动清理执行间隔，默认 1 分钟，可通过 /setcleartime 修改
DEFAULT_CLEAR_INTERVAL_SECONDS = 60
CLEAR_TIME_CONFIG_FILE = os.path.join(RUNTIME_DIR, "clear_time.json")

# 已注册命令名全集（/ 开头的输入判「是不是命令」用）：bot 按钮面板注册的
# + 仅收藏夹跑的（CLEAN_COMMANDS 除斜杠）。评论捕获的目录模式标注（/A#x）
# 以 / 开头但不是命令，靠这份名单区分（2026-09-16）。
REGISTERED_COMMAND_NAMES = frozenset({name for name in (
    "status", "stats", "find", "progress", "up", "sh", "queue", "retry",
    "dedup",
    "caption_filter", "listen", "wl", "sql", "sqlt", "paw", "origin",
    "clean", "clearmsg", "setcleartime", "done", "thread", "folder",
    "logpath", "links", "help", "start",
)})


# 需要自动清理的命令（精确匹配）
CLEAN_COMMANDS = {
    "/status",
    "/help2",
    "/links",
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
    "🤖 Chrome",
    # /sql 诊断控制台的回复
    "📋 SQL",
    # /chrome_tasks 与 /chrome_cancel 的回复（任务取消功能）
    "🌐 Chrome 任务",
    "❌ 用法：/chrome_cancel",
    "❌ 序号必须是数字。",
    "❌ 任务序号无效",
    "ℹ️ 任务已经完成，无法取消。",
    "ℹ️ 任务已经失败，无法取消。",
    "ℹ️ 任务已经取消。",
    "🟢 TG Userbot 状态正常",
    "📁 保存目录：",
    "📋 日志文件：",
    "📖 TG Userbot 命令",
    "🧹 清理完成，共删除",
    "🧹 正在扫描收藏夹程序消息",
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
    # /listen（标签监听）的命令、回复与扫描通知
    "📡 标签监听",
    "🏷 标签监听",
    # /caption_filter（Caption 命名清洗）的回复与输入窗口提示
    "🧹 Caption 清洗",
    "🧪 Caption 清洗",
    "✅ 已添加规则",
    "✅ 已删除规则",
    "🗑 已清空全部 Caption",
    "♻️ 已恢复默认规则",
    "❌ 不支持的规则类型",
    "❌ 正则表达式无效",
    "❌ 规则编号不存在",
    "❌ /caption_filter",
    # /origin（来源解析失败账本）的命令与回复
    "/origin",
    "🕘 来源解析",
    "🕘 没有来源解析失败记录",
    # /cmdt（命令模板）的命令与回复
    "/cmdt",
    "📜 命令模板",
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
    # Runtime Reporter 的事件通知（Status Panel 刻意不入此表——它要长期存活，
    # 靠 edit_message 原地刷新而不是反复重建）
    "🚀 Userbot 已启动",
    "🛑 Userbot 正在关闭",
    "⚠️ Userbot 异常",
    "✅ 已恢复",
    "♻️ 自动重放",
    # 手动外链台账（/links 与发送链接的记录回复）
    "🔗 外链台账",
    # /help2 数据字典
    "📖 TG Userbot 数据字典",
)

# 持久保留的程序通知（豁免自动清理，/clearmsg 的 include_persistent=True
# 才批量清理）：下载「结果报告」用户可能晚些才看，瞬态清理会让人错过
#（2026-09-09 验收实测：结果通知 85 秒后被自动清理删除，用户以为没收到）。
PERSISTENT_NOTIFICATION_PREFIXES = (
    "✅ Chrome 下载完成",
    "❌ Chrome 下载失败",
    # 取消也是「结果报告」：用户主动取消后要能看到它到底停没停，
    # 和成功/失败同样豁免瞬态清理（2026-09-09 的教训见上）
    "🛑 Chrome 下载已取消",
)

# ------------------------------------------------------------
# Runtime Reporter（主动运行状态汇报，2026-09-10）
# ------------------------------------------------------------
# Reporter 是**只读观察者**：只读 state.* 与 stats 事件流，不参与任何调度。
# 两类输出——① Status Panel：bot 控制面板对话里的一条消息（主账号发，bot 有
# 48h 编辑时限），首发 send_message、之后原地 edit_message 刷新；
# ② Event Notification：重要事件发独立消息（bot 账号发，见 REPORT_TO_BOT_CHAT）。
# 它自己的任何异常都被兜住，绝不拖垮下载/队列/worker。
REPORT_ENABLED = True

# Status Panel 常规刷新间隔（秒）。下载进行中另有更快的进度节奏。
REPORT_INTERVAL_SECONDS = 60   # 与 Pawchive 进度面板统一（2026-09-17）
# 有下载在跑时，面板按此间隔刷新（只在这些时候加密，空闲时回到常规间隔）
REPORT_PROGRESS_ENABLED = True
REPORT_PROGRESS_INTERVAL_SECONDS = 15

# 事件流轮询间隔（秒）：增量读 task_events.jsonl 的新行 → 派发事件通知。
# 这是「立即通知」的代价上限——事件最迟这么久被汇报出去。
REPORT_EVENT_POLL_SECONDS = 15

# 事件通知开关。startup/shutdown/error/recovery/auto_replay 都是现有体系**没有**
# 的信息（尤其自动重放与手动 /retry 以前无从分辨），故默认开。
REPORT_STARTUP = True
REPORT_SHUTDOWN = True
REPORT_ERROR = True
REPORT_RECOVERY = True
REPORT_AUTO_REPLAY = True

# 下载类通知默认**关**：download.py 已经在收藏夹发「📥 开始下载 / ✅ 下载完成 /
# ❌ 文件下载失败」，Reporter 再发一遍就是每条双份刷屏。想要更详细的版本
#（含耗时/worker）把它们打开即可。
REPORT_DOWNLOAD_START = False
REPORT_DOWNLOAD_SUCCESS = False
REPORT_DOWNLOAD_FAILED = False
REPORT_RETRY = False

# 面板「当前下载」区块最多列几条，其余折叠为「还有 X 个下载任务……」。
# 这个值是**上限**：渲染时若总长度仍超 REPORT_MAX_MESSAGE_CHARS 会自动再减。
REPORT_MAX_DOWNLOADS_SHOWN = 5
# 面板「Workers」区块最多列几条异常明细（正常 worker 只计数，不逐条列）。
REPORT_MAX_WORKER_ALERTS_SHOWN = 5

# 单条汇报的硬上限（字符）：Telegram 上限 4096，留余量。超了就自动裁剪——
# 宁可少显示，也绝不能因为超长让消息发不出去（失败还会被静默吞成"没反应"）。
REPORT_MAX_MESSAGE_CHARS = 4000
# 下载文件名在面板里的最大长度（保尾：区分性最强的原名在尾部）。
REPORT_MAX_FILENAME_CHARS = 52

# 统计缓存时长（秒）：事件流没新增时直接复用上次算好的统计，避免每次刷新
# 都全量扫描 task_events.jsonl（实测 3 万行封顶时一次 load+rebuild 约 116ms
# 同步阻塞事件循环，而下载中面板每 15s 刷一次）。
REPORT_STATS_CACHE_SECONDS = 300

# 单条程序通知发送的超时（秒）：通知都在下载收尾路径上，不能被一条僵死连接
# 拖住（telethon 请求没有读超时）。超时即按「没发出去」处理、走回落/放弃。
NOTIFY_TIMEOUT_SECONDS = 30

# 单次汇报网络请求的超时（秒）：telethon 请求没有读超时，代理节点卡住时
# send/edit 会无限挂住——而汇报是**单条后台循环**，一次挂住 = 面板从此不再
# 更新（2026-09-11 事故的另一半）。超时即放弃本轮、下轮再试。
REPORT_NET_TIMEOUT_SECONDS = 30

# 汇报主循环意外结束（网络层取消这类非停服原因）后，隔多久自动重启。
# 见 app._reporter_supervisor：这是「循环被打死却没人知道」的最后一道网。
REPORT_RESTART_DELAY_SECONDS = 5

# 汇报目标：True=发到 bot 私聊（控制/状态频道，自带清理），False=发收藏夹。
# bot 未就绪（BOT_ID 为空）时自动回落收藏夹，绝不发丢。
REPORT_TO_BOT_CHAT = True
# 回落目标（收藏夹）。
REPORT_FALLBACK_TARGET = "me"

# Status Panel 首行前缀。**刻意不进 CLEAN_NOTIFICATION_PREFIXES**：面板要长期
# 存活、靠原地编辑刷新；若被自动清理删掉，下一轮会重建（MessageIdInvalid 路径）。
REPORT_STATUS_PREFIX = "🤖 Userbot Runtime"

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
# 主连接保活心跳（2026-09-18）：代理节点在凌晨低峰对**空闲** TCP 连接按
# 60s 周期回收（实测 03:00-07:00 每小时 ~50 次「断开-6 秒恢复」循环，断开
# 时长精确 60s）。55s 间隔的轻量 ping 让链路永不落入「空闲」判定，根治之。
# 下载活跃时 ping 是无谓开销但可忽略（<0.1KB/s）。
KEEPALIVE_PING_SECONDS = 55

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


# 历史版本（runtime/ 归集之前）散在下载根下的运行时文件确切 basename。
# 迁移只认这批名字，绝不误伤其它用户文件；旧数据根整体迁移（_migrate_legacy_data）
# 也用它优先把这些名字归入 RUNTIME_DIR 而不是 DOWNLOAD_DIR。
_SCATTERED_RUNTIME_BASENAMES = (
    "download.log", "download_history.txt", "thread_config.json",
    "whitelist_config.json", "download_queue.json", "clear_time.json",
    "cd2_launch.log",
)


def _migrate_runtime_files(save_folder=None, runtime_dir=None):
    """把历史版本散在下载根下的运行时文件迁入 RUNTIME_DIR（幂等）。

    save_folder/runtime_dir 可显式传入（供单测用临时目录）；默认取模块常量。
    仅当 runtime/ 下尚不存在同名文件时才移动：重复 import / 进程已在跑新版本
    都 no-op。返回本次实际迁入的文件名列表。运行时文件本来就是根目录里「非媒体
    扩展名」的那一撮，不会被 CD2 的备份/删除规则碰（按媒体扩展名白名单过滤），
    搬进子目录后依然免疫，故移动安全。失败不阻塞启动（下轮启动或手动处理）。
    """
    if save_folder is None:
        save_folder = DOWNLOAD_DIR
    if runtime_dir is None:
        runtime_dir = RUNTIME_DIR
    moved = []
    for name in _SCATTERED_RUNTIME_BASENAMES:
        src = os.path.join(save_folder, name)
        dst = os.path.join(runtime_dir, name)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                os.replace(src, dst)
                moved.append(name)
            except OSError:
                pass
    return moved


def _ensure_dir(path, what):
    """import 期建目录；失败给出可行动的中文错误而不是一串裸 traceback。

    数据根可能落在外置/第二块盘上（/Volumes/V1）：卷没挂载时 makedirs 会
    PermissionError/OSError，这里翻译成「去挂载或改用环境变量」的指引。
    """
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        raise RuntimeError(
            f"无法创建{what}：{path}（{type(e).__name__}: {e}）。"
            "若是外置数据卷请先挂载；或用 TG_DATA_ROOT / TG_DOWNLOAD_DIR / "
            "TG_RUNTIME_DIR 改指可用目录。"
        ) from e


def _move_into(src, dst_dir):
    """把 src（文件/目录/symlink）迁入 dst_dir，保留 basename。跨卷安全。

    单文件走「copy → 同目录 .migrating 临时名 → os.replace 原子就位 → 删源」，
    任一刻中断都不会留下半个最终文件（残留 .migrating 由 _sweep_migrating_temps
    与下轮重拷兜底）。目录逐项递归；目标同名目录已存在则**并入**（断点续迁，
    大目录迁移中断后下轮接着搬），同名文件或形态不同则视为冲突不动。
    源目录只在搬空后 rmdir——绝不带着未迁走的数据删目录。
    返回 (moved, conflicts)：相对 dst_dir 的名字列表。
    """
    moved, conflicts = [], []
    name = os.path.basename(src.rstrip(os.sep))
    dst = os.path.join(dst_dir, name)
    src_is_dir = os.path.isdir(src) and not os.path.islink(src)
    if os.path.lexists(dst):
        if src_is_dir and os.path.isdir(dst) and not os.path.islink(dst):
            for entry in sorted(os.listdir(src)):
                m, c = _move_into(os.path.join(src, entry), dst)
                moved.extend(os.path.join(name, x) for x in m)
                conflicts.extend(os.path.join(name, x) for x in c)
            try:
                os.rmdir(src)
            except OSError:
                pass
        else:
            conflicts.append(name)
        return moved, conflicts
    if src_is_dir:
        os.makedirs(dst, exist_ok=True)
        for entry in sorted(os.listdir(src)):
            m, c = _move_into(os.path.join(src, entry), dst)
            moved.extend(os.path.join(name, x) for x in m)
            conflicts.extend(os.path.join(name, x) for x in c)
        try:
            os.rmdir(src)
        except OSError:
            pass
        return moved, conflicts
    tmp = dst + ".migrating"
    try:
        if os.path.lexists(tmp):
            os.remove(tmp)
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
        os.remove(src)
        moved.append(name)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        conflicts.append(name)
    return moved, conflicts


def _sweep_migrating_temps(root):
    """清掉迁移中断残留的 .migrating 临时文件（最终文件缺失的下轮重拷会重做）。"""
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith(".migrating"):
                try:
                    os.remove(os.path.join(dirpath, name))
                except OSError:
                    pass


def _migrate_legacy_data(legacy_root, download_dir=None, runtime_dir=None):
    """把旧数据根（如 ~/Downloads/Nagram）整体迁入新根（幂等，跨卷安全）。

    顺序即「先识别旧路径 → 迁移运行数据 → 迁移媒体」（任务书 §19，先迁移后
    切换，不产生旧数据孤岛）：
      ① 旧根/runtime/* → RUNTIME_DIR（db/listen 等运行数据，最优先）
      ② 旧根散落的历史运行时文件 → RUNTIME_DIR
      ③ 旧根其余全部（媒体目录等）→ DOWNLOAD_DIR
    目标已存在 → 保留新目标不覆盖、源原样保留、记入 conflicts（§20）；
    tg_userbot.db 冲突的提示点名两个路径并说明沿用新库（§21）。绝不删除旧根
    本身（§22）；单条失败留在源侧下轮再迁，绝不阻塞启动。
    返回 {"old_root", "runtime", "scattered", "media", "conflicts"}。
    """
    if download_dir is None:
        download_dir = DOWNLOAD_DIR
    if runtime_dir is None:
        runtime_dir = RUNTIME_DIR
    summary = {"old_root": legacy_root, "runtime": [], "scattered": [],
               "media": [], "conflicts": []}

    def _note_conflicts(rel_names, dst_dir):
        for rel in rel_names:
            src = os.path.join(legacy_root, rel)
            dst = os.path.join(dst_dir, rel)
            if rel == "tg_userbot.db":
                summary["conflicts"].append(
                    f"tg_userbot.db 新旧同时存在：沿用 {dst}（不覆盖）；"
                    f"旧库保留在 {src}，请人工确认后处理")
            else:
                summary["conflicts"].append(
                    f"{rel} 目标已存在，保留新文件不覆盖：{dst}"
                    f"（旧文件留在 {src}）")

    def _bucket(names, bucket, dst_dir):
        summary[bucket].extend(names)
        if names:
            print(f"[迁移] {legacy_root} → {dst_dir}：{len(names)} 项",
                  flush=True)

    def _already_imported(name, runtime_dir):
        """新根已有 <名>.imported（该店已导入归档）→ 旧根残留是陈旧副本，
        跳过搬移。防多进程闸门竞态：Chrome Agent / run.sh 助手的 config
        import 也会跑闸门，可能在主进程分阶段归档后的窗口里把旧根残留
        搬进已清空的槽位（2026-09-14 实测踩中）。"""
        return os.path.exists(os.path.join(runtime_dir, name + ".imported"))

    try:
        entries = sorted(os.listdir(legacy_root))
    except OSError:
        return summary
    for name in entries:
        src = os.path.join(legacy_root, name)
        if name == "runtime":
            try:
                children = sorted(os.listdir(src))
            except OSError:
                continue
            for child in children:
                if _already_imported(child, runtime_dir):
                    continue
                moved, conflicts = _move_into(
                    os.path.join(src, child), runtime_dir)
                _bucket(moved, "runtime", runtime_dir)
                _note_conflicts(conflicts, runtime_dir)
            continue
        if name in _SCATTERED_RUNTIME_BASENAMES:
            moved, conflicts = _move_into(src, runtime_dir)
            _bucket(moved, "scattered", runtime_dir)
            _note_conflicts(conflicts, runtime_dir)
            continue
        moved, conflicts = _move_into(src, download_dir)
        _bucket(moved, "media", download_dir)
        _note_conflicts(conflicts, download_dir)
    _sweep_migrating_temps(download_dir)
    _sweep_migrating_temps(runtime_dir)
    return summary


def _legacy_migration_root(env=None, is_termux=None, legacy_root=None):
    """import 期自动迁移的闸门：返回旧数据根路径，None = 不迁移。

    只在「零配置桌面默认部署」生效——这是路径切换会甩下旧数据的唯一形态。
    任何 TG_* 路径变量（含测试基座注入的 TG_SAVE_FOLDER）都视为自定义布局，
    由用户自己负责；Termux 布局没变无需迁移；TG_MIGRATE_LEGACY=0 可整体关掉；
    旧根不存在或就是新目标也跳过。
    """
    env = os.environ if env is None else env
    if is_termux is None:
        is_termux = IS_TERMUX
    if is_termux or env.get("TG_MIGRATE_LEGACY") == "0":
        return None
    if any(env.get(k) for k in ("TG_SAVE_FOLDER", "TG_DOWNLOAD_DIR",
                                "TG_DATA_ROOT", "TG_RUNTIME_DIR")):
        return None
    if legacy_root is None:
        legacy_root = os.path.join(str(Path.home()), "Downloads", "Nagram")
    if not os.path.isdir(legacy_root):
        return None
    if legacy_root in (DOWNLOAD_DIR, RUNTIME_DIR):
        return None
    # 幂等终局检查：旧根只剩 runtime/ 骨架、且其中每个文件都已在新 RUNTIME_DIR
    # 就位（上轮迁完 + 分裂写入增量已被收尾合并）→ 无事可做，静默跳过。
    # 不做这一步，之后每次启动都会把同名骨架文件当冲突重新警告一遍。
    try:
        pending = set(os.listdir(legacy_root)) - {"runtime"}
        if not pending:
            old_rt = os.path.join(legacy_root, "runtime")
            pending = {
                name for name in os.listdir(old_rt)
                if not os.path.lexists(os.path.join(RUNTIME_DIR, name))
                and not os.path.lexists(
                    os.path.join(RUNTIME_DIR, name + ".imported"))
            }
    except OSError:
        return None
    if not pending:
        return None
    return legacy_root


# ============================================================
# import 期一次性副作用（保持单文件时的时机：先建目录、再配日志）
# ============================================================
_ensure_dir(DOWNLOAD_DIR, "媒体下载目录")
# ------------------------------------------------------------
# Chrome Agent V1（专用 Profile + CDP，2026-09-09 用户允许新建 Profile）。
# Chrome ≥136 禁止在默认 user-data-dir 上开 remote debugging，专用 Profile
# 是唯一合法形态；该实例由 Agent 独占，与用户正常 Chrome 并行、互不触碰。
# ------------------------------------------------------------

# CDP 只监听本机回环，禁 0.0.0.0（规格 6）
CHROME_CDP_HOST = "127.0.0.1"
CHROME_CDP_PORT = 9222
# 专用 Chrome 实例的代理（--proxy-server）：缺省沿用 TG_PROXY（同机网络环境
# 一致——本机直连被墙时专用实例同样需要代理才能下载外网资源）。格式与
# Chrome 一致：socks5://host:port 或 http://host:port。空 = 直连。
CHROME_PROXY_SERVER = os.environ.get(
    "CHROME_PROXY", os.environ.get("TG_PROXY", "")).strip() or None
# 无头模式：专用 Chrome 不弹窗口、不抢焦点（--headless=new，Chrome 109+ 的
# 新无头与有头的 CDP/下载行为一致；旧 headless 不支持下载，勿用旧开关）。
# TG_CHROME_HEADLESS=0 恢复有头（要盯着下载页面时用）。
CHROME_HEADLESS = os.environ.get(
    "TG_CHROME_HEADLESS", "1").strip().lower() not in ("0", "false", "off")
# 下载落 <DOWNLOAD_DIR>/TG Chrome Download/（规格 21），随 TG_DOWNLOAD_DIR 变化；
# Chrome 媒体与普通下载同属 DOWNLOAD_DIR，只占一个子目录、命名行为不变
CHROME_DOWNLOAD_DIR = os.path.join(DOWNLOAD_DIR, "TG Chrome Download")
# Agent 专用 Profile（持久复用；绝不指向正常 Chrome 的 User Data）
CHROME_PROFILE_DIR = os.path.expanduser("~/tg_chrome_agent_profile")
# 允许使用 /chrome* 命令的 owner（tg_secrets.json chrome_agent.owner_id 可
# 覆盖；缺省 None = 运行时回落主账号 MY_ID——单用户部署即本人）
CHROME_AGENT_OWNER_ID = (_SECRET_CONFIG.get("chrome_agent") or {}).get(
    "owner_id")
# 单任务最大尝试次数与单次尝试超时（秒）（规格 29/30）
CHROME_DOWNLOAD_RETRIES = 3
CHROME_DOWNLOAD_TIMEOUT = 1800
# RETRY_WAIT 到下次执行的等待秒数
CHROME_RETRY_WAIT_SECONDS = 30
# 等待 CDP 端口就绪的超时秒数（拉起专用 Chrome 后轮询 /json/version）
CHROME_CDP_CONNECT_TIMEOUT = 60
# Agent 轮询周期（秒）：认领请求、监视下载目录
CHROME_POLL_SECONDS = 1.0
# 持久化文件（规格 24：两进程各写各的，temp+os.replace 原子写）
CHROME_TASKS_FILE = os.path.join(RUNTIME_DIR, "chrome_tasks.json")
CHROME_REQUESTS_FILE = os.path.join(RUNTIME_DIR, "chrome_requests.json")
# 取消请求：User Bot 独占写、Agent 只读（与 chrome_requests.json 同款单向通道）
CHROME_CANCEL_REQUESTS_FILE = os.path.join(
    RUNTIME_DIR, "chrome_cancel_requests.json")
# 取消请求到达时 downloadWillBegin 还没来：在这段时间内继续等它，等到就能按
# guid 真正中止下载；等不到说明下载还没开始，靠关标签页兜底。太短会错过
# 「点开链接后隔几秒才开始下载」的站点（取消变成只改状态、文件照样落地），
# 所以留 5s —— 代价只是这类取消多等几秒，换取真的把下载摁住。
CHROME_CANCEL_GUID_GRACE_SECONDS = 5.0
# CDP 重连失败后的重试间隔（秒）。Chrome 崩了/被关了，Agent 不能变成
# 只会超时的僵尸：主循环每圈判活，断了就重连；重连不成按这个间隔再试。
CHROME_CDP_RECONNECT_DELAY_SECONDS = 5.0
# chrome_tasks.json 里保留的终态任务条数上限（成功/失败/取消）：只裁最老的，
# 刚终结的任务必须留着——chrome_client 的 notify_loop 每 5s 才轮询一次通知。
CHROME_TASKS_KEEP_TERMINAL = 200
CHROME_AGENT_PID_FILE = os.path.join(RUNTIME_DIR, "chrome_agent.pid")

# Chrome Agent V2 Recovery constants
# 健康检查间隔（秒）：Agent 向 User Bot 报告状态
CHROME_HEALTH_CHECK_INTERVAL = 5.0
# 恢复超时（秒）：User Bot 等待 Agent 响应的最长时间
CHROME_RECOVERY_TIMEOUT = 30.0
# 任务备份保留数量：Agent 保留的成功/失败任务历史记录数
CHROME_BACKUP_COUNT = 3
# GUID 有效期（秒）：下载 GUID 在 Agent 内存中的有效时间
CHROME_GUID_VALIDITY_SECONDS = 300
# GUID 最大存活时间（秒）：从下载开始到记录被回收的最长时间
CHROME_MAX_GUID_AGE = 1800
# 最小进度更新间隔（秒）：Agent 向 User Bot 报告进度的时间间隔
CHROME_MIN_PROGRESS_INTERVAL = 30

# Chrome Events constants
# 事件分发器默认 GUID 超时时间（秒）
CHROME_EVENTS_DEFAULT_GUID_TIMEOUT = 300
# 最大事件处理器数量限制
CHROME_EVENTS_MAX_HANDLERS = 100

# ============================================================
# Pawchive（pawchive.pw 归档站，2026-09-14）：扫描结果入 SQLite 生命周期
# （pawchive_posts/pawchive_files，schema v8），附件直链交给 Chrome Agent
# 串行下载。扫描数据放 RUNTIME_DIR/pawchive/，下载文件落 Chrome 的
# CHROME_DOWNLOAD_DIR/<subdir>（不经普通下载队列）。
# ============================================================
PAWCHIVE_API_BASE = "https://pawchive.pw"
PAWCHIVE_FILE_BASE = "https://file.pawchive.pw"
# 全量创作者列表缓存（约 20MB，站点 q 参数无效→本地过滤），TTL 内不重拉
PAWCHIVE_CREATORS_CACHE_TTL = 7 * 86400
# Worker 无可领任务时的轮询间隔（秒）
PAWCHIVE_WORKER_POLL_SECONDS = 2.0
# 单帖处理租约（秒）：大视频在 Chrome 端串行，单帖可达数小时；
# 等待期间 worker 每 PAWCHIVE_LEASE_RENEW_SECONDS 续一次租
PAWCHIVE_LEASE_SECONDS = 3600
PAWCHIVE_LEASE_RENEW_SECONDS = 60
# 等 Chrome 任务终态的轮询间隔（秒）；Chrome Agent 自身每 1s 认领/上报
PAWCHIVE_CHROME_POLL_SECONDS = 5.0
# Chrome Agent 拉起失败/下载提交异常时的重试退避（秒）
PAWCHIVE_AGENT_RETRY_SECONDS = 30
# 磁盘保护线（GB）：CHROME_DOWNLOAD_DIR 所在卷剩余低于它 → 暂停 worker
# 并通知，/paw resume 手动恢复
PAWCHIVE_MIN_FREE_GB = 2.0
# Cookie 输入窗口（秒）：/paw cookie 或菜单按钮后等下一条文本
PAWCHIVE_INPUT_WINDOW_SECONDS = 120
# /paw csv 单文件发收藏夹前的行数上限（保护 TG 消息/文件大小）
PAWCHIVE_CSV_MAX_ROWS = 20000
# 提交 Chrome 前先 HEAD 探测直链：404/410（站点侧缺文件/死链）直接标失败，
# 不进 Chrome——否则死链没有下载事件，每条要烧满 CHROME_DOWNLOAD_TIMEOUT
# （1800s）×3 次重试才判死（2026-09-14 实测某创作者 88% 附件是 404 死链）。
PAWCHIVE_PRECHECK_HEAD = True
# 扫描数据（创作者全量缓存 JSON，约 20MB）放 RUNTIME_DIR/pawchive/
PAWCHIVE_DATA_DIR = os.path.join(RUNTIME_DIR, "pawchive")

_ensure_dir(RUNTIME_DIR, "Runtime 运行数据目录")
_migrated_runtime_files = _migrate_runtime_files()
_legacy_root = _legacy_migration_root()
_migrated_legacy = (
    _migrate_legacy_data(_legacy_root) if _legacy_root else None)
log.configure(LOG_FILE, LOG_RETENTION_DAYS)
if _migrated_runtime_files:
    log.logger.info(
        "已把历史运行时文件迁入 runtime/ 目录："
        + "、".join(_migrated_runtime_files)
    )
if _migrated_legacy:
    s = _migrated_legacy
    parts = []
    if s["runtime"]:
        parts.append(f"运行数据 {len(s['runtime'])} 项")
    if s["scattered"]:
        parts.append(f"散落运行时文件 {len(s['scattered'])} 项")
    if s["media"]:
        parts.append(f"媒体 {len(s['media'])} 项")
    log.logger.info(
        "✅ 旧数据根迁移完成：" + "、".join(parts)
        + f"（{s['old_root']} → {DOWNLOAD_DIR} 与 {RUNTIME_DIR}）。"
        "旧目录仍保留，请确认无误后手动删除。")
    for _conflict in s["conflicts"]:
        log.logger.warning(f"迁移冲突：{_conflict}")
