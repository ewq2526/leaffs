# -*- coding: utf-8 -*-
"""会话 IP 绑定与 /api/admin/auto-login 的两条边界。

1) 来源 IP 不符只该拒绝**这一次**请求，不能顺手把会话删掉
   （旧写法 pop：任何人拿到别人的 sid、从别的 IP 打一次就能把对方踢下线）
2) /api/admin/auto-login 不该被"失效 Cookie 预检"提前 401
   —— 它本就靠一次性令牌建会话，和 /login 同规则
"""
import os

import httpx

from conftest import login

WIFI = 'wifi_session'


def test_ip_mismatch_does_not_kill_the_session(client):
    """同一个 sid 从别的 IP 打一次：那次被拒，原会话必须还活着"""
    login(client)
    sid = client.cookies.get(WIFI)
    assert sid, '登录后应拿到会话 Cookie'

    transport = httpx.HTTPTransport(local_address='127.0.0.2')
    with httpx.Client(base_url=client.base_url, transport=transport, timeout=20) as other:
        other.cookies.set(WIFI, sid)
        r = other.get('/api/account/me')
        assert r.status_code == 404, r.status_code   # 统一拒绝口径（原 401）

    # 原 IP 仍然有效（旧实现里会话已被上一个请求删掉 → 这里会是 404）
    r = client.get('/api/account/me')
    assert r.status_code == 200, r.text


def test_auto_login_accepts_stale_session_cookie(client, data_root):
    """带一条过期 wifi_session + 正确的一次性令牌 → 仍然建出超管会话"""
    tok_path = os.path.join(data_root, 'config', 'local_token.txt')
    with open(tok_path, 'r', encoding='utf-8') as f:
        tok = f.read().strip()
    assert tok, '服务端启动时应写入本机一次性令牌'

    with httpx.Client(base_url=client.base_url, timeout=20) as c:
        c.cookies.set(WIFI, 'stale-session-value')
        r = c.get('/api/admin/auto-login', params={'leaf': tok}, follow_redirects=False)
        assert r.status_code == 302, r.status_code
        assert r.headers.get('location') == '/browse/', r.headers.get('location')
        sc = r.headers.get('set-cookie', '')
        assert WIFI + '=' in sc and 'HttpOnly' in sc, sc


def test_auto_login_still_rejects_bad_token(client):
    """加白名单不等于放开：令牌不对照样只送去登录页"""
    with httpx.Client(base_url=client.base_url, timeout=20) as c:
        c.cookies.set(WIFI, 'stale-session-value')
        r = c.get('/api/admin/auto-login', params={'leaf': 'not-the-token'},
                  follow_redirects=False)
        assert r.status_code == 302, r.status_code
        assert r.headers.get('location') == '/login', r.headers.get('location')
