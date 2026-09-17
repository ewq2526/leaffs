# -*- coding: utf-8 -*-
"""分享码哈希：随机盐 + PBKDF2，旧值无感迁移（N-4）。

**问题**（外部测试者定位）：`code_hash` 原来是
`sha256('leaffs-share:' + code)` —— **固定盐、单轮、无随机盐**。同一个码设两次哈希完全相同，
而分享码下限只有 6 位 → 一旦拿到 `share_access.json`（备份泄露 / 本地接触 / 任意文件读取原语），
在线侧那些限流与锁定**全部归零**。

**修法**：换成与登录口令同一套（`auth/core.py` 的 `PBKDF2_ITERATIONS` + 32 字节随机盐，
格式 `iterations$salt_b64$hash_b64`），并且**旧格式仍能校验通过、通过后顺手升级** ——
否则所有已存在的分享码会一次性失效。

单元级测试（直接把 `_ACCESS_FILE` 指到临时文件、重置进程内缓存），不需要起服务。
"""
import hashlib
import json
import os

import pytest

_LEGACY_SALT = 'leaffs-share:'


@pytest.fixture()
def sac(monkeypatch, data_root):
    """把 `share/access.py` 指到临时文件，并重置它的进程内缓存 `_CACHE`"""
    from leaffs.share import access as A
    p = os.path.join(data_root, 'share_code_probe', 'share_access.json')
    os.makedirs(os.path.dirname(p), exist_ok=True)
    monkeypatch.setattr(A, '_ACCESS_FILE', p)
    monkeypatch.setattr(A, '_CACHE', None)
    return A, p


def _stored(p):
    with open(p, encoding='utf-8') as f:
        return json.load(f)['users']['u1']['code_hash']


def _write_legacy(p, code):
    legacy = hashlib.sha256((_LEGACY_SALT + code).encode('utf-8')).hexdigest()
    with open(p, 'w', encoding='utf-8') as f:
        json.dump({'date': '', 'users': {'u1': {'code_hash': legacy}}, 'tickets': {}}, f)
    return legacy


def test_same_code_gets_different_hash(sac):
    """**随机盐的证据**：同一个码设两次，盘上哈希必须不同

    修之前是固定盐 → 两次**完全相同**（这条是"离线一击可破"的直接证据）。
    """
    A, p = sac
    A.set_code('u1', 'ABCdef123456')
    h1 = _stored(p)
    A.set_code('u1', 'ABCdef123456')
    h2 = _stored(p)
    assert h1 != h2, '同一个码两次哈希相同 → 还是固定盐（同一张彩虹表可通杀所有用户）'


def test_new_hash_uses_pbkdf2_with_password_params(sac):
    """新设的码必须是 PBKDF2 自描述格式，且迭代次数与登录口令同口径"""
    from leaffs.auth.core import PBKDF2_ITERATIONS
    A, p = sac
    A.set_code('u1', 'ABCdef123456')
    stored = _stored(p)
    assert '$' in stored, '还是旧格式（64 位 hex / 固定盐 / 单轮 sha256）：%r' % stored
    parts = stored.split('$')
    assert len(parts) == 3, stored
    assert parts[0] == str(PBKDF2_ITERATIONS), \
        '迭代次数与口令不一致（%s vs %s）' % (parts[0], PBKDF2_ITERATIONS)
    assert len(parts[1]) >= 16, '盐太短：%r' % parts[1]
    assert stored != hashlib.sha256((_LEGACY_SALT + 'ABCdef123456').encode()).hexdigest()


def test_legacy_hash_still_verifies_and_gets_upgraded(sac):
    """旧格式仍要能通过（否则老用户的分享码全废），并且**通过后顺手升级**"""
    A, p = sac
    _write_legacy(p, 'right-code')
    A._CACHE = None
    assert A.verify_code('u1', 'right-code') is True, \
        '旧格式的码不能通过 —— 那等于把所有已存在的分享码作废'
    assert '$' in _stored(p), '通过后没有升级为新格式：%r' % _stored(p)
    assert A.verify_code('u1', 'right-code') is True, '升级后必须仍能通过'
    assert A.verify_code('u1', 'wrong-code') is False


def test_wrong_code_never_writes(sac):
    """输错码：拒绝，且**绝不触发任何写入**（错误码不该有写副作用）"""
    A, p = sac
    _write_legacy(p, 'right-code')
    A._CACHE = None
    with open(p, encoding='utf-8') as f:
        before = f.read()
    assert A.verify_code('u1', 'wrong-code') is False
    with open(p, encoding='utf-8') as f:
        assert f.read() == before, '输错码竟然写盘了（应该只在通过时升级）'


def test_new_format_roundtrip(sac):
    """新格式自身的往返：设码 → 对码通过、错码拒绝"""
    A, p = sac
    A.set_code('u1', 'ABCdef123456')
    assert A.verify_code('u1', 'ABCdef123456') is True
    assert A.verify_code('u1', 'ABCdef123457') is False
    assert A.code_enabled('u1') is True
    A.set_code('u1', '')
    assert A.code_enabled('u1') is False
