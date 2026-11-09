package local.nagram.shell

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * 分享接收：任意 App「分享 → Nagram控制台」→ POST /api/enqueue。
 *
 * 后端不可达时文本进离线暂存（spool），下次打开 app / 下次分享时自动
 * 补投；服务端三级去重兜住重复。未配置服务器时先暂存再引导去配置——
 * 分享的内容绝不因为没配置而丢。
 */
class ShareReceiverActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val shared = extractSharedText(intent)
        setContent {
            MaterialTheme(
                colorScheme = lightColorScheme(
                    primary = Brand,
                    background = BgApp,
                    surface = CardWhite,
                ),
            ) {
                Surface(modifier = Modifier.fillMaxSize(), color = BgApp) {
                    ShareScreen(shared)
                }
            }
        }
    }
}

/**
 * 取分享文本：兼容 String / Spanned（getStringExtra 对 Spanned 返回 null）
 * 与 ACTION_SEND_MULTIPLE（多条文本按行拼接，服务端一次分流）。
 */
private fun extractSharedText(intent: Intent?): String {
    if (intent == null) return ""
    if (intent.action == Intent.ACTION_SEND_MULTIPLE) {
        @Suppress("DEPRECATION")
        val list = intent.getCharSequenceArrayListExtra(Intent.EXTRA_TEXT)
        if (list != null) {
            return list.mapNotNull { it?.toString()?.trim() }
                .filter { it.isNotEmpty() }
                .joinToString("\n")
        }
    }
    return (intent.getCharSequenceExtra(Intent.EXTRA_TEXT)?.toString() ?: "")
        .trim()
}

@Composable
fun ShareScreen(sharedText: String) {
    val ctx = LocalContext.current
    // 同一次分享（含转屏/进程恢复）必须复用同一 request_id；服务端据此
    // 幂等。submitted 是第二道保险：Activity 重建时不再自动重提。
    val requestId = rememberSaveable { Api.newRequestId() }
    var submitted by rememberSaveable { mutableStateOf(false) }
    var message by remember { mutableStateOf("提交中…") }
    var ok by remember { mutableStateOf<Boolean?>(null) }
    var offline by remember { mutableStateOf(false) }
    var viaTelegram by remember { mutableStateOf(false) }

    LaunchedEffect(sharedText, requestId) {
        if (submitted) return@LaunchedEffect
        submitted = true
        // 空文本必须先判断：否则未配置分支会把空串塞进 spool，永远补不掉
        if (sharedText.isEmpty()) {
            message = "没有收到分享内容"
            ok = false
            return@LaunchedEffect
        }
        if (!Prefs.configured(ctx)) {
            val saved = Prefs.spoolAdd(ctx, sharedText, requestId)
            ok = false
            offline = saved   // 暂存失败时标题必须是「未受理」，不能谎报已暂存
            message = if (saved) {
                "尚未配置服务器——内容已暂存，配置后会自动补投"
            } else {
                "离线暂存已满（100 条），本次未保存——请先配置服务器"
            }
            return@LaunchedEffect
        }
        val r = withContext(Dispatchers.IO) {
            Api.enqueue(ctx, sharedText, requestId)
        }
        ok = r.ok
        offline = r.offline
        viaTelegram = r.viaTelegram
        message = when {
            r.ok -> r.message
            r.offline -> {
                val saved = Prefs.spoolAdd(ctx, sharedText, requestId)
                offline = saved
                if (saved) {
                    "后端暂时不可达——已暂存，回到 app 会自动补投"
                } else {
                    "后端暂时不可达，且离线暂存已满（100 条）——本次未保存"
                }
            }
            else -> r.message
        }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .statusBarsPadding()
            .navigationBarsPadding()
            .padding(24.dp),
        verticalArrangement = Arrangement.Center,
    ) {
        Text(
            text = when {
                ok == null -> "分享到下载器"
                // Telegram 通道的 200 只代表 Bot API 收下了，不代表
                // userbot 已处理；HTTP 通道才是同步受理
                ok == true -> if (viaTelegram) "✅ 已转交" else "✅ 已受理"
                offline -> "⏳ 已暂存"
                else -> "❌ 未受理"
            },
            color = InkStrong,
            fontSize = 26.sp,
        )
        Spacer(Modifier.height(14.dp))
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .background(CardWhite, RoundedCornerShape(12.dp))
                .border(1.dp, Hairline, RoundedCornerShape(12.dp))
                .padding(16.dp),
        ) {
            Text(
                text = sharedText.ifEmpty { "（未收到文本）" },
                color = InkMut,
                fontSize = 14.sp,
                fontFamily = FontFamily.Monospace,
            )
        }
        Spacer(Modifier.height(14.dp))
        Text(
            text = message,
            color = if (offline) Color(0xFFB45309) else InkMut,
            fontSize = 15.sp,
        )
        Spacer(Modifier.height(24.dp))
        Button(
            onClick = {
                ctx.startActivity(
                    Intent(ctx, MainActivity::class.java)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            },
            modifier = Modifier.fillMaxWidth(),
        ) { Text("打开控制面板", fontSize = 17.sp) }
    }
}
