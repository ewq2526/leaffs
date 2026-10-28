# -*- coding: utf-8 -*-
"""关机 / 重启接口只能在本机触发。

能力属于"人坐在机器前"：走的是**本机一次性令牌**建立的会话（与「服务端本机弹选择框」
同一口径），不跟账号走 —— 远程管理员、游客、匿名一律拒绝。

⚠️ 拒绝的**状态码是 404**：项目有一条统一口径（`_REJECT_AS_NOT_FOUND`），
401/403 一律改写成 404，别让拒绝本身变成"这里有什么"的探针。所以这里断的是
"拒绝 + 理由里写了只在本机"。

⚠️ 只测拒绝侧。成功侧真的会把这台机器关掉，只能在真机上点一次；代码里唯一的动作是
`systemctl --no-block poweroff|reboot`，动作名来自白名单。
"""
import os

from conftest import PROJ_ROOT, guest_login, login

HANDLER = os.path.join(PROJ_ROOT, 'app', 'opt', 'leaffs', 'server', 'handler.py')


def _post(client, action):
    return client.post('/api/power', json={'action': action})


def test_anonymous_rejected(client):
    r = _post(client, 'poweroff')
    assert r.status_code in (401, 404), r.text


def test_super_admin_from_remote_rejected(client):
    """超级管理员也不行：这个能力不跟账号走，只跟"人在不在机器前"走。"""
    login(client)
    for action in ('poweroff', 'reboot'):
        r = _post(client, action)
        assert r.status_code == 404, '%s 没被挡住：%s' % (action, r.text)
        assert '本机' in (r.json().get('error') or ''), r.text


def test_guest_rejected(client):
    guest_login(client)
    r = _post(client, 'poweroff')
    assert r.status_code == 404, r.text
    assert '本机' in (r.json().get('error') or ''), r.text


def test_route_is_declared():
    """接上没有：路由表里得有它（引用方是桌面页那个电源菜单）。"""
    src = open(HANDLER, encoding='utf-8').read()
    assert "'/api/power'" in src, '路由没接上'
    assert '_sys_api.power_action(self)' in src, '路由没有指到 power_action'
