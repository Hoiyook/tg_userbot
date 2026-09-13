# Chrome 下载目录功能 — DeepSeek Coding Agent 实施任务书

## 1. 任务目标

在现有 Telegram UserBot 的 `/chrome` 下载功能基础上，增加通过 `/` 指定 Chrome 下载文件的子目录，同时保留最后一个 `#xxx` 作为文件名前缀标注。

现有：

```text
/chrome #测试 https://example.com/test.zip
→ TG Chrome Download/#测试 test.zip
```

新增：

```text
/chrome A/B/#测试 https://example.com/test.zip
→ TG Chrome Download/A/B/#测试 test.zip
```

核心规则：

> 最后的 `/` 后面的 `#xxx` 是文件名标注；前面的全部内容才是目录路径。

示例：

```text
/chrome A/#测试 URL
→ TG Chrome Download/A/#测试 原文件名

/chrome A/B/#测试 URL
→ TG Chrome Download/A/B/#测试 原文件名

/chrome A/B/C/#测试 URL
→ TG Chrome Download/A/B/C/#测试 原文件名
```

---

## 2. 必须保持的兼容性

以下原有行为必须保持不变：

### 不带标注

```text
/chrome https://example.com/test.zip
```

仍然下载到：

```text
TG Chrome Download/test.zip
```

### 只有标注

```text
/chrome #测试 https://example.com/test.zip
```

仍然下载到：

```text
TG Chrome Download/#测试 test.zip
```

不要改变现有的 `#标注 + 空格 + 原文件名` 逻辑。

---

## 3. 修改范围

原则：

> **最小改动。**

主要修改：

```text
tg_userbot/chrome_client.py
tg_userbot/chrome_agent.py
tests/...
```

原则上不要修改：

```text
commands.py
app.py
state.py
reporter.py
config.py
```

原因：

- `/chrome*` 已经由 `commands.py` 转发给 `chrome_client.handle_chrome_command()`
- Reporter 与本需求无关
- 不需要新增配置项

---

## 4. Chrome 参数解析

当前 `/chrome` 已经支持：

```text
/chrome URL
/chrome #标注 URL
```

现有正则已经可以接收：

```text
/chrome A/B/#测试 https://example.com/test.zip
```

因此：

> **优先不要修改正则。**

只修改参数解析逻辑即可。

建议增加一个独立解析函数，例如：

```python
parse_chrome_target(...)
```

函数名可自行决定。

### 解析规则

```text
#测试
→ download_subdir = None
→ label = "#测试"

A/#测试
→ download_subdir = "A"
→ label = "#测试"

A/B/#测试
→ download_subdir = "A/B"
→ label = "#测试"

A/B/C/#测试
→ download_subdir = "A/B/C"
→ label = "#测试"
```

本次不要额外增加：

```text
/chrome A/B URL
```

这种“只有目录、没有 `#标注`”的新语法。

> **2026-09-10 修订（实测回填）：上面这条限制已撤销。** 用户实发
> `/chrome Hyuk/250630 <URL>`（只有目录、无 `#标注`）时，头 token 被回落到
> 「当 URL 交去校验」的旧路径，回执「URL 无效：Hyuk/250630」——URL 本身完全
> 合法，报错却指向它。现支持头 token 不带 `#` 时整个作为目录（含单层），
> 唯一例外是头 token 自身就是合法 http(s) URL 时仍按 URL 解析（防止
> `/chrome <URL1> <URL2>` 建出名叫 `https:` 的目录）。解析改动在
> `chrome_client._split_subdir_label` / `parse_chrome_submit`。

---

## 5. URL 解析

现有 URL 提取逻辑需要兼容新格式。

必须确保：

```text
/chrome #测试 https://example.com/test.zip
```

得到：

```text
url = https://example.com/test.zip
label = "#测试"
download_subdir = None
```

同时：

```text
/chrome A/B/#测试 https://example.com/test.zip
```

得到：

```text
url = https://example.com/test.zip
label = "#测试"
download_subdir = "A/B"
```

`is_chrome_command()` 与 `handle_chrome_command()` 必须使用一致的解析规则。

---

## 6. Request 增加 download_subdir

给 `chrome_client.add_request()` 增加可选参数：

```python
download_subdir=None
```

例如：

```python
add_request(
    task_id,
    url,
    ...,
    label=label,
    download_subdir=download_subdir,
)
```

有目录时，在 `chrome_requests.json` 中保存：

```json
{
  "task_id": "...",
  "url": "https://example.com/test.zip",
  "label": "#测试",
  "download_subdir": "A/B"
}
```

没有目录时保持现有数据结构即可。

---

## 7. Task 增加 download_subdir

`chrome_agent.create_task()` 增加：

```python
download_subdir=None
```

并在 Task 中保存。

`claim_new_requests()` 从 request 转 task 时必须把 `download_subdir` 传递过去。

最终数据链路：

```text
Telegram
  ↓
chrome_client
  ↓
chrome_requests.json
  ↓
chrome_agent
  ↓
task
  ↓
download_subdir
  ↓
实际下载目录
```

---

## 8. 下载目录计算

在 `chrome_agent.py` 中增加一个简单辅助函数，例如：

```python
get_task_download_dir(root_dir, task)
```

逻辑：

```text
root_dir + task["download_subdir"]
```

例如：

```text
root_dir = TG Chrome Download
download_subdir = A/B

→ TG Chrome Download/A/B
```

没有 `download_subdir` 时：

```text
→ TG Chrome Download
```

目录在处理任务时按需创建：

```python
os.makedirs(task_download_dir, exist_ok=True)
```

---

## 9. Chrome 实际下载

这是核心修改。

现有：

