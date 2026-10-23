#!/bin/sh
# 把本机 leaffs 当前在用的 TLS 证书同步进系统信任库。
# 证书路径从服务自己的配置里读，不写死文件名——每套系统自己生成的证书都适用。
set -u
CFG=/srv/leaffs/config/server_config.json
DST=/usr/local/share/ca-certificates/camphor-local.crt

if [ -f "$CFG" ]; then
    cert=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("tls_cert") or "")' "$CFG" 2>/dev/null || true)
else
    cert=""
fi
[ -n "${cert:-}" ] || cert=/srv/leaffs/config/selfsigned.crt
[ -f "$cert" ] || { echo "没有找到证书：$cert" >&2; exit 0; }

if [ -f "$DST" ] && cmp -s "$cert" "$DST"; then
    echo "证书未变，跳过"
    exit 0
fi
install -m 644 "$cert" "$DST"
update-ca-certificates > /dev/null 2>&1
echo "已信任本机证书：$cert"
