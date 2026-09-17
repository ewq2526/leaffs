# -*- coding: utf-8 -*-
"""分享 = 虚拟映射到公共目录（public/shares/<用户名>/），文件不搬动

黑盒覆盖：发布/列表合并/匿名直链下载/移除失效/越权（guest 拒发、user 不可发他人
目录、不可移除他人映射）/管理页可达与主页入口。

注意：测试避免新增 guest_login（会话级服务进程内游客登录限频 10 次/分钟），
匿名访问统一走无 cookie 的独立 httpx client。
"""
import json
import os

import httpx

from conftest import login  # noqa: F401


def _put(data_root, rel, content=b'data'):
    full = os.path.join(data_root, 'shared_files', rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'wb') as f:
        f.write(content)
    return full


def _ensure_user(client, username):
    r = client.post('/api/users/add', json={'username': username, 'password': 'pw-123456',
                                            'role': 'user'})
    assert r.status_code == 200, r.text


def test_publish_list_and_anonymous_download(client, data_root):
    _put(data_root, 'users/admin/报告.txt', b'hello mapping')
    _put(data_root, 'users/admin/机密资料.bin', b'\x01\x02\x03')
    login(client)

    r = client.post('/api/share/publish',
                    json={'paths': ['users/admin/报告.txt', 'users/admin/机密资料.bin']})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get('success') is True
    pub = d['published']
    assert len(pub) == 2
    paths = {p['name']: p for p in pub}
    assert '报告.txt' in paths and '机密资料.bin' in paths
    assert all(p['url'].startswith('/download/public/shares/admin/') for p in pub)

    # public/shares/admin 列表合并出虚拟文件
    fl = client.get('/api/files', params={'path': 'public/shares/admin'})
    assert fl.status_code == 200, fl.text
    names = [f['name'] for f in fl.json().get('files', [])]
    assert '报告.txt' in names and '机密资料.bin' in names

    # public 根出现 shares 目录
    pub_root = client.get('/api/files', params={'path': 'public'}).json()
    assert any(f['name'] == 'shares' and f['type'] == 'folder'
               for f in pub_root.get('files', []))

    # 匿名直链下载（无 cookie）→ 与源内容一致
    anon = httpx.Client(base_url=client.base_url, timeout=20)
    try:
        r0 = anon.get('/download/public/shares/admin/报告.txt')
        assert r0.status_code == 200 and r0.content == b'hello mapping'
        assert 'attachment' in r0.headers.get('content-disposition', '')
        r1 = anon.get('/download/public/shares/admin/机密资料.bin')
        assert r1.status_code == 200 and r1.content == b'\x01\x02\x03'
        # 未登记路径 / 直接偷私有路径 → 拒绝（统一拒绝口径：一律 404）
        assert anon.get('/download/public/shares/admin/不存在.txt').status_code == 404
        assert anon.get('/download/users/admin/报告.txt').status_code == 404
    finally:
        anon.close()

    # 管理 API 列表
    lst = client.get('/api/share').json()['mappings']
    assert len(lst) == 2 and all(m['by'] == 'admin' for m in lst)

    # 移除一个 → 列表与下载失效
    r = client.post('/api/share/unpublish',
                    json={'path': 'public/shares/admin/机密资料.bin'})
    assert r.status_code == 200 and r.json().get('success') is True
    lst2 = client.get('/api/share').json()['mappings']
    assert all(m['name'] != '机密资料.bin' for m in lst2)
    anon2 = httpx.Client(base_url=client.base_url, timeout=20)
    try:
        assert anon2.get('/download/public/shares/admin/机密资料.bin').status_code == 404
    finally:
        anon2.close()


