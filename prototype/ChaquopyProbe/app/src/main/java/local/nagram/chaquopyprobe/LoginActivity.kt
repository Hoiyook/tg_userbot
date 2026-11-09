package local.nagram.chaquopyprobe

import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.text.InputType
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import java.io.File

/**
 * 手机号 / 验证码 / 2FA 登录：新建独立 session（可与 Mac bot 同时在线）。
 * 前置：tg_secrets.json 已导入（API_ID / API_HASH）。
 */
class LoginActivity : Activity() {

    private lateinit var phone: EditText
    private lateinit var code: EditText
    private lateinit var password: EditText
    private lateinit var sendBtn: Button
    private lateinit var loginBtn: Button
    private lateinit var pwdBtn: Button
    private lateinit var status: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Python.start 放主线程（与 ProbeService 一致；PyRuntime 幂等）
        try {
            PyRuntime.ensure(this)
        } catch (_: Throwable) {
            // 失败时由按钮点击的 catch 把具体错误显示出来
        }

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.WHITE)
            setPadding(32, 48, 32, 32)
        }
        root.addView(TextView(this).apply {
            text = "手机号登录（新建独立 session）"
            textSize = 22f
            setTextColor(Color.BLACK)
        })
        root.addView(TextView(this).apply {
            text = "\n会新建一个独立 session，可与 Mac bot 同时在线；" +
                "验证码发到你账号的 Telegram。\n" +
                "前置：tg_secrets.json 已导入（API_ID / API_HASH）。\n"
            textSize = 13f
            setTextColor(Color.DKGRAY)
        })

        phone = EditText(this).apply {
            hint = "手机号（含国家码，如 +8613800000000）"
            inputType = InputType.TYPE_CLASS_PHONE
        }
        root.addView(phone)

        sendBtn = Button(this).apply {
            text = "① 发送验证码"
            setOnClickListener { call("send_code", phone.text.toString()) }
        }
        root.addView(sendBtn)

        code = EditText(this).apply {
            hint = "Telegram 验证码"
            inputType = InputType.TYPE_CLASS_NUMBER
            isEnabled = false
        }
        root.addView(code)

        loginBtn = Button(this).apply {
            text = "② 登录"
            isEnabled = false
            setOnClickListener {
                loginBtn.isEnabled = false
                call("sign_in", code.text.toString())
            }
        }
        root.addView(loginBtn)

        password = EditText(this).apply {
            hint = "两步验证密码（如启用）"
            inputType = InputType.TYPE_CLASS_TEXT or
                InputType.TYPE_TEXT_VARIATION_PASSWORD
            visibility = TextView.GONE
        }
        root.addView(password)

        pwdBtn = Button(this).apply {
            text = "③ 提交 2FA 密码"
            visibility = TextView.GONE
            setOnClickListener {
                pwdBtn.visibility = TextView.GONE
                call("sign_in_password", password.text.toString())
            }
        }
        root.addView(pwdBtn)

        status = TextView(this).apply {
            text = ""
            textSize = 13f
            setTextColor(Color.DKGRAY)
            setPadding(0, 24, 0, 0)
        }
        root.addView(status)
        setContentView(root)

        if (!File(filesDir, "tg_secrets.json").exists()) {
            status.text = "⚠️ 请先在上一页导入 tg_secrets.json（API_ID/API_HASH）"
            sendBtn.isEnabled = false
        }
    }

    private fun call(fn: String, arg: String) {
        status.text = "处理中…（首次 import Telethon 需要几秒）"
        Thread {
            try {
                PyRuntime.ensure(applicationContext)
                val out = PyRuntime.module("android_login")
                    .callAttr(fn, filesDir.absolutePath, arg)
                    .toString()
                runOnUiThread { handle(fn, out) }
            } catch (t: Throwable) {
                runOnUiThread {
                    status.text = "失败：${t.javaClass.simpleName}: ${t.message}"
                }
            }
        }.start()
    }

    private fun handle(fn: String, out: String) {
        when {
            fn == "send_code" && out == "code_sent" -> {
                status.text = "✅ 验证码已发送，请查看 Telegram"
                code.isEnabled = true
                loginBtn.isEnabled = true
            }
            fn == "send_code" && out == "already" -> finishToMain()
            fn == "sign_in" && out == "ok" -> finishToMain()
            fn == "sign_in" && out == "password_needed" -> {
                status.text = "需要两步验证密码"
                password.visibility = TextView.VISIBLE
                pwdBtn.visibility = TextView.VISIBLE
            }
            fn == "sign_in_password" && out == "ok" -> finishToMain()
            out.startsWith("code_invalid") -> {
                status.text = "❌ 验证码无效/过期，请重新发送"
                loginBtn.isEnabled = true
            }
            out.startsWith("password_invalid") ->
                status.text = "❌ 两步验证密码错误"
            out.startsWith("flood:") ->
                status.text = "⏳ 触发限流，请等待 ${out.removePrefix("flood:")} 秒"
            out.startsWith("error:") ->
                status.text = "❌ 登录失败：\n${out.removePrefix("error:").take(800)}"
            else -> status.text = "❌ $out"
        }
    }

    private fun finishToMain() {
        status.text = "✅ 登录成功，session 已创建"
        startActivity(Intent(this, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP))
        finish()
    }

    override fun onDestroy() {
        // 用户中途退出：释放未完成的登录连接（不删已成功 session）
        try {
            if (PyRuntime.isStarted()) {
                Thread {
                    try {
                        PyRuntime.module("android_login").callAttr("reset")
                    } catch (_: Throwable) {
                    }
                }.start()
            }
        } catch (_: Throwable) {
        }
        super.onDestroy()
    }
}
