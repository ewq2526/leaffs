# -*- coding: utf-8 -*-
"""§二 第 4 条：上传的配额预留必须按**实际读入**的字节记账，不能被"声明的 Content-Length"虚占。

**修复前的做法**（`files/api.py:handle_upload`）：

```
ok, err = _check_quota(target_dir, cl)     # 读 body **之前**用声明的 cl 预检
quota_reserve(b, cl)                       # ← 按**客户端声明**的 cl 预留"在途字节"
... 读 body ...                            # ← 这里可以只发一点点、然后一直不发
finally: quota_settle(b, cl, 0)            # 请求结束时才释放
```

而 `_check_quota` 判的是「**磁盘占用 ＋ 在途预留 ＋ new_size > 配额**」——
于是**一条只发 header、正文不发完的连接**（`Content-Length` 声明成配额大小）就能把配额
**虚拟占满**，期间别人的上传全部 413，直到这条连接结束或读超时（60s），而且可以循环维持。

实测（探针 `.cache/probe_quota_reserve.py`，`public_quota` 设 1 MiB）：
受害者传 100 字节 → **413「公共文件夹空间不足」**；攻击者断开后同样的上传 → **200**。

**修法**：预检保留（它不记账，虚报只会拒掉攻击者自己）；记账改成在**唯一的读取点**
（`files/core.py` 的 `_read_more`）按真实读入的字节累加 —— `handle_upload(…, on_bytes=…)`
每读一块回调一次，`files/api.py` 那边攒够 1 MiB 记一次账并实时校验；超配额就抛
`UploadQuotaExceeded` **中断整个上传**（附加项：不把剩下的 body 白收完，也不落位半截文件）。
"""
import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

HTTP_PORT, WS_PORT = 8119, 8120
PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = '127.0.0.1'
BASE = 'http://%s:%d' % (HOST, HTTP_PORT)
MIB = 1024 * 1024
PUBLIC_QUOTA = 1 * MIB          # 故意设小，好观察"占满"
UPLOAD_MAX = 10 * MIB
BOUNDARY = 'XBOUNDARYQUOTA'


def _raw_upload_start(cookie, cl, path='public', body=b''):
    """用原始 socket 起一个上传请求：**自己控制发多少正文**。

    带 `Connection: close` —— 服务端响应后会关连接，读响应就能读到 EOF（不必解析长度）。
    """
    s = socket.create_connection((HOST, HTTP_PORT), timeout=15)
    head = (
        'POST /api/upload?path=%s HTTP/1.1\r\n'
        'Host: %s:%d\r\n'
        'Cookie: wifi_session=%s\r\n'
        'Content-Type: multipart/form-data; boundary=%s\r\n'
        'Content-Length: %d\r\n'
        'Connection: close\r\n'
        '\r\n'
    ) % (path, HOST, HTTP_PORT, cookie, BOUNDARY, cl)
    s.sendall(head.encode())
    if body:
        s.sendall(body)
    return s


def _part_head(filename):
    return ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n' % (BOUNDARY, filename)).encode()


