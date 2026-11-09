package local.nagram.chaquopyprobe

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder
import android.util.Log

/**
 * 前台服务：持有 Python 运行时，跑 probe_main / android_main。
 *
 * v0.11：Python.start 统一由 PyRuntime 管理（登录页也会启动 Python，
 * 服务不能再自己调一次）。
 */
class ProbeService : Service() {

    @Volatile
    private var threadStarted = false

    @Volatile
    private var coreStarted = false

    override fun onCreate() {
        super.onCreate()
        createChannel()
        lastStatus = "服务已创建，初始化 Python…"
        try {
            PyRuntime.ensure(applicationContext)
            lastStatus = "Python 已启动，正在启动 HTTP 服务…"
        } catch (t: Throwable) {
            lastStatus = "Python.start 失败：${t.javaClass.simpleName}: ${t.message}"
            Log.e(TAG, "Python.start failed", t)
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Android 14+ 用 specialUse（避免 dataSync 的 6h 上限）；
        // 旧系统回落 dataSync。
        val fgsType = if (android.os.Build.VERSION.SDK_INT >= 34) {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
        } else {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
        }
        startForeground(
            NOTIFICATION_ID,
            buildNotification(lastStatus),
            fgsType,
        )
        notify(lastStatus)

        // action=core：启动真正的 tg_userbot 核心（Telethon + 队列 + worker）
        if (intent?.action == "core") {
            startCore()
            return START_STICKY
        }

        // 开机自启/已运行核心时，MainActivity 的普通启动不要再起探针
        // （探针会抢 8765，与核心的看板 API 冲突）。
        if (coreStarted) {
            lastStatus = "核心运行中（跳过探针）"
            notify(lastStatus)
            return START_STICKY
        }

        if (!threadStarted) {
            threadStarted = true
            Thread {
                try {
                    if (!PyRuntime.isStarted()) {
                        lastStatus = "Python 未启动，跳过 HTTP 服务"
                        notify(lastStatus)
                        return@Thread
                    }
                    val out = PyRuntime.module("probe_main")
                        .callAttr("start", filesDir.absolutePath)
                        .toString()
                    lastStatus = "HTTP 服务：$out"
                    notify(lastStatus)

                    // 给服务器 1.5s 绑端口；失败则把 traceback 暴露出来
                    Thread.sleep(1500)
                    val err = PyRuntime.module("probe_main")
                        .callAttr("server_error")
                        .toString()
                    if (err.isNotBlank()) {
                        lastStatus = "HTTP 服务启动失败：$err"
                        notify(lastStatus)
                    }
                } catch (t: Throwable) {
                    lastStatus = "Python 启动失败：${t.javaClass.simpleName}: ${t.message}"
                    Log.e(TAG, "probe start failed", t)
                    notify(lastStatus)
                }
            }.start()
        }
        return START_STICKY
    }

    private fun startCore() {
        if (coreStarted) {
            lastStatus = "核心已在运行"
            notify(lastStatus)
            return
        }
        coreStarted = true
        Thread {
            try {
                PyRuntime.ensure(applicationContext)
                val out = PyRuntime.module("android_main")
                    .callAttr("start", filesDir.absolutePath)
                    .toString()
                lastStatus = "核心：$out"
                Log.i(TAG, "android_main.start -> $out")
                notify(lastStatus)
            } catch (t: Throwable) {
                lastStatus = "核心启动失败：${t.javaClass.simpleName}: ${t.message}"
                Log.e(TAG, "core start failed", t)
                notify(lastStatus)
            }
        }.start()
    }

    override fun onDestroy() {
        try {
            if (coreStarted) {
                PyRuntime.module("android_main").callAttr("stop")
            }
        } catch (t: Throwable) {
            Log.e(TAG, "核心停止失败", t)
        }
        try {
            if (PyRuntime.isStarted()) {
                PyRuntime.module("probe_main").callAttr("stop")
            }
        } catch (t: Throwable) {
            Log.e(TAG, "Python 停止失败", t)
        }
        threadStarted = false
        lastStatus = "服务已停止"
        stopForeground(STOP_FOREGROUND_REMOVE)
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun notify(text: String) {
        getSystemService(NotificationManager::class.java)
            .notify(NOTIFICATION_ID, buildNotification(text))
    }

    private fun buildNotification(text: String): Notification =
        Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Chaquopy Probe")
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setOngoing(true)
            .build()

    private fun createChannel() {
        val ch = NotificationChannel(
            CHANNEL_ID, "Chaquopy Probe", NotificationManager.IMPORTANCE_LOW)
        ch.description = "Python 探针前台服务"
        getSystemService(NotificationManager::class.java)
            .createNotificationChannel(ch)
    }

    companion object {
        private const val TAG = "ChaquopyProbe"
        private const val CHANNEL_ID = "probe"
        private const val NOTIFICATION_ID = 1

        // 主界面每秒轮询这个字段，把真实状态显示在屏幕上
        @Volatile
        var lastStatus: String = "未启动"
    }
}
