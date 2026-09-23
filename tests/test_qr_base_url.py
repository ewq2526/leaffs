# -*- coding: utf-8 -*-
"""二维码给出的地址必须是对外地址，不能是本机回环

背景：`_public_base_url()` 原来在 Host 是回环地址时回退到"自己解析主机名"
（`gethostbyname(gethostname())`）—— 那在安卓上必失败（主机名是机型名），恒得 127.0.0.1。
而管理页就在 WebView 里（Host 就是 127.0.0.1），所以手机上出二维码时给出的永远是回环
地址：别的设备扫到的是**它自己**的 127.0.0.1 —— 连不上，或者连到它自己那台服务上，
于是"扫码登录出来的用户不对"。桌面上看不出问题，只是因为那句在 PC 上碰巧能解析出 LAN IP。

修法：回退改走统一入口 `hosts.lan_ip()`（安卓由 leaffs_mobile 注册的探测给、桌面走 UDP
探测），共享层不再自己解析主机名 —— 平台差异只留在入口文件里。

用例里的地址用 RFC 5737 的文档保留段当替身，避免跟跑测试的机器真实 IP 撞车。
"""
import socket

import leaffs.config.core as _cfg
import leaffs.server.hosts as hosts
from leaffs.server.handler import HTTPHandler

PORT = int(_cfg.PORT)          # 本站主站端口
LAN = '203.0.113.21'           # 替身：对外地址（Wi-Fi / 热点）
FOREIGN = 'evil.example.com'


class _Req:
    """只带 `_public_base_url()` 用到的两样东西：请求头、是否 TLS。"""

    def __init__(self, host):
        self.headers = {'Host': host}

    def _is_secure(self):
        return False


def _expect(host):
    """与实现同一套拼法（端口 80 时不带端口）"""
    return 'http://%s' % host + ('' if PORT == 80 else ':%d' % PORT)


def setup_function(_fn):
    hosts.set_ip_provider(lambda: (LAN, True))


def teardown_function(_fn):
    hosts.set_ip_provider(None)
    hosts.set_ip_collector(None)


def test_loopback_host_falls_back_to_lan_address():
    """Host 是回环（手机 WebView 就是这样）→ 用对外地址，绝不能是 127.0.0.1"""
    assert HTTPHandler._public_base_url(_Req('127.0.0.1:8081')) == _expect(LAN)


def test_localhost_name_falls_back_to_lan_address():
    assert HTTPHandler._public_base_url(_Req('localhost:8081')) == _expect(LAN)


def test_lan_host_is_kept():
    """访客已经用对外地址访问 → 按它给，不回退"""
    assert HTTPHandler._public_base_url(_Req('%s:8081' % LAN)) == _expect(LAN)


def test_foreign_host_is_not_trusted():
    """伪造 / DNS rebinding 的 Host 不得进二维码（A-14）"""
    assert HTTPHandler._public_base_url(_Req(FOREIGN + ':8081')) == _expect(LAN)


def test_hostname_resolution_is_never_used(monkeypatch):
    """不许再自己解析主机名 —— 这就是当时的根因

    安卓上 `gethostname()` 是机型名、解析必然失败（真机实测 iQOO-Z11-Turbo），
    于是回退恒得 127.0.0.1。这里把解析函数直接打爆：**旧写法在这条上必然红**
    （抛异常 → 走上原来的 `host = '127.0.0.1'`）。
    """
    def _boom(*_a, **_k):
        raise OSError('hostname lookup fails (android)')

    monkeypatch.setattr(socket, 'gethostbyname', _boom)
    monkeypatch.setattr(socket, 'gethostname', _boom)
    assert HTTPHandler._public_base_url(_Req('127.0.0.1:8081')) == _expect(LAN)
