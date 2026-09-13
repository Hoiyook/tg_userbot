# 白名单双通道扫描制改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把下载白名单从「事件监听直转」改成「事件生产者 + 扫描生产者 → SQLite 任务 → Worker 统一转发」的双通道机制，支持 `/wl since` 按消息 id 回补停机漏掉的存量。

**Architecture:** 两条生产链（事件实时 / 扫描补漏）写同一张 `listener_tasks`（origin='wl'），唯一索引结构性防双转发；转发由现有 `listener_worker` 受控执行。Runtime DB 升 v3：checkpoint 加 `chain` 列（listen/wl 游标独立）、任务加 `origin` 列。

**Tech Stack:** Python stdlib（unittest/asyncio/sqlite3）+ Telethon。无新依赖。

**Spec:** `docs/plan/下载白名单双通道扫描制改造_设计规格.md`（本计划逐节实现它，两者一起读）。

## Global Constraints

- 测试命令（仓库根目录）：`.venv/bin/python -m unittest discover -s tests -p "test_*.py" -v`；单文件：`.venv/bin/python -m unittest tests.test_runtime_db -v`。stdlib unittest，**测试绝不联网**（FakeClient / mock.patch 注入）。
- **事件循环规则**：loop 绑定原语（client/lock/Event）只在 `app.main()` 创建；模块顶层绝不建。
- **SQL 只在 `runtime_db.py`**；Telegram API 绝不进事务；import 期绝不建连接。
- 所有用户可见文本**中文**；Termux 平台默认值不动。
- 写盘一律 temp + `os.replace` 原子；日志/注释中文、密度对齐周边代码。
- 每个任务结束跑全量测试再 commit（信息格式沿用仓库惯例：`功能：…` / `测试：…` / `文档：…`）。

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `tg_userbot/config.py` | 改 | `RUNTIME_DB_SCHEMA_VERSION=3` + `WHITELIST_SCAN_*` 常量 |
| `tg_userbot/runtime_db.py` | 改 | schema v3 迁移、checkpoint 带 chain、任务带 origin、claim listen 优先、stats 按 origin 过滤 |
| `tg_userbot/listener.py` | 改 | 公开 `fetch_new_messages` / `fetch_newest_id` / `build_saved_messages_task` 三个复用入口 |
| `tg_userbot/wl_scan.py` | 新建 | 白名单扫描生产者 + 两条生产链共用的 dedup 前置/任务构建/回补/视图助手 |
| `tg_userbot/listener_worker.py` | 改 | `src=origin`；来源禁转回退直下 |
| `tg_userbot/state.py` | 改 | `WL_INPUT_UNTIL` / `WL_LAST_SCAN` |
| `tg_userbot/text.py` | 改 | `wl_list_text` 加扫描信息行 |
| `tg_userbot/commands.py` | 改 | `/wl since`、`/wl scan` 分发 |
| `tg_userbot/bot.py` | 改 | 菜单「⏪ 回补」「🔄 立即扫描」+ 输入窗口 |
| `tg_userbot/menu.py` | 改 | `wl_menu_buttons` 加按钮 |
| `tg_userbot/app.py` | 改 | `record_whitelist_media` 事件生产者 + `_whitelist_scan_loop` + 删除直转代码 |
| `tg_userbot/platform.py` | 改 | 模块 docstring 的链路描述更新 |
| `tests/test_runtime_db.py` | 改 | v3 迁移/chain/origin/claim/stats 用例 |
| `tests/test_wl_scan.py` | 新建 | 扫描生产者全用例 |
| `tests/test_listener_worker.py` | 改 | src/禁转回退用例 |
| `tests/test_whitelist.py` `tests/test_commands.py` `tests/test_menu.py` | 改 | 命令/视图/菜单用例 |
| `tests/test_wl_event.py` | 新建 | 事件生产者用例 |
| `CLAUDE.md` | 改 | 白名单段/模块索引/配置指引同步 |

---

### Task 1: config 常量 + Runtime DB schema v3

**Files:**
- Modify: `tg_userbot/config.py:358`（`RUNTIME_DB_SCHEMA_VERSION`）及 LISTEN 常量区之后
- Modify: `tg_userbot/runtime_db.py`（`_SCHEMA`、`migrate()`、checkpoint 三函数、`enqueue_listener_tasks`、`claim_listener_task`、`get_listener_stats`、新增 `count_listener_tasks_for_chat`）
- Test: `tests/test_runtime_db.py`（追加两个测试类）

**Interfaces:**
- Consumes: 无（首个任务）。
- Produces（后续任务依赖的精确签名）:
  - `config.WHITELIST_SCAN_INTERVAL_SECONDS = 300`、`config.WHITELIST_SCAN_PAGES_PER_ROUND = 10`、`config.WHITELIST_SCAN_PAGE_SLEEP_SECONDS = 5.0`、`config.RUNTIME_DB_SCHEMA_VERSION = 3`
  - `runtime_db.get_listener_checkpoint(source_chat_id, chain="listen") -> int | None`
  - `runtime_db.set_listener_checkpoint(source_chat_id, last_message_id, now=None, chain="listen") -> True`
  - `runtime_db.enqueue_listener_tasks(source_chat_id, tasks, checkpoint=None, now=None, chain="listen", origin="listen") -> list[int|None]`
  - `runtime_db.claim_listener_task()`（listen 任务优先于 wl，同 origin 内按 id）
  - `runtime_db.get_listener_stats(since=None, now=None, origin=None) -> dict`
  - `runtime_db.count_listener_tasks_for_chat(source_chat_id, origin=None) -> int`（PENDING+PROCESSING）

- [ ] **Step 1: 写失败测试（追加到 tests/test_runtime_db.py 末尾）**

```python
# ============================================================
# v3 迁移：checkpoint 加 chain、任务加 origin（白名单双通道，2026-09-13）
# ============================================================
class V3MigrationTest(unittest.TestCase):
    """v2 旧库 → v3：chain 列 + origin 列；旧行归 listen；幂等可重跑。"""

    def setUp(self):
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.path = os.path.join(_TMP, "v3_migrate.db")
        if os.path.exists(self.path):
            os.remove(self.path)
        conn = sqlite3.connect(self.path)
        conn.executescript("""
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE listener_checkpoints (
                source_chat_id INTEGER PRIMARY KEY,
                last_message_id INTEGER NOT NULL,
                updated_at INTEGER NOT NULL);
            CREATE TABLE listener_tasks (
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
                payload TEXT);
            INSERT INTO schema_meta VALUES('schema_version', '2');
            INSERT INTO listener_checkpoints VALUES(111, 500, 1000);
        """)
        conn.commit()
        conn.close()

    def test_v2_migrates_to_v3(self):
        self.assertTrue(runtime_db.init_db(self.path))
        self.assertEqual(runtime_db.get_schema_version(), 3)
        # 旧行归 listen 链；wl 链无游标
        self.assertEqual(
            runtime_db.get_listener_checkpoint(111, chain="listen"), 500)
        self.assertIsNone(runtime_db.get_listener_checkpoint(111, chain="wl"))
        # origin 列生效：新插入的行默认 listen
        ids = runtime_db.enqueue_listener_tasks(111, [_task(message_id=1)])
        self.assertTrue(ids[0])
        self.assertEqual(runtime_db.get_listener_task(ids[0])["origin"],
                         "listen")

    def test_migration_idempotent(self):
        self.assertTrue(runtime_db.init_db(self.path))
        self.assertTrue(runtime_db.init_db(self.path))
        self.assertEqual(runtime_db.get_schema_version(), 3)
        self.assertEqual(
            runtime_db.get_listener_checkpoint(111, chain="listen"), 500)


class ChainAndOriginTest(unittest.TestCase):
    """chain 游标互相独立；claim listen 优先；stats 按 origin 过滤。"""

    def setUp(self):
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())

    def test_chain_isolation(self):
        runtime_db.set_listener_checkpoint(111, 500, chain="listen")
        runtime_db.set_listener_checkpoint(111, 900, chain="wl")
        self.assertEqual(
            runtime_db.get_listener_checkpoint(111, chain="listen"), 500)
        self.assertEqual(
            runtime_db.get_listener_checkpoint(111, chain="wl"), 900)
        # 回补只动 wl 游标，不波及 listen
        runtime_db.set_listener_checkpoint(111, 501, chain="listen")
        self.assertEqual(
            runtime_db.get_listener_checkpoint(111, chain="wl"), 900)

    def test_claim_prefers_listen_over_wl(self):
        ids_wl = runtime_db.enqueue_listener_tasks(
            SRC, [_task(message_id=1)], origin="wl")
        ids_listen = runtime_db.enqueue_listener_tasks(
            SRC, [_task(message_id=2)], origin="listen")
        task = runtime_db.claim_listener_task()
        self.assertEqual(task["id"], ids_listen[0],
                         "listen 任务优先，尽管 id 更大")
        runtime_db.complete_listener_task(task["id"])
        task2 = runtime_db.claim_listener_task()
        self.assertEqual(task2["id"], ids_wl[0])

    def test_stats_origin_filter(self):
        runtime_db.enqueue_listener_tasks(SRC, [_task(message_id=1)],
                                          origin="wl")
        runtime_db.enqueue_listener_tasks(SRC, [_task(message_id=2)])
        self.assertEqual(runtime_db.get_listener_stats(origin="wl")["total"], 1)
        self.assertEqual(
            runtime_db.get_listener_stats(origin="listen")["total"], 1)
        self.assertEqual(runtime_db.get_listener_stats()["total"], 2)

    def test_count_pending_for_chat(self):
        runtime_db.enqueue_listener_tasks(SRC, [_task(message_id=1)],
                                          origin="wl")
        runtime_db.enqueue_listener_tasks(CHAT_A, [_task(message_id=2)],
                                          origin="wl")
        runtime_db.enqueue_listener_tasks(SRC, [_task(message_id=3)])
        self.assertEqual(
            runtime_db.count_listener_tasks_for_chat(SRC, origin="wl"), 1)
        self.assertEqual(
            runtime_db.count_listener_tasks_for_chat(SRC), 2)
        self.assertEqual(
            runtime_db.count_listener_tasks_for_chat(CHAT_B, origin="wl"), 0)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m unittest tests.test_runtime_db -v`
Expected: FAIL —— `V3MigrationTest` 报 `get_listener_checkpoint() ... unexpected keyword 'chain'`（旧签名无 chain/origin）。

