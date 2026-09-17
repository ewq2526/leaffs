# -*- coding: utf-8 -*-
"""WS 的 Origin/Host 校验必须发生在**握手之前**（LF-14 回归）。

黑盒报告的现象（已核实）：`websockets.serve(..., process_request=_ws_http_probe)` 里的
`process_request` 本来就在握手前跑、能直接拒掉不发 101，但它当时**只查 `Upgrade` 头**；
真正的 Host/Origin 校验收在 `ws_handler` 里 —— 而 `websockets` **调用 handler 之前
101 已经发出去了**。于是不合法的连接照样完成握手、进入单 IP 连接计数，
然后才被 `close(1008, 'origin/host check failed')` 踢掉。

报告引的 `1008` 和 reason 与当时的代码逐字吻合。

现在：校验在 `process_request` 里做，不过就回 **HTTP 404**（`InvalidStatus`），
连 101 都不发；`ws_handler` 里那段重复判定删掉了（握手后必然成立，纯属白跑）。
（2026-09-15 统一拒绝口径：原来是 403，改成 404 —— 403 等于告诉对方
"这个 WS 端点存在，只是你的 Origin/Host 不被接受"。）

⚠️ 测试连的是**默认路径 `/`**（前端实际用 `/ws`）：路径不参与判定，这是有意的 ——
真正挡跨站的是 Origin 校验，path 检查拦不住任何有能力的攻击者，却会挡住别的客户端。
"""
import asyncio

import pytest
import websockets
from websockets.exceptions import InvalidStatus

from conftest import HTTP_PORT, WS_PORT

WS_URL = 'ws://127.0.0.1:%d' % WS_PORT
GOOD_ORIGIN = 'http://127.0.0.1:%d' % HTTP_PORT     # 本站主站端口
BAD_ORIGIN = 'http://evil.example'


def _connect(**headers):
    return websockets.connect(WS_URL, additional_headers=headers, open_timeout=10)


def test_bad_origin_is_rejected_before_handshake(server):
    """非法 Origin：握手阶段就被拒（404，而不是连上之后再 close 1008）"""
    async def go():
        async with _connect(Origin=BAD_ORIGIN):
            return None

    with pytest.raises(InvalidStatus) as ei:
        asyncio.run(go())
    assert ei.value.response.status_code == 404, ei.value.response


def test_origin_on_wrong_port_is_rejected_before_handshake(server):
    """同主机但端口不对：也拒（SameSite 只比主机名不比端口，这道校验是唯一防线）"""
    async def go():
        async with _connect(Origin='http://127.0.0.1:9999'):
            return None

    with pytest.raises(InvalidStatus) as ei:
        asyncio.run(go())
    assert ei.value.response.status_code == 404, ei.value.response


def test_good_origin_connects(server):
    """合法 Origin（本站主站端口）→ 正常连上（回归）"""
    async def go():
        async with _connect(Origin=GOOD_ORIGIN) as ws:
            await ws.send('{"type":"ping"}')
            return await asyncio.wait_for(ws.recv(), timeout=10)

    msg = asyncio.run(go())
    assert 'pong' in msg, msg


def test_native_client_without_origin_still_connects(server):
    """无 Origin 的原生客户端仍放行（回归：报告也确认这是设计）"""
    async def go():
        async with websockets.connect(WS_URL, open_timeout=10) as ws:
            await ws.send('{"type":"ping"}')
            return await asyncio.wait_for(ws.recv(), timeout=10)

    msg = asyncio.run(go())
    assert 'pong' in msg, msg


def test_plain_get_on_ws_port_still_gets_plain_text(server):
    """非 WS 的普通请求仍是「200 + 一行纯文本」—— `_ws_http_probe` 的原行为不能坏

    这条是为 App 冷启动预热证书例外服务的：WebView 直接打开 8081 时会发普通 GET，
    必须给个正常响应，否则 websockets 会刷一整段 InvalidUpgrade 堆栈。
    """
    import httpx
    r = httpx.get('http://127.0.0.1:%d/' % WS_PORT, timeout=10)
    assert r.status_code == 200, r.text
    assert 'WebSocket' in r.text, r.text


def test_rejection_is_logged(server, data_root):
    """拒绝要留痕（WS 失败在浏览器里是静默的，日志是唯一线索）"""
    async def go():
        async with _connect(Origin=BAD_ORIGIN):
            return None

    with pytest.raises(InvalidStatus):
        asyncio.run(go())

    import os
    log = os.path.join(data_root, 'leaffs.log')
    with open(log, encoding='utf-8', errors='replace') as f:
        text = f.read()
    assert 'WS Origin/Host 校验失败' in text, '拒绝没留日志，出问题就查不出来'
