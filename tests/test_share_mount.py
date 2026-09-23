# -*- coding: utf-8 -*-
"""服务器挂载（`mounts/`）—— 本机路径映射：只登记引用、不复制文件、只读。

两种来源、两种逻辑，这是本文件的基线：
  * 普通分享（`src`）：源在共享根内、挂在 `public/shares/<用户名>/` 下、**按用户分**、
    有分享码，大小走正常那套（有缓存）；
  * 服务器挂载（`fs`）：源是**服务器本机的绝对路径**、挂在 `mounts/<名字>` 下、
    **不分用户**（它是这台服务器的东西，不属于任何账号）、**没有分享码这一层** ——
    访问权限与公共目录一个级别；条目名就是源路径自己的名字，不起别名。

本文件只覆盖挂载那一边：登记与列出、前缀解析与只读位、越界、只读闸（系统里的真实文件
必须还在）、根层与 `mounts` 层如何合并进列表、大小口径，以及"只有服务端本机才能挂"。

HTTP 层只测**拒绝**：`/api/share/mount` 要求会话由本机一次性令牌建立，而测试客户端是
口令登录的会话 —— 正好是"远程管理员"那种情形。
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
    缓存/缩略图索引也一起隔离：建目录、删文件都会去 invalidate 目录聚合缓存，
    不换就会写进仓库真实的 `.cache`。
    """
    root = data_root_factory('mount_')
    shared = os.path.join(root, 'shared_files')
    outside = os.path.join(root, 'outside')          # 共享根**之外** = "本机路径"
    cache = os.path.join(root, '.cache')
    os.makedirs(os.path.join(shared, 'public', 'shares'), exist_ok=True)
    os.makedirs(os.path.join(shared, 'users', 'admin'), exist_ok=True)
    os.makedirs(os.path.join(shared, 'mounts'), exist_ok=True)
    os.makedirs(outside, exist_ok=True)
    os.makedirs(os.path.join(cache, 'folder_sizes'), exist_ok=True)
    os.makedirs(os.path.join(cache, 'thumbs'), exist_ok=True)

    import leaffs.files.core as fcore
    import leaffs.paths as paths
    import leaffs.share.access as sacc
    import leaffs.share.mappings as m
    import leaffs.utils.core as uc

    monkeypatch.setattr(paths, 'UPLOAD_DIR', shared)
    monkeypatch.setattr(uc, 'UPLOAD_DIR', shared)
    monkeypatch.setattr(fcore, 'UPLOAD_DIR', shared)
    monkeypatch.setattr(m, 'UPLOAD_DIR', shared)
    monkeypatch.setattr(m, '_MAPPINGS_FILE', os.path.join(root, 'share_mappings.json'))
    monkeypatch.setattr(m, '_cache', None)
    # 分享码的存储也要隔离：默认落在真实 config 下，不换就写进真配置
    monkeypatch.setattr(sacc, '_ACCESS_FILE', os.path.join(root, 'share_access.json'))
    monkeypatch.setattr(sacc, '_CACHE', None)
    monkeypatch.setattr(uc, 'CACHE_DIR', cache)
    monkeypatch.setattr(uc, 'THUMB_DIR', os.path.join(cache, 'thumbs'))
    monkeypatch.setattr(uc, 'FOLDER_SIZE_DIR', os.path.join(cache, 'folder_sizes'))
    return {'root': root, 'shared': shared, 'outside': outside, 'm': m, 'uc': uc, 'fcore': fcore}


