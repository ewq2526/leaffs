# -*- coding: utf-8 -*-
"""本机一次性令牌的候选形状校验（B1）。

`secrets.compare_digest` 的契约是「两个 ASCII str 或两个 bytes」：候选里只要有一个
非 ASCII 字符、或候选根本不是 str，它就抛 TypeError。旧代码把这异常交给了外层兜底，
可观测后果是：`/login?leaf=中文` → **500**；WS 的 `{type:'auth',token:'中文'}` 走的是
没有兜底的消息循环 → websockets 以 1011 关掉连接，对端拿到的是断连而不是应答。

这里钉死两件事：
1. 畸形候选一律按「不匹配」处理 —— 返回 False，不抛；
2. 畸形候选**不消耗**真令牌（TypeError 发生在 `_token = None` 之前，这个性质本来就
   成立，改成显式校验后必须继续成立）。
"""
import json
import os

from conftest import WS_PORT

# 非 ASCII 的几种形态：中文、内码不同的字符、带 emoji、带组合字符
_BAD_NON_ASCII = ('中文', '密码🔑', 'Å', 'e\u0301')


def _fresh(monkeypatch, data_root):
    """令牌文件指到 data_root 下的独立目录，返回 (模块, 真令牌)

    与 test_auth_hardening.py 同一口径；用自己的临时目录，不碰共享服务那份令牌。
    """
    from leaffs.auth import local_token as lt
    cfg = os.path.join(data_root, 'tok_shape')
    os.makedirs(cfg, exist_ok=True)
    monkeypatch.setattr(lt, 'CONFIG_DIR', cfg)
    monkeypatch.setattr(lt, 'LOCAL_TOKEN_FILE', os.path.join(cfg, 'local_token.txt'))
    return lt, lt.reset_and_write()


def test_non_ascii_candidate_rejected_not_raised(monkeypatch, data_root):
    """非 ASCII 候选按「不匹配」处理且不抛 —— 旧代码在这一步抛 TypeError"""
    lt, tok = _fresh(monkeypatch, data_root)
    for bad in _BAD_NON_ASCII + (tok + '中', '中' + tok):
        assert lt.try_consume(bad) is False, bad
        assert lt.get_current() == tok, '畸形候选不该消耗令牌: %r' % (bad,)
    assert lt.try_consume(tok) is True, '真令牌必须仍然可用'


def test_non_str_candidate_rejected(monkeypatch, data_root):
    """非 str 候选（数字/容器/bytes/布尔/对象）同样按「不匹配」处理"""
    lt, tok = _fresh(monkeypatch, data_root)
    for bad in (123, 1.5, True, None, {}, [], ['x'], b'abc', object()):
        assert lt.try_consume(bad) is False, bad
        assert lt.get_current() == tok, '畸形候选不该消耗令牌: %r' % (bad,)
    assert lt.try_consume(tok) is True, '真令牌必须仍然可用'


def test_ascii_mismatch_and_one_shot_unchanged(monkeypatch, data_root):
    """ASCII 但内容/长度不符 → False；真令牌的一次性语义不变"""
    lt, tok = _fresh(monkeypatch, data_root)
    for bad in ('', 'nope', 'x' * 200, tok[:-1], tok + 'x'):
        assert lt.try_consume(bad) is False, bad
        assert lt.get_current() == tok, '错误候选不该消耗令牌: %r' % (bad,)
    assert lt.try_consume(tok) is True
    assert lt.get_current() is None
    assert lt.try_consume(tok) is False, '一次性：用过就不认'


def test_login_with_non_ascii_leaf_is_not_500(client):
    """HTTP 端到端：非 ASCII 的 leaf 走「令牌不匹配」（200 登录页），不是 500"""
    r = client.get('/login', params={'leaf': '中文'}, follow_redirects=False)
    assert r.status_code != 500, r.text
    assert r.status_code == 200, r.status_code
    assert 'wifi_session' not in (r.headers.get('set-cookie') or ''), \
        '畸形令牌不该建出会话'


def test_auto_login_with_non_ascii_leaf_redirects_to_login(client):
    """第二个调用点同样收敛：/api/admin/auto-login 也是 500 → 302 /login"""
    r = client.get('/api/admin/auto-login', params={'leaf': '中文'},
                   follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.text)
    assert r.headers.get('location') == '/login', r.headers.get('location')


def test_ws_auth_with_non_ascii_token_stays_connected(server):
    """WS 端到端：非 ASCII token 不会把连接打崩（该消息现在按"白名单外"拒）

    B1 修的是 `local_token.try_consume` 对非 ASCII 候选抛 `TypeError` —— 旧代码里它从
    消息循环冒出去，连接被库以 **1011** 关掉，对端拿到的是 ConnectionClosed 而不是应答。

    ⚠️ **2026-09-16 起**：WS 的 `auth` 分支整体删除（核实为死代码），所以这里不再有
    `{'type':'auth',…}` 应答 —— 匿名发它按"白名单外消息"拒，回 `{'type':'error'}`。
    判据仍与当初一致：**连接不被打断**（发完紧跟一条 ping，必须回 pong）。
    """
    from websockets.sync.client import connect
    with connect('ws://127.0.0.1:%d' % WS_PORT) as ws:
        ws.send(json.dumps({'type': 'auth', 'token': '中文'}))
        msg = json.loads(ws.recv(timeout=5))
        assert msg.get('type') == 'error', \
            '匿名发 auth（非 ASCII token）应当按"白名单外消息"拒：%r' % msg
        ws.send(json.dumps({'type': 'ping'}))
        assert json.loads(ws.recv(timeout=5)).get('type') == 'pong', '连接被打断了'
