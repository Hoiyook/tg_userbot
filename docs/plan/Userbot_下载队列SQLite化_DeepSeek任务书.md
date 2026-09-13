# Userbot 下载队列 SQLite 化改造任务书

> 仓库：`Hoiyook/tg_userbot`
>
> 分支：从最新 `main`（或 `feature/tag-listener` 合并后）拉新功能分支 `feature/queue-sqlite`
>
> 基线提交：`a6b72f0`（数据根目录迁移与路径解耦）
>
> 执行方式：DeepSeek Coding 负责实现；架构师负责最终审计。
>
> **目标：把下载队列的持久化层从 `runtime/download_queue.json`（全量重写）换成 Runtime DB（`tg_userbot.db`）的 `download_tasks` 表。**
>
> **本任务只换持久化层，不改变任何队列业务逻辑、命令行为、展示样式。**

---

# 1. 背景与动机

队列是下载子系统的核心业务状态（排队中 + 待重试）。当前持久化方式：

```python
# queue.py:157 每次状态变更都全量 JSON dump + os.replace
def save_queue(queue, path=None):
    temp_path = path + ".tmp"
    json.dump(queue, f, ...)
    os.replace(temp_path, path)
```

真实缺陷：

1. **每次变更 O(n) 全量重写**。入队/成功/失败/删除/重试，每次都把整个队列
   重写一遍；队列大了以后每次操作的成本线性涨。
2. **单任务原子性缺失**。两个任务几乎同时收尾时，后写的全量快照可能覆盖
   前一个刚写入的状态（同事件循环内被 QUEUE_LOCK 串行化，当前无实际事故，
   但结构上每加一个突变点都在赌锁的完备性）。
3. **与 Runtime DB 的既有方向脱节**。标签监听的任务（listener_tasks）已经
   落 SQLite：状态机、租约、重试、事务原子性全部就绪；下载队列是业务状态
   里最大的一块漏网之鱼。
4. **后续阶段的地基**。task_events.jsonl / download_history.txt / dedup_index
   的 SQLite 化（Phase 2/3/4）都依赖队列先就位——队列的任务 id 是所有台账
   事件的主键锚点。

---

# 2. 现状代码事实（Phase 1 审计的底稿，实现前须逐条复核）

## 2.1 数据模型

队列记录是**开放 dict**（queue.py / app.py），实测字段全集：

```text
id            uuid4().hex，32 位小写 hex —— 全链路 task_id（台账/trace/勾稽都认它）
kind          "media" | "url"（douyin/instagram 已退役，残留按未知类型移除）
attempts      已尝试次数（失败 +1，手动 /retry 也累加）
next_retry_at 自动重放到期时间戳（float，仅 retry 榜有意义）
label         原始文件名（展示用）
final_name    入队时算好的最终文件名（展示与下载命名共用）
chat_id/msg_id  media 任务的源消息引用
url/title     url 任务的直链与标题
source_override / source_link   来源目录覆盖 / 来源链接
album_caption / parent_caption / parent_date / user_label   命名链各 override 槽
dedup_keys    判重键列表（在途判重 + 成功后 remember 复用）
...           开放 dict，未来字段只增不改
```

容器结构：`state.QUEUE = {"tasks": [...], "retry": [...]}`，**列表顺序即展示
顺序与 `/queue 3`、`/retry del 2` 的序号语义**。

## 2.2 全部突变点（每个都要接线，一个不能漏）

| # | 位置 | 变更 |
|---|---|---|
| 1 | `queue.enqueue_and_start`（queue.py:382） | 入队 + save |
| 2 | `queue.execute_queued_task` 收尾块（queue.py:528-551） | 成功→从 tasks 移除 / 在 retry 成功→从 retry 移除 / 失败→转 retry / 在 retry 失败→原位累加，+ save |
| 3 | `queue.queue_del_task`（queue.py:98-114） | 从 tasks 移除 + save |
| 4 | `commands.py:307`（/retry del） | 从 retry 移除 + save |
| 5 | `bot.py:462`（菜单 retry_del） | 从 retry 移除 + save |

## 2.3 只读消费方（**必须保持零改动**）

全部读 `state.QUEUE` 内存字典：

