# -*- coding: utf-8 -*-
"""监听地址：通配地址走双栈，IPv4 客户端进 IPv6 socket 时地址归一化。

客户端地址里带着 ::ffff: 前缀的话，会话 IP 绑定、登录限速、本机判定
（127.0.0.1 / ::1）会全部对不上。
"""
import http.server
import socket

import pytest

from leaffs.server.handler import (
    ThreadingHTTPServer, _ipv6_available, _strip_v4_mapped,
)


class _Noop(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass


def test_strip_v4_mapped():
    assert _strip_v4_mapped(('::ffff:192.168.1.5', 1234)) == ('192.168.1.5', 1234)
    assert _strip_v4_mapped(('192.168.1.5', 1234)) == ('192.168.1.5', 1234)
    assert _strip_v4_mapped(('::1', 1234)) == ('::1', 1234)


def test_wildcard_binds_dual_stack():
    """绑 0.0.0.0 实际落在 ::，IPv4 与 IPv6 都要能连上"""
    if not _ipv6_available():
        pytest.skip('系统没有 IPv6')
    srv = ThreadingHTTPServer(('0.0.0.0', 0), _Noop)
    try:
        host, port = srv.server_address[0], srv.server_address[1]
        assert host == '::'
        assert srv.address_family == socket.AF_INET6
        for target in (('127.0.0.1', port), ('::1', port)):
            conn = socket.create_connection(target, timeout=3)
            conn.close()
    finally:
        srv.server_close()


def test_specific_ipv4_stays_ipv4():
    srv = ThreadingHTTPServer(('127.0.0.1', 0), _Noop)
    try:
        assert srv.address_family == socket.AF_INET
        assert srv.server_address[0] == '127.0.0.1'
    finally:
        srv.server_close()
