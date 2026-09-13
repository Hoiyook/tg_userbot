# Telegram 标签监听功能 —— DeepSeek 开发任务书（修正版）

> **项目**：`Hoiyook/tg_userbot`  
> **目标分支**：`feature/multi-worker-v2.10`  
> **功能版本建议**：V1  
> **重要修正**：本版本明确区分“下载白名单”和“标签监听白名单”，两者完全独立。

---

## 一、需求背景

现有 UserBot 已经存在一套“下载白名单”机制：

- `state.WHITELIST_CHATS`
- `/wl` 命令
- 白名单聊天收到媒体后，自动转发到 Telegram「收藏夹」（Saved Messages）
- 收藏夹中的媒体会进入现有下载流程
- 现有下载队列、去重、命名、重试等机制继续复用

现在新增一个完全独立的功能：

> UserBot 按固定周期主动扫描指定聊天，只检查配置的标签；如果发现包含指定标签的新消息，就按照规则转发到指定目标，并可选择触发下载。

**关键点：标签监听功能不能把现有下载白名单当成监听来源。**

---

# 二、最重要的架构要求

必须存在两套互相独立的配置。

## 2.1 现有“下载白名单”

这是原有功能，不修改其语义：

```text
state.WHITELIST_CHATS
        │
        ▼
收到聊天实时消息
        │
        ▼
自动转发收藏夹
        │
        ▼
现有下载流程
```

它回答的是：

> “哪些聊天收到媒体后，需要自动下载？”

---

## 2.2 新增“标签监听白名单”

新功能独立维护监听规则：

```text
runtime/listen.json
        │
        ▼
标签监听规则
        │
        ├── source chat
        ├── tag
        ├── targets
        └── download
```

它回答的是：

> “哪些聊天需要被主动扫描？扫描什么标签？匹配后发到哪里？”

### 必须满足

监听来源：

- **不能从 `state.WHITELIST_CHATS` 推导**
- **不能调用 `/wl` 白名单作为监听来源**
- **不能要求监听聊天必须加入下载白名单**
- 一个聊天可以：
  - 只在下载白名单
  - 只在标签监听白名单
  - 同时存在于两者
  - 两者都不存在

也就是说：

```text
下载白名单 ≠ 标签监听白名单
```

这是本任务最重要的设计约束。

---

# 三、使用场景

例如：

```json
{
  "enabled": true,
  "interval_minutes": 1440,
  "listeners": [
    {
      "chat": "@source_channel",
      "tag": "#01musume",
      "targets": [
        "me",
        "@speedlearnnn"
      ],
      "download": true
    }
  ]
}
```

含义：

> 每 24 小时主动扫描 `@source_channel`，寻找新的 `#01musume` 消息。

匹配到后：

1. 转发到「收藏夹」
2. 转发到 `@speedlearnnn`
3. 触发下载

这里的 `@source_channel` **不需要加入 `/wl` 下载白名单**。

---

# 四、配置文件

## 4.1 `runtime/listen.json`

建议格式：

```json
{
  "enabled": true,
  "interval_minutes": 1440,
  "listeners": [
    {
      "chat": "@source_channel",
      "tag": "#01musume",
      "targets": [
        "me",
        "@speedlearnnn"
      ],
      "download": true
    }
  ]
}
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `enabled` | boolean | 是否启用整个标签监听 |
| `interval_minutes` | integer | 扫描周期，单位分钟 |
| `listeners` | array | 监听规则列表 |
| `chat` | string/int | 监听来源聊天，可使用 `@username` 或 chat ID |
| `tag` | string | 要匹配的标签 |
| `targets` | array | 匹配后转发目标 |
| `download` | boolean | 是否触发下载 |

---

# 五、监听规则的核心逻辑

每一条规则至少包含：

```text
来源聊天
+
标签
+
目标
+
下载开关
```

例如：

```text
@source_channel
#01musume
↓
收藏夹
↓
@speedlearnnn
↓
下载
```

另一个规则可以是：

```text
@another_channel
#xxx
↓
@speedlearnnn
```

两者互不影响。

---

# 六、扫描机制

建议新增独立模块：

```text
tg_userbot/listener.py
```

负责：

1. 加载 `listen.json`
2. 保存监听状态
3. 定时扫描
4. 解析来源聊天
5. 获取新消息
6. 判断标签
7. 执行转发
8. 触发下载
9. 保存 checkpoint
10. 错误处理和日志

---

# 七、必须有独立的扫描状态

建议新增：

```text
runtime/listen_state.json
```

例如：

```json
{
  "-1001234567890": {
    "last_message_id": 12345
  }
}
```

原因：

Telegram 的 message ID 是**聊天内唯一**的，因此 checkpoint 必须按来源聊天保存。

---

# 八、首次启用时禁止扫完整历史

这是非常重要的防护。

新增加一个监听聊天时：

> 默认不要把该聊天历史上所有符合标签的消息全部转发/下载。

首次初始化应该：

1. 获取当前聊天最新 message ID
2. 将其作为 checkpoint
3. 从下一条新消息开始监听

例如：

```text
当前最新消息 ID = 50000

