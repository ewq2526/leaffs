#!/bin/sh
# 构建前自检。
#
# 这里只放"已经反复出过、且能用命令查出来"的错，一条条自动跑，
# 不靠记性。任何一项不过就别起构建。
#
# 分两段：
#   仓库段 —— 仓库里的东西能不能构出对的镜像
#   构建目录段 —— 这个构建目录现在的状态会不会把镜像做坏
#
# 用法：sh mkimage/preflight.sh <仓库路径> <构建目录>
#   <仓库路径>  仓库检出目录
#   <构建目录>  live-build 的构建目录（与仓库分开，出问题整目录重来）
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

# 2 auto/config 与钩子必须有执行位，否则 lb config 静默忽略整份配置
for f in mkimage/live/auto/config mkimage/live/config/hooks/live/0100-camphor.chroot; do
	if [ -x "$REPO/$f" ]; then ok "$f 可执行"; else bad "$f 缺执行位"; fi
done

# 3 构建目录与包清单一律 LF：CRLF 会让脚本 bad interpreter、包名带 \r
attr=$(grep -c '^mkimage/\*\* text eol=lf' "$REPO/.gitattributes" 2>/dev/null)
[ "${attr:-0}" -ge 1 ] && ok ".gitattributes 覆盖 mkimage/" || bad ".gitattributes 缺 mkimage/ 的 LF 规则"

# 3b 图片按二进制存：mkimage/** text eol=lf 会把底图当文本转换，检出即损坏
png=$(grep -c '^\*\.png   binary' "$REPO/.gitattributes" 2>/dev/null)
[ "${png:-0}" -ge 1 ] && ok ".gitattributes 有图片的 binary 规则" || bad ".gitattributes 缺 *.png binary"

# 4 Live 项能不能起：bootappend 必须带 boot=live，否则 initramfs 走普通根挂载
grep -q 'bootappend-live.*boot=live' "$REPO/mkimage/live/auto/config" 2>/dev/null \
	&& ok "bootappend-live 带 boot=live" \
	|| bad "auto/config 的 --bootappend-live 缺 boot=live（Live 项会掉进 busybox）"

# 5 内核要自己放进 /live：--linux-packages none 时 live-build 不管内核
if [ -x "$REPO/mkimage/live/config/hooks/live/0100-live-kernel.binary" ]; then
	ok "live 内核钩子在"
else
	bad "缺 0100-live-kernel.binary（/live 没有内核，Live 项报 file not found）"
fi

# 6 镜像里不能带 /etc/default/grub：它是 conffile，装 grub 时 dpkg 会停下来问
if [ -e "$REPO/etc/default/grub" ]; then
	bad "仓库里有 etc/default/grub（装 grub 时会弹 conffile 冲突）"
else
	ok "仓库不带 etc/default/grub"
fi

# 7 软件源要随介质走：装完由安装器拷到目标系统，缺了装好的机器就没有 apt 源
[ -f "$REPO/mkimage/live/sources.list" ] && ok "sources.list 在" || bad "缺 mkimage/live/sources.list"

# 8 其它系统进菜单：os-prober 在主镜像的包清单里
grep -q '^os-prober' "$REPO/mkimage/packages.base.txt" 2>/dev/null \
	&& ok "主镜像包清单有 os-prober" \
	|| bad "主镜像包清单缺 os-prober（菜单里不会出现其它系统）"

echo
echo "== 构建目录：$BUILD"

# 9 镜像源：约定用清华，出现上游就说明有配置没被覆盖（lb config 不覆盖旧值）
#   config/includes.binary 是随 ISO 发出去的载荷，其中 sources.list 指上游是
#   故意的（装好的系统用官方源），不算"构建配置里留着上游"。
if [ -d "$BUILD/config" ]; then
	up=$(grep -rl 'deb\.debian\.org\|security\.debian\.org' "$BUILD/config" \
		--exclude-dir=includes.binary 2>/dev/null)
	if [ -n "$up" ]; then
		bad "配置里仍有上游源（删掉 config/{binary,bootstrap,chroot,common,source} 再 lb config）"
		echo "$up" | sed 's/^/         /'
	else
		ok "源全部指向镜像站"
	fi
else
	warn "还没有 config/，先跑 lb config"
fi

# 10 .build 与 chroot 必须自洽：有阶段戳却没有 chroot，现场从头重跑
if [ -e "$BUILD/.build/bootstrap" ] && [ ! -d "$BUILD/chroot" ]; then
	bad ".build 有 bootstrap 戳但 chroot 不存在（删 .build 重来）"
else
	ok ".build 与 chroot 自洽"
fi

# 11 外层 chroot 必须是完整环境：被 cache/bootstrap 顶掉过一次，
#    之后压出来的是最小 debootstrap，且 binary_rootfs 会把真环境删掉
#
#    opt/leaffs 不在主镜像这一层：leaffs 是应用，装在应用层镜像里。
#    主镜像的 chroot 里没有它是对的，出现在这里才该问一句。
if [ -d "$BUILD/chroot" ]; then
	for p in usr/bin/python3 usr/sbin/start-stop-daemon; do
		[ -e "$BUILD/chroot/$p" ] && ok "chroot 有 $p" || bad "chroot 缺 $p —— 外层 chroot 不是完整环境"
	done
	if [ -e "$BUILD/chroot/opt/leaffs" ]; then
		ok "chroot 有 opt/leaffs"
	else
		echo "  [INFO] 主镜像不含 opt/leaffs（leaffs 在应用层）"
	fi
fi

# 12 cache/bootstrap 存在时 binary_chroot 会 mv 掉外层 chroot：跑完这一轮环境就没了
if [ -d "$BUILD/cache/bootstrap" ]; then
	warn "cache/bootstrap 在：binary_chroot 会换掉外层 chroot（跑完需从备份还原）"
else
	ok "没有 cache/bootstrap（binary_chroot 只复制，不动外层）"
fi

# 13 本地包仓库不能挂在外层 chroot 里：binary_chroot 的 cp -a 会把整仓拷进镜像
mp=$(findmnt -rn -o TARGET 2>/dev/null | grep "^$BUILD/chroot/home/.*/localrepo$" | head -1)
if [ -n "$mp" ]; then
	bad "包仓库还挂在外层 chroot：$mp（复制 chroot 前必须卸掉，否则整仓进镜像）"
else
	ok "包仓库没挂在外层 chroot"
fi

# 14 构建期 dpkg 转移要能撤掉：start-stop-daemon 被换成 /bin/true 时，
#    真机上会出 dpkg-divert clashes 与 in-target is already running
if [ -d "$BUILD/chroot" ] && [ -x "$BUILD/chroot/usr/bin/env" ]; then
	n=$(chroot "$BUILD/chroot" dpkg-divert --list 2>/dev/null | grep -c '^local diversion')
	echo "  [INFO] 外层 chroot 的本地转移：$n 条"
fi

# 15 下载过的东西要能复用：三处任一处有包就不该重新下载
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
