#!/bin/sh
# Camphor 镜像的标准构建流程。
#
# 只有一个入口：把仓库内容铺进构建目录、铺安装器载荷、复制 live 母本、
# 停用全部服务、做出厂清理、压片，最后跑一遍镜像检查。每一步都对应 build 文档里记过的坑。
#
# 用法：
#   sh mkimage/build.sh <仓库> <构建目录> [image|iso]
#
#   image  改过 etc/、usr/、钩子时用（默认）：重拷 live 母本 + 压片
#   iso    只改过 ISO 上的载荷（/camphor/*）时用：只压片
#
# 环境变量：
#   CAMPHOR_LOCAL_REPO   本地包仓库目录，给 chroot 里的 apt 用；不给就跳过装包
#   CAMPHOR_MIRROR / CAMPHOR_MIRROR_SECURITY / CAMPHOR_MIRROR_BINARY[_SECURITY]
#                        传给 lb config，见 auto/config
#
# 退出码非 0 就别发镜像。
set -eu

REPO=${1:?用法: build.sh <仓库> <构建目录> [image|iso]}
BUILD=${2:?用法: build.sh <仓库> <构建目录> [image|iso]}
MODE=${3:-image}
LOCAL_REPO=${CAMPHOR_LOCAL_REPO:-}

need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少命令：$1"; exit 1; }; }
for c in lb xorriso unsquashfs cpio gzip rsync python3 dpkg-deb depmod systemctl; do need "$c"; done

step() { printf '\n=== %s\n' "$1"; }

# 仓库里的文件是 Linux 侧用的：一律 root 所有，配置 644、脚本 755
sync_etc_usr() {
	rsync -a --chown=root:root --chmod=D755,F644 --exclude=/fstab "$REPO/etc/" "$1/etc/"
	rsync -a --chown=root:root --chmod=D755,F755 "$REPO/usr/" "$1/usr/"
}

C=$BUILD/chroot
LIVE=$C/chroot

step "0 构建前自检"
sh "$REPO/mkimage/preflight.sh" "$REPO" "$BUILD"

step "1 包清单与仓库内容铺进外层 chroot"
# 主镜像的包清单由 packages.base.txt 生成，不在别处维护第二份。
# 应用与服务在 packages.app.txt，安装依赖在 packages.installer.txt，
# 两者都不进主镜像。
mkdir -p "$BUILD/config/package-lists"
sed 's/#.*//' "$REPO/mkimage/packages.base.txt" \
	| sed 's/[[:space:]]*$//' | grep -v '^$' \
	> "$BUILD/config/package-lists/camphor.list.chroot"
echo "  包清单 $(wc -l < "$BUILD/config/package-lists/camphor.list.chroot") 行"
mkdir -p "$BUILD/config/includes.chroot"
sync_etc_usr "$BUILD/config/includes.chroot"
# 外层 chroot 只有已存在时才铺：从零构建时它还没 bootstrap 出来
if [ -d "$C" ]; then
	sync_etc_usr "$C"
	# 主机名不许沿用构建机的
	printf 'camphor\n' > "$C/etc/hostname"
	chmod 644 "$C/etc/hostname"
	# /etc/default/grub 是 conffile，镜像里不能带（装 grub 时会弹框）
	rm -f "$C/etc/default/grub"
fi

step "2 钩子与 ISO 上的载荷"
# auto/config 决定 lb config 的全部选项：不同步这一份，构建出来的还是旧配置。
install -m 755 "$REPO/mkimage/live/auto/config" "$BUILD/auto/config"
for h in 0100-camphor.chroot 0100-live-kernel.binary 0200-grub-menu.binary; do
	install -m 755 "$REPO/mkimage/live/config/hooks/live/$h" "$BUILD/config/hooks/live/$h"
done
# 介质载荷整目录重建：上一版留下的文件（比如已经废弃的 postinstall.sh）
# 不会被下面的 cp 删掉，会一直跟着进 ISO。
rm -rf "$BUILD/config/includes.binary/camphor"
mkdir -p "$BUILD/config/includes.binary/camphor"
cp "$REPO/mkimage/live/sources.list"    "$BUILD/config/includes.binary/camphor/sources.list"
# 安装器：只放在介质上（/camphor/installer），不进 squashfs。系统里只有一次性
# 启动脚本，按内核参数把它注入到 /run 再跑。
# 旧目录先删掉再铺：rsync 不删多余文件，上一版留下的 backdrop.png 会一直跟着
# 进 ISO（3 MB 的死重量）。
rm -rf "$BUILD/config/includes.binary/camphor/installer"
mkdir -p "$BUILD/config/includes.binary/camphor/installer"
rsync -a --exclude=deps --exclude=__pycache__ \
	"$REPO/mkimage/live/installer/" "$BUILD/config/includes.binary/camphor/installer/"
