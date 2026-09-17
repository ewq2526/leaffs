# -*- coding: utf-8 -*-
"""删用户的级联清理：分享码与分享映射必须一并清掉（T-1）。

**问题**（外部测试者发现）：删用户只清了「账号 + 家目录归档 + 会话」，
**没清 `share_access.json` 与 `share_mappings.json` 里该用户的记录**。后果不是"残留"那么轻：

1. **重建同名账号会继承旧访问码哈希** → 新用户的分享页**仍然要求输码**，而**没人知道那个码**
   （旧用户设的）→ 他的分享直接废掉，且界面上看不出原因；
2. **继承锁定计数** → 新账号一上来可能就是"已锁定"状态；
3. 旧映射条目还留着。

（已盘清：按用户名持久化的**只有这两个文件** —— 下载器任务不落盘，
`.cache/thumbs`（按源路径）与 `.cache/folder_sizes`（按路径）会自愈，都不需要处理。）
"""
import json
import os

import httpx

from conftest import login

U = 'cas_probe'
PW = 'pw-123456'
CODE = 'abc123'


def _cfg(data_root, name):
    with open(os.path.join(data_root, 'config', name), encoding='utf-8') as f:
        return json.load(f)


def _make_user_with_share(client, name=U):
    """建用户 → 他设自己的分享码、发布自己的文件"""
    r = client.post('/api/users/add', json={'username': name, 'password': PW, 'role': 'user'})
    assert r.status_code == 200 and r.json().get('success'), r.text
    with httpx.Client(base_url=client.base_url, timeout=20) as u:
        lg = u.post('/api/auth/login', json={'username': name, 'password': PW})
        assert lg.status_code == 200 and lg.json().get('success'), lg.text
        up = u.post('/api/upload?path=users/' + name, files={'file': ('f.txt', b'x')})
        assert up.status_code == 200 and up.json().get('saved') == 1, up.text
        r = u.post('/api/share/code', json={'code': CODE})
        assert r.status_code == 200 and r.json().get('enabled') is True, r.text
        r = u.post('/api/share/publish', json={'paths': ['users/%s/f.txt' % name]})
        assert r.status_code == 200 and r.json().get('success'), r.text


def test_delete_user_purges_share_code_and_mappings(client, data_root):
    login(client)
    _make_user_with_share(client)

    # 删之前：两处都该有记录（否则这条用例没验到东西）
    sa = _cfg(data_root, 'share_access.json')
    assert U in (sa.get('users') or {}), '前置条件不成立：分享码没落盘'
    sm = _cfg(data_root, 'share_mappings.json')
    assert any((m or {}).get('by') == U for m in sm.values()), '前置条件不成立：映射没落盘'

    r = client.post('/api/users/delete', json={'username': U})
    assert r.status_code == 200 and r.json().get('success'), r.text

    sa = _cfg(data_root, 'share_access.json')
    assert U not in (sa.get('users') or {}), \
        'share_access.json 里仍残留已删用户（会继承旧码/锁定状态）'
    sm = _cfg(data_root, 'share_mappings.json')
    assert not any((m or {}).get('by') == U for m in sm.values()), \
        'share_mappings.json 里仍残留已删用户的映射'


def test_recreated_same_name_does_not_inherit_old_code(client, data_root):
    """**核心回归**：删完重建同名账号 → 不再要求输码、没有映射、未被锁定

    修之前：新账号继承旧 `code_hash` → 分享页 403 `code_required`（而没人知道那个码）。
    """
    login(client)
    _make_user_with_share(client)
    r = client.post('/api/users/delete', json={'username': U})
    assert r.status_code == 200 and r.json().get('success'), r.text

    # 重建同名账号
    r = client.post('/api/users/add', json={'username': U, 'password': PW, 'role': 'user'})
    assert r.status_code == 200 and r.json().get('success'), r.text

    with httpx.Client(base_url=client.base_url, timeout=20) as anon:
        r = anon.get('/p/%s/api' % U)
        assert r.status_code == 200, \
            '重建同名账号后分享页仍要求输码（继承了旧码，没人知道它）：%s %s' % (
                r.status_code, r.text[:120])
        assert r.json().get('files') == [], r.text

    # 该账号自己也应当看不到任何旧映射
    with httpx.Client(base_url=client.base_url, timeout=20) as u:
        lg = u.post('/api/auth/login', json={'username': U, 'password': PW})
        assert lg.status_code == 200 and lg.json().get('success'), lg.text
        r = u.get('/api/share')
        assert r.status_code == 200, r.text
        assert r.json().get('mappings') == [], \
            '新账号继承了旧映射：%r' % r.json().get('mappings')

    # 收尾：把重建的账号也删掉，免得给后面的用例留下同名账号
    r = client.post('/api/users/delete', json={'username': U})
    assert r.status_code == 200 and r.json().get('success'), r.text


def test_other_users_are_not_touched(client, data_root):
    """清一个用户不能顺手清掉别人的分享码与映射"""
    login(client)
    other = 'cas_probe2'
    r = client.post('/api/users/add', json={'username': other, 'password': PW, 'role': 'user'})
    assert r.status_code == 200 and r.json().get('success'), r.text
    with httpx.Client(base_url=client.base_url, timeout=20) as u:
        u.post('/api/auth/login', json={'username': other, 'password': PW})
        u.post('/api/upload?path=users/' + other, files={'file': ('o.txt', b'o')})
        assert u.post('/api/share/code', json={'code': 'other123'}).status_code == 200
        assert u.post('/api/share/publish',
                      json={'paths': ['users/%s/o.txt' % other]}).json().get('success')
    _make_user_with_share(client)

    r = client.post('/api/users/delete', json={'username': U})
    assert r.status_code == 200 and r.json().get('success'), r.text

    sa = _cfg(data_root, 'share_access.json')
    assert other in (sa.get('users') or {}), '把别的用户的分享码一起清了'
    sm = _cfg(data_root, 'share_mappings.json')
    assert any((m or {}).get('by') == other for m in sm.values()), '把别的用户的映射一起清了'
