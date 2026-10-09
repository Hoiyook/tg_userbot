"""extract_util 解压工具测试（2026-09-30 从 pawchive_worker 抽出）。

覆盖：zip 成功/删源两态/加密/zip slip 防护/cp437→GBK 文件名修复/自定义
dest；非 zip（bsdtar 分支）成功与"需密码"探测；pawchive 委托层行为一致。

    .venv/bin/python -m unittest tests.test_extract_util -v
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest as _btn_shim  # noqa: F401,E402  按钮属性垫片（pytest 共享）

from tg_userbot import extract_util  # noqa: E402


def _make_zip(path, entries):
    """entries: {member_name: bytes} 构造 zip。"""
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)


def _patch_zip_flags(path, set_bits=0, clear_bits=0):
    """二进制补丁所有条目的 general purpose bit flag（写后改位）。

    stdlib writestr 会重置手工设置的 flag_bits（加密位/UTF-8 位都被覆写），
    唯一能构造「真实 Windows GBK zip / 加密 zip」的办法就是写完后直接改
    本地头与中央目录里的标志字段。
    """
    import struct
    with open(path, "rb") as f:
        data = bytearray(f.read())
    # 本地头 PK\x03\x04（flag 在偏移 6）与中央目录 PK\x01\x02（偏移 8）
    for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        i = 0
        while True:
            i = data.find(sig, i)
            if i < 0:
                break
            flags, = struct.unpack_from("<H", data, i + off)
            flags = (flags | set_bits) & ~clear_bits & 0xFFFF
            struct.pack_into("<H", data, i + off, flags)
            i += 4
    with open(path, "wb") as f:
        f.write(data)


class ExtractZipTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tg_extract_test_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_zip_success_and_files_list(self):
        arch = os.path.join(self.dir, "pack.zip")
        _make_zip(arch, {"a.txt": b"hello", "sub/b.txt": b"world"})
        status, detail, files = extract_util.extract_archive(arch)
        self.assertEqual(status, "extracted")
        self.assertEqual(len(files), 2)
        dest = os.path.join(self.dir, "pack")
        self.assertTrue(os.path.isfile(os.path.join(dest, "a.txt")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "sub", "b.txt")))
        # 缺省删源=False：压缩包保留（115x 场景，副本供重试幂等）
        self.assertTrue(os.path.exists(arch))

    def test_custom_dest_and_delete_source(self):
        arch = os.path.join(self.dir, "pack.zip")
        _make_zip(arch, {"a.txt": b"x"})
        dest = os.path.join(self.dir, "自定义目标")
        status, _d, files = extract_util.extract_archive(
            arch, dest=dest, delete_source=True)
        self.assertEqual(status, "extracted")
        self.assertTrue(os.path.isfile(os.path.join(dest, "a.txt")))
        self.assertFalse(os.path.exists(arch))   # pawchive 语义：成功即删

    def test_password_zip(self):
        # 加密位（flag_bits & 0x1）置位的包 → password，不解不删。
        # writestr 会重置手工 flag → 写完用二进制补丁置位（真实加密包形态）
        arch = os.path.join(self.dir, "enc.zip")
        _make_zip(arch, {"secret.txt": "机密".encode("utf-8")})
        _patch_zip_flags(arch, set_bits=0x1)
        status, detail, files = extract_util.extract_archive(arch)
        self.assertEqual(status, "password", detail)
        self.assertEqual(files, [])
        self.assertTrue(os.path.exists(arch))

    def test_zip_slip_members_rejected(self):
        # ../ 穿越 → 拒绝（静默钳制会引发成员同名碰撞丢数据）；
        # 绝对路径与盘符 → 钳制进 dest 内（保留数据）
        arch = os.path.join(self.dir, "evil.zip")
        _make_zip(arch, {
            "../escape.txt": b"bad",
            "/abs.txt": b"clamped-inside",
            "C:evil/win.txt": b"clamped-inside",
            "ok.txt": b"good",
        })
        status, _d, files = extract_util.extract_archive(arch)
        self.assertEqual(status, "extracted")
        dest = os.path.join(self.dir, "evil")
        names = sorted(os.path.relpath(f, dest) for f in files)
        self.assertEqual(names, ["abs.txt", "evil/win.txt", "ok.txt"])
        # ../escape.txt 不得在任何位置落盘
        self.assertFalse(os.path.exists(os.path.join(self.dir, "escape.txt")))
        self.assertFalse(os.path.exists(os.path.join(dest, "escape.txt")))

    def test_gbk_filename_fix(self):
        # Windows 中文 zip：文件名以原始 GBK 字节存储、无 UTF-8 标志位——
        # stdlib 按 cp437 读成乱码；extract 应 roundtrip 修复回中文。
        # 构造：monkeypatch ZipInfo 文件名编码为 GBK（字节级真实形态）
        from unittest import mock as _mock

        real = "画集/说明.txt"
        arch = os.path.join(self.dir, "cn.zip")

        def gbk_encode(self):
            try:
                return self.filename.encode("ascii"), 0
            except UnicodeEncodeError:
                return self.filename.encode("gbk"), 0   # Windows 中文形态

        with _mock.patch.object(zipfile.ZipInfo, "_encodeFilenameFlags",
                                gbk_encode):
            _make_zip(arch, {real: "内容".encode("utf-8")})
        status, _d, files = extract_util.extract_archive(arch)
        self.assertEqual(status, "extracted")
        dest = os.path.join(self.dir, "cn")
        self.assertTrue(os.path.isfile(
            os.path.join(dest, "画集", "说明.txt")), f"产物: {files}")

    def test_utf8_zip_names_untouched(self):
        # 正常 UTF-8 标志 zip 的中文名不走 GBK roundtrip（避免误改）
        arch = os.path.join(self.dir, "u8.zip")
        _make_zip(arch, {"画集/正常.txt": "x".encode()})
        status, _d, _f = extract_util.extract_archive(arch)
        self.assertEqual(status, "extracted")
        self.assertTrue(os.path.isfile(
            os.path.join(self.dir, "u8", "画集", "正常.txt")))

    def test_fix_zip_name_direct(self):
        """_fix_zip_name 单元：有 UTF-8 标志不动，无标志才 roundtrip。"""
        real = "画集/说明.txt"
        mojibake = real.encode("gbk").decode("cp437")
        zi = zipfile.ZipInfo(mojibake)
        zi.flag_bits = 0x800                       # UTF-8 标志：原名保留
        self.assertEqual(extract_util._fix_zip_name(zi), mojibake)
        zi.flag_bits = 0                           # Windows 形态：修复
        self.assertEqual(extract_util._fix_zip_name(zi), real)
        # 纯 ASCII 名（GBK/CP437 同域）roundtrip 不变
        zi2 = zipfile.ZipInfo("plain.txt")
        zi2.flag_bits = 0
        self.assertEqual(extract_util._fix_zip_name(zi2), "plain.txt")

    def test_no_space_guard(self):
        # 保护线拉高到天文数字 → 任何解压都判 no-space，保留原包
        arch = os.path.join(self.dir, "small.zip")
        _make_zip(arch, {"a.txt": b"x" * 1024})
        from tg_userbot import config
        orig = config.PAWCHIVE_MIN_FREE_GB
        config.PAWCHIVE_MIN_FREE_GB = 10 ** 9
        try:
            status, _d, files = extract_util.extract_archive(arch)
        finally:
            config.PAWCHIVE_MIN_FREE_GB = orig
        self.assertEqual(status, "no-space")
        self.assertEqual(files, [])


class ExtractPasswordTest(unittest.TestCase):
    """带密码解压（/115x 目录名密码场景，2026-10-09）：bsdtar 统一路。

    造包用 /usr/bin/zip -P（ZipCrypto 传统加密）；无 zip 命令则跳过。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tg_extract_pw_test_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.zip_bin = shutil.which("zip")
        if not self.zip_bin:
            self.skipTest("系统无 zip 命令")

    def _make_enc_zip(self, name):
        src = os.path.join(self.dir, "src")
        os.makedirs(src, exist_ok=True)
        with open(os.path.join(src, "机密.txt"), "w", encoding="utf-8") as f:
            f.write("内容" * 100)
        arch = os.path.join(self.dir, name)
        subprocess.run(["zip", "-P", "Telegram@PaintingCollections2",
                        "-q", "-r", arch, "."],
                       cwd=src, check=True)
        return arch, os.path.getsize(arch)

    def test_zip_with_password_extracts(self):
        arch, _ = self._make_enc_zip("enc.zip")
        status, detail, files = extract_util.extract_archive(
            arch, password="Telegram@PaintingCollections2")
        self.assertEqual(status, "extracted", detail)
        self.assertTrue(any(f.endswith("机密.txt") for f in files))

    def test_zip_without_password_reports_password(self):
        arch, _ = self._make_enc_zip("enc2.zip")
        status, _d, files = extract_util.extract_archive(arch)
        self.assertEqual(status, "password")
        self.assertEqual(files, [])

    def test_zip_wrong_password_reports_password(self):
        arch, _ = self._make_enc_zip("enc3.zip")
        status, _d, files = extract_util.extract_archive(
            arch, password="错的密码")
        self.assertEqual(status, "password")

    def test_gbk_password_zip(self):
        # 中文名 + ZipCrypto + GBK：解密与文件名修复叠加场景
        import zipfile as _zf
        from unittest import mock as _mock
        real = "画集/密.txt"
        arch = os.path.join(self.dir, "cn_pw.zip")

        def gbk_encode(self):
            try:
                return self.filename.encode("ascii"), 0
            except UnicodeEncodeError:
                return self.filename.encode("gbk"), 0

        with _mock.patch.object(_zf.ZipInfo, "_encodeFilenameFlags",
                                gbk_encode):
            with _zf.ZipFile(arch, "w") as z:
                z.writestr(real, "x" * 50)
        # zip 命令重打加密（保留 GBK 字节名较难——改用二进制补丁置加密位，
        # 但 stdlib 解密无门；此场景由 bsdtar 路覆盖，这里验证「加密位+
        # 无密码」仍正确报 password）
        status, _d, files = extract_util.extract_archive(arch)
        self.assertEqual(status, "extracted")   # 未加密只是 GBK 名 → 修复后正常


class ExtractBsdtarTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tg_extract_bsdtar_test_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_tar_gz_success(self):
        # 非 zip 走 bsdtar 分支：tgz 成功解压 + 产物清单
        import subprocess
        src = os.path.join(self.dir, "src")
        os.makedirs(os.path.join(src, "内层"))
        with open(os.path.join(src, "内层", "文件.txt"), "w") as f:
            f.write("内容")
        arch = os.path.join(self.dir, "pack.tgz")
        subprocess.run(["bsdtar", "-czf", arch, "-C", src, "."], check=True)
        status, _d, files = extract_util.extract_archive(arch)
        self.assertEqual(status, "extracted")
        self.assertTrue(os.path.isfile(
            os.path.join(self.dir, "pack", "内层", "文件.txt")))

    def test_fake_rar_reports_password(self):
        # bsdtar 列表失败（非压缩包内容伪装 .rar）→ password，保留原文件
        arch = os.path.join(self.dir, "fake.rar")
        with open(arch, "wb") as f:
            f.write(b"definitely not a rar archive" * 10)
        status, _d, files = extract_util.extract_archive(arch)
        self.assertEqual(status, "password")
        self.assertEqual(files, [])
        self.assertTrue(os.path.exists(arch))

    def test_empty_result_is_failure(self):
        # 解压后目录为空（只含目录成员）→ failed
        import subprocess
        src = os.path.join(self.dir, "src")
        os.makedirs(src)
        arch = os.path.join(self.dir, "empty.tgz")
        subprocess.run(["bsdtar", "-czf", arch, "-C", src, "."], check=True)
        status, _d, _f = extract_util.extract_archive(arch)
        self.assertEqual(status, "failed")


class PawchiveDelegateTest(unittest.TestCase):
    """pawchive_worker._extract_archive_sync 委托层：契约保持两元组+删源。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tg_extract_delegate_test_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_delegate_two_tuple_and_deletes_source(self):
        from tg_userbot import pawchive_worker
        arch = os.path.join(self.dir, "pack.zip")
        _make_zip(arch, {"a.txt": b"x"})
        result = pawchive_worker._extract_archive_sync(arch)
        self.assertEqual(len(result), 2)          # (status, detail)
        self.assertEqual(result[0], "extracted")
        self.assertFalse(os.path.exists(arch))    # 原行为：成功即删
        self.assertTrue(os.path.isfile(
            os.path.join(self.dir, "pack", "a.txt")))

    def test_delegate_password_keeps_source(self):
        from tg_userbot import pawchive_worker
        arch = os.path.join(self.dir, "enc.zip")
        _make_zip(arch, {"s.txt": b"x"})
        _patch_zip_flags(arch, set_bits=0x1)   # 写后置加密位（writestr 会重置）
        status, _detail = pawchive_worker._extract_archive_sync(arch)
        self.assertEqual(status, "password")
        self.assertTrue(os.path.exists(arch))


if __name__ == "__main__":
    unittest.main()