- [ ] **Step 3: 改 config.py**

`config.py:358` 改为：

```python
RUNTIME_DB_SCHEMA_VERSION = 3   # v3：checkpoints 加 chain（listen/wl 双游标）+ tasks 加 origin
```

在 LISTEN_* 常量区之后（`LISTEN_FLOODWAIT_MAX_WAIT_SECONDS` 之后）新增：

```python
# ── 白名单扫描生产者（下载白名单的停机补漏链，2026-09-13）──
# 白名单改成「事件生产者 + 扫描生产者 → 任务表 → Worker」双通道后，扫描链的
# 节奏参数。在线时事件链兜实时，扫描只管补漏，周期可以放宽。
WHITELIST_SCAN_INTERVAL_SECONDS = 300     # 扫描周期（秒）
WHITELIST_SCAN_PAGES_PER_ROUND = 10       # 每轮每聊天最多页数（页大小复用 LISTEN_MAX_MESSAGES_PER_SCAN）
WHITELIST_SCAN_PAGE_SLEEP_SECONDS = 5.0   # 页间小睡：读请求摊开（风控）
```

- [ ] **Step 4: 改 runtime_db.py**

4a. `_SCHEMA` 里 `listener_checkpoints` 的 DDL 换成：

```python
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
```

`listener_tasks` 的 DDL 在 `payload TEXT` 之后、右括号之前加一列：

```python
        payload TEXT,
        origin TEXT NOT NULL DEFAULT 'listen'
```

4b. `migrate()` 里 `if 1 <= version < 2:` 块之后加：

```python
    if version < 3:
        # v2 → v3：checkpoints 加 chain（旧库重建，旧行归 listen）、
        # tasks 加 origin（ALTER，旧行落默认 'listen'）。新库建表时已是
        # v3 形状，这里探测到列已存在即为 no-op。
        _migrate_v3()
        logger.info("🗄 Runtime DB 迁移：v3（checkpoints.chain + tasks.origin）")
```

并在 migrate 之前新增：

```python
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
```

4c. checkpoint 三函数带 chain：

```python
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
```

4d. `enqueue_listener_tasks` 签名与 INSERT 改为（函数 docstring 补一句 origin 说明）：

```python
def enqueue_listener_tasks(source_chat_id, tasks, checkpoint=None, now=None,
                           chain="listen", origin="listen"):
```

INSERT 语句与参数：

```python
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
```

事务内 checkpoint 写入带 chain：

```python
        if checkpoint is not None:
            _write_checkpoint(conn, source_chat_id, checkpoint, now,
                              chain=chain)
```

4e. `claim_listener_task` 的 SELECT 加 origin 优先级（listen 先于 wl；同 origin 内按 id FIFO）：

```python
        row = _execute(
            conn,
            "SELECT * FROM listener_tasks WHERE status=? "
            "AND (next_retry_at IS NULL OR next_retry_at<=?) "
            "ORDER BY (origin='wl'), id LIMIT 1",
            (STATUS_PENDING, now),
        ).fetchone()
```

4f. `get_listener_stats` 改为（保留原返回形状）：

```python
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
```

（其后 `key_of` 起的归并逻辑保持不变。）

4g. 在 `get_listener_stats` 之后新增：

```python
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
```

- [ ] **Step 5: 跑全量测试**

Run: `.venv/bin/python -m unittest discover -s tests -p "test_*.py" -v`
Expected: 全 PASS（默认参数保持旧语义：chain/origin 缺省 'listen'，既有调用点零改动；claim 排序在全 listen 数据上与旧 ORDER BY id 等价）。

- [ ] **Step 6: Commit**

```bash
git add tg_userbot/config.py tg_userbot/runtime_db.py tests/test_runtime_db.py
git commit -m "功能：白名单双通道 —— Runtime DB v3（chain 双游标 + origin 任务来源 + listen 优先领取）"
```

---

### Task 2: listener 公开复用入口 + wl_scan.py 扫描生产者

**Files:**
- Modify: `tg_userbot/listener.py`（`_fetch_new` 附近加公开别名；`_build_tasks` 之后加公开助手）
- Modify: `tg_userbot/state.py`（`WL_INPUT_UNTIL`、`WL_LAST_SCAN` 占位）
- Create: `tg_userbot/wl_scan.py`
- Test: `tests/test_wl_scan.py`（新建）

**Interfaces:**
- Consumes: Task 1 的全部 runtime_db 签名与 `config.WHITELIST_SCAN_*`。
- Produces:
  - `listener.fetch_new_messages(chat_id, checkpoint)`（= `_fetch_new` 的公开别名）
  - `listener.fetch_newest_id(chat_id)`（= `_fetch_newest_id` 的公开别名）
  - `listener.build_saved_messages_task(chat_id, media, caption, origin=None) -> list[dict]`（锚点=组内最小 id，payload["source_name"] 兜底聊天标题）
  - `wl_scan.all_members_dedup_hit(media) -> bool`
  - `wl_scan.scan_wl_chat(chat_id, nap=None) -> dict`（键：scanned/created/duplicate/capped/failed）
  - `wl_scan.scan_all(manual=False) -> dict`（键：chats/failed_chats/scanned/created/duplicate/capped/ts；另有 skipped/empty_chats/db_unavailable 标记；写 `state.WL_LAST_SCAN`）
  - `wl_scan.since_checkpoint(cli, chat_token, msgid_text) -> (bool, str)`
  - `wl_scan.collect_scan_info() -> dict[int, tuple]`（chat_id → (checkpoint|None, 待执行数)）
  - `wl_scan.summary_text(totals) -> str`
  - `state.WL_INPUT_UNTIL = 0.0`、`state.WL_LAST_SCAN = None`

- [ ] **Step 1: 写失败测试（新建 tests/test_wl_scan.py）**