初始化：
last_message_id = 50000

下一轮：
只检查 > 50000 的消息
```

避免第一次启动就把大量历史文件全部转发、下载。

后续如果需要“历史扫描”，可以作为单独功能增加：

```text
▶️ 扫描历史
```

V1 不要求实现。

---

# 九、扫描时不要重复读取同一个聊天

如果配置：

```json
{
  "listeners": [
    {
      "chat": "@source",
      "tag": "#01musume"
    },
    {
      "chat": "@source",
      "tag": "#02musume"
    }
  ]
}
```

不要：

```text
扫描 @source #01musume
扫描 @source #02musume
```

这样会重复请求 Telegram。

应该：

```text
扫描 @source 一次
       │
       ├── 判断 #01musume
       │
       └── 判断 #02musume
```

即：

> **按 chat 扫描一次，再匹配该 chat 下的全部监听规则。**

---

# 十、消息多标签命中与转发去重

这是必须实现的核心去重规则。

假设同一个来源聊天配置了：

```json
{
  "chat": "@source",
  "tag": "#MMD",
  "targets": ["me", "@speedlearnnn"]
}
```

以及：

```json
{
  "chat": "@source",
  "tag": "#01musume",
  "targets": ["me", "@speedlearnnn"]
}
```

而某条消息同时包含：

```text
这是一个视频 #MMD #01musume
```

那么这条消息会同时命中两个监听规则，但：

> **不能因为命中两个标签而执行两次转发。**

正确处理方式：

```text
message_id = 12345

命中标签：
  #MMD
  #01musume

合并目标：
  me
  @speedlearnnn
```

最终：

```text
12345
 ├── 收藏夹         × 1
 └── @speedlearnnn  × 1
```

而不是：

```text
#MMD
 ├── 收藏夹
 └── @speedlearnnn

#01musume
 ├── 收藏夹
 └── @speedlearnnn
```

## 10.1 推荐内部数据结构

同一个聊天扫描完成后，先进行“匹配 → 合并”，再执行转发/下载。

概念上：

```python
matched = {
    message_id: {
        "tags": {"#MMD", "#01musume"},
        "targets": {"me", "@speedlearnnn"},
        "download": True
    }
}
```

然后对：

```text
(message_id, target)
```

进行唯一化。

## 10.2 必须满足的去重规则

### 规则 1：同一消息 + 同一目标，只转发一次

```text
(message_id=12345, target=me)
```

只能执行一次。

```text
(message_id=12345, target=@speedlearnnn)
```

只能执行一次。

### 规则 2：同一消息命中多个标签，不增加转发次数

```text
#MMD
#01musume
#xxx
```

即使同时命中 3 个标签：

```text
收藏夹：最多 1 次
@speedlearnnn：最多 1 次
```

### 规则 3：下载也必须去重

同一消息即使：

- 命中多个标签
- 同时存在于下载白名单
- 同时被标签监听触发

也不能产生重复下载。

下载必须继续复用项目现有 dedup 机制。

## 10.3 跨扫描周期去重

`listen_state.json` 中的 `last_message_id` 用于防止下一轮扫描重新处理已经检查过的消息。

如果某条消息处理成功：

```text
message_id = 12345
```

下一轮不应再次处理。

如果转发或下载失败，则不能简单地把 checkpoint 推过该消息导致永久丢失；应根据实际处理结果决定是否推进 checkpoint，或者保留失败消息供下一轮重试。

# 十、标签匹配

V1 建议采用简单、稳定的标签匹配。

例如：

```text
#01musume
```

应当匹配：

```text
这是 #01musume 的新视频
```

但不要简单使用：

```python
if tag in text:
```

避免出现明显误匹配。

建议至少考虑：

- 标签前后边界
- 大小写策略
- 文本为空
- caption 与 message text

如果用户配置：

```text
#01musume
```

则核心目标是判断消息文本中是否存在这个完整标签。

---

# 十一、转发目标

V1 至少支持：

```text
me
```

表示：

> Telegram Saved Messages / 收藏夹

以及：

```text
@speedlearnnn
```

表示指定 Telegram 聊天。

例如：

```json
"targets": [
  "me",
  "@speedlearnnn"
]
```

---

# 十二、收藏夹与下载的关系

当前项目已经把 Saved Messages 当作一个有效的下载入口。

因此：

```text
监听匹配
   │
   ▼
