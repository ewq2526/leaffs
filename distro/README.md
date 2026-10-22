# distro —— 这台机器的系统配置

目录按真实路径组织：`etc/`、`usr/` 对应根文件系统，`boot/grub.cfg` 对应 ESP 上的 `EFI/BOOT/grub.cfg`，`kernel/config-7.3.0` 是内核配置。

## 目录

| 路径 | 内容 |
|---|---|
| `kernel/config-7.3.0` | 7.3.0 内核配置（SMP，i915 走模块，文件系统与 i2c-hid 内建） |
| `boot/grub.cfg` | GRUB 菜单：Debian 签名内核、当前内核、上一版、内置盘上的 Windows 与 Debian |
| `etc/systemd/system/` | `leafos-kiosk`、`leafos-ssh`、`leafos-wifi`、`leafos-trust-cert` 及 drop-in |
| `etc/systemd/network/` | `20-wired.network`、`30-wlan.network`（DHCP） |
| `etc/systemd/system.conf.d/leafos.conf` | 全局停止超时 15s |
| `etc/systemd/logind.conf.d/90-leafos-lid.conf` | 盒盖不挂起 —— 根在 USB 盘上，S3 会断电再重新枚举，恢复不及时就是根变只读 |
| `etc/systemd/system/leafos-units.list` | 该启用的单元清单（对账用） |
| `etc/systemd/system/*.service.d/instant.conf` | `modprobe@configfs/drm/fuse/ksmbd` 改成同步装载 |
| `etc/fstab` | 只有根分区；ESP 不常挂 |
| `etc/X11/xorg.conf.d/40-touchpad.conf` | clickpad 打开轻触与拖动 |
| `etc/sysctl.d/50-leafos-quiet.conf` | 降低内核打印噪音 |
| `etc/ksmbd/ksmbd.conf` | SMB 共享定义（`public` 免密、`users` 需账号） |
| `etc/initramfs-tools/` | Debian 内核的 initramfs 模块清单（`uas` 必须在内，这块盘走 UAS） |
| `etc/modprobe.d/intel-microcode-blacklist.conf` | 微码不随内核加载 |
| `usr/local/bin/` | kiosk 会话脚本、证书同步、网络诊断、最终自检、插盘自动挂载 |
| `etc/udev/rules.d/90-leafos-automount.rules` | 分区出现即挂到 `/media/<卷标>`，拔盘由 systemd 自动卸载 |
| `etc/fonts/local.conf` | 打开 RGB 次像素渲染、中文优先 Noto Sans CJK SC（依赖系统装 `fonts-noto-cjk`）|

## 不含的东西（有意）

- `etc/ksmbd/ksmbdpwd.db`、`ksmbd.subauth`：账号口令库
- `/etc/wpa_supplicant/wpa_supplicant-wlan0.conf`：含 WiFi 预共享密钥
- 任何私钥、证书

这些在机器上就地维护，不进本目录。

## 内核构建

不要在目标机上编。构建机上：

```sh
make olddefconfig
make -j16 bzImage
make -j16 modules
```

`CONFIG_EXTRA_FIRMWARE_DIR` 指向的目录必须备好，否则报 empty firmware：需要 `rtw88/rtw8821c_fw.bin`、`regulatory.db`、`regulatory.db.p7s`（目标机上位于 `/lib/firmware`）。

## 内核安装

```sh
cp /boot/vmlinuz-7.3.0-leafos /boot/vmlinuz-7.3.0-leafos.prev    # GRUB 的 previous 项指向它
install -m 644 bzImage /boot/vmlinuz-7.3.0-leafos
make modules_install INSTALL_MOD_PATH=/tmp/stage
rsync -a --delete /tmp/stage/lib/modules/7.3.0/ /lib/modules/7.3.0/
depmod -a 7.3.0
```

内核与模块必须来自同一次构建：版本串相同，但配置变化会影响模块 ABI。

## 改内核配置的两个坑

`MISC_FILESYSTEMS` 这类 `menuconfig ... default y` 在 tinyconfig 下是关的，它后面的整块 `if` 全部不可见，`scripts/config -e` 写进去会被 `make olddefconfig` 静默丢掉。同理 `X86_INTEL_LPSS` 不打开时，`MFD_INTEL_LPSS_*`、`I2C_DESIGNWARE_*` 都不可见。

**每次改完必须回读**：`grep '^CONFIG_X=' .config`。

## 内核里已启用的关键能力

| 类别 | 配置 |
|---|---|
| 启动链 | USB 根走 `uas`（不是 `usb-storage`）；`root=` 只认 `PARTUUID` |
| 显示 | `DRM_EFIDRM=y`（`FB_EFI` 已去掉，否则帧缓冲被 efifb 抢走）、`DRM_I915=m` |
| 输入 | `HID_MULTITOUCH=y`（Precision Touchpad 类设备）、`I2C_HID_ACPI=y`、`X86_INTEL_LPSS=y`、`PINCTRL_JASPERLAKE=y` |
| 文件系统 | `NTFS3_FS`、`EXFAT_FS`、`XFS_FS`、`BTRFS_FS`、`F2FS_FS`、`HFSPLUS_FS`、`ISO9660_FS`、`UDF_FS`、`SQUASHFS`、`FUSE_FS` |
| 网络 | `RTW88_8821CE`（RTL8821CE），固件编进内核 |
