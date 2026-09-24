# -*- coding: utf-8 -*-
"""目录聚合缓存的键必须归一化，失效才真正生效（N-9）。

黑盒报告 2026-09-21 的现象：`/api/files` 的 `total_file_count` / `total_size_sum`
一经写入就**永久冻结** —— 再传文件不更新（240 秒、三次上传都是旧值），管理员删号
后同名重建，新主人看到的是**上一任**的文件数与字节数。

根因不是"少加了失效调用"：上传完成后确实调了 `invalidate_folder_cache_smart()`。
根因是**写入键与删除键形态不一致**：

    写入（abs_path → os.path.join）  = '...\\users/zzw'   ← 混合分隔符，未归一化
    删除（invalidate → abspath）     = '...\\users\\zzw'   ← 归一化过

两者永不相等 ⇒ 任何失效调用都删不掉东西 ⇒ 磁盘持久化的旧值被永久命中。
`folder_size_ttl` 看着像兜底，实则形同虚设：磁盘命中那条路根本不看时间。

本文件钉「失效必须真的删掉条目」，并把"两种形态本来就不相等"作为**对照断言**写进来 ——
否则测试可能弱到"改回旧实现也照样通过"。
"""
import json
import os
import time

import pytest


@pytest.fixture()
def agg_env(monkeypatch, tmp_path):
    """上传根 / 缓存根 / 分片目录全部指到临时目录，并清空进程内缓存"""
    import leaffs.utils.core as UC
    upload = tmp_path / 'agg_upload'
    cache = tmp_path / 'agg_cache'
    shards = cache / 'folder_sizes'
    for d in (upload, shards):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(UC, 'UPLOAD_DIR', str(upload))
    monkeypatch.setattr(UC, 'CACHE_DIR', str(cache))
    monkeypatch.setattr(UC, 'FOLDER_SIZE_DIR', str(shards))
    monkeypatch.setattr(UC, '_folder_size_memory', {})
    # 失效会顺手广播"文件树变了"（绕到 server.push），与本文件无关
    monkeypatch.setattr(UC, '_notify_gallery_changed', lambda: None)
    return str(upload), str(cache), str(shards)


def _write(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'x' * size)


def test_mixed_and_plain_separators_are_the_same_key(agg_env):
    """混合分隔符与纯分隔符必须归一到同一个键（N-9 的根因面）"""
    import leaffs.utils.core as UC
    upload, _cache, _shards = agg_env
    mixed = os.path.join(upload, 'users/zzw')          # abs_path() 的产物
    plain = os.path.join(upload, 'users', 'zzw')       # 归化后的形态

    # 对照：这两种形态本来就不相等 —— 否则这条测试打不到 N-9
    assert mixed != plain, '对照失效：混合分隔符路径应当与归一化路径不同'

    assert UC._agg_key(mixed) == UC._agg_key(plain)
    assert UC._agg_key(plain) == UC._agg_key(UC._agg_key(plain)), '归一化必须幂等'


def test_invalidation_actually_removes_the_entry(agg_env):
    """N-9 本体：调用失效之后，聚合值必须反映真实磁盘内容"""
    import leaffs.utils.core as UC
    upload, _cache, _shards = agg_env
    home = os.path.join(upload, 'users', 'zzw')
    _write(os.path.join(home, 'a.txt'), 507)

    # list_files 那条链给出的正是 abs_path() 的混合分隔符形态
    looked_up = UC.abs_path('users/zzw')
    first = UC.get_folder_stats(looked_up)
    assert (first['files'], first['size']) == (1, 507), first

    # 上传完成点做的事：落盘后失效目标目录
    _write(os.path.join(home, 'b.txt'), 123)
    UC.invalidate_folder_cache(os.path.join(upload, 'users', 'zzw'))

    second = UC.get_folder_stats(looked_up)
    assert (second['files'], second['size']) == (2, 630), \
        '失效没生效，聚合值被冻结了（N-9）：%r' % (second,)


