# -*- coding: utf-8 -*-
"""主机名 / 本机 IP 工具 —— Host/Origin 规范化与本机地址探测。

历史：原散落于 leaffs.py（_strip_host_port/_LOCAL_HOST_NAMES/_primary_lan_ip/_collect_ips），
重构抽出供 HTTP 与 WS 层共用（避免传输层模块互相 import leaffs.py）。
"""
import ipaddress
import re
import socket
import time

_LOCAL_HOST_NAMES = frozenset(('localhost', '127.0.0.1', '::1'))

# 域名标签（RFC 1035/1123）：字母数字与连字符，不以连字符开头/结尾
_HOST_LABEL_RE = re.compile(r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$')


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


def is_valid_host(host):
    """host 是不是一个合法的「主机」字面量（IPv4 / IPv6 / 域名）。

    用途：凡是要把**请求头里的 Host** 写进 URL 或 HTML 的地方，先过这一关。
    Host 是不可信输入 —— 浏览器自己不会发出带 `"` `<` `>` 的 Host，但在
    DNS rebinding 之类的场景里 Host 归攻击者控制，原样拼进 HTML 属性
    （`href="__CERT_MAIN_URL__"` 那种）就能提前闭合属性、注入标签。

    规则：先按字面量试 IPv4/IPv6（`ipaddress`）；不中再按域名规则逐标签校验
    （字母数字与连字符、不以连字符开头/结尾、单标签 ≤63、总长 ≤253）。
    主机名里的下划线、空标签、以及任何引号/尖括号/斜杠一律不接受 → 返回 False。
    """
    h = (host or '').strip().lower()
    if not h:
        return False
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    if len(h) > 253:
        return False
    return all(_HOST_LABEL_RE.match(lb) for lb in h.split('.'))


def primary_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


# ---------- 本机「全部地址」收集（安卓端注册；桌面不注册，走默认实现）----------
# 与下面的 _ip_provider 是两件事，别合并：
#   * _ip_provider 给的是「唯一对外地址」（管理页显示 / 二维码 / 分享链接用）；
#   * _ip_collector 给的是「全部本机地址」（Host/Origin 白名单、证书 SAN 用）——
#     漏掉一个就可能把合法连接拒掉。
# 为什么需要它：安卓上 socket 探测拿不到热点 AP 网卡(wlan1) 的地址（主机名是机型名
# 解析必失败、UDP 默认路由走蜂窝），而访客访问用的往往正是那个地址。曾经 WS 的
# Host/Origin 校验(`_ws_origin_host_ok`)就因此把连接全部拒掉（当时是 close 1008，
# 而浏览器对 WS 被关闭不给任何提示 —— 症状看起来像"证书没信任"，极难排查。
_ip_collector = None

# 收集结果的 TTL 缓存（2026-09-16，`issues.md` §二 第 5 条）：
# `collect_ips` 会被 **WS 每次握手**调用（`ws.py` 的 `_ws_origin_host_ok` —— 那是
# `process_request` 的**同步**回调，没法 await、也挪不进线程池），而它内部要做主机名解析。
# 桌面实测：首次 ~7.7ms、之后 ~0.3ms；但**安卓上那两次解析注定失败**（主机名是机型名，
# 见下面 81-84 行的历史记录），失败往往要等 DNS 超时 —— 每次握手都付一遍会直接卡住
# WS 事件循环。缓存把"每次全收"摊薄成"每 _IPS_TTL 秒一次"；换网后最多延迟这么久反映。
_IPS_TTL = 10.0
_IPS_CACHE = {'t': 0.0, 'ips': None}


def invalidate_ips_cache():
    """作废本机 IP 缓存（换 collector、或网络变化后需要立刻反映时调用）"""
    _IPS_CACHE['t'] = 0.0
    _IPS_CACHE['ips'] = None


def set_ip_collector(fn):
    """注册「本机全部 IPv4」提供者：fn() -> [ip, ...]（安卓用 NetworkInterface 枚举）。"""
    global _ip_collector
    _ip_collector = fn
    invalidate_ips_cache()      # collector 换了 → 旧结果必须作废


def collect_ips():
    """收集本机全部 IPv4（供 Host/Origin 白名单、证书 SAN、二维码 base 等使用）。

    **带 TTL 缓存**（`_IPS_TTL`）：它被 WS **每次握手**的 Host/Origin 校验调用，
    而那道校验跑在 `process_request` 的同步回调里（不能 await）—— 只能靠缓存把
    主机名解析的开销摊薄。要立刻反映网络变化，调 `invalidate_ips_cache()`。

    三条来源按可信度合并（统一去重、剔除回环）：
      0) 外部收集器（安卓注册，见 set_ip_collector）—— 问系统要全部网卡地址，
         是唯一能拿到"热点 AP 网卡"这类地址的路子；
      1) socket 探测（UDP 默认路由出口 + 解析本机主机名）—— 桌面可靠，安卓不可靠；
      2) 对外地址 lan_status()（安卓注册 provider 时＝它枚举到的对外地址）。
    全部落空则退化为 [primary_lan_ip()]（仍是回环就给 '127.0.0.1'）。
    """
    now = time.time()
    cached = _IPS_CACHE['ips']
    if cached is not None and (now - _IPS_CACHE['t']) < _IPS_TTL:
        return list(cached)          # 返回**副本**：调用方改不动缓存
    ips = _collect_ips_uncached()
    _IPS_CACHE['t'] = now
    _IPS_CACHE['ips'] = list(ips)
    return list(ips)


def _collect_ips_uncached():
    """真正去收集（不做缓存）—— 语义见 `collect_ips` 的 docstring"""
    out = []

    def _add(ip):
        ip = (ip or '').strip()
        if ip and not ip.startswith('127.') and ip not in out:
            out.append(ip)

    # 0) 外部收集器（安卓：枚举全部网卡）
    if _ip_collector is not None:
        try:
            for _ip in (_ip_collector() or []):
                _add(_ip)
        except Exception:
            pass
    # 1) socket 探测：UDP 默认路由出口（很快，保留）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        _add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    # ⚠️ 下面两段是"**解析本机主机名**"：**注册了 collector 的平台（安卓）直接跳过** ——
    #   · 安卓上它注定失败（主机名是机型名，见本文件上方 81-84 行的历史记录），
    #     而失败往往要等 DNS 超时；
    #   · collector 能枚举**全部网卡**（含热点 AP 网卡），结果本来就比这里更全。
    # 桌面端不注册 collector ⇒ 照旧执行，行为不变。
    if _ip_collector is None:
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
    # 2) 对外地址兜底：它是二维码/分享链接实际给出的地址，必须能在白名单里
    try:
        _add(lan_status()[0])
    except Exception:
        pass
    if not out:
        p = primary_lan_ip() or '127.0.0.1'
        out = [p] if not p.startswith('127.') else ['127.0.0.1']
    return out


# ---------- 对外可访问地址（管理页「本机 IP」/二维码、分享页链接）----------
# 安卓端 leaffs_mobile 启动时注册提供者：它能枚举网卡、认出 Wi-Fi/热点、排除蜂窝
# 数据（rmnet* 在运营商 NAT 后面，同局域网的人打不开）；桌面端不注册，走下面的
# 默认实现。放在这里是为了让共享层不依赖安卓（与本项目 check_quota 同一规矩）。
_ip_provider = None


def set_ip_provider(fn):
    """注册「对外可访问地址」提供者：fn() -> (ip, has_lan)。

    has_lan=False 表示这个地址只有本机能用（别人访问不到），前端据此提示
    「无可用局域网」。
    """
    global _ip_provider
    _ip_provider = fn


def lan_status():
    """返回 (对外地址, 是否有可用局域网)。

    默认实现（桌面）：primary_lan_ip() 走 UDP 探测拿默认路由出口地址，
    拿到非回环地址即 has_lan=True；只剩回环则 127.0.0.1 + False。
    """
    if _ip_provider is not None:
        try:
            ip, has = _ip_provider()
            ip = (ip or '').strip()
            if ip:
                return ip, bool(has)
        except Exception:
            pass
    p = primary_lan_ip()
    if p and not p.startswith('127.'):
        return p, True
    return '127.0.0.1', False


def lan_ip():
    """对外地址（没有可用局域网时就是 127.0.0.1）"""
    return lan_status()[0]
