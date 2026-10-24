# 构建发布镜像

产物是一个 `.iso`。用户把它写到 U 盘，从 U 盘启动，装到一块盘上，重启就是一台 Camphor。

这套东西不自造：Debian 的 `live-build` 已经把"引导 + 精简运行环境 + 系统本体"做成了标准产物，
下面每一处配置都能在 `live-manual`、`lb_config(1)`、`live-boot(7)` 里找到出处。

## 一、三层在 live-build 里对应什么

| 层 | 在 live-build 里 | 说明 |
|---|---|---|
| 引导 | 由 `--bootloaders grub-efi` + `--uefi-secure-boot enable` 生成 | 装的是 `shim-signed`、`grub-efi-amd64-signed` 里的签名件，Secure Boot 能过 |
| 内盒 | live 运行环境，`filesystem.squashfs` | 加 `--debian-installer live` 后附带 `live-installer` udeb，负责把系统铺到目标盘 |
| 本体 | 同一个 squashfs | 安装时被复制到目标分区，之后以 ext4 读写运行 |

`lb_config(1)` 关于 `--debian-installer live` 的原话：

> instead of installing the debian system from packages from the medium or the network,
> it installs the live system to the disk.

所以"内盒装系统"这一步不用自己写脚本，选对选项即可。

**关掉的默认项也要写下来**，否则换一台机器重建时又会打开：

| 选项 | 为什么关 |
|---|---|
| `--loadlin false` | `loadlin` 是 DOS 下引导 Linux 的老工具，Debian 13 已无此包；开着构建会停在 `binary_loadlin` 报 `Unable to locate package loadlin` |

## 二、构建环境

Debian 13 amd64，root，能连 `deb.debian.org`：

```sh
apt install live-build xorriso squashfs-tools mtools dosfstools zstd
```

`chroot` 与 `binary` 两个阶段要并存，约 8 GB。根分区通常不够宽裕，把构建目录放到
外挂盘上，仓库那份保持干净，出问题整个删掉重来：

```sh
cp -a mkimage/live /media/<挂载点>/build-camphor
cd /media/<挂载点>/build-camphor
```

取包用哪个镜像由环境变量给，默认是上游。安全更新在镜像站上是另一个路径，
两个都要换，否则构建会卡在 `security.debian.org` 上：

```sh
CAMPHOR_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian/ \
CAMPHOR_MIRROR_SECURITY=https://mirrors.tuna.tsinghua.edu.cn/debian-security/ \
lb config
```

直连 `deb.debian.org` 通常只有几十 KB/s，换成本地镜像能差一个数量级。
装进目标系统的 `sources.list` 仍指上游，不受这两个变量影响。

## 三、步骤

构建目录放在外挂盘上（第二节），仓库另有它处。下面用两个变量区分：

```sh
REPO=/path/to/camphor          # 仓库的工作副本
BUILD=/media/<挂载点>/build-camphor

# 1 包清单由 packages.txt 生成，不手工维护第二份
sed 's/#.*//' "$REPO/mkimage/packages.txt" \
  | sed 's/[[:space:]]*$//' | grep -v '^$' \
  > "$BUILD/config/package-lists/camphor.list.chroot"

# 2 仓库里的 etc/ 与 usr/ 铺进 chroot 的根。每次构建都要重做，
#   config/includes.chroot/ 是产物，不入库
rm -rf "$BUILD/config/includes.chroot"
mkdir -p "$BUILD/config/includes.chroot/opt/leaffs"
rsync -a "$REPO/etc/" "$BUILD/config/includes.chroot/etc/"
rsync -a "$REPO/usr/" "$BUILD/config/includes.chroot/usr/"

# 2c 软件源不进根文件系统，只放进 ISO：apt-setup 在不联网、不选镜像时只写
#    cdrom 源，装完那张盘就不在了，由 preseed 的 late_command 从安装介质
#    拷回镜像站版本。引导菜单不再随 ISO 提供 —— grub-installer 自己生成
#    按 UUID 找根的版本，内核命令行参数来自 etc/default/grub。
mkdir -p "$BUILD/config/includes.binary/camphor"
cp "$REPO/mkimage/live/sources.list" "$BUILD/config/includes.binary/camphor/sources.list"
rsync -a "$REPO/leaffs/" "$BUILD/config/includes.chroot/opt/leaffs/leaffs/"
cp "$REPO/requirements.txt" "$BUILD/config/includes.chroot/opt/leaffs/requirements.txt"

# 2b 内核用现成的 linux-image deb，放进 packages.chroot：live-build 直接
#    用 dpkg 装它，不去下载。auto/config 里的 --linux-packages none 关掉了
#    live-build 自己装卸内核包那一步
mkdir -p "$BUILD/config/packages.chroot"
cp /path/to/linux-image-<版本>_amd64.deb "$BUILD/config/packages.chroot/"
rsync -a "/lib/modules/$KV" "$BUILD/config/includes.chroot/lib/modules/"

# 3 生成配置并构建
cd "$BUILD"
lb config
lb build
```

