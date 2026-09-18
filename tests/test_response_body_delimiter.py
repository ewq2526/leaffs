# -*- coding: utf-8 -*-
"""HTTP/1.1 预备 · 第 1 步：**每个响应都必须有正文定界**（2026-09-18）

**为什么**：HTTP/1.0 允许靠"关闭连接"表示正文结束，所以现在这些响应都还能用；换成
keep-alive 之后就不行了 —— 没有 `Content-Length` 也没有 chunked，客户端会一直等正文
直到超时。这一步先把所有缺定界的端点补齐，**协议开关留到最后一步才切**，所以此刻
行为与改动前一致（本文件里的断言在改动前大部分是红的）。

**机制**：`HTTPHandler` 新增了 `_warn_missing_body_delimiter()` —— 在 `end_headers()`
里检查本次响应有没有 `Content-Length` 或 `Transfer-Encoding`，没有就记一条 warn
（按 method+path 去重）。⚠️ 它**只告警、不自动补**长度：自动补会掩盖"这个端点压根算错了
长度"这类问题，变成不报错的静默劣化。

本文件钉两件：
1. 各端点的定界**确实发出去了**（端到端）；
2. `_ZipStreamWriter` 的 chunked 编码**格式正确**（单元）——
   ⚠️ 这条很重要：那段代码要等协议切到 1.1 才会真正跑到，在那之前它没有任何端到端覆盖。
"""
import io

from conftest import login


def _has_delim(r):
    """响应是否自带正文定界：Content-Length 或 chunked。"""
    if r.headers.get('Content-Length') is not None:
        return True
    return r.headers.get('Transfer-Encoding', '').lower() == 'chunked'


# ---------- 端到端：各响应都带定界 ----------

def test_json_responses_have_content_length(client):
    """走 send_json 的响应（含未授权 404）都必须带 Content-Length"""
    for path in ('/api/ping', '/api/auth/check', '/api/account/me', '/api/files'):
        r = client.get(path)
        assert _has_delim(r), '%s 没有正文定界' % path


def test_login_json_has_content_length(client):
    """登录成功的 JSON 是**手写响应**（要发 Set-Cookie，没走 send_json），单独钉一下"""
    r = client.post('/api/auth/login', json={'username': 'admin', 'password': 'admin'})
    assert r.status_code == 200, r.text
    assert _has_delim(r), '登录成功响应没有正文定界'
    assert r.json().get('success') is True


def test_redirect_responses_have_zero_content_length(client):
    """★ 302 **是允许带正文**的状态码，所以不能靠关连接定界 —— 必须显式声明空正文"""
    # 未带一次性令牌 ⇒ redirect('/login')
    r = client.get('/api/admin/auto-login')
    assert r.status_code == 302, r.status_code
    assert r.headers.get('Content-Length') == '0', \
        '302 没有显式声明空正文: %r' % r.headers.get('Content-Length')


def test_logout_redirect_has_zero_content_length(client):
    login(client)
    r = client.post('/api/auth/logout')
    assert r.status_code == 302, r.status_code
    assert r.headers.get('Content-Length') == '0', \
        '登出 302 没有显式声明空正文: %r' % r.headers.get('Content-Length')


def test_raw_file_content_length_matches_size(client):
    """`/api/raw` 是流式写出的文件内容 —— 长度必须正好等于文件大小"""
    login(client)
    payload = b'0123456789' * 7          # 70 字节
    up = client.post('/api/upload?path=public',
                     files={'file': ('delim_probe.txt', payload)})
    assert up.status_code == 200, up.text

    r = client.get('/api/raw?path=public/delim_probe.txt')
    assert r.status_code == 200, r.text
    assert r.headers.get('Content-Length') == str(len(payload)), \
        'Content-Length 与实际正文不符: %r' % r.headers.get('Content-Length')
    assert r.content == payload


def test_range_error_has_zero_content_length(client):
    """416 允许带正文，同样要显式声明空正文"""
    login(client)
    up = client.post('/api/upload?path=public', files={'file': ('delim_small.txt', b'abc')})
    assert up.status_code == 200, up.text
    r = client.get('/download/public/delim_small.txt',
                   headers={'Range': 'bytes=100-200'})
    assert r.status_code == 416, r.status_code
    assert r.headers.get('Content-Length') == '0', \
        '416 没有显式声明空正文: %r' % r.headers.get('Content-Length')


# ---------- 单元：chunked 编码格式 ----------

class _FakeConn:
    def settimeout(self, _t):
        pass


class _FakeHandler:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.connection = _FakeConn()


def test_zip_stream_writer_chunked_encoding():
    """★ chunked 模式的输出必须是合法的 chunked 字节流

    格式：`<十六进制长度>\\r\\n<数据>\\r\\n` … 最后以 0 长度块 `0\\r\\n\\r\\n` 收尾。
    ⚠️ 这段代码要等协议切到 1.1 才会在真实请求里跑到，所以这里是它唯一的覆盖。
    """
    from leaffs.files.api import _ZipStreamWriter

    h = _FakeHandler()
    w = _ZipStreamWriter(h, chunked=True)
    w.write(b'hello')            # 5 字节
    w.write(b'')                 # 空写不产生块
    w.write(b'world!!')          # 7 字节
    w.finish()
    assert h.wfile.getvalue() == b'5\r\nhello\r\n7\r\nworld!!\r\n0\r\n\r\n', \
        h.wfile.getvalue()


def test_zip_stream_writer_raw_mode_unchanged():
    """非 chunked（HTTP/1.0，当前行为）时按原样直发，finish() 是空操作"""
    from leaffs.files.api import _ZipStreamWriter

    h = _FakeHandler()
    w = _ZipStreamWriter(h)      # 默认 chunked=False
    w.write(b'hello')
    w.finish()
    assert h.wfile.getvalue() == b'hello', h.wfile.getvalue()
