# -*- coding: utf-8 -*-
"""分享码授权票据必须是"服务端签发的不透明随机值"，不能是码的哈希。

旧实现把 `code_hash(code)` 直接当 Cookie 值（`leaf_sh_<用户名>`），而那个哈希能由码
推算出来：攻击者离线枚举候选码 → 算出哈希 → 自己写一个同名 Cookie 就能下载，全程
不经过 `/api/share/auth`，IP 锁与全局锁一点都拦不到（Cookie 名同样能从公开的用户名算出来）。
这里把那几条路钉死。
"""
import os

import httpx
import pytest

from conftest import login

CODE = 'abc123'
REL = 'users/admin/ticketfile.txt'
VIRTUAL = 'public/shares/admin/ticketfile.txt'


def _put(data_root, rel, content=b'ticket-data'):
    full = os.path.join(data_root, 'shared_files', rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'wb') as f:
        f.write(content)
    return full


def _setup(client, data_root, code=CODE):
    _put(data_root, REL)
    login(client)
    r = client.post('/api/share/publish', json={'paths': [REL]})
    assert r.status_code == 200 and r.json().get('success'), r.text
    r = client.post('/api/share/code', json={'code': code})
    assert r.status_code == 200 and r.json().get('enabled') is True, r.text


@pytest.fixture()
def shared(client, data_root):
    """发布一个文件并设好码；结束时清码，别影响后面的用例"""
    _setup(client, data_root)
    yield client
    client.post('/api/share/code', json={'code': ''})


def test_ticket_is_opaque_and_http_only(shared):
    """正确码下发的 Cookie：不透明票据（≠ 码哈希）且带 HttpOnly"""
    from leaffs.share import access as _sacc
    with httpx.Client(base_url=shared.base_url, timeout=20) as anon:
        r = anon.post('/api/share/auth', json={'username': 'admin', 'code': CODE})
        assert r.status_code == 200 and r.json().get('ok') is True, r.text
        sc = r.headers.get('set-cookie', '')
        assert 'HttpOnly' in sc, sc
        value = anon.cookies.get(_sacc.cookie_name('admin'))
        assert value, sc
        assert value != _sacc.code_hash(CODE), '授权票据不能是码的哈希'
        # 持票照常放行
        assert anon.get('/p/admin/api').status_code == 200
        dl = anon.get('/download/' + VIRTUAL)
        assert dl.status_code == 200 and dl.content == b'ticket-data'


def test_code_hash_as_cookie_is_rejected(shared):
    """把码哈希当 Cookie 塞进去必须无效 —— 这条正是旧实现能被绕过的地方"""
    from leaffs.share import access as _sacc
    fake = '%s=%s' % (_sacc.cookie_name('admin'), _sacc.code_hash(CODE))
    with httpx.Client(base_url=shared.base_url, timeout=20,
                      headers={'Cookie': fake}) as anon:
        r = anon.get('/download/' + VIRTUAL, follow_redirects=False)
        assert r.status_code == 302, r.status_code        # 被送回分享页输码
        assert anon.get('/p/admin/api').status_code == 403


def test_random_ticket_is_rejected(shared):
    from leaffs.share import access as _sacc
    fake = '%s=%s' % (_sacc.cookie_name('admin'), 'not-a-real-ticket')
    with httpx.Client(base_url=shared.base_url, timeout=20,
                      headers={'Cookie': fake}) as anon:
        r = anon.get('/download/' + VIRTUAL, follow_redirects=False)
        assert r.status_code == 302, r.status_code


def test_share_code_min_length_is_six(client, data_root):
    """码长下限已从 4 提到 6：5 位拒绝、6 位通过"""
    login(client)
    r = client.post('/api/share/code', json={'code': 'abc12'})
    assert r.status_code == 400, r.text
    r = client.post('/api/share/code', json={'code': 'abc123'})
    assert r.status_code == 200 and r.json().get('enabled') is True, r.text
    client.post('/api/share/code', json={'code': ''})
