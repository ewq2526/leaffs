# -*- coding: utf-8 -*-
"""静态资源条件请求（ETag / Last-Modified / 304）—— 2026-09-18

**来由**：`/static/*` 一直发 `Cache-Control: no-cache`，但**一个验证器都没有**。
`no-cache` 的语义是"可以存、但每次用之前必须回源验证"，没有验证器就只剩"每次回完整的
200 ＋ 整个文件" —— 等于退化成 `no-store`（静态资源共 653.9 KB / 34 个文件）。

**改法**：`leaffs/web/render.py` 的 `serve_static` 补 `ETag`（大小 + mtime 纳秒）与
`Last-Modified`，命中 `If-None-Match` / `If-Modified-Since` 就回 304。

⚠️ **安全前提（本文件专门钉住它）**：`/static/*` 发的是 `web_page/` 下的**原文件、
不做任何替换** ⇒ 同一个 URL 对所有用户内容完全相同，304 才不会串味。
**按会话注入的页面是另一条路**（`serve_file` 与各页面 handler 会替换 `__MY_ROLE__` /
`__MY_USERNAME__`），它们因人而异、**必须保持 `no-store`** —— 别顺手也给它们加验证器。

⚠️ 收益要说清楚：省的是**带宽**，不是 RTT —— `no-cache` 语义没变，浏览器每次仍要发
条件请求，改版仍然**立即生效**（不会出现"用户看到旧页面"）。
"""
import os
import time
from email.utils import formatdate

from conftest import login
from leaffs.paths import BASE_DIR

STATIC = '/static/app.js'
# ⚠️ `serve_static` 里是 `os.path.join(BASE_DIR, 'web_page')`，而 BASE_DIR 是 **leaffs/ 包目录**、
# 不是项目根 —— 按项目根去拼会 FileNotFoundError（第一次就拼错了）。
STATIC_FILE = os.path.join(BASE_DIR, 'web_page', 'common', 'app.js')


def test_static_sends_validators(client):
    """首次请求：200 ＋ ETag ＋ Last-Modified，缓存策略仍是 no-cache"""
    r = client.get(STATIC)
    assert r.status_code == 200, r.text
    assert r.headers.get('ETag'), '没有 ETag —— 条件请求无从谈起'
    assert r.headers.get('Last-Modified'), '没有 Last-Modified'
    assert 'no-cache' in r.headers.get('Cache-Control', ''), r.headers.get('Cache-Control')
    assert r.content, '正文是空的'


def test_if_none_match_returns_304_without_body(client):
    """命中 ETag ⇒ 304 且正文为空"""
    etag = client.get(STATIC).headers['ETag']
    r = client.get(STATIC, headers={'If-None-Match': etag})
    assert r.status_code == 304, '带相同 ETag 没回 304'
    assert r.content == b'', '304 不该带正文'


def test_304_keeps_no_cache_not_no_store(client):
    """★ 304 的 `Cache-Control` 必须还是 `no-cache`，不能被打回 `no-store`

    这是这个改动最容易踩的坑：`handler.end_headers()` 有一条兜底 —— 发现响应里
    **没有**人显式声明过 Cache-Control 就补一个 `no-store`（那是 CC1 定的"默认不可缓存"
    规则）。304 分支要是漏了显式声明，就会被兜底成 `no-store`，
    把静态资源**反而**标成完全不可缓存 —— 而且不会报错，静默劣化。
    """
    etag = client.get(STATIC).headers['ETag']
    r = client.get(STATIC, headers={'If-None-Match': etag})
    cc = r.headers.get('Cache-Control', '')
    assert 'no-cache' in cc, '304 没有显式声明缓存策略: %r' % cc
    assert 'no-store' not in cc, '304 被兜底成了 no-store: %r' % cc


def test_weak_etag_and_star_match(client):
    """`W/"x"` 与 `"x"` 等价（RFC 7232 弱比较）；`*` 表示"只要资源还在就别回正文" """
    etag = client.get(STATIC).headers['ETag']
    assert client.get(STATIC, headers={'If-None-Match': 'W/' + etag}).status_code == 304
    assert client.get(STATIC, headers={'If-None-Match': '*,...'}).status_code == 304
    # 多值列表里命中任意一个即可
    assert client.get(STATIC, headers={'If-None-Match': '"a", %s' % etag}).status_code == 304


def test_wrong_etag_gets_full_response(client):
    """ETag 不匹配 ⇒ 老实回全量，不许假装 304"""
    r = client.get(STATIC, headers={'If-None-Match': '"nope"'})
    assert r.status_code == 200
    assert r.content


