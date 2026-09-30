# tg_userbot 115 解压回传任务书

**需求**：拉取 115 指定目录下的压缩包到本地 → 解压 → 把解压产物上传回原 115 目录。
**结论：可行**。四道关（挂载读 / 挂载写→上传 / gRPC 对账 / 删除传播）2026-09-30 真机全部实测通过，见 §9。
**性质**：新功能，复用现有部件，不做无关重构。

## 1. 数据流

```
/115x <115目录>
   │ 扫描目录顶层压缩包（zip/rar/7z/tar 系）
   ▼
extract_tasks 表（PENDING，唯一键 dir+name+size 幂等）
   │ worker 串行 claim（租约 + 退避，照 pawchive 模式）
   ▼
① COPY      挂载读 → 本地 staging（staging 已有同名同尺寸 → 跳过，拷贝幂等）
② EXTRACT   复用 pawchive 解压链（zip 加密位/炸弹守卫/staging 改名；rar bsdtar
            探测；新增 cp437→GBK 文件名修复）；加密/炸弹 → TERMINAL
③ UPLOAD    staging 逐文件 cp 回挂载：<原目录>/<压缩包名>/（逐文件，带进度）
④ VERIFY    CD2 上传任务清空（tasks_reply）+ list_remote_dir 逐文件尺寸对账
   ├─ 全对上 → COMPLETED（本地 staging 清理）
   ├─ 对不上 → FAILED（附逐文件差异清单，退避重试）
   └─ 终端性（需密码/炸弹/包损坏）→ TERMINAL（明确通知，不再重试）
```

## 2. 关键设计决策

1. **走 FUSE 挂载，不走 CD2 gRPC 传输 RPC**。挂载点 `/Volumes/CloudDrive`
   即 115 根（映射：gRPC `/115open/X` ↔ 挂载 `/Volumes/CloudDrive/X`）。
   读 = 普通文件读（CD2 负责下载缓存），写 = 普通文件写（CD2 负责上传）。
   实测读写吞吐满足需求，代码量比封装 gRPC 传输 RPC 小一个量级，且天然
   支持目录树整体回传。
2. **上传回 `<原目录>/<压缩包名>/` 子目录**（非平铺）。理由：同名冲突免疫、
   天然防环（解压产物里的压缩包落在子目录，顶层扫描永不命中）、115 上
   结构清晰。命令留 `flat` 参数可平铺（用户自担同名冲突风险）。
3. **原压缩包保留，绝不删远端源**（数据准确性优先）。完成通知里提示可手删。
4. **只处理目标目录顶层压缩包**，不递归子目录（防环 + 语义清晰）。
5. **串行处理，一次一包**。115 上传带宽共享（pawchive 也在传），串行可预期。

## 3. SQLite（runtime_db.py，v11 → v12）

新表 `extract_tasks` 进 `_SCHEMA` + `if version < 12` 迁移 + 版本号：

```sql
extract_tasks (
  id INTEGER PK,
  remote_dir TEXT,          -- 115 目录（gRPC 形态 /115open/...）
  archive_name TEXT,
  archive_size INTEGER,
  status TEXT,              -- PENDING/PROCESSING/UPLOADING/COMPLETED/
                            -- FAILED/TERMINAL
  staging_dir TEXT,         -- 本地 staging 绝对路径（失败保留可续）
  uploaded_files INTEGER, uploaded_bytes INTEGER,   -- 进度展示
  attempts INTEGER, next_retry_at REAL, lease_until REAL,
  error TEXT, created_at REAL, updated_at REAL,
  UNIQUE(remote_dir, archive_name, archive_size)    -- 重发命令幂等
  + idx (status, next_retry_at)
)
```

读写助手照抄 `pawchive_posts` 模式（enqueue / claim / mark_uploaded /
verify_fail / complete / retry / release_expired / list_all / counts）。
SQL 只在 runtime_db.py，挂载 I/O 与 gRPC 不进事务。

## 4. 模块拆分

### 4.1 extract_util.py（新，从 pawchive_worker 抽出）

- 把 `_extract_archive_sync(archive_path)` 抽为
  `extract_archive(archive_path, dest, delete_source=False) -> (status, detail, files)`
  ：dest 参数化（原版固定同名目录）、删源行为参数化（115x 场景源是远端文件，
  本地副本不删）、返回解压产物文件清单（UPLOAD 步要逐文件回传）。
  **pawchive_worker 改为薄委托**，行为零变化（回归保障）。
- 新增 zip cp437→GBK 文件名修复：`flag_bits & 0x800` 未置位时，文件名按
  cp437 读出 → 尝试 `name.encode("cp437").decode("gbk")` 成功即替换
  （Windows 中文 zip 的乱码修复）。**只在新链路生效，pawchive 不动**。
- rar：沿用 bsdtar 列表探测（libarchive 3.7.4 支持 rar4+rar5 读，本机无需
  装 unrar）；探测失败 = 需密码/损坏 → "password"。

### 4.2 extract_worker.py（新）

- 常驻循环（挂 app 服务任务列表，RUNTIME_DB_READY 守卫，shutdown release）：
  claim → 挂载存在性检查 → ①②③④ → 终态。
