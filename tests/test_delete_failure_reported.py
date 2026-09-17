# -*- coding: utf-8 -*-
"""删除失败的如实上报（LF-23）。

原来的问题（用户实测，电脑端 admin）：
    文件正在被写入 / 被占用时点删除 → 前端弹**「删除成功」**，但文件还在；
    「只有等上传彻底完成之后」再删才真的删掉。

根因：`files/api.py` 结尾是 `resp = {'success': True, 'deleted': deleted}` —— **success 恒为 true**，
而前端只看 `d.success`。真实原因塞在 `failed[].error` 里，**从前端到后端没有任何一行代码读它**。
（WS 那条路径的 `success = n_deleted > 0` 是对的，两条口径原本还不一致。）

⚠️ 两个容易改坏 / 容易糊弄的地方，都写成了断言：
1. **幂等语义**：删一个不存在的文件**仍然是成功**（`deleted=0, failed=[]`）——
   判据必须是 **failed 非空**，不是 `deleted > 0`；
2. **提示必须精确**：原来那句 `'文件被占用或权限不足'` 把两种完全不同的原因混在一起，
   用户既判断不了也不知道该做什么。这里直接用 mmap 造一个真实的"被占用"，
   断言原因里**有「占用」且没有「权限」**。
"""
import mmap
import os

from conftest import login


def _make_file(data_root, name, size=100):
    p = os.path.join(data_root, 'shared_files', name)
    with open(p, 'wb') as f:
        f.write(b'x' * size)
    return p


def test_locked_file_delete_reports_failure_with_precise_reason(client, data_root):
    """被占用的文件：如实报失败，且原因精确到"占用"（with mmap 造的锁跨进程有效）"""
    login(client)
    p = _make_file(data_root, 'lf23locked.bin')
    with open(p, 'r+b') as f:
        mm = mmap.mmap(f.fileno(), 0)          # 文件映射锁住：服务端是另一个进程，照样删不掉
        try:
            r = client.post('/api/delete', json={'files': ['lf23locked.bin']})
            assert r.status_code == 200, r.text
            d = r.json()

            assert d.get('success') is False, \
                '删不掉却回了 success=true —— 前端会弹「删除成功」：%s' % d
            assert d.get('deleted') == 0, d

            failed = d.get('failed') or []
            assert failed, '没给出任何失败原因：%s' % d
            err = failed[0].get('error') or ''
            assert '占用' in err, '原因没说明是「被占用」：%r' % err
            assert '权限' not in err, \
                '把「占用」与「权限不足」两种不同原因混成一句了（提示必须精确）：%r' % err

            assert os.path.exists(p), '文件居然被删掉了？'
        finally:
            mm.close()

    # 放开占用之后必须能删掉 —— 证明这次改动没有把删除本身改坏
    r = client.post('/api/delete', json={'files': ['lf23locked.bin']})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get('success') is True, '占用解除后应当删除成功：%s' % d
    assert not os.path.exists(p), '文件还在'


def test_delete_missing_file_stays_idempotent(client):
    """删一个不存在的文件仍然是**成功**（幂等设计）—— 改 success 语义时最容易改坏这条"""
    login(client)
    r = client.post('/api/delete', json={'files': ['lf23-definitely-absent.bin']})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get('success') is True, '幂等语义被改坏了（删不存在的应当算成功）：%s' % d
    assert d.get('deleted') == 0, d
    assert not d.get('failed'), '不该报失败原因：%s' % d


def test_partial_delete_reports_both_sides(client, data_root):
    """部分成功：两边都要如实说 —— 成功几个、哪一个失败、为什么"""
    login(client)
    ok = _make_file(data_root, 'lf23ok.bin')
    locked = _make_file(data_root, 'lf23locked2.bin')
    with open(locked, 'r+b') as f:
        mm = mmap.mmap(f.fileno(), 0)
        try:
            r = client.post('/api/delete', json={'files': ['lf23ok.bin', 'lf23locked2.bin']})
            assert r.status_code == 200, r.text
            d = r.json()
            assert d.get('success') is False, '有一个删不掉，不该整体报成功：%s' % d
            assert d.get('deleted') == 1, '成功数要如实报：%s' % d
            failed = d.get('failed') or []
            assert len(failed) == 1, '失败项要逐条列出：%s' % d
            assert failed[0].get('path') == 'lf23locked2.bin', failed
            assert '占用' in (failed[0].get('error') or ''), failed
            assert not os.path.exists(ok), '成功的那个应当已被删掉'
            assert os.path.exists(locked), '失败的那个应当还在'
        finally:
            mm.close()
    os.remove(locked)
