# -*- coding: utf-8 -*-
"""环路来源没有特权：不带会话的本机请求同样只能是匿名。

**来由（2026-09-27）**：一份黑盒对抗报告把"源地址 `127.0.0.1` 且本次不发 Cookie 头"的请求
判成 `super_admin`（能读用户表、配置、会话表、日志，`POST /api/mkdir` 还真建出了目录），
据此定级为"本机提权"。复核结论是**那份脚本的假阳性**：它的 `cookie=None` 语义是
"从磁盘读管理员 Cookie"（见其 `req.py`），所以标着 no cookie 的用例实际发的是管理员会话；
同一份脚本里另一处用 `cookie=""` 真正发出无 Cookie 请求的那组，源地址 `127.0.0.1` 与
`127.0.0.2` **都是** `{"role": null}`。

本文件把这条不变量钉在**端到端**上 —— 单元层只有 `test_auth_hardening.py` 里那一句
`get_session('', '127.0.0.1')`，而"环路"这个维度只有在真的从 `127.0.0.2` 连过来时才存在：

    · 源地址 127.0.0.1 / 127.0.0.2 × Cookie 不带头 / 空头 / 伪造 → 一律匿名，管理面 404；
    · 对照组：把**真会话**从 127.0.0.1 发出去仍是 200（否则上面那些 404 可能只是"请求本身坏了"）；
    · WS 通道给同一个结论 —— 两条通道都只调 `auth/core.get_session`，不该各写一套判定。

用原始 socket 而不是 httpx：只有这样才能**精确控制 `Cookie` 头的三种形态**
（httpx 会不会把空值头丢掉不受我们控制），也才能指定请求的来源地址。
"""
import json
import socket
import ssl

from conftest import HTTP_PORT, WS_PORT, TEST_TLS, login

WIFI = 'wifi_session'
BOGUS = WIFI + '=' + '0' * 48
SOURCES = ('127.0.0.1', '127.0.0.2')      # 整个 127/8 都是环回，可用来换来源地址
COOKIE_SHAPES = (None, '')                 # None = 完全不发这个头；'' = 发一个空值头


def _recv_response(sock):
    """按 Content-Length 读一个响应，返回 (状态码, 正文)。"""
    buf = b''
    while b'\r\n\r\n' not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(b'\r\n\r\n')
    cl = 0
    for line in head.split(b'\r\n')[1:]:
        if line.lower().startswith(b'content-length:'):
            cl = int(line.split(b':', 1)[1].strip())
    while len(rest) < cl:
        chunk = sock.recv(65536)
        if not chunk:
            break
        rest += chunk
    return int(head.split(b' ')[1]), rest[:cl]


def _raw_get(path, cookie, src='127.0.0.1', timeout=10):
    """发一个 GET，返回 (状态码, 正文)。

    `cookie` 三态：`None` = **完全不发 Cookie 头**；`''` = 发空值头（`Cookie: `）；
    其它字符串原样发出。
    """
    lines = ['GET %s HTTP/1.1' % path,
             'Host: 127.0.0.1:%d' % HTTP_PORT,
             'Connection: close']
    if cookie is not None:
        lines.append('Cookie: ' + cookie)
    req = ('\r\n'.join(lines) + '\r\n\r\n').encode('utf-8')

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.bind((src, 0))        # 关键：先绑来源地址再连，服务端看到的 client_address 就是它
        sock.connect(('127.0.0.1', HTTP_PORT))
        if TEST_TLS:
            sock = ssl._create_unverified_context().wrap_socket(sock, server_hostname='127.0.0.1')
        sock.sendall(req)
        return _recv_response(sock)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _session_cookie(client):
    sid = client.cookies.get(WIFI)
    assert sid, '登录后应拿到会话 Cookie'
    return '%s=%s' % (WIFI, sid)


# ---------- 1. HTTP：无会话的环路请求 = 匿名 ----------

