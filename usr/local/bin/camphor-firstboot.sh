#!/bin/sh
# 第二阶段：把应用层装进系统，并启服务。跑完删掉自己。
#
# 应用镜像不是从介质取的 —— 它套在系统镜像里，装系统时已经随主镜像拷到
# /opt/camphor/app.squashfs。这里把它挂起来，装依赖、铺文件、启服务。
#
# 它和第一阶段（camphor-installer-boot.sh）互为开关：
# 那个脚本还在时（刚装完还没重启），这个单元的条件不成立，不会跑；
# 安装器装完会删掉自己，重启后这个脚本就能跑。
set -u

SELF=/usr/local/bin/camphor-firstboot.sh
UNIT=/etc/systemd/system/camphor-firstboot.service
APP=/opt/camphor/app.squashfs
MNT=/run/camphor-app
LIST=/etc/systemd/camphor-units.list

# 进度要打在屏上：这一段要跑一两分钟，什么都不显示就等于死机。
# /dev/kmsg 里的消息带级别前缀。级别要和启动参数里的 loglevel 对得上：内核只放行
# 「级别数字 < console_loglevel」的消息，装好的系统给的是 loglevel=6，所以 <6>
# （info）正好被挡在门外 —— 写了等于没写。用 <5>（notice）：比 info 高一档，
# loglevel=6 下能上屏，又不至于把整屏刷成调试日志。
say() {
	echo "firstboot: $*"
	printf '<5>firstboot: %s\n' "$*" >/dev/kmsg 2>/dev/null || true
}

fail() {
	say "失败：$*"
	say "保留脚本，下次开机再试"
	exit 1
}

[ -f "$APP" ] || fail "系统里没有应用镜像 $APP"
say "应用镜像：$APP"

mkdir -p "$MNT"
mountpoint -q "$MNT" && umount "$MNT"
mount -t squashfs -o loop,ro "$APP" "$MNT" || fail "挂不上应用镜像"

