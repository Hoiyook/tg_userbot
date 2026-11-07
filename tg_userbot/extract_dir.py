"""目录解压工具（2026-11-02；2026-11-05 引擎收敛）：递归解压目录下的
压缩包到同名目录。

**单一引擎（2026-11-05 审计收敛）**：单包解压全部委托
``extract_util.extract_archive``（炸弹守卫/GBK 文件名修复/zip slip 逐成员
钳制/staging 原子改名/bsdtar 密码路——一处维护，pawchive 与 /115x 同源）。
本模块只保留 /extract 特有的编排：
  * 7z 分卷（.7z.001~NNN，bsdtar 不支持分卷，走 7z 工具）
  * 同名目录幂等跳过 / dry 预览 / 「成功才删」决策 / 单包失败不中断整轮
  * 统一密码候选链（password_candidates，与 /115x 共用）

安全纪律（沿用 2026-09-28 定稿）：
  * **成功才删**：解出 ≥1 个文件才删压缩包；失败/需密码/损坏/0 文件保留
  * 同名目录已存在且有内容 → 跳过（幂等）
  * 炸弹/空间守卫统一到 extract_util（PAWCHIVE_MIN_FREE_GB=2GB，
    与原 DISK_RESERVE 等值）；路径穿越成员**逐成员跳过**（不再整包拒绝
    ——危险成员绝不落盘，其余数据保留）
  * 半成品（.part/.crdownload/...）跳过
  * 失败时清理本次建出的空目录（有内容 = 解出了一部分，保留现场）

纯同步实现 + 显式参数，无 Telegram 依赖——命令层用 asyncio.to_thread
下放，脚本直接调用。
"""
import os
import shutil
import subprocess
from pathlib import Path

from .log import logger

# 下载未完成的半成品后缀（跳过，绝不碰）
IN_PROGRESS_SUFFIXES = (".part", ".crdownload", ".download", ".tmp",
                        ".opdownload")

SUBPROCESS_TIMEOUT_DEFAULT = 14400   # 单包硬顶 4 小时（36GB 分卷实测需要）

# 压缩包后缀 → 处理器；同名目录 = 去掉这些后缀后的名字
ARCHIVE_SUFFIXES = (".zip", ".rar", ".7z", ".tar.gz", ".tgz", ".tar.bz2",
                    ".tbz2", ".tar.xz", ".txz", ".tar")

# 7z 分卷（.7z.001 ~ .7z.NNN）：只需要 .001（首卷）被收集——7z 解首卷时
# 自动找同目录其余分卷；分卷必须齐全否则报错（保留现场）。
SPLIT_SUFFIX_RE = None   # lazy 编译


def is_split_volume(name: str) -> bool:
    """.7z.001 型分卷 → True（首卷 .001 与后续卷都算）。"""
    import re
    global SPLIT_SUFFIX_RE
    if SPLIT_SUFFIX_RE is None:
        SPLIT_SUFFIX_RE = re.compile(r"\.7z\.(\d{3})$", re.IGNORECASE)
    return bool(SPLIT_SUFFIX_RE.search(name))


def is_split_first_volume(name: str) -> bool:
    """首卷（.7z.001）——唯一需要处理的分卷；其它卷只作为数据存在。"""
    import re
    return bool(re.search(r"\.7z\.001$", name, re.IGNORECASE))


def find_7z_tool() -> str:
    """可用的 7z 解压器路径（p7zip 的 7z 优先），无则返回空串。"""
    for cand in ("/opt/homebrew/bin/7z", "/usr/local/bin/7z", "7z"):
        if shutil.which(cand):
            return cand
    return ""


def archive_stem(name: str) -> str:
    """压缩包文件名 → 同名目录名（foo.tar.gz → foo，foo.zip → foo）。

    分卷：2024-08.7z.001 → 2024-08（去掉 .7z.NNN 整段）；
    rar 多卷 2026-06.part1.rar → 2026-06（去掉 .partN.rar 整段）。"""
    import re
    low = name.lower()
    if re.search(r"\.7z\.\d{3}$", low):
        return re.sub(r"\.7z\.\d{3}$", "", name, flags=re.IGNORECASE)
    if re.search(r"\.part\d+\.rar$", low):
        return re.sub(r"\.part\d+\.rar$", "", name, flags=re.IGNORECASE)
    for suf in ARCHIVE_SUFFIXES:
        if low.endswith(suf):
            return name[: -len(suf)]
    return name