```python
"""白名单扫描生产者（wl_scan.py）的单元测试。

契约（规格书 docs/plan/下载白名单双通道扫描制改造_设计规格.md §5/§8）：
1. 按 wl 游标（chain='wl'）分页扫描白名单聊天，命中「可下载媒体」即建任务
   （origin='wl'，目标=收藏夹 + download），任务与游标同事务推进。
2. 相册按 grouped_id 整组一个单元；**不跳过镜像帖**（白名单契约是一切媒体）。
3. dedup 前置：整组全部成员命中已下载/在途索引才跳过（游标照推）。
4. 背压：待执行 ≥ LISTEN_MAX_PENDING_TASKS 停扫、游标不动。
5. 分页上限：每轮最多 WHITELIST_SCAN_PAGES_PER_ROUND 页、页间睡。
6. 首启无游标 → 以当前最新 id 初始化（不扫历史）。
7. 死聊天降噪：连续失败只报第一次 ERROR。

不联网：FakeScanClient 记录调用；DB 与配置落临时目录。
"""
import asyncio
import atexit
import os
import shutil
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_wl_scan_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config  # noqa: E402
from tg_userbot import dedup  # noqa: E402
from tg_userbot import listener  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402
from tg_userbot import state  # noqa: E402
from tg_userbot import wl_scan  # noqa: E402

SRC = -1001234567890
OTHER = -1005555555555


class FakeFile:
    def __init__(self, fid="file-1", size=100, name="v.mp4"):
        self.id = fid
        self.size = size
        self.name = name
        self.mime_type = "video/mp4"


class FakeMsg:
    def __init__(self, mid, text="", grouped_id=None, chat_id=SRC, name=None):
        self.id = mid
        self.message = text
        self.grouped_id = grouped_id
        self.chat_id = chat_id
        self.date = None
        self.fwd_from = None
        self.file = FakeFile(name=name or f"f{mid}.mp4")
        self.document = object()
        self.video = object()
        self.photo = None


class FakeScanClient:
    """get_messages 内存实现：min_id 过滤 + reverse 语义，支持分页。"""

    def __init__(self, messages):
        self.store = {m.id: m for m in messages}
        self.calls = []

    async def get_messages(self, chat_id, limit=100, min_id=0,
                           reverse=False, ids=None, **kw):
        self.calls.append(dict(limit=limit, min_id=min_id, ids=ids))
        if ids:
            return [self.store[i] for i in ids if i in self.store]
        got = sorted((m for m in self.store.values()
                      if m.id > (min_id or 0)), key=lambda m: m.id)
        if reverse:
            return got[:limit]
        return list(reversed(got))[:limit]


def _nap_record(sleeps):
    async def nap(seconds):
        sleeps.append(seconds)
    return nap


class WlScanTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        self._patches = [
            mock.patch.object(state, "WHITELIST_CHATS", {SRC: "测试频道"}),
            mock.patch.object(state, "RUNTIME_DB_READY", True),
            mock.patch.object(dedup, "should_skip",
                              lambda keys: (False, "")),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = FakeScanClient([])
        state.client = self.client
        self.addCleanup(setattr, state, "client", None)

    # -- 基础：全媒体建任务 + 游标推进 -------------------------------------
    async def test_media_builds_task_and_advances_checkpoint(self):
        self.client.store = {
            m.id: m for m in (FakeMsg(10, text="#a"), FakeMsg(11),
                              FakeMsg(12, text="纯文本"))}
        r = await wl_scan.scan_wl_chat(SRC)
        self.assertEqual(r["created"], 2)          # 10、11 是媒体；12 纯文本跳过
        tasks = [t for t in runtime_db.list_listener_tasks() if t["origin"] == "wl"]
        self.assertEqual(len(tasks), 2)
        t = tasks[0]
        self.assertEqual(t["target_type"], "saved_messages")
        self.assertEqual(t["download"], 1)
        self.assertEqual(runtime_db.get_listener_checkpoint(SRC, chain="wl"), 12)

    async def test_first_scan_initializes_checkpoint_no_history(self):
        # 无游标时初始化为当前最新（12），**不**建历史任务
        self.client.store = {m.id: m for m in (FakeMsg(10), FakeMsg(12))}
        r = await wl_scan.scan_wl_chat(SRC)
        self.assertEqual(r["created"], 0)
        self.assertEqual(runtime_db.get_listener_checkpoint(SRC, chain="wl"), 12)

    # -- 相册 --------------------------------------------------------------
    async def test_album_becomes_one_task_with_members(self):
        album = [FakeMsg(20, grouped_id=77), FakeMsg(21, grouped_id=77, text="相册说明"),
                 FakeMsg(22, grouped_id=77)]
        self.client.store = {m.id: m for m in album}
        r = await wl_scan.scan_wl_chat(SRC)
        self.assertEqual(r["created"], 1)
        t = [x for x in runtime_db.list_listener_tasks() if x["origin"] == "wl"][0]
        self.assertEqual(t["message_id"], 20)      # 锚点 = 组内最小 id
        self.assertEqual(t["payload"]["member_ids"], [20, 21, 22])
        self.assertEqual(t["payload"]["caption"], "相册说明")

    # -- dedup 前置 --------------------------------------------------------
    async def test_all_members_dedup_hit_skips_unit(self):
        self.client.store = {m.id: m for m in (FakeMsg(30), FakeMsg(31))}
        with mock.patch.object(wl_scan.dedup, "should_skip",
                               lambda keys: (True, "")):
            r = await wl_scan.scan_wl_chat(SRC)
        self.assertEqual(r["created"], 0)
        # 游标照推（内容已下载过，刻意放行）
        self.assertEqual(runtime_db.get_listener_checkpoint(SRC, chain="wl"), 31)

    async def test_partial_dedup_hit_still_builds(self):
        self.client.store = {m.id: m for m in (FakeMsg(30), FakeMsg(31))}
        calls = {"n": 0}

        def flaky(keys):
            calls["n"] += 1
            return (calls["n"] == 1, "")   # 第一个成员命中、第二个不命中
        with mock.patch.object(wl_scan.dedup, "should_skip", flaky):
            r = await wl_scan.scan_wl_chat(SRC)
        self.assertEqual(r["created"], 2)  # 单元不跳过：两成员各照常建任务

    # -- 背压与分页 --------------------------------------------------------
    async def test_backpressure_stops_scan_and_keeps_cursor(self):
        with mock.patch.object(runtime_db, "count_pending_listener_tasks",
                               return_value=int(config.LISTEN_MAX_PENDING_TASKS)):
            r = await wl_scan.scan_wl_chat(SRC)
        self.assertTrue(r["capped"])
        self.assertEqual(r["scanned"], 0)
        self.assertIsNone(runtime_db.get_listener_checkpoint(SRC, chain="wl"))

    async def test_pagination_respects_pages_per_round(self):
        msgs = [FakeMsg(i) for i in range(1, 451)]   # 450 条 = 3 页
        self.client.store = {m.id: m for m in msgs}
        sleeps = []
        with mock.patch.object(config, "WHITELIST_SCAN_PAGES_PER_ROUND", 2):
            r = await wl_scan.scan_wl_chat(SRC, nap=_nap_record(sleeps))
        self.assertEqual(r["scanned"], 400)          # 只扫 2 页
        self.assertEqual(runtime_db.get_listener_checkpoint(SRC, chain="wl"), 400)
        self.assertEqual(sleeps, [config.WHITELIST_SCAN_PAGE_SLEEP_SECONDS])

    # -- 失败降噪 / 多聊天 -------------------------------------------------
    async def test_failure_marked_once(self):
        async def dead(chat_id, checkpoint):
            return None
        with mock.patch.object(listener, "fetch_new_messages", dead):
            await wl_scan.scan_wl_chat(SRC)
            self.assertTrue(wl_scan._FAILING_CHATS)
        # 第二轮失败不再打 ERROR（降噪），成功后清除
        self.client.store = {m.id: m for m in (FakeMsg(50),)}
        await wl_scan.scan_wl_chat(SRC)
        self.assertNotIn(SRC, wl_scan._FAILING_CHATS)

    async def test_scan_all_skips_non_whitelist_and_reports(self):
        state.WHITELIST_CHATS.clear()   # setUp patch 的同一 dict
        r = await wl_scan.scan_all()
        self.assertTrue(r.get("empty_chats"))


class SinceCheckpointTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        p = mock.patch.object(state, "WHITELIST_CHATS", {SRC: "测试频道"})
        p.start()
        self.addCleanup(p.stop)

    async def test_since_rejects_unknown_chat(self):
        ok, msg = await wl_scan.since_checkpoint(None, "@nope", "100")
        self.assertFalse(ok)

    async def test_since_writes_wl_cursor(self):
        ok, msg = await wl_scan.since_checkpoint(None, str(SRC), "88000")
        self.assertTrue(ok)
        self.assertEqual(
            runtime_db.get_listener_checkpoint(SRC, chain="wl"), 88000)

    async def test_since_rejects_bad_msgid(self):
        ok, _ = await wl_scan.since_checkpoint(None, str(SRC), "abc")
        self.assertFalse(ok)
        ok, _ = await wl_scan.since_checkpoint(None, str(SRC), "0")
        self.assertFalse(ok)


class BuildTaskHelperTest(unittest.TestCase):

    def test_source_name_falls_back_to_chat_title(self):
        p = mock.patch.object(state, "WHITELIST_CHATS", {SRC: "测试频道"})
        p.start()
        self.addCleanup(p.stop)
        records = listener.build_saved_messages_task(SRC, [FakeMsg(5)], "")
        self.assertEqual(records[0]["payload"]["source_name"], "测试频道")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m unittest tests.test_wl_scan -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'tg_userbot.wl_scan'`（`listener.build_saved_messages_task` 同样不存在）。

- [ ] **Step 3: listener.py 加公开入口**

3a. 在 `_fetch_newest_id` 定义之后加：

```python
# 公开别名：白名单扫描生产者（wl_scan）复用同一取消息路径。
fetch_newest_id = _fetch_newest_id
fetch_new_messages = _fetch_new
```

3b. 在 `_build_tasks` 定义之后加：

```python
def build_saved_messages_task(chat_id, media, caption, origin=None):
    """一个消息单元 → 收藏夹下载任务记录（白名单两条生产链共用）。

    ``origin`` 的 source_name 缺失时兜底为**聊天标题**：来源禁转回退直下原
    消息时副本不存在、没有 fwd_from 可解析，目录名只能靠 payload 里这个
    字段；转发路径上它与副本 fwd_from 的解析结果一致，不冲突。
    """
    origin = dict(origin) if origin else {}
    if not origin.get("source_name"):
        origin["source_name"] = (
            (state.WHITELIST_CHATS or {}).get(int(chat_id))
            or f"chat_{int(chat_id)}")
    return _build_tasks(chat_id, media, {("saved_messages", None)}, True,
                        caption, min(m.id for m in media), origin)
```

- [ ] **Step 4: state.py 加占位（在 LISTEN 相关占位附近）**

```python
WL_INPUT_UNTIL = 0.0        # 「⏪ 回补」输入窗口截止时刻（monotonic）
WL_LAST_SCAN = None         # 白名单扫描生产者最近一轮汇总（wl_scan.scan_all 写入）
```

- [ ] **Step 5: 新建 tg_userbot/wl_scan.py**