# 依赖单独打成一个只读镜像，系统里不带这些工具，开机时 loop 挂载
sh "$REPO/mkimage/build-installer-deps.sh" "$REPO" \
	"$BUILD/config/includes.binary/camphor/installer/deps.squashfs"
# 脚本的执行位在成像链路上会丢，这里先按最终权限摆好
chmod 0755 "$BUILD/config/includes.binary/camphor/installer/"*.sh
chmod 0644 "$BUILD/config/includes.binary/camphor/installer/"*.py
# 安装页底图与引导菜单底图同一份源；网页这边只用它当背景，
# 暗色遮罩在页面 CSS 里叠。
install -m 644 "$REPO/mkimage/live/assets/backdrop.jpg" \
	"$BUILD/config/includes.binary/camphor/installer/web/backdrop.jpg"

# 应用层：套在主镜像里（/opt/camphor/app.squashfs），装系统时随主镜像一起拷到盘上。
# 重启后由第二阶段脚本挂载它、铺进系统、启服务；主镜像开机时没有这些服务。
APPIMG=$BUILD/config/includes.chroot/opt/camphor/app.squashfs
# 应用层要两个位置：包源（算差集、下载 deb）与主镜像的 chroot（差集的基准）。
# 从零构建的第一步（prep）还没有 chroot，这一步留到下一次 image 跑。
if [ -d "$C" ]; then
	CAMPHOR_DEPS=${CAMPHOR_DEPS:-$LOCAL_REPO} \
	CAMPHOR_BASE_CHROOT=$C \
		sh "$REPO/mkimage/build-app-image.sh" "$REPO" "$APPIMG"
else
	echo "  还没有 chroot，跳过应用层镜像（下一次 image 模式再打）"
fi
# includes.chroot 只在 chroot 阶段被复制进去；image 模式下那一步已经跑过了，
# 所以外层 chroot 里也要放一份，否则 binary_chroot 复制出来的母本里没有它。
if [ -d "$C" ]; then
	mkdir -p "$C/opt/camphor"
	cp -f "$APPIMG" "$C/opt/camphor/app.squashfs"
fi
# 引导素材整目录同步：底图、主题、九宫格贴图、config.cfg 全在里面，
# 逐个 cp 的写法漏过一次（新加的贴图没被带上，菜单高亮不生效）。
rm -rf "$BUILD/config/bootloaders/grub-pc"
mkdir -p "$BUILD/config/bootloaders"
rsync -a "$REPO/mkimage/live/config/bootloaders/grub-pc/" "$BUILD/config/bootloaders/grub-pc/"
# 底图与标题图都只有一份源（mkimage/live/assets/）。仓库里不放副本，
# ISO 菜单主题和装好系统里那份主题都从这里取。
install -m 644 "$REPO/mkimage/live/assets/backdrop.jpg" \
	"$BUILD/config/bootloaders/grub-pc/live-theme/splash.jpg"
install -m 644 "$REPO/mkimage/live/assets/title-installer.png" \
	"$BUILD/config/bootloaders/grub-pc/live-theme/title.png"
# 装好的系统自己 grub-mkconfig 出来的菜单也要同一套外观：主题目录随主镜像
# 落到 /boot/grub/themes/camphor，安装器在 /etc/default/grub 里指过去。
THEME=$BUILD/config/includes.chroot/boot/grub/themes/camphor
mkdir -p "$THEME"
# 属主与权限在这里定死：仓库挂在 drvfs 上，rsync -a 会把 0777 leaf:leaf 原样带进镜像。
rsync -a --chown=root:root --chmod=D755,F644 --exclude=splash.* --exclude=title.png \
	"$REPO/mkimage/live/config/bootloaders/grub-pc/live-theme/" "$THEME/"
