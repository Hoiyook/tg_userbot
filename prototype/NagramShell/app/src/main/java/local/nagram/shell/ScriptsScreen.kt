package local.nagram.shell

import android.content.Context
import android.content.Intent
import android.widget.Toast
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.json.JSONArray
import org.json.JSONObject

// ── 脚本启动器（自 ScriptRunner v1.11 移植，2026-11-07 合并）──────
private const val TERMUX_PREFIX = "/data/data/com.termux/files/usr"
private const val TERMUX_HOME = "/data/data/com.termux/files/home"
private const val PREFS = "scripts"
private const val KEY_SCRIPTS = "scripts_json"
private const val PERM_RUN_COMMAND = "com.termux.permission.RUN_COMMAND"

data class Script(
    val name: String,
    val program: String,
    val args: List<String>,
)

private val OUT_FILE = "/sdcard/Download/scriptrunner_out.txt"

private val EXTRACT_DRY_BODY =
    "out=$OUT_FILE; " +
    "if [ -f \"\$HOME/extract.pyz\" ]; then " +
    "python3 \"\$HOME/extract.pyz\" \"\$1\" --dry > \"\$out\" 2>&1; " +
    "else echo \"❌ extract.pyz 不在 Termux home——先在 文件管理卡片" +
    "点「复制到 Termux」（源 /sdcard/Download/extract.pyz）\" > \"\$out\"; fi; " +
    "termux-open \"\$out\""

private val EXTRACT_RUN_BODY =
    "out=$OUT_FILE; " +
    "if [ -f \"\$HOME/extract.pyz\" ]; then " +
    "python3 \"\$HOME/extract.pyz\" \"\$1\" > \"\$out\" 2>&1; " +
    "else echo \"❌ extract.pyz 不在 Termux home——先在 文件管理卡片" +
    "点「复制到 Termux」（源 /sdcard/Download/extract.pyz）\" > \"\$out\"; fi; " +
    "termux-open \"\$out\""

private fun defaultScripts() = listOf(
    Script("🗜 解压·预览 (dry)", "$TERMUX_PREFIX/bin/bash",
        listOf("-c", EXTRACT_DRY_BODY, "_", "{要解压的目录}")),
    Script("🗜 解压·执行", "$TERMUX_PREFIX/bin/bash",
        listOf("-c", EXTRACT_RUN_BODY, "_", "{要解压的目录}")),
    Script("⚙️ 自定义命令行", "$TERMUX_PREFIX/bin/bash",
        listOf("-c", "{完整命令行，如 python3 ~/x.py 参数1}")),
)

private fun loadScripts(ctx: Context): MutableList<Script> {
    val raw = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        .getString(KEY_SCRIPTS, null)
        ?: return defaultScripts().toMutableList()
    return try {
        val arr = JSONArray(raw)
        val list = (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            val args = mutableListOf<String>()
            val a = o.getJSONArray("args")
            for (j in 0 until a.length()) args.add(a.getString(j))
            Script(o.getString("name"), o.getString("program"), args)
        }.toMutableList()
        migrateStaleDefaults(list)
        list
    } catch (e: Exception) {
        defaultScripts().toMutableList()
    }
}

/** 存量治愈：v1.2 保存的解压默认脚本没有 extract.pyz 存在检查，
 *  且后续默认脚本随版本演进（out 文件/弹结果）——同名旧版就地升级，
 *  用户自建脚本（名字不同）绝不碰。 */