```python
"""下载白名单 —— 扫描生产者（停机补漏）+ 两条生产链共用的助手。

白名单改成双通道后（规格书 docs/plan/下载白名单双通道扫描制改造_设计规格.md）：

    whitelist_config.json（/wl）
       ├─ 事件生产者【在线实时】app.record_whitelist_media → 记任务
       └─ 扫描生产者【停机补漏】本模块按 wl 游标周期扫描 → 记任务

    listener_tasks（唯一索引跨链路防双转发）──► listener_worker 统一转发

本模块与标签监听 Scanner（listener.py）同构：**只建任务、不转发**；取消息
复用 listener 的同一批函数（netio 收口、相册边界补齐）。与监听扫描的三处
刻意差异：命中条件是「可下载媒体」而非标签；**不跳过镜像帖**（白名单契约
是「这个聊天的一切媒体都要」）；游标独立在 chain='wl'。

事件生产者也共用本模块的 dedup 前置与任务构建助手（all_members_dedup_hit /
经 listener.build_saved_messages_task），保证两条链产出形状一致。
"""
import asyncio
import time

from . import config
from . import dedup
from . import listener
from . import runtime_db
from . import state
from . import whitelist
from .log import logger
from .naming import pick_group_caption_text
from .sources import is_downloadable, resolve_origin_snapshot

# 扫描重入保护（与 listener._SCANNING 分开；检查与置位之间无 await）。
_SCANNING = False

# 连续失败的聊天（死聊天降噪：只报第一次 ERROR，成功后清除）。
_FAILING_CHATS = set()


def _mark_chat_failure(chat_id, reason):
    if chat_id in _FAILING_CHATS:
        logger.info(f"📋 白名单聊天 {chat_id} 扫描仍失败（降噪不重复报）：{reason}")
        return
    _FAILING_CHATS.add(chat_id)
    logger.error(f"📋 白名单聊天 {chat_id} 扫描失败：{reason}")


def _clear_chat_failure(chat_id):
    _FAILING_CHATS.discard(chat_id)


# ============================================================
# 两条生产链共用的助手
# ============================================================
def all_members_dedup_hit(media) -> bool:
    """整组**全部成员**的判重键都命中已下载/在途索引。

    dedup 前置（规格 §8）：回补时已下载过的内容连转发都不做——收藏夹不被
    重复副本刷屏。部分命中照建任务（下载侧 per-copy 拦截兜底）；键拿不到
    （None/空）按未命中处理，绝不因判重不确定性丢媒体。
    """
    for m in media or []:
        keys = dedup.media_keys(m)
        if not keys:
            return False
        skip, _ = dedup.should_skip(keys)
        if not skip:
            return False
    return bool(media)


# ============================================================
# 扫描
# ============================================================
async def scan_wl_chat(chat_id, nap=None):
    """扫一个白名单聊天：按 wl 游标分页取新消息 → 建任务 → 同事务推游标。

    分页上限（规格 §5）：每轮最多 WHITELIST_SCAN_PAGES_PER_ROUND 页、页间睡
    WHITELIST_SCAN_PAGE_SLEEP_SECONDS——几千条积压摊到多轮读，不一口气砸请求。
    背压（规格 §8）：待执行 ≥ LISTEN_MAX_PENDING_TASKS 停扫、游标原地不动。

    返回 {scanned, created, duplicate, capped, failed}。
    """
    chat_id = int(chat_id)
    result = {"scanned": 0, "created": 0, "duplicate": 0,
              "capped": False, "failed": False}
    nap = nap or asyncio.sleep
    page_size = int(config.LISTEN_MAX_MESSAGES_PER_SCAN)
    pages = max(1, int(config.WHITELIST_SCAN_PAGES_PER_ROUND))
    page_sleep = float(config.WHITELIST_SCAN_PAGE_SLEEP_SECONDS)

    checkpoint = runtime_db.get_listener_checkpoint(chat_id, chain="wl")
    if checkpoint is None:
        newest = await listener.fetch_newest_id(chat_id)
        if newest is None:
            result["failed"] = True
            _mark_chat_failure(chat_id, "读不到最新消息（无 wl 游标）")
            return result
        runtime_db.set_listener_checkpoint(chat_id, newest, chain="wl")
        checkpoint = newest
        logger.info(
            f"📋 白名单扫描初始化游标：{chat_id} → {newest}"
            "（不扫历史；回补存量用 /wl since）")

    for page_no in range(pages):
        budget = (int(config.LISTEN_MAX_PENDING_TASKS)
                  - runtime_db.count_pending_listener_tasks())
        if budget <= 0:
            result["capped"] = True
            logger.warning(
                f"📋 待执行任务已达上限 {config.LISTEN_MAX_PENDING_TASKS}，"
                f"{chat_id} 的 wl 游标停在 {checkpoint}，Worker 消费后下轮继续")
            return result

        msgs = await listener.fetch_new_messages(chat_id, checkpoint)
        if msgs is None:
            result["failed"] = True
            _mark_chat_failure(chat_id, "读取新消息失败")
            return result
        result["scanned"] += len(msgs)
        if not msgs:
            break

        tasks = []
        processed_upto = checkpoint
        budget_left = True
        for unit in listener.group_by_album(msgs):
            unit_max_id = max(m.id for m in unit)
            media = [m for m in unit if is_downloadable(m)]
            if not media:
                # 纯文本：不建任务（与监听扫描同一纪律），游标照推
                processed_upto = max(processed_upto, unit_max_id)
                continue
            if len(tasks) >= budget:
                budget_left = False
                logger.warning(
                    f"📋 {chat_id} 本轮队列额度用尽，停在消息 "
                    f"{min(m.id for m in media)}（下轮继续）")
                break
            if all_members_dedup_hit(media):
                logger.info(
                    f"📋 {chat_id} #{min(m.id for m in media)}：整组已下载过"
                    "（dedup 前置），跳过转发")
                processed_upto = max(processed_upto, unit_max_id)
                continue
            gid = getattr(media[0], "grouped_id", None)
            caption = (pick_group_caption_text(media, gid) if gid
                       else (getattr(media[0], "message", "") or "").strip())
            origin = await resolve_origin_snapshot(media[0])
            tasks.extend(listener.build_saved_messages_task(
                chat_id, media, caption, origin))
            processed_upto = max(processed_upto, unit_max_id)

        if tasks or processed_upto != checkpoint:
            ids = runtime_db.enqueue_listener_tasks(
                chat_id, tasks, checkpoint=processed_upto,
                chain="wl", origin="wl")
            created = sum(1 for i in ids if i)
            result["created"] += created
            result["duplicate"] += len(ids) - created
        checkpoint = processed_upto

        if not budget_left:
            result["capped"] = True
            break
        if len(msgs) < page_size:
            break
        if page_no < pages - 1 and page_sleep > 0:
            await nap(page_sleep)
    return result


async def scan_all(manual=False) -> dict:
    """扫描全部白名单聊天（wl 链）。被重入挡下时带 skipped。

    每轮重新遍历 state.WHITELIST_CHATS（/wl add/del 即时生效）；**不跟随
    LISTEN_ENABLED**——白名单没有总开关概念，/wl del 移除即停。单聊天失败
    不影响其它（与 listener.scan_all 同款隔离）。
    """
    global _SCANNING
    empty = {"chats": 0, "failed_chats": 0, "scanned": 0, "created": 0,
             "duplicate": 0, "capped": 0}
    if _SCANNING:
        logger.info("📋 已有白名单扫描在进行中，跳过本次")
        return dict(empty, skipped=True)
    if not state.RUNTIME_DB_READY:
        return dict(empty, db_unavailable=True)
    chats = dict(state.WHITELIST_CHATS or {})
    if not chats:
        return dict(empty, empty_chats=True)

    _SCANNING = True
    totals = dict(empty)
    try:
        for chat_id in sorted(chats):
            totals["chats"] += 1
            try:
                r = await scan_wl_chat(chat_id)
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except runtime_db.DbUnavailable as e:
                totals["failed_chats"] += 1
                _mark_chat_failure(chat_id, f"数据库不可用：{e}")
                continue
            except Exception as e:
                totals["failed_chats"] += 1
                _mark_chat_failure(chat_id, f"{type(e).__name__}: {e}")
                continue
            if r.get("failed"):
                totals["failed_chats"] += 1
                continue
            _clear_chat_failure(chat_id)
            for key in ("scanned", "created", "duplicate"):
                totals[key] += r.get(key, 0)
            if r.get("capped"):
                totals["capped"] += 1
        totals["ts"] = time.strftime("%H:%M")
        totals["queue"] = _wl_queue_totals()
        state.WL_LAST_SCAN = dict(totals)
        logger.info(
            f"📋 白名单扫描完成（{'手动' if manual else '定时'}）："
            f"{totals['chats']} 个聊天 | 检查 {totals['scanned']} 条 | "
            f"落盘任务 {totals['created']} 条"
            + (f" | 重复跳过 {totals['duplicate']} 条" if totals["duplicate"] else "")
            + (f" | {totals['capped']} 个聊天触到队列上限" if totals["capped"] else "")
            + (f" | 失败 {totals['failed_chats']} 个" if totals["failed_chats"] else "")
        )
        return totals
    finally:
        _SCANNING = False


def _wl_queue_totals():
    """wl 任务的存量（视图展示用；DB 不可用返回 None）。"""
    try:
        return runtime_db.get_listener_stats(origin="wl")
    except runtime_db.DbUnavailable:
        return None


def is_scanning() -> bool:
    return _SCANNING


# ============================================================
# 回补（/wl since）
# ============================================================
async def since_checkpoint(cli, chat_token, msgid_text):
    """/wl since 的实现：校验聊天在白名单 → 写 wl 游标（返回 (是否成功, 提示)）。

    游标语义与扫描一致：扫描处理该 id **之后**的消息（min_id 不含本身）。
    写成比当前更大的 id 也合法——自然扫不到东西而已。
    """
    chat_id = whitelist.resolve_wl_del_key(str(chat_token or "").strip(),
                                           state.WHITELIST_CHATS)
    if chat_id is None:
        try:
            cid, _title = await whitelist.resolve_wl_target(
                cli, str(chat_token or "").strip())
        except Exception:
            cid = None
        if cid is not None and cid in state.WHITELIST_CHATS:
            chat_id = cid
    if chat_id is None:
        return False, (f"❌ /wl since：{chat_token} 不在下载白名单"
                       "（先 /wl add，或用列表序号）")
    try:
        msg_id = int(str(msgid_text).strip())
    except (TypeError, ValueError):
        msg_id = 0
    if msg_id <= 0:
        return False, "❌ /wl since：消息 id 须为正整数"
    runtime_db.set_listener_checkpoint(chat_id, msg_id, chain="wl")
    logger.info(f"📋 白名单回补游标：{chat_id} → {msg_id}（扫描其后的消息）")
    return True, (f"✅ 已把「{state.WHITELIST_CHATS[chat_id]}」的扫描起点设为 "
                  f"#{msg_id}，正在回补其后消息（受 Worker 节流控制，逐步转发）")


# ============================================================
# 视图 / 命令文案
# ============================================================
def collect_scan_info():
    """{chat_id: (checkpoint|None, 待执行任务数)}（/wl 视图用）。"""
    info = {}
    if not state.RUNTIME_DB_READY:
        return info
    for cid in state.WHITELIST_CHATS or {}:
        try:
            ckpt = runtime_db.get_listener_checkpoint(cid, chain="wl")
            pending = runtime_db.count_listener_tasks_for_chat(cid, origin="wl")
        except runtime_db.DbUnavailable:
            continue
        info[cid] = (ckpt, pending)
    return info


def summary_text(totals) -> str:
    """/wl scan 的结果正文（命令与菜单共用）。"""
    if totals.get("skipped"):
        return "📋 白名单扫描\n\n⏳ 已有扫描在进行中，请稍候。"
    if totals.get("db_unavailable"):
        return "📋 白名单扫描\n\n❌ Runtime DB 不可用，无法落盘任务。"
    if totals.get("empty_chats"):
        return "📋 白名单扫描\n\n白名单为空。"
    lines = [
        "📋 白名单扫描完成",
        "",
        f"聊天：{totals['chats']} 个（失败 {totals['failed_chats']}）",
        f"检查消息：{totals['scanned']} 条",
        f"新建转发任务：{totals.get('created', 0)} 条",
    ]
    if totals.get("duplicate"):
        lines.append(f"重复跳过：{totals['duplicate']} 条")
    if totals.get("capped"):
        lines.append("⚠️ 有聊天触到任务队列上限（下轮继续）")
    lines.append("")
    lines.append("转发由常驻 Worker 受控执行（标签监听任务优先），稍后完成。")
    return "\n".join(lines)
```

- [ ] **Step 6: 跑测试**

Run: `.venv/bin/python -m unittest tests.test_wl_scan -v`
Expected: PASS（全部用例）。

- [ ] **Step 7: 跑全量并提交**

Run: `.venv/bin/python -m unittest discover -s tests -p "test_*.py" -v`
Expected: 全 PASS。

```bash
git add tg_userbot/listener.py tg_userbot/state.py tg_userbot/wl_scan.py tests/test_wl_scan.py
git commit -m "功能：白名单扫描生产者 wl_scan —— wl 游标分页扫描、dedup 前置、/wl since 回补"
```

---

### Task 3: Worker 改动（src=origin + 来源禁转回退直下）

**Files:**
- Modify: `tg_userbot/listener_worker.py:39-44`（imports）、`execute_task`（~270-340）、新增 `_fallback_direct_download`
- Test: `tests/test_listener_worker.py`（追加用例到 WorkerExecuteTest 同类或新类）

