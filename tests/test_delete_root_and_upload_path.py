# -*- coding: utf-8 -*-
"""LF-18 / LF-19 回归：空路径删除 + 上传 path 的契约。

**LF-18（严重）**：`POST /api/delete {"files": [""]}` 曾把**整个 `shared_files` 递归删光**，
还回 `200 {"success": true, "deleted": 1}`。链路：
`_normalize_rel_path('')` 返回 `''`（把"空"当合法）→ `handle_delete` 里 `''` 不是 `None`
所以不被拦 → `os.path.join(UPLOAD_DIR, '')` = **共享根本身** → `is_path_safe` 认为"在根内"
→ `os.path.isdir` 为真 → `shutil.rmtree(共享根)` → 计数 1。

本文件用**真实文件**钉住"拒绝 + 文件还在"—— 这是唯一能抓住灾难的断言。

**LF-19**：上传的 path **只能写在 query**（`?path=`，`files/api.py:562`）。写在 multipart
字段里会被静默忽略、落到共享根，却仍回 `saved: 1` —— 现在"**完全没有 path 参数**"直接 400
并说明正确写法，不再静默落到根。（`?path=` 空值仍然是合法语义 = 上传到共享根，前端就这么传。）

⚠️ **必须用独立实例**（自建数据根 + 独立端口）：修之前跑本文件会真的把数据根的
`shared_files` 删掉 —— 挂在会话级服务上会污染后面所有用例。
"""
import json
import os
import subprocess
import sys
import time

import httpx
import pytest

from conftest import PROJ_ROOT, login

PORT, WSPORT = 8107, 8108
BASE = 'http://127.0.0.1:%d' % PORT

# 空/畸形删除目标：LF-18 是第一个，其余是同类（原先只测了后 7 种，漏了空串）
BAD_DELETE_PATHS = ('', ' ', '/', '\\', '.', '..', './', '../', 'public/..', 'users/..',
                    './x', '../x', '/x', '//x', 'E:/x', '.\\x', 'a/../x')


