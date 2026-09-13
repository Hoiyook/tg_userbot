# Userbot 数据根目录迁移与 Download / Runtime 路径解耦改造任务书

> 仓库：`Hoiyook/tg_userbot`
>
> 分支：`feature/tag-listener`
>
> 基线提交：`686159e06907e0f0d7b4eccac9c566ef25edb739`
>
> 执行方式：DeepSeek Coding 负责实现；架构师负责最终审计。
>
> **目标：PC 端优先，把 Userbot 正式数据根目录迁移到 `/Volumes/V1`，并彻底解耦媒体下载目录与 Runtime 运行数据目录。**
>
> **本任务只做路径/数据目录基础设施改造，不改变业务逻辑。**

---

# 1. 最终目录结构

迁移完成后：

```text
/Volumes/V1/
├── downloads/
│   ├── Telegram频道A/
│   ├── Telegram频道B/
│   ├── Chrome/
│   │   ├── A/
│   │   └── B/
│   └── ...
│
└── runtime/
    ├── tg_userbot.db
    ├── listen.json
    ├── whitelist_config.json
    ├── thread_config.json
    ├── task_events.jsonl
    ├── download_history.txt
    ├── download.log
    ├── chrome_agent.log
    ├── cd2_launch.log
    ├── chrome_tasks.json
    ├── chrome_requests.json
    ├── chrome_cancel_requests.json
    ├── userbot.out
    ├── chrome_agent.out
    ├── *.pid
    └── 其他确认属于 Runtime 的运行态文件
```

核心变量：

```text
DATA_ROOT    = /Volumes/V1
DOWNLOAD_DIR = /Volumes/V1/downloads
RUNTIME_DIR  = /Volumes/V1/runtime
```

---

# 2. 当前代码问题

当前 `config.py` 已经支持：

```text
TG_SAVE_FOLDER → SAVE_FOLDER
```

但 Runtime 仍然是：

```python
RUNTIME_DIR = os.path.join(SAVE_FOLDER, "runtime")
```

因此：

```text
SAVE_FOLDER
└── runtime
```

被强耦合。

当前 Runtime 已包含：

```text
tg_userbot.db
listen.json
whitelist_config.json
thread_config.json
task_events.jsonl
download_history.txt
download.log
chrome_agent.log
cd2_launch.log
chrome_tasks.json
chrome_requests.json
chrome_cancel_requests.json
PID / stdout 等
```

这次需要把二者彻底分开。

---

# 3. 配置设计

## 3.1 正式总根路径

新增：

```python
DATA_ROOT
```

PC 默认：

```python
DATA_ROOT = "/Volumes/V1"
```

环境变量覆盖：

```text
TG_DATA_ROOT
```

优先级：

```text
TG_DATA_ROOT
    ↓
/Volumes/V1
```

---

# 4. Download 路径

新增正式变量：

```python
DOWNLOAD_DIR
```

默认：

```text
/Volumes/V1/downloads
```

推荐优先级：

```text
TG_DOWNLOAD_DIR
    ↓
TG_SAVE_FOLDER（旧兼容变量）
    ↓
DATA_ROOT/downloads
```

保留：

```python
SAVE_FOLDER = DOWNLOAD_DIR
```

用于兼容现有代码。

要求：

> 新代码优先使用 `DOWNLOAD_DIR`；`SAVE_FOLDER` 只是旧兼容别名。

不要为了替换变量名而全仓库机械重构。

---

# 5. Runtime 路径

正式变量：

```python
RUNTIME_DIR
```

默认：

```text
/Volumes/V1/runtime
```

环境变量：

```text
TG_RUNTIME_DIR
```

优先级：

```text
TG_RUNTIME_DIR
    ↓
DATA_ROOT/runtime
```

关键约束：

> **严禁继续使用 `SAVE_FOLDER/runtime` 作为 Runtime 根目录。**

---

# 6. 最终配置模型

推荐实现逻辑：

