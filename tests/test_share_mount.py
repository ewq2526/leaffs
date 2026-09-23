# -*- coding: utf-8 -*-
"""分享区的**本机路径映射**（只读引用，不复制文件）。

覆盖三层：
  1. 登记与列出：`publish_fs` / `list_mappings`（含 local / type 字段）；
  2. 解析：`resolve_rel` 认映射前缀、越界判据按**登记的根**、只读位为 True；
  3. 只读闸：删除 / 建目录 / 上传对映射区一律拒绝，且**系统里的真实文件必须还在** ——
     这条是本功能的安全底线，闸门漏一处就等于给出一个删系统文件的入口。

HTTP 层只测"拒绝"：`/api/share/mount` 要求会话由**本机一次性令牌**建立，而测试客户端
是口令登录的会话，正好是"远程管理员"那种情形。挂载成功后的下载链路需要服务端进程
重启才会重新读映射表，不在 HTTP 层测（那一层由本文件前两组直接驱动函数覆盖）。
"""

import json
import os

import pytest

from conftest import login


@pytest.fixture
def mount_env(data_root_factory, monkeypatch):
    """独立数据根 + 独立映射表文件；把各模块的 `UPLOAD_DIR` 一并指过去。

    ⚠️ 必须成组替换：`paths` / `utils.core` / `files.core` / `share.mappings` 各自在
    导入时绑定了自己的 `UPLOAD_DIR`，只换一个，别的仍指向真实 `E:\\lnas\\shared_files`；
    映射表文件同理（`_MAPPINGS_FILE` 默认落在真实 config 下，不换就写进真配置）。
    """
    root = data_root_factory('mount_')
    shared = os.path.join(root, 'shared_files')
    outside = os.path.join(root, 'outside')          # 共享根**之外** = "本机路径"
    cache = os.path.join(root, '.cache')
    os.makedirs(os.path.join(shared, 'public', 'shares'), exist_ok=True)
    os.makedirs(os.path.join(shared, 'users', 'admin'), exist_ok=True)
    os.makedirs(outside, exist_ok=True)
    os.makedirs(os.path.join(cache, 'folder_sizes'), exist_ok=True)
    os.makedirs(os.path.join(cache, 'thumbs'), exist_ok=True)

    import leaffs.files.core as fcore
    import leaffs.paths as paths
    import leaffs.share.mappings as m
    import leaffs.utils.core as uc

    monkeypatch.setattr(paths, 'UPLOAD_DIR', shared)
    monkeypatch.setattr(uc, 'UPLOAD_DIR', shared)
    monkeypatch.setattr(fcore, 'UPLOAD_DIR', shared)
    monkeypatch.setattr(m, 'UPLOAD_DIR', shared)
    monkeypatch.setattr(m, '_MAPPINGS_FILE', os.path.join(root, 'share_mappings.json'))
    monkeypatch.setattr(m, '_cache', None)
    # 缓存/缩略图索引也一起隔离：建目录、删文件都会去 invalidate 目录聚合缓存，
    # 不换就会写进仓库真实的 `.cache`（那些是可重建的，但没理由让跑测试去改它）
    monkeypatch.setattr(uc, 'CACHE_DIR', cache)
    monkeypatch.setattr(uc, 'THUMB_DIR', os.path.join(cache, 'thumbs'))
    monkeypatch.setattr(uc, 'FOLDER_SIZE_DIR', os.path.join(cache, 'folder_sizes'))
    return {'root': root, 'shared': shared, 'outside': outside, 'm': m, 'uc': uc, 'fcore': fcore}


