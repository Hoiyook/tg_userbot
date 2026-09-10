# Chrome 任务维护：主动取消排队/进行中任务

> 给 DeepSeek 直接执行的开发任务书  
> 基于 `Hoiyook/tg_userbot` 的 `feature/multi-worker-v2.10` 分支编写。  
> 当前基线 HEAD：`a5fce71922678ba97afaebdbe3575d27449f5f69`

## 1. 目标

在现有 Chrome Agent 下载体系上增加：

- `/chrome_tasks`：查看当前可维护的 Chrome 任务
- `/chrome_cancel <序号>`：取消排队中或正在下载的任务
- 新增 `CANCELLED` 终态
- 正在下载的任务必须真正中止 Chrome 下载
- 删除该任务产生的未完成/半成品文件
- 取消一个任务不能停止整个 Chrome Agent
- 取消后后续任务继续执行
- CANCELLED 不重试、不重新执行
- CANCELLED 重启后仍保持 CANCELLED

**只做最小增量修改，严禁顺手重构 Chrome 系统。**

## 2. 当前真实架构

当前代码已经确认：

```text
Saved Messages
    ↓
commands.py
    ↓
chrome_client.py
    ↓
chrome_requests.json
    ↓
独立 Chrome Agent（chrome_agent.py）
    ↓
chrome_tasks.json
    ↓
chrome_client.py notify_loop
    ↓
Telegram 通知
```

关键事实：

- `chrome_client.py`：User Bot 侧。
- `chrome_agent.py`：真正执行 Chrome 下载的独立进程。
- `chrome_requests.json`：User Bot 独占写，Agent 只读。
- `chrome_tasks.json`：Agent 独占写，是任务状态事实源。
- 当前状态：`PENDING`、`RUNNING`、`RETRY_WAIT`、`SUCCESS`、`FAILED`。
- `process_pending_tasks()` 当前 FIFO 串行执行，一次只跑一个。
- `run_download_attempt()` 当前通过 CDP 事件判断下载。
- 当前已经获得 `Browser.downloadWillBegin` 的 `guid`。
- 当前 `ChromeCDPClient.command()` 可以发送任意 CDP command。
- `/chrome_stop` 是停止整个 Agent，**绝不能用来实现单任务取消**。

## 3. 新状态

增加：

```text
CANCELLED
```

状态机：

```text
PENDING
   ├──→ CANCELLED
   ↓
RUNNING
   ├──→ SUCCESS
   ├──→ RETRY_WAIT → RUNNING
   ├──→ FAILED
   └──→ CANCELLED
```

`CANCELLED` 是终态。

`claim_next()` 必须自动跳过 `CANCELLED`。

不要删除 task 记录：

```python
tasks.remove(task)
```

禁止。

应该保留任务并：

```python
task["status"] = "CANCELLED"
task["error"] = "用户主动取消"
task["finished_at"] = 当前时间
task["updated_at"] = 当前时间
```

取消不会增加 `attempts`。

## 4. `/chrome_tasks`

新增命令：

```text
/chrome_tasks
```

只显示：

- `RUNNING`
- `PENDING`
- `RETRY_WAIT`

建议顺序：

1. RUNNING
2. PENDING
3. RETRY_WAIT

每项至少显示：

```text
序号
短 task_id（前 8 位）
状态
URL
label（如果有）
download_subdir（如果有）
```

示例：

```text
🌐 Chrome 任务

1. 🟢 进行中
   ID: a1b2c3d4
   URL: https://example.com/a.zip

2. ⏳ 排队中
   ID: e5f6g7h8
   URL: https://example.com/b.zip

3. ⏳ 等待重试
   ID: i9j0k1l2
   URL: https://example.com/c.zip

可取消：1、2、3
```

没有可取消任务：

```text
🌐 Chrome 任务

当前没有可取消的任务。
```

## 5. `/chrome_cancel`

新增：

```text
/chrome_cancel <序号>
```

例如：

```text
/chrome_cancel 2
```

序号必须对应 `/chrome_tasks` 当前显示的列表。

参数错误：

```text
❌ 用法：/chrome_cancel <序号>

先发送 /chrome_tasks 查看任务列表。
```

非数字：

```text
❌ 序号必须是数字。
```

无效序号：

