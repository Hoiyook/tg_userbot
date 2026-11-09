# tgUserbot 做成 Android 自带服务 App：可行性调研

> 日期：2026-11-09
> 范围：把现有 Python userbot（Telethon + aiohttp + SQLite）跑到 Android 手机上，服务与 UI 都在手机本地，不依赖 Mac。
> 说明：本次联网检索被会话权限拦截，Android 版本细节以官方文档/真机复核为准；代码侧结论均来自仓库实证（附 file:line）。

---

## 一、结论

**核心 userbot 可以做成手机自带服务；但“完整功能自包含”做不到。**

- ✅ **可以**：Telegram 消息监听、Saved Messages 媒体下载、下载队列、SQLite、看板 API、bot 菜单、白名单/标签监听、去重/命名/清理、抖音链接转解析 bot、B 站（yt-dlp）、X/Pixiv（gallery-dl）、asmr/pixeldrain 等 HTTP 链路。
- ❌ **做不到（手机本地）**：Chrome Agent（macOS Chrome + CDP）、CloudDrive2 + 115 离线、Pawchive 的 Chrome 下载、X 的浏览器 Cookie 导入、`/chrome` 系列、`/cd2*` 系列、种子离线。
- ⚠️ **真正的难点不是 Python 能不能跑，而是 Android 后台保活、外部二进制、原生依赖和存储分区。**

三条现实路线：

| 路线 | 周期 | 功能完整度 | 适合谁 |
|---|---|---|---|
| A. Termux + 现有 WebView 壳 | 1–2 周 | 80–90%（除 Chrome/CD2） | 想最快在手机上跑起来 |
| B. 单 APK 内嵌 Python（Chaquopy） | 3–6 周核心，2–3 月完善 | 60–80% | 想要“一个 App、无 Termux” |
| C. Kotlin/TDLib 原生重写 | 6+ 月 | 可控但巨大 | 不推荐 |

---

## 二、现有资产（比想象中更接近）

1. **项目本来就有 Termux 分支**
   - `config.is_termux()` / `IS_TERMUX`（`tg_userbot/config.py:66-73`）
   - Termux 默认数据根 `/storage/emulated/0/Download/Nagram`，`DOWNLOAD_DIR == DATA_ROOT`（`config.py:108-115`）
   - Termux 上**强制关闭抖音本地解析**，走解析 bot（`config.py:608-612`）
2. **Android 壳已经存在**：`prototype/NagramShell`
   - Kotlin + Compose，minSdk 29 / targetSdk 35（`app/build.gradle.kts:9-14`）
   - WebView 载看板 + 双服务器地址（家里/外出）+ 系统分享接收（`MainActivity.kt`、`ShareReceiverActivity`）
   - 已通过 `RUN_COMMAND` 调 Termux 跑脚本（`ScriptsScreen.kt:116-145`）
   - **但它只是客户端**：没有前台服务、没有开机自启、没有本地 Python
3. **看板是 PWA/WebView**：服务一旦跑在手机本地，直接打开 `http://127.0.0.1:8765` 即可，不需要重写 UI。
4. **run.sh 是桌面脚本**：默认 `/Volumes/V1`、`.venv`，Termux 上要另写服务脚本（`run.sh:1-60`）。

---

## 三、能力矩阵（手机本地）

