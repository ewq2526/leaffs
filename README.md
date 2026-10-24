# Camphor

一个精简的 Linux 系统。内核与用户态取自 Debian 13，裁到只留跑一个全屏文件服务所需的部分；
自带 `leaffs`（文件浏览、共享、下载、设备面板），也对外提供 Web、SMB、SSH。

装在哪台机器都行，只要能跑 x86_64 内核、挂一块盘、接一块屏。

## 安装

把发布镜像写到 U 盘，从 U 盘启动，跟着屏幕上的三步走：

1. **分区**：选一块盘，调整分区，或让它自己占用剩余空间。已有的 EFI 分区会被复用。
2. **账号**：用户名与口令。
3. **安装**：展开系统、写引导，进度与日志都在页面上。

重启后从硬盘启动，第一次开机自动展开应用层，之后就是一台正常的 Camphor。

## 使用

开机进入全屏界面：浏览、上传、下载、分享。同一局域网内的其它设备用浏览器访问服务地址即可，
无需装客户端。

设备面板在 `/system`（超级管理员）：CPU、内存、温度、电池、磁盘容量与读写、服务状态、无线网络。

## 仓库

```
app/                应用层：单元、配置、leaffs 代码与依赖说明
etc/ usr/           系统层的配置与脚本，按真实路径组织
mkimage/            构建：包清单、镜像脚本、引导素材、构建文档
docs/               应用说明（LEAFFS.md）、机制（ARCHITECTURE.md）、译法表
for-distribution/   分发与第三方许可证
tests/              测试
```

介质上的东西分三层：系统骨架、应用镜像、安装依赖。
分层与两阶段安装的细节见 `mkimage/BUILD.md`。

## 文档

- 怎么构建镜像、每一步做什么、怎么验：`mkimage/BUILD.md`
- 安装器：启动链、网页后端、两阶段脚本、排查：`mkimage/INSTALLER.md`
- 踩过的坑：`mkimage/PITFALLS.md`
- 应用能做什么：`docs/LEAFFS.md`
- 服务端怎么运作：`docs/ARCHITECTURE.md`
- 版本变更：`CHANGELOG.md`

## 许可

见 `for-distribution/THIRD_PARTY_NOTICES.md`。字体与 ffmpeg 是 GPL/OFL 组件，分发时随附原文。

本项目高度依赖 AI 辅助开发（主要模型 DeepSeek V4 Flash）。
