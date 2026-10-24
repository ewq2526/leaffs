# 构建发布镜像

产物是一个 `.iso`。写到 U 盘，从 U 盘启动，装到一块盘上，重启就是一台可用的 Camphor。

不自造轮子：引导、精简运行环境、系统本体都由 Debian 的 `live-build` 产出，
配置项都能在 `live-manual`、`lb_config(1)`、`live-boot(7)` 里找到出处。
这里只写这一套东西**怎么拼起来**，以及哪些地方踩过坑。

出过的错见 `PITFALLS.md`，一行一个坑，标明能不能被脚本拦住。
安装器本身（网页后端、两阶段脚本、分区与引导的每一步）见 `INSTALLER.md`。

## 一、三层镜像

介质上有三份互不重叠的东西：

| 层 | 产物 | 里面是什么 | 什么时候用 |
|---|---|---|---|
| 主镜像 | `filesystem.squashfs` | 内核、systemd、X/WebKit/Python、安装器入口 | 装系统时展开到目标盘 |
| 应用镜像 | `app.squashfs`，以文件形式套在主镜像的 `/opt/camphor/` 下 | 应用的 deb、代码、venv、单元、配置 | 系统第一次从盘启动时展开 |
| 依赖镜像 | `deps.squashfs`，放在介质 `/camphor/installer/` | `parted`、`partprobe`、`unsquashfs` | 只在安装器运行时临时挂载 |

这么分的理由：应用里带 venv，改一次就要重打；系统骨架不该为它陪跑。
主镜像开机只有骨架，**没有任何应用服务可启** —— 不需要条件文件、不需要停用服务，
那些单元根本不在系统里。

应用镜像是**文件**，不是分区、不是第二块介质。装系统时 `unsquashfs` 把主镜像
展开到目标盘，这个文件自然跟着落到 `/opt/camphor/app.squashfs`，拔掉介质也不影响。

## 二、两个阶段

```
第一阶段（介质启动）
  GRUB 菜单 → 内核参数 camphor.installer=1
  → camphor-installer.service → /usr/local/bin/camphor-installer-boot.sh
  → 挂 deps.squashfs，起本地后端与 WebKit 窗口，网页三步：分区 / 账号 / 安装
  → unsquashfs 主镜像、写 fstab 与软件源、建账号、装引导
  → 删掉目标系统里的 camphor-installer-boot.sh

第二阶段（第一次从盘启动）
  camphor-firstboot.service
    条件：camphor-installer-boot.sh 不存在、camphor-firstboot.sh 存在
  → 挂 /opt/camphor/app.squashfs
  → dpkg -i 应用依赖（postinst 建 avahi/sshd 这些系统用户）
  → rsync 应用文件
  → 把 ksmbd 与 filemgr 里按名字认人的两处换成安装时建的账号
  → 按 /etc/systemd/camphor-units.list 启用并启动
  → 删掉应用镜像、脚本与单元自己
```

两个脚本互为开关，条件写在单元里，不靠额外的标记文件。
删掉入口脚本就是"装完了"这件事的唯一证据。

## 三、构建机

Debian 13 amd64，root，能连 `deb.debian.org`：

```sh
apt install live-build xorriso squashfs-tools mtools dosfstools zstd
```

`chroot` 与 `binary` 两个阶段要并存，构建目录留出几 GB 余量；放外挂盘上，
仓库那份保持干净，出问题整个删掉重来。

镜像源由环境变量给，默认走上游；安全更新在镜像站上是另一个路径，两个都要换：

```sh
CAMPHOR_MIRROR=https://<镜像站>/debian/ \
CAMPHOR_MIRROR_SECURITY=https://<镜像站>/debian-security/ \
  sh mkimage/build.sh <仓库> <构建目录> image
```

装进目标系统的 `sources.list` 仍指上游，不受这两个变量影响。

**起构建前先查三处有没有能复用的包**：仓库外的 deb 备份、`config/packages.chroot/`、
`cache/`。有就必须先接进构建，不许"先跑一遍看看行不行"。备份不接进去等于没备份。

## 四、标准流程

只有一个入口，不要手敲步骤：

```sh
sh mkimage/build.sh <仓库> <构建目录> [prep|iso|image]
```

