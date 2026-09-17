# -*- coding: utf-8 -*-
"""Cookie 解析口径收成一份（§三 ③-2）。

**问题**：全仓有 **5 处**手写 Cookie 解析，而且**现在已经不一致**：

| 位置 | 写法 | 同名 cookie 取哪个 |
|---|---|---|
| `auth/core.get_session` | `'=' in part` + `split('=', 1)` | 最后一个 |
| `server/handler._has_invalid_session_cookie` | `startswith(NAME + '=')` | 最后一个 |
| `share/access._cookie_ok` | `partition('=')` | 最后一个 |
| `web/render._cookie_accent` | `partition('=')` + 立即 return | **第一个** |
| `web/render._cookie_theme` | 同上 | **第一个** |

同名 Cookie 现实中不多见（浏览器写同名是覆盖），但"谁先谁后"这种语义分散在五份拷贝里
迟早出事 —— LF-27 的"列表构建两份实现"就是这么漂移的。修法 = 收成一份
`utils/core.parse_cookies()`（同名取**最后一个**，与 RFC 6265、浏览器行为、
以及前 3 处现状一致），5 处都调它。
"""
wifi = 'wifi_session'


# ---------- 1. 唯一那份解析函数 ----------

def test_parse_cookies_basics():
    from leaffs.utils.core import parse_cookies
    assert parse_cookies('a=1; b=2') == {'a': '1', 'b': '2'}
    # 没有 '=' 的段（畸形）跳过，不炸
    assert parse_cookies('a=1; junk; b=2') == {'a': '1', 'b': '2'}
    # 值里含 '='：只按第一个 '=' 切
    assert parse_cookies('a=x=y') == {'a': 'x=y'}
    # 两侧空白
    assert parse_cookies(' a = 1 ; b = 2 ') == {'a': '1', 'b': '2'}
    # 空 / None
    assert parse_cookies('') == {}
    assert parse_cookies(None) == {}
    # 空值也是"有这个名字"
    assert parse_cookies('a=') == {'a': ''}


def test_parse_cookies_same_name_takes_last():
    """★ 同名取最后一个 —— 全仓口径就定在这里"""
    from leaffs.utils.core import parse_cookies
    assert parse_cookies('a=1; a=2') == {'a': '2'}
    assert parse_cookies('sid=dead; sid=alive')['sid'] == 'alive'


# ---------- 2. ★ 旧代码上必红：页面主题两处原本"取第一个" ----------

def test_accent_cookie_same_name_takes_last():
    """同名且**都合法**时取最后一个

    ⚠️ 旧代码不是简单"取第一个"，而是"**取第一个合法值**"（非法值会被跳过）——
    所以拿"合法+非法"去测，旧代码照样绿，等于没有牙。必须两个值都合法才验得出。

    现实里同名 Cookie 通常不会并存（浏览器写同名是覆盖），只有 path/domain 不同才可能
    同时出现；统一成"最后一个"更贴近"最后写下的生效"。
    """
    from leaffs.web import render as _wm
    assert _wm._cookie_accent('leaf_accent=green; leaf_accent=red') == 'red', \
        '同名时没取最后一个（旧代码在这里给 green）'
    assert _wm._cookie_accent('leaf_accent=green') == 'green'
    assert _wm._cookie_accent('leaf_accent=nope') == ''      # 非法值仍然退回默认


def test_theme_cookie_same_name_takes_last():
    """`_cookie_theme` 同理（旧代码给第一个合法值 dark）"""
    from leaffs.web import render as _wm
    assert _wm._cookie_theme('leaf_theme=dark; leaf_theme=light') == 'light', \
        '同名时没取最后一个（旧代码在这里给 dark）'
    assert _wm._cookie_theme('leaf_theme=dark') == 'dark'
    assert _wm._cookie_theme('leaf_theme=nope') == ''


# ---------- 3. 会话解析与"无效会话预检"必须同一口径 ----------

def test_session_and_precheck_agree_on_same_name():
    """同名 wifi_session 时，两处入口必须取同一个 —— 否则一个放行、一个当匿名

    ⚠️ 这里是**单元级**、不走 HTTP：一开始写成端到端，但 httpx 对同名 Cookie 头的
    处理不可控（实测服务端拿到的顺序并不是我给的那个），端到端验不了"取第几个"。
    直接构造承载两处判定的对象更准，也更能说明问题。

    不调 `create_session`：那会往真实 `config/sessions.json` 落盘（LF-29 之后）。
    直接塞一个有 IP 绑定的内存会话，用完清掉。
    """
    import time as _t
    import leaffs.auth.core as _ac
    from leaffs.server.handler import HTTPHandler

    sid = 'testsid' + 'x' * 24
    with _ac._sessions_lock:
        _ac._sessions[sid] = {'expiry': _t.time() + 3600, 'username': 'admin',
                              'role': 'super_admin', 'ip': '127.0.0.1'}
    h = HTTPHandler.__new__(HTTPHandler)          # 不跑 __init__，只借判定方法
    h.client_address = ('127.0.0.1', 0)
    try:
        # 有效在前、无效在后 ⇒ 两处都取"无效"
        bad_last = '%s=%s; %s=deadbeef' % (wifi, sid, wifi)
        assert _ac.get_session(bad_last, '127.0.0.1') == (None, ''), \
            'get_session 没取最后一个同名 cookie'
        h.headers = {'Cookie': bad_last}
        assert h._has_invalid_session_cookie() is True, \
            '无效会话预检没取最后一个同名 cookie'

        # 反过来：有效的在后 ⇒ 两处都取"有效"
        good_last = '%s=deadbeef; %s=%s' % (wifi, wifi, sid)
        role, got = _ac.get_session(good_last, '127.0.0.1')
        assert role == 'super_admin' and got == sid, (role, got)
        h.headers = {'Cookie': good_last}
        assert h._has_invalid_session_cookie() is False, \
            '两处口径不一致：会话解析认了，预检却判为无效'
    finally:
        with _ac._sessions_lock:
            _ac._sessions.pop(sid, None)


# ---------- 4. 文本守卫：这 5 个模块不许再手写 Cookie 拆分 ----------

_SOURCES = (
    'leaffs/auth/core.py',
    'leaffs/server/handler.py',
    'leaffs/share/access.py',
    'leaffs/web/render.py',
)


def test_no_handwritten_cookie_split_left():
    """解析只有一份：这几个模块里不该再有 `split(';')` 这种手写拆分

    这条锁的是**设计决定**（LF-27 的教训：两份实现必然漂移），不是实现细节 ——
    以后要在别处解析 Cookie，请调 `utils/core.parse_cookies`。
    """
    import os
    import leaffs
    root = os.path.dirname(os.path.abspath(leaffs.__file__))
    bad = []
    for rel in _SOURCES:
        path = os.path.join(root, os.path.basename(os.path.dirname(rel)), os.path.basename(rel))
        with open(path, encoding='utf-8') as f:
            for i, line in enumerate(f, 1):
                if "split(';')" in line and not line.lstrip().startswith('#'):
                    bad.append('%s:%d %s' % (rel, i, line.strip()))
    assert not bad, '仍有手写的 Cookie 拆分，应收成 parse_cookies：\n' + '\n'.join(bad)