转发到 me
   │
   ▼
进入现有 Saved Messages 消息处理
   │
   ▼
现有下载队列
```

因此 V1 中：

> `me` 目标应视为“转发到收藏夹，并沿用现有收藏夹下载机制”。

**不要为了这个新功能重新实现一套下载器。**

必须复用现有：

- `enqueue_media`
- 下载队列
- dedup
- 命名逻辑
- retry
- 现有媒体处理流程

---

# 十三、如果 `download=true` 但没有 `me`

例如：

```json
{
  "chat": "@source",
  "tag": "#01musume",
  "targets": [
    "@speedlearnnn"
  ],
  "download": true
}
```

这种情况下可以直接调用现有下载入队能力，而不是强制再转发到收藏夹。

但必须：

- 复用现有下载队列
- 复用现有 dedup
- 复用现有命名
- 不复制下载逻辑
- 不创建第二套下载系统

---

# 十四、与现有下载白名单同时存在时的去重

这是另一个关键点。

假设：

```text
@source
```

同时存在于：

```text
下载白名单
```

和：

```text
标签监听白名单
```

某条消息：

```text
#01musume
```

实时下载流程可能已经：

```text
@source
  ↓
收藏夹
  ↓
下载
```

标签监听又可能：

```text
@source
  ↓
收藏夹
  ↓
下载
```

因此必须避免重复处理。

---

## 推荐策略

标签监听负责的是：

> **标签路由**

现有下载白名单负责的是：

> **实时媒体下载**

两者不要互相调用。

对于已经进入现有下载白名单实时流程的消息：

- 不要再次执行 `me` 转发
- 不要重复下载
- 如果监听规则还有 `@speedlearnnn`，仍然执行该目标转发

最终效果：

```text
消息
 │
 ├── 下载白名单 → 收藏夹 → 下载
 │
 └── 标签监听 → @speedlearnnn
```

而不是：

```text
消息
 │
 ├── 下载一次
 ├── 再下载一次
 └── 再转发一次收藏夹
```

---

# 十五、建议的监听处理模型

推荐：

```text
Telegram
   │
   ├───────────────┐
   │               │
   ▼               ▼
实时消息           定时扫描
   │               │
   ▼               ▼
下载白名单       标签监听白名单
   │               │
   ▼               ▼
收藏夹/下载       标签匹配
                   │
          ┌────────┴────────┐
          ▼                 ▼
       转发目标           下载
```

两个入口共享：

```text
dedup
download queue
naming
relay helpers
```

但**不共享白名单定义**。

---

# 十六、后台任务

在 `app.py` 中增加独立 listener background task。

例如：

```text
listener_loop()
```

逻辑：

```python
while running:
    load listen.json

    if enabled:
        scan_all_listener_chats()

    sleep(interval_minutes * 60)
