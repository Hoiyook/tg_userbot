# TG Userbot Runtime Reporter — AI Coding Implementation Specification

> 用途：本文档可直接交给 DeepSeek-V4-Flash / Claude Code / Codex 等 AI Coding Agent 执行。
>
> 核心目标：在不破坏现有 Userbot 功能的前提下，增加低耦合的 Runtime Reporter，让程序主动汇报运行状态、下载进度、任务结果、异常与恢复。

## 1. 任务概述

为 `Hoiyook/tg_userbot` 增加：

```text
Telegram Userbot 主动运行状态汇报（Runtime Reporter）
```

建议新增：

```text
tg_userbot/reporter.py
```

Reporter 负责：

- 程序启动/关闭通知
- 运行时间
- 当前整体状态
- 当前下载及进度
- 下载速度、ETA、耗时
- Queue 状态
- Worker 状态
- Main Client / Bot Client 状态
- Retry / Auto Replay
- Success / Final Failed
- Error / Recovery
- 今日统计

## 2. 核心原则

### 2.1 Reporter 是观察者

流程：

```text
现有系统状态
    ↓
Reporter Snapshot
    ↓
Formatter
    ↓
Telegram
```

Reporter 只能观察，不控制业务。

### 2.2 不建立第二套状态

优先复用：

```python
state.ACTIVE_DOWNLOADS
state.QUEUE
state.DOWNLOAD_WORKERS
state.DOWNLOAD_WORKER_QUEUE
```

以及：

```text
stats.py
task_events.jsonl
```

不得创建：

```text
reporter_tasks
reporter_queue
reporter_downloads
reporter_workers
reporter_stats.json
```

### 2.3 Reporter 不得改变核心业务

不得修改：

- 下载流程
- Queue 调度
- Worker 调度
- Retry
- Auto Replay
- Dedupe
- `/progress`
- `/stats`

Reporter 自己发生任何异常，也不能影响这些功能。

---

## 3. 开工前必须阅读

Agent 必须先阅读真实仓库：

```text
CLAUDE.md
tg_userbot/state.py
tg_userbot/app.py
tg_userbot/queue.py
tg_userbot/download.py
tg_userbot/workers.py
tg_userbot/stats.py
tg_userbot/config.py
tg_userbot/log.py
```

以及现有测试。

如果实际文件名不同，以仓库实际结构为准。

不要根据本文档猜测变量类型或接口。

---

## 4. 修改范围

### 优先新增

```text
tg_userbot/reporter.py
```

### 原则上允许修改

```text
tg_userbot/app.py
tg_userbot/config.py
tests/...
```

其中测试只增加 Reporter 相关测试。

### 原则上禁止修改

```text
download.py
queue.py
workers.py
stats.py
```

如果确实无法实现，才允许最小修改，并在最终报告解释原因。

---

## 5. 严格禁止范围

本任务不增加：

- Web Dashboard
- HTTP API
- Prometheus / Grafana
- SQLite / MySQL / Redis
- CPU / 内存 / 磁盘 / 网络监控
- Chrome Agent 深度监控
- 新的复杂依赖
- 无关重构
- 下载系统重构
- Worker Pool 重构
- Queue 重构

目标是最小改动。

---

## 6. Telegram 汇报目标

第一阶段默认发送到：

```python
"me"
```

即 Telegram Saved Messages。

不增加 Chat ID 配置。

---

## 7. 两类汇报

Reporter 分成：

```text
A. Status Panel
B. Event Notification
```

### Status Panel

只维护一条消息：

```text
第一次：send_message()
后续：edit_message()
```

不能每次刷新创建新消息。

### Event Notification

重要事件才发送独立消息。

---

## 8. Status Panel 刷新频率

默认：

```python
REPORT_INTERVAL_SECONDS = 300
```

即 5 分钟。

下载进度：

```python
REPORT_PROGRESS_ENABLED = True
REPORT_PROGRESS_INTERVAL_SECONDS = 15
```

禁止每次 progress callback 都调用 Telegram API。

---

## 9. Status Panel 内容

至少包含：

