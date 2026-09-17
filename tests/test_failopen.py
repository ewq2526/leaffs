# -*- coding: utf-8 -*-
"""fail-open 回归：安全边界取值失败时必须**收紧**（拒绝），不能放宽。

修的是同一个模式 —— "取策略/取上限出错 → 当作放行 / 当作不限"：
  · server/ws.py 的 delete / mkdir：旧写法 `except Exception: ws_write_ok = True`
  · files/api.py 的上传预检：旧写法 `except Exception: umax = 0`
  · files/core.py 的流式写入上限：同一个 try 里 `UMAX = 0`

关键在于 `0 = 不限` 是**合法配置值**（config/core.py 的 set_config 就是 max(0, int(...))），
所以异常路径绝不能退化成它 —— 那不是兜底，是把"配置读坏了"读成"用户要求不限"。
"""
import asyncio
import io
import os

import pytest
import websockets.exceptions      # ws.py 的 except 子句用到它，靠别处导入才可用 —— 这里显式导入

import leaffs.config.core as _cc
import leaffs.files.api as _fapi
import leaffs.files.core as _fcore
import leaffs.server.handler as _h
import leaffs.server.ws as _ws


def _boom():
    raise RuntimeError('配置读取坏了')


# ---------- HTTP 上传：预检上限（files/api.py）----------

class _FakeHandler:
    """只实现 handle_upload 在"取上限"之前会碰到的东西"""

    def __init__(self, content_type, content_length):
        self.headers = {
            'Content-Type': content_type,
            'Content-Length': str(content_length),
        }
        self.sent = []
        self.role_calls = 0

    def send_json(self, obj, status=200):
        self.sent.append((status, obj))

    def _get_effective_role(self):
        self.role_calls += 1
        return None


def _upload(h):
    _fapi.handle_upload(h, None, None, 'unused', None, None, ())


def test_upload_precheck_error_fails_closed(monkeypatch):
    """取上限抛异常 → 这次上传直接失败，不能变成"不限"继续往下走"""
    monkeypatch.setattr(_cc, 'get_upload_max_size', _boom)
    h = _FakeHandler('multipart/form-data; boundary=x', 100 * 1024 * 1024)
    _upload(h)
    assert h.role_calls == 0, '取上限失败后还继续处理请求 —— 等于把出错读成"不限"'
    assert h.sent and h.sent[0][0] == 500, h.sent
    # A2：兜底不再回显异常文本（Windows 的 OSError 文本自带绝对路径）——
    # 客户端只看到场景化固定文案，细节进运行日志（test_error_redaction.py 锁这条）。
    assert h.sent[0][1] == {'error': '上传处理失败'}, h.sent


def test_upload_zero_still_means_unlimited(monkeypatch):
    """0 = 不限仍是合法配置，不能被预检拦下（保证这次改动没歪掉语义）"""
    monkeypatch.setattr(_cc, 'get_upload_max_size', lambda: 0)
    h = _FakeHandler('multipart/form-data; boundary=x', 100 * 1024 * 1024)
    _upload(h)
    assert h.sent and h.sent[0][0] == 401, h.sent      # 未登录 —— 说明越过了大小预检


def test_upload_over_limit_still_413(monkeypatch):
    monkeypatch.setattr(_cc, 'get_upload_max_size', lambda: 1024)
    h = _FakeHandler('multipart/form-data; boundary=x', 100 * 1024 * 1024)
    _upload(h)
    assert h.sent and h.sent[0][0] == 413, h.sent


# ---------- 流式落盘：累计写入上限（files/core.py）----------

def test_stream_limit_error_fails_closed(monkeypatch, data_root):
    """取上限抛异常 → 上传中断，绝不能退化成 UMAX=0（不限）把整个 body 收下"""
    monkeypatch.setattr(_fcore, 'UPLOAD_DIR', os.path.join(data_root, 'up_failopen'))
    monkeypatch.setattr(_cc, 'get_upload_max_size', _boom)
    body = (b'--X\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\n'
            b'hello\r\n--X--\r\n')
    with pytest.raises(RuntimeError):
        _fcore.handle_upload(io.BytesIO(body), 'multipart/form-data; boundary=X', len(body), '')


