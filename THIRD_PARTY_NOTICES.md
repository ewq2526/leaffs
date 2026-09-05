# LeafFS 第三方组件与许可声明（Third-Party Notices）

本文档列出 LeafFS 源码树内随附、或运行时依赖的第三方组件。除另行注明外，本项目对这些组件**均原样使用、未做修改**。
分发（含把源码目录或打包后的可执行产物提供给他人）时，请连同本文件一并提供；各组件完整许可文本以其上游官方发布为准。

## AI 编程辅助声明

本项目为**高度 AI 辅助开发的项目**：开发过程中大量代码由 AI 生成或经 AI 辅助完成，主要使用的模型为 DeepSeek V4 Flash，人工负责需求定义、代码审阅与集成。AI 辅助生成的内容属于本项目源码的一部分，不额外主张独立版权或许可；其不随产物额外分发、无独立再分发义务。AI 生成或协助编写的代码可能存在错误或安全疏漏，使用前请自行审阅与测试。

| 组件（随附位置） | 用途（本项目内） | 许可证 | 上游/官方源码 |
|---|---|---|---|
| Python 标准库（运行时环境） | 程序主体运行 | PSF License | python.org |
| websockets（Python 依赖，16.x） | WebSocket 服务 | BSD-3-Clause | github.com/aaugustin/websockets |
| QRCode.js（`leaffs/web_page/common/qrcode.min.js`） | 页面二维码生成 | MIT | github.com/davidshimjs/qrcodejs |
| openssl（`leaffs/openssl.exe`） | 未配置证书时自动生成自签服务器证书 | Apache-2.0 | github.com/openssl/openssl |
| aria2c（`leaffs/aria2c.exe`） | 下载器：磁力/种子/HTTP 直链任务（aria2 RPC 模式） | GPL-2.0-or-later | github.com/aria2/aria2 |
| ffmpeg（`leaffs/ffmpeg.exe`） | 生成视频/图片缩略图 | LGPL-2.1+（具体构建是否含 GPL 部分以其版本信息为准） | ffmpeg.org |
| pythonnet / pywebview | 本机桌面窗口（可选）：代码在启动桌面窗的入口处动态 `import webview`，其底层依赖 pythonnet | MIT / BSD 系 | 各自官方仓库 |

## 本项目目前的义务与做法

- **许可原文**：各组件许可证原文、版本与官方源码地址清单已随本文件提供在 `licenses/` 目录（含 COMPONENTS.txt），分发时请连同该目录一并提供。
- **源码形态分发**：上述第三方文件以原版形式随源码目录携带；不再单独制作/分发独立打包产物。
- **关于 pythonnet / pywebview**：本项目代码有引用（桌面窗口入口动态导入，未静态链接），并存在于本机运行环境（site-packages）中。**是否随分发物提供它们，取决于发布内容**——若源码包/产物中带上了这些库或其安装文件，就一并附上其许可文本与版权声明；若分发物不含它们（仅含本项目自身文件，库由使用方自行安装），则本项无再分发义务。
- **若日后发布含上述组件的可执行产物**：随产物放入各组件许可证原文与版权声明（建议 `licenses/` 目录），并注明组件版本与官方源码地址；其中 aria2c、ffmpeg、openssl 需在发布说明中给出官方源码获取地址。
- **修改义务**：本项目未修改任何上述组件。若日后对任一组件做了修改，则按该组件许可证（aria2c/ffmpeg 涉及 GPL/LGPL 时尤其严格）提供对应源码或书面获取要约。

> 以上按本项目当前实际情况整理；最终以各组件官方许可文本为准。

**兜底说明**：本声明力求覆盖随附与依赖的全部第三方组件，但可能仍有遗漏或表述不准确。若发现未列明的第三方组件、许可疏漏或其它疑问，请联系项目作者（ewq2526@163.com）反馈，以便及时核对与补正。
