package local.nagram.chaquopyprobe

import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.text.method.ScrollingMovementMethod
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONObject
import java.io.File
import java.security.SecureRandom

/**
 * 首次启动初始化：导入 tg_secrets.json + tg_downloader.session。
 *
 * 这是 onboarding 的最小可用版；手机号/验证码/2FA 登录界面是下一版。
 * 两个文件都导入后，点「完成」进入 MainActivity，探针/核心才启动
 * （因此 tg_userbot.config 会在文件就绪后才 import，不会读到空 secrets）。
 *
 * 重要：session 是账号 auth key，**不能和 Mac bot 同时在线**。首次使用
 * 前先在 Mac 上 ./run.sh stop；手机核心停掉后再 ./run.sh start。
 */
class OnboardingActivity : Activity() {

    private lateinit var status: TextView
    private lateinit var ready: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.WHITE)
            setPadding(32, 48, 32, 32)
        }

        root.addView(TextView(this).apply {
            text = "TG Userbot 初始化"
            textSize = 22f
            setTextColor(Color.BLACK)
        })
        root.addView(TextView(this).apply {
            text = "\n首次使用需要两个文件：\n" +
                "1) tg_secrets.json（API_ID / API_HASH）\n" +
                "2) tg_downloader.session（Telethon session）\n\n" +
                "注意：session 是账号 auth key，不能和 Mac bot 同时在线。\n" +
                "请先在 Mac 上执行 ./run.sh stop，再导入并启动手机核心。\n"
            textSize = 13f
            setTextColor(Color.DKGRAY)
        })

        root.addView(Button(this).apply {
            text = "① 导入 tg_secrets.json"
            setOnClickListener { pick(REQ_SECRETS) }
        })
        root.addView(Button(this).apply {
            text = "② 导入 tg_downloader.session"
            setOnClickListener { pick(REQ_SESSION) }
        })
        root.addView(Button(this).apply {
            text = "没有 session？用手机号登录"
            setOnClickListener {
                if (!File(filesDir, "tg_secrets.json").exists()) {
                    status.text = "请先导入 tg_secrets.json（API_ID/API_HASH）"
                } else {
                    startActivity(Intent(this@OnboardingActivity,
                        LoginActivity::class.java))
                }
            }
        })
        root.addView(Button(this).apply {
            text = "授权存储（公共 Downloads）"
            setOnClickListener { PublicStorage.request(this@OnboardingActivity) }
        })
        root.addView(Button(this).apply {
            text = "③ 完成，进入主界面"
            setOnClickListener { finishAndStart() }
        })

        ready = TextView(this).apply {
            text = ""
            textSize = 13f
            setTextColor(Color.DKGRAY)
            setPadding(0, 24, 0, 8)
        }
        root.addView(ready)

        status = TextView(this).apply {
            text = ""
            textSize = 12f
            setTextColor(Color.DKGRAY)
            setPadding(0, 8, 0, 0)
            movementMethod = ScrollingMovementMethod()
        }
        root.addView(status)

        setContentView(root)
        refreshReady()
    }

    private fun pick(requestCode: Int) {
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "*/*"
        }
        @Suppress("DEPRECATION")
        startActivityForResult(intent, requestCode)
    }

    @Deprecated("Android 原生 onActivityResult（minSdk 29，够用）")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (resultCode != RESULT_OK) return
        val uri = data?.data ?: return
        when (requestCode) {
            REQ_SECRETS -> importSecrets(uri)
            REQ_SESSION -> importSession(uri)
        }
        refreshReady()
    }

    private fun importSecrets(uri: android.net.Uri) {
        try {
            val text = contentResolver.openInputStream(uri)
                ?.bufferedReader()?.use { it.readText() }
                ?: throw IllegalArgumentException("无法读取文件")
            val obj = JSONObject(text)
            if (!obj.has("api_id") || !obj.has("api_hash")) {
                throw IllegalArgumentException("缺少 api_id / api_hash")
            }
            if (!obj.has("api")) {
                // 没有 api 段时补一个本机看板配置（token 随机生成）
                obj.put("api", JSONObject().apply {
                    put("enabled", true)
                    put("bind", "127.0.0.1")
                    put("port", 8765)
                    put("token", randomToken())
                })
            }
            File(filesDir, "tg_secrets.json")
                .writeText(obj.toString(2))
            status.text = "✅ tg_secrets.json 已导入"
        } catch (t: Throwable) {
            status.text = "❌ secrets 导入失败：${t.javaClass.simpleName}: ${t.message}"
        }
    }

    private fun importSession(uri: android.net.Uri) {
        try {
            val target = File(filesDir, "tg_downloader.session")
            contentResolver.openInputStream(uri)?.use { input ->
                target.outputStream().use { output -> input.copyTo(output) }
            } ?: throw IllegalArgumentException("无法读取文件")
            val header = ByteArray(16)
            target.inputStream().use { it.read(header) }
            if (!String(header).startsWith("SQLite format 3")) {
                target.delete()
                throw IllegalArgumentException("不是 Telethon 的 SQLite session 文件")
            }
            status.text = "✅ session 已导入（${target.length()} 字节）"
        } catch (t: Throwable) {
            status.text = "❌ session 导入失败：${t.javaClass.simpleName}: ${t.message}"
        }
    }

    override fun onResume() {
        super.onResume()
        PublicStorage.persist(this)
        refreshReady()
    }

    private fun refreshReady() {
        val secrets = File(filesDir, "tg_secrets.json").exists()
        val session = File(filesDir, "tg_downloader.session").exists()
        val pub = PublicStorage.hasAccess(this)
        ready.text = "就绪状态：secrets ${if (secrets) "✅" else "❌"}" +
            " · session ${if (session) "✅" else "❌"}\n" +
            "存储：" + if (pub) {
                "公共 Downloads（${PublicStorage.targetDir(this).absolutePath}）"
            } else {
                "App 私有（可点上方按钮授权公共 Downloads）"
            }
    }

    private fun finishAndStart() {
        val secrets = File(filesDir, "tg_secrets.json").exists()
        val session = File(filesDir, "tg_downloader.session").exists()
        if (!secrets || !session) {
            status.text = "还缺文件：${if (!secrets) "tg_secrets.json " else ""}" +
                "${if (!session) "tg_downloader.session" else ""}"
            return
        }
        PublicStorage.persist(this)
        startActivity(Intent(this, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP))
        finish()
    }

    private fun randomToken(): String {
        val bytes = ByteArray(24)
        SecureRandom().nextBytes(bytes)
        return android.util.Base64.encodeToString(
            bytes,
            android.util.Base64.URL_SAFE or
                android.util.Base64.NO_WRAP or
                android.util.Base64.NO_PADDING)
    }

    companion object {
        private const val REQ_SECRETS = 1001
        private const val REQ_SESSION = 1002
    }
}