- 挂载映射助手 `mount_of(remote_dir)`：`/115open/X` →
  `/Volumes/CloudDrive/X`（`config.CLOUD_MOUNT_BASE`，启动时校验挂载在，
  不在 → 任务保持 PENDING + 节流告警，不烧重试次数）。
- COPY：`shutil.copyfile`（读挂载走 CD2 缓存）到
  `staging/<包名>/archive.<ext>`；staging 已有同名同尺寸 → 跳过（幂等）。
- 磁盘预检：包大小 + 压缩比估算 vs 余量-margin（沿用 PAWCHIVE_MIN_FREE_GB）。
- UPLOAD：staging 解压产物 → `os.walk` 逐文件 cp 回
  `<mount>/<原目录相对>/<包名>/<相对路径>`（makedirs 逐级）；跳过目标已存在
  且尺寸一致的文件（重试幂等，115 秒传场景常见）。
- VERIFY：`cd2_api.tasks_reply()` 上传任务清空 + `list_remote_dir` 对产物
  目录逐文件尺寸（目录树逐层列）；对账失败 → 差异清单入库 error，退避重试。
- 通知：开始（可选）/ 完成成功（包名、文件数、字节、耗时）/ 失败（原因+
  细节）/ TERMINAL（含替代建议）。全部走 notify_user，失败可溯源。

### 4.3 命令与菜单

- `/115x <115路径>`：入队该目录顶层压缩包，回执「发现 N 个包，已入队」；
  幂等（已入队/已完成的跳过并计数回执）。
- `/115x` 裸命令：状态总览（各状态计数、当前处理包、进度、磁盘余量）。
- `/115x stop`：停止 worker（在途包 release 回 PENDING）。
- `/115x del <任务id前缀>`：移除任务（含 staging 清理）。
- bot 菜单：CD2/工具视图加「🗜 115 解压」入口；MENU_ACTIONS 加 `extract`。
- **/help、BOT_COMMANDS、命令 if 链三处同步**（仓库纪律）。

## 5. 边界与风险（如实）

| 风险 | 处置 |
|---|---|
| 加密包（zip 加密位 / rar 探测失败） | TERMINAL「需密码」，不解不传，通知明示 |
| zip 炸弹 | 沿用 pawchive 三重守卫（成员数/单成员/压缩比） |
| zip 中文文件名乱码 | cp437→GBK 修复（新链路内做） |
| 磁盘不足 | 预检拦截 → FAILED「no-space」退避；余量低于保护线暂停 worker |
| 写穿透一致性：写入即返回 ≠ 上传完成 | VERIFY 双保险：CD2 任务清空 + 尺寸对账；只信对账 |
| 115 上传限速/风控 | 串行 + 任务间节流；对账失败退避重试，绝不并行轰炸 |
| 挂载掉线 | 启动/每包前校验，掉线暂停 + 告警，恢复自动续 |
| 大目录 | 命令回执给出包数量与预估（拷贝+解压+上传三段） |
| 残缺产物误导 | UPLOAD 前不解压到远端；VERIFY 不过 → FAILED，绝不标完成 |

## 6. 测试（stdlib unittest，沿用现有约定）

- test_extract_util：抽出的解压函数回归（zip 成功/加密/炸弹/空间不足、rar
  探测、cp437→GBK 修复、delete_source 两态、产物清单）。
- test_extract_db：v12 迁移、claim 乐观锁/租约、唯一键幂等、终态流转。
- test_extract_worker：mock 挂载与 cd2_api，验证 COPY 幂等跳过、UPLOAD
  逐文件回传与已存在跳过、VERIFY 差异 → FAILED、终端性流转、挂载缺失暂停。
- 接线：test_app_wiring / test_menu 更新。

## 7. 实施顺序

1. extract_util 抽取 + GBK 修复 + 测试（pawchive 零回归）
2. runtime_db v12 + 助手 + 测试
3. extract_worker + 测试
4. 命令/菜单/接线 + /help 三处同步 + 测试
5. 真机冒烟：`/115x /115open/云下载` 对一个小 zip 走完全链路，核对 115 端
   产物与对账结果

## 8. 不做的事

- 不递归子目录、不并行上传、不删远端源压缩包、不做密码自动尝试、
  不引入第二套任务状态机（挂现有 runtime_db 模式）。

## 9. 可行性实测记录（2026-09-30）

| 探测 | 结果 |
|---|---|
| 挂载结构 | /Volumes/CloudDrive = 115 根；gRPC `/115open/X` ↔ 挂载 `/X` |
| 读（冷块 512KB） | 604KB/s 起步，CD2 流式供给 |
| 写 + 回读 | 0.25s 写入，内容一致 |
| 10MB 持续写 | 0.57s 落盘，gRPC 对账尺寸 10485760 精确一致 |
| 删除传播 | rm 挂载文件 → gRPC 侧同步消失 |
| 解压工具链 | bsdtar 3.5.3 / libarchive 3.7.4（zip/rar4/rar5/7z 可读，无需装 unrar） |
| 复用件 | pawchive 解压链（加密/炸弹/空间守卫 + staging）、cd2_api.tasks_reply / list_remote_dir（对账）、pawchive 任务表模式（claim/租约/退避） |