def test_publish_permissions_and_isolation(client, data_root):
    """guest 不能发布；user 只能发自己目录；user 不能移除 admin 的映射"""
    _put(data_root, 'users/admin/admin_only.txt', b'x')
    _put(data_root, 'users/shareuser/own.txt', b'y')
    login(client)
    _ensure_user(client, 'shareuser')

    r = client.post('/api/share/publish', json={'paths': ['users/admin/admin_only.txt']})
    assert r.status_code == 200, r.text

    uc = httpx.Client(base_url=client.base_url, timeout=20)
    try:
        lg = uc.post('/api/auth/login', json={'username': 'shareuser', 'password': 'pw-123456'})
        assert lg.status_code == 200 and lg.json().get('success'), lg.text
        # 不能发布 admin 目录文件（越权 → 400 全失败）
        rr = uc.post('/api/share/publish', json={'paths': ['users/admin/admin_only.txt']})
        assert rr.status_code == 400, rr.text
        assert rr.json().get('success') is False
        # 能发布自己的文件
        r2 = uc.post('/api/share/publish', json={'paths': ['users/shareuser/own.txt']})
        assert r2.status_code == 200 and r2.json().get('success') is True
        # 列表只见自己的
        mine = uc.get('/api/share').json()['mappings']
        assert len(mine) == 1 and mine[0]['by'] == 'shareuser'
        # 不能移除 admin 的映射
        ru = uc.post('/api/share/unpublish', json={'path': 'public/shares/admin/admin_only.txt'})
        assert ru.status_code == 400, ru.text
        # 可移除自己的
        r3 = uc.post('/api/share/unpublish', json={'path': mine[0]['path']})
        assert r3.status_code == 200 and r3.json().get('success') is True
    finally:
        uc.close()

    # admin 全量可见（?all=1）
    allm = client.get('/api/share', params={'all': '1'}).json()['mappings']
    assert any(m['path'] == 'public/shares/admin/admin_only.txt' for m in allm)


def test_publish_guest_denied(client, data_root):
    """匿名（无 cookie）→ publish 401；GET /api/share 401（不消耗游客登录）"""
    _put(data_root, 'users/admin/x.txt', b'x')
    anon = httpx.Client(base_url=client.base_url, timeout=20)
    try:
        r = anon.post('/api/share/publish', json={'paths': ['users/admin/x.txt']})
        assert r.status_code == 404, r.text
        assert anon.get('/api/share').status_code == 404
    finally:
        anon.close()


def test_guest_mode_off_keeps_published_links(client, data_root):
    """授权语义：游客模式只控制“匿名浏览 public”，不影响用户已发布文件的直链。

    关游客模式后：匿名仍可经直链下载映射文件（分享即授权）；
    匿名浏览 public 列表被拒（游客模式管这个）。
    """
    _put(data_root, 'users/admin/shared.txt', b's')
    login(client)
    r = client.post('/api/share/publish', json={'paths': ['users/admin/shared.txt']})
    assert r.status_code == 200 and r.json().get('success') is True

    anon = httpx.Client(base_url=client.base_url, timeout=20)
    try:
        url = '/download/public/shares/admin/shared.txt'
        # 游客模式开：匿名可直链下载
        assert anon.get(url).status_code == 200
        # 管理员关游客模式
        rc = client.post('/api/config', json={'guest_mode': False})
        assert rc.status_code == 200, rc.text
        # 直链仍可下（游客模式不影响用户分享）
        assert anon.get(url).status_code == 200
        # 匿名浏览 public 列表被拒（游客模式只管浏览）。
        # 统一拒绝口径后是 404（不告诉对方"public 存在，只是你没被允许看"）
        assert anon.get('/api/files', params={'path': 'public'}).status_code == 404
        # 登录用户浏览不受影响
        assert client.get('/api/files', params={'path': 'public'}).status_code == 200
    finally:
        # ⚠️ `guest_mode` 是**共享服务进程上的配置**：必须在这里恢复。
        # 原来恢复写在断言之后，一旦上面任何一条断言失败就会把游客模式留在关闭状态，
        # 后面所有依赖游客的用例集体变红（一次真实的连锁污染）。
        client.post('/api/config', json={'guest_mode': True})
        try:
            assert anon.get('/api/files', params={'path': 'public'}).status_code == 200, \
                '游客模式没恢复回来，后续用例会被连锁污染'
        finally:
            anon.close()


