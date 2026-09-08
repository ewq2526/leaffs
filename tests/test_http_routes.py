# -*- coding: utf-8 -*-
"""HTTP GET 路由覆盖：页面/管理API/下载/原始内容/搜索（查表路由回归护栏）"""
import os

from conftest import login, guest_login

wifi = 'wifi_session'


def _upload(client, sub, name, data):
    r = client.post('/api/upload?path=' + sub, files={'file': (name, data)})
    assert r.status_code == 200 and r.json().get('saved') == 1, r.text


# ---------- 页面路由 ----------

def test_login_page_anon(client):
    r = client.get('/login')
    assert r.status_code == 200
    assert '登录' in r.text or 'Sign In' in r.text or 'login' in r.text.lower()


def test_me_redirects_when_anon(client):
    r = client.get('/me', follow_redirects=False)
    assert r.status_code == 302


def test_admin_page_forbidden_anon(client):
    r = client.get('/admin')
    assert r.status_code == 403


def test_admin_page_ok(client):
    login(client)
    r = client.get('/admin')
    assert r.status_code == 200
    assert '__MY_ROLE__' not in r.text


def test_admin_users_page_ok(client):
    login(client)
    r = client.get('/admin/users')
    assert r.status_code == 200


def test_downloader_pages_ok(client):
    login(client)
    assert client.get('/url-download').status_code == 200
    assert client.get('/url-download/peers').status_code == 200


# ---------- 管理/会话 GET API ----------

def test_admin_get_apis(client):
    login(client)
    for p in ('/api/config', '/api/config/advanced', '/api/config/deep',
              '/api/sessions', '/api/logs', '/api/users'):
        r = client.get(p)
        assert r.status_code == 200, (p, r.status_code, r.text[:200])


def test_session_sid(client):
    login(client)
    r = client.get('/api/session/sid')
    assert r.status_code == 200
    assert len(r.json().get('sid', '')) >= 10


# ---------- 文件内容路由 ----------

def test_upload_download_raw_roundtrip(client):
    login(client)
    data = os.urandom(4096)
    _upload(client, 'users/admin', 'round.bin', data)
    r = client.get('/download/users/admin/round.bin')
    assert r.status_code == 200
    assert r.content == data
    r2 = client.get('/api/raw', params={'path': 'users/admin/round.bin'})
    assert r2.status_code == 200
    assert r2.content == data


def test_search(client):
    login(client)
    _upload(client, 'users/admin', 'needle_abc.txt', b'x')
    r = client.get('/api/search', params={'q': 'needle_abc'})
    assert r.status_code == 200
    names = [f['name'] for f in r.json().get('files', [])]
    assert 'needle_abc.txt' in names


def test_guest_browse_public_list_api(client):
    guest_login(client)
    r = client.get('/api/files', params={'path': 'public'})
    assert r.status_code == 200
    assert 'files' in r.json()
