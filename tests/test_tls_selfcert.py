# -*- coding: utf-8 -*-
"""自签服务器证书：不靠 openssl、也不靠 cryptography 也能生成、能被 ssl 加载。

为什么要有这条：安卓没有随包的 openssl.exe，原来唯一的证书来源是 `cryptography`。
但它的 `_rust.so` 按 4 KB 页对齐编译，在 **16 KB 页**的设备/镜像上 dlopen 直接失败
（`program alignment (4096) cannot be smaller than system page size (16384)`），于是
证书生成不了、按"策略 A"拒绝明文启动，服务整个起不来 —— 而 Android 15 起 16 KB 页
就是真机选项。

这里把 openssl 查找结果置空，强制走纯 Python 那条，然后验证：
  1. 证书/私钥真的写出来了；
  2. `ssl.SSLContext.load_cert_chain` 能加载（DER 与密钥格式没写错）；
  3. 真的完成一次握手（服务端自己签的，客户端拿它当根，再开 hostname 检查）。
"""
import os
import socket
import ssl
import threading

import pytest

from leaffs.server import selfcert, tls


@pytest.fixture()
def cert_paths(tmp_path):
    return str(tmp_path / 'selfsigned.crt'), str(tmp_path / 'selfsigned.key')


def test_generate_without_openssl(monkeypatch, cert_paths):
    """没有 openssl.exe 时走纯 Python 生成，且能被 ssl 加载。"""
    monkeypatch.setattr(tls, '_openssl_exe', lambda: None)
    cert, key = cert_paths
    assert tls._auto_gen_server_cert(cert, key) is True
    assert os.path.getsize(cert) > 0 and os.path.getsize(key) > 0

    ctx = tls.new_server_ctx()
    ctx.load_cert_chain(cert, key)          # 格式不对这里就抛
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_cert_content(cert_paths):
    """自签叶证书：主机名/IP 进 SAN、不是 CA、有效期十年左右。"""
    cert, key = cert_paths
    ok, err = selfcert.generate(cert, key, common_name='LeafFS Server',
                                dns_names=['localhost', 'leaf-host'],
                                ip_addresses=['127.0.0.1', '192.168.1.5', 'fe80::1'])
    assert ok, err

    decoded = ssl._ssl._test_decode_cert(cert)      # 标准库自带的解码器
    attrs = [a for rdn in decoded['subject'] for a in rdn]
    assert ('commonName', 'LeafFS Server') in attrs
    assert decoded['issuer'] == decoded['subject']  # 自签
    assert decoded['version'] == 3

    san = {(t, v) for t, v in decoded.get('subjectAltName', ())}
    assert ('DNS', 'localhost') in san
    assert ('DNS', 'leaf-host') in san
    assert ('IP Address', '127.0.0.1') in san
    assert ('IP Address', '192.168.1.5') in san
    assert ('IP Address', 'FE80:0:0:0:0:0:0:1') in san

    # CA:FALSE 是 critical 扩展，解码器不给；直接对编码后的字节验
    # （OID 2.5.29.19 → 06 03 55 1D 13，critical TRUE → 01 01 FF，值 → 04 02 30 00）
    with open(cert, 'rb') as f:
        raw = ssl.PEM_cert_to_DER_cert(f.read().decode('ascii'))
    assert bytes.fromhex('0603551d130101ff04023000') in raw


def test_real_handshake(cert_paths):
    """起一次真握手：客户端把这张自签当根、并开 hostname 检查。"""
    cert, key = cert_paths
    ok, err = selfcert.generate(cert, key, dns_names=['localhost'],
                               ip_addresses=['127.0.0.1'])
    assert ok, err

    sctx = tls.new_server_ctx()
    sctx.load_cert_chain(cert, key)
    srv = socket.socket()
    srv.bind(('127.0.0.1', 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    result = {}

    def serve():
        try:
            conn, _ = srv.accept()
            with sctx.wrap_socket(conn, server_side=True) as s:
                s.sendall(b'hello')
                result['ok'] = True
        except Exception as e:                      # pragma: no cover
            result['err'] = '%s: %s' % (type(e).__name__, e)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    cctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    cctx.check_hostname = True
    cctx.verify_mode = ssl.CERT_REQUIRED
    cctx.load_verify_locations(cert)
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=5) as raw:
            with cctx.wrap_socket(raw, server_hostname='localhost') as s:
                assert s.recv(16) == b'hello'
    finally:
        srv.close()
        t.join(5)
    assert result.get('ok'), result