```text
dedup.should_skip      在途判重（tasks + retry 扫两遍）
menu.py / commands.py  /queue /retry 列表视图与 bot 菜单视图
finder.py              /find 媒体下落查询（含队列在途）
reporter.py            面板「待处理/待重试」快照
stats.py               台账
app.recover_queue_tasks  启动重放 tasks
```

## 2.4 启动顺序（有坑，必须调整）

```text
app.py:1050  state.QUEUE = queue.load_queue()     ← 先
app.py:1096  runtime_db.init_db()                  ← 后
```

队列装载目前**早于** DB 初始化。本任务要求把 `init_db()` 块整体**前移**到
`state.QUEUE_LOCK = asyncio.Lock()` 之后、`queue.load_queue()` 之前，并保持
其既有失败语义（init 失败只降级、绝不让 userbot 起不来）。

## 2.5 runtime_db 既有纪律（全部沿用，不得违反）

```text
SQL 只在 runtime_db.py（§33 硬规矩），其他模块调函数
import 期绝不建连接；init_db() 由 main() 显式调用，返回 bool
单进程单连接、事件循环线程内；事务必须短
DbUnavailable = 等锁超限/SQL 错误 → 调用方记日志不崩
schema_meta 存版本；当前 RUNTIME_DB_SCHEMA_VERSION = 3
Chrome Agent 进程 import 本包但绝不开库 —— 单写者红线
```

---

# 3. 架构决策（审计重点，实现前先读懂）

## 3.1 内存工作副本 + write-through

**`state.QUEUE` 内存字典保持为唯一工作副本与读取面，DB 只是持久化层。**

```text
（不变）纯数据函数改内存字典：queue_enqueue / queue_fail_to_retry / ...
（替换）save_queue 全量重写  →  单行事务 write-through
（替换）load_queue 读 JSON   →  启动时从 DB SELECT 装载
（不变）9 个只读消费方继续读 state.QUEUE，一行不改
```

理由：队列的所有消费方都建立在「列表 + dict」语义上，改成 DB 直查（方案 B）
要动 9 个模块 + 重写全部列表序号逻辑，违背「不改变业务逻辑」的边界；
write-through 把爆炸半径压到「2 个函数 + 5 个突变点」。

## 3.2 失败语义与 JSON 时代逐字对齐

JSON 时代：内存改完 → save 失败仅 WARNING，**内存仍是权威**，重启丢最后一笔。
SQLite 时代完全同构：内存先改（纯函数），DB write-through 失败（DbUnavailable）
仅 WARNING、**不回滚内存**、绝不抛给下载链路。区别只是：JSON 失败丢的是整个
文件的新鲜度，DB 失败只丢那一行——严格更优。

**禁止**反向设计（DB 先行、内存跟随）——那会把 DB 故障升级成下载链路故障。

## 3.3 不引入 PROCESSING/租约状态

listener_tasks 有 PENDING/PROCESSING/lease，因为它的 Worker 跨重启消费任务。
下载队列的执行是**进程内**的：执行中记录留在 tasks（JSON 时代语义），EXECUTING
集合管在途判定（内存），重启后 `recover_queue_tasks` 重放全部 tasks。
表里只有 `QUEUED` / `RETRY` 两态，终态即删除行——与今天的 JSON 形态一一对应。
**禁止**顺手加租约/PROCESSING——那是行为变更，不是持久化层替换。

---

# 4. 表设计

```sql
CREATE TABLE IF NOT EXISTS download_tasks (
    id            TEXT PRIMARY KEY,      -- 沿用 uuid4().hex，绝不改 ID 方案
    kind          TEXT NOT NULL,
    state         TEXT NOT NULL,         -- 'QUEUED' | 'RETRY'（终态即删行）
    seq           INTEGER NOT NULL,      -- 全局单调序号，装载时 ORDER BY seq
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL,
    enqueued_at   INTEGER NOT NULL,
    payload       TEXT NOT NULL          -- 其余开放字段的 JSON（_dumps/_loads 复用）
)
```

要点：

- **seq 的语义 = 列表位置**。tasks 与 retry 各自按 seq 排序展示；
  入队/转 retry 分配 `MAX(seq)+1`（追加到尾部）；`queue_retry_failed`
  原位累加 **不改 seq**；删除后序号空洞无害（只用于 ORDER BY）。
