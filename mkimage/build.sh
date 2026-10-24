#!/bin/sh
# Camphor 镜像的标准构建流程。
#
# 只有一个入口：把仓库内容铺进构建目录、重打 d-i 的 initrd、复制 live 母本、
# 做出厂清理、压片，最后跑一遍镜像检查。每一步都对应 build 文档里记过的坑。
#
# 用法：
#   sh mkimage/build.sh <仓库> <构建目录> [image|iso]
#
#   image  改过 etc/、usr/、钩子、preseed 时用（默认）：重拷 live 母本 + 压片
#   iso    只改过 preseed 或 ISO 上的载荷（/camphor/*）时用：只重打 initrd + 压片
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
for c in lb xorriso unsquashfs cpio gzip rsync; do need "$c"; done

step() { printf '\n=== %s\n' "$1"; }

# 仓库里的文件是 Linux 侧用的：一律 root 所有，配置 644、脚本 755
sync_etc_usr() {
	rsync -a --chown=root:root --chmod=D755,F644 --exclude=/fstab "$REPO/etc/" "$1/etc/"
	rsync -a --chown=root:root --chmod=D755,F755 "$REPO/usr/" "$1/usr/"
}

# 两个 d-i initrd：把 includes.installer 里的预置答案重打进去
repack_initrd() {
	target=$1
	tmp=$BUILD/.repack-initrd
	rm -rf "$tmp"; mkdir -p "$tmp"
	gzip -dc "$BUILD/binary/$target" | (cd "$tmp" && cpio -i --make-directories --no-absolute-filenames 2>/dev/null)
	( cd "$BUILD/config/includes.installer" && find -print0 | \
		cpio -pumd0 --no-preserve-owner "$tmp/" >/dev/null 2>&1 )
	( cd "$tmp" && find -print0 | cpio -H newc -o0 2>/dev/null | gzip -9 > "$BUILD/binary/$target" )
	rm -rf "$tmp"
	echo "    $target: $(stat -c %s "$BUILD/binary/$target") 字节"
}

# 撤掉构建期的 dpkg 转移。live-build 把 start-stop-daemon 换成 /bin/true，
# 撤的时候要先删替身，否则 dpkg-divert --rename 拒绝覆盖。
clear_diversions() {
	root=$1
	for path in $(chroot "$root" dpkg-divert --list 2>/dev/null | \
			sed -n 's/^local diversion of \([^ ]*\) to \(.*\)$/\1/p'); do
		to=$(chroot "$root" dpkg-divert --list 2>/dev/null | \
			sed -n "s|^local diversion of ${path} to \(.*\)\$|\1|p")
		if [ -n "$to" ] && [ -e "$root$to" ]; then
			rm -f "$root$path"
			chroot "$root" dpkg-divert --local --rename --remove "$path" >/dev/null 2>&1 || true
		else
			chroot "$root" dpkg-divert --local --no-rename --remove "$path" >/dev/null 2>&1 || true
		fi
	done
	n=$(chroot "$root" dpkg-divert --list 2>/dev/null | grep -c '^local diversion' || true)
	if [ "${n:-0}" != "0" ]; then
		echo "构建期 dpkg 转移还剩 ${n} 条，撤不干净就别压片" >&2
		exit 1
	fi
	case "$(file -b "$root/usr/sbin/start-stop-daemon" 2>/dev/null)" in
		*ELF*) ;;
		*) echo "start-stop-daemon 还是替身，别压片" >&2; exit 1 ;;
	esac
}