```python
DATA_ROOT = os.environ.get("TG_DATA_ROOT", "/Volumes/V1")

DOWNLOAD_DIR = os.environ.get(
    "TG_DOWNLOAD_DIR",
    os.environ.get(
        "TG_SAVE_FOLDER",
        os.path.join(DATA_ROOT, "downloads"),
    ),
)

RUNTIME_DIR = os.environ.get(
    "TG_RUNTIME_DIR",
    os.path.join(DATA_ROOT, "runtime"),
)

# legacy compatibility
SAVE_FOLDER = DOWNLOAD_DIR
```

实际代码必须结合当前 `config.py` 的加载顺序实现，不允许机械粘贴导致现有平台探测、Secrets、日志初始化顺序变化。

---

# 7. Runtime 文件统一归属

至少以下文件统一进入：

```text
RUNTIME_DIR
```

包括：

```text
tg_userbot.db
listen.json
whitelist_config.json
thread_config.json
task_events.jsonl
download_history.txt
download.log
chrome_agent.log
cd2_launch.log
chrome_tasks.json
chrome_requests.json
chrome_cancel_requests.json
chrome_agent.pid
userbot.out
chrome_agent.out
```

以及 Phase 1 审计确认属于 Runtime 的其他文件。

---

# 8. 媒体文件统一归属

所有用户真正下载的媒体都属于：

```text
DOWNLOAD_DIR
```

例如：

```text
/Volumes/V1/downloads/
├── 来源A/
├── 来源B/
└── Chrome/
```

禁止 Runtime 目录承载媒体文件。

---

# 9. Chrome 特别规则

## 9.1 Chrome 下载

当前 Chrome 下载目录逻辑必须归属：

```text
DOWNLOAD_DIR
```

例如：

```text
/Volumes/V1/downloads/Chrome/
```

现有：

```text
/chrome A/B/#标注 URL
```

的目录和命名行为必须保持不变。

## 9.2 Chrome Agent Runtime

以下必须属于：

```text
RUNTIME_DIR
```

```text
chrome_agent.log
chrome_tasks.json
chrome_requests.json
chrome_cancel_requests.json
chrome_agent.pid
chrome_agent.out
```

禁止 Chrome 模块自己拼：

```text
SAVE_FOLDER/runtime
```

---

# 10. SQLite

继续保留现有：

```text
TG_RUNTIME_DB
```

最终默认：

```text
RUNTIME_DIR/tg_userbot.db
```

即：

```python
RUNTIME_DB_FILE = os.environ.get(
    "TG_RUNTIME_DB",
    os.path.join(RUNTIME_DIR, "tg_userbot.db"),
)
```

`TG_RUNTIME_DB` 的单文件覆盖能力不能丢。

本任务禁止重新设计 SQLite。

---

# 11. 日志

所有 Runtime 日志：

```text
RUNTIME_DIR/download.log
RUNTIME_DIR/chrome_agent.log
RUNTIME_DIR/cd2_launch.log
```

启动 stdout/stderr：

```text
RUNTIME_DIR/userbot.out
RUNTIME_DIR/chrome_agent.out
```

必须检查日志模块是否还有自行推导路径的逻辑。

---

# 12. Cleanup

当前：

```python
clean_temp_files(root=None)
```

默认使用 `SAVE_FOLDER`。

这个函数实际是：

> 清理媒体目录中的 `.download` 临时文件。

因此改造后应该继续针对：

```text
DOWNLOAD_DIR
```

而不是 Runtime。

不要扩大 Cleanup 的扫描范围。

---

# 13. CD2

CD2 的媒体扫描/同步根应该是：

```text
DOWNLOAD_DIR
```

不要改成：

```text
DATA_ROOT
```

也不要把：

```text
RUNTIME_DIR
```

纳入媒体处理。

例如：

```text
/Volumes/V1/runtime/tg_userbot.db
```

绝不能因为根目录统一而被 CD2 当作媒体数据。