def test_if_modified_since(client):
    """`If-Modified-Since` 用响应里的 Last-Modified 命中；比资源旧的时间必须回全量

    ⚠️ "比资源旧"要用**绝对**时间（epoch），不能用"一天前"这种相对值 —— 文件 mtime 是固定的，
    而"一天前"会随时间流逝推到它后面，断言就自己反转了。2026-09-19 实测踩到：
    `app.js` 的 mtime 是 09-18 06:59，而"一天前"已经是 09-18 20:33 ⇒ 304 ≠ 200 挂掉。
    """
    lm = client.get(STATIC).headers['Last-Modified']
    assert client.get(STATIC, headers={'If-Modified-Since': lm}).status_code == 304
    epoch = formatdate(0, usegmt=True)      # 1970-01-01，一定早于任何文件的 mtime
    r = client.get(STATIC, headers={'If-Modified-Since': epoch})
    assert r.status_code == 200, '1970 年就不该命中 304: %s' % r.status_code


def test_if_none_match_wins_over_if_modified_since(client):
    """★ 优先序（RFC 7232）：带了 `If-None-Match` 就不再理会 `If-Modified-Since`

    构造一个**未来**的 IMS ＋ 一个**错的** INM：只看 INM 就该回 200。
    实现要是反了（先看 IMS），这里会错误地回 304 —— 那样客户端会一直用旧文件。
    """
    future = formatdate(time.time() + 86400, usegmt=True)
    r = client.get(STATIC, headers={
        'If-None-Match': '"nope"',
        'If-Modified-Since': future,
    })
    assert r.status_code == 200, '带 If-None-Match 时不该拿 If-Modified-Since 做判定'


def test_etag_tracks_file_change(client):
    """文件变了（mtime 变）⇒ ETag 跟着变，旧 ETag 不再命中

    ⚠️ 动的是项目里的真实静态文件，所以**只改 mtime、内容一字不动**，且 finally 里恢复。
    （内容没变就不影响任何其它测试；万一中途挂了，残留也只是 mtime，git 不跟踪它。）
    """
    before = client.get(STATIC)
    etag = before.headers['ETag']
    st = os.stat(STATIC_FILE)
    try:
        os.utime(STATIC_FILE, (st.st_atime, st.st_mtime + 10))
        r = client.get(STATIC, headers={'If-None-Match': etag})
        assert r.status_code == 200, '文件改了旧 ETag 还能命中 —— 验证器没跟着变'
        assert r.headers['ETag'] != etag, 'ETag 没变'
    finally:
        os.utime(STATIC_FILE, (st.st_atime, st.st_mtime))


# ---------- 安全前提：这两条塌了，缓存就会串味 ----------

def test_static_serves_raw_placeholders_not_rendered(client):
    """★ `/static/*` 发的是原文件：占位符**原样保留** ⇒ 内容与用户无关

    页面（`/`、`/login`…）走渲染，会把 `__MY_ROLE__` 换成真实角色；`/static/` 这条路
    只把文件原样吐出去。这正是"能给 /static/ 加验证器"的前提 ——
    它要是哪天开始替换，304 就会把 A 的页面发给 B。
    """
    r = client.get('/static/home/home.html')
    assert r.status_code == 200, r.text
    assert '__MY_ROLE__' in r.text or '__MY_USERNAME__' in r.text, \
        '静态路径竟然做了替换 —— 那它就不能被缓存'


def test_static_etag_identical_across_identities(client):
    """★ 匿名与已登录拿到**同一个** ETag —— 再钉一次"与身份无关"

    而且匿名时拿到的 ETag，登录之后仍然能命中 304（反向也一样）。
    """
    anon = client.get(STATIC)
    login(client)
    authed = client.get(STATIC)
    assert anon.headers['ETag'] == authed.headers['ETag'], '静态资源竟然因身份而异'
    assert client.get(STATIC, headers={'If-None-Match': anon.headers['ETag']}).status_code == 304


def test_session_pages_stay_no_store(client):
    """★ 反过来钉住：按会话注入的页面**必须**保持 `no-store`

    `/login` 的 HTML 里含 `__GUEST_DISPLAY__` 之类由服务端决定的东西，主页更是直接注入
    `__MY_USERNAME__` —— 缓存它们就是跨用户串味。别顺手给它们也加验证器。
    """
    r = client.get('/login', follow_redirects=True)
    assert r.status_code == 200, r.text
    cc = r.headers.get('Cache-Control', '')
    assert 'no-store' in cc, '登录页的缓存策略变了: %r' % cc
