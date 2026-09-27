# TG Userbot UX Round 1 开发任务书

> 目标：在不破坏现有下载、Pawchive、标签监听、Chrome 任务等核心功能的前提下，做一轮“控制 Bot 用户体验优化”。
>
> 核心原则：**最小改动、以当前代码为准、不要重构后端核心架构。**

## 一、开发准则

1. 一切以当前仓库代码为准。先阅读当前分支相关代码，不根据旧版本、记忆或猜测开发。
2. 不虚构不存在的函数、字段、状态、接口或统计数据。
3. 最小改动。优先修改 `text.py`、`bot.py`、菜单/命令相关模块。
4. 不要重写 SQLite、Queue、Listener、Dedup、Lease、Pawchive 下载状态机。
5. 保持现有普通下载、Saved Messages、Forward→下载、Retry、Dedup、Listener、Whitelist、Pawchive、Chrome、Caption、Cookie、SQL/Shell/Upload、自动清理及现有命令。
6. 不要因为 UX 改造改变业务语义。
7. 如任务书与当前代码不一致，以当前代码为准，并在最终报告中说明。

---

# 二、P0-1：主菜单增加真正的全局总览

## 当前问题

主菜单目前偏重普通下载；Pawchive、标签监听、Chrome 等任务可能正在工作，但主菜单看起来仍然没有任务。

## 目标

主菜单顶部提供系统级概览，例如：

```text
🤖 TG Userbot

🟢 系统运行正常

📥 普通下载
  处理中：2
  待处理：4
  待重试：1

🐾 Pawchive
  处理中：3
  待处理：5

📡 标签监听
  状态：正常
  待处理：2

🌐 Chrome
  运行：1
  待处理：2

📊 今日完成：38
💾 今日下载：12.4 GB
```

实际字段必须根据当前代码可靠可获取的数据决定，不能照抄示例后虚构数字。

### 实现要求

- 优先复用现有 Queue、SQLite、Pawchive、Listener、Chrome 统计函数。
- 不要为每个模块重新建立统计系统。
- 某模块没有可靠实时统计时，只显示已有可靠状态，不要猜数字。
- 总览必须避免“普通下载为 0，但其他模块正在工作时整体看起来像没任务”。

---

# 三、P0-2：修复 `/status` 健康状态显示

当前 `text.py` 的 `status_text()` 存在固定绿色标题与实际断开状态矛盾的问题，例如：

```text
🟢 TG Userbot 状态正常

连接：断开
```

## 要求

根据当前实际连接状态动态显示：

正常：

```text
🟢 TG Userbot 运行正常
连接：正常
```

断开：

```text
🔴 TG Userbot 连接异常
连接：断开
```

如果当前代码确实能可靠判断“正在重连”，可以使用黄色状态。

重点：先确认 `connected`、Telegram client 状态、reconnect 状态的真实来源，不要只改文字。

---

# 四、P0-3：统一 Input Window 用户体验

当前存在多个“下一条普通消息作为输入”的模式：

- Cookie
- Find
- Caption
- Listen
- Whitelist since
- SQL template
- Shell
- Upload
- Pawchive
- CMD template

用户忘记当前模式时，普通消息可能被错误消费。

## 目标

保留现有输入逻辑，但统一提示和取消机制。

### A. 进入输入状态时明确提示

例如：

```text
🍪 设置抖音 Cookie

请发送下一条消息作为 Cookie。

当前操作：等待 Cookie
⏱ 120 秒后自动取消

[❌ 取消]
```

或：

```text
🔍 文件查询

请发送下一条消息作为搜索关键词。

当前操作：等待搜索条件

[❌ 取消]
```

### B. 所有主要 input window 提供取消按钮

点击：

```text
[❌ 取消]
```

必须：

1. 清除对应 pending/input 状态
2. 更新或删除当前提示
3. 返回主菜单
4. 不影响其他后台任务

### C. Cancel 必须真正清理状态

重点检查：

```text
cookie
find
caption
listen
wl since
sqlt
shell
upload
paw
cmdt
```

### D. 成功输入后清理等待状态

流程必须是：

```text
进入 input mode
→ 等待输入
→ 输入成功
→ 处理完成
→ input mode 清除
→ 下一条普通消息正常处理
```

### E. Timeout

如果当前已经使用 120 秒 timeout，原则上保持，不要随意修改。

timeout 后必须：

- 清除 pending 状态
- 用户知道已取消
- 不影响后台任务

