#!/bin/sh
# 安装末尾由 preseed 的 late_command 调用：sh /cdrom/camphor/postinstall.sh
#
# 跑在 debian-installer 环境里，不是目标的 chroot 里：
#   /target  刚装好的系统
#   /cdrom   安装介质
#
# 放在脚本里而不是写成一条 preseed：那条长命令在 preseed 的续行与引号处理下
# 会被截断（finish-install 报 unterminated quoted string），而且出错也难查。
set -e

echo "camphor: 软件源回填"
cp /cdrom/camphor/sources.list /target/etc/apt/sources.list

echo "camphor: 合并 /etc/default/grub"
# 这份文件是 grub-common 的 conffile：镜像里预先改它，装 grub 时 dpkg 会停下来
# 问用哪个版本；整份覆盖又会把 grub-installer 写的其它设置（比如其它系统的探测）
# 一起抹掉。所以按键合并，其余行保留。
f=/target/etc/default/grub
for kv in 'GRUB_DISTRIBUTOR="Camphor"' \
          'GRUB_TIMEOUT=8' \
          'GRUB_CMDLINE_LINUX_DEFAULT="quiet loglevel=4"' \
          'GRUB_CMDLINE_LINUX="net.ifnames=0 console=tty0 panic=10 rootwait"' \
          'GRUB_DISABLE_OS_PROBER=false' ; do
	k=${kv%%=*}
	if grep -q "^$k=" "$f" ; then
		sed -i "s|^$k=.*|$kv|" "$f"
	else
		printf '%s\n' "$kv" >> "$f"
	fi
done

echo "camphor: 重生成引导菜单"
in-target update-grub

echo "camphor: 把按名字认人的两处换成安装者的账号"
u=$(awk -F: '$3>=1000 && $3<65534{print $1; exit}' /target/etc/passwd)
[ -n "$u" ] || { echo "camphor: 目标系统里没有普通用户" >&2 ; exit 1 ; }
echo "camphor: 账号 $u"
sed -i "s/^\([[:space:]]*force user = \).*/\1$u/" /target/etc/ksmbd/ksmbd.conf
sed -i "s/^\([[:space:]]*valid users = \).*/\1$u/" /target/etc/ksmbd/ksmbd.conf
sed -i "s/^\([[:space:]]*\)User=.*/\1User=$u/;s/^\([[:space:]]*\)Group=.*/\1Group=$u/" /target/etc/systemd/system/camphor-filemgr.service

echo "camphor: 收尾完成"
