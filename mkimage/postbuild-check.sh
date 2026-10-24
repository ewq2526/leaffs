#!/bin/sh
# 压完片之后的检查：镜像里该有的在不在、不该有的有没有。
#
# 构建前的那一半在 mkimage/preflight.sh；这一半只能等 ISO 出来再看，
# 覆盖的是「退出码 0 但镜像其实是坏的」那类情况。
#
# 用法：sh mkimage/postbuild-check.sh <ISO> [构建目录]
set -u

ISO=${1:?用法: postbuild-check.sh <ISO> [构建目录]}
BUILD=${2:-}
fails=0
ok()  { printf '  [ OK ] %s\n' "$1"; }
bad() { printf '  [FAIL] %s\n' "$1"; fails=$((fails + 1)); }

[ -f "$ISO" ] || { echo "找不到 $ISO"; exit 1; }
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT

echo "== ISO：$ISO"
iso_size=$(stat -c %s "$ISO")
echo "  [INFO] $iso_size 字节"

# 1 顶层内容：内核在 /live，安装介质上要有那两份载荷
xorriso -indev "$ISO" -find /live 2>/dev/null > "$T/live.txt"
xorriso -indev "$ISO" -find /camphor 2>/dev/null > "$T/camphor.txt"
grep -q '/live/vmlinuz-' "$T/live.txt"   && ok "/live 有内核"   || bad "/live 没有内核（Live 项会报 file not found）"
grep -q '/live/initrd.img-' "$T/live.txt" && ok "/live 有 initrd" || bad "/live 没有 initrd"
grep -q '/live/filesystem.squashfs' "$T/live.txt" && ok "/live 有 squashfs" || bad "/live 没有 squashfs"
grep -q '/camphor/sources.list' "$T/camphor.txt"    && ok "/camphor 有 sources.list"    || bad "/camphor 缺 sources.list"
grep -q '/camphor/installer/' "$T/camphor.txt"      && ok "/camphor 有安装器"           || bad "/camphor 缺安装器"

# 2 把 squashfs 取出来看
xorriso -osirrox on -indev "$ISO" -extract /live/filesystem.squashfs "$T/fs.squashfs" >/dev/null 2>&1 \
	|| bad "取不出 squashfs（xorriso extract 失败）"

if [ -f "$T/fs.squashfs" ]; then
	sq_size=$(stat -c %s "$T/fs.squashfs")
	if [ "$sq_size" -lt 500000000 ]; then
		bad "squashfs 只有 $sq_size 字节（压了个空目录？binary_rootfs 之前删了阶段戳却没重拷 chroot）"
	else
		ok "squashfs $sq_size 字节"
	fi
	unsquashfs -ll "$T/fs.squashfs" 2>/dev/null > "$T/sq.list"

	# 该有的
	grep -q '^os-prober' "$(dirname "$0")/packages.base.txt" 2>/dev/null && \
		{ grep -q 'squashfs-root/usr/bin/os-prober$' "$T/sq.list" && ok "镜像里有 os-prober" || bad "镜像里没有 os-prober"; }

	hostname=$(unsquashfs -cat "$T/fs.squashfs" etc/hostname 2>/dev/null | tr -d '\n')
	[ "$hostname" = "camphor" ] && ok "/etc/hostname = camphor" || bad "/etc/hostname = '$hostname'（应为 camphor）"

	# 无线：配置在应用层，随 app.squashfs 铺进来。
	# 主镜像里没有它是正常的 —— 这里只在镜像自称带应用时才查。
	if grep -q 'squashfs-root/etc/wpa_supplicant/wpa_supplicant-wlan0.conf$' "$T/sq.list"; then
		ok "镜像里有 wpa_supplicant-wlan0.conf"
	else
		echo "  [INFO] 主镜像不含 wpa_supplicant-wlan0.conf（在应用层）"
	fi

	mid=$(unsquashfs -cat "$T/fs.squashfs" etc/machine-id 2>/dev/null | tr -d '\n')
	[ -z "$mid" ] && ok "/etc/machine-id 为空" || bad "/etc/machine-id 不是空的（出厂态应交给首次启动生成）"

	# Secure Boot 的签名链：装到盘上的 grub 必须是签名版 + 微软签名的 shim。
	# 少了它，装完所有启动项在固件里都报安全错误，装出来的系统起不来。
	grep -q 'shimx64\.efi\.signed$' "$T/sq.list" \
		&& ok "镜像里有签名 shim（shimx64.efi.signed）" \
		|| bad "镜像里没有 shimx64.efi.signed：装完过不了 Secure Boot"
	grep -q 'grubx64\.efi\.signed$' "$T/sq.list" \
		&& ok "镜像里有签名 grub（grubx64.efi.signed）" \
		|| bad "镜像里没有 grubx64.efi.signed：装完过不了 Secure Boot"
	unsquashfs -cat "$T/fs.squashfs" var/lib/dpkg/status 2>/dev/null > "$T/dpkg.status"
	for p in shim-signed grub-efi-amd64-signed; do
		grep -q "^Package: $p$" "$T/dpkg.status" \
			&& ok "$p 已装进镜像" \
			|| bad "$p 不在镜像里（grub-installer 会写未签名版）"
	done

	# 不该有的
	grep -q 'squashfs-root/etc/default/grub$' "$T/sq.list" \
		&& bad "镜像里有 /etc/default/grub（装 grub 时会弹 conffile 冲突）" \
		|| ok "镜像不带 /etc/default/grub"
	grep -q 'squashfs-root/etc/ssh/ssh_host' "$T/sq.list" \
		&& bad "镜像里有 SSH 主机密钥" || ok "没有 SSH 主机密钥"
	grep -q 'squashfs-root/home/' "$T/sq.list" \
		&& bad "镜像里有 /home 下的东西（构建路径或残留账号）" || ok "没有 /home 残留"
	grep -q 'squashfs-root/root/.cache' "$T/sq.list" \
		&& bad "镜像里有 /root/.cache（pip 缓存等）" || ok "没有 /root/.cache"
	grep -q 'cpython-312' "$T/sq.list" \
		&& bad "镜像里有 cpython-312 字节码（旧解释器残留）" || ok "没有旧字节码"
	grep -q 'localrepo' "$T/sq.list" \
		&& bad "镜像里有本地包仓库（复制 chroot 时仓库没卸载）" || ok "没有包仓库痕迹"

	# 日志：只允许空文件与 README
	n=$(unsquashfs -ll "$T/fs.squashfs" 2>/dev/null | awk '$1 ~ /^-/ && $6 ~ /squashfs-root\/var\/log/ && $3+0 > 0' | wc -l)
	[ "$n" -eq 0 ] && ok "/var/log 没有非空日志" || bad "/var/log 还有 $n 个非空日志（构建过程痕迹）"

	# start-stop-daemon 不能是 /bin/true 的替身
	ssd=$(unsquashfs -ll "$T/fs.squashfs" 2>/dev/null | grep 'squashfs-root/usr/sbin/start-stop-daemon$' | awk '{print $3}')
	if [ "${ssd:-0}" -gt 1000 ]; then ok "start-stop-daemon 像真身（$ssd 字节）"
	else bad "start-stop-daemon 是替身（${ssd:-?} 字节）"; fi
fi

echo
if [ "$fails" -gt 0 ]; then
	echo "镜像检查不过：$fails 项"
	exit 1
fi
echo "镜像检查通过"
