# -*- coding: utf-8 -*-
"""命令注册表——用户命令的**唯一事实源**（2026-11-12，结构债工程 A）。

此前同一份命令知识散落五处：config.REGISTERED_COMMAND_NAMES（审计）、
bot.BOT_COMMANDS（面板）、commands.Android 拦截名单、commands._help_text
（手册）、MENU_ACTIONS（菜单回调，独立空间不在本表）。新增命令要同步
3-5 处，漏任何一处就是一类已知 bug（面板不显示/审计漏记/手册缺失）。

本文件集中声明全部用户命令的元数据，四份清单**全部由此派生**：

    config.REGISTERED_COMMAND_NAMES  ← registered()（审计全集）
    bot.BOT_COMMANDS                 ← panel_commands()（panel_desc 非空）
    commands.Android 拦截            ← android_blocked()（android_block=True）
    commands._help_text              ← help_groups()（按组渲染）

纪律：
  * 新增命令 = 在 ALL 里加一行 + 写 handler，其它位置**零改动**；
  * 本文件是**纯数据**——只准 import re/datetime，绝不 import 项目内
    任何模块（被 config 顶层消费，环会炸启动）；
  * tests/test_command_registry.py 钉住「派生 == 手工原值」，迁移期
    防漂移，迁移完成后防回退。

组名（group）即 /help 的【】段落名，渲染时保持原有顺序。
"""

import re


class Command:
    """一条用户命令的静态元数据（不可变值对象）。"""

    __slots__ = ("name", "group", "help_line", "panel_desc", "android_block",
                 "aliases")

    def __init__(self, name, group, help_line=None, panel_desc=None,
                 android_block=False, aliases=()):
        self.name = name
        self.group = group
        self.help_line = help_line
        self.panel_desc = panel_desc
        self.android_block = android_block
        self.aliases = tuple(aliases)

    def __repr__(self):
        return f"Command({self.name!r}, group={self.group!r})"


