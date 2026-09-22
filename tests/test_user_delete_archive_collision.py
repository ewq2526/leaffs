# -*- coding: utf-8 -*-
"""LF-30：同一秒内删两次同名用户 → 归档目录名撞车，整个删账号操作失败。

**问题**：`_archive_user_dir` 把归档目录名定成 `<用户名>-<秒级时间戳>`，而
`os.rename` 到**已存在目录**在 Windows 上必然 `FileExistsError`（WinError 183，
空目录、非空目录都一样）。于是「删 → 同名重建 → 再删」只要落在**同一秒**里，
第二次就返回「用户目录归档失败（账号未删除，可重试）」——
**账号没删、分享码与映射也没清**（清理排在归档之后），而文案说的"可重试"
在同一秒内**必然**再失败。

**怎么发现的**：2026-09-17 全量测试偶发 2 failed —— `test_user_delete_cascade.py:100`
的收尾删除失败 ⇒ 账号残留 ⇒ 下一个用例建同名账号拿 `Username exists` 400。
两个失败是同一条链。重跑 241 passed，是偶发。

**为什么这里是单元级**：触发条件就是"同一秒"，端到端只能靠运气撞秒，会变成
偶发假绿。所以直接把时钟钉住 —— 但测的仍然是**真实函数**与**真实 `os.rename`**，
不是替身。
"""
import os

import pytest


@pytest.fixture()
def archive_env(data_root_factory, monkeypatch):
    """UPLOAD_DIR 指到临时数据根；`time.strftime` 钉死在某一秒上。

    ⚠️ `_archive_user_dir` 里是函数内 `from leaffs.paths import UPLOAD_DIR`，
    取值发生在调用时，所以 patch 模块属性有效。
    """
    root = data_root_factory('arc_')
    import leaffs.paths as uc
    monkeypatch.setattr(uc, 'UPLOAD_DIR', os.path.join(root, 'shared_files'))
    import leaffs.auth.users_api as ua

    fixed = '20260101-000000'
    monkeypatch.setattr(ua.time, 'strftime', lambda fmt, t: fixed)
    return uc, ua, fixed


def _make_home(uc, name, payload):
    """造一个用户家目录，里面放一个有内容的文件（用来验证档案没丢）"""
    d = os.path.join(uc.UPLOAD_DIR, 'users', name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'f.txt'), 'w', encoding='utf-8') as f:
        f.write(payload)
    return d


def test_same_second_recreate_then_delete_still_succeeds(archive_env):
    """★ 核心回归：时钟没走时删第二次，必须照样成功，且两次各落一个目录

    未修代码在这里返回 `(None, '用户目录归档失败（账号未删除，可重试）')`。
    """
    uc, ua, fixed = archive_env

    _make_home(uc, 'u1', 'first')
    r1 = ua._archive_user_dir('u1')
    assert r1 == ('users/.deleted/u1-%s' % fixed, None), r1

    # 同名重建后再删 —— 时钟停在原地，归档名会撞上第一个
    _make_home(uc, 'u1', 'second')
    r2 = ua._archive_user_dir('u1')
    assert r2[1] is None, '同一秒内的第二次归档失败了：%r' % (r2,)
    assert r2[0] != r1[0], '两次归档落到了同一个目录：%r' % (r2,)

    deleted = os.path.join(uc.UPLOAD_DIR, 'users', '.deleted')
    dirs = sorted(os.listdir(deleted))
    assert len(dirs) == 2, dirs

    # 两份档案都还在（归档是"改名保命"，不能因为撞名丢一份）
    got = set()
    for d in dirs:
        with open(os.path.join(deleted, d, 'f.txt'), encoding='utf-8') as f:
            got.add(f.read())
    assert got == {'first', 'second'}, got


def test_different_second_keeps_plain_name(archive_env, monkeypatch):
    """对照：跨秒时不加序号 —— 正常路径的归档名保持原样（可读性不回退）"""
    uc, ua, fixed = archive_env
    stamps = iter(['20260101-000000', '20260101-000001'])
    monkeypatch.setattr(ua.time, 'strftime', lambda fmt, t: next(stamps))

    _make_home(uc, 'u1', 'first')
    r1 = ua._archive_user_dir('u1')
    _make_home(uc, 'u1', 'second')
    r2 = ua._archive_user_dir('u1')

    assert r1 == ('users/.deleted/u1-20260101-000000', None), r1
    assert r2 == ('users/.deleted/u1-20260101-000001', None), r2
