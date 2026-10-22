#!/bin/sh
set -u
act=$1; k=${2:-}
[ -n "$k" ] || exit 0
dev="/dev/$k"; [ -b "$dev" ] || exit 0
prop() { /usr/bin/udevadm info --query=property --name="$dev" 2>/dev/null | sed -n "s/^$1=//p" | head -1; }
label=$(prop ID_FS_LABEL); [ -n "$label" ] || label=$(prop ID_FS_LABEL_ENC)
fst=$(prop ID_FS_TYPE); ptype=$(prop ID_PART_ENTRY_TYPE)
case "$ptype" in c12a7328-f81f-11d2-ba4b-00a0c93ec93b) exit 0;; esac
pk=$(lsblk -no PKNAME "$dev" 2>/dev/null); depth=1
while [ -n "$pk" ] && [ $depth -lt 4 ]; do
  for p in /dev/$pk?*; do
    /usr/bin/udevadm info --query=property --name="$p" 2>/dev/null | grep -q "^ID_FS_LABEL=LEAFESP$" && exit 0
  done
  pk=$(lsblk -no PKNAME "/dev/$pk" 2>/dev/null); depth=$((depth+1))
done
root_pk=$(lsblk -no PKNAME "$(findmnt -no SOURCE /)" 2>/dev/null)
[ -n "$root_pk" ] && [ "$(lsblk -no PKNAME "$dev")" = "$root_pk" ] && exit 0
grep -q "^$dev " /proc/mounts && exit 0
label=$(printf "%s" "${label:-$k}" | tr -c "A-Za-z0-9._-" "_")
mkdir -p /media
typearg=""; first=noatime
case "$fst" in
  ntfs|ntfs3) typearg="-t ntfs3"; first=ro,noatime ;;
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
