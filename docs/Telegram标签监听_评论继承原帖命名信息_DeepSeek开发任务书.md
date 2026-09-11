# Telegram 标签监听：讨论组评论继承频道原帖命名信息
## DeepSeek 开发任务书

> 仓库：`Hoiyook/tg_userbot`  
> 分支：`feature/tag-listener`  
> 基线提交：`2ec171aa863b830e7d00dcf144b3bd109de3a5aa`

## 1. 目标

当讨论组 B 的消息明确属于频道 A 某条原帖的评论/回复时，B 的**下载文件命名**继承 A 的：

- caption/标题文本
- 消息日期

A、B 仍然是**两个独立下载任务**。不要修改 Telegram 原消息、转发内容或现有 Producer/Consumer、SQLite、checkpoint、下载队列架构。

## 2. 命名规则

### A 原帖
完全保持现有命名逻辑：

- caption：A 自己的 caption
- 日期：A 自己的消息日期
- 用户通过现有转发输入框填写的自定义标注：继续按原逻辑生效

### B 普通消息
如果 B 不是 A 的评论：

- caption：B 自己的 caption/现有 fallback
- 日期：B 自己的日期

### B → A 评论
必须通过 Telegram **真实 reply/thread/linked discussion 关系**确认 B 属于 A，禁止按时间、最近消息、caption 相似度或消息 ID 猜测。

确认后：

- caption：继承 A
- 日期：继承 A
- A/B 仍是独立下载任务
- 只影响文件命名输入，不修改消息

## 3. 自定义标注：最高优先级

现有流程：

`转发/下载 → 弹出输入框 → 用户输入自定义标注 → 文件命名`

这个功能必须完整保留，不能删除、绕过或改变交互。

最终规则：

```text
日期：
B 是 A 评论 → A 日期
否则       → 当前消息日期

命名文本：
有自定义标注 → 自定义标注
否则：
    B 是 A 评论 → A caption
    否则       → 当前消息 caption / 原有 fallback
```

例如：

```text
A caption = "Movie Name"
B 回复 A
用户输入自定义标注 = "我的备注"
```

最终必须使用：

```text
我的备注
```

而不是：

```text
Movie Name
```

但日期仍使用 A 的日期。

## 4. Album

### A 是 album
使用现有 `pick_group_caption_text` 等 album caption 逻辑；B 评论继承 A album caption 和 A 日期。

### B 是 album
保持现有 album grouping、命名、去重逻辑；B album 内文件继承 A caption/date。

## 5. 实现原则

先阅读并理解：

- `tg_userbot/listener.py`
- `tg_userbot/app.py`
- `tg_userbot/naming.py`
- `tg_userbot/queue.py`
- Telegram reply/thread/讨论组相关代码
- 现有测试

优先复用现有：

```python
compute_final_filename(...)
get_caption(...)
pick_group_caption_text(...)
```

不要重新设计文件命名系统。

推荐数据流：

```text
发现 B
 ↓
确认 B → A
 ↓
读取 A caption/date
 ↓
形成命名上下文
 ↓
入下载任务
 ↓
现有命名流程
```

命名信息应尽量在**入队时形成快照**，不要让下载 worker 很久以后再次查询 A，否则 A 编辑/删除会导致 retry 时文件名变化。

## 6. 父消息查询失败

如果 A 无法获取或无法确认：

- B 不能丢失
- Producer 不能因此崩溃
- fallback 到 B 原有命名逻辑
- 日期无法继承时也使用现有 B 日期逻辑

日志应能区分成功继承、父消息不存在、访问失败、无法确认关系，但避免刷屏。

## 7. 数据库

先检查现有任务记录是否已有可复用字段。

不要无条件新增数据库字段。

若确实需要 schema 变化：

- 做兼容 migration
- 老任务可正常恢复
- 不删除已有字段
- 不改变 task state
- 不改变 Producer/Consumer 架构

## 8. TDD：必须先测试后代码

严格执行：

`RED → GREEN → REFACTOR`

必须覆盖：

1. B 普通消息：B caption + B date
2. B 回复 A：A caption
3. B 回复 A：A date
4. B 回复 A + 自定义标注：自定义标注覆盖 A caption
5. B 回复 A + 自定义标注：文本用自定义标注、日期仍用 A date
6. A 无 caption：不崩溃，走原有 fallback，日期仍可继承 A
7. 父消息查询失败：B 不丢失，fallback
8. A album：继承 album caption/date
9. B album：保持 grouping，同时继承 A caption/date
10. **完整回归现有“转发 → 输入框 → 自定义标注 → 最终文件名”链路**

特别注意：不能只测试一个新 helper 就声称自定义标注没有回归。

## 9. 测试

执行定向新增测试，并最终执行：

```bash
pytest -q
```

失败时保留测试，分析原因后做最小修改。

## 10. 修改范围

只做本功能需要的最小修改。

优先涉及：

- listener / app enqueue 路径
- naming
- 对应测试

禁止顺手：

- 重写 listener
- 重写 queue/downloader
- 重写 SQLite
- 重写 Producer/Consumer
- 修改 checkpoint
- 修改 whitelist/listen.json
- 修改 Android/Termux 逻辑
- 清理无关代码

## 11. 性能要求

重点检查大量评论时是否会产生大量 parent-message API 请求。

优先：

- 使用消息对象已有信息
- 同一轮扫描复用已获取的父消息
- 必要时做轻量缓存

避免因为每条评论都重复查询 A 而造成 FloodWait。

不要为此引入复杂的新架构。

## 12. 验收标准

必须全部满足：

- [ ] B 评论正确识别对应 A
- [ ] B 继承 A caption
- [ ] B 继承 A 日期
- [ ] A/B 仍为独立任务
- [ ] album 正常
- [ ] 父消息失败时 B 不丢失
- [ ] retry 命名稳定
- [ ] 原有自定义标注输入框仍存在
- [ ] 自定义标注仍进入最终文件名
- [ ] 自定义标注优先于 inherited caption
- [ ] 自定义标注 + A 日期正确
- [ ] 普通消息命名不变
- [ ] checkpoint 不变
- [ ] SQLite Producer/Consumer 不变
- [ ] 下载队列不变
- [ ] `pytest -q` 全部通过

## 13. 最终汇报

DeepSeek 完成后必须说明：

1. 修改了哪些文件
2. 每个文件修改内容
3. 如何确认 B → A
4. caption/date 如何继承
5. 自定义标注如何保持最高优先级
6. album 如何处理
7. 父消息失败如何 fallback
8. 是否增加 API 请求，如何避免请求风暴
9. 新增/修改了哪些测试
10. `pytest -q` 完整结果
11. 是否存在未解决问题

建议 commit：

```text
功能：讨论组评论继承频道原帖命名信息
```

## 14. 架构师审计重点

完成后重点审计三件事：

### A. 自定义标注是否真的保住
检查完整链路：

`输入框 → custom label → enqueue → naming → compute_final_filename → 最终文件名`

### B. 日期是否真的来自 A
不能出现“caption 来自 A、日期仍来自 B”的半实现。

### C. 是否产生 API 请求风暴
大量 B 评论不能导致每条都重复查询 A。

## 核心原则

> 这是“命名上下文继承”功能，不是下载架构改造。
>
> B 如果明确属于 A，就继承 A 的 caption 和日期；但用户原来在转发输入框填写的自定义标注必须完整保留，并拥有最高文本优先级。