- **payload 装「其余一切」**。记录是开放 dict，全列规范化要动大量模块且
  未来字段每次都要 ALTER TABLE；listener_tasks.payload 已确立 JSON payload
  先例。`dedup_keys` 等全部进 payload，round-trip 必须逐字段无损（含
  unicode、未知的未来字段——**未知字段也要保住**，禁止白名单过滤）。
- `enqueued_at` 记录入库时间（排查用，int epoch）。

Schema 版本：`RUNTIME_DB_SCHEMA_VERSION` 3 → **4**，`download_tasks` 进
`_SCHEMA`（CREATE IF NOT EXISTS 本身幂等），`migrate()` 对旧库执行建表。
禁止 ALTER 已有 listener 表。

---

# 5. runtime_db 新增函数契约

全部放 `runtime_db.py`（SQL 收口），签名与语义如下：

```python
def queue_load_all() -> list[dict]
    """SELECT 全部行 ORDER BY seq，还原成队列记录 dict 列表。
    每行: {**payload, "id","kind","attempts","next_retry_at"(float|None),
            "__state": "QUEUED"|"RETRY"} ；payload 里的同名字段以列值为准。"""

def queue_insert(record: dict, state: str, now=None) -> None
    """入队：seq = MAX(seq)+1，payload = 其余字段。短事务单行 INSERT。"""

def queue_move_to_retry(record_id: str, attempts: int, next_retry_at: float, now=None) -> None
    """fail_to_retry 对应：UPDATE state='RETRY', attempts, next_retry_at, seq=MAX+1。"""

def queue_update_retry(record_id: str, attempts: int, next_retry_at: float) -> None
    """retry_failed 对应：只 UPDATE attempts 与 next_retry_at，**不动 seq**。"""

def queue_delete(record_id: str) -> None
    """成功/移除对应：按 id DELETE。行不存在不报错（幂等）。"""

def queue_count() -> dict
    """{"queued": n, "retry": m}，启动日志与迁移判定用。"""
```

统一要求：

- 每个函数内部 `has_connection()` 判断；DB 未就绪 → 抛 `DbUnavailable`
  （调用方按 §3.2 降级）。**绝不静默吞**——降级决策在 queue.py，职责分离。
- 复用既有 BUSY 有限重试；单行事务，不做批量。
- `queue_load_all` 遇到坏 payload 行：跳过 + WARNING，不让一条坏行打死装载。

---

# 6. queue.py 接线规范

## 6.1 持久化选择器

```python
# config.py
QUEUE_STORE = os.environ.get("TG_QUEUE_STORE", "sqlite").strip().lower() or "sqlite"
# "sqlite" | "json"  —— json 是一键回滚开关（§10）
```

`queue.py` 内部：

```python
def _persist_ready():     # sqlite 模式且 state.RUNTIME_DB_READY
def _save_after_mutation(record, op): ...
    # op ∈ {"insert","to_retry","update_retry","delete"}
    # sqlite 模式 → 调 runtime_db 对应函数；DbUnavailable → WARNING 降级
    # json 模式 / DB 未就绪 → 沿用 save_queue(state.QUEUE) 旧路径
```

**5 个突变点全部改调 `_save_after_mutation`**（§2.2 表格逐一核对），内存
纯函数调用保持原样、顺序不变（先改内存、后持久化）。

`load_queue` 保留（JSON 模式与迁移导入还要用）；新增：

```python
def load_queue_from_db() -> dict
    """queue_load_all() → {"tasks":[...QUEUED...], "retry":[...RETRY...]}
    按 seq 排序；DB 未就绪/异常 → 返回 None（调用方回落 load_queue）。"""
```

## 6.2 旧函数的去留

- `save_queue` / `load_queue` **保留不删**：json 回滚模式、一次性迁移导入、
  单测都要用。加注释说明「仅 json 模式/迁移路径使用」。
- 纯数据函数（queue_enqueue 等）**一行不改**——它们的单测继续有效。

---

# 7. 启动顺序与一次性迁移导入

## 7.1 顺序调整（app.py main）

```text
（前移到队列装载之前）
if runtime_db.init_db():
    state.RUNTIME_DB_READY = True
    ...（listener.migrate_legacy_state 等原逻辑随块整体前移）
state.QUEUE_LOCK = asyncio.Lock()
state.QUEUE = queue.load_queue_any()      # 见 7.2
```

init_db 失败路径语义不变：WARNING + 继续启动（此时自动落 json 模式）。

