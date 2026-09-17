# -*- coding: utf-8 -*-
"""核心回归：登录/会话/权限门槛/上传建目录/删除/账号API/页面/WS"""
import os

import pytest

from conftest import login, guest_login, WS_PORT

wifi = 'wifi_session'


# ---------- 基础与登录 ----------

def test_ping(client):
    r = client.get('/api/ping')
    assert r.status_code == 200 and r.json().get('ok') is True


def test_admin_login_and_check(client):
    login(client, 'admin', 'admin')
    r = client.get('/api/auth/check')
    assert r.status_code == 200
    d = r.json()
    assert d.get('role') in ('admin', 'super_admin')


def test_bad_password_rejected(client):
    r = client.post('/api/auth/login', json={'username': 'admin', 'password': 'wrong-pass'})
    assert r.status_code == 403


def test_guest_login_and_check(client):
    guest_login(client)
    r = client.get('/api/auth/check')
    assert r.status_code == 200 and r.json().get('role') == 'guest'


# ---------- 权限门槛 ----------

def test_anon_admin_api_forbidden(client):
    r = client.get('/api/stats')
    assert r.status_code == 404, '统一拒绝口径：身份/权限拒绝一律 404'


def test_invalid_session_cookie_rejected(client):
    client.cookies.set(wifi, 'deadbeef' * 4, domain='127.0.0.1', path='/')
    r = client.get('/api/stats')
    assert r.status_code == 404, '带无效会话 → 统一拒绝口径 → 404'


def test_guest_cannot_call_users_api(client):
    guest_login(client)
    r = client.get('/api/users')
    assert r.status_code == 404, '统一拒绝口径：身份/权限拒绝一律 404'


def test_guest_cannot_delete(client):
    guest_login(client)
    r = client.post('/api/delete', json={'files': ['public/x.txt']})
    assert r.status_code == 404


# ---------- 上传 / 列表 / 删除 ----------

def test_upload_creates_missing_dirs(client, data_root):
    login(client)
    sub = 'users/admin/newdir_a/newdir_b'
    r = client.post('/api/upload?path=' + sub,
                    files={'file': ('hello.txt', b'hello-leaffs')})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get('success') is True and d.get('saved') == 1, d
    fp = os.path.join(data_root, 'shared_files', sub, 'hello.txt')
    assert os.path.isfile(fp)
    with open(fp, 'rb') as f:
        assert f.read() == b'hello-leaffs'


def test_guest_upload_public(client, data_root):
    guest_login(client)
    r = client.post('/api/upload?path=public',
                    files={'file': ('g.txt', b'guest-data')})
    assert r.status_code == 200, r.text
    assert r.json().get('success') is True
    assert os.path.isfile(os.path.join(data_root, 'shared_files', 'public', 'g.txt'))


def test_guest_upload_own_dir_forbidden(client):
    guest_login(client)
    r = client.post('/api/upload?path=users/admin',
                    files={'file': ('x.txt', b'x')})
    assert r.status_code == 404


def test_file_list_and_delete(client, data_root):
    login(client)
    r = client.post('/api/upload?path=public',
                    files={'file': ('delme.txt', b'del')})
    assert r.json().get('saved') == 1
    r = client.get('/api/files', params={'path': 'public'})
    assert r.status_code == 200
    names = [x['name'] for x in r.json().get('files', [])]
    assert 'delme.txt' in names
    r = client.post('/api/delete', json={'files': ['public/delme.txt']})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get('success') is True and d.get('deleted') == 1, d
    assert not os.path.exists(os.path.join(data_root, 'shared_files', 'public', 'delme.txt'))


# ---------- 账号 API ----------

def test_account_me_admin(client):
    login(client)
    r = client.get('/api/account/me')
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get('success') is True
    assert d.get('role') in ('admin', 'super_admin')
    assert 'quota' in d and 'sessions' in d and 'current' in d
    assert any(s.get('current') for s in d['sessions'])


def test_account_password_wrong_old(client):
    login(client)
    r = client.post('/api/account/password',
                    json={'old_password': 'nope', 'new_password': 'abcdefgh'})
    assert r.status_code == 403
    assert '原密码' in r.json().get('error', '') or 'password' in r.text


def test_account_password_guest_forbidden(client):
    guest_login(client)
    r = client.post('/api/account/password',
                    json={'old_password': 'x', 'new_password': 'yyyyyyyy'})
    assert r.status_code == 404, '游客改密是"身份不足"，按统一口径 404'


def test_account_revoke_other_session(server):
    import httpx
    c1 = httpx.Client(base_url=server, timeout=20.0)
    c2 = httpx.Client(base_url=server, timeout=20.0)
    try:
        login(c1)
        login(c2)
        r = c1.post('/api/account/revoke-sessions', json={})
        assert r.status_code == 200, r.text
        assert r.json().get('revoked') >= 1
        # c2 的会话已被踢：访问需登录接口应被拒（统一拒绝口径 → 404）
        r2 = c2.get('/api/stats')
        assert r2.status_code == 404, r2.status_code
        # c1 当前会话不受影响
        assert c1.get('/api/auth/check').status_code == 200
    finally:
        c1.close()
        c2.close()


# ---------- 页面渲染 ----------

def test_me_page_renders(client):
    login(client)
    r = client.get('/me')
    assert r.status_code == 200
    assert '__MY_ROLE__' not in r.text
    assert '我的' in r.text or 'My Account' in r.text


def test_guest_browse_public_page(client):
    guest_login(client)
    r = client.get('/browse/', follow_redirects=False)
    assert r.status_code in (200, 302)
    target = client.get('/browse/public')
    assert target.status_code == 200


# ---------- WebSocket ----------

def test_ws_ping_pong(client):
    from websockets.sync.client import connect
    guest_login(client)
    cookie = client.cookies.get(wifi)
    assert cookie
    with connect('ws://127.0.0.1:%d' % WS_PORT,
                 additional_headers={'Cookie': '%s=%s' % (wifi, cookie)}) as ws:
        ws.send('{"type":"ping"}')
        msg = ws.recv()
        assert '"pong"' in msg
