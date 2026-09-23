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
import secrets
import threading
import time

from leaffs.paths import UPLOAD_DIR, CONFIG_DIR, MOUNT_DIRNAME
from leaffs.runtime_log import add_log
from leaffs.utils.core import resolve_rel

_MAPPINGS_FILE = os.path.join(CONFIG_DIR, 'share_mappings.json')
_SHARE_ROOT_REL = 'public/shares'      # 普通分享：按用户分（public/shares/<用户名>/）
_MOUNT_ROOT_REL = MOUNT_DIRNAME        # 服务器挂载：不分用户（mounts/<名字>），与 public 同级
_MAX_TOTAL = 500          # 全量映射上限（防失控增长）
_MAX_USER = 100           # 单用户上限（只约束普通分享；挂载不分用户，只受总量上限约束）

_lock = threading.Lock()
_cache = None             # {virtual_rel: {'src'|'fs': ..., 'by': 属主, 'created': float, 'label': 随机标签}}


def _new_label():
    """给一条分享生成**随机标签** —— 分享码按它索引，不按名字。

    名字会变（改名/移动）、会撞（不同命名空间同名）、能枚举；
    随机标签三样都不沾（见 `share/access.py` 顶部的说明）。
    """
    return secrets.token_urlsafe(9)


def _load_locked():
    global _cache
    if _cache is not None:
        return
    _cache = {}
    need_save = False
    try:
        with open(_MAPPINGS_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for vp, item in raw.items():
                if not isinstance(vp, str) or not isinstance(item, dict):
                    continue
                src = item.get('src')
                fs = item.get('fs')
                by = item.get('by')
                if not isinstance(by, str):
                    continue
                entry = {'by': by, 'created': float(item.get('created') or 0)}
                # 两种来源互斥：`src` = 共享根内相对路径（原有），
                # `fs` = 服务器本机绝对路径（2026-09-22 起，只读映射）。
                # 旧记录只有 src，照旧读进来；这里**不能**只挑 src，
                # 否则新登记的 fs 条目会在下次加载时被静默丢掉。
                if isinstance(src, str) and src:
                    entry['src'] = src
                elif isinstance(fs, str) and fs:
                    entry['fs'] = fs
                else:
                    continue
                # 随机标签：分享码按它索引。老记录没有 → 补一个，**并且落盘** ——
                # 不落盘的话每次加载都会换一个新标签，那条分享的码就永远对不上。
                lab = item.get('label')
                if isinstance(lab, str) and lab:
                    entry['label'] = lab
                else:
                    entry['label'] = _new_label()
                    need_save = True
                _cache[vp] = entry
    except FileNotFoundError:
        pass
    except Exception as e:
        add_log('分享映射加载失败: %s' % e, 'warn')
    if need_save:
        try:
            _save_locked()
        except Exception:
            pass


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
        _cache[vp] = {'src': norm, 'by': username, 'created': time.time(),
                      'label': _new_label()}
        _save_locked()
    add_log('分享映射已添加: %s → %s' % (norm, vp), 'info')
    return vp, None


def publish_fs(fs_path, username):
    """登记一条**服务器本机路径**映射（目录或单个文件）—— 只登记引用，不复制文件。

    与 `publish` 是**两种来源、两种逻辑**：
      * `publish` 的源是共享根内的相对路径，挂在 `public/shares/<用户名>/` 下、**按用户分**；
      * 这里的源是服务器本机的绝对路径，挂在 `mounts/<名字>` 下、**不分用户** ——
        它是这台服务器的东西，不属于任何账号（所以也没有用户名那一层）。

    **条目名 = 源路径自己的名字**，不起别名：挂载只有服务端本机能做，他看到的名字就该是
    自己在资源管理器里看到的那个 —— 名字是路径的投影，不是标签。
    同名撞车**直接拒**：名字必须与实物一一对应，自动改名会让名字和实际指向对不上。

    **只读**：写操作对挂载区一律拒绝（`resolve_rel` 的只读位，五个写入口都据此判）。
    调用方必须先确认"操作者就在服务端本机"（本机令牌会话）；本函数只校验路径与登记本身。
    """
    from leaffs.files.core import sanitize_entry_name
    if not username or not isinstance(fs_path, str) or not fs_path.strip():
        return None, '参数缺失'
    if sanitize_entry_name(username) is None:
        return None, '用户名包含非法字符'
    if not os.path.isabs(fs_path):
        return None, '必须是指向本机的绝对路径'
    target = os.path.abspath(fs_path)
    # 共享根内的东西走 `publish`（相对路径）那条路，不必也不该从这里再挂一次
    inside = os.path.normcase(os.path.abspath(UPLOAD_DIR)) + os.sep
    if os.path.normcase(target + os.sep).startswith(inside):
        return None, '共享根内的路径请用普通分享'
    try:
        if not os.path.exists(target):
            return None, '路径不存在'
        if not (os.path.isfile(target) or os.path.isdir(target)):
            return None, '只能映射目录或普通文件'
    except Exception:
        return None, '路径不可读'
    name = sanitize_entry_name(os.path.basename(target.rstrip('\\/')))
    if name is None:
        return None, '这个名字不能作为挂载条目名'
    vp = _MOUNT_ROOT_REL + '/' + name
    with _lock:
        _load_locked()
        if len(_cache) >= _MAX_TOTAL:
            return None, '挂载数量已达上限'
        if vp in _cache:
            return None, '已经有一条同名挂载（%s），先卸掉它再挂' % _cache[vp].get('fs', '')
        _cache[vp] = {'fs': target, 'by': username, 'created': time.time(),
                      'label': _new_label()}
        _save_locked()
    add_log('本机路径已挂载: %s → %s' % (target, vp), 'info')
    return vp, None


def lookup_fs_prefix(rel):
    """相对路径是否落在某条**本机路径映射**下；命中返回 `(虚拟前缀, 本机绝对路径)`。

    映射目录**内部**的文件不在表里逐条登记，靠"前缀 + 余部"拼出来 —— 这是那种解析的
    唯一实现：`utils.core.resolve_rel` 与本模块的 `resolve` 都调它。
    嵌套映射（先把 `E:\\媒体` 挂成 A、再把 `E:\\媒体\\电影` 挂成 B）取**最长前缀**。

    ⚠️ 余部拼出来后仍要用 `safe_path(登记的根, 结果)` 判定 —— 按登记值判，
    绝不拿请求里的字符串去拼绝对路径。
    """
    if not isinstance(rel, str) or not rel:
        return None
    rel = rel.replace('\\', '/').strip('/')
    # 挂载条目一律登记在 `mounts/` 下：先用这一点把绝大多数请求挡在锁外
    if not rel.startswith(_MOUNT_ROOT_REL + '/'):
        return None
    best = None
    with _lock:
        _load_locked()
        for vp, item in _cache.items():
            fs = item.get('fs') if isinstance(item, dict) else None
            if not fs:
                continue
            if rel == vp or rel.startswith(vp + '/'):
                if best is None or len(vp) > len(best[0]):
                    best = (vp, fs)
    return best


def access_scope(vp):
    """这条虚拟路径属于哪个**分享条目**：返回 `(标签, 属主)`。

    分享码按**条目**判（每条分享一个码），所以闸门需要"路径 → 标签"这一步：
      * `public/shares/<用户名>/<名字>` → 那一条分享；
      * `mounts/<名字>` 及其**内部任意层级** → 那个挂载点（`fs` 来源靠前缀解析）。
    不属于任何登记条目（普通磁盘路径）→ `('', '')`，闸门据此恒放行。
    """
    if not isinstance(vp, str) or not vp:
        return ('', '')
    rel = vp.replace('\\', '/').strip('/')
    with _lock:
        _load_locked()
        item = _cache.get(rel)
        if isinstance(item, dict) and item.get('label'):
            return (item['label'], item.get('by') or '')
    hit = lookup_fs_prefix(rel)
    if hit:
        with _lock:
            _load_locked()
            item = _cache.get(hit[0])
        if isinstance(item, dict) and item.get('label'):
            return (item['label'], item.get('by') or '')
    return ('', '')


def all_scopes():
    """所有条目的 `(标签, 属主, 虚拟路径)` —— 供启动时把老的"按用户名一个码"迁移过来。"""
    with _lock:
        _load_locked()
        return [(it.get('label') or '', it.get('by') or '', vp)
                for vp, it in _cache.items()
                if isinstance(it, dict) and it.get('label')]


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
        label = item.get('label') or ''
        del _cache[vp]
        _save_locked()
    if label:
        # 条目没了，它的码 / 计数 / 锁 / 票据也一起清 —— 否则就是指向不存在分享的残留
        try:
            from leaffs.share import access as _sacc
            _sacc.forget(label)
        except Exception:
            pass
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
    """列出映射：本人（is_admin=True 可看全量）。

    `local=True` 表示这条来源是**服务器本机路径**（`src` 里给的就是那个绝对路径，
    只给本人与 admin 看）；目录映射带 `type='folder'`，它没有下载直链。

    ⚠️ 锁内只拷贝数据，`_mapped_target` / `_virtual_stat` 一律放到锁外调 ——
    它们自己会取同一把 `_lock`（不可重入），锁里再调就是死锁。
    """
    with _lock:
        _load_locked()
        rows = [(vp, dict(_cache[vp])) for vp in sorted(_cache)]
    out = []
    for vp, item in rows:
        if not is_admin and item.get('by') != username:
            continue
        fs = item.get('fs', '')
        mapped = _mapped_target(vp)
        out.append({
            'path': vp,                       # 虚拟路径（相对共享根）
            'name': vp.rsplit('/', 1)[-1],
            'src': fs or item.get('src', ''),
            'by': item.get('by', ''),
            'created': item.get('created', 0),
            'exists': os.path.exists(fs) if fs else _src_exists(item.get('src', '')),
            'local': bool(fs),
            'type': 'folder' if (mapped and os.path.isdir(mapped)) else 'file',
        })
    return out


def _src_exists(src_rel):
    try:
        resolved = resolve_rel(src_rel, UPLOAD_DIR)
        return bool(resolved) and os.path.isfile(resolved[0])
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
                    'mtime': st[1], 'path': vp, 'url': '/download/' + vp})
    return out


