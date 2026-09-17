# -*- coding: utf-8 -*-
"""LF-31：请求被拒时没读的请求体 → 关连接回 RST，客户端拿不到我们的错误 JSON。

**问题**：服务端在很多拒绝路径上**没读请求体**就发响应并关连接，而内核在关闭一个
接收缓冲仍非空的连接时会回 **RST** —— 对端因此看不到刚发出的错误 JSON，
只报"网络错误"（httpx 侧就是 `WinError 10053/10054`）。

**为什么只有大 body 能稳定测**：`rfile` 是带缓冲的，请求头与**小** body 通常被同一次
读进用户态缓冲 ⇒ 内核缓冲空 ⇒ 正常 FIN（所以线上只有约 0.5% 踩中，靠运气）。
而 body 远大于一次 TCP 段时，服务端早退那一刻**不可能**已收完 ⇒ 必然残留 ⇒ 必然 RST。
⇒ 这条用例在未修的代码上稳定红，不是碰运气。
"""
import json

from conftest import guest_login, login

BIG = 2 * 1024 * 1024      # 超过 MAX_API_BODY_SIZE（1 MiB）⇒ 必然 413；
#                          又明显小于服务端丢弃残留的上限 _DRAIN_LIMIT（4 MiB），
#                          所以这条是**确定性**的，不踩边界（4 MiB body 正好压在上限上，
#                          会概率性地还剩一点没读完 ⇒ 偶发红）。


def test_oversized_post_gets_json_not_rst(client):
    """★ 超过上限的 POST：必须回 413 JSON，而不是把连接中止掉

    未修代码上这里会抛 `httpx.ReadError: [WinError 10053]`（body 还没发完，
    服务端已经在 413 早退后关了连接）。
    """
    login(client)
    r = client.post('/api/config', json={'pad': 'y' * BIG})
    assert r.status_code == 413, (r.status_code, r.text[:200])
    # 注意：send_json 的 JSON 是 ensure_ascii 的，中文在报文里是 \uXXXX 转义，
    # 所以断言必须解析后再比 —— 直接 in r.text 会误判
    assert json.loads(r.text).get('error') == '请求体过大', r.text[:200]


def test_guest_rejected_post_gets_json(client):
    """游客带大 body 的 POST 被拒（403 早退同样不读 body）时，也必须拿到 JSON"""
    guest_login(client)
    r = client.post('/api/users/add',
                    json={'username': 'drain_probe', 'password': 'pw-123456', 'pad': 'y' * BIG})
    assert r.status_code in (413, 403, 404), (r.status_code, r.text[:200])
    assert r.text.lstrip().startswith('{'), r.text[:200]


def test_normal_post_still_works(client):
    """回归保护：普通的、没超限的 POST 不受影响（收口不能把正常路径弄坏）"""
    login(client)
    r = client.post('/api/mkdir', json={'path': 'public', 'name': 'drain_probe_dir'})
    assert r.status_code == 200, r.text
    d = json.loads(r.text)
    assert d.get('success') is True or d.get('error'), d
