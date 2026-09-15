# tg_userbot 持久化与可靠性优化任务书

**目标对象：DeepSeek / 编码 Agent**  
**当前基线：`9cc7e90146c67e024e17de7111662c1511d21105`**  
**任务性质：可靠性优化，不做无关重构**

## 1. 总目标

重点优化：
1. 核心任务持久化：DB 短暂失败 + 进程崩溃不能导致任务永久消失。
2. 重复控制：允许 At-least-once，但尽可能做到幂等，避免重复任务和重复下载。
3. 重试恢复：重启、DB 故障、Telegram API 失败、下载失败后都能恢复。

最终目标不是 Exactly-once，而是：

> **Durable + At-least-once + Idempotent Side Effects + Reconciliation**

---

## 2. 当前审计结论

当前总体架构：

```text
Telegram / Pawchive
        ↓
Producer / Scanner
        ↓
SQLite
        ↓
Worker
        ↓
外部副作用
        ↓
文件系统
```

Listener 的 `listener_tasks + checkpoint` 已经采用任务与 checkpoint 同事务的正确模式。

主要风险：

### P0-1：download queue

当前存在：

```text
内存 state.QUEUE 更新
        ↓
尝试写 SQLite
        ↓
SQLite 写失败
        ↓
仍继续执行
```

之后若进程崩溃，内存任务消失，SQLite 中没有任务。

**结果：下载任务可能永久丢失。**

### P0-2：Forward → Download Queue

存在：

```text
listener task
   ↓
Telegram Forward 成功
   ↓
enqueue_media()
   ↓
download_tasks 写入失败
```

结果可能是：

```text
Saved Messages 已有副本
SQLite 没有 download task
```

没有 reconciliation 时，下载任务可能永久丢失。

不要尝试 Telegram API + SQLite 两阶段提交。

### P1-1：Dedup 持久化失败

当前：

```text
文件下载成功
 ↓
dedup DB 写失败
 ↓
仍更新内存 DEDUP_INDEX
```

若随后 crash：

```text
文件存在
DB dedup key 不存在
```

重启后可能重复下载。

### P1-2：Forward 成功但 complete DB 更新失败

这是典型 At-least-once 场景：

```text
PROCESSING
 ↓
Forward 成功
 ↓
complete DB 写失败
 ↓
lease 到期
 ↓
再次执行
```

不要为了消灭这种重复而牺牲任务可靠性。

原则：

> **宁可重复执行，也不要丢任务。**

---

## 3. 必须保持的现有语义

不得破坏：

- Listener Scanner / Worker Producer-Consumer 架构。
- Listener checkpoint 与 task 同事务。
- `listener_tasks` 唯一索引。
- 相册整组任务语义。
- WL 实时事件链 + 扫描链。
- `/wl since`。
- FloodWait 全局暂停。
- Listener retry / lease recovery。
- Pawchive `pawchive_posts + pawchive_files` 状态机。
- Pawchive `.part` 断点续传。
- Pawchive 已存在文件的幂等检查。
- Dedup 的“不确定就放行”原则。
- 用户现有命名、annotation、parent caption/date 功能。
- 不做无关 UI/Android 架构重构。
- 不大规模重写 queue/runtime_db。

---

## 4. P0：修复 Download Queue 持久化一致性

核心要求：

```text
SQLite durable
      ↓
允许进入执行态
```

不能：

```text
RAM 有任务
DB 没任务
 ↓
继续执行
```

推荐：

```text
创建 record
    ↓
SQLite INSERT 成功
    ↓
更新 state.QUEUE
    ↓
spawn worker
```

如果 DB INSERT 失败：

- 不得把任务视为已入队。
- 不得启动该任务。
- 记录错误。
- 上游必须保留可恢复路径。

如果现有调用结构要求先改内存，则 DB 失败后必须回滚内存状态，并禁止 spawn。

**验收：任何被 Worker 执行的任务都必须已经存在 SQLite。**

必须测试：

1. INSERT 成功。
2. DB locked。
3. DB unavailable。
4. payload serialization failure。
5. INSERT 前 crash。
6. INSERT 后、RAM 更新前 crash。

---

## 5. P0：解决 Forward 成功 → Download Task 丢失

不要做 Telegram + SQLite 两阶段提交。

推荐扩展现有 listener task 生命周期，使它能表达：

```text
PENDING
   ↓
PROCESSING
   ↓
FORWARDED
   ↓
DOWNLOAD_PENDING
   ↓
DOWNLOAD_ENQUEUED
   ↓
SUCCESS
```

具体状态名可以按现有 schema 最小改动实现。

Forward 成功后必须留下持久化事实，至少能表达：

- forward 已成功。
- forwarded message id（能可靠取得时保存）。
- download enqueue 是否完成。
- 原 listener task id。

