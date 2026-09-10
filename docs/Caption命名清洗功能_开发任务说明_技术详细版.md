# Caption 命名清洗功能 — 开发任务说明（含开发指导 / 技术实现细节）

> **给 DeepSeek / Coding Agent 的直接开发任务。**
>
> 目标：在现有 Telegram UserBot 的**文件命名链路**中增加 Caption 清洗能力，并通过 Bot 动态配置清洗规则。
>
> **重要：本任务不是让你重新设计整个项目，而是在现有代码上做一次“小范围、可验证”的功能增加。**
>
> 请严格遵守“先理解现有代码 → 最小修改 → 测试 → 汇报”的流程。

---

# 1. 任务目标

为现有 Telegram 转发媒体的文件命名增加 Caption 清洗能力。

处理链路：

```text
Telegram 转发消息
    ↓
读取 Caption
    ↓
按配置规则清洗 Caption
    ↓
得到清洗后的命名文本
    ↓
进入现有命名逻辑
    ↓
如果最终文件名超长
    ↓
继续使用现有 truncate_filename() 逻辑
```

**本次只增加 Caption 清洗，不重构现有命名系统。**

---

# 2. 开发前必须做的事情

在修改代码之前，先检查当前仓库实际代码。

重点查看：

```text
tg_userbot/config.py
tg_userbot/naming.py
tg_userbot/bot.py
tg_userbot/menu.py
tg_userbot/commands.py
tests/
```

特别确认：

1. `naming.py` 中 `get_caption()` 的实际实现。
2. `compute_final_filename()` 的实际参数和调用方式。
3. 当前配置是如何持久化的。
4. Bot 命令是直接在 `bot.py` 处理，还是由 `commands.py` 分发。
5. 当前菜单按钮如何注册和处理。
6. 当前是否已经存在“输入窗口 / 等待下一条消息”的机制。
7. 当前测试框架和测试运行方式。

**不要根据本任务说明猜测这些代码的实际结构。**

先阅读实际代码，再选择最小修改点。

如果实际项目结构和本文档略有不同，以现有项目代码为准，但不能改变下面定义的功能行为。

---

# 3. 真实需求示例

原始 Caption：

```text
作者：#腿玩年 期数：bl11 角色：#弱音 i站地址【 https://ecchi.iwara.tv/videos/jprxauwjbqfepebnb 】 标签：#MMD #掉装备
```

期望清洗后：

```text
#腿玩年 bl11 #弱音 #MMD #掉装备
```

然后将这个结果交给现有文件名生成逻辑。

**如果清洗后的 Caption 很长，不在 Caption Filter 中重新实现截断，继续走现有的 `MAX_FILENAME_BYTES` / `truncate_filename()` 逻辑。**

---

# 4. 四种规则类型

规则统一通过前缀区分类型：

```text
exact:xxx
contains:xxx
regex:xxx
field:xxx
```

---

## 4.1 exact：精确匹配

例如：

```text
exact:作者：
```

只删除完全匹配的：

```text
作者：
```

不要把它自动解释成：

```text
作者
作者真的很厉害
这个作者
```

也不要做语义推断。

### 技术实现

可以直接使用：

```python
text.replace(pattern, "")
```

但要注意：

- pattern 为空时不能执行无限制替换逻辑。
- 规则必须去掉 `exact:` 前缀后再使用。
- exact 的含义是“删除这个确切字符串”。

---

# 5. contains：包含匹配

例如：

```text
contains:广告
```

Caption 中只要包含：

```text
广告
```

就删除匹配到的字符串。

例如：

```text
这是广告内容
```

结果：

```text
这是内容
```

这是**有意的宽匹配**。

不要为了防止误删而偷偷改变 contains 的语义。

如果用户不希望宽匹配，可以使用 `exact:` 或 `field:`。

---

# 6. regex：正则匹配

例如：

```text
regex:https?://\S+
```

用于删除 URL。

例如：

```text
regex:【.*?】
```

用于删除：

```text
【xxx】
```

### 技术实现

建议使用 Python 标准库：

```python
import re
```

核心：

```python
re.sub(pattern, "", text)
```

### 非法正则

必须捕获：

```python
re.error
```

不能因为一个错误规则导致整个 UserBot 崩溃。

推荐行为：

```text
发现非法 regex
    ↓
跳过该规则
    ↓
其他规则继续执行
```

Bot 添加规则时最好直接告诉用户：

```text
❌ 正则规则无效：
regex:(abc

原因：...
```

但不需要做复杂的规则校验系统。

---

# 7. field：字段式匹配（最重要）

`field:` **绝对不能实现成简单的 contains。**

例如：

```text
field:作者
```

应该识别：

```text
作者：#腿玩年
作者:#腿玩年
作者 : #腿玩年
作者 ： #腿玩年
```

然后删除整个字段：

```text
作者：#腿玩年
```

---

# 8. field 的正确语义

字段格式：

```text
字段名 + 冒号 + 字段值
```

支持：

```text
：
:
```

并允许冒号附近存在空格：

```text
作者：xxx
作者:xxx
作者 : xxx
作者 ： xxx
```