```text
🤖 Userbot Runtime

🟢 RUNNING

⏱ 运行时间
04h 36m
启动：2026-09-10 13:20:15

📊 当前任务
队列：3
下载中：2
重试中：1

⬇️ 当前下载

1. example.mp4
   63% | 820 MB / 1.3 GB
   8.4 MB/s | ETA 01:02
   Worker #2 | 01:36

⚙️ Workers
3/3 正常
#1 BUSY
#2 BUSY
#3 IDLE

📈 今日统计
收到：18
成功：13
失败：1
重试：4
自动重放：3
去重：2
取消：0

🔌 Connections
Main Client：🟢
Bot Client：🟢

🕐 最后活动
12 秒前

更新时间
2026-09-10 17:56:21
```

具体文字遵循项目现有风格即可。

---

## 10. Runtime 状态

支持：

```text
🟢 RUNNING
🟡 DEGRADED
🔴 ERROR
```

### RUNNING

Main Client、Bot Client、Worker 基本正常。

### DEGRADED

仍可工作但存在局部异常，例如：

- Worker 异常
- Bot Client 暂时断开
- 持续 Retry
- 持续下载失败

### ERROR

核心系统不可正常工作，例如：

- 主 Client 无法工作
- 所有 Worker 不可用
- 核心任务系统停止

Reporter 自身异常不能让系统进入 ERROR。

---

## 11. Runtime 数据

记录：

```python
started_at
```

展示：

```text
启动时间
运行时间
```

第一阶段不持久化。

程序重启重新计时。

---

## 12. Last Activity

最后活动可以来自：

- 收到任务
- Queue 变化
- 下载开始
- 下载成功
- 下载失败
- Retry
- Auto Replay
- Worker 状态变化
- Client 状态变化

展示：

```text
最后活动：12 秒前
```

---

## 13. 下载状态

必须复用：

```python
state.ACTIVE_DOWNLOADS
```

尽量展示：

```text
文件名
进度
已下载大小
总大小
速度
ETA
耗时
Worker
```

无法可靠获得的字段：

```text
--
```

禁止猜测。

### 大小

使用：

```text
KB / MB / GB
```

### ETA

计算：

```text
remaining_bytes / speed
```

如果：

```text
speed <= 0
```

则：

```text
ETA: --
```

---

## 14. 下载任务过多

Telegram 消息长度有限。

如果任务很多：

```text
只展示前 N 个
```

并显示：

```text
还有 X 个下载任务……
```

同时保留总数。

---

## 15. Queue

复用：

```python
state.QUEUE
```

至少显示：

```text
待处理
下载中
重试中
```

不得重新实现 Queue。

---

## 16. Worker

显示：

```text
Worker 数量
BUSY
IDLE
UNHEALTHY
```

如果当前 Worker 对象可靠提供当前任务，可以展示：

```text
#1 → example.mp4
```

否则不要猜。

Reporter 不负责创建、销毁、重启或调度 Worker。

---

## 17. Client

复用现有：

```text
state.client
state.bot_client
```

展示：

```text
Main Client：🟢
Bot Client：🟢
```

不得创建额外 Client。

---

## 18. Stats

复用现有：

```text
stats.py
task_events.jsonl
collect_stats()
rebuild_stats()
```

统计口径尽量与 `/stats` 一致。

至少：

```text
Received
Success
Failed
Retry
Auto Replay
Dedupe
Cancelled
```

没有现成指标就不要重建统计系统。

---

## 19. Task Events

复用已有事件：

```text
RECEIVED
QUEUED
RUNNING
RETRY
SUCCESS
FAILED
CANCELLED
REMOVED
DEDUP_HIT
```

Auto Replay 如已有独立事件/日志机制，应复用现有来源。

不要建立第二套事件系统。

---

## 20. Event Notification

支持：

```text
STARTUP
RETRY
AUTO_REPLAY
SUCCESS
FAILED
ERROR
RECOVERY
SHUTDOWN
```

下载开始通知默认关闭，避免高并发时刷屏。

---

## 21. Retry

示例：

```text
🔄 任务重试

文件：example.mp4
第：2/3 次
原因：Connection reset
```

中间 Retry 不应被视为最终 Failed。

---

## 22. Auto Replay

示例：

```text
♻️ 自动重放

文件：example.mp4
第：2/3 次
原因：之前下载失败
```

必须与普通 Retry 区分。

Reporter 不得主动触发或修改 Auto Replay。

---

## 23. Success

示例：

```text
✅ 下载成功

文件：example.mp4
大小：1.32 GB
耗时：03m 21s
Worker：#2
```

---

## 24. Final Failed

只有最终失败才单独通知：

