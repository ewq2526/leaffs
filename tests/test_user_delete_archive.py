# -*- coding: utf-8 -*-
"""删除用户时的家目录处理（LF-06 回归）。

黑盒报告的现象（已核实）：`auth/core.py` 的 `delete_user()` 只做三件事 ——
`del _users[username]`、`_save_users()`、踢掉该用户会话，**没有任何目录处理**；
而家目录是登录时按需建的（`makedirs(exist_ok=True)`）→ 于是**同名重建账号会原样继承
旧目录的全部内容**。对照：`/api/users/rename` 是搬目录的（还带失败回滚），所以 delete
这条纯属遗漏。

现在的口径（用户拍板）：删用户时把家目录**改名归档**到 `users/.deleted/<name>-<时间戳>/`，
**不删文件** —— 删账号是管理员一个操作，删目录却会永久毁掉那个用户的全部文件。
"""
import os

import httpx

from conftest import BASE_URL, login


def _arch_path(data_root, arch):
    return os.path.join(data_root, 'shared_files', *arch.split('/'))


def test_deleted_user_files_are_archived_not_destroyed(client, data_root):
    """删用户：账号没了，但文件被**归档**而不是删掉"""
    login(client)
    name = 'zzarch'
    r = client.post('/api/users/add',
                    json={'username': name, 'password': 'pw-123456', 'role': 'user'})
    assert r.status_code == 200, r.text
    r = client.post('/api/upload?path=users/' + name, files={'file': ('keep.txt', b'data')})
    assert r.json().get('saved') == 1, r.text
    src = os.path.join(data_root, 'shared_files', 'users', name, 'keep.txt')
    assert os.path.isfile(src)

    r = client.post('/api/users/delete', json={'username': name})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get('success') is True, d
    arch = d.get('archived_dir')
    assert arch and arch.startswith('users/.deleted/'), d
    assert not os.path.exists(src), '原位置应当已经搬走'
    assert os.path.isfile(os.path.join(_arch_path(data_root, arch), 'keep.txt')), \
        '文件必须被归档而不是销毁'


def test_recreated_user_does_not_inherit_old_files(client):
    """★ 核心：同名重建 → 新账号拿到的是**空目录**（原来会继承旧文件）"""
    login(client)
    name = 'zzrecreate'
    client.post('/api/users/add',
                json={'username': name, 'password': 'pw-123456', 'role': 'user'})
    client.post('/api/upload?path=users/' + name, files={'file': ('old.txt', b'old')})
    r = client.post('/api/users/delete', json={'username': name})
    assert r.json().get('success') is True, r.text

    r = client.post('/api/users/add',
                    json={'username': name, 'password': 'pw-123456', 'role': 'user'})
    assert r.status_code == 200, r.text
    with httpx.Client(base_url=BASE_URL, timeout=20.0) as uc:
        r = uc.post('/api/auth/login', json={'username': name, 'password': 'pw-123456'})
        assert r.status_code == 200, r.text
        r = uc.get('/api/files', params={'path': 'users/' + name})
        assert r.status_code == 200, r.text
        names = [f['name'] for f in r.json().get('files', [])]
    assert names == [], '同名重建继承了旧文件（LF-06 原样复发）：%r' % names


def test_deleting_a_user_with_no_files_is_fine(client):
    """从没登录过、没有家目录的用户照样能删（archived_dir 为 null，不算错）"""
    login(client)
    name = 'zznofile'
    client.post('/api/users/add',
                json={'username': name, 'password': 'pw-123456', 'role': 'user'})
    r = client.post('/api/users/delete', json={'username': name})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get('success') is True, d
    assert d.get('archived_dir') is None, d