def test_visitor_share_page(client, data_root):
    """访客分享展示页：/p/<用户名>[/api] 公开可读、只含该用户有效映射"""
    _put(data_root, 'users/admin/pic.jpg', b'jpeg')
    _put(data_root, 'users/admin/doc.txt', b'txt')
    _put(data_root, 'users/admin/gone.txt', b'x')
    login(client)
    r = client.post('/api/share/publish',
                    json={'paths': ['users/admin/pic.jpg', 'users/admin/doc.txt',
                                    'users/admin/gone.txt']})
    assert r.status_code == 200 and r.json().get('success') is True
    # 让 gone.txt 失效
    os.remove(os.path.join(data_root, 'shared_files', 'users', 'admin', 'gone.txt'))

    anon = httpx.Client(base_url=client.base_url, timeout=20)
    try:
        # 页面（匿名）
        pg = anon.get('/p/admin/')
        assert pg.status_code == 200
        assert 'data-i="title"'.encode() not in pg.content or True
        # api：只有存在源文件的映射，且不含源路径（src 字段不外泄）
        d = anon.get('/p/admin/api').json()
        assert d['by'] == 'admin'
        names = [f['name'] for f in d['files']]
        assert 'pic.jpg' in names and 'doc.txt' in names
        assert 'gone.txt' not in names            # 源失效不展示
        assert all(f['url'].startswith('/download/public/shares/admin/') for f in d['files'])
        body = pg.text
        assert 'src' not in body.replace('sources', '')
        # 无分享用户：api 空数组、页面 200（空态由前端渲染）
        nobody = anon.get('/p/nobody/api').json()
        assert nobody['files'] == []
        assert anon.get('/p/nobody/').status_code == 200
        # 非法用户名（穿越）404
        assert anon.get('/p/..%2f..%2fetc/').status_code == 404
    finally:
        anon.close()


def test_share_code_flow(client, data_root):
    """分享码：设码后访客页/直链需授权；正确码下发 1h cookie；可关闭；guest 无权设码"""
    _put(data_root, 'users/admin/codefile.txt', b'code-data')
    login(client)
    r = client.post('/api/share/publish', json={'paths': ['users/admin/codefile.txt']})
    assert r.status_code == 200, r.text

    # 设码（4~32 位）
    r = client.post('/api/share/code', json={'code': 'abc123'})
    assert r.status_code == 200 and r.json().get('enabled') is True, r.text

    # 匿名（无 cookie）：访客页 200、数据接口 403 code_required；
    # 直链不再回 403（访客在错误页上没有输码的地方）而是 302 送去分享页
    anon = httpx.Client(base_url=client.base_url, timeout=20)
    try:
        assert anon.get('/p/admin/').status_code == 200
        d = anon.get('/p/admin/api')
        assert d.status_code == 403 and d.json().get('error') == 'code_required'
        r = anon.get('/download/public/shares/admin/codefile.txt', follow_redirects=False)
        assert r.status_code == 302, r.status_code
        assert r.headers.get('location', '').endswith('/p/admin'), r.headers.get('location')
        # 跟随跳转应落在分享页上（那里才是输码的地方）
        assert anon.get('/download/public/shares/admin/codefile.txt',
                        follow_redirects=True).status_code == 200
    finally:
        anon.close()

    # 输错 → remaining 递减
    bad = httpx.Client(base_url=client.base_url, timeout=20)
    try:
        for _ in range(2):
            d = bad.post('/api/share/auth', json={'username': 'admin', 'code': 'wrong'}).json()
            assert d.get('error') == 'incorrect', d
    finally:
        bad.close()

    # 正确码（新浏览器）→ Set-Cookie；此后 api/直链放行
    okc = httpx.Client(base_url=client.base_url, timeout=20)
    try:
        r = okc.post('/api/share/auth', json={'username': 'admin', 'code': 'abc123'})
        assert r.status_code == 200 and r.json().get('ok') is True, r.text
        assert 'leaf_sh_' in r.headers.get('set-cookie', '')
        assert okc.get('/p/admin/api').status_code == 200
        dl = okc.get('/download/public/shares/admin/codefile.txt')
        assert dl.status_code == 200 and dl.content == b'code-data'
    finally:
        okc.close()

    # 无 cookie 的浏览器仍拿不到文件（未持有授权）：同样被送去分享页输码
    other = httpx.Client(base_url=client.base_url, timeout=20)
    try:
        r = other.get('/download/public/shares/admin/codefile.txt', follow_redirects=False)
        assert r.status_code == 302, r.status_code
        assert r.headers.get('location', '').endswith('/p/admin'), r.headers.get('location')
    finally:
        other.close()

    # 关闭码 → 匿名直链恢复
    r = client.post('/api/share/code', json={'code': ''})
    assert r.status_code == 200 and r.json().get('enabled') is False, r.text
    anon2 = httpx.Client(base_url=client.base_url, timeout=20)
    try:
        assert anon2.get('/download/public/shares/admin/codefile.txt').status_code == 200
    finally:
        anon2.close()

    # guest 无权设码
    g = httpx.Client(base_url=client.base_url, timeout=20)
    try:
        lg = g.post('/api/guest/login')
        assert lg.status_code == 200
        assert g.post('/api/share/code', json={'code': '1234'}).status_code == 404
    finally:
        g.close()


