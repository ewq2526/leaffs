# -*- coding: utf-8 -*-
"""会话表落盘（2026-09-17）：**重启服务不该把人登出**。

**修复前的做法**：`auth/core.py` 的 `_sessions` 是**纯内存 dict**，代码注释自述
「重启即清空，可接受」。实际不可接受 —— 每次重启服务、每次安卓 App 被系统杀掉后重启，
用户都要重新登录一遍。

**修法**：会话表落盘到 `config/sessions.json`；`app.start_server` 里 `load_users()` 之后
调 `load_sessions()`；**只在"会话集合变化"时写盘**（新建 / 扫码兑换 / 登出 / 关游客模式 /
踢人 / 删用户），`get_session` 这种每请求都走的只读路径**绝不写盘**。

⚠️ 附带的安全要求：**"登出 / 踢人 / 删用户"也必须落盘** —— 否则那些会话只从内存消失，
**重启之后会复活**（被删掉的用户带着旧会话回来）。本文件第二条测试就是钉这个。

⚠️ 这个文件里装着 **sid**（等于凭据），与同目录的 `users.json`（口令哈希）、
`share_access.json`（授权票据）同级敏感；`.gitignore` 已挡住 `config/`，不入库。
"""
import json
import os
import subprocess
import sys
import time

import httpx
import pytest

from leaffs.auth import core as C

HTTP_PORT, WS_PORT = 8121, 8122
PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = 'http://127.0.0.1:%d' % HTTP_PORT


def _write_cfg(root):
    with open(os.path.join(root, 'config', 'server_config.json'), 'w', encoding='utf-8') as f:
        json.dump({'http_port': HTTP_PORT, 'ws_port': WS_PORT, 'tls_enabled': False,
                   'guest_mode': True, 'access_log': False}, f)
    with open(os.path.join(root, 'config', 'users.json'), 'w', encoding='utf-8') as f:
        json.dump({'admin': {'password': 'admin', 'role': 'super_admin'}}, f)


def _start(root):
    env = dict(os.environ, LEAFFS_PROJECT_ROOT=root, LEAFFS_NO_WEBVIEW='1')
    logf = open(os.path.join(root, 'server.log'), 'ab', buffering=0)
    proc = subprocess.Popen([sys.executable, '-m', 'leaffs'], cwd=PROJ_ROOT, env=env,
                            stdout=logf, stderr=subprocess.STDOUT)
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError('服务提前退出')
        try:
            if httpx.get(BASE + '/api/ping', timeout=1.0).status_code == 200:
                return proc, logf
        except Exception:
            time.sleep(0.5)
    raise RuntimeError('服务没起来：%s' % BASE)


def _stop(proc, logf):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    logf.close()


def test_session_survives_restart(data_root_factory):
    """★ 重启服务后，**同一个 Cookie 仍然有效**（这就是"重启不该把人登出"）

    真起两次服务（同一个数据根），不是白盒模拟 —— 因为要证明的正是"进程重启"这件事。
    """
    root = data_root_factory('sess_')
    _write_cfg(root)

    proc, logf = _start(root)
    try:
        with httpx.Client(base_url=BASE, timeout=20) as c:
            r = c.post('/api/auth/login', json={'username': 'admin', 'password': 'admin'})
            assert r.status_code == 200, r.text
            cookie = c.cookies.get('wifi_session')
        assert cookie, '登录没拿到会话 Cookie'
        # 顺带确认会话表**确实落盘了**（不然下面的断言会以"重启丢会话"的形式失败，
        # 但那时看不出是漏了写盘还是加载没生效）
        sessions_file = os.path.join(root, 'config', 'sessions.json')
        assert os.path.isfile(sessions_file), '会话表没有落盘：%s' % sessions_file
    finally:
        _stop(proc, logf)

    proc, logf = _start(root)
    try:
        with httpx.Client(base_url=BASE, timeout=20) as c:
            c.cookies.set('wifi_session', cookie)
            d = c.get('/api/auth/check').json()
        assert d.get('username') == 'admin', (
            '重启后会话失效了 —— 又被登出一次（会话表要么没落盘、要么没在启动时加载）：%r' % d)
    finally:
        _stop(proc, logf)


