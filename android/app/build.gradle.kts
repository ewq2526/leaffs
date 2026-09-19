import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.chaquopy)
}

// release 签名：口令放在 android/keystore.properties（已被 .gitignore 挡住，绝不入库）。
// ⚠️ 刻意做成"没有密钥也能构建" —— 别人 clone 或 CI 上只是产出未签名包，
//    不会在配置阶段直接报错卡住。
val keystorePropsFile = rootProject.file("keystore.properties")
val keystoreProps = Properties().apply {
    if (keystorePropsFile.exists()) keystorePropsFile.inputStream().use { load(it) }
}
val hasReleaseKey = keystoreProps.getProperty("storeFile")
    ?.let { rootProject.file(it).isFile } == true

// LeafFS Python 源码单一事实来源：仓库根 leaffs/ 包目录。
// 构建前同步（排除桌面二进制 exe/dll），生成干净源码树供 Chaquopy 打包。
val syncLeaffs by tasks.registering(Sync::class) {
    from(rootProject.file("../leaffs")) {
        exclude(
            "**/*.exe",
            "**/*.dll",
            "**/*.pyc",
            "**/__pycache__/**",
        )
    }
    into(layout.buildDirectory.dir("generated/python/leaffs"))
}

android {
    namespace = "com.leaffs.mobile"

    compileSdk {
        version = release(37)
    }

    defaultConfig {
        applicationId = "com.leaffs.mobile"
        minSdk = 26
        targetSdk = 37
        // 版本号：安卓版与服务端**共用同一份代码、同一套版本**，所以跟着主项目的版本走。
        // 规则＝主×10000 + 次×100 + 修订（1.0.6 ⇒ 10006）—— 由 versionName 映射而来，
        // 这样每版自己算得出，不靠"记住上次发到几"。
        // ⚠️ versionCode 只增不减（Android 靠它判断是不是升级），改之前先确认不小于已发布的那个。
        versionCode = 10006
        versionName = "1.0.6"
    }

    // 按 ABI 分开出包：每个包只含单一架构的原生库（GeckoView 内核 + Python 运行时），
    // 不再产出含两个架构的巨型通用包。
    // 为什么不用 splits.abi / app bundle：Chaquopy 官方 FAQ 明确说这两个特性对它「帮助不大」，
    // 且 AGP 不允许 ndk.abiFilters 与 splits 同时设置；Chaquopy 又必须靠 abiFilters 指定 ABI，
    // 因此按官方推荐改用 flavor 维度，每个 flavor 只保留一个 ABI。
    //（Python 3.12 起无 32 位运行时，故不提供 armeabi-v7a / x86）
    flavorDimensions += "abi"
    productFlavors {
        create("arm64") {
            dimension = "abi"
            ndk { abiFilters += "arm64-v8a" }   // 真机
        }
        create("x86") {
            dimension = "abi"
            ndk { abiFilters += "x86_64" }      // 模拟器
        }
    }

    // release 签名：口令来自 android/keystore.properties（文件不在就跳过，见文件顶部说明）。
    // 没有签名配置时 release 产出的是**未签名**包，装不上 —— 要装就必须有这一节。
    if (hasReleaseKey) {
        signingConfigs {
            create("release") {
                storeFile = rootProject.file(keystoreProps.getProperty("storeFile"))
                storePassword = keystoreProps.getProperty("storePassword")
                keyAlias = keystoreProps.getProperty("keyAlias")
                keyPassword = keystoreProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            optimization {
                enable = false
            }
            if (hasReleaseKey) signingConfig = signingConfigs.getByName("release")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }
}

chaquopy {
    defaultConfig {
        version = "3.12"
        pyc {
            // 开发期不预编译，保留源码便于看栈与日志；发布前可改回 true
            src = false
        }
        pip {
            // 与桌面版同版本（licenses/LICENSE.websockets-16.0-BSD-3.txt），纯 Python 实现
            install("websockets==16.0")
            // dl 模块（tracker/m3u8 探测）依赖；纯 Python，Chaquopy 镜像有 wheel
            install("httpx")
            // 安卓没有 openssl.exe：TLS 自签证书由它生成（leaffs/server/tls.py）。
            // 带原生扩展，Chaquopy 官方镜像提供对应 wheel
            install("cryptography")
        }
    }
    sourceSets {
        getByName("main") {
            // 默认 src/main/python（安卓入口 leaffs_mobile.py）+ 同步生成的 leaffs 源码树
            srcDir(layout.buildDirectory.dir("generated/python"))
        }
    }
}

tasks.named("preBuild") {
    dependsOn(syncLeaffs)
}

dependencies {
    // GeckoView 真内核：火狐系内核，网页能力与电脑浏览器一致
    // 146 传递 kotlin-stdlib 2.2.x，与 AGP9 内置 Kotlin 编译器兼容（155 需要 stdlib 2.4，超出版本上限）
    implementation("org.mozilla.geckoview:geckoview:146.0.20251217121356")
    // 仅用 ComponentActivity + OnBackPressedDispatcher：API 33 起 onBackPressed 对返回手势失效
    implementation("androidx.activity:activity:1.9.3")
}

// Chaquopy 的 merge*PythonSources 任务消费 syncLeaffs 生成的源码树，
// 需显式声明依赖（Gradle 9 任务依赖校验）
tasks.configureEach {
    if (name.startsWith("merge") && name.endsWith("PythonSources")) {
        dependsOn(syncLeaffs)
    }
}
