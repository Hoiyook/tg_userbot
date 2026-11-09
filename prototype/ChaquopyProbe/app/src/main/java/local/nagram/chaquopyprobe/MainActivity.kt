package local.nagram.chaquopyprobe

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import android.view.ViewGroup
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.TextView
import java.io.File

/**
 * 探针 UI：启动前台服务 → 每秒显示服务真实状态 → 状态显示服务已启动后
 * 加载 127.0.0.1:8765（另加 3s/8s 固定延迟兜底）。
 *
 * v0.4：不再用 HttpURLConnection 探测端口（在部分 Android 上被 cleartext/
 * 代理策略挡住，导致 loadUrl 永不调用）；改为以服务状态为准 + 定时兜底。
 */
class MainActivity : Activity() {

    private lateinit var web: WebView
    private lateinit var status: TextView
    private val handler = Handler(Looper.getMainLooper())
    private var loaded = false
    private var dashboardOpened = false

    private val statusPoller = object : Runnable {
        override fun run() {
            val s = ProbeService.lastStatus
            status.text = s
            status.setTextColor(
                if (s.contains("失败") || s.contains("Exception")) Color.RED
                else Color.DKGRAY)
            // 服务报告 HTTP 已启动 → 立刻加载页面（只加载一次）
            if (!loaded && s.contains("started via")) {
                loadProbe()
            }
            // 开机自启的核心已在跑：直接打开看板（带 token）
            if (!dashboardOpened && s.contains("核心运行中")) {
                dashboardOpened = true
                openDashboard()
            }
            handler.postDelayed(this, 1000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // 首次启动：secrets/session 没就绪时先进初始化页，且不启动 Python
        // （保证 tg_userbot.config 在文件就绪后才 import，不会读到空 secrets）。
        val secrets = File(filesDir, "tg_secrets.json")
        val session = File(filesDir, "tg_downloader.session")
        if (!secrets.exists() || !session.exists()) {
            stopService(Intent(this, ProbeService::class.java))
            startActivity(Intent(this, OnboardingActivity::class.java))
            finish()
            return
        }
        // 每次启动都按当前授权状态刷新公共存储选择（权限可能被撤销）
        PublicStorage.persist(this)

        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(
                arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1001)
        }

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.WHITE)
        }

        status = TextView(this).apply {
            text = "正在启动 Python 探针…"
            textSize = 13f
            setTextColor(Color.DKGRAY)
            setPadding(24, 24, 24, 8)
        }

        val buttons = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
        }
        buttons.addView(Button(this).apply {
            text = "启动服务"
            setOnClickListener { startProbe() }
        })
        buttons.addView(Button(this).apply {
            text = "停止服务"
            setOnClickListener {
                stopService(Intent(this@MainActivity, ProbeService::class.java))
                status.text = "服务已停止"
            }
        })
        buttons.addView(Button(this).apply {
            text = "启动核心"
            setOnClickListener {
                startForegroundService(
                    Intent(this@MainActivity, ProbeService::class.java)
                        .setAction("core"))
                status.text = "核心启动中…（Telethon 登录）"
            }
        })
        buttons.addView(Button(this).apply {
            text = "打开看板"
            setOnClickListener { openDashboard() }
        })
        buttons.addView(Button(this).apply {
            text = "电池白名单"
            setOnClickListener {
                val pm = getSystemService(PowerManager::class.java)
                if (pm.isIgnoringBatteryOptimizations(packageName)) {
                    status.text = "✅ 已在电池白名单"
                } else {
                    try {
                        startActivity(Intent(
                            Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                            Uri.parse("package:" + packageName)))
                    } catch (_: Exception) {
                        startActivity(Intent(
                            Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
                    }
                }
            }
        })
        buttons.addView(Button(this).apply {
            text = "存储授权"
            setOnClickListener {
                PublicStorage.request(this@MainActivity)
                status.text = "授权后点「重启App」使下载目录生效"
            }
        })
        buttons.addView(Button(this).apply {
            text = "重启App"
            setOnClickListener {
                val intent = Intent(this@MainActivity, MainActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or
                        Intent.FLAG_ACTIVITY_CLEAR_TASK)
                startActivity(intent)
                Runtime.getRuntime().exit(0)
            }
        })
        buttons.addView(Button(this).apply {
            text = "扫描媒体"
            setOnClickListener {
                status.text = "扫描公共目录…"
                Thread {
                    val n = PublicStorage.scanMedia(applicationContext)
                    runOnUiThread {
                        status.text = if (n > 0) {
                            "已扫描 $n 个媒体文件到相册"
                        } else {
                            "没有可扫描的媒体（未授权或目录为空）"
                        }
                    }
                }.start()
            }
        })
        buttons.addView(Button(this).apply {
            text = "刷新页面"
            setOnClickListener { loadProbe() }
        })

        // 5 个按钮在窄屏会超宽：套一层横向滚动，不裁剪
        val buttonScroll = HorizontalScrollView(this).apply {
            isHorizontalScrollBarEnabled = false
            addView(buttons)
        }

        web = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            webViewClient = WebViewClient()
            // 模拟器 ANGLE 对 WebView 支持不佳（Program link failed），
            // 软件层保证探针页可渲染；真机同样可用（页面很轻）。
            setLayerType(View.LAYER_TYPE_SOFTWARE, null)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f)
        }

        root.addView(status)
        root.addView(buttonScroll)
        root.addView(web)
        setContentView(root)

        handler.post(statusPoller)
        startProbe()
    }

    private fun startProbe() {
        loaded = false
        status.text = "启动前台服务中…"
        startForegroundService(Intent(this, ProbeService::class.java))
        // 服务状态轮询是主路径；3s/8s 是兜底
        handler.postDelayed({ loadProbe() }, 3000)
        handler.postDelayed({ web.loadUrl(PROBE_URL) }, 8000)
    }

    private fun loadProbe() {
        loaded = true
        web.loadUrl(PROBE_URL)
    }

    /** 真正的看板：由 android_main 返回带 token 的本地地址。 */
    private fun openDashboard() {
        dashboardOpened = true
        status.text = "打开看板…"
        Thread {
            try {
                val url = PyRuntime.module("android_main")
                    .callAttr("dashboard_url")
                    .toString()
                runOnUiThread {
                    web.loadUrl(url)
                    status.text = "看板已加载"
                }
            } catch (t: Throwable) {
                runOnUiThread {
                    status.text = "打开看板失败：${t.javaClass.simpleName}: ${t.message}"
                }
            }
        }.start()
    }

    override fun onResume() {
        super.onResume()
        // 从「所有文件访问」设置页返回后刷新公共存储选择
        PublicStorage.persist(this)
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        super.onDestroy()
    }

    companion object {
        private const val PROBE_URL = "http://127.0.0.1:8765/"
    }
}