```python
run_download_attempt(
    cdp,
    task["url"],
    download_dir,
    timeout,
)
```

本身已经支持指定下载目录。

因此不要重构 Chrome/CDP 下载机制。

处理任务时：

```text
task
 ↓
计算 task_download_dir
 ↓
创建目录
 ↓
run_download_attempt(..., task_download_dir, ...)
```

也就是把原来的全局：

```text
CHROME_DOWNLOAD_DIR
```

在具体下载时替换为：

```text
task_download_dir
```

例如：

```text
/chrome A/B/#测试 URL
```

实际下载：

```text
TG Chrome Download/A/B/test.zip
```

---

## 10. Label Rename 必须继续工作

现有：

```python
apply_label_rename(download_dir, task)
```

不要破坏。

修改后必须使用：

```python
apply_label_rename(task_download_dir, task)
```

这样：

```text
下载：
TG Chrome Download/A/B/test.zip

重命名：
TG Chrome Download/A/B/#测试 test.zip
```

而不能让重命名逻辑继续寻找根目录。

---

## 11. Retry 必须保持目录

如果：

```text
download_subdir = A/B
```

第一次下载失败，后续 Retry 仍然必须使用：

```text
TG Chrome Download/A/B
```

因此 `download_subdir` 必须保存在 task 中，而不能只存在于第一次命令处理阶段。

---

## 12. Recovery 必须支持子目录

这是本次实现中必须处理的一点。

当前 `recover_tasks()` 如果直接使用：

```python
os.path.join(download_dir, filename)
```

需要调整为使用 task 对应的实际下载目录。

例如：

```text
task:
download_subdir = A/B
filename = #测试 test.zip
```

实际文件：

```text
TG Chrome Download/A/B/#测试 test.zip
```

Recovery 必须检查这个位置，而不是：

```text
TG Chrome Download/#测试 test.zip
```

否则程序重启后可能错误地重新下载已经完成的任务。

---

## 13. Result 通知

当前 `result_text(task)` 如果只显示全局：

```text
download_dir()
```

新增子目录后，建议显示实际 task 下载目录：

```text
下载目录：TG Chrome Download/A/B
```

这是小范围 UI 调整，不要重构通知系统。

---

## 14. 不要做的事情

本次只实现下载子目录功能，不要顺便重构。

不要修改：

- Queue
- Worker
- Reporter
- 普通 Telegram 下载
- 抖音/B站下载
- Chrome Profile
- Chrome 启动机制
- CDP 连接机制
- 重试架构
- 并发架构
- 命令路由
- 全局配置结构

也不要实现：

- 多 Chrome 实例
- 多 CDP
- 多 Worker
- 下载后再移动文件的方案
- 新的配置项
- “只有目录、没有 `#标注`”的命令语法

核心原则：

> **能用现有代码实现，就不要引入新架构。**

---

## 15. 最小测试要求

考虑到这是个人使用程序，不需要建立庞大的测试体系。

只需要针对本次修改做最基本的回归验证。

至少确认：

### 参数解析

```text
/chrome URL
/chrome #测试 URL
/chrome A/#测试 URL
/chrome A/B/#测试 URL
/chrome A/B/C/#测试 URL
```

解析结果正确。

### 实际目录

确认：

```text
A/B/#测试
```

最终下载到：

```text
TG Chrome Download/A/B/#测试 原文件名
```

### 原有功能

确认：

```text
/chrome URL
/chrome #测试 URL
```

没有被破坏。

### Retry / Recovery

至少确认代码逻辑中：

```text
download_subdir
```

会随 task 持久化，并用于 Retry 和 Recovery。

如果项目现有测试可以方便覆盖上述逻辑，可以增加少量测试；**不要为了测试而大规模新增测试代码。**

---

## 16. 完成标准

以下全部满足即可：

```text
/chrome URL
```

正常。

```text
/chrome #测试 URL
```

正常。

```text
/chrome A/#测试 URL
```

正常。

```text
/chrome A/B/#测试 URL
```

正常。

```text
/chrome A/B/C/#测试 URL
```

正常。

最终：

```text
TG Chrome Download/A/B/#测试 原文件名
```

并且：

- Label Rename 正常
- Retry 保持原目录
- Recovery 能识别子目录中的已完成文件
- 原有 `/chrome` 功能不受影响
- 没有修改无关模块

---

## 17. 完成后汇报

实现完成后请简要汇报：

```text
## 修改文件

- tg_userbot/chrome_client.py
- tg_userbot/chrome_agent.py
- tests/xxx.py（如果有）

## 核心修改

1. ...
2. ...
3. ...

## 功能验证

/chrome URL              ✅
/chrome #标注 URL        ✅
/chrome A/#标注 URL      ✅
/chrome A/B/#标注 URL    ✅

## Retry / Recovery

- Retry 子目录：✅
- Recovery 子目录：✅

## 测试

pytest：

XX passed

## 未修改模块

- commands.py
- app.py
- state.py
- reporter.py
- config.py

## 其他

...
```

如果测试失败，不要直接说完成。

说明：

```text
哪个测试失败
为什么失败
是否与本次修改有关
```

---

# 最终验收示例

假设：

```text
CHROME_DOWNLOAD_DIR =
/Users/xxx/TG Chrome Download
```

发送：

```text
/chrome A/B/#测试 https://example.com/test.zip
```

最终必须得到：

```text
/Users/xxx/TG Chrome Download/
└── A/
    └── B/
        └── #测试 test.zip
```

而：

```text
/chrome #测试 https://example.com/test.zip
```

仍然得到：

```text
/Users/xxx/TG Chrome Download/
└── #测试 test.zip
```

**不要扩大需求，不要重构现有 Chrome Agent，只完成上述最小改动。**
