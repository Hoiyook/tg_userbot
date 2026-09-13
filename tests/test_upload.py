"""/up 文件上传（upload.py + 命令分发 + 输入窗口）的单元测试。

覆盖：路径解析（相对/~）、文件校验（存在/目录/大小上限）、最近文件列表、
进度文本与回执、上传流程（进度节流 + 失败兜底）、/up 命令分发。

运行方式（在项目根目录）：
    .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
"""
import asyncio
import atexit
import os
import shutil
import tempfile
import unittest
from unittest import mock

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_upload_test_")
# 退出时回收临时目录（测试跑完就地删，别让 /var/folders 越堆越多）
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import commands  # noqa: E402
from tg_userbot import config  # noqa: E402
from tg_userbot import state  # noqa: E402
from tg_userbot import upload  # noqa: E402


def _make_file(name, content=b"x", mtime=None):
    path = os.path.join(_TMP, name)
    with open(path, "wb") as f:
        f.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class ResolvePathTest(unittest.TestCase):
    def setUp(self):
        self._old = state.SHELL_CWD
        state.SHELL_CWD = _TMP

    def tearDown(self):
        state.SHELL_CWD = self._old

    def test_absolute_path_unchanged(self):
        p = os.path.join(_TMP, "a.txt")
        self.assertEqual(upload.resolve_path(p), p)

    def test_relative_resolves_against_shell_cwd(self):
        self.assertEqual(
            upload.resolve_path("a.txt"), os.path.join(_TMP, "a.txt")
        )

    def test_dotdot_normalized(self):
        p = upload.resolve_path("sub/../a.txt")
        self.assertEqual(p, os.path.join(_TMP, "a.txt"))

    def test_tilde_expanded(self):
        self.assertEqual(
            upload.resolve_path("~/foo.txt"),
            os.path.expanduser("~/foo.txt"),
        )

    def test_whitespace_stripped(self):
        self.assertEqual(
            upload.resolve_path("  a.txt  "), os.path.join(_TMP, "a.txt")
        )


class ValidateFileTest(unittest.TestCase):
    def test_missing_rejected(self):
        ok, msg = upload.validate_file("/nonexistent_up_test_path")
        self.assertFalse(ok)
        self.assertIn("❌", msg)

    def test_directory_rejected(self):
        ok, msg = upload.validate_file(_TMP)
        self.assertFalse(ok)
        self.assertIn("目录", msg)

    def test_oversize_rejected(self):
        path = _make_file("big.bin", b"x" * 10)
        with mock.patch.object(config, "TG_UPLOAD_MAX_BYTES", 4):
            ok, msg = upload.validate_file(path)
        self.assertFalse(ok)
        self.assertIn("上限", msg)

    def test_valid_file_passes(self):
        path = _make_file("ok.bin", b"12345")
        ok, msg = upload.validate_file(path)
        self.assertTrue(ok)
        self.assertEqual(msg, "")


class RecentFilesTest(unittest.TestCase):
    def test_newest_first_and_dirs_excluded(self):
        d = tempfile.mkdtemp(prefix="up_recent_", dir=_TMP)
        _make_file(os.path.join(d, "old.txt"), mtime=1000)
        _make_file(os.path.join(d, "new.txt"), mtime=2000)
        os.makedirs(os.path.join(d, "subdir"), exist_ok=True)
        self.assertEqual(
            upload.recent_files(d),
            [os.path.join(d, "new.txt"), os.path.join(d, "old.txt")],
        )

    def test_limit_respected(self):
        d = tempfile.mkdtemp(prefix="up_limit_", dir=_TMP)
        for i in range(12):
            _make_file(os.path.join(d, f"f{i:02d}.txt"), mtime=i)
        self.assertEqual(len(upload.recent_files(d, limit=8)), 8)

    def test_missing_dir_returns_empty(self):
        self.assertEqual(upload.recent_files("/nonexistent_up_dir"), [])


class ProgressAndReceiptTextTest(unittest.TestCase):
    def test_progress_text_percent_and_bytes(self):
        text = upload.progress_text("a.bin", 250, 1000)
        self.assertIn("a.bin", text)
        self.assertIn("25%", text)

    def test_progress_text_zero_total(self):
        self.assertIn("0%", upload.progress_text("a.bin", 0, 0))

    def test_receipt_text_has_name_and_elapsed(self):
        text = upload.receipt_text("a.bin", 12345, 3.2)
        self.assertIn("a.bin", text)
        self.assertIn("3.2", text)
        self.assertIn("收藏夹", text)


