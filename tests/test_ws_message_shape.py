# -*- coding: utf-8 -*-
"""WS 消息的形状契约：畸形输入**不许把连接打崩**。

`ws_handler` 的消息循环外面只兜了 `ConnectionClosed`，其它异常会冒出去 →
websockets 以 **1011** 关掉整条连接、并刷一整段堆栈日志（带服务器绝对路径）。

而解析之后的代码直接假定「是 dict」＋「各字段是 str / list[str]」，共 **7 处**：

  * **非对象 JSON**（`"abc"` / `123` / `[1,2]` / `null`）→ `data.get(...)` → AttributeError
  * `token` / `sid` / `path` / `name` 非 str → `(v or '').strip()` / `p + '/'` → AttributeError / TypeError
  * `paths` 非 list → `for p in paths` → TypeError；**是字符串则逐字符当路径**（不崩但语义错）

任何能连上 WS 的客户端都能触发，**匿名就行、不需要登录**（`auth`/`qr-sub`/`ping` 都在匿名白名单里）。

修法：解析后加一次形状校验 + 两个取值助手（`ws.py` 的 `_ws_str` / `_ws_str_list`）。

本文件每类都用一个 **ping 探针**：发完畸形消息紧接着发 `{"type":"ping"}`，
必须收到 `pong` —— 这就是"连接还活着"的判据（修之前会变成连接被打断）。
"""
import json

import httpx
import pytest

from conftest import WS_PORT, HTTP_PORT, login

WS_URL = 'ws://127.0.0.1:%d' % WS_PORT

CASES = (
    ('非对象 JSON：字符串', '"abc"', True),
    ('非对象 JSON：数字', '123', True),
    ('非对象 JSON：数组', '[1,2]', True),
    ('非对象 JSON：null', 'null', True),
    ('压根不是 JSON', 'not json at all', True),
    # 二进制帧：内容不是合法 UTF-8 时 `json.loads(bytes)` 抛 UnicodeDecodeError ——
    # 原来没捕这个异常，它会冒出消息循环、把连接以 **1011** 打掉
    # （测试者报的 `close 0x03f3` 就是它，W1 漏掉的第 9 类输入）。
    ('二进制帧：非 UTF-8', b'\xff\xfe\x80', True),
    ('二进制帧：合法 JSON', json.dumps({'type': 'ping'}).encode(), False),
    ('sid 非字符串（qr-sub）', json.dumps({'type': 'qr-sub', 'sid': 123}), False),
    ('path 非字符串（list）', json.dumps({'type': 'list', 'path': 123}), False),
    ('paths 非数组（delete）', json.dumps({'type': 'delete', 'paths': 123}), False),
    ('paths 是字符串（delete）', json.dumps({'type': 'delete', 'paths': 'abc'}), False),
    ('name 非字符串（mkdir）', json.dumps({'type': 'mkdir', 'path': 'public', 'name': 456}), False),
    ('type 非字符串', json.dumps({'type': 123}), True),
)
# 第三列 = 是否应当**明确回一条"格式不合法"**：
#   帧级/信封级的问题（不是 JSON、不是对象、type 非字符串、非 UTF-8 二进制）→ 回错；
#   字段级的问题（token/path/paths/name 类型不对）→ 按既有口径"当空"走正常分支，
#   本来就有一条业务应答（如 qr-sub 的业务错误），不需要再叠一条格式错。
#
# 2026-09-21：删掉原来的一例 `token 非字符串（auth）` —— 它的 type 是 `auth`，
#   而 auth 消息分支已于 2026-09-16 删除（全仓无发送方）。那个用例此前"通过"，
#   只是因为 auth 落到链尾被**静默丢弃**（既无 error 也无业务应答），恰好满足
#   want_error=False；链尾补上兜底分支之后它才暴露出来。
#   「字段级问题 → 走业务分支」这层语义由 qr-sub 一例覆盖，删掉不减少覆盖面。


def _connect(cookie=None):
    from websockets.sync.client import connect
    headers = {'Cookie': 'wifi_session=%s' % cookie} if cookie else None
    return connect(WS_URL, additional_headers=headers, open_timeout=10)


@pytest.fixture(scope='module')
def ws_cookie(server):
    """**已登录**的会话 Cookie —— 必须用它，否则 `list`/`delete`/`mkdir` 这些
    「不在匿名白名单里」的消息会被提前拦下，压根走不到字段处理，
    那几处崩点就测不到（第一版就是这么漏掉的）。"""
    with httpx.Client(base_url='http://127.0.0.1:%d' % HTTP_PORT, timeout=20) as c:
        login(c)
        cookie = c.cookies.get('wifi_session')
    assert cookie
    return cookie


@pytest.fixture(scope='module')
def ws_client(server):
    """带 `server` 的占位夹具：保证服务已启动（用例依赖 `ws_cookie` 即可）"""
    return server