# 出厂状态：不留构建过程与身份
factory_cleanup() {
	root=$1
	rm -rf "$root/home/leaf" "$root/home"
	rm -rf "$root/var/lib/apt/lists"
	mkdir -p "$root/var/lib/apt/lists/partial"
	rm -rf "$root/root/.cache"
	find "$root/var/log" -maxdepth 1 -type f ! -name README -delete 2>/dev/null || true
	rm -f "$root"/var/log/apt/* "$root"/var/log/apt/*.xz
	rm -rf "$root"/var/log/journal/* "$root"/var/log/runit/*
	: > "$root/var/log/lastlog"
	: > "$root/var/log/wtmp"
	: > "$root/var/log/btmp"
	find "$root/opt/leaffs" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	: > "$root/etc/machine-id"
	rm -f "$root"/etc/ssh/ssh_host_*
}

C=$BUILD/chroot
LIVE=$C/chroot

step "0 构建前自检"
sh "$REPO/mkimage/preflight.sh" "$REPO" "$BUILD"

step "1 仓库内容铺进外层 chroot"
mkdir -p "$BUILD/config/includes.chroot"
sync_etc_usr "$BUILD/config/includes.chroot"
sync_etc_usr "$C"
# 主机名不许沿用构建机的
printf 'camphor\n' > "$C/etc/hostname"
chmod 644 "$C/etc/hostname"
# /etc/default/grub 是 conffile，镜像里不能带（装 grub 时会弹框）
rm -f "$C/etc/default/grub"

step "2 钩子、预置答案与 ISO 上的载荷"
for h in 0100-camphor.chroot 0100-live-kernel.binary 0200-grub-menu.binary; do
	install -m 755 "$REPO/mkimage/live/config/hooks/live/$h" "$BUILD/config/hooks/live/$h"
done
mkdir -p "$BUILD/config/includes.installer" "$BUILD/config/includes.binary/camphor"
cp "$REPO/mkimage/live/config/includes.installer/preseed.cfg"         "$BUILD/config/includes.installer/"
cp "$REPO/mkimage/live/config/includes.installer/preseed-hostname.cfg" "$BUILD/config/includes.installer/"
cp "$REPO/mkimage/live/config/includes.installer/preseed-answers.cfg"  "$BUILD/config/includes.installer/"
cp "$REPO/mkimage/live/sources.list"    "$BUILD/config/includes.binary/camphor/sources.list"
cp "$REPO/mkimage/live/postinstall.sh"  "$BUILD/config/includes.binary/camphor/postinstall.sh"
mkdir -p "$BUILD/config/bootloaders/grub-pc/live-theme"
cp "$REPO"/mkimage/live/config/bootloaders/grub-pc/splash.png   "$BUILD/config/bootloaders/grub-pc/"
cp "$REPO"/mkimage/live/config/bootloaders/grub-pc/config.cfg   "$BUILD/config/bootloaders/grub-pc/"
cp "$REPO"/mkimage/live/config/bootloaders/grub-pc/theme.cfg    "$BUILD/config/bootloaders/grub-pc/"
cp "$REPO"/mkimage/live/config/bootloaders/grub-pc/live-theme/theme.txt "$BUILD/config/bootloaders/grub-pc/live-theme/"

step "3 重打 d-i 的两个 initrd"
repack_initrd install/initrd.gz
repack_initrd install/gtk/initrd.gz

if [ "$MODE" = "iso" ]; then
	step "4 只重出 ISO"
	cd "$BUILD"
	rm -f .build/binary_includes .build/binary_checksums .build/binary_iso .lock
	lb binary
	sh "$REPO/mkimage/postbuild-check.sh" "$BUILD/camphor-amd64.hybrid.iso"
	echo
	ls -l "$BUILD"/camphor-amd64.hybrid.iso
	sha256sum "$BUILD"/camphor-amd64.hybrid.iso
	exit 0
fi

step "4 本地包仓库：装包时要挂，复制 chroot 前必须卸"
if [ -n "$LOCAL_REPO" ]; then
	mkdir -p "$C/etc/apt"
	# chroot 里看到的是 $LOCAL_REPO 本身（bind 到同一路径），不是 $C 前缀
	cat > "$C/etc/apt/sources.list" <<EOF
deb [trusted=yes] file://$LOCAL_REPO trixie main non-free-firmware
deb [trusted=yes] file://$LOCAL_REPO trixie-security main non-free-firmware
deb [trusted=yes] file://$LOCAL_REPO trixie-updates main non-free-firmware
EOF
	mountpoint -q "$C$LOCAL_REPO" || mount --bind "$LOCAL_REPO" "$C$LOCAL_REPO"
	chroot "$C" apt-get update >/dev/null 2>&1 || echo "  （apt-get update 失败，检查 $LOCAL_REPO）"
	chroot "$C" apt-get -y install os-prober >/dev/null 2>&1 || echo "  （os-prober 已装或装不上，postbuild 会查）"
	umount "$C$LOCAL_REPO"
	echo "  已卸下 $C$LOCAL_REPO"
else
	echo "  没给 CAMPHOR_LOCAL_REPO，跳过装包"
fi

step "5 复制 live 母本"
cd "$BUILD"
rm -f .build/binary_chroot
lb binary_chroot

step "6 修母本"
printf 'camphor\n' > "$LIVE/etc/hostname"
chmod 644 "$LIVE/etc/hostname"
rm -f "$LIVE/etc/default/grub"
cp "$REPO/mkimage/live/sources.list" "$LIVE/etc/apt/sources.list"
rm -f "$LIVE"/etc/apt/apt.conf.d/00secure "$LIVE"/etc/apt/apt.conf.d/00recommends "$LIVE/etc/apt/apt.conf"
rm -f "$LIVE/usr/sbin/policy-rc.d" "$LIVE/etc/debian_chroot"
clear_diversions "$LIVE"
factory_cleanup "$LIVE"
echo "  主机名 $(cat "$LIVE/etc/hostname")；os-prober $([ -e "$LIVE/usr/bin/os-prober" ] && echo 在 || echo 缺)"

step "7 构建依赖：给外层 chroot 挂仓库"
if [ -n "$LOCAL_REPO" ]; then
	mkdir -p "$C$LOCAL_REPO"
	mountpoint -q "$C$LOCAL_REPO" || mount --bind "$LOCAL_REPO" "$C$LOCAL_REPO"
fi
rm -f .build/chroot_apt .build/chroot_archives
lb chroot_apt install-binary >/dev/null 2>&1 || true
if [ -n "$LOCAL_REPO" ]; then chroot "$C" apt-get update >/dev/null 2>&1 || true; fi

step "8 压片"
rm -f .build/binary_includes .build/binary_hooks .build/binary_rootfs \
      .build/binary_checksums .build/binary_iso .lock
lb binary
if [ -n "$LOCAL_REPO" ] && mountpoint -q "$C$LOCAL_REPO"; then umount "$C$LOCAL_REPO"; fi

step "9 镜像检查"
sh "$REPO/mkimage/postbuild-check.sh" "$BUILD/camphor-amd64.hybrid.iso"

step "10 产物"
ls -l "$BUILD/camphor-amd64.hybrid.iso"
sha256sum "$BUILD/camphor-amd64.hybrid.iso"
