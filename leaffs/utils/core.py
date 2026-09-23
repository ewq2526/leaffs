#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
📁 core_utils.py — 核心工具函数（从 file_system.py 和 server_config.py 抽离）
"""

import os
import json
import time
import threading
import shutil
import subprocess
import mimetypes
import errno
import hashlib
import html as html_mod
from collections import OrderedDict

# ===================================================
# 运行时目录定位：单一数据源是 leaffs.paths（见该模块文档）。
# 这里只导入**本模块自己要用**的名字，不再替别的模块转口 —— 需要路径常量一律
# `from leaffs.paths import ...`（2026-09-22 收口；原先经此处取路径的模块已全部直连）。
# ===================================================
from leaffs.paths import (  # noqa: E402
    UPLOAD_DIR, UPLOAD_TMP_DIR, CACHE_DIR, THUMB_DIR,
    MOUNT_DIRNAME, is_upload_tmp_entry, find_bundled_exe,
)
FOLDER_SIZE_DB = os.path.join(CACHE_DIR, 'folder_sizes.json')

# ========== 常量 ==========
COPY_BUFFER_SIZE = 16777216
CACHE_MAX_ITEMS = 50
CACHE_TTL = 5
FOLDER_SIZE_TTL = 5

# ========== 缩略图清理常量 ==========
THUMB_SAMPLE_RATIO = 0.1          # 采样比例（10%）
THUMB_MISS_THRESHOLD = 0.05       # 源文件缺失阈值（>5% 触发全量清理）
THUMB_SCAN_BATCH = 100            # 后台扫描每批数量
THUMB_SCAN_INTERVAL = 0.5         # 每批间隔（秒）

# ========== 防抖缓存失效常量 ==========
DEBOUNCE_DELAY = 2.0  # 防抖延迟（秒），上传停止后等待 2 秒执行最终清除

# ========== 请求头工具 ==========

def parse_cookies(raw):
    """把请求头里的 `Cookie` 串解析成 {名字: 值}（同名取**最后一个**）。

    **全仓只此一份**：会话解析（`auth/core.get_session`）、无效会话预检
    （`server/handler._has_invalid_session_cookie`）、分享码票据
    （`share/access._cookie_ok`）、页面主题偏好（`web/render._cookie_accent` /
    `_cookie_theme`）现在都调它。

    为什么必须收成一份：这五处原本各写一遍，而且**已经不一致了** —— 会话那三处取
    "最后一个"，主题那两处取"第一个**合法**值"，同名且都合法时结果就不同。
    两份实现必然漂移（LF-27 的列表构建就是这么来的），所以把"同名谁说了算"
    这个语义钉在一个地方。

    取最后一个与 RFC 6265 及浏览器行为一致（同名 Cookie 以最后写下的为准）。
    没有 `=` 的畸形段跳过；非字符串输入当空 —— 这是原来那几处各自的容错语义
    （`share/access` 的 try/except、`render` 的 `cookie or ''`）搬到这里的，不是新加的保险。
    """
    if not isinstance(raw, str):
        return {}
    out = {}
    for seg in raw.split(';'):
        k, sep, v = seg.partition('=')
        if not sep:
            continue
        out[k.strip()] = v.strip()
    return out


# ========== 路径工具 ==========
def safe_path(base, target):
    """确保 target 在 base 目录下（带路径边界，防止 base 前缀被仿冒）"""
    real_target = os.path.realpath(target)
    real_base = os.path.realpath(base)
    if os.name == 'nt':
        # Windows 路径比较不区分大小写
        nt = os.path.normcase(real_target)
        nb = os.path.normcase(real_base)
    else:
        nt, nb = real_target, real_base
    return nt == nb or nt.startswith(nb + os.sep)

def _mapped_prefix(rel_path):
    """问分享映射表：这条相对路径是否落在一条**本机路径**映射下（lazy import）。

    为什么把 import 放进函数里：`share.mappings` 依赖 `files.core`，而两者都依赖本模块 ——
    顶层 import 会成环。运行时再导入则一定已经加载完，依赖方向也保持干净
    （utils 不认识 share）。取不到（未加载 / 出错）就当作没有映射，
    退回共享根本身的解析 —— 与"这条功能还没启用"行为一致。
    """
    if not rel_path or not isinstance(rel_path, str):
        return None
    if not rel_path.replace('\\', '/').lstrip('/').startswith(MOUNT_DIRNAME + '/'):
        return None
    try:
        from leaffs.share import mappings as _mappings
        return _mappings.lookup_fs_prefix(rel_path)
    except Exception:
        return None


def resolve_rel(rel_path, base=None):
    """相对路径 → `(绝对路径, 所属根, 只读)`；非法 / 越界一律 `None`。

    **全仓唯一的"相对路径 → 绝对路径"入口**：读路径（预览 / 下载 / 缩略图 /
    打包 / 搜索）与写路径（上传 / 删除 / 建目录）都必须走它。
    各处自己 `os.path.join(根, rel)` 再 `safe_path` 的写法必然漏掉某一处 ——
    判定散着写就会漂移，收成这一份才谈得上"新来源接进来不会漏"。

    `base` 默认共享根。调用方注入的那个根（`files/api.py` 的 `UPLOAD_DIR` 参数）
    要显式传进来，别在这里偷偷用全局值：测试会 patch 注入值，
    两者混用会让"以为在临时目录里跑"的用例落到真实数据根上。

    命中分享区里的**本机路径映射**时，改用那条登记的真实根拼余部，
    并且第三位返回 `True` —— 那种来源只读，写路径必须据此拒绝。
    """
    mapped = _mapped_prefix(rel_path)
    if mapped:
        prefix, real_root = mapped
        rest = rel_path[len(prefix):].strip('/\\')
        full = os.path.join(real_root, rest) if rest else real_root
        if not safe_path(real_root, full):
            return None
        return full, real_root, True
    root = UPLOAD_DIR if base is None else base
    full = os.path.join(root, rel_path) if rel_path else root
    if not safe_path(root, full):
        return None
    return full, root, False


def abs_path(rel_path):
    """相对路径转绝对路径，并做安全检查（非法 / 越界返回 None）"""
    resolved = resolve_rel(rel_path)
    return resolved[0] if resolved else None


# Win32 保留设备名：这些名字（含 `NUL.txt` 这种带扩展名的形态）在 Windows 上由系统
# 按设备解析，**不是**普通文件。
_WIN_DEVICE_NAMES = frozenset(
    ['CON', 'PRN', 'AUX', 'NUL']
    + ['COM%d' % i for i in range(1, 10)]
    + ['LPT%d' % i for i in range(1, 10)]
)


def has_windows_device_name(rel_path):
    """路径里是否含 Win32 保留设备名（任意一段命中即算，大小写不敏感）。

    为什么必须挡：`os.path.exists(r'...\\NUL')` 在 Windows 上返回 **True** —— Win32 把
    `NUL` 当设备，于是这个路径能通过"文件是否存在"的检查，一路进到缩略图流水线才抛异常。
    表现是**管理员拿到 500、非管理员因为先被权限检查拦下而拿到 404**（黑盒报告 N-3 说的
    "错误码不一致"就是它；`/api/raw?path=NUL` 则是返回 0 字节）。

    判定取**点号之前**那一段：Windows 把 `NUL.txt` 同样解析成设备，只比整段是不够的；
    尾随空格与点在 Win32 里也会被吃掉，一并 rstrip 掉。

    正常文件系统里这些名字本来就创建不出来，挡掉不会误伤真实文件
    （`NULL.txt`、`COM10`、`console` 都照常放行）。
    """
    for seg in rel_path.replace('\\', '/').split('/'):
        if seg.split('.', 1)[0].rstrip(' .').upper() in _WIN_DEVICE_NAMES:
            return True
    return False


def normalize_rel_path(rel_path):
    """规范化相对路径并禁止路径穿越 —— **全仓唯一一份**。

    为什么收到这里：原来 `files/core.py` 与 `files/api.py` 各有一份**逐字相同**的实现
    （`files/api.py` 刻意不 import `files/core.py`，于是两边各自抄了一遍）。
    两份一样的判定必然漂移 —— N-3 就是例证：设备名检查该加的地方其实是**两份**，
    只加一份就会漏。收成一份之后，新增判定不会再漏掉另一半。

    返回规范化后的相对路径（正斜杠）；**非法一律返回 `None`**，`''` 表示"根"（合法）。
    """
    if not rel_path:
        return ''
    # 先检查原始路径中是否包含 ..（必须在 normpath 之前检查，否则 normpath 会先解析掉 ..）
    raw_parts = rel_path.replace('\\', '/').split('/')
    if '..' in raw_parts:
        return None
    if raw_parts and raw_parts[0] in ('..', '.'):
        return None
    # 规范化路径：移除多余的 . 和 /
    norm = os.path.normpath(rel_path).replace('\\', '/')
    # 禁止绝对路径
    if norm.startswith('/'):
        return None
    # 禁止 Windows 盘符（C:/x、C:x）：normpath 不去盘符，而
    # os.path.join(UPLOAD_DIR, 'C:/x') 在 Windows 上会直接返回 'C:/x' —— 等于跳出共享根
    if len(norm) >= 2 and norm[1] == ':' and norm[0].isalpha():
        return None
    # 标准化后再次检查，防止 normpath 改变相对结构
    if norm in ('..', '../') or norm.startswith('../'):
        return None
    # 禁止 Win32 保留设备名（NUL/CON/COM1…）：os.path.exists 对它们返回 True，
    # 于是能混过"文件是否存在"的检查、到下游才炸（N-3）
    if has_windows_device_name(norm):
        return None
    return norm


def get_mime(path):
    mime, _ = mimetypes.guess_type(path)
    return mime or 'application/octet-stream'

def esc_html(text):
    return html_mod.escape(str(text))

# ========== 文件内容缓存 ==========
_file_cache = OrderedDict()
_file_cache_lock = threading.Lock()

def read_file_cached(path):
    now = time.time()
    with _file_cache_lock:
        if path in _file_cache:
            data, mtime, cache_time = _file_cache[path]
            if now - cache_time < CACHE_TTL and mtime == os.path.getmtime(path):
                return data
    try:
        with open(path, 'rb') as f:
            data = f.read()
        mtime = os.path.getmtime(path)
        with _file_cache_lock:
            _file_cache[path] = (data, mtime, now)
            while len(_file_cache) > CACHE_MAX_ITEMS:
                _file_cache.popitem(last=False)
        return data
    except Exception:
        return None

def invalidate_file_cache(path):
    with _file_cache_lock:
        _file_cache.pop(path, None)

# ========== 文件夹聚合缓存（大小 + 文件数 + 文件夹数，按路径分片持久化） ==========
# 每个“路径”条目在一次扫描中同时得到 大小/文件数/文件夹数，三者天然同源。
# 递归读取时复用子路径条目按路径加和，因此任何目录（包括共享根这个“大文件夹”）
# 都能直接读出聚合统计；文件树变更经 invalidate_folder_cache 使对应路径失效，
# 下一次读取即重算 —— 目录列表与服务器统计完全共享这一机制，无需额外逻辑。
#
# 磁盘持久化按“顶级路径段”分片为 folder_sizes/<段>.json：一次失效/写入只重写
# 所属分片的小文件，避免条目很多时对单一大文件做全量 dump。
# 旧版单文件 folder_sizes.json 在模块加载时自动迁移拆分（幂等）。
_folder_size_memory = {}
_folder_size_lock = threading.RLock()
_folder_db_lock = threading.RLock()  # 保护分片文件读写（RLock 支持递归调用）
FOLDER_SIZE_DIR = os.path.join(CACHE_DIR, 'folder_sizes')

def _shard_name(path):
    """把路径归到某个分片：共享目录按第一层(users/public/__root__)，缓存归 __cache__，其余 __meta__"""
    try:
        ap = os.path.abspath(path)
        ud = os.path.abspath(UPLOAD_DIR)
        if ap == ud or ap.startswith(ud + os.sep):
            rel = os.path.relpath(ap, ud)
            return '__root__' if rel == '.' else rel.split(os.sep)[0]
        cd = os.path.abspath(CACHE_DIR)
        if ap == cd or ap.startswith(cd + os.sep):
            return '__cache__'
    except Exception:
        pass
    return '__meta__'

def _shard_file(name):
    return os.path.join(FOLDER_SIZE_DIR, name + '.json')

def _load_shard(name):
    """读取单个分片（在 _folder_db_lock 内调用）"""
    try:
        fp = _shard_file(name)
        if os.path.exists(fp):
            with open(fp, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_shard(name, db):
    """原子写单个分片（在 _folder_db_lock 内调用）"""
    try:
        fp = _shard_file(name)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        tmp = fp + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(db, f)
        os.replace(tmp, fp)
    except Exception:
        pass

def _shard_names_on_disk():
    try:
        if os.path.isdir(FOLDER_SIZE_DIR):
            return [fn[:-5] for fn in os.listdir(FOLDER_SIZE_DIR) if fn.endswith('.json')]
    except Exception:
        pass
    return []

def _migrate_legacy_folder_db():
    """一次性迁移旧版单文件 folder_sizes.json → 分片目录（旧文件改名保留）"""
    try:
        if not os.path.exists(FOLDER_SIZE_DB):
            return
        with open(FOLDER_SIZE_DB, 'r') as f:
            legacy = json.load(f)
        if not isinstance(legacy, dict):
            return
        groups = {}
        for key, value in legacy.items():
            groups.setdefault(_shard_name(key), {})[key] = value
        with _folder_db_lock:
            for name, items in groups.items():
                db = _load_shard(name)
                db.update(items)
                _save_shard(name, db)
            os.rename(FOLDER_SIZE_DB, FOLDER_SIZE_DB + '.legacy')
    except Exception:
        pass

_migrate_legacy_folder_db()

def _load_folder_agg(value):
    """解析持久化的路径条目；旧版纯整数大小缺计数，视为无效需重扫补齐"""
    if isinstance(value, dict):
        try:
            return {
                'size': int(value.get('size', 0)),
                'files': int(value.get('files', 0)),
                'folders': int(value.get('folders', 0)),
            }
        except Exception:
            return None
    return None

def _agg_key(path):
    """目录聚合缓存的**唯一键**：`normcase(abspath(path))`。

    为什么必须归一化：这套缓存是「按路径存取」的，而调用方给出的路径形态各不相同 ——
    `abs_path()` 用 `os.path.join(UPLOAD_DIR, rel)` 拼出来的是**混合分隔符**
    （`...\\users/zzw`：rel 里的 `/` 不会被 join 换掉），而
    `invalidate_folder_cache()` 内部一律 `os.path.abspath()` 做了归一化。
    于是**写入用的键和删除用的键永远不相等**，任何失效调用都删不掉东西：

        写入 key = 'E:\\test\\shared_files\\users/zzw'
        删除 key = 'E:\\test\\shared_files\\users\\zzw'      ← 永不匹配

    后果（外部黑盒报告 N-9，2026-09-21）：目录聚合值一经写入就**永久冻结** ——
    上传不更新（`saved>0` 后确实调了失效，但删不到）；管理员删号重建后，新主人
    看到的是上一任的文件数与字节数（`recursive` 失效用的前缀是
    `...\\users\\`，也匹配不上 `...\\users/zzv2`）。`folder_size_ttl` 看着像兜底，
    实则形同虚设：磁盘命中那条路根本不看时间。

    归一化放在**存取两端**，是让「检查的对象」和「取用的对象」重新变成同一个 ——
    修的是根因，不是给失效失败加保险丝。

    normcase 顺带抹平 Windows 的大小写与盘符大小写差异（同目录只该有一个槽位）。
    """
    return os.path.normcase(os.path.abspath(path))


def _get_folder_agg(path):
    """读取目录聚合 {size, files, folders}：内存 → 分片磁盘 → 递归扫描（按路径加和）"""
    path = _agg_key(path)
    now = time.time()
    with _folder_size_lock:
        if path in _folder_size_memory:
            agg, ts = _folder_size_memory[path]
            if now - ts < FOLDER_SIZE_TTL:
                return dict(agg)
    # 分片磁盘快查（短暂持锁）
    sh = _shard_name(path)
    with _folder_db_lock:
        db = _load_shard(sh)
        agg = _load_folder_agg(db.get(path)) if path in db else None
    if agg is not None:
        with _folder_size_lock:
            _folder_size_memory[path] = (agg, now)
        return agg
    # 磁盘无此路径或为旧格式 → 锁外扫描（扫描期间不阻塞其它目录的磁盘读取）
    agg = _scan_folder_agg(path)
    now = time.time()
    with _folder_size_lock:
        _folder_size_memory[path] = (agg, now)
    with _folder_db_lock:
        db = _load_shard(sh)
        existing = _load_folder_agg(db.get(path)) if path in db else None
        if existing is not None:
            # 并发线程已写入：以已持久化的为准并同步内存
            with _folder_size_lock:
                _folder_size_memory[path] = (existing, time.time())
            return existing
        db[path] = agg
        _save_shard(sh, db)
    return agg


def _migrate_folder_db_keys():
    """一次性清掉分片里**未归一化的旧键**（幂等）。

    旧版本的键用的是调用方给出的原始路径形态（`...\\users/zzw` —— `abs_path()` 用
    `os.path.join` 拼出的混合分隔符），与 `_agg_key()` 归一化后的键不相等：既命不中、
    也删不掉，只会永久躺在磁盘上。

    直接**丢弃**而不是合并：下一次读取会重新扫描得到正确值。合并反而会把"从未失效过
    的陈旧数字"继续带下去 —— 那正是 N-9 本身。
    """
    with _folder_db_lock:
        for name in _shard_names_on_disk():
            db = _load_shard(name)
            stale = [k for k in db if k != _agg_key(k)]
            if not stale:
                continue
            for k in stale:
                del db[k]
            _save_shard(name, db)


# 必须在任何一次聚合读取之前跑完 —— 否则旧键会先被读进内存并当成有效值
_migrate_folder_db_keys()


def _scan_folder_agg(path):
    """扫描单个目录并递归加和子目录缓存条目（不持有 db 锁）

    LF-22：跳过上传临时目录 —— 那里装的是"写到一半"的数据，不该算进用户的已用空间，
    也不该让目录大小在上传过程中一路往上涨（用户看到的"大小时刻在变"就是它）。
    本函数是递归的（子目录也走它），所以在这一处跳过即可全层级生效。
    """
    agg = {'size': 0, 'files': 0, 'folders': 0}
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if is_upload_tmp_entry(entry.name):
                        continue
                    if entry.is_file(follow_symlinks=False):
                        agg['size'] += entry.stat(follow_symlinks=False).st_size
                        agg['files'] += 1
                    elif entry.is_dir(follow_symlinks=False):
                        sub = _get_folder_agg(entry.path)
                        agg['size'] += sub['size']
                        agg['files'] += sub['files']
                        agg['folders'] += 1 + sub['folders']
                except Exception:
                    pass
    except Exception:
        pass
    return agg

def get_folder_size(path):
    """目录总大小（保留原名/语义，供现有调用使用）"""
    return _get_folder_agg(path)['size']

def get_folder_stats(path):
    """目录聚合统计 {size, files, folders}（共享根可当“大文件夹”直接读取）"""
    return _get_folder_agg(path)

def _ancestors_under_upload(ap):
    """共享根内绝对路径的祖先链（不含自身、含共享根）。

    目录聚合缓存是“递归加和”（根 = 整树；任一子路径变更都会改变所有祖先的
    聚合值），因此失效时须联动失效祖先，否则根/父级统计会长期停留在旧值
    （曾导致“我的页”全站用量显示 0）。

    入参必须是 `_agg_key()` 归一化后的路径，比较基准同源 —— 否则会出现
    "前缀看着对、字符串不相等"的静默失效（N-9 的成因面之一）。
    """
    root = _agg_key(UPLOAD_DIR)
    if ap == root:
        return []
    if not ap.startswith(root + os.sep):
        return []
    out = []
    cur = os.path.dirname(ap)
    while True:
        out.append(cur)
        if cur == root:
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return out


def _notify_gallery_changed():
    """文件树变了 → 通知订阅它的页面重新拉列表（WS 推送）。

    挂在缓存失效这里，而不是各个写入点：任何文件改动都要失效缓存，所以这里天然
    覆盖全部路径（上传/删除/建目录/下载落盘/原生导入/用户改名…）。逐点打点必然
    会漏 —— 已经漏过一次（下载落盘没接）。
    延迟导入：push 那条链会绕回本模块，顶层导入成环。
    """
    try:
        from leaffs.server import push as _push
        _push.broadcast_gallery_changed()
    except Exception:
        pass


def invalidate_folder_cache(path, recursive=False):
    """
    使文件夹聚合缓存（大小/文件数/文件夹数）失效。
    recursive=True 时也清理 path 下所有子目录的缓存（用于整个目录被删除的场景）。
    recursive=False 时只清理 path 本身（上传文件、创建文件夹等场景）。
    共享根内的路径会联动失效其祖先聚合（含共享根），保证根/父级统计不陈旧。
    分片写入只重写受影响的分片文件。
    """
    _notify_gallery_changed()
    ap = _agg_key(path)
    ancestors = _ancestors_under_upload(ap)
    with _folder_size_lock:
        mem_keys = set()
        if recursive:
            path_prefix = ap.rstrip(os.sep) + os.sep
            for k in _folder_size_memory:
                if k == ap or k.startswith(path_prefix):
                    mem_keys.add(k)
        else:
            mem_keys.add(ap)
        for anc in ancestors:
            mem_keys.add(anc)
        for k in mem_keys:
            _folder_size_memory.pop(k, None)
    with _folder_db_lock:
        # 顶层根(共享根/缓存根)整体失效 → 清理所有分片；否则按受影响分片清理
        root_ap = _agg_key(UPLOAD_DIR)
        cache_ap = _agg_key(CACHE_DIR)
        if recursive and (ap == root_ap or ap == cache_ap):
            for name in _shard_names_on_disk():
                _save_shard(name, {})
            return
        shard_targets = {}

        def _add_key(p):
            shard_targets.setdefault(_shard_name(p), set()).add(p)

        _add_key(ap)
        if recursive:
            prefix = ap.rstrip(os.sep) + os.sep
            db0 = _load_shard(_shard_name(ap))
            for k in db0:
                if k == ap or k.startswith(prefix):
                    _add_key(k)
        for anc in ancestors:
            _add_key(anc)
        for name, keys in shard_targets.items():
            db = _load_shard(name)
            hit = False
            for k in keys:
                if k in db:
                    del db[k]
                    hit = True
            if hit:
                _save_shard(name, db)

# ========== 配额在途记账（C-06 / IC-QUOTA） ==========
# 进程内“在途字节”账本：写操作（上传等）在配额检查通过后 quota_reserve 预留字节，
# 请求结束（成功或异常）用 quota_settle 结算，防止并行请求利用陈旧的磁盘统计
# （TOCTOU）绕过配额。bucket 命名：'total' / 'public' / 'users/<name>'。


class UploadQuotaExceeded(Exception):
    """读流期间实时校验发现超额 —— 立刻中断这次上传。

    为什么放在这里：`files/core.py`（上传解析，在 `_read_more` 里回调）与
    `files/api.py`（记账、兜底响应）**都要用**它，而这两家是"注入解耦"的关系
    （`files/api.py` 不 import `files/core.py`）。配额记账本来就住在这一节，
    异常与它同处最自然。

    2026-09-16（§二 第 4 条）：预留原来按**客户端声明的 `Content-Length`** 记，
    于是"声明一个大值、正文不发完"就能虚占配额、把别人的上传全挤成 413；
    改成按**实际读入字节**记账后，这个异常负责"读到一半才发现配额已满"（并发挤爆）
    时终止上传，而不是把 body 白收完再判定。
    """
    pass

_quota_pending = {}          # bucket_key(str) -> int 在途字节
_quota_lock = threading.Lock()

def quota_reserve(bucket, size):
    """预留 size 在途字节（配额检查通过后、实际写入前调用）"""
    with _quota_lock:
        _quota_pending[bucket] = _quota_pending.get(bucket, 0) + int(size)

def quota_settle(bucket, reserved, actual):
    """结算：把本请求预留的 reserved 字节替换为实际落盘字节 actual（成功=预留额，
    失败/未写=0），保证账本不泄漏（异常路径也必须在 finally 中调用）"""
    with _quota_lock:
        cur = _quota_pending.get(bucket, 0) - reserved + int(actual)
        if cur <= 0: _quota_pending.pop(bucket, None)
        else: _quota_pending[bucket] = cur

def quota_pending(bucket):
    """返回 bucket 当前在途字节（配额检查方并入“已用”口径）"""
    with _quota_lock:
        return _quota_pending.get(bucket, 0)

# ========== 防抖缓存失效状态 ==========
_debounce_timer = None
_debounce_pending = False
_debounce_lock = threading.Lock()
_debounce_path = None  # 当前正在防抖的路径
_debounce_gen = 0      # 世代计数器，防止旧回调覆盖新定时器引用

def invalidate_folder_cache_smart(path):
    """
    带防抖的文件夹缓存失效函数（解决连续上传时定时器不断重置永不执行的问题）

    逻辑：
    - 首次调用：立即清除缓存，并启动 2 秒防抖定时器
    - 同一路径的后续调用：不做任何操作（不取消定时器），让第一次的定时器自然到期执行
    - 路径变化时：立即清除旧路径的缓存，再为新路径立即清除缓存并启动新定时器
    - 定时器到期后确保执行最终清除
    """
    # 这几个名字在本函数里也有赋值，缺了这个 global 声明它们就成了局部变量：
    # 下面第一次读 _debounce_path 会直接 UnboundLocalError，函数整个失效
    # （实测：安卓上传后报"失效目录缓存失败"，缓存只能靠 TTL 自然过期，
    #  表现为刚传完的文件要等几秒才出现在网页列表里）
    global _debounce_pending, _debounce_timer, _debounce_path, _debounce_gen

    def _do_invalidate(gen):
        """防抖回调：仅当 gen 与当前世代匹配时才执行清理"""
        global _debounce_pending, _debounce_timer, _debounce_path
        with _debounce_lock:
            if gen != _debounce_gen:
                return
            if _debounce_path is not None:
                invalidate_folder_cache(_debounce_path)
            _debounce_pending = False
            _debounce_timer = None
            _debounce_path = None

    with _debounce_lock:
        # 路径变化时：立即清除旧路径缓存，然后重置为新路径首次调用流程
        if _debounce_path is not None and _debounce_path != path:
            if _debounce_timer is not None:
                _debounce_timer.cancel()
                _debounce_timer = None
            invalidate_folder_cache(_debounce_path)
            _debounce_pending = False
            _debounce_path = None

        if not _debounce_pending:
            # 首次调用（或路径变化后）：立即清除 + 启动定时器
            invalidate_folder_cache(path)
            _debounce_pending = True
            _debounce_path = path
            _debounce_gen += 1
            _debounce_timer = threading.Timer(DEBOUNCE_DELAY, _do_invalidate, args=(_debounce_gen,))
            _debounce_timer.daemon = True
            _debounce_timer.start()
        # else: 同一路径的后续调用，不做任何操作，让已有定时器自然到期

# ========== 视频缩略图 ==========
FFMPEG_PATH = find_bundled_exe('ffmpeg.exe')
if FFMPEG_PATH is None:
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        FFMPEG_PATH = 'ffmpeg'   # PATH 中的全局 ffmpeg 兜底
    except Exception:
        FFMPEG_PATH = None

def has_ffmpeg():
    return FFMPEG_PATH is not None


# ---------- 缩略图后端 ----------
# 桌面有 ffmpeg；安卓没有，由 App 注册一个原生生成器（系统 API 解码图片 / 取视频帧），
# 输出同样是宽 256 的 JPEG —— 缓存、索引、失效全部复用下面现成的机制。
# 放在这里与 server.hosts.set_ip_provider 同一规矩：共享层不依赖安卓。
_native_thumb_fn = None
_native_thumb_lock = threading.Lock()


def set_native_thumbnailer(fn):
    """注册原生缩略图生成器：传一个带 generate(src_path, thumb_path) -> bool 的对象。

    安卓侧传的是 Kotlin 的 Thumbnailer 实例。注意是「对象」而不是函数 ——
    Java 对象只能调它的方法，不能像 Python 函数那样直接调用它。
    """
    global _native_thumb_fn
    with _native_thumb_lock:
        _native_thumb_fn = fn


def _native_thumbnailer():
    with _native_thumb_lock:
        return _native_thumb_fn


def thumbnail_backend():
    """当前实际可用的缩略图后端：'ffmpeg'（桌面）/ 'native'（安卓系统 API）/ ''（都没有）"""
    if FFMPEG_PATH:
        return 'ffmpeg'
    if _native_thumbnailer() is not None:
        return 'native'
    return ''


def _thumb_name(file_path):
    """缩略图落盘文件名：`sha256(归一化绝对路径)[:40] + '.jpg'`。

    **不能再用"相对路径把分隔符换成下划线"**（原实现）：那是有损变换 ——
    `users/zzv/p_s.png` 与 `users/zzv_p/s.png` 会压成同一个名字，于是两个账号的私有
    图片共用一个缓存文件。实测（2026-09-21 黑盒报告 N-1）：攻击者请求**自己的**
    `users/zzv_q/r.png` 拿到的是受害者的图，而直读受害者原图仍是 404 ——
    权限判定本身没问题，出问题的是"检查的对象"与"取用的对象"不是同一份。
    落盘名的唯一性必须来自**无损**变换，所以用路径哈希。

    归一化用 `normcase(abspath)`：Windows 路径不区分大小写，且调用点可能给出混合
    分隔符（`...\\users/zzw`）或不同盘符大小写 —— 不归一化就会为同一个文件算出两个
    不同的缓存（白占空间，且 `_delete_thumb` 删不干净）。
    """
    key = os.path.normcase(os.path.abspath(file_path))
    return hashlib.sha256(key.encode('utf-8')).hexdigest()[:40] + '.jpg'


def _is_thumb_name(name):
    """是否为本模块生成的落盘名（40 位十六进制 + `.jpg`）—— 供旧格式迁移判定"""
    return (isinstance(name, str) and len(name) == 44 and name.endswith('.jpg')
            and all(c in '0123456789abcdef' for c in name[:40]))


def _thumb_path(file_path):
    return os.path.join(THUMB_DIR, _thumb_name(file_path))

def _thumb_index():
    return os.path.join(THUMB_DIR, 'index.json')

_thumb_index_lock = threading.Lock()

# 缩略图目录统计失效节流：与 folder 缓存 TTL 对齐，短时间内多次增删缩略图
# 只真正失效一次（避免高频生成视频缩略图时反复触发 folder db 读写）
_thumb_invalidate_ts = 0.0
_thumb_invalidate_lock = threading.Lock()

def _invalidate_thumbs_throttled():
    global _thumb_invalidate_ts
    now = time.time()
    with _thumb_invalidate_lock:
        if now - _thumb_invalidate_ts < FOLDER_SIZE_TTL:
            return
        _thumb_invalidate_ts = now
    invalidate_folder_cache(THUMB_DIR)

def _thumb_index_add(file_path):
    with _thumb_index_lock:
        try:
            idx = _thumb_index()
            index = {}
            if os.path.exists(idx):
                with open(idx, 'r') as f:
                    index = json.load(f)
            # 索引值必须与 _thumb_path() 落盘名同源，否则清理时找不到文件
            index[file_path] = _thumb_name(file_path)
            with open(idx, 'w') as f:
                json.dump(index, f)
        except Exception:
            pass
    # 缩略图目录内容已变化：节流地使 THUMB_DIR 的 folder 聚合缓存失效
    _invalidate_thumbs_throttled()

def _thumb_index_remove(file_path):
    with _thumb_index_lock:
        try:
            idx = _thumb_index()
            if os.path.exists(idx):
                with open(idx, 'r') as f:
                    index = json.load(f)
                if file_path in index:
                    del index[file_path]
                with open(idx, 'w') as f:
                    json.dump(index, f)
        except Exception:
            pass
    # 缩略图目录内容已变化：节流地使 THUMB_DIR 的 folder 聚合缓存失效
    _invalidate_thumbs_throttled()

def _delete_thumb(file_path):
    thumb = _thumb_path(file_path)
    try:
        if os.path.exists(thumb):
            os.remove(thumb)
    except Exception:
        pass
    _thumb_index_remove(file_path)

_thumb_generating = set()
_thumb_generating_lock = threading.Lock()

def _thumb_fail_log(file_path, why, stderr=None):
    """缩略图生成失败要能查到 —— 原来**只有"抛异常"那条路**有日志：

    * ffmpeg **返回码非 0**（实测 1×1 的图让它报 `one of its streams received no packets`
      / `Conversion failed!`，returncode=69）
    * 返回码 0 但**没产出文件**
    * 安卓原生生成器返回失败

    这几种的表现与"正在生成中"**一模一样**（都是返回 None、都不写日志），排查时是黑洞。

    走 `add_log`（进 `leaffs.log` + 管理页「日志」，且自带控制字符清理）而不是 `print(stderr)`：
    打包/安卓运行下 stderr 去向不可靠。路径记**相对 UPLOAD_DIR** 的形式，不写绝对路径。
    """
    try:
        from leaffs.runtime_log import add_log
        tail = ''
        if stderr:
            tail = ' stderr尾部=%r' % stderr.decode('utf-8', 'replace')[-300:]
        add_log('[THUMB] 生成失败 %s：%s%s'
                % (os.path.relpath(file_path, UPLOAD_DIR), why, tail), 'warn')
    except Exception:
        # 日志通道失败不该影响缩略图流程（与 runtime_log.log_exception 同一处理）
        pass


def _generate_thumbnail(file_path):
    """同步生成单张缩略图（仅内部调用）：桌面走 ffmpeg，安卓走 App 注册的原生生成器"""
    native = _native_thumbnailer()
    if not FFMPEG_PATH and native is None:
        return None
    thumb = _thumb_path(file_path)
    try:
        r = None
        if FFMPEG_PATH:
            # 桌面：图片直接转，视频取第 1 秒一帧；两条命令都输出宽 256 的 JPEG
            ext = os.path.splitext(file_path)[1].lower()
            is_video = ext in ('.mp4', '.webm', '.mov', '.avi', '.mkv', '.flv', '.ts', '.mts',
                               '.m4v', '.3gp', '.ogv', '.wmv', '.vob', '.mpeg', '.mpg')
            if is_video:
                r = subprocess.run(
                    [FFMPEG_PATH, '-i', file_path, '-ss', '00:00:01',
                     '-vf', 'scale=256:-1', '-vframes', '1', '-q:v', '3', '-y', thumb],
                    capture_output=True, timeout=30,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
            else:
                r = subprocess.run(
                    [FFMPEG_PATH, '-i', file_path, '-vf', 'scale=256:-1',
                     '-q:v', '3', '-y', thumb],
                    capture_output=True, timeout=30,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
            if r.returncode != 0:
                _thumb_fail_log(file_path, 'ffmpeg 返回码 %s' % r.returncode, r.stderr)
        elif not native.generate(file_path, thumb):
            _thumb_fail_log(file_path, '原生生成器返回失败')
            return None
        if os.path.exists(thumb):
            _thumb_index_add(file_path)
            return thumb
        # 返回码 0 却没产出文件（同样要能查到）
        _thumb_fail_log(file_path, '没有产出文件')
    except Exception as e:
        # 不静默：生成失败要能查到（此前这里 pass，出问题时日志里一片空白）
        _thumb_fail_log(file_path, '%s: %s' % (type(e).__name__, e))
    return None

def _schedule_thumbnail(file_path):
    """后台异步生成缩略图（同一路径单飞，避免并发重复生成）"""
    if not FFMPEG_PATH and _native_thumbnailer() is None:
        return
    with _thumb_generating_lock:
        if file_path in _thumb_generating:
            return
        _thumb_generating.add(file_path)

    def _worker():
        try:
            _generate_thumbnail(file_path)
        finally:
            with _thumb_generating_lock:
                _thumb_generating.discard(file_path)

    try:
        threading.Thread(target=_worker, daemon=True).start()
    except Exception:
        with _thumb_generating_lock:
            _thumb_generating.discard(file_path)

def get_thumbnail(file_path):
    """取缩略图：命中且源文件未更新则直接返回；否则删旧图并在后台异步重新生成。

    返回 None 表示暂无可用缩略图（正在生成/没有可用后端），调用方应显示占位。
    """
    thumb = _thumb_path(file_path)
    try:
        src_st = os.stat(file_path)
    except OSError:
        return None
    try:
        th_st = os.stat(thumb)
        # 源文件 mtime 未晚于缩略图 → 缓存有效
        if th_st.st_mtime >= src_st.st_mtime:
            return thumb
    except OSError:
        pass
    # 缓存缺失或源文件已更新：移除旧图，后台重新生成（不阻塞请求）
    if os.path.exists(thumb):
        try:
            os.remove(thumb)
        except Exception:
            pass
    _thumb_index_remove(file_path)
    _schedule_thumbnail(file_path)
    return None

def delete_fail_reason(e):
    """把删除失败的真实成因翻成一句**能指导用户**的话（LF-23）。

    原来 HTTP 与 WS 两条路都写死成 `'文件被占用或权限不足'` —— 那是**两种完全不同的原因**：
    前者关掉占用它的程序就能删，后者是只读/权限问题，处置方式完全不同。
    混在一句里，用户既判断不出是哪种、也不知道该做什么。

    （顺带：那句文案此前还根本显示不出来 —— 前端只读 `success`，而它恒为 true。）
    """
    we = getattr(e, 'winerror', None)
    en = getattr(e, 'errno', None)
    if we == 32 or en == errno.EBUSY:                  # ERROR_SHARING_VIOLATION：被别的进程用着
        return '文件正被其他程序占用（关掉占用它的程序后重试）'
    if we == 5 or en in (errno.EACCES, errno.EPERM):   # ERROR_ACCESS_DENIED：权限/只读
        return '权限不足（只读或受系统保护）'
    if en == errno.ENOTEMPTY:
        return '目录非空，无法删除'
    if en == errno.ENOENT:
        return '文件已不存在'
    if en == errno.EISDIR:
        return '目标是目录'
    return '删除失败：%s' % (e,)


def cleanup_upload_tmp():
    """清掉上传临时目录里的残留（LF-26）。

    正常路径下临时文件会被 finally 删掉、或被 os.replace 落位成正式文件；
    但**进程被 kill / 崩溃**时，正在写的那个会留在原地，而且没有任何地方记账 ——
    再加上这个目录对用户不可见（LF-22），不清就成了"看不见也删不掉"的隐形垃圾。

    **只在启动阶段调用**：那一刻必然没有任何在途上传，"整个目录清空"是安全的。
    **绝不在运行期调用** —— 运行期清它会把正在上传的文件干掉
    （这正是 `.part` 暴露在列表里时、用户手删导致那次上传失败的那个场景）。
    """
    removed = 0
    try:
        if not os.path.isdir(UPLOAD_TMP_DIR):
            return 0
        for name in os.listdir(UPLOAD_TMP_DIR):
            p = os.path.join(UPLOAD_TMP_DIR, name)
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                    removed += 1
                else:
                    os.remove(p)
                    removed += 1
            except Exception:
                pass
    except Exception:
        pass
    return removed


def cleanup_orphan_thumbs():
    """
    启动时快速清理缩略图（采样检测 + 后台全量扫描）
    不阻塞服务器启动，采样判断是否需要后台清理
    """
    map_file = _thumb_index()
    if not os.path.exists(map_file):
        return

    with _thumb_index_lock:
        try:
            with open(map_file, 'r') as f:
                index = json.load(f)
            if not index:
                try:
                    os.remove(map_file)
                except Exception:
                    pass
                return

            # 落盘名迁移（N-1，2026-09-21）：旧名是"相对路径 + 分隔符换下划线"的**有损**
            # 变换，可能两两撞名（见 _thumb_name 文档）。改名后旧文件已无人引用，但
            # cleanup 的判据是"源文件是否还在" —— 源文件都在，所以它们**不会被下面的
            # 逻辑清掉**，只会永久占着磁盘。这里按落盘名格式识别旧条目、连文件一起删，
            # 让它们按新规则重新生成（缩略图本来就是可重建的缓存）。
            # 幂等：跑过一次之后索引里全是新格式，stale 恒为空。
            stale = [(k, v) for k, v in index.items() if not _is_thumb_name(v)]
            if stale:
                for _src, _name in stale:
                    # 只删 THUMB_DIR 直接子项：索引是可写的本地文件，损坏/被改过时
                    # 不能让一个带分隔符的值把删除动作引到目录外
                    if isinstance(_name, str) and os.path.basename(_name) == _name:
                        try:
                            _old = os.path.join(THUMB_DIR, _name)
                            if os.path.exists(_old):
                                os.remove(_old)
                        except Exception:
                            pass
                    index.pop(_src, None)
                try:
                    with open(map_file, 'w') as f:
                        json.dump(index, f)
                except Exception:
                    pass
                if not index:
                    try:
                        os.remove(map_file)
                    except Exception:
                        pass
                    return

            # 采样检测
            items = list(index.items())
            sample_size = max(1, int(len(items) * THUMB_SAMPLE_RATIO))
            sample = items[:sample_size]

            missing = 0
            for src_path, _ in sample:
                if not os.path.exists(src_path):
                    missing += 1

            missing_ratio = missing / sample_size

            if missing_ratio < THUMB_MISS_THRESHOLD:
                # 问题不严重：仅清理采样中缺失的条目
                for src_path, thumb_name in sample:
                    if not os.path.exists(src_path):
                        thumb_path = os.path.join(THUMB_DIR, thumb_name)
                        try:
                            if os.path.exists(thumb_path):
                                os.remove(thumb_path)
                        except Exception:
                            pass
                        del index[src_path]
                try:
                    with open(map_file, 'w') as f:
                        json.dump(index, f)
                except Exception:
                    pass
                return

            # 缺失比例高 → 启动后台线程全量扫描
            print(f"缩略图索引异常（缺失 {int(missing_ratio * 100)}%），启动后台清理...")
            _start_background_cleanup(index, items)

        except Exception:
            # 索引损坏，直接删除重建
            try:
                os.remove(map_file)
            except Exception:
                pass


def _start_background_cleanup(index, items):
    """后台线程：分批扫描，逐步清理孤立缩略图"""
    def _worker():
        # 先标记需要删除的条目
        to_delete = []
        for src_path, thumb_name in items:
            if not os.path.exists(src_path):
                to_delete.append((src_path, thumb_name))

        # 分批删除，避免占用过多 IO
        for i in range(0, len(to_delete), THUMB_SCAN_BATCH):
            batch = to_delete[i:i + THUMB_SCAN_BATCH]
            for src_path, thumb_name in batch:
                thumb_path = os.path.join(THUMB_DIR, thumb_name)
                try:
                    if os.path.exists(thumb_path):
                        os.remove(thumb_path)
                except Exception:
                    pass
                # 在锁内修改索引
                with _thumb_index_lock:
                    if src_path in index:
                        del index[src_path]

            # 每批完成后更新索引文件（防止中断丢失进度）
            with _thumb_index_lock:
                try:
                    with open(_thumb_index(), 'w') as f:
                        json.dump(index, f)
                except Exception:
                    pass

            time.sleep(THUMB_SCAN_INTERVAL)

        # 如果索引为空，删除文件
        with _thumb_index_lock:
            if not index:
                try:
                    os.remove(_thumb_index())
                except Exception:
                    pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

# ========== 断线异常集合 ==========
DISCONNECTED_EXCEPTIONS = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError)

# ========== 格式化工具 ==========
def format_size(b):
    if not b or b == 0:
        return '0 B'
    u = ['B', 'KB', 'MB', 'GB', 'TB']
    i = 0
    while b >= 1024 and i < len(u) - 1:
        b /= 1024
        i += 1
    return ('%.1f' % b).rstrip('0').rstrip('.') + ' ' + u[i] if isinstance(b, float) else str(int(b)) + ' ' + u[i]

def format_time(ts):
    if not ts:
        return ''
    d = time.localtime(ts)
    return '%02d-%02d %02d:%02d' % (d.tm_mon, d.tm_mday, d.tm_hour, d.tm_min)