字段值一直持续到：

1. 下一个“已配置的 field 字段”开始；
2. 或 Caption 结束。

例如配置：

```text
field:作者
field:期数
field:角色
field:标签
```

输入：

```text
作者：#腿玩年 期数：bl11 角色：#弱音 标签：#MMD #掉装备
```

逻辑上应该解析成：

```text
作者：#腿玩年
期数：bl11
角色：#弱音
标签：#MMD #掉装备
```

然后删除这些字段的“字段名 + 冒号 + 字段值”，保留：

```text
#腿玩年 bl11 #弱音 #MMD #掉装备
```

---

# 9. field 的核心算法建议

不要尝试用一个极其复杂的正则一次解决所有情况。

建议采用：

```text
当前 Caption
    ↓
根据当前配置中的所有 field 名称
构造“下一个字段”的识别模式
    ↓
找到 field:目标字段 的起点
    ↓
找到下一个已配置 field 的起点
    ↓
删除当前字段范围
    ↓
继续处理
```

推荐思路：

```python
field_pattern = r"(?P<name>作者|期数|角色|标签)\s*[：:]\s*"
```

但**不要把上面的字段名写死在 Python 代码中**。

字段名必须来自：

```text
field:作者
field:期数
field:角色
...
```

配置。

---

# 10. field 防误删是硬性要求

以下文本：

```text
这个作者真的很厉害。
我很喜欢这个作者。
作者真的很厉害。
这个作者：真的很厉害。
我认识一个作者：张三。
```

不能因为存在：

```text
field:作者
```

就被删除。

核心判断是：

> `field:作者` 只匹配“结构化字段”，不匹配普通自然语言中的“作者”。

例如：

```text
作者：#腿玩年
```

是结构化字段。

而：

```text
这个作者真的很厉害
```

不是。

---

# 11. field 的边界建议

为了减少误删，建议要求字段名在 Caption 的“字段边界”出现。

例如：

```text
作者：#腿玩年
```

可以匹配。

但：

```text
这个作者：真的很厉害
```

不要匹配。

可以把字段起点定义为：

```text
Caption 开头
```

或者：

```text
前面存在空白 / 换行
```

然后再要求：

```text
字段名 + 可选空格 + : / ：
```

即类似：

```regex
(?<!\S)作者\s*[：:]
```

注意：

**最终正则必须根据实际代码测试后确定。**

不要为了追求一个“看起来很漂亮”的正则而牺牲实际行为。

---

# 12. 字段顺序不能固定

以下都必须支持：

```text
作者：A 期数：B 角色：C 标签：D
```

以及：

```text
角色：C 作者：A 标签：D
```

以及：

```text
标签：D 作者：A
```

字段可以缺失。

不能写成：

```text
作者 → 期数 → 角色 → 标签
```

这样的固定流程。

程序必须根据 Caption 中实际出现的位置处理。

---

# 13. 字段值可以包含空格

例如：

```text
作者：John Smith 期数：第12期 标签：#MMD #test
```

应该识别为：

```text
作者：John Smith
期数：第12期
标签：#MMD #test
```

而不是把：

```text
John
```

当成字段值、把：

```text
Smith
```

当成普通文本。

所以 field 的结束位置必须是：

```text
下一个已识别 field
```

而不是：

```text
下一个空格
```

---

# 14. field 与 URL / 括号的处理

默认规则：

```python
CAPTION_FILTER_RULES = [
    "field:作者",
    "field:期数",
    "field:角色",
    "field:i站地址",
    "field:标签",
    r"regex:https?://\S+",
    r"regex:【.*?】",
]
```

这些规则必须真正成为默认配置。

对于：

```text
i站地址【 https://ecchi.iwara.tv/videos/jprxauwjbqfepebnb 】
```

默认规则最终应清除 URL 和对应包裹内容，使最终 Caption 得到：

```text
#腿玩年 bl11 #弱音 #MMD #掉装备
```

注意：

如果 `field:i站地址` 已经完整删除这一字段，那么 URL/括号正则可能不会再命中。

这是正常现象。

不要为了“每条规则都必须实际执行”而修改算法。

---

# 15. 默认配置必须写入配置文件

必须把默认规则真正写入现有配置体系。

建议配置项名称：

```python
CAPTION_FILTER_RULES = [
    "field:作者",
    "field:期数",
    "field:角色",
    "field:i站地址",
    "field:标签",
    r"regex:https?://\S+",
    r"regex:【.*?】",
]
```

要求：

1. 规则是配置项。
2. 默认值就是上面这组规则。
3. 程序启动时读取。
4. Bot 修改后持久化。
5. 修改规则不需要修改 Python 代码。
6. 重启 UserBot 后 Bot 修改过的规则仍然存在。

**不要把默认规则只写在 `caption_filter.py` 中。**

---

# 16. 配置持久化技术要求

先检查现有项目如何保存可变配置。

优先复用：

```text
现有 JSON / config / settings 持久化机制
```

不要新增：

```text
数据库
SQLite
Web 管理后台
复杂配置框架
```