### F. 统一实现方式

可以使用类似：

```text
input_cancel:<mode>
```

但只是建议。

不要为了统一而建立复杂框架；优先复用现有 callback handler 和状态结构。

### G. 输入模式互斥

检查多个 input mode 是否可能同时存在。

如果当前已经保证互斥，保持现状。

只有发现确实可能出现两个等待状态同时存在并导致消息误消费时，才做最小修复。

---

# 五、普通文本默认行为

检查当前逻辑。

在不影响功能的情况下：

- `/start`：主菜单
- `/help`：帮助
- 按钮：正常处理
- URL：继续识别
- Forward：继续识别
- Input Window：继续接收输入
- 已注册命令：继续执行

对于普通的、没有业务意义的文本，例如：

```text
你好
```

可以考虑不响应，或者只给一个简短提示。

以当前项目交互习惯选择，不要让这个修改破坏 URL、Forward、Input Window 或已有命令。

---

# 六、P1-1：重新整理主菜单信息架构

不要删除功能，只重新组织入口。

建议：

## ⭐ 核心

```text
📊 总览
📈 实时进度
📥 下载中心
🔍 找文件
```

## 🤖 自动化

```text
🐾 Pawchive
📡 标签监听
🌐 Chrome
📋 白名单
```

## ⚙️ 设置

```text
🧵 并发
🛡 去重
🍪 抖音 Cookie
✏️ Caption 清洗
🧹 自动清理
```

## 🛠 工具

```text
📊 台账
📐 SQL 模板
🖥 命令行
📤 上传
```

实际名称、callback_data、布局以当前代码为准。

禁止：

- 因菜单重排修改业务逻辑
- 删除原命令
- 删除原功能
- 让旧按钮失效

如果必须调整 callback_data，应考虑兼容旧 callback。

---

# 七、P1-2：提高 `/find` 的入口优先级

`/find` 是典型的核心用户场景：

> “以前下载过这个文件，现在在哪里？”

建议放到一级菜单核心区域：

```text
🔍 找文件
```

不要让用户必须进入工具菜单才能找到。

---

# 八、P1-3：优化错误提示

当前部分错误可能直接显示：

```text
HTTP 500
TimeoutError
ConnectionError
```

这些技术信息可以保留，但用户首先应该知道：

1. 什么失败了
2. 原因是什么
3. 下一步怎么办

推荐：

```text
❌ 下载失败

文件：example.mp4

原因：
HTTP 500

建议：
可以稍后重试。

[🔁 重试] [🔎 查看]
```

如果当前无法提供按钮：

```text
❌ 下载失败

文件：example.mp4

原因：HTTP 500

可使用 /retry 重新处理。
```

日志和 traceback 不要丢。

---

# 九、P1-4：统一“进度”概念

当前可能存在：

- 普通下载 progress
- Pawchive progress
- Chrome progress
- Listener task

检查 `📈 进度` / `/progress` 是否会让用户误以为它代表所有任务。

如果可以低成本实现，可以显示：

```text
📈 实时进度

📥 普通下载
...

🐾 Pawchive
...

🌐 Chrome
...

📡 标签监听
...
```

如果各模块没有统一可靠实时数据：

- 不要重写后台架构
- 保留现有 `/progress`
- 明确它的统计范围

---

# 十、Pawchive 本轮只做 UX

当前 Pawchive 已有：

- status
- scan
- manual
- retry
- archive
- cookie
- CSV
- progress

并且当前代码已经处理了：

> 已确认死链不会被 `/paw retry` 无限重试。

本轮不要重新修改 Pawchive retry / dead-link / 下载状态机。

可以低成本改善扫描进度，例如：

```text
🐾 Pawchive

阶段：获取帖子

作者：xxx
进度：42 / 100

✅ 作者解析
🔄 获取帖子
⬜ 建立下载任务
⬜ 开始下载
```

但不要：

- 改数据库状态定义
- 改 retry 语义
- 改 dead-link 处理
- 改下载 worker
- 改 Chrome Agent

除非确实为 UX 必须，并先说明原因。

---

# 十一、Chrome 任务保持稳定 task_id

当前 Chrome 使用稳定 `task_id`。

本轮不要：

- 改成 list index
- 重新设计任务 ID
- 因菜单重排修改任务 ID

菜单文字可以优化，但 task_id 必须保持稳定。

---

# 十二、验收测试

## 1. Status

Telegram 正常：

