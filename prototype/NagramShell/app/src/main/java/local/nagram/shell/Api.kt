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
        val offline: Boolean = false,
    )

    fun enqueue(ctx: android.content.Context, text: String): Result {
        if (Prefs.telegramConfigured(ctx)) {
            val r = relayViaTelegram(ctx, text)
            if (r.ok) return r
            if (!r.offline) return r   // Telegram 明确拒绝（token 错等）直接报
            // 网络层不通（没挂代理等）→ 家里 WiFi 直连兜底
        }
        if (Prefs.httpConfigured(ctx)) {
            return postEnqueue(ctx, text)
        }
        return if (Prefs.telegramConfigured(ctx)) r_offline()
        else Result(false, "尚未配置（填 Telegram 中转或服务器地址）")
    }

    private fun r_offline() = Result(false, "后端不可达", offline = true)

    fun relayViaTelegram(ctx: android.content.Context, text: String): Result {
        val bot = Prefs.botToken(ctx)
        val chatId = Prefs.chatId(ctx)
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
                    .put("chat_id", chatId.toLong())
                    .put("text", RELAY_MARKER + text)
                    .toString().toByteArray())
            }
            val code = conn.responseCode
            val body = (if (code in 200..299) conn.inputStream
            else conn.errorStream)?.bufferedReader()?.use { it.readText() } ?: ""
            val obj = try { JSONObject(body) } catch (e: Exception) { JSONObject() }
            when {
                code in 200..299 && obj.optBoolean("ok") ->
                    Result(true, "已通过 Telegram 转交——结果看控制面板通知")
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

    fun postEnqueue(ctx: android.content.Context, text: String): Result {
        val base = Prefs.url(ctx)
        return try {
            val conn = URL("$base/api/enqueue").openConnection()
                    as HttpURLConnection
            conn.requestMethod = "POST"
            conn.connectTimeout = 10_000
            conn.readTimeout = 120_000
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            val token = Prefs.token(ctx)
            if (token.isNotEmpty()) {
                conn.setRequestProperty("Authorization", "Bearer $token")
            }
            conn.outputStream.use { out ->
                out.write(JSONObject().put("text", text).toString().toByteArray())
            }
            val code = conn.responseCode
            val body = (if (code in 200..299) conn.inputStream
            else conn.errorStream)?.bufferedReader()?.use { it.readText() } ?: ""
            val obj = try { JSONObject(body) } catch (e: Exception) { JSONObject() }
            when {
                code == 401 -> Result(false, "令牌无效（401）——请核对 token")
                code in 200..299 && obj.optBoolean("ok") ->
                    Result(true, obj.optString("message"))
                else -> Result(false, obj.optString("error")
                    .ifEmpty { "HTTP $code" })
            }
        } catch (e: IOException) {
            Result(false, "后端不可达：${e.message ?: "网络错误"}", offline = true)
        } catch (e: Exception) {
            Result(false, "失败：${e.message}")
        }
    }

    /** 把离线暂存的文本逐条补投；成功的移出 spool。返回剩余条数。 */
    fun flushSpool(ctx: android.content.Context): Int {
        for (text in Prefs.spoolAll(ctx)) {
            if (Api.enqueue(ctx, text).ok) {
                Prefs.spoolRemove(ctx, text)
            }
        }
        return Prefs.spoolAll(ctx).size
    }
}
