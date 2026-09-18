# -*- coding: utf-8 -*-
"""HTTP/1.1 预备 · 第 3 步：空闲超时 + 拒绝 chunked 请求体（2026-09-18）

两个都是"切到 keep-alive 之前必须先有的东西"：

1. **空闲超时**（`KEEPALIVE_IDLE_TIMEOUT = 15`）：`ThreadingMixIn` 是一个连接一个线程、
   准入位上限 256，而浏览器会把空闲连接挂上几分钟。不主动收，几十个客户端就能把线程和
   准入位占满 —— 等于自己给自己做 DoS。条件写成 `close_connection is False`
   （＝"这条连接已经确定要复用"），所以 **1.0 下永远不成立**，那条路径一点没动。

2. **拒绝 chunked 请求体**：所有接口都按 `Content-Length` 读正文，遇到
   `Transfer-Encoding: chunked` 会**静默当成 0 字节**（上传内容凭空消失）。HTTP/1.0 的
   客户端不用 chunked、1.1 的会用，所以必须在切协议之前堵成明确的 411。

⚠️ 这里用**原始 socket** 发请求 —— 要精确控制 `Transfer-Encoding` 头，httpx 不方便。
"""
import socket

from conftest import HTTP_PORT


def _raw(payload: bytes, read_timeout=10.0) -> bytes:
    """发一段原始字节，读回全部响应（服务端会关连接，读到 EOF 为止）。"""
    with socket.create_connection(('127.0.0.1', HTTP_PORT), timeout=read_timeout) as s:
        s.sendall(payload)
        chunks = []
        while True:
            try:
                b = s.recv(65536)
            except socket.timeout:
                break
            if not b:
                break
            chunks.append(b)
        return b''.join(chunks)


def _status(raw: bytes) -> int:
    return int(raw.split(b' ', 2)[1])


# ---------- chunked 请求体一律 411 ----------

def test_chunked_post_is_rejected_with_411(server):
    """★ chunked 的 POST 必须明确回 411，不能静默当成空正文

    ⚠️ 依赖 `server` fixture 只是为了把服务拉起来 —— raw socket 绕过 httpx，
    拿不到 httpx 的 base_url。
    """
    req = (b'POST /api/ping HTTP/1.0\r\n'
           b'Host: 127.0.0.1\r\n'
           b'Transfer-Encoding: chunked\r\n'
           b'\r\n'
           b'5\r\nhello\r\n0\r\n\r\n')
    raw = _raw(req)
    assert _status(raw) == 411, raw[:200]
    assert b'Content-Length' in raw, '411 响应自己也没带正文定界'


def test_chunked_get_is_rejected_with_411(server):
    req = (b'GET / HTTP/1.0\r\n'
           b'Host: 127.0.0.1\r\n'
           b'Transfer-Encoding: chunked\r\n'
           b'\r\n'
           b'0\r\n\r\n')
    raw = _raw(req)
    assert _status(raw) == 411, raw[:200]


def test_identity_transfer_encoding_is_accepted(server):
    """`identity` 等价于没有编码 ⇒ 不该被 411 挡掉

    ⚠️ 只能用 raw socket 发：httpx 自己就不允许 `Transfer-Encoding: identity`
    （客户端侧直接抛 LocalProtocolError）。
    """
    req = (b'GET /api/ping HTTP/1.0\r\n'
           b'Host: 127.0.0.1\r\n'
           b'Transfer-Encoding: identity\r\n'
           b'\r\n')
    raw = _raw(req)
    assert _status(raw) == 200, raw[:200]


def test_normal_request_unaffected(client):
    """没有 Transfer-Encoding 的普通请求照旧"""
    r = client.get('/api/ping')
    assert r.status_code == 200


# ---------- 空闲超时 ----------

def test_keepalive_idle_timeout_is_much_shorter_than_read_timeout():
    """空闲超时必须明显短于 READ_TIMEOUT —— 否则 keep-alive 下等于没收"""
    from leaffs.server.handler import HTTPHandler
    assert HTTPHandler.KEEPALIVE_IDLE_TIMEOUT < HTTPHandler.READ_TIMEOUT
    assert HTTPHandler.KEEPALIVE_IDLE_TIMEOUT <= 30, \
        '空闲超时太长，起不到回收线程/准入位的作用'


def test_idle_timeout_only_applies_to_reusable_connections():
    """★ 空闲超时只在"连接已确定复用"时才设 —— 1.0 下 close_connection 恒为 True"""
    import inspect

    from leaffs.server.handler import HTTPHandler
    src = inspect.getsource(HTTPHandler.handle_one_request)
    assert 'KEEPALIVE_IDLE_TIMEOUT' in src, 'handle_one_request 没有设空闲超时'
    assert "getattr(self, 'close_connection', True) is False" in src, \
        '没有用"已确定复用"作为条件 —— 1.0 下也会被套上短超时'


def test_parse_request_restores_read_timeout():
    """请求头读完之后要换回完整读超时，否则读正文/上传会被空闲超时误杀"""
    import inspect

    from leaffs.server.handler import HTTPHandler
    src = inspect.getsource(HTTPHandler.parse_request)
    assert 'READ_TIMEOUT' in src, 'parse_request 没有把读超时换回去'


def test_do_get_and_do_post_both_check_chunked():
    """两个入口都要查 —— 漏一个就有一条路能静默丢正文"""
    import inspect

    from leaffs.server.handler import HTTPHandler
    for name in ('do_GET', 'do_POST'):
        src = inspect.getsource(getattr(HTTPHandler, name))
        assert '_reject_chunked_body' in src, '%s 没有检查 chunked 请求体' % name
