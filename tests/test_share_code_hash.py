# -*- coding: utf-8 -*-
"""分享码的存法与回显：**存明文**，迁移来的旧哈希仍能校验、并在通过时升级成明文。

来由：分享码原来按用户名存哈希（先是固定盐单轮 sha256，后来换成 PBKDF2 + 随机盐），
且粒度是"每个用户一个"。现在两样都变了 —— 码**按条目（随机标签）**走，并且**存明文**：
它本来就是"告诉别人"的东西（转发、抄纸上），不是秘密口令；而且要**能被本人读回**，
哈希存法读不回，等于设完就丢。只有本人与管理员能读（`read_code` 里判）。

于是这个文件钉三件事：
  1. 按标签比对：设码后对码通过、错码拒绝；
  2. **迁移来的旧哈希码**仍能通过 —— 两种老格式都要认（更旧的固定盐 sha256、
     上一版的 PBKDF2+随机盐）—— 并且**通过后顺手升级成明文**，否则那些码永远读不回来；
  3. 明文只给**本人与管理员**；访客与他人读到的永远是空。

单元级测试（直接把 `_ACCESS_FILE` 指到临时文件、重置进程内缓存），不需要起服务。
"""
import hashlib
import json
import os

import pytest

_LEGACY_SALT = 'leaffs-share:'          # 更旧那一版的固定盐（只读老数据用）
LABEL = 'share-label-1'


@pytest.fixture()
def sac(monkeypatch, data_root):
    """把 `share/access.py` 指到临时文件，并重置它的进程内缓存 `_CACHE`"""
    from leaffs.share import access as A
    p = os.path.join(data_root, 'share_code_probe', 'share_access.json')
    os.makedirs(os.path.dirname(p), exist_ok=True)
    monkeypatch.setattr(A, '_ACCESS_FILE', p)
    monkeypatch.setattr(A, '_CACHE', None)
    return A, p


def _item(p, label=LABEL):
    """盘上那条分享的记录（码、旧哈希、计数都在它下面）"""
    with open(p, encoding='utf-8') as f:
        return json.load(f)['items'][label]


def _legacy_sha256(code):
    """更旧那一版的算法：固定盐 + 单轮 sha256。

    这里独立写一遍而不是调 `access._legacy_code_hash` —— 要钉的是**盘上那个老格式**
    还能被认，用被测代码自己的算法算"旧值"就把这条断言架空了。
    """
    return hashlib.sha256((_LEGACY_SALT + code).encode('utf-8')).hexdigest()


def _seed_hash(A, stored_hash, owner='u1'):
    """塞一个"迁移来的哈希码"：明文为空、只有哈希（照迁移后的样子）"""
    with A._LOCK:
        A._load_locked()
        it = A._item(LABEL, owner)
        it['code'] = ''
        it['code_hash'] = stored_hash
        A._save_locked()
    return stored_hash


def test_plain_code_roundtrip(sac):
    """设码 → 对码通过、错码拒绝；清除后等于没设"""
    A, p = sac
    A.set_code(LABEL, 'ABCdef123456')
    assert A.code_enabled(LABEL) is True
    assert _item(p)['code'] == 'ABCdef123456', '码没有存成明文（明文才能被本人读回）'
    assert A.verify_code(LABEL, 'ABCdef123456') is True
    assert A.verify_code(LABEL, 'ABCdef123457') is False
    A.set_code(LABEL, '')
    assert A.code_enabled(LABEL) is False
    assert A.verify_code(LABEL, 'ABCdef123456') is False
    # 没有标签就没有"哪条分享"可设 —— 界面漏传路径时不能默默设到别处去
    assert A.set_code('', 'ABCdef123456')[0] is False


def test_verify_needs_both_label_and_code(sac):
    """空标签 / 空码一律不通过（空码不能被当成"放行"）"""
    A, _p = sac
    A.set_code(LABEL, 'ABCdef123456')
    assert A.verify_code('', 'ABCdef123456') is False
    assert A.verify_code(LABEL, '') is False
    assert A.verify_code('no-such-label', 'ABCdef123456') is False


def test_migrated_sha256_hash_still_verifies_and_gets_upgraded(sac):
    """更旧的固定盐 sha256 仍要能通过（否则老用户的分享码全废），并且通过后升级成明文"""
    A, p = sac
    _seed_hash(A, _legacy_sha256('right-code'))
    assert A.code_enabled(LABEL) is True
    # 升级前只有哈希：回显不出明文，只能告诉界面"重设一次才能看到"
    assert A.read_code(LABEL, 'u1', 'u1') == {'code': '', 'legacy': True, 'enabled': True}
    assert A.verify_code(LABEL, 'right-code') is True, \
        '旧格式的码不能通过 —— 那等于把所有已存在的分享码作废'
    it = _item(p)
    assert it['code'] == 'right-code', '通过后没有升级为明文：%r' % it
    assert it['code_hash'] == '', '升级成明文后还留着旧哈希'
    assert A.verify_code(LABEL, 'right-code') is True, '升级后必须仍能通过'
    assert A.verify_code(LABEL, 'wrong-code') is False


def test_migrated_pbkdf2_hash_still_verifies_and_gets_upgraded(sac):
    """上一版的格式（PBKDF2 + 随机盐，`iters$salt$hash`）同样要认，同样升级成明文"""
    A, p = sac
    _seed_hash(A, A.code_hash('right-code'))
    assert A.code_enabled(LABEL) is True
    assert A.verify_code(LABEL, 'right-code') is True
    it = _item(p)
    assert it['code'] == 'right-code', '通过后没有升级为明文：%r' % it
    assert it['code_hash'] == '', '升级成明文后还留着旧哈希'


def test_wrong_code_never_writes(sac):
    """输错码：拒绝，且**绝不触发任何写入**（错误码不该有写副作用）"""
    A, p = sac
    _seed_hash(A, _legacy_sha256('right-code'))
    with open(p, encoding='utf-8') as f:
        before = f.read()
    assert A.verify_code(LABEL, 'wrong-code') is False
    with open(p, encoding='utf-8') as f:
        assert f.read() == before, '输错码竟然写盘了（应该只在通过时升级）'


def test_read_code_only_for_owner_and_admin(sac):
    """明文只给本人与管理员；访客与他人读到的永远是空（连"有没有设码"都不给）"""
    A, _p = sac
    A.set_code(LABEL, 'plain-code', owner='u1')
    assert A.read_code(LABEL, 'u1', 'u1') == \
        {'code': 'plain-code', 'legacy': False, 'enabled': True}
    assert A.read_code(LABEL, 'root', 'u1', is_admin=True)['code'] == 'plain-code'
    assert A.read_code(LABEL, '', 'u1') == \
        {'code': '', 'legacy': False, 'enabled': False}, '访客读到了码'
    assert A.read_code(LABEL, 'u2', 'u1') == \
        {'code': '', 'legacy': False, 'enabled': False}, '他人读到了码'
    # 没有标签（不属于任何登记条目）也读不出东西
    assert A.read_code('', 'u1', 'u1', is_admin=True) == \
        {'code': '', 'legacy': False, 'enabled': False}