---

# 14. Reporter / Stats / Queue

凡是读取：

```text
SQLite
JSON Runtime 状态
task_events
download_history
log
```

都应使用：

```text
RUNTIME_DIR
```

凡是读写媒体：

```text
DOWNLOAD_DIR
```

禁止这些模块各自推导：

```text
SAVE_FOLDER/runtime
```

---

# 15. run.sh

当前 `run.sh` 已经通过：

```python
config.RUNTIME_DIR
```

获取 Runtime 路径，这种方式继续保留。

要求：

- `userbot.out` → `RUNTIME_DIR`
- `chrome_agent.out` → `RUNTIME_DIR`
- PID → `RUNTIME_DIR`

不得在 shell 中正常逻辑里硬编码：

```text
~/Downloads/Nagram/runtime
```

也不要直接硬编码：

```text
/Volumes/V1/runtime
```

正常运行时：

```text
config.py
    ↓
RUNTIME_DIR
    ↓
run.sh
```

只有 config 模块无法加载时，才允许一个简单兼容 fallback。

---

# 16. Session

当前：

```text
SESSION_NAME = ~/tg_downloader
```

本次：

> **保持不动。**

不要把 Session 自动迁移到：

```text
/Volumes/V1/runtime
```

也不要修改 Session 存储逻辑。

---

# 17. Secrets

当前：

```text
TG_SECRETS_FILE
```

本次：

> **保持不动。**

不要把：

```text
tg_secrets.json
```

迁移到：

```text
/Volumes/V1/runtime
```

除非未来单独设计。

---

# 18. 旧数据迁移

这是本任务核心。

旧版本可能存在：

```text
旧下载根/runtime/
```

甚至还有历史版本散落在：

```text
旧下载根/
```

中的 Runtime 文件。

目标：

```text
/Volumes/V1/runtime/
```

媒体则迁移/归集到：

```text
/Volumes/V1/downloads/
```

---

# 19. 迁移原则

必须：

```text
先识别旧路径
    ↓
创建新目录
    ↓
迁移旧数据
    ↓
确认
    ↓
切换到新路径
```

禁止：

```text
先切换路径
↓
再尝试找旧数据
```

否则可能造成：

```text
旧数据孤岛
```

---

# 20. 迁移冲突策略

如果：

```text
旧/runtime/listen.json
```

与：

```text
/Volumes/V1/runtime/listen.json
```

同时存在：

默认：

```text
保留新目标文件
不覆盖
记录 WARNING
```

不要设计复杂自动合并。

同样适用于：

```text
tg_userbot.db
whitelist_config.json
thread_config.json
task_events.jsonl
download_history.txt
```

---

# 21. SQLite 迁移特别要求

对于：

```text
tg_userbot.db
```

必须特别小心。

如果旧 DB 存在：

```text
旧/runtime/tg_userbot.db
```

而目标不存在：

```text
/Volumes/V1/runtime/tg_userbot.db
```

可以迁移。

如果目标已经存在：

> **禁止覆盖。**

启动时应该明确提示：

```text
检测到旧 DB 与新 DB 同时存在
→ 使用哪个
→ 为什么
```

不要无提示地覆盖数据库。

---

# 22. Runtime 迁移不得删除旧目录

迁移完成后：

> 本任务不要自动删除旧数据目录。

原因：

- 防止误迁移；
- 方便回滚；
- 方便用户人工确认。

可以输出：

```text
✅ Runtime 数据迁移完成
旧目录仍保留，请确认无误后手动删除。
```

---

# 23. 全仓路径审计

### Phase 1 必须先做

在修改任何生产代码之前，搜索：

