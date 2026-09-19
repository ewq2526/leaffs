# -*- coding: utf-8 -*-
"""LF-32：自助"退出其它设备"的撤销必须落盘 —— 否则被踢的会话会在服务重启后复活。

原先 `leaffs/auth/self_api.py` 的 `account_revoke_sessions` 手抄了一份与
`auth/core.py::_revoke_user_sessions` **逐字同构**的删除循环，却漏了落盘那一步：
内存里的会话是删掉了，但记录还留在 `sessions.json` 里，`load_sessions()` 下次启动
会把它读回内存并重新生效 —— "踢下线"只对当前进程有效。

修法是把撤销收成一份：`account_revoke_sessions` 改调 `_revoke_user_sessions`
（它本来就落盘），并给它加了返回撤销条数。

本文件钉住三件事：
  1. 踢完之后**盘上**已经没有被踢的 sid（核心那条，在未修代码上会红）；
  2. 回给前端的 `revoked` 计数正确（不能为了收口把语义丢掉）；
  3. 踢失败时**不谎报成功**（原来那段 `except Exception: pass` 会回 success: true）。

会话表用 monkeypatch 隔离到临时文件，测试结束原样还原，不碰真实 config/。
"""
import json
import os
import sys
import time

import pytest

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJ_ROOT not in sys.path:
    sys.path.insert(0, PROJ_ROOT)

import leaffs.auth.core as C
import leaffs.auth.self_api as S

COOKIE = 'wifi_session'          # auth/core.py 的 AUTH_COOKIE


class _FakeHandler:
    """只实现 account_revoke_sessions 用到的三个接口（headers / client_address / send_json）。"""

    def __init__(self, sid):
        self.headers = {'Cookie': '%s=%s' % (COOKIE, sid)}
        self.client_address = ('127.0.0.1', 54321)
        self.sent = None

    def send_json(self, data, status=200):
        self.sent = (data, status)


@pytest.fixture()
def sessions(data_root_factory, monkeypatch):
    """把会话表隔离到临时文件 + 空的进程内表；结束时原样还原。"""
    root = data_root_factory('lf32_')
    monkeypatch.setattr(C, 'SESSIONS_FILE', os.path.join(root, 'sessions.json'))
    saved = dict(C._sessions)
    with C._sessions_lock:
        C._sessions.clear()
    yield root
    with C._sessions_lock:
        C._sessions.clear()
        C._sessions.update(saved)


def _seed(pairs):
    """往会话表塞入 (sid, username) 并落盘 —— 模拟"确实已经写进 sessions.json"。"""
    exp = time.time() + 3600
    with C._sessions_lock:
        for sid, user in pairs:
            C._sessions[sid] = {'expiry': exp, 'username': user,
                                'role': 'user', 'ip': '127.0.0.1'}
        C._save_sessions_locked()


def _on_disk():
    with open(C.SESSIONS_FILE, encoding='utf-8') as f:
        return json.load(f)


def test_revoked_session_is_gone_from_disk(sessions):
    """核心：踢完盘上就不能再有它 —— 未修代码上这里会红。"""
    _seed([('sid_keep', 'u1'), ('sid_kick', 'u1')])
    assert 'sid_kick' in _on_disk(), '前置条件：两个会话都应已落盘'

    h = _FakeHandler('sid_keep')
    S.account_revoke_sessions(h)

    assert h.sent == ({'success': True, 'revoked': 1}, 200)
    assert 'sid_kick' not in C._sessions, '被踢的会话还在内存表里'

    disk = _on_disk()
    assert 'sid_keep' in disk, '当前设备的会话被误删'
    assert 'sid_kick' not in disk, \
        '被踢的会话仍留在 sessions.json 里 —— 服务重启后 load_sessions() 会让它复活'


def test_other_users_sessions_untouched(sessions):
    """踢只针对本账号：别人的会话一个都不能动（防"踢太狠"）。"""
    _seed([('sid_keep', 'u1'), ('sid_kick', 'u1'), ('sid_other', 'u2')])

    S.account_revoke_sessions(_FakeHandler('sid_keep'))

    disk = _on_disk()
    assert 'sid_other' in disk and 'sid_other' in C._sessions
    assert 'sid_kick' not in disk


def test_revoke_failure_is_not_reported_as_success(sessions, monkeypatch):
    """踢失败必须如实报错 —— 原来 `except Exception: pass` 会回 success: true。

    未修代码上这条也会红：它根本不调 `_revoke_user_sessions`，
    打桩的异常不会被触发，于是照样回一个 success。
    """
    _seed([('sid_keep', 'u1')])

    def _boom(username, keep_sid=''):
        raise RuntimeError('模拟撤销/落盘失败')

    monkeypatch.setattr(C, '_revoke_user_sessions', _boom)

    h = _FakeHandler('sid_keep')
    S.account_revoke_sessions(h)

    assert h.sent is not None, '失败时没有任何响应'
    data, status = h.sent
    assert data.get('success') is False, '踢失败了却报成功'
    assert status == 500


def test_guest_cannot_revoke(sessions):
    """游客没有可管理的会话（这条行为不变，改收口时别顺手改掉）。"""
    _seed([('sid_guest', '游客'), ('sid_other', 'u2')])

    h = _FakeHandler('sid_guest')
    S.account_revoke_sessions(h)

    data, status = h.sent
    assert data.get('success') is False and status == 403
    assert 'sid_other' in _on_disk(), '游客被拒时不应误伤别人的会话'
