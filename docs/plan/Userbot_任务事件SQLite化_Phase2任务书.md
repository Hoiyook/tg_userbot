# Userbot 任务事件 SQLite 化改造任务书（Phase 2）

> 仓库：`Hoiyook/tg_userbot`
>
> 分支：`feature/queue-sqlite`（或其合并后的新分支 `feature/events-sqlite`）
>
> 基线提交：队列 SQLite 化提交（`download_tasks`/schema v4 已落地）
>
> 前置阅读：`docs/plan/Userbot_下载队列SQLite化_DeepSeek任务书.md`（Phase 1，双模式/导入/降级纪律全部沿用）
>
> **目标：把下载任务事件流从 `runtime/task_events.jsonl` 迁到 Runtime DB 的 `download_events` 表；stats/reporter 从全文件扫描/字节游标换成行存储。**
>
> **本任务只换存储，不改变事件词表、发射点、台账口径、面板行为。**

---

# 1. 背景与动机

事件流（`task_events.jsonl`，append-only JSONL）是台账/对账与 Reporter 通知的数据源，现状缺陷：

1. **stats 全量扫描**：`load_events()` 每次读+解析整个文件（3 万行封顶实测
   116ms 同步阻塞事件循环；reporter 靠 300s 缓存 + dirty 标记硬扛）。
2. **reporter 字节偏移游标**：`poll_events()` 按文件 size 增量读，还要为
   `trim_event_file` 的原子重写打「size 变小 → 重置游标」的补丁。
3. **启动裁剪全量重写**：`trim_event_file` 读全部 → 保尾 3 万条 → 原子重写。
4. **命名地雷**：DB 里已有一张 listener 的 `task_events` 表（自增 id，
   外键 listener_tasks），与 JSONL 是**两套 ID 空间**（listener 自增 id vs
   下载任务 uuid），CLAUDE.md 明确警告绝不混用——文件态加剧混淆。
5. **队列 SQLite 化（Phase 1）之后**，事件流是业务状态里最大的一块文件残留。

---

# 2. 现状代码事实（Phase 1 审计底稿，实现前逐条复核）

## 2.1 事件 dict 形态（所有消费方的共同契约）

```python
# stats.emit_event 写出的 rec：
{"ts": "2026-09-13 21:20:48",      # 本地时间串，秒精度，"%Y-%m-%d %H:%M:%S"
 "ev": "SUCCESS",                  # 事件类型（词表见下）
 "id": "<uuid4.hex>",              # 下载任务 task_id；输入侧事件**无此键**
 "label": "26-09-06 测试.mp4",     # 制表/换行压空格，截 80 字符
 "bytes": 200000, "src": "me", ... # extra：None 值不写
}
```

事件类型词表（**不得增删改**）：`RECEIVED / QUEUED / RUNNING / RETRY /
FAILED / SUCCESS / CANCELLED / REMOVED / DEDUP_SKIPPED / DEDUP_HIT /
AUTO_REPLAY / LISTEN_SCAN / LISTEN_FAIL`。
无 task_id 的事件：`DEDUP_SKIPPED`、`LISTEN_SCAN`、`LISTEN_FAIL`。

## 2.2 读写点清单

| # | 位置 | 职责 |
|---|---|---|
| 1 | `stats.emit_event`（stats.py:58） | **唯一写入点**；JSONL 单行 append，失败仅告警 |
| 2 | `stats.load_events`（stats.py:79） | 全量读 + JSON 解析 + 坏行跳过；stats_text(:456) 与 reporter.collect_stats(:474) 消费 |
| 3 | `stats.trim_event_file`（stats.py:100） | 保尾 3 万条原子重写；**app.py:1143 启动时调用** |
| 4 | `reporter.mark_event_cursor/_event_file_size/poll_events`（reporter.py:695-730） | 字节偏移增量读；size 回退=trim 重写 → 重置游标 |
| 5 | `stats.rebuild_stats` / `_event_text` | **纯函数**消费 event dict 列表——本任务零改动 |

调用 emit_event 的模块（语义不动）：queue / download / app / listener 链路。

## 2.3 reporter 的两个语义锚点（迁移必须逐字保留）

- `mark_event_cursor()`：启动时游标 = 文件末尾——**历史事件不重放通知**。
  DB 模式对应「游标 = 当前 MAX(id)」。
