# -*- coding: utf-8 -*-
"""WS 认证：**只认握手 Cookie**。

历史上这里还有一条 `{type:'auth'}` 消息分支：
  · 最早认 `{type:'auth', sid}` —— 那个 sid 就是 HttpOnly cookie 里的长期会话凭据，
    把它交给页面（旧接口 `/api/session/sid`）等于绕开 HttpOnly（JS 能读走它）；
  · 后来改成只认"本机一次性令牌"（环回来源 + `token` 字段）；
  · **2026-09-16 整体删除** —— 全仓**没有任何发送方**（是死代码），
    而它还是"只查了环回、**漏查 Host**"的那条路（HTTP 的 `/login?leaf=` 两样都查）。
    本机令牌登录从此只剩 HTTP `/login?leaf=` 一条路。

所以现在：认证只由 WS 握手的 Cookie 完成，`{type:'auth'}` 不再有专属应答。
"""
import json

from conftest import login, WS_PORT

wifi = 'wifi_session'


def _connect(cookie=None):
    from websockets.sync.client import connect
    headers = {'Cookie': '%s=%s' % (wifi, cookie)} if cookie else None
    return connect('ws://127.0.0.1:%d' % WS_PORT, additional_headers=headers)


def test_ws_cookie_auth_can_subscribe_admin(client):
    """握手 Cookie 认证就够了：连上直接 admin-sub 就能收到管理推送"""
    login(client)
    cookie = client.cookies.get(wifi)
    assert cookie
    with _connect(cookie) as ws:
        ws.send(json.dumps({'type': 'admin-sub'}))
        msg = json.loads(ws.recv(timeout=5))
        assert msg.get('type') == 'admin_data', msg


def test_ws_auth_message_has_no_reply(client):
    """`{type:'auth'}` 已整体删除：**已认证连接**上它不再有专属应答

    这条测试的牙是**「不能出现 type 为 auth 的应答」** —— 若有人把那条分支加回来，
    这里会收到 `{'type':'auth',…}` 而红。

    2026-09-21 调整：链尾补了「未知消息类型」兜底分支之后，auth 不再被**静默**忽略，
    而是回一条 `{'type':'error'}`。所以判据从"第一条就是 pong"放宽为
    "不出现 auth 应答，且连接仍然可用（最终等到 pong）" —— 原来那种写法
    实际上是把"静默丢弃"当成了期望行为。
    """
    login(client)
    cookie = client.cookies.get(wifi)
    assert cookie
    with _connect(cookie) as ws:
        ws.send(json.dumps({'type': 'auth', 'sid': cookie}))
        ws.send(json.dumps({'type': 'ping'}))
        seen = []
        got_pong = False
        for _ in range(6):
            msg = json.loads(ws.recv(timeout=5))
            seen.append(msg.get('type'))
            assert msg.get('type') != 'auth', \
                'WS auth 分支应当已删除，却收到了 %r' % msg
            if msg.get('type') == 'pong':
                got_pong = True
                break
        assert got_pong, 'auth 之后连接不可用（没等到 pong），收到 %r' % seen


def test_ws_auth_message_from_anonymous_is_rejected(client):
    """匿名连接发 auth 按"白名单外消息"拒 —— 'auth' 已从 `_WS_ANON_ALLOWED` 移除"""
    with _connect() as ws:
        ws.send(json.dumps({'type': 'auth', 'token': 'x'}))
        msg = json.loads(ws.recv(timeout=5))
        assert msg.get('type') == 'error', \
            '匿名发 auth 应当被拒（白名单已去掉 auth），却收到 %r' % msg


def test_ws_anonymous_cannot_subscribe_admin(client):
    """未认证连接发 admin-sub 仍被拒 —— 换认证方式不能顺手放开这一面"""
    with _connect() as ws:
        ws.send(json.dumps({'type': 'admin-sub'}))
        msg = json.loads(ws.recv(timeout=5))
        assert msg.get('type') == 'error', msg
