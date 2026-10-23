#!/bin/sh
# kiosk 的启动器：等显卡就绪，再把进程交给 X。
#
# 两件要等的事：
#   1. /dev/dri/card0 —— i915 由 udev 并行加载，这台机器上大约 5 秒才建出节点。
#      卡没到就起 X，结果一定是 "no screens found" 退出，再靠 Restart 等 3 秒重来，
#      白花一次 X 启动，中间屏幕还黑着。
#   2. 服务端口 —— 服务器由 leaffs.service 提前拉起，窗口只是连上去；端口没起来时
#      开窗口只会显示错误页。
#
# 两件都等不到就一直等：这台机器没有显卡或没有服务都没有界面可言，退出反而把
# "没界面"变成"服务反复重启"。
set -u

waited=0
while [ ! -e /dev/dri/card0 ]; do
    sleep 0.1
    waited=$((waited + 1))
    [ $((waited % 50)) -eq 0 ] && echo "kiosk: 已等 $((waited / 10)) 秒，/dev/dri/card0 还没出现"
done
[ "$waited" -gt 0 ] && echo "kiosk: 等了约 $((waited * 100)) ms 才见到 /dev/dri/card0"

waited=0
until ss -ltn 2>/dev/null | grep -qE ':8080\b'; do
    sleep 0.1
    waited=$((waited + 1))
    [ $((waited % 50)) -eq 0 ] && echo "kiosk: 已等 $((waited / 10)) 秒，服务端口 8080 还没在听"
done
[ "$waited" -gt 0 ] && echo "kiosk: 等了约 $((waited * 100)) ms 才见到端口 8080"

exec /usr/bin/xinit /usr/local/bin/camphor-kiosk-session.sh -- :0 -nolisten tcp vt1