```text
❌ 任务序号无效，请先发送 /chrome_tasks。
```

已完成：

```text
ℹ️ 任务已经完成，无法取消。
```

已失败：

```text
ℹ️ 任务已经失败，无法取消。
```

已取消：

```text
ℹ️ 任务已经取消。
```

## 6. 权限

新增命令必须复用现有 Chrome owner 权限检查。

当前 `handle_chrome_command()` 已有 owner 机制。

**不要新建另一套权限系统。**

## 7. 取消 PENDING / RETRY_WAIT

排队任务取消：

```text
找到 task
↓
确认不是 RUNNING
↓
status = CANCELLED
↓
保存 chrome_tasks.json
↓
后续 claim_next() 永远跳过
```

不能直接删除。

取消后不进入 `RETRY_WAIT`。

## 8. 取消 RUNNING：核心要求

如果任务：

```text
RUNNING
```

不能只修改 JSON。

必须真正中止 Chrome 当前下载。

**绝对禁止：**

```python
stop_agent()
```

也禁止：

```python
os.kill(agent_pid, ...)
```

更不能杀 Chrome。

正确结果：

```text
A RUNNING
B PENDING
C PENDING

取消 A

A CANCELLED
B RUNNING
C PENDING
```

## 9. 推荐 IPC

当前 User Bot 与 Agent 已经使用 JSON 文件 IPC。

优先继续使用现有设计。

如果当前结构无法安全复用 `chrome_requests.json`，可以新增：

```text
chrome_cancel_requests.json
```

格式可类似：

```json
{
  "cancellations": [
    {
      "task_id": "xxxxxxxx",
      "created_at": "2026-09-10 12:00:00"
    }
  ]
}
```

原则：

- User Bot 独占写
- Agent 只读
- temp + `os.replace()` 原子写

**不要为了取消功能引入 Redis、数据库、socket server 等新基础设施。**

## 10. CDP 取消

当前 `run_download_attempt()` 已经从：

```text
Browser.downloadWillBegin
```

获取：

```text
guid
```

因此优先使用：

```text
Browser.cancelDownload
```

参数：

```json
{
  "guid": "当前任务的 guid"
}
```

当前 `ChromeCDPClient.command()` 已经是通用 CDP command 发送入口，应优先复用。

必须处理：

- guid 已存在
- guid 尚未产生
- 下载已经完成
- 下载已经被取消
- CDP cancel 调用失败

## 11. guid 尚未产生的竞态

必须处理：

```text
RUNNING
↓
用户取消
↓
guid 还没有产生
↓
之后才收到 downloadWillBegin
```

不能因此取消失败。

应该保留一个任务级取消请求/事件。

当随后收到：

```text
Browser.downloadWillBegin
```

拿到 guid 后立即取消。

最终：

```text
CANCELLED
```

## 12. run_download_attempt

当前函数：

```python
run_download_attempt(cdp, url, download_dir, timeout)
```

应最小扩展，使它支持取消，例如：

```python
run_download_attempt(
    cdp,
    url,
    download_dir,
    timeout,
    cancel_event=None,
)
```

具体方案可以根据实际代码调整。

但必须保持正常返回语义：

```text
正常完成 → SUCCESS 路径
超时 → 原有 FAILED/RETRY 路径
取消 → CANCELLED 路径
```

不要无意义地修改所有调用点。

如果修改返回值，必须检查全部调用方。

## 13. 不要用粗暴 sleep 轮询

当前下载是 CDP 事件驱动：

```python
cdp.next_event(timeout=remaining)
```

不要改成：

```python
while:
    sleep(1)
    检查取消
```

应尽量使用 asyncio 的事件/任务等待机制。

不能因为增加取消而丢失：

- `downloadWillBegin`
- `downloadProgress`

## 14. 取消与完成的竞态

推荐规则：

```text
取消请求已经先被确认
→ CANCELLED

SUCCESS 已经先被确认
→ 保留 SUCCESS
```

不能把已经完整成功的任务删除或改成 CANCELLED。

## 15. 半成品清理

这是已经确认的需求：

> **取消 RUNNING 任务时，删除已经下载了一部分的临时/未完成文件，只保留完整完成的文件。**

Chrome 常见：

```text
xxx.zip.crdownload
```

