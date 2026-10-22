# distro —— 这台机器的系统配置

目录按真实路径组织：`etc/`、`usr/` 对应根文件系统，`boot/grub.cfg` 对应 ESP 上的 `EFI/BOOT/grub.cfg`。

## 目录

| 路径 | 内容 |
|---|---|
| `boot/grub.cfg` | GRUB 菜单：Debian 签名内核，以及内置盘上的 Windows 与 Debian |
| `archive/kernel-7.3/` | 已停用的自编 7.3 内核：完整配置、重建与安装步骤、当年的能力清单与缺口 |
| `etc/systemd/system/` | `leafos-kiosk`、`leafos-ssh`、`leafos-wifi`、`leafos-trust-cert` 及 drop-in |
| `etc/systemd/network/` | `20-wired.network`、`30-wlan.network`（DHCP） |
| `etc/systemd/system.conf.d/leafos.conf` | 全局停止超时 15s |
| `etc/systemd/logind.conf.d/90-leafos-lid.conf` | 盒盖不挂起 —— 根在 USB 盘上，S3 会断电再重新枚举，恢复不及时就是根变只读 |
| `etc/modules-load.d/leafos-zram.conf` | 开机加载 `zram` |
| `etc/modules-load.d/leafos-netfilter.conf` | 开机加载 nf_tables 与 ip_tables 两套的常用模块（其余由内核 alias 自动装载） |
| `etc/systemd/system/leafos-zram-swap.service` + `usr/local/bin/leafos-zram-swap.sh` | 在 zram 上开内存 1/4 的压缩交换区（zstd）。这台设备没有物理 swap，`zswap` 内建但这里不启用 —— 没有磁盘 swap 时它无事可做，两套叠起来只是双重压缩 |
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

## 依赖的系统包

镜像里额外装了这几个包，不是 Debian 基础系统自带：

| 包 | 用途 | 备注 |
|---|---|---|
| `fonts-noto-cjk` | 界面正文与中文字形 | SIL OFL 1.1，分发须随附许可证（见 `for-distribution/licenses/`）|
| `ffmpeg` | 视频缩略图与时长（带 `ffprobe`） | GPL-2.0-or-later，构建启用 `--enable-gpl`，同样要随附许可证 |

`leafos-kiosk` 启动日志会各打一行 `ffmpeg: 已安装 / 未安装`、`aria2c: 已安装 / 未安装`，缺了直接看那里。

## 不含的东西（有意）

- `etc/ksmbd/ksmbdpwd.db`、`ksmbd.subauth`：账号口令库
- `/etc/wpa_supplicant/wpa_supplicant-wlan0.conf`：含 WiFi 预共享密钥
- 任何私钥、证书

这些在机器上就地维护，不进本目录。


## 内核

**用 Debian 13 的签名内核 6.12.113**（`linux-image-6.12.113+deb13-amd64`）：签名链可用、硬件支持完整、升级跟着发行版走。

模块树按需维持：`/lib/modules/6.12.113+deb13-amd64` 现有 **850 个（33 MB）**。基础是这台机器实际用得上的那几个，另加了一批通用驱动 —— 换硬件或插外设时不用重编内核：

- 存储与文件系统：ahci、nvme、uas、usb-storage、loop、overlay、ntfs3/xfs/btrfs/f2fs/hfsplus/isofs/udf/squashfs/vfat/exfat
- 输入与平台：i2c-hid-acpi、hid-multitouch、intel-lpss、pinctrl-jasperlake
- 显示：i915 + drm 全家
- 内存压缩：`zram`（配 `leafos-zram-swap.service`）
- 网络过滤：`nf_tables`、`nf_conntrack`、`nft_ct`、`nft_nat`、`ip_tables`、`iptable_filter`、`iptable_nat`、`ip6table_filter`、`x_tables`
- 有线网卡：整套 `drivers/net/ethernet`（115 个，含 `r8169`、`e1000e`、`igb/igc`、`ixgbe`、`tg3`）与 `drivers/net/phy`
- 无线网卡：整套 `drivers/net/wireless`（149 个，含 `iwlwifi`、`ath10k/ath11k`、`mt76`、`rtw88/rtw89`、`rtl8xxxu`、`brcmfmac`）
- 蓝牙：`drivers/bluetooth` + `net/bluetooth`（含 `btusb`）
- 其它外设总线：`drivers/net/{usb,can,wan,wwan}`

无线与部分有线网卡还需要固件：`firmware-{iwlwifi,realtek,atheros,brcm80211,misc-nonfree}`，`/lib/firmware` 约 396 MB（`nvidia/` 那 64 MB 已删 —— 内核里没有 `nouveau`/`amdgpu`，那些固件没有代码会用）。

**`uas` 必须进 initramfs** —— 系统盘走 UAS，缺了根就挂不上。initramfs 用 `MODULES=list`，清单在 `etc/initramfs-tools/modules`，实际打进去 19 个模块、12.5 MB。不要装 `plymouth`：它会把 20 MB 的 CJK 字体和整套 pango 拖进 initrd，体积直接翻倍。

自编的 7.3 内核已停用：内核与模块从系统上移除，GRUB 菜单只剩 Debian 这一项。**原因是签名** —— 固件信任微软签名的 shim，再由 shim 验发行版签名的内核，自编内核拿不到这套密钥，要用它就得一直关着 Secure Boot。**配置、重建步骤与当年的能力清单归档在 `archive/kernel-7.3/`，不是删掉了。**
