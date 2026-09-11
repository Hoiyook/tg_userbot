# Telegram 标签监听 Producer/Consumer + SQLite 架构调整
## DeepSeek 开发任务书

> **目标：** 将现有 Tag Listener 从“扫描后直接执行”调整为“Scanner 持久化任务 + Worker 受控执行”的 Producer/Consumer 架构，并以 SQLite 作为 Listener 第一阶段的运行时持久化层。
>
> **核心原则：最小改动、保留现有功能、只改变 Listener 的执行架构与持久化方式。**

---

## 1. 项目背景

仓库：`Hoiyook/tg_userbot`

目标分支：`feature/tag-listener`

当前 Tag Listener 已具备：

- `runtime/listen.json`
- Tag 匹配
- 多 Tag 命中合并
- Album / `grouped_id` 处理
- Saved Messages / 指定聊天目标
- `download=true`
- Listener checkpoint
- 配置热加载
- 网络保护
- 与现有下载白名单保持独立

当前主要问题：

```text
Telegram
   ↓
Listener Scanner
   ↓
匹配
   ↓
直接 Forward / Download
```

一次扫描发现大量匹配消息时，可能在短时间产生大量 Telegram 转发请求。

目标：

```text
Telegram
   ↓
Listener Scanner
   ↓
SQLite 持久化任务
   ↓
Listener Worker
   ↓
受控 Forward
   ↓
现有 Download Queue
```

---

## 2. 总体目标

必须实现：

1. Scanner 与 Worker 解耦。
2. 任务持久化后才能推进 checkpoint。
3. 重复扫描不会产生重复任务。
4. Worker 崩溃后任务可以恢复。
5. FloodWait 必须按 Telegram 返回值处理。
6. 不破坏现有下载系统。
7. 不改变 `/wl` 和下载白名单语义。
8. Listener 与 Download Whitelist 继续独立。
9. 配置继续使用 JSON。
10. 第一阶段只迁移 Listener Runtime State 到 SQLite。

最终结构：

```text
                         listen.json
                             │
                             ↓
Telegram ─────────→ Listener Scanner
                             │
                  Tag Match / Merge / Dedup
                             │
                             ↓
                     SQLite Runtime DB
                     ┌─────────────────┐
                     │ checkpoints     │
                     │ listener_tasks  │
                     │ task_events     │
                     └────────┬────────┘
                              │
                              ↓
                     Listener Worker
                              │
                    ┌─────────┴─────────┐
                    ↓                   ↓
                 Forward          enqueue_media()
                    ↓                   ↓
                Telegram          现有下载队列
```

---

## 3. 非目标 / 严禁顺手改动

本任务不是：

- 重写整个项目。
- 重写现有下载队列。
- 重写 Download Worker。
- 重写 Caption 清洗。
- 重写 Chrome 下载。
- 重写 Dedup。
- 把所有 JSON 都迁移到 SQLite。
- 把所有日志迁移到 SQLite。
- 合并 Listener 与 Download Whitelist。
- 修改 `/wl` 行为。
- 修改现有实时 Saved Messages 处理逻辑。
- 大范围重构 `app.py`。

凡是与本任务无直接关系的代码，不要顺手重构。发现可以优化但与本任务无关的问题，记录 TODO 即可。

---

## 4. 数据分层原则

### 4.1 用户配置：JSON

继续保留：

```text
runtime/
├── listen.json
├── dedup_config.json
└── caption_filter.json
```

`listen.json` 仍然是 Listener 配置 Source of Truth。

### 4.2 业务运行状态：SQLite

新增：

```text
runtime/tg_userbot.db
```

第一阶段：

```text
schema_meta
listener_checkpoints
listener_tasks
task_events
```

后续可以逐步迁移：

```text
queue.json
dedup_index.txt
download_history.txt
task_events.jsonl
```

但本次不要迁移这些旧系统。

### 4.3 技术日志：文件

继续保留：

```text
download.log
chrome_agent.log
cd2_launch.log
```

---

## 5. SQLite 初始化

数据库路径必须使用现有：

```python
config.RUNTIME_DIR
```

禁止各模块重新定义 Runtime 路径。

数据库：

```text
runtime/tg_userbot.db
```

初始化要求：

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
```

优先保证 Runtime DB 持久性，可采用：

```sql
PRAGMA synchronous=FULL;
```

要求：

- 不在活跃运行时只复制 `.db` 文件。
- WAL 下仍然只有一个 writer。
- 不建立长时间写事务。
- Telegram API 调用绝不能放在 SQLite transaction 中。

---

## 6. Schema Version

建立：

```sql
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

至少保存：

```text
schema_version
```

