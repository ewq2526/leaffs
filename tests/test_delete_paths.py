# -*- coding: utf-8 -*-
"""`/api/delete` 的路径判定（LF-01 回归）。

黑盒报告的现象（已核实，并已用它自己的 `delmatrix_rs.txt` 对证）：

    匿名 {"files": ["zzabsent"]}   -> 403 无权限删除
    匿名 {"files": ["./zzabsent"]} -> 200 {"success": true, "deleted": 0}   ← 判定被跳过

根因不是报告说的 `path.find('/')`（那个写法全仓不存在），而是：
`_normalize_rel_path()` 对 `./x`、`../x`、`/x`、`C:/x` 一律返回 `None` → 循环里直接 `continue`
→ **整段判定（write_allowed + _check_path_permission）被跳过** → 收尾只看
`deleted == 0 and skipped_permission > 0`，而非法路径没被计数 → 落到 `200 success`。

报告另外那句"任何含 `/` 的合法路径都删不掉、管理员同样中招"是**错的**：
`tests/test_smoke_core.py` 删 `public/delme.txt` 一直是绿的。

⚠️ 游客用例走**独立源 IP**：游客登录限速是按来源 IP 计 10 次/分钟，而整个测试 session
共用一个服务进程 —— 挤占 127.0.0.1 的桶会把后面排队跑 guest 的用例顶成 429（踩过一次）。
"""
import httpx
import os

from conftest import BASE_URL, login

# 报告里那张判定表用过的形态（都是 `_normalize_rel_path` 判为非法的那批）
_BAD_PATHS = ('./zzabsent', '../zzabsent', '/zzabsent', '//zzabsent',
              'E:/zzabsent', '.\\zzabsent', 'a/../zzabsent')


def _guest_client(ip='127.0.0.6'):
    """独立来源 IP 的客户端：不占 127.0.0.1 的游客登录限速桶"""
    return httpx.Client(base_url=BASE_URL,
                        transport=httpx.HTTPTransport(local_address=ip),
                        timeout=20.0)


def test_anonymous_paths_are_always_denied(client):
    """匿名删除：畸形路径与普通路径都必须被拒（畸形那条原来会变成 `200 success`）。

    统一拒绝口径（2026-09-15）后拒绝码是 404，不再是 403。
    """
    with _guest_client() as gc:
        r = gc.post('/api/guest/login')
        assert r.status_code == 200, r.text
        r = gc.post('/api/delete', json={'files': ['zzabsent']})
        assert r.status_code == 404, '对照项（这条一直是对的）：%s' % r.text
        for bad in _BAD_PATHS:
            r = gc.post('/api/delete', json={'files': [bad]})
            assert r.status_code == 404, '%r -> %s %s' % (bad, r.status_code, r.text)


def test_admin_bad_path_is_not_a_silent_success(client):
    """管理员发畸形路径：也要被拒（404），而不是 `200 success: deleted 0` 这种谎报"""
    login(client)
    for bad in _BAD_PATHS:
        r = client.post('/api/delete', json={'files': [bad]})
        assert r.status_code == 404, '%r -> %s %s' % (bad, r.status_code, r.text)


def test_admin_deletes_nested_file_as_before(client, data_root):
    """回归：含 `/` 的**合法**路径照删不误（报告说这条坏了，实测是好的）"""
    login(client)
    sub = 'users/admin/deldir'
    r = client.post('/api/upload?path=' + sub, files={'file': ('gone.txt', b'x')})
    assert r.json().get('saved') == 1, r.text
    full = os.path.join(data_root, 'shared_files', sub, 'gone.txt')
    assert os.path.isfile(full)

    r = client.post('/api/delete', json={'files': [sub + '/gone.txt']})
    assert r.status_code == 200, r.text
    assert r.json().get('deleted') == 1, r.text
    assert not os.path.exists(full), '合法路径没删掉'


def test_mixed_request_reports_skipped(client, data_root):
    """混合请求：合法项删掉 → 200，且如实回报被拒的数量"""
    login(client)
    sub = 'users/admin/mixdir'
    r = client.post('/api/upload?path=' + sub, files={'file': ('keepme.txt', b'x')})
    assert r.json().get('saved') == 1, r.text
    full = os.path.join(data_root, 'shared_files', sub, 'keepme.txt')

    r = client.post('/api/delete', json={'files': [sub + '/keepme.txt', './zzabsent']})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get('deleted') == 1, d
    assert d.get('skipped_permission') == 1, d
    assert not os.path.exists(full)
