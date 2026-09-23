# -*- coding: utf-8 -*-
"""响应头（LF-10 回归）：公共安全头不许漏，JSON 不许被缓存。

黑盒报告点出两处：`send_json` 从没加 `Cache-Control`；登录成功 / 游客登录这两处
**手写响应**连 nosniff / XFO / Referrer / CSP 都没有。

根因不是那两处写漏了，而是**项目没有统一出口** —— 统一响应口只有 `send_json` /
`redirect`，而手写 `send_response(...)` 的地方全在它们之外（全仓 22 处里有 11 处漏过）。
所以修法是覆写 `end_headers` 自动补头，这里就从"任意响应"的角度去验。

⚠️ 缓存策略**不是一刀切**：JSON 一律 `no-store`，而静态资源 / 缩略图保持各自的缓存
（缩略图是 `max-age=3600`）—— 别把性能改坏。
"""
import json
import os

import httpx
import pytest

from conftest import PROJ_ROOT, login

# 每个响应都该有的公共安全头
_COMMON = {
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'no-referrer',
}


def _assert_common(resp, where):
    for k, v in _COMMON.items():
        assert resp.headers.get(k) == v, '%s: 缺 %s（实际 %r）' % (where, k, resp.headers.get(k))


def _publish(client, src_rel):
    """把一条文件发布成分享，返回它的**虚拟路径**（设码要指明是哪一条）"""
    r = client.post('/api/share/publish', json={'paths': [src_rel]})
    assert r.status_code == 200 and r.json().get('success'), r.text
    pub = r.json().get('published') or []
    assert pub, r.text
    return pub[0]['path']


def _set_code(client, vp, code):
    """给**这一条**分享设码（码的粒度是每条分享，不再按用户名）"""
    r = client.post('/api/share/code', json={'path': vp, 'code': code})
    assert r.status_code == 200 and r.json().get('enabled') is bool(code), r.text


def _label_of(data_root, vp):
    """取这条分享的随机标签 —— 访客输码时用它指认"哪一条"（`/api/share/auth {label, code}`）。

    映射表由服务端写在自己的数据根里，这里把测试进程内的模块指向同一份文件，
    再走一次 `access_scope`（服务端闸门判"这个路径属于哪条分享"用的就是它）。
    """
    import leaffs.share.mappings as _m
    _m._MAPPINGS_FILE = os.path.join(data_root, 'config', 'share_mappings.json')
    _m._cache = None
    label, _owner = _m.access_scope(vp)
    return label


def _drop_share(client, vp):
    """清码并移除映射 —— 分享码是**共享服务进程上的持久状态**，断言成败都要还原"""
    client.post('/api/share/code', json={'path': vp, 'code': ''})
    client.post('/api/share/unpublish', json={'path': vp})


def test_json_api_has_common_headers_and_no_store(client):
    """普通 JSON API：公共头齐全，且 Cache-Control: no-store"""
    r = client.get('/api/ping')
    assert r.status_code == 200, r.text
    _assert_common(r, '/api/ping')
    assert 'default-src' in (r.headers.get('Content-Security-Policy') or ''), \
        '/api/ping: JSON 应当带收紧的 CSP'
    assert r.headers.get('Cache-Control') == 'no-store', \
        '/api/ping: JSON 必须 no-store，实际 %r' % r.headers.get('Cache-Control')


def test_handwritten_login_responses_have_common_headers(client):
    """两处**手写**响应（登录成功 / 游客登录）也要有公共头 —— 报告点的就是这里"""
    r = client.post('/api/auth/login', json={'username': 'admin', 'password': 'admin'})
    assert r.status_code == 200, r.text
    _assert_common(r, 'POST /api/auth/login')
    assert r.headers.get('Cache-Control') == 'no-store', \
        '登录响应含身份信息，必须 no-store：%r' % r.headers.get('Cache-Control')


def test_handwritten_guest_login_has_common_headers(client):
    import httpx
    from conftest import BASE_URL
    with httpx.Client(base_url=BASE_URL,
                      transport=httpx.HTTPTransport(local_address='127.0.0.8'),
                      timeout=20.0) as gc:
        r = gc.post('/api/guest/login')
    assert r.status_code == 200, r.text
    _assert_common(r, 'POST /api/guest/login')
    assert r.headers.get('Cache-Control') == 'no-store', \
        '游客登录响应必须 no-store：%r' % r.headers.get('Cache-Control')


def test_error_responses_have_common_headers(client):
    """错误响应（含 403/404 这类）也要带头 —— 它们走的是另一条自实现路径"""
    login(client)
    r = client.get('/api/nonexistent-endpoint-zz')
    assert r.status_code >= 400, r.text
    _assert_common(r, '不存在的端点')


