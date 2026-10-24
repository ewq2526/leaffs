# 构建依赖

起构建之前，这张表里的东西必须都到位。少一样，构建会停在某个阶段，而不是
事先告诉你。反过来，表里标了“可复用”的，一律先从盘上拿，不许重新下载。

## 1 构建机上的软件

```sh
apt install live-build xorriso squashfs-tools mtools dosfstools zstd dpkg-dev apt-utils
```

| 包 | 干什么 |
|---|---|
| `live-build` | 整个流程的骨架，`lb config` / `lb build` |
| `xorriso` | 拼 ISO、取 ISO 里的文件 |
| `squashfs-tools` | 把根文件系统压成 squashfs |
| `mtools` `dosfstools` | 做 EFI 引导段那点 FAT 映像 |
| `zstd` | 压缩与解压本地仓库的包 |
| `dpkg-dev` | `dpkg-scanpackages`，生成本地仓库索引 |
| `apt-utils` | `apt-ftparchive release`，生成带校验和的 `Release` |

## 2 目标系统的包

`mkimage/packages.txt`，一行一个，由它生成 `config/package-lists/camphor.list.chroot`。
清单是唯一来源，不要在别处再维护第二份。

## 3 内核

```
linux-image-6.12.113+deb13-amd64_6.12.113-1_amd64.deb   约 104 MB
```

清单里写的是具体版本名（没有 `linux-image-amd64` 这个 meta），因为这个 deb 来自
本地仓库。换内核要同时改两处：`packages.txt` 那一行、仓库里的 deb。

## 4 安装器（d-i）

`installer_debian-installer` 阶段会把 Debian 安装器取进 `binary/`：自己的内核、
initrd、几百个 udeb，加起来约 213 MB。

* **可复用**：`binary/` 整个目录。只要它还在、`.build/installer_debian-installer`
  这个戳也在，这一段就不会重跑。
* **不要删** `binary/`：它是这套流程里最贵的一次下载。

## 5 Python 依赖

`requirements.txt`：`httpx`、`websockets==16.0`、`pywebview`。

在 chroot 里装进 `/opt/leaffs/venv`，索引用清华（`PIP_INDEX`）。要完全离线可以
先 `pip download -r requirements.txt -d wheels`，钩子改成
`pip install --no-index --find-links /wheels -r requirements.txt`。

## 6 本地包仓库

```
/home/leaf/localrepo/pool/                          801 个 deb
/home/leaf/localrepo/dists/{trixie,trixie-updates,trixie-security}/
```

* 三个套件目录都必须有，`chroot` 的 `sources.list` 会同时引用它们。
* 每个 `Release` 要有 `Suite`/`Codename`/`Components` 与校验和，否则
  debootstrap 报 `Invalid Release file` 或 `no entry for main/binary-amd64/Packages`。
* `Release` 未签名，配置里要 `LB_APT_SECURE=false`。
* `pool/` 是平铺的，`main` 与 `non-free-firmware` 两份 `Packages` 是同一份内容
  （都扫整个 pool），`Filename` 写 `pool/xxx.deb`。加包后照原样重建：
  `dpkg-scanpackages pool /dev/null`，再由 `apt-ftparchive release` 重出三份 `Release`。

### 6.1 binary 阶段的构建依赖也要在仓库里

`binary_*` 各阶段的 `Check_package chroot` 会往 chroot 里现装构建工具，
用完由 `Remove_packages` 再删掉 —— 这些包**必须能从本地仓库装到**，
否则构建就停在那一步。按当前配置（`iso-hybrid`、`grub-efi`、
`--uefi-secure-boot enable`、rootfs 为 squashfs）会用到：

| 阶段 | 包 |
|---|---|
| `binary_rootfs` | `squashfs-tools`（依赖 `liblzo2-2`） |
| `binary_grub-efi` | `grub-efi-amd64-bin` `grub-efi-ia32-bin` `grub-common` `mtools` `dosfstools` |
| `binary_grub-efi` 的 Secure Boot 分支 | `grub-efi-amd64-signed` `shim-signed` |
| `binary_iso` | `xorriso` `isolinux` |

连同传递依赖共 35 个包、约 16 MB，已在 `deb-cache/` 里。
不跑的分支不要备：`grub-pc-bin`（BIOS 引导为空）、`isomd5sum`（不是 dracut-live）、
`dctrl-tools`（没有二进制包清单）、`e2fsprogs`（rootfs 不是 ext*）、
`memtest`、`syslinux`、`win32-loader`、`loadlin`。

## 7 必须联网的两处

| 什么时候 | 多少 | 为什么不能本地 |
|---|---|---|
| d-i 的 udeb | 约 213 MB | 安装器专用包，普通 deb 缓存里没有 |
| pip 的 wheel | 几 MB | 可以从 PyPI 预取，见第 5 节 |

d-i 那 213 MB 可以靠复用 `binary/` 免掉；`binary/` 在移动硬盘的那份环境里是现成的。

## 8 起构建前的核对表

```
□ localrepo/pool 里，packages.txt 的每个包都在
□ packages.txt 里的内核版本 == localrepo 里那个 linux-image deb
□ binary/install 存在（d-i 已取回），.build/installer_debian-installer 存在
□ cache/ 或 localrepo 覆盖 bootstrap 所需的包
□ 四个镜像变量已按部署地设好，配置里没有残留的上游源
```

前四项由 `mkimage/preflight.sh` 检查一部分，剩下的要人看一眼。

