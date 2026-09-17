# -*- coding: utf-8 -*-
"""下载器页面门槛（A-03 页面侧）。

数据 API（/api/url-download/*）一直有 _dl_allowed() 门槛，但两个页面处理器
（/url-download、/url-download/peers）原来不看 role：页面 HTML 谁都能拿。
这里守住"入口与 API 同一口径"：未登录 → 登录页；已登录但无资格（guest 且
downloader_guest_allowed 关闭，默认关闭）→ 被拒（统一拒绝口径后是 404）。
"""
import httpx
import pytest

from conftest import login, guest_login

PAGES = ('/url-download', '/url-download/peers', '/url-download/anything')


@pytest.mark.parametrize('path', PAGES)
def test_downloader_page_requires_login(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 302, (path, r.status_code)
    assert '/login' in r.headers.get('Location', ''), r.headers.get('Location')


def test_downloader_page_guest_forbidden(server):
    # 换一个环回源 IP 登录：游客登录有进程内频控（10 次/分钟，按来源 IP 计），
    # 整个 session 共享一个服务进程，不能再去挤 127.0.0.1 的额度
    transport = httpx.HTTPTransport(local_address='127.0.0.2')
    with httpx.Client(base_url=server, transport=transport, timeout=20.0) as c:
        guest_login(c)
        for path in PAGES:
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 404, (path, r.status_code)


def test_downloader_page_admin_ok(client):
    login(client)
    for path in ('/url-download', '/url-download/peers'):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 200, (path, r.status_code)
        assert 'html' in r.headers.get('Content-Type', '')


def test_downloader_api_gate_unchanged(client):
    """数据 API 的门槛不能被这次改动带偏：未登录仍是"被拒"（统一口径后 404）"""
    r = client.get('/api/url-download/tasks', follow_redirects=False)
    assert r.status_code == 404, r.status_code
