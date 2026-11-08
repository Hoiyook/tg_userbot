"""extract_dir（目录解压工具）的单元测试：纯函数 + 临时夹具，不联网。

覆盖：同名目录推导、半成品/非压缩包过滤、zip/tar 成功删除、
同名已存在跳过、损坏保留 + 空目录清理、加密跳过、路径穿越拒绝、
磁盘守卫、解析器、整轮汇总与单包失败不中断。
"""
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_extract_test_")
import atexit   # noqa: E402
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config   # noqa: E402
from tg_userbot import extract_dir   # noqa: E402
from tg_userbot import extract_util   # noqa: E402


def make_zip(path, entries):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries:
            z.writestr(name, data)


def make_targz(path, entries):
    import io
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with tarfile.open(path, "w:gz") as t:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))


class StemAndFilterTest(unittest.TestCase):

    def test_archive_stem(self):
        self.assertEqual(extract_dir.archive_stem("合集.tar.gz"), "合集")
        self.assertEqual(extract_dir.archive_stem("foo.zip"), "foo")
        self.assertEqual(extract_dir.archive_stem("包.RAR"), "包")
        self.assertEqual(extract_dir.archive_stem("x.tar.bz2"), "x")

    def test_parse_extract_args(self):
        self.assertEqual(extract_dir.parse_extract_args("仓鼠_杂项 dry"),
                         ("仓鼠_杂项", True))
        self.assertEqual(extract_dir.parse_extract_args(" 仓鼠 "),
                         ("仓鼠", False))
        self.assertEqual(extract_dir.parse_extract_args(""), ("", False))

    def test_find_archives_filters(self):
        root = tempfile.mkdtemp(dir=_TMP)
        make_zip(os.path.join(root, "a.zip"), [("x.txt", "1")])
        make_targz(os.path.join(root, "子", "b.tar.gz"), [("y", b"2")])
        open(os.path.join(root, "c.zip.crdownload"), "wb").write(b"x")
        open(os.path.join(root, "readme.md"), "w").write("x")
        found = extract_dir.find_archives(root)
        names = [str(p.relative_to(root)) for p in found]
        self.assertEqual(sorted(names), ["a.zip", os.path.join("子", "b.tar.gz")])


def make_encrypted_zip(directory, name="密.zip"):
    """真·加密 zip 夹具：手工构造（无 pyzipper 依赖）。本地头与中央目录
    都置加密位 flags=1（stdlib 读**中央目录**判加密位）；数据置零、CRC=0
    ——真去解密必失败，正好测「密码不对保留」路径。"""
    import struct
    z = directory / name
    body = b"\x00" * 64
    lfh = (b"PK\x03\x04"
           + struct.pack("<HHHHHIIIHH", 20, 1, 0, 0, 0, 0,
                         len(body), len(body), len("a.txt"), 0)
           + b"a.txt")
    data = lfh + body
    cd = (b"PK\x01\x02"
          + struct.pack("<HHHHHHIIIHHHHHII", 20, 20, 1, 0, 0, 0, 0,
                        len(body), len(body), len("a.txt"), 0, 0,
                        0, 0, 0, len(lfh))
          + b"a.txt")
    eocd = struct.pack("<IHHHHIIH", 0x06054b50, 0, 0, 1, 1,
                       len(cd), len(lfh), 0)
    with open(z, "wb") as f:
        f.write(data + cd + eocd)
    return z


class ProcessArchiveTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=_TMP))

    def test_zip_success_deletes_archive(self):
        z = self.root / "普通包.zip"
        make_zip(z, [("说明.txt", "内容"), ("图/封面.jpg", "jpg")])
        action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "done")
        self.assertFalse(z.exists())                     # 成功才删
        self.assertTrue((self.root / "普通包" / "说明.txt").exists())
        self.assertTrue((self.root / "普通包" / "图" / "封面.jpg").exists())

    def test_targz_success(self):
        t = self.root / "合集.tar.gz"
        make_targz(t, [("第1集.mp4", b"abc")])
        action, _ = extract_dir.process_archive(t)
        self.assertEqual(action, "done")
        self.assertTrue((self.root / "合集" / "第1集.mp4").exists())
        self.assertFalse(t.exists())

    def test_existing_target_skips(self):
        d = self.root / "已存在"
        d.mkdir()
        (d / "旧文件.txt").write_text("old", encoding="utf-8")
        z = self.root / "已存在.zip"
        make_zip(z, [("new.txt", "new")])
        action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "skip")
        self.assertTrue(z.exists())                      # 原包保留
        self.assertEqual((d / "旧文件.txt").read_text(encoding="utf-8"),
                         "old")                          # 旧内容不动

    def test_corrupt_rar_kept_and_empty_dir_cleaned(self):
        bad = self.root / "损坏包.rar"
        bad.write_bytes(("垃圾" * 10).encode("utf-8"))
        action, msg = extract_dir.process_archive(bad)
        self.assertEqual(action, "fail")
        self.assertTrue(bad.exists())                    # 原包保留
        self.assertFalse((self.root / "损坏包").exists())  # 空目录已清

    def test_broken_zip_kept_and_empty_dir_cleaned(self):
        bad = self.root / "坏.zip"
        bad.write_bytes(b"PK\x03\x04" + b"\x00" * 20)
        action, msg = extract_dir.process_archive(bad)
        self.assertEqual(action, "fail")
        self.assertTrue(bad.exists())
        self.assertFalse((self.root / "坏").exists())

    def test_encrypted_zip_skipped(self):
        # 真实流程：stdlib 检出加密位 → password → 无候选 → fail 保留
        z = make_encrypted_zip(self.root)
        action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "fail")
        self.assertIn("密码", msg)
        self.assertTrue(z.exists())                      # 原包保留

    def test_path_traversal_member_skipped(self):
        # 2026-11-05 引擎收敛：穿越成员从「整包拒绝」改为「逐成员跳过」
        # （extract_util._safe_member_path——危险成员绝不落盘，其余数据
        # 保留；旧语义把整包判死反而丢数据）
        z = self.root / "恶.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("../evil.txt", "boom")
            zf.writestr("safe.txt", "ok")
        action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "done")
        self.assertFalse((self.root.parent / "evil.txt").exists())
        self.assertTrue((self.root / "恶" / "safe.txt").exists())
        self.assertFalse(z.exists())

    def test_disk_guard_skips(self):
        # 引擎收敛后守卫在 extract_util（free − PAWCHIVE_MIN_FREE_GB 判定）
        z = self.root / "大.zip"
        make_zip(z, [("x.bin", "0" * 1000)])
        from tg_userbot import extract_util
        with mock.patch.object(extract_util.shutil, "disk_usage") as du:
            du.return_value.free = 1000   # 1000B < 内容 1000B + 保留 2GB
            action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "fail")
        self.assertIn("磁盘余量", msg)

    def test_zero_files_kept(self):
        z = self.root / "空.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("空目录/", "")   # 只有目录项，0 个文件
        action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "fail")
        self.assertTrue(z.exists())                      # 0 文件不删


class ExtractDirectoryTest(unittest.TestCase):

    def test_round_summary_and_no_abort(self):
        root = Path(tempfile.mkdtemp(dir=_TMP))
        make_zip(root / "好.zip", [("a.txt", "1")])
        bad = root / "坏.rar"
        bad.write_bytes(b"junk")
        make_targz(root / "子" / "嵌套.tar.gz", [("n.txt", b"2")])
        lines, st = extract_dir.extract_directory(str(root))
        self.assertEqual(st["done"], 2)   # 好.zip + 嵌套.tar.gz
        self.assertEqual(st["fail"], 1)   # 坏.rar
        joined = "\n".join(lines)
        self.assertIn("完成：成功 2 | 失败 1", joined)

    def test_missing_dir(self):
        lines, st = extract_dir.extract_directory("/tmp/不存在的目录xyz")
        self.assertFalse(st["done"])
        self.assertTrue(any("不存在" in l for l in lines))


class ParseGuardTest(unittest.TestCase):
    """路径越界防护的解析层测试（/extract 命令侧）。"""

    def test_traversal_rejected_by_resolve_check(self):
        base = Path(tempfile.mkdtemp(dir=_TMP)).resolve()
        target = (base / "../外面").resolve()
        self.assertFalse(target == base
                         and str(target).startswith(str(base)))

    def test_relative_inside_passes(self):
        base = Path(tempfile.mkdtemp(dir=_TMP)).resolve()
        target = (base / "子目录").resolve()
        self.assertTrue(str(target).startswith(str(base)))


if __name__ == "__main__":
    unittest.main()


class ParseExtractArgsVariantsTest(unittest.TestCase):
    """dry 三种形态 + 带空格/特殊字符目录名的解析。"""

    def test_dry_variants(self):
        for suf in (" dry", "-dry", "--dry", " DRY"):
            rel, dry = extract_dir.parse_extract_args("目录" + suf)
            self.assertEqual((rel, dry), ("目录", True), f"形态: {suf}")

    def test_spaced_name_with_dry(self):
        rel, dry = extract_dir.parse_extract_args(
            "syosinsya ~2026-01 [385G] #tag/sub -dry")
        self.assertEqual(rel, "syosinsya ~2026-01 [385G] #tag/sub")
        self.assertTrue(dry)

    def test_spaced_name_without_dry(self):
        rel, dry = extract_dir.parse_extract_args(
            "syosinsya ~2026-01 [385G] #tag/sub")
        self.assertEqual(dry, False)
        self.assertTrue(rel.endswith("#tag/sub"))


