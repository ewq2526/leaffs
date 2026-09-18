# -*- coding: utf-8 -*-
"""HTTP/1.1 预备 · 第 2 步：**每请求状态**必须重算，补读结果必须能被信任（2026-09-18）

两个改动，都是"一条连接多个请求"才暴露出来的问题：

1. **总时长预算**（`_req_t0` / `_exempt_total_timeout`）原来在 `handle()` 开头设**一次**。
   HTTP/1.0 下一个连接一个请求，所以没问题；keep-alive 下第 2、3 个请求会继承第 1 个的
   起点和豁免 —— 预算凭空少掉，或者一次上传的豁免传染给后面所有请求。
   ⇒ 挪进 `parse_request()`，每请求重算。

2. **补读未读正文**（LF-31 那套）原来只挂在 `finish()` 里，而 `StreamRequestHandler.finish()`
   在**整条连接**收尾时才调用一次。keep-alive 下上一个请求没读完的正文会留在 `rfile` 里，
   被下一个请求当成它的请求行来解析（请求走私式的错位，**而且不报错**）。
   ⇒ `_drain_unread_input()` 改成**返回"这条连接还能不能复用"**，由
   `handle_one_request()` 每请求调用；读不干净就关连接。

⚠️ 现在的协议仍是 HTTP/1.0，「复用」这件事**在端到端上还观察不到** —— 真正让它生效的是
最后一步切 `protocol_version`。所以这里主要靠**单元**和**静态**断言把这层正确性钉住，
免得第 4 步切完才发现哪一处漏了。
"""
import io

from conftest import login

import leaffs.server.handler as _h


# ---------- 1. 每请求重算预算 ----------

def test_budget_reset_lives_in_parse_request():
    """★ 预算起点必须在 `parse_request` 里设（每请求都会走到），不能留在 `handle()`"""
    import inspect

    src_pr = inspect.getsource(_h.HTTPHandler.parse_request)
    assert '_req_t0' in src_pr, 'parse_request 没有重设总时长预算起点'
    assert '_exempt_total_timeout' in src_pr, 'parse_request 没有清掉上一个请求的豁免'

    src_handle = inspect.getsource(_h.HTTPHandler.handle)
    assert 'self._req_t0 = ' not in src_handle, \
        'handle() 里还在设 _req_t0 —— keep-alive 下第 2 个请求会继承第 1 个的起点'


def test_two_requests_on_same_client_both_succeed(client):
    """端到端弱验证：同一客户端连发多个请求都正常（预算/豁免错乱会表现为莫名的超时）"""
    login(client)
    for _ in range(3):
        r = client.get('/api/ping')
        assert r.status_code == 200, r.text


# ---------- 2. 补读结果必须能被信任 ----------

class _FakeConn:
    def __init__(self):
        self._t = 60

    def gettimeout(self):
        return self._t

    def settimeout(self, t):
        self._t = t


class _FakeRfile:
    def __init__(self, data=b'', bytes_read=0):
        self._buf = io.BytesIO(data)
        self.bytes_read = bytes_read

    def read1(self, _n):
        return self._buf.read(_n)


class _FakeHandler:
    """只够 `_drain_unread_input` 用：它读 headers / rfile / connection / _body_base。"""

    def __init__(self, cl, body_left=b'', already_read=0):
        self.headers = {'Content-Length': str(cl)}
        self.rfile = _FakeRfile(body_left, bytes_read=already_read)
        self.connection = _FakeConn()
        self._body_base = 0


def _drain(cl, body_left=b'', already_read=0, monkeypatch=None, seconds=0.1):
    if monkeypatch is not None:
        monkeypatch.setattr(_h, '_DRAIN_SECONDS', seconds)
    h = _FakeHandler(cl, body_left, already_read)
    return _h.HTTPHandler._drain_unread_input(h)


def test_drain_returns_true_when_body_already_consumed(monkeypatch):
    """正文早就读完了 ⇒ True（连接可复用），而且是**零开销**的那条路径"""
    # cl=10，rfile 已读 10 字节
    assert _drain(10, already_read=10, monkeypatch=monkeypatch) is True


def test_drain_returns_true_when_leftovers_can_be_read(monkeypatch):
    """有残留但读得到 ⇒ 读完 ⇒ True"""
    assert _drain(10, body_left=b'0123456789', already_read=0, monkeypatch=monkeypatch) is True


def test_drain_returns_false_when_leftovers_never_arrive(monkeypatch):
    """★ 对端不再发（缓冲区空）⇒ 补读超时 ⇒ **False** —— 调用方据此关连接

    keep-alive 下这条最关键：残留没读干净还继续复用，下一个请求就会从错位的地方开始解析。
    """
    assert _drain(100, body_left=b'', already_read=0, monkeypatch=monkeypatch) is False


def test_drain_returns_false_without_parse_request_baseline():
    """没走过 parse_request（拿不到基线）⇒ 无从判断 ⇒ False（按不可复用处理）"""
    h = _FakeHandler(10)
    h._body_base = None
    assert _h.HTTPHandler._drain_unread_input(h) is False


def test_handle_one_request_closes_connection_when_drain_fails():
    """★ `handle_one_request` 必须在补读不干净时关掉连接"""
    import inspect

    src = inspect.getsource(_h.HTTPHandler.handle_one_request)
    assert '_drain_unread_input' in src, 'handle_one_request 没有调用补读'
    assert 'close_connection = True' in src, \
        '补读失败时没有关连接 —— 残留正文会污染下一个请求'
    assert 'super().handle_one_request()' in src, '没有走标准库的请求处理'


def test_finish_still_drains_before_closing():
    """连接收尾那一步的补读仍然保留（关闭前确保干净，避免 RST 丢掉响应）"""
    import inspect

    src = inspect.getsource(_h.HTTPHandler.finish)
    assert '_drain_unread_input' in src, 'finish() 不再补读 —— LF-31 会复发'
