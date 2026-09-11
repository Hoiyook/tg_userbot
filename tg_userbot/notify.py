"""程序主动通知的统一出口：一律发到 bot 控制面板对话（2026-09-11）。

**为什么**：用户反馈收藏夹被程序通知淹没。收藏夹的职责是**媒体容器**——
转发副本带「转发自」头、落盘目录由副本的 `fwd_from` 解析、手工转发评论靠
那里的纯文本认标注（`app._record_me_label`）；通知混在里面既是噪声，又给
标注识别添乱。所以主动通知（下载开始/完成/失败、去重拦截、解析成功、
Chrome 结果、汇报事件）统一改到控制面板对话，收藏夹只剩媒体与**用户自己
发的命令及其回复**（命令回复留在发出命令的那个对话）。

**为什么由 bot 账号发**：通知要出现在 bot 对话里，可以由主账号发、也可以
由 bot 账号发。选 bot 账号是因为 bot 自己的出站消息**不会回流成更新**——
主账号发到 bot 对话会被 `bot_message_handler` 当成 owner 指令，每发一条
通知就回一次主菜单（2026-09-11 实测：汇报的恢复通知在日志里刷出成片
「owner 发送 '✅ 已恢复…'，显示主菜单」）。语义上也更顺：控制面板 bot 给
你发通知。

**回落**：bot 未配置/未连接/发送失败 → 走主账号发收藏夹，通知绝不丢。

**收口**：发送经 `netio.shielded`。通知是下载收尾路径里的一步，若它自己
因为断线的 `future.cancel()` 把 `CancelledError` 冒出去，正在下载的任务会
被误判成「被真取消」而留在队列里——通知不该有这种权力。失败只记日志。
"""
from . import netio
from . import state
from .config import NOTIFY_TIMEOUT_SECONDS
from .log import logger


def _bot_can_send():
    """bot 账号能不能替我们发：连着、且知道发给谁。"""
    bot = state.bot_client
    return bool(bot is not None and state.MY_ID and bot.is_connected())


async def notify_user(text, link_preview=False):
    """发一条程序主动通知；返回 True=已发出，False=两边都没发出去。

    优先 bot 账号 → 控制面板对话；否则回落主账号 → 收藏夹。
    """
    if _bot_can_send():
        sent = await netio.shielded(
            lambda: state.bot_client.send_message(
                state.MY_ID, text, link_preview=link_preview),
            NOTIFY_TIMEOUT_SECONDS,
            "发送通知（控制面板）",
        )
        if sent is not None:
            return True
        logger.warning("通知改走收藏夹回落（控制面板没发出去）")

    client = state.client
    if client is None:
        return False
    sent = await netio.shielded(
        lambda: client.send_message("me", text, link_preview=link_preview),
        NOTIFY_TIMEOUT_SECONDS,
        "发送通知（收藏夹）",
    )
    return sent is not None
