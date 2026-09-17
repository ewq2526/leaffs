# -*- coding: utf-8 -*-
"""CSRF：所有改状态的 POST 统一走同源校验（Origin / Referer）。

背景：后端原来零 CSRF 防护，唯一防线是会话 Cookie 的 SameSite=Lax ——
跨站（不同主机）确实拦得住，但 SameSite 只比 scheme+host、**不含端口**，
明文模式下同一台机器别的端口上的页面照样能带着会话 Cookie 打进来。
"""
from conftest import login


def _mkdir(client, name, **kw):
    return client.post('/api/mkdir', json={'path': '', 'name': name}, **kw)


def test_post_cross_origin_rejected(client):
    """跨站 Origin → 拒绝；同站不同端口 → 拒绝；同源 / 只带 Referer → 放行

    拒绝码是 404（统一拒绝口径，2026-09-15），不再是 403。
    """
    login(client)

    r = _mkdir(client, 'csrf_x', headers={'Origin': 'https://evil.example'})
    assert r.status_code == 404, r.text

    r = _mkdir(client, 'csrf_y', headers={'Origin': 'http://127.0.0.1:9999'})
    assert r.status_code == 404, '同站不同端口也要挡（SameSite 不含端口）：%s' % r.text

    r = _mkdir(client, 'csrf_z', headers={'Origin': 'null'})
    assert r.status_code == 404, 'Origin: null（沙箱/跨源页）也应拒绝：%s' % r.text

    base = str(client.base_url)
    r = _mkdir(client, 'csrf_ok', headers={'Origin': base})
    assert r.status_code == 200, r.text

    r = _mkdir(client, 'csrf_ok2', headers={'Referer': base + '/browse/'})
    assert r.status_code == 200, '没有 Origin 时用 Referer 判定：%s' % r.text

    r = _mkdir(client, 'csrf_ok3')
    assert r.status_code == 200, '两个头都没有 → 当非浏览器客户端放行：%s' % r.text
