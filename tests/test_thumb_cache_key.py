# -*- coding: utf-8 -*-
"""缩略图缓存键必须无碰撞（N-1）。

黑盒报告 2026-09-21 实测：两个账号共用了一个缩略图文件，一方拿到另一方的私有图片 ——
而直读受害者原图仍是 404，**权限判定本身是对的**。出问题的是取用缓存那一步：

    鉴权按「路径」做，缓存内容按「路径扁平化字符串」取 —— 检查的对象和取用的对象不是同一个。

`.cache/thumbs/index.json` 里的原始证据：

    "E:\\test\\shared_files\\users/zzv/p_s.png"    -> "users_zzv_p_s.png.jpg"
    "E:\\test\\shared_files\\users/zzv_p/s.png"    -> "users_zzv_p_s.png.jpg"   ← 同一个文件

旧落盘名把路径分隔符换成下划线，是**有损**变换。本文件钉三件事：
不同源路径必须是不同缓存槽位、同一源路径的不同写法必须收敛到同一槽位、
旧格式条目要能迁移干净（且不许删到缩略图目录外面）。

旧算法作为**对照**写进测试里 —— 否则测试可能弱到"改回旧实现也照样通过"。
"""
import json
import os

import pytest


@pytest.fixture()
def thumb_env(monkeypatch, data_root):
    """缩略图目录与上传根都指到临时数据根（绝不碰仓库 .cache 与真实 UPLOAD_DIR）"""
    import leaffs.utils.core as UC
    upload = os.path.join(data_root, 'thumb_key_files')
    thumbs = os.path.join(data_root, 'thumb_key_thumbs')
    os.makedirs(upload, exist_ok=True)
    os.makedirs(thumbs, exist_ok=True)
    monkeypatch.setattr(UC, 'UPLOAD_DIR', upload)
    monkeypatch.setattr(UC, 'THUMB_DIR', thumbs)
    # 缩略图目录统计失效那条链会写到真实 CACHE_DIR，这里与本文件无关，掐掉
    monkeypatch.setattr(UC, '_invalidate_thumbs_throttled', lambda: None)
    return upload, thumbs


def _legacy_thumb_name(file_path, upload_dir):
    """旧实现（有损）：相对路径把分隔符换成下划线。**仅作对照**，不参与生产代码。"""
    rel = os.path.relpath(file_path, upload_dir).replace('\\', '_').replace('/', '_').replace(':', '_')
    return rel + '.jpg'


def test_colliding_paths_get_separate_cache_slots(thumb_env):
    """两个会撞名的路径必须是两个缓存槽位（N-1 本体）"""
    import leaffs.utils.core as UC
    upload, _thumbs = thumb_env
    victim = os.path.join(upload, 'users', 'zzv', 'p_s.png')
    attacker = os.path.join(upload, 'users', 'zzv_p', 's.png')

    # 对照：旧算法确实把两者压成同一个名字 —— 复现不出碰撞就说明这条回归测不到 N-1
    assert _legacy_thumb_name(victim, upload) == _legacy_thumb_name(attacker, upload), \
        '对照失效：这两个路径本来就该撞名，测试没打在 N-1 上'

    assert UC._thumb_path(victim) != UC._thumb_path(attacker), \
        '两个不同源文件仍指向同一个缩略图槽位 —— 必然有一方显示另一方的图'


def test_same_file_written_differently_shares_one_slot(thumb_env):
    """同一文件的混合分隔符写法必须收敛到同一槽位（否则白占空间、还删不干净）"""
    import leaffs.utils.core as UC
    upload, _thumbs = thumb_env
    plain = os.path.join(upload, 'users', 'zzv', 'p_s.png')
    mixed = os.path.join(upload, 'users/zzv', 'p_s.png')
    assert UC._thumb_path(plain) == UC._thumb_path(mixed)


def test_index_value_matches_disk_name(thumb_env):
    """index.json 里记的名字必须就是落盘名，否则清理时找不到文件"""
    import leaffs.utils.core as UC
    upload, thumbs = thumb_env
    src = os.path.join(upload, 'users', 'zzv', 'p_s.png')
    os.makedirs(os.path.dirname(src), exist_ok=True)
    with open(src, 'wb') as f:
        f.write(b'x')

    UC._thumb_index_add(src)

    with open(os.path.join(thumbs, 'index.json'), 'r') as f:
        index = json.load(f)
    assert index.get(src) == UC._thumb_name(src)
    assert os.path.join(thumbs, index[src]) == UC._thumb_path(src)


def test_legacy_thumb_entries_are_migrated(thumb_env):
    """旧格式条目要连文件一起清掉 —— 源文件还在，靠采样式清理是清不掉的"""
    import leaffs.utils.core as UC
    upload, thumbs = thumb_env
    src = os.path.join(upload, 'users', 'zzv', 'p_s.png')
    os.makedirs(os.path.dirname(src), exist_ok=True)
    with open(src, 'wb') as f:
        f.write(b'x')

    legacy = _legacy_thumb_name(src, upload)
    legacy_file = os.path.join(thumbs, legacy)
    with open(legacy_file, 'wb') as f:
        f.write(b'old thumb')
    idx_path = os.path.join(thumbs, 'index.json')
    with open(idx_path, 'w') as f:
        json.dump({src: legacy}, f)

    UC.cleanup_orphan_thumbs()

    assert not os.path.exists(legacy_file), '旧格式缩略图文件没被清掉，会永久占着磁盘'
    assert not os.path.exists(idx_path), '索引里只剩被迁移掉的条目，文件本身应当删除'


def test_new_format_entries_survive_migration(thumb_env):
    """迁移不许误伤新格式（幂等：跑过一次之后 stale 恒为空）"""
    import leaffs.utils.core as UC
    upload, thumbs = thumb_env
    src = os.path.join(upload, 'users', 'zzv', 'p_s.png')
    os.makedirs(os.path.dirname(src), exist_ok=True)
    with open(src, 'wb') as f:
        f.write(b'x')

    keep = UC._thumb_name(src)
    keep_file = os.path.join(thumbs, keep)
    with open(keep_file, 'wb') as f:
        f.write(b'thumb')
    idx_path = os.path.join(thumbs, 'index.json')
    with open(idx_path, 'w') as f:
        json.dump({src: keep}, f)

    UC.cleanup_orphan_thumbs()

    assert os.path.exists(keep_file), '新格式缩略图被误删了'
    with open(idx_path, 'r') as f:
        assert json.load(f).get(src) == keep


def test_migration_never_deletes_outside_thumb_dir(thumb_env):
    """索引是可写本地文件：带分隔符的值不能把删除动作引到缩略图目录外"""
    import leaffs.utils.core as UC
    upload, thumbs = thumb_env
    src = os.path.join(upload, 'users', 'zzv', 'p_s.png')
    os.makedirs(os.path.dirname(src), exist_ok=True)
    with open(src, 'wb') as f:
        f.write(b'x')

    outside = os.path.join(os.path.dirname(thumbs), 'not_a_thumb.jpg')
    with open(outside, 'wb') as f:
        f.write(b'do not delete me')
    with open(os.path.join(thumbs, 'index.json'), 'w') as f:
        json.dump({src: '../not_a_thumb.jpg'}, f)

    UC.cleanup_orphan_thumbs()

    assert os.path.exists(outside), '越界删除：缩略图目录外的文件被删了'
