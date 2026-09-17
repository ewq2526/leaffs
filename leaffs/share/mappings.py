# -*- coding: utf-8 -*-
"""分享映射（虚拟映射到公共目录，文件本体不搬动）。

模型（v3 确认）：
  * 分享 = 把「自己权限内的文件」登记为虚拟条目，显示在公共目录
    public/shares/<用户名>/ 下 —— 磁盘上不产生任何副本/链接，源文件原位不动；
  * 访客：浏览 LeafFS 公共目录（public → shares/<用户名>）即可看到并下载这些文件；
    每个文件另有单独下载直链（/download/<虚拟路径>，匿名可下，guest_mode 开启时）；
  * 用户（分享者）：/share 管理页列出自己的映射、可复制直链、可移除；
  * 越权防线：列表合并与下载解析都只认映射表登记项（虚拟路径 ↔ 源相对路径精确对应），
    不接受任意路径输入；用户只能映射自己权限内（users/<我>/** 与 public/**）的文件、
    只能移除自己创建的映射（admin 全量）。

虚拟路径规则：public/shares/<用户名>/<文件名>；虚拟文件名自动避让（与磁盘已存在
条目、已登记映射同名时追加 " (n)"），保证磁盘+虚拟两层视图无重名。
"""
import json
import os
import threading
import time

from leaffs.paths import UPLOAD_DIR, CONFIG_DIR
from leaffs.runtime_log import add_log

_MAPPINGS_FILE = os.path.join(CONFIG_DIR, 'share_mappings.json')
_SHARE_ROOT_REL = 'public/shares'
_MAX_TOTAL = 500          # 全量映射上限（防失控增长）
_MAX_USER = 100           # 单用户上限

_lock = threading.Lock()
_cache = None             # {virtual_rel: {'src': src_rel, 'by': username, 'created': float}}


def _load_locked():
    global _cache
    if _cache is not None:
        return
    _cache = {}
    try:
        with open(_MAPPINGS_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for vp, item in raw.items():
                if not isinstance(vp, str) or not isinstance(item, dict):
                    continue
                src = item.get('src')
                by = item.get('by')
                if isinstance(src, str) and src and isinstance(by, str):
                    _cache[vp] = {'src': src, 'by': by,
                                  'created': float(item.get('created') or 0)}
    except FileNotFoundError:
        pass
    except Exception as e:
        add_log('分享映射加载失败: %s' % e, 'warn')


def _save_locked():
    try:
        tmp = _MAPPINGS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_cache, f, ensure_ascii=False)
        os.replace(tmp, _MAPPINGS_FILE)
    except Exception as e:
        add_log('分享映射保存失败: %s' % e, 'err')


def _user_dir_rel(username):
    from leaffs.files.core import sanitize_entry_name
    safe = sanitize_entry_name(username)
    return _SHARE_ROOT_REL + '/' + (safe or 'user')


def _virtual_name_available(username, name):
    """虚拟文件名是否可用：磁盘 public/shares/<u>/ 无同名，且未登记同名映射"""
    d = os.path.join(UPLOAD_DIR, _user_dir_rel(username))
    try:
        existing = set(os.listdir(d)) if os.path.isdir(d) else set()
    except Exception:
        existing = set()
    mapped = set()
    pref = _user_dir_rel(username) + '/'
    for vp in _cache:
        if vp.startswith(pref):
            mapped.add(vp.rsplit('/', 1)[-1])
    return name not in existing and name not in mapped


def _unique_name(username, base_name):
    from leaffs.files.core import sanitize_entry_name
    safe = sanitize_entry_name(base_name)
    if not safe:
        return None
    if _virtual_name_available(username, safe):
        return safe
    stem, dot, ext = safe.rpartition('.')
    if not stem:
        stem, ext = safe, ''
    n = 1
    while True:
        cand = '%s (%d)%s' % (stem, n, dot + ext) if dot else '%s (%d)' % (stem, n)
        if _virtual_name_available(username, cand):
            return cand
        n += 1


