package local.nagram.shell

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/** 一条离线待补投的分享：id 是服务端幂等键，重投必须复用同一 id。 */
data class SpoolItem(val id: String, val text: String)

/**
 * 壳 app 的全部本地状态：服务器地址 / token / Telegram 中转配置 / 离线补投暂存。
 *
 * 两条入队通道（2026-11-01 定稿）：
 * - **Telegram 中转**（botToken + chatId 配置后优先）：app 持 bot token 经
 *   Bot API 把「🛰 <request_id> <原文>」发进 bot 对话，userbot 收到后走同一条
 *   入队链路。安卓单 VPN 限制下（Tailscale 与 Hiddify 互斥），这是唯一不依赖
 *   直连的通道——手机 Telegram 能通，它就能通。
 * - **HTTP 直连**（serverUrl 配置）：家里同一 WiFi 直达本地 API，回执更快。
 *
 * 离线补投：两条通道都不通时进 spool，每次 app 启动/回到前台/配置保存后
 * 自动重投；每条带独立 request_id，服务端据此拒绝重复受理。
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
    private const val KEY_SPOOL_BACKUP = "spool_backup"
    private const val SPOOL_MAX = 100

    private fun sp(ctx: Context) = ctx.getSharedPreferences(NAME, 0)

    private fun isHttp(s: String): Boolean =
        s.startsWith("http://") || s.startsWith("https://")

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
            if (isHttp(home)) home else if (isHttp(away)) away else ""
        }
    }

    fun token(ctx: Context): String =
        sp(ctx).getString(KEY_TOKEN, "")!!.trim()

    /** 看板 401 时经 JS bridge 回写的 token（只改 token，保留其它字段）。 */
    fun saveToken(ctx: Context, token: String) {
        sp(ctx).edit().putString(KEY_TOKEN, token.trim()).apply()
    }

    fun botToken(ctx: Context): String =
        sp(ctx).getString(KEY_BOT_TOKEN, "")!!.trim()

    fun chatId(ctx: Context): String =
        sp(ctx).getString(KEY_CHAT_ID, "")!!.trim()

    fun telegramConfigured(ctx: Context): Boolean =
        botToken(ctx).isNotEmpty() && chatId(ctx).isNotEmpty()

    fun httpConfigured(ctx: Context): Boolean =
        isHttp(homeUrl(ctx)) || isHttp(awayUrl(ctx))

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

    /** 读 spool；旧版纯字符串条目就地迁移成 {id,text}（只迁移一次）。 */
    private fun readSpool(ctx: Context): Pair<MutableList<SpoolItem>, Boolean> {
        val raw = sp(ctx).getString(KEY_SPOOL, "[]") ?: "[]"
        val arr = try {
            JSONArray(raw)
        } catch (e: Exception) {
            // 坏数据不静默丢弃：先留一份原文备份，便于人工找回
            sp(ctx).edit().putString(KEY_SPOOL_BACKUP, raw).apply()
            return Pair(mutableListOf<SpoolItem>(), true)
        }
        var migrated = false
        val out = mutableListOf<SpoolItem>()
        for (i in 0 until arr.length()) {
            when (val v = arr.opt(i)) {
                is JSONObject -> {
                    val id = v.optString("id")
                    val text = v.optString("text")
                    if (id.isNotEmpty()) {
                        out.add(SpoolItem(id, text))
                    } else {
                        out.add(SpoolItem(newSpoolId(), text))
                        migrated = true
                    }
                }
                is String -> {
                    out.add(SpoolItem(newSpoolId(), v))
                    migrated = true
                }
            }
        }
        return Pair(out, migrated)
    }

    private fun newSpoolId(): String =
        java.util.UUID.randomUUID().toString().replace("-", "")

    private fun writeSpool(ctx: Context, items: List<SpoolItem>) {
        val arr = JSONArray()
        items.forEach {
            arr.put(JSONObject().put("id", it.id).put("text", it.text))
        }
        sp(ctx).edit().putString(KEY_SPOOL, arr.toString()).apply()
    }

    /** 暂存一条；spool 满（100 条）返回 false，调用方必须如实告知用户。 */
    @Synchronized
    fun spoolAdd(ctx: Context, text: String, id: String): Boolean {
        val (items, _) = readSpool(ctx)
        if (items.size >= SPOOL_MAX) return false
        items.add(SpoolItem(id, text))
        writeSpool(ctx, items)
        return true
    }

    @Synchronized
    fun spoolAll(ctx: Context): List<SpoolItem> {
        val (items, migrated) = readSpool(ctx)
        if (migrated) writeSpool(ctx, items)
        return items
    }

    /** 按 id 移除（绝不按文本全删——同链接多次分享是两条独立请求）。 */
    @Synchronized
    fun spoolRemove(ctx: Context, id: String) {
        val (items, _) = readSpool(ctx)
        writeSpool(ctx, items.filterNot { it.id == id })
    }
}
