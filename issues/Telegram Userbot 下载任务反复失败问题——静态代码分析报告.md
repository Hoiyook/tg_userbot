Telegram Userbot 下载任务反复失败问题——静态代码分析报告

项目： Hoiyook/tg_userbot
当前分支： feature/multi-worker-v2.10
问题日期： 2026-09-10

> ## ⚖️ 核实结论（2026-09-10，Claude Code 基于真实日志与事件流复核）
>
> 本报告**提出的结构性风险基本属实，但对本次故障的归因方向是错的**。逐条判定：
>
> | 报告中的判断 | 核实结果 |
> |---|---|
> | §2 `DOWNLOAD_IDLE_TIMEOUT` 基于「无 progress_callback」可能误杀 | **风险机制成立，实测零触发**——全量日志 grep `无任何进度`/`无进度超过`/`TimeoutError` = **0 次**（9/6–9/10 全部日志文件）。不该改这个值。 |
> | §3 失败 worker 被直接放回池 | **属实**。`download.py:881` 在 `finally` 对所有退出路径一律 `workers.release(worker)`；`workers.py:257` `q.put_nowait()` 无健康检查。 |
> | §4 reconnect 只看 `is_connected()`，有半开连接风险 | **属实**。`download.py:516`。 |
> | §8「异常连接→超时→retry→复用异常 worker→再超时」是本次故障的核心链 | **证伪**。342 次失败尝试里 **94%（323/342）死在 55–70 秒**（telethon keepalive 周期），不是 120 秒看门狗；`TimeoutError` 0 次。报告该链应产出的「秒级即败的死连接」特征只占 **9/342（2.6%）**。 |
> | §5 `.download` 删临时文件丢已下数据 | **属实但属已知设计取舍**（CLAUDE.md 明载不支持断点续传、删半成品整文件重下）。实测样本：341.82MB 文件跑 4%（13.75MB）后失败、重试从 0 开始。 |
> | §6 `DOWNLOAD_RETRIES=3` 语义易混淆 | **属实，但不是缺陷**。`download.py:839` 确认是「单次执行内的尝试上限」，retry 榜累计是另一层。 |
>
> **真正根因（本次事件）**：共享代理链路被打穿，非程序缺陷。硬证据两条——
> ① 与 worker 池**完全无关**的 bot 菜单客户端（独立 TelegramClient）在同一分钟窗口内
> 每 60 秒断连一次（07:40:00 / 07:41:01 / 07:42:01…）；
> ② 07:42:37 有 **14 条 worker 在同一秒**被发现已陈尸池中并就地重连。
> 单条 worker 中毒不可能产生这两条现象。
>
> **报告末尾三个问题的直接回答**：
> 1. `DOWNLOAD_IDLE_TIMEOUT=120` 存在误杀机制风险，但 4 天日志实测 **0 次触发**；若要改，
>    应改**判定方式**（区分"无回调"与"底层无响应"），而不是调大数值。
> 2. **是**，失败 worker 确实会被原样放回池（Q2 属实）。
> 3. 该循环**结构上存在**，但**本次未发生**，因此不是本次"一直重试"的核心机制。
>    §8 的因果链被日志证伪。
>
> 处置：见 `002-重试韧性与恢复重放.md`（已把 worker 健康隔离列为关联项、低优先级）。
> 本次故障真正的"体验放大"问题在 002，日志丢失问题在 001（已修，待重启）。

1. 问题概述

当前 Telegram Userbot 的正常工作流程是：用户将 Telegram 消息或媒体转发至「收藏夹（Saved Messages）」，Userbot 获取对应 Message 后，通过 Telethon 下载媒体文件。

2026 年 9 月 10 日上午出现一个异常情况：某个转发到收藏夹的文件无法正常下载，任务持续失败并反复重试。

目前对 download.py、queue.py、config.py 等代码进行静态分析后，初步判断问题不一定只是单纯的下载超时时间设置过短，更值得关注的是：

下载超时、Worker 连接复用、连接恢复以及重试机制之间可能存在组合性问题。

2. 最值得关注的问题：下载超时机制

当前配置：

DOWNLOAD_IDLE_TIMEOUT = 120

代码中的 120 秒并不是传统意义上的“网络请求超时”。

它实际上表示：

连续 120 秒没有收到 Telethon 的 progress_callback 回调，就认为下载已经没有进展。

当前逻辑大致是：

开始下载
    ↓
等待 Telethon download_media
    ↓
progress_callback 被调用
    ↓
更新 last_activity
    ↓
