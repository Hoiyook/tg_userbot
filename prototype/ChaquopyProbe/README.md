# ChaquopyProbe：tg_userbot 的 Android 自带服务验证

这是 `docs/plan/AndroidApp可行性调研.md` 里 **Phase 2** 的验证工程，现在
已经不只是“探针”——**tg_userbot 核心已经能在 APK 内跑通**：

- 整个 `tg_userbot` 包在 Chaquopy 下 28/28 模块可导入；
- Telethon 用户客户端登录、3 条下载 worker 连接；
- 转发视频到 Saved Messages → 按命名规则下载到 App 私有目录；
- 真正的看板 `api_server` 在 `127.0.0.1:8765` 启动，WebView 正常渲染，
  台账「今日成功 1 / 1,056,089 字节」。

## 已验证（Android 15 arm64 模拟器）

| 项 | 结果 |
|---|---|
| Python | ✅ 3.12.1（APK 内嵌） |
| telethon | ✅ 1.45.0 |
| aiohttp | ✅ 3.10.10（Android wheel，arm64-v8a + x86_64） |
| httpx | ✅ 0.28.1 |
| python-socks | ✅ 3.2.1 |
| SQLite | ✅ 3.39.2，`journal_mode=wal` |
| tg_userbot 导入 | ✅ 28/28（含 app / commands / api_server / bot / listener） |
| Telethon 登录 | ✅ 用户 `dramjou0`，3 worker |
| Saved Messages 下载 | ✅ `downloads/宅次元同人馆NSFW/26-10-27 129166 ... .mp4` |
| 看板 API / WebView | ✅ `127.0.0.1:8765`，今日成功 1 |

### 三个坑（已修）

1. **`aiohttp.web` 不能属性访问**：aiohttp 3.10.10 的 `__getattr__` 不暴露
   `web` 子模块，必须 `from aiohttp import web`。
2. **`Python.start` 要放主线程**：`AndroidPlatform` 初始化依赖应用上下文；
   重活（import aiohttp/telethon）才放后台线程。
3. **模拟器 `adb reverse tcp:8765` 抢端口**：NagramShell 测试留下的规则会让
   探针/API 绑不上；先 `adb reverse --remove tcp:8765`。

## 首次启动（Onboarding，文件导入版）

真机首次使用不需要 adb：

1. 准备 `tg_secrets.json`（含 `api_id` / `api_hash`；缺 `api` 段时会自动补
   一个 loopback 看板配置 + 随机 token）和 `tg_downloader.session`；
2. 把两个文件放到手机（USB/网盘下载到 Downloads 均可）；
3. 打开 App → 初始化页：
   - ① 导入 `tg_secrets.json`
   - ② 导入 `tg_downloader.session`（校验 SQLite 头）
   - ③ 完成，进入主界面
4. 主界面 → 启动核心 → 打开看板。

**注意**：session 是账号 auth key，不能和 Mac bot 同时在线。先在 Mac 上
`./run.sh stop`，手机核心停掉后再 `./run.sh start`。

v0.11 起也支持手机号/验证码/2FA 登录（见下），不需要复制 Mac session。

## 手机号登录（v0.11）

不复制 Mac session 时，用手机号新建**独立 session**（可与 Mac bot 同时在线）：

1. 先导入 `tg_secrets.json`（API_ID/HASH；session 文件不需要）；
2. Onboarding 点「没有 session？用手机号登录」；
3. 输入手机号（含国家码）→ 发送验证码 → 输入 Telegram 验证码；
4. 如有两步验证，输入 2FA 密码；
5. 成功后 session 创建在 App 私有目录 → 完成 → 启动核心 → 打开看板。

验证码发到你账号的 Telegram；Mac bot 可以继续运行（这是独立 session）。
登录页已在模拟器验证渲染；实际发码/登录需要真机手机号与验证码。

## 构建

前置：JDK 17、Android SDK、网络。

```bash
cd prototype/ChaquopyProbe
./gradlew :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

- `tg_userbot/` 由 Gradle 的 `syncUserbot` 任务在构建前从仓库根同步到
  `app/src/main/python/tg_userbot/`（该目录已 gitignore，不要手改副本）。
- 本工程从 `NagramShell` 复制 Gradle wrapper；如果
  `gradle/wrapper/gradle-wrapper.jar` 缺失，执行：

```bash
cp prototype/NagramShell/gradle/wrapper/gradle-wrapper.jar \
   prototype/ChaquopyProbe/gradle/wrapper/
chmod +x prototype/ChaquopyProbe/gradlew
```

## 模拟器上验证核心（会短暂停 Mac bot）

因为用的是同一份 Telethon auth key，**测试期间必须停掉 Mac bot**：

```bash
./run.sh stop

# 凭据与 session 推进 App 私有目录
adb push tg_secrets.json /data/local/tmp/
adb push ~/tg_downloader.session /data/local/tmp/
adb push prototype/ChaquopyProbe/android_env.json /data/local/tmp/
adb shell run-as local.nagram.chaquopyprobe cp /data/local/tmp/tg_secrets.json files/
adb shell run-as local.nagram.chaquopyprobe cp /data/local/tmp/tg_downloader.session files/
adb shell run-as local.nagram.chaquopyprobe cp /data/local/tmp/android_env.json files/

