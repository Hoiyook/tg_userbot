package local.nagram.shell

import android.content.Context
import org.json.JSONArray

/**
 * 壳 app 的全部本地状态：服务器地址 / token / Telegram 中转配置 / 离线补投暂存。
 *
 * 两条入队通道（2026-11-01 定稿）：
 * - **Telegram 中转**（botToken + chatId 配置后优先）：app 持 bot token 经
 *   Bot API 把「🛰 <原文>」发进 bot 对话，userbot 收到后走同一条入队链路。
 *   安卓单 VPN 限制下（Tailscale 与 Hiddify 互斥），这是唯一不依赖直连的
 *   通道——手机 Telegram 能通，它就能通。
 * - **HTTP 直连**（serverUrl 配置）：家里同一 WiFi 直达本地 API，回执更快。
 *
 * 离线补投：两条通道都不通时进 spool，下次打开 app / 分享时自动重投。
 */
object Prefs {
    private const val NAME = "nagram_shell"
    private const val KEY_URL = "server_url"
    private const val KEY_TOKEN = "token"
    private const val KEY_BOT_TOKEN = "bot_token"
    private const val KEY_CHAT_ID = "chat_id"
    private const val KEY_SPOOL = "spool"

    private fun sp(ctx: Context) = ctx.getSharedPreferences(NAME, 0)

    fun url(ctx: Context): String =
        sp(ctx).getString(KEY_URL, "")!!.trim().trimEnd('/')

    fun token(ctx: Context): String =
        sp(ctx).getString(KEY_TOKEN, "")!!.trim()

    fun botToken(ctx: Context): String =
        sp(ctx).getString(KEY_BOT_TOKEN, "")!!.trim()

    fun chatId(ctx: Context): String =
        sp(ctx).getString(KEY_CHAT_ID, "")!!.trim()

    fun telegramConfigured(ctx: Context): Boolean =
        botToken(ctx).isNotEmpty() && chatId(ctx).isNotEmpty()

    fun httpConfigured(ctx: Context): Boolean = url(ctx).startsWith("http")

    /** 两条通道任一配好即算可用（分享优先 Telegram，看板用 HTTP） */
    fun configured(ctx: Context): Boolean =
        telegramConfigured(ctx) || httpConfigured(ctx)

    fun save(ctx: Context, url: String, token: String,
             botToken: String, chatId: String) {
        sp(ctx).edit()
            .putString(KEY_URL, url.trim().trimEnd('/'))
            .putString(KEY_TOKEN, token.trim())
            .putString(KEY_BOT_TOKEN, botToken.trim())
            .putString(KEY_CHAT_ID, chatId.trim())
            .apply()
    }

    @Synchronized
    fun spoolAdd(ctx: Context, text: String) {
        val arr = JSONArray(sp(ctx).getString(KEY_SPOOL, "[]"))
        arr.put(text)
        sp(ctx).edit().putString(KEY_SPOOL, arr.toString()).apply()
    }

    @Synchronized
    fun spoolAll(ctx: Context): List<String> {
        val arr = JSONArray(sp(ctx).getString(KEY_SPOOL, "[]"))
        return (0 until arr.length()).map { arr.getString(it) }
    }

    @Synchronized
    fun spoolRemove(ctx: Context, text: String) {
        val arr = JSONArray(sp(ctx).getString(KEY_SPOOL, "[]"))
        val out = JSONArray()
        for (i in 0 until arr.length()) {
            if (arr.getString(i) != text) out.put(arr.getString(i))
        }
        sp(ctx).edit().putString(KEY_SPOOL, out.toString()).apply()
    }
}