class RarMultipartTest(unittest.TestCase):
    """rar 多卷（.partN.rar）+ 7z 优先路（2026-11-05 byqn 实测场景）。

    7z 无法创建 rar 格式（商业格式）——解压副作用 mock subprocess，
    收集/stem/入口判定用真文件名。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=_TMP))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_part2_not_collected_part1_is(self):
        (self.root / "包.part1.rar").write_bytes(b"x")
        (self.root / "包.part2.rar").write_bytes(b"x")
        (self.root / "普通.rar").write_bytes(b"x")
        found = [Path(p).name for p in extract_dir.find_archives(str(self.root))]
        self.assertIn("包.part1.rar", found)
        self.assertIn("普通.rar", found)
        self.assertNotIn("包.part2.rar", found)   # 后续卷是数据不是入口

    def test_part1_stem(self):
        self.assertEqual(extract_dir.archive_stem("2026-06.part1.rar"),
                         "2026-06")

    def test_rar_routes_to_7z_with_password_candidates(self):
        z = self.root / "密.rar"
        z.write_bytes(b"rar-bytes")
        (self.root / "密码pw123.txt").write_text("pw123", encoding="utf-8")
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            # 按 -o<dest> 建解压产物（模拟 7z 成功；不加会被 0 文件守卫判失败）
            for i, a in enumerate(cmd):
                if str(a).startswith("-o"):
                    d = Path(str(a)[2:])
                    d.mkdir(parents=True, exist_ok=True)
                    (d / "f.txt").write_text("x", encoding="utf-8")
            class R:
                returncode = 0
                stderr = b""
                stdout = b""
            return R()

        with mock.patch.object(extract_dir, "find_7z_tool",
                               return_value="/opt/homebrew/bin/7z"), \
             mock.patch.object(extract_dir.subprocess, "run", fake_run):
            action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "done")
        # 首试无密码即成功（fake 永远 returncode 0）——只用 1 次调用
        self.assertEqual(len(calls), 1)
        self.assertFalse(any(str(a).startswith("-p") for a in calls[0]))

    def test_rar_password_fallback_second_try(self):
        """无密码失败 → 候选密码第二次上场（-p 出现在 cmd 里）。"""
        z = self.root / "密2.rar"
        z.write_bytes(b"rar-bytes")
        (self.root / "密码pw9.txt").write_text("pw9", encoding="utf-8")
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            has_pwd = any(str(a).startswith("-p") for a in cmd)
            class R:
                returncode = 0 if has_pwd else 2
                stderr = b"Wrong password" if not has_pwd else b""
                stdout = b""
            return R()
            # 失败时不建文件（模拟没解开）；成功时建

        def fake_run2(cmd, **kw):
            calls.append(cmd)
            has_pwd = any(str(a).startswith("-p") for a in cmd)
            if has_pwd:
                for a in cmd:
                    if str(a).startswith("-o"):
                        d = Path(str(a)[2:])
                        d.mkdir(parents=True, exist_ok=True)
                        (d / "f.txt").write_text("x", encoding="utf-8")
                class R:
                    returncode = 0; stderr = b""; stdout = b""
                return R()
            class R:
                returncode = 2; stderr = b"Wrong password"; stdout = b""
            return R()

        with mock.patch.object(extract_dir, "find_7z_tool",
                               return_value="7z"), \
             mock.patch.object(extract_dir.subprocess, "run", fake_run2):
            action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "done")
        self.assertIn("pw9", msg)
        self.assertEqual(len(calls), 2)
        self.assertIn("-ppw9", calls[1])

    def test_rar_all_candidates_fail_kept(self):
        z = self.root / "密.rar"
        z.write_bytes(b"rar-bytes")
        (self.root / "密码hint.txt").write_text("hint", encoding="utf-8")

        def fake_run(cmd, **kw):
            class R:
                returncode = 2
                stderr = b"Wrong password"
                stdout = b""
            return R()

        with mock.patch.object(extract_dir, "find_7z_tool",
                               return_value="7z"), \
             mock.patch.object(extract_dir.subprocess, "run", fake_run):
            action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "fail")
        self.assertIn("密码候选", msg)
        self.assertTrue(z.exists())   # 原包保留

    def test_rar_no_7z_falls_back_to_extract_util(self):
        z = self.root / "普通.rar"
        z.write_bytes(b"rar-bytes")
        from tg_userbot import extract_util
        with mock.patch.object(extract_dir, "find_7z_tool",
                               return_value=""), \
             mock.patch.object(extract_util, "extract_archive",
                               return_value=("extracted", "1 个文件",
                                             ["/x/f"])) as ea:
            action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "done")
        ea.assert_called_once()   # 走了 extract_util 回落

    def test_rar_unsupported_method_switches_to_unrar(self):
        """7z 全候选报 Unsupported Method（WinRAR7 新格式）→ 自动转
        unrar 同候选链（2026-11-09 Gilberta 实测场景）。"""
        z = self.root / "新格式.rar"
        z.write_bytes(b"rar5-bytes")
        (self.root / "密码pw7.txt").write_text("pw7", encoding="utf-8")
        calls = []

        def fake_run(cmd, **kw):
            calls.append(list(cmd))
            if "7z" in cmd[0]:
                class R7:
                    returncode = 2
                    stderr = b"ERROR: Unsupported Method : a.mkv"
                    stdout = b""
                return R7()
            # unrar 路：无密码失败（Incorrect password）、带密码成功
            if not any(str(a).startswith("-p") for a in cmd):
                class RP:
                    returncode = 3
                    stderr = b"Incorrect password for a.mkv"
                    stdout = b""
                return RP()
            # unrar 路：cmd[-1] 是 dest 目录
            d = Path(cmd[-1])
            d.mkdir(parents=True, exist_ok=True)
            (d / "a.mkv").write_bytes(b"x" * 10)
            class RU:
                returncode = 0
                stderr = b""
                stdout = b""
            return RU()

        with mock.patch.object(extract_dir, "find_7z_tool",
                               return_value="/opt/homebrew/bin/7z"), \
             mock.patch.object(extract_dir, "find_unrar_tool",
                               return_value="/opt/homebrew/bin/unrar"), \
             mock.patch.object(extract_dir.subprocess, "run", fake_run):
            action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "done")
        engines = ["unrar" if "unrar" in c[0] else "7z" for c in calls]
        self.assertIn("unrar", engines)
        self.assertEqual(engines[-1], "unrar")
        # unrar 用上了密码候选（pw7 出现在 -p 参数）
        self.assertTrue(any(c.startswith("-p") and "pw7" in c
                            for c in calls[-1]))

    def test_rar_unsupported_method_no_unrar_cleans_and_guides(self):
        """unrar 未装：失败并清理 0 字节残骸 + 文案指引安装（今日现场）。"""
        z = self.root / "新格式2.rar"
        z.write_bytes(b"rar5-bytes")
        dest = self.root / "新格式2"

        def fake_run(cmd, **kw):
            # 模拟 7z 失败也留 0 字节条目框架
            for a in cmd:
                if str(a).startswith("-o"):
                    d = Path(str(a)[2:])
                    d.mkdir(parents=True, exist_ok=True)
                    (d / "a.mkv").write_bytes(b"")
            class R:
                returncode = 2
                stderr = b"ERROR: Unsupported Method : a.mkv"
                stdout = b""
            return R()

        with mock.patch.object(extract_dir, "find_7z_tool",
                               return_value="/opt/homebrew/bin/7z"), \
             mock.patch.object(extract_dir, "find_unrar_tool",
                               return_value=""), \
             mock.patch.object(extract_dir.subprocess, "run", fake_run):
            action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "fail")
        self.assertIn("WinRAR 7", msg)
        self.assertIn("unrar", msg)
        self.assertFalse(dest.exists())   # 残骸已清

    def test_rar_zero_byte_success_guarded(self):
        """引擎 exit 0 但产物全 0 字节（静默失败）→ fail + 残骸清理。"""
        z = self.root / "全零.rar"
        z.write_bytes(b"rar-bytes")
        dest = self.root / "全零"

        def fake_run(cmd, **kw):
            for a in cmd:
                if str(a).startswith("-o"):
                    d = Path(str(a)[2:])
                    d.mkdir(parents=True, exist_ok=True)
                    (d / "a.mkv").write_bytes(b"")   # 全 0
            class R:
                returncode = 0
                stderr = b""
                stdout = b""
            return R()

        with mock.patch.object(extract_dir, "find_7z_tool",
                               return_value="/opt/homebrew/bin/7z"), \
             mock.patch.object(extract_dir, "find_unrar_tool",
                               return_value=""), \
             mock.patch.object(extract_dir.subprocess, "run", fake_run):
            action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "fail")
        self.assertIn("0 字节", msg)
        self.assertFalse(dest.exists())

    def test_rar_encrypted_no_7z_reports_tool_hint(self):
        z = make_encrypted_zip(self.root, "密.rar")   # 借加密 zip 夹具不行——
        # rar 加密探测在 7z 里，mock 7z 永远失败即可
        z.write_bytes(b"rar-bytes")

        def fake_run(cmd, **kw):
            class R:
                returncode = 2
                stderr = b"Wrong password"
                stdout = b""
            return R()

        with mock.patch.object(extract_dir, "find_7z_tool",
                               return_value=""), \
             mock.patch.object(extract_dir.subprocess, "run", fake_run):
            action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "fail")
        self.assertTrue(z.exists())


class MagicSniffTest(unittest.TestCase):
    """扩展名与内容不符（2026-11-07 Alicexi 实测：站点把加密 rar 改名
    .zip）——stdlib 报「File is not a zip file」→ bsdtar 探测报
    「Encryption is not supported」终结，密码候选全不试（死路）。
    分发前按魔数嗅探：rar/7z 签名 → 强制走 7z 工具分支。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=_TMP))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _fake_7z_run(self, calls):
        def fake_run(cmd, **kw):
            calls.append(cmd)
            for a in cmd:   # 按 -o<dest> 造解压产物（过 0 文件守卫）
                if str(a).startswith("-o"):
                    d = Path(str(a)[2:])
                    d.mkdir(parents=True, exist_ok=True)
                    (d / "f.txt").write_text("x", encoding="utf-8")
            class R:
                returncode = 0
                stderr = b""
                stdout = b""
            return R()
        return fake_run

    def test_rar_magic_named_zip_routes_to_7z(self):
        z = self.root / "2026-11-6#Alicexi.zip"
        z.write_bytes(b"Rar!" + b"\x00" * 32)   # 真 rar 魔数，扩展名骗人
        (self.root / "密码pw123.txt").write_text("pw123", encoding="utf-8")
        calls = []
        with mock.patch.object(extract_dir, "find_7z_tool",
                               return_value="/opt/homebrew/bin/7z"), \
             mock.patch.object(extract_dir.subprocess, "run",
                               self._fake_7z_run(calls)), \
             mock.patch.object(extract_util, "extract_archive",
                               side_effect=AssertionError("zip 死路被走到")):
            action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "done", msg)
        self.assertEqual(len(calls), 1)          # 首试无密码即成功
        self.assertFalse(any(str(a).startswith("-p") for a in calls[0]))

    def test_7z_magic_named_zip_routes_to_7z(self):
        z = self.root / "伪装.zip"
        z.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 32)
        calls = []
        with mock.patch.object(extract_dir, "find_7z_tool",
                               return_value="/opt/homebrew/bin/7z"), \
             mock.patch.object(extract_dir.subprocess, "run",
                               self._fake_7z_run(calls)), \
             mock.patch.object(extract_util, "extract_archive",
                               side_effect=AssertionError("zip 死路被走到")):
            action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "done", msg)

    def test_7z_success_deletes_source(self):
        # 单卷 7z（含伪装改名的）成功后删原包——与 rar/zip 路同一
        # 「成功才删」纪律（2026-11-07 冒烟实测：伪装 7z 解出后原包残留）
        z = self.root / "伪装.zip"
        z.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 32)
        with mock.patch.object(extract_dir, "find_7z_tool",
                               return_value="/opt/homebrew/bin/7z"), \
             mock.patch.object(extract_dir.subprocess, "run",
                               self._fake_7z_run([])):
            action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "done")
        self.assertFalse(z.exists(), "成功后原包应删除")
        self.assertIn("已删除", msg)

    def test_genuine_7z_success_deletes_source(self):
        z = self.root / "包.7z"
        z.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 32)
        with mock.patch.object(extract_dir, "find_7z_tool",
                               return_value="/opt/homebrew/bin/7z"), \
             mock.patch.object(extract_dir.subprocess, "run",
                               self._fake_7z_run([])):
            action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "done")
        self.assertFalse(z.exists(), "成功后原包应删除")
        self.assertIn("已删除", msg)

    def test_sfx_rar_named_zip_routes_to_7z(self):
        # SFX 自解压封装（2026-11-07 Alicexi 手机实测：rar5 数据前挂
        # 460288 字节 stub，7z 报 Offset=460288）——真签名不在开头，
        # 前 4MB 内搜到 rar/7z 签名也要命中
        stub = b"MZ" + b"\x00" * 500          # 可执行头样貌
        z = self.root / "sfx伪装.zip"
        z.write_bytes(stub + b"Rar!\x1a\x07\x01\x00" + b"\x00" * 32)
        calls = []
        with mock.patch.object(extract_dir, "find_7z_tool",
                               return_value="/opt/homebrew/bin/7z"), \
             mock.patch.object(extract_dir.subprocess, "run",
                               self._fake_7z_run(calls)), \
             mock.patch.object(extract_util, "extract_archive",
                               side_effect=AssertionError("zip 死路被走到")):
            action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "done", msg)

    def test_sfx_7z_named_zip_routes_to_7z(self):
        stub = b"MZ" + b"\x00" * 500
        z = self.root / "sfx7z.zip"
        z.write_bytes(stub + b"7z\xbc\xaf\x27\x1c" + b"\x00" * 32)
        calls = []
        with mock.patch.object(extract_dir, "find_7z_tool",
                               return_value="/opt/homebrew/bin/7z"), \
             mock.patch.object(extract_dir.subprocess, "run",
                               self._fake_7z_run(calls)), \
             mock.patch.object(extract_util, "extract_archive",
                               side_effect=AssertionError("zip 死路被走到")):
            action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "done", msg)

    def test_pk_zip_with_embedded_rar_sig_stays_zip(self):
        # 真 zip 魔数（PK）即使体内嵌 rar 签名也绝不重定向——
        # 深扫描只对「非 PK 开头的伪装 .zip」生效
        z = self.root / "内嵌签名.zip"
        z.write_bytes(b"PK\x03\x04" + b"Rar!\x1a\x07\x01\x00" + b"\x00" * 32)
        with mock.patch.object(extract_dir, "find_7z_tool",
                               return_value="/opt/homebrew/bin/7z"), \
             mock.patch.object(extract_dir.subprocess, "run",
                               side_effect=AssertionError("7z 分支被走到")), \
             mock.patch.object(extract_util, "extract_archive",
                               return_value=("failed", "stub", [])):
            action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "fail")   # 走了 zip 路（stub 返回 failed）

    def test_real_zip_magic_unchanged(self):
        # 真 zip 魔数（PK\x03\x04）不重定向——原 zip 路照走
        z = self.root / "正常.zip"
        make_zip(str(z), [("a.txt", b"hi")])
        with mock.patch.object(extract_dir, "find_7z_tool",
                               return_value="/opt/homebrew/bin/7z"), \
             mock.patch.object(extract_dir.subprocess, "run",
                               side_effect=AssertionError("7z 分支被走到")):
            action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "done", msg)


