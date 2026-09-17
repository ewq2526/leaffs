# -*- coding: utf-8 -*-
"""三台服务的 `Server` 响应头都不许暴露技术栈（LF-16 回归）。

黑盒报告点的是"`Server` 头与错误响应泄露技术栈"，已核实：
- **WS（8081）确实泄露**：`websockets.serve(...)` 没传 `server_header`，用库默认值
  `Python/3.12 websockets/16.0`；
- **HTTP 主站（8080）不泄露** —— `handler.py` 的 `server_version = 'LeafFS'` 加覆写
  `version_string()` 就是干这个的，报告把"已脱敏的结果"当成了问题；
- **8082 也不泄露** —— `cert_remind.py` 同样设了 `server_version = 'LeafFS'` 且
  `sys_version = ''`。
所以这是"三台里漏了一台"，本轮把 WS 那台补齐。

⚠️ WS 的响应头要**裸 socket** 才能看到（`websockets.connect` 不暴露握手响应头）。
"""
import socket

from conftest import HTTP_PORT, WS_PORT


def _ws_handshake_headers():
    """裸 socket 发一次 WS 握手，返回响应头文本（我们不带 Origin：原生客户端会放行）"""
    s = socket.create_connection(('127.0.0.1', WS_PORT), timeout=5)
    try:
        s.sendall(b'GET /ws HTTP/1.1\r\n'
                  b'Host: 127.0.0.1:%d\r\n'
                  b'Upgrade: websocket\r\n'
                  b'Connection: Upgrade\r\n'
                  b'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n'
                  b'Sec-WebSocket-Version: 13\r\n\r\n' % WS_PORT)
        return s.recv(4096).decode('latin-1', 'replace')
    finally:
        s.close()


def _server_header_of(text):
    for line in text.split('\r\n'):
        if line.lower().startswith('server:'):
            return line.split(':', 1)[1].strip()
    return None


def test_ws_server_header_hides_the_stack(server):
    """WS 握手响应头：Server 是 LeafFS，不含 Python / websockets 版本"""
    text = _ws_handshake_headers()
    assert '101' in text.split('\r\n')[0], '握手没成功，拿不到响应头：%r' % text[:200]
    got = _server_header_of(text)
    assert got == 'LeafFS', 'WS 的 Server 头是 %r —— 应当与另外两台一致' % got
    low = text.lower()
    assert 'python' not in low, '响应头里出现了 Python 版本：%r' % text[:400]
    assert 'websockets/' not in low, '响应头里出现了 websockets 版本：%r' % text[:400]


def test_http_server_header_hides_the_stack(client):
    """HTTP 主站：同样只报 LeafFS（这条一直是好的，别改坏）"""
    r = client.get('/api/ping')
    assert r.status_code == 200, r.text
    got = r.headers.get('Server')
    assert got == 'LeafFS', 'HTTP 的 Server 头是 %r' % got
    assert 'Python' not in (got or ''), got
    assert 'BaseHTTP' not in (got or ''), got