```

要求：

- 不阻塞主消息处理
- 不影响下载 worker
- 不影响 Chrome worker
- 不影响 reporter
- 异常不能导致 UserBot 主进程退出

---

# 十七、配置动态生效

建议每轮扫描时重新读取：

```text
runtime/listen.json
```

或者提供安全的 reload 机制。

这样通过控制 Bot 修改监听规则后：

> 不需要重启 UserBot。

---

# 十八、控制 Bot 菜单

不要把它塞进现有：

```text
📋 白名单
```

而应该增加一个独立入口：

```text
📡 标签监听
```

原因：

```text
📋 白名单
    ↓
下载白名单

📡 标签监听
    ↓
标签监听白名单
```

用户必须能够明显区分两套系统。

---

# 十九、标签监听菜单建议

```text
📡 标签监听

状态：🟢 开启
扫描周期：24 小时

监听规则：

1️⃣ @source_channel
   🏷 #01musume
   📌 收藏夹
   📢 @speedlearnnn
   ⬇️ 自动下载：开启

2️⃣ @another_channel
   🏷 #xxx
   📢 @speedlearnnn
   ⬇️ 自动下载：关闭

[➕ 添加监听]
[✏️ 修改监听]
[🗑 删除监听]
[▶️ 立即扫描]
[⏱ 扫描周期]
[🔄 开关]
[🔙 返回]
```

---


# 二十一、来源聊天必须以 Chat ID 作为唯一身份

这是持久化设计中的强制要求。

Telegram 的：

- chat 名称 / 标题：可以修改
- `@username`：可以修改、被取消或重新设置
- `chat_id`：应作为该聊天的稳定身份标识

因此监听规则内部不能把：

```text
chat_name
```

或：

```text
@username
```

作为唯一键。

## 21.1 推荐存储结构

首次添加监听时，用户可以输入：

```text
@source_channel
```

程序通过 Telegram API 解析后，必须持久化：

```json
{
  "chat_id": -1001234567890,
  "chat_name": "Source Channel",
  "chat_username": "source_channel",
  "tag": "#01musume",
  "targets": [
    "me",
    "@speedlearnnn"
  ],
  "download": true
}
```

其中：

```text
chat_id
```

是唯一身份。

而：

```text
chat_name
chat_username
```

只是展示/辅助信息。

## 21.2 聊天改名后仍然必须正常监听

例如初始：

```text
chat_id: -1001234567890
名称：01视频
username：@source_channel
```

之后管理员修改为：

```text
chat_id: -1001234567890
名称：MMD资源频道
username：@new_source_channel
```

监听不能因此失效。

程序应该继续通过：

```text
chat_id = -1001234567890
```

访问该聊天。

## 21.3 Username 变化

如果：

```text
@source_channel
```

修改成：

```text
@new_source_channel
```

监听仍然应该继续工作。

菜单展示信息可以在扫描/刷新时重新获取当前：

```text
名称
username
```

但不能修改 `chat_id` 身份。

## 21.4 配置文件建议

因此 `listen.json` 推荐使用：

```json
{
  "enabled": true,
  "interval_minutes": 1440,
  "listeners": [
    {
      "chat_id": -1001234567890,
      "chat_name": "Source Channel",
      "chat_username": "source_channel",
      "tag": "#01musume",
      "targets": [
        "me",
        "@speedlearnnn"
      ],
      "download": true
    }
  ]
}
```

如果用户通过 Bot 修改聊天名称/username，程序可以同步更新展示字段，但：

```text
chat_id
```

不得改变。

## 21.5 `listen_state.json` 也必须使用 Chat ID

例如：

```json
{
  "-1001234567890": {
    "last_message_id": 12345
  }
}
```

不要使用：

```json
{
  "@source_channel": {
    "last_message_id": 12345
  }
}
```

也不要使用：

```json
{
  "Source Channel": {
    "last_message_id": 12345
  }
}
```

原因是 username 和名称都可能变化。

## 21.6 首次配置解析规则

用户输入：

```text
@source_channel
```

或者：

```text
-1001234567890
```

程序都应该支持。

如果输入 username：

```text
@source_channel
       ↓
Telegram API resolve
       ↓
chat_id = -1001234567890
       ↓
保存 chat_id
```

后续扫描优先使用：

```text
chat_id
```

而不是再次依赖 username。


# 二十、添加监听流程

推荐使用现有 `open_input_window()` 输入机制。

流程：

```text
点击「添加监听」
        ↓
