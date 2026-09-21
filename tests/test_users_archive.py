# -*- coding: utf-8 -*-
"""已删除用户归档的清单与清理入口（N-7）。

黑盒报告 N-7（2026-09-21）：删用户只把家目录**改名归档**到 `users/.deleted/<名字>-<时间戳>/`，
数据留在那里只增不减；归档本身权限正确、同名碰撞处理正确，但**有没有清理机制未验证**。

查下来：归档是**有意**的设计（`_archive_user_dir` 的注释写明理由 —— 删账号是管理员
一个操作，删目录却会永久毁掉那个用户的全部文件），确实没有任何清理机制，管理员只能
自己摸到浏览页输入 `users/.deleted` 去删。所以补的是一个**看得见的入口**，不是改归档行为。

本文件钉住：
  * 清单如实反映磁盘上的归档（含文件数与占用）
  * 删除是真删，且**只能删归档根下的单层目录**（清理入口不能变成任意目录删除器）
  * 删掉之后 `users` 的聚合统计跟着更新（缓存失效挂在删除点上）
"""
import io
import os

import httpx
import pytest

from conftest import BASE_URL, login

PW = 'TestPass123!'


def _make_archive(client, name, payload=b'archive me'):
    """建用户 → 传一个文件 → 删用户（产生归档），返回归档目录名"""
    r = client.post('/api/users/add', json={'username': name, 'password': PW, 'role': 'user'})
    assert r.status_code == 200, r.text
    r = client.post('/api/upload?path=users/' + name, files={'file': ('keep.txt', payload)})
    assert r.status_code == 200, r.text
    r = client.post('/api/users/delete', json={'username': name})
    assert r.status_code == 200 and r.json().get('success'), r.text
    archived = r.json().get('archived_dir')
    assert archived, '删用户没有产生归档目录，这条测试就没有对象可测'
    return os.path.basename(archived)


def _archive_names(client):
    r = client.get('/api/users/archive')
    assert r.status_code == 200, r.text
    return [a['name'] for a in r.json()['archives']]


def test_archive_list_reflects_the_disk(client):
    login(client)
    entry = _make_archive(client, 'arch_list_user', b'0123456789')

    r = client.get('/api/users/archive')
    assert r.status_code == 200, r.text
    items = {a['name']: a for a in r.json()['archives']}
    assert entry in items, '归档没出现在清单里：%s' % list(items)

    item = items[entry]
    assert item['files'] == 1, item
    assert item['size'] == 10, item
    assert item['mtime'] > 0, item


def test_delete_one_archive_really_removes_it(client):
    login(client)
    entry = _make_archive(client, 'arch_del_user')
    assert entry in _archive_names(client)

    r = client.post('/api/users/archive/delete', json={'names': [entry]})
    assert r.status_code == 200, r.text
    assert r.json().get('success') is True, r.text
    assert r.json().get('deleted') == 1, r.text

    assert entry not in _archive_names(client), '接口说删了，清单里还在'


def test_delete_all_archives(client):
    login(client)
    _make_archive(client, 'arch_all_a')
    _make_archive(client, 'arch_all_b')
    assert _archive_names(client), '清空前应当至少有一个归档'

    r = client.post('/api/users/archive/delete', json={'all': True})
    assert r.status_code == 200, r.text
    assert r.json().get('success') is True, r.text
    assert r.json().get('deleted') >= 1, r.text

    assert _archive_names(client) == [], '清空之后清单不为空'


@pytest.fixture(scope='module')
def guard_user(server):
    """受害者用户（含一个文件）：供"越界删除"用例反复引用。

    那个用例是 parametrize 的，**不能**在每次调用里建同名用户 —— 数据根是整个
    session 共用的，第二次就会撞 "Username exists"。
    """
    with httpx.Client(base_url=BASE_URL, timeout=20.0, verify=False) as c:
        login(c)
        name = 'arch_guard_user'
        c.post('/api/users/add', json={'username': name, 'password': PW, 'role': 'user'})
        c.post('/api/upload?path=users/' + name, files={'file': ('v.txt', b'v')})
    return name


@pytest.mark.parametrize('bad', [
    '..', '../..', '.', '', 'a/b', '...\\x', '/abs', 'C:/x',
])
def test_cleanup_can_only_reach_inside_the_archive_root(client, guard_user, bad):
    """清理入口不是任意目录删除器：只接受归档根下的**单层目录名**"""
    login(client)

    r = client.post('/api/users/archive/delete', json={'names': [bad]})
    assert r.status_code == 400, '%r 被当成合法归档名接受了：%s' % (bad, r.text)
    assert r.json().get('deleted') == 0, r.text

    got = client.get('/api/files', params={'path': 'users/' + guard_user})
    assert got.status_code == 200, got.text
    assert 'v.txt' in [f['name'] for f in got.json()['files']], \
        '越界删除：受害者的家目录被动了（bad=%r）' % (bad,)