def _mkfile(path, data=b'x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
    return path


def _mount_movie(mount_env):
    """挂一个本机目录 `outside/电影`，返回里面那个文件的真实路径"""
    outside = mount_env['outside']
    inner = _mkfile(os.path.join(outside, '电影', 'a.mkv'), b'film')
    vp, err = mount_env['m'].publish_fs(os.path.join(outside, '电影'), 'admin')
    assert err is None, err
    assert vp == 'mounts/电影', vp
    return inner


# ---------- ① 登记与列出 ----------

def test_mount_directory_registers_under_mounts(mount_env):
    """挂本机目录：挂在 mounts/ 下、名字取自源目录、标记为 local"""
    m, outside = mount_env['m'], mount_env['outside']
    _mkfile(os.path.join(outside, '电影', 'a.mkv'), b'y' * 10)

    vp, err = m.publish_fs(os.path.join(outside, '电影'), 'admin')
    assert err is None, err
    assert vp == 'mounts/电影', vp

    items = m.list_mappings('admin')
    assert len(items) == 1
    it = items[0]
    assert it['local'] is True, it
    assert it['exists'] is True, it
    assert it['src'] == os.path.join(outside, '电影'), it


def test_mount_single_file_keeps_its_own_name(mount_env):
    """单文件也支持：条目名就是文件名"""
    m, outside = mount_env['m'], mount_env['outside']
    _mkfile(os.path.join(outside, '资料', 'a.pdf'), b'%PDF')
    vp, err = m.publish_fs(os.path.join(outside, '资料', 'a.pdf'), 'admin')
    assert err is None, err
    assert vp == 'mounts/a.pdf', vp


def test_mount_same_name_is_rejected_not_renamed(mount_env):
    """同名撞车**直接拒**：名字必须与实物一一对应，自动改名会让名字和指向对不上"""
    m, outside = mount_env['m'], mount_env['outside']
    os.makedirs(os.path.join(outside, 'd1', '电影'), exist_ok=True)
    os.makedirs(os.path.join(outside, 'd2', '电影'), exist_ok=True)
    assert m.publish_fs(os.path.join(outside, 'd1', '电影'), 'admin')[1] is None
    vp, err = m.publish_fs(os.path.join(outside, 'd2', '电影'), 'admin')
    assert vp is None and err, (vp, err)
    assert len(m.list_mappings('admin')) == 1


def test_mount_rejects_shared_root_and_relative(mount_env):
    """共享根内的东西走普通分享那条路；相对路径没有意义"""
    m, shared, outside = mount_env['m'], mount_env['shared'], mount_env['outside']
    inner = _mkfile(os.path.join(shared, 'users', 'admin', 'x.txt'))
    assert m.publish_fs(inner, 'admin')[0] is None
    assert m.publish_fs('relative/path', 'admin')[0] is None
    assert m.publish_fs(os.path.join(outside, 'nope'), 'admin')[0] is None


# ---------- ② 解析与只读位 ----------

def test_mapped_file_resolves_to_real_path_and_is_readonly(mount_env):
    """挂载目录**内部**的文件不在表里逐条登记，靠前缀 + 余部解析出来，且标记只读"""
    m, uc, outside = mount_env['m'], mount_env['uc'], mount_env['outside']
    real = _mkfile(os.path.join(outside, '电影', 'sub', 'b.txt'), b'z')
    assert m.publish_fs(os.path.join(outside, '电影'), 'admin')[1] is None

    resolved = uc.resolve_rel('mounts/电影/sub/b.txt')
    assert resolved is not None
    # 余部按正斜杠拼进去，结果可能是混合分隔符 —— 与 abs_path() 的既有形态一致
    assert os.path.normcase(os.path.normpath(resolved[0])) == \
        os.path.normcase(os.path.normpath(real)), resolved
    assert resolved[2] is True, '挂载来源必须是只读的'
    assert os.path.normcase(resolved[1]) == os.path.normcase(os.path.join(outside, '电影'))


def test_mapped_path_cannot_escape_registered_root(mount_env):
    """沿着挂载目录往外走必须被拦住（按登记的根判，不按请求字符串拼）"""
    m, uc, outside = mount_env['m'], mount_env['uc'], mount_env['outside']
    os.makedirs(os.path.join(outside, '电影'), exist_ok=True)
    assert m.publish_fs(os.path.join(outside, '电影'), 'admin')[1] is None
    _mkfile(os.path.join(outside, 'secret.txt'), b'sec')

    assert uc.resolve_rel('mounts/电影/../secret.txt') is None
    assert uc.resolve_rel('mounts/电影/../../outside/secret.txt') is None


def test_nested_mounts_take_longest_prefix(mount_env):
    """嵌套挂载以更具体的那条为准"""
    m, uc, outside = mount_env['m'], mount_env['uc'], mount_env['outside']
    _mkfile(os.path.join(outside, '媒体', '电影', 'a.mkv'), b'1')
    assert m.publish_fs(os.path.join(outside, '媒体'), 'admin')[1] is None
    assert m.publish_fs(os.path.join(outside, '媒体', '电影'), 'admin')[1] is None

    hit = m.lookup_fs_prefix('mounts/电影/a.mkv')
    assert hit is not None and hit[0] == 'mounts/电影', hit
    resolved = uc.resolve_rel('mounts/电影/a.mkv')
    assert resolved[0] == os.path.join(outside, '媒体', '电影', 'a.mkv')
    assert resolved[2] is True


def test_shared_root_paths_stay_writable(mount_env):
    """对照：共享根里的普通路径照旧可写（只读位是挂载来源独有的）"""
    resolved = mount_env['uc'].resolve_rel('users/admin/x.txt')
    assert resolved is not None
    assert resolved[2] is False, resolved


def test_old_records_without_fs_field_still_load(mount_env):
    """兼容：老映射表（普通分享，只有 src）照旧能读 —— 新字段不能让已有分享失效"""
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

def test_delete_through_mount_is_refused_and_file_survives(mount_env):
    """最要紧的一条：从挂载区删文件，系统里的真实文件必须原封不动"""
    inner = _mount_movie(mount_env)
    fcore = mount_env['fcore']

    deleted, failed = fcore.delete_paths(['mounts/电影/a.mkv'])
    assert deleted == 0, (deleted, failed)
    assert failed and '只读' in failed[0][1], failed
    assert os.path.isfile(inner), '系统里的真实文件被删了 —— 只读闸漏了'


def test_delete_mounted_directory_itself_is_refused(mount_env):
    """删挂载点本身也一样拒"""
    _mount_movie(mount_env)
    deleted, failed = mount_env['fcore'].delete_paths(['mounts/电影'])
    assert deleted == 0, (deleted, failed)
    assert os.path.isdir(os.path.join(mount_env['outside'], '电影'))


def test_mkdir_inside_mount_is_refused(mount_env):
    """在挂载区里建目录必须拒，并且不能在系统目录里真的建出来"""
    _mount_movie(mount_env)
    ok, err = mount_env['fcore'].mkdir('mounts/电影', 'newdir')
    assert ok is False, (ok, err)
    assert err and '只读' in err, err
    assert not os.path.exists(os.path.join(mount_env['outside'], '电影', 'newdir'))


def test_mkdir_inside_shared_root_still_works(mount_env):
    """对照：共享根里的建目录不受影响"""
    ok, err = mount_env['fcore'].mkdir('users/admin', 'okdir')
    assert ok is True, (ok, err)
    assert os.path.isdir(os.path.join(mount_env['shared'], 'users', 'admin', 'okdir'))


# ---------- ④ 列表合并与大小口径 ----------

def test_root_list_shows_mounts_folder_without_size(mount_env):
    """根层多出一个与 public/users 并列的 mounts 目录；**size 报 0**（不参与统计）"""
    _mount_movie(mount_env)
    m = mount_env['m']
    res = {'files': [], 'total_file_count': 0, 'total_size_sum': 0}
    assert m.merge_into_list('', res) is True, res
    row = [f for f in res['files'] if f['name'] == 'mounts'][0]
    assert row['type'] == 'folder' and row['local'] is True, row
    assert row['size'] == 0, row
    assert res['total_size_sum'] == 0 and res['total_file_count'] == 0, res


def test_mounts_level_lists_each_mount(mount_env):
    """mounts 层：每条挂载一个条目，名字就是源目录名；目录不给下载直链"""
    _mount_movie(mount_env)
    m = mount_env['m']
    res = {'files': [], 'total_file_count': 0, 'total_size_sum': 0}
    assert m.merge_into_list('mounts', res) is True, res
    row = [f for f in res['files'] if f['name'] == '电影'][0]
    assert row['type'] == 'folder' and row['local'] is True, row
    assert 'dl' not in row, row
    assert row['size'] == 0, row                     # 目录：不报大小


def test_share_area_no_longer_contributes_public_size(mount_env):
    """`public` 下那个 shares 目录同样报 0：分享区全是虚拟条目，不参与公共目录统计"""
    m, shared = mount_env['m'], mount_env['shared']
    _mkfile(os.path.join(shared, 'users', 'admin', 'big.bin'), b'x' * 9000)
    assert m.publish('users/admin/big.bin', 'admin')[1] is None

    res = {'files': [], 'total_file_count': 0, 'total_size_sum': 0}
    assert m.merge_into_list('public', res) is True, res
    row = [f for f in res['files'] if f['name'] == 'shares'][0]
    assert row['size'] == 0, row


def test_src_share_still_reports_its_file_size(mount_env):
    """对照：普通分享（src）走正常逻辑 —— 它本来就是共享根里的文件，照常报大小"""
    m, shared = mount_env['m'], mount_env['shared']
    _mkfile(os.path.join(shared, 'users', 'admin', 'f.bin'), b'x' * 1234)
    assert m.publish('users/admin/f.bin', 'admin')[1] is None

    res = {'files': [], 'total_file_count': 0, 'total_size_sum': 0}
    assert m.merge_into_list('public/shares/admin', res) is True, res
    row = [f for f in res['files'] if f['name'] == 'f.bin'][0]
    assert row['size'] == 1234, row
    assert res['total_size_sum'] == 1234, res


def test_mount_inside_list_shows_real_sizes(mount_env):
    """进了挂载目录之后：条目按真实大小报（那一层走服务端既有的目录统计）"""
    m, fcore, outside = mount_env['m'], mount_env['fcore'], mount_env['outside']
    _mkfile(os.path.join(outside, '电影', '系列', 'a.mkv'), b'x' * 4000)
    assert m.publish_fs(os.path.join(outside, '电影'), 'admin')[1] is None

    cur, err = fcore.list_files('mounts/电影')
    assert err is None, err
    sub = [f for f in cur['files'] if f['name'] == '系列'][0]
    assert sub['size'] == 4000, sub
    assert cur['total_size_sum'] == 4000, cur


def test_locked_mount_shows_placeholder_not_name(mount_env):
    """设了码又没解锁的挂载点：列表里给"需要访问码"占位 —— **不给名字**，
    只带上随机标签（不可枚举、不含名字），访客对着它输码。解锁后就正常显示。
    """
    import leaffs.share.access as sacc

    m = mount_env['m']
    _mount_movie(mount_env)
    label, owner = m.access_scope('mounts/电影')
    assert label, '登记条目时应当生成随机标签'
    sacc.set_code(label, 'code123456', owner)

    res = {'files': [], 'total_file_count': 0, 'total_size_sum': 0}
    assert m.merge_into_list('mounts', res, unlocked=lambda vp: False) is True, res
    row = res['files'][0]
    assert row['type'] == 'locked', row
    assert row['label'] == label, row
    assert row['name'] == '需要访问码', row
    assert row['path'] == '', row

    res2 = {'files': [], 'total_file_count': 0, 'total_size_sum': 0}
    m.merge_into_list('mounts', res2, unlocked=lambda vp: True)
    row2 = [f for f in res2['files'] if f['name'] == '电影'][0]
    assert row2['type'] == 'folder', row2


# ---------- ⑤ 落点复核按"所属的根"判 ----------

def test_root_for_uses_registered_root_for_mounted_paths(mount_env):
    """缩略图与打包里各有一句"解析结果仍须落在根内"的复核。

    一律按共享根判的话，挂载进来的目录会被整个判出去 —— 缩略图 403、打包跳过。
    """
    import leaffs.files.api as fapi

    m, outside, shared = mount_env['m'], mount_env['outside'], mount_env['shared']
    _mkfile(os.path.join(outside, '电影', 'a.mkv'), b'a')
    assert m.publish_fs(os.path.join(outside, '电影'), 'admin')[1] is None

    def norm(p):
        return os.path.normcase(os.path.normpath(p))

    root = fapi._root_for('mounts/电影/a.mkv', shared)
    assert norm(root) == norm(os.path.join(outside, '电影')), root
    assert norm(fapi._root_for('users/admin/x.txt', shared)) == norm(shared)
    assert norm(fapi._root_for('mounts/电影/../../x', shared)) == norm(shared)


# ---------- ⑥ 权限：与公共目录一个级别 ----------

def test_mounts_visibility_matches_public(mount_env):
    """挂载区与公共目录一个级别：能看 public 的就能看 mounts，也一并受 guest_mode 约束"""
    from leaffs.files.core import check_path_permission_core as chk

    # 管理员：全通
    assert chk('admin', 'admin', 'mounts') is True
    # 普通用户：public 能看，mounts 一样能看
    assert chk('user', 'bob', 'mounts') is True
    assert chk('user', 'bob', 'mounts/电影') is True
    # 游客：guest_mode 开着能看，关掉一律拒（防绕过 UI 直接调 API）
    assert chk('guest', '', 'mounts', guest_mode=True) is True
    assert chk('guest', '', 'mounts', guest_mode=False) is False
    # 对照：public 的口径完全一致
    assert chk('guest', '', 'public', guest_mode=True) is True
    assert chk('guest', '', 'public', guest_mode=False) is False


# ---------- ⑦ HTTP：鉴权与入口可见性 ----------

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
