plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "local.nagram.chaquopyprobe"
    compileSdk = 35

    defaultConfig {
        applicationId = "local.nagram.chaquopyprobe"
        minSdk = 29
        targetSdk = 35
        versionCode = 16
        versionName = "0.16"

        ndk {
            // 真机 arm64 + 模拟器 x86_64；Chaquopy 要求至少一个 ABI
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    packaging {
        // 与 NagramShell 一致：本机 NDK 缺 llvm-strip，保留 so 调试符号
        jniLibs.keepDebugSymbols += "**/*.so"
    }
}

chaquopy {
    defaultConfig {
        // 如果构建报 “Python 3.12 is not supported”，改成 3.11 再试
        version = "3.12"
        pip {
            // 探针只验证“能不能 import / 能不能起服务”；版本交给 Chaquopy
            // 仓库解析，避免钉死一个没有 Android wheel 的版本。
            install("telethon")
            install("aiohttp")
            install("httpx")
            install("python-socks[asyncio]")
        }
    }
}

// tg_userbot 的唯一真相源是仓库根的 tg_userbot/；每次构建前同步到
// Python 源目录，避免在探针里维护第二份 86 文件副本。
val syncUserbot by tasks.registering(Sync::class) {
    from(rootProject.projectDir.parentFile.parentFile.resolve("tg_userbot"))
    into(layout.projectDirectory.dir("src/main/python/tg_userbot"))
    exclude("**/__pycache__/**", "**/*.pyc", "**/*.pyo")
}
// Chaquopy 里真正消费该目录的是 merge*PythonSources（generate*SourceAssets
// 也一并挂上），Gradle 8.9 会校验隐式依赖。
tasks.matching {
    it.name.endsWith("PythonSources") ||
        it.name.endsWith("PythonSourceAssets")
}.configureEach { dependsOn(syncUserbot) }