**Interfaces:**
- Consumes: Task 1 的 origin 列（任务行带 `origin`）；Task 2 的 `listener.build_saved_messages_task`（payload 带 source_name）。
- Produces: Worker 行为——入队副本 `src=task["origin"] or "listen"`；`ChatForwardsRestrictedError` → download=1 的收藏夹任务回退 `app.enqueue_media(原消息, source_chat_id, payload.source_name)` 并标 FAILED（文案「来源禁转，已回退直下原消息（无收藏夹副本）」），其余任务原样 FAILED。

- [ ] **Step 1: 写失败测试（追加到 tests/test_listener_worker.py 末尾）**

（沿用该文件既有的 setUp/execute_task 调用模式；`_run` 助手按文件内现状——若既有用例直接 `await lw.execute_task(task)` 则照抄该形态。）

```python
from telethon.errors import ChatForwardsRestrictedError  # noqa: E402  # 加到文件头 telethon 导入块


class WlOriginAndFallbackTest(unittest.IsolatedAsyncioTestCase):
    """origin=wl 任务：src 显式传 wl；来源禁转回退直下原消息。"""

    def setUp(self):
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        lw.reset_pause()

    def _mk_task(self, origin="wl"):
        rec = {
            "message_id": 101, "grouped_id": None,
            "target_type": "saved_messages", "target_chat_id": None,
            "download": 1,
            "payload": {"member_ids": [101], "caption": "标题",
                        "source_name": "测试频道"},
        }
        ids = runtime_db.enqueue_listener_tasks(SRC, [rec], origin=origin)
        return runtime_db.get_listener_task(ids[0])

    async def test_enqueue_copy_gets_task_origin_as_src(self):
        task = self._mk_task(origin="wl")
        seen = {}

        async def fake_enqueue(copy, source_link, album_caption, src,
                               parent_date=None, parent_caption=None,
                               source_name=None):
            seen["src"] = src
            return None

        with mock.patch.object(lw, "_enqueue_copy", fake_enqueue), \
                mock.patch.object(lw, "listener_fetch",
                                  self._fake_fetch):
            ok = await lw.execute_task(task)
        self.assertTrue(ok)
        self.assertEqual(seen["src"], "wl")

    async def test_forwards_restricted_falls_back_direct(self):
        task = self._mk_task(origin="wl")
        enqueued = []

        async def restricted(*a, **kw):
            raise ChatForwardsRestrictedError()

        async def fake_enqueue(message, chat_id, source_override, **kw):
            enqueued.append((message.id, chat_id, source_override))

        async def boom(*a, **kw):
            raise ChatForwardsRestrictedError()
        with mock.patch.object(lw, "_forward", boom), \
                mock.patch("tg_userbot.app.enqueue_media", fake_enqueue), \
                mock.patch.object(lw, "listener_fetch", self._fake_fetch):
            ok = await lw.execute_task(task)
        self.assertFalse(ok)                       # 任务算失败（无收藏夹副本）
        self.assertEqual(enqueued, [(101, SRC, "测试频道")])
        self.assertEqual(
            runtime_db.get_listener_task(task["id"])["status"], "FAILED")
        self.assertIn("回退直下",
                      runtime_db.get_listener_task(task["id"])["last_error"])

    async def test_forwards_restricted_chat_target_no_fallback(self):
        rec = {
            "message_id": 102, "grouped_id": None,
            "target_type": "chat", "target_chat_id": CHAT_A,
            "download": 0, "payload": {"member_ids": [102], "caption": ""},
        }
        ids = runtime_db.enqueue_listener_tasks(SRC, [rec])
        task = runtime_db.get_listener_task(ids[0])
        enqueued = []

        async def fake_enqueue(*a, **kw):
            enqueued.append(a)

        async def boom(*a, **kw):
            raise ChatForwardsRestrictedError()
        with mock.patch.object(lw, "_forward", boom), \
                mock.patch("tg_userbot.app.enqueue_media", fake_enqueue), \
                mock.patch.object(lw, "listener_fetch", self._fake_fetch):
            ok = await lw.execute_task(task)
        self.assertFalse(ok)
        self.assertEqual(enqueued, [])             # 非 download 任务不回退
        self.assertEqual(
            runtime_db.get_listener_task(task["id"])["status"], "FAILED")

    async def _fake_fetch(self, state_mod, source_chat_id, member_ids):
        """execute_task 的取消息注入：返回与 member_ids 对应的假消息。"""
        return [FakeMessage(mid) for mid in member_ids]
```

（`FakeMessage` 用该文件顶部已有的那个类；`CHAT_A` 同理已存在。若 `listener_fetch` 的注入形态与既有用例不同——以文件内现状为准对齐。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m unittest tests.test_listener_worker -v`
Expected: FAIL —— src 仍恒为 "listen"；禁转异常走 `_handle_failure`（无回退、error 文案不含「回退直下」）。

- [ ] **Step 3: 实现**

3a. imports 补 `ChatForwardsRestrictedError`：

```python
from telethon.errors import (
    ChannelPrivateError,
    ChatForwardsRestrictedError,
    ChatWriteForbiddenError,
    FloodWaitError,
    PeerIdInvalidError,
)
```

3b. `execute_task` 里转发成功后的入队循环，`"listen"` 字面量改为任务 origin：

```python
            src = task.get("origin") or "listen"
            for copy in copies:
                own_text = (getattr(copy, "message", "") or "").strip()
                cap = None if own_text else (caption or None)
                try:
                    await _enqueue_copy(copy, source_link, cap, src,
                                        parent_date, parent_caption,
                                        source_name)
```

3c. `except asyncio.CancelledError:` 之前（即 `except Exception` 之前）插入禁转分支：

```python
    except ChatForwardsRestrictedError as e:
        # 来源禁转：永久错误；download=1 的收藏夹任务回退**直下原消息**
        #（媒体不丢，只丢收藏夹副本——与事件生产者的 DB 不可用回退同一纪律，
        # 规格 §6.1）。messages 在上面的取消息步骤已拿到。
        payload = task.get("payload") or {}
        if task.get("target_type") == "saved_messages" and task.get("download"):
            ok = await _fallback_direct_download(
                task, messages, payload.get("source_name"))
            err = ("来源禁转，已回退直下原消息（无收藏夹副本）"
                   if ok else "来源禁转且直下回退也失败")
            logger.warning(f"📡 任务 #{task_id} 来源禁转，已回退直下：{label}")
        else:
            err = f"{type(e).__name__}: {e}"
            logger.error(f"📡 任务 #{task_id} 来源禁转（无回退语义）：{label}")
        _safe(lambda: runtime_db.fail_listener_task(task_id, error=err))
        return False
```

3d. 新增（放在 `_handle_failure` 附近）：

```python
async def _fallback_direct_download(task, messages, source_name):
    """来源禁转的回退：把**原消息**直接交给现有下载链路（不转发、无副本）。

    enqueue_media 自带 dedup 与命名全链路；目录名用 payload 快照的
    source_name（原消息没有 fwd_from 可解析，目录只能靠它）。
    """
    from . import app   # 函数内导入：与 _enqueue_copy 同理（顶层互相导入成环）
    ok = True
    for m in messages:
        try:
            await app.enqueue_media(
                m, int(task["source_chat_id"]),
                source_name or f"chat_{task['source_chat_id']}")
        except Exception as e:
            ok = False
            logger.error(
                f"📡 任务 #{task['id']} 直下回退失败（消息 {m.id}）：{e}")
    return ok
```

- [ ] **Step 4: 跑测试**

Run: `.venv/bin/python -m unittest tests.test_listener_worker -v`
Expected: PASS（含既有用例回归——既有任务的 origin 默认 'listen'，src 行为不变）。

- [ ] **Step 5: 全量 + Commit**

Run: `.venv/bin/python -m unittest discover -s tests -p "test_*.py" -v`

```bash
git add tg_userbot/listener_worker.py tests/test_listener_worker.py
git commit -m "功能：Worker 按 origin 显式记账 + 来源禁转回退直下原消息"
```

---

### Task 4: 命令、视图与菜单（/wl since、/wl scan、视图、按钮、输入窗口）

**Files:**
- Modify: `tg_userbot/whitelist.py:75-93`（`parse_wl_command`）
- Modify: `tg_userbot/text.py:101-121`（`wl_list_text`）
- Modify: `tg_userbot/commands.py:112-176`（/wl 分发）与文件头 imports
- Modify: `tg_userbot/menu.py:267-274`（`wl_menu_buttons`）
- Modify: `tg_userbot/bot.py`（`open_input_window`、`handle_menu_action`、`bot_message_handler` 窗口消费、新增 `_handle_wl_since_input`）
- Test: `tests/test_whitelist.py`、`tests/test_commands.py`、`tests/test_menu.py`

**Interfaces:**
- Consumes: Task 2 的 `wl_scan.since_checkpoint / scan_all / collect_scan_info / summary_text`、`state.WL_INPUT_UNTIL`、`state.WL_LAST_SCAN`。
- Produces: `/wl since <聊天> <消息id>`、`/wl scan`；`parse_wl_command` 新动作 `("since", "<chat> <msgid>")` / `("scan", None)`；`text.wl_list_text(chats=None, scan_info=None, last_scan=None)`；菜单动作 `wl_since` / `wl_scan`。

- [ ] **Step 1: 写失败测试**

1a. tests/test_whitelist.py 追加（放进现有 parse 命令测试类或新类）：

```python
class ParseWlSinceScanTest(unittest.TestCase):
    """/wl 新子命令：since（回补）与 scan（立即扫描）。"""

    def test_parse_since(self):
        self.assertEqual(whitelist.parse_wl_command("/wl since 1 88000"),
                         ("since", "1 88000"))
        self.assertEqual(whitelist.parse_wl_command("/wl since"),
                         ("since", ""))

    def test_parse_scan(self):
        self.assertEqual(whitelist.parse_wl_command("/wl scan"),
                         ("scan", None))
        self.assertIsNone(whitelist.parse_wl_command("/wl scans"))
```

1b. tests/test_commands.py 追加（沿用该文件的 FakeEvent 模式；需 init DB）：

```python
class WlSinceScanCommandTest(unittest.IsolatedAsyncioTestCase):
    """/wl scan 与 /wl since 的命令分发。"""

    def setUp(self):
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())

    async def test_since_usage_reply(self):
        ev = FakeEvent()
        handled = await commands.handle_command(ev, "/wl since")
        self.assertTrue(handled)
        self.assertIn("用法", ev.replies[0])

    async def test_since_bad_chat_replies_error(self):
        ev = FakeEvent()
        with mock.patch.object(state, "WHITELIST_CHATS", {}):
            handled = await commands.handle_command(ev, "/wl since 1 88000")
        self.assertTrue(handled)
        self.assertIn("不在下载白名单", ev.replies[0])

    async def test_scan_replies_summary(self):
        ev = FakeEvent()
        with mock.patch.object(wl_scan, "scan_all",
                               mock.AsyncMock(return_value={"chats": 0})):
            handled = await commands.handle_command(ev, "/wl scan")
        self.assertTrue(handled)
        self.assertIn("白名单扫描", ev.replies[0])