# ---------- WS 写策略（server/ws.py 的 delete / mkdir）----------

class _FakeWS:
    """够 ws_handler 跑到 delete / mkdir 分支的最小假连接"""

    def __init__(self, messages):
        self.remote_address = ('127.0.0.1', 51234)
        self.cached_role = 'admin'
        self.cached_user = 'admin'
        self._messages = list(messages)
        self.sent = []
        self.closed = []

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._messages:
            yield m

    async def send(self, m):
        self.sent.append(m)

    async def close(self, code=1000, reason=''):
        self.closed.append((code, reason))


def _boom_write(*a, **k):
    raise RuntimeError('写策略判定坏了')


def test_ws_delete_check_error_fails_closed(monkeypatch):
    """写策略判定抛异常 → 删除不执行（异常结束连接），不能默认放行"""
    monkeypatch.setattr(_ws._fs_api, 'ws_write_allowed', _boom_write)
    fake = _FakeWS(['{"type":"delete","paths":["public/nope.txt"]}'])
    with pytest.raises(RuntimeError):
        asyncio.run(_ws.ws_handler(fake))
    assert not [m for m in fake.sent if '"delete"' in m], fake.sent


def test_ws_mkdir_check_error_fails_closed(monkeypatch):
    monkeypatch.setattr(_ws._fs_api, 'ws_write_allowed', _boom_write)
    fake = _FakeWS(['{"type":"mkdir","path":"public","name":"nope"}'])
    with pytest.raises(RuntimeError):
        asyncio.run(_ws.ws_handler(fake))
    assert not [m for m in fake.sent if '"mkdir"' in m], fake.sent


# ---------- 游客写开关（files/api.py 的 _guest_write_flag）----------

class _FakeRoleHandler:
    """够 write_allowed 取到角色就行"""

    def __init__(self, role):
        self._role = role

    def _get_effective_role(self):
        return self._role


def test_guest_write_flag_error_fails_closed(monkeypatch):
    """取"游客能否写 public"的开关抛异常 → 拒写，不能当成用户放开了写权限"""
    monkeypatch.setattr(_cc, 'get_guest_public_write', _boom)
    with pytest.raises(RuntimeError):
        _fapi.write_allowed(_FakeRoleHandler('guest'), 'public', 'upload')


def test_guest_public_write_defaults_to_off(monkeypatch, data_root):
    """新装默认：游客不能在 public 下新建文件（配置里没有这个键时也是关的）

    产品默认已从"开"改成"关" —— 游客模式一开，局域网里任何人点一下就能往
    所有人可见的 public/ 放文件，这不该是默认行为。
    """
    cfg_file = os.path.join(data_root, 'cfg_default', 'server_config.json')
    os.makedirs(os.path.dirname(cfg_file), exist_ok=True)
    with open(cfg_file, 'w', encoding='utf-8') as f:
        f.write('{}')                      # 故意不含 guest_public_write
    monkeypatch.setattr(_cc, 'CONFIG_FILE', cfg_file)
    monkeypatch.setattr(_cc, '_guest_public_write', True)   # 先弄成开，确认会被默认值覆盖
    _cc.load_config()
    assert _cc.get_guest_public_write() is False, '缺键时必须落到"关"'


# ---------- 下载器配额检查（handler.HTTPHandler._dl_quota_check）----------

def test_dl_quota_check_error_fails_closed():
    """配额检查自身抛异常 → 让这次请求失败，不能当成配额够用"""
    class _Fake:
        def _check_quota(self, save_dir_abs, est_bytes):
            raise RuntimeError('配额检查坏了')

    with pytest.raises(RuntimeError):
        _h.HTTPHandler._dl_quota_check(_Fake(), '/tmp', 1)