产物 `camphor-amd64.hybrid.iso`。

写盘按 `live-manual` 4.4.2 的做法，用 `cp` 就够，不必 `dd`：

```sh
cp camphor-amd64.hybrid.iso ${USBSTICK}   # 整盘，不是分区
sync
```

清理时用 `lb clean`（或 `--chroot --binary`），**不要用 `lb clean --all`**：
`--all` 会连 `cache/` 一起删掉，下次构建所有包得重下一遍。
已下载的 deb 另外留一份在构建目录外（例如 `<构建盘>/deb-cache/`），
构建中每 30 秒同步一次，构建中断也不至于重来。

改了 `auto/config` 里的选项之后要先 `lb clean --config` 再 `lb config`，
否则旧值会被保留，`lb_config(1)` 在 DESCRIPTION 里专门警告过这一点。

## 三点五、只改镜像内容时怎么重出 ISO

改了 `etc/`、`usr/`、`preseed.cfg`，不重跑 chroot 阶段也能出 ISO。顺序固定，
每一步都在下面这些坑旁边：

```sh
B=<构建目录> ; REPO=<仓库>
C=$B/chroot ; LIVE=$C/chroot

# 1 仓库内容要铺两处：includes.chroot 影响「下一次 chroot 阶段」，
#   外层 chroot 才是这一轮 lb binary_chroot 会复制的源。只铺前者等于没改。
rsync -a --chown=root:root --chmod=D755,F644 --exclude=/fstab "$REPO/etc/" "$C/etc/"
rsync -a --chown=root:root --chmod=D755,F755 "$REPO/usr/" "$C/usr/"

# 2 预置答案改了要重打两个 d-i initrd（见第五节）
repack install/initrd.gz ; repack install/gtk/initrd.gz

# 3 重拷 live 母本，然后修副本（构建期的 apt 与人机痕迹不能进镜像）
rm -f .build/binary_chroot ; lb binary_chroot
#   软件源写成 mkimage/live/sources.list 的内容
#   删 etc/apt/apt.conf.d/00secure 00recommends、usr/sbin/policy-rc.d、etc/debian_chroot
#   撤掉本地 dpkg 转移（见 3.3）

# 4 压片
rm -f .build/binary_includes .build/binary_hooks .build/binary_rootfs \
      .build/binary_checksums .build/binary_iso .lock
lb binary
```

### 3.1 `includes.chroot` 不是给 `lb binary` 用的

`lb binary_chroot` 复制的是外壳 `chroot/`。`config/includes.chroot` 只在
**chroot 阶段**铺进去。只改仓库就跑 `lb binary`，镜像里仍是旧文件 ——
而且 `lb binary` 退出码是 0、ISO 时间戳是新的，看着完全正常。

### 3.2 `cache/bootstrap` 存在时，`binary_chroot` 会换掉外层 chroot

```
/usr/lib/live/build/binary_chroot
    rm -rf chroot/chroot
    if [ -d cache/bootstrap ]; then
            mv chroot chroot.tmp
            cp -a cache/bootstrap chroot     # 外层 chroot 变成干净 bootstrap
    else
            cp -a chroot chroot.tmp
    fi
    mv chroot.tmp chroot/chroot
```

