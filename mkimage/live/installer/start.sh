#!/bin/sh
# 安装器的入口：在 live 系统里起一个独立的 X 会话，窗口只显示安装页。
# 这个文件与 server.py、web/ 一起从介质注入到 /run/camphor-installer/，
# 系统里没有它们。
#
# X 会话退出时不要直接返回 —— 那会让 systemd 重启服务，又注入一次，
# 看起来就是死循环。停在这里把原因留在屏幕上。
set -u

# 从介质拷过来的会话脚本同样可能没有执行位，xinit 需要它可执行
chmod 0755 /run/camphor-installer/session.sh 2>/dev/null || true

/usr/bin/xinit /run/camphor-installer/session.sh -- :0 -nolisten tcp vt1
rc=$?

echo
echo "installer: X 会话退出了（退出码 $rc）"
echo "installer: 停在控制台，重启机器可回到引导菜单"
exec /bin/sh