install -m 644 "$REPO/mkimage/live/assets/backdrop.jpg" "$THEME/splash.jpg"
# 介质上的标题是「安装程序」，装好的系统不是安装程序，换成机器用途。
install -m 644 "$REPO/mkimage/live/assets/title-system.png" "$THEME/title.png"
# 装好系统的菜单条目由 grub-mkconfig 生成，带 "Advanced options for …" 这种长标题，
# 还可能挂着别的系统。介质上那两三条短条目用 16% 的窄框刚好，这里必须放宽，
# 否则文字被截断成 "Advanced options for Camphor GNU/Li…"。
sed -i 's/^\twidth = 16%$/\twidth = 56%/' "$THEME/theme.txt"
# includes.chroot 只在 chroot 阶段复制；image 模式下那一步已经跑过，外层直接补一份。
if [ -d "$C" ]; then
	mkdir -p "$C/boot/grub/themes/camphor"
	rsync -a --chown=root:root --chmod=D755,F644 "$THEME/" "$C/boot/grub/themes/camphor/"
fi


if [ "$MODE" = "prep" ]; then
	step "3 材料已铺好（从零构建的第一步）"
	echo "  下一步：lb clean --chroot --binary --stage → lb config →"
	echo "           lb bootstrap → lb chroot → build.sh image"
	exit 0
fi

if [ "$MODE" = "iso" ]; then
	step "3 只重出 ISO"
	cd "$BUILD"
	# binary_hooks 必须一起删：菜单项与超时是 0200 钩子改的，
	# 留着这个戳，改了钩子也不会重跑，出的还是旧菜单。
	rm -f .build/binary_includes .build/binary_checksums .build/binary_iso \
	      .build/binary_hooks .build/binary_grub_cfg .rescue .lock
	lb binary
	sh "$REPO/mkimage/postbuild-check.sh" "$BUILD/camphor-amd64.hybrid.iso"
	echo
	ls -l "$BUILD"/camphor-amd64.hybrid.iso
	sha256sum "$BUILD"/camphor-amd64.hybrid.iso
	exit 0
fi

step "4 依赖收进独立目录：构建只认这一份"
# 包源准备两份没有意义，还容易一份改了另一份没改。
# 起构建前把源仓库硬链接成一份独立目录（同一文件系统上不占空间），
# 后面所有装包都只用它；源仓库不动，缓存也不删。
if [ -n "$LOCAL_REPO" ]; then
	DEPS=${CAMPHOR_DEPS:-$(dirname "$BUILD")/camphor-deps}
	if [ ! -d "$DEPS/pool" ] || [ "$DEPS" != "$LOCAL_REPO" ]; then
		rm -rf "$DEPS"
		cp -al "$LOCAL_REPO" "$DEPS" 2>/dev/null || cp -a "$LOCAL_REPO" "$DEPS"
	fi
	echo "  依赖目录 $DEPS：$(ls "$DEPS/pool"/*.deb | wc -l) 个包"
	LOCAL_REPO=$DEPS
fi

step "5 本地包仓库：装包时要挂，复制 chroot 前必须卸"
if [ -n "$LOCAL_REPO" ]; then
	mkdir -p "$C/etc/apt"
	# chroot 里看到的是 $LOCAL_REPO 本身（bind 到同一路径），不是 $C 前缀
	cat > "$C/etc/apt/sources.list" <<EOF
