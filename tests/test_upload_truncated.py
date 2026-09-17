# -*- coding: utf-8 -*-
"""上传中断时不能把半截数据当成完整文件落位（LF-24）；前端要有中断处理（LF-25）。

LF-24 的现象（隔离实例端到端复现过）：

    声明 Content-Length=2097324，实际只发 838929 字节后 shutdown(SHUT_WR)
    响应: {"success": true, "saved": 1, "errors": []}
    → 文件 838788 字节 / 期望 2097152 字节：**半截被落位成正式文件**

根因：写 part 的内层循环有三个 break 出口 ——「遇到下一个 part 的边界」「遇到整个 multipart 的
结束边界」（这两个是正常结束）和「数据耗尽」（`not _read_more()`）。第三条与另外两条
**共用同一段落位逻辑**，于是截断的数据照样 `os.replace` 落位、`saved += 1`、回 `success: true`。
而对端 RST 那条路（抛异常）反而会正确清理 `.part` —— **只有 EOF 这条漏了**。

判据：**只有见到 multipart 边界才算完整**。格式完好的 body 必然以 `--boundary--` 结束，
所以"数据耗尽却没见到边界"就是截断 —— 比区分 FIN/RST 更稳，也顺带覆盖 body 被截断的情况。

⚠️ 必须走端到端（自建实例 + 原始 socket）：单元级直接调 `handle_upload` 用的是**测试进程**的
`UPLOAD_DIR`（= 工作区的 `shared_files`），往那儿写测试文件是不可接受的。
"""
import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTTP_PORT = 8105
WS_PORT = 8106
BASE_URL = 'http://127.0.0.1:%d' % HTTP_PORT

BOUNDARY = '----LeafFSTruncProbe7f3a'
PAYLOAD = bytes(range(256)) * 4096          # 1 MiB
FILENAME = 'trunc.bin'


def _body(filename=FILENAME):
    b = BOUNDARY.encode()
    head = (b'--' + b + b'\r\n'
            b'Content-Disposition: form-data; name="file"; filename="' + filename.encode() + b'"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\n')
    return head + PAYLOAD + b'\r\n--' + b + b'--\r\n'