```

（若该文件已有 FakeEvent 且 reply 收集属性名不同，以文件现状为准；`wl_scan` / `runtime_db` 按该文件既有 import 风格补。）

1c. tests/test_menu.py 追加：

```python
class WlMenuButtonsTest(unittest.TestCase):

    def test_wl_menu_has_since_and_scan(self):
        p = mock.patch.object(state, "WHITELIST_CHATS", {})
        p.start()
        self.addCleanup(p.stop)
        rows = menu.wl_menu_buttons()
        labels = [b.text for row in rows for b in row]
        self.assertTrue(any("回补" in t for t in labels))
        self.assertTrue(any("立即扫描" in t for t in labels))
```

（test_menu.py 的既有 import 形态照抄文件头。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m unittest tests.test_whitelist tests.test_commands tests.test_menu -v`
Expected: FAIL —— `parse_wl_command("/wl since …")` 返回 `("invalid", None)`；菜单无新按钮；命令落 usage 分支。

- [ ] **Step 3: 实现**

3a. `whitelist.parse_wl_command` 在 `del` 分支之后加：

```python
    if sub.startswith("since"):
        return ("since", sub[len("since"):].strip())
    if sub == "scan":
        return ("scan", None)
```

3b. `text.wl_list_text` 整体替换为：

```python
def wl_list_text(chats=None, scan_info=None, last_scan=None):
    """生成白名单列表文本（命令与 bot 菜单共用）。

    scan_info：{chat_id: (checkpoint|None, 待执行任务数)}，来自
    wl_scan.collect_scan_info()；last_scan：state.WL_LAST_SCAN 快照。
    两者缺省时省略对应行（旧调用与测试兼容）。保持「📋 下载白名单」字面
    前缀不变（/wl 回复靠前缀自动清理）。
    """
    chats = state.WHITELIST_CHATS if chats is None else chats
    if not chats:
        return (
            "📋 下载白名单：空\n\n"
            "机制：白名单 chat 的媒体记为转发任务，由常驻 Worker 转发进"
            "收藏夹下载（副本保留）。\n"
            "用法：/wl add <ID或@用户名>，或回复一条从目标 chat "
            "转发的消息后发送 /wl add"
        )
    lines = []
    for i, (cid, title) in enumerate(sorted(chats.items()), start=1):
        lines.append(f"{i}. {title} ({cid})")
        info = (scan_info or {}).get(cid)
        if info:
            ckpt, pending = info
            state_parts = ["已扫至 #%s" % ckpt if ckpt else "未扫描",
                           f"待执行 {pending} 条"]
            lines.append("   " + " | ".join(state_parts))
    head = "📋 下载白名单：媒体记为转发任务，由常驻 Worker 转发进收藏夹下载（副本保留）"
    if last_scan:
        head += (f"\n上轮扫描：{last_scan.get('ts', '-')} 检查 "
                 f"{last_scan.get('scanned', 0)} 条 / 新建 "
                 f"{last_scan.get('created', 0)} 条")
    return (
        head + "\n\n" + "\n".join(lines) + "\n\n"
        "回补停机漏掉的存量：/wl since <序号|@用户名|ID> <消息id>\n"
        "立即扫描一轮：/wl scan"
    )
```

3c. `commands.py`：文件头 import 区加 `from . import wl_scan`（若无）；/wl 分发的 `del` 分支之后、usage 回复之前加：

```python
        if action == "scan":
            totals = await wl_scan.scan_all(manual=True)
            await event.reply(wl_scan.summary_text(totals))
            return True

        if action == "since":
            parts = (arg or "").split()
            if len(parts) != 2:
                await event.reply(
                    "❌ 用法：/wl since <序号|@用户名|ID> <消息id>\n"
                    "例：/wl since 1 88000 —— 从 #88000 之后开始回补")
                return True
            ok, msg = await wl_scan.since_checkpoint(
                state.client, parts[0], parts[1])
            await event.reply(msg)
            if ok:
                asyncio.create_task(wl_scan.scan_all(manual=True))
            return True
```

（`asyncio` 若未在 commands.py 顶部导入则补。）usage 文案同步改为：

```python
        await event.reply(
            "❌ 用法：/wl list | /wl add <ID或@用户名> | /wl del <ID或序号>\n"
            "        /wl scan（立即扫描）| /wl since <聊天> <消息id>（回补）")
```

3d. `menu.wl_menu_buttons` 整体替换为：

```python
def wl_menu_buttons():
    rows = [
        [Button.inline("➕ 添加", encode_menu_data("wl_add")),
         Button.inline("⏪ 回补", encode_menu_data("wl_since"))],
        [Button.inline("🔄 立即扫描", encode_menu_data("wl_scan"))],
    ]
    for cid, title in sorted(state.WHITELIST_CHATS.items()):
        rows.append(
            [Button.inline(f"➖ {title}", encode_menu_data("wl_del", str(cid)))]
        )
    rows.append([Button.inline("🔙 返回主菜单", encode_menu_data("home"))])
    return rows
```

3e. `bot.py`：

`open_input_window` 的重置块加 `state.WL_INPUT_UNTIL = 0.0`；`elif kind.startswith("listen_"):` 之后、`else:` 之前加：

```python
    elif kind == "wl_since":
        # 复用标签监听的窗口时长（120s）
        state.WL_INPUT_UNTIL = (
            time.monotonic() + config.LISTEN_INPUT_WINDOW_SECONDS
        )
```

docstring 的 kind 列表补 `\"wl_since\"（白名单回补）`。

`handle_menu_action` 的 `if action == "wl":` 分支改为：

```python
    if action == "wl":
        return (text.wl_list_text(scan_info=wl_scan.collect_scan_info(),
                                  last_scan=state.WL_LAST_SCAN),
                menu.wl_menu_buttons())
    if action == "wl_since":
        open_input_window("wl_since")
        return (
            "⏪ 回补白名单存量\n\n"
            "请发送：<序号|@用户名|ID> <消息id>\n"
            "例：1 88000 —— 把 1 号白名单聊天的扫描起点设为 #88000，"
            "回补其后消息（受 Worker 节流控制，逐步转发）。\n\n"
            f"{config.LISTEN_INPUT_WINDOW_SECONDS} 秒内有效，"
            "发送 / 开头的命令可取消。",
            menu.back_home_buttons(),
        )
    if action == "wl_scan":
        return (wl_scan.summary_text(await wl_scan.scan_all(manual=True)),
                menu.wl_menu_buttons())
```

`bot_message_handler` 的 LISTEN 输入窗口消费块之后、`if from_id:` 之前加：

```python
    # 白名单回补等待窗口：普通文本当作「<聊天> <消息id>」（/ 开头退出窗口）。
    if state.WL_INPUT_UNTIL and time.monotonic() < state.WL_INPUT_UNTIL:
        if not text.startswith("/"):
            state.WL_INPUT_UNTIL = 0.0
            await _handle_wl_since_input(event, text)
            return
        state.WL_INPUT_UNTIL = 0.0
```

文件末尾附近新增：

```python
async def _handle_wl_since_input(event, text):
    """白名单回补窗口的输入：一行「<序号|@用户名|ID> <消息id>」。"""
    parts = text.strip().split()
    if len(parts) != 2:
        await state.bot_client.send_message(
            state.MY_ID, "❌ 格式：<序号|@用户名|ID> <消息id>，例：1 88000")
        return
    ok, msg = await wl_scan.since_checkpoint(state.client, parts[0], parts[1])
    await state.bot_client.send_message(state.MY_ID, msg)
    if ok:
        asyncio.create_task(wl_scan.scan_all(manual=True))
```

bot.py 文件头 import 区补 `from . import wl_scan`（`asyncio` 若缺则补）。

- [ ] **Step 4: 跑测试**

Run: `.venv/bin/python -m unittest tests.test_whitelist tests.test_commands tests.test_menu tests.test_wl_scan -v`
Expected: PASS。若既有用例断言了旧版 `wl_list_text` 正文措辞，**更新断言**以匹配新文案（「📋 下载白名单」前缀不变）。

- [ ] **Step 5: 全量 + Commit**

Run: `.venv/bin/python -m unittest discover -s tests -p "test_*.py" -v`

```bash
git add tg_userbot/whitelist.py tg_userbot/text.py tg_userbot/commands.py tg_userbot/menu.py tg_userbot/bot.py tests/test_whitelist.py tests/test_commands.py tests/test_menu.py
git commit -m "功能：/wl since 回补 + /wl scan + 白名单视图与菜单接入扫描链"
```

---

### Task 5: app.py 事件生产者改造 + 扫描循环接线 + 删除直转代码

**Files:**
- Modify: `tg_userbot/app.py`（imports；`new_message_handler:535-540`；新增 `record_whitelist_media` / `_record_single` / `_record_album_member` / `_record_album_group` / `_fallback_direct_download` / `_whitelist_scan_loop`；删除 §4.4 清单；`main()` 接线）
- Modify: `tg_userbot/platform.py:1-8`（docstring 链路描述）
- Test: `tests/test_wl_event.py`（新建）

**Interfaces:**
- Consumes: Task 1 runtime_db 签名；Task 2 `wl_scan.all_members_dedup_hit`、`listener.build_saved_messages_task`；Task 4 已就绪的命令面。
- Produces:
  - `app.record_whitelist_media(message, chat_id, source_override)`（协程；单条走 `_record_single`，grouped_id 非空走 `_record_album_member` 攒批协调）
  - `app._whitelist_scan_loop()`（常驻；`RUNTIME_DB_READY` 门槛；不跟随 LISTEN_ENABLED）

- [ ] **Step 1: 写失败测试（新建 tests/test_wl_event.py）**