取消后必须删除当前任务对应的未完成文件。

**禁止：**

```python
删除整个 download_dir 下所有 .crdownload
```

因为可能存在其他任务。

清理必须绑定当前任务的：

- task
- guid
- filename
- task download directory

同时兼容已有：

```text
download_subdir
```

不能误删其他目录任务。

## 16. 取消后的任务执行

取消当前 RUNNING：

```text
取消 Chrome 下载
↓
清理半成品
↓
CANCELLED
↓
save_tasks()
↓
返回 process_pending_tasks()
↓
继续 claim_next()
```

不能让 Agent 因取消当前任务而退出。

## 17. 通知

现有：

```python
notify_pending_results()
```

目前主要处理：

```text
SUCCESS
FAILED
```

建议加入：

```text
CANCELLED
```

通知：

```text
🛑 Chrome 下载已取消

任务 ID：a1b2c3d4
状态：CANCELLED
URL：https://example.com/a.zip
原因：用户主动取消
```

继续复用现有：

```text
notified_at
```

保证通知幂等。

## 18. `/chrome_status`

现有状态统计增加：

```text
取消
```

例如：

```text
任务统计：累计 10
（成功 5 / 失败 1 / 取消 2 / 进行中 1 / 排队 1）
```

不要改变原有字段含义。

## 19. 原有功能必须保持

以下全部不能被破坏：

```text
/chrome URL
/chrome A URL
/chrome #标签 URL
/chrome A/B/#标签 URL
```

必须保持：

- URL 解析
- label 解析
- download_subdir
- safe_subdir
- Chrome Profile
- Proxy
- CDP
- 文件命名
- 重试
- 任务持久化
- 结果通知

不要修改普通 Telegram 下载系统。

## 20. 文件修改范围

优先只修改：

```text
tg_userbot/chrome_client.py
tg_userbot/chrome_agent.py
tests/相关 Chrome 测试
```

默认不要新建模块。

除非实际代码证明必须，否则不要修改：

```text
app.py
state.py
queue.py
workers.py
reporter.py
caption_filter.py
naming.py
```

尤其不要为了这个需求改普通下载队列。

## 21. 测试：只做关键测试

不要增加几十个测试。

至少覆盖：

### 测试 1

```text
PENDING → CANCELLED
```

并验证 `claim_next()` 不会再认领。

### 测试 2

```text
RETRY_WAIT → CANCELLED
```

### 测试 3

```text
RUNNING → CANCELLED
```

并验证其他任务不受影响。

### 测试 4

```text
CANCELLED
```

不会被 `claim_next()` 认领。

### 测试 5

取消当前任务时：

```text
xxx.zip.crdownload
```

被删除。

### 测试 6

取消 A：

```text
A.crdownload → 删除
B.crdownload → 保留
```

### 测试 7

`SUCCESS` / `FAILED` 不能被取消。

### 测试 8

CANCELLED 可以被通知，并设置 `notified_at`。

如果项目已经有 FakeCDP，必须复用。

## 22. 开发顺序

严格按这个顺序：

### Step 1

先读取：

```text
chrome_client.py
chrome_agent.py
tests/所有 Chrome 相关测试
```

确认实际调用链。

### Step 2

写/修改最小单元测试。

### Step 3

实现 Agent 内部：

```text
PENDING / RETRY_WAIT cancel
RUNNING cancel
```

### Step 4

实现 CDP：

```text
Browser.cancelDownload
```

### Step 5

实现半成品清理。

### Step 6

实现：

```text
/chrome_tasks
/chrome_cancel N
```

### Step 7

加入 CANCELLED 通知。

### Step 8

更新 `/chrome_status`。

### Step 9

运行：

```bash
pytest -q
```

再运行专门 Chrome 测试（如果存在）。

### Step 10

检查：

```bash
git diff --stat
git diff
```

确认没有无关修改。

## 23. 常见错误，禁止

### 错误 A

直接：

```python
tasks.remove(task)
```

禁止。

### 错误 B

只修改：

```text
chrome_tasks.json
```

不真正停止 Chrome。

禁止。

### 错误 C

调用：

```python
stop_agent()
```

禁止。

### 错误 D

删除整个目录中的半成品。

禁止。

### 错误 E