@pytest.fixture(scope='module')
def inst(data_root_factory):
    """隔离实例：public_quota = 1 MiB（小到一眼能占满），upload_max_size = 10 MiB"""
    root = data_root_factory('quota_')
    cfg = {'http_port': HTTP_PORT, 'ws_port': WS_PORT, 'tls_enabled': False,
           'guest_mode': True, 'guest_public_write': True, 'access_log': False,
           'public_quota': PUBLIC_QUOTA, 'upload_max_size': UPLOAD_MAX}
    with open(os.path.join(root, 'config', 'server_config.json'), 'w', encoding='utf-8') as f:
        json.dump(cfg, f)
    with open(os.path.join(root, 'config', 'users.json'), 'w', encoding='utf-8') as f:
        json.dump({'admin': {'password': 'admin', 'role': 'super_admin'}}, f)

    env = dict(os.environ, LEAFFS_PROJECT_ROOT=root, LEAFFS_NO_WEBVIEW='1')
    logf = open(os.path.join(root, 'server.log'), 'wb', buffering=0)
    proc = subprocess.Popen([sys.executable, '-m', 'leaffs'], cwd=PROJ_ROOT, env=env,
                            stdout=logf, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError('隔离实例提前退出')
            try:
                if httpx.get(BASE + '/api/ping', timeout=1.0).status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError('隔离实例没起来：%s' % BASE)
        yield root
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        logf.close()


@pytest.fixture(scope='module')
def cookie(inst):
    with httpx.Client(base_url=BASE, timeout=20) as c:
        r = c.post('/api/auth/login', json={'username': 'admin', 'password': 'admin'})
        assert r.status_code == 200, r.text
        ck = c.cookies.get('wifi_session')
    assert ck
    return ck


def test_declared_content_length_cannot_hog_quota(inst, cookie):
    """★ 复现（旧代码必红）：声明一个大 `Content-Length`、正文不发完，**占不到配额**

    旧行为：`quota_reserve(b, cl)` 按声明值预留 → 一条这样的连接就把 public 配额虚拟占满，
    期间别人的上传全被 413。
    """
    atk = _raw_upload_start(cookie, cl=PUBLIC_QUOTA, body=_part_head('hog.bin') + b'A' * 8192)
    try:
        time.sleep(1.2)          # 让服务端进入读取（旧代码此时已把 cl 记进在途账）
        with httpx.Client(base_url=BASE, timeout=20) as c:
            c.cookies.set('wifi_session', cookie)     # ⚠️ 必须带会话，否则 404 Unauthorized
            r = c.post('/api/upload?path=public',
                       files={'file': ('innocent.txt', b'x' * 100, 'text/plain')})
        assert r.status_code == 200, (
            '受害者被"声明的 Content-Length"虚占的配额挡住了：%d %s\n'
            '（预留必须按**实际读入**的字节记，不能按客户端说了算的声明值）'
            % (r.status_code, r.text[:200]))
    finally:
        try:
            atk.close()
        except Exception:
            pass


def test_oversized_declared_length_still_rejected_by_precheck(inst, cookie):
    """对照：读前预检**没被削弱** —— 声明值大到超过配额/上限的请求仍直接 413

    （防"修过头"：预检仍在用声明值，只是它**不记账**了。）
    """
    head = (
        'POST /api/upload?path=public HTTP/1.1\r\n'
        'Host: %s:%d\r\n'
        'Cookie: wifi_session=%s\r\n'
        'Content-Type: multipart/form-data; boundary=%s\r\n'
        'Content-Length: %d\r\n'
        'Connection: close\r\n'
        '\r\n'
    ) % (HOST, HTTP_PORT, cookie, BOUNDARY, UPLOAD_MAX * 10)
    s = socket.create_connection((HOST, HTTP_PORT), timeout=15)
    try:
        s.sendall(head.encode())
        s.settimeout(10)
        raw = b''
        while b'\r\n\r\n' not in raw:
            piece = s.recv(4096)
            if not piece:
                break
            raw += piece
        assert b'413' in raw.split(b'\r\n')[0], \
            '超过上限的声明值应当被读前预检直接拒（413）：%r' % raw[:200]
    finally:
        s.close()


# ---------- 附加项：读流期间的实时校验（白盒 —— 时序确定，不做脆弱的并发计时）----------

class _ChunkedReader:
    """每次 `read()` 只给 step 字节的假 rfile —— 用来证明"读到一半就停了" """

    def __init__(self, data, step):
        self._data = data
        self._pos = 0
        self._step = step
        self.reads = 0

    def read(self, n):
        if self._pos >= len(self._data):
            return b''
        size = min(n, self._step)
        out = self._data[self._pos:self._pos + size]
        self._pos += size
        self.reads += 1
        return out


def _mk_body(size=512 * 1024, name='big.bin'):
    return (('--XB\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
             'Content-Type: application/octet-stream\r\n\r\n' % name).encode()
            + b'A' * size + b'\r\n--XB--\r\n')


def test_upload_aborts_mid_stream_when_callback_raises_inside_file_block(data_root_factory,
                                                                        monkeypatch):
    """附加项（块内）：写 part 期间回调抛 `UploadQuotaExceeded` → 中断 + 如实记错 + 不落位

    白盒而不是端到端：端到端要制造"两条连接正好同时把配额挤爆"的时序，那种测试会变 flaky。
    这里直接钉机制 —— 用分块读取器，让回调在**第 2 次**被调用时抛：
    第 1 块（32 KB）已经足够解析出 part 头（约 110 字节），所以第 2 次调用确实发生在
    "写 part"的块内，走的是"记一条带文件名的错并结束"那条分支。
    """
    from leaffs.files import core as _fcore
    from leaffs.utils.core import UploadQuotaExceeded

    root = data_root_factory('quotaabort_')
    shared = os.path.join(root, 'shared_files')
    # ⚠️ **不能**靠环境变量：`leaffs.paths` 在**导入时**就把 UPLOAD_DIR 定下来了，
    # 测试进程里改 env 不会重算 —— 那样 `handle_upload` 会往项目目录里写文件。
    # 直接替换模块级常量（它内部用的就是这两个名字）。
    monkeypatch.setattr(_fcore, 'UPLOAD_DIR', shared)
    monkeypatch.setattr(_fcore, 'UPLOAD_TMP_DIR', os.path.join(shared, '.uploads'))

    body = _mk_body()
    reader = _ChunkedReader(body, 32 * 1024)      # 每块 32 KB ⇒ 共 ~17 块
    seen = []

    def _on_bytes(n):
        seen.append(n)
        if len(seen) >= 2:                        # 第 2 次：此时正在写 part（块内）
            raise UploadQuotaExceeded('公共文件夹空间不足')

    saved, errors = _fcore.handle_upload(reader, 'multipart/form-data; boundary=XB',
                                         len(body), 'public', on_bytes=_on_bytes)

    assert saved == 0, '超配额时不该有任何文件落盘'
    assert any('超过可用配额' in e for e in errors), '必须如实说明是超配额：%r' % errors
    assert len(seen) == 2, '回调一抛就该停，实际被调了 %d 次' % len(seen)
    assert reader.reads <= 4, (
        '读到一半就该停，实际读了 %d 次（body 共 ~17 块）—— 附加项没生效' % reader.reads)
    pub = os.path.join(shared, 'public')
    left = os.listdir(pub) if os.path.isdir(pub) else []
    assert not [n for n in left if n.endswith('.bin')], '超配额中断后留下了文件：%s' % left


def test_upload_propagates_quota_exception_outside_file_block(data_root_factory, monkeypatch):
    """附加项（块外）：解析 boundary / part 头期间抛出 → **契约是"冒给调用方"**

    这条把契约本身钉住：`files/api.py` 的 `except UploadQuotaExceeded`
    就是接它的（接住后回 413）。若哪天有人在 core 里把它吞掉、变成"静默失败且不说明原因"，
    这条会红。
    """
    from leaffs.files import core as _fcore
    from leaffs.utils.core import UploadQuotaExceeded

    root = data_root_factory('quotaouter_')
    shared = os.path.join(root, 'shared_files')
    monkeypatch.setattr(_fcore, 'UPLOAD_DIR', shared)
    monkeypatch.setattr(_fcore, 'UPLOAD_TMP_DIR', os.path.join(shared, '.uploads'))

    body = _mk_body()
    reader = _ChunkedReader(body, 32 * 1024)

    def _on_bytes(n):
        raise UploadQuotaExceeded('公共文件夹空间不足')     # 第 1 次就抛 = 块外

    with pytest.raises(UploadQuotaExceeded):
        _fcore.handle_upload(reader, 'multipart/form-data; boundary=XB',
                             len(body), 'public', on_bytes=_on_bytes)


def test_upload_without_callback_still_works(data_root_factory, monkeypatch):
    """对照：不传 `on_bytes`（旧调用方式）时上传照常成功 —— 默认参数是向后兼容的"""
    import io

    from leaffs.files import core as _fcore

    root = data_root_factory('quotanocb_')
    shared = os.path.join(root, 'shared_files')
    monkeypatch.setattr(_fcore, 'UPLOAD_DIR', shared)
    monkeypatch.setattr(_fcore, 'UPLOAD_TMP_DIR', os.path.join(shared, '.uploads'))

    body = (b'--XB\r\nContent-Disposition: form-data; name="file"; filename="ok.txt"\r\n'
            b'Content-Type: text/plain\r\n\r\nhello\r\n--XB--\r\n')
    saved, errors = _fcore.handle_upload(io.BytesIO(body),
                                         'multipart/form-data; boundary=XB',
                                         len(body), 'public')
    assert saved == 1, '不带回调的正常上传必须成功：saved=%r errors=%r' % (saved, errors)
    assert not errors, errors