def test_malformed_messages_do_not_kill_the_connection(ws_client, ws_cookie):
    """11 类畸形消息逐个发，每类之后 ping 必须回 pong（连接不能被打断）

    **每个用例用一条独立连接**：一处崩掉不该掩盖后面的用例（第一版共用一个连接 +
    `break`，结果只看到第 1 类，后面 10 类根本没被验证）。
    """
    from websockets.exceptions import ConnectionClosed

    bad = []
    for name, payload, want_error in CASES:
        try:
            with _connect(ws_cookie) as ws:
                ws.send(payload)
                ws.send(json.dumps({'type': 'ping'}))
                got_pong = False
                got_error = False
                last = ''
                # 一路读到 pong：格式错的消息会先回一条 error，然后才是 pong
                for _ in range(6):
                    last = ws.recv(timeout=5)
                    if '"pong"' in last:
                        got_pong = True
                        break
                    if '"type": "error"' in last or '"type":"error"' in last:
                        got_error = True
                if not got_pong:
                    bad.append('%s：发了 ping 也没等到 pong（最后收到 %r）' % (name, last[:100]))
                if want_error and not got_error:
                    bad.append('%s：格式不合法却**没有任何应答**（客户端无法区分'
                               '"不支持"与"服务端卡住"）' % name)
                if not want_error and got_error:
                    bad.append('%s：字段级问题不该再叠一条格式错（有业务应答就够了）' % name)
        except ConnectionClosed as e:
            bad.append('%s：**连接被打断** %s' % (name, e))
    assert not bad, '畸形 WS 消息处理不对：\n  ' + '\n  '.join(bad)


def test_shape_helpers_reject_non_string_inputs():
    """白盒：两个取值助手的形状语义

    （黑盒那条只能证明"连接没崩"；**"字符串被逐字符当路径"这种语义错它抓不到** ——
    不存在的单字符路径会被静默跳过、failed 里不留条目，所以得在这里直接钉住取值语义。）
    """
    from leaffs.server import ws as W

    assert W._ws_str({'token': 'x'}, 'token') == 'x'
    assert W._ws_str({'token': 123}, 'token') == '', '非字符串必须当空，不能原样带下去'
    assert W._ws_str({'token': None}, 'token') == ''
    assert W._ws_str({}, 'token') == ''
    assert W._ws_str({'token': 123}, 'token', 'dflt') == 'dflt'

    assert W._ws_str_list({'paths': ['a', 'b']}, 'paths') == ['a', 'b']
    assert W._ws_str_list({'paths': 'abc'}, 'paths') == [], \
        '字符串不能被当成路径列表（修之前会逐字符迭代，把 "a"/"b"/"c" 当路径）'
    assert W._ws_str_list({'paths': 123}, 'paths') == []
    assert W._ws_str_list({'paths': [1, 'a', None, {}]}, 'paths') == ['a'], \
        '列表里的非字符串元素要剔掉'
    assert W._ws_str_list({}, 'paths') == []


# 合法字符串、但服务端认不出的 type。
# 与上面的"畸形"不同类：那些进不了分发链，这些能走完全程却没落到任何分支。
UNKNOWN_TYPE_CASES = (
    ('未知类型', json.dumps({'type': 'nosuchtype'})),
    ('未知类型：大小写变体', json.dumps({'type': 'PING'})),
    ('type 为空串', json.dumps({'type': ''})),
    ('缺少 type', json.dumps({'foo': 1})),
)


def test_unknown_type_gets_an_explicit_error(ws_client, ws_cookie):
    """认不出的 type 必须回一条明确错误，不能静默丢弃。

    修之前的表现（外部测试反馈）：**匿名**连接回 `Unauthorized`（在 A-17
    「未认证最小响应集」处就被拦下），而**游客/管理员**连接走完整个 if/elif
    链、链尾没有兜底分支 → 完全无响应，客户端只能一直等。

    所以这条用例必须带**已登录**的 cookie —— 用匿名连接测的话，走的还是
    `Unauthorized` 那条老路，链尾有没有兜底根本测不出来。
    """
    bad = []
    for name, payload in UNKNOWN_TYPE_CASES:
        with _connect(ws_cookie) as ws:
            ws.send(payload)
            got_error = False
            last = ''
            try:
                # 只可能来一条 error；给足 5 秒，超时即"服务端没理我"
                for _ in range(4):
                    last = ws.recv(timeout=5)
                    if '"type": "error"' in last or '"type":"error"' in last:
                        got_error = True
                        break
            except TimeoutError:
                pass
            if not got_error:
                bad.append('%s：没有任何应答（最后收到 %r）' % (name, last[:100] or '无'))
    assert not bad, (
        '未知 type 必须明确回错，否则客户端分不清「服务端不支持」与「服务端卡住」：\n  '
        + '\n  '.join(bad)
    )


def test_unknown_type_does_not_break_the_connection(ws_client, ws_cookie):
    """收到未知 type 之后，连接要照常可用（不关连接、不丢后续消息）。

    与 `_ws_proto_error` 的既定口径一致：一条错消息不该让客户端掉线。
    """
    with _connect(ws_cookie) as ws:
        ws.send(json.dumps({'type': 'nosuchtype'}))
        ws.send(json.dumps({'type': 'ping'}))
        got_pong = False
        for _ in range(6):
            try:
                if '"pong"' in ws.recv(timeout=5):
                    got_pong = True
                    break
            except TimeoutError:
                break
        assert got_pong, '未知 type 之后连接不可用了（没等到 pong）'
