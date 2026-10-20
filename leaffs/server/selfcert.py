# -*- coding: utf-8 -*-
"""纯 Python 自签服务器证书生成（不依赖 openssl 可执行文件，也不依赖 cryptography）。

为什么要有这个模块：安卓没有随包的 `openssl.exe`，原来唯一的路是用 `cryptography`
生成 —— 但它的 `_rust.so` 是按 4 KB 页对齐编的，在 **16 KB 页**的设备/镜像上 dlopen
直接失败（`program alignment (4096) cannot be smaller than system page size (16384)`），
证书生成不了、按"策略 A"拒绝明文启动，整个服务起不来。16 KB 页从 Android 15 起就是
真机选项，Play 也在往这个方向推，所以不能靠"换个 4 KB 的机器"绕过去。

这里用 ECDSA P-256 自签：椭圆曲线运算 + X.509 DER 编码 + PEM 打包，全部标准库。
只在第一次启动（config 下没有证书）时跑一次，耗时可忽略。

规格与原 openssl 那条保持一致：自签**叶**证书（非 CA）、SAN 覆盖 localhost / 本机
主机名 / 本机全部 IPv4、有效期 10 年、PEM 落盘（证书 + 私钥各一个文件）。
"""
import base64
import hashlib
import os
import secrets
import time

# ---------------------------------------------------------------- P-256 参数
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (_GX, _GY)


def _inv(a, m):
    return pow(a, m - 2, m)


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    x1, y1 = p
    x2, y2 = q
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if p == q:
        lam = (3 * x1 * x1 + _A) * _inv(2 * y1, _P) % _P
    else:
        lam = (y2 - y1) * _inv(x2 - x1, _P) % _P
    x3 = (lam * lam - x1 - x2) % _P
    y3 = (lam * (x1 - x3) - y1) % _P
    return (x3, y3)


def _mul(k, point=_G):
    result = None
    addend = point
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        k >>= 1
    return result


