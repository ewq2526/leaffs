#!/bin/bash
exec > /dev/console 2>&1
echo "===== FINAL BEGIN ====="
echo "--- services ---"
systemctl is-active ksmbd avahi-daemon leaffs wsdd2 systemd-networkd
echo "--- listeners ---"
ss -lntup 2>/dev/null | grep -E ":445|:3702|:5357|:8080|:5353" | awk '{print $1, $5}'
echo "--- shares ---"
smbclient -L localhost -U leaf%leaf -m SMB3 2>&1 | sed -n '4,9p'
echo "--- https ---"
python3 - <<'PY'
import ssl, urllib.request
c = ssl._create_unverified_context()
print("/login ->", urllib.request.urlopen("https://127.0.0.1:8080/login", timeout=5, context=c).status)
PY
echo "===== FINAL END ====="