def _mapped_target(vp):
    """本机路径映射对应的真实路径（条目精确命中，或落在映射目录内部）；没有则 None。

    只做"查表 + 拼余部"，**不判存在、不判文件还是目录** —— 调用方各自决定
    （下载只认文件，列表要看目录）。余部拼完仍按**登记的根**判定越界。
    """
    with _lock:
        _load_locked()
        item = _cache.get(vp)
    if isinstance(item, dict) and item.get('fs'):
        return item['fs']
    hit = lookup_fs_prefix(vp)
    if not hit:
        return None
    prefix, root = hit
    rest = vp[len(prefix):].strip('/')
    full = os.path.join(root, rest) if rest else root
    from leaffs.utils.core import safe_path
    if not safe_path(root, full):
        return None
    return full


def _fs_file(path):
    """是普通文件才返回它 —— 目录由列表那条路处理，不从这里出去。"""
    try:
        if os.path.isfile(path):
            return path
    except Exception:
        pass
    return None


def resolve(virtual_rel):
    """虚拟路径 → 源**文件**绝对路径；未命中 / 源已不存在 / 命中目录 → None。

    来源依次判：本机路径映射（条目或映射目录内部）→ 共享根内的相对路径（原有）。
    """
    if not isinstance(virtual_rel, str):
        return None
    vp = virtual_rel.replace('\\', '/').strip('/')
    mapped = _mapped_target(vp)
    if mapped is not None:
        return _fs_file(mapped)
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
    return _fs_file(full)


