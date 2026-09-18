# -*- coding: utf-8 -*-
"""`keepalive_timeout` 配置项（2026-09-18，HTTP/1.1 的附属改动）

起因：切 HTTP/1.1 时先把空闲超时实现成了类常量（`KEEPALIVE_IDLE_TIMEOUT = 15`），
用户要求做成正规配置项。这里照 `io_idle_timeout_secs` 那一套对齐：默认值 / 载入钳制 /
深配白名单 / 应用校验 / 导出 / UI 回显。

它管的是「keep-alive 下等下一个请求」的时间：一条连接占一个线程，所以这个值必须短。

⚠️ 测试进程**不能**走 `apply_deep_config` 去验"生效" —— 测试进程的 `CONFIG_DIR` 指的是
项目真实的 `config/`，那样会改到真实配置。所以：
  - 「保存 / 边界 / 落盘」由**服务端进程**验（它跑在临时数据根里，`client.post` 即可）；
  - 「handler 真的按配置取值」在测试进程里用 monkeypatch 改模块全局，不落盘。
"""
from conftest import login


def test_keepalive_timeout_exposed_in_deep_config(client):
    """深配接口必须返回这个键（否则 UI 回显只能靠猜默认值）"""
    login(client)
    r = client.get('/api/config/deep')
    assert r.status_code == 200, r.text
    d = r.json()
    assert 'keepalive_timeout' in d, '深配没有返回 keepalive_timeout'
    assert d['keepalive_timeout'] == 15.0, '默认值不是 15: %r' % d['keepalive_timeout']


def test_keepalive_timeout_can_be_saved_and_read_back(client):
    """能存、能读回来（服务端跑在临时数据根里，改的是那份配置）"""
    login(client)
    try:
        r = client.post('/api/config/deep', json={'keepalive_timeout': 7.5})
        assert r.status_code == 200, r.text
        d = client.get('/api/config/deep').json()
        assert d['keepalive_timeout'] == 7.5, d['keepalive_timeout']
    finally:
        client.post('/api/config/deep', json={'keepalive_timeout': 15})


def test_keepalive_timeout_rejects_out_of_range(client):
    """★ 越界必须整体失败（400），不能"设了个不合法的值"就放过去

    下界 1 秒：再小等于没有空闲回收，keep-alive 就退化成"连接一直占着线程"；
    上界 300 秒：比读超时（60s）都长，等于没设。
    """
    login(client)
    for bad in (0.5, 0, 301, 1000, 'abc', None):
        r = client.post('/api/config/deep', json={'keepalive_timeout': bad})
        assert r.status_code == 400, '应拒绝 %r，实际 %s' % (bad, r.status_code)
    # 越界被拒之后值不该被改动
    d = client.get('/api/config/deep').json()
    assert d['keepalive_timeout'] == 15.0, d['keepalive_timeout']


def test_keepalive_timeout_in_known_keys():
    """白名单里得有它 —— 不在白名单的键会被整体拒绝（那是"未知配置项"的保护）"""
    from leaffs.config.core import _DEEP_KNOWN_KEYS
    assert 'keepalive_timeout' in _DEEP_KNOWN_KEYS


def test_loaded_value_is_clamped():
    """盘上被手改成非法值时，载入要钳制到边界（与 io_idle_timeout_secs 同一套容错）"""
    from leaffs.config.core import _clamp_num
    assert _clamp_num(0.1, 1.0, 300.0, float) == 1.0
    assert _clamp_num(9999, 1.0, 300.0, float) == 300.0
    assert _clamp_num('abc', 1.0, 300.0, float) == 1.0
    assert _clamp_num('12.5', 1.0, 300.0, float) == 12.5


def test_handler_reads_configured_value(monkeypatch):
    """★ handler 的空闲超时取的是**配置值**，不是那个类常量

    ⚠️ 这里用 monkeypatch 改模块全局，**不落盘** —— 见文件头的说明。
    """
    import leaffs.config.core as cc
    from leaffs.server.handler import HTTPHandler

    monkeypatch.setattr(cc, '_keepalive_timeout_secs', 3.0, raising=False)

    class _FakeHandler:
        KEEPALIVE_IDLE_TIMEOUT = 15          # 回退值，不该被用到

    assert HTTPHandler._keepalive_idle_seconds(_FakeHandler()) == 3.0


def test_handler_falls_back_when_config_unavailable(monkeypatch):
    """配置层拿不到值时退回类常量（与 fs_api 的 WRITE_STALL_TIMEOUT 同一写法）"""
    import leaffs.config.core as cc
    from leaffs.server.handler import HTTPHandler

    def _boom():
        raise RuntimeError('config unavailable')

    monkeypatch.setattr(cc, 'get_keepalive_timeout_secs', _boom)

    class _FakeHandler:
        KEEPALIVE_IDLE_TIMEOUT = 15

    assert HTTPHandler._keepalive_idle_seconds(_FakeHandler()) == 15.0
