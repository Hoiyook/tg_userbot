package local.nagram.chaquopyprobe

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.MediaScannerConnection
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.Settings
import java.io.File

/**
 * 公共 Downloads 存储（/sdcard/Download/Nagram）。
 *
 * - Android 11+ 用 MANAGE_EXTERNAL_STORAGE（侧载个人 App，不走 Play 政策）；
 * - Android 10 用 WRITE_EXTERNAL_STORAGE + requestLegacyExternalStorage；
 * - 授权后写 `files/public_download_dir.txt`，Python 侧据此设置
 *   TG_DOWNLOAD_DIR（见 android_paths.download_dir）。
 */
object PublicStorage {

    @Volatile
    private var migrating = false

    private val MEDIA_EXT = setOf(
        "mp4", "mkv", "mov", "avi", "wmv", "flv", "webm", "ts", "m4v",
        "mpg", "mpeg", "3gp", "jpg", "jpeg", "png", "gif", "webp",
        "heic", "bmp", "tiff", "mp3", "m4a", "ogg", "opus", "flac",
        "wav", "aac")

    fun targetDir(context: Context): File =
        File(Environment.getExternalStorageDirectory(), "Download/Nagram")

    fun hasAccess(context: Context): Boolean =
        if (Build.VERSION.SDK_INT >= 30) {
            Environment.isExternalStorageManager()
        } else {
            context.checkSelfPermission(
                Manifest.permission.WRITE_EXTERNAL_STORAGE) ==
                PackageManager.PERMISSION_GRANTED
        }

    fun request(activity: Activity) {
        if (Build.VERSION.SDK_INT >= 30) {
            val intent = Intent(
                Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                Uri.parse("package:" + activity.packageName))
            try {
                activity.startActivity(intent)
            } catch (_: Exception) {
                activity.startActivity(Intent(
                    Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION))
            }
        } else {
            @Suppress("DEPRECATION")
            activity.requestPermissions(
                arrayOf(Manifest.permission.WRITE_EXTERNAL_STORAGE), 2002)
        }
    }

    /** 把当前授权状态落到 files/public_download_dir.txt（Python 侧读取）。 */
    fun persist(context: Context) {
        val flag = File(context.filesDir, "public_download_dir.txt")
        try {
            if (hasAccess(context)) {
                targetDir(context).mkdirs()
                flag.writeText(targetDir(context).absolutePath)
                ensureMigrated(context)
            } else if (flag.exists()) {
                flag.delete()
            }
        } catch (_: Throwable) {
        }
    }

    /** 后台把旧 App 私有媒体搬到公共 Downloads（授权后首次/每次启动兜底）。 */
    fun ensureMigrated(context: Context) {
        if (!hasAccess(context) || migrating) return
        val src = File(context.filesDir, "downloads")
        if (!src.isDirectory || src.listFiles()?.isEmpty() != false) return
        migrating = true
        Thread {
            try {
                migrateAppPrivate(context)
                scanMedia(context)
            } catch (_: Throwable) {
            } finally {
                migrating = false
            }
        }.start()
    }

    private fun migrateAppPrivate(context: Context): Int {
        val src = File(context.filesDir, "downloads")
        if (!src.isDirectory) return 0
        val dstRoot = targetDir(context)
        if (!dstRoot.exists() && !dstRoot.mkdirs()) return 0
        var moved = 0
        src.walkTopDown().filter { it.isFile }.forEach { file ->
            val rel = file.toRelativeString(src)
            val dst = File(dstRoot, rel)
            dst.parentFile?.mkdirs()
            var candidate = dst
            var i = 1
            while (candidate.exists()) {
                val ext = dst.extension
                val suffix = if (ext.isNotEmpty()) ".$ext" else ""
                candidate = File(dst.parentFile,
                    "${dst.nameWithoutExtension} ($i)$suffix")
                i++
            }
            try {
                file.copyTo(candidate, overwrite = false)
                if (candidate.length() == file.length()) {
                    file.delete()
                    moved++
                }
            } catch (_: Throwable) {
            }
        }
        // 清掉空的旧目录
        src.walkBottomUp()
            .filter { it.isDirectory && it.listFiles()?.isEmpty() == true }
            .forEach { it.delete() }
        return moved
    }

    /** 扫描公共目录下的媒体文件，触发系统相册索引。返回扫描条数。 */
    fun scanMedia(context: Context): Int {
        if (!hasAccess(context)) return 0
        val root = targetDir(context)
        if (!root.exists()) return 0
        val files = root.walkTopDown()
            .filter { it.isFile && it.extension.lowercase() in MEDIA_EXT }
            .map { it.absolutePath }
            .toList()
        if (files.isEmpty()) return 0
        MediaScannerConnection.scanFile(
            context, files.toTypedArray(), null, null)
        return files.size
    }
}
