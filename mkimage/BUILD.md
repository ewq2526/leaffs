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

# 2c 菜单不进根文件系统，只放进 ISO：安装时 grub-installer 会重写
#    /boot/grub/grub.cfg，按卷标找根的这份由 preseed 的 late_command
#    从安装介质拷进目标系统
mkdir -p "$BUILD/config/includes.binary/camphor"
cp "$REPO/boot/grub.cfg" "$BUILD/config/includes.binary/camphor/grub.cfg"
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

## 四、配置目录

```
mkimage/live/
├─ auto/config                          —— 全部 lb config 选项与理由
└─ config/
   ├─ hooks/live/0100-camphor.chroot    —— 建 leaf、启用单元、venv、出厂清理
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

`preseed.cfg` 是按安装手册附录 B 写出来的，但整条安装流程一次也没跑过。
下面几处是它最可能出错的地方，实跑时先看这几项：

1. **根分区卷标。** 配方里写的是 `label{ camphor }`，手册 B.4.8 只说明"配方格式支持
   指定文件系统卷标"，没给例子。装完先 `e2label` 看它到底有没有落上；没落上就在
   `late_command` 里补一条。
2. **`late_command` 换菜单。** d-i 用 `grub-mkconfig` 重写 `/boot/grub/grub.cfg`，
   写出来按 UUID 找根。菜单原件由 chroot 钩子留在 `/usr/share/camphor/grub.cfg`，
   `late_command` 拷回去。要验的是拷回去之后还能不能起——`grub-installer` 也往 ESP
   写了指路文件，那里面仍是它自己生成的 UUID。
3. **`--uefi-secure-boot enable`。** 按文档会装签名 shim 与 grub，但在 OVMF 的
   Secure Boot 变量下能否真的过，只能启动一次看。
4. **要不要再出一份 tar。** `-b tar` 是 live-build 的合法产物类型，能给出裸根文件系统。
   ISO 里的 squashfs 已经承担了"装到盘上"这件事，是否还需要 tar 形态，等 ISO 通了再定。
