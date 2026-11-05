package com.jiajun.scriptrunner

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import org.json.JSONArray
import org.json.JSONObject

// ── 数据模型 ────────────────────────────────────────────────
// Termux 固定路径（$PREFIX / home）——RUN_COMMAND 的 program 必须是
// Termux 内的绝对路径，相对 PATH 不解析
private const val TERMUX_PREFIX = "/data/data/com.termux/files/usr"
private const val TERMUX_HOME = "/data/data/com.termux/files/home"
private const val PREFS = "scripts"
private const val KEY_SCRIPTS = "scripts_json"
// Termux 的 RUN_COMMAND 是 dangerous 级自定义权限——manifest 声明之外
// 还必须运行时请求（2026-11-05 实测：v1.5 已声明仍 SecurityException，
// 就是缺运行时授予）
private const val PERM_RUN_COMMAND = "com.termux.permission.RUN_COMMAND"

data class Script(
    val name: String,
    val program: String,      // Termux 绝对路径（如 $PREFIX/bin/python3）
    val args: List<String>,   // 参数数组；元素含 {提示} 的部分运行时弹输入
)

// 输出统一走「落文件 + termux-open 自动弹出」（窗口通道实测不可靠：
// 会话焦点/extra 键名——2026-11-05）；用户路径经 $1 位置参数传入，
// 撇号/空格/中文目录名全部免疫（GreatoDoggo's 实测坑）
private val OUT_FILE = "/sdcard/Download/scriptrunner_out.txt"

private val EXTRACT_DRY_BODY =
    "out=$OUT_FILE; " +
    "if [ -f \"\$HOME/extract.pyz\" ]; then " +
    "python3 \"\$HOME/extract.pyz\" \"\$1\" --dry > \"\$out\" 2>&1; " +
    "else echo \"❌ extract.pyz 不在 Termux home——先在 app 文件管理卡片" +
    "点「复制到 Termux」（源 /sdcard/Download/extract.pyz）\" > \"\$out\"; fi; " +
    "termux-open \"\$out\""

private val EXTRACT_RUN_BODY =
    "out=$OUT_FILE; " +
    "if [ -f \"\$HOME/extract.pyz\" ]; then " +
    "python3 \"\$HOME/extract.pyz\" \"\$1\" > \"\$out\" 2>&1; " +
    "else echo \"❌ extract.pyz 不在 Termux home——先在 app 文件管理卡片" +
    "点「复制到 Termux」（源 /sdcard/Download/extract.pyz）\" > \"\$out\"; fi; " +
    "termux-open \"\$out\""

private fun defaultScripts() = listOf(
    Script(
        name = "🗜 解压·预览 (dry)",
        program = "$TERMUX_PREFIX/bin/bash",
        args = listOf("-c", EXTRACT_DRY_BODY, "_", "{要解压的目录}"),
    ),
    Script(
        name = "🗜 解压·执行",
        program = "$TERMUX_PREFIX/bin/bash",
        args = listOf("-c", EXTRACT_RUN_BODY, "_", "{要解压的目录}"),
    ),
    Script(
        name = "⚙️ 自定义命令行",
        program = "$TERMUX_PREFIX/bin/bash",
        args = listOf("-c", "{完整命令行，如 python3 ~/x.py 参数1}"),
    ),
)

// ── 持久化（SharedPreferences + JSON）───────────────────────
private const val KEY_SCRIPTS_VER = "scripts_ver"
private const val SCRIPTS_VER = 2   // v2：解压脚本迁移到输出文件通道


private fun loadScripts(ctx: Context): MutableList<Script> {
    val prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
    // 旧版脚本（窗口通道）整体升级：重置为 v2 默认（自定义脚本一并
    // 重置——v1.x 期间用户自定义项可忽略，2026-11-06）
    if (prefs.getInt(KEY_SCRIPTS_VER, 1) < SCRIPTS_VER) {
        return defaultScripts().toMutableList().also {
            saveScripts(ctx, it)
            prefs.edit().putInt(KEY_SCRIPTS_VER, SCRIPTS_VER).apply()
        }
    }
    val raw = prefs.getString(KEY_SCRIPTS, null)
        ?: return defaultScripts().toMutableList()
    return try {
        val arr = JSONArray(raw)
        (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            val args = mutableListOf<String>()
            val a = o.getJSONArray("args")
            for (j in 0 until a.length()) args.add(a.getString(j))
            Script(o.getString("name"), o.getString("program"), args)
        }.toMutableList()
    } catch (e: Exception) {
        defaultScripts().toMutableList()
    }
}

