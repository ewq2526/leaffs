# -*- coding: utf-8 -*-
"""搜索默认基路径（回归）：游客必须落到 public/，不能按"用户名空不空"推

背景：`files/api.py` 的 `search_files` 原来这么推默认基路径——

    base_path = f'users/{username}' if username else 'public'

而游客会话的 username 是 `游客`（非空，见 `auth/login_api.py` 的
`create_session('游客', 'guest')`），于是推出 `users/游客`；
`check_path_permission_core` 对 guest 只认 public → 403。
前端搜索**不传 base**（`web_page/home/home.html` 的 `/api/search?q=`），
所以游客在浏览页搜索从来都是失败。

本文件锁住三件事：游客默认搜 public、普通用户仍只搜自己目录、
显式传 base 与管理员全站的语义都没变。
"""
import os

import httpx

from conftest import BASE_URL, login


def _put(data_root, rel, content=b'data'):
    full = os.path.join(data_root, 'shared_files', rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'wb') as f:
        f.write(content)
    return full


def _client_at(ip):
    """指定来源 IP 的客户端：搜索是 6/60s、游客登录是 10/min 的 per-IP 限流，
    与其它用例共用 127.0.0.1 会互相踩 429 —— 各自用各自的回环地址。"""
    return httpx.Client(base_url=BASE_URL,
                        transport=httpx.HTTPTransport(local_address=ip),
                        timeout=20.0)


def test_guest_search_defaults_to_public(data_root, server):
    """游客不传 base → 200，且结果只可能来自 public/（回归：曾恒 403）

    显式声明 `server`：本用例用自带来源 IP 的客户端，不经过 `client`，
    不声明的话服务不会启动（fixture 无人请求就不跑）。
    """
    _put(data_root, 'public/gfind_aa.txt', b'public-side')
    _put(data_root, 'users/admin/gfind_bb.txt', b'private-side')
    with _client_at('127.0.0.2') as gc:
        r = gc.post('/api/guest/login')
        assert r.status_code == 200, r.text
        r = gc.get('/api/search', params={'q': 'gfind_'})
        assert r.status_code == 200, '游客搜索必须可用：%s' % r.text
        d = r.json()
    paths = [f['path'] for f in d['files']]
    assert 'public/gfind_aa.txt' in paths, d
    assert not [p for p in paths if p.startswith('users/')], \
        '游客不能搜到 users/ 下的东西：%s' % paths


def test_user_search_stays_in_own_dir(data_root, client):
    """普通用户不传 base → 仍只搜自己的目录（行为不变）"""
    login(client)
    r = client.post('/api/users/add',
                    json={'username': 'search_probe_user',
                          'password': 'pw-123456', 'role': 'user'})
    assert r.status_code == 200, r.text
    _put(data_root, 'users/search_probe_user/sfind_aa.txt', b'mine')
    _put(data_root, 'public/sfind_bb.txt', b'public-side')
    with _client_at('127.0.0.3') as uc:
        r = uc.post('/api/auth/login',
                    json={'username': 'search_probe_user', 'password': 'pw-123456'})
        assert r.status_code == 200, r.text
        r = uc.get('/api/search', params={'q': 'sfind_'})
        assert r.status_code == 200, r.text
        paths = [f['path'] for f in r.json()['files']]
    assert 'users/search_probe_user/sfind_aa.txt' in paths, paths
    assert 'public/sfind_bb.txt' not in paths, \
        '普通用户的默认搜索范围是自己的目录：%s' % paths


def test_guest_explicit_public_base(data_root, server):
    """游客显式 base=public 与不传等价（回归）"""
    _put(data_root, 'public/gfind_cc.txt', b'x')
    with _client_at('127.0.0.4') as gc:
        r = gc.post('/api/guest/login')
        assert r.status_code == 200, r.text
        r = gc.get('/api/search', params={'q': 'gfind_cc', 'base': 'public'})
        assert r.status_code == 200, r.text
        paths = [f['path'] for f in r.json()['files']]
    assert 'public/gfind_cc.txt' in paths, paths


def test_guest_out_of_scope_base_denied(data_root, server):
    """游客显式传越界 base → 被拒（范围之外是"拦下"，不是"搜不到"）

    前端不会传这个，但接口是公开的：直接构造请求必须被拒。
    含 `users/游客`——修复前默认推出来的那个值，同样必须被拒。
    拒绝码是 404（统一拒绝口径，2026-09-15）。
    """
    _put(data_root, 'users/admin/gfind_dd.txt', b'private-side')
    with _client_at('127.0.0.5') as gc:
        r = gc.post('/api/guest/login')
        assert r.status_code == 200, r.text
        for bad in ('users/admin', 'users/游客', 'users'):
            r = gc.get('/api/search', params={'q': 'gfind_', 'base': bad})
            assert r.status_code == 404, \
                '游客搜 %r 必须被拒（而不是静默搜不到）：%s' % (bad, r.text)


def test_admin_search_still_whole_tree(data_root, client):
    """管理员不传 base → 仍搜全站（含 users/ 与 public/）"""
    _put(data_root, 'users/admin/afind_aa.txt', b'a')
    _put(data_root, 'public/afind_bb.txt', b'b')
    login(client)
    r = client.get('/api/search', params={'q': 'afind_'})
    assert r.status_code == 200, r.text
    paths = [f['path'] for f in r.json()['files']]
    assert 'public/afind_bb.txt' in paths, paths
    assert 'users/admin/afind_aa.txt' in paths, paths
