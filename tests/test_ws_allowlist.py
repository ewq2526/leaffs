# -*- coding: utf-8 -*-
"""WS 的 Host/Origin 白名单必须认得出「本机全部地址」

背景（真机踩过）：安卓上 socket 探测拿不到热点 AP 网卡(wlan1) 的地址 ——
主机名是机型名、解析必失败；UDP 默认路由走蜂窝、拿到的是 rmnet 地址。于是访客用
热点地址访问手机服务端时，Host 不在白名单里，连接在 TLS 握手之后被 close(1008) 拒掉；
而浏览器对 WS 被关闭不给任何提示，症状看起来完全像"证书没信任"，用户怎么放行 8081 都没用。

修法：安卓启动时用 NetworkInterface 枚举全部网卡地址，注册到 hosts.set_ip_collector()；
collect_ips() 把它并进白名单，并额外并入 lan_status() 的对外地址做兜底。

用例里的地址用 RFC 5737 的文档保留段（203.0.113.0/24、198.51.100.0/24）当替身，
避免跟跑测试的机器真实 IP 撞车。
"""
import leaffs.config.core as _cfg
import leaffs.server.hosts as hosts
from leaffs.server.ws import _ws_origin_host_ok

PORT = int(_cfg.PORT)      # 本站主站端口：Origin 的端口必须等于它
HOTSPOT = '203.0.113.7'    # 替身：热点 AP 网卡（wlan1）
CELL = '198.51.100.9'      # 替身：蜂窝（rmnet_data1）
FOREIGN = 'evil.example.com'


def _h(host, origin=''):
    """构造握手请求头。

    LF-14 之后校验函数只吃 `headers`（调用点上移到了握手前 `process_request`），
    不再依赖 websocket 对象上的 `.request` / `.origin`。
    """
    d = {'Host': host}
    if origin:
        d['Origin'] = origin
    return d


def setup_function(_fn):
    hosts.set_ip_collector(None)
    hosts.set_ip_provider(None)


def teardown_function(_fn):
    hosts.set_ip_collector(None)
    hosts.set_ip_provider(None)


def test_hotspot_address_rejected_without_collector():
    """不注册收集器 = 安卓修复前的行为：热点地址进不了白名单，连接被拒（复现问题）"""
    assert HOTSPOT not in hosts.collect_ips()
    assert _ws_origin_host_ok(_h(HOTSPOT + ':8081', 'https://' + HOTSPOT + ':%d' % PORT)) is False


def test_collector_addresses_are_allowed():
    """安卓注册"全部本机地址"后：热点与蜂窝地址都要放行"""
    hosts.set_ip_collector(lambda: [HOTSPOT, CELL])
    allowed = hosts.collect_ips()
    assert HOTSPOT in allowed and CELL in allowed
    assert _ws_origin_host_ok(_h(HOTSPOT + ':8081', 'https://' + HOTSPOT + ':%d' % PORT)) is True
    assert _ws_origin_host_ok(_h(CELL + ':8081', 'https://' + CELL + ':%d' % PORT)) is True


def test_foreign_host_still_rejected():
    """放宽范围后防 DNS rebinding 仍然有效：外域 Host/Origin 一律拒"""
    hosts.set_ip_collector(lambda: [HOTSPOT])
    assert _ws_origin_host_ok(_h(FOREIGN + ':8081', 'https://' + FOREIGN)) is False
    # Host 是外域、Origin 是本机 —— 两者不同站，也拒
    assert _ws_origin_host_ok(_h(FOREIGN + ':8081', 'https://' + HOTSPOT)) is False
    # Origin 是字面量 null（sandboxed iframe / 本地文件）照旧拒
    assert _ws_origin_host_ok(_h(HOTSPOT + ':8081', 'null')) is False


def test_lan_status_address_included():
    """对外地址（二维码/分享链接实际给出的那个）必须在白名单里"""
    hosts.set_ip_provider(lambda: ('192.168.77.9', True))
    assert '192.168.77.9' in hosts.collect_ips()
    assert _ws_origin_host_ok(_h('192.168.77.9:8081', 'https://192.168.77.9:%d' % PORT)) is True


def test_localhost_names_always_allowed():
    assert _ws_origin_host_ok(_h('127.0.0.1:8081')) is True
    assert _ws_origin_host_ok(_h('localhost:8081')) is True
    assert _ws_origin_host_ok(_h('[::1]:8081')) is True


def test_collector_failure_is_swallowed():
    """收集器抛异常不能让白名单整体失效（退回 socket 探测的结果）"""
    def _boom():
        raise RuntimeError('java bridge not ready')

    hosts.set_ip_collector(_boom)
    assert isinstance(hosts.collect_ips(), list)


def test_origin_port_must_be_the_site_port():
    """主机名对了但**端口不对**的 Origin 一律拒

    SameSite 只比主机名、不比端口：本机任意其它端口上的页面照样会带上会话 Cookie
    连过来，而 WebSocket 不受同源策略约束 —— 服务端这道 Origin 校验是唯一防线，
    漏掉端口就等于"本机任何端口的页面都能冒充当本站页面"。
    """
    hosts.set_ip_collector(lambda: [HOTSPOT])
    # 本站主站端口的页面：放行
    assert _ws_origin_host_ok(_h(HOTSPOT + ':8081', 'https://%s:%d' % (HOTSPOT, PORT))) is True
    # 同一台机器的别的端口：拒
    assert _ws_origin_host_ok(_h(HOTSPOT + ':8081', 'http://%s:9999' % HOTSPOT)) is False
    assert _ws_origin_host_ok(_h(HOTSPOT + ':8081', 'http://%s:8081' % HOTSPOT)) is False
    # 不带端口（按 scheme 默认端口算）：主站不是 80/443 时同样拒
    if PORT not in (80, 443):
        assert _ws_origin_host_ok(_h(HOTSPOT + ':8081', 'http://' + HOTSPOT)) is False
