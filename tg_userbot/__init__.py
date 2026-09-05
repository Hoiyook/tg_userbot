"""TG Userbot 扁平包。

拆包后：代码在 tg_userbot/ 下按子系统分模块；仓库根保留薄壳
tg_userbot_final.py 只做运行入口（CLAUDE.md 运行命令 / pkill 管理规则
都 key 在这个文件名上，保持不变）。

本包在 import 时只做一件事：导入 config，触发其一次性启动副作用
（mkdir SAVE_FOLDER + 配置共享 logger）。事件循环绑定原语（client/锁/
信号量/事件）只在 app.main() 内构造，见 state.py 顶注与 CLAUDE.md。
"""
from . import config  # noqa: F401  触发 config 的 import 期启动