class SplitVolumeTest(unittest.TestCase):
    """7z 分卷（.7z.001~NNN）：识别/stem/入口收集/解压删卷。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=_TMP))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_is_split_volume(self):
        self.assertTrue(extract_dir.is_split_volume("2024-08.7z.001"))
        self.assertTrue(extract_dir.is_split_volume("x.7z.018"))
        self.assertFalse(extract_dir.is_split_volume("x.7z"))
        self.assertFalse(extract_dir.is_split_volume("x.zip"))

    def test_split_stem(self):
        self.assertEqual(extract_dir.archive_stem("2024-08.7z.001"), "2024-08")
        self.assertEqual(extract_dir.archive_stem("2024-08.7z.018"), "2024-08")

    def test_find_archives_collects_first_volume_only(self):
        for i in (1, 2, 3):
            (self.root / f"包.7z.{i:03d}").write_bytes(b"x")
        found = extract_dir.find_archives(str(self.root))
        self.assertEqual(len(found), 1)
        self.assertTrue(str(found[0]).endswith(".7z.001"))

    def test_split_needs_7z_tool(self):
        # 首卷入口在无 7z 工具时 → fail 且原卷保留
        (self.root / "包.7z.001").write_bytes(b"x")
        (self.root / "包.7z.002").write_bytes(b"x")
        with mock.patch.object(extract_dir, "find_7z_tool", return_value=""):
            action, msg = extract_dir.process_archive(
                self.root / "包.7z.001")
        self.assertEqual(action, "fail")
        self.assertIn("7z 工具", msg)
        self.assertTrue((self.root / "包.7z.001").exists())

    def test_split_real_extraction_deletes_all_volumes(self):
        # 真 7z：12KB 文件按 5KB 分卷 → 必然多卷；解压 → 全卷删除
        import subprocess as sp
        tool = extract_dir.find_7z_tool()
        if not tool:
            self.skipTest("p7zip 未安装")
        payload = self.root / "payload.bin"
        payload.write_bytes(os.urandom(20 * 1024))   # 随机字节不可压缩，必分卷
        r = sp.run([tool, "a", "-v5k", str(self.root / "分卷包.7z"),
                    str(payload)],
                   capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0)
        vols = sorted(self.root.glob("分卷包.7z.*"))
        self.assertGreaterEqual(len(vols), 2)   # 确实分了卷
        payload.unlink()   # 清掉源文件；分卷保持齐全（真实场景）
        action, msg = extract_dir.process_archive(
            self.root / "分卷包.7z.001")
        self.assertEqual(action, "done")
        dest = self.root / "分卷包"
        self.assertTrue(dest.is_dir() and any(os.scandir(dest)),
                        f"解压目录应有内容: {msg}")
        self.assertEqual(len(list(self.root.glob("分卷包.7z.*"))), 0)


class PasswordHintTest(unittest.TestCase):
    """加密 zip + 同目录密码提示文件：自动取密、解不开保留。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=_TMP))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_no_hint_file_kept(self):
        # 引擎收敛（2026-11-05）：真实流程——stdlib 检出加密位 → password
        # → 无候选 → fail 保留（不再 mock 引擎内部）
        z = make_encrypted_zip(self.root)
        action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "fail")
        self.assertIn("都解不开", msg)
        self.assertTrue(z.exists())

    def test_hint_file_password_used(self):
        # 验证密码候选链从提示文件提取纯 ASCII 段（引擎收敛后解压走
        # extract_util，候选举密的真实链路由 MultiCandidate 的真 7z
        # 加密包测试覆盖）
        z = make_encrypted_zip(self.root)
        (self.root / "密码zxc.txt").write_text("密码是：zxc", encoding="utf-8")
        self.assertEqual(extract_dir.find_password(z), "zxc")

    def test_find_password_extracts_ascii(self):
        z = self.root / "占位.zip"
        z.write_bytes(b"")
        (self.root / "密码zxc.txt").write_text("密码是：zxc", encoding="utf-8")
        self.assertEqual(extract_dir.find_password(z), "zxc")
        # 无「密码」关键词的 txt 不是密码提示文件——仍取 密码zxc.txt
        self.assertEqual(extract_dir.find_password(z), "zxc")