如果项目已经存在：

```python
load_xxx_config()
save_xxx_config()
```

之类的机制，直接复用。

---

# 17. 注意“默认配置”和“当前配置”不是一回事

建议明确区分：

```python
DEFAULT_CAPTION_FILTER_RULES = [...]
```

和：

```python
CAPTION_FILTER_RULES
```

或者采用项目现有配置机制实现同样效果。

逻辑上必须满足：

```text
reset
    ↓
恢复 DEFAULT_CAPTION_FILTER_RULES
    ↓
持久化
```

而不是：

```text
reset
    ↓
重新从当前配置读取
```

否则 reset 没有意义。

---

# 18. Bot 配置必须支持

增加：

```text
/caption_filter
```

至少支持：

```text
/caption_filter
/caption_filter add field:作者
/caption_filter del 3
/caption_filter clear
/caption_filter reset
/caption_filter test <text>
```

---

# 19. Bot：查看规则

输入：

```text
/caption_filter
```

显示：

```text
🧹 Caption 清洗规则

1. field:作者
2. field:期数
3. field:角色
4. field:i站地址
5. field:标签
6. regex:https?://\S+
7. regex:【.*?】

共 7 条
```

没有规则时：

```text
🧹 Caption 清洗规则

当前没有任何规则。
```

---

# 20. Bot：添加规则

例如：

```text
/caption_filter add field:作者
```

添加成功：

```text
✅ 已添加规则：

field:作者
```

### 添加前建议做的最小校验

必须检查：

```text
是否有合法前缀
```

支持：

```text
exact:
contains:
regex:
field:
```

如果：

```text
abc:作者
```

则返回：

```text
❌ 不支持的规则类型。

支持：
exact:
contains:
regex:
field:
```

如果是：

```text
regex:(abc
```

应该返回：

```text
❌ 正则表达式无效。
```

不要保存明显非法的 regex。

---

# 21. Bot：删除规则

例如：

```text
/caption_filter del 3
```

删除第 3 条。

成功：

```text
✅ 已删除规则 3：

field:角色
```

不存在：

```text
/caption_filter del 99
```

返回：

```text
❌ 规则编号不存在。
```

---

# 22. Bot：清空规则

```text
/caption_filter clear
```

直接清空当前规则并持久化即可。

如果现有 Bot 对危险操作已经有确认机制，则复用现有机制。

**不要为了这个功能单独设计复杂确认系统。**

---

# 23. Bot：恢复默认

```text
/caption_filter reset
```

恢复：

```python
[
    "field:作者",
    "field:期数",
    "field:角色",
    "field:i站地址",
    "field:标签",
    r"regex:https?://\S+",
    r"regex:【.*?】",
]
```

然后立即持久化。

---

# 24. Bot：测试清洗

例如：

```text
/caption_filter test 作者：#腿玩年 期数：bl11 角色：#弱音 标签：#MMD
```

返回：

```text
🧪 Caption 清洗测试

原文：
作者：#腿玩年 期数：bl11 角色：#弱音 标签：#MMD

结果：
#腿玩年 bl11 #弱音 #MMD
```

测试必须调用真正的：

```python
clean_caption()
```

不能在 Bot 里复制一套清洗逻辑。

---

# 25. Bot 菜单

如果现有菜单架构适合增加按钮，可以增加：

```text
🧹 Caption 清洗
```

进入后：

```text
📋 查看规则
➕ 添加规则
➖ 删除规则
🧪 测试清洗
♻️ 恢复默认
🗑 清空规则
🔙 返回
```

但：

> **命令功能优先于菜单功能。**

如果当前菜单架构改起来很复杂，不要重构菜单。

直接保证：

```text
/caption_filter
```

可用即可。

---

# 26. Bot 输入窗口

如果项目已有：

```text
等待下一条消息
输入 Cookie
输入搜索条件
```

等输入状态机制，Caption Filter 的：

```text
添加规则
测试 Caption
```

优先复用。

不要新增一个完全独立的状态管理系统。

---

# 27. 新增模块：caption_filter.py

建议：

```text
tg_userbot/caption_filter.py
```

这个模块只负责：

> Caption → 清洗后的 Caption

不要让它负责：

```text
Telegram
下载
文件名
Worker
Queue
Reporter
Bot UI
```

---

# 28. 建议 API

至少：

```python
parse_caption_filter_rule(rule)
clean_caption(caption, rules=None)
```

推荐逻辑：

```python
def parse_caption_filter_rule(rule):
    ...
```

返回一个简单结构，例如：

```python
{
    "type": "field",
    "pattern": "作者",
}
```

或者使用：

```python
@dataclass
class CaptionFilterRule:
    type: str
    pattern: str
```

不需要为了这个功能引入复杂 class hierarchy。

---

# 29. parse_caption_filter_rule 技术细节

规则：

```text
field:作者
```

解析：

```text
type = "field"
pattern = "作者"
```

规则：

```text
regex:https?://\S+
```

解析：

```text
type = "regex"
pattern = "https?://\S+"
```

规则：

```text
contains:广告
```