def test_removed_session_does_not_come_back_after_restart(data_root_factory, monkeypatch):
    """★ 踢人 / 删用户必须把盘上一起改掉 —— 否则重启后那些会话**复活**

    白盒（比端到端稳、也更快）：造一个 bob 的会话 → 删掉 bob → 重启（清空内存再加载）
    → 断言 bob 的 sid **不在**表里。
    """
    root = data_root_factory('sessdel_')
    monkeypatch.setattr(C, 'USERS_FILE', os.path.join(root, 'users.json'))
    monkeypatch.setattr(C, 'SESSIONS_FILE', os.path.join(root, 'sessions.json'))
    monkeypatch.setattr(C, '_users', {'admin': {'role': 'super_admin', 'password': 'x' * 20},
                                      'bob': {'role': 'user', 'password': 'y' * 20}})
    monkeypatch.setattr(C, '_sessions', {})

    sid_bob = C.create_session('bob', 'user')
    assert sid_bob in C._sessions
    assert os.path.isfile(C.SESSIONS_FILE), '建会话后没有落盘'

    ok, err = C.delete_user('bob', caller_role='super_admin')
    assert ok, err
    assert sid_bob not in C._sessions, '删用户没踢掉他的会话'

    # 模拟"重启"：内存清空后从盘上加载
    monkeypatch.setattr(C, '_sessions', {})
    C.load_sessions()
    assert sid_bob not in C._sessions, (
        '重启后被删用户的会话复活了 —— 踢人只改了内存、没改盘（登出/踢人必须落盘）')


def test_logout_is_persisted(data_root_factory, monkeypatch):
    """对照：登出也要落盘（否则"登出"在重启后白做）

    ⚠️ **必须带"建会话时盘上确实有它"的前置断言**：否则在"从不落盘"的旧行为下，
    盘上没有任何文件 ⇒ 重启加载得到空表 ⇒ "重启后没有它"**也成立** —— 这条会变成假绿
    （第一次有牙验证时就踩到了：它在我把落盘改成空操作之后仍然是绿的）。
    """
    root = data_root_factory('sessout_')
    monkeypatch.setattr(C, 'USERS_FILE', os.path.join(root, 'users.json'))
    monkeypatch.setattr(C, 'SESSIONS_FILE', os.path.join(root, 'sessions.json'))
    monkeypatch.setattr(C, '_sessions', {})

    sid = C.create_session('admin', 'super_admin')
    with open(C.SESSIONS_FILE, encoding='utf-8') as f:
        assert sid in json.load(f), '建会话后盘上没有它（落盘没生效）'

    C.remove_session(sid)
    with open(C.SESSIONS_FILE, encoding='utf-8') as f:
        assert sid not in json.load(f), '登出后盘上还留着它'

    monkeypatch.setattr(C, '_sessions', {})
    C.load_sessions()
    assert sid not in C._sessions, '登出没落盘：重启后那个会话又回来了'


def test_expired_sessions_are_dropped_on_load(data_root_factory, monkeypatch):
    """对照：启动加载时丢掉已过期的条目（盘上留着它们没有意义，还占容量配额）"""
    root = data_root_factory('sessexp_')
    monkeypatch.setattr(C, 'SESSIONS_FILE', os.path.join(root, 'sessions.json'))
    with open(C.SESSIONS_FILE, 'w', encoding='utf-8') as f:
        json.dump({'live': {'expiry': time.time() + 3600, 'username': 'admin', 'role': 'super_admin'},
                   'dead': {'expiry': time.time() - 1, 'username': 'admin', 'role': 'super_admin'},
                   'junk': {'username': 'admin'}}, f)     # 结构不合法：丢弃，而不是让整表加载失败
    monkeypatch.setattr(C, '_sessions', {})
    C.load_sessions()
    assert 'live' in C._sessions
    assert 'dead' not in C._sessions, '过期条目没被丢掉'
    assert 'junk' not in C._sessions, '坏条目没被丢掉（一个坏条目不该让整表加载失败）'