| 功能 | 依赖 | Termux | 单 APK 内嵌 | 说明 |
|---|---|---|---|---|
| Telegram userbot / bot / 下载队列 | telethon、aiohttp、sqlite3 | ✅ | ✅ | 核心；纯 Python + SQLite |
| Saved Messages 媒体下载 | telethon | ✅ | ✅ | 走 worker 池 |
| 看板 / API / PWA | aiohttp | ✅ | ✅ | 本地 127.0.0.1:8765 |
| 抖音链接 | 解析 bot（Termux 已强制） | ✅ | ✅ | 本地 f2 在 Termux 被禁用 |
| B 站 | yt-dlp + ffmpeg | ✅（pkg 安装） | ⚠️ 需打包二进制 | 代码里路径写死 `.venv/bin/yt-dlp`（`bili_dl.py:53-54`） |
| X / Pixiv | gallery-dl | ✅（pip） | ⚠️ 可打包（纯 Python） | X 还需要 Cookie；路径写死 `.venv/bin/gallery-dl`（`x_dl.py:34-35`） |
| asmr / pixeldrain / ehentai(HTTP) | httpx/requests | ✅ | ✅ | 纯 HTTP |
| 解压 | bsdtar / 7z / ffmpeg | ✅（pkg 安装） | ⚠️ 需打包二进制 | `extract_util.py:138-254` |
| **iwara** | Chrome Agent（CDP） | ❌ | ❌ | `chrome_agent._CHROME_CANDIDATES` 只有 macOS Chrome（`chrome_agent.py:70-84`） |
| **`/chrome` 系列** | 桌面 Chrome | ❌ | ❌ | 桌面专属 |
| **Pawchive** | Chrome Agent | ❌ | ❌ | 附件直链靠 Chrome 下载 |
| **X 浏览器 Cookie 导入** | browser_cookie3 + 桌面浏览器 | ❌ | ❌ | 手机无桌面 Chrome 配置；可改手动粘贴 |
| **CD2/115 离线、种子、备份** | CloudDrive2 二进制 + gRPC | ❌ | ❌ | `cd2.py` / `cd2_api.py` / `torrent_offline.py` |
| **wnacg / ehentai 种子路** | CD2/115 | ❌ | ❌ | `wnacg.py:179`、`ehentai.py:189` |
| 代理 | python-socks | ✅ | ✅ | 手机需另装代理 App（Hiddify/Clash 等） |
| SQLite WAL | stdlib sqlite3 | ✅ | ✅ | 必须放应用私有目录，不能放 `/sdcard` FUSE |

**核心依赖的 Android 可行性**：telethon（纯 Python）、aiohttp（有 C 扩展，但 Termux/Chaquopy 都有成熟轮子）、httpx（纯）、python-socks（纯）、pyaes/rsa（纯）。`grpcio` / `curl_cffi` / `pycryptodomex` / `pillow` 等原生包是内嵌 APK 的主要麻烦；核心链路可以完全避开。

---

## 四、路线对比

| 维度 | A. Termux + 壳 | B. Chaquopy 单 APK | C. Kivy/BeeWare | D. Kotlin/TDLib |
|---|---|---|---|---|
| 单 App | 否（Termux + 壳） | 是 | 是 | 是 |
| Python 生态 | 完整 | 大部分（原生包受限） | 大部分 | 无 |
| 外部二进制 | pkg 安装 | 需按 ABI 打包/禁用 | 需打包 | 无 |
| 后台长驻 | termux-wake-lock + Termux:Boot | 前台服务 + 通知 + 电池白名单 | 需自写 Service | 原生可控 |
| 构建复杂度 | 低 | 中高（Gradle + Python + 原生包） | 高（UI 重写） | 极高 |
| APK 体积 | 壳很小 | 50–100MB | 50MB+ | 20–40MB |
| 功能完整度 | 最高（除 Chrome/CD2） | 中（需砍原生依赖） | 低（UI 重写） | 需重写全部业务 |
| 分发 | Termux 不在 Play；侧载/F-Droid | 侧载；Play 大概率拒 | 侧载 | 侧载 |
| 推荐度 | ⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐ | ❌ |

---

## 五、Android 的硬限制（必须提前接受）

1. **后台不是“装了就常驻”**
   - Android 8+ 需要前台服务 + 常驻通知；Android 12+ 限制从后台启动前台服务；Android 13+ 需要 `POST_NOTIFICATIONS`；Android 14+ 要求声明 `foregroundServiceType`；Android 15 对 `dataSync` 类型有 6 小时/24 小时上限（以官方文档为准）。
   - 国产 ROM（小米/华为/三星）还会额外杀后台，必须让用户把 App 设为“无限制/允许后台”。
   - 结论：无论哪条路线，都要在首次启动引导用户做电池优化白名单。
2. **Termux 的额外坑**
   - Termux 不在 Google Play（2020 年起），需 F-Droid/GitHub。
   - `RUN_COMMAND` 需要 Termux 设置 `allow-external-apps=true` + 授予壳 App `com.termux.permission.RUN_COMMAND`。
   - `termux-wake-lock` 保活；`Termux:Boot` 开机自启。
   - Android 12+ phantom process killer 会杀后台子进程（Python/yt-dlp/ffmpeg 都算），可能需要 ADB 关闭 `settings_enable_monitor_phantom_procs` 或接受被 kill。