```text
❌ 下载失败

文件：example.mp4
重试：3/3
耗时：05m 42s
原因：Connection reset
```

不要每个底层失败都刷屏。

---

## 25. Error / Recovery

异常：

```text
⚠️ Userbot 异常

组件：Worker #2
状态：UNHEALTHY
原因：Connection error
```

恢复：

```text
✅ Worker #2 已恢复

状态：HEALTHY
```

---

## 26. Error Dedup

同一异常持续发生：

```text
Worker #2 error
Worker #2 error
Worker #2 error
```

只能通知一次。

建议 fingerprint：

```text
component
+
error type
+
normalized error message
```

不要直接使用完整 traceback 作为 fingerprint。

状态：

```text
NORMAL
↓
ERROR
↓
ERROR CONTINUES
↓
RECOVERY
↓
NORMAL
```

通知：

```text
ERROR：一次
持续异常：不重复
RECOVERY：一次
```

---

## 27. Startup

启动时：

```text
🚀 Userbot 已启动

启动时间：13:20:15
Workers：3
状态：🟢 RUNNING
```

配置：

```python
REPORT_STARTUP = True
```

---

## 28. Shutdown

正常关闭：

```text
🛑 Userbot 正在关闭

运行时间：08h 32m
当前下载：1
队列任务：3
```

配置：

```python
REPORT_SHUTDOWN = True
```

如果 Telegram 不可用，发送失败不能阻塞关闭。

---

## 29. 配置

建议：

```python
REPORT_ENABLED = True

REPORT_INTERVAL_SECONDS = 300

REPORT_PROGRESS_ENABLED = True
REPORT_PROGRESS_INTERVAL_SECONDS = 15

REPORT_STARTUP = True
REPORT_SHUTDOWN = True
REPORT_ERROR = True
REPORT_RECOVERY = True
REPORT_TASK_EVENTS = True

REPORT_DOWNLOAD_START = False
REPORT_DOWNLOAD_SUCCESS = True
REPORT_DOWNLOAD_FAILED = True
REPORT_RETRY = True
```

遵循项目现有配置风格。

---

## 30. Reporter 生命周期

启动：

```text
Core initialization
↓
Queue / Worker / Client
↓
Reporter
↓
Startup Notification
↓
Status Panel
↓
Reporter Loop
```

停止：

```text
STOP_EVENT
↓
Reporter stop
↓
Shutdown Notification
↓
Reporter exit
```

复用：

```python
state.STOP_EVENT
```

不要创建第二个 Stop Event。

---

## 31. Reporter Task

Reporter 应作为独立 asyncio Task：

```python
asyncio.create_task(reporter.run())
```

不得阻塞：

- 主事件循环
- 下载
- Queue
- Worker
- Bot handlers

---

## 32. 异常隔离

Reporter 主循环普通异常必须捕获。

推荐逻辑：

```python
while not stop_event.is_set():
    try:
        ...
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(...)
    await sleep(...)
```

Reporter 异常不能让 Reporter 永久死亡，也不能影响核心系统。

---

## 33. Telegram API 错误

至少考虑：

```text
Timeout
FloodWait
RPCError
NetworkError
MessageNotModified
MessageIdInvalid
```

具体异常类型以当前 Telethon 版本为准。

发送失败：

```text
记录日志
↓
本轮结束
↓
下一周期再尝试
```

禁止无限高速重试。

---

## 34. MessageNotModified

如果状态没有变化：

```text
MessageNotModified
```

应视为正常，不应产生 ERROR。

---

## 35. MessageIdInvalid

如果状态消息不存在：

```text
status_message_id = None
```

下一周期重新创建。

Reporter 不得因此永久失效。

---

## 36. FloodWait

遇到 FloodWait：

```text
记录
↓
等待 / 跳过本轮
↓
下一周期继续
```

不要进入紧密重试循环。

---

## 37. Snapshot 并发安全

采用：

```text
快速读取
↓
创建 snapshot
↓
释放锁
↓
格式化
↓
Telegram API
```

禁止：

```text
持有 QUEUE_LOCK
↓
Telegram API
```

如果 `ACTIVE_DOWNLOADS` 是共享字典，先安全复制。

例如：

```python
downloads = list(state.ACTIVE_DOWNLOADS.values())
```

---

## 38. Status Message ID

第一阶段仅内存保存：

```python
status_message_id = None
```

第一次：

```text
send_message()
```

之后：

```text
edit_message()
```