def test_share_page_and_code_auth_have_common_headers(client, data_root):
    """B5：两处**手写响应**也要有公共头（`/p/<用户>` 页面、`/api/share/auth` 成功路径）。

    它们不走 `send_json` / `redirect`，是 A8「覆写 `end_headers` 统一补头」覆盖的典型，
    而这两条路径此前**没有任何用例守着** —— 回归风险最高的就是这种"靠统一出口兜住"的地方。

    页面那条**不该**带收紧的 CSP（`public.html` 有内联脚本，`script-src 'self'` 会打断它），
    所以这里只断言公共头，不把 CSP 钉成非空 —— 免得将来有人"顺手补个 CSP"把分享页打坏。

    ⚠️ 成功路径必须先给**某一条分享**设码（码的粒度是每条分享，设码要指明 path），
    而分享码与映射都是**共享服务进程上的持久状态**：设完必须清掉（放 `finally`），
    否则后面依赖"匿名可读分享页"的用例（`test_share_sessions.py`）会撞上一条需要输码的
    分享而变红。授权 Cookie 的名字是固定常量 `access.SHARE_COOKIE`（不再随用户名变）。
    """
    r = client.get('/p/nobody/')
    assert r.status_code == 200, r.text
    _assert_common(r, '/p/nobody/')

    login(client)
    r = client.post('/api/upload?path=public', files={'file': ('hdrprobe.txt', b'h')})
    assert r.status_code == 200 and r.json().get('saved') == 1, r.text
    vp = _publish(client, 'public/hdrprobe.txt')
    _set_code(client, vp, 'abc123')
    try:
        from leaffs.share import access as _sacc
        label = _label_of(data_root, vp)
        assert label, '登记一条分享时没有生成标签'
        with httpx.Client(base_url=client.base_url, timeout=20) as anon:
            ok = anon.post('/api/share/auth', json={'label': label, 'code': 'abc123'})
            assert ok.status_code == 200, ok.text
            _assert_common(ok, 'POST /api/share/auth（成功路径）')
            assert _sacc.SHARE_COOKIE + '=' in (ok.headers.get('set-cookie') or ''), \
                '成功路径必须下发授权 Cookie（%s）：%r' % (
                    _sacc.SHARE_COOKIE, ok.headers.get('set-cookie'))
    finally:
        # 共享进程上的状态：断言成败都要还原，否则污染后续用例
        _drop_share(client, vp)


def test_handwritten_streaming_and_page_responses_have_cache_control(client, data_root):
    """手写响应（流式文件 / 二维码页 / 分享页）都必须带 `Cache-Control`

    这几处原来**一个缓存头都没有** —— 而它们装的是用户文件内容、一次性登录二维码、
    对外开放的分享页。没有缓存头 = 允许浏览器/中间缓存按启发式规则留存，
    在共享设备或代理后面就是实打实的泄露面。
    现在缓存策略走统一出口（`end_headers` 兜底补 `no-store`），缓存是**例外**、必须显式声明。
    """
    login(client)
    # 造一个真实文件给 /api/raw 与 /api/zip 用
    r = client.post('/api/upload?path=public', files={'file': ('cacheprobe.txt', b'cc')})
    assert r.status_code == 200 and r.json().get('saved') == 1, r.text

    checks = [
        ('/api/raw?path=public/cacheprobe.txt', client.get('/api/raw?path=public/cacheprobe.txt')),
        ('/api/qrcode', client.get('/api/qrcode')),
        ('/p/nobody/', client.get('/p/nobody/')),
    ]
    for where, resp in checks:
        assert resp.status_code == 200, (where, resp.status_code, resp.text[:120])
        assert resp.headers.get('Cache-Control'), \
            '%s：手写响应没有 Cache-Control（会被启发式缓存）：%r' % (
                where, dict(resp.headers))


def test_zip_and_share_auth_success_have_cache_control(client, data_root):
    """zip 打包流与分享码认证成功路径（含授权 Cookie）也要有缓存头"""
    import json as _json
    import urllib.parse as _up
    login(client)
    r = client.post('/api/upload?path=public', files={'file': ('zipc.txt', b'z')})
    assert r.status_code == 200, r.text
    q = '/api/zip?files=' + _up.quote(_json.dumps(['public/zipc.txt']))
    z = client.get(q)
    assert z.status_code == 200, z.text
    assert z.headers.get('Cache-Control'), \
        '/api/zip：打包流没有 Cache-Control：%r' % dict(z.headers)

    # 码的粒度是**每条分享**：先发布这一条、再给它设码，输码时用它的随机标签指认
    vp = _publish(client, 'public/zipc.txt')
    _set_code(client, vp, 'abc123')
    try:
        label = _label_of(data_root, vp)
        with httpx.Client(base_url=client.base_url, timeout=20) as anon:
            ok = anon.post('/api/share/auth', json={'label': label, 'code': 'abc123'})
            assert ok.status_code == 200, ok.text
            assert ok.headers.get('Cache-Control'), \
                '分享码成功路径（含授权 Cookie）没有 Cache-Control：%r' % dict(ok.headers)
    finally:
        _drop_share(client, vp)