ALL = (

    Command("115dup", "工具/系统", "/115dup scan|…|apply —— 115 查重＋审表执行（deleted/移动/重命名）", "115 全盘查重：/115dup scan|list|show|del；裸命令看最近结果", False),
    Command("115inv", "工具/系统", "/115inv scan [分钟] —— 115 全量清单（分类体检的前置）", "115 全量清单：/115inv scan [分钟]；裸命令看进度", False),
    Command("115x", "工具/系统", "/115x <115路径> —— 115 解压回传（拉压缩包→解压→回传+对账）", "115 解压回传：/115x <115路径>；裸命令看状态", True),
    Command("asmr", "工具/系统", "/asmr <asmr-300链接|RJ号> [only 关键词] —— 音声作品→115 声音/asmr/RJ号//cmdq [成功|失败|待执行] —— 指令台账（离线回放执行情况）", "ASMR 音声：/asmr <asmr-300链接|RJ号> [only 关键词]；裸命令看队列", True),
    Command("bili", "工具/系统", "/bili <BV号|URL|b23短链|合集URL> —— B站视频/合集下载（archive 持久去重：单视频重发跳过、合集重跑只收新视频；画质按 cookie 账号档位，设置→🌐 B站Cookie）", "B站视频/合集下载：/bili <BV号|链接|合集URL>；裸命令看状态", True),
    Command("botclean", "工具/系统", "/botclean on|off —— bot 对话自动清理开关", "bot 对话自动清理开关：/botclean on|off", False),
    Command("caption_filter", "工具/系统", "/caption_filter 列表｜/caption_filter_add 规则", "Caption 命名清洗规则列表", False),
    Command("caption_filter_add", "工具/系统", "/caption_filter 列表｜/caption_filter_add 规则", "添加清洗规则：/caption_filter_add 规则", False),
    Command("caption_filter_del", "工具/系统", "/caption_filter_del 序号｜/caption_filter_test 原文", "删除清洗规则：/caption_filter_del 序号", False),
    Command("caption_filter_test", "工具/系统", "/caption_filter_del 序号｜/caption_filter_test 原文", "试清洗：/caption_filter_test 原文", False),
    Command("cd2check", "工具/系统", "/origin —— 解析失败账本；/cd2ck 日志对账；/cd2check —— API 实对账", "CloudDrive2 备份对账：本地媒体 vs 115 远端", True),
    Command("cd2ck", "工具/系统", "/origin —— 解析失败账本；/cd2ck 日志对账；/cd2check —— API 实对账", "115 备份对账：本地滞留媒体 × 备份日志交叉", True),
    Command("cd2tasks", "工具/系统", "/cd2tasks —— CD2 上传任务明细", "CD2 上传任务：查看正在往 115 传输的文件", True),
    Command("chrome", "Chrome", "/chrome [子目录/][#标注] URL —— 直链下载，网盘页可见打开", "用 Chrome 下载：/chrome <URL>", True),
    Command("chrome_cancel", "Chrome", "/chrome_tasks；/chrome_cancel 序号", "取消 Chrome 任务：/chrome_cancel <序号>", True),
    Command("chrome_failed", "Chrome", "/chrome_failed —— 最近失败任务；/chrome_retry 序号 —— 重试", "Chrome 最近失败任务明细", True),
    Command("chrome_retry", "Chrome", "/chrome_failed —— 最近失败任务；/chrome_retry 序号 —— 重试", "重试 Chrome 失败任务：/chrome_retry <序号>", True),
    Command("chrome_start", "Chrome", "/chrome_start｜/chrome_stop｜/chrome_status", "启动 Chrome 下载 Agent", True),
    Command("chrome_status", "Chrome", "/chrome_start｜/chrome_stop｜/chrome_status", "查看 Chrome Agent 状态", True),
    Command("chrome_stop", "Chrome", "/chrome_start｜/chrome_stop｜/chrome_status", "停止 Chrome 下载 Agent", True),
    Command("chrome_tasks", "Chrome", "/chrome_tasks；/chrome_cancel 序号", "查看可取消的 Chrome 任务", True),
    Command("clean", "工具/系统", "/clean｜/clearmsg｜/setcleartime 30s|1m|1h|off", "清理 .download 临时文件", False),
    Command("clearmsg", "工具/系统", "/clean｜/clearmsg｜/setcleartime 30s|1m|1h|off", "清理程序产生的消息", False),
    Command("cmdhis", "监控查询", "/cmdhis [N] —— 最近发给 bot 的消息（发 1 同效）", "最近发给 bot 的消息（整块可复制重发）", False),
    Command("cmdq", "工具/系统", "/asmr <asmr-300链接|RJ号> [only 关键词] —— 音声作品→115 声音/asmr/RJ号//cmdq [成功|失败|待执行] —— 指令台账（离线回放执行情况）", "指令台账：/cmdq [成功|失败|待执行]（离线回放执行情况）", False),
    Command("cmdt", "工具/系统", "/cmdt 列表｜/cmdt_add 名 命令｜/cmdt_del 名｜/cmdt_run 名", "命令模板列表", False),
    Command("cmdt_add", "工具/系统", "/cmdt 列表｜/cmdt_add 名 命令｜/cmdt_del 名｜/cmdt_run 名", "保存命令模板：/cmdt_add 名字 命令", False),
    Command("cmdt_del", "工具/系统", "/cmdt 列表｜/cmdt_add 名 命令｜/cmdt_del 名｜/cmdt_run 名", "删除命令模板：/cmdt_del 名字", False),
    Command("cmdt_run", "工具/系统", "/cmdt 列表｜/cmdt_add 名 命令｜/cmdt_del 名｜/cmdt_run 名", "执行命令模板：/cmdt_run 名字", False),
    Command("dedup", "下载自动行为", "/dedup [off]；/thread [3-20] —— 去重/并发", "查看/设置重复媒体去重", False),
    Command("done", "监控查询", "/done [N][关键词] —— 下载记录（默认10）", "查看最近下载记录", False),
    Command("donenotify", "下载自动行为", "/donenotify [on|off] —— 完成即时通知（默认关）", None, False),
    Command("downloading", "监控查询", "/progress｜/downloading —— 下载进度", None, False),
    Command("dyu", "工具/系统", "/dyu <抖音作者主页> [子目录] —— 作者全作品批量串行下载", "抖音作者批量：/dyu <主页链接> [子目录]", True),
    Command("ehentai", "工具/系统", "/ehentai <画廊URL> [作者] —— 种子优先，无种子走原包→漫画/作者", "e-hentai 画廊：/ehentai <URL> [作者]，种子优先", True),
    Command("extract", "工具/系统", "/extract <子目录> [dry] —— 递归解压压缩包到同名目录（成功才删）", "递归解压目录：/extract <子目录> [dry]", True),
    Command("find", "监控查询", "/find 关键词 —— 媒体下落三源查询", "按关键字查一条媒体的下落", False),
    Command("folder", "监控查询", "/status｜/folder｜/logpath —— 状态/目录/日志", None, False),
    Command("gum", "Gumroad", "/gum <链接> [目录] —— 购买内容批量下载（重跑即续传）", "Gumroad 下载：/gum <链接> [目录]；裸命令看任务", True),
    Command("gumjobs", "Gumroad", "/gumst [目录] —— 各任务进度；/gumjobs —— 任务列表", None, False),
    Command("gumst", "Gumroad", "/gumst [目录] —— 各任务进度；/gumjobs —— 任务列表", "Gumroad 进度：/gumst [目录]", False),
    Command("health", "工具/系统", "/health —— 一键巡检（连接/磁盘/队列/卡死嫌疑）", "一键巡检：/health（连接/磁盘/队列/卡死）", False),
    Command("help", "其它", "/help", "查看全部命令", False),
    Command("help2", "工具/系统", "/help2 —— 数据表与配置文件字典", None, False),
    Command("inspect", "工具/系统", "/inspect —— 系统巡检（连接/任务/产出/失败/备份）", "系统巡检：连接/任务/产出/失败/备份 一屏", False),
    Command("links", "外链台账", "发『链接 备注』即登记（自动查重）；/links [关键词] 清单/搜索", None, False),
    Command("listen", "工具/系统", "/listen 列表｜/listen_on｜/listen_off｜/listen_scan", "标签监听：状态与规则列表", False),
    Command("listen_add", "工具/系统", "/listen_add 聊天 标签 [目标,目标] [on|off]｜/listen_edit 序号", "新增监听规则：/listen_add 聊天 标签 [目标] [on]", False),
    Command("listen_del", "工具/系统", "/listen_del 序号｜/listen_interval 分钟", "删除监听规则：/listen_del 序号", False),
    Command("listen_edit", "工具/系统", "/listen_add 聊天 标签 [目标,目标] [on|off]｜/listen_edit 序号", "修改监听规则：/listen_edit 序号", False),
    Command("listen_failed", "工具/系统", "/listen_failed —— 失败任务明细（/listen_retry 行id 重试｜/listen_failed del 行id 删除）", "监听失败任务明细与重试", False),
    Command("listen_interval", "工具/系统", "/listen_del 序号｜/listen_interval 分钟", "设置扫描周期：/listen_interval 分钟", False),
    Command("listen_list", "其它", "/listen_list", None, False),
    Command("listen_off", "工具/系统", "/listen 列表｜/listen_on｜/listen_off｜/listen_scan", "关闭标签监听", False),
    Command("listen_on", "工具/系统", "/listen 列表｜/listen_on｜/listen_off｜/listen_scan", "开启标签监听", False),
    Command("listen_retry", "工具/系统", "/listen_failed —— 失败任务明细（/listen_retry 行id 重试｜/listen_failed del 行id 删除）", None, False),
    Command("listen_scan", "工具/系统", "/listen 列表｜/listen_on｜/listen_off｜/listen_scan", "标签监听立即扫描", False),
    Command("logpath", "监控查询", "/status｜/folder｜/logpath —— 状态/目录/日志", None, False),
    Command("manual", "工具/系统", "/manual —— 下载链路操作指引（完整机制文档）", "下载链路操作指引文档", False),
    Command("memo", "Pawchive", "/memo 内容 —— 备忘录：随手记；/memo 查看列表", "备忘录：/memo 内容 随手记；/memo 查看列表", False),
    Command("now", "工具/系统", "/now —— 系统现在在干什么（各链路实时状态）", "实时状态：/now 看系统现在在干什么", False),
    Command("origin", "工具/系统", "/origin —— 解析失败账本；/cd2ck 日志对账；/cd2check —— API 实对账", "查看评论来源解析失败账本（可溯源）", False),
    Command("paw", "Pawchive", "/paw（=status）—— 状态/失败画像/Cookie/扫描进度", "Pawchive：状态总览（失败画像/扫描进度）", True),
    Command("paw_archive", "Pawchive", "/paw_archive —— 归档明细；/paw_archive_del 行id|起-止", "Pawchive 归档明细/执行归档", True),
    Command("paw_att", "Pawchive", "/paw_att URL|ID|行id —— 附件外链状态", "Pawchive 帖子附件外链：/paw_att 行id|URL", True),
    Command("paw_backfill", "Pawchive", "/paw_backfill 作者 —— 回填历史帖（补站点后补的数据）", "Pawchive 回填作者历史帖", True),
    Command("paw_cookie", "Pawchive", "/paw_cookie Cookie —— 会话 Cookie", "Pawchive 设置 Cookie", True),
    Command("paw_csv", "Pawchive", "/paw_find 词；/paw_csv [作者]", "Pawchive 导出直链 CSV：/paw_csv [作者]", True),
    Command("paw_done", "Pawchive", "/paw_manual [作者|名字#id|export]；/paw_done 行id|起-止|作者", "Pawchive 标完成：/paw_done 行id|起-止|作者", True),
    Command("paw_fail", "Pawchive", "/paw_fail —— 非死链失败明细", "Pawchive 非死链失败明细", True),
    Command("paw_find", "Pawchive", "/paw_find 词；/paw_csv [作者]", "Pawchive 按名称查询：/paw_find 关键词", True),
    Command("paw_help", "其它", "/paw_help", None, True),
    Command("paw_manual", "Pawchive", "/paw_manual [作者|名字#id|export]；/paw_done 行id|起-止|作者", "Pawchive 待人工外链工作台", True),
    Command("paw_notify", "Pawchive", "/paw_notify on|off —— 帖子开始/完成通知开关", "帖子开始/完成通知开关：/paw_notify on|off", True),
    Command("paw_pause", "Pawchive", "/paw_retry 行ID|all；/paw_pause｜/paw_resume", "Pawchive 暂停下载 worker", True),
    Command("paw_plan", "Pawchive", "/paw_plan 作者 [all]；/paw_plan 作者 since 日期 [all]", "Pawchive 扫描作者入队：/paw_plan 作者 [all]", True),
    Command("paw_post", "Pawchive", "/paw_search 词；/paw_post URL|ID（可刷新补差）", "Pawchive 单帖获取/刷新：/paw_post URL|ID", True),
    Command("paw_pr", "Pawchive", "/paw_pr URL|ID|行id —— 执行详情报告", "Pawchive 帖子详情报告：/paw_pr 行id|URL", True),
    Command("paw_progress", "Pawchive", "/paw_progress 作者 —— 按作者进度（完成率/死链/待办）", "Pawchive 按作者进度：/paw_progress 作者", True),
    Command("paw_resume", "Pawchive", "/paw_retry 行ID|all；/paw_pause｜/paw_resume", "Pawchive 恢复下载 worker", True),
    Command("paw_retry", "Pawchive", "/paw_retry 行ID|all；/paw_pause｜/paw_resume", "Pawchive 重投失败帖：/paw_retry 行id|all", True),
    Command("paw_search", "Pawchive", "/paw_search 词；/paw_post URL|ID（可刷新补差）", "Pawchive 搜作者：/paw_search 关键词", True),
    Command("paw_since", "Pawchive", "/paw_since 日期|off —— 默认时间下限", "Pawchive 扫描时间下限：/paw_since 日期|off", True),
    Command("paw_status", "其它", "/paw_status", None, True),
    Command("pd", "Gumroad", "/pd <链接> [目录] —— 中转下载（列表/单文件均可，重跑即续传）", "pixeldrain 下载：/pd <链接> [目录]；裸命令看任务", True),
    Command("pdclean", "Gumroad", "/pdclean <链接> —— 清理云端中转（本地确认完好后再清）", "pixeldrain 清理云端中转：/pdclean <链接>", True),
    Command("pdjobs", "Gumroad", "/pdst [tag] —— 各任务进度；/pdjobs —— 任务列表", None, True),
    Command("pdst", "Gumroad", "/pdst [tag] —— 各任务进度", "pixeldrain 进度：/pdst [tag]；裸命令看全部", True),
    Command("pixiv", "工具/系统", "/pixiv <ID|用户名> —— 下载 Pixiv 画师作品（App API，用户名自动解析成 ID；裸命令看已跟列表）", "Pixiv 画师下载：/pixiv <ID|用户名>；裸命令看已跟列表", True),
    Command("progress", "监控查询", "/progress｜/downloading —— 下载进度", "查看进行中下载的实时进度", False),
    Command("queue", "监控查询", "/queue；/queue_del 序号 —— 队列/移除", "查看下载队列", False),
    Command("queue_del", "监控查询", "/queue；/queue_del 序号 —— 队列/移除", "从队列移除：/queue_del 序号", False),
    Command("report", "工具/系统", "/report —— 运维报告传 115（待人工/失败/重试/快照/台账/体检）", "运维报告传 115：/report [manual|failed|retry|queue|stats|chrome]", False),
    Command("restart", "工具/系统", "/restart —— 重启 bot（优雅停机，约 30 秒）", "重启 bot（优雅停机 + 自动拉起，约 30 秒）", False),
    Command("retry", "监控查询", "/retry；/retry_all；/retry_del 序号 —— 待重试", "查看待重试列表", False),
    Command("retry_all", "监控查询", "/retry；/retry_all；/retry_del 序号 —— 待重试", "重放全部待重试任务", False),
    Command("retry_del", "监控查询", "/retry；/retry_all；/retry_del 序号 —— 待重试", "从待重试移除：/retry_del 序号", False),
    Command("setcleartime", "工具/系统", "/clean｜/clearmsg｜/setcleartime 30s|1m|1h|off", None, False),
    Command("sh", "工具/系统", "/sh [命令]；/sh cd 目录 —— 命令行（黑名单纪律）", "命令行：执行 shell 命令，如 /sh ls", True),
    Command("sql", "工具/系统", "/sql 一条SQL —— 诊断控制台", None, False),
    Command("sqlt", "工具/系统", "/sqlt 列表｜/sqlt_add 名 SQL｜/sqlt_del 名｜/sqlt 名", "SQL 查询模板列表", False),
    Command("sqlt_add", "工具/系统", "/sqlt 列表｜/sqlt_add 名 SQL｜/sqlt_del 名｜/sqlt 名", "保存 SQL 模板：/sqlt_add 名字 SQL", False),
    Command("sqlt_del", "工具/系统", "/sqlt 列表｜/sqlt_add 名 SQL｜/sqlt_del 名｜/sqlt 名", "删除 SQL 模板：/sqlt_del 名字", False),
    Command("start", "监控查询", "/start —— 打开按钮菜单（给 bot 发任意文本同效）", None, False),
    Command("stats", "监控查询", "/stats [天数] —— 台账（默认1，上限7）", "台账：今日转发/解析/成功/失败汇总", False),
    Command("status", "监控查询", "/status｜/folder｜/logpath —— 状态/目录/日志", None, False),
    Command("thread", "下载自动行为", "/dedup [off]；/thread [3-20] —— 去重/并发", "查看/设置并行下载路数", False),
    Command("tme_backfill", "外链台账", "/tme_backfill —— 台账 t.me 链接回灌入队", None, False),
    Command("up", "工具/系统", "/up 路径 —— 上传到收藏夹", "上传文件到收藏夹：/up <路径>", True),
    Command("usage", "监控查询", "/usage —— 功能使用统计", "功能使用统计：各功能使用次数与频率", False),
    Command("wl", "工具/系统", "/wl 列表｜/wl_add ID或@名｜/wl_del ID或序号｜/wl_scan", "查看下载白名单", False),
    Command("wl_add", "工具/系统", "/wl 列表｜/wl_add ID或@名｜/wl_del ID或序号｜/wl_scan", "加入白名单：/wl_add ID或@用户名", False),
    Command("wl_del", "工具/系统", "/wl 列表｜/wl_add ID或@名｜/wl_del ID或序号｜/wl_scan", "移出白名单：/wl_del ID或序号", False),
    Command("wl_list", "其它", "/wl_list", None, False),
    Command("wl_scan", "工具/系统", "/wl 列表｜/wl_add ID或@名｜/wl_del ID或序号｜/wl_scan", "白名单立即扫描", False),
    Command("wl_since", "工具/系统", "/wl_since 聊天 消息id —— 回补存量", "回补存量：/wl_since 聊天 消息id", False),
    Command("wnacg", "工具/系统", "/wnacg <地址|aid> [作者] —— wnacg 漫画→115 漫画[/<作者>]→自动解压", "wnacg 漫画：/wnacg <地址>，下载→115→自动解压", True),
    Command("x", "工具/系统", "/x <@作者> —— 下载 X 作者作品（gallery-dl+Agent 登录态）", "X 作者作品下载：/x @作者；裸命令看已跟列表", True),
)
del Command  # 模块级只留 ALL 与查询函数（Command 类已完成使命）