# ---------------------------------------------------------------- DER 编码
def _tlv(tag, payload):
    n = len(payload)
    if n < 0x80:
        return bytes([tag, n]) + payload
    ln = n.to_bytes((n.bit_length() + 7) // 8, 'big')
    return bytes([tag, 0x80 | len(ln)]) + ln + payload


def _seq(*parts):
    return _tlv(0x30, b''.join(parts))


def _set(*parts):
    return _tlv(0x31, b''.join(parts))


def _int(v):
    if v == 0:
        return _tlv(0x02, b'\x00')
    body = v.to_bytes((v.bit_length() + 7) // 8, 'big')
    if body[0] & 0x80:
        body = b'\x00' + body
    return _tlv(0x02, body)


def _oid(dotted):
    parts = [int(x) for x in dotted.split('.')]
    body = bytearray([parts[0] * 40 + parts[1]])
    for v in parts[2:]:
        chunk = [v & 0x7F]
        v >>= 7
        while v:
            chunk.append((v & 0x7F) | 0x80)
            v >>= 7
        body.extend(reversed(chunk))
    return _tlv(0x06, bytes(body))


def _bitstr(data, unused=0):
    return _tlv(0x03, bytes([unused]) + data)


def _octets(data):
    return _tlv(0x04, data)


def _utf8(s):
    return _tlv(0x0C, s.encode('utf-8'))


def _bool(v):
    return _tlv(0x01, b'\xff' if v else b'\x00')


def _utc_time(ts):
    return _tlv(0x17, time.strftime('%y%m%d%H%M%SZ', time.gmtime(ts)).encode())


def _name(common_name):
    cn = _set(_seq(_oid('2.5.4.3'), _utf8(common_name)))
    return _seq(cn)


def _general_names(dns_names, ip_addresses):
    import ipaddress
    items = []
    for d in dns_names:
        items.append(_tlv(0x82, d.encode('ascii')))       # [2] dNSName
    for ip in ip_addresses:
        try:
            packed = ipaddress.ip_address(ip).packed        # v4 → 4 字节，v6 → 16 字节
        except ValueError:
            items.append(_tlv(0x82, str(ip).encode('ascii')))   # 不是 IP 就当域名
            continue
        items.append(_tlv(0x87, packed))                   # [7] iPAddress
    return _seq(*items)


# ---------------------------------------------------------------- 生成
def _ecdsa_sign(priv, data):
    e = int.from_bytes(hashlib.sha256(data).digest(), 'big')
    while True:
        k = secrets.randbelow(_N - 1) + 1
        point = _mul(k)
        if point is None:
            continue
        r = point[0] % _N
        if r == 0:
            continue
        s = (_inv(k, _N) * (e + r * priv)) % _N
        if s:
            return r, s


def _pem(label, der):
    b64 = base64.b64encode(der).decode('ascii')
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return ('-----BEGIN %s-----\n%s\n-----END %s-----\n'
            % (label, '\n'.join(lines), label))


def generate(cert_path, key_path, common_name='LeafFS Server',
             dns_names=('localhost',), ip_addresses=(), days=3650):
    """生成自签服务器证书与私钥（PEM）。返回 (ok, 错误信息)。

    `ip_addresses` 收字符串（v4/v6 都行）；不是合法 IP 的当域名写进 SAN。
    """
    try:
        priv = secrets.randbelow(_N - 1) + 1
        pub = _mul(priv)
        if pub is None:
            return False, '椭圆曲线公钥点计算失败'
        x, y = pub

        spki = _seq(
            _seq(_oid('1.2.840.10045.2.1'), _oid('1.2.840.10045.3.1.7')),
            _bitstr(b'\x04' + x.to_bytes(32, 'big') + y.to_bytes(32, 'big')),
        )

        extensions = _seq(
            _seq(_oid('2.5.29.17'), _octets(_general_names(list(dns_names),
                                                          list(ip_addresses)))),
            _seq(_oid('2.5.29.19'), _bool(True), _octets(_seq())),          # CA:FALSE
            _seq(_oid('2.5.29.15'), _bool(True), _octets(_bitstr(b'\x80', 7))),
        )

        now = int(time.time())
        serial = int.from_bytes(secrets.token_bytes(16), 'big') >> 1 | 1
        tbs = _seq(
            _tlv(0xA0, _int(2)),                                   # v3
            _int(serial),
            _seq(_oid('1.2.840.10045.4.3.2')),                     # ecdsa-with-SHA256
            _name(common_name),
            _seq(_utc_time(now - 300), _utc_time(now + days * 86400)),
            _name(common_name),
            spki,
            _tlv(0xA3, extensions),                                # [3] extensions
        )

        r, s = _ecdsa_sign(priv, tbs)
        sig = _seq(_int(r), _int(s))
        cert_der = _seq(tbs, _seq(_oid('1.2.840.10045.4.3.2')), _bitstr(sig))

        # PKCS#8：里面有 SEC1（含曲线参数与公钥），OpenSSL / ssl 都能直接读
        sec1 = _seq(
            _int(1),
            _octets(priv.to_bytes(32, 'big')),
            _tlv(0xA0, _oid('1.2.840.10045.3.1.7')),
            _tlv(0xA1, _bitstr(b'\x04' + x.to_bytes(32, 'big') + y.to_bytes(32, 'big'))),
        )
        pkcs8 = _seq(_int(0),
                     _seq(_oid('1.2.840.10045.2.1'), _oid('1.2.840.10045.3.1.7')),
                     _octets(sec1))

        with open(cert_path, 'w', encoding='ascii') as f:
            f.write(_pem('CERTIFICATE', cert_der))
        with open(key_path, 'w', encoding='ascii') as f:
            f.write(_pem('PRIVATE KEY', pkcs8))
        return True, ''
    except Exception as e:                                        # pragma: no cover
        for p in (cert_path, key_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        return False, '%s: %s' % (type(e).__name__, e)

