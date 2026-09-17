# -*- coding: utf-8 -*-
"""认证加固的回归测试：首启无口令、令牌并发只兑现一次、深层配置字段分级"""
import os
import threading

import httpx

from conftest import login


def test_first_boot_super_admin_has_no_password(monkeypatch, data_root):
    """首启不写口令：空口令/猜测都登不进；自己设置口令之后才能用密码登录"""
    import leaffs.auth.core as C

    cfg = os.path.join(data_root, 'auth_cfg')
    os.makedirs(cfg, exist_ok=True)
    monkeypatch.setattr(C, 'USERS_FILE', os.path.join(cfg, 'users.json'))
    monkeypatch.setattr(C, '_users', {})

    C.load_users()
    assert C.has_password('admin') is False
    assert C.is_default_admin_password() is True
    assert C.verify_login('admin', '') is None, '空口令不能登录'
    assert C.verify_login('admin', 'admin') is None

    ok, err = C.self_change_password('admin', 'mynewpass123')
    assert ok, err
    assert C.has_password('admin') is True
    assert C.is_default_admin_password() is False
    assert C.verify_login('admin', 'mynewpass123') == 'super_admin'
    assert C.verify_login('admin', '') is None, '设过口令后空口令依然不能登录'


def test_local_token_is_one_shot_even_concurrently(monkeypatch, data_root):
    """令牌一次性：并发打同一个令牌也只能兑现一个会话（原来是两条无锁语句）"""
    from leaffs.auth import local_token as lt

    cfg = os.path.join(data_root, 'tok_cfg')
    os.makedirs(cfg, exist_ok=True)
    monkeypatch.setattr(lt, 'CONFIG_DIR', cfg)
    monkeypatch.setattr(lt, 'LOCAL_TOKEN_FILE', os.path.join(cfg, 'local_token.txt'))

    tok = lt.reset_and_write()
    assert lt.get_current() == tok
    assert lt.try_consume('nope') is False
    assert lt.get_current() == tok, '错误的候选不该消耗令牌'

    out = []
    out_lock = threading.Lock()

    def worker():
        r = lt.try_consume(tok)
        with out_lock:
            out.append(r)

    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert out.count(True) == 1, '并发下同一个令牌只能成功一次'
    assert lt.get_current() is None
    assert lt.try_consume(tok) is False


def test_get_session_has_no_auth_bypass_switch():
    """get_session 不该有"关闭鉴权"的开关：没 Cookie 就是匿名，不可能变成超管。

    旧签名是 `get_session(cookie, auth_enabled, ip)`，函数第一行
    `if not auth_enabled: return 'super_admin', ''` —— 传 False 直接是超管。
    当时全仓已无调用点（都传 True），属于定时炸弹，已连同参数一起删除。
    """
    import inspect

    from leaffs.auth import core as C

    assert 'auth_enabled' not in inspect.signature(C.get_session).parameters, \
        '认证开关不能被加回来'
    assert C.get_session('', '127.0.0.1') == (None, ''), '没 Cookie 必须是匿名'
    assert C.get_session('wifi_session=nope', '127.0.0.1') == (None, ''), '无效会话必须是匿名'


def test_deep_config_security_fields_need_super_admin(client, data_root):
    """关审计日志 / 改会话寿命 / 下调口令哈希强度：只有超管能改"""
    login(client)   # 内置 admin 是 super_admin
    r = client.post('/api/config/deep', json={'access_log': False})
    assert r.status_code == 200, '超管应能改：%s' % r.text

    name, pw = 'plainadmin', 'plainadmin123'
    r = client.post('/api/users/add', json={'username': name, 'password': pw})
    assert r.status_code == 200 and r.json().get('success'), r.text
    r = client.post('/api/users/role', json={'username': name, 'role': 'admin'})
    assert r.status_code == 200 and r.json().get('success'), r.text

    other = httpx.Client(base_url=client.base_url, timeout=20)
    try:
        r = other.post('/api/auth/login', json={'username': name, 'password': pw})
        assert r.status_code == 200, '普通 admin 应能登录：%s' % r.text

        r = other.post('/api/config/deep', json={'access_log': False})
        assert r.status_code == 404, '普通 admin 不该能关审计日志：%s' % r.text
        r = other.post('/api/config/deep', json={'pbkdf2_iterations': 100000})
        assert r.status_code == 404, '普通 admin 不该能下调哈希强度：%s' % r.text

        # 非安全字段照常能改，别把普通 admin 一并锁死
        r = other.post('/api/config/deep', json={'folder_size_ttl': 60})
        assert r.status_code == 200, r.text
    finally:
        other.close()
