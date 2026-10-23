#!/bin/sh
# 块设备插上/拔出时自动挂到 /media/<卷标>。
#
# 不挂的东西：
#   - EFI 系统分区（vfat，按分区类型判断）
#   - 根分区自己
#   - 正在用作 / 的那块盘上的分区？**不**跳过——根盘上可能有 Windows 的数据分区，
#     那正是要挂的（以前这里写成"跳过与根同盘的所有分区"，迁移到内部盘之后把 Windows
#     的 NTFS 一并跳过了）
set -u
act=$1; k=${2:-}
[ -n "$k" ] || exit 0
dev="/dev/$k"; [ -b "$dev" ] || exit 0
prop() { /usr/bin/udevadm info --query=property --name="$dev" 2>/dev/null | sed -n "s/^$1=//p" | head -1; }
label=$(prop ID_FS_LABEL); [ -n "$label" ] || label=$(prop ID_FS_LABEL_ENC)
fst=$(prop ID_FS_TYPE); ptype=$(prop ID_PART_ENTRY_TYPE)
# EFI 系统分区不挂
case "$ptype" in c12a7328-f81f-11d2-ba4b-00a0c93ec93b) exit 0;; esac
# 根分区自己不挂
rootdev=$(findmnt -no SOURCE / 2>/dev/null)
[ -n "$rootdev" ] && [ "$dev" = "$rootdev" ] && exit 0
grep -q "^$dev " /proc/mounts && exit 0
label=$(printf "%s" "${label:-$k}" | tr -c "A-Za-z0-9._-" "_")
mkdir -p /media
typearg=""; first=noatime
case "$fst" in
  # ntfs3 连只读都拒绝"脏卷"（Windows 没干净关机、或开着快速启动），加 force；
  # 只读挂载时 force 不会写卷，也不用退回用户态的 ntfs-3g（FUSE 吞吐差一个量级）。
  ntfs|ntfs3) typearg="-t ntfs3"; first=ro,noatime,force ;;
  exfat) typearg="-t exfat" ;;
esac
case "$act" in
  mount)
    mp="/media/${label:-$k}"; n=2; base="$mp"
    while [ -e "$mp" ]; do mp="$base-$n"; n=$((n+1)); done
    mkdir -p "$mp"
    /usr/bin/systemd-mount --no-block --collect $typearg -o "$first" "$dev" "$mp" >/dev/null 2>&1 && exit 0
    rmdir "$mp" 2>/dev/null; exit 0 ;;
  umount)
    /usr/bin/systemd-mount --umount --no-block "$dev" >/dev/null 2>&1 || true ;;
esac
exit 0