@pytest.fixture(scope='module')
def del_server(data_root_factory):
    root = data_root_factory('delroot_')
    with open(os.path.join(root, 'config', 'server_config.json'), 'w', encoding='utf-8') as f:
        json.dump({'http_port': PORT, 'ws_port': WSPORT, 'tls_enabled': False,
                   'guest_mode': True, 'access_log': False, 'max_total_conns': 256}, f)
    with open(os.path.join(root, 'config', 'users.json'), 'w', encoding='utf-8') as f:
        json.dump({'admin': {'password': 'admin', 'role': 'super_admin'}}, f)
    env = dict(os.environ)
    env['LEAFFS_PROJECT_ROOT'] = root
    env['LEAFFS_NO_WEBVIEW'] = '1'
    logf = open(os.path.join(root, 'server.log'), 'wb', buffering=0)
    proc = subprocess.Popen([sys.executable, '-m', 'leaffs'], cwd=PROJ_ROOT, env=env,
                            stdout=logf, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError('服务提前退出')
            try:
                if httpx.get(BASE + '/api/ping', timeout=1.0).status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError('服务未就绪')
        yield root
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        logf.close()


@pytest.fixture(scope='module')
def admin(del_server):
    c = httpx.Client(base_url=BASE, timeout=30)
    login(c)
    yield c
    c.close()


def _shared(root):
    return os.path.join(root, 'shared_files')


def _plant(admin, root):
    """在共享根下造几个真文件（删除灾难的"证人"）"""
    admin.post('/api/upload?path=public', files={'file': ('x.txt', b'x')})
    admin.post('/api/upload?path=public/sub', files={'file': ('z.txt', b'z')})
    admin.post('/api/upload?path=users/admin', files={'file': ('y.txt', b'y')})
    for rel in ('public/x.txt', 'public/sub/z.txt', 'users/admin/y.txt'):
        assert os.path.isfile(os.path.join(_shared(root), rel)), '前置造数失败：%s' % rel


def _assert_data_alive(root, where):
    for rel in ('public/x.txt', 'public/sub/z.txt', 'users/admin/y.txt'):
        assert os.path.isfile(os.path.join(_shared(root), rel)), \
            '%s：%s 被删了！共享根内容不该被这个请求动' % (where, rel)
    assert os.path.isdir(_shared(root)), '%s：整个 shared_files 都没了' % where


def test_empty_path_delete_is_rejected_and_data_survives(admin, del_server):
    """LF-18：`{"files": [""]}` 必须被拒，且**一个文件都不能少**"""
    _plant(admin, del_server)
    r = admin.post('/api/delete', json={'files': ['']})
    assert r.status_code == 404, \
        '空路径删除没被拒（%s %s）—— 这正是删光共享根的那条' % (r.status_code, r.text)
    _assert_data_alive(del_server, 'LF-18')


def test_all_malformed_delete_paths_rejected_and_data_survives(admin, del_server):
    """同族全覆盖：每个畸形目标都要被拒，且每轮之后数据都还在"""
    _plant(admin, del_server)
    bad = []
    for p in BAD_DELETE_PATHS:
        r = admin.post('/api/delete', json={'files': [p]})
        if r.status_code not in (400, 404):
            bad.append('%r -> %s %s' % (p, r.status_code, r.text[:60]))
        try:
            _assert_data_alive(del_server, '目标 %r' % p)
        except AssertionError as e:
            bad.append(str(e))
    assert not bad, '这些畸形删除目标没被挡住：\n  ' + '\n  '.join(bad)


def test_upload_without_path_param_is_rejected_not_silently_rooted(admin, del_server):
    """LF-19：完全没有 `path` 参数 → 400（此前静默落到共享根还回 saved:1）"""
    before = set(os.listdir(_shared(del_server)))
    r = admin.post('/api/upload', files={'file': ('stray.txt', b's')})
    assert r.status_code == 400, \
        '不带 path 的上传没被拒（%s %s）—— 会静默落到共享根' % (r.status_code, r.text)
    assert 'path' in r.text, '错误提示要说明 path 该写在哪：%s' % r.text
    after = set(os.listdir(_shared(del_server)))
    assert not (after - before), '被拒的上传还是落盘了：%s' % (after - before)


def test_upload_path_in_query_still_works(admin, del_server):
    """回归：`?path=public` 照旧生效，别把正常上传改坏"""
    r = admin.post('/api/upload?path=public', files={'file': ('ok1.txt', b'1')})
    assert r.status_code == 200 and r.json().get('saved') == 1, r.text
    assert os.path.isfile(os.path.join(_shared(del_server), 'public', 'ok1.txt'))


def test_upload_with_empty_query_path_still_means_shared_root(admin, del_server):
    """`?path=`（空值）仍是合法语义 = 上传到共享根，管理员允许（前端就这么传）"""
    r = admin.post('/api/upload?path=', files={'file': ('rootfile.txt', b'r')})
    assert r.status_code == 200 and r.json().get('saved') == 1, r.text
    assert os.path.isfile(os.path.join(_shared(del_server), 'rootfile.txt'))


def test_normal_user_can_upload_to_own_dir(del_server, admin):
    """反证 LF-20：普通用户上传到自己的目录**是通的**（不是权限 bug）"""
    r = admin.post('/api/users/add', json={'username': 'u_probe',
                                           'password': 'pw-123456', 'role': 'user'})
    assert r.status_code == 200, r.text
    with httpx.Client(base_url=BASE, timeout=30) as u:
        lg = u.post('/api/auth/login', json={'username': 'u_probe', 'password': 'pw-123456'})
        assert lg.status_code == 200 and lg.json().get('success'), lg.text
        for p in ('users/u_probe', 'users/u_probe/', 'users/u_probe/sub'):
            rr = u.post('/api/upload?path=' + p, files={'file': ('mine.txt', b'm')})
            assert rr.status_code == 200 and rr.json().get('saved') == 1, (p, rr.status_code, rr.text)
        rr = u.post('/api/upload', files={'file': ('stray2.txt', b's')})
        assert rr.status_code == 400, '普通用户不带 path 应当被明确拒绝：%s' % rr.text
