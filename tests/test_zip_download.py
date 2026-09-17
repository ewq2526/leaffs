# -*- coding: utf-8 -*-
"""`/api/zip` 的行为（LF-05 回归）。

黑盒报告的现象（已核实）：原来把"路径非法 / 无权限 / 路径不安全 / 文件不存在"四种情况
**全部 `continue` 跳过**，而 `entries` 为空时**照样**走流式分支，发出 200 + 22 字节空 ZIP
（只有 EOCD）—— 一个"看起来合法"的假成功。

报告错的两处：①「权限探测 oracle」不成立（匿名对**所有**路径都拿同一个 22 字节响应，零区分度）；
② 它表里 `files=x` 的响应体写成 JSON 错误对象，实际是 HTML 500 页。

现在的口径（用户拍板）：**一个都没打成功就统一回 404** —— 不区分"无权"与"不存在"，
免得变成权限 oracle。
"""
import io
import json
import urllib.parse
import zipfile

import httpx

from conftest import BASE_URL, login


def _zip(client, paths, base=''):
    q = '/api/zip?files=' + urllib.parse.quote(json.dumps(paths))
    if base:
        q += '&base=' + urllib.parse.quote(base)
    return client.get(q)


def _names(resp):
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        return zf.namelist()


def _guest_client(ip='127.0.0.7'):
    """独立来源 IP：不占 127.0.0.1 的游客登录限速桶（踩过这个坑）"""
    return httpx.Client(base_url=BASE_URL,
                        transport=httpx.HTTPTransport(local_address=ip),
                        timeout=20.0)


def test_admin_gets_a_real_zip(client):
    """合法打包必须返回**真 ZIP** —— 只看状态码是不够的（原来 200 也可能是个空壳）"""
    login(client)
    r = client.post('/api/upload?path=public', files={'file': ('ziptest_a.txt', b'hello')})
    assert r.json().get('saved') == 1, r.text

    r = _zip(client, ['public/ziptest_a.txt'])
    assert r.status_code == 200, r.text
    assert r.headers.get('Content-Type') == 'application/zip', r.headers
    assert _names(r) == ['public/ziptest_a.txt'], _names(r)


def test_missing_path_is_404_not_an_empty_zip(client):
    """不存在的路径 → 404（原来 200 + 空 ZIP）"""
    login(client)
    r = _zip(client, ['public/zz_no_such_file.txt'])
    assert r.status_code == 404, r.text


def test_denied_and_missing_are_indistinguishable():
    """无权路径与不存在路径回**同一个** 404 —— 不给权限/存在性 oracle"""
    with _guest_client() as gc:
        r = gc.post('/api/guest/login')
        assert r.status_code == 200, r.text
        denied = _zip(gc, ['users/admin/secret.txt'])       # 无权
        missing = _zip(gc, ['users/admin/zz_no_such.txt'])  # 不存在
        assert denied.status_code == 404, denied.text
        assert missing.status_code == 404, missing.text
        assert denied.status_code == missing.status_code


def test_partial_success_still_packs_the_good_ones(client):
    """混合请求：能打的照打（部分成功是合理的），状态码 200"""
    login(client)
    r = client.post('/api/upload?path=public', files={'file': ('ziptest_b.txt', b'x')})
    assert r.json().get('saved') == 1, r.text

    r = _zip(client, ['public/ziptest_b.txt', 'public/zz_missing_2.txt'])
    assert r.status_code == 200, r.text
    assert _names(r) == ['public/ziptest_b.txt'], _names(r)


def test_published_share_can_be_zipped(client):
    """分享出来的文件（虚拟路径）也要能打进包 —— 原来 zip 完全没有映射解析"""
    login(client)
    r = client.post('/api/upload?path=users/admin', files={'file': ('share_me.txt', b'shared')})
    assert r.json().get('saved') == 1, r.text
    r = client.post('/api/share/publish', json={'paths': ['users/admin/share_me.txt']})
    assert r.status_code == 200, r.text

    vpath = 'public/shares/admin/share_me.txt'
    r = _zip(client, [vpath])
    assert r.status_code == 200, '分享文件应当能打包：%s' % r.text
    assert _names(r) == ['share_me.txt'], _names(r)
