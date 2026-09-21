#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
file_system.py - File system management layer
Unified file operations: browse, upload, download, delete, search
"""

import os
import json
import shutil
import threading
import time
import tempfile
import zipfile as _zipfile

from leaffs.utils.core import (
    BASE_DIR, UPLOAD_DIR, CACHE_DIR, THUMB_DIR, COPY_BUFFER_SIZE,
    UPLOAD_TMP_DIR, is_upload_tmp_entry,
    safe_path, abs_path, get_mime, esc_html,
    read_file_cached, invalidate_file_cache,
    get_folder_size, get_folder_stats,
    invalidate_folder_cache, invalidate_folder_cache_smart,
    has_ffmpeg, thumbnail_backend, _delete_thumb, cleanup_orphan_thumbs, get_thumbnail,
    cleanup_upload_tmp, delete_fail_reason,
    has_windows_device_name,
    UploadQuotaExceeded,
)

from leaffs.runtime_log import log_exception as _log_exc


def check_path_permission_core(role, username, path, guest_mode=True):
    """路径访问权限判定（HTTP 与 WS 共用；原 leaffs.py 内联，重构移入 files 域）。

    admin/super_admin 全通；游客/匿名仅在游客模式开启时可访问 public；
    普通用户可访问自己的 users/<name>/ 与 public/。路径含 .. / 绝对路径一律拒绝。
    """
    if path:
        # 先检查原始路径中是否包含 ..（必须在 normpath 之前检查）
        raw_parts = path.replace('\\', '/').split('/')
        if '..' in raw_parts:
            return False
        if raw_parts and raw_parts[0] in ('..', '.'):
            return False
        # 规范化路径
        norm_path = os.path.normpath(path).replace('\\', '/')
        # 禁止绝对路径
        if norm_path.startswith('/'):
            return False
        # 标准化后再次检查
        if norm_path in ('..', '../') or norm_path.startswith('../'):
            return False
        path = norm_path
    if role in ('super_admin', 'admin'):
        return True
    # public/shares 是分享的**虚拟区**（只由映射表登记，磁盘上不放实体）：
    # 普通用户/游客能往 public/ 写，只要在那儿建出实体 public/shares/<用户名>/ 目录，
    # 里面的文件就绕过了分享码（列表按磁盘条目优先、下载按磁盘路径）。这里一律拒绝。
    # （admin 放行：他本来就能读全部，也要留一条清理污染的路。）
    if path == 'public/shares' or path.startswith('public/shares/'):
        return False
    # 游客/匿名：仅在游客模式开启时可访问 public；关闭后一律拒绝（防绕过 UI 直接调 API/WS）
    if role == 'guest' or not username:
        if not guest_mode:
            return False
        return path.startswith('public/') or path == 'public'
    return (path.startswith(f'users/{username}/') or path == f'users/{username}'
            or path.startswith('public/') or path == 'public')


def get_user_dir(username):
    if not username: return None
    folder = os.path.join(UPLOAD_DIR, 'users', username)
    os.makedirs(folder, exist_ok=True)
    return folder

def get_user_start_path(username, role):
    """返回用户登录后的默认起始路径"""
    if role == 'guest':
        # 游客不区分用户名，一律从 public 开始
        return 'public'
    if role in ('super_admin', 'admin'):
        if username:
            return 'users/' + username
        return ''
    if username: return 'users/' + username
    return 'public'

# Windows 保留设备名（大小写不敏感；含带扩展名，如 CON.txt —— 按第一个 '.' 前主段判定）
_WIN_RESERVED_DEVICE_NAMES = frozenset((
    'CON', 'PRN', 'AUX', 'NUL',
    'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
    'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9',
))


def _is_windows_reserved_name(name):
    """按 Windows 保留设备名判定名称主段（第一个 '.' 之前，大小写不敏感）。

    命中 CON/PRN/AUX/NUL/COM1-9/LPT1-9（含带扩展名，如 CON.txt、com1.PDF）→ True；
    普通名称（中文/emoji/多点名如 a.b.txt、console.txt、COM10.txt）不受影响。
    """
    if not isinstance(name, str) or not name:
        return False
    main = name.split('.', 1)[0].upper()
    return main in _WIN_RESERVED_DEVICE_NAMES


def sanitize_entry_name(name):
    """只允许普通文件名：拒绝空、. 与 ..、路径分隔符 / \\、冒号、控制字符、超长(>200)。

    附加卫生：① Windows 保留设备名（CON/PRN/AUX/NUL/COM1-9/LPT1-9，含带扩展名，
    按 '.' 前主段判定）；② 显式拒绝 CR/LF（\r \n，与下方 ord<32 控制字符检查叠加，
    防后续放宽控制字符过滤时回归）。中文/emoji 名不受影响。
    """
    if not isinstance(name, str) or not name:
        return None
    # 反斜杠统一视为路径分隔符，直接拒绝（不尝试兼容）
    if '\\' in name:
        return None
    # 路径分隔符 / 与冒号（Windows 盘符/ADS）一律拒绝
    if any(ch in name for ch in '/\\:'):
        return None
    # 拒绝 . 与 .. 以及任何包含 .. 的名字
    if name in ('.', '..') or '..' in name:
        return None
    # 拒绝 Windows 保留设备名（含带扩展名，如 CON.txt）
    if _is_windows_reserved_name(name):
        return None
    # 拒绝 CR/LF：显式声明（现状 ord<32 控制字符检查已覆盖，此处防回归）
    if '\r' in name or '\n' in name:
        return None
    # 拒绝控制字符
    if any(ord(ch) < 32 for ch in name):
        return None
    # 拒绝超长文件名
    if len(name) > 200:
        return None
    return name

def _normalize_rel_path(rel_path):
    """规范化相对路径并禁止路径穿越"""
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
    # 标准化后再次检查
    if norm in ('..', '../') or norm.startswith('../'):
        return None
    # 禁止 Win32 保留设备名（NUL/CON/COM1…）：os.path.exists 对它们返回 True，
    # 于是能混过"文件是否存在"的检查、到下游才炸（N-3）
    if has_windows_device_name(norm):
        return None
    return norm

def _count_files_recursive(path):
    """递归统计指定路径下的文件总数和总大小

    C-03：list_files 已改走 folder 聚合缓存（get_folder_stats），本函数无调用点，
    仅保留给将来需要“无缓存强制实时递归”的场景使用。
    """
    total_files = 0
    total_size = 0
    try:
        for entry in os.scandir(path):
            try:
                if entry.is_dir(follow_symlinks=False):
                    sub_files, sub_size = _count_files_recursive(entry.path)
                    total_files += sub_files
                    total_size += sub_size
                else:
                    total_files += 1
                    total_size += entry.stat(follow_symlinks=False).st_size
            except Exception:
                pass
    except Exception:
        pass
    return total_files, total_size


def list_files(rel_path):
    # 额外规范化防御
    norm_path = _normalize_rel_path(rel_path)
    if norm_path is None:
        return None, 'Permission error'
    rel_path = norm_path
    full = abs_path(rel_path)
    if full is None: return None, 'Permission error'
    if not os.path.exists(full): return {'current_path': rel_path or '', 'files': []}, None
    files = []
    try:
        with os.scandir(full) as it:
            for entry in it:
                # LF-22：上传临时目录不对用户可见（它装的是"写到一半"的数据）
                if is_upload_tmp_entry(entry.name):
                    continue
                rp = os.path.join(rel_path, entry.name).replace('\\', '/') if rel_path else entry.name
                try:
                    if entry.is_dir(follow_symlinks=False):
                        files.append({'name': entry.name, 'path': rp, 'type': 'folder',
                                      'size': get_folder_size(entry.path),
                                      'mtime': entry.stat(follow_symlinks=False).st_mtime})
                    else:
                        files.append({'name': entry.name, 'path': rp, 'type': 'file',
                                      'size': entry.stat(follow_symlinks=False).st_size,
                                      'mtime': entry.stat(follow_symlinks=False).st_mtime})
                except Exception: pass
    except OSError:
        # 只接 PermissionError 是不够的：递一个**文件**路径进来抛的是 NotADirectoryError，
        # 而它的消息里带完整绝对路径 —— 会一路逃到 HTTP 兜底被回显
        # （黑盒渗透测试正是靠这个把服务器目录布局还原出来的）。
        return None, '无法读取该目录'
    files.sort(key=lambda x: (x['type'] != 'folder', x['name'].lower()))
    # C-03：递归统计改走 folder 聚合缓存（get_folder_stats，TTL 5s + 变更即失效），
    # 去掉每次列表都全盘递归的 DoS 面；与目录项逐项 size 语义冲突处以缓存聚合为准。
    # （目录项 size 同样来自 get_folder_size 缓存，同源一致；首次/失效后扫描同旧成本。）
    try:
        _stats = get_folder_stats(full)
        total_files = _stats.get('files', 0) if _stats else 0
        total_size = _stats.get('size', 0) if _stats else 0
    except Exception:
        total_files, total_size = 0, 0
    return {'current_path': rel_path or '', 'files': files, 'total_file_count': total_files, 'total_size_sum': total_size}, None

def delete_paths(paths):
    """删除文件/目录，返回 (deleted, failed)。

    C-07：逐项 try 并如实收集失败原因（句柄占用/权限等不再静默计入成功），
    failed 为 [(rel_path, reason)] 列表，供 HTTP/WS 调用方逐条反馈。
    WS 调用点（leaffs.py ws_handler 'delete' 分支，A-05 工作面）需按两元组适配。
    """
    deleted = 0
    failed = []        # [(rel_path, reason)]
    for rel_path in paths:
        # 规范化路径防御
        norm_path = _normalize_rel_path(rel_path)
        if norm_path is None:
            continue
        rel_path = norm_path
        full = abs_path(rel_path)
        if full is None: continue
        if not os.path.exists(full): continue
        try:
            parent = os.path.dirname(full)
            if os.path.isfile(full):
                _delete_thumb(full)
                os.remove(full)
                invalidate_file_cache(full)
                invalidate_folder_cache(parent)
            else:
                for root, _, files in os.walk(full):
                    for f in files: _delete_thumb(os.path.join(root, f))
                shutil.rmtree(full)
                invalidate_folder_cache(full, recursive=True)
            if parent and parent != full: invalidate_folder_cache(parent)
            deleted += 1
        except PermissionError as e:
            # LF-23：精确区分"被占用"与"权限不足" —— 原来混成一句，用户判断不了也做不了
            failed.append((rel_path, delete_fail_reason(e)))
        except Exception as e:
            failed.append((rel_path, delete_fail_reason(e)))
    return deleted, failed

def mkdir(rel_path, name):
    # 名称净化：拒绝 / \\ : . .. 控制字符与超长
    if sanitize_entry_name(name) is None:
        return False, '名称不合法'
    # 规范化路径防御
    if rel_path:
        norm_path = _normalize_rel_path(rel_path + '/')
        if norm_path is None:
            return False, 'Permission error'
        rel_path = norm_path.rstrip('/')
    full = os.path.join(UPLOAD_DIR, rel_path, name) if rel_path else os.path.join(UPLOAD_DIR, name)
    if not safe_path(UPLOAD_DIR, full): return False, 'Permission error'
    try:
        os.makedirs(full, exist_ok=True)
        invalidate_folder_cache(os.path.dirname(full))
        return True, None
    except Exception as e:
        _log_exc('创建目录', e)
        return False, '创建目录失败'

def search_files(query):
    query = query.lower().strip()
    if not query: return []
    results = []
    try:
        for root, dirs, files in os.walk(UPLOAD_DIR):
            # LF-22：直接剪枝，不进上传临时目录（省得白遍历一遍再过滤）
            dirs[:] = [d for d in dirs if not is_upload_tmp_entry(d)]
            for fname in files:
                if query in fname.lower():
                    full = os.path.join(root, fname)
                    rel = os.path.relpath(full, UPLOAD_DIR).replace('\\', '/')
                    try:
                        st = os.stat(full)
                        results.append({'name': fname, 'path': rel, 'type': 'file', 'size': st.st_size, 'mtime': st.st_mtime})
                    except Exception: pass
    except Exception: pass
    results.sort(key=lambda x: x['name'].lower())
    return results

def get_server_stats():
    """服务器文件统计 —— 把共享根当“一个大文件夹”读取 folder 聚合缓存

    大小/文件数/文件夹数在目录扫描时一次同源取得并缓存（get_folder_stats）；
    子路径（users/public）按各自条目复用，根条目即整树聚合。
    文件树变更经 invalidate_folder_cache 使对应路径失效后自动重算，
    这里没有任何独立的全盘扫描逻辑。
    """
    users_dir = os.path.join(UPLOAD_DIR, 'users')
    public_dir = os.path.join(UPLOAD_DIR, 'public')
    empty = {'size': 0, 'files': 0, 'folders': 0}
    total = get_folder_stats(UPLOAD_DIR) if os.path.isdir(UPLOAD_DIR) else empty
    users = get_folder_stats(users_dir) if os.path.isdir(users_dir) else empty
    public = get_folder_stats(public_dir) if os.path.isdir(public_dir) else empty
    thumbs = get_folder_stats(THUMB_DIR) if os.path.isdir(THUMB_DIR) else empty
    # 排除目录索引文件 index.json（它不属于缩略图）
    thumb_count = thumbs['files']
    thumb_size = thumbs['size']
    idx_path = os.path.join(THUMB_DIR, 'index.json')
    if os.path.isfile(idx_path):
        try:
            thumb_count = max(0, thumb_count - 1)
            thumb_size -= os.path.getsize(idx_path)
        except Exception:
            pass
    return {
        'file_count': total['files'], 'folder_count': total['folders'],
        'total_size': total['size'],
        'thumb_count': thumb_count, 'thumb_size': thumb_size,
        'users_used': users['size'], 'public_used': public['size'],
        'total_used': total['size'],
    }

def _parse_boundary(content_type):
    for part in content_type.split(';'):
        p = part.strip()
        if p.startswith('boundary='):
            b = p[9:].strip()
            if b.startswith('"') and b.endswith('"'): b = b[1:-1]
            return b
    return None


class _UploadSizeLimit(Exception):
    """服务端上传累计写入超过大小上限，终止整个上传"""


def _split_upload_relpath(filename):
    """解析浏览器“上传文件夹”的多段相对路径（如 a/b.txt），逐段净化。

    规则：先把 '\\\\' 统一替换为 '/' 后按 '/' 分段并滤掉空段；任一段
    （=='.'/'..'、含 '..'、含 '/' 或 '\\\\' 或 ':'、超长 >200、含控制字符
    ord<32、Windows 保留设备名 CON/PRN/AUX/NUL/COM1-9/LPT1-9（含带扩展名）、
    CR/LF）或总段数 >20 均整体拒绝，返回 None。合法则返回净化后的段列表。
    sanitize_entry_name（纯单文件名、拒绝任何路径分隔符）保持不变，
    继续供 mkdir 等仅需单个名称的接口使用。
    """
    if not isinstance(filename, str) or not filename:
        return None
    fn = filename.replace('\\', '/')
    parts = [seg for seg in fn.split('/') if seg]
    if not parts or len(parts) > 20:
        return None
    for seg in parts:
        if seg in ('.', '..') or '..' in seg:
            return None
        if any(ch in seg for ch in '/\\:'):
            return None
        if len(seg) > 200:
            return None
        # Windows 保留设备名（含带扩展名，目录段同样拒绝，如 CON/note.txt）
        if _is_windows_reserved_name(seg):
            return None
        # 显式拒绝 CR/LF（\r \n，与下方 ord<32 控制字符检查叠加，防回归）
        if '\r' in seg or '\n' in seg:
            return None
        if any(ord(ch) < 32 for ch in seg):
            return None
    return parts


# R2 自动改名（guest 上传承接“覆盖”→自动改名，绝不覆盖）：
# 目标已存在时按 名_1.ext、名_2.ext… 探测空位，最多尝试该次数（含原名本身）。
_AUTO_UNIQUE_MAX = 1000


def _auto_unique_candidate(full_path, seq):
    """生成自动改名候选绝对路径：a/b.txt → a/b_1.txt（seq>=1）；无扩展名时直接追加 _seq"""
    directory, base = os.path.split(full_path)
    name, ext = os.path.splitext(base)
    new_base = '{0}_{1}{2}'.format(name, seq, ext)
    return os.path.join(directory, new_base) if directory else new_base


def handle_upload(rfile, content_type, content_length, sub_path, auto_unique=False,
                  on_bytes=None):
    """multipart 流式上传解析与落盘。

    C-05：不再直写目标文件——先写唯一 .part 临时文件，全部字节写入成功后原子落位
    （os.replace/os.rename）；任何异常（含 _UploadSizeLimit/连接断开）在 finally 清理
    .part，不残留半截文件。并发同名上传各自 .part，落位时后写者原子覆盖（文件体完整）。
    R2：auto_unique=True 时（guest 上传路径传 True）目标已存在绝不覆盖，按 名_1.ext、
    名_2.ext… 探测空位（Windows 下 os.rename 目标已存在抛 FileExistsError，天然不覆盖），
    全部候选被占则报错、不计入 saved。

    on_bytes：可选的 `(读入字节数) -> None` 回调，**每从 socket 读入一块就调一次**
    （在唯一的读取点 `_read_more` 里）。2026-09-16（§二 第 4 条）用它把配额预留
    从"按客户端声明的 Content-Length"改成"按**实际读入**的字节"。
    **回调可以抛 `UploadQuotaExceeded` 表示"已经超配额"，此时分两种落点：**
      · 抛出点在**单文件处理块内**（正在写某个 part）—— 本函数接住，记一条带文件名的错、
        结束整个上传并返回（半截 `.part` 由既有的 `finally` 清理，不落位）；
      · 抛出点在**块外**（主循环里解析 boundary / part 头期间）—— 异常**直接冒给调用方**，
        由调用方按"超配额"回响应（`files/api.py` 就是这么做的，回 413）。
    """
    boundary = _parse_boundary(content_type)
    if not boundary: return 0, ['Cannot parse Content-Type']
    # 目标目录不存在时自动创建：前端“选择已有目录”正常流程目录必在，但 API/自定义
    # 场景可上传到尚未存在的子路径（如 path=xxx/新建目录），不建目录则写 .part 直接失败。
    # 此处已过 fs_api 的权限/路径校验（write_allowed/_check_path_permission/R2 根权限），
    # 但建目录前仍要再确认落在共享根内：盘符路径（C:/…）在 Windows 上会让 join 直接跳出去
    target_root = os.path.join(UPLOAD_DIR, sub_path) if sub_path else UPLOAD_DIR
    from leaffs.utils.core import safe_path as _safe_path
    if not _safe_path(UPLOAD_DIR, target_root):
        return 0, ['路径不合法']
    try:
        os.makedirs(target_root, exist_ok=True)
    except Exception:
        return 0, [f'无法创建目标目录: {sub_path or "/"}']
    # LF-22：临时文件落在 UPLOAD_TMP_DIR（import 期已建，但用户可能把它删了）——
    # 这里建不出来就如实失败，别等写临时文件时才炸出一句看不懂的错
    try:
        os.makedirs(UPLOAD_TMP_DIR, exist_ok=True)
    except Exception:
        return 0, ['无法创建上传临时目录']
    boundary_bytes = ('--' + boundary).encode('latin-1')
    end_boundary = ('--' + boundary + '--').encode('latin-1')
    saved = 0; errors = []; remaining = content_length
    buf = b''
    # 服务端累计写入上限（0 = 不限，是**合法配置值**）；读流期间实时拦截越过上限的请求。
    # 不吞异常：拿不到上限就退化成 0，等于把"读配置出错"读成"用户要求不限" ——
    # 宁可让异常把这次上传打断，由调用方按失败处理。
    from leaffs.config import core as _cc
    CHUNK = max(4096, int(_cc.get_upload_chunk()))
    UMAX = int(_cc.get_upload_max_size())
    up_total = 0

    def _read_more():
        nonlocal buf, remaining
        if remaining <= 0: return False
        chunk_size = min(CHUNK, remaining)
        chunk = rfile.read(chunk_size)
        if not chunk: return False
        # §二 第 4 条：按**实际读入**的字节回调（配额记账 + 实时校验）。
        # 回调可以抛 UploadQuotaExceeded 中断整个上传 —— 见本函数 docstring。
        if on_bytes is not None:
            on_bytes(len(chunk))
        buf += chunk; remaining -= len(chunk)
        return True

    def _skip_part_content():
        """丢弃当前 part 的正文直到下一个 boundary（被拒绝/出错的文件用）"""
        nonlocal buf
        buf = buf[head_end + 4:]
        while True:
            idx = buf.find(boundary_bytes)
            if idx >= 0:
                buf = buf[idx:]
                return
            if not _read_more():
                buf = b''
                return

    def _write_bounded(f, data):
        """带服务端累计上限的写盘：超限抛 _UploadSizeLimit 终止整个上传"""
        nonlocal up_total
        if UMAX > 0 and up_total + len(data) > UMAX:
            raise _UploadSizeLimit()
        if data:
            f.write(data)
            up_total += len(data)

    while True:
        idx = buf.find(boundary_bytes)
        if idx >= 0:
            header_start = buf.find(b'\r\n', idx)
            if header_start < 0:
                if not _read_more(): break
                continue
            head_end = buf.find(b'\r\n\r\n', header_start + 2)
            if head_end < 0:
                if not _read_more(): break
                if len(buf) > 65536: errors.append('Header too large'); return saved, errors
                continue
            header_part = buf[header_start + 2:head_end].decode('latin-1', errors='replace')
            filename = None
            for line in header_part.split('\r\n'):
                if line.lower().startswith('content-disposition:'):
                    for seg in line.split(';'):
                        seg = seg.strip()
                        if seg.lower().startswith('filename='):
                            raw = seg[9:].strip().strip('"')
                            if raw.startswith("'") and raw.endswith("'"): raw = raw[1:-1]
                            if "UTF-8''" in raw or "utf-8''" in raw:
                                import urllib.parse
                                raw = urllib.parse.unquote(raw.split("''", 1)[1])
                            else:
                                # 修复 latin-1 解码导致的中文乱码：
                                # header 被 latin-1 解码后，UTF-8 中文变成了一堆 latin-1 字符，
                                # 重新用 latin-1 编码回字节，再用 UTF-8 解码即可还原
                                try:
                                    raw_bytes = raw.encode('latin-1')
                                    raw = raw_bytes.decode('utf-8')
                                except (UnicodeEncodeError, UnicodeDecodeError):
                                    pass
                            filename = raw; break
            if not filename:
                _skip_part_content()
                continue
            # 浏览器“上传文件夹”携带 webkitRelativePath（如 a/b.txt），此处恢复多段上传：
            # 先逐段净化（任一段非法则整体拒绝），再在 target_dir 下按段建子目录，
            # 保存位置仍只由请求的 sub_path 决定。
            segs = _split_upload_relpath(filename)
            if segs is None:
                errors.append(f'{filename}: 文件名不合法')
                _skip_part_content()
                continue
            target_dir = os.path.join(UPLOAD_DIR, sub_path) if sub_path else UPLOAD_DIR
            sub_dir = target_dir
            if len(segs) > 1:
                try:
                    for s in segs[:-1]:
                        sub_dir = os.path.join(sub_dir, s)
                    os.makedirs(sub_dir, exist_ok=True)
                except Exception:
                    errors.append(f'{filename}: 无法创建子目录')
                    _skip_part_content()
                    continue
            full_path = os.path.join(sub_dir, segs[-1])
            if not safe_path(UPLOAD_DIR, full_path):
                errors.append(f'{filename}: Unsafe path')
                _skip_part_content()
                continue
            # realpath 前缀校验：确保多段拼接后的目标（含可能的符号链接解析）仍在 target_dir 内
            try:
                real_target = os.path.realpath(target_dir)
                real_full = os.path.realpath(full_path)
                if not (real_full == real_target or real_full.startswith(real_target + os.sep)):
                    errors.append(f'{filename}: 目录越界')
                    _skip_part_content()
                    continue
            except Exception:
                errors.append(f'{filename}: 路径校验失败')
                _skip_part_content()
                continue
            buf = buf[head_end + 4:]
            # C-05：先写唯一临时文件，全部字节成功后再原子落位；异常清理见 finally。
            # LF-22：临时文件写在 UPLOAD_DIR/.uploads/ 下，**不在共享目录里** ——
            # 原来写成 `<目标>.part.<tid>.<ns>` 直接躺在目标目录，于是 列表/搜索/大小统计
            # 都能看见它、还能被直链下载（用户看到"一个正在写入、大小还在涨的怪文件"）。
            # 落位仍是 os.replace：同盘、原子。文件名保留 .part 后缀只为排障好认。
            part_path = os.path.join(
                UPLOAD_TMP_DIR,
                '{0}-{1}.part'.format(threading.get_ident(), time.time_ns()))
            try:
                truncated = False      # LF-24：本次 part 是否被截断（见下面数据耗尽那个分支）
                try:
                    with open(part_path, 'wb') as f:
                        written = 0
                        while True:
                            idx = buf.find(boundary_bytes)
                            if idx >= 0:
                                write_data = buf[:idx]
                                if write_data.endswith(b'\r\n'): write_data = write_data[:-2]
                                _write_bounded(f, write_data); written += len(write_data)
                                buf = buf[idx:]; break
                            idx_end = buf.find(end_boundary)
                            if idx_end >= 0:
                                write_data = buf[:idx_end]
                                if write_data.endswith(b'\r\n'): write_data = write_data[:-2]
                                _write_bounded(f, write_data); written += len(write_data)
                                buf = buf[idx_end:]; break
                            safe_len = len(buf) - len(boundary_bytes) - 4
                            if safe_len > 0: _write_bounded(f, buf[:safe_len]); buf = buf[safe_len:]
                            if not _read_more():
                                # LF-24：数据耗尽却始终没见到 multipart 边界 = 对端提前断开
                                # （浏览器刷新 / 关标签 / 源文件被删都走这条），或 body 本身被截断。
                                # 格式完好的 body 必然以 `--boundary--` 结束，所以"没见到边界"
                                # 就是这个 part 不完整的判据 —— 比区分 FIN/RST 更稳
                                # （实测：直接 close 也走 EOF，不是 RST）。
                                # 原来这里与"遇到边界"那两个分支共用同一段落位逻辑，于是半截数据
                                # 照样 os.replace 落位、saved += 1、还回 success:true。
                                truncated = True
                                if buf: _write_bounded(f, buf); buf = b''
                                break
                    if truncated:
                        # LF-24：不落位 —— .part 交给 finally 清掉，saved 不增，
                        # 并如实记错（LF-23 之后前端会把这句话显示出来）
                        errors.append(f'{filename}: 上传中断，数据不完整（已丢弃）')
                    elif auto_unique:
                        # R2：guest 上传绝不覆盖——目标已存在则按 名_1.ext、名_2.ext… 探测
                        # 空位（最多 _AUTO_UNIQUE_MAX 个候选），探测与落位一体原子。
                        placed = False
                        for seq in range(_AUTO_UNIQUE_MAX + 1):
                            cand = full_path if seq == 0 else _auto_unique_candidate(full_path, seq)
                            try:
                                if os.path.exists(cand):
                                    continue
                                os.rename(part_path, cand)
                                placed = True
                                break
                            except FileExistsError:
                                continue
                        if placed:
                            saved += 1
                        else:
                            errors.append(f'{filename}: 同名文件过多，自动改名失败（最多尝试 {_AUTO_UNIQUE_MAX} 个候选名）')
                    else:
                        # 非 guest：保留原覆盖语义（os.replace 原子覆盖/新建一致）
                        os.replace(part_path, full_path)
                        saved += 1
                finally:
                    # 任何异常（含 _UploadSizeLimit/连接断开/落位失败）都清理 .part，不残留
                    try:
                        if os.path.exists(part_path):
                            os.remove(part_path)
                    except Exception:
                        pass
            except _UploadSizeLimit:
                # 达到服务端累计写入上限：本文件记错并停止继续写入/接收
                errors.append(f'{filename}: 上传超过大小上限')
                return saved, errors
            except UploadQuotaExceeded as _qe:
                # §二 第 4 条附加项：读流期间实时校验发现超额（并发挤爆）——
                # 立刻停，别把剩下的 body 白收完再判定。
                errors.append(f'{filename}: 超过可用配额（{_qe}），已停止接收')
                return saved, errors
            except Exception as e:
                _log_exc('保存上传文件 %s' % filename, e)
                errors.append(f'{filename}: 写入失败')
                # buf 已进入正文切片，直接从当前位置推进到下一个 boundary
                while True:
                    idx = buf.find(boundary_bytes)
                    if idx >= 0: buf = buf[idx:]; break
                    if not _read_more(): buf = b''; break
        else:
            if not _read_more(): break
    return saved, errors

def create_zip(file_list, base_path):
    fd, zip_path = tempfile.mkstemp(suffix='.zip')
    os.close(fd)
    try:
        with _zipfile.ZipFile(zip_path, 'w', _zipfile.ZIP_DEFLATED) as zf:
            for rel_path in file_list:
                # 规范化路径防御
                norm_path = _normalize_rel_path(rel_path)
                if norm_path is None:
                    continue
                rel_path = norm_path
                full = os.path.join(UPLOAD_DIR, rel_path)
                if not safe_path(UPLOAD_DIR, full): continue
                if not os.path.exists(full): continue
                base_full = os.path.join(UPLOAD_DIR, base_path) if base_path else UPLOAD_DIR
                arcname = os.path.relpath(full, base_full)
                if os.path.isfile(full):
                    zf.write(full, arcname)
                elif os.path.isdir(full):
                    for root, dirs, files in os.walk(full):
                        for file in files:
                            fp = os.path.join(root, file)
                            zf.write(fp, os.path.relpath(fp, base_full))
        return zip_path
    except Exception:
        try: os.unlink(zip_path)
        except: pass
        return None