private fun migrateStaleDefaults(list: MutableList<Script>) {
    val defaults = defaultScripts()
    for (i in list.indices) {
        val d = defaults.firstOrNull { it.name == list[i].name }
            ?: continue
        val body = list[i].args.firstOrNull { it.contains("extract.pyz") }
        if (body == null || !body.contains("scriptrunner_out.txt"))
            list[i] = d
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

private val PLACEHOLDER = Regex("\\{(.+?)\\}")
private fun placeholdersOf(args: List<String>): List<String> =
    args.flatMap { PLACEHOLDER.findAll(it).map { m -> m.groupValues[1] } }
private fun substitute(arg: String, values: Map<String, String>): String =
    PLACEHOLDER.replace(arg) { m -> values[m.groupValues[1]] ?: m.groupValues[0] }

/** 原始命令发射（权限检查 + RUN_COMMAND）；脚本运行与文件管理卡共用。 */
private fun runRaw(ctx: Context, program: String, args: Array<String>) {
    val granted = androidx.core.content.ContextCompat.checkSelfPermission(
        ctx, PERM_RUN_COMMAND
    ) == android.content.pm.PackageManager.PERMISSION_GRANTED
    if (!granted) {
        val act = ctx as? android.app.Activity
        if (act != null) {
            act.requestPermissions(arrayOf(PERM_RUN_COMMAND), 1001)
            Toast.makeText(ctx, "⚠️ 请在弹窗里点「允许」（Termux 运行命令权限）",
                Toast.LENGTH_LONG).show()
        }
        return
    }
    val intent = Intent("com.termux.RUN_COMMAND").apply {
        setClassName("com.termux", "com.termux.app.RunCommandService")
        putExtra("com.termux.RUN_COMMAND_PATH", program)
        putExtra("com.termux.RUN_COMMAND_ARGUMENTS", args)
        putExtra("com.termux.RUN_COMMAND_WORKDIR", TERMUX_HOME)
        putExtra("com.termux.RUN_COMMAND_RUN_IN_TERM", true)
        putExtra("com.termux.RUN_COMMAND_BACKGROUND", false)
    }
    try {
        ctx.startForegroundService(intent)
        Toast.makeText(ctx, "✅ 已发送到 Termux——输出看 Termux 窗口",
            Toast.LENGTH_LONG).show()
    } catch (e: Exception) {
        Toast.makeText(ctx, "❌ 调用失败（${e.javaClass.simpleName}）："
            + "确认 Termux 为 F-Droid/GitHub 版且 allow-external-apps=true",
            Toast.LENGTH_LONG).show()
    }
}

private fun runInTermux(ctx: Context, script: Script,
                        argValues: Map<String, String>) {
    val args = script.args.map { substitute(it, argValues) }.toTypedArray()
    runRaw(ctx, script.program, args)
}

private fun runCardCmd(ctx: Context, body: String) {
    val out = "/sdcard/Download/scriptrunner_out.txt"
    val wrapped = "{ $body ; } > \"$out\" 2>&1; " +
        "echo '── 完成 ──' >> \"$out\"; " +
        "termux-open \"$out\""
    runRaw(ctx, "$TERMUX_PREFIX/bin/bash", arrayOf("-c", wrapped))
}

@Composable
fun ScriptsScreen() {
    val ctx = LocalContext.current
    var scripts by remember { mutableStateOf(loadScripts(ctx)) }
    var editing by remember { mutableStateOf<Script?>(null) }
    var editingIndex by remember { mutableIntStateOf(-1) }
    var running by remember { mutableStateOf<Script?>(null) }
    // 文件管理卡状态（composable 顶层——删除确认对话框在 Column 外）
    var srcPath by remember { mutableStateOf("/sdcard/Download/") }
    var delName by remember { mutableStateOf("") }
    var pendingDel by remember { mutableStateOf<String?>(null) }

    Column(Modifier.fillMaxSize().background(BgApp).padding(16.dp)) {
        Text("🛠 脚本", color = InkStrong, fontSize = 22.sp,
             fontWeight = FontWeight.Bold)
        Text("点脚本运行（输出在 Termux 窗口）。前置：Termux 设 " +
            "allow-external-apps=true",
            style = MaterialTheme.typography.bodySmall, color = InkMut)
        Spacer(Modifier.height(12.dp))

        LazyColumn(Modifier.weight(1f),
                   verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(scripts.size) { i ->
                val s = scripts[i]
                Card(
                    Modifier.fillMaxWidth()
                        .border(1.dp, Hairline, RoundedCornerShape(14.dp)),
                    shape = RoundedCornerShape(14.dp),
                    colors = CardDefaults.cardColors(containerColor = CardWhite),
                    elevation = CardDefaults.cardElevation(defaultElevation = 0.dp),
                ) {
                    Column(Modifier.padding(14.dp)) {
                        Text(s.name, color = InkStrong, fontSize = 16.sp,
                             fontWeight = FontWeight.SemiBold)
                        Text(s.program.substringAfterLast('/') +
                            " " + s.args.joinToString(" "),
                            color = InkMut, fontSize = 12.sp, maxLines = 2)
                        Row(Modifier.padding(top = 8.dp),
                            horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Button(onClick = {
                                if (placeholdersOf(s.args).isEmpty())
                                    runInTermux(ctx, s, emptyMap())
                                else running = s
                            }) { Text("▶ 运行") }
                            OutlinedButton(onClick = {
                                editing = s; editingIndex = i }) {
                                Text("编辑") }
                            TextButton(onClick = {
                                scripts = scripts.toMutableList()
                                    .apply { removeAt(i) }
                                saveScripts(ctx, scripts)
                            }) { Text("删除", color = Danger) }
                        }
                    }
                }
            }
        }

        Spacer(Modifier.height(12.dp))
        OutlinedButton(onClick = {
            editing = Script("", "$TERMUX_PREFIX/bin/python3", listOf())
            editingIndex = -1
        }, modifier = Modifier.fillMaxWidth()) { Text("＋ 添加脚本") }

        // ── 📂 Termux 脚本文件管理（自 ScriptRunner 移植）──────────
        // 普通 app 无权直接写 Termux 私有目录——复制/删除/查看全部
        // 发 RUN_COMMAND 让 Termux 自己执行（结果在 Termux 窗口可见）
        Spacer(Modifier.height(16.dp))
        Card(
            Modifier.fillMaxWidth()
                .border(1.dp, Hairline, RoundedCornerShape(14.dp)),
            shape = RoundedCornerShape(14.dp),
            colors = CardDefaults.cardColors(containerColor = CardWhite),
            elevation = CardDefaults.cardElevation(defaultElevation = 0.dp),
        ) {
            Column(Modifier.padding(14.dp)) {
                Text("📂 Termux 脚本文件管理", color = InkStrong,
                     style = MaterialTheme.typography.titleMedium,
                     fontWeight = FontWeight.SemiBold)
                Text(
                    "复制/删除 Termux home（~/）里的脚本文件；" +
                        "操作经 Termux 执行，结果看 Termux 窗口",
                    style = MaterialTheme.typography.bodySmall, color = InkMut,
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
                Row(verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(
                        value = delName, onValueChange = { delName = it },
                        label = { Text("home 里的文件名") },
                        singleLine = true,
                        modifier = Modifier.weight(1f),
                    )
                    Button(
                        onClick = {
                            if (delName.isNotBlank())
                                pendingDel = delName.trim()
                        },
                        colors = ButtonDefaults.buttonColors(
                            containerColor = Danger),
                    ) { Text("🗑 删除") }
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
                Button(
                    onClick = {
                        runRaw(ctx, "$TERMUX_PREFIX/bin/bash", arrayOf(
                            "-c", "rm ~/\"$name\" && echo \"✅ 已删除 $name\""))
                        pendingDel = null
                    },
                    colors = ButtonDefaults.buttonColors(containerColor = Danger),
                ) { Text("删除") }
            },
            dismissButton = {
                OutlinedButton(onClick = { pendingDel = null }) { Text("取消") }
            },
        )
    }

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
                            label = { Text(ph) }, singleLine = true,
                            modifier = Modifier.fillMaxWidth())
                    }
                    if (phs.isEmpty()) Text("该脚本无参数，直接确认运行。")
                }
            },
            confirmButton = { Button(onClick = {
                runInTermux(ctx, s, values); running = null
            }) { Text("▶ 在 Termux 运行") } },
            dismissButton = { OutlinedButton(onClick = {
                running = null }) { Text("取消") } },
        )
    }

    editing?.let { s ->
        var name by remember(s) { mutableStateOf(s.name) }
        var program by remember(s) { mutableStateOf(s.program) }
        var argsText by remember(s) { mutableStateOf(s.args.joinToString("␟")) }
        AlertDialog(
            onDismissRequest = { editing = null },
            title = { Text(if (editingIndex >= 0) "编辑脚本" else "添加脚本") },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    OutlinedTextField(value = name,
                        onValueChange = { name = it },
                        label = { Text("名称") }, singleLine = true,
                        modifier = Modifier.fillMaxWidth())
                    OutlinedTextField(value = program,
                        onValueChange = { program = it },
                        label = { Text("程序（Termux 绝对路径）") },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth())
                    OutlinedTextField(value = argsText,
                        onValueChange = { argsText = it },
                        label = { Text("参数（␟ 分隔多段；{提示} 为运行时输入）") },
                        modifier = Modifier.fillMaxWidth())
                }
            },
            confirmButton = { Button(onClick = {
                val args = argsText.split("␟").map { it.trim() }
                    .filter { it.isNotEmpty() }
                val ns = Script(name.trim(), program.trim(), args)
                scripts = scripts.toMutableList().apply {
                    if (editingIndex >= 0) set(editingIndex, ns) else add(ns)
                }
                saveScripts(ctx, scripts)
                editing = null
            }) { Text("保存") } },
            dismissButton = { OutlinedButton(onClick = {
                editing = null }) { Text("取消") } },
        )
    }
}
