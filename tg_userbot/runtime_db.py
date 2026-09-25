"""Runtime DB：标签监听的业务状态持久化层（SQLite）。

**为什么要有这一层**：原先「扫描即执行」——Scanner 一边扫一边 forward，
每轮扫描末尾才把状态写回 ``listen_state.json`` 一次。那条路径有三个真实缺陷：
① 一轮扫描命中几十条时，进程在中途被杀会把这几十条的状态**全部丢掉**（checkpoint
没推进、pending 没落盘），重启后整批重跑、已转发过的重发一遍；② 转发之间零间隔，
既容易触发 FloodWait 也没有节流手段；③ ``pending`` 已经是「半个任务表」却没有
状态、租约、次数上限。

改成「扫描 ≠ 执行」后：Scanner 只把匹配结果落成**持久化任务**，Worker 常驻受控
消费。任务与 checkpoint 在**同一个事务**里提交，于是 checkpoint 的语义变成
「此位置之前需要建的任务都已落盘」——与「任务是否已发送成功」彻底解耦
（前者是 Scanner 的事，后者是 Worker 的事）。崩溃最多丢正在执行的那一条。

**分层纪律（规格 §4）**：配置 → JSON（``listen.json`` 仍是唯一真相）；
业务状态 → 本模块；技术日志 → 文件（``download.log`` 等原样不动）。

**三条硬规矩**：
1. **SQL 只在本模块**（§33）。其他模块一律调这里的函数，不许散落 SQL。
2. **Telegram API 绝不出现在事务里**（§5/§23）。这里只有本地文件 I/O，
   事务都是「几条 INSERT/UPDATE」级别的短事务。
3. **import 期绝不建连接**。Chrome Agent 进程也 import 本包（它要用
   ``chrome_client`` 的纯函数），若 import 即开库，那个进程就成了第二个写者，
   WAL 的单写者前提当场被破坏。连接一律由 ``main()`` 显式 ``init_db()`` 建立。

**WAL 在 Termux 上的坑**：``RUNTIME_DIR`` 在安卓上落在 ``/storage/emulated/0``
（FUSE 外部存储），而 WAL 依赖 mmap 共享内存，历史上在该文件系统上不可用。所以
``init_db()`` **读回 ``PRAGMA journal_mode`` 的实际返回值**：不是 ``wal`` 就回落
``DELETE`` 并显著告警，绝不假装生效。真机若因此卡顿，可用 ``TG_RUNTIME_DB`` 把
数据库挪到应用私有目录。

**并发**：单进程单连接、全部访问都在事件循环线程内（sqlite3 的
``check_same_thread`` 默认 True 正好兜住误用）。SQLite 调用是同步的，会阻塞
事件循环——所以事务必须短（批量插入合并成一次提交），且失败按 §39 做**有限**
重试，绝不无限循环、绝不因此崩掉主进程。
"""
import collections
import json
import os
import re
import sqlite3
import time
from datetime import datetime

from . import config
from .log import logger

# 任务状态（规格 §25）。重试**不新增状态**：回到 PENDING + next_retry_at。
STATUS_PENDING = "PENDING"
STATUS_PROCESSING = "PROCESSING"
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"
STATUS_CANCELLED = "CANCELLED"
TERMINAL_STATUSES = (STATUS_SUCCESS, STATUS_FAILED, STATUS_CANCELLED)

# 转发已完成、下载副本入队未完成的中间态（2026-09-15，P0-2 对账）。
# PROCESSING →（转发成功）FORWARDED →（副本全部入队）SUCCESS。
# 停在 FORWARDED 的任务由 listener_worker 的对账器凭 payload.copy_msg_ids
# 补建下载任务，**绝不重新转发**；不参与 claim（只领 PENDING）与租约恢复
# （只恢复 PROCESSING）——它不是「执行中」，是「等对账」。
STATUS_FORWARDED = "FORWARDED"

# 事件类型（规格 §10）。命名与现有 stats 对齐（RECEIVED/QUEUED/RUNNING/RETRY/
# SUCCESS/FAILED/CANCELLED/DEDUP_HIT），另加 LEASE_EXPIRED。
# 注意：**这套事件只属于 listener 自己的任务生命周期**，与 stats 写进
# runtime/task_events.jsonl 的下载任务事件是两套 ID 空间，绝不能混用。
EV_RECEIVED = "RECEIVED"
EV_RUNNING = "RUNNING"
EV_RETRY = "RETRY"
EV_SUCCESS = "SUCCESS"
EV_FAILED = "FAILED"
EV_CANCELLED = "CANCELLED"
EV_LEASE_EXPIRED = "LEASE_EXPIRED"

# 关注列表状态。**过期是「置为失效」而不是删除**——保留下来才能回答
# 「这条帖子到底有没有等到讨论串」，也是排查时的证据。
FOLLOW_ACTIVE = "ACTIVE"
FOLLOW_EXPIRED = "EXPIRED"

# Pawchive 帖子生命周期（schema v8）。与 listener 的状态集刻意错开：
# MANUAL（有外链等人工处理）与 COMPLETED（彻底完成）是 pawchive 特有终态；
# FAILED 不自动重试（Chrome 端已重试过 3 次），由 /paw retry 手动重投。
PAW_POST_PENDING = "PENDING"
PAW_POST_PROCESSING = "PROCESSING"
PAW_POST_COMPLETED = "COMPLETED"
PAW_POST_MANUAL = "MANUAL"
PAW_POST_FAILED = "FAILED"
# 死链归档（2026-09-16）：FAILED 帖全是 404 死链（站点删档），批量移入
# ARCHIVED 让 PENDING/MANUAL 的真实待办更清晰；post_id 仍占唯一索引，
# 后续扫描自然去重不会复活。
PAW_POST_ARCHIVED = "ARCHIVED"
PAW_POST_TERMINAL = (PAW_POST_COMPLETED, PAW_POST_MANUAL, PAW_POST_FAILED)

# 站点侧死链的失败标记前缀（worker 写入，retry 重投时据此跳过——死链
# 重试一万次也是 404，不属于可恢复数据）
PAW_DEAD_LINK_MARK = "站点缺文件(404)"

# 文件级状态：SUBMITTED = 已交给 Chrome Agent（对账以 chrome_tasks.json 为准）
PAW_FILE_PENDING = "PENDING"
PAW_FILE_SUBMITTED = "SUBMITTED"
PAW_FILE_DONE = "DONE"
PAW_FILE_FAILED = "FAILED"


class DbUnavailable(RuntimeError):
    """Runtime DB 本次操作不可用（等锁超限 / 非 BUSY 类 SQL 错误）。

    调用方（Scanner / Worker）应把它当成「本轮没做成」：记日志、**不推进
    checkpoint**、不崩主进程，下一轮重来。原始异常挂在 ``__cause__`` 上。
    """


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # v3 起：chain 区分 listen（标签监听）与 wl（下载白名单）两条独立游标。
    # 同一聊天两边都在时互不干扰——/wl since 回补只倒退 wl 游标。
    """
    CREATE TABLE IF NOT EXISTS listener_checkpoints (
        source_chat_id INTEGER NOT NULL,
        chain TEXT NOT NULL DEFAULT 'listen',
        last_message_id INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (source_chat_id, chain)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS listener_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        grouped_id INTEGER,
        target_type TEXT NOT NULL,
        target_chat_id INTEGER,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_retry_at INTEGER,
        created_at INTEGER NOT NULL,
        started_at INTEGER,
        completed_at INTEGER,
        lease_until INTEGER,
        last_error TEXT,
        download INTEGER NOT NULL DEFAULT 0,
        payload TEXT,
        origin TEXT NOT NULL DEFAULT 'listen'
    )
    """,
    # 唯一约束。**必须建在 COALESCE(target_chat_id, 0) 上**：SQLite 的 UNIQUE
    # 索引里 NULL 彼此不相等，按字面 (…, target_chat_id) 建的话，收藏夹目标
    # （target_chat_id = NULL）那条唯一约束**完全失效**——同一消息能插出两条
    # 收藏夹任务、转发两次。收藏夹恰恰是最常见的那个目标。
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_listener_task_unique
    ON listener_tasks (
        source_chat_id, message_id, target_type, COALESCE(target_chat_id, 0)
    )
    """,
    # 领取用的索引：按 (status, next_retry_at) 找可执行任务
    """
    CREATE INDEX IF NOT EXISTS idx_listener_task_pickup
    ON listener_tasks (status, next_retry_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        payload TEXT,
        FOREIGN KEY(task_id) REFERENCES listener_tasks(id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_task_events_task
    ON task_events (task_id, id)
    """,
    # 评论跟进：命中标签的帖子进关注列表，之后按天跟进它的评论区。
    # caption / post_date / source_name 是**建列表时的快照**——后面十几次检查
    # 直接用，不再回频道取一次原帖（原帖被编辑/删除也不影响已定下的命名）。
    """
    CREATE TABLE IF NOT EXISTS listener_follows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER NOT NULL,
        post_id INTEGER NOT NULL,
        source_chat_id INTEGER,
        caption TEXT,
        post_date TEXT,
        source_name TEXT,
        status TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        checks INTEGER NOT NULL DEFAULT 0,
        last_checked_at INTEGER,
        last_error TEXT
    )
    """,
    # 同一个帖子只关注一次（重跑/重启都不会插出第二条）
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_listener_follow_unique
    ON listener_follows (channel_id, post_id)
    """,
    # 取到期关注用：(status, last_checked_at)
    """
    CREATE INDEX IF NOT EXISTS idx_listener_follow_due
    ON listener_follows (status, last_checked_at)
    """,
    # ============================================================
    # 下载队列持久化（2026-09-13，任务书：下载队列 SQLite 化）。
    # 替代 runtime/download_queue.json 的「每次变更全量重写」：单行事务
    # write-through，内存字典（state.QUEUE）仍是唯一工作副本与读取面。
    # state 只有 QUEUED/RETRY 两态（终态即删行）——下载任务的执行是进程内的，
    # 在途判定走内存 EXECUTING，不引入 listener 的 PROCESSING/租约。
    # seq 是全局单调序号：装载时 ORDER BY seq 还原 tasks/retry 的列表顺序
    # （展示与 /queue 3、/retry del 2 的序号语义都建立在它上面）。
    # 除固定列外的一切字段（含未来新增的未知字段）都装 payload JSON，
    # round-trip 必须逐字段无损。
    # ============================================================
    """
    CREATE TABLE IF NOT EXISTS download_tasks (
        id            TEXT PRIMARY KEY,
        kind          TEXT NOT NULL,
        state         TEXT NOT NULL,
        seq           INTEGER NOT NULL,
        attempts      INTEGER NOT NULL DEFAULT 0,
        next_retry_at REAL,
        enqueued_at   INTEGER NOT NULL,
        payload       TEXT NOT NULL
    )
    """,
    # 装载顺序 + 启动日志的 count 查询都按 state 过滤
    """
    CREATE INDEX IF NOT EXISTS idx_download_tasks_state
    ON download_tasks (state, seq)
    """,
    # ============================================================
    # 下载任务事件流（2026-09-14，任务书：任务事件 SQLite 化 Phase 2）。
    # 替代 runtime/task_events.jsonl：台账/对账（stats.rebuild_stats）与
    # Reporter 通知的数据源。**与 listener 的 task_events 表是两套 ID 空间**
    # （那边自增 id 外键 listener_tasks；这边 task_id 是下载任务 uuid），
    # 绝不混用，故命名刻意错开。ts 存 epoch 秒（读出渲染回本地串）；task_id
    # 可空（DEDUP_SKIPPED/LISTEN_SCAN/LISTEN_FAIL 等输入侧事件无任务）；
    # **不建外键**（事件流是独立台账，避免先于任务行/任务删行被级联的边角）。
    # rowid（id）单调递增 = Reporter 的增量游标；裁剪删旧行不影响游标语义。
    # ============================================================
    """
    CREATE TABLE IF NOT EXISTS download_events (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts        INTEGER NOT NULL,
        ev        TEXT NOT NULL,
        task_id   TEXT,
        payload   TEXT
    )
    """,
    # 台账按窗口聚合（ts 区间扫描）与按任务对账（task_id, id）
    """
    CREATE INDEX IF NOT EXISTS idx_download_events_ts ON download_events (ts)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_download_events_task
    ON download_events (task_id, id)
    """,
    # ============================================================
    # 下载历史（2026-09-14，Phase 3，schema v6）。替代
    # runtime/download_history.txt：行格式
    # `ts | 类型 | 文件名 | 大小 | 来源：xxx` 是全部消费方（/done、/find、
    # 台账回退口径）的接口，行→列拆解只发生在写入/读出边界，渲染逐字符还原。
    # 旧类型（统一链之前的 抖音/Instagram）原样保留；不合 5 段格式的行进
    # raw 列兜底（零丢失）。无封顶（原文件同样无界）。
    # ============================================================
    """
    CREATE TABLE IF NOT EXISTS download_history (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts        INTEGER,
        kind      TEXT,
        filename  TEXT,
        size_text TEXT,
        source    TEXT,
        raw       TEXT
    )
    """,
    # ============================================================
    # 去重索引（2026-09-14，Phase 4，schema v7）。替代
    # runtime/dedup_index.txt：key/ts/filename 三列忠实映射
    # `key	日期	文件名` 行（ts 保留原短格式纯信息字段）。内存 dict
    # （state.DEDUP_INDEX）仍是判重热路径的唯一读取面——本表只负责持久化
    # 与启动装载，同键重复行容忍（dict 后写胜，与文件时代一致）。
    # ============================================================
    """
    CREATE TABLE IF NOT EXISTS dedup_index (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        key      TEXT NOT NULL,
        ts       TEXT,
        filename TEXT
    )
    """,
    # ============================================================
    # Pawchive 扫描结果（2026-09-14，schema v8）。/paw plan 把创作者帖子
    # 扫描结果按**帖子级**落库，由 pawchive_worker 以生命周期方式消费：
    # PENDING →（claim 租约）PROCESSING → 附件直链逐条交给 Chrome Agent →
    #   全部 DONE 且无外链 → COMPLETED（已完成）
    #   全部 DONE 且有外链 → MANUAL（人工处理，外链清单在 ext_links）
    #   Chrome 失败/取消   → FAILED（/paw retry 重投 PENDING）
    # UNIQUE(service, creator_id, post_id)：重复 plan 幂等——已存在的帖子
    # 整体跳过（状态与文件进度都不动），只有新帖子才入队。
    # ============================================================
    """
    CREATE TABLE IF NOT EXISTS pawchive_posts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        service      TEXT NOT NULL,
        creator_id   TEXT NOT NULL,
        creator_name TEXT,
        post_id      TEXT NOT NULL,
        title        TEXT,
        published    TEXT,
        post_url     TEXT,
        subdir       TEXT,
        status       TEXT NOT NULL,
        ext_links    TEXT,
        ext_count    INTEGER NOT NULL DEFAULT 0,
        attempts     INTEGER NOT NULL DEFAULT 0,
        next_retry_at INTEGER,
        lease_until  INTEGER,
        created_at   INTEGER NOT NULL,
        started_at   INTEGER,
        completed_at INTEGER,
        last_error   TEXT,
        scan_batch   TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_pawchive_post_unique
    ON pawchive_posts (service, creator_id, post_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_pawchive_post_pickup
    ON pawchive_posts (status, next_retry_at)
    """,
    # 文件级进度：worker 重领帖子时只提交还差的那部分；chrome_task_id 是
    # 与 chrome_tasks.json 对账的钥匙（Agent 终态 = 文件终态的唯一真相源）。
    """
    CREATE TABLE IF NOT EXISTS pawchive_files (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        post_row      INTEGER NOT NULL,
        url           TEXT NOT NULL,
        filename      TEXT,
        status        TEXT NOT NULL,
        chrome_task_id TEXT,
        size_bytes    INTEGER,
        attempts      INTEGER NOT NULL DEFAULT 0,
        error         TEXT,
        updated_at    INTEGER,
        FOREIGN KEY(post_row) REFERENCES pawchive_posts(id)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_pawchive_file_unique
    ON pawchive_files (post_row, url)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_pawchive_file_pickup
    ON pawchive_files (post_row, status)
    """,
    # ============================================================
    # 手动外链台账（2026-09-16，schema v9）。移动端自己找到的网盘链接
    # 发进来登记（默认 PENDING 未处理），人工处理完手动标 DONE；再次
    # 发送同链接时按 url_key 精确查重提示已处理。url_key 保留原始大小写
    # （MEGA 解密键区分大小写，lower 会造成不同链接误判重复）。
    # ============================================================
    """
    CREATE TABLE IF NOT EXISTS manual_links (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        url        TEXT NOT NULL,
        url_key    TEXT NOT NULL UNIQUE,
        host       TEXT,
        note       TEXT,
        status     TEXT NOT NULL DEFAULT 'PENDING',
        created_at INTEGER NOT NULL,
        done_at    INTEGER
    )
    """,
    # ============================================================
    # 功能使用审计（2026-09-24，schema v11）：按 (功能名, 日期) 聚合计数，
    # /usage 看排行，/sql 亦可自由聚合。功能名 = 指令（/paw plan）或
    # 面板动作（menu:home）。
    # ============================================================
    """
    CREATE TABLE IF NOT EXISTS feature_usage (
        name     TEXT NOT NULL,
        day      TEXT NOT NULL,
        count    INTEGER NOT NULL DEFAULT 0,
        first_at INTEGER NOT NULL,
        last_at  INTEGER NOT NULL,
        PRIMARY KEY (name, day)
    )
    """,
)