输入来源聊天
        ↓
输入标签
        ↓
选择目标
        ↓
选择是否下载
        ↓
保存 listen.json
```

例如：

```text
请输入监听聊天：
@source_channel
```

然后：

```text
请输入监听标签：
#01musume
```

然后：

```text
请选择转发目标：

☑ 收藏夹
☑ @speedlearnnn
☐ 其他
```

最后：

```text
自动下载：
🟢 开启
```

---

# 二十一、立即扫描

菜单提供：

```text
▶️ 立即扫描
```

点击后：

```text
📡 开始扫描...

@source_channel
  检查 23 条新消息
  匹配 #01musume：2 条
  收藏夹：2
  @speedlearnnn：2
  下载：2

扫描完成。
```

立即扫描不能破坏正常的定时任务。

---

# 二十二、日志

建议增加清晰日志：

```text
📡 标签监听任务启动
📡 扫描周期：1440 分钟
📡 监听规则：3 条
```

扫描：

```text
📡 开始扫描：@source_channel
📡 检查消息：10001 ~ 10023
🏷 匹配：#01musume
📤 转发：@speedlearnnn
📌 转发：收藏夹
⬇️ 下载任务已加入队列
📡 扫描完成
```

无匹配：

```text
📡 @source_channel：没有匹配的新消息
```

异常：

```text
⚠️ 标签监听失败：@source_channel
原因：...
```

异常必须被捕获，不能让后台任务退出。

---

# 二十三、错误处理

至少处理：

- chat 不存在
- chat 无访问权限
- username 无效
- Telegram FloodWait
- 网络异常
- 消息读取失败
- 转发失败
- 下载入队失败
- JSON 文件损坏
- 配置字段缺失
- 非法扫描周期
- 非法目标

对于单个聊天失败：

> 不能影响其他监听聊天。

例如：

```text
@source1 ❌
@source2 ✅
@source3 ✅
```

`@source1` 失败不能让整个 listener loop 停止。

---

# 二十四、配置文件损坏保护

不要直接：

```python
json.load(...)
```

然后异常退出。

应该：

1. 捕获 JSONDecodeError
2. 写日志
3. 使用空配置/安全配置
4. 不影响 UserBot 主进程

写配置时建议：

```text
临时文件
   ↓
flush
   ↓
atomic replace
```

避免 Bot 写配置时程序异常导致 JSON 半截损坏。

---

# 二十五、建议涉及的代码文件

优先新增：

```text
tg_userbot/listener.py
```

可能修改：

```text
tg_userbot/config.py
tg_userbot/app.py
tg_userbot/bot.py
tg_userbot/menu.py
```

如确实需要：

```text
tg_userbot/state.py
```

---

# 二十六、明确禁止修改的核心行为

本任务不是重构下载系统。

除非实现确实需要，否则不要修改：

```text
whitelist.py 的原有白名单语义
```

不要改变：

- `/wl`
- `state.WHITELIST_CHATS`
- 现有 Saved Messages 下载入口
- 现有下载队列
- dedup
- 文件命名
- retry
- Chrome worker
- reporter
- 其他已有功能

尤其禁止把：

```python
state.WHITELIST_CHATS
```

改造成：

```python
listener chats
```

这是错误设计。

---

# 二十七、测试要求

## 27.1 最重要测试

### 测试 A：监听聊天不在下载白名单

```text
下载白名单：
无 @source

标签监听：
@source + #01musume
```

发送：

```text
hello #01musume
```

必须：

```text
监听成功
```

证明两套白名单真正独立。

---

## 27.2 测试 B：监听聊天同时在下载白名单

```text
下载白名单：
@source