```text
🟢 ...
连接：正常
```

Telegram 断开：

```text
🔴 ...
连接：断开
```

禁止出现：

```text
🟢 状态正常
连接：断开
```

## 2. Input Window

至少完整测试：

- Cookie
- Find
- Caption
- Listen

验证：

```text
进入 input mode
→ 显示当前操作
→ 有取消按钮
→ Cancel 后状态清除
→ 下一条普通消息不会被错误消费
```

## 3. Timeout

```text
进入 input mode
→ timeout
→ 状态清除
→ 后续普通消息不会被消费
```

## 4. 输入成功

```text
进入 input mode
→ 发送合法输入
→ 成功处理
→ 状态清除
→ 下一条普通消息正常处理
```

## 5. Global Summary

验证：

- 普通下载有任务时显示
- Pawchive 有任务时显示
- Listener 有任务时显示
- Chrome 有任务时显示（如果当前代码可可靠统计）
- 无任务时不产生虚假数字

## 6. 原有命令回归

至少：

```text
/start
/help
/status
/progress
/queue
/retry
/find
/paw
```

以及主要按钮。

## 7. 普通下载回归

至少完成一次：

```text
普通文件
→ Queue
→ 下载
→ 完成
```

确保 UX 修改没有影响下载。

---

# 十三、代码质量

1. 不复制大量代码，优先复用已有状态格式化函数。
2. 不把复杂业务逻辑塞进 Telegram handler。
3. 注意 `bot.py`、`commands.py`、`text.py`、`pawchive.py`、`app.py` 现有依赖，避免循环依赖。
4. UI 刷新不要制造大量 INFO 日志。
5. 不要留下 debug 开关、临时文件、测试代码。
6. 不要为了本轮任务修改无关代码。

---

# 十四、Git 修改纪律

开发前执行：

```bash
git status
git branch --show-current
git log -1 --oneline
```

开发后必须检查：

```bash
git diff --stat
git diff
```

确认没有：

- 无关文件
- 大规模格式化
- 调试代码
- 临时文件
- API Key
- Cookie
- Token
- 本地敏感路径

建议独立 commit，例如：

```text
fix: make status health indicator reflect connection state
feat: improve input window UX
feat: add global task overview
refactor: reorganize bot main menu
```

---

# 十五、最终交付报告

完成后必须报告：

## 1. 实际修改文件

列出每个文件和具体修改内容。

## 2. 修改原因

按：

```text
问题 → 修改 → 影响
```

说明。

## 3. 明确未修改内容

实际未修改的才可以写，例如：

```text
SQLite：未修改
Queue：未修改
Listener 核心状态机：未修改
Dedup：未修改
Lease：未修改
Pawchive 下载状态机：未修改
Chrome task_id：未修改
```

## 4. 测试结果

只报告实际执行过的测试，不允许虚构。

## 5. Git diff 摘要

说明：

- 修改行数
- 修改文件
- 是否存在无关变更
- 是否有兼容性风险

---

# 十六、最终验收标准

## P0

- [ ] 主菜单能体现系统级任务状态
- [ ] `/status` 不再出现“绿色正常 + 连接断开”
- [ ] 主要 input window 有明确“当前操作”
- [ ] 主要 input window 有 Cancel
- [ ] Cancel 真正清理 pending 状态
- [ ] timeout 后不会错误消费下一条消息

## P1

- [ ] 主菜单按用户任务模型重新组织
- [ ] `/find` 位于核心入口
- [ ] 错误信息包含下一步处理建议
- [ ] `/progress` 范围不会误导用户
- [ ] Pawchive / Chrome 核心逻辑未被重写

## 回归

- [ ] 普通下载正常
- [ ] Queue 正常
- [ ] Retry 正常
- [ ] Listener 正常
- [ ] Pawchive 正常
- [ ] Chrome task 正常
- [ ] Saved Messages cleanup 正常
- [ ] 原有命令正常

---

# 十七、最重要的边界

本轮核心目标：

> **让 TG Userbot 更容易用，而不是把整个项目重新架构。**

严格遵守：

```text
UX 优化 > UI 重构
最小改动 > 大规模抽象
复用现有状态 > 新建状态系统
实际代码 > 猜测
真实数据 > 虚构统计
保持兼容 > 追求“漂亮架构”
```

如果某项实现需要明显修改核心架构：

**先停下来，在最终报告中说明为什么需要改，不要擅自扩大改动范围。**