def _mkfile(path, data=b'x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
    return path


# ---------- ① 登记与列出 ----------

def test_mount_directory_registers_readonly_folder(mount_env):
    """挂一个本机目录：登记成功、列表里是文件夹、标着"本机路径"、来源给的是真实路径"""
    m, outside = mount_env['m'], mount_env['outside']
    _mkfile(os.path.join(outside, '电影', 'a.mkv'), b'y' * 10)
    _mkfile(os.path.join(outside, '电影', 'sub', 'b.txt'), b'z' * 4)

    vp, err = m.publish_fs(os.path.join(outside, '电影'), '电影', 'admin')
    assert err is None, err
    assert vp == 'public/shares/admin/电影', vp

    items = m.list_mappings('admin')
    assert len(items) == 1
    it = items[0]
    assert it['type'] == 'folder', it
    assert it['local'] is True, it
    assert it['exists'] is True, it
    assert it['src'] == os.path.join(outside, '电影'), it


def test_mount_single_file_registers_file(mount_env):
    """单文件也支持：类型是 file，不是 folder"""
    m, outside = mount_env['m'], mount_env['outside']
    src = _mkfile(os.path.join(outside, '资料', 'a.pdf'), b'%PDF')

    vp, err = m.publish_fs(src, '', 'admin')
    assert err is None, err
    it = m.list_mappings('admin')[0]
    assert it['type'] == 'file', it
    assert it['name'] == 'a.pdf', it


def test_mount_rejects_shared_root_path(mount_env):
    """共享根内的路径不走这条路（那是普通分享的事），避免同一个区被挂两次"""
    m, shared = mount_env['m'], mount_env['shared']
    inner = _mkfile(os.path.join(shared, 'users', 'admin', 'x.txt'))
    vp, err = m.publish_fs(inner, 'x', 'admin')
    assert vp is None and err, (vp, err)


def test_mount_rejects_missing_and_relative(mount_env):
    m, outside = mount_env['m'], mount_env['outside']
    assert m.publish_fs(os.path.join(outside, 'nope'), 'n', 'admin')[0] is None
    assert m.publish_fs('relative/path', 'n', 'admin')[0] is None


# ---------- ② 解析与只读位 ----------

def test_mapped_file_resolves_to_real_path_and_is_readonly(mount_env):
    """映射目录**内部**的文件不在表里逐条登记，靠前缀 + 余部解析出来，且标记只读"""
    m, uc, outside = mount_env['m'], mount_env['uc'], mount_env['outside']
    real = _mkfile(os.path.join(outside, '电影', 'sub', 'b.txt'), b'z')
    m.publish_fs(os.path.join(outside, '电影'), '电影', 'admin')

    resolved = uc.resolve_rel('public/shares/admin/电影/sub/b.txt')
    assert resolved is not None
    # 余部是按正斜杠拼进去的，结果可能是混合分隔符 —— 与 `abs_path()` 的既有形态一致，
    # 下游（open / exists / walk）都吃得下，这里按 normpath 比
    assert os.path.normcase(os.path.normpath(resolved[0])) == \
        os.path.normcase(os.path.normpath(real)), resolved
    assert resolved[2] is True, '映射来源必须是只读的'
    assert os.path.normcase(resolved[1]) == os.path.normcase(os.path.join(outside, '电影'))


def test_mapped_path_cannot_escape_registered_root(mount_env):
    """沿着映射目录往外走必须被拦住（按登记的根判，不按请求字符串拼）"""
    m, uc, outside = mount_env['m'], mount_env['uc'], mount_env['outside']
    os.makedirs(os.path.join(outside, '电影'), exist_ok=True)
    vp, err = m.publish_fs(os.path.join(outside, '电影'), '电影', 'admin')
    assert err is None, err
    _mkfile(os.path.join(outside, 'secret.txt'), b'sec')

    assert uc.resolve_rel('public/shares/admin/电影/../secret.txt') is None
    assert uc.resolve_rel('public/shares/admin/电影/../../outside/secret.txt') is None


def test_nested_mappings_take_longest_prefix(mount_env):
    """嵌套映射以更具体的那条为准"""
    m, uc, outside = mount_env['m'], mount_env['uc'], mount_env['outside']
    _mkfile(os.path.join(outside, '媒体', '电影', 'a.mkv'), b'1')
    vp1, err1 = m.publish_fs(os.path.join(outside, '媒体'), 'media', 'admin')
    vp2, err2 = m.publish_fs(os.path.join(outside, '媒体', '电影'), 'movies', 'admin')
    assert (err1, err2) == (None, None), (err1, err2)

    hit = m.lookup_fs_prefix('public/shares/admin/movies/a.mkv')
    assert hit is not None and hit[0] == 'public/shares/admin/movies', hit
    resolved = uc.resolve_rel('public/shares/admin/movies/a.mkv')
    assert resolved[0] == os.path.join(outside, '媒体', '电影', 'a.mkv')
    assert resolved[2] is True


def test_shared_root_paths_stay_writable(mount_env):
    """对照：共享根里的普通路径照旧可写（只读位是映射独有的，不是所有路径都变只读）"""
    uc, shared = mount_env['uc'], mount_env['shared']
    resolved = uc.resolve_rel('users/admin/x.txt')
    assert resolved is not None
    assert resolved[2] is False, resolved


def test_old_records_without_fs_field_still_load(mount_env):
    """兼容：老映射表（只有 src）照旧能读 —— 新字段不能让已有分享失效"""
    m, shared, root = mount_env['m'], mount_env['shared'], mount_env['root']
    _mkfile(os.path.join(shared, 'users', 'admin', 'old.txt'), b'o')
    legacy = {'public/shares/admin/old.txt':
              {'src': 'users/admin/old.txt', 'by': 'admin', 'created': 1.0}}
    with open(os.path.join(root, 'share_mappings.json'), 'w', encoding='utf-8') as f:
        json.dump(legacy, f)

    # fixture 已把 `_cache` 置空，这里才第一次读盘 —— 读到的就是上面那份老格式
    items = m.list_mappings('admin')
    assert len(items) == 1 and items[0]['local'] is False, items
    resolved = mount_env['uc'].resolve_rel('public/shares/admin/old.txt')
    assert resolved is not None and resolved[2] is False, resolved


# ---------- ③ 只读闸 ----------

def _mount_movie(mount_env):
    outside = mount_env['outside']
    inner = _mkfile(os.path.join(outside, '电影', 'a.mkv'), b'film')
    vp, err = mount_env['m'].publish_fs(os.path.join(outside, '电影'), '电影', 'admin')
    assert err is None, err
    return inner


def test_delete_through_mapping_is_refused_and_file_survives(mount_env):
    """最要紧的一条：从分享区删映射目录里的文件，真实文件必须原封不动"""
    inner = _mount_movie(mount_env)
    fcore = mount_env['fcore']

    deleted, failed = fcore.delete_paths(['public/shares/admin/电影/a.mkv'])
    assert deleted == 0, (deleted, failed)
    assert failed and '只读' in failed[0][1], failed
    assert os.path.isfile(inner), '系统里的真实文件被删了 —— 只读闸漏了'


def test_delete_mapped_directory_itself_is_refused(mount_env):
    """删映射目录本身也一样拒（不能把整个挂载点删掉）"""
    _mount_movie(mount_env)
    deleted, failed = mount_env['fcore'].delete_paths(['public/shares/admin/电影'])
    assert deleted == 0, (deleted, failed)
    assert os.path.isdir(os.path.join(mount_env['outside'], '电影'))


def test_mkdir_inside_mapping_is_refused(mount_env):
    """在映射区里建目录必须拒，并且不能在系统目录里真的建出来"""
    _mount_movie(mount_env)
    ok, err = mount_env['fcore'].mkdir('public/shares/admin/电影', 'newdir')
    assert ok is False, (ok, err)
    assert err and '只读' in err, err
    assert not os.path.exists(os.path.join(mount_env['outside'], '电影', 'newdir'))


def test_mkdir_inside_shared_root_still_works(mount_env):
    """对照：共享根里的建目录不受影响"""
    ok, err = mount_env['fcore'].mkdir('users/admin', 'okdir')
    assert ok is True, (ok, err)
    assert os.path.isdir(os.path.join(mount_env['shared'], 'users', 'admin', 'okdir'))


# ---------- ④ HTTP：鉴权与入口可见性 ----------

def test_mount_api_requires_local_token_session(client):
    """远程（口令登录）的管理员不能挂本机路径 —— 这正是"只有服务端窗口"的意思"""
    login(client)
    r = client.post('/api/share/mount', json={'path': 'E:\\'})
    assert r.status_code in (403, 404), r.status_code
    assert r.status_code != 200


def test_share_list_reports_can_mount_false_for_remote_session(client):
    """入口可见性由服务端给（can_mount）：远程会话拿不到它"""
    login(client)
    r = client.get('/api/share')
    assert r.status_code == 200
    assert r.json().get('can_mount') is False, r.text[:200]


# ---------- ⑤ 访客分享页：进映射目录 ----------

def test_list_public_marks_folder_and_file(mount_env):
    """分享页数据要能区分目录与文件：目录给 path（进目录用），文件给 url（直链）"""
    m, outside = mount_env['m'], mount_env['outside']
    _mkfile(os.path.join(outside, '电影', 'a.mkv'), b'1')
    single = _mkfile(os.path.join(outside, 'one.pdf'), b'2')
    assert m.publish_fs(os.path.join(outside, '电影'), '电影', 'admin')[1] is None
    assert m.publish_fs(single, 'one.pdf', 'admin')[1] is None

    items = {it['name']: it for it in m.list_public('admin')}
    assert items['电影']['type'] == 'folder', items
    assert items['电影']['path'] == 'public/shares/admin/电影'
    assert 'url' not in items['电影'], '目录不该有下载直链'
    assert items['one.pdf']['type'] == 'file'
    assert items['one.pdf']['url'] == '/download/public/shares/admin/one.pdf'


def test_list_public_dir_lists_one_level(mount_env):
    """进目录后列一层：子目录还能继续点，文件带直链"""
    m, outside = mount_env['m'], mount_env['outside']
    _mkfile(os.path.join(outside, '电影', 'sub', 'b.txt'), b'b')
    _mkfile(os.path.join(outside, '电影', 'a.mkv'), b'a')
    assert m.publish_fs(os.path.join(outside, '电影'), '电影', 'admin')[1] is None

    rows = {it['name']: it for it in m.list_public_dir('admin', 'public/shares/admin/电影')}
    assert set(rows) == {'a.mkv', 'sub'}, rows
    assert rows['sub']['type'] == 'folder'
    assert rows['sub']['path'] == 'public/shares/admin/电影/sub'
    assert rows['a.mkv']['type'] == 'file'
    assert rows['a.mkv']['url'] == '/download/public/shares/admin/电影/a.mkv'
    assert rows['a.mkv']['size'] == 1


def test_list_public_dir_rejects_other_users_area(mount_env):
    """借别人的分享页列目录必须拒 —— 属主对不上就是空列表"""
    m, outside = mount_env['m'], mount_env['outside']
    _mkfile(os.path.join(outside, '电影', 'a.mkv'), b'a')
    assert m.publish_fs(os.path.join(outside, '电影'), '电影', 'admin')[1] is None
    assert m.list_public_dir('bob', 'public/shares/admin/电影') == []


def test_list_public_dir_rejects_non_mapping_and_escape(mount_env):
    """不是映射目录、或者想往外走 —— 一律空列表（不给"这儿有东西"的信号）"""
    m, outside = mount_env['m'], mount_env['outside']
    _mkfile(os.path.join(outside, '电影', 'a.mkv'), b'a')
    assert m.publish_fs(os.path.join(outside, '电影'), '电影', 'admin')[1] is None

    # 分享区本身不是映射目录：普通分享的条目是单文件，没有"目录内部"这回事
    assert m.list_public_dir('admin', 'public/shares/admin') == []
    # 映射里的普通文件也不是目录
    assert m.list_public_dir('admin', 'public/shares/admin/电影/a.mkv') == []
    # 往外走
    assert m.list_public_dir('admin', 'public/shares/admin/电影/../..') == []
    assert m.list_public_dir('admin', 'public/shares/admin/电影/../../outside') == []


# ---------- ⑥ 落点复核按"所属的根"判 ----------

def test_root_for_uses_registered_root_for_mapped_paths(mount_env):
    """缩略图与打包里各有一句"解析结果仍须落在根内"的复核。

    那些地方一律按共享根判的话，映射进来的目录会被整个判出去 —— 缩略图 403、
    打包跳过。判定必须按**这条路径所属的根**：映射路径 = 登记的真实目录，
    普通路径 = 共享根。
    """
    import leaffs.files.api as fapi

    m, outside, shared = mount_env['m'], mount_env['outside'], mount_env['shared']
    _mkfile(os.path.join(outside, '电影', 'a.mkv'), b'a')
    assert m.publish_fs(os.path.join(outside, '电影'), '电影', 'admin')[1] is None

    def norm(p):
        return os.path.normcase(os.path.normpath(p))

    # 映射路径 → 根是登记的真实目录（不是共享根，也不是它的父目录）
    root = fapi._root_for('public/shares/admin/电影/a.mkv', shared)
    assert norm(root) == norm(os.path.join(outside, '电影')), root
    # 对照：共享根内的普通路径，根就是共享根
    assert norm(fapi._root_for('users/admin/x.txt', shared)) == norm(shared)
    # 越界路径解析不出来 → 退回 base，于是后续那次 is_path_safe 必然失败（拒绝）
    assert norm(fapi._root_for('public/shares/admin/电影/../../x', shared)) == norm(shared)


# ---------- ⑦ 本机目录选择器 ----------

def test_browse_api_requires_local_token_session(client):
    """选择器读的是**服务器本机**的文件系统 —— 远程会话必须拿不到（与挂载同一档鉴权）"""
    login(client)
    r = client.get('/api/mount/browse')
    assert r.status_code in (403, 404), r.status_code
    r2 = client.get('/api/mount/browse', params={'path': 'C:\\'})
    assert r2.status_code in (403, 404), r2.status_code


def test_local_roots_shape():
    """根列表：Windows 给存在的盘符，其它平台给 `/`"""
    from leaffs.server.handler import _local_roots

    roots = _local_roots()
    assert roots, roots
    paths = [r['path'] for r in roots]
    if os.name == 'nt':
        assert all(p.endswith(':\\') for p in paths), paths
        assert any(p.upper().startswith('C') for p in paths), paths
    else:
        assert paths == ['/']
