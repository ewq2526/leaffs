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
import html as html_mod
from collections import OrderedDict
import sys

# ===================================================
# 运行时目录定位（不写死任何绝对路径，兼容源码运行与打包运行）
# ---------------------------------------------------
# 设计：
#   * 源码运行（python -m leaffs）：可写数据根 = 项目根（leaffs 包的上层），
#     资源根（web_page、内置 exe、默认数据）= leaffs 包目录。
#   * 打包运行（PyInstaller/Nuitka/其它把 Python 编译成可执行程序的工具）：
#     可写数据根 = 主程序(exe)所在目录，资源根 = 解包/内置资源目录
#     （PyInstaller 的 sys._MEIPASS，其它工具回退到 exe 所在目录）。
#     运行产生的文件夹(shared_files/.cache/config/日志)全部落在数据根，
#     不依赖、也不写入只读的解包目录。
# ===================================================


def _is_frozen():
    """是否处于打包后的运行环境（兼容 PyInstaller 与 Nuitka standalone/onefile）"""
    return (bool(getattr(sys, 'frozen', False))
            or bool(getattr(sys, '_MEIPASS', None))
            or bool(getattr(sys, '__compiled__', False)))


def _bundle_root():
    """打包后内置资源所在的只读根目录"""
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        return meipass
    return os.path.dirname(os.path.abspath(sys.executable))


# leaffs 源码包目录（用于定位源码布局中的资源/旧数据）
_CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FROZEN = _is_frozen()

# BASE_DIR —— 资源根（web_page 页面/静态资源、内置 exe 依赖所在；原则上只读）
BASE_DIR = _bundle_root() if _FROZEN else _CODE_DIR

# APP_DIR / PROJECT_DIR —— 数据根（运行产物所在，始终可写、始终跟随主程序/项目根）
APP_DIR = os.path.dirname(os.path.abspath(sys.executable)) if _FROZEN else os.path.dirname(_CODE_DIR)
PROJECT_DIR = APP_DIR  # 兼容旧名：项目/应用根目录

UPLOAD_DIR = os.path.join(PROJECT_DIR, 'shared_files')   # 共享上传数据
CACHE_DIR = os.path.join(PROJECT_DIR, '.cache')          # 缩略图/文件夹大小等缓存
THUMB_DIR = os.path.join(CACHE_DIR, 'thumbs')
CONFIG_DIR = os.path.join(PROJECT_DIR, 'config')         # 服务端/下载器/账号等运行配置
FOLDER_SIZE_DB = os.path.join(CACHE_DIR, 'folder_sizes.json')

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)


def _migrate_legacy_config():
    """源码开发期：把旧位置(代码包 config/)里已有的运行配置一次性搬到数据层 config/。

    打包运行时旧位置不存在，直接跳过；从零首次运行也无需搬运。
    """
    if _FROZEN:
        return
    legacy_dir = os.path.join(_CODE_DIR, 'config')
    try:
        if not os.path.isdir(legacy_dir):
            return
        for fn in os.listdir(legacy_dir):
            src = os.path.join(legacy_dir, fn)
            dst = os.path.join(CONFIG_DIR, fn)
            if fn.endswith('.json') and os.path.isfile(src) and not os.path.exists(dst):
                shutil.copy2(src, dst)
    except Exception:
        pass


_migrate_legacy_config()


def find_bundled_exe(name):
    """按通用顺序定位内置依赖可执行文件（如 aria2c.exe / ffmpeg.exe）

    查找顺序：打包解包根 → 主程序所在目录 → 源码资源目录(_CODE_DIR) → PATH。
    找不到返回 None。
    """
    candidates = []
    if _FROZEN:
        candidates.append(os.path.join(_bundle_root(), name))
        candidates.append(os.path.join(APP_DIR, name))
    candidates.append(os.path.join(_CODE_DIR, name))
    for p in candidates:
        try:
            if os.path.isfile(p):
                return p
        except Exception:
            continue
    try:
        found = shutil.which(name)
        if found:
            return found
    except Exception:
        pass
    return None

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