未来支持：

```text
v1 → v2 → v3
```

Migration 必须：

- 可重复执行
- 幂等
- 中途失败可再次执行
- 不破坏已有数据

---

## 7. listener_checkpoints

```sql
CREATE TABLE listener_checkpoints (
    source_chat_id INTEGER PRIMARY KEY,
    last_message_id INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
```

`last_message_id` 表示：

> Scanner 已经成功将该位置之前需要创建的 Listener Tasks 持久化完成。

不表示：

> 所有任务已经成功发送。

---

## 8. listener_tasks

建议：

```sql
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

    last_error TEXT
);
```

唯一约束：

```sql
CREATE UNIQUE INDEX idx_listener_task_unique
ON listener_tasks (
    source_chat_id,
    message_id,
    target_type,
    target_chat_id
);
```

确保同一：

```text
source + message + target
```

不会重复创建任务。

---

## 9. Target 模型

Saved Messages：

```text
target_type = saved_messages
target_chat_id = NULL
```

普通聊天：

```text
target_type = chat
target_chat_id = Telegram chat_id
```

`@username` 只允许作为输入，解析后必须保存 `chat_id`。

name / username 仅用于展示，不能作为身份。

---

## 10. task_events

```sql
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    task_id INTEGER NOT NULL,

    event_type TEXT NOT NULL,

    created_at INTEGER NOT NULL,

    payload TEXT,

    FOREIGN KEY(task_id)
        REFERENCES listener_tasks(id)
);
```

建议事件：

```text
RECEIVED
QUEUED
RUNNING
RETRY
SUCCESS
FAILED
CANCELLED
LEASE_EXPIRED
DEDUP_HIT
```

尽量复用现有 Stats 事件命名。

---

## 11. Scanner 职责

Scanner 负责：

1. 读取 Listener 配置。
2. 发现 source chat。
3. 根据 checkpoint 扫描新消息。
4. Tag matching。
5. 多 Tag 合并。
6. Album / grouped_id 处理。
7. 计算目标。
8. 计算是否 download。
9. 生成 Listener Tasks。
10. 写入 SQLite。
11. transaction 成功后更新 checkpoint。

Scanner 不负责：

- Telegram Forward
- Telegram Download
- FloodWait 等待
- Worker Retry
- 实际任务执行

---

## 12. Worker 职责

新增：

```text
tg_userbot/listener_worker.py
```

Worker：

```text
获取 pending task
↓
claim
↓
PROCESSING
↓
执行 Telegram Forward
↓
SUCCESS / RETRY / FAILED
```

Worker 不读取 `listen.json` 来重新决定已入队任务的目标。

已入队任务的目标以：

```text
listener_tasks
```

为准。

---

## 13. Checkpoint 原子性

错误实现：

```text
扫描
↓
更新 checkpoint
↓
写 task
```

禁止。

正确：

```text
扫描
↓
生成 tasks
↓
BEGIN
    INSERT tasks
    INSERT task_events
    UPDATE checkpoint
COMMIT
```

只有 COMMIT 成功才推进 checkpoint。

失败：

```text
ROLLBACK
```

下一轮可以重新扫描。

依靠唯一索引避免重复任务。

---

## 14. 多 Tag 合并

例如：

```text
message 100
内容：
xxx #MMD xxx #01musume
```

规则：

```text
#MMD       → A
#01musume  → B
```

最终：

```text
A
B
```

而不是：

```text
A
A
B
```

可以先构造：

```python
matched = {
    message_id: {
        "matched_tags": {"#MMD", "#01musume"},
        "targets": {
            ("chat", A),
            ("chat", B)
        },
        "download": True
    }
}
```

再根据 target 创建任务。

---

## 15. 同一消息多个 Target

例如：

```text
Saved Messages
Chat A
Chat B
```

必须是三个独立任务：

```text
Task 1 → Saved Messages
Task 2 → Chat A
Task 3 → Chat B
```

A 失败不能导致其他目标重新执行。

---

## 16. Album

保留当前 Album 行为。

`grouped_id` 写入：

```text
listener_tasks.grouped_id
```

不要为了 SQLite 重写 Album 业务。

唯一键仍然基于：

```text
source_chat_id
message_id
target_type
target_chat_id
```

---

## 17. 首次 Listener Checkpoint

第一次监听某个 source chat：

```text
没有 checkpoint
```

不要扫描历史。

执行：

```text
获取当前最新 message_id
↓
checkpoint = latest_id
```

从下一次开始监听未来消息。

---

## 18. 新增 Tag

已有：

```text
checkpoint = 5000
```

新增：

```text
#NEW
```

