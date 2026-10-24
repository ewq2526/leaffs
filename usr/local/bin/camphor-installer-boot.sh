#!/bin/sh
# 一次性入口：只有内核参数带 camphor.installer=1 时这个单元才会启动。
#
# 它做的事：
#   1. 找介质上的安装器载荷（/camphor/installer，含 deps.squashfs）
#   2. 找不到 → 这块介质没有安装器：删掉自己并关机
#   3. 找到 → 把依赖镜像 loop 挂到 /run，注入组件，启动安装器
#   4. 安装器返回（装完或失败）→ 卸载依赖、删掉自己
#
# 系统里只留这一个入口，跑过一次就不存在了；安装器本体与依赖都在介质上。
set -u

SELF=/usr/local/bin/camphor-installer-boot.sh
DEPS=/run/camphor-deps

# 无界面模式：不起 X、不等显卡，只把后端跑起来给外面调。
HEADLESS=0
grep -q 'camphor.headless=1' /proc/cmdline && HEADLESS=1

if [ "$HEADLESS" = 1 ]; then
	# 写到内核日志：串口一定能看到，排错不用猜
	exec >/dev/kmsg 2>&1
else
	exec >/dev/tty1 2>&1
fi

die() {
	echo
	echo "installer: $*"
	echo "installer: 删掉入口脚本，关机"
	rm -f -- "$SELF"
	sync
	sleep 2
	poweroff -f
}

if [ "$HEADLESS" = 0 ]; then
	echo "installer: 等显卡就绪"
	waited=0
	while [ ! -e /dev/dri/card0 ]; do
		sleep 0.1
		waited=$((waited + 1))
		[ $((waited % 50)) -eq 0 ] && echo "installer: 已等 $((waited / 10)) 秒"
	done
	echo "installer: 显卡就绪（约 $((waited * 100)) ms）"
fi

SRC=
for d in /cdrom/camphor/installer \
         /lib/live/mount/medium/camphor/installer \
         /run/live/medium/camphor/installer; do
	[ -d "$d" ] && { SRC=$d; break; }
done
[ -n "$SRC" ] || die "介质上没有安装器（找过 /camphor/installer）"

rm -rf /run/camphor-installer
mkdir -p /run/camphor-installer
cp -a "$SRC/." /run/camphor-installer/
# 从介质拷过来的文件不一定带执行位（成像链路会抹掉），补一次
chmod 0755 /run/camphor-installer/start.sh /run/camphor-installer/session.sh 2>/dev/null
chmod -R a+rX /run/camphor-installer 2>/dev/null
echo "installer: 从 $SRC 注入到 /run/camphor-installer"

# 依赖是介质上的一个只读镜像：直接挂，不进系统
if [ -f /run/camphor-installer/deps.squashfs ]; then
	mkdir -p "$DEPS"
	if mount -t squashfs -o loop,ro /run/camphor-installer/deps.squashfs "$DEPS"; then
		export PATH="$DEPS/usr/sbin:$DEPS/usr/bin:$PATH"
		export LD_LIBRARY_PATH="$DEPS/usr/lib/x86_64-linux-gnu:$DEPS/lib/x86_64-linux-gnu"
		echo "installer: 依赖镜像已挂到 $DEPS"
	else
		die "依赖镜像挂不上"
	fi
fi

for tool in parted unsquashfs; do
	command -v "$tool" >/dev/null 2>&1 || die "缺少 $tool（依赖镜像里没有）"
done
echo "installer: 依赖就绪"

# 无界面模式：只在没有可靠显示的机器上验证安装流程时用（内核参数 camphor.headless=1）。
# 不起 X、不起窗口，直接把后端跑起来，由外部调接口触发安装。
# 正式安装走的是下面的图形会话，那条路上后端只听本机。
if [ "$HEADLESS" = 1 ]; then
	echo "installer: 无界面模式，只起后端"
	export PATH="$DEPS/usr/sbin:$DEPS/usr/bin:$PATH"
	export LD_LIBRARY_PATH="$DEPS/usr/lib/x86_64-linux-gnu:$DEPS/lib/x86_64-linux-gnu"
	export CAMPHOR_BIND=0.0.0.0
	exec /usr/bin/python3 /run/camphor-installer/server.py
fi

# 不用 exec：安装器返回之后还要收拾自己
/bin/sh /run/camphor-installer/start.sh

echo
echo "installer: 安装器已退出，卸载依赖、删掉入口脚本"
umount "$DEPS" 2>/dev/null || true
rm -f -- "$SELF"
sync
exec sleep infinity