## 7.2 load_queue_any 的迁移决策树

```python
def load_queue_any():
    if QUEUE_STORE != "sqlite" or not state.RUNTIME_DB_READY:
        return load_queue()                      # json 模式
    rows = queue_load_all() 有行？
    ├─ 有行：
    │    download_queue.json 存在？
    │    ├─ 是 → WARNING「DB 已有队列，忽略并归档旧 JSON（n 任务/m 重试）」
    │    │        rename → download_queue.json.imported
    │    └─ 否 → 无事
    │    return 由 rows 装载的 dict
    └─ 无行：
         download_queue.json 存在且非空？
         ├─ 是 → 逐行导入（seq 按 tasks→retry 列表顺序分配）
         │        enqueue 的 QUEUED 事件不补发（历史任务不打台账）
         │        rename → download_queue.json.imported + INFO 汇总
         └─ 否 → return 空队列
```

硬性要求：

- 导入**原样保字段**：payload 就是整个 record 减去四个列字段，round-trip
  无损（含 dedup_keys、未知字段）。
- **绝不删除旧 JSON**，只改名 `.imported`（先例：listen_state.json →
  `.migrated`）。
- 导入失败：WARNING + 保留原 JSON + 回落 json 模式启动，绝不阻塞。
- 幂等：`.imported` 已存在时不重复导入（表空 + 只有 .imported → 空队列 +
  INFO「旧队列已导入过，如需重导请手工恢复」）。

---

# 8. 降级矩阵（全部要有测试）

| 场景 | 行为 |
|---|---|
| `TG_QUEUE_STORE=json` | 全程 JSON，DB 一眼不看（回滚开关） |
| init_db 失败 | 自动落 json 模式 + WARNING，下载链路照常 |
| 运行中 write-through 抛 DbUnavailable | WARNING 仅告警，内存权威，不回滚不重试到死 |
| queue_load_all 坏行 | 跳过该行 + WARNING，其余照常装载 |
| DB 正常 | 单行事务，全量重写从此消失 |

---

# 9. 禁止事项

- 禁止改 `execute_queued_task` / `_run_queued_task` / `download_file` 的任何
  执行逻辑（真取消链路、`.download` 清理、worker 归还一处都不许碰）。
- 禁止改 EXECUTING 内存语义、recover_queue_tasks 行为。
- 禁止改 /queue、/retry、菜单的命令行为、序号语义、展示文本。
- 禁止改 task_id 生成方案（uuid4().hex）。
- 禁止把 task_events.jsonl / download_history / dedup_index 一起迁了
  （Phase 2/3/4 另立任务书；本任务连「事件与队列同事务」都不做）。
- 禁止让 Chrome Agent 进程碰 DB（连 import 路径上的连接都不许新增）。
- 禁止 ALTER listener_* 已有表；禁止重设计 runtime_db 的事务/BUSY 机制。
- 禁止顺手修无关问题（发现了记到 issues/，不混进本提交）。

---

# 10. 回滚方案

运行时一键回滚（不用回退代码）：

```bash
TG_QUEUE_STORE=json ./run.sh restart
```

数据面回滚：`.imported` 文件 `mv` 回 `download_queue.json` 即可；DB 表废弃
不用、不删（与「不删旧数据」一贯原则一致）。

---

# 11. 测试要求（TDD，先测后码）

新增 `tests/test_queue_db.py`（复用 test_runtime_db.py 的临时 DB 模式），
至少覆盖：

1. **round-trip 无损**：含 unicode 长名、dedup_keys、next_retry_at、未知
   额外字段的 record → insert → load → 逐字段相等。
2. **两态装载**：QUEUED/RETRY 分别落到 tasks/retry，各自按 seq 排序。
3. **seq 语义**：入队 3 条 → 删中间 → load 顺序保持；转 retry 追加到
   retry 尾部；retry_failed 原位（seq 不变）。
4. **状态迁移**：insert / move_to_retry / update_retry / delete 每个的
   DB 行状态断言（含 attempts、next_retry_at 数值）。
5. **一次性导入**：构造旧 JSON（tasks+retry 混合、含 unicode/未知字段）→
   导入后表行数、seq 顺序、字段无损；JSON 改名 `.imported`；重跑幂等。
