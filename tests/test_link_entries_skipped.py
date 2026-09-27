# -*- coding: utf-8 -*-
"""链接条目（symlink / Windows junction）一律不列、不算。

**来由（2026-09-27 活服务实测）**：挂载源里放一个 junction 指向根外目录，匿名访客在
`/api/files` 里**看到了它**（被当目录列出来），而且它的大小栏报的是**链接目标的真实字节数**
（实测 24543，与 `C:\\Windows\\System32\\drivers\\etc` 的真实值完全一致）—— 也就是
"目录大小"跟着链接**走出了共享根**，把根外目录的体积白给了访客。

**根因不是"忘了 `follow_symlinks=False`"**：Windows 上 junction 带 DIRECTORY 属性，实测

    entry.is_dir(follow_symlinks=False) → True      ← 所以照样被当目录
    entry.is_symlink() / os.path.islink() → False    ← 所以这些判据挡不住
    os.walk(followlinks=False) → 照样走进去

唯一认得出来的是重解析点属性（`st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT`）；
这个判定收在 `utils/core.is_link_entry()` **一处**，列表与目录聚合都调它。

Windows 上 `os.symlink` 需要开发者模式（普通账号建不出来，实测 WinError 1314），
所以这里用 `mklink /J` 造 junction；其它平台走 `os.symlink` —— 两边钉的是同一件事。
"""
import os
import subprocess

import pytest

WINDOWS = os.name == 'nt'


def _make_link(link_path, target_path):
    if WINDOWS:
        r = subprocess.run(['cmd', '/c', 'mklink', '/J', link_path, target_path],
                           capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip('建不出 junction：%s' % (r.stdout or r.stderr))
    else:
        os.symlink(target_path, link_path, target_is_directory=True)


@pytest.fixture()
def env(monkeypatch, data_root_factory):
    """共享根指到临时目录：里面 `public/` 有一个真文件 + 一个指向**共享根外**的链接"""
    import leaffs.files.core as FC
    import leaffs.utils.core as UC

    root = data_root_factory('link_')
    shared = os.path.join(root, 'shared_files')
    cache = os.path.join(root, 'cache')
    shards = os.path.join(cache, 'folder_sizes')
    for d in (shared, shards):
        os.makedirs(d, exist_ok=True)
    # 共享根**外**的目标目录：塞 4096 字节，"跟出去"才有可观测的大小
    target = os.path.join(root, 'outside', 'target')
    os.makedirs(os.path.join(target, 'inner'), exist_ok=True)
    with open(os.path.join(target, 'inner', 'big.bin'), 'wb') as f:
        f.write(b'x' * 4096)
    # 共享根里：public/keep.txt + public/link_out → 根外目标
    pub = os.path.join(shared, 'public')
    os.makedirs(pub, exist_ok=True)
    with open(os.path.join(pub, 'keep.txt'), 'wb') as f:
        f.write(b'y' * 10)
    _make_link(os.path.join(pub, 'link_out'), target)

    monkeypatch.setattr(UC, 'UPLOAD_DIR', shared)
    monkeypatch.setattr(FC, 'UPLOAD_DIR', shared)
    monkeypatch.setattr(UC, 'CACHE_DIR', cache)
    monkeypatch.setattr(UC, 'FOLDER_SIZE_DIR', shards)
    monkeypatch.setattr(UC, '_folder_size_memory', {})
    # 失效会顺手广播"文件树变了"（绕到 server.push），与本文件无关
    monkeypatch.setattr(UC, '_notify_gallery_changed', lambda: None)
    return shared, pub, target


def test_link_entry_is_recognised(env):
    """判据本身：链接必须被 `is_link_entry` 认出来 —— 否则下面两条都是假绿"""
    import leaffs.utils.core as UC
    _shared, pub, _target = env
    with os.scandir(pub) as it:
        got = {e.name: UC.is_link_entry(e) for e in it}
    assert got.get('link_out') is True, got
    assert got.get('keep.txt') is False, got


def test_list_files_does_not_list_link_entries(env):
    """列表不列链接条目：它指向共享根外，内容访问本来就被 realpath 挡住，
    列出来只会把根外的名字与体积露出去"""
    import leaffs.files.core as FC
    _shared, _pub, _target = env
    data, err = FC.list_files('public')
    assert err is None, err
    names = sorted(f['name'] for f in data['files'])
    assert names == ['keep.txt'], names


def test_folder_size_does_not_follow_link_entries(env):
    """目录大小不跟着链接走出共享根

    对照那一条**必须有**：根外目标自己确实有 4096 字节，否则"没跟出去"可能只是
    因为目标本来就是空的，断言等于没牙。
    """
    import leaffs.utils.core as UC
    _shared, pub, target = env
    assert UC.get_folder_size(target) >= 4096, '对照失效：根外目标应当是空的'
    agg = UC.get_folder_stats(pub)
    assert agg['size'] == 10, agg      # 只算 keep.txt，不含链接目标的 4096
    assert agg['files'] == 1, agg


def test_search_does_not_walk_out_through_link_entries(env):
    """搜索不跟着链接走出共享根

    `os.walk` 的 `followlinks=False` **挡不住 junction**（实测）——原来它把共享根**外**
    文件的名字与大小列进了搜索结果，任何登录用户都能搜到。剪枝收在 `prune_walk_dirs`，
    搜索（`files/core` 与 `files/api` 两条实现）、打包、清缩略图共用同一处判据。
    """
    import leaffs.files.core as FC
    _shared, _pub, _target = env
    assert FC.search_files('big.bin') == [], '搜索结果里出现了共享根外的文件'
    # 对照：共享根里自己的文件照样搜得到（否则"搜不到"可能只是搜索坏了）
    assert [r['name'] for r in FC.search_files('keep.txt')] == ['keep.txt']
