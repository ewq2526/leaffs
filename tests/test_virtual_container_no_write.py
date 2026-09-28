# -*- coding: utf-8 -*-
"""虚拟容器（`public/mounts` / `public/shares`）里不许新增真实文件。

**来由（2026-09-27 实测，用普通用户账号）**：这两个容器在磁盘上只是空壳，里面的条目全由
`share/mappings.py` 登记。但当时**能往容器里上传真实文件**，而列表只显示映射条目，于是：
* 那个文件**三方（自己/管理员/匿名）列表都看不见** ⇒ 网页上根本选不中、删不掉，
  只能上服务器按路径删；而目录统计**照算它**（"空的却占 14 字节"）；
* 名字若撞上挂载点，还会跟挂载条目纠缠（同款现象见 M-1 那笔：实体目录被挂载点遮蔽）。

所以规矩定成：**容器本身全拒（含删除 —— 删了挂载区/分享区就没了）；容器内部只许删、
不许新增**（"只许删"这一条是给已经存在的隐藏文件留的清理出口，别一起堵死）。

HTTP 与 WS 两条写策略必须同一口径（`write_allowed` / `ws_write_allowed`），
下载器的落点也按同一判据拒（`dl_api.handle_start`）。
"""
import os
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def _handler(role):
    """假 handler：写策略里只用到 `_get_effective_role()`"""
    return SimpleNamespace(_get_effective_role=lambda: role)


# ---------- 1. 写策略本身（单元级：两条路径 + 三种角色） ----------

@pytest.mark.parametrize('role', ['user', 'admin', 'super_admin', 'guest'])
def test_http_policy_blocks_new_files_inside_containers(role):
    from leaffs.files.api import write_allowed
    h = _handler(role)
    for container in ('public/mounts', 'public/shares'):
        for op in ('upload', 'mkdir', 'overwrite'):
            assert write_allowed(h, container, op) is False, (role, container, op)
            assert write_allowed(h, container + '/x/y.txt', op) is False, (role, container, op)
        # 容器本身不许删
        assert write_allowed(h, container, 'delete') is False, (role, container)
    # 容器**内部**的删除要放行（清既有隐藏文件的唯一出口）——交给角色规则继续判
    assert write_allowed(_handler('user'), 'public/mounts/hidden.txt', 'delete') is True
    assert write_allowed(_handler('admin'), 'public/shares/u/hidden.txt', 'delete') is True
    # 别把正常写入一起堵了：容器之外照旧
    assert write_allowed(_handler('user'), 'public/ok.txt', 'upload') is True
    assert write_allowed(_handler('user'), 'public/ok', 'mkdir') is True


@pytest.mark.parametrize('role', ['user', 'admin', 'super_admin', 'guest'])
def test_ws_policy_same_rules(role):
    from leaffs.files.api import ws_write_allowed
    for container in ('public/mounts', 'public/shares'):
        assert ws_write_allowed(role, 'x', container, 'upload') is False
        assert ws_write_allowed(role, 'x', container, 'mkdir') is False
        assert ws_write_allowed(role, 'x', container, 'delete') is False
        assert ws_write_allowed(role, 'x', container + '/inner.txt', 'upload') is False
        assert ws_write_allowed(role, 'x', container + '/inner.txt', 'delete') is True or role == 'guest'
    assert ws_write_allowed('user', 'x', 'public/ok.txt', 'upload') is True


def test_both_policies_agree():
    """HTTP 与 WS 两条写策略不许漂移（同角色、同路径、同动作）"""
    from leaffs.files.api import write_allowed, ws_write_allowed
    for role in ('user', 'guest', 'admin'):
        for path in ('public/mounts', 'public/mounts/a.txt', 'public/shares',
                     'public/shares/u/a.txt', 'public/ok.txt', 'users/admin/a.txt'):
            for op in ('upload', 'mkdir', 'delete'):
                a = write_allowed(_handler(role), path, op)
                b = ws_write_allowed(role, 'who', path, op)
                assert a == b, 'HTTP/WS 写策略不一致：%s %s %s -> %s / %s' % (role, path, op, a, b)


# ---------- 2. 端到端（真服务） ----------

def _login_admin(client):
    r = client.post('/api/auth/login', json={'username': 'admin', 'password': 'admin'})
    assert r.status_code == 200, r.text


def test_upload_into_container_refused_but_public_still_works(client):
    """上传：容器本身与容器内部都拒；`public/` 下照旧放行（对照）"""
    _login_admin(client)
    for path in ('public/mounts', 'public/mounts/sub', 'public/shares', 'public/shares/admin'):
        r = client.post('/api/upload', params={'path': path},
                        files={'file': ('plant.txt', b'X' * 8)})
        body = r.json() if r.headers.get('content-type', '').startswith('application/json') else {}
        assert not body.get('success'), '%s 竟然收下了上传：%s' % (path, r.text)
    # 对照：正常目录不受影响
    r = client.post('/api/upload', params={'path': 'public'},
                    files={'file': ('normal_ok.txt', b'Y' * 8)})
    assert r.status_code == 200 and r.json().get('success'), r.text
    client.post('/api/delete', json={'files': ['public/normal_ok.txt']})


def test_mkdir_into_container_refused(client, data_root):
    """建目录：容器本身就是父目录时拒（否则能造出与挂载点同名的实体目录）"""
    _login_admin(client)
    r = client.post('/api/mkdir', json={'path': 'public/mounts', 'name': 'shadow'})
    assert r.status_code in (403, 404), r.text
    assert '无权限' in (r.json().get('error') or ''), r.text
    assert not os.path.exists(os.path.join(data_root, 'shared_files', 'public',
                                           'mounts', 'shadow'))


def test_container_itself_cannot_be_deleted_but_inner_junk_can(client, data_root):
    """容器本身不许删；容器里**已经存在**的文件必须还能删（清理出口）"""
    _login_admin(client)
    r = client.post('/api/delete', json={'files': ['public/mounts']})
    assert r.status_code in (403, 404), r.text
    r = client.post('/api/delete', json={'files': ['public/shares']})
    assert r.status_code in (403, 404), r.text
    # 造一个"历史上混进去的"真实文件，确认删得掉
    junk = os.path.join(data_root, 'shared_files', 'public', 'mounts', 'legacy_junk.txt')
    with open(junk, 'wb') as f:
        f.write(b'junk')
    r = client.post('/api/delete', json={'files': ['public/mounts/legacy_junk.txt']})
    assert r.status_code == 200 and r.json().get('deleted') == 1, r.text
    assert not os.path.exists(junk)


def test_url_downloader_cannot_target_container(client):
    """下载器落点也不能指进容器（与 upload/mkdir 同一口径）"""
    _login_admin(client)
    for sd in ('public/mounts', 'public/mounts/sub', 'public/shares'):
        r = client.post('/api/url-download/start',
                        json={'url': 'https://example.com/x.txt', 'save_dir': sd})
        body = r.json() if r.headers.get('content-type', '').startswith('application/json') else {}
        assert not body.get('success'), '%s 竟然收下了下载任务：%s' % (sd, r.text)
        assert '映射区' in (body.get('error') or ''), r.text