真正的构建环境被搬到 `chroot/chroot`，而 `binary_rootfs` 收尾会
`rm -rf chroot/chroot` —— **跑完这一轮，构建目录里就没有可用的环境了**，
再跑只能得到一份最小 debootstrap。要么先 `rm -rf cache/bootstrap`（只复制，
不搬），要么认下这一轮之后必须从环境备份还原。

同理，binary 钩子不能指望外层 `chroot/boot` 有内核；`0100-live-kernel.binary`
是从 `binary/live/filesystem.squashfs` 里取的。

### 3.3 构建期的 dpkg 转移要连替身一起删

live-build 把 `/usr/sbin/start-stop-daemon` 转移成 `/bin/true` 的符号链接。
撤的时候直接 `dpkg-divert --rename --remove` 会被拒：

```
dpkg-divert: error: rename involves overwriting '/usr/sbin/start-stop-daemon'
with different file '/usr/sbin/start-stop-daemon.distrib', not allowed
```

要先删掉那个替身再撤。撤不掉的后果不是"少一个包"，是装到真机上以后
`dpkg-divert ... clashes` 和 `in-target is already running` 连锁失败。
判断标准只有一条：`start-stop-daemon` 是 ELF，`dpkg-divert --list | grep -c
'^local diversion'` 是 0。不满足就别压片。

## 四、配置目录

```
mkimage/live/
├─ auto/config                          —— 全部 lb config 选项与理由
└─ config/
   ├─ hooks/live/0100-camphor.chroot    —— 重建 initramfs、启用单元、venv、出厂清理
   ├─ hooks/live/0100-live-kernel.binary —— 把内核与 initrd 放进 binary/live
   └─ includes.installer/preseed.cfg    —— d-i 的预置答案
```

还有一处是第 2 步生成、不入库的：

- `config/includes.chroot/` —— 含 `etc/`、`usr/`、`/opt/leaffs`，
  以及生成的 `config/package-lists/camphor.list.chroot`。

## 五、验证

不要在真机上试。用带 Secure Boot 变量文件的 OVMF 起 QEMU：

```
qemu-system-x86_64 -m 4096 -machine q35,smm=on \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.secboot.fd \
  -drive if=pflash,format=raw,file=OVMF_VARS_4M.ms.fd \
  -drive file=camphor-amd64.hybrid.iso,format=raw,media=cdrom \
  -drive id=d0,file=disk.img,format=raw,if=none -device virtio-blk-pci,drive=d0
```

要验到：ISO 起得来 → 安装项把系统铺到 `disk.img` → 从 `disk.img` 起 → 桌面出来。

QEMU 有 virtio/bochs 显示，`/dev/dri/card0` 会出现，所以 kiosk 那套内存门槛也能一起验。

## 六、还没验的几处

`preseed.cfg` 只写与机器无关的答案：不联网、不跑 tasksel、不留 root 口令、
安装器缺固件时不问。分区、账号、主机名、时区、语言、键盘、引导位置都由安装者当场选。
下面几处是实跑时先要看的：

1. **装完能不能起。** `late_command` 把 `/etc/fstab` 的根从 `LABEL=camphor`
   改成分区的 UUID —— 这行脚本单独在真 ext4 上跑通过（改出来的 UUID 与
   `blkid` 一致），但还没在 d-i 里跑过整轮。装完先 `cat /etc/fstab` 核对。
2. **分区由人做之后的盘。** 要验「已有分区 + 空闲区」的盘（GPT、MBR 各一次）：
   能建出根、已有分区一个不动。整盘擦除不在方案里。
3. **账号认不认得上。** ksmbd 的两个共享与 `camphor-filemgr` 的用户名由
   `late_command` 换成安装者建的账号；验法是装完看 `ksmbd.conf` 里是不是那个名字，
   再从另一台机器连一次共享。
4. **交互屏不能停住。** 语言、键盘、主机名、时区、用户、分区、引导位置 ——
   每一屏都要走一遍，看有没有哪一问把安装卡住。
5. **要不要再出一份 tar。** `-b tar` 是 live-build 的合法产物类型，能给出裸根文件系统。
   ISO 里的 squashfs 已经承担了"装到盘上"这件事，是否还需要 tar 形态，等 ISO 通了再定。
