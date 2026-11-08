# ASMR-300 接入调研报告与实施任务书

> 2026-10-26 实地调研（浏览器 + curl 逐项验证），目标：`我发送 https://www.asmr-300.com/work/RJ01207846 → userbot 自动下载到 115`。

## 一、站点本质（调研结论）

**asmr-300.com 是 Kikoeru（asmr.one 同款开源音声库前端）的镜像站**，SPA 壳 + 独立 JSON API，**不需要浏览器渲染，也不需要登录**。域名矩阵（三台机器各司其职）：

| 域名 | 作用 | 直连(国内网络) | 语言拦截 |
|---|---|---|---|
| `www.asmr-300.com` | SPA 页面壳（无内容） | ❌ 超时，必须走代理 | ✅ 拦 `Accept-Language: en-*`（403 反爬页 "remember, no english"） |
| `api.asmr-200.com` | **JSON API（元数据+文件树）** | ❌ 超时，必须走代理 | ❌ 不拦（实测无 zh 头也放行） |
| `raw.kiko-play-niptan.one` | **文件直链 CDN** | ✅ **直连可用** | ❌ 不拦；支持 Range 断点；**不支持 HEAD**（超时，预检须用 Range GET） |

**拦截机制实测**：同一 UA 下 `Accept-Language: zh-CN,zh;q=0.9` → 200 正常页；`en-US,en;q=0.9` → 403 拦截页。带上 zh 头即完全正常（用户浏览器本就是 zh 环境，该拦截只影响英文环境的脚本/无头浏览器）。

## 二、API 协议（全部实测通过）

```
① 元数据  GET https://api.asmr-200.com/api/work/{数字id}
   → { id, title, circle:{name}, source_id:"RJ01207846", tags[], vas[], release, ... }
② 文件树  GET https://api.asmr-200.com/api/tracks/{数字id}
   → [{type:"folder",title,children:[…]} | {type:"audio|image|text", title, hash, size, mediaDownloadUrl, mediaStreamUrl}]
```

- **ID 归一**：`/work/RJ01207846` → 去 `RJ` 前缀 → `01207846`（有无前导零都认，老 6 位 RJ 同样支持，如 RJ398337 → 398337 实测 200）。API 拒绝带 `RJ` 前缀（400 Invalid value）。
- 叶子节点的 **`mediaDownloadUrl` 是带完整目录结构的直链**（`https://raw.kiko-play-niptan.one/media/download/daily/<日期>/RJ号/<目录/…/文件名>`，URL 已百分号编码），**按它逐文件下载即可原样还原目录树**。
- **`size` 字段是精确字节数** → 天然对账依据（下载完核对，无需 HEAD——CDN 不支持 HEAD）。
- **无整包 zip 端点**（/api/work/{id}/zip 404、/api/zip/{id} 401）→ 逐文件是唯一形态。
- 文件树可能有根层散文件（不止文件夹），遍历要兼容。
- 作品常见**多版本目录**（本例 `wav(无SE)/` 与 `wav(含SE)/` 内容互为变体，各 2.3GB）——是否全量下载要给用户选择。

**测速**（本机 Hiddify）：raw CDN 走代理 ~11.8 MB/s、直连 ~5.8 MB/s，均支持 Range 断点续传。

## 三、命令设计（克隆 /wnacg 心智模型）

```
/asmr <asmr-300.com/work/RJxxxxx URL | RJ号 | 数字id> [only <目录关键词>]   入队下载
/asmr                     队列状态（各状态计数 + 最近任务 + 当前进行中）
/asmr retry <RJ号|all>     重投 FAILED
/asmr del <RJ号>           移除
/asmr stop ｜ /asmr start   暂停/恢复领取（在途跑完）
```

- `only <关键词>`：只下文件树里**路径命中关键词的子目录/文件**（例：`/asmr RJ01207846 only 含SE` 只下 2.3GB 那一版）；缺省全量。
- RJ 号主键判重（重复发链接直接回执已有，同 wnacg 的 aid 语义）。
- 落盘：`/115open/声音/asmr/RJ号/`（115 现有约定已确认：`声音/asmr/RJ1218390/…` 同构），目录树按 API 原样还原。

## 四、模块设计（asmr_dl.py，骨架抄 wnacg.py）

```
/asmr 命令
  ↓ parse（URL/RJ号 → 数字 id）
API 元数据 + 文件树（httpx，⚠ 必须走 TG_PROXY 同款代理 + Accept-Language: zh-CN）
  ↓ 入库 asmr_tasks（schema +1：RJ 主键、status、title、circle、files JSON、
    total/done_files、size_bytes、only_filter、error）
常驻 worker（串行逐文件，克隆 wnacg 纪律）
  ├─ 逐文件 GET mediaDownloadUrl（raw CDN：代理优先、直连回落；Range 断点 .part 续传）
  ├─ 写 CD2 挂载 声音/asmr/RJ号/<相对路径>（mount_of 需扩展：/115open/声音/… 映射）
  ├─ 下载完按 API size 对账 → rename 就位 → done_files+1（断点重启天然续跑）
  └─ 全部完成 → gRPC FindFileByPath 抽查对账 → notify（含总大小/文件数/115 位置）
```

接线清单（**新增命令的 /help 维护纪律**，CLAUDE.md 硬性要求）：
`commands.py` 分发 + `config.REGISTERED_COMMAND_NAMES` + `bot.BOT_COMMANDS` + `config.MENU_ACTIONS`（若带按钮）+ `_COMMAND_SUBS`（asmr 的 retry/del/stop/start）+ `/help` 文案 + `docs/操作手册.md` + `/now` 视图（_CURRENT 同款）+ `/report queue` 采集器加一行 asmr 存量。

**边界与风险**：
1. 元数据请求**必须走代理**（直连超时）——复用 wnacg 的代理纪律；忘带 zh 头会被 www 拦（API 实测不拦，但统一带上，防镜像策略收紧）。
2. raw CDN **不支持 HEAD**——死链预检用 `Range: bytes=0-0`（同 pawchive temp_url 的坑）。
3. 直链 URL 里嵌了首次入库日期（`daily/2024-07-26/`），**不要自己拼 URL**，一律取 API 返回的 `mediaDownloadUrl` 原文。
4. 站点是社区公益库：串行 + 文件间节流 3-5s（wnacg 同款），不做高并发。
5. 同一作品重复入库风险为零（RJ 主键）；已存在目录时幂等跳过尺寸一致的文件。
6. 大作品（>4GB）下载耗时以十分钟计 → 任务全持久化 SQLite，重启续跑（.part 保留）。

## 五、本次实测记录（RJ01207846）

- 41 文件 / 4.55GB（wav 两版各 2.3GB + 插图 + 剧本），API 全部就绪。
- 脚本 `/tmp/asmr300/download.py`（幂等断点）已把全作下载到 115 `/声音/asmr/RJ01207846/`——即本任务书的活体验证：**协议、直链、断点、对账全链路跑通**。