def test_loopback_without_a_session_gets_no_role(server):
    """★ 环路 × 三种 Cookie 形态：`/api/auth/check` 一律 `role: null`"""
    for src in SOURCES:
        for cookie in COOKIE_SHAPES + (BOGUS,):
            st, body = _raw_get('/api/auth/check', cookie, src=src)
            assert st == 200, (src, cookie, st, body[:200])
            role = json.loads(body).get('role')
            assert role is None, \
                '源 %s 的 Cookie=%r 请求拿到了角色 %r' % (src, cookie, role)


def test_loopback_without_a_session_cannot_touch_admin_api(server):
    """★ 同一批无会话请求打管理面：一律 404（统一拒绝口径），绝不能是 200"""
    for src in SOURCES:
        for cookie in COOKIE_SHAPES + (BOGUS,):
            for path in ('/api/users', '/api/config/deep', '/api/logs', '/api/connections'):
                st, body = _raw_get(path, cookie, src=src)
                assert st == 404, \
                    '源 %s 的 Cookie=%r 请求打 %s 拿到 %d：%s' % (src, cookie, path, st, body[:200])


def test_real_session_from_loopback_is_a_working_control(client):
    """对照组：**有会话**的请求从 127.0.0.1 发出去必须是 200

    没有这条，上面那些 404 可能只是"这条手写的原始请求本身就坏"，断言就没有牙了。
    """
    login(client)
    st, body = _raw_get('/api/users', _session_cookie(client), src='127.0.0.1')
    assert st == 200, (st, body[:200])


def test_session_is_bound_to_its_source_address(client):
    """会话认来源地址：`127.0.0.1` 建的会话，从 `127.0.0.2` 拿同一份 Cookie 用不了

    这就是那份报告"带 Cookie 反而没特权"的那一列 —— 它以为判据是"这次请求有没有 Cookie 头"，
    实际是会话记录里的来源 IP 绑定（`auth/core._same_client`）。顺带确认这次拒绝**不会**
    把会话删掉（旧实现会 pop，等于随便谁拿 sid 打一次就能把人踢下线）。
    """
    login(client)
    ck = _session_cookie(client)
    st, body = _raw_get('/api/users', ck, src='127.0.0.2')
    assert st == 404, (st, body[:200])
    assert client.get('/api/users').status_code == 200, '原来源地址的会话被这次请求弄坏了'


# ---------- 2. WS：与 HTTP 同一个结论 ----------

def _ws_connect(cookie):
    """`cookie` 三态同 `_raw_get`：None = 不发头，'' = 空值头，其它原样。"""
    from websockets.sync.client import connect
    if TEST_TLS:
        url, kw = 'wss://127.0.0.1:%d' % WS_PORT, {'ssl': ssl._create_unverified_context()}
    else:
        url, kw = 'ws://127.0.0.1:%d' % WS_PORT, {}
    headers = None if cookie is None else {'Cookie': cookie}
    return connect(url, additional_headers=headers, **kw)


def test_ws_agrees_with_http_on_a_sessionless_connection(server):
    """★ WS 通道对无会话连接给出与 HTTP 相同结论：`admin-sub` 只回 error，绝不下发管理数据

    报告把两条通道的差异当成"各有一套鉴权实现"。实际两条通道都只调 `auth/core.get_session`
    （`ws.py` 的 `_ws_get_session`），差异出在**客户端带了什么**：HTTP 那侧带了磁盘上的
    管理员 Cookie，WS 那侧真没带。
    """
    for cookie in COOKIE_SHAPES + (BOGUS,):
        with _ws_connect(cookie) as ws:
            ws.send(json.dumps({'type': 'admin-sub'}))
            msg = json.loads(ws.recv(timeout=5))
            assert msg.get('type') != 'admin_data', \
                'Cookie=%r 的 WS 连接拿到了管理数据：%r' % (cookie, msg)
            assert msg.get('type') == 'error', (cookie, msg)
