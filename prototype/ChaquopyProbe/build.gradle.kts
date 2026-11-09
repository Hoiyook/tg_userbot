plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    // 与 NagramShell 同 AGP/Kotlin/Gradle 版本；Chaquopy 15.0.1 支持 Python 3.8–3.12
    id("com.chaquo.python") version "15.0.1" apply false
}
