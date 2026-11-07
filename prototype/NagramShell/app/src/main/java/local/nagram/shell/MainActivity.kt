package local.nagram.shell

import android.annotation.SuppressLint
import android.os.Bundle
import android.webkit.WebView
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import com.google.accompanist.swiperefresh.rememberSwipeRefreshState
import com.google.accompanist.swiperefresh.SwipeRefresh
import com.google.accompanist.swiperefresh.SwipeRefreshIndicator
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.border
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * 主界面 = 配置页（未配置时）或后端看板 WebView。
 *
 * 壳只记住「服务器地址 + token」一件事：看板 HTML 由 userbot 进程 serve
 * （?token= 引导一次进 localStorage），同源免 CORS；改 UI 不用发 APK。
 * 打开 app 时顺带补投离线暂存的分享。
 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                Surface(modifier = Modifier.fillMaxSize(),
                        color = Color(0xFF10151C)) {
                    App()
                }
            }
        }
    }
}

@Composable
fun App() {
    val ctx = LocalContext.current
    var configured by remember { mutableStateOf(Prefs.configured(ctx)) }
    var editing by remember { mutableStateOf(false) }
    var spoolNote by remember { mutableStateOf("") }
    var tab by remember { mutableStateOf("panel") }

    LaunchedEffect(Unit) {
        val remaining = withContext(Dispatchers.IO) { Api.flushSpool(ctx) }
        if (remaining > 0) spoolNote = "⏳ 有 $remaining 条离线分享待补投"
    }

    // 未配置：先全屏走配置页（配好才出现双页签）
    if (!configured || editing) {
        ConfigScreen(
            onCancel = { if (configured) editing = false },
            onSaved = {
                configured = true
                editing = false
            },
        )
        return
    }

    // 已配置：面板 / 脚本 双页签（2026-11-07 合并 ScriptRunner）
    Column(Modifier.fillMaxSize().background(Color(0xFF10151C))) {
        Box(Modifier.weight(1f)) {
            when {
                tab == "scripts" -> ScriptsScreen()
                Prefs.httpConfigured(ctx) -> DashboardScreen(
                    onEdit = { editing = true },
                    spoolNote = spoolNote,
                )
                // 只配了 Telegram 通道（服务器地址选填）：没有可加载的
                // 看板地址，绝不能拿空 URL 去 loadUrl（会摔在错误页）
                else -> TelegramOnlyScreen(
                    onEdit = { editing = true },
                    spoolNote = spoolNote,
                )
            }
        }
        Row(Modifier.fillMaxWidth().background(Color(0xFF1A2230))) {
            TabItem("🌐 面板", tab == "panel", Modifier.weight(1f)) {
                tab = "panel"
            }
            TabItem("🛠 脚本", tab == "scripts", Modifier.weight(1f)) {
                tab = "scripts"
            }
        }
    }
}

@Composable
fun TabItem(label: String, selected: Boolean, modifier: Modifier,
            onClick: () -> Unit) {
    Column(modifier
        .clickable(onClick = onClick)
        .padding(vertical = 10.dp),
        horizontalAlignment = Alignment.CenterHorizontally) {
        Text(label, color = if (selected) Color(0xFF5B8CFF)
             else Color(0xFF8FA1BD), fontSize = 14.sp)
        Spacer(Modifier.height(2.dp))
        Box(Modifier
            .size(width = 36.dp, height = 3.dp)
            .background(if (selected) Color(0xFF5B8CFF) else Color.Transparent,
                RoundedCornerShape(2.dp)))
    }
}

@Composable
fun TelegramOnlyScreen(onEdit: () -> Unit, spoolNote: String) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(24.dp),
        verticalArrangement = Arrangement.Center,
    ) {
        Text("✅ 分享直达已就绪", color = Color(0xFFE8EDF4), fontSize = 26.sp)
        Spacer(Modifier.height(14.dp))
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .background(Color(0xFF1A2230), RoundedCornerShape(12.dp))
                .padding(16.dp),
        ) {
            Text(
                "用法：任意 app 里点「分享」→ 选「存到下载器」→ 提示已通过"
                        + " Telegram 转交即可，进度与结果看 Telegram 控制面板"
                        + "（📲 分享直达通知 + bot 菜单）",
                color = Color(0xFFB9C8DA), fontSize = 14.sp, lineHeight = 20.sp,
            )
        }
        if (spoolNote.isNotEmpty()) {
            Spacer(Modifier.height(12.dp))
            Text(spoolNote, color = Color(0xFFFBBF24), fontSize = 14.sp)
        }
        Spacer(Modifier.height(20.dp))
        Text(
            "看板仪表盘需要「服务器直连」地址：家里同一 WiFi 时在 ⚙ 里填"
                    + " http://<Mac的IP>:8765（token 见 tg_secrets.json api 段）；"
                    + "填了以后打开 app 就是实时仪表盘",
            color = Color(0xFF8FA3BB), fontSize = 13.sp, lineHeight = 19.sp,
        )
        Spacer(Modifier.height(24.dp))
        OutlinedButton(onClick = onEdit, modifier = Modifier.fillMaxWidth()) {
            Text("⚙ 重新配置", fontSize = 16.sp)
        }
    }
}