解析：

```text
type = "contains"
pattern = "广告"
```

规则：

```text
exact:作者：
```

解析：

```text
type = "exact"
pattern = "作者："
```

---

# 30. 解析规则的推荐实现

可以采用：

```python
RULE_TYPES = {"exact", "contains", "regex", "field"}
```

然后：

```python
prefix, separator, pattern = rule.partition(":")
```

再判断：

```python
if prefix not in RULE_TYPES:
    raise ValueError(...)
```

注意：

regex 本身可能包含很多 `:`，例如：

```text
regex:https?://\S+
```

所以必须只切分第一个冒号。

不要：

```python
rule.split(":")
```

然后假设只有两个元素。

正确思路：

```python
rule.split(":", 1)
```

或：

```python
partition(":")
```

---

# 31. 空规则处理

以下规则都应该视为无效：

```text
field:
exact:
contains:
regex:
```

Bot 添加时拒绝。

程序运行时遇到无效规则：

```text
跳过
```

不要崩溃。

---

# 32. clean_caption 推荐执行顺序

建议：

```text
原始 Caption
    ↓
解析规则
    ↓
field
    ↓
exact / contains
    ↓
regex
    ↓
统一空白
    ↓
返回结果
```

原因：

`field` 依赖 Caption 的原始字段结构。

因此不要先把所有空白压缩掉，也不要先把 Caption 随便切碎。

---

# 33. 一个重要实现建议：先处理 field

field 的逻辑和普通字符串替换不同。

建议：

```python
field_rules = [...]
other_rules = [...]
```

然后：

```text
先 field
再 exact / contains
最后 regex
```

不要对所有规则简单做：

```python
for rule in rules:
    text = apply_rule(text, rule)
```

然后让 field 和普通规则完全使用同一套算法。

---

# 34. field 的推荐实现思路

可以写一个内部函数：

```python
_remove_field(text, field_name, all_field_names)
```

逻辑：

```text
1. 找到 field_name 的合法字段起点
2. 找到它后面的下一个已配置字段起点
3. 删除 [当前字段起点, 下一个字段起点) 
4. 如果没有下一个字段，则删除到文本末尾
5. 重复处理
```

伪代码：

```python
def remove_field(text, field_name, all_field_names):
    start_pattern = build_field_start_pattern(field_name)
    next_pattern = build_any_field_start_pattern(all_field_names)

    # 找当前字段
    # 找下一个字段
    # 删除整个字段区间

    return text
```

---

# 35. 更简单的实现方式也可以

如果 DeepSeek 认为逐字段删除更容易维护，可以先找到所有字段：

```text
字段 A 起点
字段 B 起点
字段 C 起点
字段 D 起点
```

构造：

```python
[(start, end, field_name), ...]
```

然后删除需要过滤的字段。

这种方式反而更容易保证：

```text
字段值可以包含空格
字段顺序不固定
字段可以缺失
```

推荐优先考虑这种方式。

---

# 36. 字段识别的关键点

字段名必须使用：

```python
re.escape(field_name)
```

因为用户可能配置：

```text
field:A+B
```

不能直接把 `A+B` 当正则。

即：

```python
re.escape(field_name)
```

是必须的。

---

# 37. regex / contains / exact 的规则顺序

如果规则列表：

```text
contains:作者
field:作者
```

那么 `field:作者` 必须先处理。

否则：

```text
contains:作者
```

会先破坏：

```text
作者：#腿玩年
```

字段结构。

所以不要单纯按照配置列表顺序执行。

建议按照类型分组：

```text
field
exact
contains
regex
```

---

# 38. 但是不要改变用户配置的“优先级”语义

对于同类型规则，例如：

```text
contains:A
contains:B
contains:C
```

可以按配置顺序执行。

用户看到的规则顺序和实际执行顺序应该尽量一致。

只需要把：

```text
field
```

提升到前面处理。

---

# 39. 空白标准化

清洗完成后统一处理：

```text
首尾空白 → 删除
连续空格 → 一个空格
换行 → 空格
连续空白 → 一个空格
```

例如：

```text
#MMD     #掉装备
```

变成：

```text
#MMD #掉装备
```

例如：

```text
#MMD

#掉装备
```

变成：

```text
#MMD #掉装备
```

---

# 40. 不能破坏 #

必须保留：

```text
#MMD #掉装备
```

绝对不能变成：

```text
#MMD#掉装备
```

因此空白标准化不能简单粗暴：

```python
text.replace(" ", "")
```

---

# 41. Caption 清洗不能做的事情

不要：

```text
自动删除所有中文
自动删除所有英文
自动删除所有 URL
自动删除所有括号
自动删除所有“作者”
自动删除所有“标签”
自动猜测哪些内容重要
```

只有配置中的规则才能删除内容。

---

# 42. naming.py 接入

现有 `naming.py` 继续负责：

```text
Caption 获取
文件名生成
原始文件名
日期前缀
Label
MIME 扩展名
文件名长度限制
truncate_filename()
```

本次只增加：

```text
get_caption()
    ↓
clean_caption()
    ↓
现有命名逻辑
```

