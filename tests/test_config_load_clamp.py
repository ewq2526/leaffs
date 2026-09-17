# -*- coding: utf-8 -*-
"""配置文件**载入**（`load_config`）的容错钳制。

原来这里有三处不彻底：

1. 只有 `pbkdf2_iterations` / `salt_length` / 几个 IP 上限做了钳制，其余十几个键是
   **直接取盘上的值** —— `upload_max_size: -5`、`max_total_conns: 0` 会被照单全收；
2. `guest_public_write` 等六个布尔键用的是 `bool(cfg.get(...))` —— 盘上写字符串
   `"false"` 会被读成 **True**（语义反转，而这是安全相关的键）；
3. 整个读取块被一个 `except Exception: pass` 包着 —— 任何一行抛异常（比如
   `tls_trust_port` 是字符串），**后面所有键就全不加载**，静默退回模块默认值。

现在统一走 `_to_bool` / `_clamp_num`，且 `load_config` 末尾的 `save_config()` 会把
钳制后的值写回。本文件就是验证"写回的那个文件"。

用**独立端口 + 独立数据根**起一个真实服务进程，不碰 conftest 的会话级实例。
"""
import json
import os
import subprocess
import sys
import time

import httpx
import pytest

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTTP_PORT = 8095
WS_PORT = 8096
BASE_URL = 'http://127.0.0.1:%d' % HTTP_PORT

# 故意写坏：区间外、类型错、布尔写成字符串
BAD_CFG = {
    'http_port': HTTP_PORT,
    'ws_port': WS_PORT,
    'tls_enabled': False,
    'guest_mode': 'false',
    'guest_public_write': 'false',
    'access_log': 'true',
    'harden_config_acls': 'false',
    'max_total_conns': 0,
    'io_idle_timeout_secs': 'abc',
    'upload_max_size': -5,
    'max_api_body_size': 1,
    'session_expiry_days': 99999,
    'cache_ttl': 999999,
    'ca_validity_days': 0,
    'ws_max_conn_per_ip': 999,
    'upload_chunk': 1,
    'tls_trust_port': '8082',
    # 服务端不认识的键（用户手加的 / 将来版本才有的）：必须原样保留
    'zz_user_note': 'keep me',
}

# 启动后写回的配置文件里应当出现的值
EXPECT = {
    'guest_mode': False,            # 'false' → False（不是 bool('false') == True）
    'guest_public_write': False,
    'access_log': True,             # 'true' → True
    'harden_config_acls': False,
    'max_total_conns': 8,           # 0 → 下限 8
    'io_idle_timeout_secs': 1.0,    # 'abc' → 下限
    'upload_max_size': 0,           # -5 → 下限 0（0 = 不限制）
    'max_api_body_size': 4096,
    'session_expiry_days': 3650,
    'cache_ttl': 86400,
    'ca_validity_days': 1,
    'ws_max_conn_per_ip': 256,
    'upload_chunk': 4096,
    'tls_trust_port': 8082,
}


@pytest.fixture(scope='module')
def clamped_root(data_root_factory):
    root = data_root_factory('clamp_')
    with open(os.path.join(root, 'config', 'server_config.json'), 'w', encoding='utf-8') as f:
        json.dump(BAD_CFG, f)
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
                raise RuntimeError('server exited early')
            try:
                if httpx.get(BASE_URL + '/api/ping', timeout=1.0).status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError('server not ready on %s' % BASE_URL)
        yield root
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log.close()


def test_bad_config_is_clamped_and_written_back(clamped_root):
    """启动一次后，写回的配置里每个坏值都被钳到合法边界"""
    with open(os.path.join(clamped_root, 'config', 'server_config.json'),
              encoding='utf-8') as f:
        got = json.load(f)
    bad = []
    for k, want in EXPECT.items():
        assert k in got, '写回的配置里缺 %s（是不是 load_config 中途抛异常了？）' % k
        if got[k] != want:
            bad.append('%s: 期望 %r，实际 %r' % (k, want, got[k]))
    assert not bad, '未被钳制：\n  ' + '\n  '.join(bad)