def test_missing_names_is_rejected(client):
    """什么都不指定就报错，不能当成"删全部"（那正是 N-10/WS-D 那类假成功）"""
    login(client)
    for body in ({}, {'names': []}, {'names': 'x'}, {'all': False}):
        r = client.post('/api/users/archive/delete', json=body)
        assert r.status_code == 400, '%r 被接受了：%s' % (body, r.text)
        assert r.json().get('success') is not True, r.text


def test_unknown_archive_is_reported_as_failure(client):
    login(client)
    r = client.post('/api/users/archive/delete', json={'names': ['no-such-archive-xyz']})
    assert r.status_code == 400, r.text
    d = r.json()
    assert d.get('success') is False and d.get('deleted') == 0, d
    assert d.get('failed'), '失败必须逐项说明：%s' % d


def test_deleting_an_archive_refreshes_the_parent_totals(client):
    """归档也在 users 子树里：删掉之后聚合统计必须跟着降（失效挂在删除点上）"""
    login(client)
    entry = _make_archive(client, 'arch_agg_user', b'x' * 500)

    def users_total():
        d = client.get('/api/files', params={'path': 'users'}).json()
        return d['total_size_sum']

    before = users_total()
    r = client.post('/api/users/archive/delete', json={'names': [entry]})
    assert r.status_code == 200 and r.json().get('deleted') == 1, r.text
    after = users_total()
    assert after < before, \
        '删了归档但 users 的聚合值没降（缓存没失效）：before=%s after=%s' % (before, after)


def test_admin_can_browse_into_an_archive(client):
    """归档目录本身必须能进去看 —— 清理之前总得先看清里面是什么。

    用户 2026-09-21 反馈「网页不能进入归档目录」：卡片上当时只有「删除」，
    等于让人盲删。先钉住后端可达（列表 + 页面两条路都通），界面入口另说。
    """
    import urllib.parse

    login(client)
    entry = _make_archive(client, 'arch_browse_user', b'browse me')
    rel = 'users/.deleted/' + entry

    got = client.get('/api/files', params={'path': rel})
    assert got.status_code == 200, got.text
    names = [f['name'] for f in got.json()['files']]
    assert 'keep.txt' in names, '归档目录列不出来：%s' % names

    page = client.get('/browse/' + urllib.parse.quote(rel, safe=''))
    assert page.status_code == 200, \
        '浏览页进不去归档目录（%s）：%s' % (rel, page.status_code)


def test_admin_page_has_a_way_into_the_archive():
    """前端守卫：卡片上必须有**能进目录**的入口。

    用户 2026-09-21 实测反馈「网页不能进入归档目录」—— 后端与浏览页一直是通的
    （见上面那条），问题在于卡片当时只给了「删除」，等于让人对着看不见的东西盲删。
    前端在测试环境里跑不起来，所以按项目既有做法用**静态守卫**把这个入口钉住。
    """
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'leaffs', 'web_page', 'management')
    for fn in ('users.html', 'users.en.html'):
        src = io.open(os.path.join(base, fn), encoding='utf-8').read()
        assert 'function openArchive(' in src, '%s 没有进归档目录的入口' % fn
        assert "'/browse/'+encodeURIComponent('users/.deleted/'" in src, \
            '%s 的入口没有指到浏览页的归档目录' % fn
        assert src.count('openArchive(') >= 2, '%s 里入口没接到按钮上' % fn


def test_archive_endpoints_require_admin(client):
    """匿名拿不到清单、也清不了东西。

    拒绝口径是 **404**：`send_json` 会把身份/权限类拒绝统一改写成 404（不泄露
    "这个端点存在，只是你没权限"，见 handler._REJECT_AS_NOT_FOUND）。所以这里先
    确认管理员能正常拿到清单 —— 否则匿名 404 也可能只是路由没注册，那这条就白测了。
    """
    login(client)
    assert client.get('/api/users/archive').status_code == 200, '管理员都拿不到，路由没注册上'

    with httpx.Client(base_url=BASE_URL, timeout=20.0, verify=False) as anon:
        r = anon.get('/api/users/archive')
        assert r.status_code in (401, 403, 404), r.text
        r = anon.post('/api/users/archive/delete', json={'all': True})
        assert r.status_code in (401, 403, 404), r.text
