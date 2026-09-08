# -*- coding: utf-8 -*-
"""主机名 / 本机 IP 工具 —— Host/Origin 规范化与本机地址探测。

历史：原散落于 leaffs.py（_strip_host_port/_LOCAL_HOST_NAMES/_primary_lan_ip/_collect_ips），
重构抽出供 HTTP 与 WS 层共用（避免传输层模块互相 import leaffs.py）。
"""
import socket

_LOCAL_HOST_NAMES = frozenset(('localhost', '127.0.0.1', '::1'))


def strip_host_port(host):
    """Host/Origin host → 裸主机名（小写、去端口/方括号；IPv6 字面量原样保留）

    IPv4「host:port」与「host」行为不变；IPv6 三种形态都正确归约：
    `[::1]:8080` → `::1`；`[::1]` → `::1`；裸 `::1`/`2001:db8::1`（含 ':'>1）不再被
    当成 host:port 截断（旧实现把 `::1` rsplit(':',1) 成 `:`，导致 Origin http://[::1]:8080
    白名单校验失败被误拒）。
    """
    h = (host or '').strip().lower()
    if not h:
        return ''
    if h.startswith('['):
        end = h.find(']')
        if end != -1:
            return h[1:end]
        return h
    if h.count(':') > 1:
        return h  # 裸 IPv6（无端口段）：不作 host:port 拆分
    if ':' in h:
        h = h.rsplit(':', 1)[0]
    return h


def primary_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def collect_ips():
    """收集本机全部 IPv4（供 Host/Origin 白名单、二维码 base 等使用）。

    保守实现（socket.getaddrinfo / gethostbyname_ex），不依赖 psutil/ipconfig。
    结果去重并剔除环回地址；探测全失败则退化为 [primary_lan_ip(), '127.0.0.1']。
    """
    out = []

    def _add(ip):
        ip = (ip or '').strip()
        if ip and not ip.startswith('127.') and ip not in out:
            out.append(ip)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        _add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            _add(info[4][0])
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            _add(ip)
    except Exception:
        pass
    if not out:
        p = primary_lan_ip() or '127.0.0.1'
        out = [p] if not p.startswith('127.') else ['127.0.0.1']
    return out
