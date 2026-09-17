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
import html as html_mod
from collections import OrderedDict

# ===================================================
# 运行时目录定位：已收敛到 leaffs.paths（单一数据源，见该模块文档）。
# 此处仅做名字转口，保持 utils_core 历史导出兼容；下游模块将逐步改为直连 leaffs.paths。
# ===================================================
from leaffs.paths import (  # noqa: E402
    _CODE_DIR, _FROZEN, BASE_DIR, APP_DIR, PROJECT_DIR,
    UPLOAD_DIR, UPLOAD_TMP_DIR, UPLOAD_TMP_DIRNAME, CACHE_DIR, THUMB_DIR, CONFIG_DIR,
    is_upload_tmp_entry, is_upload_tmp_relpath,
    find_bundled_exe,
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
    """
    root = os.path.abspath(UPLOAD_DIR)
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
    ap = os.path.abspath(path)
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
        root_ap = os.path.abspath(UPLOAD_DIR)
        cache_ap = os.path.abspath(CACHE_DIR)
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