```text
SAVE_FOLDER
RUNTIME_DIR
DOWNLOAD_DIR
CHROME_DOWNLOAD_DIR
LOG_FILE
CHROME_AGENT_LOG_FILE
CD2_LAUNCH_LOG
TASK_EVENTS_FILE
DOWNLOAD_HISTORY_FILE
WHITELIST_FILE
LISTEN_CONFIG_FILE
THREAD_CONFIG_FILE
RUNTIME_DB_FILE
CHROME_TASKS_FILE
CHROME_REQUESTS_FILE
CHROME_CANCEL_REQUESTS_FILE
PID_FILE
```

以及：

```text
~/Downloads/Nagram
/storage/emulated/0/Download/Nagram
SAVE_FOLDER/runtime
os.path.join(
Path(
open(
mkdir(
makedirs(
```

---

# 24. Phase 1 必须分类

对每个命中的路径，列出：

```text
文件：
代码位置：
当前路径：
用途：
应该归属：
修改方案：
```

归属只能从以下选择：

```text
DOWNLOAD_DIR
RUNTIME_DIR
保持现状（Session / Secrets 等）
```

如果无法判断：

> 暂停实现，列出来让架构师审。

---

# 25. 禁止机械替换

绝对禁止：

```text
全局 SAVE_FOLDER → DOWNLOAD_DIR
```

因为当前代码中 `SAVE_FOLDER` 可能仍然用于媒体。

正确方法：

```text
逐个引用点判断用途
↓
媒体 → DOWNLOAD_DIR
Runtime → RUNTIME_DIR
```

---

# 26. 测试要求

新增/修改：

```text
tests/test_config.py
```

必须覆盖：

### 测试 1：默认路径

```text
DATA_ROOT=/Volumes/V1
DOWNLOAD_DIR=/Volumes/V1/downloads
RUNTIME_DIR=/Volumes/V1/runtime
```

### 测试 2：TG_DATA_ROOT

```text
TG_DATA_ROOT=/tmp/test-data
```

得到：

```text
/tmp/test-data/downloads
/tmp/test-data/runtime
```

### 测试 3：TG_DOWNLOAD_DIR

```text
TG_DOWNLOAD_DIR=/tmp/downloads
```

得到：

```text
DOWNLOAD_DIR=/tmp/downloads
```

### 测试 4：TG_RUNTIME_DIR

```text
TG_RUNTIME_DIR=/tmp/runtime
```

得到：

```text
RUNTIME_DIR=/tmp/runtime
```

### 测试 5：两个目录完全独立

```text
TG_DOWNLOAD_DIR=/tmp/d
TG_RUNTIME_DIR=/tmp/r
```

不得互相影响。

### 测试 6：旧 TG_SAVE_FOLDER 兼容

```text
TG_SAVE_FOLDER=/tmp/legacy
```

得到：

```text
DOWNLOAD_DIR=/tmp/legacy
SAVE_FOLDER=/tmp/legacy
```

### 测试 7：新 Download 配置优先级

同时：

```text
TG_DOWNLOAD_DIR=/tmp/new
TG_SAVE_FOLDER=/tmp/legacy
```

必须：

```text
DOWNLOAD_DIR=/tmp/new
```

### 测试 8：Runtime DB 单文件覆盖

```text
TG_RUNTIME_DB=/tmp/db/tg.db
```

必须：

```text
RUNTIME_DB_FILE=/tmp/db/tg.db
```

### 测试 9：Chrome 路径

验证：

```text
Chrome 下载 → DOWNLOAD_DIR
Chrome Runtime → RUNTIME_DIR
```

### 测试 10：旧 Runtime 迁移

验证：

```text
old/runtime/*
    ↓
/Volumes/V1/runtime/*
```

### 测试 11：目标存在时不覆盖

验证：

```text
新文件保持不变
```

---

# 27. 回归测试

完成后必须运行：

```bash
pytest -q
```

并运行项目当前已有的 unittest 专项测试命令。

至少确认：

```text
普通 Telegram 下载
白名单
Tag Listener
评论跟进
SQLite
Chrome
Reporter
Cleanup
CD2
Caption Filter
命名
```

全部正常。

---

# 28. 本任务明确不改的东西

禁止：