private fun saveScripts(ctx: Context, scripts: List<Script>) {
    val arr = JSONArray()
    scripts.forEach { s ->
        val o = JSONObject().put("name", s.name).put("program", s.program)
        val a = JSONArray()
        s.args.forEach { a.put(it) }
        o.put("args", a)
        arr.put(o)
    }
    ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        .edit().putString(KEY_SCRIPTS, arr.toString()).apply()
}

// ── 占位符：args 元素里的 {提示} 运行时弹输入框 ─────────────
private val PLACEHOLDER = Regex("\\{(.+?)\\}")

private fun placeholdersOf(args: List<String>): List<String> =
    args.flatMap { PLACEHOLDER.findAll(it).map { m -> m.groupValues[1] } }

private fun substitute(arg: String, values: Map<String, String>): String =
    PLACEHOLDER.replace(arg) { m ->
        values[m.groupValues[1]] ?: m.groupValues[0]
    }

// ── RUN_COMMAND 发射 ────────────────────────────────────────
private fun runInTermux(ctx: Context, script: Script, argValues: Map<String, String>) {
    val args = script.args.map { substitute(it, argValues) }.toTypedArray()
    runRaw(ctx, script.program, args)
}

/** 直接发射一条命令到 Termux（文件管理卡片用：cp/rm/ls）。 */
private fun runRaw(ctx: Context, program: String, args: Array<String>) {
    // ⓪ 运行时权限（dangerous 级）：没授予先请求——系统会弹「允许
    // ScriptRunner 在 Termux 环境中运行命令？」
    val granted = androidx.core.content.ContextCompat.checkSelfPermission(
        ctx, PERM_RUN_COMMAND) == android.content.pm.PackageManager.PERMISSION_GRANTED
    if (!granted) {
        Toast.makeText(ctx, "⚠️ 缺少 Termux 运行命令权限——请完全退出本 " +
            "app 重开，在弹窗里点「允许」；或到 系统设置→应用→ScriptRunner→" +
            "权限 手动打开", Toast.LENGTH_LONG).show()
        return
    }
    // 前置检测（多包名兼容：官方 F-Droid/GitHub=com.termux，
    // nightly 分支=com.termux.nightly）。需 manifest <queries> 声明，
    // 否则 Android 11+ 对已装的包也隐身
    val candidates = listOf("com.termux", "com.termux.nightly")
    val termuxPkg = candidates.firstOrNull { pkg ->
        try {
            ctx.packageManager.getPackageInfo(pkg, 0); true
        } catch (e: Exception) { false }
    }
    if (termuxPkg == null) {
        Toast.makeText(ctx, "❌ 未检测到 Termux——请先安装 " +
            "F-Droid/GitHub 版 Termux（若已装，是 Android 包可见性限制：" +
            "更新本 app 至最新版）", Toast.LENGTH_LONG).show()
        return
    }
    val intent = Intent("com.termux.RUN_COMMAND").apply {
        setClassName(termuxPkg, "com.termux.app.RunCommandService")
        putExtra("com.termux.RUN_COMMAND_PATH", program)
        putExtra("com.termux.RUN_COMMAND_ARGUMENTS", args)
        // 0.118 现行键名（旧 WORKDIR 已弃用——错误键名会被静默忽略）
        putExtra("com.termux.RUN_COMMAND_CURRENT_WORKDIR", TERMUX_HOME)
        putExtra("com.termux.RUN_COMMAND_SESSION_NAME", "ScriptRunner")
        putExtra("com.termux.RUN_COMMAND_RUN_IN_TERM", true)
        putExtra("com.termux.RUN_COMMAND_BACKGROUND", false)
    }
    try {
        ctx.startForegroundService(intent)
        sentToast(ctx)
    } catch (e: Exception) {
        // Android 12+/14 对跨 app startForegroundService 收紧——回退
        // startService（服务已 exported 时前台调用允许；Termux 新版
        // 服务内部自管前台类型）
        try {
            ctx.startService(intent)
            sentToast(ctx)
        } catch (e2: Exception) {
            showCallError(ctx, e2)
        }
    }
}