程序重启后重新创建。

不增加持久化。

---

## 39. Event Cursor

Reporter 不得每轮扫描整个历史 `task_events.jsonl` 并重复发送。

优先使用现有增量接口。

如果没有：

```text
记录当前读取位置 / offset
```

Reporter 启动时：

> 不应把旧历史全部重新通知给用户。

只处理 Reporter 启动后的新事件。

---

## 40. 性能边界

Reporter 不得：

- 高频扫描整个事件文件；
- 每秒调用 Telegram API；
- 持有锁进行网络请求；
- 阻塞事件循环；
- 大量创建 Task；
- 保存大量重复历史状态。

建议：

```text
Status：300s
Download Progress：15s
Important Event：立即
```

---

## 41. 安全边界

消息中不得泄露：

- Bot Token
- API Hash
- API ID
- Cookie
- Authorization
- 敏感路径
- 完整敏感异常信息

必要时对异常信息做清洗。

---

## 42. 推荐内部结构

单文件即可：

```text
reporter.py

Reporter
├── lifecycle
│   ├── start()
│   ├── stop()
│   └── run()
│
├── snapshot
│   ├── collect_runtime()
│   ├── collect_downloads()
│   ├── collect_queue()
│   ├── collect_workers()
│   ├── collect_connections()
│   └── collect_stats()
│
├── formatter
│   ├── format_bytes()
│   ├── format_duration()
│   ├── format_speed()
│   ├── format_eta()
│   └── build_status_text()
│
├── telegram
│   ├── send_status()
│   ├── edit_status()
│   └── send_event()
│
└── protection
    ├── event dedupe
    ├── error dedupe
    └── exception isolation
```

不要为了架构漂亮而拆成大量文件。

---

## 43. 推荐接口

可以采用：

```python
class Reporter:

    async def start(self):
        ...

    async def stop(self):
        ...

    async def run(self):
        ...

    async def update_status(self):
        ...

    async def notify_event(self, event):
        ...

    async def notify_error(self, ...):
        ...

    def build_status_text(self):
        ...
```

根据真实项目调整，不要求机械照搬。

---

## 44. 纯函数

尽量把以下逻辑做成纯函数：

```python
format_bytes()
format_duration()
format_speed()
format_eta()
build_status_text()
```

便于测试。

---

## 45. 测试范围

### 必须测试

```text
1. Startup
2. Status Panel 创建
3. Status Panel 编辑
4. ACTIVE_DOWNLOADS 有任务
5. ACTIVE_DOWNLOADS 为空
6. Queue
7. Retry Queue
8. 多 Worker
9. BUSY
10. IDLE
11. UNHEALTHY
12. Client Connected
13. Client Disconnected
14. Retry
15. Auto Replay
16. Success
17. Final Failed
18. Error
19. Recovery
20. Shutdown
21. MessageNotModified
22. Telegram Timeout
23. Telegram API Error
24. FloodWait
25. MessageIdInvalid
26. Error Dedup
27. Reporter Exception Isolation
28. Telegram 不可用时不无限重试
29. 大量下载任务消息长度控制
30. ETA 无效数据
```

### 不需要重新测试

```text
下载算法
yt-dlp
Chrome Agent
Queue 调度算法
Worker 下载算法
Retry 算法
Auto Replay 算法
Dedupe 算法
Telegram 文件下载/上传
```

Reporter 只测试：

> 能否正确观察和展示这些系统产生的状态。

---

## 46. 测试分层

### Level 1

纯函数测试：

```text
format_bytes
format_duration
format_speed
format_eta
build_status_text
```

### Level 2

Reporter 单元测试，Mock：

```text
Telegram Client
state
queue
stats
```

### Level 3

项目全量回归。

---

## 47. 测试顺序

先：

```bash
pytest tests/test_reporter.py
```

实际路径以仓库为准。

然后：

```bash
pytest
```

最后运行项目已有静态检查。

例如：

```bash
python -m compileall tg_userbot
```

如果项目已有：

```text
ruff
mypy
flake8
```

按现有配置执行。

不要为了本任务新增检查工具。

---

## 48. 变更预算

目标：

```text
业务代码：约 300～700 行
测试代码：约 200～400 行
```

如果业务代码明显超过：

```text
1000 行
```

必须检查是否过度设计。

如果修改：

```text
10+ 个核心文件
```

必须重新审查范围。

---

## 49. 文件预算