def abs_path(rel_path):
    """相对路径转绝对路径，并做安全检查"""
    full = os.path.join(UPLOAD_DIR, rel_path) if rel_path else UPLOAD_DIR
    if not safe_path(UPLOAD_DIR, full):
        return None
    return full

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

def _get_folder_agg(path):
    """读取目录聚合 {size, files, folders}：内存 → 分片磁盘 → 递归扫描（按路径加和）"""
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

def _scan_folder_agg(path):
    """扫描单个目录并递归加和子目录缓存条目（不持有 db 锁）"""
    agg = {'size': 0, 'files': 0, 'folders': 0}
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
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

def invalidate_folder_cache(path, recursive=False):
    """
    使文件夹聚合缓存（大小/文件数/文件夹数）失效。
    recursive=True 时也清理 path 下所有子目录的缓存（用于整个目录被删除的场景）。
    recursive=False 时只清理 path 本身（上传文件、创建文件夹等场景）。
    分片写入只重写受影响的分片文件。
    """
    with _folder_size_lock:
        if recursive:
            path_prefix = path.rstrip(os.sep) + os.sep
            keys = [k for k in _folder_size_memory if k == path or k.startswith(path_prefix)]
        else:
            keys = [k for k in _folder_size_memory if k == path]
        for k in keys:
            del _folder_size_memory[k]
    ap = os.path.abspath(path)
    with _folder_db_lock:
        # 顶层根(共享根/缓存根)整体失效 → 清理所有分片；否则只清所属分片内匹配键
        if recursive and (ap == os.path.abspath(UPLOAD_DIR) or ap == os.path.abspath(CACHE_DIR)):
            names = _shard_names_on_disk()
        else:
            names = [_shard_name(ap)]
        prefix = ap.rstrip(os.sep) + os.sep
        for name in names:
            db = _load_shard(name)
            if recursive:
                keys = [k for k in db if k == ap or k.startswith(prefix)]
            else:
                keys = [k for k in db if k == ap]
            if not keys:
                continue
            for k in keys:
                del db[k]
            _save_shard(name, db)

# ========== 配额在途记账（C-06 / IC-QUOTA） ==========
# 进程内“在途字节”账本：写操作（上传等）在配额检查通过后 quota_reserve 预留字节，
# 请求结束（成功或异常）用 quota_settle 结算，防止并行请求利用陈旧的磁盘统计
# （TOCTOU）绕过配额。bucket 命名：'total' / 'public' / 'users/<name>'。
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

def _thumb_path(file_path):
    rel = os.path.relpath(file_path, UPLOAD_DIR).replace('\\', '_').replace('/', '_').replace(':', '_')
    return os.path.join(THUMB_DIR, rel + '.jpg')

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
            rel = os.path.relpath(file_path, UPLOAD_DIR).replace('\\', '_').replace('/', '_').replace(':', '_')
            index[file_path] = rel + '.jpg'
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

def _generate_thumbnail(file_path):
    """同步执行 ffmpeg 生成单张缩略图（仅内部调用）"""
    if not FFMPEG_PATH:
        return None
    thumb = _thumb_path(file_path)
    try:
        ext = os.path.splitext(file_path)[1].lower()
        is_video = ext in ('.mp4', '.webm', '.mov', '.avi', '.mkv', '.flv', '.ts', '.mts',
                           '.m4v', '.3gp', '.ogv', '.wmv', '.vob', '.mpeg', '.mpg')
        if is_video:
            subprocess.run(
                [FFMPEG_PATH, '-i', file_path, '-ss', '00:00:01',
                 '-vf', 'scale=256:-1', '-vframes', '1', '-q:v', '3', '-y', thumb],
                capture_output=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
        else:
            subprocess.run(
                [FFMPEG_PATH, '-i', file_path, '-vf', 'scale=256:-1',
                 '-q:v', '3', '-y', thumb],
                capture_output=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
        if os.path.exists(thumb):
            _thumb_index_add(file_path)
            return thumb
    except Exception:
        pass
    return None

def _schedule_thumbnail(file_path):
    """后台异步生成缩略图（同一路径单飞，避免并发重复跑 ffmpeg）"""
    if not FFMPEG_PATH:
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

    返回 None 表示暂无可用缩略图（正在生成/无 ffmpeg），调用方应显示占位。
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