6. **导入冲突**：表已有行 + JSON 也在 → DB 赢、JSON 归档、WARNING。
7. **json 回滚开关**：TG_QUEUE_STORE=json 时不碰 DB，load/save 走旧路径。
8. **降级**：RUNTIME_DB_READY=False 时 load 回落 JSON；write-through
   DbUnavailable → WARNING、内存变更保留、不抛异常。
9. **坏 payload 行**：一行坏 → 跳过 + 其余装载成功。
10. **schema v3→v4**：v3 库升级后 download_tasks 存在、listener 表原样、
    重启幂等。

既有 `tests/test_queue.py` **必须原样全绿**（纯函数与执行器逻辑未变的证明）；
若有个别用例因 save 层 mock 需要调整，只许调整测试的**接线方式**，不许改
断言语义。

---

# 12. 回归要求

```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
PYTHONPATH=. .venv/bin/pytest -q
```

全部通过后，真机验证清单（架构师执行）：

```text
1. ./run.sh restart → 启动日志出现「下载队列：n 个任务 | 待重试 m 个」（与迁移前一致）
2. download_queue.json → download_queue.json.imported；download_tasks 表行数对得上
3. 收藏夹转发一条媒体 → /queue 可见 → sqlite3 下表里多一行 QUEUED
4. 下载成功 → 行消失、task_events.jsonl 出现 SUCCESS（同一 id）
5. 断代理复现失败 → 行转 RETRY、attempts+1、next_retry_at 为未来时间戳
6. /queue del 1 在途取消 → 行删除、事件 CANCELLED
7. 下载进行中 kill 进程 → 重启后任务自动重放（recover_queue_tasks）
8. TG_QUEUE_STORE=json ./run.sh restart → 行为与旧版完全一致（回滚演练）
```

---

# 13. 实施顺序

```text
Phase 1  复核 §2 审计底稿（行号可能漂移，以内容为准），输出差异报告
Phase 2  TDD：runtime_db 的 download_tasks 表 + 6 个函数（测试 1-4、9、10）
Phase 3  TDD：queue.py 持久化选择器 + 5 个突变点接线（测试 7、8）
Phase 4  TDD：启动迁移导入 load_queue_any + app.py 顺序调整（测试 5、6）
Phase 5  全量回归 + 专项（test_queue / test_queue_db / test_runtime_db）
Phase 6  真机验证清单（§12）
Phase 7  提交（含 CLAUDE.md 更新：队列持久化层说明 + TG_QUEUE_STORE）
```

---

# 14. 完成后自查

```text
□ grep -n "save_queue" tg_userbot/ 的调用只剩 json 模式分支与迁移路径
□ 5 个突变点全部走 _save_after_mutation（对照 §2.2 表）
□ 只读消费方（§2.3）git diff 为零
□ download_tasks 无 PROCESSING/lease 列（防顺手加戏）
□ Chrome Agent 启动路径无 init_db/连接调用
□ TG_QUEUE_STORE=json 全流程演练通过
□ 旧 JSON 只改名未删除
```

---

# 15. 架构师最终审计重点

1. **语义等价**：diff 逐行看——纯函数与执行器应当零改动；所有变化收敛在
   持久化调用与装载路径。
2. **失败注入**：亲手把 RUNTIME_DB 置坏跑一轮——下载必须照常，只许 WARNING。
3. **导入无损**：拿真机 download_queue.json 副本跑导入测试，逐字段 diff。
4. **ID 闭环**：入队 → 事件流 → 成功删除，task_id 三处一致。
5. **回滚开关**：json 模式下 DB 表增长必须为零。

---

# 16. Commit 要求

建议：

```text
功能：下载队列持久化 SQLite 化 —— download_tasks 表 + write-through + 一次性导入
```

提交前必须 `PYTHONPATH=. .venv/bin/pytest -q` 与 unittest discover 双绿并
汇报实际输出。CLAUDE.md 同步更新（队列持久化、TG_QUEUE_STORE、Phase 2-4
待办指针）。

---

# 17. 最终原则

> **队列的业务逻辑与内存语义一行不动；SQLite 只替换「怎么存」，不碰「怎么跑」。
> 内存永远是权威，DB 尽力持久化；DB 坏了下载照常，json 开关一键回滚。
> task_id 全链路不变，旧数据只改名不删除，先迁移后切换，全程可回退。**