_RAR_PART_RE = None   # lazy：.partN.rar（N≥2 的后续卷是数据不是入口）


def is_rar_continuation(name: str) -> bool:
    """.part2.rar 及以后 → True（多卷 rar 的后续卷；part1 是唯一入口）。

    实测场景（2026-11-05 byqn）：23GB 月度归档里 2026-06.part1.rar +
    2026-06.part2.rar——不排除的话 part2 会被当独立包解（必然失败）。"""
    import re
    global _RAR_PART_RE
    if _RAR_PART_RE is None:
        _RAR_PART_RE = re.compile(r"\.part(\d+)\.rar$", re.IGNORECASE)
    m = _RAR_PART_RE.search(name)
    if not m:
        return False
    return int(m.group(0)[5:-4]) >= 2


def is_archive(name: str) -> bool:
    low = name.lower()
    if not low.endswith(ARCHIVE_SUFFIXES):
        return False
    return not is_rar_continuation(name)


def is_in_progress(name: str) -> bool:
    return name.lower().endswith(IN_PROGRESS_SUFFIXES)


def find_archives(root: str) -> list:
    """递归收集目录树里的压缩包（半成品与非压缩包不收）。

    7z 分卷只收首卷（.7z.001）——后续卷是数据不是入口。"""
    targets = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in sorted(filenames):
            if is_in_progress(fn):
                continue
            if is_archive(fn):
                targets.append(Path(dirpath) / fn)
            elif is_split_first_volume(fn):
                targets.append(Path(dirpath) / fn)
    return targets


def password_from_filename(name: str) -> str:
    """压缩包文件名规则（2026-11-03 owner 约定）：最后一个 # 后面的内容
    是密码——`合集#备注#zxc.zip` → zxc。没有 # 或末段为空/是纯后缀 → 空串。

    注意末段要剥掉扩展名（zxc.zip → zxc），且只认「# 后还有实际字符」的
    形态——普通文件名（如 `视频.mp4`）不带 # 自然返回空。"""
    base = name
    for suf in ARCHIVE_SUFFIXES:
        if base.lower().endswith(suf):
            base = base[: -len(suf)]
            break
    if "#" not in base:
        return ""
    last = base.rsplit("#", 1)[1].strip()
    return last


def password_candidates(archive_path, extra=None) -> list:
    """任意压缩包的统一密码候选链（/extract 与 /115x 共用，2026-11-03）。

    按序收集、去重：
    ① extra（调用方指定的，如 /115x 的目录名密码——最先试）
    ② 压缩包文件名规则：最后一个 # 后的内容
    ③ 同目录密码提示文件名 stem（密码zxc.txt → zxc）
    ④ 同目录密码提示文件内容（「密码是：xxx」→ xxx）
    返回候选列表（可能为空）。"""
    out = []

    def _add(pwd):
        if pwd and pwd not in out:
            out.append(pwd)

    _add(extra)
    _add(password_from_filename(os.path.basename(archive_path)))
    try:
        parent = os.path.dirname(os.path.abspath(archive_path))
        for fn in sorted(os.listdir(parent)):
            # 文件名清洗（2026-11-05 byqn 实测）：传输/下载的文件名常带
            # 尾随空格（"密码xxx.txt "）——strip 后再判形态，否则候选链
            # 整个跳过、密码就在眼前却找不到
            fn_clean = fn.strip()
            low = fn_clean.lower()
            if not low.endswith(".txt"):
                continue
            if "密码" not in fn_clean and "password" not in low:
                continue
            stem = fn_clean[:-4] if low.endswith(".txt") else fn_clean
            stem = stem.replace("密码", "").replace("password", "").strip()
            _add(stem)
            try:
                with open(os.path.join(parent, fn), encoding="utf-8",
                          errors="ignore") as f:
                    content = f.read(4096)
            except OSError as e:
                logger.debug(f"密码提示文件读取失败 {fn}：{e}")
                continue
            import re as _re
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                m = _re.search(r"([\x21-\x7e]{3,})\s*$", line)
                _add(m.group(1) if m else line[:64])
    except Exception as e:
        logger.debug(f"密码候选收集失败：{e}")
    # ⑤ 所在目录名（2026-11-06 c291dGhwbHVz 实测：byqn/MMD 两个大包的
    # 密码都是目录名——来源方打包时用密码当目录名的约定）
    try:
        _add(os.path.basename(os.path.dirname(
            os.path.abspath(archive_path))))
    except Exception:
        pass
    return out