class _FakeClient:
    """send_file 假客户端：同步回调进度，可注入异常与耗时。"""

    def __init__(self, steps=(0.25, 0.5, 0.75, 1.0), delay=0.0, exc=None):
        self.steps = steps
        self.delay = delay
        self.exc = exc
        self.calls = []

    async def send_file(self, target, path, progress_callback=None, **kw):
        self.calls.append((target, path))
        size = os.path.getsize(path)
        for s in self.steps:
            if progress_callback:
                progress_callback(int(size * s), size)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc


class UploadWithProgressTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.path = _make_file("up_flow.bin", b"z" * 100)

    async def _run(self, client):
        updates = []

        async def update(text):
            updates.append(text)

        result = await upload.upload_with_progress(client, self.path, update)
        return updates, result

    async def test_success_returns_receipt_and_reports_progress(self):
        with mock.patch.object(config, "UPLOAD_PROGRESS_EDIT_SECONDS", 0.01):
            updates, (ok, text) = await self._run(
                _FakeClient(delay=0.05))
        self.assertTrue(ok)
        self.assertIn("up_flow.bin", text)
        self.assertIn("收藏夹", text)
        # 进度至少报过一次（初次 0% 之外还有节流后的更新）
        self.assertGreaterEqual(len(updates), 1)
        self.assertTrue(all("⬆️" in t for t in updates))

    async def test_client_error_returns_failure_message(self):
        updates, (ok, text) = await self._run(
            _FakeClient(exc=RuntimeError("network down")))
        self.assertFalse(ok)
        self.assertIn("上传失败", text)
        self.assertIn("network down", text)

    async def test_send_file_targets_saved_messages(self):
        client = _FakeClient()
        await self._run(client)
        self.assertEqual(client.calls, [("me", self.path)])


class UpCommandDispatchTest(unittest.TestCase):
    """commands.handle_command 的 /up 分支。

    真实流程：先 reply 一条「⬆️ 上传中」状态消息，进度原地编辑，最终回执
    也是原地编辑（status.edit）——所以断言目标是假消息的 edit 调用序列。
    """

    def _run(self, cmd):
        class FakeMsg:
            def __init__(self):
                self.edits = []

            async def edit(self, text, **kwargs):
                self.edits.append(text)

        class FakeEvent:
            def __init__(self):
                self.replies = []
                self.msgs = []

            async def reply(self, text, **kwargs):
                self.replies.append(text)
                msg = FakeMsg()
                self.msgs.append(msg)
                return msg

        ev = FakeEvent()
        ok = asyncio.run(commands.handle_command(ev, cmd))
        return ok, ev

    def setUp(self):
        self._old_client = state.client
        state.client = mock.MagicMock()
        state.client.send_file = mock.AsyncMock()
        self._old_cwd = state.SHELL_CWD
        state.SHELL_CWD = _TMP

    def tearDown(self):
        state.client = self._old_client
        state.SHELL_CWD = self._old_cwd

    def test_no_arg_shows_usage(self):
        ok, ev = self._run("/up")
        self.assertTrue(ok)
        self.assertIn("用法", ev.replies[0])

    def test_missing_file_rejected(self):
        ok, ev = self._run("/up /nonexistent_up_file_xyz")
        self.assertTrue(ok)
        self.assertIn("❌", ev.replies[0])
        state.client.send_file.assert_not_awaited()

    def test_valid_file_uploads_and_receipts_in_place(self):
        path = _make_file("cmd_up.bin", b"123")
        ok, ev = self._run(f"/up {path}")
        self.assertTrue(ok)
        state.client.send_file.assert_awaited_once()
        self.assertEqual(state.client.send_file.await_args.args[0], "me")
        status = ev.msgs[-1]
        # 先 reply 的状态消息展示 0% 进度，最终被原地编辑为回执
        self.assertIn("⬆️", ev.replies[-1])
        final = status.edits[-1]
        self.assertIn("cmd_up.bin", final)
        self.assertIn("收藏夹", final)

    def test_relative_path_uses_shell_cwd(self):
        _make_file("rel_up.bin", b"1")
        ok, ev = self._run("/up rel_up.bin")
        self.assertTrue(ok)
        state.client.send_file.assert_awaited_once()
        self.assertIn("rel_up.bin", ev.msgs[-1].edits[-1])


if __name__ == "__main__":
    unittest.main()
