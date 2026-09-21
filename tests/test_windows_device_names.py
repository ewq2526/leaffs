# -*- coding: utf-8 -*-
"""Win32 保留设备名不许当文件路径（N-3）。

黑盒报告 N-3（2026-09-21）：

    os.path.exists(r'...\\NUL') = True      ← Win32 设备语义
    /api/thumb?path=NUL  → admin 得到 500，非管理员正确 404
    /api/raw?path=NUL    → 0 字节

也就是说 `NUL` 通过了"文件是否存在"的检查，一路进到缩略图流水线才抛异常。
管理员拿到 500 是因为他通过得了权限检查，非管理员在权限那一步就被拦下 —— 所以
同一个输入两种状态码。**没有泄露、没有穿越、没有状态改变**，实质影响为零，
但它是个不该存在的错误码不一致，也是一类输入没被挡住。

修法：在路径规范化的入口（两份 `_normalize_rel_path`，HTTP 侧与 files 层各一份）
统一拒绝保留设备名，判定做成共享 helper `has_windows_device_name()` ——
**两份实现本来完全一样**，不该再长出第三份。

判定取点号之前那一段：Windows 把 `NUL.txt` 同样当设备；尾随空格与点也会被吃掉。
"""
import os

import pytest

from conftest import login

# 命中：保留设备名（含带扩展名、大小写、尾随空格/点、以及夹在任意路径段里）
REJECTED = (
    'NUL', 'nul', 'NUL.txt', 'NUL.png', 'con', 'CON', 'CON.txt', 'aux', 'AUX',
    'PRN', 'prn.log', 'COM1', 'com9', 'LPT1', 'lpt9', 'NUL ',
    'users/admin/NUL', 'public/CON/x.txt', 'a/b/COM1.dat',
)
# 放行：看着像、其实不是保留名
ALLOWED = (
    '', 'public', 'users/admin/a.txt',
    'NULL.txt',        # 双 L，不是 NUL
    'CONSOLE', 'console.txt', 'auxiliary.txt',
    'COM10', 'COM0', 'LPT10',
    'my.nul',          # 设备名必须在点号**之前**那一段
    'nul_backup.txt',  # 不是"点号前正好等于 NUL"
)


@pytest.mark.parametrize('p', REJECTED)
def test_reserved_names_are_rejected(p):
    from leaffs.utils.core import has_windows_device_name

    assert has_windows_device_name(p) is True, '没认出保留设备名：%r' % p


@pytest.mark.parametrize('p', ALLOWED)
def test_lookalikes_are_not_rejected(p):
    from leaffs.utils.core import has_windows_device_name

    assert has_windows_device_name(p) is False, '误伤了正常名字：%r' % p


def test_both_normalizers_agree():
    """两份 _normalize_rel_path 必须口径一致（它们本来就是同一份代码抄了两遍）"""
    from leaffs.files import api as FA
    from leaffs.files import core as FC

    for p in ('NUL', 'NUL.txt', 'users/admin/NUL', 'public/CON/x.txt'):
        assert FA._normalize_rel_path(p) is None, 'HTTP 侧没挡住：%r' % p
        assert FC._normalize_rel_path(p) is None, 'files 层没挡住：%r' % p
    for p in ('public', 'users/admin/a.txt', 'NULL.txt', 'COM10'):
        assert FA._normalize_rel_path(p) == FC._normalize_rel_path(p) == p, p


@pytest.mark.skipif(os.name != 'nt', reason='Win32 设备语义只在 Windows 上存在')
def test_the_probe_really_hits_win32_device_semantics():
    """对照：NUL 在 Win32 下确实"存在"，这条测试才算打在 N-3 上"""
    import leaffs.utils.core as UC

    assert os.path.exists(os.path.join(UC.UPLOAD_DIR, 'NUL')), \
        '对照失效：Win32 上 os.path.exists(...\\NUL) 本应为 True（这正是 N-3 的入口）'


def test_thumb_and_raw_no_longer_blow_up_on_device_names(client):
    """端到端：同一个输入不许再出现"管理员 500 / 非管理员 404"的分叉"""
    login(client)

    for path in ('NUL', 'NUL.txt', 'CON', 'AUX', 'COM1'):
        r = client.get('/api/thumb', params={'path': path})
        assert r.status_code != 500, \
            '缩略图对 %r 仍然 500（N-3）：%s' % (path, r.text[:200])

    r = client.get('/api/raw', params={'path': 'NUL'})
    assert r.status_code != 200, \
        'NUL 仍被当成可读文件（修前是 200 + 0 字节）：%s' % r.status_code