## 9 重建这套构建环境

盘上那套东西丢了，按下面顺序重建；每一步都能单独验证，不用等整个构建跑完。

```sh
# 1 工具
apt install live-build xorriso squashfs-tools mtools dosfstools zstd dpkg-dev apt-utils

# 2 本地包仓库：deb 从备份目录来（原来是构建中抓下来的那批）
R=/home/leaf/localrepo
mkdir -p $R/pool $R/dists/trixie/main/binary-amd64 $R/dists/trixie/non-free-firmware/binary-amd64
cp -l /path/to/deb-cache/*.deb $R/pool/        # 跨文件系统时装 CP 会退化，可改用 cp
cd $R
dpkg-scanpackages --multiversion pool /dev/null > dists/trixie/main/binary-amd64/Packages
gzip -kf dists/trixie/main/binary-amd64/Packages
cp dists/trixie/main/binary-amd64/Packages    dists/trixie/non-free-firmware/binary-amd64/
cp dists/trixie/main/binary-amd64/Packages.gz dists/trixie/non-free-firmware/binary-amd64/

# 3 另外两个套件：索引各放一份（不能 cp 到自身）
for s in trixie-updates trixie-security; do
  mkdir -p dists/$s/{main,non-free-firmware}/binary-amd64
  for c in main non-free-firmware; do
    cp dists/trixie/main/binary-amd64/Packages    dists/$s/$c/binary-amd64/
    cp dists/trixie/main/binary-amd64/Packages.gz dists/$s/$c/binary-amd64/
  done
done

# 4 Release：三个套件各一份，必须带 Suite/Codename/Components 与校验和
for s in trixie trixie-updates trixie-security; do
  apt-ftparchive \
    -o APT::FTPArchive::Release::Origin=Camphor \
    -o APT::FTPArchive::Release::Label=Camphor \
    -o APT::FTPArchive::Release::Suite=$s \
    -o APT::FTPArchive::Release::Codename=trixie \
    -o APT::FTPArchive::Release::Architectures=amd64 \
    -o APT::FTPArchive::Release::Components="main non-free-firmware" \
    release dists/$s > dists/$s/Release
done

# 5 配置：本地仓库未签名，必须关掉签名校验；d-i 仍走镜像站
CAMPHOR_MIRROR=file://$R CAMPHOR_MIRROR_SECURITY=file://$R \
CAMPHOR_MIRROR_BINARY=https://mirrors.tuna.tsinghua.edu.cn/debian/ \
CAMPHOR_MIRROR_BINARY_SECURITY=https://mirrors.tuna.tsinghua.edu.cn/debian-security/ \
  lb config
echo 'LB_APT_SECURE="false"' >> config/common

# 6 d-i：要么允许 installer 阶段下载一次，要么把现成的 binary/ 搬过来
#    （搬了之后记得 touch .build/installer_debian-installer，否则会重下）

# 7 起构建前
sh mkimage/preflight.sh <仓库路径> <构建目录>
```

### 踩过的坑（每条都真实卡住过构建）

| 现象 | 根因 |
|---|---|
| `lb config` 的选项全被忽略 | `auto/config` 没有执行位 |
| 改了镜像源却不生效 | `lb config` 不覆盖已存在的配置值，删掉 `config/{binary,bootstrap,chroot,common,system}` 再跑 |
| `Invalid Release file, no entry for main/binary-amd64/Packages` | 手写的 `Release` 没有校验和，要用 `apt-ftparchive release` |
| `Asked to install suite trixie, but got (codename: )` | `Release` 缺 `Suite`/`Codename` |
| `Failed getting release signature file` | 本地仓库没签名，要 `LB_APT_SECURE=false` |
| `trixie-updates/.../Packages` 找不到 | 本地仓库只做了 `trixie` 一个套件 |
| `Invalid cross-device link` | `config/packages.chroot` 软链到别的文件系统，lb 要硬链接 |
| `lb bootstrap_debootstrap already locked` | 上一轮进程没清干净，留了锁 |
| `/boot 下没有内核` | 清单里没有内核包，`--linux-packages none` 又把 lb 那步关了 |
| `Unable to locate package linux-image-amd64` | 本地仓库只有具体版本内核，没有 meta |
| binary 阶段 `Unable to locate package <构建工具>` | 这些包由 `Check_package chroot` 现装、用完再删，本地仓库必须覆盖，见 6.1 |
| `Unable to locate package loadlin` | `LB_LOADLIN` 默认 true，Debian 13 已无此包；`--loadlin false` |
| binary 续跑时 `File not found - .../localrepo/pool/...` | 仓库的 bind mount 在 `binary_rootfs` 内部做，续跑会跳过那个阶段；手动 `mount --bind` 后 <br>再跑，跑完卸载 |
| 收尾 `Remove_packages` 报某个包 `Unable to locate package` | 构建目录里的 `chroot.installed_tmp_pkgs` 留着上一轮失败的包名，删掉那一行 |
| `package '<x>' has no installation candidate` | 同「缺包」，补进本地仓库 |
| `chroot/chroot` 出现在构建目录 | 不是错误：`--build-with-chroot true` 下 `chroot/` 是构建环境，<br>`chroot/chroot/` 才是被打成 squashfs 的 live 系统。判定看 `binary/live/filesystem.squashfs` 的大小（本项目约 1 GB） |
| 磁盘空转、进度不动 | 单线程 `gzip` 压 initramfs，或纯网络等待 |