private fun sentToast(ctx: Context) {
    // 已发送 ≠ 已执行：Termux 侧 allow-external-apps 未开时会拒绝并
    // 只发一条通知——明确告诉用户往哪查（实测「没反应」的头号原因）
    Toast.makeText(ctx, "✅ 已发送到 Termux——若 Termux 没弹出新窗口，" +
        "检查 ~/.termux/termux.properties 是否含 allow-external-apps=true " +
        "并完全退出 Termux 重开", Toast.LENGTH_LONG).show()
}

/** 文件卡片命令统一走法：后台执行 → 输出落 Download/scriptrunner_out.txt
 *  → termux-open 自动弹系统查看器。绕开「会话焦点/空窗口」整类问题——
 *  结果一定以系统弹窗形式出现（2026-11-05 实测 Termux 窗口路线不可靠）。
 */
private fun runCardCmd(ctx: Context, body: String) {
    val out = "/sdcard/Download/scriptrunner_out.txt"
    val wrapped = "{ $body ; } > \"$out\" 2>&1; " +
        "echo '── 完成 ──' >> \"$out\"; " +
        "termux-open \"$out\""
    runRaw(ctx, "$TERMUX_PREFIX/bin/bash", arrayOf("-c", wrapped))
}

/** 调用失败大弹窗：完整异常文本（可复制）——小 Toast 看不全排查不了。 */
private fun showCallError(ctx: Context, e: Exception) {
    val full = buildString {
        append("调用失败：${e.javaClass.simpleName}\n")
        append(e.message ?: "（无详细信息）")
        var cause = e.cause
        var depth = 0
        while (cause != null && depth < 3) {
            append("\n← ${cause.javaClass.simpleName}: ${cause.message}")
            cause = cause.cause; depth++
        }
        append("\n\n常见原因：① Termux 版本过旧（请更新到 F-Droid 最新版，")
        append("Android 14+ 需 0.118+）；② allow-external-apps 未生效；")
        append("③ 手机系统限制跨应用启动服务")
    }
    val view = android.widget.TextView(ctx).apply {
        text = full
        setTextIsSelectable(true)
        setPadding(48, 32, 48, 16)
        textSize = 14f
    }
    android.app.AlertDialog.Builder(ctx)
        .setTitle("❌ 调用失败（详情可复制）")
        .setView(view)
        .setPositiveButton("复制全部") { _, _ ->
            val cb = ctx.getSystemService(Context.CLIPBOARD_SERVICE)
                as android.content.ClipboardManager
            cb.setPrimaryClip(
                android.content.ClipData.newPlainText("error", full))
            Toast.makeText(ctx, "已复制", Toast.LENGTH_SHORT).show()
        }
        .setNegativeButton("关闭", null)
        .show()
}

// ── UI ──────────────────────────────────────────────────────
class MainActivity : ComponentActivity() {
    // 现代权限 API（onRequestPermissionsResult 覆写与新 androidx 冲突）
    private lateinit var permLauncher:
        androidx.activity.result.ActivityResultLauncher<String>

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // 首启即请求（早弹早授予；拒绝后可从设置页开）
        permLauncher = registerForActivityResult(
            androidx.activity.result.contract.ActivityResultContracts
                .RequestPermission(),
        ) { granted ->
            if (granted) {
                Toast.makeText(this, "✅ 权限已授予——现在点脚本即可运行",
                               Toast.LENGTH_LONG).show()
            } else {
                Toast.makeText(this, "❌ 未授予。可到 系统设置→应用→" +
                    "ScriptRunner→权限 手动打开「在 Termux 中运行命令」",
                    Toast.LENGTH_LONG).show()
            }
        }
        if (androidx.core.content.ContextCompat.checkSelfPermission(
                this, PERM_RUN_COMMAND
            ) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            permLauncher.launch(PERM_RUN_COMMAND)
        }
        setContent {
            MaterialTheme {
                Surface(Modifier.fillMaxSize()) {
                    AppRoot(this)
                }
            }
        }
    }
}

