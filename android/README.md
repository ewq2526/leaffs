# LeafFS 安卓版

把 LeafFS 服务器跑在安卓手机上：App 启动后，同一 WiFi / 热点下的设备用浏览器访问手机地址，
即可浏览、下载文件（与桌面版**同一套网页与账号体系**）。

## 技术结构

- Chaquopy 把 Python 3.12 与 LeafFS 源码嵌入 APK（纯 Python 依赖 websockets）。
- **Python 源码的单一事实来源是仓库根 `leaffs/`**：构建时由 Gradle 同步任务拷入
  `app/build/generated/python/`（排除桌面 exe/dll），桌面代码零改动、**不维护两份拷贝**。
- 安卓入口 `app/src/main/python/leaffs_mobile.py`：编排 HTTP + WS + 管理推送 + 看门狗
  （**不复用**桌面 `leaffs.app.start_server`，避开 webview / aria2c / 证书页等桌面流程）。
- 路径复用现有机制：资源 = 随包解包的 leaffs 包目录（`paths.BASE_DIR` 自动解析）；
  可写数据 = `$HOME/leaffs_home`（`HOME` = App 私有 files 目录，经 `LEAFFS_PROJECT_ROOT` 注入）。
- Kotlin 侧：`MainActivity.kt`（容器与证书放行）、`LeafService.kt`（前台服务）、
  `LfDialog.kt`（原生卡片）、`Thumbnailer.kt`（缩略图原生后端）。

## 版本配套

Gradle 9.5.0 + AGP 9.3.0（AGP 9 内置 Kotlin，无需 KGP 插件）+ Chaquopy 17.0.0（Python 3.12）。
minSdk 26 / targetSdk 37。

## 常见问题

- **AGP 9.3.0 与 Chaquopy 17 的兼容性**：Chaquopy 17 官方只测到 AGP 9.2.x，9.3 通常可用。
  若报不兼容，把 `gradle/libs.versions.toml` 里的 `agp` 降到 9.2.x。
- **configuration cache 报错**：工程按标准模板开了 `org.gradle.configuration-cache=true`；
  若与 Chaquopy 冲突，临时改成 false 再试。
- **Chaquopy 报 buildPython 错误**：它要求构建机 Python 与 App 内同为 3.12。
- **换端口**：默认 http 8080 / ws 8081。启动后 App 私有目录
  `files/leaffs_home/config/server_config.json` 可改（与桌面版同格式）。
- **卸载重装会清空数据**：账号与共享文件都在 App 私有目录，卸载即删。

## 目录

```
android/
  settings.gradle.kts / build.gradle.kts / gradle.properties
  gradlew(.bat) + gradle/wrapper/       # 工程自带 wrapper
  gradle/libs.versions.toml             # 版本目录（AGP / Chaquopy）
  local.properties                      # sdk.dir —— 本机文件，不入库
  app/build.gradle.kts                  # Chaquopy 配置 + leaffs 源码同步任务
  app/src/main/AndroidManifest.xml
  app/src/main/python/leaffs_mobile.py  # Python 服务器编排入口
  app/src/main/java/com/leaffs/mobile/  # Kotlin：MainActivity / LeafService / LfDialog / Thumbnailer
```
