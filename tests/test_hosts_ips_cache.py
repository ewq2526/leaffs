# -*- coding: utf-8 -*-
"""`issues.md` §二 第 5 条：`collect_ips()` 加 TTL 缓存 ＋ 注册 collector 时跳过主机名解析。

**来由**：`collect_ips` 被 **WS 每次握手**的 Host/Origin 校验调用（`ws.py` 的
`_ws_origin_host_ok`），而那道校验跑在 `process_request` 的**同步**回调里 ——
不能 await、也挪不进线程池，所以主机名解析（`getaddrinfo` / `gethostbyname_ex`）
的代价只能靠缓存摊薄。

- **桌面实测**：首次 ~7.7 ms、之后 ~0.3 ms（操作系统有 DNS 缓存）⇒ 桌面本来就不是问题。
- **安卓才是问题**：那两次解析**注定失败**（主机名是机型名，`hosts.py` 里有历史记录），
  失败往往要等 DNS 超时，而每次握手都付一遍 ⇒ 直接卡住 WS 事件循环。

**修法**：① 结果加 TTL 缓存（`_IPS_TTL`；`set_ip_collector` 会作废它）；
② **注册了 collector 的平台跳过那两段主机名解析**（collector 能枚举全部网卡、
结果本来就比它全；桌面不注册 collector ⇒ 行为不变）。

⚠️ 本文件全是白盒：真去测"安卓上解析要多久"需要真机，测不了；但"有没有白做这件事"
用调用计数就能钉死 —— 而那正是本条修复的实质。
"""
import socket

import pytest

from leaffs.server import hosts


@pytest.fixture(autouse=True)
def _clean_ips_cache():
    """每个用例前后都作废缓存 —— 免得用例里注册的假 collector 把假 IP 留在缓存里"""
    hosts.invalidate_ips_cache()
    yield
    hosts.invalidate_ips_cache()


def _count_dns(monkeypatch):
    """把两个主机名解析函数换成计数器（返回固定的假 IP）"""
    calls = {'getaddrinfo': 0, 'gethostbyname_ex': 0}

    def _gai(*a, **kw):
        calls['getaddrinfo'] += 1
        return [(socket.AF_INET, None, None, '', ('10.9.9.9', 0))]

    def _ghe(*a, **kw):
        calls['gethostbyname_ex'] += 1
        return (socket.gethostname(), [], ['10.9.9.8'])

    monkeypatch.setattr(hosts.socket, 'getaddrinfo', _gai)
    monkeypatch.setattr(hosts.socket, 'gethostbyname_ex', _ghe)
    return calls


def test_registered_collector_skips_hostname_resolution(monkeypatch):
    """★ 修的就是这条：注册了 collector（安卓）时**不再做主机名解析**

    解析在安卓上注定失败（主机名是机型名），失败还要等 DNS 超时 —— 每次握手白付一遍。
    """
    calls = _count_dns(monkeypatch)
    hosts.set_ip_collector(lambda: ['192.168.43.1'])
    ips = hosts.collect_ips()
    assert '192.168.43.1' in ips, ips
    assert calls == {'getaddrinfo': 0, 'gethostbyname_ex': 0}, (
        '有 collector 还去解析本机主机名（安卓上必失败、可能要等 DNS 超时）：%r' % calls)


def test_without_collector_still_resolves_hostname(monkeypatch):
    """对照：**桌面**（没注册 collector）照旧解析主机名 —— 这条修复不能误伤桌面"""
    calls = _count_dns(monkeypatch)
    hosts.set_ip_collector(None)
    ips = hosts.collect_ips()
    assert calls['getaddrinfo'] >= 1 and calls['gethostbyname_ex'] >= 1, (
        '桌面路径的主机名解析被误删了：%r' % calls)
    assert '10.9.9.9' in ips, ips


def test_result_is_cached_within_ttl(monkeypatch):
    """TTL 内第二次调用**不再重新收集** —— 这是把握手开销摊薄的关键"""
    n = {'uncached': 0}
    real = hosts._collect_ips_uncached

    def _counting():
        n['uncached'] += 1
        return real()

    monkeypatch.setattr(hosts, '_collect_ips_uncached', _counting)
    a = hosts.collect_ips()
    b = hosts.collect_ips()
    assert n['uncached'] == 1, 'TTL 内不该重新收集，实际收集了 %d 次' % n['uncached']
    assert a == b
    assert a is not b, '要返回副本，别把缓存本身交出去'


def test_cache_expires_and_recollects(monkeypatch):
    """缓存过期后会重新收集"""
    n = {'uncached': 0}
    real = hosts._collect_ips_uncached

    def _counting():
        n['uncached'] += 1
        return real()

    monkeypatch.setattr(hosts, '_collect_ips_uncached', _counting)
    hosts.collect_ips()
    hosts._IPS_CACHE['t'] -= (hosts._IPS_TTL + 1)      # 手动让它过期
    hosts.collect_ips()
    assert n['uncached'] == 2, '过期后应当重新收集，实际 %d 次' % n['uncached']


def test_set_ip_collector_invalidates_cache(monkeypatch):
    """换 collector **立刻**作废缓存 —— 不然新网卡的地址要等 TTL 才被认（正是 WS 白名单那类问题）"""
    monkeypatch.setattr(hosts, '_ip_collector', None)
    hosts.set_ip_collector(lambda: ['10.1.1.1'])
    assert '10.1.1.1' in hosts.collect_ips()
    hosts.set_ip_collector(lambda: ['10.2.2.2'])
    ips = hosts.collect_ips()
    assert '10.2.2.2' in ips, '换 collector 后没立刻生效（缓存没作废）：%s' % ips