# 1 依赖：走 dpkg，包的 postinst 要跑（avahi、sshd 这些系统用户靠它建）
#
# 但 postinst 会把服务拉起来，而这时应用层的文件还没铺 —— 配置与 drop-in 都还不在。
# ksmbd 就是这样：13:18:06 被 dpkg 拉起，找不到 /etc/ksmbd/ksmbd.conf 直接以 1 退出；
# avahi 与 ssh 也在同一时刻先起来了。服务统一放到下面第 3 步起，那时配置已就位。
#
# 挡的办法是 policy-rc.d —— Debian 的公开接口，不是自造的补丁：
#   · sysv 脚本：invoke-rc.d(8) 先问它，101 表示不让执行；
#   · systemd 单元：postinst 走 deb-systemd-invoke，那份脚本的说明第一句就是
#     "wrapper around systemctl, respecting policy-rc.d"，同样按 101 判定不跑。
# 中途被打断也要撤掉：这个文件留着，以后所有 apt 操作都不会再启动服务。
if [ -d "$MNT/pool" ]; then
	n=$(ls "$MNT/pool"/*.deb 2>/dev/null | wc -l)
	say "装 $n 个依赖包（约 20 秒，下面没有输出是正常的）"
	trap 'rm -f /usr/sbin/policy-rc.d' EXIT INT TERM HUP
	printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
	chmod 0755 /usr/sbin/policy-rc.d
	dpkg -i --force-confnew "$MNT/pool"/*.deb >/tmp/firstboot-dpkg.log 2>&1 || {
		say "dpkg 有失败，日志在 /tmp/firstboot-dpkg.log"
		tail -5 /tmp/firstboot-dpkg.log
	}
	rm -f /usr/sbin/policy-rc.d
	trap - EXIT INT TERM HUP
	say "依赖包装完"
fi

# 2 应用核心：文件与单元
if [ -d "$MNT/root" ]; then
	say "铺应用文件"
	rsync -aHAX --numeric-ids "$MNT/root/" / || fail "铺文件失败"
fi

# 2.1 账号回填 + 共享目录 + 用户库链接
#   SMB 的落盘身份**不用**安装账号：那个账号在 sudo 组里。这里建一个专用账号，只用来
#   落盘共享目录 —— 没有登录 shell、不在 sudo 里。
say "账号回填"
u=$(awk -F: '$3 >= 1000 && $3 < 65534 { print $1; exit }' /etc/passwd)
if ! getent passwd camphor-smb >/dev/null 2>&1; then
	useradd --system --no-create-home --shell /usr/sbin/nologin camphor-smb \
		|| say "建 camphor-smb 失败"
fi
# 用户库由后端生成，写在内存盘上（/run，关机即消失，启动时按账号表重建）——
# 盘上常驻的只有密文。这里只把 ksmbd 指过去；此刻目标文件还不存在也没关系，
# 符号链接先立着，app 起来就会写。
mkdir -p /run/ksmbd
rm -f /etc/ksmbd/ksmbdpwd.db
ln -s /run/ksmbd/ksmbdpwd.db /etc/ksmbd/ksmbdpwd.db
# 共享目录的根：属主是那个专用账号，后端在里面给每个账号建自己的子目录。
install -d -o camphor-smb -g camphor-smb -m 0770 /srv/leaffs/shared_files/users \
	|| say "共享目录建不出来"
# 公共目录也归它：游客模式下 SMB 直接以这个身份往里写（网页那边是 root，不受影响）。
install -d -o camphor-smb -g camphor-smb -m 0775 /srv/leaffs/shared_files/public \
	|| say "公共目录建不出来"
if [ -n "$u" ]; then
	say "账号回填：$u"
	svc=/etc/systemd/system/camphor-filemgr.service
	if [ -f "$svc" ]; then
		sed -i "s/^\([[:space:]]*\)User=.*/\1User=$u/;s/^\([[:space:]]*\)Group=.*/\1Group=$u/" "$svc"
	fi
else
	say "系统里没有普通用户，账号相关的两处保持占位"
fi

# 3 服务：按清单启用并启动
# 清单里只有常驻服务。两个一次性入口（安装器、这个单元自己）由构建钩子
# 单独启用，不在名单里 —— 出现在名单里就会被下面这行 start，而 start 一个
# 正在运行它的单元等于等自己结束。
systemctl daemon-reload
units=
while read -r unit state; do
	case "$unit" in ''|'#'*) continue ;; esac
	[ "$state" = enabled ] || continue
	if [ ! -f "/etc/systemd/system/$unit" ] && [ ! -f "/usr/lib/systemd/system/$unit" ]; then
		continue
	fi
	units="$units $unit"
done < "$LIST"
# 一次调用启一个事务，由 systemd 自己并行起。原来逐个 systemctl enable/start：
# 每个 enable 都触发一次 daemon-reload，start 又各等各的，十几个单元串下来就是
# 半分钟，屏幕前看起来就是"卡在首次启动"。
say "启用并启动服务：$units"
systemctl enable --quiet $units 2>/dev/null || true
systemctl start $units 2>/dev/null || true
failed=$(systemctl --failed --no-legend 2>/dev/null | awk '{print $1}' | tr '\n' ' ')
[ -n "$failed" ] && say "起不来的：$failed"

umount "$MNT"

say "装完了，删掉应用镜像与这个一次性脚本"
# 应用镜像已经铺进系统，留着只是白占一份空间
rm -f "$APP"
rmdir /opt/camphor 2>/dev/null || true
# 先 disable 再删文件：不清掉多用户目标的那个 wants 链接，之后每次开机都会
# 报一条「单元文件不存在」。
systemctl disable --quiet camphor-firstboot.service 2>/dev/null || true
rm -f "$SELF" "$UNIT"
systemctl daemon-reload
exit 0
