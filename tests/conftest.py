# -*- coding: utf-8 -*-
"""pytest 共享夹具：真实服务进程（隔离数据根）+ HTTP 客户端

服务以子进程方式运行真实 leaffs（python -m leaffs），数据根由
LEAFFS_PROJECT_ROOT 重定向到临时目录，LEAFFS_NO_WEBVIEW 关闭桌面窗口。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import httpx
import pytest

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJ_ROOT not in sys.path:
    sys.path.insert(0, PROJ_ROOT)

# TLS 开关：默认明文（避免自签证书流程）。LEAFFS_TEST_TLS=1 时改走 HTTPS ——
# 有些路径只在 TLS 下才会暴露：2026-09-21 外部报告「回 HTTP/1.1 但每请求后关连接」，
# 而明文下 13 个路径 × 2 种身份共 26 个组合全部正常复用，一个都没主动关。
# 在此之前 TLS 分支没有任何测试覆盖（本文件原先写死 tls_enabled=False）。
TEST_TLS = os.environ.get('LEAFFS_TEST_TLS') == '1'

HTTP_PORT = 8090
WS_PORT = 8091
BASE_URL = '%s://127.0.0.1:%d' % ('https' if TEST_TLS else 'http', HTTP_PORT)

TEST_RUNS_DIR = os.path.join(PROJ_ROOT, '.cache', 'test_runs')
STALE_ROOT_AGE = 24 * 3600      # 秒：超过这个岁数的残留一定是"死运行"留下的


def new_data_root(prefix='run_'):
    """在 .cache/test_runs 下建一个独立数据根（config/ 一并建好）并返回路径。

    放工作区 .cache 下、用 os.makedirs 逐层创建，是为了避开沙箱对 mkdtemp 产物的限制
    （系统临时目录在受限沙箱里可能写不进去），所以**不用** pytest 的 tmp_path_factory。
    """
    root = os.path.join(TEST_RUNS_DIR, prefix + uuid.uuid4().hex[:12])
    os.makedirs(os.path.join(root, 'config'), exist_ok=True)
    return root


def sweep_stale_roots():
    """清掉 .cache/test_runs 下**超过 24 小时**的残留。

    一次测试会话不可能跑 24 小时，所以 24h 前的条目一定是**被中断的运行**留下的
    （夹具 teardown 的 rmtree 没跑到）。按岁数筛，就不会碰到并发运行中的根。
    """
    if not os.path.isdir(TEST_RUNS_DIR):
        return
    now = time.time()
    for name in os.listdir(TEST_RUNS_DIR):
        path = os.path.join(TEST_RUNS_DIR, name)
        try:
            if now - os.path.getmtime(path) < STALE_ROOT_AGE:
                continue
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
        except OSError:
            pass


def _tail(path, n=3000):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()[-n:]
    except Exception:
        return ''


@pytest.fixture(scope='session')
def data_root():
    """临时数据根（config/shared_files/.cache 全部落这里；放工作区 .cache 下，
    用 os.makedirs 逐层创建以避开沙箱对 mkdtemp 产物的限制）。

    建根之前先 `sweep_stale_roots()`：把被中断的运行留下的旧残留按岁数扫掉。
    """
    sweep_stale_roots()
    root = new_data_root('run_')
    cfg = {
        'http_port': HTTP_PORT,
        'ws_port': WS_PORT,
        'tls_enabled': TEST_TLS,       # 由 LEAFFS_TEST_TLS 决定；默认明文
        'guest_mode': True,
        # 显式打开"游客可写 public"：这是测试要覆盖的行为（test_guest_upload_public 等），
        # 不跟着产品默认值走 —— 产品的默认值已改成关
        'guest_public_write': True,
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
    # 账号文件也预置成已知口令：首启现在是**随机初始口令**（没人知道，服务端本机即超管），
    # 测试要能确定地登录，所以这里直接给出 admin/admin（明文形态，服务端启动会自动哈希）
    with open(os.path.join(root, 'config', 'users.json'), 'w', encoding='utf-8') as f:
        json.dump({'admin': {'password': 'admin', 'role': 'super_admin'}}, f)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope='module')
def data_root_factory():
    """自建数据根 + 模块结束时统一清掉（谁造谁登记）。

    来由：原来是三条路各自 `makedirs`、teardown 只 terminate 服务进程**不删目录**
    （`test_config_load_clamp.py` 的 clamp_/savefail_、`test_access_log_sanitize.py` 的 log_），
    全量跑一轮就多 3 个残留，累积到 69 个 0.2 MB。会话级 `data_root` 一直是清的，
    漏的只有"自建数据根"这一路 —— 所以把"造 + 清"收到这里，谁造谁登记。
    """
    made = []

    def _make(prefix='tmp_'):
        root = new_data_root(prefix)
        made.append(root)
        return root

    yield _make
    for root in made:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope='session')
def server(data_root):
    """启动真实服务子进程；就绪后 yield BASE_URL；teardown 终止

    范围 session：全部测试共享一个服务进程（登录/游客登录有进程内频控，
    如游客 10 次/分钟；新增用例若需 guest 会话需控制总量，勿改为 module 级——
    module 重启边界存在连接重置竞态，曾致 test_guest_upload_own_dir_forbidden
    偶发 httpx.ReadError）。
    """
    # 注：data_root 为 session 级；server 按文件重启时使用同一 data_root，
    # 服务端配置（server_config.json）与账号文件保持一次会话内一致。
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
                r = httpx.get(BASE_URL + '/api/ping', timeout=1.0, verify=False)
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
        # 排障：保留最近一次服务端日志副本（data_root 会话结束即删除）
        try:
            import shutil
            keep = os.path.join(PROJ_ROOT, '.cache', 'last_server.log')
            shutil.copyfile(log_path, keep)
        except Exception:
            pass


@pytest.fixture()
def client(server):
    """每次测试一个干净会话（独立 cookie）。verify=False 供 TLS 模式用自签证书。"""
    with httpx.Client(base_url=server, timeout=20.0, verify=False) as c:
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