# SQLITE_BUSY / SQLITE_LOCKED 的典型文案（只用于判定是否值得重试）
_BUSY_MARKERS = ("locked", "busy")

_CONN = None
_JOURNAL_MODE = None
_OPEN_PATH = None      # 当前连接对应的路径（sqlite3.Connection 不允许挂自定义属性）


# ============================================================
# 连接
# ============================================================
def db_path() -> str:
    """数据库路径（**调用时**读 config，方便测试与 TG_RUNTIME_DB 覆盖）。"""
    return getattr(config, "RUNTIME_DB_FILE", config.RUNTIME_DB_FILE)


def has_connection() -> bool:
    """当前进程是否已开库（main() 之外应当恒为 False）。"""
    return _CONN is not None


def journal_mode():
    """实际生效的 journal 模式（'wal' / 'delete' / …），未初始化返回 None。"""
    return _JOURNAL_MODE


def _conn():
    if _CONN is None:
        raise DbUnavailable("Runtime DB 未初始化（应先调用 init_db()）")
    return _CONN


def _retry_delay():
    """等锁重试之间的短暂停顿。读 config 便于测试把延时调成 0。"""
    try:
        delay = float(getattr(config, "RUNTIME_DB_BUSY_RETRY_DELAY_SECONDS", 0.2))
    except (TypeError, ValueError):
        delay = 0.2
    if delay > 0:
        time.sleep(delay)


def _is_busy(exc) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _BUSY_MARKERS)


def _execute(conn, sql, params=()):
    """最底层执行入口：所有语句都从这里走（统一日志/测试注入的收口）。"""
    return conn.execute(sql, params)


def _retry(proc, what):
    """跑 proc()，遇 SQLITE_BUSY/LOCKED 有限重试；用尽抛 DbUnavailable。

    只对「等锁」类错误重试——表不存在这类错误重试一万次也不会好，只会
    掩盖问题（§39：有限次数、不无限循环）。
    """
    attempts = max(1, int(getattr(config, "RUNTIME_DB_BUSY_RETRIES", 5)))
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return proc()
        except sqlite3.OperationalError as e:
            if not _is_busy(e):
                raise
            last = e
            logger.warning(
                f"🗄 Runtime DB 等锁（{what}），第 {attempt}/{attempts} 次重试：{e}"
            )
            _retry_delay()
    logger.error(f"🗄 Runtime DB 等锁超限，放弃本次操作（{what}）：{last}")
    raise DbUnavailable(f"{what}：等锁超限（{last}）") from last


def _write(proc, what):
    """在 IMMEDIATE 写事务里跑 proc(conn)，成功 COMMIT、异常 ROLLBACK。

    用 ``BEGIN IMMEDIATE`` 立刻取写锁：避免「读事务升级写事务」时的死锁窗口。
    BEGIN/COMMIT/ROLLBACK 走裸 conn.execute（不进 _execute），这样测试注入
    失败点正好落在**业务语句**上，而不是事务控制语句上。
    """
    def run():
        conn = _conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            out = proc(conn)
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except Exception as e:      # 回滚都失败：只记日志，原异常优先
                logger.warning(f"🗄 Runtime DB 回滚失败（{what}）：{e}")
            raise
        conn.execute("COMMIT")
        return out

    try:
        return _retry(run, what)
    except DbUnavailable:
        raise
    except sqlite3.Error as e:
        logger.error(f"🗄 Runtime DB 写失败（{what}）：{type(e).__name__}: {e}")
        raise DbUnavailable(f"{what} 失败：{e}") from e


def _read(proc, what):
    """读操作也同样收口：失败一律抛 DbUnavailable，**绝不返回一个假值**。

    checkpoint 尤其不能糊弄：读失败时返回 0/None 会被上层当成「没有 checkpoint」，
    进而把整个历史当新消息扫下来。
    """
    def run():
        return proc(_conn())

    try:
        return _retry(run, what)
    except DbUnavailable:
        raise
    except sqlite3.Error as e:
        logger.error(f"🗄 Runtime DB 读失败（{what}）：{type(e).__name__}: {e}")
        raise DbUnavailable(f"{what} 失败：{e}") from e


def close_db():
    """关闭连接（幂等；未初始化时 no-op）。"""
    global _CONN, _JOURNAL_MODE, _OPEN_PATH
    if _CONN is not None:
        try:
            _CONN.close()
        except Exception as e:
            logger.warning(f"🗄 Runtime DB 关闭失败（忽略）：{e}")
    _CONN = None
    _JOURNAL_MODE = None
    _OPEN_PATH = None


def init_db(path=None) -> bool:
    """建连接 + 设 PRAGMA + 建表/迁移。可重复调用（幂等）。

    返回是否可用。失败只记日志返回 False——数据库起不来不能让整个 userbot
    起不来（标签监听是增量功能，降级成「监听不工作」而不是「程序不工作」）。
    """
    global _CONN, _JOURNAL_MODE, _OPEN_PATH
    target = path or db_path()
    try:
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if _CONN is not None:
            # 同一个路径重复 init：直接复用，不重开（避免句柄泄漏）
            if _OPEN_PATH == target:
                return True
            close_db()
        conn = sqlite3.connect(target)
        conn.row_factory = sqlite3.Row

        # WAL：读回**实际**生效的模式。安卓外部存储（FUSE）上可能拿不到 wal，
        # 那时 SQLite 会保持原模式——必须如实记下来并告警，不能假装成功。
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        _JOURNAL_MODE = str(mode).lower()
        if _JOURNAL_MODE != "wal":
            logger.warning(
                f"🗄 Runtime DB 未能启用 WAL（实际 journal_mode={_JOURNAL_MODE}）。"
                "该文件系统可能不支持共享内存（安卓外部存储常见）。功能不受"
                "影响，只是并发读写退化为文件锁；卡顿可用 TG_RUNTIME_DB 把库"
                f"挪到应用私有目录。路径：{target}"
            )
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            f"PRAGMA busy_timeout={int(config.RUNTIME_DB_BUSY_TIMEOUT_MS)}")
        synchronous = str(config.RUNTIME_DB_SYNCHRONOUS).upper()
        if synchronous not in ("OFF", "NORMAL", "FULL", "EXTRA"):
            synchronous = "FULL"
        conn.execute(f"PRAGMA synchronous={synchronous}")
        conn.isolation_level = None      # 自己管事务（BEGIN IMMEDIATE/COMMIT）
        _CONN = conn
        _OPEN_PATH = target
        version = migrate()
        logger.info(
            f"🗄 Runtime DB 就绪：{target} | schema v{version} | "
            f"journal={_JOURNAL_MODE} | synchronous={synchronous} | "
            f"busy_timeout={config.RUNTIME_DB_BUSY_TIMEOUT_MS}ms"
        )
        return True
    except Exception as e:
        logger.error(f"🗄 Runtime DB 初始化失败（{target}）："
                     f"{type(e).__name__}: {e}")
        close_db()
        return False


# ============================================================
# schema 版本 / 迁移
# ============================================================
def get_schema_meta(key):
    row = _read(lambda c: _execute(
        c, "SELECT value FROM schema_meta WHERE key=?", (key,)).fetchone(),
        f"读 schema_meta.{key}")
    return row[0] if row else None


