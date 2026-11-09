package local.nagram.chaquopyprobe

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import java.io.File

/** 开机自启：secrets/session 就绪时直接拉起核心前台服务。 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        val files = context.filesDir
        if (!File(files, "tg_secrets.json").exists() ||
            !File(files, "tg_downloader.session").exists()) {
            return
        }
        PublicStorage.persist(context)
        val svc = Intent(context, ProbeService::class.java).setAction("core")
        if (Build.VERSION.SDK_INT >= 26) {
            context.startForegroundService(svc)
        } else {
            context.startService(svc)
        }
    }
}
