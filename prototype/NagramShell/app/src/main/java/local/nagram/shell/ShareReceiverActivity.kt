package local.nagram.shell

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
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
        val shared = intent?.getStringExtra(Intent.EXTRA_TEXT).orEmpty().trim()
        setContent {
            MaterialTheme {
                Surface(modifier = Modifier.fillMaxSize(),
                        color = Color(0xFF10151C)) {
                    ShareScreen(shared)
                }
            }
        }
    }
}

@Composable
fun ShareScreen(sharedText: String) {
    val ctx = LocalContext.current
    var message by remember { mutableStateOf("提交中…") }
    var ok by remember { mutableStateOf<Boolean?>(null) }
    var offline by remember { mutableStateOf(false) }

    LaunchedEffect(sharedText) {
        if (!Prefs.configured(ctx)) {
            Prefs.spoolAdd(ctx, sharedText)
            message = "尚未配置服务器——内容已暂存，配置后会自动补投"
            ok = false
            offline = true
            return@LaunchedEffect
        }
        if (sharedText.isEmpty()) {
            message = "没有收到分享内容"
            ok = false
            return@LaunchedEffect
        }
        val r = withContext(Dispatchers.IO) { Api.enqueue(ctx, sharedText) }
        ok = r.ok
        offline = r.offline
        message = when {
            r.ok -> r.message
            r.offline -> {
                Prefs.spoolAdd(ctx, sharedText)
                "后端暂时不可达——已暂存，打开 app 时自动补投"
            }
            else -> r.message
        }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(24.dp),
        verticalArrangement = Arrangement.Center,
    ) {
        Text(
            text = when {
                ok == null -> "分享到下载器"
                ok == true -> "✅ 已受理"
                offline -> "⏳ 已暂存"
                else -> "❌ 未受理"
            },
            color = Color(0xFFE8EDF4),
            fontSize = 26.sp,
        )
        Spacer(Modifier.height(14.dp))
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .background(Color(0xFF1A2230), RoundedCornerShape(12.dp))
                .padding(16.dp),
        ) {
            Text(
                text = sharedText.ifEmpty { "（未收到文本）" },
                color = Color(0xFFB9C8DA),
                fontSize = 14.sp,
                fontFamily = FontFamily.Monospace,
            )
        }
        Spacer(Modifier.height(14.dp))
        Text(text = message, color = Color(0xFF8FA3BB), fontSize = 15.sp)
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