如果现有：

```python
compute_final_filename(message, caption=None, ...)
```

已经支持显式 Caption：

**优先：**

```python
caption = get_caption(message)
caption = clean_caption(caption)
return compute_final_filename(message, caption=caption, ...)
```

或者在合适的现有入口中清洗一次。

**不要复制 `compute_final_filename()` 的整个实现。**

---

# 43. 一个非常重要的要求：只清洗一次

不要出现：

```text
Bot test → clean_caption()
命名 → clean_caption()
截断 → clean_caption()
```

正常下载命名链路只需要：

```text
获取 Caption
    ↓
清洗一次
    ↓
现有命名
```

Bot 测试只是单独调用：

```text
clean_caption()
```

---

# 44. 不要修改 truncate_filename()

现有：

```python
truncate_filename()
```

继续作为唯一的文件名长度控制逻辑。

不要在：

```text
caption_filter.py
```

中加入：

```python
truncate_caption()
```

或者新的 UTF-8 截断。

正确：

```text
原 Caption
    ↓
Caption Filter
    ↓
清洗后的 Caption
    ↓
compute_final_filename()
    ↓
MAX_FILENAME_BYTES
    ↓
truncate_filename()
```

---

# 45. 无 Caption / 清洗后为空

没有 Caption：

```python
""
```

保持现有行为。

Caption 被全部清洗掉：

```python
""
```

也保持现有行为。

不要改成：

```text
未命名文件
```

不要在 Caption Filter 中添加 fallback 文件名。

---

# 46. 默认规则最终效果

默认：

```python
CAPTION_FILTER_RULES = [
    "field:作者",
    "field:期数",
    "field:角色",
    "field:i站地址",
    "field:标签",
    r"regex:https?://\S+",
    r"regex:【.*?】",
]
```

输入：

```text
作者：#腿玩年 期数：bl11 角色：#弱音 i站地址【 https://ecchi.iwara.tv/videos/jprxauwjbqfepebnb 】 标签：#MMD #掉装备
```

输出：

```text
#腿玩年 bl11 #弱音 #MMD #掉装备
```

---

# 47. 测试：不要写过量测试

用户明确要求：

> 只需要覆盖核心行为，不需要建立庞大的测试体系。

至少测试下面这些。

---

## Test 1：真实 Caption

输入：

```text
作者：#腿玩年 期数：bl11 角色：#弱音 i站地址【 https://ecchi.iwara.tv/videos/jprxauwjbqfepebnb 】 标签：#MMD #掉装备
```

期望：

```text
#腿玩年 bl11 #弱音 #MMD #掉装备
```

---

## Test 2：field 防误删

规则：

```text
field:作者
```

输入：

```text
这个作者真的很厉害。
```

必须保持：

```text
这个作者真的很厉害。
```

---

## Test 3：exact

规则：

```text
exact:作者：
```

输入：

```text
作者：#腿玩年
```

结果至少应该删除：

```text
作者：
```

保留：

```text
#腿玩年
```

---

## Test 4：contains

规则：

```text
contains:广告
```

输入：

```text
这是广告内容
```

结果：

```text
这是内容
```

---

## Test 5：regex URL

规则：

```text
regex:https?://\S+
```

输入：

```text
测试 https://example.com/video abc
```

结果：

```text
测试 abc
```

---

## Test 6：中文/英文冒号

规则：

```text
field:作者
```

测试：

```text
作者：张三
作者:张三
作者 : 张三
作者 ： 张三
```

都应该识别。

---

## Test 7：换行字段

输入：

```text
作者：#腿玩年
期数：bl11
角色：#弱音
标签：#MMD #掉装备
```

输出：

```text
#腿玩年 bl11 #弱音 #MMD #掉装备
```

---

## Test 8：超长

验证：

```text
clean_caption()
```

不会截断。

最终文件名仍然由：

```text
truncate_filename()
```

处理。

---

# 48. Bot 测试

至少手工验证：

```text
/caption_filter
/caption_filter add field:测试
/caption_filter
/caption_filter del N
/caption_filter clear
/caption_filter reset
/caption_filter test 作者：#腿玩年 期数：bl11
```

并重启一次 UserBot，确认：

```text
Bot 修改后的规则仍然存在
```

---

# 49. 修改范围

优先控制在：

```text
tg_userbot/caption_filter.py       # 新增
tg_userbot/naming.py               # 最小接入
tg_userbot/config.py               # 默认规则/持久化入口
tg_userbot/bot.py                  # Bot 命令菜单/交互
tg_userbot/commands.py             # 如果当前命令由此分发
tg_userbot/menu.py                 # 如需要菜单
tests/test_caption_filter.py       # 少量核心测试
```

原则上不要修改：

```text
app.py
state.py
queue.py
workers.py
reporter.py
chrome_client.py
chrome_agent.py
```

除非实际调用链确实必须修改。

---

# 50. 不要做的事情

不要：