| 模式 | 什么时候用 | 做什么 |
|---|---|---|
| `image`（默认） | 改过 `etc/`、`usr/`、钩子、主题、包清单 | 铺内容 → 复制 live 母本 → 修母本 → 压片 → 出 ISO |
| `iso` | 只改过介质载荷（`/camphor/` 下的安装器、菜单钩子） | 只重跑 binary 阶段 |
| `prep` | 从零构建的第一步 | 只把材料铺进构建目录，不跑 lb |

脚本自己会做、不要绕过的事：

1. 起手跑 `mkimage/preflight.sh`
2. 收尾跑 `mkimage/postbuild-check.sh`
3. 撤构建期 dpkg 转移，核对 `start-stop-daemon` 是真 ELF、本地转移为 0
4. 复制 chroot 前卸掉本地仓库挂载
5. 打印 ISO 路径与 sha256

`iso` 模式删掉 `binary_includes`、`binary_hooks`、`binary_checksums`、`binary_iso`、
`binary_grub_cfg` 几个阶段戳 —— 菜单与介质载荷都由这些阶段产出，不删戳就不会重跑。

### 4.1 每一步在做什么

```
0  自检                  preflight.sh：仓库与构建目录都会查，不过就退出

1  包清单与仓库内容      packages.base.txt → config/package-lists/camphor.list.chroot
                        仓库的 etc/、usr/ 铺两处：
                          构建目录的 config/includes.chroot（下次 chroot 阶段用）
                          外层 chroot（image 模式要直接用，那一步已经跑过）
                        主机名显式写成 camphor

2  钩子与介质载荷        auto/config、三个 chroot/binary 钩子
                        /camphor/sources.list、安装器目录、deps.squashfs
                        安装页背景与引导素材（底图、标题图、九宫格选中条）
                        应用层镜像 app.squashfs：
                          用主镜像的 dpkg 状态算差集，只装应用多出来的包
                          建 venv、重写 shebang、mksquashfs
                        装好系统那一份主题目录（/boot/grub/themes/camphor）

3  分岔                prep：到此结束，等手工 lb config/bootstrap/chroot
                        iso：下面是「只重出 ISO」的分支，删戳后直接 lb binary

4  包源收成一份          把 CAMPHOR_LOCAL_REPO 硬链接成独立目录，构建只认它
                        源仓库不动、缓存不动；同一文件系统上不占空间

5  装包                  本地仓库 bind 进 chroot（chroot 里看到的是同一路径）
                        apt update，装 os-prober（菜单要显示同盘的其它系统）
                        复制 chroot 前必须卸挂，否则整仓 deb 会被复制进镜像

6  复制 live 母本        lb binary_chroot → 构建目录里的 chroot/chroot
                        之后所有「改母本」的动作都作用在它身上

7  修母本                /etc/hostname = camphor
                        去掉 /etc/default/grub（它是 conffile，装 grub 时会弹冲突）
                        /etc/apt/sources.list 换成装好系统要用的那份
                        删构建期 apt 配置、policy-rc.d、debian_chroot
                        撤 dpkg 转移（判据：start-stop-daemon 是真 ELF、本地转移为 0）
                        按 camphor-units.list 建启用链接（这一层没有的单元跳过）
                        出厂清理：/home、apt 列表、日志、machine-id、SSH 主机密钥

8  构建期依赖           外层 chroot 挂同一个仓库，把 binary 阶段要用的包装进 chroot
                        （mksquashfs 在 chroot 里跑，宿主机装了不算）

9  压片                 删掉 includes/hooks/rootfs/checksums/iso/grub_cfg/
                        bootloader_splash/grub-efi 与 .lock 这些阶段戳，再 lb binary
                        有戳就跳过，于是在改过的素材上出的还是旧图

10 镜像检查              postbuild-check.sh：ISO 里的内核与 squashfs、镜像内容、
                        出厂状态、签名链、squashfs 大小下限

11 产物                  打印 ISO 路径与 sha256
```

### 4.2 阶段戳

`.build/` 下每个文件代表「某一步已经跑过」。改了什么就删对应的戳：

