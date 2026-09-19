# -*- coding: utf-8 -*-
"""页面 CSP（2026-09-18）：把子资源锁在同源 + 自己的 WebSocket 上。

**为什么**：`serve_file` 等页面渲染路径以前**完全没有 CSP**，页面里一旦出现
`<img src="外部">`、外链脚本/样式、或 `fetch('http://外部')`，浏览器就会真的去请求 ——
那是「软件内不访问外部链接」的最后一块缺口。**导航**由客户端的白名单管
（安卓 `onLoadRequest`、桌面 WebView2 导航层），**子资源**归这条 CSP。它由浏览器执行，
所以安卓 / 桌面 / 任何浏览器一起生效。

**为什么走统一出口**：发 HTML 的地方一共 6 处（`serve_file` / 两个管理页 / 登录页 /
公开分享页 / 扫码页），手工点必漏一处，而漏掉的那处就是缺口。所以放在
`HTTPHandler.end_headers` 里按 `Content-Type` 判断（项目本来就是这么处理安全头与缓存头的）。

⚠️ 已有的显式 CSP（`/static/*` 按 mime 发的、JSON 的 `default-src 'none'`、文件流的
sandbox）**不受影响** —— `_common_security_headers` 幂等，先发的那个说了算。
"""
from conftest import WS_PORT, login

CSP_HEADER = 'Content-Security-Policy'


def test_login_page_has_page_csp(client):
    """登录页（手工响应，不走 serve_file）也必须有 CSP"""
    r = client.get('/login')
    assert r.status_code == 200, r.text
    csp = r.headers.get(CSP_HEADER, '')
    assert "default-src 'self'" in csp, csp
    assert "object-src 'none'" in csp, csp
    assert "form-action 'self'" in csp, csp


def test_browse_page_has_page_csp(client):
    """主页面（serve_file 那条路）"""
    login(client)
    r = client.get('/', follow_redirects=True)
    assert r.status_code == 200, r.text
    assert "default-src 'self'" in r.headers.get(CSP_HEADER, '')


def test_admin_page_has_page_csp(client):
    """管理页（另一条渲染函数）—— 单独钉一下，它就是"手工点会漏"的那种"""
    login(client)
    r = client.get('/admin')
    assert r.status_code == 200, r.text
    assert "default-src 'self'" in r.headers.get(CSP_HEADER, '')


def test_csp_carries_own_ws_origin(client):
    """★ `connect-src` 必须带上自己的 WS 地址，否则下载器的实时进度会被打断

    前端连的是 `ws(s)://<location.hostname>:<WS_PORT>/ws`，端口可配置 ⇒ 只写 `'self'`
    是不够的（CSP 的 'self' 不覆盖 ws:// 的另一个端口）。
    """
    r = client.get('/login')
    csp = r.headers.get(CSP_HEADER, '')
    assert 'ws://' in csp and 'wss://' in csp, 'connect-src 没有 WS 源: %r' % csp
    assert ':%d' % WS_PORT in csp, 'CSP 里的 WS 端口不是配置值 %d: %r' % (WS_PORT, csp)


def test_csp_has_no_foreign_host(client):
    """★ 除了自己的 WS，不允许出现任何外部源（这条 CSP 的**全部意义**就在这儿）"""
    r = client.get('/login')
    csp = r.headers.get(CSP_HEADER, '')
    assert 'http://' not in csp.replace('ws://', ''), csp
    assert 'https://' not in csp.replace('wss://', ''), csp
    assert '*' not in csp, 'CSP 里有通配符，等于没限制: %r' % csp


def test_static_csp_not_overridden(client):
    """/static/* 有自己更贴切的策略（按 mime），不能被页面 CSP 覆盖"""
    r = client.get('/static/app.js')
    csp = r.headers.get(CSP_HEADER, '')
    assert csp.strip() == "default-src 'self'", \
        '/static 的 CSP 被改了: %r' % csp


def test_json_csp_not_overridden(client):
    """JSON 的 CSP 仍是收紧的 default-src 'none'"""
    r = client.get('/api/ping')
    assert r.headers.get(CSP_HEADER) == "default-src 'none'", \
        r.headers.get(CSP_HEADER)


# ---------- host 净化（拼进头之前必须做，否则就是头注入） ----------

def _csp_host(raw):
    from leaffs.web.render import _csp_host as f

    class _H:
        def __init__(self, v):
            self.headers = {'Host': v} if v is not None else {}

    return f(_H(raw))


def test_csp_host_accepts_normal_forms():
    assert _csp_host('localhost:8080') == 'localhost'
    assert _csp_host('127.0.0.1:8090') == '127.0.0.1'
    assert _csp_host('192.168.1.5') == '192.168.1.5'
    assert _csp_host('[::1]:8080') == '[::1]'
    assert _csp_host('[2001:db8::1]:8080') == '[2001:db8::1]'


def test_csp_host_rejects_junk():
    """★ 带引号/分号/空格/换行的 Host 一律不接受（宁可退化，也不拼进头里）"""
    for bad in ('', '   ', 'evil.com; script-src *', "a'b", 'has space',
                'evil\r\nX-Injected: 1', '[::1', 'a,b'):
        assert _csp_host(bad) == '', '应当拒绝 %r，得到 %r' % (bad, _csp_host(bad))
    assert _csp_host(None) == '', '没有 Host 头时应当返回空串'