deb [trusted=yes] file://$LOCAL_REPO trixie main non-free-firmware
deb [trusted=yes] file://$LOCAL_REPO trixie-security main non-free-firmware
deb [trusted=yes] file://$LOCAL_REPO trixie-updates main non-free-firmware
EOF
	# 挂载点要自己建：出厂清理把 /home 删掉了，image 模式不会再跑 chroot 阶段
	# 把那棵目录树建回来，不建这一句 mount 直接失败。
	mkdir -p "$C$LOCAL_REPO"
	mountpoint -q "$C$LOCAL_REPO" || mount --bind "$LOCAL_REPO" "$C$LOCAL_REPO"
	chroot "$C" apt-get update >/dev/null 2>&1 || echo "  （apt-get update 失败，检查 $LOCAL_REPO）"
	# 包清单是唯一来源：image 模式复用已有的 chroot，chroot 阶段不会重跑，
	# 所以清单里新加的包必须在这里补装 —— 否则改了清单等于没改，镜像里照样没有。
	# 已经装过的 apt 会跳过，只有新增的才会真装。
	# 补装同样不带 Recommends：lb 的 chroot 阶段已由 --apt-recommends false 关掉，
	# 这一步要是按 apt 默认装，清单之外的推荐包又会从这里进来。
	pkgs=$(sed 's/#.*//' "$BUILD/config/package-lists/camphor.list.chroot" | tr '\n' ' ')
	if [ -n "$pkgs" ]; then
		if ! chroot "$C" apt-get -y -o APT::Install-Recommends=false install $pkgs >/tmp/camphor-apt.log 2>&1; then
			echo "  包清单有装不上的，尾部日志：" >&2
			tail -8 /tmp/camphor-apt.log >&2
			umount "$C$LOCAL_REPO" 2>/dev/null || true
			exit 1
		fi
	fi
	chroot "$C" apt-get -y -o APT::Install-Recommends=false install os-prober >/dev/null 2>&1 || echo "  （os-prober 已装或装不上，postbuild 会查）"
	umount "$C$LOCAL_REPO"
	echo "  已卸下 $C$LOCAL_REPO"
	# chroot.packages.install 是 lb 装包那一刻记下的集合。钩子后来会 purge 掉一批
	# （旧 X 驱动及其依赖），这个文件没人更新，就与实际装的包对不上 —— 拿它当依据
	# 会得出错的结论。按实际重写一份，让记录跟着盘上的东西走。
	chroot "$C" dpkg-query -W -f='${binary:Package}\t${Version}\n' \
		> "$BUILD/chroot.packages.install" 2>/dev/null || true
	echo "  包记录已按实际重写：$(wc -l < "$BUILD/chroot.packages.install") 个"
	# image 模式不重跑 chroot 阶段的收尾，这几份清单会停在上一轮全量构建的时间上。
	# 留着它们，谁翻出来当"这版装了哪些包"就会看错。
	rm -f "$BUILD/camphor-amd64.files" "$BUILD/camphor-amd64.packages" "$BUILD/chroot.files"
else
	echo "  没给 CAMPHOR_LOCAL_REPO，跳过装包"
fi

step "6 复制 live 母本"
cd "$BUILD"
rm -f .build/binary_chroot
lb binary_chroot

step "7 修母本：与 chroot 阶段同一份钩子"
# 钩子只在 chroot 阶段跑，image 模式是复制母本之后直接压片 —— 母本手术全靠
# 这一步补上，补法是把同一份钩子放进母本里执行，见 sync-rootfs.sh 的说明。
sh "$REPO/mkimage/sync-rootfs.sh" "$REPO" "$LIVE"

step "8 构建依赖：给外层 chroot 挂仓库"
if [ -n "$LOCAL_REPO" ]; then
	mkdir -p "$C$LOCAL_REPO"
	mountpoint -q "$C$LOCAL_REPO" || mount --bind "$LOCAL_REPO" "$C$LOCAL_REPO"
fi
rm -f .build/chroot_apt .build/chroot_archives
lb chroot_apt install-binary >/dev/null 2>&1 || true
if [ -n "$LOCAL_REPO" ]; then chroot "$C" apt-get update >/dev/null 2>&1 || true; fi

step "9 压片"
# 引导素材（底图、主题、config.cfg）由 binary_grub_cfg 与
# binary_bootloader_splash 两个阶段铺进 binary/boot/grub。这两个阶段有戳就跳过，
# 于是改了主题也不会生效 —— 必须连它们一起重跑。
rm -f .build/binary_includes .build/binary_hooks .build/binary_rootfs \
      .build/binary_checksums .build/binary_iso .build/binary_grub_cfg \
      .build/binary_bootloader_splash .build/binary_grub-efi .lock
# binary_includes 是「拷进去」不是「同步」：上一版留在 binary/camphor 下的文件
# 没人删，会一直跟着进 ISO（比如跑过 py_compile 之后多出来的 __pycache__）。
rm -rf binary/camphor
lb binary
if [ -n "$LOCAL_REPO" ] && mountpoint -q "$C$LOCAL_REPO"; then umount "$C$LOCAL_REPO"; fi

step "10 镜像检查"
sh "$REPO/mkimage/postbuild-check.sh" "$BUILD/camphor-amd64.hybrid.iso"

step "11 产物"
ls -l "$BUILD/camphor-amd64.hybrid.iso"
sha256sum "$BUILD/camphor-amd64.hybrid.iso"
