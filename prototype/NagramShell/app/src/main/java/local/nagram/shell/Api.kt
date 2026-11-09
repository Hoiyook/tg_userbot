package local.nagram.shell

import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * 与 userbot 的两条网络出口：
 * - [relayViaTelegram]：Bot API sendMessage「🛰 <原文>」→ userbot 中转分支
 *   入队（标记常量与 api_server.APP_RELAY_MARKER 保持一致）；
 * - [postEnqueue]：本地 API POST /api/enqueue（同步回执，家里 WiFi 用）。
 *
 * [enqueue] 串起优先级：Telegram 配置了先走它（任何网络可用），网络层
 * 失败再回落 HTTP（家里直连更快），都失败交回 spool。阻塞式实现，调用方
 * 负责放工作线程。
 */
object Api {

    /** 与 tg_userbot/api_server.py 的 APP_RELAY_MARKER 保持一致 */
    private const val RELAY_MARKER = "🛰 "

    data class Result(
        val ok: Boolean,
        val message: String,
        /** 网络/服务端瞬时故障：可重试、可进 spool。 */
        val offline: Boolean = false,
        /** 永久失败（如 400 文本里没有任何可识别链接）：重试也不会变，spool 应丢弃。 */
        val permanent: Boolean = false,
        /** 成功时走的是 Telegram 中转（只代表已投递，不代表 userbot 已处理）。 */
        val viaTelegram: Boolean = false,
    )

    data class FlushResult(val remaining: Int, val dropped: Int)

    /** 每次分享一个幂等键：32 位小写十六进制（与 api_server 的校验一致）。 */
    fun newRequestId(): String =
        java.util.UUID.randomUUID().toString().replace("-", "")

    fun enqueue(ctx: android.content.Context, text: String,
                requestId: String = newRequestId()): Result {
        var tgError: Result? = null
        if (Prefs.telegramConfigured(ctx)) {
            val r = relayViaTelegram(ctx, text, requestId)
            if (r.ok) return r
            // 两条通道相互独立：Telegram 配错（token 吊销/chat ID 写错）
            // 不该拖死本来可用的 HTTP。任何失败都继续试 HTTP。
            tgError = r
        }
        if (Prefs.httpConfigured(ctx)) {
            val http = postEnqueue(ctx, text, requestId)
            if (http.ok || tgError == null) return http
            // 两条都失败：保留 HTTP 的错误分类（决定是否 spool），
            // 同时把 Telegram 的就近错误一并展示，避免丢信息。
            return http.copy(
                message = "${http.message}（Telegram 通道：${tgError.message}）")
        }
        return tgError ?: Result(
            false, "尚未配置（填 Telegram 中转或服务器地址）")
    }

    fun relayViaTelegram(ctx: android.content.Context, text: String,
                         requestId: String): Result {
        val bot = Prefs.botToken(ctx)
        val chat = Prefs.chatId(ctx).toLongOrNull()
            ?: return Result(
                false, "数字 ID 不是合法数字（应全为数字，如 5452449426）",
                offline = true)
        return try {
            val conn = URL("https://api.telegram.org/bot$bot/sendMessage")
                    .openConnection() as HttpURLConnection
            conn.requestMethod = "POST"
            conn.connectTimeout = 10_000
            conn.readTimeout = 25_000
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            conn.outputStream.use { out ->
                out.write(JSONObject()
                    .put("chat_id", chat)
                    // 载荷 = 🛰 <request_id> <原文>；服务端据此幂等去重，
                    // 旧版 userbot 不认前缀时 id 只是多出来的一个词。
                    .put("text", RELAY_MARKER + requestId + " " + text)
                    .toString().toByteArray())
            }
            val code = conn.responseCode
            val body = (if (code in 200..299) conn.inputStream
            else conn.errorStream)?.bufferedReader()?.use { it.readText() } ?: ""
            val obj = try { JSONObject(body) } catch (e: Exception) { JSONObject() }
            when {
                code in 200..299 && obj.optBoolean("ok") ->
                    Result(true, "已通过 Telegram 转交——结果看控制面板通知",
                        viaTelegram = true)
                code == 401 -> Result(false, "Bot token 无效（401）")
                else -> Result(false, obj.optString("description")
                    .ifEmpty { "Telegram API HTTP $code" })
            }
        } catch (e: IOException) {
            Result(false, "Telegram 不可达：${e.message ?: "网络错误"}",
                   offline = true)
        } catch (e: Exception) {
            Result(false, "失败：${e.message}")
        }
    }

    /** 并发探活家里/外网两个 base：先回的赢（家里直连通常快得多）。
     *  可达 = `/api/version` 返回 200 + `ok:true`（带 token 验证），
     *  端口通但 401/404/非本服务的一律不算——否则会选中一个用不了的
     *  地址。只配一个地址就不探直接验它。都不通返回 ""。 */
    fun probeBoth(ctx: android.content.Context): String {
        val token = Prefs.token(ctx)
        val candidates = listOf(Prefs.homeUrl(ctx), Prefs.awayUrl(ctx))
            .filter { it.startsWith("http://") || it.startsWith("https://") }
            .distinct()
        if (candidates.isEmpty()) return ""
        if (candidates.size == 1) {
            return if (probe(candidates[0], token)) candidates[0] else ""
        }
        val es = java.util.concurrent.Executors.newFixedThreadPool(2)
        try {
            val futures = candidates.map { c ->
                es.submit(java.util.concurrent.Callable {
                    if (probe(c, token)) c else null
                })
            }
            val deadline = System.currentTimeMillis() + 4500
            while (System.currentTimeMillis() < deadline) {
                for (f in futures) {
                    if (f.isDone) {
                        val r = f.get()
                        if (r != null) return r
                    }
                }
                Thread.sleep(50)
            }
            return ""
        } finally {
            es.shutdownNow()
        }
    }