Crash 场景必须成立：

```text
Forward 成功
↓
程序 crash
↓
重启
↓
发现 FORWARDED + 未入 download queue
↓
自动补建 download task
```

如果能够可靠判断已经 Forward：

```text
已 Forward
↓
不要再次 Forward
↓
只补后续 download enqueue
```

无法可靠判断时，可以允许重复 Forward。

---

## 6. P1：Dedup 持久化强化

保留现有 key：

```text
tg:<file.id>
f:<filename>:<size>
dyc:<aweme_id>
c:<sha256>
```

保留原则：

> 无法确定是否重复时，一律放行。

不要为了去重误拦截媒体。

推荐：

```text
文件成功落盘
 ↓
写 dedup_index
 ↓
成功后更新内存
```

如果现有结构必须先更新内存，则增加待持久化机制或启动 reconciliation。

启动时可检查：

```text
已有最终文件
+
dedup_index
+
download_history
```

能够可靠得到 key 时：

```text
文件存在
dedup 不存在
 ↓
补写 dedup
```

不要无条件扫描整个下载目录计算 SHA256；优先 filename + size 等轻量信息。

---

## 7. P1：统一任务状态与文件状态恢复

必须区分：

```text
Task state
```

与：

```text
Side effect state
```

典型 crash：

```text
Task = PROCESSING
File = DONE
```

不是错误。

重启时：

```text
PROCESSING
+
目标文件存在且有效
        ↓
直接补成 DONE
```

不要重新完整下载。

Pawchive 已经采用类似思路，应将原则推广到普通下载。

---

## 8. Pawchive：保持当前设计，只加强验证

当前 Pawchive：

```text
PENDING
 ↓
PROCESSING + lease
 ↓
files PENDING
 ↓
httpx download
 ↓
file DONE
 ↓
post COMPLETED
```

不要重写主流程。

必须验证：

### A
下载完成 → `os.replace` → `mark_file_done` 前 crash。

重启必须识别完整文件。

### B
`.part` 存在 → crash → lease expired → 重试。

必须正确 Range resume。

### C
HTTP 404 / 410：

不得无限 retry。

### D
HTTP 500 / timeout / connection reset：

允许 retry。

### E
post PROCESSING → worker crash：

lease 到期后必须恢复 PENDING。

---

## 9. Listener：保持 At-least-once

当前：

```text
Forward 成功
DB complete 失败
 ↓
以后可能重复 Forward
```

这是可接受的。

不要为了“绝不重复”而禁止 retry。

优先级：

```text
不丢任务
>
不重复执行
```

---

## 10. Retry 统一规范

所有 Worker：

```text
临时错误
   ↓
retry
   ↓
指数退避
   ↓
达到上限
   ↓
FAILED
   ↓
人工 retry
```

不要失败就创建新 task。

必须复用原 task id。

以下字段必须持久化：

```text
attempts
next_retry_at
status
lease_until
```

重启后必须恢复。

明确区分：

```text
lease：
防止两个 worker 同时处理一个 task

retry：
决定失败后什么时候再次处理
```

---

## 11. 统一可靠性模型

最终统一为：

```text
             ┌─────────────┐
             │ Durable DB  │
             └──────┬──────┘
                    │
                  claim
                    │
                    ▼
             ┌─────────────┐
             │ PROCESSING  │
             │ + lease     │
             └──────┬──────┘
                    │
              external side effect
                    │
          ┌─────────┴─────────┐
          │                   │
        success             crash
          │                   │
          ▼                   ▼
       COMPLETE          lease expire
                              │
                              ▼
                           retry
```

核心原则：

> **DB 是任务真相，内存只是缓存/执行视图。**

---

## 12. 必须增加的测试

不要只测试 happy path。

### Download Queue

- DB INSERT 成功。
- DB INSERT 失败。
- INSERT 后 crash。
- retry 状态重启恢复。
- queued 状态重启恢复。
- attempts 持久化。
- next_retry_at 持久化。
- 同一 task 不生成多个 task。
- 坏 payload 不影响其它任务。

### Listener

- task + checkpoint 同事务。
- task INSERT 失败 → checkpoint 不推进。
- checkpoint 写失败 → task rollback。
- Forward 成功 + complete 失败。
- lease 到期恢复。
- 重复扫描不产生第二 task。
- 不同 producer 不产生重复 task。
- album 不被拆成多个任务。
- DB unavailable 不推进 checkpoint。

### Forward → Download

重点：

```text
Forward success
download enqueue success
```

以及：

```text
Forward success
download enqueue DB failure
crash
restart
reconcile
```

最终必须存在 download task。

### Dedup

- DB write success。
- DB write failure。
- crash 后 restart。
- 文件存在但 dedup 不存在。
- filename + size。
- tg file id。
- SHA256。
- key 缺失时放行。

