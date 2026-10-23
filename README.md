# Camphor

一个精简的 Linux 系统。内核与用户态取自 Debian 13，裁到只留跑一个全屏文件服务所需的部分；自带 `leaffs`（文件浏览、共享、下载、设备面板），也对外提供 Web、SMB、SSH。

装在哪台机器都行，只要能跑 x86_64 内核、挂一块盘、接一块屏。

## 安装

1. 装 Debian 13 最小系统，根分区 ext4，ESP 512M。
2. 仓库里的 `etc/`、`usr/`、`boot/` 按同样路径拷到根与 ESP（`boot/grub.cfg` → `EFI/BOOT/grub.cfg`）。
3. 装 `fonts-noto-cjk`、`ffmpeg`、`firmware-{iwlwifi,realtek,atheros,brcm80211,misc-nonfree}`。
4. 内核模块按需补（存储、输入、i915、rtw88、zram、netfilter 等）；系统盘走 USB 时，`uas` 必须进 initramfs。
5. 启用 `etc/systemd/system/camphor-units.list` 里列的单元。

## 使用

开机进入全屏界面：浏览、上传、下载、分享。同一局域网内的其它设备用浏览器访问服务地址即可，无需装客户端。

设备面板在 `/system`（超级管理员）：CPU、内存、温度、电池、磁盘容量与读写、服务状态、无线网络。

## 仓库

```
etc/ usr/ boot/     系统的配置，按真实路径组织
leaffs/             自带的应用：文件服务 + 网页 + 设备面板
docs/               应用说明（LEAFFS.md）、机制（ARCHITECTURE.md）、译法表
for-distribution/   分发与第三方许可证
tests/              测试
```

## 文档

- 应用能做什么：`docs/LEAFFS.md`
- 服务端怎么运作：`docs/ARCHITECTURE.md`
- 版本变更：`CHANGELOG.md`

## 许可

见 `for-distribution/THIRD_PARTY_NOTICES.md`。字体与 ffmpeg 是 GPL/OFL 组件，分发时随附原文。

本项目高度依赖 AI 辅助开发（主要模型 DeepSeek V4 Flash）。
