#!/bin/sh
# 开机检查一次物理内存：低于界面所需就在屏幕上说清楚，然后退出。
#
# 界面本身由 camphor-kiosk 的 ConditionMemory=2G 挡住——内存不够时 systemd 直接跳过
# 那个单元。但"跳过"只体现在状态里，屏幕上是一片黑，所以这里补一句人话。
#
# 不做常驻轮询：真正卡死时内存压力的速度远超轮询周期，事后停界面救不回来，
# 那样的看门狗只是白耗 CPU。
set -u

FLOOR_MB=${FLOOR_MB:-1500}
total_mb=$(( $(awk '/^MemTotal:/{print $2}' /proc/meminfo) / 1024 ))

if [ "$total_mb" -lt "$FLOOR_MB" ]; then
    # 屏幕那一句走 camphor-console-msg.sh：它在写之前强制转成 ASCII。
    # 内核控制台字体只有 256/512 个字形，汉字在屏幕上是方框；中文版进 journal。
    CAMPHOR_CONSOLE=/dev/tty1 /usr/local/bin/camphor-console-msg.sh \
        "camphor: not enough memory: ${total_mb} MB, the UI needs 1.5 GB; kiosk not started"
    logger -t camphor-lowmem "内存不足：${total_mb} MB，低于图形界面所需的 1.5 GB，界面不启动" 2>/dev/null || true
    logger -t camphor-lowmem "文件服务（Web / SMB / SSH）正常；要强开界面：systemctl start camphor-kiosk" 2>/dev/null || true
else
    logger -t camphor-lowmem "内存 ${total_mb} MB，够用" 2>/dev/null || true
fi
exit 0