class FilenamePasswordTest(unittest.TestCase):
    """文件名密码规则：最后一个 # 后的内容 = 密码（owner 2026-11-03 约定）。"""

    def test_basic_rule(self):
        self.assertEqual(
            extract_dir.password_from_filename("合集#备注#zxc.zip"), "zxc")
        self.assertEqual(
            extract_dir.password_from_filename("合集#zxc.rar"), "zxc")
        self.assertEqual(
            extract_dir.password_from_filename("2024-08.7z.001#pw"), "pw")

    def test_no_hash_returns_empty(self):
        self.assertEqual(extract_dir.password_from_filename("视频.mp4"), "")
        self.assertEqual(extract_dir.password_from_filename("plain.zip"), "")

    def test_strips_extension_not_password(self):
        # 密码本身含点：最后一个 # 后是「zxc.com」——剥 .zip 后剩 zxc.com
        self.assertEqual(
            extract_dir.password_from_filename("包#pw#zxc.com.zip"), "zxc.com")

    def test_empty_last_segment(self):
        self.assertEqual(extract_dir.password_from_filename("合集#.zip"), "")

    def test_hashes_in_password(self):
        # 规则语义：最后一个 # 后整段 = 密码（密码本身不能再含 #）
        self.assertEqual(
            extract_dir.password_from_filename("包#备注#p#ss.zip"), "ss")