- 重构现有命名系统
- 重写 `truncate_filename()`
- 修改 Telegram 下载流程
- 修改 Worker
- 修改 Queue
- 修改 Reporter
- 修改 Chrome Agent
- 新增数据库
- 新增 Web 管理页面
- 引入复杂第三方依赖
- 自动猜测用户想删除什么
- 把 `field:` 实现成简单字符串 contains
- 把字段顺序写死
- 在 Bot 中复制 Caption 清洗逻辑
- 在多个模块复制规则解析逻辑
- 为了这个功能大范围整理旧代码

核心原则：

> **配置明确表达意图，程序严格按规则执行。**

---

# 51. 推荐开发步骤

请严格按照下面顺序开发。

## Step 1：阅读代码

先检查：

```text
config.py
naming.py
bot.py
commands.py
menu.py
tests/
```

确认实际调用链。

---

## Step 2：先实现纯函数

先创建：

```text
tg_userbot/caption_filter.py
```

先实现：

```python
parse_caption_filter_rule()
clean_caption()
```

不要一开始就修改 Bot。

---

## Step 3：先写核心测试

先确保：

```text
真实 Caption
field 防误删
exact
contains
regex
换行
中英文冒号
```

通过。

---

## Step 4：接入 naming.py

只增加：

```text
Caption
 ↓
clean_caption()
 ↓
原命名逻辑
```

不要改其他命名行为。

---

## Step 5：接入配置

加入默认：

```python
CAPTION_FILTER_RULES = [
    "field:作者",
    "field:期数",
    "field:角色",
    "field:i站地址",
    "field:标签",
    r"regex:https?://\S+",
    r"regex:【.*?】",
]
```

使用现有持久化机制。

---

## Step 6：接入 Bot

增加：

```text
/caption_filter
```

实现：

```text
查看
添加
删除
清空
恢复默认
测试
```

---

## Step 7：运行测试

执行项目现有测试方式。

优先：

```bash
pytest -q
```

如果项目有自己的测试命令，以项目现有方式为准。

---

## Step 8：手工验证 Bot

验证：

```text
查看
添加
删除
清空
恢复默认
测试
重启后配置仍存在
```

---

# 52. 如果发现现有架构与文档不一致

不要停下来要求用户重新设计。

按照下面原则处理：

```text
现有架构
    ↓
寻找最小接入点
    ↓
保持本文档定义的功能行为
```

例如：

如果实际 Bot 命令全部由：

```text
commands.py
```

分发，那么：

```text
caption_filter
```

应该在 `commands.py` 接入，而不是强行把全部逻辑塞到 `bot.py`。

如果实际配置持久化已经集中在某个配置管理模块，就使用现有模块。

**本文档规定的是功能和行为，不要求机械照抄文件结构。**

---

# 53. 代码质量要求

代码要：

- 简单
- 可读
- 小范围修改
- 标准库优先
- 不增加无意义抽象

推荐：

```python
import re
from dataclasses import dataclass
```

如果不需要 dataclass，就不要使用。

不要为了一个 Caption Filter 创建：

```text
BaseRule
ExactRule
ContainsRule
RegexRule
FieldRule
RuleFactory
RuleRegistry
RuleManager
```

这一类过度设计。

---

# 54. 错误处理原则

以下错误不能导致整个 UserBot 崩溃：

```text
未知规则前缀
空规则
非法 regex
Bot 删除不存在的编号
Bot 输入格式错误
```

正常行为：

```text
提示错误
跳过非法规则 / 拒绝保存
继续运行
```

---

# 55. 性能要求

Caption 通常很短。

不需要为了这个功能做复杂性能优化。

正常：

```text
O(规则数量 × Caption 长度)
```

即可。

不要增加：

```text
数据库
缓存系统
异步任务
Worker
线程池
```

---

# 56. 一个容易犯的错误

不要这样写：

```python
for rule in rules:
    if rule.startswith("field:"):
        text = text.replace(rule[6:], "")
```

这是错误的。

因为：

```text
field:作者
```

不是：

```text
删除所有“作者”
```

必须识别：

```text
作者：字段值
```

这一整个结构。

---

# 57. 另一个容易犯的错误

不要这样：

```python
re.sub(r"作者.*? ", "", text)
```

因为：

```text
这个作者真的很厉害
```

可能被误删。

必须先判断：

```text
作者
```

是不是一个真正的字段起点。

---

# 58. 第三个容易犯的错误

不要在 Caption Filter 中调用：

```python
truncate_filename()
```

或者复制它的代码。

Caption Filter 只负责：

```text
内容清洗
```

文件名长度由：

```text
naming.py
```

负责。

---

# 59. 第四个容易犯的错误

不要在 Bot 中直接修改：

```python
CAPTION_FILTER_RULES.append(...)
```

然后就认为持久化完成。

必须：

```text
修改当前配置
    ↓
保存配置
    ↓
更新运行时配置
```

保证：

```text
立即生效
+
重启后仍存在
```

---

# 60. 运行时配置的重要要求

Bot 修改后：

```text
不重启 UserBot
```

下一条 Telegram 消息就应该使用新规则。

不能要求：

```text
修改配置
↓
重启 UserBot
↓
才生效
```

---

