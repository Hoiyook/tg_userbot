"""bot 按钮菜单：回调数据编解码与各视图按钮构造（纯函数）。

encode_menu_data 生成 m:<action>[:<arg>]（≤64 字节），parse_menu_data 逆解析。
build_main_menu_text 为菜单头部；各 *_menu_buttons 依运行态 state.QUEUE /
state.WHITELIST_CHATS 在**调用时**读取（菜单每次展示都取最新状态）。
Button 为 Telethon 类型（telethon.Button），import 期无副作用。
"""
from telethon import Button

from . import state
from .config import DOWNLOAD_CONCURRENCY_MAX, MENU_ACTIONS


def encode_menu_data(action, arg=None):
    """把菜单动作编码成回调数据（Telegram 限制 ≤64 字节）。"""
    data = f"m:{action}"
    if arg is not None:
        data += f":{arg}"
    return data.encode("utf-8")


def parse_menu_data(data):
    """解析回调数据，返回 (动作, 参数)；无法识别返回 ("unknown", None)。"""
    try:
        parts = data.decode("utf-8").split(":")
        if len(parts) < 2 or len(parts) > 3 or parts[0] != "m":
            return ("unknown", None)
        action = parts[1]
        arg = parts[2] if len(parts) == 3 else None
        if action not in MENU_ACTIONS:
            return ("unknown", None)
        return (action, arg)
    except Exception:
        return ("unknown", None)


def build_main_menu_text():
    return (
        "🤖菜单\n\n"
        "点击按钮操作，结果会更新在这条消息里。\n"
        "【我的收藏】 里的命令照常可用。"
    )


def main_menu_buttons():
    return [
        [Button.inline("📊 状态", encode_menu_data("status")),
         Button.inline("📈 进度", encode_menu_data("progress"))],
        [Button.inline("📜 下载记录", encode_menu_data("done")),
         Button.inline("📋 白名单", encode_menu_data("wl"))],
        [Button.inline("📥 队列", encode_menu_data("queue")),
         Button.inline("🔁 待重试", encode_menu_data("retry"))],
        [Button.inline("🧵 并发", encode_menu_data("thread")),
         Button.inline("🧹 清理", encode_menu_data("clean"))],
        [Button.inline("🖥 启动CD2", encode_menu_data("cd2")),
         Button.inline("🛑 停止CD2", encode_menu_data("cd2_stop"))],
        [Button.inline("🗂 备份记录", encode_menu_data("bak"))],
    ]


def queue_menu_buttons():
    rows = []
    for i, r in enumerate(state.QUEUE["tasks"], start=1):
        label = (r.get("label") or "(无)")[:20]
        rows.append(
            [Button.inline(
                f"❌ {i} {label}", encode_menu_data("queue_del", r["id"])
            )]
        )
    rows.append([Button.inline("🔙 返回主菜单", encode_menu_data("home"))])
    return rows


def retry_menu_buttons():
    rows = []
    for i, r in enumerate(state.QUEUE["retry"], start=1):
        label = (r.get("label") or "(无)")[:14]
        rows.append(
            [
                Button.inline(
                    f"▶️ {i}", encode_menu_data("retry_run", r["id"])
                ),
                Button.inline(
                    f"❌ {i} {label}", encode_menu_data("retry_del", r["id"])
                ),
            ]
        )
    rows.append([Button.inline("🔙 返回主菜单", encode_menu_data("home"))])
    return rows


def back_home_buttons():
    return [[Button.inline("🔙 返回主菜单", encode_menu_data("home"))]]


def wl_menu_buttons():
    rows = [[Button.inline("➕ 添加", encode_menu_data("wl_add"))]]
    for cid, title in sorted(state.WHITELIST_CHATS.items()):
        rows.append(
            [Button.inline(f"➖ {title}", encode_menu_data("wl_del", str(cid)))]
        )
    rows.append([Button.inline("🔙 返回主菜单", encode_menu_data("home"))])
    return rows


def thread_menu_buttons():
    # 预设档位（只出不大于 DOWNLOAD_CONCURRENCY_MAX 的），每行 3 个
    presets = [p for p in (3, 5, 10, 15, 20, 25) if p <= DOWNLOAD_CONCURRENCY_MAX]
    rows = [
        [Button.inline(str(p), encode_menu_data("thread", str(p))) for p in presets[i:i + 3]]
        for i in range(0, len(presets), 3)
    ]
    rows.append([Button.inline("🔙 返回主菜单", encode_menu_data("home"))])
    return rows