```python
"""白名单事件生产者（app.record_whitelist_media）的单元测试。

契约（规格书 §4）：
1. 单条媒体 → listener_tasks 一条任务（origin='wl'，目标收藏夹 + download），
   payload 带 source_name（目录兜底）与 caption。
2. 相册 → 攒批协调器把整组合成**一条**任务（member_ids 进 payload）。
3. dedup 前置：整组全命中 → 不建任务。
4. DB 不可用（DbUnavailable）→ 回退直下原消息（app.enqueue_media），媒体不丢。
"""
import asyncio
import atexit
import os
import shutil
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tg_userbot_wl_event_test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import app  # noqa: E402
from tg_userbot import runtime_db  # noqa: E402
from tg_userbot import state  # noqa: E402
from tg_userbot import wl_scan  # noqa: E402
from tg_userbot.runtime_db import DbUnavailable  # noqa: E402

SRC = -1001234567890


class FakeFile:
    def __init__(self, fid="file-1", size=100, name="v.mp4"):
        self.id = fid
        self.size = size
        self.name = name
        self.mime_type = "video/mp4"


class FakeMsg:
    def __init__(self, mid, text="", grouped_id=None, chat_id=SRC):
        self.id = mid
        self.message = text
        self.grouped_id = grouped_id
        self.chat_id = chat_id
        self.date = None
        self.fwd_from = None
        self.file = FakeFile()
        self.document = object()
        self.video = object()
        self.photo = None


def _wl_tasks():
    return [t for t in runtime_db.list_listener_tasks() if t["origin"] == "wl"]


class RecordWhitelistMediaTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())
        p1 = mock.patch.object(state, "WHITELIST_CHATS", {SRC: "测试频道"})
        p1.start()
        self.addCleanup(p1.stop)
        p2 = mock.patch.object(wl_scan.dedup, "should_skip",
                               lambda keys: (False, ""))
        p2.start()
        self.addCleanup(p2.stop)
        # 事件生产者不做评论继承回源（单测不联网）：resolve 恒 None
        p3 = mock.patch.object(app, "resolve_origin_snapshot",
                               mock.AsyncMock(return_value=None))
        p3.start()
        self.addCleanup(p3.stop)

    async def test_single_media_becomes_task(self):
        msg = FakeMsg(10, text="标题")
        await app.record_whitelist_media(msg, SRC, "测试频道")
        tasks = _wl_tasks()
        self.assertEqual(len(tasks), 1)
        t = tasks[0]
        self.assertEqual(t["source_chat_id"], SRC)
        self.assertEqual(t["message_id"], 10)
        self.assertEqual(t["target_type"], "saved_messages")
        self.assertEqual(t["download"], 1)
        self.assertEqual(t["payload"]["source_name"], "测试频道")
        self.assertEqual(t["payload"]["caption"], "标题")

    async def test_dedup_hit_skips_task(self):
        msg = FakeMsg(11)
        p = mock.patch.object(wl_scan.dedup, "should_skip",
                              lambda keys: (True, ""))
        p.start()
        self.addCleanup(p.stop)
        await app.record_whitelist_media(msg, SRC, "测试频道")
        self.assertEqual(_wl_tasks(), [])

    async def test_db_unavailable_falls_back_direct_download(self):
        msg = FakeMsg(12)
        enqueued = []

        async def fake_enqueue(message, chat_id, source_override, **kw):
            enqueued.append((message.id, chat_id, source_override))

        def boom(*a, **kw):
            raise DbUnavailable("db down")
        with mock.patch.object(runtime_db, "enqueue_listener_tasks", boom), \
                mock.patch.object(app, "enqueue_media", fake_enqueue):
            await app.record_whitelist_media(msg, SRC, "测试频道")
        self.assertEqual(enqueued, [(12, SRC, "测试频道")])

    async def test_album_becomes_one_task(self):
        p1 = mock.patch.object(app, "_WL_ALBUM_DEBOUNCE_SECONDS", 0)
        p2 = mock.patch.object(app, "_WL_ALBUM_SETTLE_SECONDS", 0)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        members = [FakeMsg(20, grouped_id=77), FakeMsg(21, grouped_id=77, text="相册说明")]
        fetched = mock.AsyncMock(return_value=members)
        p3 = mock.patch.object(app, "_fetch_album_members", fetched)
        p3.start()
        self.addCleanup(p3.stop)

        await app._record_album_member(members[0], SRC, "测试频道")
        await app._record_album_member(members[1], SRC, "测试频道")
        key = (SRC, 77)
        await app._WL_ALBUM_TASKS[key]["task"]   # 等协调任务跑完

        tasks = _wl_tasks()
        self.assertEqual(len(tasks), 1)
        t = tasks[0]
        self.assertEqual(t["message_id"], 20)
        self.assertEqual(t["payload"]["member_ids"], [20, 21])
        self.assertEqual(t["payload"]["caption"], "相册说明")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m unittest tests.test_wl_event -v`
Expected: FAIL —— `record_whitelist_media` / `_WL_ALBUM_*` 不存在。

- [ ] **Step 3: 实现 app.py**

3a. imports 补 `from . import wl_scan`（现有 `from . import listener` 附近）。

3b. `new_message_handler` 的白名单分支（现 535-540 行）改为：

```python
        if is_me:
            asyncio.create_task(_enqueue_me(message))
        else:
            # 白名单双通道（2026-09-13）：事件生产者只**记任务**，转发由
            # listener_worker 受控执行；停机漏掉的由 wl 扫描生产者按游标补。
            asyncio.create_task(
                record_whitelist_media(message, event.chat_id, source_override)
            )
```

3c. 在 `relay_chat_media` 原位置新增事件生产者（新函数，随后删除旧直转函数）：

```python
# ============================================================
# 白名单事件生产者：媒体事件 → 记持久化转发任务（转发归 listener_worker）
# ============================================================
# 相册攒批窗口与迟到成员宽限（与旧直转协调器同值，产出从「转发」变「任务」）
_WL_ALBUM_DEBOUNCE_SECONDS = 1.5
_WL_ALBUM_SETTLE_SECONDS = 5.0
# key=(chat_id, grouped_id) → {"seen": {msg_id: msg}, "task": Task, "done": bool}
_WL_ALBUM_TASKS = {}


async def record_whitelist_media(message, chat_id, source_override):
    """白名单 chat 媒体事件 → 记持久化转发任务（origin='wl'）。

    事件生产者只「记录」，不转发（与标签监听 Scanner 同构）：任务落
    listener_tasks，Worker 受控转发 + 入队下载。同一单元与扫描生产者并发
    产生时由唯一索引兜底，双转发结构上不可能。DB 不可用回退直下原消息
    （媒体不丢，只丢收藏夹副本——规格 §4.3）。
    """
    if getattr(message, "grouped_id", None):
        await _record_album_member(message, chat_id, source_override)
    else:
        await _record_single(message, chat_id, source_override)


async def _record_single(message, chat_id, source_override):
    """单条媒体 → 一条收藏夹任务。"""
    if wl_scan.all_members_dedup_hit([message]):
        logger.info(
            f"⏭️ 白名单媒体 {message.id} 已下载过（dedup 前置），不建转发任务")
        return
    origin = await resolve_origin_snapshot(message)
    caption = (message.message or "").strip()
    records = listener.build_saved_messages_task(
        chat_id, [message], caption, origin)
    try:
        runtime_db.enqueue_listener_tasks(chat_id, records,
                                          origin="wl", chain="wl")
        logger.info(f"📋 白名单媒体已记任务：{chat_id} #{message.id}")
    except runtime_db.DbUnavailable as e:
        logger.warning(
            f"📋 记白名单任务失败（DB 不可用），回退直下原消息 "
            f"#{message.id}：{e}")
        await _fallback_direct_download(message, chat_id, source_override,
                                        origin)


async def _record_album_member(message, chat_id, source_override):
    """相册成员事件：登记进组协调状态；首个成员负责起建任务协程。"""
    key = (chat_id, message.grouped_id)
    st = _WL_ALBUM_TASKS.get(key)
    if st is None:
        st = _WL_ALBUM_TASKS[key] = {"seen": {}, "task": None, "done": False}
    if st["done"]:
        return  # 整组已落盘；迟到的重复成员直接忽略（唯一索引双保险）
    st["seen"][message.id] = message
    if st["task"] is None:
        st["task"] = asyncio.create_task(
            _record_album_group(key, chat_id, source_override))


async def _record_album_group(key, chat_id, source_override):
    """攒批窗口后整组建任务：拉权威成员 → dedup 前置 → 一条任务。"""
    grouped_id = key[1]
    try:
        await asyncio.sleep(_WL_ALBUM_DEBOUNCE_SECONDS)
        st = _WL_ALBUM_TASKS.get(key)
        if st is None or st["done"]:
            return
        seen_ids = list(st["seen"].keys())
        if not seen_ids:
            return
        members = await _fetch_album_members(
            chat_id, grouped_id, min(seen_ids), max(seen_ids))
        if not members:
            # 权威列表拉不到（读源 chat 失败）→ 用已登记成员兜底；
            # 缺失成员由扫描链按游标补。
            members = [st["seen"][i] for i in sorted(seen_ids)]
            logger.warning(
                f"📋 相册建任务：未能从源 chat 取到完整成员"
                f"（grouped_id={grouped_id}，已登记 {len(members)} 个）")
        if wl_scan.all_members_dedup_hit(members):
            logger.info(
                f"📋 相册 {chat_id} 组 {grouped_id} 整组已下载过"
                "（dedup 前置），不建转发任务")
        else:
            caption = pick_group_caption_text(members, grouped_id)
            origin = await resolve_origin_snapshot(members[0])
            records = listener.build_saved_messages_task(
                chat_id, members, caption, origin)
            try:
                runtime_db.enqueue_listener_tasks(chat_id, records,
                                                  origin="wl", chain="wl")
                logger.info(
                    f"📋 白名单相册已记任务：{chat_id} 组 {grouped_id}"
                    f"（{len(members)} 个成员）")
            except runtime_db.DbUnavailable as e:
                logger.warning(
                    f"📋 相册建任务失败（DB 不可用），逐条回退直下：{e}")
                for m in members:
                    await _fallback_direct_download(m, chat_id,
                                                    source_override, origin)
        st["done"] = True
        await asyncio.sleep(_WL_ALBUM_SETTLE_SECONDS)
        _WL_ALBUM_TASKS.pop(key, None)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"相册建任务流程异常（grouped_id={grouped_id}）：{e}")
        st = _WL_ALBUM_TASKS.pop(key, None)


async def _fallback_direct_download(message, chat_id, source_override,
                                    origin=None):
    """回退直下原消息（DB 不可用；不转发、无收藏夹副本，媒体不丢）。"""
    try:
        await enqueue_media(
            message, chat_id, _origin_folder(origin) or source_override,
            parent_date=_origin_date(origin),
            parent_caption=_origin_caption(origin),
        )
    except Exception as e:
        logger.exception(f"白名单媒体直下回退也失败（msg_id={message.id}）：{e}")
```