理想：

```text
新增：
1～2 个文件

修改：
2～4 个现有文件

测试：
1～2 个文件
```

如果明显超出，需要解释原因。

---

## 50. 依赖预算

目标：

```text
新增第三方依赖：0
```

如果确实必要：

1. 优先标准库；
2. 再考虑已有依赖；
3. 最后才增加新依赖；
4. 最终报告说明原因。

---

## 51. AI Agent 禁止事项

禁止：

- 顺手优化无关代码；
- 重构下载系统；
- 重构 Queue；
- 重构 Worker；
- 重构 Stats；
- 修改 Retry；
- 修改 Auto Replay；
- 修改 Dedupe；
- 修改 `/progress`；
- 修改 `/stats`；
- 改测试来掩盖实现错误；
- 为了漂亮架构引入复杂设计；
- 增加本文档没有要求的功能。

遇到现有代码不理想：

> 优先兼容，而不是重构。

---

## 52. 实施流程

### Phase 1 — Analysis

读取真实仓库。

原则上不修改代码。

### Phase 2 — Implementation Plan

明确：

```text
新增哪些文件
修改哪些文件
每个文件改什么
为什么改
风险
测试
```

### Phase 3 — Implementation

优先完成：

```text
reporter.py
```

然后最小化修改：

```text
app.py
config.py
```

### Phase 4 — Tests

增加 Reporter 测试。

### Phase 5 — Targeted Tests

运行 Reporter 测试。

### Phase 6 — Regression

运行完整测试。

### Phase 7 — Static Check

运行项目已有静态检查。

### Phase 8 — Final Diff Review

检查是否出现无关修改。

---

## 53. 最终验收 Checklist

```text
[ ] Reporter 独立模块存在
[ ] Status Panel 创建
[ ] Status Panel 编辑
[ ] 不重复创建
[ ] Runtime 正确
[ ] Last Activity 正确
[ ] ACTIVE_DOWNLOADS 正确
[ ] Queue 正确
[ ] Retry 正确
[ ] Worker 正确
[ ] Client 状态正确
[ ] 今日 Stats 正确
[ ] Startup 通知
[ ] Retry 通知
[ ] Auto Replay 通知
[ ] Success 通知
[ ] Final Failed 通知
[ ] Error 通知
[ ] Recovery 通知
[ ] Shutdown 通知
[ ] Error Dedup
[ ] Telegram Error 不影响核心业务
[ ] FloodWait 不无限重试
[ ] MessageNotModified 正确处理
[ ] MessageIdInvalid 可重新创建 Status
[ ] 消息长度受控
[ ] 没有高频 API 调用
[ ] 没有 Lock + Network
[ ] 无不必要依赖
[ ] 未修改下载核心逻辑
[ ] 未修改 Queue 核心逻辑
[ ] 未修改 Worker 核心逻辑
[ ] 未修改 Retry
[ ] 未修改 Auto Replay
[ ] 未修改 Dedupe
[ ] 未修改 /progress
[ ] 未修改 /stats
[ ] Reporter 测试通过
[ ] 全量测试通过
[ ] 静态检查通过
[ ] Git diff 已审查
```

---

## 54. 完成定义

只有满足：

```text
功能实现
+
Reporter 测试通过
+
现有测试通过
+
静态检查通过
+
最终 diff 审查
```

才能宣布完成。

“代码写完”不等于完成。

---

# 55. 给 AI Coding Agent 的最终指令

你现在是本项目的 Coding Agent。

请严格执行本文档。

优先级：

```text
1. 保持现有功能不变
2. 最小范围实现 Reporter
3. 复用现有状态和事件
4. 不重复实现已有系统
5. 不扩大需求
6. 先理解真实代码
7. 小步修改
8. 先测试再修复
9. 完成后全量验证
10. 最后审查 git diff
```

如果本文档与实际代码不一致：

> 以实际代码为准，采用最小兼容方案，并在最终报告说明。

如果某字段无法可靠获取：

> 显示 `--`，不要猜测。

如果发现需要修改核心模块：

> 先判断能否通过 Reporter 侧兼容解决；只有确实无法实现时才做最小修改，并说明原因。

最终目标：

```text
现有 Userbot
      +
低耦合 Runtime Reporter
      ↓
主动运行状态汇报
      ↓
不改变原有业务行为
      ↓
Reporter 自己故障也不会拖垮 Userbot
```