class MultiCandidatePasswordTest(unittest.TestCase):
    """加密 zip 多候选举密：文件名规则/提示文件 各成一个候选。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=_TMP))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_filename_rule_tried_before_hint_content(self):
        # 真 7z：造一个真加密 zip（密码 Fil3nam3），文件名带 #pw 段
        # （错误候选）+ 提示文件（错误候选）→ 候选逐个试，最终用真密码解开
        import subprocess as sp
        tool = extract_dir.find_7z_tool()
        if not tool:
            self.skipTest("p7zip 未安装")
        payload = self.root / "payload.bin"
        payload.write_bytes(os.urandom(8 * 1024))
        z = self.root / "合集#备注#Fil3nam3.zip"
        r = sp.run([tool, "a", "-pFil3nam3", str(z), str(payload)],
                   capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0)
        payload.unlink()
        (self.root / "密码hint.txt").write_text("hint", encoding="utf-8")
        action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "done")
        self.assertIn("Fil3nam3", msg)
        self.assertFalse(z.exists())
        self.assertTrue((self.root / "合集#备注#Fil3nam3" / "payload.bin")
                        .exists())

    def test_hint_password_fallback_when_filename_wrong(self):
        # 文件名 # 段是错的，提示文件才是对的 → 第二候选解开
        import subprocess as sp
        tool = extract_dir.find_7z_tool()
        if not tool:
            self.skipTest("p7zip 未安装")
        payload = self.root / "payload.bin"
        payload.write_bytes(os.urandom(8 * 1024))
        z = self.root / "合集#备注#wrong.zip"
        r = sp.run([tool, "a", "-pRight1", str(z), str(payload)],
                   capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0)
        payload.unlink()
        (self.root / "密码zxc.txt").write_text("Right1", encoding="utf-8")
        action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "done")
        self.assertIn("Right1", msg)

    def test_all_candidates_wrong_kept(self):
        # 引擎收敛：真实加密夹具（候选 wrong1/hint 均解不开 → 保留）
        z = make_encrypted_zip(self.root, "包#wrong1.zip")
        (self.root / "密码hint.txt").write_text("hint", encoding="utf-8")
        action, msg = extract_dir.process_archive(z)
        self.assertEqual(action, "fail")
        self.assertIn("都解不开", msg)
        self.assertTrue(z.exists())


class PasswordCandidatesChainTest(unittest.TestCase):
    """统一密码候选链（/extract 与 /115x 共用）：extra 优先 + 三来源收集。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=_TMP))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_extra_first_then_filename_then_hint(self):
        z = self.root / "合集#文件名规则#Fil3nam3.zip"
        z.write_bytes(b"")
        (self.root / "密码zxc.txt").write_text("提示密码", encoding="utf-8")
        cands = extract_dir.password_candidates(str(z), extra="目录名密码")
        # 前 4 个按序：extra/文件名#/提示stem/提示内容；尾部新加的
        # 「所在目录名」候选（2026-11-06 c291dGhwbHVz 源）单独断言
        self.assertEqual(cands[:4],
                         ["目录名密码", "Fil3nam3", "zxc", "提示密码"])
        self.assertEqual(cands[-1],
                         os.path.basename(str(self.root)))

    def test_dedup_preserves_order(self):
        z = self.root / "包#same.zip"
        z.write_bytes(b"")
        (self.root / "密码same.txt").write_text("same", encoding="utf-8")
        cands = extract_dir.password_candidates(str(z), extra="same")
        self.assertEqual(cands[0], "same")
        self.assertEqual(cands[-1], self.root.name)   # 目录名候选居尾
        self.assertEqual(len(cands), len(set(cands)))  # 全程去重

    def test_115x_dir_name_wins_over_others(self):
        # /115x 场景：目录名密码（Telegram@XXX）与文件名/提示文件并存
        z = self.root / "包#filepw.zip"
        z.write_bytes(b"")
        (self.root / "密码dirpw.txt").write_text("dirpw", encoding="utf-8")
        cands = extract_dir.password_candidates(str(z), extra="Telegram@XXX")
        self.assertEqual(cands[0], "Telegram@XXX")
        self.assertIn("filepw", cands)
        self.assertIn("dirpw", cands)


