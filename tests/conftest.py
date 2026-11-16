"""pytest 共享夹具。

① 路径隔离兜底（2026-11-16 事故根修，先于一切业务 import 执行）：
   测试进程必须指到临时目录，绝不写生产 runtime。事故链：部分测试
   文件不设 TG_SAVE_FOLDER → 它们先 import tg_userbot.config 时
   RUNTIME_DIR 冻结在生产路径 /Volumes/V1/runtime → 之后跑
   test_commands 的 /listen off|on|interval（各自 save_listen_config）
   把 state.LISTEN_RULES=[]（ListenCommandTest.asyncSetUp 清的）写进
   生产 listen.json——监听规则 6 条被清空、评论区跟进停摆 6 天
   （2026-11-10 00:32，owner 11-16 发现）。pytest 的 conftest 先于
   所有测试模块 import，在这里设 env 就能保证 config 冻结前隔离已
   生效；各测试文件自己的设置继续兼容（同值幂等）。
   unittest discover 直跑不经 conftest——但那些文件自带 env 设置，
   且全量回归统一走 pytest。

② Telethon 1.45.0（Layer 229）按钮属性兼容垫片：
   Button.inline / Button.url 统一返回新的 KeyboardInlineButton 构造，
   回调数据 / URL 挪进 type=InlineButtonTypeCallback.data / .url 子对象，
   旧顶层属性消失。生产代码从不读按钮对象属性，只有测试断言读——
   在这里补回 property，断言保持原样。
"""
import atexit
import os
import shutil
import tempfile

# —— ① 路径隔离兜底（必须在 import tg_userbot 之前）——
_TMP = tempfile.mkdtemp(prefix="tg_userbot_pytest_conftest_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
if "TG_SAVE_FOLDER" not in os.environ:
    os.environ["TG_SAVE_FOLDER"] = _TMP

from telethon.tl.types import KeyboardInlineButton  # noqa: E402

if not hasattr(KeyboardInlineButton, "data"):
    KeyboardInlineButton.data = property(
        lambda self: getattr(getattr(self, "type", None), "data", None))
if not hasattr(KeyboardInlineButton, "url"):
    KeyboardInlineButton.url = property(
        lambda self: getattr(getattr(self, "type", None), "url", None))