def find_passwords(path) -> list:
    """/extract 场景的密码候选（无调用方 extra）——统一走 password_candidates。"""
    return password_candidates(path)


def find_password(path) -> str:
    """兼容包装：返回首选候选（无候选返回空串）。"""
    cands = find_passwords(path)
    return cands[0] if cands else ""


def process_archive(path, dry: bool = False,
                    timeout: int = SUBPROCESS_TIMEOUT_DEFAULT) -> tuple:
    """处理单个压缩包。返回 (动作, 说明)。

    动作: "done" 已解压并删除 / "skip" 跳过（已存在等） / "fail" 失败保留

    单包解压委托 extract_util.extract_archive（引擎收敛 2026-11-05）：
    炸弹守卫、GBK 修复、zip slip、staging 原子改名、bsdtar 密码路（AES
    与 ZipCrypto 都支持，不再依赖 7z 工具解加密 zip）全在那边维护。
    """ 
    name = path.name
    stem = archive_stem(name)
    if not stem.strip():
        return "skip", "文件名解不出同名目录"
    dest_dir = path.parent / stem

    # 同名目录已存在且非空 → 视为已解压过，跳过（幂等，防重复解压）
    if dest_dir.is_dir() and any(os.scandir(dest_dir)):
        return "skip", f"同名目录已存在且有内容: {dest_dir.name}"

    low = name.lower()

    # 魔数嗅探（2026-11-07 Alicexi 实测）：站点/网盘会把加密 rar 改名
    # .zip，甚至套 SFX 自解压头（实测 rar5 数据前挂 460288 字节 stub，
    # 7z 报 Offset=460288）——扩展名和开头字节都不可信。rar/7z 签名
    # → 强制走下方 7z 工具分支（7z 按内容开包，天然吃 SFX）。
    try:
        with open(path, "rb") as f:
            magic = f.read(8)
    except OSError:
        magic = b""
    if magic.startswith(b"Rar!"):                  # rar4/rar5 同签名
        low = name.lower() + ".rar"                # 唯一作用：命中 .rar 分支
    elif magic.startswith(b"7z\xbc\xaf\x27\x1c"):
        low = name.lower() + ".7z"
    elif low.endswith(".zip") and not magic.startswith(b"PK"):
        # 伪装 .zip 且开头非 PK：前 4MB 搜内嵌签名（SFX stub 典型几十~
        # 几百 KB）；真 zip 不进这条，零额外开销
        head = b""
        try:
            with open(path, "rb") as f:
                head = f.read(4 * 1024 * 1024)
        except OSError:
            pass
        if b"Rar!\x1a\x07" in head:
            low = name.lower() + ".rar"
        elif b"7z\xbc\xaf\x27\x1c" in head:
            low = name.lower() + ".7z"

    # 7z 分卷：需要 p7zip/7z 工具（bsdtar 不支持分卷）；首卷入口
    if is_split_volume(name):
        tool = find_7z_tool()
        if not tool:
            return "fail", ("未找到 7z 工具（brew/pkg install p7zip）——"
                            "保留分卷")
        if dry:
            return "dry", f"将解压分卷（7z）→ {dest_dir.name}/"
        dest_dir.mkdir(parents=True, exist_ok=True)
        # 密码候选循环（2026-11-06 MMD 实测：外层分卷也加密；目录名
        # c291dGhwbHVz 就是密码——候选链目录名源命中）
        candidates = password_candidates(str(path))
        tries = [None] + candidates
        last_err = ""
        pwd_used = False
        for pwd in tries:
            cmd = [tool, "x", "-y", f"-o{dest_dir}", str(path)]
            if pwd:
                cmd.insert(2, f"-p{pwd}")
            try:
                r = subprocess.run(
                    cmd, capture_output=True, stdin=subprocess.DEVNULL,
                    timeout=timeout)
            except subprocess.TimeoutExpired:
                return "fail", f"解压超时（>{timeout}s）——保留分卷"
            except Exception as e:
                return "fail", f"解压异常: {type(e).__name__}: {e}"
            if r.returncode == 0:
                pwd_used = pwd
                break
            last_err = (r.stderr or r.stdout or b"").decode(
                "utf-8", "ignore").strip()
        if pwd_used is False:
            if dest_dir.is_dir() and not any(os.scandir(dest_dir)):
                dest_dir.rmdir()
            hint = "、".join(c[:16] for c in candidates) or "无候选"
            return "fail", (f"分卷 7z 解压失败（密码候选试过：{hint}）"
                            f"——保留分卷（{last_err[:120]}）")
        err_note = last_err  # 部分卷解压时的非致命告警
        n = sum(1 for _ in os.scandir(dest_dir))
        if n <= 0:
            return "fail", "解压完成但 0 个文件——保留分卷待人工检查"
        # 成功：删除全部相关分卷（.001 ~ .NNN）
        import re as _re
        base_name = _re.sub(r"\.7z\.\d{3}$", "", name, flags=_re.IGNORECASE)
        removed = 0
        for dp, _dn, fns in os.walk(path.parent):
            for fn in fns:
                if fn.startswith(base_name) and is_split_volume(fn):
                    try:
                        os.remove(os.path.join(dp, fn))
                        removed += 1
                    except OSError as e:
                        logger.warning(f"分卷删除失败 {fn}：{e}")
        pwd_note = f"（用密码 {pwd_used[:16]}）" if pwd_used else ""
        return ("done",
                f"✅ 分卷解出 {n} 个文件{pwd_note} → {dest_dir.name}/"
                f"（已删除 {removed} 个分卷）")

    # rar：**一律 7z 工具优先**（2026-11-05 byqn 实测——bsdtar/libarchive
    # 对加密 rar 直接拒："Encryption is not supported"；分卷 .partN.rar
    # bsdtar 也不支持。7z 加密/分卷都行，且密码候选链共用）。7z 缺席时
    # 回落 bsdtar（唯一可行场景：单卷不加密 rar）。
    if low.endswith(".rar"):
        tool = find_7z_tool()
        if not tool:
            if dry:
                return "dry", f"将解压（bsdtar 回落，7z 未装）→ {dest_dir.name}/"
            # 回落 = extract_util（bsdtar 路；加密 rar 它会报 password——
            # 没有 7z 就解不了，如实保留原包）
            from .extract_util import extract_archive
            status, detail, files = extract_archive(
                str(path), dest=str(dest_dir), delete_source=True)
            if status == "extracted":
                return ("done", f"✅ 解出 {len(files)} 个文件 → "
                        f"{dest_dir.name}/（压缩包已删除）")
            if dest_dir.is_dir() and not any(os.scandir(dest_dir)):
                dest_dir.rmdir()
            if status == "password":
                return ("fail", "加密 rar 且未装 7z 工具（brew/pkg "
                        "install p7zip）——保留原包")
            return "fail", str(detail) or "解压失败"
        if dry:
            return "dry", f"将解压（7z）→ {dest_dir.name}/"
        dest_dir.mkdir(parents=True, exist_ok=True)
        candidates = password_candidates(path)
        tries = [None] + candidates   # 先无密码（很多 rar 只是假加密提示）
        last_err = ""
        pwd_used = False              # False=尚无成功；None=无密码成功（哨兵区分）
        for pwd in tries:
            cmd = [tool, "x", "-y", f"-o{dest_dir}", str(path)]
            if pwd:
                cmd.insert(2, f"-p{pwd}")
            try:
                r = subprocess.run(
                    cmd, capture_output=True, stdin=subprocess.DEVNULL,
                    timeout=timeout)
            except subprocess.TimeoutExpired:
                return "fail", f"解压超时（>{timeout}s）——保留原包"
            except Exception as e:
                return "fail", f"解压异常: {type(e).__name__}: {e}"
            if r.returncode == 0:
                pwd_used = pwd
                break
            last_err = (r.stderr or r.stdout or b"").decode(
                "utf-8", "ignore").strip()
        if pwd_used is False:
            if dest_dir.is_dir() and not any(os.scandir(dest_dir)):
                dest_dir.rmdir()
            hint = "、".join(c[:16] for c in candidates) or "无候选"
            return "fail", (f"rar 解压失败（密码候选试过：{hint}）"
                            f"——保留原包（{last_err[:100]}）")
        n = sum(1 for _ in os.scandir(dest_dir))
        if n <= 0:
            return "fail", "解压完成但 0 个文件——保留原包待人工检查"
        # 分卷 rar：成功删全部相关卷（.partN.rar 一起走）
        import re as _re
        base_no_part = _re.sub(r"\.part\d+\.rar$", "", name,
                               flags=_re.IGNORECASE)
        removed = 0
        for dp, _dn, fns in os.walk(path.parent):
            for fn in fns:
                fn_low = fn.lower()
                if fn == name or (
                        fn_low.startswith(base_no_part.lower())
                        and fn_low.endswith(".rar")
                        and ".part" in fn_low):
                    try:
                        os.remove(os.path.join(dp, fn))
                        removed += 1
                    except OSError as e:
                        logger.warning(f"分卷删除失败 {fn}：{e}")
        pwd_note = f"（用密码 {pwd_used[:16]}）" if pwd_used else ""
        return ("done",
                f"✅ 解出 {n} 个文件{pwd_note} → {dest_dir.name}/"
                + (f"（已删除 {removed} 个分卷）" if removed > 1 else ""))

    # .7z 单卷：**7z 工具优先**（加密 7z bsdtar/libarchive 不支持——
    # rar 分支同款结论；无密码先试 → 候选循环，目录名是候选之一）
    if low.endswith(".7z"):
        tool = find_7z_tool()
        if not tool:
            return "fail", ("未找到 7z 工具（brew/pkg install p7zip）——"
                            "保留原包")
        if dry:
            return "dry", f"将解压（7z）→ {dest_dir.name}/"
        dest_dir.mkdir(parents=True, exist_ok=True)
        candidates = password_candidates(path)
        tries = [None] + candidates
        last_err = ""
        pwd_used = False
        for pwd in tries:
            cmd = [tool, "x", "-y", f"-o{dest_dir}", str(path)]
            if pwd:
                cmd.insert(2, f"-p{pwd}")
            try:
                r = subprocess.run(
                    cmd, capture_output=True, stdin=subprocess.DEVNULL,
                    timeout=timeout)
            except subprocess.TimeoutExpired:
                return "fail", f"解压超时（>{timeout}s）——保留原包"
            except Exception as e:
                return "fail", f"解压异常: {type(e).__name__}: {e}"
            if r.returncode == 0:
                pwd_used = pwd
                break
            last_err = (r.stderr or r.stdout or b"").decode(
                "utf-8", "ignore").strip()
        if pwd_used is False:
            if dest_dir.is_dir() and not any(os.scandir(dest_dir)):
                dest_dir.rmdir()
            hint = "、".join(c[:16] for c in candidates) or "无候选"
            return "fail", (f"7z 解压失败（密码候选试过：{hint}）"
                            f"——保留原包（{last_err[:100]}）")
        n = sum(1 for _ in os.scandir(dest_dir))
        if n <= 0:
            return "fail", "解压完成但 0 个文件——保留原包待人工检查"
        # 成功删原包（与 rar/zip 路同一「成功才删」纪律；2026-11-07 冒烟
        # 实测伪装 7z 解出后原包残留——删包动作此前漏了）
        gone = ""
        try:
            os.remove(path)
            gone = "（压缩包已删除）"
        except OSError as e:
            logger.warning(f"🗜 7z 原包删除失败：{e}")
        pwd_note = f"（用密码 {pwd_used[:16]}）" if pwd_used else ""
        # 产物可能又是压缩包（双层压缩：外层分卷解出加密内层）——
        # extract_directory 下轮自动接力（幂等跳过已解的）
        inner = [f for f in os.listdir(dest_dir)
                 if f.lower().endswith((".7z", ".rar", ".zip"))]
        note2 = (f"；⚠️ 产物含内层压缩包 {inner[0]}（需再跑一轮 /extract "
                 f"解它）" if inner else "")
        return ("done",
                f"✅ 解出 {n} 个文件{pwd_note} → {dest_dir.name}/{gone}"
                f"{note2}")

    if dry:
        return "dry", f"将解压 → {dest_dir.name}/"

    # ── 单包解压：全部委托 extract_util（含密码候选逐个试）──
    from .extract_util import extract_archive
    candidates = password_candidates(path)
    status, detail, files = extract_archive(
        str(path), dest=str(dest_dir), delete_source=True)
    if status == "password" and candidates:
        # 密码候选按序试，第一个解开的获胜（文件名 # 段/提示文件名/
        # 提示文件内容——两级提示可能各写了一个密码）
        for pwd in candidates:
            status, detail, files = extract_archive(
                str(path), dest=str(dest_dir), delete_source=True,
                password=pwd)
            if status == "extracted":
                return ("done",
                        f"✅ 解出 {len(files)} 个文件（用密码 {pwd}）→ "
                        f"{dest_dir.name}/（压缩包已删除）")
        return ("fail",
                f"密码 {'/'.join(c[:16] for c in candidates)} 都解不开"
                f"——保留原包（{str(detail)[:80]}）")
    if status == "extracted":
        return ("done",
                f"✅ 解出 {len(files)} 个文件 → {dest_dir.name}/"
                f"（压缩包已删除）")
    # 失败清理：空目录与空 staging 残留（extract_util 失败时 staging
    # 留现场；/extract 纪律 = 空的清掉、有内容的保留）
    staging = str(dest_dir) + ".extracting"
    if os.path.isdir(staging) and not any(os.scandir(staging)):
        shutil.rmtree(staging, ignore_errors=True)
    if dest_dir.is_dir() and not any(os.scandir(dest_dir)):
        dest_dir.rmdir()
    if status == "password":
        return ("fail",
                "需密码（找不到密码候选：无提示文件、文件名无 # 段）"
                "或已损坏——保留原包")
    if status == "no-space":
        return "fail", f"磁盘余量不足：{detail}"
    return "fail", str(detail) or "解压失败"


