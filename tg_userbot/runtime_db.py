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
import json
import os
import sqlite3
import time

from . import config
from .log import logger

# 任务状态（规格 §25）。重试**不新增状态**：回到 PENDING + next_retry_at。
STATUS_PENDING = "PENDING"
STATUS_PROCESSING = "PROCESSING"
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"
STATUS_CANCELLED = "CANCELLED"
TERMINAL_STATUSES = (STATUS_SUCCESS, STATUS_FAILED, STATUS_CANCELLED)

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
    """
    CREATE TABLE IF NOT EXISTS listener_checkpoints (
        source_chat_id INTEGER PRIMARY KEY,
        last_message_id INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
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
        payload TEXT
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
    if version != target:
        set_schema_meta("schema_version", target)
        logger.info(f"🗄 Runtime DB schema 版本：{version or '（无）'} → {target}")
    return target


# ============================================================
# checkpoint
# ============================================================
def get_listener_checkpoint(source_chat_id):
    """取某个监听来源的 checkpoint；从未设过返回 None。

    None 与 0 语义不同：None = 还没建立过（首次添加监听要走「取当前最新 id」
    的初始化路径），0 = 明确的「从头开始」。上层绝不能把 None 当成 0 用。
    """
    row = _read(lambda c: _execute(
        c, "SELECT last_message_id FROM listener_checkpoints "
           "WHERE source_chat_id=?", (int(source_chat_id),)).fetchone(),
        "读监听 checkpoint")
    return int(row[0]) if row else None


def _write_checkpoint(conn, source_chat_id, last_message_id, now):
    """写 checkpoint（独立函数：既是事务内的一个步骤，也是测试的注入点）。"""
    _execute(conn,
             "INSERT INTO listener_checkpoints"
             "(source_chat_id, last_message_id, updated_at) VALUES(?,?,?) "
             "ON CONFLICT(source_chat_id) DO UPDATE SET "
             "last_message_id=excluded.last_message_id, "
             "updated_at=excluded.updated_at",
             (int(source_chat_id), int(last_message_id), int(now)))


def set_listener_checkpoint(source_chat_id, last_message_id, now=None):
    """单独写 checkpoint（初始化、人工重置用；扫描路径请用原子版本）。"""
    now = _now(now)
    _write(lambda conn: _write_checkpoint(
        conn, source_chat_id, last_message_id, now), "写监听 checkpoint")
    logger.info(f"🗄 checkpoint 已写入：{source_chat_id} → {last_message_id}")
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


def enqueue_listener_tasks(source_chat_id, tasks, checkpoint=None, now=None):
    """把一批任务与 checkpoint **在同一个事务里**落盘（§13）。

    tasks: [{message_id, grouped_id, target_type, target_chat_id,
             download, payload}, …]
      * ``message_id`` 是**单元锚点**：相册取组内最小成员 id（这不是任务书的
        字面写法，而是 §8 唯一索引与 §16「保留 Album 整组转发」同时成立的唯一
        办法——按成员 id 建任务会让 Worker 逐条转发，收藏夹里相册被劈成 N 条
        散消息）。整组成员 id 放在 ``payload["member_ids"]``。
    checkpoint: 非 None 时随事务一起推进。调用方传「本轮最后一个已处理消息的
      id」；**没有入队的消息不能传进来**（否则那些消息被永久跳过，§30）。

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
                " payload) VALUES(?,?,?,?,?,?,0,?,?,?)",
                (source_chat_id, int(task["message_id"]),
                 task.get("grouped_id"), str(task["target_type"]),
                 (None if task.get("target_chat_id") is None
                  else int(task["target_chat_id"])),
                 STATUS_PENDING, now, 1 if task.get("download") else 0,
                 _dumps(task.get("payload"))),
            )
            if cur.rowcount:
                task_id = cur.lastrowid
                _insert_event(conn, task_id, EV_RECEIVED, now,
                              {"target": str(task.get("target_type"))})
                ids.append(task_id)
            else:
                ids.append(None)
        if checkpoint is not None:
            _write_checkpoint(conn, source_chat_id, checkpoint, now)
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
    领取按 id 升序（FIFO），保证同一批消息按发现顺序处理。
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
            "ORDER BY id LIMIT 1",
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
    """任务成功：SUCCESS + completed_at（终态，不再被领取）。"""
    now = _now(now)

    def do(conn):
        cur = _execute(
            conn,
            "UPDATE listener_tasks SET status=?, completed_at=?, "
            "lease_until=NULL, last_error=NULL WHERE id=? AND status=?",
            (STATUS_SUCCESS, now, int(task_id), STATUS_PROCESSING),
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


def get_listener_stats(since=None, now=None):
    """监听任务的状态分布（统计/对账用）。since = unix 秒，按 created_at 过滤。"""
    if since is None:
        rows = _read(lambda c: _execute(
            c, "SELECT status, COUNT(*) AS n FROM listener_tasks "
               "GROUP BY status").fetchall(), "统计监听任务")
    else:
        rows = _read(lambda c: _execute(
            c, "SELECT status, COUNT(*) AS n FROM listener_tasks "
               "WHERE created_at>=? GROUP BY status",
            (int(since),)).fetchall(), "统计监听任务")
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
