"""统一串行任务 Worker 框架（2026-10-26 定档，缺陷一修复）。

**背景**：领任务→租约→处理→取消放回→过期恢复的外层协议曾在
extract/wnacg/listener/pawchive/cmdq 各复制一份，细节漂移出两类实测
事故（E43485 停机取消不放回白等 10 分钟；E24388 幽灵任务无人回收）。
本框架把协议收进一处，各 worker 只实现域钩子：

- ``claim()``          领一条任务（runtime_db 域访问器），无任务返回 None
- ``process(task)``    业务处理；**CancelledError 必须上抛**（框架负责
                       在取消时调 release 放回，再上抛停机）
- ``release(task)``    把在途任务放回 PENDING（同步 DB 调用；幂等——
                       process 内部若已放过，这里 rowcount=0 无害）
- ``recover_startup()``启动恢复过期租约（各自 DB 访问器）
- ``extras_start()``   附加常驻循环（如 extract 的目录扫描/租约回收）

框架保证：
1. 主循环永不退出（任何一轮异常吞掉记日志）；
2. 优雅停机取消 process 时**先 release 再上抛**——彻底消灭「app 层
   兜底读不到 task_id」的顺序坑（E43485 根因）；
3. 暂停/停止/空转节奏统一可测。

**纪律**：import 期零副作用；loop 协程由 app 层 create_task 并持强引用。
"""
import asyncio

from .log import logger


class SerialTaskWorker:
    """串行一次一条的 worker 外壳。子类实现钩子后实例化即用。"""

    name = "?"                    # 日志前缀（如 "📖 wnacg"）
    poll_seconds = 5.0            # 空转/暂停轮询间隔（__init__ 可被 config 覆盖）
    idle_sleep = 0.0              # 每完成一条的节流间隔（防风控）
    error_sleep = 10.0            # 轮内异常后的冷却
    recover_seconds = 0.0         # >0：框架自带周期租约回收循环（秒）

    def __init__(self):
        self._current = None      # 在途任务（release/状态视图用）
        self._stop = False
        self._extra_tasks = set()   # 附加循环的强引用（§35）

    # ── 子类钩子 ──────────────────────────────────────────
    def claim(self):
        """领一条任务；无任务返回 None。"""
        raise NotImplementedError

    async def process(self, task):
        """处理一条任务。CancelledError 上抛（框架接管释放）。"""
        raise NotImplementedError

    def release(self, task):
        """把 task 放回 PENDING（幂等）。默认无操作。"""
        return None

    def recover_startup(self):
        """启动恢复过期租约。默认无操作。"""

    def recover_periodic(self):
        """周期恢复过期租约（recover_seconds>0 时由框架调度）。默认无操作。"""

    def extras_start(self):
        """附加常驻循环（子类自建 task 并持强引用）。默认无操作。"""

    def paused(self):
        """暂停判定（/xxx stop 类命令）。默认否。"""
        return False

    def startup_banner(self):
        """启动日志行（含限速/租约参数）。"""

    # ── 框架主体 ──────────────────────────────────────────
    def _safe_release(self, task):
        """release 的兜底包装：释放失败只记日志（转租约自愈）。"""
        try:
            return self.release(task)
        except Exception as e:
            logger.warning(
                f"{self.name} 放回 PENDING 失败（转租约自愈）：{e}")
            return None

    async def run(self):
        """常驻主循环：启动恢复 → 附加循环 → 轮询领取处理。"""
        banner = self.startup_banner()
        if banner:
            logger.info(banner)
        try:
            self.recover_startup()
        except Exception as e:
            logger.warning(f"{self.name} 启动租约恢复失败：{e}")
        try:
            self.extras_start()
        except Exception as e:
            logger.warning(f"{self.name} 附加循环启动失败：{e}")
        if self.recover_seconds > 0:
            # 周期租约回收（框架级——asmr 曾只有启动恢复，挂死任务要等
            # 下次进程重启才回 PENDING，E24388 同款幽灵）
            rt = asyncio.ensure_future(self._periodic_recover_loop())
            self._extra_tasks.add(rt)
            rt.add_done_callback(self._extra_tasks.discard)
        while not self._stop:
            try:
                if self.paused():
                    await asyncio.sleep(self.poll_seconds)
                    continue
                task = self.claim()
                if task is None:
                    await asyncio.sleep(self.poll_seconds)
                    continue
                self._current = task
                try:
                    await self.process(task)
                except asyncio.CancelledError:
                    # 优雅停机：先放回（幂等）再上抛——这是本框架存在的
                    # 全部理由（E43485：停机后白等租约+恢复扫描 10 分钟）
                    self._safe_release(task)
                    logger.info(f"{self.name} 停机取消：已放回待处理")
                    raise
                finally:
                    self._current = None
                if self.idle_sleep > 0:
                    await asyncio.sleep(self.idle_sleep)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"{self.name} worker 轮询异常：{e}")
                await asyncio.sleep(self.error_sleep)

    async def _periodic_recover_loop(self):
        """周期租约回收：过期的在途（挂死/SIGKILL 遗留）回 PENDING。"""
        try:
            while True:
                await asyncio.sleep(self.recover_seconds)
                try:
                    self.recover_periodic()
                except Exception as e:
                    logger.warning(f"{self.name} 周期租约回收失败：{e}")
        except asyncio.CancelledError:
            raise

    def request_stop(self):
        """请求停止（/xxx stop 类命令）。返回是否有在途。"""
        self._stop = True
        return self._current is not None

    def release_inflight(self):
        """停机兜底：外层（app.py）在协程取消后再调一次。幂等。"""
        if self._current is None:
            return False
        released = self._safe_release(self._current)
        return bool(released)


