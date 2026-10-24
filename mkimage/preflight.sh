#!/bin/sh
# 构建前自检。
#
# 这里只放"已经反复出过、且能用命令查出来"的错，一条条自动跑，
# 不靠记性。任何一项不过就别起构建。
#
# 用法：sh mkimage/preflight.sh <仓库路径> <构建目录>
#   <仓库路径>  eg. /mnt/e/os
#   <构建目录>  eg. /home/leaf/build-camphor
set -u

REPO=${1:-.}
BUILD=${2:-.}
fails=0

ok()   { printf '  [ OK ] %s\n' "$1"; }
bad()  { printf '  [FAIL] %s\n' "$1"; fails=$((fails + 1)); }
warn() { printf '  [WARN] %s\n' "$1"; }

echo "== 仓库：$REPO"

# 1 发布物里不许出现具体机器（型号、盘符、私有网段、"这台机/那台机器"这类指代）
#   设备名与网段用区分大小写的规则：/dev/sdX 这类占位符是允许的。
hits=$( { grep -rniE 'rtl8[0-9]{3}|这台机|那台机器|构建机上正在跑' \
		--exclude=preflight.sh \
		"$REPO/mkimage" "$REPO/etc" "$REPO/boot" "$REPO/README.md" "$REPO/README.en.md" 2>/dev/null
	  grep -rnE '/dev/sd[b-z][0-9]|10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}' \
		--exclude=preflight.sh \
		"$REPO/mkimage" "$REPO/etc" "$REPO/boot" "$REPO/README.md" "$REPO/README.en.md" 2>/dev/null
	} )
if [ -n "$hits" ]; then
	bad "仓库里有具体机器信息"
	echo "$hits" | sed 's/^/         /'
else
	ok "没有具体机器信息"
fi

# 2 auto/config 与 chroot 钩子必须有执行位，否则 lb config 静默忽略整份配置
for f in mkimage/live/auto/config mkimage/live/config/hooks/live/0100-camphor.chroot; do
	if [ -x "$REPO/$f" ]; then ok "$f 可执行"; else bad "$f 缺执行位"; fi
done

# 3 构建目录与包清单一律 LF：CRLF 会让脚本 bad interpreter、包名带 \r
attr=$(grep -c '^mkimage/\*\* text eol=lf' "$REPO/.gitattributes" 2>/dev/null)
[ "${attr:-0}" -ge 1 ] && ok ".gitattributes 覆盖 mkimage/" || bad ".gitattributes 缺 mkimage/ 的 LF 规则"

echo "== 构建目录：$BUILD"

# 4 镜像源：约定用清华，出现上游就说明有配置没被覆盖（lb config 不覆盖旧值）
if [ -d "$BUILD/config" ]; then
	up=$(grep -rl 'deb\.debian\.org\|security\.debian\.org' "$BUILD/config" 2>/dev/null)
	if [ -n "$up" ]; then
		bad "配置里仍有上游源（删掉 config/{binary,bootstrap,chroot,common,source} 再 lb config）"
		echo "$up" | sed 's/^/         /'
	else
		ok "源全部指向镜像站"
	fi
else
	warn "还没有 config/，先跑 lb config"
fi

# 5 .build 与 chroot 必须自洽：有阶段戳却没有 chroot，现场从头重跑
if [ -e "$BUILD/.build/bootstrap" ] && [ ! -d "$BUILD/chroot" ]; then
	bad ".build 有 bootstrap 戳但 chroot 不存在（删 .build 重来）"
else
	ok ".build 与 chroot 自洽"
fi

# 6 下载过的东西要能复用：三处任一处有包就不该重新下载
n=0
[ -d "$BUILD/config/packages.chroot" ] && n=$(ls "$BUILD/config/packages.chroot"/*.deb 2>/dev/null | wc -l)
echo "  [INFO] packages.chroot 里 $n 个 deb"
[ -d "$BUILD/cache" ] && echo "  [INFO] cache/ 存在：$(du -sh "$BUILD/cache" 2>/dev/null | cut -f1)"
[ -n "${CAMPHOR_DEB_CACHE:-}" ] && \
	echo "  [INFO] 外部 deb 备份：$CAMPHOR_DEB_CACHE（$(ls "$CAMPHOR_DEB_CACHE"/*.deb 2>/dev/null | wc -l) 个）"

echo
if [ "$fails" -gt 0 ]; then
	echo "自检不过：$fails 项，先修再起构建"
	exit 1
fi
echo "自检通过"