def _virtual_stat(vp):
    """虚拟条目的展示大小/时间（= 源头）；源失效返回 None。

    ⚠️ **分享区里的条目有两种来源，大小口径不同**（别一刀切）：
      * `src` = 共享根内的相对路径（普通分享）：**照常报大小** —— 它本来就是共享根里的
        东西，参与公共目录的大小统计；
      * `fs` = 服务器本机路径（挂载进来的）：**目录不报大小**（报 0）—— 它是映射出来的，
        不该参与公共目录的大小计算；要看大小就点进去，那一层用服务端既有的目录统计。
        单文件照常报（就是那个文件的大小）。
    """
    with _lock:
        _load_locked()
        item = _cache.get(vp)
    if isinstance(item, dict) and item.get('fs'):
        try:
            st = os.stat(item['fs'])
        except Exception:
            return None
        if os.path.isdir(item['fs']):
            return 0, int(st.st_mtime)          # fs 来源的目录：不报大小
        return st.st_size, int(st.st_mtime)
    full = _mapped_target(vp) or resolve(vp)
    if not full:
        return None
    try:
        st = os.stat(full)
        size = 0 if os.path.isdir(full) else st.st_size
        return size, int(st.st_mtime)
    except Exception:
        return None


def merge_into_list(rel_path, result, unlocked=None):
    """把 rel_path 下的虚拟映射条目合并进 list_files 结果 dict（就地修改）。

    规则：只补充磁盘上不存在的层级/文件（磁盘条目优先，避免重名）；
    同时把虚拟条目计入 total_file_count / total_size_sum。
    返回是否发生了合并。

    unlocked：可选回调 `(虚拟路径) -> bool`。返回 False 的那条分享**一律不合并** ——
    连名字都不出现在列表里，统计也不计入。分享码没解锁时，文件名/大小/修改时间
    本身就是信息，不能白看列表。传 None 表示不过滤（服务端内部调用）。
    """
    _load_locked()
    rel = (rel_path or '').replace('\\', '/').strip('/')
    if rel not in ('', 'public', 'public/shares', _MOUNT_ROOT_REL) \
            and not rel.startswith('public/shares/'):
        return False

    def visible(vp):
        """这条分享解锁了没有（判定只有一份实现：HTTP 与 WS 都提供同一个回调）"""
        if unlocked is None:
            return True
        if not vp:
            return False
        try:
            return bool(unlocked(vp))
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

    # ---------- 服务器挂载区（`mounts/`，与 public 同级）----------
    # 两种来源两种逻辑：普通分享**按用户分**（条目挂在用户名下）；挂载**没有属主** ——
    # 它的访问权限与公共目录一个级别（由权限层判），码挂在自己那条的随机标签上
    # （下面按 `visible(vp)` 逐条判，锁着的挂载点给"需要访问码"占位）。
    # ⚠️ 根层那个 `mounts` 目录的 size 报 0：挂载是映射出来的，**不参与**目录大小统计；
    # 进了 `mounts/` 之后，条目按各自来源报（文件报真实大小，目录由 `_virtual_stat` 报 0）。
    if rel in ('', _MOUNT_ROOT_REL):
        mounts = sorted((vp, item) for vp, item in _cache.items()
                        if isinstance(item, dict) and item.get('fs'))
        if not mounts:
            return False
        if rel == '':
            if _MOUNT_ROOT_REL in disk_names:
                return False
            times = [st[1] for st in (_virtual_stat(vp) for vp, _ in mounts)
                     if st is not None]
            if not times:
                return False
            files.append({'name': _MOUNT_ROOT_REL, 'path': _MOUNT_ROOT_REL, 'type': 'folder',
                          'size': 0, 'mtime': max(times),
                          'virtual': True, 'local': True})
            return True
        merged = False
        count_extra = 0
        size_extra = 0
        for vp, item in mounts:
            name = vp.split('/', 1)[1]
            if name in disk_names:
                continue
            if not visible(vp):
                # 锁着的挂载点给一个**占位**：不给名字，只给随机标签让访客能对着它输码。
                # （分享区那边不给占位 —— 它有分享页那条单独的入口；挂载区在浏览页里，
                #   不给占位就永远没有输码的地方。）
                label = item.get('label') or ''
                if label:
                    files.append({'name': '需要访问码', 'path': '', 'type': 'locked',
                                  'label': label, 'size': 0, 'mtime': 0,
                                  'virtual': True, 'local': True})
                    merged = True
                continue
            st = _virtual_stat(vp)
            if st is None:
                continue
            if os.path.isdir(item.get('fs', '')):
                files.append({'name': name, 'path': vp, 'type': 'folder',
                              'size': st[0], 'mtime': st[1],
                              'virtual': True, 'local': True})
            else:
                files.append({'name': name, 'path': vp, 'type': 'file',
                              'size': st[0], 'mtime': st[1],
                              'virtual': True, 'local': True,
                              'dl': '/download/' + vp})
                count_extra += 1
                size_extra += st[0]
            merged = True
        if count_extra:
            bump(count_extra, size_extra)
        return merged

    merged = False
    if rel == 'public':
        # 有映射时补虚拟根目录项 public/shares（统计=其下全部有效映射）
        if not _cache:
            return False
        subs = []
        for vp in _cache:
            if not visible(vp):
                continue
            st = _virtual_stat(vp)
            if st is not None:
                subs.append(st)
        if not subs:
            return False
        if 'shares' not in disk_names:
            # size 报 0：分享区全是虚拟条目，**不参与公共目录的大小统计**
            # （`subs` 只用来判"到底有没有可用映射"和取 mtime）
            files.append({'name': 'shares', 'path': 'public/shares', 'type': 'folder',
                          'size': 0,
                          'mtime': max(s[1] for s in subs), 'virtual': True})
            merged = True
        return merged

    if rel == 'public/shares':
        # 每个有映射的用户一个虚拟文件夹
        users = sorted({m.split('/')[2] for m in _cache
                        if m.startswith('public/shares/') and len(m.split('/')) >= 3})
        for u in users:
            if u in disk_names:
                continue
            subs = []
            pref = 'public/shares/' + u + '/'
            for vp in _cache:
                # 逐条判：没解锁的分享**连名字都不给**；该用户名下一条都没解锁 → 目录不出现
                if vp.startswith(pref) and visible(vp):
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
    # **逐条判解锁**：没解锁的那条连名字都不出现 —— 空列表，而不是 403：
    # 403 等于告诉对方"这里本来有东西"
    pref = rel + '/'
    count_extra = 0
    size_extra = 0
    for vp in sorted(_cache):
        if not vp.startswith(pref):
            continue
        if not visible(vp):
            continue                    # 这一条没解锁 → 连名字都不给
        rest = vp[len(pref):]
        if '/' in rest:                 # 更深层级不存在（映射只到文件名）
            continue
        name = rest
        if name in disk_names:
            continue
        st = _virtual_stat(vp)
        if st is None:
            continue
        # 普通分享只登记**文件**（`publish` 里 `isfile` 卡着），这里没有目录那一支 ——
        # 目录是挂载区的事（`mounts/`，上面单独一支处理）
        files.append({'name': name, 'path': vp, 'type': 'file',
                      'size': st[0], 'mtime': st[1], 'virtual': True,
                      'dl': '/download/' + vp})
        count_extra += 1
        size_extra += st[0]
        merged = True
    if count_extra:
        bump(count_extra, size_extra)
    return merged