- 重写下载队列
- 重写 Worker
- 重写 SQLite
- 重写 Tag Listener
- 修改标签监听业务规则
- 修改白名单业务规则
- 修改评论跟进逻辑
- 修改 Caption 命名规则
- 修改 Caption Filter
- 修改 Chrome Task 状态机
- 修改 Reporter 逻辑
- 接入 Pawchive
- 修改 Session
- 修改 Secrets
- 为 Android 做额外改造
- 顺手修复无关问题

---

# 29. 实施顺序

必须：

```text
Phase 1
全仓路径审计
        ↓
Phase 2
输出路径归属表
        ↓
Phase 3
确认 /Volumes/V1 数据结构
        ↓
Phase 4
实现 DATA_ROOT / DOWNLOAD_DIR / RUNTIME_DIR
        ↓
Phase 5
实现旧数据迁移
        ↓
Phase 6
替换 Runtime 路径引用
        ↓
Phase 7
检查 Chrome / CD2 / Cleanup / run.sh
        ↓
Phase 8
专项测试
        ↓
Phase 9
完整回归
        ↓
Phase 10
提交
```

---

# 30. Phase 1 强制汇报格式

DeepSeek 在修改生产代码前必须先输出：

```text
=== Userbot 路径审计 ===

当前下载根：
...

当前 Runtime 根：
...

目标数据根：
/Volumes/V1

发现的媒体路径：
...

发现的 Runtime 文件：
...

发现的硬编码路径：
...

发现的 SAVE_FOLDER Runtime 用法：
...

需要修改的文件：
...

保持不变的文件：
...

迁移风险：
...

最终路径：
DATA_ROOT    = /Volumes/V1
DOWNLOAD_DIR = /Volumes/V1/downloads
RUNTIME_DIR  = /Volumes/V1/runtime
```

---

# 31. 完成后必须自查

最终仓库不应该存在业务代码：

```text
SAVE_FOLDER/runtime
```

不应该新增：

```text
~/Downloads/Nagram/runtime
```

不应该出现：

```text
Runtime 文件 → DOWNLOAD_DIR
媒体文件 → RUNTIME_DIR
```

不应该出现模块自行猜路径：

```text
os.path.join(SAVE_FOLDER, "runtime", ...)
```

正式逻辑统一：

```text
媒体 → config.DOWNLOAD_DIR
Runtime → config.RUNTIME_DIR
```

---

# 32. 架构师最终审计重点

完成后重点看：

### 1. 是否真正解耦

不能只是：

```text
SAVE_FOLDER 改名成 DOWNLOAD_DIR
```

然后 Runtime 仍偷偷依赖 Download。

必须真正形成：

```text
/Volumes/V1/downloads
/Volumes/V1/runtime
```

### 2. 是否有隐藏旧路径

重点检查：

```text
run.sh
chrome_client.py
chrome_agent.py
cleanup.py
cd2.py
log.py
reporter.py
stats.py
queue.py
```

### 3. 是否破坏旧数据

特别检查：

```text
tg_userbot.db
listen.json
whitelist_config.json
thread_config.json
task_events.jsonl
download_history.txt
```

### 4. 是否破坏 Chrome

```text
Chrome 媒体
→ downloads

Chrome 状态/日志
→ runtime
```

### 5. 是否破坏 CD2

```text
CD2
→ DOWNLOAD_DIR
```

不能把 Runtime 一起同步。

---

# 33. Commit 要求

建议：

```text
功能：Userbot 数据根目录迁移与路径解耦
```

提交前必须：

```bash
pytest -q
```

并汇报实际输出。

---

# 34. 最终原则

> **`/Volumes/V1` 是 Userbot 的正式数据根目录；`/Volumes/V1/downloads` 只存用户媒体，`/Volumes/V1/runtime` 只存程序运行数据。所有模块统一从 `config.py` 获取路径；先做全仓审计，再迁移，再切换，最后完整回归。**