标签监听：
@source + #01musume
```

匹配消息：

```text
hello #01musume
```

必须：

```text
下载只发生一次
收藏夹转发只发生一次
@speedlearnnn 转发一次
```

---

## 27.3 测试 C：不匹配标签

```text
hello #02musume
```

监听：

```text
#01musume
```

结果：

```text
不转发
不下载
```

---

## 27.4 测试 D：多规则同一聊天

```text
@source + #01musume
@source + #02musume
```

同一轮：

> Telegram 只扫描一次 `@source`。

---

## 27.5 测试 E：首次启动

聊天已有：

```text
10000 条历史消息
```

首次创建监听：

```text
不要扫描并处理全部历史消息
```

---

## 27.6 测试 F：checkpoint

第一次：

```text
last_message_id = 100
```

第二次出现：

```text
101
102
103
```

只处理：

```text
101 ~ 103
```

第三次扫描：

```text
没有新消息
```

不得重复处理 101~103。

---

## 27.7 测试 G：转发失败

例如：

```text
@speedlearnnn
```

转发失败。

要求：

- 不影响其他目标
- 不影响其他聊天
- 失败消息不要错误地推进 checkpoint，导致永久丢失

---

# 二十八、完成标准

完成后必须满足：

### 功能

- [ ] `runtime/listen.json` 正常工作
- [ ] `runtime/listen_state.json` 正常工作
- [ ] 可以配置多个监听聊天
- [ ] 每个聊天可以配置多个标签
- [ ] 可以配置多个转发目标
- [ ] 支持 `me`
- [ ] 支持 `@username`
- [ ] 支持自动下载
- [ ] 支持定时扫描
- [ ] 支持立即扫描
- [ ] 支持 Bot 菜单管理

### 架构

- [ ] 标签监听白名单独立于下载白名单
- [ ] 不依赖 `state.WHITELIST_CHATS`
- [ ] 监听聊天可以不加入 `/wl`
- [ ] 下载白名单可以不加入标签监听
- [ ] 两者同时存在时不会重复下载
- [ ] 使用现有下载队列和 dedup
- [ ] 不新增第二套下载系统

### 稳定性

- [ ] 单个监听聊天失败不会影响其他监听
- [ ] listener 异常不会导致主程序退出
- [ ] FloodWait 有处理
- [ ] JSON 损坏有保护
- [ ] checkpoint 正确保存
- [ ] 首次启用不会扫描整个历史
- [ ] 不重复处理已经处理过的消息

### 回归

必须确认：

- [ ] `/wl` 正常
- [ ] 原有自动下载正常
- [ ] Saved Messages 下载正常
- [ ] 下载队列正常
- [ ] dedup 正常
- [ ] Chrome 功能正常
- [ ] reporter 正常
- [ ] 其他 Bot 菜单正常

---

# 二十九、给 DeepSeek 的最终开发原则

这是一个**增量功能**。

请严格遵循：

> **少改现有代码，优先新增独立模块；复用现有能力，不重复实现。**

尤其牢记：

```text
下载白名单
    ≠
标签监听白名单
```

正确关系：

```text
                    Telegram
                       │
          ┌────────────┴────────────┐
          │                         │
          ▼                         ▼
      实时收到消息               定时主动扫描
          │                         │
          ▼                         ▼
     下载白名单               标签监听白名单
          │                         │
          ▼                         ▼
       收藏夹/下载                标签匹配
                                    │
                         ┌──────────┴──────────┐
                         ▼                     ▼
                      转发目标                下载
```

**绝对不要把现有 `/wl` 白名单当成标签监听来源。**

---


# 二十二、来源与目标聊天的持久化身份统一规范

本功能必须统一采用：

```text
source_chat_id → 监听来源
target.chat_id → 普通转发目标
```

**来源和目标都不能依赖 username 或聊天名称作为持久化唯一身份。**

## 22.1 来源聊天

监听规则必须使用：

```json
"source_chat_id": -1001234567890
```

不要使用：

```json
"chat": "@source_channel"
```

`@source_channel` 只能作为用户输入时的便捷方式，程序收到后必须通过 Telegram API 解析成 `chat_id`，再保存。

## 22.2 转发目标

普通 Telegram 聊天/频道目标也必须使用：

```json
{
  "type": "chat",
  "chat_id": -1009876543210,
  "name": "SpeedLearn"
}
```

不要长期保存：

```json
"targets": [
  "@speedlearnnn"
]
```

`@speedlearnnn` 可以作为 Bot 添加目标时的输入方式，但解析成功后应持久化 `chat_id`。

## 22.3 Saved Messages 是特殊目标

Telegram「收藏夹 / Saved Messages」不需要保存普通 chat ID。

建议使用明确的类型：

```json
{
  "type": "saved_messages"
}
```

因此完整配置建议：

```json
{
  "enabled": true,
  "interval_minutes": 1440,
  "listeners": [
    {
      "source_chat_id": -1001234567890,
      "tag": "#01musume",
      "targets": [
        {
          "type": "saved_messages"
        },
        {
          "type": "chat",
          "chat_id": -1009876543210,
          "name": "SpeedLearn"
        }
      ],
      "download": true
    }
  ]
}
```

其中：

```text
source_chat_id
    ↓