### Pawchive

- target 已存在且 size 正确。
- target 已存在但 size 错误。
- `.part` resume。
- Range 返回 200。
- 404。
- 410。
- 500。
- timeout。
- lease recovery。
- file DONE 但 post 尚未 finalize。
- finalize 前 crash。

---

## 13. 必须增加故障注入测试

建议增加可注入点：

```text
fail_before_queue_persist
fail_after_queue_persist
fail_after_forward
fail_before_download_enqueue
fail_after_download
fail_before_mark_done
fail_db_write
```

统一测试：

```text
执行
 ↓
故意 crash
 ↓
重新初始化 DB / worker
 ↓
reconcile
 ↓
继续执行
```

必须证明：

### 不丢

所有应该执行的任务最终都能执行。

### 不无限重复

恢复不会产生无限 task。

### 可幂等

已经完成的文件不会反复完整下载。

---

## 14. 严格禁止

禁止：

1. 为修复丢任务，把系统退回纯内存队列。
2. 每次 retry 创建新 task。
3. 用 filename 单独作为全局 task ID。
4. 为追求 Exactly-once 而阻止所有 retry。
5. 在 SQLite transaction 内调用 Telegram API。
6. 在 SQLite transaction 内执行长时间下载。
7. 核心任务 DB 写失败后静默吞掉并继续认为已持久化。
8. Dedup 不确定时拦截下载。
9. 大规模重写 queue/runtime_db。
10. 引入第二套互相独立的任务状态机。

---

## 15. 实现顺序

### Task 1：只读审计调用链

先不要改代码。

输出：

```text
queue enqueue
queue retry
queue delete
listener claim
listener complete
forward
enqueue_media
dedup remember
pawchive finalize
```

的调用关系和状态转换图。

### Task 2：先写失败测试

优先写：

```text
DB failure → crash → restart
```

相关测试。

测试必须先失败。

### Task 3：修 Download Queue Durable

达到：

```text
DB commit
 ↓
允许执行
```

### Task 4：修 Forward → Download reconciliation

### Task 5：修 Dedup persistence/reconciliation

### Task 6：Pawchive 故障注入与恢复测试

不要无必要修改 Pawchive 主流程。

### Task 7：完整测试

执行：

```bash
pytest -q
```

记录：

```text
总测试数
通过
失败
跳过
```

---

## 16. 完成标准

### 数据安全

- [ ] DB 暂时失败不会导致核心任务被执行后永久消失。
- [ ] 重启后所有已持久化任务都能恢复。
- [ ] retry 状态不会丢。
- [ ] lease 到期任务能恢复。

### 重复控制

- [ ] 同一 `(source_chat, message, target)` 不生成多个 listener task。
- [ ] retry 不生成新 task。
- [ ] crash recovery 不产生无限重复。
- [ ] 已完成文件不会无意义重复下载。

### 跨状态一致性

- [ ] Forward 成功但 download enqueue 失败时，重启后能够补偿。
- [ ] 文件落盘成功但 DB 状态未更新时，重启能够识别。
- [ ] Dedup 写失败不会永久造成索引缺失。

### 重试

- [ ] 临时错误自动 retry。
- [ ] attempts / next_retry_at 持久化。
- [ ] 永久错误不会无限 retry。
- [ ] 人工 retry 仍可用。

### 回归

- [ ] 原有全部测试通过。
- [ ] Listener 功能不变。
- [ ] WL 功能不变。
- [ ] Pawchive 功能不变。
- [ ] Dedup 功能不变。
- [ ] 命名逻辑不变。
- [ ] 不引入第二套任务状态机。

---

## 17. 最重要的验收场景

不要只报告“全部测试通过”。

必须证明：

### 场景 1：任务持久化失败

```text
任务进入内存
↓
DB 写失败
↓
程序 crash
↓
restart
```

不能永久丢失。

### 场景 2：Forward 与 Download 队列之间 crash

```text
Telegram Forward 已成功
↓
Download Task 尚未持久化
↓
程序 crash
↓
restart
```

Download Task 最终必须出现。

### 场景 3：文件已落盘但状态未更新

```text
文件已经完整落盘
↓
DB DONE 尚未写入
↓
程序 crash
↓
restart
```

不能再次无意义地完整下载。

---

# 最终目标

```text
Producer
   ↓
Durable SQLite Task
   ↓
Claim + Lease
   ↓
External Side Effect
   ↓
Idempotent / Reconciliable
   ↓
Durable Final State
```

即：

> **任务以 SQLite 为真相源，内存只是运行时缓存；允许 At-least-once，但所有重要副作用都必须具备幂等或 reconciliation 能力。**

只解决上述可靠性问题，不为了“代码更漂亮”进行无关重构。
