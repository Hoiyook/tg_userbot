plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "local.nagram.shell"
    compileSdk = 35

    defaultConfig {
        applicationId = "local.nagram.shell"
        minSdk = 29
        targetSdk = 35
        versionCode = 4
        versionName = "1.4"
    }

    // 本机 NDK 27.0.12077973 缺 toolchains/llvm/prebuilt（strip 失败）；
    // 原型无 native 代码，保留 so 调试符号即可完全跳过 llvm-strip。
    packaging {
        jniLibs.keepDebugSymbols += "**/*.so"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)
    implementation(libs.accompanist.swiperefresh)
}