def test_recursive_invalidation_clears_the_subtree(agg_env):
    """删号重建场景：recursive 失效必须清掉整棵子树，否则新主人看到上一任的数字"""
    import shutil
    import leaffs.utils.core as UC
    upload, _cache, _shards = agg_env
    home = os.path.join(upload, 'users', 'zzv2')
    _write(os.path.join(home, 'old.txt'), 507)

    looked_up = UC.abs_path('users/zzv2')
    assert UC.get_folder_stats(looked_up)['size'] == 507

    # 删用户 = 家目录改名归档 + recursive 失效 users 整棵子树
    shutil.rmtree(home)

    UC.invalidate_folder_cache(os.path.join(upload, 'users'), recursive=True)

    fresh = UC.get_folder_stats(looked_up)
    assert (fresh['files'], fresh['size']) == (0, 0), \
        '同名重建后仍看到上一任的聚合值（N-9 跨账号残留）：%r' % (fresh,)


def _settle(UC, timeout=10):
    """等后台把目录大小补完。

    列表（`list_files`）现在**只查缓存、不算**：算大小是磁盘活，挂载点可能指向几十万
    文件的目录，同步算会把列表卡死。没算过的排给后台，用户刷新一次就有 ——
    测试要断真实数字，就在这里等一下。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if UC.folder_agg_queue_idle():
            return
        time.sleep(0.02)
    raise AssertionError('后台补算没排空（%ss）' % timeout)


def test_list_files_totals_follow_the_files(agg_env):
    """黑盒报告里冻住的就是这两个字段，直接钉在 list_files 上"""
    from leaffs.files import core as FC
    import leaffs.utils.core as UC
    upload, _cache, _shards = agg_env
    home = os.path.join(upload, 'users', 'zzw')
    _write(os.path.join(home, 'a.txt'), 507)

    # 第一次列：还没算过 → 统计先给 None（列表不等它），同时排给了后台
    got, err = FC.list_files('users/zzw')
    assert err is None and got['total_size_sum'] is None, got
    _settle(UC)
    got, err = FC.list_files('users/zzw')
    assert err is None and (got['total_file_count'], got['total_size_sum']) == (1, 507), got

    _write(os.path.join(home, 'b.txt'), 123)
    UC.invalidate_folder_cache(os.path.join(upload, 'users', 'zzw'))

    got, err = FC.list_files('users/zzw')
    assert err is None
    assert len(got['files']) == 2                 # 文件数组是磁盘实况，立刻就是对的
    assert got['total_size_sum'] is None, got     # 聚合值刚被失效，后台在补
    _settle(UC)
    got, err = FC.list_files('users/zzw')
    assert (got['total_file_count'], got['total_size_sum']) == (2, 630), \
        'files 数组对了但聚合值没跟上（N-9 的典型表现）：%r' % (got,)


def test_legacy_shard_keys_are_dropped_at_startup(agg_env):
    """旧键（未归一化）在启动迁移时必须被丢弃，否则永远命不中也删不掉"""
    import leaffs.utils.core as UC
    upload, _cache, shards = agg_env
    legacy_key = os.path.join(upload, 'users/zzw')       # 旧版本写下的形态
    shard = os.path.join(shards, 'users.json')
    with open(shard, 'w') as f:
        json.dump({legacy_key: {'size': 507, 'files': 1, 'folders': 0}}, f)

    UC._migrate_folder_db_keys()

    with open(shard, 'r') as f:
        assert json.load(f) == {}, '旧键没被清掉 —— 它会一直被当成有效缓存值'


def test_normalized_keys_survive_the_migration(agg_env):
    """迁移不许误伤新键（幂等）"""
    import leaffs.utils.core as UC
    upload, _cache, shards = agg_env
    key = UC._agg_key(os.path.join(upload, 'users', 'zzw'))
    shard = os.path.join(shards, 'users.json')
    with open(shard, 'w') as f:
        json.dump({key: {'size': 507, 'files': 1, 'folders': 0}}, f)

    UC._migrate_folder_db_keys()

    with open(shard, 'r') as f:
        assert json.load(f) == {key: {'size': 507, 'files': 1, 'folders': 0}}
