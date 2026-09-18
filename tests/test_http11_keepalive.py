# -*- coding: utf-8 -*-
"""HTTP/1.1 第 4 步：连接复用真的生效，且复用是安全的（2026-09-18）

前三步（正文定界 / 每请求状态 / 空闲超时 + 拒绝 chunked）在 HTTP/1.0 下**不改变行为**，
所以那时候「复用」这件事在端到端上根本观察不到。切了 `protocol_version = 'HTTP/1.1'`
之后才是真正的验收 —— 本文件用**原始 socket** 手工在一个连接上连发多个请求，
这是唯一能证明"复用了、而且第二个请求没被第一个的残留污染"的办法。

⚠️ 用 `Content-Length` 判读正文结束，而不是等 EOF —— 等 EOF 就正好把"有没有复用"这件事
测没了（连接复用成功的话根本不会 EOF）。
"""
import socket

from conftest import HTTP_PORT


def _read_response(sock):
    """按 Content-Length 读一个完整响应，返回 (状态行+头, 正文)。"""
    buf = b''
    while b'\r\n\r\n' not in buf:
        b = sock.recv(65536)
        if not b:
            raise AssertionError('连接提前关闭（说明没复用）: %r' % buf[:200])
        buf += b
    head, rest = buf.split(b'\r\n\r\n', 1)
    cl = 0
    for line in head.split(b'\r\n')[1:]:
        if line.lower().startswith(b'content-length:'):
            cl = int(line.split(b':', 1)[1].strip())
    while len(rest) < cl:
        b = sock.recv(65536)
        if not b:
            break
        rest += b
    return head, rest[:cl]


def _status(head):
    return int(head.split(b'\r\n', 1)[0].split(b' ')[1])


def test_two_requests_on_one_connection_reuse_it(server):
    """★ 一个连接连发两个 GET，都拿到 200 —— 复用生效"""
    with socket.create_connection(('127.0.0.1', HTTP_PORT), timeout=10) as s:
        s.sendall(b'GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
        h1, b1 = _read_response(s)
        assert _status(h1) == 200, h1[:200]
        assert not h1.split(b'\r\n', 1)[0].startswith(b'HTTP/1.0'), \
            '状态行还是 1.0: %r' % h1.split(b'\r\n', 1)[0]
        assert b'Connection: close' not in h1, '第一个响应就宣告关闭: %r' % h1

        # 同一个 socket 再发一次
        s.sendall(b'GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
        h2, b2 = _read_response(s)
        assert _status(h2) == 200, h2[:200]
        assert b1 == b2, '两次响应不一致'


def test_post_then_get_on_one_connection(server):
    """★ POST（带正文）之后紧接一个 GET —— 这条同时压到三件事：

    定界（POST 的正文长度）、补读（POST 的正文必须被读完，否则残留会污染下一个请求）、
    每请求预算（不是必须，但同一个连接上的第二个请求要走完整流程）。
    只要有一处没做对，第二个响应就会错位。
    """
    body = b'{"username":"nobody","password":"wrong"}'
    req1 = (b'POST /api/auth/login HTTP/1.1\r\n'
            b'Host: 127.0.0.1\r\n'
            b'Content-Type: application/json\r\n'
            b'Content-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
    with socket.create_connection(('127.0.0.1', HTTP_PORT), timeout=10) as s:
        s.sendall(req1)
        h1, _ = _read_response(s)
        assert _status(h1) in (403, 404), h1[:200]      # 口令错 ⇒ 被拒（R1 会把它变成 404）

        s.sendall(b'GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
        h2, b2 = _read_response(s)
        assert _status(h2) == 200, \
            'POST 之后的下一个请求错位了（正文没读完/定界不对）: %r' % h2[:200]
        assert b2.startswith(b'{'), b2[:200]


def test_connection_close_is_honoured(server):
    """客户端明说 `Connection: close` ⇒ 服务端必须关（RFC 7230）"""
    with socket.create_connection(('127.0.0.1', HTTP_PORT), timeout=10) as s:
        s.sendall(b'GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                  b'Connection: close\r\n\r\n')
        h1, _ = _read_response(s)
        assert _status(h1) == 200, h1[:200]
        assert b'Connection: close' in h1, '没有回 Connection: close: %r' % h1
        # 连接应该被关：再读会立刻拿到空
        assert s.recv(1) == b''


def test_status_line_is_http_1_1(client):
    """协议版本确实切了（httpx 走正常路径也看一眼）"""
    r = client.get('/api/ping')
    assert r.status_code == 200
    assert r.http_version == 'HTTP/1.1', r.http_version