监听来源

targets[].chat_id
    ↓
普通转发目标

targets[].type = saved_messages
    ↓
收藏夹
```

## 22.4 名称和 username 的定位

`name` 和 `username` 都不能作为身份键。

它们最多用于：

- Bot 菜单显示
- 日志显示
- 用户输入时辅助解析
- 调试

例如：

```json
{
  "type": "chat",
  "chat_id": -1009876543210,
  "name": "SpeedLearn"
}
```

即使目标聊天以后改名：

```text
SpeedLearn
↓
SpeedLearn Archive
```

仍然必须通过：

```text
-1009876543210
```

继续转发。

如果保存了 `username`，它也只能作为展示/缓存信息，不能作为唯一身份。

## 22.5 Bot 添加来源/目标

用户不需要手工输入 chat ID。

例如添加监听来源：

```text
请输入监听聊天：
@source_channel
```

程序：

```text
@source_channel
       ↓
Telegram API resolve
       ↓
source_chat_id = -1001234567890
       ↓
保存
```

添加转发目标：

```text
请输入目标聊天：
@speedlearnnn
```

程序：

```text
@speedlearnnn
       ↓
Telegram API resolve
       ↓
target.chat_id = -1009876543210
       ↓
保存
```

因此：

> **username 是输入方式，不是持久化身份。**

## 22.6 Chat ID 与 State 必须统一

`listen_state.json` 必须使用 `source_chat_id`：

```json
{
  "-1001234567890": {
    "last_message_id": 12345
  }
}
```

配置：

```text
listen.json
    ↓
source_chat_id
```

状态：

```text
listen_state.json
    ↓
source_chat_id
```

两者必须能够直接对应。

## 22.7 多标签、多目标去重

一条消息同时命中：

```text
#MMD
#01musume
```

即使两个规则都包含：

```text
收藏夹
@speedlearnnn
```

也只能产生：

```text
(message_id, saved_messages) × 1
(message_id, -1009876543210) × 1
```

不能产生两次收藏夹转发，也不能产生两次目标频道转发。

推荐先建立：

```python
matched = {
    message_id: {
        "matched_tags": {"#MMD", "#01musume"},
        "targets": {
            ("saved_messages", None),
            ("chat", -1009876543210)
        },
        "download": True
    }
}
```

然后统一执行。

## 22.8 不要通过字符串比较判断目标是否相同

不要使用：

```python
if target == "@speedlearnnn":
```

来判断持久化目标身份。

应该根据标准化后的：

```text
type + chat_id
```

判断。

例如：

```text
("chat", -1009876543210)
```

就是唯一目标。

Saved Messages：

```text
("saved_messages", None)
```

也是唯一目标。

这样即使 username 或名称变化，也不会产生重复目标。


# 三十、V1 暂不实现

以下功能不要在本次任务中擅自扩展：

- 历史消息全量扫描
- 正则标签规则
- OR/AND 复杂标签表达式
- Web 管理后台
- 数据库存储
- 新下载器
- 新队列系统
- 新 dedup 系统
- 多账号监听
- 复杂权限系统
- 收藏夹“只转发但禁止现有下载”的特殊模式

如果后续需要，再单独设计。

---

## 最终目标

用户可以通过 Bot 菜单配置：

```text
监听哪个聊天
+
监听哪个标签
+
转发到哪里
+
是否下载
+
多久扫描一次
```

并且：

> **这套标签监听配置完全独立于现有 `/wl` 下载白名单。**

这是本功能最核心的验收条件。