def publish(src_rel, username):
    """登记一个映射：src_rel 为共享根内相对路径（须存在且为文件）。

    返回 (virtual_rel, None) 成功 / (None, errmsg) 失败。
    """
    from leaffs.files.core import sanitize_entry_name
    if not username or not isinstance(src_rel, str) or not src_rel.strip():
        return None, '参数缺失'
    if sanitize_entry_name(username) is None:
        return None, '用户名包含非法字符'
    norm = src_rel.replace('\\', '/').strip('/')
    if norm == _SHARE_ROOT_REL or norm.startswith(_SHARE_ROOT_REL + '/'):
        return None, '不能映射公共分享区内的路径'
    # 必须落在共享根内：norm 是调用方给的字符串，光靠 os.path.join 不够 ——
    # 盘符路径（C:/...）在 Windows 上会让 join 直接返回它本身，等于把根外文件挂成分享
    from leaffs.utils.core import abs_path
    full = abs_path(norm)
    if not full:
        return None, '路径不合法'
    try:
        if not os.path.isfile(full):
            return None, '源文件不存在'
    except Exception:
        return None, '源文件不可读'
    vdir = _user_dir_rel(username)
    with _lock:
        _load_locked()
        if len(_cache) >= _MAX_TOTAL:
            return None, '分享映射数量已达上限'
        mine = sum(1 for vp in _cache if _cache[vp].get('by') == username)
        if mine >= _MAX_USER:
            return None, '你的分享映射数量已达上限（%d）' % _MAX_USER
        name = _unique_name(username, os.path.basename(norm))
        if name is None:
            return None, '文件名非法'
        vp = vdir + '/' + name
        _cache[vp] = {'src': norm, 'by': username, 'created': time.time()}
        _save_locked()
    add_log('分享映射已添加: %s → %s' % (norm, vp), 'info')
    return vp, None


def remove(virtual_rel, username, is_admin=False):
    """移除映射。仅创建者本人或 admin。返回 (ok, errmsg)。"""
    if not isinstance(virtual_rel, str):
        return False, '参数缺失'
    vp = virtual_rel.replace('\\', '/').strip('/')
    with _lock:
        _load_locked()
        item = _cache.get(vp)
        if item is None:
            return False, '映射不存在'
        if not is_admin and item.get('by') != username:
            return False, '无权移除他人的分享'
        del _cache[vp]
        _save_locked()
    add_log('分享映射已移除: %s' % vp, 'info')
    return True, None


def remove_by_owner(username):
    """删除 `by == username` 的**全部**映射（删除用户时的级联清理，T-1）。

    不清理的话，重建同名账号会继承旧分享清单（条目指向的源文件已随家目录归档，
    列表里不显示，但记录一直留着）。返回删掉的条数。
    """
    if not username:
        return 0
    with _lock:
        _load_locked()
        gone = [vp for vp, m in _cache.items()
                if isinstance(m, dict) and m.get('by') == username]
        for vp in gone:
            _cache.pop(vp, None)
        if gone:
            _save_locked()
    if gone:
        add_log('已清理用户 %s 的分享映射 %d 条' % (username, len(gone)), 'info')
    return len(gone)


def list_mappings(username, is_admin=False):
    """列出映射：本人（is_admin=True 可看全量）。"""
    with _lock:
        _load_locked()
        out = []
        for vp in sorted(_cache):
            item = _cache[vp]
            if not is_admin and item.get('by') != username:
                continue
            out.append({
                'path': vp,                       # 虚拟路径（相对共享根）
                'name': vp.rsplit('/', 1)[-1],
                'src': item.get('src', ''),
                'by': item.get('by', ''),
                'created': item.get('created', 0),
                'exists': _src_exists(item.get('src', '')),
            })
        return out


def _src_exists(src_rel):
    try:
        return os.path.isfile(os.path.join(UPLOAD_DIR, src_rel))
    except Exception:
        return False


def valid_username(username):
    """用户名是否可作为分享子目录名（与发布时的 sanitize 一致）"""
    from leaffs.files.core import sanitize_entry_name
    try:
        return isinstance(username, str) and bool(username) and sanitize_entry_name(username) == username
    except Exception:
        return False


def list_public(username):
    """访客分享展示页数据：某用户名下存在源文件的映射（公开信息，不含源路径）。

    与直链同授权语义：映射 = 用户主动发布，独立于游客模式开关。
    注意：_virtual_stat/resolve 会再取锁，故锁内只拷贝列表，锁外做 stat。
    """
    if not valid_username(username):
        return []
    pref = _user_dir_rel(username) + '/'
    with _lock:
        _load_locked()
        vps = [vp for vp in sorted(_cache)
               if vp.startswith(pref) and _cache[vp].get('by') == username]
    out = []
    for vp in vps:
        st = _virtual_stat(vp)
        if st is None:
            continue
        out.append({'name': vp.rsplit('/', 1)[-1], 'size': st[0],
                    'mtime': st[1], 'url': '/download/' + vp})
    return out


def resolve(virtual_rel):
    """虚拟路径 → 源文件绝对路径；未命中或源已不存在返回 None。"""
    if not isinstance(virtual_rel, str):
        return None
    vp = virtual_rel.replace('\\', '/').strip('/')
    with _lock:
        _load_locked()
        item = _cache.get(vp)
        if item is None:
            return None
    src = item.get('src', '')
    if not src:
        return None
    # 下发给访客之前再校验一次落在共享根内：表里可能还留着 publish 校验补上之前写进去的记录
    from leaffs.utils.core import abs_path
    full = abs_path(src)
    if not full:
        return None
    try:
        if os.path.isfile(full):
            return full
    except Exception:
        pass
    return None