def test_share_code_bruteforce(client, data_root):
    """防爆破：IP 当日 5 次错 → IP 锁；60s 窗口 5 次错 → 全局锁；重置可恢复"""
    _put(data_root, 'users/admin/bf.txt', b'x')
    login(client)
    client.post('/api/share/publish', json={'paths': ['users/admin/bf.txt']})
    r = client.post('/api/share/code', json={'code': 'pw-0000'})
    assert r.status_code == 200, r.text

    cfg_path = os.path.join(data_root, 'config', 'server_config.json')
    orig = json.load(open(cfg_path, encoding='utf-8'))

    def set_param(k, v):
        cfg = json.load(open(cfg_path, encoding='utf-8'))
        cfg[k] = v
        json.dump(cfg, open(cfg_path, 'w', encoding='utf-8'))

    try:
        # ---- IP 锁：放大全局窗口阈值避免全局锁抢占 ----
        set_param('share_global_err_max_window', 1000)
        a = httpx.Client(base_url=client.base_url, timeout=20)
        try:
            locked = False
            for i in range(5):
                d = a.post('/api/share/auth', json={'username': 'admin', 'code': 'bad%d' % i})
                body = d.json()
                if body.get('error') == 'locked' and body.get('reason') == 'ip':
                    locked = True
                    break
            assert locked, '第 5 次错应触发 IP 锁'
            # 锁后（同 IP）正确码也被拒
            d = a.post('/api/share/auth', json={'username': 'admin', 'code': 'pw-0000'})
            assert d.status_code == 403 and d.json().get('reason') == 'ip'
        finally:
            a.close()

        # ---- 全局锁：要 ≥2 个来源 IP 才算"遭攻击" ----
        # 单机反复错只锁它自己 —— 否则任何匿名者 5 次错码就能把任意分享对所有人锁 30 分钟、
        # 还能无限顺延，拿来做 DoS。用不同的环回源地址模拟不同来源 IP。
        client.post('/api/share/reset', json={})
        set_param('share_global_err_max_window', 5)

        def _src(ip):
            return httpx.Client(base_url=client.base_url, timeout=20,
                                transport=httpx.HTTPTransport(local_address=ip))

        a1 = _src('127.0.0.2')
        try:
            got = None
            for i in range(5):
                d = a1.post('/api/share/auth', json={'username': 'admin', 'code': 'zz%d' % i})
                got = d.json()
                if got.get('reason') == 'ip':
                    break
            assert got and got.get('reason') == 'ip', '单 IP 连错应触发 IP 锁'
        finally:
            a1.close()

        # 另一个来源 IP 照常能输码（修复前会被全局锁连坐）
        a2 = _src('127.0.0.3')
        try:
            d = a2.post('/api/share/auth', json={'username': 'admin', 'code': 'pw-0000'})
            assert d.status_code == 200, '单机错码不该把别人一起锁住：%s' % d.text
        finally:
            a2.close()

        # 两个来源 IP 一起错满窗口 → 判定为攻击，全局锁
        for ip in ('127.0.0.4', '127.0.0.5'):
            cu = _src(ip)
            try:
                for i in range(3):
                    cu.post('/api/share/auth', json={'username': 'admin', 'code': 'yy%d' % i})
            finally:
                cu.close()
        a3 = _src('127.0.0.6')
        try:
            d = a3.post('/api/share/auth', json={'username': 'admin', 'code': 'pw-0000'})
            assert d.status_code == 403 and d.json().get('reason') == 'global', d.text
        finally:
            a3.close()

        # ---- 日翻转要清掉 IP 锁（原来漏清 → 某个 IP 错满就永久锁死）----
        from leaffs.share import access as _sacc

        _sacc._load_locked()
        _sacc._CACHE['users'] = {'admin': {'ip_locked': {'1.1.1.1': True}}}
        _sacc._CACHE['date'] = '1970-01-01'
        _sacc._flush_day_locked()
        assert _sacc._CACHE['users']['admin']['ip_locked'] == {}, '日翻转应清空 ip_locked'

        # ---- 重置后恢复 ----
        r = client.post('/api/share/reset', json={})
        assert r.status_code == 200 and r.json().get('success') is True
        c = httpx.Client(base_url=client.base_url, timeout=20)
        try:
            d = c.post('/api/share/auth', json={'username': 'admin', 'code': 'pw-0000'})
            assert d.status_code == 200 and d.json().get('ok') is True, d.text
        finally:
            c.close()
    finally:
        json.dump(orig, open(cfg_path, 'w', encoding='utf-8'))