- `collect_stats` 的缓存与 dirty 标记机制不动。

---

# 3. 架构决策（审计重点）

## 3.1 dict 接口不动，存储在下面换（与队列同款纪律）

所有消费方（rebuild_stats 纯函数、reporter 通知分发）都吃 **event dict**。
本任务只把 dict 的来源从「读文件」换成「读行」，dict 形状逐字段兼容
（含 ts 仍是本地时间串、无 task_id 时无 `id` 键）。**禁止**把 SQL 推进
rebuild_stats——它是纯函数、有完整测试，动它就是行为变更。

## 3.2 新表 `download_events`，listener 的表一眼不看

命名避开 `task_events`（已被 listener 占用）；两套 ID 空间继续分开、
CLAUDE.md 的警告原样保留。**事件与 download_tasks 之间不建外键**（刻意）：
事件流是独立台账，不与任务行建引用关系，避免「事件先于任务行/任务删行后
事件被级联」的边角。

## 3.3 双模式：DB 优先，未连接回落 JSONL

`has_connection()` 判断（比队列的 RUNTIME_DB_READY 更底层、更准确）：
DB 已连接 → 走表；未连接（测试环境 / init 失败）→ 沿用 JSONL 旧路径。
这样**既有 stats/reporter 测试零改动**（它们不 init DB，自然走 JSONL 路径，
继续证明 dict 形状契约），生产 init 失败时事件照常落文件。
**不设 `TG_EVENTS_STORE` 开关**：事件是台账（可观测性数据）不是不可再生
的业务状态，写失败仅告警本来就是今天的行为；回滚 = 代码回退，不需要
运行时开关。

## 3.4 不做「事件与队列同事务」

SUCCESS 事件带 bytes、在 download.py 发，与队列收尾块不同层；强行同事务
要把下载结果穿进收尾块——动业务逻辑。列出但**明确不做**，留作未来评估。

---

# 4. 表设计

```sql
CREATE TABLE IF NOT EXISTS download_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,  -- 全事件流单调序 = reporter 游标
    ts        INTEGER NOT NULL,                   -- epoch 秒（读出时渲染回本地串）
    ev        TEXT NOT NULL,
    task_id   TEXT,                               -- 下载任务 uuid；输入侧事件为 NULL；无外键
    payload   TEXT                                -- 其余字段 JSON（label/src/why/attempts/bytes/kind/...）
);
CREATE INDEX IF NOT EXISTS idx_download_events_ts ON download_events (ts);
CREATE INDEX IF NOT EXISTS idx_download_events_task ON download_events (task_id, id);
```

- **ts 存 epoch 秒**（现有串就是秒精度）：窗口查询/未来聚合要数值；读出时
  `datetime(ts,'unixepoch','localtime')` 等价渲染，dict 形状不变。
  导入解析：`int(time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S")))`，
  解析失败的行跳过并计数。
- Schema 版本 v4 → **v5**；`download_events` 进 `_SCHEMA`（幂等）；
  **禁止 ALTER listener 已有表**。

---

# 5. runtime_db 新增函数契约

```python
def download_event_insert(ev, task_id=None, payload=None, ts=None) -> None
    """单行 INSERT（payload = emit rec 除 ts/ev/id 外的 dict）。短事务。"""

def download_events_since(last_id, limit=2000) -> list[dict]
    """reporter 增量：WHERE id > ? ORDER BY id LIMIT ?，
    返回与 JSONL rec 同形状的 dict 列表（见 5.1）。"""

def download_events_all() -> list[dict]
    """load_events 的 DB 等价：全量按 id 升序 → rec dict 列表。"""

def download_events_count() -> int

def download_events_trim(keep) -> int
    """保尾 keep 条：DELETE 最旧的，返回删除行数。单事务；
    rowid 单调性不受删旧影响（reporter 游标无需重置——旧文件 size 补丁消亡）。"""
```

## 5.1 行 → rec dict（形状兼容的唯一定义点）

```python
{"ts": <本地串 "YYYY-mm-dd HH:MM:SS">,
 "ev": <ev>,
 "id": <task_id>,          # 仅 task_id 非空时**才放这个键**（与 JSONL 逐字对齐）
 **payload}                # payload 平铺；与 ts/ev/id 冲突时以列值为准
```

