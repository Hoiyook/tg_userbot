"""Schema DDL 全集（2026-11-12 结构债工程 B 拆出）。

纯数据：一个字符串元组，无导入、无状态、无逻辑——任何地方引用它
都是值拷贝，不存在测试 mock 语义问题（对比：函数/连接拆分曾因
模块属性 patch 语义断裂被回滚，见 B2 复盘）。migrate() 仍单点在
__init__.py（迁移编排顺序不可分散）。
"""

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # v3 起：chain 区分 listen（标签监听）与 wl（下载白名单）两条独立游标。
    # 同一聊天两边都在时互不干扰——/wl since 回补只倒退 wl 游标。
    """
    CREATE TABLE IF NOT EXISTS listener_checkpoints (
        source_chat_id INTEGER NOT NULL,
        chain TEXT NOT NULL DEFAULT 'listen',
        last_message_id INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (source_chat_id, chain)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS listener_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        grouped_id INTEGER,
        target_type TEXT NOT NULL,
        target_chat_id INTEGER,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_retry_at INTEGER,
        created_at INTEGER NOT NULL,
        started_at INTEGER,
        completed_at INTEGER,
        lease_until INTEGER,
        last_error TEXT,
        download INTEGER NOT NULL DEFAULT 0,
        payload TEXT,
        origin TEXT NOT NULL DEFAULT 'listen'
    )
    """,
    # 唯一约束。**必须建在 COALESCE(target_chat_id, 0) 上**：SQLite 的 UNIQUE
    # 索引里 NULL 彼此不相等，按字面 (…, target_chat_id) 建的话，收藏夹目标
    # （target_chat_id = NULL）那条唯一约束**完全失效**——同一消息能插出两条
    # 收藏夹任务、转发两次。收藏夹恰恰是最常见的那个目标。
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_listener_task_unique
    ON listener_tasks (
        source_chat_id, message_id, target_type, COALESCE(target_chat_id, 0)
    )
    """,
    # 领取用的索引：按 (status, next_retry_at) 找可执行任务
    """
    CREATE INDEX IF NOT EXISTS idx_listener_task_pickup
    ON listener_tasks (status, next_retry_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        payload TEXT,
        FOREIGN KEY(task_id) REFERENCES listener_tasks(id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_task_events_task
    ON task_events (task_id, id)
    """,
    # 评论跟进：命中标签的帖子进关注列表，之后按天跟进它的评论区。
    # caption / post_date / source_name 是**建列表时的快照**——后面十几次检查
    # 直接用，不再回频道取一次原帖（原帖被编辑/删除也不影响已定下的命名）。
    """
    CREATE TABLE IF NOT EXISTS listener_follows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER NOT NULL,
        post_id INTEGER NOT NULL,
        source_chat_id INTEGER,
        caption TEXT,
        post_date TEXT,
        source_name TEXT,
        status TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        checks INTEGER NOT NULL DEFAULT 0,
        last_checked_at INTEGER,
        last_error TEXT
    )
    """,
    # 同一个帖子只关注一次（重跑/重启都不会插出第二条）
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_listener_follow_unique
    ON listener_follows (channel_id, post_id)
    """,
    # 取到期关注用：(status, last_checked_at)
    """
    CREATE INDEX IF NOT EXISTS idx_listener_follow_due
    ON listener_follows (status, last_checked_at)
    """,
    # ============================================================
    # 下载队列持久化（2026-09-13，任务书：下载队列 SQLite 化）。
    # 替代 runtime/download_queue.json 的「每次变更全量重写」：单行事务
    # write-through，内存字典（state.QUEUE）仍是唯一工作副本与读取面。
    # state 只有 QUEUED/RETRY 两态（终态即删行）——下载任务的执行是进程内的，
    # 在途判定走内存 EXECUTING，不引入 listener 的 PROCESSING/租约。
    # seq 是全局单调序号：装载时 ORDER BY seq 还原 tasks/retry 的列表顺序
    # （展示与 /queue 3、/retry del 2 的序号语义都建立在它上面）。
    # 除固定列外的一切字段（含未来新增的未知字段）都装 payload JSON，
    # round-trip 必须逐字段无损。
    # ============================================================
    """
    CREATE TABLE IF NOT EXISTS download_tasks (
        id            TEXT PRIMARY KEY,
        kind          TEXT NOT NULL,
        state         TEXT NOT NULL,
        seq           INTEGER NOT NULL,
        attempts      INTEGER NOT NULL DEFAULT 0,
        next_retry_at REAL,
        enqueued_at   INTEGER NOT NULL,
        payload       TEXT NOT NULL
    )
    """,
    # 装载顺序 + 启动日志的 count 查询都按 state 过滤
    """
    CREATE INDEX IF NOT EXISTS idx_download_tasks_state
    ON download_tasks (state, seq)
    """,
    # ============================================================
    # 下载任务事件流（2026-09-14，任务书：任务事件 SQLite 化 Phase 2）。
    # 替代 runtime/task_events.jsonl：台账/对账（stats.rebuild_stats）与
    # Reporter 通知的数据源。**与 listener 的 task_events 表是两套 ID 空间**
    # （那边自增 id 外键 listener_tasks；这边 task_id 是下载任务 uuid），
    # 绝不混用，故命名刻意错开。ts 存 epoch 秒（读出渲染回本地串）；task_id
    # 可空（DEDUP_SKIPPED/LISTEN_SCAN/LISTEN_FAIL 等输入侧事件无任务）；
    # **不建外键**（事件流是独立台账，避免先于任务行/任务删行被级联的边角）。
    # rowid（id）单调递增 = Reporter 的增量游标；裁剪删旧行不影响游标语义。
    # ============================================================
    """
    CREATE TABLE IF NOT EXISTS download_events (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts        INTEGER NOT NULL,
        ev        TEXT NOT NULL,
        task_id   TEXT,
        payload   TEXT
    )
    """,
    # 台账按窗口聚合（ts 区间扫描）与按任务对账（task_id, id）
    """
    CREATE INDEX IF NOT EXISTS idx_download_events_ts ON download_events (ts)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_download_events_task
    ON download_events (task_id, id)
    """,
    # ============================================================
    # 下载历史（2026-09-14，Phase 3，schema v6）。替代
    # runtime/download_history.txt：行格式
    # `ts | 类型 | 文件名 | 大小 | 来源：xxx` 是全部消费方（/done、/find、
    # 台账回退口径）的接口，行→列拆解只发生在写入/读出边界，渲染逐字符还原。
    # 旧类型（统一链之前的 抖音/Instagram）原样保留；不合 5 段格式的行进
    # raw 列兜底（零丢失）。无封顶（原文件同样无界）。
    # ============================================================
    """
    CREATE TABLE IF NOT EXISTS download_history (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts        INTEGER,
        kind      TEXT,
        filename  TEXT,
        size_text TEXT,
        source    TEXT,
        raw       TEXT
    )
    """,
    # ============================================================
    # 去重索引（2026-09-14，Phase 4，schema v7）。替代
    # runtime/dedup_index.txt：key/ts/filename 三列忠实映射
    # `key	日期	文件名` 行（ts 保留原短格式纯信息字段）。内存 dict
    # （state.DEDUP_INDEX）仍是判重热路径的唯一读取面——本表只负责持久化
    # 与启动装载，同键重复行容忍（dict 后写胜，与文件时代一致）。
    # ============================================================
    """
    CREATE TABLE IF NOT EXISTS dedup_index (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        key      TEXT NOT NULL,
        ts       TEXT,
        filename TEXT
    )
    """,
    # ============================================================
    # Pawchive 扫描结果（2026-09-14，schema v8）。/paw plan 把创作者帖子
    # 扫描结果按**帖子级**落库，由 pawchive_worker 以生命周期方式消费：
    # PENDING →（claim 租约）PROCESSING → 附件直链逐条交给 Chrome Agent →
    #   全部 DONE 且无外链 → COMPLETED（已完成）
    #   全部 DONE 且有外链 → MANUAL（人工处理，外链清单在 ext_links）
    #   Chrome 失败/取消   → FAILED（/paw retry 重投 PENDING）
    # UNIQUE(service, creator_id, post_id)：重复 plan 幂等——已存在的帖子
    # 整体跳过（状态与文件进度都不动），只有新帖子才入队。
    # ============================================================
    """
    CREATE TABLE IF NOT EXISTS pawchive_posts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        service      TEXT NOT NULL,
        creator_id   TEXT NOT NULL,
        creator_name TEXT,
        post_id      TEXT NOT NULL,
        title        TEXT,
        published    TEXT,
        post_url     TEXT,
        subdir       TEXT,
        status       TEXT NOT NULL,
        ext_links    TEXT,
        ext_count    INTEGER NOT NULL DEFAULT 0,
        attempts     INTEGER NOT NULL DEFAULT 0,
        next_retry_at INTEGER,
        lease_until  INTEGER,
        created_at   INTEGER NOT NULL,
        started_at   INTEGER,
        completed_at INTEGER,
        last_error   TEXT,
        scan_batch   TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_pawchive_post_unique
    ON pawchive_posts (service, creator_id, post_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_pawchive_post_pickup
    ON pawchive_posts (status, next_retry_at)
    """,
    # 文件级进度：worker 重领帖子时只提交还差的那部分；chrome_task_id 是
    # 与 chrome_tasks.json 对账的钥匙（Agent 终态 = 文件终态的唯一真相源）。
    """
    CREATE TABLE IF NOT EXISTS pawchive_files (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        post_row      INTEGER NOT NULL,
        url           TEXT NOT NULL,
        filename      TEXT,
        status        TEXT NOT NULL,
        chrome_task_id TEXT,
        size_bytes    INTEGER,
        attempts      INTEGER NOT NULL DEFAULT 0,
        error         TEXT,
        updated_at    INTEGER,
        FOREIGN KEY(post_row) REFERENCES pawchive_posts(id)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_pawchive_file_unique
    ON pawchive_files (post_row, url)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_pawchive_file_pickup
    ON pawchive_files (post_row, status)
    """,
    # ============================================================
    # 手动外链台账（2026-09-16，schema v9）。移动端自己找到的网盘链接
    # 发进来登记（默认 PENDING 未处理），人工处理完手动标 DONE；再次
    # 发送同链接时按 url_key 精确查重提示已处理。url_key 保留原始大小写
    # （MEGA 解密键区分大小写，lower 会造成不同链接误判重复）。
    # ============================================================
    """
    CREATE TABLE IF NOT EXISTS manual_links (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        url        TEXT NOT NULL,
        url_key    TEXT NOT NULL UNIQUE,
        host       TEXT,
        note       TEXT,
        status     TEXT NOT NULL DEFAULT 'PENDING',
        created_at INTEGER NOT NULL,
        done_at    INTEGER
    )
    """,
    # ============================================================
    # 功能使用审计（2026-09-24，schema v11）：按 (功能名, 日期) 聚合计数，
    # /usage 看排行，/sql 亦可自由聚合。功能名 = 指令（/paw plan）或
    # 面板动作（menu:home）。
    # ============================================================
    """
    CREATE TABLE IF NOT EXISTS feature_usage (
        name     TEXT NOT NULL,
        day      TEXT NOT NULL,
        count    INTEGER NOT NULL DEFAULT 0,
        first_at INTEGER NOT NULL,
        last_at  INTEGER NOT NULL,
        PRIMARY KEY (name, day)
    )
    """,
    # ============================================================
    # 115 解压回传任务（2026-09-30，schema v12）：<115目录> 顶层的压缩包
    # 拉到本地解压后回传原目录。唯一键 (remote_dir, archive_name,
    # archive_size) 幂等：重复 /115x 自动跳过已完成/在队。状态机：
    # PENDING → PROCESSING（租约）→ COMPLETED / FAILED（退避重试）/
    # TERMINAL（需密码/炸弹/损坏——不可恢复，不再重试）。
    # ============================================================
    """
    CREATE TABLE IF NOT EXISTS extract_tasks (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        remote_dir     TEXT NOT NULL,
        archive_name   TEXT NOT NULL,
        archive_size   INTEGER NOT NULL,
        status         TEXT NOT NULL,
        staging_dir    TEXT,
        uploaded_files INTEGER NOT NULL DEFAULT 0,
        uploaded_bytes INTEGER NOT NULL DEFAULT 0,
        attempts       INTEGER NOT NULL DEFAULT 0,
        next_retry_at  INTEGER,
        lease_until    INTEGER,
        error          TEXT,
        created_at     INTEGER NOT NULL,
        started_at     INTEGER,
        completed_at   INTEGER,
        updated_at     INTEGER NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_extract_task_unique
        ON extract_tasks (remote_dir, archive_name, archive_size)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_extract_pickup
        ON extract_tasks (status, next_retry_at)
    """,
    # ============================================================
    # 115 全盘查重结果（2026-10-23，/115dup）。一次扫描一个 run，
    # 新 run 落库时清掉旧 run（replace-per-scan：/115dup list 永远
    # 对着最近一次扫描）。group_id 是 run 内 1..N、按可释放空间降序
    # ——list 的页序、del 的组号都建立在它上面。
    # ============================================================
    """
    CREATE TABLE IF NOT EXISTS dup_runs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        root         TEXT NOT NULL,
        status       TEXT NOT NULL,
        started_at   INTEGER NOT NULL,
        finished_at  INTEGER,
        total_files  INTEGER NOT NULL DEFAULT 0,
        dup_groups   INTEGER NOT NULL DEFAULT 0,
        dup_files    INTEGER NOT NULL DEFAULT 0,
        wasted_bytes INTEGER NOT NULL DEFAULT 0,
        error        TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dup_files (
        run_id     INTEGER NOT NULL,
        group_id   INTEGER NOT NULL,
        path       TEXT NOT NULL,
        size       INTEGER NOT NULL,
        sha1       TEXT NOT NULL,
        write_time INTEGER,
        deleted    INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (run_id, group_id, path)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_dup_files_run
        ON dup_files (run_id, group_id)
    """,
    # ============================================================
    # 115 全量清单（2026-10-24，/115inv，schema v15）——分类管理的数据
    # 底座：classify 体检、后续批量迁移 dry-run 校验都从这里读。
    # 按顶层目录分段扫描：done_tops 记已完成的顶层目录（inv_state），
    # 每段重扫某顶层目录时先删旧行再写（该目录内的增量以整段重扫为准）。
    # ============================================================
    """
    CREATE TABLE IF NOT EXISTS inv_files (
        path       TEXT PRIMARY KEY,
        top        TEXT NOT NULL,
        ext        TEXT NOT NULL DEFAULT '',
        size       INTEGER NOT NULL,
        sha1       TEXT NOT NULL DEFAULT '',
        write_time INTEGER NOT NULL DEFAULT 0,
        scanned_at INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_inv_top
        ON inv_files (top)
    """,
    """
    CREATE TABLE IF NOT EXISTS inv_empty_dirs (
        path       TEXT PRIMARY KEY,
        scanned_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inv_state (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cmd_ledger (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id     INTEGER NOT NULL,
        msg_id      INTEGER NOT NULL,
        cmd_text    TEXT NOT NULL,
        status      TEXT NOT NULL,
        error       TEXT,
        received_at INTEGER NOT NULL,
        executed_at INTEGER,
        UNIQUE (chat_id, msg_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cmd_cursors (
        chat_id         TEXT PRIMARY KEY,      -- int 或 "me"（文本键兼容收藏夹）
        last_seen_msg_id INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS wnacg_tasks (
        aid          INTEGER PRIMARY KEY,
        title        TEXT,
        zip_url      TEXT,
        filename     TEXT,
        status       TEXT NOT NULL,
        attempts     INTEGER NOT NULL DEFAULT 0,
        next_retry_at INTEGER,
        lease_until  INTEGER,
        size_bytes   INTEGER,
        error        TEXT,
        created_at   INTEGER NOT NULL,
        updated_at   INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS asmr_tasks (
        rj           INTEGER PRIMARY KEY,   -- RJ 数字号（判重主键）
        title        TEXT,
        circle       TEXT,                  -- 社团名（展示用）
        only_filter  TEXT,                  -- only <关键词>：路径命中才下（空=全量）
        total_files  INTEGER NOT NULL DEFAULT 0,  -- 信息性计数（真相=API 文件树）
        done_files   INTEGER NOT NULL DEFAULT 0,
        status       TEXT NOT NULL,
        attempts     INTEGER NOT NULL DEFAULT 0,
        next_retry_at INTEGER,
        lease_until  INTEGER,
        size_bytes   INTEGER,               -- 总字节（信息性）
        error        TEXT,
        created_at   INTEGER NOT NULL,
        updated_at   INTEGER NOT NULL
    )
    """,
    # v22：X/Pixiv 作者下载队列（TaskDomain 域；key=作者/X 目标串）
    """CREATE TABLE IF NOT EXISTS xn_tasks (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        target       TEXT NOT NULL UNIQUE,  -- @作者 / x.com 推文或作者页 URL
        kind         TEXT,                  -- author/tweet（领取时判定）
        label        TEXT,
        status       TEXT NOT NULL,
        attempts     INTEGER NOT NULL DEFAULT 0,
        next_retry_at INTEGER,
        lease_until  INTEGER,
        error        TEXT,
        created_at   INTEGER NOT NULL,
        updated_at   INTEGER NOT NULL
    )
    """,
    """CREATE TABLE IF NOT EXISTS xi_tasks (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        target       TEXT NOT NULL UNIQUE,  -- 画师名/用户ID/pixiv URL
        kind         TEXT,
        label        TEXT,
        status       TEXT NOT NULL,
        attempts     INTEGER NOT NULL DEFAULT 0,
        next_retry_at INTEGER,
        lease_until  INTEGER,
        error        TEXT,
        created_at   INTEGER NOT NULL,
        updated_at   INTEGER NOT NULL
    )
    """,
    # v21：B站串行下载队列（SerialTaskWorker 域；url 原始串判重——
    # b23 短链与长链算两条，真实去重由 yt-dlp archive 兜底）
    """CREATE TABLE IF NOT EXISTS bili_tasks (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        url          TEXT NOT NULL UNIQUE,  -- 提交的原始链接（延迟解析）
        kind         TEXT,                  -- video/合集/系列（领到时解析回填）
        label        TEXT,                  -- 展示名（解析后回填）
        status       TEXT NOT NULL,         -- PENDING/PROCESSING/DONE/FAILED
        attempts     INTEGER NOT NULL DEFAULT 0,
        next_retry_at INTEGER,
        lease_until  INTEGER,
        error        TEXT,
        created_at   INTEGER NOT NULL,
        updated_at   INTEGER NOT NULL
    )
    """,
    # v20：115 离线完成守望台账（2026-11-05 审计——守望 task 只存内存，
    # 重启后磁力在 115 照下但永无人通知）
    """CREATE TABLE IF NOT EXISTS offline_watch (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        info_hash    TEXT,                 -- 115 离线任务哈希（可空）
        to_folder    TEXT NOT NULL,        -- 目标目录（守望轮询处）
        label        TEXT,                 -- 通知展示名
        known_names  TEXT,                 -- 提交时目录快照 JSON（判新增）
        status       TEXT NOT NULL,        -- WATCHING / DONE
        created_at   INTEGER NOT NULL,
        updated_at   INTEGER NOT NULL
    )
    """,
    # v23：分享提交幂等台账（2026-11-09）。客户端每次分享带一个 32 位
    # request_id；服务端先 claim（INSERT PROCESSING），成功后写 DONE +
    # 首次结果。重放读到 DONE 直接返回原结果，不再执行分流。处理中超过
    # stale 秒视为进程中途死掉，允许重新 claim；过期行由 init_db 裁剪。
    """CREATE TABLE IF NOT EXISTS api_requests (
        request_id TEXT PRIMARY KEY,
        status     TEXT NOT NULL,          -- PROCESSING / DONE
        result     TEXT,                   -- DONE 时的首次结果 JSON
        created_at INTEGER NOT NULL
    )
    """,
)