def extract_directory(root: str, dry: bool = False,
                      timeout: int = SUBPROCESS_TIMEOUT_DEFAULT) -> tuple:
    """跑整轮：找包 → 逐包处理 → 汇总。单包失败绝不中断整轮。

    返回 (输出行列表, 计数 dict)。"""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return ([f"❌ 目录不存在: {root}"],
                {"done": 0, "fail": 0, "skip": 0})

    targets = find_archives(root)
    if not targets:
        return (["没有找到压缩包。"],
                {"done": 0, "fail": 0, "skip": 0})

    lines = [f"找到 {len(targets)} 个压缩包"
             + ("（预览模式）" if dry else "") + "："]
    n_done = n_fail = n_skip = 0
    for p in targets:
        try:
            action, msg = process_archive(p, dry, timeout)
        except Exception as e:   # 单包失败绝不中断整轮
            action, msg = "fail", f"未预期异常: {type(e).__name__}: {e}"
        mark = {"done": "✅", "fail": "❌", "skip": "·", "dry": "🔍"}.get(
            action, "?")
        lines.append(f"{mark} {p.name}  {msg}")
        if action == "done" or (action == "dry" and msg.startswith("将解压")):
            n_done += 1
        elif action == "fail":
            n_fail += 1
        elif action == "skip":
            n_skip += 1

    lines.append(f"\n完成：成功 {n_done} | 失败 {n_fail}（原包保留）| "
                 f"跳过 {n_skip}")
    if n_fail:
        lines.append("失败的原因多为需密码/损坏——原包一律保留，请人工处理。")
    return (lines, {"done": n_done, "fail": n_fail, "skip": n_skip})


def parse_extract_args(arg: str) -> tuple:
    """解析 /extract 参数：『<目录> [dry]』→ (目录, 是否预览)。

    dry 形态（尾部，大小写不敏感）：` dry` / `-dry` / `--dry`——用户实际
    输入三种都会出现（2026-11-03 实测：目录名带空格时 -dry 常被写成
    连字符形）。目录名本身叫 dry 时语义不设防（工具脚本，注释说明）。"""
    arg = str(arg or "").strip()
    low = arg.lower()
    dry = False
    for suf in ("--dry", "-dry", " dry"):   # 先长后短，避免剥 "-dry" 残 "-"
        if low.endswith(suf):
            dry = True
            arg = arg[:-len(suf)].strip()
            break
    return arg, dry