不得重新扫描：

```text
1 ~ 5000
```

沿用当前 source chat checkpoint。

---

## 19. 配置修改与已入队任务

例如：

```text
10:00
#A → Chat B

10:01
消息 100 入队

10:02
#A → Chat C
```

消息 100 仍然：

```text
→ Chat B
```

新消息才：

```text
→ Chat C
```

配置只影响新任务生成。

---

## 20. Listener 与 Download Whitelist

必须继续独立：

```text
Listener config
```

与：

```python
state.WHITELIST_CHATS
```

不得合并。

不要从 Download Whitelist 生成 Listener source。

---

## 21. 白名单重叠

如果 source chat 同时属于 Download Whitelist，且 Listener 也需要 Saved Messages：

```text
已有流程覆盖的目标
→ 不重复 Forward
```

但 Listener 独有目标：

```text
Chat B
→ 正常创建 Listener Task
```

不要因此修改 `/wl`。

---

## 22. download=true

Listener Worker 不自己下载。

正确：

```text
Listener Worker
↓
Forward to Saved Messages
↓
existing app.enqueue_media(...)
↓
existing Download Queue
↓
existing Download Worker
```

禁止创建第二套 Downloader。

---

## 23. Worker Claim

短事务：

```text
BEGIN
↓
选择一个可执行 task
↓
status = PROCESSING
started_at = now
lease_until = now + lease_seconds
↓
COMMIT
```

然后才：

```text
Telegram API
```

禁止把 Telegram API 调用放在 SQLite transaction 内。

---

## 24. Lease / Crash Recovery

例如：

```text
PROCESSING
lease_until = now + 600
```

Worker 崩溃后：

```text
PROCESSING
lease_until < now
```

恢复：

```text
PENDING
lease_until = NULL
```

并记录：

```text
LEASE_EXPIRED
```

启动时必须执行过期任务恢复。

---

## 25. Task 状态

推荐：

```text
PENDING
PROCESSING
SUCCESS
FAILED
CANCELLED
```

Retry 可以使用：

```text
PENDING + next_retry_at
```

推荐：

```text
PENDING
↓
PROCESSING
↓
SUCCESS

PROCESSING
↓
PENDING + next_retry_at

PROCESSING
↓
FAILED
```

---

## 26. Retry

临时错误：

```text
NetworkError
Timeout
ServerError
```

进入：

```text
PENDING
```

并设置：

```text
next_retry_at
```

复用现有 Queue 的 backoff 风格，避免重新设计一套完全不同的 Retry 机制。

---

## 27. FloodWait

如果 Telegram 返回：

```text
FloodWait(N)
```

必须：

```text
读取 N
↓
记录事件
↓
next_retry_at = now + N
```

不能固定等待 60 秒。

不能立即重试。

第一版：

```text
Listener Worker concurrency = 1
```

FloodWait 时暂停 Listener Worker 发送能力。

---

## 28. 永久错误

例如：

```text
ChatWriteForbidden
ChannelPrivate
PeerIdInvalid
目标不存在
无发送权限
```

如果确认等待无法解决：

```text
FAILED
```

不能无限 Retry。

错误分类必须根据当前 Telethon 版本实际异常类型实现。

---

## 29. At-least-once

本系统保证：

```text
at-least-once
```

不保证：

```text
exactly-once
```

典型情况：

```text
Telegram Forward 已成功
↓
程序在 SUCCESS 写入前崩溃
↓
Lease 到期
↓
任务重新执行
```

理论上可能重复 Forward。

SQLite UNIQUE 只能防止重复创建任务，不能保证 Telegram API exactly-once。

---

## 30. Queue 上限

必须存在：

```text
max_pending_tasks
```

建议初始值：

```text
1000
```

但应作为配置项，而非硬编码。

达到上限：

```text
Scanner 停止创建新 Listener Tasks
```

没有入队的消息：

> 不能推进 checkpoint。

---

## 31. Scanner / Worker 周期

Scanner：

```text
按 listen.json interval_minutes
```

Worker：

```text
常驻
```

一次扫描产生大量任务时：

```text
Scanner → SQLite
Worker → 慢慢消费
```

而不是扫描完立即连续发送。

---

## 32. Worker 调度

第一版：

```text
concurrency = 1
```

可以配置：

```text
min_forward_interval_seconds
```

采用保守值。

不要声称任何工程配置是 Telegram 官方“安全阈值”。

Telegram 返回 FloodWait 时，以服务端返回值为最高优先级。

---

## 33. Runtime DB API

新增：

```text
tg_userbot/runtime_db.py
```

其他模块不要散落 SQL。

至少需要能力：

