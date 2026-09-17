# -*- coding: utf-8 -*-
"""上传配额「在途字节」必须在请求结束时释放（LF-21）。

原来 `leaffs/files/api.py` 结算时传 `_actual = cl if saved > 0 else 0`，而
`utils/core.py` 的 `quota_settle` 是 `pending - reserved + actual` —— 成功时
`-cl + cl` 相互抵消，预留**永久留在账本里**（只有失败才归零）。于是
`_quota_pending` 从"在途账"变成"历史累积账"，而 `_check_quota` 又把它与磁盘
实际占用**相加** → 同一批字节算两遍，攒过配额后每一次上传都在预检被 413。
（实测指纹：大文件上传成功后紧接着连续 413；且删掉已上传文件也不恢复 ——
账本在进程内存里，只有重启才清零。）

本文件用**独立端口 + 独立数据根**起真实服务，把 `public_quota` 调到 20 KB：

    第 1 个 8 KB  → 200（磁盘 8 KB）
    第 2 个 8 KB  → 200（磁盘 16 KB，仍在上限内）    ← 修复前这里就是 413
    第 3 个 8 KB  → 413（磁盘将到 24 KB，真的超了）   ← 对照组：配额仍然在拦

第 3 步是对照组，防的是"把配额限制改没了"这种假修复。
每一步之间都等 > `debounce_delay`：`invalidate_folder_cache_smart` 是防抖的，
不等的话第 3 步可能因缓存滞后被误放行，那就把"防抖滞后"误判成缺陷了。
"""
import json
import os
import subprocess
import sys
import time

import httpx
import pytest

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTTP_PORT = 8099
WS_PORT = 8100
BASE_URL = 'http://127.0.0.1:%d' % HTTP_PORT

QUOTA = 20 * 1024                # public_quota = 20 KB
CHUNK = 8 * 1024                 # 每个文件 8 KB
DEBOUNCE = 2.0                   # 与下面配置里的 debounce_delay 一致
# 文件刻意取小：413 是在读 body **之前**预检发出的，服务端随即关闭连接；
# body 若还没发完，客户端只会拿到连接中止（WinError 10053）而不是 413。
# 8 KB 能一次发完，所以下面每一步的 413 都是可读的响应。


@pytest.fixture(scope='module')
def quota_root(data_root_factory):
    """独立数据根 + 独立端口起真实服务进程，配额调到 2 MB"""
    root = data_root_factory('quota_')
    cfg = {
        'http_port': HTTP_PORT,
        'ws_port': WS_PORT,
        'tls_enabled': False,
        'guest_mode': False,
        'access_log': False,
        'public_quota': QUOTA,
        'user_quota': QUOTA,
        'total_quota': 16 * 1024 * 1024,
        'upload_max_size': 10 * 1024 * 1024,
        'debounce_delay': DEBOUNCE,
    }
    with open(os.path.join(root, 'config', 'server_config.json'), 'w', encoding='utf-8') as f:
        json.dump(cfg, f)
    # data_root_factory 只建目录，账号文件要自己预置，否则服务会生成随机初始口令、登不进去
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
                raise RuntimeError('server exited early; log:\n' + _tail(root))
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


def _tail(root, n=1200):
    try:
        with open(os.path.join(root, 'leaffs.log'), encoding='utf-8', errors='replace') as f:
            return f.read()[-n:]
    except Exception:
        return '(无日志)'


@pytest.fixture(scope='module')
def quota_client(quota_root):
    with httpx.Client(base_url=BASE_URL, timeout=60.0) as c:
        r = c.post('/api/auth/login', json={'username': 'admin', 'password': 'admin'})
        assert r.status_code == 200, r.text
        assert r.json().get('success') is True, r.text
        yield c


def _upload(client, name, size=CHUNK):
    return client.post('/api/upload?path=public',
                       files={'file': (name, b'x' * size, 'application/octet-stream')})


def test_sequential_uploads_are_not_double_counted(quota_client):
    """连续上传：已落盘的字节不能再记一遍账"""
    r1 = _upload(quota_client, 'q1.bin')
    assert r1.status_code == 200, '第 1 个上传应当成功：%s' % r1.text
    assert r1.json().get('saved') == 1, r1.text

    time.sleep(DEBOUNCE + 0.6)      # 让防抖定时器到期，排除缓存滞后

    r2 = _upload(quota_client, 'q2.bin')
    assert r2.status_code == 200, (
        '第 2 个上传被拒了（磁盘 8 KB + 8 KB 仍在 20 KB 上限内）—— '
        '配额账本的"在途预留"在上传成功后没有释放：%s' % r2.text)
    assert r2.json().get('saved') == 1, r2.text

    time.sleep(DEBOUNCE + 0.6)

    r3 = _upload(quota_client, 'q3.bin')
    assert r3.status_code == 413, (
        '第 3 个上传应当被配额拦下（磁盘将到 24 KB > 20 KB）—— '
        '配额限制是不是被改没了？：%s' % r3.text)