_ANDROID_PREFIX_FALLBACK = ("chrome", "cd2", "bili", "pixiv", "paw", "wnacg",
                            "ehentai", "dyu", "extract", "shell", "asmr",
                            "pd", "115x")
_ANDROID_EXACT = ("x", "sh", "up", "gum")


def registered():
    """审计全集（name 无斜杠）——config.REGISTERED_COMMAND_NAMES 的源。"""
    return {c.name for c in ALL}


def panel_commands():
    """bot 命令面板清单 [(name, desc)]——bot.BOT_COMMANDS 的源。"""
    return [(c.name, c.panel_desc) for c in ALL if c.panel_desc]


def android_blocked(cmd):
    """该命令（无斜杠）是否应在 Android 端拦截。

    显式标记优先；未标记的命令沿用既有**宽前缀**语义（族代表 +
    startswith）——历史上 /pdst 一直被 /pd 前缀拦，属合法宽拦。"""
    for c in ALL:
        if c.name == cmd:
            return c.android_block
    return (cmd in _ANDROID_EXACT
            or any(cmd == p or cmd.startswith(p) for p in _ANDROID_PREFIX_FALLBACK))


def help_groups():
    """[(组名, [(name, help_line), …])]——/help 渲染的源，组序稳定。"""
    seen_groups = []
    by_group = {}
    for c in ALL:
        if c.group not in by_group:
            by_group[c.group] = []
            seen_groups.append(c.group)
        by_group[c.group].append((c.name, c.help_line or f"/{c.name}"))
    return [(g, by_group[g]) for g in seen_groups]


def get(name):
    """按名（无斜杠）查 Command；不存在返回 None。"""
    for c in ALL:
        if c.name == name:
            return c
    return None