    /** 探活必须验「是我们这台服务 + token 可用」，不能只看端口通。 */
    private fun probe(base: String, token: String): Boolean = try {
        val conn = URL("$base/api/version").openConnection()
                as HttpURLConnection
        conn.connectTimeout = 2_000
        conn.readTimeout = 2_000
        conn.instanceFollowRedirects = false
        if (token.isNotEmpty()) conn.setRequestProperty("X-Token", token)
        val code = conn.responseCode
        var ok = false
        if (code == 200) {
            val body = conn.inputStream?.bufferedReader()?.use { it.readText() }
                ?: ""
            val obj = try { JSONObject(body) } catch (e: Exception) {
                JSONObject()
            }
            ok = obj.optBoolean("ok")
        }
        conn.disconnect()
        ok
    } catch (e: Exception) {
        false
    }

    fun postEnqueue(ctx: android.content.Context, text: String,
                    requestId: String): Result {
        val base = Prefs.resolveCached(ctx)
        val r = postTo(ctx, base, text, requestId)
        if (!r.offline) return r
        // 缓存地址不通/不是我们的服务/瞬时 5xx → 重新探活换路重试一次
        val winner = probeBoth(ctx)
        if (winner.isNotEmpty() && winner != base) {
            Prefs.setLastGood(ctx, winner)
            return postTo(ctx, winner, text, requestId)
        }
        return r
    }

    private fun postTo(ctx: android.content.Context, base: String,
                       text: String, requestId: String): Result {
        return try {
            val conn = URL("$base/api/enqueue").openConnection()
                    as HttpURLConnection
            conn.requestMethod = "POST"
            conn.connectTimeout = 10_000
            // 服务端 dispatch 硬顶 120s；客户端必须比它长，否则会把
            // 「服务端还在处理」误判成网络失败再投一次。
            conn.readTimeout = 130_000
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            val token = Prefs.token(ctx)
            if (token.isNotEmpty()) {
                conn.setRequestProperty("Authorization", "Bearer $token")
            }
            conn.outputStream.use { out ->
                out.write(JSONObject().put("text", text)
                    .put("request_id", requestId)
                    .toString().toByteArray())
            }
            val code = conn.responseCode
            val body = (if (code in 200..299) conn.inputStream
            else conn.errorStream)?.bufferedReader()?.use { it.readText() } ?: ""
            val obj = try { JSONObject(body) } catch (e: Exception) { JSONObject() }
            // 服务端 400 走 dispatch 结果时字段是 message，缺字段才是 error
            val msg = obj.optString("error").ifEmpty { obj.optString("message") }
            when {
                code == 401 -> Result(false,
                    "令牌无效（401）——请到配置页核对 token")
                code in 200..299 && obj.optBoolean("ok") ->
                    Result(true, obj.optString("message"))
                // 业务拒绝（如文本里没有可识别链接）：同一文本重试不会变。
                // 只认带 ok 字段的「我们的 400」；别的 400 可能是走错服务，
                // 按可重试处理，避免误丢 spool。
                code == 400 && obj.has("ok") -> Result(
                    false, msg.ifEmpty { "HTTP 400" }, permanent = true)
                code == 400 -> Result(false, msg.ifEmpty { "HTTP 400" },
                    offline = true)
                // 429/5xx/504：瞬时故障，可 spool；request_id 保证重投幂等
                code == 429 || code >= 500 -> Result(false,
                    msg.ifEmpty { "服务端暂时不可用（HTTP $code）" },
                    offline = true)
                // 2xx 但不是我们的 JSON（典型：家里 IP 被别的设备占了）
                code in 200..299 -> Result(false,
                    "服务器返回了非预期内容（HTTP $code）", offline = true)
                // 403/404 等：可能是走错地址，换路再试；最后进 spool
                else -> Result(false, msg.ifEmpty { "HTTP $code" },
                    offline = true)
            }
        } catch (e: IOException) {
            Result(false, "后端不可达：${e.message ?: "网络错误"}", offline = true)
        } catch (e: Exception) {
            Result(false, "失败：${e.message}")
        }
    }

    /** 把离线暂存的分享逐条补投；成功/永久失败移出 spool。 */
    fun flushSpool(ctx: android.content.Context): FlushResult {
        var dropped = 0
        for (item in Prefs.spoolAll(ctx)) {
            val r = enqueue(ctx, item.text, item.id)
            when {
                r.ok -> Prefs.spoolRemove(ctx, item.id)
                r.permanent -> {
                    Prefs.spoolRemove(ctx, item.id)
                    dropped++
                }
            }
        }
        return FlushResult(Prefs.spoolAll(ctx).size, dropped)
    }
}