3. **存储分区**
   - 写 `/sdcard/Download/Nagram` 需要 Termux `termux-setup-storage`；单 APK 需要 SAF 或 `MANAGE_EXTERNAL_STORAGE`（后者 Play 政策严格，侧载可用）。
   - **SQLite WAL 必须放应用私有目录**（Termux home 或 APK 内部存储），不能放 FUSE 的 `/sdcard`；项目已有此警告（`config.py:459-461`）。
4. **原生依赖**
   - `grpcio`、`curl_cffi`、`pycryptodomex`、`pillow`、`lz4` 等原生包在 Chaquopy 上不一定有轮子；核心链路应避开，CD2/f2 等功能按需砍。
5. **分发与合规**
   - Termux 不在 Play；单 APK 的 Telegram userbot 很可能被 Play 以自动化/滥用类政策拒绝，现实分发是侧载/F-Droid。
   - Telegram 对 userbot/自动化本身是灰区；与现在在 Mac 上跑没有本质区别，但公开分发风险更高。
6. **电量/流量**
   - 长连接 + 下载 + 后台保活，耗电明显；用户需要接受。

---

## 六、推荐路线（分阶段）

### Phase 0：Termux 真机验证（1–2 天）

目的：用最低成本验证“核心链路在手机上到底能不能跑”，拿到真实的失败清单。

```bash
pkg install python git ffmpeg libarchive p7zip
pip install telethon aiohttp httpx python-socks
git clone <repo> && cd tg_userbot
export TG_SAVE_FOLDER=/sdcard/Download/Nagram
export TG_RUNTIME_DB=$HOME/tg_downloader/tg_userbot.db
export TG_PROXY=socks5://127.0.0.1:<代理端口>   # 按手机代理 App 实际端口
termux-wake-lock
python tg_userbot_final.py
```

验证清单：收藏夹媒体下载、抖音链接转解析 bot、B 站（yt-dlp）、看板 `127.0.0.1:8765`、白名单/标签监听、SQLite 重启恢复、代理。

### Phase 1：Termux + 壳整合（1–2 周）

- 新增 `scripts/termux_service.sh {start|stop|status}`（替代桌面 `run.sh`）。
- `NagramShell` 增加“启动/停止服务 / 打开本地看板”按钮，走 `RUN_COMMAND`。
- 加 `Termux:Boot` 自启脚本；首次启动引导电池白名单。
- Android 配置档（`is_termux`）：
  - 禁用 Chrome Agent / CD2 / Pawchive / 种子离线 / iwara；
  - 关闭浏览器 Cookie 导入，改为粘贴 Cookie；
  - `YTDLP` / `GALLERY_DL` 从写死 `.venv/bin/*` 改为 `shutil.which` / `sys.executable -m`；
  - 媒体走 `/sdcard/Download/Nagram`，DB 走 Termux home。
- 结果：手机上可日常使用，除桌面专属功能外功能完整。

### Phase 2（可选）：Chaquopy 单 APK（3–6 周核心）

- Kotlin 前台服务持有 Python 运行时（Chaquopy），WebView 载本地看板。
- 第一版只做核心：Telegram、下载队列、SQLite、看板、白名单/监听、抖音转 bot。
- 逐步加：yt-dlp/gallery-dl（纯 Python，可打包）；ffmpeg/bsdtar/7z（按 ABI 打包或先禁用）。
- 砍掉/远程化：Chrome Agent、CD2/115、Pawchive、种子离线、浏览器 Cookie 导入。
- 必须做：前台服务 + 通知、开机自启、电池白名单引导、应用私有存储、崩溃恢复。

### Phase 3（不推荐）：Kotlin/TDLib 原生重写

TDLib 官方支持用户账号，但等于重写 46k 行业务；除非要做商业级 App，否则不值。

---

## 七、需要改的具体代码点（Phase 0/1）

| 位置 | 问题 | 改法 |
|---|---|---|
| `tg_userbot/bili_dl.py:53-54` | `YTDLP` 写死 `../.venv/bin/yt-dlp` | `shutil.which("yt-dlp")` 或 `sys.executable -m yt_dlp` |
| `tg_userbot/x_dl.py:34-35` | `GALLERY_DL` 写死 `../.venv/bin/gallery-dl` | 同上 |
| `run.sh` | 桌面路径 `/Volumes/V1`、`.venv` | 新增 `termux_service.sh` |
| `chrome_agent.py:70-84` | 只找 macOS Chrome | Android 上直接禁用相关功能 |
| `cd2.py` / `cd2_api.py` | CD2 二进制 + gRPC | Android 上禁用；或改为远程 CD2 地址 |
| `browser_cookies.py` | 读桌面浏览器 Cookie | Android 上改为手动粘贴 |
| `config.py` | Termux 路径/DB 已有 | 补 Android 配置档（禁用列表） |
| `NagramShell` | 只有 WebView + 脚本 | 加服务启停、前台通知、Boot 自启 |
| `extract_util.py` | 依赖 bsdtar/7z | Termux 安装；APK 内嵌需打包 |

