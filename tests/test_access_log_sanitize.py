# -*- coding: utf-8 -*-
"""访问日志必须干净（LF-12 回归）。

黑盒报告的现象（已核实）：`log_message` 把请求行第 2 段直接拼进日志，只做了
"敏感参数值替换"（A-16），**没有控制字符剔除** —— 而项目早就有 `sanitize_log_text`，
登录那边也在用，唯独这条路径没走。

报告的三点里**两点不成立**：① 换行注入不可能（请求行按行读、`self.path` 未解码）；
② 「攻击者存活确认通道」不成立（他本就看得到自己的状态码）；展示层还有 `esc()`。
**真实后果**只是：`\\x1b` 这类控制字节会原样落进日志文件，`cat`/`tail` 时 ANSI 转义
真的会生效、把显示搅乱。

⚠️ 两个前提：
  * 这类字节**必须原始字节直发** —— `self.path` 未解码，URL 里写 `%1b` 进去的是
    4 个可打印字符，不是控制字符，所以用 socket 发；
  * 会话级实例把 `access_log` 关着（测试环境有意如此），所以这里自起一个**打开访问日志**
    的独立实例，否则 `log_message` 第一句就 return，什么都测不到。
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
HTTP_PORT = 8097
WS_PORT = 8098
BASE_URL = 'http://127.0.0.1:%d' % HTTP_PORT


@pytest.fixture(scope='module')
def log_root(data_root_factory):
    """独立实例：访问日志**打开**（会话级实例关着它）"""
    root = data_root_factory('log_')
    cfg = {
        'http_port': HTTP_PORT,
        'ws_port': WS_PORT,
        'tls_enabled': False,
        'guest_mode': True,
        'access_log': True,          # ← 本文件要测的就是它
        'upload_max_size': 104857600,
        'max_total_conns': 256,
    }
    with open(os.path.join(root, 'config', 'server_config.json'), 'w', encoding='utf-8') as f:
        json.dump(cfg, f)
    with open(os.path.join(root, 'config', 'users.json'), 'w', encoding='utf-8') as f:
        json.dump({'admin': {'password': 'admin', 'role': 'super_admin'}}, f)
    env = dict(os.environ)
    env['LEAFFS_PROJECT_ROOT'] = root
    env['LEAFFS_NO_WEBVIEW'] = '1'
    logf = open(os.path.join(root, 'server.log'), 'wb', buffering=0)
    proc = subprocess.Popen([sys.executable, '-m', 'leaffs'], cwd=PROJ_ROOT, env=env,
                            stdout=logf, stderr=subprocess.STDOUT)
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
        logf.close()


def _raw_request(line_bytes):
    """直发原始字节的请求行（绕过 URL 编码）"""
    s = socket.create_connection(('127.0.0.1', HTTP_PORT), timeout=5)
    try:
        s.sendall(line_bytes + b'\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n')
        try:
            s.recv(4096)
        except Exception:
            pass
    finally:
        s.close()


def _log_text(log_root, needle=None, tries=15):
    """读运行日志（等它落盘；给了 needle 就等到出现为止）"""
    path = os.path.join(log_root, 'leaffs.log')
    text = ''
    for _ in range(tries):
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                text = f.read()
        except Exception:
            text = ''
        if needle and needle in text:
            break
        time.sleep(0.2)
    return text


def test_control_bytes_never_reach_the_log(log_root):
    """直发带 \\x1b / \\x07 的请求行 → 日志里不该出现这些控制字节"""
    _raw_request(b'GET /a\x1bb\x07c HTTP/1.1')
    text = _log_text(log_root, needle='/abc')
    assert '\x1b' not in text, '日志里出现了 ESC 控制字节（终端显示会被搅乱）：%r' % text[-400:]
    assert '\x07' not in text, '日志里出现了 BEL 控制字节：%r' % text[-400:]
    assert '/abc' in text, '请求本身应当仍被记录（只是控制字符被剔掉）：%r' % text[-400:]


def test_sensitive_params_still_masked(log_root):
    """A-16 的敏感参数脱敏不能被搞坏：`sid=` 的值仍要变成 `***`"""
    httpx.get(BASE_URL + '/api/ping?sid=SECRETVALUEZZ&x=1', timeout=5.0)
    text = _log_text(log_root, needle='sid=***')
    assert 'SECRETVALUEZZ' not in text, '敏感参数值没脱敏：%r' % text[-400:]
    assert 'sid=***' in text, '脱敏格式变了？%r' % text[-400:]


def test_overlong_request_line_is_truncated(log_root):
    """超长请求行 → 日志里那条要截断（防日志膨胀）"""
    _raw_request(b'GET /' + b'z' * 5000 + b' HTTP/1.1')
    text = _log_text(log_root, needle='zzzz')
    lines = [ln for ln in text.splitlines() if 'zzzz' in ln]
    assert lines, '超长请求没进日志？'
    assert len(lines[-1]) < 600, '日志行没被截断，长度 %d' % len(lines[-1])
