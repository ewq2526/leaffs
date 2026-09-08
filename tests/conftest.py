# -*- coding: utf-8 -*-
"""pytest 共享夹具：真实服务进程（隔离数据根）+ HTTP 客户端

服务以子进程方式运行真实 leaffs（python -m leaffs），数据根由
LEAFFS_PROJECT_ROOT 重定向到临时目录，LEAFFS_NO_WEBVIEW 关闭桌面窗口。
"""
import json
import os
import subprocess
import sys
import tempfile
import time

import httpx
import pytest

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJ_ROOT not in sys.path:
    sys.path.insert(0, PROJ_ROOT)

HTTP_PORT = 8090
WS_PORT = 8091
BASE_URL = 'http://127.0.0.1:%d' % HTTP_PORT


def _tail(path, n=3000):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()[-n:]
    except Exception:
        return ''


@pytest.fixture(scope='session')
def data_root():
    """临时数据根（config/shared_files/.cache 全部落这里；放工作区 .cache 下，
    用 os.makedirs 逐层创建以避开沙箱对 mkdtemp 产物的限制）"""
    import shutil
    import uuid
    tmp_base = os.path.join(PROJ_ROOT, '.cache', 'test_runs')
    root = os.path.join(tmp_base, 'run_' + uuid.uuid4().hex[:12])
    os.makedirs(os.path.join(root, 'config'), exist_ok=True)
    cfg = {
        'http_port': HTTP_PORT,
        'ws_port': WS_PORT,
        'tls_enabled': False,          # 测试走明文，避免自签证书流程
        'guest_mode': True,
        'access_log': False,
        'folder_size_ttl': 60,
        'upload_max_size': 104857600,
        'max_total_conns': 256,
        'max_conn_per_ip': 64,
        'ws_max_conn_per_ip': 20,
    }
    with open(os.path.join(root, 'config', 'server_config.json'),
              'w', encoding='utf-8') as f:
        json.dump(cfg, f)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope='session')
def server(data_root):
    """启动真实服务子进程；就绪后 yield BASE_URL；teardown 终止"""
    env = dict(os.environ)
    env['LEAFFS_PROJECT_ROOT'] = data_root
    env['LEAFFS_NO_WEBVIEW'] = '1'
    log_path = os.path.join(data_root, 'server.log')
    logf = open(log_path, 'wb', buffering=0)
    proc = subprocess.Popen(
        [sys.executable, '-m', 'leaffs'],
        cwd=PROJ_ROOT, env=env, stdout=logf, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError('server exited early; log:\n' + _tail(log_path))
            try:
                r = httpx.get(BASE_URL + '/api/ping', timeout=1.0)
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError('server not ready; log:\n' + _tail(log_path))
        yield BASE_URL
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        logf.close()


@pytest.fixture()
def client(server):
    """每次测试一个干净会话（独立 cookie）"""
    with httpx.Client(base_url=server, timeout=20.0) as c:
        yield c


def login(client, username='admin', password='admin'):
    """登录并把返回会话留在 client cookie 中"""
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get('success') is True, d
    return d


def guest_login(client):
    r = client.post('/api/guest/login')
    assert r.status_code == 200, r.text
    return r.json()