---

## 八、待真机验证的问题

1. 目标手机的 Android 版本/OEM（小米/华为/三星后台策略差异极大）。
2. Termux 前台 + Python + yt-dlp/ffmpeg 子进程能否稳定过夜（phantom process killer）。
3. 手机代理 App（Hiddify/Clash）能否给 Python 提供稳定的本地 SOCKS 端口；VPN 模式下是否还需要 `TG_PROXY`。
4. `/sdcard/Download/Nagram` 的读写权限与媒体扫描（相册可见性）。
5. SQLite WAL 在 Termux home 的稳定性；断电容灾。
6. B 站/X 的 Cookie 在手机上的手动维护成本。
7. 单 APK 方案下 aiohttp 等 C 扩展在目标 ABI 的可用性（Chaquopy 包仓库）。

---

## 九、建议

**先做 Phase 0（Termux 真机验证），不要一上来写 APK。** 这一步 1–2 天就能回答“手机上到底哪些能用”，而且现有代码几乎不用改。验证通过后，Phase 1 的 Termux + 壳就能满足“手机自带服务”的日常使用；如果确实不能接受 Termux，再投入 Phase 2 的 Chaquopy 单 APK，但要接受功能缩水和 3–6 周的工程量。

---

## 十、验证结果（2026-11-09）

已用 `prototype/ChaquopyProbe/`（Chaquopy 15.0.1）在 **Android 15 arm64 模拟器**上跑通单 APK 核心链路：

| 项 | 结果 |
|---|---|
| Python | ✅ 3.12.1（APK 内嵌） |
| telethon | ✅ 1.45.0 |
| aiohttp | ✅ 3.10.10（Android wheel，arm64-v8a + x86_64） |
| httpx | ✅ 0.28.1 |
| python-socks | ✅ 3.2.1 |
| SQLite | ✅ 3.39.2，`journal_mode=wal`（应用私有目录） |
| HTTP 服务 | ✅ aiohttp @ 127.0.0.1:8765，WebView 正常渲染 |
| 桌面依赖 | ❌ 本探针不含 Chrome Agent / CD2（预期内） |

**结论：单 APK（Chaquopy）路线对“核心 userbot + 看板 + SQLite”是可行的**，最大风险（aiohttp 无 Android wheel）已排除。

两个实现坑：
1. aiohttp 3.10.10 必须 `from aiohttp import web`，不能 `aiohttp.web`；
2. 模拟器上 NagramShell 测试留下的 `adb reverse tcp:8765 tcp:8765` 会抢占设备端口，探针绑不上；先 `adb reverse --remove tcp:8765`。

**核心移植（同日完成）**：`tg_userbot` 已搬进 `prototype/ChaquopyProbe/app/src/main/python/`（Gradle 构建前从仓库根同步），并验证：

- 28/28 模块导入；
- Telethon 登录（用户 `dramjou0`）+ 3 条下载 worker；
- 转发媒体到 Saved Messages → 按命名规则落盘到 App 私有 `downloads/`；
- 真正的 `api_server` 看板在 `127.0.0.1:8765` 启动，WebView 渲染，台账「今日成功 1 / 1,056,089 字节」；
- 首次启动 Onboarding：文件导入 `tg_secrets.json` + `tg_downloader.session`，以及手机号/验证码/2FA 登录（v0.11，新建独立 session，可与 Mac bot 同时在线）。

已完成 `TG_ANDROID` 能力守卫：桌面路由落台账、桌面命令拦截、API 对 CD2/Chrome 返回友好文本、看板隐藏对应卡片。

公共存储（v0.12）：授权「所有文件访问」后媒体落 `/sdcard/Download/Nagram/<来源>/`，SQLite/日志仍在 App 私有；主界面「扫描媒体」触发 MediaScanner 入库。SAF 留给上架/多用户场景。

后台（v0.14）：`specialUse` 前台服务（避开 Android 15 dataSync 6h 上限）+ BootReceiver + 电池白名单入口。

剩余：bot token/代理可视化设置、release 签名。