| 戳 | 删了会重做什么 |
|---|---|
| `binary_includes` | 介质上的载荷（/camphor 下的安装器、背景图） |
| `binary_hooks` | binary 钩子（菜单改写） |
| `binary_grub_cfg` / `binary_bootloader_splash` | 引导配置与主题素材 |
| `binary_rootfs` | squashfs（要从 chroot/chroot 重新压，别单独删它） |
| `binary_checksums` / `binary_iso` | 校验和与 ISO 封装 |
| `binary_chroot` | live 母本（复制 chroot/chroot） |
| `chroot_apt` / `chroot_archives` | chroot 里的包与 apt 状态 |

## 五、包清单与两个镜像脚本

三份清单各管一层，`build.sh` 只读它们：

```
mkimage/packages.base.txt        主镜像（骨架）
mkimage/packages.app.txt         应用镜像
mkimage/packages.installer.txt   依赖镜像
```

两个镜像由独立脚本生成：

```sh
sh mkimage/build-app-image.sh       <仓库> <输出路径>   # 应用层：差集 + venv + mksquashfs
sh mkimage/build-installer-deps.sh  <仓库> <输出路径>   # 依赖层：只装 parted 那几个
```

应用层是按包差集算出来的：对照主镜像的 `dpkg` 状态，只把应用多出来的包装进去。
venv 用 `--ignore-installed`，否则 pip 见系统已有就跳过，出厂系统里没有那个包。

## 六、引导素材与主题

```
mkimage/live/config/bootloaders/grub-pc/
├─ config.cfg      终端、分辨率、insmod、source theme.cfg
├─ theme.cfg       有主题就用，没有退回纯文字菜单
└─ live-theme/     theme.txt、底图、标题图、九宫格选中条
```

底图与标题图的源在 `mkimage/live/assets/`，`build.sh` 现拷进主题目录，
仓库里不存第二份。**装好的系统也要同一套外观**：主题目录随主镜像落到
`/boot/grub/themes/camphor`，安装器在 `/etc/default/grub` 里写 `GRUB_THEME`
与 `gfxterm`，装完之后 `update-grub` 出来的菜单沿用同一份主题。

## 七、验收

不要在真机上试。用带 Secure Boot 变量文件的 OVMF 起 QEMU：

```sh
qemu-system-x86_64 -enable-kvm -m 4096 -machine q35,smm=on \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.secboot.fd \
  -drive if=pflash,format=raw,file=OVMF_VARS_4M.fd \
  -cdrom <ISO> -boot order=d \
  -drive id=d0,file=<测试盘>,format=raw,if=none -device virtio-blk-pci,drive=d0 \
  -device qemu-xhci -device usb-tablet
```

三件事分开验，各有各的判据：

| 验什么 | 判据 |
|---|---|
| ISO 起得来 | 菜单出现；Secure Boot 下 `shim → 签名 grub` 这条链被接受 |
| 装得进去 | 分区、账号、安装三步走完；盘上 fstab、账号、引导项都对 |
| 装完起得来 | 从盘起，第二阶段展开应用层，服务起来，安装脚本与镜像自己消失 |

看日志比截图可靠：`-serial file:<路径>` 一次拿到内核、systemd 与安装器的全部输出；
无图形时用 QEMU monitor 的 `screendump` 抓屏（写出的是 PPM，不是 PNG）。

## 八、构建环境备份

构建目录被中断的 `lb` 收尾动过之后，chroot 可能已经被掏空（只剩几十个包、
grub 都不在）。花几分钟压一份能省掉一整轮重装：

```sh
tar --zstd -cf env.tar.zst --exclude='chroot/proc/*' --exclude='chroot/sys/*' \
  ./chroot ./cache ./config ./auto ./installer ./local .build
```

不打 `binary/` 与 `*.iso`：那是一次 `lb binary` 就能重做出来的东西。
恢复了之后直接 `sh mkimage/build.sh <仓库> <构建目录> image`。

## 九、发版

写盘用 `cp` 就够，不必 `dd`：

```sh
cp camphor-amd64.hybrid.iso ${USBSTICK}   # 整盘，不是分区
sync
```

`for-distribution/` 只放随产物给用户的东西：许可原文、第三方声明、使用说明。
构建脚本、包清单、这份文档属于构建方，留在 `mkimage/`。
`build.sh` 结束时打印的路径与 sha256 一并记进发布记录。
