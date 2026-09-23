"""pytest 共享夹具：Telethon 1.45.0（Layer 229）按钮属性兼容垫片。

1.45.0 起 Button.inline / Button.url 统一返回新的 KeyboardInlineButton
构造，回调数据 / URL 分别挪进了 type=InlineButtonTypeCallback.data /
InlineButtonTypeType.url 子对象，旧的顶层 .data / .url 属性消失。
生产代码从不读按钮对象属性（回调数据来自 event.data，与按钮类无关），
只有测试断言读 .data / .url——在这里把两个旧属性补回 property，测试
断言保持原样（等于把「按钮回调数据可从按钮对象读回」这一契约保留）。
"""
from telethon.tl.types import KeyboardInlineButton

if not hasattr(KeyboardInlineButton, "data"):
    KeyboardInlineButton.data = property(
        lambda self: getattr(getattr(self, "type", None), "data", None))
if not hasattr(KeyboardInlineButton, "url"):
    KeyboardInlineButton.url = property(
        lambda self: getattr(getattr(self, "type", None), "url", None))