def test_cached_assets_keep_their_own_policy(client):
    """兜底补头**不能**把该缓存的东西一刀切成 no-store（那会把性能改坏）

    静态资源是 `no-cache`、缩略图是 `max-age=3600` —— 两处都显式声明，自动豁免。
    """
    r = client.get('/static/app.js')
    if r.status_code == 200:
        assert r.headers.get('Cache-Control') != 'no-store', \
            '静态资源被兜底逻辑改成了 no-store：%r' % r.headers.get('Cache-Control')
    # 缩略图：**异步生成** —— 首次请求 404（"正在生成"占位），后台生成完才有，
    # 所以轮询等它出来，别把首次 404 当失败。
    #
    # ⚠️ 图片别用 1×1：ffmpeg 对它报 `one of its streams received no packets` /
    # `Conversion failed!`（返回码 69）→ 静默不产出 → 这条用例会一直 404。
    # 2×2 及以上就正常（实测 2/8/64 都可以）。而且**返回码非 0 时上游不记录任何日志**，
    # 查起来是黑洞 —— 见 fix-log 的 CC1「仍未动的地方」。
    import struct
    import time
    import zlib

    from leaffs.utils.core import FFMPEG_PATH
    if not FFMPEG_PATH:
        pytest.skip('本机没有 ffmpeg，桌面端缩略图后端不可用（安卓靠原生生成器，另说）')

    def _chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data +
                struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))

    w = h = 8
    raw = b''.join(b'\x00' + b'\x80\x40\x20' * w for _ in range(h))
    png = (b'\x89PNG\r\n\x1a\n' +
           _chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) +
           _chunk(b'IDAT', zlib.compress(raw)) + _chunk(b'IEND', b''))

    login(client)
    up = client.post('/api/upload?path=public', files={'file': ('cc.png', png)})
    assert up.status_code == 200, up.text
    t = None
    for _ in range(12):
        t = client.get('/api/thumb?path=public/cc.png')
        if t.status_code == 200:
            break
        time.sleep(0.5)
    assert t is not None and t.status_code == 200, \
        '缩略图 6 秒内没生成出来（后台生成失败？）：%s' % (t.text[:150] if t else '')
    assert t.headers.get('Cache-Control') == 'max-age=3600', \
        '缩略图的缓存策略被兜底逻辑改掉了：%r' % t.headers.get('Cache-Control')


def test_thumbnail_cache_policy_still_declared_in_source():
    """白盒：缩略图那行显式缓存策略还在 —— 与"本机有没有缩略图后端"无关

    兜底逻辑的规则是"默认 no-store、要缓存必须显式声明"，所以只要有人删掉这行，
    缩略图就会变成每次重下（性能变差，而且**不会有任何报错**）。
    """
    text = open(os.path.join(PROJ_ROOT, 'leaffs', 'files', 'api.py'), encoding='utf-8').read()
    assert "send_header('Cache-Control', 'max-age=3600')" in text, \
        '缩略图的缓存策略声明被删了 —— 兜底逻辑会把它改成 no-store（性能变差）'


def test_every_response_has_cache_control(client):
    """兜底断言：一组代表端点的**任何** 200 响应都不许没有 `Cache-Control`"""
    login(client)
    missing = []
    for p in ('/api/ping', '/api/files?path=public', '/api/auth/check', '/browse/public',
              '/p/nobody/', '/api/share', '/api/stats', '/api/qrcode'):
        r = client.get(p)
        if r.status_code == 200 and not r.headers.get('Cache-Control'):
            missing.append(p)
    assert not missing, '这些 200 响应没有 Cache-Control：%s' % missing


def test_every_request_in_a_series_has_headers(client):
    """连续多个请求都要带头（防"只有第一个请求带头"那类标记污染）

    主服务现在是 HTTP/1.0（一请求一连接，实例不复用），这条暂时测不到复用场景；
    但 `send_response` 里的标记清理是**正确性前提**，将来升到 HTTP/1.1 + keep-alive
    时它决定了从第二个请求起还带不带头 —— 所以这条测试先立在这里。
    """
    login(client)
    for i in range(4):
        r = client.get('/api/ping')
        assert r.status_code == 200, r.text
        _assert_common(r, '第 %d 个请求' % (i + 1))
        assert r.headers.get('Cache-Control') == 'no-store', '第 %d 个请求' % (i + 1)


def test_page_headers_and_cache(client):
    """页面 HTML：公共头在，缓存策略仍是项目既有的 no-store（没被我改成别的）"""
    login(client)
    r = client.get('/browse/public')
    assert r.status_code == 200, r.text
    _assert_common(r, '/browse/public')
    assert r.headers.get('Cache-Control') == 'no-store', r.headers.get('Cache-Control')


def test_static_assets_are_not_forced_no_store(client):
    """静态资源**不能**被一刀切成 no-store —— 那会把缓存性能改坏"""
    r = client.get('/static/app.js')
    if r.status_code != 200:
        return                      # 路径不存在就跳过（由别的用例保证可达性）
    cc = r.headers.get('Cache-Control')
    assert cc != 'no-store', \
        '静态资源被强制不缓存了 —— 那等于每次打开页面都重下：%r' % cc
    _assert_common(r, '/static/app.js')
