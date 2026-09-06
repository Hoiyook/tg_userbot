"""从本地浏览器 Cookie 数据库提取 douyin.com 域 cookie。

用 f2 自带的 browser_cookie3 直接读浏览器的加密 Cookie 库——比手动
DevTools 复制省事，且能拿到 HttpOnly 的 sessionid（document.cookie
拿不到）。懒导入 + 线程执行：macOS 首次读 Chrome 会等钥匙串授权框，
菜单侧必须用 run_in_executor 调用，不能阻塞事件循环。

注意：Chrome/Edge 可在浏览器运行中读取；Firefox 必须完全退出（库锁）；
Safari 解析常年半残，不提供。
"""

_BROWSER_LOADERS = {
    "chrome": "chrome",
    "edge": "edge",
    "firefox": "firefox",
}


def supported_browsers():
    return tuple(_BROWSER_LOADERS)


def load_browser_cookie_string(browser):
    """读指定浏览器的 douyin.com cookie，返回 (cookie_str, err)。

    err 非 None 时 cookie_str 为空串。调用方（bot 菜单/CLI）拿到非空
    cookie 后交给 config.save_douyin_cookie 持久化 + 实时生效。
    """
    loader_name = _BROWSER_LOADERS.get((browser or "").strip().lower())
    if loader_name is None:
        return "", (
            f"不支持的浏览器：{browser}（可选：{' / '.join(_BROWSER_LOADERS)}）"
        )

    try:
        import browser_cookie3
    except Exception as e:
        return "", (
            f"browser_cookie3 不可用（f2 未安装？）：{type(e).__name__}: {e}"
        )

    try:
        jar = getattr(browser_cookie3, loader_name)(domain_name="douyin.com")
    except Exception as e:
        hint = ""
        if isinstance(e, PermissionError):
            # macOS 钥匙串：Chrome/Edge 的 Cookie 由「Chrome Safe Storage」
            # 密钥加密，首次读取弹授权框。用户点了拒绝后不再弹，只能去
            # 钥匙串访问里恢复授权。
            hint = (
                "\n💡 macOS 钥匙串授权问题：首次读取会弹「访问 Chrome 安全"
                "存储」授权框，请点「允许」；若此前点了「拒绝」，打开「钥匙串"
                "访问」→ 登录钥匙串 → 搜索 Chrome Safe Storage → 显示简介 → "
                "访问控制 → 允许 Python 访问后重试"
            )
        elif loader_name == "firefox":
            hint = "（Firefox 读取前需完全退出浏览器）"
        return "", f"读取 {browser} cookie 失败{hint}：{type(e).__name__}: {e}"

    parts = []
    for c in jar:
        if c.value is None:
            continue
        parts.append(f"{c.name}={c.value}")
    if not parts:
        return "", (
            f"{browser} 里没有 douyin.com 的 cookie"
            "（请先在该浏览器登录 douyin.com）"
        )

    return "; ".join(parts), None