def _virtual_stat(vp):
    """虚拟文件的展示大小/时间（= 源文件）；源失效返回 None"""
    full = resolve(vp)
    if not full:
        return None
    try:
        st = os.stat(full)
        return st.st_size, int(st.st_mtime)
    except Exception:
        return None


def _owner_of_virtual(vp):
    """public/shares/<用户名>/… → 用户名；不是映射路径则返回 ''"""
    parts = (vp or '').split('/')
    if len(parts) >= 3 and parts[0] == 'public' and parts[1] == 'shares':
        return parts[2]
    return ''


def merge_into_list(rel_path, result, unlocked=None):
    """把 rel_path 下的虚拟映射条目合并进 list_files 结果 dict（就地修改）。

    规则：只补充磁盘上不存在的层级/文件（磁盘条目优先，避免重名）；
    同时把虚拟条目计入 total_file_count / total_size_sum。
    返回是否发生了合并。

    unlocked：可选回调 (owner) -> bool。返回 False 的 owner，其虚拟条目**一律不合并** ——
    连文件夹本身都不出现在列表里，统计也不计入。分享码没解锁时，文件名/大小/修改时间
    本身就是信息，不能白看列表。传 None 表示不过滤（服务端内部调用）。
    """
    _load_locked()
    rel = (rel_path or '').replace('\\', '/').strip('/')
    if rel != 'public' and rel != 'public/shares' and not rel.startswith('public/shares/'):
        return False

    def visible(owner):
        if unlocked is None:
            return True
        if not owner:
            return False
        try:
            return bool(unlocked(owner))
        except Exception:
            return False

    files = result.get('files')
    if files is None:
        files = []
        result['files'] = files
    # public/shares 是保留的**虚拟区**：磁盘上的同名条目一律不展示（权限层也已经拒绝读写）。
    # 不剔的话，谁在 public/ 下建一个实体 shares/<用户名>/ 目录，就能顶掉虚拟分享条目，
    # 里面放的文件还绕过了分享码。
    if rel == 'public':
        files = [f for f in files if f.get('name') != 'shares']
        result['files'] = files
    elif rel == 'public/shares':
        files = []
        result['files'] = files
    disk_names = {f.get('name') for f in files}

    def bump(count_extra, size_extra):
        result['total_file_count'] = (result.get('total_file_count') or 0) + count_extra
        result['total_size_sum'] = (result.get('total_size_sum') or 0) + size_extra

    merged = False
    if rel == 'public':
        # 有映射时补虚拟根目录项 public/shares（统计=其下全部有效映射）
        if not _cache:
            return False
        subs = []
        for vp in _cache:
            if not visible(_owner_of_virtual(vp)):
                continue
            st = _virtual_stat(vp)
            if st is not None:
                subs.append(st)
        if not subs:
            return False
        if 'shares' not in disk_names:
            files.append({'name': 'shares', 'path': 'public/shares', 'type': 'folder',
                          'size': sum(s[0] for s in subs),
                          'mtime': max(s[1] for s in subs), 'virtual': True})
            merged = True
        return merged

    if rel == 'public/shares':
        # 每个有映射的用户一个虚拟文件夹
        users = sorted({m.split('/')[2] for m in _cache
                        if m.startswith('public/shares/') and len(m.split('/')) >= 3})
        for u in users:
            if not visible(u):
                continue
            if u in disk_names:
                continue
            subs = []
            pref = 'public/shares/' + u + '/'
            for vp in _cache:
                if vp.startswith(pref):
                    st = _virtual_stat(vp)
                    if st is not None:
                        subs.append(st)
            if not subs:
                continue
            files.append({'name': u, 'path': 'public/shares/' + u, 'type': 'folder',
                          'size': sum(s[0] for s in subs),
                          'mtime': max(s[1] for s in subs), 'virtual': True})
            merged = True
        return merged

    # rel == public/shares/<用户名>：补充该用户映射的文件（源失效的不展示）
    # 没解锁就整个目录什么都不给 —— 空列表，而不是 403：403 等于告诉对方"这里本来有东西"
    if not visible(_owner_of_virtual(rel)):
        return False
    pref = rel + '/'
    count_extra = 0
    size_extra = 0
    for vp in sorted(_cache):
        if not vp.startswith(pref):
            continue
        rest = vp[len(pref):]
        if '/' in rest:                 # 更深层级不存在（映射只到文件名）
            continue
        name = rest
        if name in disk_names:
            continue
        st = _virtual_stat(vp)
        if st is None:
            continue
        files.append({'name': name, 'path': vp, 'type': 'file',
                      'size': st[0], 'mtime': st[1], 'virtual': True,
                      'dl': '/download/' + vp})
        count_extra += 1
        size_extra += st[0]
        merged = True
    if count_extra:
        bump(count_extra, size_extra)
    return merged