```python
init_db()
get_schema_version()
migrate()

get_listener_checkpoint(source_chat_id)
set_listener_checkpoint(...)

enqueue_listener_tasks(...)
get_pending_listener_task(...)
claim_listener_task(...)
complete_listener_task(...)
retry_listener_task(...)
fail_listener_task(...)
cancel_listener_task(...)

recover_expired_listener_tasks()

record_task_event(...)
get_listener_stats(...)
```

函数名可按现有项目风格调整。

核心要求：

> SQLite SQL 集中在 Runtime DB 层。

---

## 34. listener.py 改造边界

保留：

- listen.json 读取
- Rule validation
- normalize_target
- target_key
- Tag matching
- Album grouping
- 多 Tag 合并
- download 判断
- source chat 解析
- 配置热加载

替换：

```text
listen_state.json
```

以及：

```text
pending work execution
```

改成：

```text
runtime_db
+
listener_worker
```

---

## 35. listener_worker.py

基本循环：

```python
while running:
    recover_expired_tasks()

    task = claim_next_task()

    if not task:
        await sleep(...)

    execute_task(task)

    update result
```

必须支持：

- graceful shutdown
- asyncio cancellation
- Telegram exceptions
- Retry
- FloodWait
- Lease
- task event
- Stats

后台 asyncio Task 必须保持强引用，不能创建后丢失。

---

## 36. app.py

只做必要接入：

```text
启动 Runtime DB
↓
migration
↓
recover expired listener tasks
↓
启动 Listener Worker
↓
启动 Listener Scanner
```

不要大范围重构 `main()`。

现有：

- download queue
- workers
- reconnect watchdog

继续工作。

---

## 37. stats.py

第一阶段：

- 保持现有 Stats 工作。
- 增加 Listener SQLite 数据读取。
- 暂不删除 `task_events.jsonl`。
- 不一次性重写整个统计系统。

最终再逐步：

```text
Stats → SQLite
```

---

## 38. listen_state.json Migration

首次升级：

```text
启动
↓
创建 DB
↓
发现 listen_state.json
↓
读取 checkpoint
↓
写入 listener_checkpoints
↓
验证
↓
迁移成功
```

旧文件不要直接删除。

可以改名：

```text
listen_state.json.migrated
```

Migration 必须幂等。

---

## 39. 数据库错误

处理：

```text
SQLITE_BUSY
SQLITE_LOCKED
```

不能直接导致整个 Userbot 崩溃。

可以：

```text
记录日志
↓
短暂等待
↓
有限次数重试
```

避免无限循环。

数据库错误与 Telegram Retry 必须分开处理。

---

## 40. 测试要求

必须增加自动化测试。

### Database

测试：

- schema 创建
- migration
- checkpoint
- task insert
- UNIQUE
- task claim
- task complete
- task retry
- task fail
- lease recovery
- event insert

### Scanner

测试：

- 单 Tag
- 多 Tag
- 多 Target
- 重复扫描
- 新增 Tag
- 首次 checkpoint
- Album
- whitelist overlap
- queue full
- transaction rollback

### Worker

测试：

- 正常成功
- 网络失败
- Retry
- FloodWait
- 永久错误
- Lease
- 崩溃恢复
- cancellation

---

## 41. 关键故障测试

### Case A

```text
INSERT tasks 成功
UPDATE checkpoint 失败
```

预期：

```text
整个 transaction rollback
```

### Case B

```text
任务没有持久化
```

预期：

```text
checkpoint 不推进
```

### Case C

```text
Worker claim
↓
进程 kill
```

预期：

```text
Lease 到期
↓
任务恢复 PENDING
```

### Case D

```text
Telegram Forward success
↓
SUCCESS 更新前 crash
```

预期：

```text
任务最终可以重新执行
```

并接受 at-least-once。

### Case E

```text
Scanner 扫描大量消息
↓
Queue 达到上限
```

预期：

```text
未进入 Queue 的消息不能推进 checkpoint
```

---

## 42. 验收标准

### 架构

- [ ] Scanner 不直接执行 Forward。
- [ ] Worker 独立执行。
- [ ] SQLite 持久化 Listener Runtime State。
- [ ] JSON 继续作为 Listener 配置。

### 一致性

- [ ] Task 与 checkpoint 使用正确 transaction。
- [ ] 重复扫描不会产生重复任务。
- [ ] Worker 崩溃可以恢复。
- [ ] Lease 可以恢复 PROCESSING 任务。

### Telegram

- [ ] FloodWait 按服务端返回时间处理。
- [ ] 临时错误 Retry。
- [ ] 永久错误 FAILED。
- [ ] Worker 不因为单个任务失败退出。

