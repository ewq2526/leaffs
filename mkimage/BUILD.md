# 构建发布镜像

产物是一个 `.iso`。用户把它写到 U 盘，从 U 盘启动，装到一块盘上，重启就是一台 Camphor。

这套东西不自造：Debian 的 `live-build` 已经把「引导 + 精简运行环境 + 系统本体」做成了标准产物，
下面每一处配置都能在 `live-manual`、`lb_config(1)`、`live-boot(7)` 里找到出处。

出过的错不写在这份里 —— 见 `PITFALLS.md`，一行一个坑，标明能不能被脚本拦住。

## 一、三层在 live-build 里对应什么

| 层 | 在 live-build 里 | 说明 |
|---|---|---|
| 引导 | 由 `--bootloaders grub-efi` + `--uefi-secure-boot enable` 生成 | 装的是 `shim-signed`、`grub-efi-amd64-signed` 里的签名件，Secure Boot 能过 |
| 内盒 | live 运行环境，`filesystem.squashfs` | 加 `--debian-installer live` 后附带 `live-installer` udeb，负责把系统铺到目标盘 |
| 本体 | 同一个 squashfs | 安装时被复制到目标分区，之后以 ext4 读写运行 |

`lb_config(1)` 关于 `--debian-installer live` 的原话：

> instead of installing the debian system from packages from the medium or the network,
> it installs the live system to the disk.

所以「内盒装系统」这一步不用自己写脚本，选对选项即可。

**关掉的默认项也要写下来**，否则换一台机器重建时又会打开：

| 选项 | 为什么关 |
|---|---|
| `--loadlin false` | `loadlin` 是 DOS 下引导 Linux 的老工具，Debian 13 已无此包；开着构建会停在 `binary_loadlin` 报 `Unable to locate package loadlin` |

## 二、构建机

Debian 13 amd64，root，能连 `deb.debian.org`：

```sh
apt install live-build xorriso squashfs-tools mtools dosfstools zstd
```

`chroot` 与 `binary` 两个阶段要并存，约 8 GB。根分区通常不够宽裕，把构建目录放到
外挂盘上，仓库那份保持干净，出问题整个删掉重来：

```sh
cp -a mkimage/live /media/<挂载点>/build-camphor
```

镜像源由环境变量给，默认是上游。安全更新在镜像站上是另一个路径，两个都要换：

```sh
CAMPHOR_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian/ \
CAMPHOR_MIRROR_SECURITY=https://mirrors.tuna.tsinghua.edu.cn/debian-security/ \
lb config
```

直连上游通常只有几十 KB/s，换成本地镜像能差一个数量级。
装进目标系统的 `sources.list` 仍指上游，不受这两个变量影响。

**起构建前先查三处有没有能复用的包**，有就必须先接进构建，不许「先跑一遍看看行不行」：
仓库外的 deb 备份目录、`config/packages.chroot/`、`cache/`。备份不接进去等于没备份。

## 三、标准流程

只有一个入口，不要手敲步骤：

```sh
sh mkimage/build.sh <仓库> <构建目录> [image|iso]

# 本地包仓库也给了时，脚本会临时挂进 chroot 装包，复制 chroot 前卸掉
CAMPHOR_LOCAL_REPO=/path/to/localrepo \
  sh mkimage/build.sh <仓库> <构建目录> image
```

| 模式 | 什么时候用 | 做什么 |
|---|---|---|
| `image`（默认） | 改过 `etc/`、`usr/`、钩子、preseed、引导素材 | 铺内容 → 重打两个 initrd → 复制 live 母本 → 修母本（主机名、软件源、dpkg 转移、出厂清理）→ 压片 → 镜像检查 |
| `iso` | 只改过 preseed 或 ISO 上的载荷（`/camphor/*`） | 重打 initrd → 压片 → 镜像检查 |

脚本自己会做的检查（不过就退出，不要绕过）：

1. 起手跑 `mkimage/preflight.sh`
2. 收尾跑 `mkimage/postbuild-check.sh`
3. 撤构建期 dpkg 转移并核对 `start-stop-daemon` 是真 ELF、本地转移为 0
4. 复制 chroot 前卸掉本地仓库挂载
5. 打印 ISO 路径与 sha256

它不负责的：`lb config`、首次从零 bootstrap、装系统包。那三件事见第四节。

### 3.1 脚本里每一步在做什么