继续下载

如果超过 120 秒没有收到进度回调：

120 秒没有 progress callback
        ↓
watchdog 认为下载卡死
        ↓
cancel download task
        ↓
抛出 TimeoutError
        ↓
进入 retry

这里存在一个重要风险：

“没有 progress callback”并不一定等于“网络已经死掉”。

例如：

Telegram DC 响应暂时变慢；
代理出现短暂阻塞；
TCP 连接仍然存在；
Telethon 正在等待底层网络数据；
某个下载阶段暂时没有触发 progress callback。

都有可能出现：

网络连接仍然存在
        +
下载任务实际上仍未彻底失败
        ↓
长时间没有 progress callback
        ↓
被 120 秒 watchdog 误判为超时

因此，当前 DOWNLOAD_IDLE_TIMEOUT 的检测方式本身比“120 秒这个数值是否合理”更加值得审查。

3. 更严重的潜在问题：失败 Worker 可能被继续复用

当前下载流程会从 Worker Pool 中借用 Worker：

borrow worker
    ↓
worker.download_media()
    ↓
成功 / 超时 / 网络异常
    ↓
finally
    ↓
release worker

问题在于：

当 Worker 因 Timeout、ConnectionError 等异常导致下载失败后，它似乎仍然会被直接释放回 Worker Pool。

也就是说，代码目前没有明显形成完整的：

Worker 出现严重网络异常
        ↓
标记 Worker unhealthy
        ↓
从 Pool 移除
        ↓
断开连接
        ↓
销毁 Worker
        ↓
重新创建 Worker

这样的隔离机制。

因此存在一种比较危险的故障链：

Worker A
   ↓
网络连接异常 / 半死连接
   ↓
下载 Timeout
   ↓
Worker A 被 release
   ↓
Worker A 返回 Pool
   ↓
下一次 retry 再次 borrow Worker A
   ↓
继续使用异常连接
   ↓
再次 Timeout

如果实际情况确实如此，那么单纯增加 DOWNLOAD_IDLE_TIMEOUT 并不能真正解决问题。

4. reconnect 机制存在半开连接风险

当前 _sleep_and_reconnect() 的核心逻辑类似：

if not transfer.is_connected():
    await transfer.connect()

这个判断存在一个问题：

is_connected() == True

只能说明 Telethon 认为连接仍然存在，并不一定代表：

这个连接当前仍然能够正常传输数据。

例如存在：

TCP 半开连接；
代理连接异常；
NAT 状态异常；
Telegram DC 连接异常；
底层 socket 实际已经不可用，但状态仍显示 connected。

这种情况下可能出现：

is_connected() == True
        ↓
代码认为不需要 reconnect
        ↓
继续使用原 Worker
        ↓
下载再次失败

因此，连接恢复机制不能只依赖 is_connected() 判断。

5. 下载失败会丢失已经完成的部分数据

当前下载采用：

xxx.download

作为临时文件。

如果下载过程中已经完成了大量数据，例如：

1 GB 文件
        ↓
已经下载 900 MB
        ↓
网络异常
        ↓
TimeoutError
        ↓
删除 .download
        ↓
retry
        ↓
重新从 0 开始

那么之前已经下载的 900 MB 会全部丢失。

这会产生两个问题：

网络不稳定时会重复消耗大量流量；
大文件越接近完成阶段失败，浪费越严重。

因此需要进一步确认当前 Telethon 下载方式是否能够可靠支持断点续传，以及项目是否应该在特定网络异常下保留临时文件。

不过这属于优化方向，不一定是本次“持续重试”的直接根因。

6. 当前 Retry 机制可能放大问题

当前：

DOWNLOAD_RETRIES = 3

这个数字更准确地说是：

一次任务执行过程中的下载重试次数。

它并不是整个任务生命周期的最大重试次数。

可能形成：

第一次执行
 ├─ attempt 1
 ├─ attempt 2
 └─ attempt 3
        ↓
     仍然失败
        ↓
    进入 retry 队列

如果之后再次执行 /retry：

retry
 ├─ attempt 1
 ├─ attempt 2
 └─ attempt 3

因此从用户角度看，就可能表现为：

“这个任务为什么一直在重试？”

所以需要区分：

单次下载 retry；
Queue retry；
/retry；
是否存在自动重新执行。
7. 系统实际上存在两层 Timeout

目前代码中至少需要区分两个不同的 Timeout。

第一层：获取 Telegram Message

queue.py 在执行任务时，会先调用：

state.client.get_messages(...)

