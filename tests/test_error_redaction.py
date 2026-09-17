# -*- coding: utf-8 -*-
"""兜底异常**不得回显给客户端**：响应里只能有固定文案，细节进日志。

背景（外部黑盒报告的 LF-02，已逐条核实）：原先 9 处兜底是
`handler.send_json({'error': str(e)}, 500)` —— 而 Windows 的 OSError 文本自带完整绝对路径，
报告里那句 `[WinError 267] ... 'E:\\test\\shared_files\\public\\新建 文本文档.txt'`
就是这么来的：服务器目录布局等于免费送出去。

还有一条专门制造这种异常的路在底层：`files/core.py` 的 `os.scandir` 原先只接
`PermissionError`，递一个**文件**路径进去抛的是 `NotADirectoryError`（消息里带路径），
它会一路逃到 HTTP 兜底被回显。

本文件锁住两件事：**响应不泄露**，以及**日志里有细节**（不然就是"没泄露但也没线索"）。
"""
import os

from conftest import login

# 响应体里绝不该出现的东西
_FORBIDDEN = ('WinError', 'OSError', 'NotADirectoryError', 'TypeError', 'AttributeError',
              'Errno', '\\\\', ':/', 'Traceback', 'iterable')


def _assert_no_leak(r, where, expect_status=None, expect_text=None):
    body = r.text
    if expect_status is not None:
        assert r.status_code == expect_status, '%s: 期望 %s，实际 %s %s' % (
            where, expect_status, r.status_code, body)
    for bad in _FORBIDDEN:
        assert bad not in body, '%s: 响应里漏了 %r —— %s' % (where, bad, body)
    if expect_text is not None:
        assert r.json().get('error') == expect_text, '%s: %s' % (where, body)


def test_listing_a_file_path_does_not_leak_absolute_path(client, data_root):
    """把一个**文件**路径交给 /api/files：底层抛 NotADirectoryError，响应里只能有固定文案"""
    login(client)
    r = client.post('/api/upload?path=public', files={'file': ('leakprobe.txt', b'x')})
    assert r.status_code == 200, r.text
    assert os.path.isfile(os.path.join(data_root, 'shared_files', 'public', 'leakprobe.txt'))

    r = client.get('/api/files', params={'path': 'public/leakprobe.txt'})
    _assert_no_leak(r, '文件路径当目录列', expect_status=404, expect_text='无法读取该目录')


def test_internal_error_fallback_hides_details(client):
    """走 500 兜底时也只回场景化固定文案（`{"files": 123}` 会让 for 循环抛 TypeError）"""
    login(client)
    r = client.post('/api/delete', json={'files': 123})
    _assert_no_leak(r, '删除兜底', expect_status=500, expect_text='删除失败')


def test_details_are_recorded_in_the_runtime_log(client, data_root):
    """不回显 ≠ 不记录：细节必须能在运行日志（leaffs.log）里查到，否则排障就瞎了"""
    login(client)
    client.post('/api/delete', json={'files': 123})
    log = os.path.join(data_root, 'leaffs.log')
    assert os.path.isfile(log), '运行日志没生成'
    with open(log, encoding='utf-8', errors='replace') as f:
        text = f.read()
    assert '删除 失败' in text or '删除失败' in text, \
        '兜底异常的细节没进运行日志：\n' + text[-600:]
    assert 'TypeError' in text, '日志里应当带上异常类型，实际：\n' + text[-600:]