CANCELLED 自动重试。

禁止。

### 错误 F

取消 A 后 B 不继续执行。

禁止。

### 错误 G

guid 尚未产生时取消失败。

禁止。

### 错误 H

为了本功能修改 Chrome 启动、Profile、Proxy。

禁止。

### 错误 I

顺手重构无关模块。

禁止。

## 24. 最终验收场景

### 场景 A：取消排队

```text
A RUNNING
B PENDING
C PENDING

/chrome_cancel 2

A RUNNING
B CANCELLED
C PENDING
```

A 完成后：

```text
C RUNNING
```

### 场景 B：取消运行中

存在：

```text
xxx.zip.crdownload
```

执行：

```text
/chrome_cancel 1
```

最终：

```text
task = CANCELLED
xxx.zip.crdownload 不存在
Agent 继续运行
```

### 场景 C：取消 A 不影响 B

```text
A RUNNING
B PENDING
```

取消 A：

```text
A CANCELLED
B RUNNING
```

### 场景 D：重启 Agent

CANCELLED 任务重启后：

```text
CANCELLED
```

不能变成：

```text
PENDING
```

### 场景 E：SUCCESS

SUCCESS 文件必须保留。

执行取消不能删除成品。

## 25. 数据兼容

旧 `chrome_tasks.json` 没有 CANCELLED 没关系。

旧状态：

```text
PENDING
RUNNING
RETRY_WAIT
SUCCESS
FAILED
```

必须继续正常工作。

不要重写历史 JSON 数据结构。

新增字段只允许增量添加。

## 26. 完成标准

全部满足才算完成：

```text
[ ] /chrome_tasks 可查看任务
[ ] /chrome_cancel N 可取消 PENDING
[ ] /chrome_cancel N 可取消 RETRY_WAIT
[ ] /chrome_cancel N 可取消 RUNNING
[ ] RUNNING 取消真正中止 Chrome 下载
[ ] RUNNING 取消删除未完成文件
[ ] 不误删其他任务文件
[ ] CANCELLED 不再执行
[ ] CANCELLED 不自动重试
[ ] CANCELLED 重启后保持
[ ] 不停止整个 Agent
[ ] 取消后下一个任务继续
[ ] SUCCESS 不误取消
[ ] FAILED 不误取消
[ ] CANCELLED 可以通知
[ ] /chrome_status 统计兼容
[ ] 原 /chrome 功能保持
[ ] 普通 Telegram 下载功能保持
[ ] pytest -q 通过
[ ] git diff 无无关修改
```

## 27. 给 DeepSeek 的最终执行指令

现在开始执行：

1. 先读取真实仓库代码，不要猜。
2. 重点检查 `chrome_client.py`、`chrome_agent.py` 和现有 Chrome 测试。
3. 按本文实现单任务取消。
4. 优先最小修改。
5. 不要重构现有 Chrome 架构。
6. 不要修改普通 Telegram 下载系统。
7. 不要修改 Reporter。
8. 不要修改 Caption。
9. 不要杀 Agent 实现单任务取消。
10. RUNNING 必须真正中止 CDP 下载。
11. 取消 RUNNING 必须清理当前任务半成品。
12. 使用 `CANCELLED` 终态。
13. 取消后继续执行后续任务。
14. 添加最少但关键的测试。
15. 执行完整测试。
16. 最后检查 `git diff`。
17. 如果实际代码与本任务书存在差异，以仓库真实代码为准，但选择最小兼容修改，不要扩大需求。

如果实现过程中发现某个设计无法直接落地，不要自行重构整个系统；先采用与当前架构最接近的最小方案。

## 28. 完成后汇报格式

```text
## Chrome 任务取消功能完成

### 修改文件
- xxx
- xxx

### 实现功能
- /chrome_tasks
- /chrome_cancel N
- 排队任务取消
- 运行中任务取消
- 半成品清理
- CANCELLED 状态
- CANCELLED 通知

### 测试
pytest -q
结果：XX passed

### Git Diff
修改文件数量：X
新增代码：X 行
删除代码：X 行

### 风险/注意事项
- ...
```

如果测试失败，不得写“基本完成”。

必须明确：

- 哪个测试失败
- 为什么失败
- 是否已修复
- 是否还有风险