@Composable
fun AppRoot(ctx: Context) {
    var scripts by remember { mutableStateOf(loadScripts(ctx)) }
    var editing by remember { mutableStateOf<Script?>(null) }
    var editingIndex by remember { mutableIntStateOf(-1) }   // -1 = 新增
    var running by remember { mutableStateOf<Script?>(null) }
    var pendingDel by remember { mutableStateOf<String?>(null) }   // 删除确认

    Column(Modifier.fillMaxSize().padding(16.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("🛠 ScriptRunner", style = MaterialTheme.typography.headlineSmall)
            Spacer(Modifier.width(8.dp))
            val vName = remember {
                try {
                    ctx.packageManager.getPackageInfo(
                        ctx.packageName, 0).versionName
                } catch (e: Exception) { "?" }
            }
            Text(
                "v$vName",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Text(
            "点脚本运行（输出在 Termux 窗口）。前置：① Termux 设 " +
                "allow-external-apps=true 并 termux-reload-settings；" +
                "② 小米/澎湃系统：系统设置→应用→ScriptRunner→「后台弹出界面」" +
                "放行（否则命令执行了但界面弹不出来）",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(12.dp))

        LazyColumn(
            modifier = Modifier.weight(1f),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            items(scripts.size) { i ->
                val s = scripts[i]
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(14.dp)) {
                        Text(s.name, style = MaterialTheme.typography.titleMedium)
                        Text(
                            s.program.substringAfterLast('/') +
                                " " + s.args.joinToString(" "),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            maxLines = 2,
                        )
                        Row(
                            Modifier.padding(top = 8.dp),
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            Button(onClick = {
                                if (placeholdersOf(s.args).isEmpty()) {
                                    runInTermux(ctx, s, emptyMap())
                                } else {
                                    running = s
                                }
                            }) { Text("▶ 运行") }
                            OutlinedButton(onClick = {
                                editing = s; editingIndex = i
                            }) { Text("编辑") }
                            OutlinedButton(onClick = {
                                scripts = scripts.toMutableList().apply {
                                    removeAt(i)
                                }
                                saveScripts(ctx, scripts)
                            }) { Text("删除") }
                        }
                    }
                }
            }
        }

        Spacer(Modifier.height(12.dp))
        Button(
            onClick = {
                editing = Script("", "$TERMUX_PREFIX/bin/python3", listOf())
                editingIndex = -1
            },
            modifier = Modifier.fillMaxWidth(),
        ) { Text("+ 添加脚本") }

        // ── 📂 Termux 脚本文件管理 ──
        // 普通 app 无权直接写 Termux 私有目录——复制/删除/查看全部
        // 发 RUN_COMMAND 让 Termux 自己执行（结果在 Termux 窗口可见）
        var srcPath by remember {
            mutableStateOf("/sdcard/Download/")
        }
        var delName by remember { mutableStateOf("") }
        Spacer(Modifier.height(16.dp))
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(14.dp)) {
                Text("📂 Termux 脚本文件管理",
                     style = MaterialTheme.typography.titleMedium)
                Text(
                    "复制/删除 Termux home（~/）里的脚本文件；" +
                        "操作经 Termux 执行，结果看 Termux 窗口",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.height(8.dp))
                Button(onClick = {
                    runCardCmd(ctx,
                        "echo '[1] allow-external 配置行（有 = 正常）：'; " +
                        "grep -n allow-external \"\$HOME/.termux/termux.properties\" 2>&1; " +
                        "echo '[2] Termux 版本：'; echo \"\$TERMUX_VERSION\"; " +
                        "echo '[3] 重载设置：'; termux-reload-settings 2>&1 " +
                        "&& echo '已重载'; echo '── 诊断完成，截这段给我 ──'")
                }, modifier = Modifier.fillMaxWidth()) {
                    Text("🔍 诊断 Termux（配置/版本/重载）")
                }
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(
                    value = srcPath, onValueChange = { srcPath = it },
                    label = { Text("脚本文件路径（源）") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(8.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = {
                        runCardCmd(ctx,
                            "cp \"$srcPath\" \"\$HOME/\" 2>&1 " +
                            "&& echo \"✅ 已复制：\$(basename \"$srcPath\")\"")
                    }) { Text("📋 复制到 Termux") }
                    OutlinedButton(onClick = {
                        runCardCmd(ctx,
                            "/data/data/com.termux/files/usr/bin/ls -la " +
                            "\$HOME 2>&1")
                    }) { Text("📜 查看 home 文件") }
                }
                Spacer(Modifier.height(8.dp))
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    OutlinedTextField(
                        value = delName, onValueChange = { delName = it },
                        label = { Text("home 里的文件名") },
                        singleLine = true,
                        modifier = Modifier.weight(1f),
                    )
                    Button(onClick = {
                        if (delName.isNotBlank()) pendingDel = delName.trim()
                    }) { Text("🗑 删除") }
                }
            }
        }
    }

    // ── 删除确认 ──
    pendingDel?.let { name ->
        AlertDialog(
            onDismissRequest = { pendingDel = null },
            title = { Text("删除确认") },
            text = { Text("确定删除 Termux home 里的「$name」？此操作不可恢复。") },
            confirmButton = {
                Button(onClick = {
                    runRaw(ctx, "$TERMUX_PREFIX/bin/bash", arrayOf(
                        "-c", "rm ~/\"$name\" && echo \"✅ 已删除 $name\""))
                    pendingDel = null
                }) { Text("删除") }
            },
            dismissButton = {
                OutlinedButton(onClick = { pendingDel = null }) { Text("取消") }
            },
        )
    }

    // ── 运行参数输入对话框 ──
    running?.let { s ->
        val phs = placeholdersOf(s.args)
        val values = remember(s) { mutableStateMapOf<String, String>() }
        AlertDialog(
            onDismissRequest = { running = null },
            title = { Text("运行：${s.name}") },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    phs.forEach { ph ->
                        OutlinedTextField(
                            value = values[ph] ?: "",
                            onValueChange = { values[ph] = it },
                            label = { Text(ph) },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth(),
                        )
                    }
                    if (phs.isEmpty()) Text("该脚本无参数，直接确认运行。")
                }
            },
            confirmButton = {
                Button(onClick = {
                    runInTermux(ctx, s, values)
                    running = null
                }) { Text("▶ 在 Termux 运行") }
            },
            dismissButton = {
                OutlinedButton(onClick = { running = null }) { Text("取消") }
            },
        )
    }

    // ── 编辑/新增对话框 ──
    editing?.let { s ->
        var name by remember(s) { mutableStateOf(s.name) }
        var program by remember(s) { mutableStateOf(s.program) }
        var argsText by remember(s) {
            mutableStateOf(s.args.joinToString("␟"))
        }
        AlertDialog(
            onDismissRequest = { editing = null },
            title = { Text(if (editingIndex >= 0) "编辑脚本" else "添加脚本") },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    OutlinedTextField(
                        value = name, onValueChange = { name = it },
                        label = { Text("名称") }, singleLine = true,
                        modifier = Modifier.fillMaxWidth(),
                    )
                    OutlinedTextField(
                        value = program, onValueChange = { program = it },
                        label = { Text("程序（Termux 绝对路径）") },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth(),
                    )
                    OutlinedTextField(
                        value = argsText, onValueChange = { argsText = it },
                        label = { Text("参数（␟ 分隔多段；{提示} 为运行时输入）") },
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Text(
                        "示例：$TERMUX_HOME/extract.pyz␟{目录}␟--dry",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            },
            confirmButton = {
                Button(onClick = {
                    val args = argsText.split("␟").map { it.trim() }
                        .filter { it.isNotEmpty() }
                    val ns = Script(name.trim(), program.trim(), args)
                    scripts = scripts.toMutableList().apply {
                        if (editingIndex >= 0) set(editingIndex, ns) else add(ns)
                    }
                    saveScripts(ctx, scripts)
                    editing = null
                }) { Text("保存") }
            },
            dismissButton = {
                OutlinedButton(onClick = { editing = null }) { Text("取消") }
            },
        )
    }
}