3d. **删除**（§4.4 清单，均在现 app.py）：`relay_chat_media`（696）、`_relay_single`（712）、`_forward_to_me`（753）、`_relay_album_member`（894）、`_relay_album_group`（971）、`_forward_album_to_me`（942）、`_fallback_relay_each`（1049）、`_ALBUM_RELAY`（891）及旧 `_ALBUM_DEBOUNCE_SECONDS` / `_ALBUM_SETTLE_SECONDS`（888-889）。**保留**：`_ALBUM_SIBLING_RANGE`、`_ALBUM_CAPTIONS`、`_fetch_group_caption`、`_maybe_album_caption`（`_enqueue_me` 在用）、`_fetch_album_members`（新协调器在用）、`_record_me_label` / `_take_me_label`。

3e. `_listener_loop` 之后新增扫描循环：

```python
async def _whitelist_scan_loop():
    """白名单扫描生产者后台循环：按 WHITELIST_SCAN_INTERVAL_SECONDS 补漏。

    与标签监听是两套独立配置/游标（chain='wl'）；**不跟随 LISTEN_ENABLED**——
    白名单没有总开关概念，/wl del 移除聊天即停。在线时事件生产者兜实时，
    这里只负责补停机缺口，周期可以放宽。异常一律兜住；被 main 取消时以
    CancelledError 收尾。
    """
    if not state.RUNTIME_DB_READY:
        logger.warning("📋 白名单扫描循环未启动（Runtime DB 不可用）")
        return
    await asyncio.sleep(LISTEN_STARTUP_DELAY_SECONDS)
    while True:
        try:
            await wl_scan.scan_all()
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:
            logger.error(f"📋 白名单扫描异常（{type(e).__name__}: {e}）")
        await asyncio.sleep(config.WHITELIST_SCAN_INTERVAL_SECONDS)
```

（`config` 已以 `from .config import …` 方式导入——把 `WHITELIST_SCAN_INTERVAL_SECONDS` 加进那个 import 列表，或改用 `from . import config` 局部引用；与周边代码风格一致即可。）

3f. `main()` 接线：`listener_worker_task = listener_worker.start_worker()` 块之后加：

```python
    # 白名单扫描生产者（停机补漏链）：与标签监听扫描并列的独立循环
    wl_scan_task = asyncio.create_task(_whitelist_scan_loop())
```

并把 `wl_scan_task` 加入 finally 里**两个**取消/await 元组（`main_serve_task, bot_keepalive_task, …`）。

3g. `platform.py` 模块 docstring 第 3-5 行的白名单链路描述改为：

```
旧版「链接 → 平台对话 → 点按钮 → 平台自下」链路已删除：解析 bot 的私聊
在下载白名单上，其回复的直发视频由白名单事件生产者（app.record_whitelist_media）
记为转发任务、经 listener_worker 转发进 Saved Messages → 走统一媒体下载
（落转发来源目录，不再写 Douyin/Instagram 子目录）。
```

- [ ] **Step 4: 跑测试**

Run: `.venv/bin/python -m unittest tests.test_wl_event -v`
Expected: PASS。

- [ ] **Step 5: 全量 + Commit**

Run: `.venv/bin/python -m unittest discover -s tests -p "test_*.py" -v`
Expected: 全 PASS（`grep -rn "relay_chat_media\|_relay_single\|_forward_album_to_me" tg_userbot/ tests/` 应无残留引用——platform.py docstring 已同步）。

```bash
git add tg_userbot/app.py tg_userbot/platform.py tests/test_wl_event.py
git commit -m "功能：白名单事件生产者化 —— 媒体事件记任务、Worker 统一转发，删除直转代码"
```

---

### Task 6: 台账 origin 口径 + CLAUDE.md 同步 + 全量回归

**Files:**
- Modify: `tg_userbot/stats.py:327-334`（`_listener_task_stats`）
- Modify: `tg_userbot/listener.py:1274`（`view_text` 的 `get_listener_stats()`）
- Modify: `CLAUDE.md`（多处）
- Test: `tests/test_stats.py`（追加口径用例）；全量回归

**Interfaces:**
- Consumes: Task 1 的 `get_listener_stats(origin=)`。
- Produces: 台账「📡 标签监听」分节与 `/listen` 视图只统计 origin='listen' 的任务。

- [ ] **Step 1: 写失败测试（追加到 tests/test_stats.py）**

（沿用该文件的 DB 夹具形态；若该文件目前不 init runtime_db，则在测试内 `runtime_db.init_db()` + addCleanup close。）

```python
class ListenerTaskStatsOriginTest(unittest.TestCase):
    """台账的 📡 标签监听 分节只统计 origin='listen' 的任务（§37 v3 口径）。"""

    def setUp(self):
        runtime_db.close_db()
        self.addCleanup(runtime_db.close_db)
        self.assertTrue(runtime_db.init_db())

    def test_wl_tasks_not_counted_in_listen_section(self):
        ids = runtime_db.enqueue_listener_tasks(
            -1001234567890,
            [{"message_id": 1, "grouped_id": None,
              "target_type": "saved_messages", "target_chat_id": None,
              "download": 1, "payload": None}],
            origin="wl")
        runtime_db.complete_listener_task(ids[0])
        stats_map = runtime_db.get_listener_stats(origin="listen")
        self.assertEqual(stats_map["total"], 0)   # wl 成功不进 listen 口径
        stats_map = runtime_db.get_listener_stats(origin="wl")
        self.assertEqual(stats_map["success"], 1)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m unittest tests.test_stats -v`
Expected: FAIL（若 Step 3 未做时 get_listener_stats 尚无 origin 参数则 Task 1 已实现——本步失败点在 `_listener_task_stats` 仍不过滤：手工断言 stats.py 渲染路径）。若 Task 1 已使该用例直接通过，则本步记录 PASS 并直接进入 Step 3 的调用点修改（口径改动在调用方）。

- [ ] **Step 3: 改调用点**

3a. `stats.py` `_listener_task_stats` 返回行改为：

```python
        return runtime_db.get_listener_stats(since=since, origin="listen")
```

3b. `listener.py` `view_text` 里：

```python
            q = runtime_db.get_listener_stats(origin="listen")
```

- [ ] **Step 4: CLAUDE.md 同步（逐处）**

1. **Overview 第 4 条**：改述白名单机制——「Media arriving in any chat on the download whitelist … is **recorded as a persisted forward task** (origin='wl') and forwarded to Saved Messages by the resident listener_worker; a whitelist scan producer sweeps each whitelisted chat by message-id cursor (`chain='wl'`) to backfill anything missed while the process was down; `/wl since <chat> <msgid>` sets the backfill cursor」。
2. **Module index**：`app.py` 角色描述里白名单转发改为「事件生产者（record_whitelist_media + 相册建任务协调器）」；新增一行 `wl_scan.py`（白名单扫描生产者：wl 游标分页扫描、dedup 前置、/wl since、视图助手）。
3. **「Download whitelist (unified relay)」整节**：改写为双通道架构（事件生产者 + 扫描生产者 + Worker 统一执行），保留「转发副本带「转发自」头、fwd_from 解析目录、按钮保留代价」等不变语义的描述；删去 `_relay_single`/`_forward_album_to_me` 等已不存在函数的描述，相册整组语义改由「协调器产出一条 member_ids 任务、Worker 整组一次 forward」表述。
4. **「标签监听」段 §14 表述**：补一句「白名单改造后同一消息的收藏夹任务在两链间由唯一索引互斥（origin 列）」。
5. **「标签监听配置指引」表**：新增三行——`WHITELIST_SCAN_INTERVAL_SECONDS` / `WHITELIST_SCAN_PAGES_PER_ROUND` / `WHITELIST_SCAN_PAGE_SLEEP_SECONDS`（改哪：config.py；生效：需重启）。
6. **「台账」段**：输入事件三桶说明更新——白名单中转现在显式 `src='wl'`（`listener_worker._enqueue_copy` 传任务 origin），不再被数进收藏桶。
7. **Running 节测试清单**：补 `test_wl_scan.py` / `test_wl_event.py` 的一句话描述。

- [ ] **Step 5: 全量回归**

Run: `.venv/bin/python -m unittest discover -s tests -p "test_*.py" -v`
Expected: 全 PASS，零 skip 新增。

- [ ] **Step 6: Commit**

```bash
git add tg_userbot/stats.py tg_userbot/listener.py tests/test_stats.py CLAUDE.md
git commit -m "文档：白名单双通道改造全量同步（台账 origin 口径 + CLAUDE.md）"
```

---

## Self-Review 记录

- **规格覆盖**：§3 数据模型→Task 1；§4 事件生产者（含相册/回退/删除清单）→Task 5；§5 扫描生产者→Task 2+Task 5(循环)；§6 Worker→Task 3；§7 命令视图→Task 4；§8 风控（分页/背压/dedup 前置/周期）→Task 1/2 实现+测试断言；§9 台账与文档→Task 6；§10 测试清单逐条映射到各任务测试步骤。
- **占位符扫描**：无 TBD/TODO；所有代码步骤给出完整代码；两处「以文件现状为准」（test_listener_worker 的 `_run` 形态、test_commands 的 FakeEvent 属性名）是对既有夹具的适配指引而非空泛指令。
- **类型一致性**：`get_listener_checkpoint(..., chain)`、`enqueue_listener_tasks(..., chain=, origin=)`、`build_saved_messages_task(chat_id, media, caption, origin)`、`scan_wl_chat(chat_id, nap)`、`since_checkpoint(cli, chat_token, msgid_text)` 在定义与消费两端签名一致；`state.WL_INPUT_UNTIL`/`WL_LAST_SCAN`、`app._WL_ALBUM_DEBOUNCE_SECONDS`/`_WL_ALBUM_SETTLE_SECONDS`/`_WL_ALBUM_TASKS` 命名前后一致。