# 代理：模拟器经 adb reverse 直通 Mac 的 Hiddify（android_env.json 里写 127.0.0.1:12334）
adb reverse tcp:12334 tcp:12334
```

打开 App：

1. 等探针页加载（环境 + tg_userbot 导入审计）；
2. 点 **启动核心** → Telethon 登录、3 条 worker；
3. 点 **打开看板** → 真正的看板（带 token）；
4. 从手机/电脑 Telegram 往 Saved Messages 发媒体，看板「今日成功」+1。

收尾（重要）：

```bash
adb shell am force-stop local.nagram.chaquopyprobe
adb shell run-as local.nagram.chaquopyprobe rm -f \
  files/tg_downloader.session files/tg_secrets.json files/android_env.json
adb reverse --remove tcp:12334
./run.sh start
```

## 配置

- `android_env.json`（gitignore，可自定义）：环境变量表，模拟器示例
  `{"TG_PROXY": "socks5://127.0.0.1:12334"}`。真机上如果系统 VPN 已接管流量，
  可以不配；如果本机有代理 App，填它的本地端口。
- `tg_secrets.json` / session 只放 App 私有目录，不入库、不进 APK。
- 下载目录固定为 App 私有 `files/downloads/`（暂未接 SAF/公共 Downloads）。

## Android 能力守卫（v0.10）

App 启动时设置 `TG_ANDROID=1`，主包据此：

- `routing`：B站 / X / iwara / 种子不再自动下载，落外链台账；
- 命令：`/chrome* /cd2* /bili /x /pixiv /paw* /wnacg /ehentai /dyu* /extract /shell /sh /up` 直接回「Android 端不支持」；
- API：`/api/backup`、`/api/chrome` 返回友好文本，不再 import CD2/Chrome；
- 看板：隐藏「备份到 115」「Chrome Agent」卡片。

Mac 端（没有 `TG_ANDROID`）行为完全不变。

## 公共存储（v0.12）

- Onboarding 点「授权存储（公共 Downloads）」：Android 11+ 授予「所有文件访问」
  （`MANAGE_EXTERNAL_STORAGE`），Android 10 授 `WRITE_EXTERNAL_STORAGE`；
- 已登录用户在主界面点「存储授权」；授权后必须点「重启App」让
  `config.DOWNLOAD_DIR` 重新 import，否则仍用旧目录；
- 授权后媒体落 `/sdcard/Download/Nagram/<来源>/`，SQLite/日志仍在 App 私有目录
  （`files/runtime/`）；
- 主界面「扫描媒体」按钮触发 MediaScanner，把公共目录的媒体索引进相册；
- v0.15：授权后自动把 App 私有 `files/downloads` 里的旧媒体迁移到公共目录
  （复制成功才删源，重名自动加 `(1)`），迁移完自动扫描入库；
- 未授权时回落到 App 私有 `files/downloads`；
- `MANAGE_EXTERNAL_STORAGE` 只适合侧载个人 App，不上 Google Play；将来要上架
  需改 SAF/MediaStore。

## 后台保活/开机自启（v0.14）

- 前台服务类型 `specialUse`（Android 14+），旧系统回落 `dataSync`——
  避开 Android 15 对 dataSync 的 6 小时/24 小时上限；
- `BootReceiver`：开机且 secrets/session 就绪时自动拉起核心；
- 主界面「电池白名单」：引导加入忽略电池优化；
- 开机自启的核心已在跑时，打开 App 会跳过探针并直接打开看板。

注意：`specialUse` 是侧载个人 App 用法，Google Play 需要审核理由；国产 ROM
（小米/华为/三星）仍需手动允许自启/后台；若厂商拦截 `BOOT_COMPLETED`，
手动打开一次 App 也会恢复。

## 已知边界

- **桌面专属功能不可用**：Chrome Agent（iwara/`/chrome`/Pawchive）、
  CD2/115 离线与备份、浏览器 Cookie 导入、yt-dlp/gallery-dl/ffmpeg/bsdtar/7z
  外部二进制。v0.10 起 `TG_ANDROID` 守卫已隐藏看板卡片并拦截对应命令/路由。
- **Onboarding**：文件导入 + 手机号/验证码/2FA 登录（v0.11）都已支持；
  bot token / 代理的可视化设置界面还没做（当前随 `tg_secrets.json` /
  `android_env.json` 提供）。
- **存储**：v0.12 起可授权公共 Downloads（`MANAGE_EXTERNAL_STORAGE`，侧载）；
  未授权时回落到 App 私有目录。上架/多用户场景仍需 SAF/MediaStore。
- **后台**：v0.14 已做 `specialUse` 前台服务 + BootReceiver + 电池白名单入口；
  国产 ROM 的额外自启限制仍需用户手动放行。
- **debug APK**：未做 release 签名、正式包名、图标/名称。

## 下一步

1. **Onboarding**：文件导入 + 手机号登录已完成（v0.11）；下一步加
   bot token / 代理的可视化设置界面。
2. **Android 能力开关**：✅ v0.10 已完成（`TG_ANDROID=1`：路由/命令/API/
   看板四处统一守卫）。
3. **存储**：v0.12 已支持公共 Downloads（MANAGE）+ 扫描入库；上架再改 SAF。
4. **Telegram bot 菜单**：bot token 登录 bot 客户端，启用按钮面板。
5. **后台保活**：✅ v0.14 已完成（specialUse + BootReceiver + 电池白名单）；
   OEM 自启限制仍需用户在系统设置里放行。
6. **正式打包**：release 签名、版本号、图标、README/分发说明。
