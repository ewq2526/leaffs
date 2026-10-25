#!/bin/sh
# 母本收尾：让 image 模式复制出来的那份 chroot 和从零构建得到的东西一致。
#
# live-build 只在 chroot 阶段跑 config/hooks/live/ 里的钩子；image 模式是把
# chroot 复制成 chroot/chroot 之后直接压片，钩子不会再跑。于是钩子里的母本手术
# ——屏蔽 network-online、摘掉旧 X 驱动、重建 initramfs、建 /boot 链接、按清单
# 启用单元、设默认目标——在 image 模式下一次都不会生效。补漏过一次：镜像里
# wait-online 还在，真机开机停在 "Waiting for Network to be Configured" 上两分钟。
#
# 这里的做法是把同一份钩子放进母本里执行，不另写一份：两种构建走同一段逻辑，
# 改钩子就是改母本，不存在第二份需要同步的清单。
#
# 用法：sh mkimage/sync-rootfs.sh <仓库> <母本目录>
set -eu

REPO=${1:?用法: sync-rootfs.sh <仓库> <母本目录>}
ROOT=${2:?用法: sync-rootfs.sh <仓库> <母本目录>}
HOOK=$REPO/mkimage/live/config/hooks/live/0100-camphor.chroot
LIST=$ROOT/etc/systemd/camphor-units.list

[ -d "$ROOT/etc" ] || { echo "$ROOT 不像母本：没有 etc/" >&2; exit 1; }
[ -f "$HOOK" ] || { echo "找不到钩子 $HOOK" >&2; exit 1; }
[ -f "$LIST" ] || { echo "找不到单元清单 $LIST" >&2; exit 1; }

step() { printf '\n=== %s\n' "$1"; }

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
	# 构建时在 chroot 里干活的账号目录，连同整个 /home 一起清掉：
	# 镜像里不该有任何人的家目录，账号是安装时才建的。
	rm -rf "$root/home"
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

step "母本：身份、软件源、引导参数"
printf 'camphor\n' > "$ROOT/etc/hostname"
chmod 644 "$ROOT/etc/hostname"
# /etc/default/grub 是 grub 的 conffile，镜像里带着它，装 grub 时会弹冲突
rm -f "$ROOT/etc/default/grub"
cp "$REPO/mkimage/live/sources.list" "$ROOT/etc/apt/sources.list"

step "母本：清旧的启用链接"
# 早先按清单手工 ln -sf 写启用链接，目标一律写成 /etc/systemd/system/<单元>；
# 单元实际在 /usr/lib/systemd/system/ 下的（networkd 就是），链接指向一个不存在
# 的文件，服务起不来。更麻烦的是 systemctl enable 看到链接已经在，就认为已启用，
# 不会纠正它 —— 错的链接会一直跟着镜像走。
# 所以先按清单把旧的启用链接撤掉，后面由钩子用 systemctl enable 重新建。
while read -r unit state; do
	case "$unit" in ''|'#'*) continue ;; esac
	for d in "$ROOT"/etc/systemd/system/*.wants "$ROOT"/etc/systemd/system/*.requires; do
		[ -d "$d" ] || continue
		rm -f "$d/$unit"
	done
done < "$LIST"

step "母本：跑 chroot 阶段的钩子"
install -m 755 "$HOOK" "$ROOT/tmp/camphor-chroot-hook"
trap 'rm -f "$ROOT/tmp/camphor-chroot-hook"' EXIT
chroot "$ROOT" /tmp/camphor-chroot-hook

step "母本：构建期残留与出厂清理"
rm -f "$ROOT"/etc/apt/apt.conf.d/00secure "$ROOT"/etc/apt/apt.conf.d/00recommends "$ROOT/etc/apt/apt.conf"
rm -f "$ROOT/usr/sbin/policy-rc.d" "$ROOT/etc/debian_chroot"
clear_diversions "$ROOT"
factory_cleanup "$ROOT"

echo "  主机名 $(cat "$ROOT/etc/hostname")；os-prober $([ -e "$ROOT/usr/bin/os-prober" ] && echo 在 || echo 缺)"