`rebuild_stats` 的日期匹配是 `str(e.get("ts",""))[:10]`——ts 渲染串必须与
旧格式逐字符一致（本地时区）。

---

# 6. stats.py 接线规范

- **`emit_event` 签名与词表零改动**。函数体：组 rec（label 清洗逻辑原样）
  → `has_connection()` ？ `download_event_insert(ev, rec.get("id"), payload)`
  : JSONL append（旧代码原样）。两类失败都只 `logger.warning`（文案沿用
  「写任务事件失败（不影响下载）」），**绝不抛**。
- **`load_events` 同款双模式**：连接在 → `download_events_all()`；否则
  JSONL 旧代码。`path` 参数保留（测试与 JSONL 路径用）。
- **`trim_event_file` 保留**为 JSONL 模式专用；新增：

```python
def migrate_and_trim_events():
    """启动入口（app.py:1143 的 stats.trim_event_file() 替换为它）：
    ① 一次性导入决策树（§7）② 按当前模式裁剪（DB → download_events_trim；
    JSONL → trim_event_file）。"""
```

---

# 7. 一次性导入（决策树与队列同构）

```text
init_db 失败 / 未连接            → 不导入，走 JSONL 模式（下次启动再试）
DB 连接 + 表空 + JSONL 非空      → 逐行导入（坏行跳过计数）+ INFO 汇总
                                  + 改名 task_events.jsonl.imported（绝不删除）
DB 连接 + 表有行 + JSONL 存在    → DB 赢，JSONL 归档 + WARNING（含行数）
DB 连接 + JSONL 缺失/空          → 无动作
导入中途失败（含 DB 异常）       → WARNING + 保留原 JSONL + 不改名；
                                  新事件已流向 DB（旧历史查询不到，可接受——
                                  台账是可观测数据，不是业务状态）
```

导入幂等：`.imported` 已存在且表空 → INFO「此前已导入」+ 无动作。

---

# 8. reporter.py 接线规范

- **`mark_event_cursor`**：DB 模式 → `_event_cursor = 当前 MAX(id)`
  （`download_events_count`==0 时为 0）；JSONL 模式 → 文件 size（原样）。
- **`poll_events`**：DB 模式 → `download_events_since(_event_cursor)` →
  逐条派发（分发代码零改动）→ 游标推进到最后一条的 id；空结果直接返回。
  JSONL 模式 → 旧字节偏移代码原样保留（含 size 回退重置补丁——只在
  JSONL 路径有意义）。
- reporter 仍是**只读观察者**：对 download_events 只有 SELECT。
- `collect_stats` 的缓存/dirty 机制、全量 `load_events` 口径**本任务不动**
  （「since 增量重算」是行为优化，列未来）。

---

# 9. 禁止事项

- 禁止动 listener 的 `task_events` 表与其读写路径；禁止统一两套 ID 空间。
- 禁止改 `emit_event` 签名、事件词表、label 清洗规则、None 值不落 extra 的行为。
- 禁止改 `rebuild_stats`/`_event_text`/`_legacy_text` 纯函数与台账口径。
- 禁止改 reporter 的通知分发逻辑与缓存机制。
- 禁止做「事件与队列同事务」（§3.4）与「collect_stats 增量重算」。
- 禁止动 `download.log` / `download_history.txt` / `dedup_index.txt`
  （Phase 3/4 另立任务书）。
- 禁止让 Chrome Agent 进程碰 DB。
- 禁止顺手修无关问题。

---

# 10. 测试要求（TDD，先测后码）

新增 `tests/test_events_db.py`（表/函数层，基座复用 test_queue_db 模式）与
`tests/test_events_wiring.py`（接线/导入/reporter）：

1. **rec 形状 parity**：带 label/src/bytes 的事件 insert → 读回 dict 与
   原 rec 逐键相等（ts 本地串格式一致）。
2. **无 task_id 事件**（DEDUP_SKIPPED/LISTEN_SCAN）：读回 dict **无 `id` 键**。
3. **since 游标**：插 5 条 → since(2) 返回第 3-5 条按序；since(MAX) → 空。
4. **trim 保尾**：插 10 条 trim(4) → 最旧 6 条删、新 4 条在；之后 insert 的
   rowid 继续单调（游标语义不破）。
