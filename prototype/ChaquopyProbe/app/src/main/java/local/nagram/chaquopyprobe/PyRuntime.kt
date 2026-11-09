package local.nagram.chaquopyprobe

import android.content.Context
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

/**
 * 进程级 Python 运行时：登录页 / ProbeService 共用。
 *
 * Chaquopy 的 Python.start() 每个进程只能调一次；之前 ProbeService 自己
 * 维护 pyStarted，登录页再调一次会抛 “Python already started”。这里统一。
 */
object PyRuntime {
    @Volatile
    private var started = false

    @Synchronized
    fun ensure(context: Context) {
        if (!started) {
            Python.start(AndroidPlatform(context.applicationContext))
            started = true
        }
    }

    fun isStarted(): Boolean = started

    fun module(name: String): PyObject = Python.getInstance().getModule(name)
}
