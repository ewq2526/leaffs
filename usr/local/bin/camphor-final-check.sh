#!/bin/bash
# One-shot acceptance check: print the LAN-facing facts to the console for a human.
# Read-only: it changes no config and touches no file inside the shares.
# Output is ASCII only -- the console has no CJK font (preflight enforces this).
exec > /dev/console 2>&1
echo "===== FINAL BEGIN ====="

echo "--- services ---"
systemctl is-active ksmbd avahi-daemon leaffs wsdd2 systemd-networkd

echo "--- listeners (445 SMB / 3702+5355 WSD+LLMNR / 5353 mDNS / 8080 HTTP) ---"
ss -lntup 2>/dev/null | grep -E ':(445|3702|5355|5353|8080)\b' | awk '{print $1, $5}'

echo "--- ksmbd ---"
pgrep -a ksmbd.mountd || echo "ksmbd.mountd is not running"
/usr/sbin/ksmbd.control -l 2>&1 | sed -n '1,10p'

echo "--- share dirs and modes ---"
for d in /srv/leaffs/shared_files/public /srv/leaffs/shared_files/users; do
	if [ -d "$d" ]; then
		stat -c '%A %U:%G %n' "$d"
	else
		echo "missing $d"
	fi
done

echo "--- mDNS service records ---"
ls -1 /etc/avahi/services/ 2>/dev/null || echo "no /etc/avahi/services"
if command -v avahi-browse >/dev/null 2>&1; then
	avahi-browse -rt _smb._tcp 2>/dev/null | sed -n '1,10p'
else
	echo "avahi-browse not installed, only the service file above is checked"
fi

echo "--- https ---"
python3 - <<'PY'
import ssl, urllib.request
c = ssl._create_unverified_context()
print("/login ->", urllib.request.urlopen("https://127.0.0.1:8080/login", timeout=5, context=c).status)
PY

echo "===== FINAL END ====="