def set_schema_meta(key, value):
    def do(conn):
        _execute(conn,
                 "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))
    return _write(do, f"写 schema_meta.{key}")


def get_schema_version() -> int:
    raw = get_schema_meta("schema_version")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _table_columns(conn, table):
    return {r["name"] for r in _execute(
        conn, f"PRAGMA table_info({table})").fetchall()}


def _migrate_v3():
    def do(conn):
        if "chain" not in _table_columns(conn, "listener_checkpoints"):
            _execute(conn, """
                CREATE TABLE listener_checkpoints_v3 (
                    source_chat_id INTEGER NOT NULL,
                    chain TEXT NOT NULL DEFAULT 'listen',
                    last_message_id INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (source_chat_id, chain)
                )""")
            _execute(conn,
                     "INSERT INTO listener_checkpoints_v3 "
                     "SELECT source_chat_id, 'listen', last_message_id, "
                     "updated_at FROM listener_checkpoints")
            _execute(conn, "DROP TABLE listener_checkpoints")
            _execute(conn,
                     "ALTER TABLE listener_checkpoints_v3 "
                     "RENAME TO listener_checkpoints")
        if "origin" not in _table_columns(conn, "listener_tasks"):
            _execute(conn,
                     "ALTER TABLE listener_tasks ADD COLUMN origin "
                     "TEXT NOT NULL DEFAULT 'listen'")
    _write(do, "迁移 v3（chain + origin）")


def migrate() -> int:
    """建表 + 逐版本迁移，幂等、可重复执行、中途失败可重跑（§6）。

    **先建表再读版本**：全新库上 ``schema_meta`` 还不存在，先读版本会直接
    报「no such table」。（读操作收口成 DbUnavailable 而不是返回假值，正是
    为了不让这种顺序错误被静默吞掉——这里靠顺序修，不靠兜底。）

    每一步都是 ``CREATE ... IF NOT EXISTS`` 或带条件的数据修补，所以重复执行
    安全；版本号最后写回，故「跑了一半崩掉」重跑时前面的步骤会再走一遍而不会
    出错，也不会破坏已有数据。
    """
    def create(conn):
        for ddl in _SCHEMA:
            _execute(conn, ddl)

    _write(create, "建表")
    version = get_schema_version()

    target = int(config.RUNTIME_DB_SCHEMA_VERSION)
    if version < 1:
        # v0 → v1：本模块的初始 schema（上面的 CREATE 已经覆盖）
        logger.info("🗄 Runtime DB 迁移：建立 v1 schema")
    if 1 <= version < 2:
        # v1 → v2：新增 listener_follows（评论跟进关注列表）。纯建表，没有数据
        # 迁移——旧库补上这张空表即可，所以上面的 CREATE IF NOT EXISTS 就够了。
        logger.info("🗄 Runtime DB 迁移：v2（+ listener_follows 关注列表）")
    if version < 3:
        # v2 → v3：checkpoints 加 chain（旧库重建，旧行归 listen）、
        # tasks 加 origin（ALTER，旧行落默认 'listen'）。新库建表时已是
        # v3 形状，这里探测到列已存在即为 no-op。
        _migrate_v3()
        logger.info("🗄 Runtime DB 迁移：v3（checkpoints.chain + tasks.origin）")
    if version < 4:
        # v3 → v4：新增 download_tasks（下载队列持久化）。纯建表，没有数据
        # 迁移——上面的 CREATE IF NOT EXISTS 已覆盖；旧队列 JSON 由 queue.py
        # 的启动导入负责（表空 + JSON 非空 → 一次性导入）。
        logger.info("🗄 Runtime DB 迁移：v4（+ download_tasks 下载队列）")
    if version < 5:
        # v4 → v5：新增 download_events（下载任务事件流）。纯建表；旧 JSONL
        # 由 stats.migrate_and_trim_events 的启动导入负责。
        logger.info("🗄 Runtime DB 迁移：v5（+ download_events 任务事件流）")
    if version < 6:
        # v5 → v6：新增 download_history（下载历史）。纯建表；旧 TXT 由
        # history.migrate_history_to_db 的启动导入负责。
        logger.info("🗄 Runtime DB 迁移：v6（+ download_history 下载历史）")
    if version < 7:
        # v6 → v7：新增 dedup_index（去重索引）。纯建表；旧 TXT 由
        # dedup.load_index 的启动导入负责。
        logger.info("🗄 Runtime DB 迁移：v7（+ dedup_index 去重索引）")
    if version < 8:
        # v7 → v8：新增 pawchive_posts / pawchive_files（Pawchive 扫描结果
        # 生命周期）。纯建表，没有数据迁移——旧数据只有独立 CLI 脚本的
        # manifest/CSV，不在 bot 数据域内，不做导入。
        logger.info("🗄 Runtime DB 迁移：v8（+ pawchive_posts/pawchive_files）")
    if version < 9:
        # v8 → v9：新增 manual_links（手动外链台账）。纯建表。
        logger.info("🗄 Runtime DB 迁移：v9（+ manual_links 手动外链台账）")
    if version < 10:
        # v9 → v10：manual_links 加 note（链接备注，发链接附言可更新）。
        # ALTER 幂等：列已存在即跳过。
        def _v10(conn):
            cols = {r["name"] for r in _execute(
                conn, "PRAGMA table_info(manual_links)").fetchall()}
            if "note" not in cols:
                _execute(conn, "ALTER TABLE manual_links ADD COLUMN note TEXT")
        _write(_v10, "迁移 v10（manual_links.note）")
        logger.info("🗄 Runtime DB 迁移：v10（+ manual_links.note）")
    if version < 11:
        # v10 → v11：新增 feature_usage（功能使用审计，按天计数）。纯建表。
        logger.info("🗄 Runtime DB 迁移：v11（+ feature_usage 功能使用审计）")
    if version != target:
        set_schema_meta("schema_version", target)
        logger.info(f"🗄 Runtime DB schema 版本：{version or '（无）'} → {target}")
    return target


# ============================================================
# checkpoint
# ============================================================
def get_listener_checkpoint(source_chat_id, chain="listen"):
    """取某条链的 checkpoint；从未设过返回 None。

    None 与 0 语义不同：None = 还没建立过，0 = 明确的「从头开始」。
    chain：'listen'（标签监听）/ 'wl'（下载白名单扫描）。
    """
    row = _read(lambda c: _execute(
        c, "SELECT last_message_id FROM listener_checkpoints "
           "WHERE source_chat_id=? AND chain=?",
        (int(source_chat_id), str(chain))).fetchone(),
        "读监听 checkpoint")
    return int(row[0]) if row else None


def _write_checkpoint(conn, source_chat_id, last_message_id, now,
                      chain="listen"):
    """写 checkpoint（独立函数：既是事务内的一个步骤，也是测试的注入点）。"""
    _execute(conn,
             "INSERT INTO listener_checkpoints"
             "(source_chat_id, chain, last_message_id, updated_at) "
             "VALUES(?,?,?,?) "
             "ON CONFLICT(source_chat_id, chain) DO UPDATE SET "
             "last_message_id=excluded.last_message_id, "
             "updated_at=excluded.updated_at",
             (int(source_chat_id), str(chain), int(last_message_id),
              int(now)))


def set_listener_checkpoint(source_chat_id, last_message_id, now=None,
                            chain="listen"):
    """单独写某条链的 checkpoint（初始化、人工回补用；扫描路径请用原子版本）。"""
    now = _now(now)
    _write(lambda conn: _write_checkpoint(
        conn, source_chat_id, last_message_id, now, chain=chain),
        "写监听 checkpoint")
    logger.info(f"🗄 checkpoint 已写入：{source_chat_id}[{chain}] → "
                f"{last_message_id}")
    return True


# ============================================================
# 任务：入队（事务）
# ============================================================
def _now(now=None):
    return int(time.time() if now is None else now)


def _dumps(payload):
    if payload is None:
        return None
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.warning(f"🗄 任务 payload 无法序列化（丢弃）：{e}")
        return None


def _loads(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _row_to_task(row):
    if row is None:
        return None
    rec = dict(row)
    rec["payload"] = _loads(rec.get("payload"))
    return rec


def enqueue_listener_tasks(source_chat_id, tasks, checkpoint=None, now=None,
                           chain="listen", origin="listen"):
    """把一批任务与 checkpoint **在同一个事务里**落盘（§13）。

    tasks: [{message_id, grouped_id, target_type, target_chat_id,
             download, payload}, …]
      * ``message_id`` 是**单元锚点**：相册取组内最小成员 id（这不是任务书的
        字面写法，而是 §8 唯一索引与 §16「保留 Album 整组转发」同时成立的唯一
        办法——按成员 id 建任务会让 Worker 逐条转发，收藏夹里相册被劈成 N 条
        散消息）。整组成员 id 放在 ``payload["member_ids"]``。
    checkpoint: 非 None 时随事务一起推进。调用方传「本轮最后一个已处理消息的
      id」；**没有入队的消息不能传进来**（否则那些消息被永久跳过，§30）。
    chain/origin: 写哪条链的 checkpoint（'listen'/'wl'）与任务来源标签
      （'listen'=事件链 / 'wl'=白名单扫描链）——claim 时 listen 优先，stats
      可按 origin 过滤；缺省即旧的事件链行为。

    返回与 tasks 等长的 id 列表，**重复任务（被唯一索引吃掉）位置为 None**。
    失败抛 DbUnavailable 且**整个事务回滚**——checkpoint 绝不先于任务落盘。
    """
    source_chat_id = int(source_chat_id)
    tasks = list(tasks or [])
    now = _now(now)

    def do(conn):
        ids = []
        for task in tasks:
            cur = _execute(
                conn,
                "INSERT OR IGNORE INTO listener_tasks "
                "(source_chat_id, message_id, grouped_id, target_type, "
                " target_chat_id, status, attempts, created_at, download, "
                " payload, origin) VALUES(?,?,?,?,?,?,0,?,?,?,?)",
                (source_chat_id, int(task["message_id"]),
                 task.get("grouped_id"), str(task["target_type"]),
                 (None if task.get("target_chat_id") is None
                  else int(task["target_chat_id"])),
                 STATUS_PENDING, now, 1 if task.get("download") else 0,
                 _dumps(task.get("payload")), str(origin)),
            )
            if cur.rowcount:
                task_id = cur.lastrowid
                _insert_event(conn, task_id, EV_RECEIVED, now,
                              {"target": str(task.get("target_type"))})
                ids.append(task_id)
            else:
                ids.append(None)
        if checkpoint is not None:
            _write_checkpoint(conn, source_chat_id, checkpoint, now,
                              chain=chain)
        return ids

    ids = _write(do, "入队监听任务")
    created = sum(1 for i in ids if i)
    dup = len(ids) - created
    logger.info(
        f"🗄 任务落盘：来源 {source_chat_id} | 新建 {created} 条"
        + (f" | 重复跳过 {dup} 条" if dup else "")
        + (f" | checkpoint → {checkpoint}" if checkpoint is not None else "")
    )
    return ids


def _insert_event(conn, task_id, event_type, now, payload=None):
    _execute(conn,
             "INSERT INTO task_events(task_id, event_type, created_at, payload)"
             " VALUES(?,?,?,?)",
             (int(task_id), str(event_type), int(now), _dumps(payload)))


def record_task_event(task_id, event_type, payload=None, now=None):
    """记一条任务事件；task_id 不存在（外键失败）返回 None。

    外键失败不抛异常：事件是**观察数据**，它写不进去绝不该把业务流程打断
    （与 stats.emit_event「写失败仅告警」同一纪律）。
    """
    now = _now(now)

    def do(conn):
        _insert_event(conn, task_id, event_type, now, payload)
        return True

    try:
        _write(do, f"记任务事件 {event_type}")
        return 1
    except DbUnavailable as e:
        logger.warning(f"🗄 记任务事件失败（不影响任务本身）：{e}")
        return None
    except sqlite3.IntegrityError:
        logger.warning(f"🗄 任务 {task_id} 不存在，事件 {event_type} 已丢弃")
        return None


# ============================================================
# 任务：领取 / 完成 / 重试 / 失败 / 取消
# ============================================================
def count_pending_listener_tasks(now=None):
    """队列存量（含在途 PROCESSING）：背压用。"""
    row = _read(lambda c: _execute(
        c, "SELECT COUNT(*) FROM listener_tasks WHERE status IN (?,?)",
        (STATUS_PENDING, STATUS_PROCESSING)).fetchone(),
        "统计待执行任务")
    return int(row[0])


def get_listener_task(task_id):
    row = _read(lambda c: _execute(
        c, "SELECT * FROM listener_tasks WHERE id=?", (int(task_id),)
    ).fetchone(), "读监听任务")
    return _row_to_task(row)


def list_listener_tasks(status=None, limit=200):
    """列任务（排查与统计用；按 id 升序，与领取顺序一致）。"""
    if status:
        rows = _read(lambda c: _execute(
            c, "SELECT * FROM listener_tasks WHERE status=? ORDER BY id "
               "LIMIT ?", (str(status), int(limit))).fetchall(),
            "列监听任务")
    else:
        rows = _read(lambda c: _execute(
            c, "SELECT * FROM listener_tasks ORDER BY id LIMIT ?",
            (int(limit),)).fetchall(), "列监听任务")
    return [_row_to_task(r) for r in rows]


def claim_listener_task(now=None, lease_seconds=None):
    """领一条可执行任务并落 PROCESSING + 租约；没有则返回 None（§23）。

    短事务：只在里面选一条 + 改状态，**Telegram API 调用绝不能进来**。
    可选范围：PENDING 且（无 next_retry_at 或已到期）。attempts 在这里 +1
    ——「尝试次数」的口径是「被领取执行的次数」，与下载队列一致。
    领取顺序：listen 优先于 wl（事件链兜实时，扫描链只补漏），同 origin
    内按 id 升序（FIFO），保证同一批消息按发现顺序处理。
    """
    now = _now(now)
    if lease_seconds is None:
        lease_seconds = int(config.LISTEN_WORKER_LEASE_SECONDS)
    lease_until = now + max(1, int(lease_seconds))

    def do(conn):
        row = _execute(
            conn,
            "SELECT * FROM listener_tasks WHERE status=? "
            "AND (next_retry_at IS NULL OR next_retry_at<=?) "
            "ORDER BY (origin='wl'), id LIMIT 1",
            (STATUS_PENDING, now),
        ).fetchone()
        if row is None:
            return None
        task_id = row["id"]
        # 带上状态条件：并发下（将来放开并发）抢到的第二个 UPDATE 会不匹配行，
        # rowcount=0 就当没领到，绝不出现两条执行流跑同一条任务。
        cur = _execute(
            conn,
            "UPDATE listener_tasks SET status=?, started_at=?, lease_until=?, "
            "attempts=attempts+1 WHERE id=? AND status=?",
            (STATUS_PROCESSING, now, lease_until, task_id, STATUS_PENDING),
        )
        if not cur.rowcount:
            return None
        _insert_event(conn, task_id, EV_RUNNING, now,
                      {"lease_until": lease_until,
                       "attempts": int(row["attempts"]) + 1})
        return task_id

    task_id = _write(do, "领取监听任务")
    if task_id is None:
        return None
    task = get_listener_task(task_id)
    logger.info(
        f"🗄 领取任务 #{task_id}：来源 {task['source_chat_id']} "
        f"消息 {task['message_id']} → {_target_label(task)} "
        f"（第 {task['attempts']} 次尝试，租约至 {lease_until}）"
    )
    return task


def _target_label(task) -> str:
    if task.get("target_type") == "saved_messages":
        return "收藏夹"
    return f"chat {task.get('target_chat_id')}"


def complete_listener_task(task_id, now=None, detail=None):
    """任务成功：SUCCESS + completed_at（终态，不再被领取）。

    接受 PROCESSING（转发+入队一气呵成）与 FORWARDED（P0-2 对账器补建
    完成入口队）两种来路。
    """
    now = _now(now)

    def do(conn):
        cur = _execute(
            conn,
            "UPDATE listener_tasks SET status=?, completed_at=?, "
            "lease_until=NULL, last_error=NULL WHERE id=? AND status IN (?,?)",
            (STATUS_SUCCESS, now, int(task_id),
             STATUS_PROCESSING, STATUS_FORWARDED),
        )
        if cur.rowcount:
            _insert_event(conn, task_id, EV_SUCCESS, now, detail)
        return bool(cur.rowcount)

    ok = _write(do, "标记任务成功")
    if ok:
        logger.info(f"🗄 任务 #{task_id} 完成（SUCCESS）")
    else:
        logger.warning(f"🗄 任务 #{task_id} 不在 PROCESSING，忽略完成标记")
    return ok


def retry_listener_task(task_id, next_retry_at, error=None, now=None):
    """临时错误：回到 PENDING + next_retry_at（同一行，不新增任务 §25）。"""
    now = _now(now)

    def do(conn):
        cur = _execute(
            conn,
            "UPDATE listener_tasks SET status=?, next_retry_at=?, "
            "lease_until=NULL, last_error=? WHERE id=?",
            (STATUS_PENDING, int(next_retry_at),
             (None if error is None else str(error)[:500]), int(task_id)),
        )
        if cur.rowcount:
            _insert_event(conn, task_id, EV_RETRY, now,
                          {"next_retry_at": int(next_retry_at),
                           "error": (None if error is None
                                     else str(error)[:200])})
        return bool(cur.rowcount)

    return _write(do, "任务转重试")


def fail_listener_task(task_id, error=None, now=None):
    """永久错误：FAILED（终态，等人看，不无限重试 §28）。"""
    now = _now(now)

    def do(conn):
        cur = _execute(
            conn,
            "UPDATE listener_tasks SET status=?, completed_at=?, "
            "lease_until=NULL, last_error=? WHERE id=?",
            (STATUS_FAILED, now, (None if error is None
                                  else str(error)[:500]), int(task_id)),
        )
        if cur.rowcount:
            _insert_event(conn, task_id, EV_FAILED, now,
                          {"error": (None if error is None
                                     else str(error)[:200])})
        return bool(cur.rowcount)

    ok = _write(do, "标记任务失败")
    if ok:
        logger.warning(f"🗄 任务 #{task_id} 永久失败（FAILED）：{error}")
    return ok


def cancel_listener_task(task_id, now=None):
    """取消任务（PENDING/PROCESSING 都能取消；终态是 no-op）。"""
    now = _now(now)

    def do(conn):
        cur = _execute(
            conn,
            "UPDATE listener_tasks SET status=?, completed_at=?, "
            "lease_until=NULL WHERE id=? AND status IN (?,?)",
            (STATUS_CANCELLED, now, int(task_id),
             STATUS_PENDING, STATUS_PROCESSING),
        )
        if cur.rowcount:
            _insert_event(conn, task_id, EV_CANCELLED, now)
        return bool(cur.rowcount)

    return _write(do, "取消监听任务")


def release_listener_task(task_id, now=None):
    """把手上的任务放回 PENDING（优雅停机用：不等租约到期）。

    回 PENDING 而不是 FAILED——任务根本没被判定成败，重启后立刻重跑。
    """
    def do(conn):
        cur = _execute(
            conn,
            "UPDATE listener_tasks SET status=?, lease_until=NULL "
            "WHERE id=? AND status=?",
            (STATUS_PENDING, int(task_id), STATUS_PROCESSING),
        )
        return bool(cur.rowcount)

    ok = _write(do, "释放任务")
    if ok:
        logger.info(f"🗄 任务 #{task_id} 已放回待执行（优雅停机）")
    return ok


def recover_expired_listener_tasks(now=None):
    """把租约过期的 PROCESSING 任务恢复成 PENDING，返回恢复条数（§24）。

    这是 Worker 崩溃后唯一的自愈手段，**启动时必须先跑一次**。
    记 LEASE_EXPIRED 事件（不是 RETRY——语义不同：前者是「没人写结果」，
    后者是「明确判定了失败」）。
    """
    now = _now(now)

    def do(conn):
        rows = _execute(
            conn,
            "SELECT id, source_chat_id, message_id FROM listener_tasks "
            "WHERE status=? AND lease_until IS NOT NULL AND lease_until<?",
            (STATUS_PROCESSING, now),
        ).fetchall()
        for row in rows:
            _execute(
                conn,
                "UPDATE listener_tasks SET status=?, lease_until=NULL, "
                "last_error=? WHERE id=?",
                (STATUS_PENDING, "lease expired", row["id"]),
            )
            _insert_event(conn, row["id"], EV_LEASE_EXPIRED, now,
                          {"recovered_at": now})
        return [dict(r) for r in rows]

    rows = _write(do, "恢复过期租约任务")
    if rows:
        detail = "、".join(
            f"#{r['id']}(来源 {r['source_chat_id']} 消息 {r['message_id']})"
            for r in rows[:5])
        logger.warning(
            f"🗄 检测到 {len(rows)} 条租约过期的在途任务（Worker 曾崩溃/被杀），"
            f"已恢复为待执行：{detail}"
            + (f" 等 {len(rows) - 5} 条" if len(rows) > 5 else "")
        )
    return len(rows)


def mark_listener_forwarded(task_id, copy_msg_ids, now=None):
    """转发成功后落持久化事实（P0-2）：status=FORWARDED + 副本 id 进 payload。

    copy_msg_ids 是**收藏夹里转发副本**的消息 id 列表（对账器凭它取回副本
    补建下载任务）。短事务；只允许 PROCESSING → FORWARDED。
    """
    now = _now(now)

    def do(conn):
        row = _execute(
            conn, "SELECT payload FROM listener_tasks WHERE id=?",
            (int(task_id),)).fetchone()
        if row is None:
            return False
        payload = _loads(row["payload"]) or {}
        payload["copy_msg_ids"] = [int(x) for x in (copy_msg_ids or []) if x]
        cur = _execute(
            conn,
            "UPDATE listener_tasks SET status=?, lease_until=NULL, payload=? "
            "WHERE id=? AND status=?",
            (STATUS_FORWARDED, _dumps(payload), int(task_id),
             STATUS_PROCESSING),
        )
        return bool(cur.rowcount)

    return _write(do, "标记任务已转发（待下载入队）")


def mark_listener_enqueue_retry(task_id, failed_ids, delay_seconds, now=None):
    """副本入队失败：留在 FORWARDED，安排对账器 delay 后补建（**不重新转发**）。

    failed_ids 记进 payload（排查用）；next_retry_at 复用既有列（对账器按它
    挑到期任务，与 retry 语义无关——任务并没有回 PENDING）。
    """
    now = _now(now)

    def do(conn):
        row = _execute(
            conn, "SELECT payload FROM listener_tasks WHERE id=?",
            (int(task_id),)).fetchone()
        if row is None:
            return False
        payload = _loads(row["payload"]) or {}
        payload["enqueue_failed_ids"] = [int(x) for x in (failed_ids or []) if x]
        cur = _execute(
            conn,
            "UPDATE listener_tasks SET next_retry_at=?, payload=? "
            "WHERE id=? AND status=?",
            (now + max(1, int(delay_seconds)), _dumps(payload),
             int(task_id), STATUS_FORWARDED),
        )
        return bool(cur.rowcount)

    return _write(do, "安排转发副本入队对账")


def list_listener_tasks_due_forwarded(now=None, limit=10):
    """列出对账到期的 FORWARDED 任务（next_retry_at 为空视作立即到期）。"""
    now = _now(now)
    rows = _read(lambda c: _execute(
        c,
        "SELECT * FROM listener_tasks WHERE status=? "
        "AND (next_retry_at IS NULL OR next_retry_at<=?) ORDER BY id LIMIT ?",
        (STATUS_FORWARDED, now, int(limit))).fetchall(),
        "列到期 FORWARDED 任务")
    return [_row_to_task(r) for r in rows]


# ============================================================
# Pawchive 扫描结果（schema v8）：帖子级生命周期 + 文件级进度。
# /paw plan 落库 → pawchive_worker 领取 → Chrome Agent 下载 → 终态。
# 命名与 listener 系列刻意分开（pawchive_* 前缀）：两套 ID 空间、两套
# 状态机，绝不共用 task_events 表。
# ============================================================
def _row_to_pawchive_post(row):
    if row is None:
        return None
    rec = dict(row)
    rec["ext_links"] = _loads(rec.get("ext_links")) or []
    return rec


def enqueue_pawchive_posts(service, creator_id, creator_name, posts,
                           scan_batch=None, now=None):
    """一批帖子（含附件直链与外链）落库；**已存在的帖子整体跳过**（幂等）。

    posts: [{post_id, title, published, post_url, subdir,
             files: [{url, filename}], ext_links: [{domain, url, text}]}]
    唯一索引 (service, creator_id, post_id) 吃掉重复：重复 plan 时已存在
    帖子的状态与文件进度原样保留，只有新帖子才进 PENDING。
    单事务：帖子与其文件要么一起进、要么都不进。返回 (created, skipped)。
    """
    service = str(service)
    creator_id = str(creator_id)
    now = _now(now)

    def do(conn):
        created = skipped = 0
        for post in posts:
            ext_links = post.get("ext_links") or []
            cur = _execute(
                conn,
                "INSERT OR IGNORE INTO pawchive_posts "
                "(service, creator_id, creator_name, post_id, title, published,"
                " post_url, subdir, status, ext_links, ext_count, created_at,"
                " scan_batch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (service, creator_id,
                 (None if creator_name is None else str(creator_name)),
                 str(post["post_id"]),
                 (None if post.get("title") is None else str(post["title"])),
                 (None if post.get("published") is None
                  else str(post["published"])),
                 (None if post.get("post_url") is None
                  else str(post["post_url"])),
                 (None if post.get("subdir") is None else str(post["subdir"])),
                 PAW_POST_PENDING, _dumps(ext_links), len(ext_links), now,
                 (None if scan_batch is None else str(scan_batch))),
            )
            if not cur.rowcount:
                skipped += 1
                continue
            created += 1
            post_row = cur.lastrowid
            for f in post.get("files") or []:
                if not f or not f.get("url"):
                    continue  # 站点侧未给 path 的附件本来就无法下载
                _execute(
                    conn,
                    "INSERT OR IGNORE INTO pawchive_files "
                    "(post_row, url, filename, status, updated_at) "
                    "VALUES(?,?,?,?,?)",
                    (post_row, str(f["url"]),
                     (None if f.get("filename") is None
                      else str(f["filename"])),
                     PAW_FILE_PENDING, now),
                )
        return created, skipped

    created, skipped = _write(do, "落库 Pawchive 扫描结果")
    if created or skipped:
        logger.info(
            f"🗄 Pawchive 扫描落库：{creator_name}（{service}/{creator_id}）"
            f" 新建 {created} 帖 | 已存在跳过 {skipped} 帖"
        )
    return created, skipped


def search_pawchive_posts(term, limit=10):
    """按关键词搜帖子（标题/作者，大小写不敏感子串匹配），新的在前。"""
    like = f"%{str(term or '').strip()}%"
    if like == "%%":
        return []
    rows = _read(lambda c: _execute(
        c,
        "SELECT * FROM pawchive_posts "
        "WHERE LOWER(title) LIKE LOWER(?) OR LOWER(creator_name) LIKE LOWER(?) "
        "ORDER BY id DESC LIMIT ?",
        (like, like, int(limit))).fetchall(),
        "按名称搜索 Pawchive 帖子")
    return [_row_to_pawchive_post(r) for r in rows]


def pawchive_known_post_ids(service, creator_id):
    """某创作者已入库的全部站点帖子 id（集合）——扫描提前停止的判定源。"""
    rows = _read(lambda c: _execute(
        c,
        "SELECT post_id FROM pawchive_posts "
        "WHERE service=? AND creator_id=?",
        (str(service), str(creator_id))).fetchall(),
        "查 Pawchive 已入库帖子 id")
    return {r["post_id"] for r in rows}


def pawchive_status_counts():
    """各状态帖子计数（/paw status 视图）。"""
    rows = _read(lambda c: _execute(
        c, "SELECT status, COUNT(*) FROM pawchive_posts GROUP BY status"
    ).fetchall(), "统计 Pawchive 帖子状态")
    return {r[0]: int(r[1]) for r in rows}


def get_pawchive_post(row_id):
    row = _read(lambda c: _execute(
        c, "SELECT * FROM pawchive_posts WHERE id=?", (int(row_id),)
    ).fetchone(), "读 Pawchive 帖子")
    return _row_to_pawchive_post(row)


def list_pawchive_posts(status=None, limit=100):
    """列帖子；status 给定时按 id 升序（领取顺序），否则按 id 降序（最新在前）。"""
    if status:
        rows = _read(lambda c: _execute(
            c, "SELECT * FROM pawchive_posts WHERE status=? ORDER BY id "
               "LIMIT ?", (str(status), int(limit))).fetchall(),
            "列 Pawchive 帖子")
    else:
        rows = _read(lambda c: _execute(
            c, "SELECT * FROM pawchive_posts ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall(), "列 Pawchive 帖子")
    return [_row_to_pawchive_post(r) for r in rows]


def find_pawchive_posts_by_post_id(post_id):
    """按站点帖子 ID 找已入库的帖子行（可能跨创作者多条，通常 0/1 条）。"""
    rows = _read(lambda c: _execute(
        c, "SELECT * FROM pawchive_posts WHERE post_id=? ORDER BY id",
        (str(post_id),)).fetchall(), "按帖子 ID 查 Pawchive 帖子")
    return [_row_to_pawchive_post(r) for r in rows]


def claim_next_pawchive_post(now=None, lease_seconds=None):
    """领一条 PENDING 帖子 → PROCESSING + 租约；没有则返回 None。

    短事务：只选一条 + 改状态，网络/Chrome 调用绝不能进来。
    attempts 口径与 listener 一致 = 被领取执行的次数。
    """
    now = _now(now)
    if lease_seconds is None:
        lease_seconds = int(config.PAWCHIVE_LEASE_SECONDS)
    lease_until = now + max(60, int(lease_seconds))

    def do(conn):
        row = _execute(
            conn,
            "SELECT id FROM pawchive_posts WHERE status=? "
            "AND (next_retry_at IS NULL OR next_retry_at<=?) "
            "ORDER BY id LIMIT 1",
            (PAW_POST_PENDING, now),
        ).fetchone()
        if row is None:
            return None
        # 带状态条件的 UPDATE：并发领取时输家 rowcount=0，绝不双跑。
        cur = _execute(
            conn,
            "UPDATE pawchive_posts SET status=?, started_at=?, lease_until=?, "
            "attempts=attempts+1 WHERE id=? AND status=?",
            (PAW_POST_PROCESSING, now, lease_until,
             row["id"], PAW_POST_PENDING),
        )
        if not cur.rowcount:
            return None
        return row["id"]

    post_row = _write(do, "领取 Pawchive 帖子")
    if post_row is None:
        return None
    post = get_pawchive_post(post_row)
    logger.info(
        f"🗄 Pawchive 领取帖子 #{post_row}：{post['creator_name']} "
        f"{post['post_id']}（第 {post['attempts']} 次尝试）"
    )
    return post


def renew_pawchive_lease(post_row, lease_seconds=None, now=None):
    """续租：单帖处理可能持续数小时（大视频在 Chrome 端串行），必须周期续。"""
    now = _now(now)
    if lease_seconds is None:
        lease_seconds = int(config.PAWCHIVE_LEASE_SECONDS)
    lease_until = now + max(60, int(lease_seconds))

    def do(conn):
        cur = _execute(
            conn,
            "UPDATE pawchive_posts SET lease_until=? WHERE id=? AND status=?",
            (lease_until, int(post_row), PAW_POST_PROCESSING),
        )
        return bool(cur.rowcount)

    return _write(do, "续 Pawchive 租约")


def get_pawchive_post_row(row_id):
    """按行 id 取单帖（找不到返回 None）。"""
    row = _read(lambda c: _execute(
        c, "SELECT * FROM pawchive_posts WHERE id=?", (int(row_id),)
    ).fetchone(), f"取 Pawchive 帖子（{row_id}）")
    return _row_to_pawchive_post(row) if row else None


def archive_pawchive_failed(now=None):
    """FAILED → ARCHIVED 批量归档（死链终态隔离）。

    **只归档死链帖**：帖内存在可恢复文件（PENDING，或 FAILED 且非死链）
    的帖子保持 FAILED 不动——归档会冻结它们的数据（2026-09-17 用户决策：
    归档不得埋掉可恢复数据）。返回 (归档帖数, 保留帖数)。
    """
    now = _now(now)

    def do(conn):
        rows = _execute(
            conn, "SELECT id FROM pawchive_posts WHERE status=?",
            (PAW_POST_FAILED,)).fetchall()
        archived = kept = 0
        for r in rows:
            pid = r["id"]
            recoverable = _execute(
                conn,
                "SELECT COUNT(*) FROM pawchive_files WHERE post_row=? AND ("
                "status=? OR (status=? AND (error IS NULL "
                "OR error NOT LIKE ?)))",
                (pid, PAW_FILE_PENDING, PAW_FILE_FAILED,
                 PAW_DEAD_LINK_MARK + "%"),
            ).fetchone()[0]
            if recoverable:
                kept += 1
                continue
            cur = _execute(
                conn,
                "UPDATE pawchive_posts SET status=?, lease_until=NULL, "
                "completed_at=COALESCE(completed_at, ?) WHERE id=? AND "
                "status=?",
                (PAW_POST_ARCHIVED, now, pid, PAW_POST_FAILED))
            if cur.rowcount:
                archived += 1
        return archived, kept

    return _write(do, "归档 Pawchive FAILED 帖（死链帖）")


def resurrect_archived_recoverable(now=None):
    """一次性补救：已归档帖里**误埋的可恢复文件**（PENDING/FAILED 非死链）
    所在帖子 → 重投 PENDING（连同其全部非死链文件）；纯死链帖保持 ARCHIVED。

    死链文件不重投（重试也是 404）。返回 (重投帖数, 重投文件数)。
    """
    now = _now(now)

    def do(conn):
        rows = _execute(
            conn,
            "SELECT DISTINCT p.id FROM pawchive_posts p JOIN pawchive_files f "
            "ON f.post_row=p.id WHERE p.status=? AND f.status IN (?,?) AND "
            "(f.error IS NULL OR f.error NOT LIKE ?)",
            (PAW_POST_ARCHIVED, PAW_FILE_PENDING, PAW_FILE_FAILED,
             PAW_DEAD_LINK_MARK + "%"),
        ).fetchall()
        posts_requeued = files_requeued = 0
        for r in rows:
            pid = r["id"]
            cur = _execute(
                conn,
                "UPDATE pawchive_posts SET status=?, completed_at=NULL, "
                "last_error=? WHERE id=? AND status=?",
                (PAW_POST_PENDING, "重新投放误归档的可恢复文件",
                 pid, PAW_POST_ARCHIVED),
            )
            if not cur.rowcount:
                continue
            posts_requeued += 1
            cur2 = _execute(
                conn,
                "UPDATE pawchive_files SET status=?, chrome_task_id=NULL, "
                "updated_at=? WHERE post_row=? AND status IN (?,?) AND "
                "(error IS NULL OR error NOT LIKE ?)",
                (PAW_FILE_PENDING, now, pid,
                 PAW_FILE_PENDING, PAW_FILE_FAILED,
                 PAW_DEAD_LINK_MARK + "%"),
            )
            files_requeued += cur2.rowcount or 0
        return posts_requeued, files_requeued

    return _write(do, "重投已归档帖中的可恢复文件")


def delete_pawchive_post(post_row):
    """删除 Pawchive 帖及其全部附件行（🗑 按钮；不可逆操作）。"""
    def do(conn):
        _execute(conn, "DELETE FROM pawchive_files WHERE post_row=?",
                 (int(post_row),))
        cur = _execute(conn, "DELETE FROM pawchive_posts WHERE id=?",
                       (int(post_row),))
        return cur.rowcount > 0
    return _write(do, f"删除 Pawchive 帖（{post_row}）")


def complete_pawchive_manual_post(post_row, now=None):
    """外链人工处理完成：MANUAL → COMPLETED。

    只允许从 MANUAL 流转（PENDING/PROCESSING 不能跳过下载直接标完成）；
    已是 COMPLETED 返回 False（幂等友好）。返回是否发生流转。"""
    def do(conn):
        cur = _execute(
            conn,
            "UPDATE pawchive_posts SET status=?, completed_at=?, "
            "lease_until=NULL WHERE id=? AND status=?",
            (PAW_POST_COMPLETED, _now(now), int(post_row), PAW_POST_MANUAL))
        return cur.rowcount > 0
    return _write(do, f"标记 Pawchive 外链帖完成（{post_row}）")


def list_pawchive_files(post_row):
    rows = _read(lambda c: _execute(
        c, "SELECT * FROM pawchive_files WHERE post_row=? ORDER BY id",
        (int(post_row),)).fetchall(), "列 Pawchive 文件")
    return [dict(r) for r in rows]


def mark_pawchive_file_submitted(file_id, chrome_task_id, now=None):
    now = _now(now)

    def do(conn):
        cur = _execute(
            conn,
            "UPDATE pawchive_files SET status=?, chrome_task_id=?, "
            "attempts=attempts+1, error=NULL, updated_at=? WHERE id=?",
            (PAW_FILE_SUBMITTED, str(chrome_task_id), now, int(file_id)),
        )
        return bool(cur.rowcount)

    return _write(do, "标记 Pawchive 文件已提交")


def mark_pawchive_file_done(file_id, size_bytes=None, now=None):
    now = _now(now)

    def do(conn):
        cur = _execute(
            conn,
            "UPDATE pawchive_files SET status=?, size_bytes=?, error=NULL, "
            "updated_at=? WHERE id=?",
            (PAW_FILE_DONE,
             (None if size_bytes is None else int(size_bytes)),
             now, int(file_id)),
        )
        return bool(cur.rowcount)

    return _write(do, "标记 Pawchive 文件完成")


# ============================================================
# 失败明细（/paw fail，2026-09-25）
# ============================================================
def non_dead_failed_posts(limit=20):
    """有**非死链**失败文件的 FAILED 帖（可救的失败），按可救性排序。

    每行 {id, creator_name, title, attempts, done, errors}：errors =
    [(错误摘要, 数量)]（死链错误不进），done = 同帖已完成文件数。
    死链（error 以 PAW_DEAD_LINK_MARK 开头）不占版面——重投它们无意义，
    /paw fail 只关心救得回来的。"""

    def do(conn):
        posts = _execute(
            conn,
            "SELECT p.id, p.creator_name, p.title, p.attempts FROM "
            "pawchive_posts p WHERE p.status = ? ORDER BY p.id DESC",
            (PAW_POST_FAILED,)).fetchall()
        out = []
        for p in posts:
            files = _execute(
                conn, "SELECT status, error FROM pawchive_files "
                "WHERE post_row=?", (p["id"],)).fetchall()
            errors = collections.Counter(
                (r["error"] or "（无错误信息）")[:60] for r in files
                if r["status"] == PAW_FILE_FAILED
                and not (r["error"] or "").startswith(PAW_DEAD_LINK_MARK))
            if not errors:
                continue
            done = sum(1 for r in files if r["status"] == PAW_FILE_DONE)
            out.append({
                "id": p["id"], "creator_name": p["creator_name"],
                "title": p["title"], "attempts": p["attempts"],
                "done": done,
                "errors": errors.most_common(3),
            })
            if len(out) >= int(limit):
                break
        return out

    return _read(do, "统计非死链失败帖")


def failed_file_split():
    """失败文件两分：(死链数, 非死链数)。/paw fail 的总览行。"""

    def do(conn):
        rows = _execute(
            conn, "SELECT error, COUNT(*) FROM pawchive_files "
            "WHERE status=? GROUP BY error", (PAW_FILE_FAILED,)).fetchall()
        dead = sum(n for err, n in rows
                   if (err or "").startswith(PAW_DEAD_LINK_MARK))
        total = sum(n for _e, n in rows)
        return dead, total - dead

    return _read(do, "两分失败文件")


# ============================================================
# 功能使用审计（schema v11）
# ============================================================
def feature_usage_bump(name, now=None):
    """功能使用计数 +1（按「功能名 × 本地日期」聚合）。

    低频写（人手触发），每事件一次小事务；DB 失败由 _write 记日志不抛。"""
    now = _now(now)
    day = time.strftime("%Y-%m-%d", time.localtime(now))

    def do(conn):
        _execute(
            conn,
            "INSERT OR IGNORE INTO feature_usage "
            "(name, day, count, first_at, last_at) VALUES (?, ?, 0, ?, ?)",
            (name, day, now, now))
        _execute(
            conn,
            "UPDATE feature_usage SET count = count + 1, last_at = ? "
            "WHERE name = ? AND day = ?",
            (now, name, day))

    _write(do, "记录功能使用")


def feature_usage_top(limit=20, now=None):
    """使用排行：[{'name','total','recent7','first_day','last_at'}, ...]。

    total=累计次数；recent7=近 7 天次数；last_at=最近一次使用的 epoch。"""
    now = _now(now)
    cutoff_day = time.strftime("%Y-%m-%d", time.localtime(now - 7 * 86400))

    def do(conn):
        return _execute(
            conn,
            "SELECT name, SUM(count) AS total, "
            "SUM(CASE WHEN day >= ? THEN count ELSE 0 END) AS recent7, "
            "MIN(day) AS first_day, MAX(last_at) AS last_at "
            "FROM feature_usage GROUP BY name "
            "ORDER BY total DESC, name LIMIT ?",
            (cutoff_day, int(limit))).fetchall()

    rows = _read(do, "统计功能使用")
    return [{"name": r[0], "total": int(r[1]), "recent7": int(r[2]),
             "first_day": r[3], "last_at": int(r[4])} for r in rows]


def update_pawchive_file_url(file_id, url, now=None):
    """URL 自愈回写：老数据无扩展名 URL 被补出可用地址后更新存量行。"""
    now = _now(now)

    def do(conn):
        cur = _execute(
            conn, "UPDATE pawchive_files SET url=?, updated_at=? WHERE id=?",
            (url, now, int(file_id)))
        return cur.rowcount

    return _write(do, "更新 Pawchive 文件直链")


def mark_pawchive_file_failed(file_id, error=None, now=None):
    now = _now(now)

    def do(conn):
        cur = _execute(
            conn,
            "UPDATE pawchive_files SET status=?, error=?, updated_at=? "
            "WHERE id=?",
            (PAW_FILE_FAILED,
             (None if error is None else str(error)[:500]),
             now, int(file_id)),
        )
        return bool(cur.rowcount)

    return _write(do, "标记 Pawchive 文件失败")


def mark_pawchive_file_pending(file_id, now=None):
    """SUBMITTED/FAILED → PENDING（chrome task 失联时重投用）。"""
    now = _now(now)

    def do(conn):
        cur = _execute(
            conn,
            "UPDATE pawchive_files SET status=?, chrome_task_id=NULL, "
            "updated_at=? WHERE id=? AND status IN (?,?)",
            (PAW_FILE_PENDING, now, int(file_id),
             PAW_FILE_SUBMITTED, PAW_FILE_FAILED),
        )
        return bool(cur.rowcount)

    return _write(do, "Pawchive 文件重投")


def finalize_pawchive_post(post_row, status, error=None, now=None):
    """终态流转：PROCESSING → COMPLETED / MANUAL / FAILED。"""
    now = _now(now)
    if status not in PAW_POST_TERMINAL:
        raise ValueError(f"非法的 Pawchive 终态：{status}")

    def do(conn):
        cur = _execute(
            conn,
            "UPDATE pawchive_posts SET status=?, completed_at=?, "
            "lease_until=NULL, last_error=? WHERE id=? AND status=?",
            (str(status), now,
             (None if error is None else str(error)[:500]),
             int(post_row), PAW_POST_PROCESSING),
        )
        return bool(cur.rowcount)

    ok = _write(do, f"Pawchive 帖子终态 {status}")
    if ok:
        logger.info(f"🗄 Pawchive 帖子 #{post_row} → {status}")
    else:
        logger.warning(
            f"🗄 Pawchive 帖子 #{post_row} 不在 PROCESSING，忽略终态 {status}")
    return ok


def postpone_pawchive_post(post_row, next_retry_at, error=None, now=None):
    """暂时性失败：PROCESSING → PENDING + next_retry_at（退避重试）。

    与 listener 的 retry_listener_task 同语义——同一行回 PENDING，不新增任务。
    """
    now = _now(now)

    def do(conn):
        cur = _execute(
            conn,
            "UPDATE pawchive_posts SET status=?, next_retry_at=?, "
            "lease_until=NULL, last_error=? WHERE id=? AND status=?",
            (PAW_POST_PENDING, int(next_retry_at),
             (None if error is None else str(error)[:500]),
             int(post_row), PAW_POST_PROCESSING),
        )
        return bool(cur.rowcount)

    return _write(do, "Pawchive 帖子转退避重试")


def retry_pawchive_posts(row_ids=None, now=None):
    """FAILED → PENDING（/paw retry）：**只重投可恢复文件**。

    已确认的站点死链（error 以 PAW_DEAD_LINK_MARK 开头的 FAILED 文件）保持
    FAILED 不动——重试也是 404，重投它们只会空转一轮预检；纯死链帖子（一个
    可恢复文件都没有）整帖跳过、维持 FAILED。

    row_ids=None 表示处理全部失败帖子。返回 (重投帖子数, 跳过帖子数)。
    """
    now = _now(now)

    def do(conn):
        if row_ids is None:
            rows = _execute(
                conn, "SELECT id FROM pawchive_posts WHERE status=?",
                (PAW_POST_FAILED,)).fetchall()
            targets = [r["id"] for r in rows]
        else:
            targets = [int(x) for x in row_ids]
        requeued = skipped = 0
        for pid in targets:
            files = _execute(
                conn,
                "SELECT id, status, error FROM pawchive_files "
                "WHERE post_row=?",
                (pid,),
            ).fetchall()
            # 可恢复 = 失败但非死链；已处 PENDING 的文件（崩溃态）也算
            recoverable = [
                r["id"] for r in files
                if r["status"] == PAW_FILE_PENDING
                or (r["status"] == PAW_FILE_FAILED
                    and not (r["error"] or "").startswith(PAW_DEAD_LINK_MARK))
            ]
            if not files or not recoverable:
                # 纯死链帖 / 无文件帖：没有可恢复数据，跳过
                skipped += 1
                continue
            cur = _execute(
                conn,
                "UPDATE pawchive_posts SET status=?, next_retry_at=NULL, "
                "lease_until=NULL WHERE id=? AND status=?",
                (PAW_POST_PENDING, pid, PAW_POST_FAILED),
            )
            if not cur.rowcount:
                continue
            requeued += 1
            dead = len(files) - len(recoverable)
            marks = ",".join(str(i) for i in recoverable)
            _execute(
                conn,
                f"UPDATE pawchive_files SET status=?, chrome_task_id=NULL, "
                f"updated_at=? WHERE id IN ({marks})",
                (PAW_FILE_PENDING, now),
            )
            if dead:
                # 死链留在原地的事实写进帖子备注，/paw status 可见
                _execute(
                    conn,
                    "UPDATE pawchive_posts SET last_error=? WHERE id=?",
                    (f"重投时跳过 {dead} 个已确认死链（不再重试）", pid),
                )
        return requeued, skipped

    result = _write(do, "重投 Pawchive 失败帖子")
    if result[0]:
        logger.info(f"🗄 Pawchive 重投 {result[0]} 条失败帖子")
    return result


def classify_pawchive_failed():
    """失败帖画像：返回 (可重投帖数, 纯死链/无文件帖数)。

    「可重投」与 retry_pawchive_posts 的重投判定**同源**（帖内至少一个
    PENDING 或非死链 FAILED 文件）——画像里的数字就是 /paw retry all 会
    真正动的那批，两处永不出现口径打架。/paw status 展示用。
    """

    def do(conn):
        rows = _execute(
            conn, "SELECT id FROM pawchive_posts WHERE status=?",
            (PAW_POST_FAILED,)).fetchall()
        recoverable = dead = 0
        for pid in [r["id"] for r in rows]:
            files = _execute(
                conn, "SELECT status, error FROM pawchive_files "
                "WHERE post_row=?", (pid,)).fetchall()
            has = any(
                r["status"] == PAW_FILE_PENDING
                or (r["status"] == PAW_FILE_FAILED
                    and not (r["error"] or "").startswith(PAW_DEAD_LINK_MARK))
                for r in files)
            if files and has:
                recoverable += 1
            else:
                dead += 1
        return recoverable, dead

    return _read(do, "统计 Pawchive 失败帖画像")


def release_pawchive_post(post_row):
    """PROCESSING → PENDING（优雅停机）。

    文件状态原样保留：SUBMITTED 的文件由 worker 重领时先对账
    chrome_tasks.json——终态就地吸收，失联（task 被裁剪/不存在）才重投。
    """
    def do(conn):
        cur = _execute(
            conn,
            "UPDATE pawchive_posts SET status=?, lease_until=NULL "
            "WHERE id=? AND status=?",
            (PAW_POST_PENDING, int(post_row), PAW_POST_PROCESSING),
        )
        return bool(cur.rowcount)

    ok = _write(do, "释放 Pawchive 帖子")
    if ok:
        logger.info(f"🗄 Pawchive 帖子 #{post_row} 已放回待处理（优雅停机）")
    return ok


def recover_expired_pawchive_posts(now=None):
    """租约过期的 PROCESSING → PENDING（worker 崩溃自愈，启动时先跑一次）。"""
    now = _now(now)

    def do(conn):
        rows = _execute(
            conn,
            "SELECT id FROM pawchive_posts "
            "WHERE status=? AND lease_until IS NOT NULL AND lease_until<?",
            (PAW_POST_PROCESSING, now),
        ).fetchall()
        for row in rows:
            _execute(
                conn,
                "UPDATE pawchive_posts SET status=?, lease_until=NULL, "
                "last_error=? WHERE id=?",
                (PAW_POST_PENDING, "lease expired", row["id"]),
            )
        return [dict(r) for r in rows]

    rows = _write(do, "恢复过期租约的 Pawchive 帖子")
    if rows:
        logger.warning(
            f"🗄 检测到 {len(rows)} 条租约过期的 Pawchive 帖子"
            "（worker 曾崩溃/被杀），已恢复待处理"
        )
    return len(rows)


# ============================================================
# 事件与统计
# ============================================================
def get_task_events(task_id):
    rows = _read(lambda c: _execute(
        c, "SELECT * FROM task_events WHERE task_id=? ORDER BY id",
        (int(task_id),)).fetchall(), "读任务事件")
    out = []
    for row in rows:
        rec = dict(row)
        rec["payload"] = _loads(rec.get("payload"))
        out.append(rec)
    return out


def get_listener_stats(since=None, now=None, origin=None):
    """监听任务的状态分布（统计/对账用）。

    since = unix 秒，按 created_at 过滤；origin 过滤任务来源
    ('listen'/'wl'，None = 全部)——台账的 📡 标签监听 分节只算 listen，
    /wl 视图只算 wl。
    """
    where, params = [], []
    if since is not None:
        where.append("created_at>=?")
        params.append(int(since))
    if origin is not None:
        where.append("origin=?")
        params.append(str(origin))
    sql = "SELECT status, COUNT(*) AS n FROM listener_tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY status"
    rows = _read(lambda c: _execute(c, sql, tuple(params)).fetchall(),
                 "统计监听任务")
    # 键用小写（展示层友好），与状态常量一一对应
    key_of = {
        STATUS_PENDING: "pending",
        STATUS_PROCESSING: "processing",
        STATUS_SUCCESS: "success",
        STATUS_FAILED: "failed",
        STATUS_CANCELLED: "cancelled",
    }
    out = {v: 0 for v in key_of.values()}
    for row in rows:
        name = key_of.get(str(row["status"]), str(row["status"]).lower())
        out[name] = out.get(name, 0) + int(row["n"])
    out["total"] = sum(out.values())
    return out


def count_listener_tasks_for_chat(source_chat_id, origin=None):
    """某聊天某来源的待执行（PENDING+PROCESSING）任务数（/wl 视图用）。"""
    sql = ("SELECT COUNT(*) FROM listener_tasks WHERE source_chat_id=? "
           "AND status IN (?,?)")
    params = [int(source_chat_id), STATUS_PENDING, STATUS_PROCESSING]
    if origin is not None:
        sql += " AND origin=?"
        params.append(str(origin))
    row = _read(lambda c: _execute(c, sql, tuple(params)).fetchone(),
                "统计聊天待执行任务")
    return int(row[0])


# ============================================================
# /sql 诊断控制台（owner-only，直接作用于 runtime DB）
# ============================================================
# SQL 只在本模块（§33）——/sql 的执行口也在这里，命令层只做分发与渲染。
_SQL_CMD_RE = re.compile(r"^/sql(?:\s|$)", re.IGNORECASE)


def is_sql_command(text) -> bool:
    """/sql 开头的命令（含裸命令）；/sqlite 不算。清理白名单同用此判定。"""
    return bool(_SQL_CMD_RE.match(str(text or "").strip()))


def execute_user_sql(sql, max_rows=None):
    """执行 owner 输入的**一条** SQL，返回结果 dict（/sql 诊断控制台）。

    返回三种形态：
      {"kind": "rows", "columns": […], "rows": [[原生值…], …], "more": bool}
      {"kind": "done", "rowcount": N}         # N=-1 = 语句不报告影响行数
      {"kind": "error", "message": "…"}       # 语法/约束/多语句等——错误是
                                              # **结果**不是故障，原样给用户看
    单元格保留**原生值**（截断/None 形态是展示层 text.format_sql_result 的
    事）；行数由 max_rows 封顶。权限全放开（用户 2026-09-13 定）：写语句
    立即生效（isolation_level=None 的自动提交），无撤销；只有 ATTACH/DETACH
    拒绝——它们会逃出当前库文件，破坏「单进程单库」前提。执行是同步的、
    跑在事件循环里：正常诊断查询毫秒级；病态慢查询会卡循环，属接受的代价
    （帮助文本已提示）。
    """
    if max_rows is None:
        max_rows = int(config.SQL_CONSOLE_MAX_ROWS)
    text = str(sql or "").strip()
    if not text:
        return {"kind": "error", "message": "空语句"}
    head = text.split(None, 1)[0].strip(";(").lower()
    if head in ("attach", "detach"):
        return {"kind": "error",
                "message": "拒绝 ATTACH/DETACH：诊断控制台只作用于当前库"}
    conn = _conn()
    try:
        cur = conn.execute(text)
    except sqlite3.Warning as e:
        # 多语句拼接正是 sqlite3 用 Warning 拦的（"You can only execute one
        # statement at a time"）——转成人话，不让它冒成异常
        return {"kind": "error", "message": f"只允许一条语句：{e}"}
    except sqlite3.Error as e:
        return {"kind": "error", "message": f"{type(e).__name__}: {e}"}

    if cur.description is not None:
        columns = [d[0] for d in cur.description]
        rows = [list(row) for row in cur.fetchmany(max_rows)]
        more = cur.fetchone() is not None
        logger.info(f"🗄 /sql 查询：{text[:200]!r} → 显示 {len(rows)} 行")
        return {"kind": "rows", "columns": columns, "rows": rows,
                "more": more}
    rowcount = int(cur.rowcount)
    logger.info(f"🗄 /sql 执行：{text[:200]!r} → rowcount={rowcount}")
    return {"kind": "done", "rowcount": rowcount}


# ============================================================
# 评论跟进：关注列表（命中标签的帖子 → 之后按天跟进它的评论区）
# ============================================================
def add_listener_follow(channel_id, post_id, source_chat_id=None,
                        caption=None, post_date=None, source_name=None,
                        ttl_seconds=None, max_active=None, now=None):
    """把一条帖子加入关注列表；返回新记录 id，重复或已满返回 None。

    ``caption`` / ``post_date`` / ``source_name`` 是**建列表时的快照**：后面十
    几次检查直接用它们，不再回频道取原帖。原帖日后被编辑/删除，已定下的命名
    也不会漂移（与队列记录的 parent_* 快照同一条纪律）。

    上限按**活跃**条数算（失效的不占额度）；满了只记日志返回 None——这是背压，
    不是错误，Scanner 照常建它的下载任务。
    """
    now = _now(now)
    ttl = int(config.LISTEN_FOLLOW_TTL_SECONDS if ttl_seconds is None
              else ttl_seconds)
    cap = int(config.LISTEN_FOLLOW_MAX if max_active is None else max_active)

    def do(conn):
        active = int(_execute(
            conn, "SELECT COUNT(*) FROM listener_follows WHERE status=?",
            (FOLLOW_ACTIVE,)).fetchone()[0])
        if active >= cap:
            logger.warning(
                f"📡 关注列表已达上限 {cap}（当前 {active}），"
                f"帖子 {channel_id}/{post_id} 不再加入跟进"
            )
            return None
        cur = _execute(
            conn,
            "INSERT OR IGNORE INTO listener_follows "
            "(channel_id, post_id, source_chat_id, caption, post_date, "
            " source_name, status, created_at, expires_at, checks) "
            "VALUES(?,?,?,?,?,?,?,?,?,0)",
            (int(channel_id), int(post_id),
             None if source_chat_id is None else int(source_chat_id),
             caption, post_date, source_name,
             FOLLOW_ACTIVE, now, now + ttl))
        return cur.lastrowid if cur.rowcount else None

    return _write(do, "加入关注列表")


def list_due_follows(interval_seconds=None, limit=None, now=None):
    """取到期的活跃关注（从未检查过、或距上次检查已超过间隔）。"""
    now = _now(now)
    gap = int(config.LISTEN_FOLLOW_INTERVAL_SECONDS
              if interval_seconds is None else interval_seconds)
    sql = ("SELECT * FROM listener_follows WHERE status=? "
           "AND (last_checked_at IS NULL OR last_checked_at<=?) "
           "ORDER BY last_checked_at IS NOT NULL, last_checked_at, id")
    params = [FOLLOW_ACTIVE, now - gap]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return _read(lambda c: [dict(r) for r in _execute(c, sql, params).fetchall()],
                 "读到期关注")


def touch_listener_follow(follow_id, error=None, now=None):
    """记一次检查：checks+1、刷 last_checked_at、留最后一次错误文案。"""
    now = _now(now)
    return _write(lambda c: _execute(
        c, "UPDATE listener_follows SET last_checked_at=?, checks=checks+1, "
           "last_error=? WHERE id=?",
        (now, (str(error)[:500] if error else None), int(follow_id))),
        "更新关注检查时间")


def expire_listener_follows(now=None):
    """把过期的活跃关注**置为失效**（不删除——留下「到底等到没有」的证据）。

    返回置失效的条数（调用方要把它报进统计里，所以必须回 rowcount，不能回
    Cursor）。
    """
    now = _now(now)

    def do(conn):
        return _execute(
            conn,
            "UPDATE listener_follows SET status=? WHERE status=? AND expires_at<=?",
            (FOLLOW_EXPIRED, FOLLOW_ACTIVE, now)).rowcount

    return _write(do, "关注列表置失效")


def trim_expired_follows(keep=None):
    """裁剪失效记录（保留最新 keep 条），防表无限增长。返回删除条数。"""
    keep = int(config.LISTEN_FOLLOW_KEEP_EXPIRED if keep is None else keep)

    def do(conn):
        return _execute(
            conn,
            "DELETE FROM listener_follows WHERE status=? AND id NOT IN "
            "(SELECT id FROM listener_follows WHERE status=? "
            " ORDER BY id DESC LIMIT ?)",
            (FOLLOW_EXPIRED, FOLLOW_EXPIRED, keep)).rowcount

    return _write(do, "裁剪失效关注")


def list_listener_follows(status=None, limit=None):
    """列出关注记录（视图/排查用）。"""
    sql = "SELECT * FROM listener_follows"
    params = []
    if status is not None:
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return _read(lambda c: [dict(r) for r in _execute(c, sql, params).fetchall()],
                 "读关注列表")


def count_listener_follows(status=None):
    if status is None:
        row = _read(lambda c: _execute(
            c, "SELECT COUNT(*) FROM listener_follows").fetchone(), "统计关注")
    else:
        row = _read(lambda c: _execute(
            c, "SELECT COUNT(*) FROM listener_follows WHERE status=?",
            (status,)).fetchone(), "统计关注")
    return int(row[0])


# ============================================================
# 下载队列持久化（2026-09-13，任务书：下载队列 SQLite 化）
# ============================================================
# 内存字典（state.QUEUE）是唯一工作副本与读取面；本组函数只负责
# write-through 持久化与启动装载。全部单行短事务，复用 _write/_read 的
# BUSY 有限重试；DB 未初始化时抛 DbUnavailable——降级决策（回落 JSON）
# 在 queue.py，本层绝不静默吞错。
_QUEUE_FIXED_COLUMNS = ("id", "kind", "attempts", "next_retry_at")


def _queue_payload(record):
    """记录 → payload JSON：固定列之外的一切字段原样进 payload。"""
    payload = {k: v for k, v in record.items() if k not in _QUEUE_FIXED_COLUMNS}
    return _dumps(payload)


def _queue_row_to_record(row):
    """行 → 队列记录 dict：payload 展开平铺，列值覆盖 payload 同名字段，
    另带 __state/__seq 供装载方分桶排序（QUEUED→tasks / RETRY→retry）。
    payload 不是合法 JSON 或不是对象 → 抛 ValueError（queue_load_all 据此
    跳过坏行），绝不把坏行洗成一条空记录混进队列。"""
    raw = row["payload"]
    try:
        payload = json.loads(raw) if raw else {}
    except (TypeError, ValueError) as e:
        raise ValueError(f"payload 不是合法 JSON：{e}") from e
    if not isinstance(payload, dict):
        raise ValueError(f"payload 不是对象（{type(payload).__name__}）")
    rec = dict(payload)
    rec["id"] = row["id"]
    rec["kind"] = row["kind"]
    rec["attempts"] = int(row["attempts"] or 0)
    rec["next_retry_at"] = row["next_retry_at"]
    rec["__state"] = row["state"]
    rec["__seq"] = row["seq"]
    return rec


def queue_load_all():
    """全部队列行按 seq 排序返回；坏 payload 行跳过 + WARNING（不崩装载）。"""

    def read(conn):
        out = []
        for row in _execute(
                conn, "SELECT id, kind, state, seq, attempts, next_retry_at, "
                      "payload FROM download_tasks ORDER BY seq").fetchall():
            try:
                out.append(_queue_row_to_record(row))
            except Exception as e:   # payload 展开异常等：一行坏不拖垮全部
                logger.warning(
                    f"🗄 下载队列坏行已跳过（id={row['id']}）：{e}")
        return out

    return _read(read, "装载下载队列")


def _queue_max_seq(conn):
    row = _execute(conn, "SELECT MAX(seq) FROM download_tasks").fetchone()
    return int(row[0] or 0)


def queue_insert(record, state="QUEUED", now=None):
    """入队 write-through：seq = MAX(seq)+1（追加到该列表尾部）。"""
    def do(conn):
        _execute(conn,
                 "INSERT INTO download_tasks(id, kind, state, seq, attempts, "
                 "next_retry_at, enqueued_at, payload) "
                 "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                 (record["id"], record.get("kind"), state,
                  _queue_max_seq(conn) + 1, int(record.get("attempts", 0)),
                  record.get("next_retry_at"), _now(now),
                  _queue_payload(record)))
    _write(do, f"下载队列入库（{record.get('id', '?')[:8]}）")


def queue_move_to_retry(record_id, attempts, next_retry_at):
    """fail_to_retry 对应：转 RETRY、累加次数、记退避到期、seq 追加到 retry 尾。"""
    def do(conn):
        _execute(conn,
                 "UPDATE download_tasks SET state='RETRY', attempts=?, "
                 "next_retry_at=?, seq=? WHERE id=?",
                 (int(attempts), next_retry_at,
                  _queue_max_seq(conn) + 1, record_id))
    _write(do, f"下载队列转待重试（{record_id[:8]}）")


def queue_update_retry(record_id, attempts, next_retry_at):
    """retry_failed 对应：原位累加——只动 attempts/next_retry_at，**不动 seq**。"""
    _write(lambda conn: _execute(
        conn,
        "UPDATE download_tasks SET attempts=?, next_retry_at=? WHERE id=?",
        (int(attempts), next_retry_at, record_id)),
        f"下载队列重试累加（{record_id[:8]}）")


def queue_delete(record_id):
    """成功/移除对应：按 id 删行。行不存在不报错（幂等）。"""
    _write(lambda conn: _execute(
        conn, "DELETE FROM download_tasks WHERE id=?", (record_id,)),
        f"下载队列删行（{record_id[:8]}）")


def queue_count():
    """{"queued": n, "retry": m}（键小写，任务书 §5 契约）：启动日志与
    迁移导入的判定用。state 列存大写 'QUEUED'/'RETRY'，这里归一。"""
    def read(conn):
        counts = {"queued": 0, "retry": 0}
        for row in _execute(
                conn, "SELECT state, COUNT(*) FROM download_tasks "
                      "GROUP BY state").fetchall():
            key = str(row[0] or "").strip().lower()
            if key in counts:
                counts[key] = int(row[1])
        return counts

    return _read(read, "统计下载队列")


# ============================================================
# 下载任务事件流（2026-09-14，任务书：任务事件 SQLite 化 Phase 2）
# ============================================================
# stats.emit_event / stats.load_events 的 DB 后端。rec dict 形状是消费方
# （rebuild_stats 纯函数、reporter 通知分发）的契约：ts 渲染回与 JSONL 逐
# 字符一致的本地串、task_id 非空才有 "id" 键、payload 平铺、列值优先。
_EVENT_TS_FMT = "%Y-%m-%d %H:%M:%S"
_EVENT_REC_KEYS = ("ts", "ev", "id")


def _event_epoch_from_str(ts_str):
    """本地时间串 → epoch 秒；解析失败抛 ValueError（导入方跳过该行）。"""
    return int(time.mktime(time.strptime(str(ts_str).strip(), _EVENT_TS_FMT)))


def _event_ts_str(epoch):
    """epoch → 本地时间串（datetime.fromtimestamp，与旧 JSONL 同格式）。"""
    return datetime.fromtimestamp(int(epoch)).strftime(_EVENT_TS_FMT)


def _event_row_to_rec(row):
    """行 → rec dict（形状兼容的唯一定义点，任务书 §5.1）。"""
    try:
        payload = json.loads(row["payload"]) if row["payload"] else {}
    except (TypeError, ValueError) as e:
        raise ValueError(f"payload 不是合法 JSON：{e}") from e
    if not isinstance(payload, dict):
        raise ValueError(f"payload 不是对象（{type(payload).__name__}）")
    rec = dict(payload)
    rec["ts"] = _event_ts_str(row["ts"])
    rec["ev"] = row["ev"]
    if row["task_id"]:
        rec["id"] = row["task_id"]
    return rec


def download_event_insert(ev, task_id=None, payload=None, ts=None):
    """单行事件写入（emit_event 的 DB 后端）。ts=None 取当前时间。"""
    if ts is None:
        ts = int(time.time())
    _write(lambda conn: _execute(
        conn,
        "INSERT INTO download_events(ts, ev, task_id, payload) "
        "VALUES(?, ?, ?, ?)",
        (int(ts), ev, task_id,
         _dumps(payload) if payload else None)),
        f"任务事件入库（{ev}）")


def download_events_all():
    """全量事件按 id 升序 → rec dict 列表（load_events 的 DB 等价）。
    坏 payload 行跳过 + WARNING，不崩装载。"""
    def read(conn):
        out = []
        for row in _execute(
                conn, "SELECT id, ts, ev, task_id, payload "
                      "FROM download_events ORDER BY id").fetchall():
            try:
                out.append(_event_row_to_rec(row))
            except Exception as e:
                logger.warning(f"🗄 事件坏行已跳过（id={row['id']}）：{e}")
        return out

    return _read(read, "装载任务事件")


def download_events_since(last_id, limit=2000):
    """id > last_id 的增量事件按序返回（reporter 游标读取）。"""
    def read(conn):
        out = []
        for row in _execute(
                conn, "SELECT id, ts, ev, task_id, payload "
                      "FROM download_events WHERE id > ? "
                      "ORDER BY id LIMIT ?", (int(last_id), int(limit))
        ).fetchall():
            try:
                out.append(_event_row_to_rec(row))
            except Exception as e:
                logger.warning(f"🗄 事件坏行已跳过（id={row['id']}）：{e}")
        return out

    return _read(read, "增量读任务事件")


def download_events_count():
    return int(_read(lambda c: _execute(
        c, "SELECT COUNT(*) FROM download_events").fetchone()[0],
        "统计任务事件"))


def download_events_max_id():
    """当前最大 rowid（reporter 启动游标 = MAX(id)：历史事件不重放通知）。"""
    return int(_read(lambda c: _execute(
        c, "SELECT COALESCE(MAX(id), 0) FROM download_events").fetchone()[0],
        "读任务事件游标"))


def download_events_trim(keep):
    """保尾 keep 条（台账封顶），返回删除行数；rowid 单调性不受影响。"""
    def do(conn):
        cur = _execute(
            conn, "DELETE FROM download_events WHERE id NOT IN "
                  "(SELECT id FROM download_events ORDER BY id DESC LIMIT ?)",
            (int(keep),))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return _write(do, "裁剪任务事件")


# ============================================================
# 下载历史（2026-09-14，Phase 3，schema v6）
# ============================================================
# history.append_history/get_history_lines 的 DB 后端。渲染行
# `ts | 类型 | 文件名 | 大小 | 来源：xxx` 是消费方接口：列拆解只在
# 写入/读出边界发生，读出逐字符还原。不合 5 段格式的行进 raw 兜底。
_HISTORY_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _history_parse_record(record):
    """渲染行 → 列 dict；不是 5 段（或 ts 不合法）→ {"raw": 原文} 兜底。"""
    parts = str(record).split(" | ")
    if len(parts) == 5 and parts[4].startswith("来源："):
        try:
            ts = int(time.mktime(time.strptime(parts[0], _HISTORY_TS_FMT)))
        except ValueError:
            return {"raw": record}
        return {"ts": ts, "kind": parts[1], "filename": parts[2],
                "size_text": parts[3], "source": parts[4]}
    return {"raw": record}


def _history_row_to_line(row):
    if row["raw"]:
        return row["raw"]
    return " | ".join((_event_ts_str(row["ts"]), row["kind"],
                       row["filename"], row["size_text"], row["source"]))


def history_append_record(record):
    """一行历史入库（append_history 的 DB 后端；raw 兜底零丢失）。"""
    cols = _history_parse_record(record)
    _write(lambda conn: _execute(
        conn,
        "INSERT INTO download_history(ts, kind, filename, size_text, "
        "source, raw) VALUES(?, ?, ?, ?, ?, ?)",
        (cols.get("ts"), cols.get("kind"), cols.get("filename"),
         cols.get("size_text"), cols.get("source"), cols.get("raw"))),
        "下载历史入库")


def history_lines(n=None):
    """渲染行列表（get_history_lines 的 DB 等价）：n=None 全量按 id 升序；
    n=尾部 n 条（升序，与文件 tail 语义一致）。"""
    def read(conn):
        if n is None:
            rows = _execute(
                conn, "SELECT ts, kind, filename, size_text, source, raw "
                      "FROM download_history ORDER BY id").fetchall()
        else:
            rows = list(reversed(_execute(
                conn, "SELECT ts, kind, filename, size_text, source, raw "
                      "FROM download_history ORDER BY id DESC LIMIT ?",
                (int(n),)).fetchall()))
        return [_history_row_to_line(r) for r in rows]

    return _read(read, "读下载历史")


def history_count():
    return int(_read(lambda c: _execute(
        c, "SELECT COUNT(*) FROM download_history").fetchone()[0],
        "统计下载历史"))


# ============================================================
# 去重索引（2026-09-14，Phase 4，schema v7）
# ============================================================
# dedup.remember/load_index 的 DB 后端。三列忠实映射文件的
# `key\t日期\t文件名` 行（ts 保留 "26-09-13 21:02" 短格式原样，纯信息）；
# 同键重复行容忍（装载时 dict 后写胜）。
def dedup_index_append(key, ts, filename):
    """一行索引入库（remember 的 DB 后端）。"""
    _write(lambda conn: _execute(
        conn, "INSERT INTO dedup_index(key, ts, filename) VALUES(?, ?, ?)",
        (key, ts, filename)), f"去重索引入库（{str(key)[:24]}）")


def dedup_index_all():
    """全量行按 id 升序 → [{"id","key","ts","filename"}]（装载用）。"""
    def read(conn):
        return [dict(r) for r in _execute(
            conn, "SELECT id, key, ts, filename FROM dedup_index "
                  "ORDER BY id").fetchall()]

    return _read(read, "读去重索引")


def dedup_index_count():
    return int(_read(lambda c: _execute(
        c, "SELECT COUNT(*) FROM dedup_index").fetchone()[0],
        "统计去重索引"))


def dedup_index_trim(keep):
    """保尾 keep 行（启动裁剪），返回删除行数。"""
    def do(conn):
        cur = _execute(
            conn, "DELETE FROM dedup_index WHERE id NOT IN "
                  "(SELECT id FROM dedup_index ORDER BY id DESC LIMIT ?)",
            (int(keep),))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return _write(do, "裁剪去重索引")


# ============================================================
# 手动外链台账（2026-09-16，schema v9）
# ============================================================
# manual_links 模块的存储层：url_key UNIQUE 承担查重（重复登记在 INSERT
# 层被拒，调用方取回已有行报状态）；状态只有 PENDING/DONE 两态，终态
# 不删行——台账的历史就是查重数据源。
def manual_link_add(url, host=None, note=None, now=None):
    """登记一条链接（含备注）。返回 (state, row)。

    state ∈ {"new", "note_updated", "pending", "done"}：url_key UNIQUE 兜底
    查重；同 url_key 重发且带**不同**备注 → 更新备注记 "note_updated"（状态
    不变）；无备注或备注相同 → 原查重语义。"""
    key = str(url).strip()
    def do(conn):
        try:
            _execute(
                conn,
                "INSERT INTO manual_links(url, url_key, host, note, status, "
                "created_at) VALUES(?, ?, ?, ?, 'PENDING', ?)",
                (key, key, host, note, _now(now)))
            row = _execute(
                conn, "SELECT * FROM manual_links WHERE url_key=?",
                (key,)).fetchone()
            return ("new", dict(row))
        except sqlite3.IntegrityError:
            row = _execute(
                conn, "SELECT * FROM manual_links WHERE url_key=?",
                (key,)).fetchone()
            if note is not None and (row["note"] or "") != note:
                _execute(conn, "UPDATE manual_links SET note=? WHERE id=?",
                         (note, row["id"]))
                row = _execute(
                    conn, "SELECT * FROM manual_links WHERE url_key=?",
                    (key,)).fetchone()
                return ("note_updated", dict(row))
            return ("pending" if row["status"] == "PENDING" else "done",
                    dict(row))
    return _write(do, f"外链台账登记（{key[:40]}）")


def manual_link_done(link_id, now=None):
    """PENDING → DONE；已是 DONE 或不存在返回 False（幂等）。"""
    def do(conn):
        cur = _execute(
            conn,
            "UPDATE manual_links SET status='DONE', done_at=? "
            "WHERE id=? AND status='PENDING'",
            (_now(now), int(link_id)))
        return cur.rowcount > 0
    return _write(do, f"外链台账标记完成（{link_id}）")


def get_manual_link(link_id):
    """按行 id 取台账行（找不到返回 None）。"""
    row = _read(lambda c: _execute(
        c, "SELECT * FROM manual_links WHERE id=?", (int(link_id),)
    ).fetchone(), f"取外链台账行（{link_id}）")
    return dict(row) if row else None


def list_manual_links(status=None, limit=50):
    """台账行列表：status=None 全量按 id 降序（最新在前）。"""
    if status:
        rows = _read(lambda c: _execute(
            c, "SELECT * FROM manual_links WHERE status=? "
               "ORDER BY id DESC LIMIT ?", (str(status), int(limit))
        ).fetchall(), "列外链台账")
    else:
        rows = _read(lambda c: _execute(
            c, "SELECT * FROM manual_links ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall(), "列外链台账")
    return [dict(r) for r in rows]


def search_manual_links(keyword, limit=50):
    """按关键词搜台账（备注或 URL 子串，大小写不敏感），全部状态含终态。

    DONE 条目参与搜索——找回历史也是需求的一部分。"""
    like = f"%{str(keyword or '').strip()}%"
    rows = _read(lambda c: _execute(
        c, "SELECT * FROM manual_links WHERE note LIKE ? COLLATE NOCASE "
           "OR url LIKE ? COLLATE NOCASE ORDER BY id DESC LIMIT ?",
        (like, like, int(limit))).fetchall(), "搜索外链台账")
    return [dict(r) for r in rows]


def count_manual_links(status=None):
    if status:
        return int(_read(lambda c: _execute(
            c, "SELECT COUNT(*) FROM manual_links WHERE status=?",
            (str(status),)).fetchone()[0], "统计外链台账"))
    return int(_read(lambda c: _execute(
        c, "SELECT COUNT(*) FROM manual_links").fetchone()[0], "统计外链台账"))


def list_tables():
    """库内全部表名（sqlite_master，按名排序）——/help2 数据字典用。"""
    return [r[0] for r in _read(
        lambda c: _execute(
            c, "SELECT name FROM sqlite_master WHERE type='table' "
               "ORDER BY name").fetchall(), "列数据库表")]
