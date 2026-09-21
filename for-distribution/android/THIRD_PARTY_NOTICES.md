# LeafFS 安卓版第三方组件与许可声明（Third-Party Notices — Android）

本文档列出 **LeafFS 安卓版 APK** 内包含的第三方组件。除另行注明外，本项目对这些组件**均原样使用、未做修改**。
分发 APK 时请连同本文件与同目录的 `licenses/` 一并提供；各组件完整许可文本以其上游官方发布为准。

> 桌面版（Windows）的组件清单与本文件**不重叠**，见 `../THIRD_PARTY_NOTICES.md`。

## AI 编程辅助声明

本项目为**高度 AI 辅助开发的项目**：开发过程中大量代码由 AI 生成或经 AI 辅助完成，主要使用的模型为 DeepSeek V4 Flash，人工负责需求定义、代码审阅与集成。AI 生成或协助编写的代码可能存在错误或安全疏漏，使用前请自行审阅与测试。

## 组件清单

### 应用与构建链

| 组件 | 版本 | 用途（本项目内） | 许可证 | 上游/官方源码 |
|---|---|---|---|---|
| GeckoView | 146.0.20251217121356 | 网页内核：显示 LeafFS 页面 | **MPL-2.0** | github.com/mozilla/geckoview |
| Chaquopy | 17.0.0 | 在安卓上运行 Python（构建插件与运行时） | MIT | github.com/chaquo/chaquopy |
| androidx 家族（activity 1.9.3，以及 annotation、arch.core、core、customview、fragment、interpolator、lifecycle、loader、profileinstaller、savedstate、startup、tracing、versionedparcelable、viewpager 共 23 个模块） | 随 androidx.activity 传递引入 | Activity 基类、返回手势、生命周期、状态保存等 | Apache-2.0 | developer.android.com/jetpack/androidx |
| Kotlin 标准库 | 随 GeckoView 传递引入 | Kotlin 运行时 | Apache-2.0 | github.com/JetBrains/kotlin |
| kotlinx.coroutines（core / android） | 随 GeckoView 传递引入 | Kotlin 协程运行时 | Apache-2.0 | github.com/Kotlin/kotlinx.coroutines |
| Google Play services（base / basement / fido / tasks） | 随 GeckoView 传递引入 | GeckoView 的 WebAuthn（通行密钥）等能力 | Android Software Development Kit License（Google 专有） | developers.google.com/android/guides/setup |

### Python 运行时（由 Chaquopy 打包进 APK）

| 组件 | 版本 | 用途（本项目内） | 许可证 | 上游/官方源码 |
|---|---|---|---|---|
| Python | 3.12 | 服务端程序主体 | PSF-2.0 | python.org |
| websockets | 16.0 | WebSocket 服务 | BSD-3-Clause | github.com/aaugustin/websockets |
| httpx | 0.28.1 | 下载器的 HTTP 客户端 | BSD-3-Clause | github.com/encode/httpx |
| httpcore | 1.0.9 | httpx 的底层传输 | BSD-3-Clause | github.com/encode/httpcore |
| h11 | 0.16.0 | httpcore 的 HTTP/1.1 实现 | MIT | github.com/python-hyper/h11 |
| anyio | 4.15.1 | httpx 的异步后端 | MIT | github.com/agronholm/anyio |
| certifi | 2026.7.22 | TLS 根证书包 | **MPL-2.0** | github.com/certifi/python-certifi |
| cryptography | 42.0.8 | 生成自签服务器证书（安卓上没有 openssl） | Apache-2.0 或 BSD-3-Clause（双许可，任选其一） | github.com/pyca/cryptography |
| cffi | 1.17.1 | cryptography 的 C 绑定 | MIT | github.com/python-cffi/cffi |
| pycparser | 3.0 | cffi 的 C 声明解析 | BSD-3-Clause | github.com/eliben/pycparser |
| typing_extensions | 4.16.0 | 类型标注向后兼容 | PSF-2.0 | github.com/python/typing_extensions |
| chaquopy_libffi（libffi） | 3.3 | cffi 的底层原生库 | MIT（另含 BUILDTOOLS 组件） | github.com/libffi/libffi ／ Chaquopy 构建 |
| pyelftools | 0.26 | Chaquopy 引导期解析 ELF | Unlicense（公有领域） | github.com/eliben/pyelftools |
| setuptools | 68.2.2 | Chaquopy 引导期 | MIT | github.com/pypa/setuptools |

> 版本号取自 APK 构建产物内的实际内容（Chaquopy 的 `requirements-common.imy` / `bootstrap.imy` 中各组件的 `dist-info`），**与随包内容一致**，不是按依赖声明推测的。

## 本项目目前的义务与做法

- **许可原文**：上述各组件的许可证原文已随本文件提供在 `licenses/` 目录，并在 APK 内以 `assets/licenses/` 提供。
- **MPL-2.0 组件（GeckoView、certifi）**：按 MPL-2.0 第 3.2 条，已在本文档给出各组件**官方源码获取地址**（见上表「上游/官方源码」列）；本项目未修改这些组件，因此不产生额外的源码公开义务。
- **无 GPL/LGPL 组件**：安卓版**不包含** aria2c 与 ffmpeg（这两个只在桌面版随附），因此不存在 GPL 系列「必须提供对应源码」的义务。
- **修改义务**：本项目未修改任何上述组件。若日后对任一组件做了修改，则按该组件许可证的要求提供对应源码或书面获取要约。
- **Python 依赖的来源**：由 Chaquopy 从其官方镜像安装对应 wheel，本项目未对其做任何改动。

## 2026-09-21 核对记录

本声明为本轮新增 —— 此前安卓版**没有任何第三方声明**，桌面那份 `THIRD_PARTY_NOTICES.md` 里的组件与实际打包内容几乎不重叠，套用不了。

核对方式：直接读取 APK 构建产物内的 `chaquopy/requirements-common.imy` 与 `chaquopy/bootstrap.imy`（二者均为 zip 容器，各组件自带 `dist-info` 与许可原文），逐项提取组件名、版本与许可文本 —— 两者的字节数与 APK 内的同名文件**逐字节一致**（2851386 / 599781），确认清单来源即随包内容；应用层与传递依赖读 APK 的 `META-INF/*.version` 标记文件与顶层库标记（`play-services-*.properties`、`kotlin/`、`kotlinx_coroutines_*.version`）；GeckoView 与 Chaquopy 的许可类型另经上游官方渠道确认（Chaquopy 官方公告与 GitHub API 均标注 MIT）。

> 以上按本项目当前实际情况整理；最终以各组件官方许可文本为准。

**兜底说明**：本声明力求覆盖 APK 内包含的全部第三方组件，但可能仍有遗漏或表述不准确。若发现未列明的第三方组件、许可疏漏或其它疑问，请联系项目作者（ewq2526@163.com）反馈，以便及时核对与补正。