@Composable
fun ConfigScreen(onSaved: () -> Unit, onCancel: (() -> Unit)?) {
    val ctx = LocalContext.current
    var url by remember { mutableStateOf(Prefs.url(ctx)) }
    var token by remember { mutableStateOf(Prefs.token(ctx)) }
    var botToken by remember { mutableStateOf(Prefs.botToken(ctx)) }
    var chatId by remember { mutableStateOf(Prefs.chatId(ctx)) }
    var note by remember { mutableStateOf("") }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(24.dp),
        verticalArrangement = Arrangement.Center,
    ) {
        Text("配置下载器", color = Color(0xFFE8EDF4), fontSize = 24.sp)
        Spacer(Modifier.height(8.dp))
        Text(
            "① Telegram 中转（推荐，选填）：分享走 Bot API，任何网络都能用，"
                    + "不与代理冲突。bot token 找管理 bot 的 @BotFather 看；"
                    + "数字 ID 向 @userinfobot 发消息可查",
            color = Color(0xFF8FA3BB), fontSize = 13.sp,
        )
        Spacer(Modifier.height(12.dp))
        OutlinedTextField(
            value = botToken,
            onValueChange = { botToken = it },
            label = { Text("Bot Token（选填，如 123456:ABC-xxx）") },
            visualTransformation = PasswordVisualTransformation(),
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
        )
        Spacer(Modifier.height(10.dp))
        OutlinedTextField(
            value = chatId,
            onValueChange = { chatId = it },
            label = { Text("你的数字 ID（选填，如 5452449426）") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
        )
        Spacer(Modifier.height(16.dp))
        Text(
            "② 服务器直连（选填）：家里同一 WiFi 时看板更快；"
                    + "token 见 tg_secrets.json 的 api 段",
            color = Color(0xFF8FA3BB), fontSize = 13.sp,
        )
        Spacer(Modifier.height(12.dp))
        OutlinedTextField(
            value = url,
            onValueChange = { url = it },
            label = { Text("服务器地址（选填，如 http://192.168.31.161:8765）") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
        )
        Spacer(Modifier.height(10.dp))
        OutlinedTextField(
            value = token,
            onValueChange = { token = it },
            label = { Text("API 访问令牌（选填）") },
            visualTransformation = PasswordVisualTransformation(),
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
        )
        Spacer(Modifier.height(18.dp))
        Button(
            onClick = {
                if (botToken.isBlank() && !url.trim().startsWith("http")) {
                    note = "至少配置 Telegram 中转（Bot Token + 数字 ID）或服务器地址"
                    return@Button
                }
                Prefs.save(ctx, url, token, botToken, chatId)
                onSaved()
            },
            modifier = Modifier.fillMaxWidth(),
        ) { Text("保存并打开控制台", fontSize = 16.sp) }
        if (note.isNotEmpty()) {
            Spacer(Modifier.height(8.dp))
            Text(note, color = Color(0xFFF87171), fontSize = 13.sp)
        }
        if (onCancel != null) {
            Spacer(Modifier.height(6.dp))
            TextButton(onClick = onCancel, modifier = Modifier.fillMaxWidth()) {
                Text("返回看板")
            }
        }
    }
}

@SuppressLint("SetJavaScriptEnabled")
@Composable
fun DashboardScreen(onEdit: () -> Unit, spoolNote: String) {
    val ctx = LocalContext.current
    val base = Prefs.url(ctx)
    val token = Prefs.token(ctx)
    val target = if (token.isNotEmpty()) "$base/?token=$token" else "$base/"
    val refreshTrigger = remember { mutableStateOf(0) }

    Box(Modifier.fillMaxSize()) {
        // 下拉刷新：SwipeRefresh 包 WebView——WebView 内部滚动到顶时
        // 才触发刷新手势，刷新 = window.location.reload()（看板由后端
        // serve，no-store 保证拿到最新版）
        com.google.accompanist.swiperefresh.SwipeRefresh(
            state = rememberSwipeRefreshState(isRefreshing = false),
            onRefresh = { refreshTrigger.value += 1 },
            modifier = Modifier.fillMaxSize(),
            indicator = { state, trigger ->
                com.google.accompanist.swiperefresh.SwipeRefreshIndicator(
                    state = state,
                    refreshTriggerDistance = trigger,
                    scale = true,
                    backgroundColor = Color.White,
                    contentColor = Color(0xFF2563EB),
                )
            },
        ) {
            val currentTarget = remember(refreshTrigger.value) { target }
            AndroidView(
                modifier = Modifier.fillMaxSize(),
                factory = { context ->
                    WebView(context).apply {
                        settings.javaScriptEnabled = true
                        settings.domStorageEnabled = true
                        loadUrl(currentTarget)
                    }
                },
                update = { webView ->
                    // refreshTrigger 变化时重载（下拉刷新）
                    webView.loadUrl(currentTarget)
                },
            )
        }
        if (spoolNote.isNotEmpty()) {
            Text(
                spoolNote,
                color = Color(0xFFB45309), fontSize = 12.sp,
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(top = 44.dp)
                    .background(Color(0xFFFEF3C7))
                    .padding(horizontal = 14.dp, vertical = 6.dp),
            )
        }
        // 悬浮配置球：悬在看板底部导航上方，不占布局、不挡内容
        TextButton(
            onClick = onEdit,
            modifier = Modifier
                .align(Alignment.BottomEnd)
                .padding(end = 14.dp, bottom = 146.dp)
                .background(Color.White, CircleShape)
                .border(1.dp, Color(0xFFE2E8F0), CircleShape)
                .size(48.dp),
        ) {
            Text("⚙", fontSize = 20.sp, color = Color(0xFF334155))
        }
    }
}
