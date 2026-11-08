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
    private const val KEY_URL = "server_url"      // 家里地址（同一 WiFi）
    private const val KEY_AWAY = "away_url"       // 外网地址（Tailscale 等）
    private const val KEY_LAST_GOOD = "last_good_url"  // 探活赢家缓存
    private const val KEY_TOKEN = "token"
    private const val KEY_BOT_TOKEN = "bot_token"
    private const val KEY_CHAT_ID = "chat_id"
    private const val KEY_SPOOL = "spool"

    private fun sp(ctx: Context) = ctx.getSharedPreferences(NAME, 0)

    /** 家里地址（同一 WiFi 时用，最快）。 */
    fun homeUrl(ctx: Context): String =
        sp(ctx).getString(KEY_URL, "")!!.trim().trimEnd('/')

    /** 外网地址（外出时用，如 Tailscale https://xxx.ts.net）。 */
    fun awayUrl(ctx: Context): String =
        sp(ctx).getString(KEY_AWAY, "")!!.trim().trimEnd('/')

    /** 上次探活赢家的 base（不带尾斜杠）；空 = 还没探过。 */
    fun lastGood(ctx: Context): String =
        sp(ctx).getString(KEY_LAST_GOOD, "")!!.trim().trimEnd('/')

    fun setLastGood(ctx: Context, base: String) {
        sp(ctx).edit().putString(KEY_LAST_GOOD, base.trim().trimEnd('/')).apply()
    }

    /** 快速解析（不探活）：上次赢家优先 → 家里 → 外网。 */
    fun resolveCached(ctx: Context): String {
        val home = homeUrl(ctx)
        val away = awayUrl(ctx)
        return lastGood(ctx).ifEmpty {
            if (home.startsWith("http")) home else if (away.startsWith("http")) away else ""
        }
    }

    fun token(ctx: Context): String =
        sp(ctx).getString(KEY_TOKEN, "")!!.trim()

    fun botToken(ctx: Context): String =
        sp(ctx).getString(KEY_BOT_TOKEN, "")!!.trim()

    fun chatId(ctx: Context): String =
        sp(ctx).getString(KEY_CHAT_ID, "")!!.trim()

    fun telegramConfigured(ctx: Context): Boolean =
        botToken(ctx).isNotEmpty() && chatId(ctx).isNotEmpty()

    fun httpConfigured(ctx: Context): Boolean =
        homeUrl(ctx).startsWith("http") || awayUrl(ctx).startsWith("http")

    /** 两条通道任一配好即算可用（分享优先 Telegram，看板用 HTTP） */
    fun configured(ctx: Context): Boolean =
        telegramConfigured(ctx) || httpConfigured(ctx)

    fun save(ctx: Context, homeUrl: String, awayUrl: String, token: String,
             botToken: String, chatId: String) {
        sp(ctx).edit()
            .putString(KEY_URL, homeUrl.trim().trimEnd('/'))
            .putString(KEY_AWAY, awayUrl.trim().trimEnd('/'))
            .putString(KEY_TOKEN, token.trim())
            .putString(KEY_BOT_TOKEN, botToken.trim())
            .putString(KEY_CHAT_ID, chatId.trim())
            // 地址变了，旧探活赢家不再可信，清掉让本轮重新探
            .putString(KEY_LAST_GOOD, "")
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
