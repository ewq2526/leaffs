# 自编内核 7.3.0（已归档）

**状态：停用。** 系统现在用 Debian 13 的签名内核 `6.12.113+deb13-amd64`；这套自编内核不再参与启动，小盘上的 `vmlinuz-7.3.0-leafos`、`.prev` 与 `/lib/modules/7.3.0` 已移除，换机型或要恢复启动时按下面的步骤重建即可。

停用的原因是**签名**：这台机器的固件信任微软签名的 shim，再由 shim 验发行版签名的内核。自编内核拿不到这套签名密钥，进不了 Secure Boot 信任链——要用它就只能一直关着 Secure Boot。发行版内核自带签名，配合 shim 与 Debian 签名的 GRUB 可以正常开启。

附带的好处是硬件支持跟着发行版走，加设备不必改配置重编；但那是副产品，不是换内核的理由。

同目录的 `config-7.3.0` 是完整配置；`kernel-7.3.0-*.tar.gz` 三个源码包留在构建机的 `E:\os\.build`，不在仓库里。内核二进制已删除，重建即可（配置一致时产物一致）。

## 重建

在构建机的 WSL 里跑，不要在目标机上编：

```sh
make olddefconfig
make -j16 bzImage
make -j16 modules
```

`CONFIG_EXTRA_FIRMWARE_DIR` 指向的目录必须备好，否则报 empty firmware：需要 `rtw88/rtw8821c_fw.bin`、`regulatory.db`、`regulatory.db.p7s`（目标机上位于 `/lib/firmware`）。

## 安装

```sh
cp /boot/vmlinuz-7.3.0-leafos /boot/vmlinuz-7.3.0-leafos.prev
install -m 644 bzImage /boot/vmlinuz-7.3.0-leafos
make modules_install INSTALL_MOD_PATH=/tmp/stage
rsync -a --delete /tmp/stage/lib/modules/7.3.0/ /lib/modules/7.3.0/
depmod -a 7.3.0
```

内核与模块必须来自同一次构建：版本串相同，但配置变化会影响模块 ABI。

GRUB 里对应的两项（主盘 `grub.cfg` 用 `PARTUUID=1c5c1d24-9e66-46a1-85bc-81d9641de983`，小盘用 `0cec4cce-926b-4b9f-b346-f6ed4f64bf72`）：

```
menuentry 'LeafOS 7.3 (current)' --id leafos-current {
    linux /boot/vmlinuz-7.3.0-leafos root=PARTUUID=<根分区> rw rootwait net.ifnames=0 console=tty0 console=ttyS0 panic=10 loglevel=7
}

menuentry 'LeafOS 7.3 (previous - fallback)' --id leafos-prev {
    linux /boot/vmlinuz-7.3.0-leafos.prev root=PARTUUID=<根分区> rw rootwait net.ifnames=0 console=tty0 console=ttyS0 panic=10 loglevel=4
}
```

## 改配置的两个坑

`MISC_FILESYSTEMS` 这类 `menuconfig ... default y` 在 tinyconfig 下是关的，它后面的整块 `if` 全部不可见，`scripts/config -e` 写进去会被 `make olddefconfig` 静默丢掉。同理 `X86_INTEL_LPSS` 不打开时，`MFD_INTEL_LPSS_*`、`I2C_DESIGNWARE_*` 都不可见。

**每次改完必须回读**：`grep '^CONFIG_X=' .config`。

## 当年启用的关键能力

| 类别 | 配置 |
|---|---|
| 启动链 | USB 根走 `uas`（不是 `usb-storage`）；`root=` 只认 `PARTUUID` |
| 显示 | `DRM_EFIDRM=y`（`FB_EFI` 已去掉，否则帧缓冲被 efifb 抢走）、`DRM_I915=m` |
| 输入 | `HID_MULTITOUCH=y`（Precision Touchpad 类设备）、`I2C_HID_ACPI=y`、`X86_INTEL_LPSS=y`、`PINCTRL_JASPERLAKE=y` |
| 文件系统 | `NTFS3_FS`、`EXFAT_FS`、`XFS_FS`、`BTRFS_FS`、`F2FS_FS`、`HFSPLUS_FS`、`ISO9660_FS`、`UDF_FS`、`SQUASHFS`、`FUSE_FS` |
| 网络 | `RTW88_8821CE`（RTL8821CE），固件编进内核 |

缺口（当时未补，恢复启动时要注意）：没有 `BLK_DEV_LOOP`、`OVERLAY_FS`、`SENSORS_CORETEMP`，所以挂不了 ISO/镜像文件，也读不到 CPU 核心温度。