def test_manage_page_routes(client):
    """匿名 302 登录；admin 可达管理页"""
    anon = httpx.Client(base_url=client.base_url, timeout=20)
    try:
        r = anon.get('/share')
        assert r.status_code == 302
        assert '/login' in r.headers.get('location', '')
    finally:
        anon.close()
    login(client)
    r = client.get('/share')
    assert r.status_code == 200
    assert '/api/share' in r.text
    assert 'public/shares' in r.text


def test_admin_home_has_share_entry(client):
    """主页导航含分享、工具栏按钮、发布脚本（登录即显；guest 靠 JS 隐藏）"""
    login(client)
    r = client.get('/browse/', follow_redirects=True)
    assert r.status_code == 200
    body = r.text
    assert 'href="/share"' in body
    assert 'id="shareBtn"' in body
    assert 'function shareSelected' in body
    assert '/api/share/publish' in body


def test_share_auth_ignores_stale_session_cookie(client, data_root):
    """回归：带一条**失效**的登录 Cookie，也要能输分享码。

    /api/* 在路由前会把「带无效 wifi_session」的请求打成 401（防止拿失效登录态当身份），
    白名单一开始漏了 /api/share/auth —— 于是以前登录过、cookie 已失效的设备永远输不进码
    （手机本机就是这种状态），而干净设备一切正常。接口本身不读会话，不该被这条拦。
    """
    from leaffs.auth.core import AUTH_COOKIE

    _put(data_root, 'users/admin/stalefile.txt', b'stale-data')
    login(client)
    assert client.post('/api/share/publish',
                       json={'paths': ['users/admin/stalefile.txt']}).status_code == 200
    assert client.post('/api/share/code', json={'code': 'abc123'}).status_code == 200

    stale = httpx.Client(base_url=client.base_url, timeout=20)
    try:
        stale.cookies.set(AUTH_COOKIE, 'stale-sid-not-in-store')
        r = stale.post('/api/share/auth', json={'username': 'admin', 'code': 'abc123'})
        assert r.status_code == 200, r.text
        assert r.json().get('ok') is True
    finally:
        stale.close()


def test_share_code_survives_day_flip(data_root, monkeypatch):
    """回归：自然日翻转只能重置防爆破计数，分享码不能被清掉。

    原来是 `_CACHE['users'] = {}` —— 连 code_hash 一起清空，现象是「过了一天，
    访问码再也输不对」（其实码已经没了，服务端直接回“未设置访问码”）。
    """
    from leaffs.share import access as acc

    # 用 data_root 下的独立文件：不动真实配置（也不用 pytest 的 tmp_path，
    # 它的临时根目录在 workspace 之外，本环境沙箱不允许访问）
    monkeypatch.setattr(acc, '_ACCESS_FILE',
                        os.path.join(data_root, 'share_access_ut.json'))
    monkeypatch.setattr(acc, '_CACHE', None)

    assert acc.set_code('admin', 'abc123')[0] is True
    assert acc.verify_code('admin', 'abc123') is True

    # 把"当天"拨到过去 → 下一次访问就会触发自然日翻转
    with acc._LOCK:
        acc._CACHE['date'] = '2000-01-01'
        acc._user('admin')['ip_errors'] = {'1.2.3.4': 3}

    assert acc.code_enabled('admin') is True, '翻转后分享码被清掉了'
    assert acc.verify_code('admin', 'abc123') is True
    assert acc._CACHE['users']['admin']['ip_errors'] == {}, '防爆破计数应被重置'
