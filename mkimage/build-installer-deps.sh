#!/bin/sh
# 把安装器的外部依赖打成一个 squashfs 镜像。
#
# 输入是仓库里的 deps/*.deb（parted、unsquashfs 与其库），输出一份只读镜像；
# 系统本身不带这些工具，开机时由安装器入口 loop 挂载它再用。
#
# 用法：sh build-installer-deps.sh <仓库> <输出文件>
set -eu

REPO=${1:?用法: build-installer-deps.sh <仓库> <输出>}
OUT=${2:?用法: build-installer-deps.sh <仓库> <输出>}
SRC=$REPO/mkimage/live/installer/deps

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT

ls "$SRC"/*.deb >/dev/null 2>&1 || { echo "仓库里没有 deps/*.deb" >&2; exit 1; }
for deb in "$SRC"/*.deb; do
	dpkg-deb -x "$deb" "$T"
done

# 补运行时库：对树里每个 ELF 跑 ldd，镜像里没有的从构建机拷一份。
# 基础 C 库不拷 —— 目标系统一定有，拷了反而可能和它的版本打架。
find "$T" -type f -perm -u+x | while read -r f; do
	case "$(file -b "$f" 2>/dev/null)" in
	*ELF*) ldd "$f" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i ~ /^\//) print $i}' ;;
	*) : ;;
	esac
done | sort -u | while read -r lib; do
	[ -n "$lib" ] || continue
	[ -e "$T$lib" ] && continue
	case "$lib" in
		*/libc.so.*|*/ld-linux*|*/libm.so.*|*/libpthread.so.*|*/libdl.so.*|\
		*/librt.so.*|*/libresolv.so.*|*/libgcc_s.so.*|*/libnss_*) continue ;;
	esac
	[ -e "$lib" ] || continue
	mkdir -p "$T$(dirname "$lib")"
	cp -L "$lib" "$T$lib"
done

mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"
mksquashfs "$T" "$OUT" -noappend -comp xz -quiet >/dev/null
echo "    依赖镜像：$(basename "$OUT")  $(stat -c %s "$OUT") 字节"