并使用：

QUEUE_FETCH_TIMEOUT

限制获取 Message 的时间。

它解决的是：

Telegram Message 能不能正常获取。

第二层：实际媒体下载

获取 Message 成功后，进入：

download.download_file()

这里使用：

DOWNLOAD_IDLE_TIMEOUT = 120

解决的是：

媒体下载过程中是否长时间没有 progress callback。

因此排查实际问题时必须根据日志判断：

QUEUE_FETCH_TIMEOUT

还是：

DOWNLOAD_IDLE_TIMEOUT

真正触发了异常。

8. 当前最值得怀疑的完整故障链

综合目前的静态分析，我认为最值得优先验证的是下面这条链路：

Telegram / Proxy / DC 出现网络异常
              ↓
Telethon download_media 暂时无法产生 progress callback
              ↓
连续 120 秒没有 progress callback
              ↓
DOWNLOAD_IDLE_TIMEOUT 触发
              ↓
download task 被 cancel
              ↓
TimeoutError
              ↓
当前 Worker 被 release 回 Pool
              ↓
Worker 的底层连接可能仍处于异常状态
              ↓
retry
              ↓
再次使用异常 Worker / connection
              ↓
再次 Timeout
              ↓
重复失败

如果日志能够证明这条链路，那么问题的核心就不是简单的：

“120 秒太短。”

而应该是：

当前下载超时机制与 Worker 生命周期管理、连接恢复机制之间存在缺陷。

9. 问题优先级判断
问题  优先级 当前判断
DOWNLOAD_IDLE_TIMEOUT 基于 progress callback  🔴 高 确定存在设计风险
Timeout 后 Worker 是否继续复用 🔴 高 高度值得检查
reconnect 仅依赖 is_connected()  🔴 高 存在半开连接风险
Retry 可能反复使用异常连接  🔴 高 与上述问题可能形成故障循环
失败后删除 .download 🟠 中 会造成大量重复下载
DOWNLOAD_RETRIES=3 的语义  🟠 中 容易造成持续 retry
QUEUE_FETCH_TIMEOUT 🟡 待确认 需要结合日志判断
Telegram Message / file reference / DC 问题 🟡 待确认 需要错误日志进一步确认
10. 目前不能直接下的结论

仅通过静态代码分析，目前还不能 100% 确定本次故障就是 DOWNLOAD_IDLE_TIMEOUT 导致的。

还需要结合实际日志确认是否出现：

下载超过 120s 无任何进度

或者：

队列任务取消息超时

以及是否出现：

AuthBytesInvalidError
ConnectionError
TimeoutError
RPCError

这些错误。

因此目前最合理的结论是：

120 秒无进度 watchdog 是明确存在的设计风险，但真正导致本次任务持续失败的根因，需要结合日志进一步确认 Worker、Telethon 连接以及 Telegram DC/代理状态。

11. 后续修复原则

在确认根因之前，不建议直接：

120 秒 → 300 秒

或者简单关闭 Timeout。

更合理的修复方向应该是：

重新评估 DOWNLOAD_IDLE_TIMEOUT 的检测机制；
区分“没有 progress callback”和“底层网络真正无响应”；
Timeout / ConnectionError 后，对 Worker 进行健康状态判断；
对异常 Worker 必要时进行销毁和重建，而不是直接放回 Pool；
改进 reconnect 机制，避免只依赖 is_connected()；
明确单次 retry 和任务生命周期 retry 的关系；
评估 .download 临时文件是否可以用于断点续传；
增加针对 Timeout、Worker 异常、连接重建和 retry 的测试。

原则是最小修改。

不能因为修复下载问题而破坏目前已经正常工作的：

多 Worker；
下载队列；
去重；
任务状态；
统计；
Telegram 消息处理；
Chrome Agent 等其他功能。
12. 最终需要 Claude Code 回答的问题

本次代码审查最终最重要的是回答下面三个问题：

第一：

DOWNLOAD_IDLE_TIMEOUT=120 是否存在误杀正常下载任务的可能？如果存在，应该修改“超时判断机制”，还是单纯增加时间？

第二：

一个 Worker 因 Timeout / ConnectionError 导致下载失败后，是否仍然可能被重新放回 Worker Pool，并在下一次 retry 中继续使用？

第三：

如果上述情况存在，是否会形成“异常连接 → 下载超时 → retry → 复用异常 Worker → 再次超时”的循环？

如果第三个问题答案为 是，那么这很可能就是当前“任务一直重试失败”现象的核心机制之一。