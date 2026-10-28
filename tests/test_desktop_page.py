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


def test_placeholders_are_replaced():
    """桌面页里那几个占位符一个都不许留在响应里。"""
    h, html = _serve()
    assert h.status == 200
    for ph in ('__DESK_STATE_JSON__', '__WS_PORT_NUM__', '__MY_USERNAME__',
               '__MY_ROLE__', '__SERVER_BASE_URL__', '__UPLOAD_MAX_BYTES__'):
        assert ph not in html, '%s 没被替换' % ph
    assert 'leaf' in html
    assert 'super_admin' in html


def test_state_json_is_parseable():
    """注入的那一段从页面里取回来，必须能 JSON.parse —— 就是页面干的事。"""
    _h, html = _serve()
    start = html.index("window.__DESK_STATE__='") + len("window.__DESK_STATE__='")
    end = html.index("'", start)
    text = html[start:end]
    # 页面拿到的就是这个字符串；JS 字符串里的转义要按 JS 规则还原
    data = json.loads(text.encode('utf-8').decode('unicode_escape')
                      if '\\\\' in text else text)
    assert isinstance(data, dict)
    assert 'wifi' in data and 'power' in data


def test_quote_in_ssid_does_not_break_the_script(monkeypatch):
    """SSID 里带撇号时不能被当成字符串结束 —— 那会把整段脚本拆坏。"""
    import leaffs.system.wifi as wifi
    monkeypatch.setattr(wifi, 'status', lambda: {'ssid': "it's a net", 'state': 'COMPLETED'})
    _h, html = _serve()
    start = html.index("window.__DESK_STATE__='") + len("window.__DESK_STATE__='")
    end = html.index("'", start)
    # 真正的字符串结束符不会出现在里面：撇号被转义成了 \'
    assert "\\'" in html[start:end]