# 61. 建议配置接口

如果现有项目允许，可以提供：

```python
get_caption_filter_rules()
set_caption_filter_rules(rules)
reset_caption_filter_rules()
```

这样：

```text
naming.py
```

只需要：

```python
rules = get_caption_filter_rules()
caption = clean_caption(caption, rules)
```

Bot 也使用同一套配置接口。

这样可以避免：

```text
Bot 一套配置逻辑
naming.py 一套配置逻辑
```

---

# 62. 配置职责建议

推荐：

```text
config.py
    ↓
负责配置存取

caption_filter.py
    ↓
负责 Caption 清洗

naming.py
    ↓
负责文件名生成

bot.py / commands.py
    ↓
负责用户操作
```

不要让：

```text
caption_filter.py
```

自己读写文件配置。

这样测试会更简单。

---

# 63. 最终架构

目标结构：

```text
                    ┌──────────────────┐
                    │      Bot         │
                    │ 查看/增删/测试   │
                    └────────┬─────────┘
                             │
                             ↓
                    ┌──────────────────┐
                    │     config.py    │
                    │ 当前规则/持久化   │
                    └────────┬─────────┘
                             │
                             ↓
Telegram Caption ──→ caption_filter.py
                             │
                             ↓
                    清洗后的 Caption
                             │
                             ↓
                       naming.py
                             │
                             ↓
                  现有文件名生成逻辑
                             │
                             ↓
                    truncate_filename()
```

---

# 64. 完成标准

必须满足：

1. 默认规则写入配置文件。
2. Bot 可以查看规则。
3. Bot 可以增加规则。
4. Bot 可以删除规则。
5. Bot 可以清空规则。
6. Bot 可以恢复默认规则。
7. Bot 可以测试 Caption 清洗结果。
8. `exact:` 正常。
9. `contains:` 正常。
10. `regex:` 正常。
11. `field:` 正常。
12. `field:作者` 不会误删“这个作者真的很厉害”。
13. 真实 Caption 得到：

```text
#腿玩年 bl11 #弱音 #MMD #掉装备
```

14. 清洗后的超长内容继续使用原来的截断逻辑。
15. 没有 Caption 时原有命名行为不变。
16. Bot 修改规则后立即生效。
17. Bot 修改规则后重启 UserBot 仍然保留。
18. 未涉及的下载、队列、Worker、Reporter 等功能不受影响。

---

# 65. 最终验证命令

完成后至少执行：

```bash
pytest -q
```

如果项目已有针对命名功能的测试，也要一起执行。

如果测试失败：

> **不要声称完成。**

必须说明：

```text
失败测试
失败原因
当前修改状态
```

---

# 66. 完成后汇报格式

完成后只需要简洁汇报：

```text
## 修改文件

- tg_userbot/caption_filter.py
- tg_userbot/naming.py
- tg_userbot/config.py
- tg_userbot/commands.py / bot.py
- ...

## 核心修改

1. 增加 Caption Filter
2. 接入现有命名流程
3. 增加配置持久化
4. 增加 Bot 配置命令

## Bot

- 查看规则：✅
- 添加规则：✅
- 删除规则：✅
- 清空规则：✅
- 测试：✅
- 恢复默认：✅
- 重启后保留：✅

## Caption 清洗

- exact：✅
- contains：✅
- regex：✅
- field：✅
- 防误删：✅

## 超长处理

继续使用原 truncate_filename()：✅

## 测试

pytest：XX passed

## 未修改核心模块

- queue.py
- workers.py
- reporter.py
- chrome_client.py
- chrome_agent.py

## 备注

...
```

如果测试失败，不要写“完成”，如实说明。

---

# 67. 最终目标

输入：

```text
作者：#腿玩年 期数：bl11 角色：#弱音 i站地址【 https://ecchi.iwara.tv/videos/jprxauwjbqfepebnb 】 标签：#MMD #掉装备
```

最终命名文本：

```text
#腿玩年 bl11 #弱音 #MMD #掉装备
```

而：

```text
这个作者真的很厉害。
```

保持不变。

以后可以直接通过 Bot 调整规则，不需要修改代码。

---

# 68. 给开发 Agent 的最后要求

**请不要一上来直接大规模修改代码。**

执行顺序必须是：

```text
阅读现有代码
    ↓
确认实际调用链
    ↓
给出简短实施计划
    ↓
实现 caption_filter.py
    ↓
写核心测试
    ↓
接入 naming.py
    ↓
接入 config.py
    ↓
接入 Bot
    ↓
运行测试
    ↓
检查 diff
    ↓
最终汇报
```

如果发现某一步需要修改本文档没有列出的核心模块：

1. 先确认是否真的必要；
2. 优先寻找更小的接入点；
3. 只有确实必要时才修改；
4. 最终汇报中明确说明为什么修改。

**不要顺手重构旧代码。**

**不要修改无关功能。**

**不要为了“代码更漂亮”扩大修改范围。**

> ### 核心原则
>
> **这是一个“小功能增量”，不是一次架构重构。**
>
> **先保证现有功能不坏，再实现 Caption 清洗。**