5. **emit_event DB 模式**：落表；DbUnavailable → WARNING 不抛（assertLogs）。
6. **emit_event / load_events 无 DB 模式**：回落 JSONL 文件（既有行为）。
7. **双模式形状 parity**：同一组事件分别走 JSONL 与 DB，`load_events()`
   返回的 list 相等（这是消费方零改动的证明）。
8. **golden parity（关键）**：同一事件集（覆盖全部词表 + 跨天 + 无 id 事件）
   分别经旧文件路径与新 DB 路径喂 `rebuild_stats` → 输出文本**逐字符相等**。
9. **导入**：构造 JSONL（unicode、坏行、ts 坏串、无 id 行）→ 导入行数/字段
   无损 → `.imported` 改名 → 幂等重跑。
10. **导入冲突**：表有行 + JSONL 在 → DB 赢 + WARNING + 归档。
11. **reporter 游标**：mark = MAX(id)；新事件 poll 只回新增；trim 后 poll
    不丢不重（DB 模式无 size 重置）。
12. **v4→v5 迁移**：v4 库升级后表在、listener 表原样、幂等。

既有 `tests/test_stats.py` / `test_reporter.py` **必须原样全绿**（JSONL
回落路径未变的证明）。

---

# 11. 回归要求

```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
PYTHONPATH=. .venv/bin/pytest -q
```

真机验证清单（架构师执行）：

```text
1. ./run.sh restart → 启动日志：v5 迁移 + 「已导入 n 条历史事件」（n≈2.1 万）
   + task_events.jsonl → .imported
2. /stats 今日输出与迁移前一致（golden 对账）
3. 转发一条媒体 → 事件通知照常（reporter DB 模式 poll 生效）→ 下载完成后面板刷新
4. /sql: SELECT ev, COUNT(*) FROM download_events GROUP BY ev —— 词表与 JSONL 时代一致
5. /sqlt 队列 照常（Phase 1 不回归）
6. 下载进行中 kill 进程 → 重启 → 台账连续（事件流无断档）
```

---

# 12. 实施顺序

```text
Phase 1  复核 §2 审计底稿，输出差异报告
Phase 2  TDD：schema v5 + 5 个函数（测试 1-4、12）
Phase 3  TDD：emit_event / load_events / trim 双模式（测试 5-8）
Phase 4  TDD：reporter 游标切换（测试 11）
Phase 5  TDD：migrate_and_trim_events 导入 + app.py 接线（测试 9-10）
Phase 6  全量回归 + golden parity
Phase 7  真机验证清单（§11）
Phase 8  提交（含 CLAUDE.md：download_events 段 + 两套 ID 空间警示更新）
```

---

# 13. 完成后自查

```text
□ grep TASK_EVENTS_FILE：只在 JSONL 回落路径、导入函数与 config 定义出现
□ emit_event 调用点（约 10 处）git diff 为零
□ rebuild_stats/_event_text/_legacy_text git diff 为零
□ download_events 无外键、无对 listener 表的任何引用
□ reporter 对 DB 只有 SELECT
□ 旧 JSONL 只改名未删除；.imported 幂等
```

---

# 14. 架构师最终审计重点

1. **golden parity**：亲手对账迁移前后 `/stats 今日`（迁移前先截屏留存）。
2. **dict 形状**：抽 10 条真实导入行 vs 原 JSONL 行逐键 diff（含无 id 事件）。
3. **游标语义**：重启 → 旧事件不重发通知；新下载 → 通知即时（15s 内）。
4. **失败注入**：运行中把 DB 文件置坏 → 事件写失败仅 WARNING，下载照常。
5. **两套 ID 空间**：listener task_events 表 SELECT 前后行数一致。

---

# 15. Commit 要求

```text
功能：任务事件流 SQLite 化 —— download_events 表（Phase 2，schema v5）
```

提交前双套件全绿并汇报实际输出。CLAUDE.md 更新：台账段的数据源描述、
「两套 ID 空间」警示改为「两表分立（listener task_events / download_events）」、
模块索引 runtime_db 角色加 v5。

---

# 16. 最终原则

> **事件词表、发射点、台账口径、面板行为一行不动；SQLite 只换「存哪」不换「怎么算」。
> dict 形状是消费方的契约，逐字段兼容；DB 优先、JSONL 回落；坏数据跳过不崩；
> 旧文件只改名不删除；golden parity 是验收的唯一硬标准。**