### 业务

- [ ] 多 Tag 正确合并。
- [ ] 多 Target 独立。
- [ ] Album 保持原行为。
- [ ] 新增 Tag 不扫历史。
- [ ] Listener 与 Whitelist 独立。
- [ ] 白名单重叠不产生重复 Saved Messages Forward。

### 下载

- [ ] Listener 不创建第二套 Downloader。
- [ ] 继续使用现有 `app.enqueue_media()`。
- [ ] 现有下载队列行为不改变。

### 兼容性

- [ ] `/wl` 正常。
- [ ] 现有下载功能正常。
- [ ] 现有实时 Saved Messages 正常。
- [ ] 现有 Bot 菜单正常。
- [ ] 原有测试全部通过。

---

## 43. 建议实施顺序

### Phase 1

建立：

```text
runtime_db.py
```

完成：

```text
schema
migration
connection
WAL
```

先写测试。

### Phase 2

实现：

```text
listener_checkpoints
listener_tasks
task_events
```

完成：

```text
CRUD
claim
lease
retry
event
```

### Phase 3

改造：

```text
listener.py
```

实现：

```text
Scanner → SQLite
```

先验证：

```text
扫描
→ 任务进入 DB
→ checkpoint 正确
```

### Phase 4

新增：

```text
listener_worker.py
```

实现：

```text
SQLite
→ Worker
→ Forward
```

### Phase 5

接入：

```text
app.py
```

完成：

```text
startup
migration
recovery
worker
scanner
```

### Phase 6

Stats：

```text
SQLite → Listener statistics
```

### Phase 7

完整回归测试。

---

## 44. 后续 SQLite 迁移路线

本次完成后不要继续大改。

未来：

```text
Phase 1
Listener Runtime
        ↓
Phase 2
Download Queue
        ↓
Phase 3
Dedup
        ↓
Phase 4
Download History
        ↓
Phase 5
Stats
```

最终：

```text
runtime/
│
├── listen.json
├── dedup_config.json
├── caption_filter.json
│
├── tg_userbot.db
│
├── download.log
├── chrome_agent.log
└── cd2_launch.log
```

形成：

```text
配置 → JSON
业务状态 → SQLite
技术日志 → Log
```

---

## 45. 给 DeepSeek 的最终执行要求

请先阅读当前仓库代码，重点检查：

```text
tg_userbot/listener.py
tg_userbot/app.py
tg_userbot/queue.py
tg_userbot/workers.py
tg_userbot/stats.py
tg_userbot/config.py
tg_userbot/whitelist.py
tg_userbot/bot.py
tg_userbot/menu.py
```

然后：

1. 先确认当前代码实际结构。
2. 不要根据任务书猜测函数名。
3. 优先复用现有逻辑。
4. 先写测试，再实现核心 Runtime DB。
5. 每完成一个阶段运行测试。
6. 不要顺手重构无关代码。
7. 不要删除旧功能。
8. 不要降低现有功能能力。
9. 如果发现任务书与当前代码冲突，暂停实现并报告冲突点。
10. 不允许为了通过测试而修改测试掩盖真实问题。
11. 最终运行完整测试。
12. 最终报告：
   - 修改文件
   - 新增文件
   - 删除文件
   - 数据迁移方式
   - 测试结果
   - 已知限制
   - 未完成项

**不要把“代码能运行”当作完成。必须证明 Scanner、SQLite、Worker、Crash Recovery、Retry、FloodWait、Duplicate、Checkpoint 的关键测试通过。**

---

## 46. 最终架构

```text
                         ┌──────────────────┐
                         │   listen.json    │
                         │  Listener Config │
                         └────────┬─────────┘
                                  │
                                  ↓
Telegram ───────────────→ Listener Scanner
                                  │
                    ┌─────────────┼─────────────┐
                    │             │             │
                 Tag Match    Multi-Tag      Album
                    │          Merge          Group
                    └─────────────┼─────────────┘
                                  ↓
                         ┌──────────────────┐
                         │ tg_userbot.db    │
                         │                  │
                         │ checkpoints      │
                         │ listener_tasks   │
                         │ task_events      │
                         └────────┬─────────┘
                                  │
                                  ↓
                         Listener Worker
                                  │
                           controlled rate
                                  │
                         ┌────────┴────────┐
                         ↓                 ↓
                      Forward          enqueue_media
                         ↓                 ↓
                    Telegram          Existing Queue
                                           │
                                           ↓
                                     Existing Worker
```

**最终目标：**

```text
扫描 ≠ 执行
```

变成：

```text
扫描 → 持久化 → 排队 → 受控执行
```