class DomainWorker(SerialTaskWorker):
    """TaskDomain 域的通用串行 worker 基类（2026-11-05 队列统一）。

    claim/release/recover 全部接 runtime_db.TaskDomain——wnacg/bili 各自
    手写三钩子的形态到此收敛；子类只需 process(task)（业务执行 +
    complete/fail + 通知）与 startup_banner。key_field 是行里业务键的
    列名（bili=url，x/pixiv=target）。"""

    def __init__(self, domain, key_field="target"):
        super().__init__()
        self.domain = domain
        self.key_field = key_field

    def claim(self):
        return self.domain.claim_next()

    def release(self, task):
        return self.domain.release(task[self.key_field])

    def recover_startup(self):
        self.domain.recover_expired()

    def queue_rows(self, limit=12):
        """队列行（状态文本/看板用）；DB 不可用返回空（不炸状态页）。"""
        try:
            return self.domain.list(limit=limit)
        except Exception:
            return []


async def download_with_watchdog(url, dest, expected=None, check=30.0,
                                 stall=240.0, download_fn=None, label="下载"):
    """下载 + .part 增长看门狗（协程侧，兜住同步层一切无超时挂死）。

    download_file_blocking 的读超时/流速看门狗只覆盖「连接已建立后」；
    getaddrinfo/连接建立阶段挂死时线程永远不返回（2026-10-26 生产实测：
    .part 冻结 13 分钟、进程无 CDN socket、零日志）。本包装在协程侧
    监控 .part 文件增长：连续 stall 秒零增长 → 弃线程抛 TimeoutError
    （含 "Timeout" 走瞬时退避，不烧尝试额度）。被弃线程泄漏由进程重启
    兜底（罕见事件）。stall 必须大于 download_fn 内部「重试 sleep+读
    超时×次数+重连」的合法静默窗口（默认 60×3+≈200s → 240s 起步）。
    """
    import os
    if download_fn is None:
        raise ValueError("download_fn required")
    part = dest + ".part"
    job = asyncio.ensure_future(
        asyncio.to_thread(download_fn, url, dest, expected))
    last = -1
    stall_acc = 0.0
    while True:
        try:
            return await asyncio.wait_for(asyncio.shield(job),
                                          timeout=check)
        except asyncio.TimeoutError:
            cur = os.path.getsize(part) if os.path.exists(part) else 0
            if cur != last:
                last = cur
                stall_acc = 0.0
            else:
                stall_acc += check
            if stall_acc >= stall:
                job.cancel()   # 尽力通知；线程可能仍挂着——弃之不管
                raise TimeoutError(
                    f"下载无进展超过 {int(stall_acc)}s（.part 停在 "
                    f"{last} 字节，疑似 DNS/连接挂死），弃线程转重试")
        except asyncio.CancelledError:
            job.cancel()
            raise


async def copy_with_watchdog(src, dst, expected=None, check=60.0,
                             stall=900.0, copy_fn=None, label="拷贝"):
    """挂载拷贝 + 无进展看门狗（extract 实战版泛化，2026-10-26）。

    FUSE 冻结时 read/write 会永久阻塞线程且 to_thread 不可取消——
    实测任务卡同一字节 12 小时。每 check 秒查一次 dst 增长，连续
    stall 秒无增长 → 抛 TimeoutError 走重试（底层线程可能仍阻塞，
    但 worker 不再被拖死）。expected 给定时完成后校验总字节数
    （短读抛 IOError，绝不把残缺副本当成功）。copy_fn(src, dst)。
    """
    import os
    if copy_fn is None:
        def _plain(s, d):
            with open(s, "rb") as fi, open(d, "wb") as fo:
                while True:
                    b = fi.read(4 * 1024 * 1024)
                    if not b:
                        break
                    fo.write(b)
        copy_fn = _plain
    copied = asyncio.ensure_future(asyncio.to_thread(copy_fn, src, dst))
    stall_acc = 0.0
    last = -1
    logged = 0
    result = None
    try:
        while True:
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(copied), timeout=check)
                break
            except asyncio.TimeoutError:
                cur = os.path.getsize(dst) if os.path.exists(dst) else 0
                if cur != last:
                    # 对比「上次已记录」水位：check 内增量常不足大阈值，
                    # 对比上次轮询会永远够不到（进度全程静默）
                    if cur - logged >= 256 * 1024 * 1024:
                        logger.info(
                            f"[{label}] 拷贝进度：{cur} / {expected or '?'}"
                            " 字节")
                        logged = cur
                    last = cur
                    stall_acc = 0.0
                else:
                    stall_acc += check
                if stall_acc >= stall:
                    copied.cancel()   # 尽力通知底层线程（阻塞中则弃）
                    raise TimeoutError(
                        f"拷贝无进度超过 {int(stall_acc)}s（挂载冻结），"
                        "转重试")
    except asyncio.CancelledError:
        copied.cancel()
        raise
    if expected is not None:
        actual = os.path.getsize(dst) if os.path.exists(dst) else -1
        if actual != int(expected):
            raise IOError(
                f"拷贝后尺寸不一致（本地 {actual} ≠ 期望 {expected}）")
    return result
