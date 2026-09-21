# -*- coding: utf-8 -*-
"""WS 的 delete 分支：什么都没做就不许报成功（WS-D）。

外部黑盒报告（2026-09-21）把这条判成「死分支且报假成功」：
任何身份发 `{"type":"delete"}` 或 `{"type":"delete","files":[...]}` 都回
`{"type":"delete","success":true,"deleted":0}`，文件纹丝不动。

真相是**字段名对不上**：这个分支读的是 `paths`，而报告按 HTTP 那边的习惯发了
`files`（HTTP 的 `/api/delete` 收的正是 `files`）—— 列表为空 → 循环不执行 →
`failed` 为空 → `success: not failed` 恒真。分支本身是好的：路径权限、
`ws_write_allowed`、真删除都在下面。

所以这条的实质与 N-10 同类 —— **假成功**：请求什么都没做，调用方却收到成功。
一个"删掉了 0 个"的响应，读起来和"删成功了但本来就没有"完全一样。

本文件钉两件事：空列表必须回 `success:false`；给了真实路径必须**真的删掉**
（证明它不是死分支，只是字段名对不上）。
"""
import json

import httpx
import pytest

from conftest import WS_PORT, HTTP_PORT, login

WS_URL = 'ws://127.0.0.1:%d' % WS_PORT


@pytest.fixture(scope='module')
def ws_cookie(server):
    """已登录的会话 Cookie —— delete 不在匿名白名单里，没有它走不到字段处理"""
    with httpx.Client(base_url='http://127.0.0.1:%d' % HTTP_PORT, timeout=20) as c:
        login(c)
        cookie = c.cookies.get('wifi_session')
    assert cookie
    return cookie


def _connect(cookie):
    from websockets.sync.client import connect
    return connect(WS_URL, additional_headers={'Cookie': 'wifi_session=%s' % cookie},
                   open_timeout=10)


def _send_delete(cookie, payload):
    """发一条 delete，读回它的应答（error 消息不算，等 type=delete 那条）"""
    with _connect(cookie) as ws:
        ws.send(json.dumps(payload))
        for _ in range(6):
            msg = json.loads(ws.recv(timeout=5))
            if msg.get('type') == 'delete':
                return msg
    raise AssertionError('没等到 delete 应答')


@pytest.mark.parametrize('payload', [
    {'type': 'delete'},                                # 压根没给路径
    {'type': 'delete', 'paths': []},                   # 给了空列表
    {'type': 'delete', 'paths': 'abc'},                # 类型不对（会被当空）
    {'type': 'delete', 'files': ['whatever.txt']},     # 报告里的形态（HTTP 那边叫 files）
])
def test_nothing_to_delete_is_not_reported_as_success(ws_cookie, payload):
    msg = _send_delete(ws_cookie, payload)
    assert msg.get('success') is False, \
        'WS-D：什么都没删却回了成功，调用方会以为删掉了：%r' % (msg,)
    assert msg.get('deleted') == 0, msg
    assert msg.get('msg'), '失败必须说明原因：%r' % (msg,)


def test_real_paths_are_actually_deleted(client, ws_cookie):
    """给了真实路径必须真的删掉 —— 证明它不是"死分支"，而是字段名对不上"""
    login(client)
    r = client.post('/api/upload?path=users/admin', files={'file': ('wsdel_probe.txt', b'x')})
    assert r.status_code == 200, r.text

    def _names():
        got = client.get('/api/files', params={'path': 'users/admin'})
        assert got.status_code == 200, got.text
        return [f['name'] for f in got.json().get('files', [])]

    assert 'wsdel_probe.txt' in _names(), _names()

    msg = _send_delete(ws_cookie, {'type': 'delete', 'paths': ['users/admin/wsdel_probe.txt']})
    assert msg.get('success') is True, msg
    assert msg.get('deleted') == 1, msg

    assert 'wsdel_probe.txt' not in _names(), \
        'WS 回了删掉 1 个，磁盘上还在：%s' % _names()
