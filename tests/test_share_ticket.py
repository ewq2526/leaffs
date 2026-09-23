# -*- coding: utf-8 -*-
"""分享码授权票据必须是"服务端签发的不透明随机值"，不能是码的哈希。

旧实现把 `code_hash(code)` 直接当 Cookie 值（`leaf_sh_<用户名>`），而那个哈希能由码
推算出来：攻击者离线枚举候选码 → 算出哈希 → 自己写一个同名 Cookie 就能下载，全程
不经过 `/api/share/auth`，IP 锁与全局锁一点都拦不到（Cookie 名同样能从公开的用户名算出来）。
这里把那几条路钉死。

码的粒度改成**每条分享**之后，Cookie 名是**固定常量** `access.SHARE_COOKIE`
（不再按用户名拼），值是一张票据 —— 一张票据累积多条分享的解锁，所以它不再是
"某个用户"的凭据，而是一串"已解锁哪几条"的记录。
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


def _publish(client, data_root):
    """登记 REL 并返回它的虚拟路径。

    先卸掉同名映射：映射与它们的标签都活在**服务端进程**里（session 级夹具共用一个
    服务端），而条目名撞车时会自动避让成 `ticketfile (1).txt` —— 不卸的话码就设到那条
    新条目上，用例却还在验老的虚拟路径。卸掉再发布，每条用例都拿到干净的一条。
    """
    _put(data_root, REL)
    client.post('/api/share/unpublish', json={'path': VIRTUAL})
    r = client.post('/api/share/publish', json={'paths': [REL]})
    assert r.status_code == 200 and r.json().get('success'), r.text
    return r.json()['published'][0]['path']


def _label_of(data_root, vp):
    """这条分享的**随机标签**（分享码按它索引，不再按用户名）。"""
    import leaffs.share.mappings as _m
    _m._MAPPINGS_FILE = os.path.join(data_root, 'config', 'share_mappings.json')
    _m._cache = None
    label, _owner = _m.access_scope(vp)
    return label


def _setup(client, data_root, code=CODE):
    """发布 REL 并给这一条设好码，返回 (虚拟路径, 标签)"""
    login(client)
    vp = _publish(client, data_root)
    assert vp == VIRTUAL, vp
    label = _label_of(data_root, vp)
    assert label, '登记一条分享时没有生成标签'
    r = client.post('/api/share/code', json={'path': vp, 'code': code})
    assert r.status_code == 200 and r.json().get('enabled') is True, r.text
    return vp, label


@pytest.fixture()
def shared(client, data_root):
    """发布一个文件并设好码；结束时清码，别影响后面的用例"""
    vp, label = _setup(client, data_root)
    yield client, vp, label
    client.post('/api/share/code', json={'path': vp, 'code': ''})


def test_ticket_is_opaque_and_http_only(shared):
    """正确码下发的 Cookie：不透明票据（≠ 码哈希）且带 HttpOnly"""
    from leaffs.share import access as _sacc
    client, vp, label = shared
    with httpx.Client(base_url=client.base_url, timeout=20) as anon:
        r = anon.post('/api/share/auth', json={'label': label, 'code': CODE})
        assert r.status_code == 200 and r.json().get('ok') is True, r.text
        sc = r.headers.get('set-cookie', '')
        assert 'HttpOnly' in sc, sc
        assert _sacc.SHARE_COOKIE + '=' in sc, sc
        value = anon.cookies.get(_sacc.SHARE_COOKIE)
        assert value, sc
        assert value != _sacc.code_hash(CODE), '授权票据不能是码的哈希'
        # 持票照常放行
        assert anon.get('/p/admin/api').status_code == 200
        dl = anon.get('/download/' + vp, follow_redirects=False)
        assert dl.status_code == 200 and dl.content == b'ticket-data', \
            (dl.status_code, dl.headers.get('location'), dict(anon.cookies), dl.text[:200])


def test_code_hash_as_cookie_is_rejected(shared):
    """把码哈希当 Cookie 塞进去必须无效 —— 这条正是旧实现能被绕过的地方"""
    from leaffs.share import access as _sacc
    client, vp, label = shared
    fake = '%s=%s' % (_sacc.SHARE_COOKIE, _sacc.code_hash(CODE))
    with httpx.Client(base_url=client.base_url, timeout=25,
                      headers={'Cookie': fake}) as anon:
        r = anon.get('/download/' + vp, follow_redirects=False)
        assert r.status_code == 302, r.status_code        # 被送回分享页输码
        # 列表也不再有页面级的 403：逐条判，没解锁的那条连名字都不给
        body = anon.get('/p/admin/api').json()
        assert {'label': label, 'locked': True} in body['files'], body
        assert 'ticketfile.txt' not in [f.get('name') for f in body['files']], body


def test_random_ticket_is_rejected(shared):
    from leaffs.share import access as _sacc
    client, vp, _label = shared
    fake = '%s=%s' % (_sacc.SHARE_COOKIE, 'not-a-real-ticket')
    with httpx.Client(base_url=client.base_url, timeout=20,
                      headers={'Cookie': fake}) as anon:
        r = anon.get('/download/' + vp, follow_redirects=False)
        assert r.status_code == 302, r.status_code


def test_share_code_min_length_is_six(client, data_root):
    """码长下限已从 4 提到 6：5 位拒绝、6 位通过"""
    login(client)
    vp = _publish(client, data_root)
    r = client.post('/api/share/code', json={'path': vp, 'code': 'abc12'})
    assert r.status_code == 400, r.text
    r = client.post('/api/share/code', json={'path': vp, 'code': 'abc123'})
    assert r.status_code == 200 and r.json().get('enabled') is True, r.text
    client.post('/api/share/code', json={'path': vp, 'code': ''})