---

# 69. 实施记录（2026-09-10，落地时对文档矛盾的裁定）

按 §2/§52「以现有项目代码为准、保持本文档定义的功能行为」执行。落地时发现本文档
有三处会互相矛盾或不适用于本仓库的地方，裁定如下（均已按裁定实现并测试）。

## 69.1 §42 的接入顺序写反了：清洗必须在 sanitize **之前**

§42 建议 `get_caption() → clean_caption() → 现有命名逻辑`。但本仓库的
`naming.get_caption()` 返回的是**已被 `sanitize_filename` 处理过**的文本，而
sanitize 会破坏字段结构（实测）：

```text
作者:张三                        → 作者_张三        （ASCII 冒号被换成 _）
作者：#腿玩年\n期数：bl11        → 作者：#腿玩年_期数：bl11（换行被换成 _）
i站地址【 https://… 】           → i站地址【 https___… 】
```

照 §42 实现，`field:作者` 永远匹配不上 ASCII 冒号写法，换行分隔的字段也认不出来
（Test 6 / Test 7 直接失败）。故改为：**原始 `message.message` → clean_caption →
sanitize**，唯一入口是 `naming.get_caption(message, override=None)`；`download.py`
只做「消息自带文字 vs 相册继承说明」的优先级选择、取原始文本（`raw_caption`），
保证全链路只清洗一次（§43 的要求）。

## 69.2 §8/§34 与 §46/§64/§67 冲突：field 取「剥标签、留值」

§8/§34 说删「字段名 + 冒号 + **字段值**」（`删除 [当前字段起点, 下一个字段起点)`）；
§46/§64/§67 三次给出的验收输出却**保留字段值**（`#腿玩年 bl11 #弱音 #MMD #掉装备`）。
两者不可兼得：按 §34 的伪代码实现，真实 Caption 会**整段变空串**（所有文字都在字段里）。

裁定以**验收输出为准**（§46/§64/§67 是验收标准，§8/§34 是实现建议）：
`field:X` = 剥掉「X + 冒号（含邻近空格）」，字段值全部保留。

## 69.3 `i站地址【…】` 没有冒号：补一条「括号也算字段分隔符」

§7/§8 只支持 `:` 与 `：`，而真实 Caption 里 `i站地址【 https://… 】` 是**无冒号**写法。
若严格只认冒号，`i站地址` 标签会残留在文件名里，§46/§67 的目标输出达不到。

故补充（这是本文档唯一一处规格**扩充**）：字段名后**紧跟左括号**（`【（([`）时同样
视为字段起点，此时只去掉字段名本身，括号内容交给默认规则里的 `regex:【.*?】` /
`regex:https?://\S+` 清掉（§14 的「清除 URL 和对应包裹内容」即由此达成）。

## 69.4 已验证的最终行为

```text
作者：#腿玩年 期数：bl11 角色：#弱音 i站地址【 https://… 】 标签：#MMD #掉装备
  → #腿玩年 bl11 #弱音 #MMD #掉装备          （§46/§64.13/§67 逐字一致）
换行分隔的同义 Caption                        → 同上（Test 7）
这个作者真的很厉害。/ 我很喜欢这个作者。/ 作者真的很厉害。/
这个作者：真的很厉害。/ 我认识一个作者：张三。 → 全部原样保留（§10 五条，Test 2）
作者:张三 / 作者 : 张三 / 作者 ： 张三        → 张三（Test 6）
作者：John Smith 期数：第12期 标签：#MMD #test → John Smith 第12期 #MMD #test
```

命名结果示例：`26-09-10 #腿玩年 bl11 #弱音 #MMD #掉装备 - 1080p.mp4`；
清洗后为空 → 回到既有的 `媒体类型_时间戳` 兜底，绝不出现「未命名文件」。

## 69.5 与文档不同的两处实现选择（§52 允许）

- **配置归属**：§62 建议 `config.py` 负责配置存取。本仓库的既有惯例是**特性模块
  自己存取自己的运行时 JSON**（`dedup.py` / `whitelist.py` / `thread.py` 皆如此），
  且 §62 同时要求 `caption_filter.py` 不要自己读写文件配置。折中：**默认规则常量
  （`DEFAULT_CAPTION_FILTER_RULES`）与文件路径放 `config.py`**（满足 §15/§17），
  load/save 等薄函数放 `caption_filter.py`（沿用仓库既有机制，非新造轮子）。
- **§28 的 `set_caption_filter_rules` 未实现**：没有调用方（UI 只需要 add/del/
  clear/reset），按 §53「不增加无意义抽象」省略；`get_rules()` 有提供。

## 69.6 未做（不在本次范围 / 需人工）

- §25 菜单已实现（命令为主路径，菜单复用同一批服务函数）；§26 输入窗口复用现有
  cookie/查询机制并**改为三窗口互斥**（否则先开的 cookie 窗口会把一段规则当
  cookie 存进 tg_secrets.json）。
- §48 的 Bot 手工验证（真实 Telegram 收发）需在真机上进行，未由本次自动化测试覆盖。