def test_service_still_usable_after_clamping(clamped_root):
    """钳制之后服务照常可用（没被别人一格配置弄死）"""
    r = httpx.get(BASE_URL + '/api/ping', timeout=5.0)
    assert r.status_code == 200, r.text


def test_clamping_keeps_a_backup_of_the_original(clamped_root):
    """钳制并写回之前必须**备份原文件** —— 真机上配置改错了救不回来，
    所以这一条跟"值被钳对了"同样重要：没有备份，就没法回退。
    """
    bak = os.path.join(clamped_root, 'config', 'server_config.json.bak')
    assert os.path.isfile(bak), '钳制写回前必须留下原配置的备份'
    with open(bak, encoding='utf-8') as f:
        old = json.load(f)
    assert old.get('max_total_conns') == 0, '备份里应当是**改之前**的原值'
    assert old.get('guest_public_write') == 'false', '备份里应当是原样的字符串'
    assert old.get('upload_max_size') == -5


def test_clamping_is_logged_in_the_runtime_log(clamped_root):
    """钳制的记录要落在**运行日志**（leaffs.log）里 —— 管理页日志页看的就是它。

    只打到 stderr 是不够的：真机上没人去看子进程的 stderr。
    """
    log = os.path.join(clamped_root, 'leaffs.log')
    assert os.path.isfile(log), '运行日志没生成'
    with open(log, encoding='utf-8', errors='replace') as f:
        text = f.read()
    assert '钳制' in text, '运行日志里应当有载入钳制的记录，实际内容：\n' + text[-800:]
    assert 'server_config.json.bak' in text, '记录里应当告诉用户备份在哪'
    assert 'max_total_conns' in text, '记录里应当点名是哪些键被改了'


def test_unknown_keys_in_config_are_preserved(clamped_root):
    """**只写越界键**：盘上我们不认识的键必须原样留着（用户手加的、将来版本的）。

    如果这里改成整份重写（`save_config()` 那样从内存重建），那些键会被静默抹掉 ——
    等于替用户重写了他的配置。
    """
    with open(os.path.join(clamped_root, 'config', 'server_config.json'),
              encoding='utf-8') as f:
        got = json.load(f)
    assert got.get('zz_user_note') == 'keep me', \
        '不认识的键被抹掉了：这次写回把整份配置重写了？实际内容：%r' % (got,)


def test_save_failure_is_reported_not_swallowed(data_root_factory):
    """保存失败必须返回 False（不能静默）—— 用**子进程 + 临时根**验证，绝不碰真实配置。

    这条是"真机出事救不了"的另一半：写不进去却不吭声，用户以为存好了，
    重启一看全没了。
    """
    root = data_root_factory('savefail_')
    code = (
        'import os, sys\n'
        'sys.path.insert(0, %r)\n'
        'os.environ["LEAFFS_PROJECT_ROOT"] = %r\n'
        'import leaffs.config.core as cc\n'
        '# point CONFIG_FILE into a nonexistent dir: read -> {}, write -> always fails\n'
        'cc.CONFIG_FILE = os.path.join(%r, "no_such_dir", "server_config.json")\n'
        'print("RESULT=" + str(cc.save_config()))\n'
    ) % (PROJ_ROOT, root, root)
    env = dict(os.environ)
    env['LEAFFS_PROJECT_ROOT'] = root
    env['LEAFFS_NO_WEBVIEW'] = '1'
    outpath = os.path.join(root, 'probe.out')
    with open(outpath, 'wb') as f:
        subprocess.run([sys.executable, '-c', code], cwd=PROJ_ROOT, env=env,
                       stdout=f, stderr=subprocess.STDOUT, timeout=120)
    with open(outpath, encoding='utf-8', errors='replace') as f:
        text = f.read()
    assert 'RESULT=False' in text, \
        'save_config() 写失败时没有返回 False（被静默了）：\n' + text[-800:]
