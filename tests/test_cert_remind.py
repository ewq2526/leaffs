# -*- coding: utf-8 -*-
"""证书提示页（8082）—— Host 不可信输入的防护与按钮链接渲染。

不起主站：直接在临时端口上跑 CertRemindHandler，用**原始 socket** 发请求 ——
`Host: a"><script>…` 这种头 http.client / httpx 不会替我们发出去，必须自己拼。
"""
import socket
import threading

import pytest

import leaffs.config.core as _cfg
from leaffs.server.cert_remind import CertRemindHandler
from leaffs.server.handler import ThreadingHTTPServer

MAIN_PORT = 8443          # 假装的主站端口，断言里用它


@pytest.fixture()
def cert_port(monkeypatch):
    """起一个 8082 同类服务（随机端口），并把主站端口固定成 MAIN_PORT"""
    monkeypatch.setattr(_cfg, 'PORT', MAIN_PORT)
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), CertRemindHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def _raw_get(port, host_header=None, path='/'):
    req = 'GET %s HTTP/1.0\r\n' % path
    if host_header is not None:
        req += 'Host: %s\r\n' % host_header
    req += '\r\n'
    with socket.create_connection(('127.0.0.1', port), timeout=5) as s:
        s.sendall(req.encode('utf-8'))
        out = b''
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
    return out


def _status(raw):
    return raw.split(b'\r\n', 1)[0]


def _body(raw):
    return raw.split(b'\r\n\r\n', 1)[1].decode('utf-8', 'replace')


def test_normal_host_builds_main_url(cert_port):
    raw = _raw_get(cert_port, host_header='192.168.1.9:8082')
    assert b' 200 ' in _status(raw)
    assert 'href="https://192.168.1.9:%d/"' % MAIN_PORT in _body(raw)


def test_domain_host_kept(cert_port):
    raw = _raw_get(cert_port, host_header='leaf.local:8082')
    assert 'href="https://leaf.local:%d/"' % MAIN_PORT in _body(raw)


def test_ipv6_host_gets_brackets(cert_port):
    raw = _raw_get(cert_port, host_header='[::1]:8082')
    assert 'href="https://[::1]:%d/"' % MAIN_PORT in _body(raw)


def test_missing_host_falls_back_to_localhost(cert_port):
    raw = _raw_get(cert_port, host_header=None)
    assert 'href="https://localhost:%d/"' % MAIN_PORT in _body(raw)


@pytest.mark.parametrize('bad', [
    'a"><script>alert(1)</script>',      # 闭合 href 属性注入标签
    "a'><script>alert(1)</script>",      # 单引号变体
    'a" onmouseover="alert(1)',          # 注入事件属性
    'evil.com/x',                        # 路径混入主机位
    'a_b.com',                           # 下划线：不是合法主机名
    '-a.com',                            # 标签以连字符开头
    'a b.com',                           # 空格
    'a..b.com',                          # 空标签
    'x' * 300 + '.com',                  # 超长
])
def test_invalid_host_cannot_reach_page(cert_port, bad):
    """非法 Host 一律不用：既不注入，也不把值带进页面，按钮退回 localhost"""
    raw = _raw_get(cert_port, host_header=bad)
    body = _body(raw)
    assert b' 200 ' in _status(raw)
    assert '<script' not in body
    assert 'onmouseover' not in body
    assert bad.split(':')[0] not in body
    assert 'href="https://localhost:%d/"' % MAIN_PORT in body


def test_other_path_404(cert_port):
    raw = _raw_get(cert_port, host_header='192.168.1.9:8082', path='/nope')
    assert b' 404 ' in _status(raw)