@pytest.fixture(scope='module')
def trunc_root(data_root_factory):
    """独立数据根 + 独立端口起真实服务"""
    root = data_root_factory('trunc_')
    with open(os.path.join(root, 'config', 'server_config.json'), 'w', encoding='utf-8') as f:
        json.dump({'http_port': HTTP_PORT, 'ws_port': WS_PORT, 'tls_enabled': False,
                   'guest_mode': False, 'access_log': False,
                   'upload_max_size': 100 * 1024 * 1024}, f)
    with open(os.path.join(root, 'config', 'users.json'), 'w', encoding='utf-8') as f:
        json.dump({'admin': {'password': 'admin', 'role': 'super_admin'}}, f)
    env = dict(os.environ)
    env['LEAFFS_PROJECT_ROOT'] = root
    env['LEAFFS_NO_WEBVIEW'] = '1'
    log = open(os.path.join(root, 'server.log'), 'wb', buffering=0)
    proc = subprocess.Popen([sys.executable, '-m', 'leaffs'], cwd=PROJ_ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError('server exited early; log tail:\n' + _tail(root))
            try:
                if httpx.get(BASE_URL + '/api/ping', timeout=1.0).status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError('server not ready on %s; log:\n%s' % (BASE_URL, _tail(root)))
        yield root
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log.close()


def _tail(root, n=1000):
    try:
        with open(os.path.join(root, 'leaffs.log'), encoding='utf-8', errors='replace') as f:
            return f.read()[-n:]
    except Exception:
        return '(无日志)'


@pytest.fixture(scope='module')
def cookie(trunc_root):
    with httpx.Client(base_url=BASE_URL, timeout=30.0) as c:
        r = c.post('/api/auth/login', json={'username': 'admin', 'password': 'admin'})
        assert r.status_code == 200, r.text
        return '; '.join('%s=%s' % (k, v) for k, v in c.cookies.items())


def _post_raw(cookie_hdr, sub, data, shutdown_write=True):
    """原始 socket 发 data（可截断），然后 FIN 或直接 close；返回响应文本"""
    body = _body()
    s = socket.create_connection(('127.0.0.1', HTTP_PORT), timeout=30)
    head = ('POST /api/upload?path=%s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'
            'Content-Type: multipart/form-data; boundary=%s\r\n'
            'Content-Length: %d\r\nCookie: %s\r\nConnection: close\r\n\r\n'
            % (sub, HTTP_PORT, BOUNDARY, len(body), cookie_hdr)).encode()
    s.sendall(head + data)
    time.sleep(0.4)
    if shutdown_write:
        s.shutdown(socket.SHUT_WR)
    try:
        resp = s.recv(65536).decode('utf-8', 'replace')
    except Exception:
        resp = ''
    s.close()
    time.sleep(1.0)                      # 给服务端收尾的时间
    return resp


def _dir(root, sub):
    return os.path.join(root, 'shared_files', sub)


def test_full_upload_lands_intact(trunc_root, cookie):
    """对照组：完整上传必须成功、且落位内容一致（防"干脆一律不落位"的假修复）"""
    resp = _post_raw(cookie, 'trunc_full', _body())
    assert ' 200 ' in resp.split('\r\n')[0], '完整上传没成功：%s' % resp[:200]
    d = _dir(trunc_root, 'trunc_full')
    p = os.path.join(d, FILENAME)
    assert os.path.isfile(p), '完整上传没落位：%s' % (os.listdir(d) if os.path.isdir(d) else [])
    with open(p, 'rb') as f:
        assert f.read() == PAYLOAD, '落位内容与上传内容不一致'


def test_eof_truncated_upload_is_discarded(trunc_root, cookie):
    """EOF（FIN）截断：**不落位** —— 原来会把半截数据当成完整文件

    响应那条只做**条件断言**：客户端提前断开时，服务端可能读到 EOF（正常回错误响应），
    也可能撞上 RST 而走 `DISCONNECTED_EXCEPTIONS` —— 那条路刻意**不补响应**
    （"请求没完成即断开"不该算 500）。全量跑时机器更忙，时序一变就会撞上后者
    （实测：单独跑绿、全量跑红，就是这个）。两种都由"不落位"兜住，
    所以核心断言是下面那条文件断言。
    """
    body = _body()
    resp = _post_raw(cookie, 'trunc_eof', body[:int(len(body) * 0.4)], shutdown_write=True)
    tail = resp.split('\r\n\r\n')[-1] if resp else ''
    if tail:
        assert '"success": false' in tail, \
            '截断的上传被当成成功了（响应 %r）—— 半截数据会落位成正式文件' % tail[:200]
    d = _dir(trunc_root, 'trunc_eof')
    assert not os.path.isfile(os.path.join(d, FILENAME)), \
        '截断的数据被落位成了正式文件：%s' % (os.listdir(d) if os.path.isdir(d) else [])


def test_hard_close_truncated_upload_is_discarded(trunc_root, cookie):
    """硬断（直接 close，不走 FIN）：同样不落位 —— 这条路原来就是对的，别改坏"""
    body = _body()
    _post_raw(cookie, 'trunc_hard', body[:int(len(body) * 0.4)], shutdown_write=False)
    d = _dir(trunc_root, 'trunc_hard')
    assert not os.path.isfile(os.path.join(d, FILENAME)), \
        '硬断的数据被落位了：%s' % (os.listdir(d) if os.path.isdir(d) else [])


def test_frontend_has_abort_and_beforeunload():
    """LF-25：两份 home 页都要有 `onabort`（复位队列）与 `beforeunload`（拦误刷新）"""
    base = os.path.join(PROJ_ROOT, 'leaffs', 'web_page', 'home')
    missing = []
    for name in ('home.html', 'home.en.html'):
        with open(os.path.join(base, name), encoding='utf-8') as f:
            text = f.read()
        if 'xhr.onabort' not in text:
            missing.append('%s：缺 xhr.onabort' % name)
        if 'beforeunload' not in text:
            missing.append('%s：缺 beforeunload' % name)
    assert not missing, '前端中断处理没补齐：\n  ' + '\n  '.join(missing)