```sh
# 1 包清单由 packages.txt 生成，不手工维护第二份
sed 's/#.*//' "$REPO/mkimage/packages.txt" \
  | sed 's/[[:space:]]*$//' | grep -v '^$' \
  > "$BUILD/config/package-lists/camphor.list.chroot"

# 2 仓库里的 etc/ 与 usr/ 要铺两处：外层 chroot（这一轮会被复制）、
#   includes.chroot（下一次 chroot 阶段用）
rsync -a --chown=root:root --chmod=D755,F644 --exclude=/fstab "$REPO/etc/" "$BUILD/chroot/etc/"
rsync -a --chown=root:root --chmod=D755,F755 "$REPO/usr/" "$BUILD/chroot/usr/"
rsync -a --chown=root:root --chmod=D755,F644 --exclude=/fstab "$REPO/etc/" "$BUILD/config/includes.chroot/etc/"
rsync -a --chown=root:root --chmod=D755,F755 "$REPO/usr/" "$BUILD/config/includes.chroot/usr/"

# 3 软件源与收尾脚本只放进 ISO（apt-setup 装完就找不到那张盘了；
#   收尾由 late_command 调 /cdrom/camphor/postinstall.sh）
mkdir -p "$BUILD/config/includes.binary/camphor"
cp "$REPO/mkimage/live/sources.list"   "$BUILD/config/includes.binary/camphor/"
cp "$REPO/mkimage/live/postinstall.sh" "$BUILD/config/includes.binary/camphor/"

# 4 重打两个 d-i initrd（预置答案改了才需要，脚本会自己做）
#    install/initrd.gz 与 install/gtk/initrd.gz

# 5 复制 live 母本，然后修副本：主机名、软件源、撤 dpkg 转移、出厂清理
rm -f "$BUILD/.build/binary_chroot"
lb binary_chroot

# 6 压片
rm -f "$BUILD"/.build/binary_{includes,hooks,rootfs,checksums,iso} "$BUILD/.lock"
lb binary
```

## 四、首次从零

一次配好之后日常只跑 `build.sh`。从零要做的三件事：

```sh
cd "$BUILD"

# 1 带镜像源配置（来源改动后要先 lb clean --config 再 lb config，
#   否则旧值会被保留，lb_config(1) 在 DESCRIPTION 里专门警告过）
lb config

# 2 内核用现成的 linux-image deb，放进 packages.chroot：live-build 直接用
#   dpkg 装它，不去下载。--linux-packages none 关掉了它自己装卸内核那一步
mkdir -p config/packages.chroot
cp /path/to/linux-image-<版本>_amd64.deb config/packages.chroot/

# 3 整轮构建
lb build
```

清理时用 `lb clean`（或 `--chroot --binary`），**不要用 `lb clean --all`**：
`--all` 会连 `cache/` 一起删掉，下次构建所有包得重下一遍。
已下载的 deb 另外留一份在构建目录外，构建中断也不至于重来。

## 五、配置目录

```
mkimage/live/
├─ auto/config                          —— 全部 lb config 选项与理由
├─ sources.list                         —— 装好的系统用的软件源，随 ISO 走
├─ postinstall.sh                       —— 安装末尾的收尾（软件源、grub 设置、账号）
└─ config/
   ├─ bootloaders/grub-pc/              —— 引导界面底图与主题
   ├─ hooks/live/0100-camphor.chroot    —— 重建 initramfs、启用单元、venv、出厂清理
   ├─ hooks/live/0100-live-kernel.binary —— 把内核与 initrd 放进 binary/live
   ├─ hooks/live/0200-grub-menu.binary  —— 菜单条目改名
   └─ includes.installer/               —— d-i 的预置答案（三份，主流程见 PITFALLS.md）
```

还有一处是构建时生成、不入库的：

- `config/includes.chroot/` —— 含 `etc/`、`usr/`、`/opt/leaffs`，
  以及生成的 `config/package-lists/camphor.list.chroot`。

## 六、验证

不要在真机上试。用带 Secure Boot 变量文件的 OVMF 起 QEMU：

```
qemu-system-x86_64 -m 4096 -machine q35,smm=on \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.secboot.fd \
  -drive if=pflash,format=raw,file=OVMF_VARS_4M.ms.fd \
  -drive file=camphor-amd64.hybrid.iso,format=raw,media=cdrom \
  -drive id=d0,file=disk.img,format=raw,if=none -device virtio-blk-pci,drive=d0
```

三件事分开验，各有各的判据：

| 验什么 | 判据 |
|---|---|
| ISO 起得来 | 菜单出现，Secure Boot 下 `shim → 签名 grub` 这条链被接受 |
| 装得进去 | 交互安装每一屏都问、都不停；装完的盘上 fstab、账号、软件源、菜单都对 |
| 装完起得来 | 从盘起，内核 → initramfs → 根挂上 → kiosk 出界面 |

QEMU 有 virtio/bochs 显示，`/dev/dri/card0` 会出现，所以 kiosk 那套内存门槛也能一起验。

验 Live 不要靠截图猜：绕开菜单，用 `-kernel` / `-initrd` / `-append "boot=live … console=ttyS0"`
直接起，串口一次拿到内核、systemd 与 X 的全部输出。

## 七、发版

写盘按 `live-manual` 4.4.2 的做法，用 `cp` 就够，不必 `dd`：

```sh
cp camphor-amd64.hybrid.iso ${USBSTICK}   # 整盘，不是分区
sync
```

发布物进 `for-distribution/`，那里只放随产物给用户的东西：许可原文、第三方声明、使用说明。
`build.sh` 结束时打印路径与 sha256 一并记下来。

## 八、还没验的

1. **菜单里出现其它系统（Windows）的端到端。** 机制就位（`os-prober` +
   `GRUB_DISABLE_OS_PROBER=false` + 收尾重跑 `update-grub`），缺一块带 Windows 的盘实测。
2. **要不要再出一份 tar。** `-b tar` 是 live-build 的合法产物类型，能给出裸根文件系统；
   是否还需要这个形态，等需要时再定。
3. **安装器固件弹窗的根因。** 现在用 `hw-detect/load_firmware false` 绕过；
   镜像里 `/firmware/*.deb` 是软链、官方带固件的镜像是真文件，为什么软链不被取走还没查。
