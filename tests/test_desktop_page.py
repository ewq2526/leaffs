# -*- coding: utf-8 -*-
"""桌面页：占位符要被服务端真的替换掉，注入的状态要能被页面解析。

`__DESK_STATE_JSON__` 没被替换时页面拿到的是那个普通字符串，JSON.parse 失败就退回
订阅 —— 看起来只是第一帧慢一下，实际上是这条注入链路从来没接上过。
"""
import io
import json
import os
import sys

import pytest

def _leaffs_dir():
    """两种布局都认：仓库根（app/opt/leaffs）与部署树（leaffs/）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in ('leaffs', os.path.join('app', 'opt', 'leaffs')):
        cand = os.path.abspath(os.path.join(here, '..', rel))
        if os.path.isfile(os.path.join(cand, 'web_page', 'desktop', 'desktop.html')):
            return cand
    pytest.skip('找不到 leaffs 目录（页面不在当前布局里）')


BASE_DIR = _leaffs_dir()


class FakeHandler(object):
    """只实现 serve_desktop 用到的那几样。"""

    def __init__(self, cookie=''):
        self.headers = {'Cookie': cookie}
        self.client_address = ('127.0.0.1', 12345)
        self.wfile = io.BytesIO()
        self.status = None
        self.sent = []

    def send_response(self, code):
        self.status = code

    def send_header(self, name, value):
        self.sent.append((name, value))

    def end_headers(self):
        pass

    def send_error(self, code):
        self.status = code


def _serve(cookie=''):
    from leaffs.web import render
    h = FakeHandler(cookie)
    render.serve_desktop(h, os.path.abspath(BASE_DIR),
                         lambda c, addr: ('super_admin', 'sid1'),
                         lambda sid: 'leaf')
    return h, h.wfile.getvalue().decode('utf-8')


def _state_raw(html):
    """把 `window.__DESK_STATE__='…'` 里那段 JS 单引号字符串取出来（识别反斜杠转义）。"""
    marker = "window.__DESK_STATE__='"
    start = html.index(marker) + len(marker)
    out = []
    i = start
    while i < len(html):
        c = html[i]
        if c == '\\':
            out.append(html[i:i + 2])
            i += 2
            continue
        if c == "'":
            break
        out.append(c)
        i += 1
    return ''.join(out)


def test_placeholders_are_replaced():
    """桌面页里那几个占位符一个都不许留在响应里。"""
    h, html = _serve()
    assert h.status == 200
    for ph in ('__DESK_STATE_JSON__', '__WS_PORT_NUM__', '__DESK_ROLE_JSON__'):
        assert ph not in html, '%s 没被替换' % ph


def test_role_is_injected():
    """角色决定桌面上出现哪几个图标：注入成字符串，页面才好比较。"""
    _h, html = _serve()
    assert "window.__DESK_ROLE__='super_admin'" in html


def test_state_json_is_parseable():
    """注入的那一段按 JS 转义规则还原后，必须能 JSON.parse —— 就是页面干的事。"""
    _h, html = _serve()
    raw = _state_raw(html).encode('utf-8').decode('unicode_escape')
    data = json.loads(raw)
    assert isinstance(data, dict)
    assert 'wifi' in data and 'power' in data
    assert isinstance(data.get('devices'), int)


def test_quote_in_ssid_does_not_break_the_script(monkeypatch):
    """SSID 里带撇号时不能被当成字符串结束 —— 那会把整段脚本拆坏。"""
    import leaffs.system.wifi as wifi
    monkeypatch.setattr(wifi, 'status', lambda: {'ssid': "it's a net", 'state': 'COMPLETED'})
    _h, html = _serve()
    raw = _state_raw(html)
    assert '\\' in raw                     # 撇号被转义了
    data = json.loads(raw.encode('utf-8').decode('unicode_escape'))
    assert data['wifi']['ssid'] == "it's a net"
