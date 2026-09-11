"""网络调用收口 netio.shielded 的单元测试（2026-09-11 从 cleanup._shielded 提取）。

为什么要这一层：telethon 断线会对 pending 请求 future 调 `cancel()`，py3.8+
的 `CancelledError` 是 `BaseException`，会绕开 `except Exception` 一路冒到
调用方的循环外——清理任务、汇报任务都因此被「当场打死、再无人重启」（实测：
2026-09-11 07:08 起汇报静默停摆数小时）。

收口把请求放进**子任务**、结局一律经 `result()` 读取，从结构上区分两件事：

- 子任务以 CancelledError 收场 = **网络层取消** → 返回 None，本轮跳过；
- 调用方自己被真取消（停服）= 在 `await` 处原样上抛。

不联网：全部是本地协程。
"""
import asyncio
import atexit
import os
import shutil
import tempfile
import unittest

# 必须在首个 tg_userbot import 之前把保存目录指到临时目录
_TMP = tempfile.mkdtemp(prefix="tg_userbot_netio_test_")
# 退出时回收临时目录（测试跑完就地删，别让 /var/folders 越堆越多）
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import netio  # noqa: E402


class ShieldedTest(unittest.IsolatedAsyncioTestCase):
    """收口的四种结局：成功 / 网络层取消 / 超时 / 普通异常。"""

    async def test_returns_value_on_success(self):
        async def ok():
            return "结果"

        self.assertEqual(await netio.shielded(ok, 1.0, "测试请求"), "结果")

    async def test_network_cancel_returns_none_instead_of_raising(self):
        """子任务被网络层取消（future.cancel()）不得打断调用方。"""
        ran_after = []

        async def cancelled():
            raise asyncio.CancelledError()

        result = await netio.shielded(cancelled, 1.0, "测试请求")
        # 收口把手里的 CancelledError 变成了「本轮没做成」
        ran_after.append(True)
        self.assertIsNone(result)
        self.assertEqual(ran_after, [True], "收口把调用方一并取消了")

    async def test_timeout_returns_none_and_cancels_child(self):
        """僵死连接（无读超时的请求）由超时兜住，不让调用方永久卡死。"""
        child_done = []

        async def hang():
            try:
                await asyncio.sleep(30)
            finally:
                child_done.append(True)

        result = await netio.shielded(hang, 0.02, "测试请求")
        self.assertIsNone(result)
        self.assertEqual(child_done, [True], "超时后子任务没有被取消回收")

    async def test_exception_returns_none(self):
        async def boom():
            raise ValueError("网络炸了")

        self.assertIsNone(await netio.shielded(boom, 1.0, "测试请求"))

    async def test_genuine_cancel_propagates(self):
        """调用方被真取消（停服）必须原样上抛，且不留下孤儿子任务。"""
        child_cancelled = []

        async def hang():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                child_cancelled.append(True)
                raise

        async def caller():
            await netio.shielded(hang, 30.0, "测试请求")

        task = asyncio.create_task(caller())
        await asyncio.sleep(0.02)          # 让子任务真正跑起来
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(child_cancelled, [True], "停服时子任务没被取消")


if __name__ == "__main__":
    unittest.main()
