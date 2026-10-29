# -*- coding: utf-8 -*-
"""关机 / 重启只给超级管理员，不分"人是不是在机器前"。

能力挂在**账号角色**上：匿名、游客、普通用户、管理员（非超管）一律拒绝；
超级管理员远程登录同样可以关。早先按"本机一次性令牌建立的会话"判，而那个标记
只有 `/login?leaf=` 那一次能拿到 —— 桌面窗口只要是账号口令登进来的，坐在机器前
也永远被挡住。

⚠️ 拒绝的**状态码是 404**：项目有一条统一口径（`_REJECT_AS_NOT_FOUND`），
401/403 一律改写成 404，别让拒绝本身变成"这里有什么"的探针。

⚠️ 不测成功执行。成功侧真的会把这台机器关掉，只能在真机上点一次；代码里唯一的
动作是 `systemctl --no-block poweroff|reboot`，动作名来自白名单。放行那一条故意
带一个白名单外的动作：能走到"未知操作"，就说明过了权限门，而这条路径不执行任何命令。
"""
import os
import re

import httpx

from conftest import PROJ_ROOT, guest_login, login

PW = 'pw-123456'

API_SRC = os.path.join(PROJ_ROOT, 'app', 'opt', 'leaffs', 'system', 'api.py')
HANDLER_SRC = os.path.join(PROJ_ROOT, 'app', 'opt', 'leaffs', 'server', 'handler.py')


def _post(client, action):
    return client.post('/api/power', json={'action': action})


def _make_client(base_url, username, role):
    """建账号并登录，返回带着这条会话的独立客户端"""
    with httpx.Client(base_url=base_url, timeout=20.0) as admin:
        login(admin)
        r = admin.post('/api/users/add', json={'username': username,
                                               'password': PW, 'role': 'user'})
        assert r.status_code == 200 and r.json().get('success'), r.text
        if role == 'admin':
            r = admin.post('/api/users/role', json={'username': username, 'role': 'admin'})
            assert r.status_code == 200 and r.json().get('success'), r.text
    c = httpx.Client(base_url=base_url, timeout=20.0)
    r = c.post('/api/auth/login', json={'username': username, 'password': PW})
    assert r.status_code == 200 and r.json().get('success'), r.text
    return c


def test_anonymous_rejected(client):
    r = _post(client, 'poweroff')
    assert r.status_code in (401, 404), r.text


def test_guest_rejected(client):
    guest_login(client)
    r = _post(client, 'poweroff')
    assert r.status_code == 404, r.text


def test_plain_user_rejected(client):
    c = _make_client(client.base_url, 'pw_probe_user', 'user')
    try:
        for action in ('poweroff', 'reboot'):
            r = _post(c, action)
            assert r.status_code == 404, '%s 没被挡住：%s' % (action, r.text)
    finally:
        c.close()


def test_admin_role_rejected(client):
    """管理员管账号与分享，不碰设备动作；关机要超级管理员。"""
    c = _make_client(client.base_url, 'pw_probe_admin', 'admin')
    try:
        r = _post(c, 'poweroff')
        assert r.status_code == 404, r.text
    finally:
        c.close()


def test_super_admin_passes_the_gate(client):
    """超管放行：账号口令登录的会话照样能过，不再看"本机令牌"。"""
    login(client)
    r = _post(client, 'no-such-action')
    assert r.status_code == 400, r.text
    err = r.json().get('error') or ''
    assert '未知操作' in err, r.text
    assert '本机' not in err, r.text


def test_gate_is_declared_at_dispatch():
    """判据在分派层：`/api/power` 必须在 `_SUPER_ADMIN_ONLY_POST` 里。"""
    src = open(HANDLER_SRC, encoding='utf-8').read()
    m = re.search(r'_SUPER_ADMIN_ONLY_POST = frozenset\(\((.*?)\)\)', src, re.S)
    assert m, '分派层没有超级管理员 POST 门槛清单'
    assert "'/api/power'" in m.group(1), '关机接口没进超级管理员门槛'


def test_no_local_token_check_left():
    """处理函数里不再判"是不是本机会话"——那正是把坐在机器前的人挡住的原因。"""
    src = open(API_SRC, encoding='utf-8').read()
    m = re.search(r'def power_action\(.*', src, re.S)
    assert m, '找不到 power_action'
    assert '_is_local_token_session' not in m.group(0), 'power_action 里还留着本机会话判据'
