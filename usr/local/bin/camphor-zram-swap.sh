#!/bin/sh
# 在 zram 上开一块压缩交换区：这台设备没有物理 swap，压缩内存是唯一的换出手段。
# 大小取内存的 1/4 —— 再大只是把 RAM 借给压缩池，回不了本。
set -e
[ -e /dev/zram0 ] || modprobe zram

if swapon --show=NAME --noheadings 2>/dev/null | grep -q zram0; then
    exit 0
fi

# 设备可能已经初始化过（维护时 swapoff 过、再启动本单元就是这种情形）：此时直接写
# disksize 会返回 EBUSY/IO error。先 reset 回未初始化状态再重设。
cur=$(cat /sys/block/zram0/disksize 2>/dev/null || echo 0)
if [ "${cur:-0}" != "0" ]; then
    echo 1 > /sys/block/zram0/reset
fi

mem_kb=$(awk "/^MemTotal:/{print \$2}" /proc/meminfo)
size=$((mem_kb / 4 * 1024))
echo zstd > /sys/block/zram0/comp_algorithm 2>/dev/null || true
echo "$size" > /sys/block/zram0/disksize
mkswap /dev/zram0 > /dev/null
swapon -p 100 /dev/zram0