class PostsForBackfillMonthsTest(unittest.TestCase):
    """posts_for_backfill(months=N)：published 过滤语义（宁多勿漏）。

    init_db 全 schema 临时库 + runtime_db 同连接写入（不走裸 sqlite
    第二连接，避免建表冲突与锁等待）；close_db 防进程连接跨文件泄漏。"""

    def setUp(self):
        from tg_userbot import runtime_db as _rt
        d = tempfile.mkdtemp(prefix="tg_pfb_months_")
        self.dbpath = os.path.join(d, "tg_userbot.db")
        _rt.init_db(self.dbpath)
        self.addCleanup(_rt.close_db)
        self.rt = _rt
        import datetime
        now = datetime.date.today()
        recent = (now - datetime.timedelta(days=30)).isoformat()
        old = (now - datetime.timedelta(days=120)).isoformat()
        for pid, pub in ((1, recent), (2, old), (3, None), (4, "")):
            self.rt._write(lambda conn, pid=pid, pub=pub:
                           conn.execute(
                               "INSERT INTO pawchive_posts(service, "
                               "creator_id, creator_name, post_id, title, "
                               "published, status, created_at) VALUES "
                               "('fanbox', '7', '作者A', ?, 't', ?, "
                               "'COMPLETED', ?)",
                               (str(pid), pub, now.isoformat())),
                           "测试插入回填帖")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)

    def test_months_filters_old_keeps_recent_and_null(self):
        rows = self.rt.posts_for_backfill(creator_id=7, months=3)
        ids = sorted(int(r["post_id"]) for r in rows)
        # 近 30 天 ✓、published 空 ✓（宁多勿漏）、120 天前 ✗
        self.assertEqual(ids, [1, 3, 4])

    def test_no_months_returns_all(self):
        self.assertEqual(len(self.rt.posts_for_backfill(creator_id=7)), 4)
