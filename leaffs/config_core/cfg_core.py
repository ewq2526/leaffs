#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server_config.py - 配置、连接追踪、并发控制
"""

import os
import json
import threading
import time
import math

from leaffs.utils_core.ut_core import (
    CONFIG_DIR, UPLOAD_DIR, CACHE_DIR, THUMB_DIR,
    DISCONNECTED_EXCEPTIONS,
    read_file_cached, invalidate_file_cache,
    get_folder_size, invalidate_folder_cache,
    get_thumbnail, has_ffmpeg, _delete_thumb, cleanup_orphan_thumbs,
    COPY_BUFFER_SIZE,
)

CONFIG_FILE = os.path.join(CONFIG_DIR, 'server_config.json')

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)

_config_lock = threading.RLock()

# 架构修订（R4）：真实资源保护上限是整机“总连接/线程准入” _max_total_conns（默认 256，
# 配置键 max_total_conns，超限立即拒绝、绝不排队）。UI“并发数”(max_concurrent) 是该上限
# 的同一旋钮/回显：_max_concurrent 恒镜像 _max_total_conns（load/apply/update 同步），
# config 文件里旧版写入的 max_concurrent=10 属旧全局槽时代的过期值，不再读取。
# 慢客户端“无进展超时”（写侧）为 _io_idle_timeout_secs（默认 120s，配置键
# io_idle_timeout_secs；读侧沿用 HTTPHandler 每连接 socket 读超时 60s）。
_max_concurrent = 10   # 启动后由 load_config 镜像为 _max_total_conns
_max_total_conns = 256
_io_idle_timeout_secs = 120.0
_speed_limit = 0
_guest_mode = False
_user_quota = 5368709120
_public_quota = 5368709120
_total_quota = 53687091200

_pbkdf2_iterations = 600000
_salt_length = 32

# B-11 深度配置安全下限：低于下限的 pbkdf2/salt 配置一律不生效
# （apply_deep_config 整体拒绝并报错；load_config 载入时钳制到下限，落盘写钳制后值）
PBKDF2_ITER_MIN = 100000
SALT_LEN_MIN = 16

_thumb_sample_ratio = 0.1
_thumb_miss_threshold = 0.05
_thumb_scan_batch = 100
_thumb_scan_interval = 0.5

# HTTPS（TLS）：默认启用；开启后 HTTP/WS 均以 TLS 服务、Cookie 自动加 Secure
_tls_enabled = True
_tls_cert = ''
_tls_key = ''
_tls_trust_port = 8082  # 明文“证书安装引导页”端口（TLS 启用时提供）
_upload_max_size = 10 * 1024 * 1024 * 1024
_copy_buffer_size = 16 * 1024 * 1024
_cache_max_items = 50
_cache_ttl = 5
_folder_size_ttl = 5
_debounce_delay = 2.0
_max_api_body_size = 1 * 1024 * 1024
_preview_max_size = 10 * 1024 * 1024
_upload_chunk = 1 * 1024 * 1024   # 上传流式读取块大小（字节）
_zip_max_files = 500              # ZIP 打包文件数量上限（0 = 不限）
# ZIP 打包下载默认全流式（边压边发、零临时磁盘）；置 False = 先打包后发送
# （有 Content-Length，但大包会先写满临时文件：占用大量磁盘、更耗时，盘满即传输失败）
_zip_streaming = True
_session_expiry_days = 30

# IC-CFG（定稿修订 R3；与 §4 冲突处以 R3 为准）：新增安全配置键及默认值。
# 说明：D1 采用 R1 拍板值 guest 可新建但禁覆盖/删除（上传自动改名由 fs_core 落盘处实现）；
# D5 采用 R1「保守加固」，auto_trust_ca 默认 true（同指纹 no-op、同名异指纹并存新名）。
_guest_public_write = True            # D1/R1：guest 是否允许在 public[/子目录] 下新建文件
_downloader_guest_allowed = False     # D2：guest 是否允许使用 URL 下载器（默认禁用）
# CA 信任策略（用户决定制 v2，2026-09-03 拍板）：默认 false = 绝不自动安装、绝不主动询问，
# 仅引导页(8082)手动安装（指纹+一次性接入码）；置 true = 首次启动询问一次（交互 y/N），
# 拒绝后记录 declined 不再询问；再次启用只能到深度配置重新置 true。
_auto_trust_ca = False
_ca_trust_decision = 'unset'          # 'unset' | 'accepted' | 'declined'
_trust_bind_host = '0.0.0.0'           # 证书提示页（8082）监听地址，默认 0.0.0.0 全网监听（与主服务一致，局域网/最终用户可达；纯信息页无 CA 分发，trust_bind_host 可覆盖）
_ca_validity_days = 730               # IC-TLS：自签 CA 有效期（天），不再 3650
_max_conn_per_ip = 20                 # A-11：每 IP 活跃连接上限
# WS：每 IP 活跃 WebSocket 连接上限（A-06 默认 8；与 HTTP 各限各的，页面连接数卡一并调整）
_ws_max_conn_per_ip = 8
_access_log = True                    # A-16：请求级访问日志开关
_harden_config_acls_enabled = False   # 分发安全：默认不做运行时 config 目录 ACL 手术（避免不同用户
                                      # 环境/管理员运行一次后普通用户启动被锁），需显式开启


def load_config():
    global _max_concurrent, _max_total_conns, _io_idle_timeout_secs, _speed_limit, _guest_mode, _user_quota, _public_quota, _total_quota, PORT, WS_PORT
    global _pbkdf2_iterations, _salt_length
    global _thumb_sample_ratio, _thumb_miss_threshold, _thumb_scan_batch, _thumb_scan_interval
    global _upload_max_size, _copy_buffer_size, _cache_max_items, _cache_ttl, _folder_size_ttl
    global _debounce_delay, _max_api_body_size, _preview_max_size, _session_expiry_days
    global _upload_chunk, _zip_max_files, _zip_streaming
    global _guest_public_write, _downloader_guest_allowed, _auto_trust_ca, _trust_bind_host
    global _ca_validity_days, _max_conn_per_ip, _access_log
    global _ws_max_conn_per_ip
    global _harden_config_acls_enabled
    global _ca_trust_decision
    # 集成修复（B2 上报预存缺陷）：TLS 四字段同样需要 global，否则赋值落在局部、
    # server_config.json 中的 tls_* 永不恢复（重启恒为模块默认 True/8082）。
    global _tls_enabled, _tls_cert, _tls_key, _tls_trust_port
    cfg = {}
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                cfg = json.load(f)
            # R4：真实准入上限取 max_total_conns（缺省 256）；config 里遗留的
            # max_concurrent=10 是旧全局槽时代的过期值，不再读取 —— UI“并发数”回显
            # 恒镜像真实上限（见 _max_concurrent = _max_total_conns 与
            # update_concurrent_limit/get_max_concurrent），保证“显示即真实、改了即生效”。
            _max_total_conns = max(1, int(cfg.get('max_total_conns', 256)))
            _max_concurrent = _max_total_conns
            try:
                _io_idle_timeout_secs = max(1.0, float(cfg.get('io_idle_timeout_secs', 120.0)))
            except (TypeError, ValueError):
                _io_idle_timeout_secs = 120.0
            _speed_limit = cfg.get('download_speed_limit', 0)
            if 'auth_enabled' in cfg:
                _guest_mode = not bool(cfg['auth_enabled'])
            else:
                _guest_mode = cfg.get('guest_mode', False)
            _user_quota = cfg.get('user_quota', 5368709120)
            _public_quota = cfg.get('public_quota', 5368709120)
            _total_quota = cfg.get('total_quota', 53687091200)
            if 'http_port' in cfg:
                PORT = int(cfg['http_port'])
            if 'ws_port' in cfg:
                WS_PORT = int(cfg['ws_port'])
            # B-11 载入钳制：配置文件被手改低于下限也不生效（钳到下限，save_config 按钳制后值写回）
            _pbkdf2_iterations = max(PBKDF2_ITER_MIN, int(cfg.get('pbkdf2_iterations', 600000)))
            _salt_length = max(SALT_LEN_MIN, int(cfg.get('salt_length', 32)))
            _thumb_sample_ratio = cfg.get('thumb_sample_ratio', 0.1)
            _thumb_miss_threshold = cfg.get('thumb_miss_threshold', 0.05)
            _thumb_scan_batch = cfg.get('thumb_scan_batch', 100)
            _thumb_scan_interval = cfg.get('thumb_scan_interval', 0.5)
            _upload_max_size = cfg.get('upload_max_size', 10 * 1024 * 1024 * 1024)
            _copy_buffer_size = cfg.get('copy_buffer_size', 16 * 1024 * 1024)
            _cache_max_items = cfg.get('cache_max_items', 50)
            _cache_ttl = cfg.get('cache_ttl', 5)
            _folder_size_ttl = cfg.get('folder_size_ttl', 5)
            _debounce_delay = cfg.get('debounce_delay', 2.0)
            _max_api_body_size = cfg.get('max_api_body_size', 1 * 1024 * 1024)
            _preview_max_size = cfg.get('preview_max_size', 10 * 1024 * 1024)
            _session_expiry_days = cfg.get('session_expiry_days', 30)
            _upload_chunk = cfg.get('upload_chunk', 1 * 1024 * 1024)
            _zip_max_files = cfg.get('zip_max_files', 500)
            _zip_streaming = _to_bool(cfg.get('zip_streaming', True))
            _tls_enabled = bool(cfg.get('tls_enabled', True))
            _tls_cert = str(cfg.get('tls_cert', '') or '')
            _tls_key = str(cfg.get('tls_key', '') or '')
            _tls_trust_port = int(cfg.get('tls_trust_port', 8082))
            _guest_public_write = bool(cfg.get('guest_public_write', True))
            _downloader_guest_allowed = bool(cfg.get('downloader_guest_allowed', False))
            _auto_trust_ca = bool(cfg.get('auto_trust_ca', False))
            _ca_trust_decision = cfg.get('ca_trust_decision', 'unset')
            if _ca_trust_decision not in ('unset', 'accepted', 'declined'):
                _ca_trust_decision = 'unset'
            _trust_bind_host = str(cfg.get('trust_bind_host', '0.0.0.0') or '0.0.0.0').strip() or '0.0.0.0'
            _ca_validity_days = max(1, int(cfg.get('ca_validity_days', 730)))
            _max_conn_per_ip = max(1, int(cfg.get('max_conn_per_ip', 20)))
            _ws_max_conn_per_ip = max(1, int(cfg.get('ws_max_conn_per_ip', 8)))
            _access_log = bool(cfg.get('access_log', True))
            _harden_config_acls_enabled = bool(cfg.get('harden_config_acls', False))
    except Exception:
        pass
    save_config()
    sync_all_constants()
    import logging
    logging.getLogger('wifi_convey').info('配置已加载（共 %d 项）', len(get_deep_config_dict()))


def save_config():
    # 整个写配置过程加锁（RLock 可重入：apply_deep_config/set_tls_enabled 等调用方
    # 已持锁时也能正常进入），临时文件名按线程唯一，写完原子替换，
    # 避免多线程并发写同一临时文件导致配置损坏。
    with _config_lock:
        cfg = {
            'max_concurrent': _max_concurrent,
            'download_speed_limit': _speed_limit,
            'guest_mode': _guest_mode,
            'user_quota': _user_quota,
            'public_quota': _public_quota,
            'total_quota': _total_quota,
            'tls_enabled': _tls_enabled,
            'tls_cert': _tls_cert,
            'tls_key': _tls_key,
            'tls_trust_port': _tls_trust_port,
        }
        deep_cfg = get_deep_config_dict()
        cfg.update(deep_cfg)
        save_ports_to_config(cfg)
        try:
            tmp = f"{CONFIG_FILE}.{threading.get_ident()}.tmp"
            with open(tmp, 'w') as f:
                json.dump(cfg, f, indent=2)
            os.replace(tmp, CONFIG_FILE)
        except Exception:
            pass


def get_speed_limit(): return _speed_limit
def get_guest_mode(): return _guest_mode
def get_default_user_quota(): return _user_quota
def get_public_quota(): return _public_quota
def get_total_quota(): return _total_quota


def set_speed_limit(bytes_per_sec):
    global _speed_limit
    _speed_limit = max(0, bytes_per_sec)
    _user_limiter._users.clear()
    save_config()


def set_guest_mode(val):
    global _guest_mode
    _guest_mode = bool(val)
    save_config()


def set_quotas(user_q=None, public_q=None, total_q=None):
    global _user_quota, _public_quota, _total_quota
    if user_q is not None: _user_quota = int(user_q)
    if public_q is not None: _public_quota = int(public_q)
    if total_q is not None: _total_quota = int(total_q)
    save_config()


def get_pbkdf2_iterations(): return _pbkdf2_iterations
def get_salt_length(): return _salt_length
def get_thumb_sample_ratio(): return _thumb_sample_ratio
def get_thumb_miss_threshold(): return _thumb_miss_threshold
def get_thumb_scan_batch(): return _thumb_scan_batch
def get_thumb_scan_interval(): return _thumb_scan_interval
def get_upload_max_size(): return _upload_max_size
def get_copy_buffer_size(): return _copy_buffer_size
def get_cache_max_items(): return _cache_max_items
def get_cache_ttl(): return _cache_ttl
def get_folder_size_ttl(): return _folder_size_ttl
def get_debounce_delay(): return _debounce_delay
def get_max_api_body_size(): return _max_api_body_size
def get_preview_max_size(): return _preview_max_size
def get_upload_chunk(): return _upload_chunk
def get_zip_max_files(): return _zip_max_files
def get_zip_streaming(): return _zip_streaming
def get_tls_enabled(): return _tls_enabled
def get_tls_cert(): return _tls_cert
def get_tls_key(): return _tls_key
def get_tls_trust_port(): return _tls_trust_port

def set_tls_enabled(enabled):
    """保存 TLS 开关（重启后由 run_http/run_ws 生效）"""
    global _tls_enabled
    with _config_lock:
        _tls_enabled = bool(enabled)
        save_config()
    return True
def get_session_expiry_days(): return _session_expiry_days

def get_guest_public_write(): return _guest_public_write
def get_downloader_guest_allowed(): return _downloader_guest_allowed
def get_auto_trust_ca(): return _auto_trust_ca

def get_ca_trust_decision(): return _ca_trust_decision

def set_ca_trust_decision(value):
    """记录用户对“自动安装本机 CA”的选择（unset/accepted/declined），持久化到 server_config.json。"""
    global _ca_trust_decision
    with _config_lock:
        if value not in ('unset', 'accepted', 'declined'):
            return False
        _ca_trust_decision = value
        save_config()
        return True
def get_trust_bind_host(): return _trust_bind_host
def get_ca_validity_days(): return _ca_validity_days
def get_max_conn_per_ip(): return _max_conn_per_ip
def get_ws_max_conn_per_ip(): return _ws_max_conn_per_ip
def get_access_log(): return _access_log

def get_harden_config_acls(): return _harden_config_acls_enabled


def get_deep_config_dict():
    return {
        'pbkdf2_iterations': _pbkdf2_iterations,
        'salt_length': _salt_length,
        'thumb_sample_ratio': _thumb_sample_ratio,
        'thumb_miss_threshold': _thumb_miss_threshold,
        'thumb_scan_batch': _thumb_scan_batch,
        'thumb_scan_interval': _thumb_scan_interval,
        'upload_max_size': _upload_max_size,
        'copy_buffer_size': _copy_buffer_size,
        'cache_max_items': _cache_max_items,
        'cache_ttl': _cache_ttl,
        'folder_size_ttl': _folder_size_ttl,
        'debounce_delay': _debounce_delay,
        'max_api_body_size': _max_api_body_size,
        'preview_max_size': _preview_max_size,
        'session_expiry_days': _session_expiry_days,
        'upload_chunk': _upload_chunk,
        'zip_max_files': _zip_max_files,
        'zip_streaming': _zip_streaming,
        # R4：整机总连接/线程准入上限（资源保护，默认 256；无排队，超限直接拒绝）
        'max_total_conns': _max_total_conns,
        # R4：慢客户端“无进展超时”（写侧，秒；读侧沿用 60s socket 读超时）
        'io_idle_timeout_secs': _io_idle_timeout_secs,
        # IC-CFG（R3）：新增安全配置键（load/save/get/apply_deep 同步；save_config 经本函数落盘）
        'guest_public_write': _guest_public_write,
        'downloader_guest_allowed': _downloader_guest_allowed,
        'auto_trust_ca': _auto_trust_ca,
        'ca_trust_decision': _ca_trust_decision,
        'trust_bind_host': _trust_bind_host,
        'ca_validity_days': _ca_validity_days,
        'max_conn_per_ip': _max_conn_per_ip,
        'ws_max_conn_per_ip': _ws_max_conn_per_ip,
        'access_log': _access_log,
        'harden_config_acls': _harden_config_acls_enabled,
    }


def _to_bool(v):
    """宽松布尔归一：兼容 JSON 原生 bool / 0|1 / 'true'/'false' 等字符串（避免 bool('false')==True 陷阱）"""
    if isinstance(v, str):
        return v.strip().lower() in ('1', 'true', 'yes', 'on', '开')
    return bool(v)


def apply_deep_config(data):
    """应用深度配置并返回 (ok: bool, err: str)。

    B-11：pbkdf2_iterations 低于 PBKDF2_ITER_MIN / salt_length 低于 SALT_LEN_MIN →
    返回 (False, 具体文案) 且本次整体失败（不落任何键、不落盘），由 cfg_api 转 400。
    全部通过才保存并同步运行时常量；成功返回 (True, '')。
    """
    with _config_lock:
        global _pbkdf2_iterations, _salt_length
        global _thumb_sample_ratio, _thumb_miss_threshold, _thumb_scan_batch, _thumb_scan_interval
        global _upload_max_size, _copy_buffer_size, _cache_max_items, _cache_ttl, _folder_size_ttl
        global _debounce_delay, _max_api_body_size, _preview_max_size, _session_expiry_days
        global _upload_chunk, _zip_max_files
        global _zip_streaming
        global _guest_public_write, _downloader_guest_allowed, _auto_trust_ca, _trust_bind_host
        global _ca_validity_days, _max_conn_per_ip, _access_log
        global _ws_max_conn_per_ip
        global _ca_trust_decision
        global _harden_config_acls_enabled
        global _max_total_conns, _max_concurrent, _io_idle_timeout_secs
        # B-11 下限预检：任一违规 → 整体失败，本次不应用任何键
        if 'pbkdf2_iterations' in data:
            try:
                _chk = int(data['pbkdf2_iterations'])
            except Exception:
                return False, 'pbkdf2_iterations 必须是整数'
            if _chk < PBKDF2_ITER_MIN:
                return False, 'pbkdf2_iterations 不得低于 %d' % PBKDF2_ITER_MIN
        if 'salt_length' in data:
            try:
                _chk = int(data['salt_length'])
            except Exception:
                return False, 'salt_length 必须是整数'
            if _chk < SALT_LEN_MIN:
                return False, 'salt_length 不得低于 %d' % SALT_LEN_MIN
        changed = False
        if 'pbkdf2_iterations' in data:
            _pbkdf2_iterations = int(data['pbkdf2_iterations']); changed = True
        if 'salt_length' in data:
            _salt_length = int(data['salt_length']); changed = True
        if 'thumb_sample_ratio' in data:
            val = float(data['thumb_sample_ratio'])
            if 0.001 <= val <= 1.0: _thumb_sample_ratio = val; changed = True
        if 'thumb_miss_threshold' in data:
            val = float(data['thumb_miss_threshold'])
            if 0.001 <= val <= 1.0: _thumb_miss_threshold = val; changed = True
        if 'thumb_scan_batch' in data:
            val = int(data['thumb_scan_batch'])
            if 1 <= val <= 10000: _thumb_scan_batch = val; changed = True
        if 'thumb_scan_interval' in data:
            val = float(data['thumb_scan_interval'])
            if 0.01 <= val <= 60.0: _thumb_scan_interval = val; changed = True
        if 'upload_max_size' in data:
            _upload_max_size = max(0, int(data['upload_max_size'])); changed = True
        if 'copy_buffer_size' in data:
            _copy_buffer_size = int(data['copy_buffer_size']); changed = True
        if 'cache_max_items' in data:
            _cache_max_items = int(data['cache_max_items']); changed = True
        if 'cache_ttl' in data:
            _cache_ttl = int(data['cache_ttl']); changed = True
        if 'folder_size_ttl' in data:
            _folder_size_ttl = int(data['folder_size_ttl']); changed = True
        if 'debounce_delay' in data:
            _debounce_delay = float(data['debounce_delay']); changed = True
        if 'max_api_body_size' in data:
            _max_api_body_size = int(data['max_api_body_size']); changed = True
        if 'preview_max_size' in data:
            _preview_max_size = int(data['preview_max_size']); changed = True
        if 'upload_chunk' in data:
            _upload_chunk = max(4096, int(data['upload_chunk'])); changed = True
        if 'zip_max_files' in data:
            _zip_max_files = max(0, int(data['zip_max_files'])); changed = True
        if 'zip_streaming' in data:
            _zip_streaming = _to_bool(data['zip_streaming']); changed = True
        # R4：整机总连接/线程准入上限（无排队资源保护；改小可即时收紧、改大立即放量）。
        # UI“并发数”(max_concurrent) 回显随本值同步镜像，保证显示即真实。
        # 校验与 B-11（pbkdf2/salt）同模式：非法值整体失败(400)、不改运行时值、不落盘。
        if 'max_total_conns' in data:
            try:
                _chk = int(data['max_total_conns'])
            except Exception:
                return False, 'max_total_conns 必须在 1~20000 之间'
            if _chk < 1 or _chk > 20000:
                return False, 'max_total_conns 必须在 1~20000 之间'
            _max_total_conns = _chk
            _max_concurrent = _chk
            changed = True
        # R4：慢客户端写侧无进展超时（秒，>=1）；非法/非有限值整体失败(400)、不改值
        if 'io_idle_timeout_secs' in data:
            try:
                _chk = float(data['io_idle_timeout_secs'])
            except Exception:
                return False, 'io_idle_timeout_secs 必须 ≥1'
            if not math.isfinite(_chk) or _chk < 1.0:
                return False, 'io_idle_timeout_secs 必须 ≥1'
            _io_idle_timeout_secs = _chk
            changed = True
        if 'session_expiry_days' in data:
            _session_expiry_days = int(data['session_expiry_days']); changed = True
        # IC-CFG（R3）新增键
        if 'guest_public_write' in data:
            _guest_public_write = _to_bool(data['guest_public_write']); changed = True
        if 'downloader_guest_allowed' in data:
            _downloader_guest_allowed = _to_bool(data['downloader_guest_allowed']); changed = True
        if 'auto_trust_ca' in data:
            _old_auto = _auto_trust_ca
            _auto_trust_ca = _to_bool(data['auto_trust_ca']); changed = True
            if _auto_trust_ca and not _old_auto:
                # 用户决定制：深度配置重新开启 = 重新同意 → 清除既往 declined，下次启动重新询问/安装
                _ca_trust_decision = 'unset'
        if 'ca_trust_decision' in data:
            _d = str(data['ca_trust_decision']).strip().lower()
            if _d in ('unset', 'accepted', 'declined'):
                _ca_trust_decision = _d; changed = True
        if 'access_log' in data:
            _access_log = _to_bool(data['access_log']); changed = True
        if 'trust_bind_host' in data:
            host = str(data['trust_bind_host']).strip()
            if not host:
                return False, 'trust_bind_host 不能为空'
            _trust_bind_host = host; changed = True
        if 'ca_validity_days' in data:
            try:
                _ca_validity_days = max(1, int(data['ca_validity_days']))
            except Exception:
                return False, 'ca_validity_days 必须是整数'
            changed = True
        if 'max_conn_per_ip' in data:
            try:
                _max_conn_per_ip = max(1, int(data['max_conn_per_ip']))
            except Exception:
                return False, 'max_conn_per_ip 必须是整数'
            changed = True
        if 'ws_max_conn_per_ip' in data:
            try:
                _chk_ws = int(data['ws_max_conn_per_ip'])
            except Exception:
                return False, 'ws_max_conn_per_ip 必须是整数'
            if _chk_ws < 1 or _chk_ws > 256:
                return False, 'ws_max_conn_per_ip 必须在 1~256 之间'
            _ws_max_conn_per_ip = _chk_ws
            changed = True
        if 'harden_config_acls' in data:
            _harden_config_acls_enabled = _to_bool(data['harden_config_acls']); changed = True
        if changed:
            save_config()
            sync_all_constants()
        return True, ''


def sync_all_constants():
    import leaffs.utils_core.ut_core as _cu
    _cu.COPY_BUFFER_SIZE = _copy_buffer_size
    _cu.CACHE_MAX_ITEMS = _cache_max_items
    _cu.CACHE_TTL = _cache_ttl
    _cu.FOLDER_SIZE_TTL = _folder_size_ttl
    _cu.THUMB_SAMPLE_RATIO = _thumb_sample_ratio
    _cu.THUMB_MISS_THRESHOLD = _thumb_miss_threshold
    _cu.THUMB_SCAN_BATCH = _thumb_scan_batch
    _cu.THUMB_SCAN_INTERVAL = _thumb_scan_interval
    _cu.DEBOUNCE_DELAY = _debounce_delay

    import leaffs.auth_account_core.ac_core as _aa
    _aa.PBKDF2_ITERATIONS = _pbkdf2_iterations
    _aa.SALT_LENGTH = _salt_length
    _aa.SESSION_EXPIRY_DAYS = _session_expiry_days

    # 不要 import leaffs.leaffs（那会触发模块二次执行，是直接 python leaffs/leaffs.py
    # 运行时下载器出现双管理器的根因）。只同步已加载的模块实例：
    # 常规运行（python -m leaffs / 打包）模块名 leaffs.leaffs 已在 sys.modules；
    # 直接脚本运行（python leaffs/leaffs.py）时则是 __main__。
    import sys
    _leaffs_mods = []
    _m = sys.modules.get('leaffs.leaffs')
    if _m is not None: _leaffs_mods.append(_m)
    _mm = sys.modules.get('__main__')
    if _mm is not None and str(getattr(_mm, '__file__', '')).replace('\\', '/').endswith('leaffs.py'):
        _leaffs_mods.append(_mm)
    for _mod in _leaffs_mods:
        try:
            _mod.MAX_API_BODY_SIZE = _max_api_body_size
            _mod.PREVIEW_MAX_SIZE = _preview_max_size
        except Exception:
            pass

    # 导出别名 COPY_BUFFER_SIZE 动态化：每次同步后本模块的绑定即最新值，
    # 使 leaffs.py 读取 _cfg.COPY_BUFFER_SIZE 拿到的是新值（顶部 ut_core 的绑定仅作回退）。
    try:
        globals()['COPY_BUFFER_SIZE'] = _copy_buffer_size
    except Exception:
        pass


# 服务器进程启动时间（管理页“服务器状态-运行时间”显示真实运行时长）
SERVER_START_TIME = time.time()

def get_server_uptime():
    return max(0, int(time.time() - SERVER_START_TIME))


# 连接追踪
_connections_lock = threading.Lock()
_connections = {}

# 最近活动时间刷新粒度（秒）：轮询/静态资源等后台请求不计入“活动”，
# 只有距上次活动超过该间隔才更新 last_seen，避免活动时间恒被轮询刷成几秒前
ACTIVITY_TRACK_INTERVAL = 60.0

def _parse_device(ua):
    if not ua:
        return '未知'
    ua_lower = ua.lower()
    if 'iphone' in ua_lower or 'ipad' in ua_lower: return 'iOS设备'
    if 'android' in ua_lower: return 'Android设备'
    if 'macintosh' in ua_lower or 'mac os' in ua_lower: return 'Mac电脑'
    if 'windows' in ua_lower: return 'Windows电脑'
    if 'linux' in ua_lower: return 'Linux设备'
    if 'curl' in ua_lower or 'wget' in ua_lower: return '命令行'
    if 'python' in ua_lower: return 'Python'
    return ua[:20] + '...'

def track_connection(ip, user_agent='', username='', role=''):
    now = time.time()
    device = _parse_device(user_agent)
    with _connections_lock:
        if ip in _connections:
            info = _connections[ip]
            info['request_count'] += 1
            info['device'] = device
            if username: info['username'] = username
            if role: info['role'] = role
            # 节流：后台轮询/静态资源请求不持续刷新“最近活动”，
            # 只有间隔超过 ACTIVITY_TRACK_INTERVAL 才视为一次新活动
            if now - info['last_seen'] >= ACTIVITY_TRACK_INTERVAL:
                info['last_seen'] = now
        else:
            _connections[ip] = {
                'first_seen': now, 'last_seen': now,
                'user_agent': user_agent[:120] if user_agent else '',
                'request_count': 1, 'username': username or '',
                'device': device, 'role': role or ''
            }

def get_connections():
    with _connections_lock:
        now = time.time()
        expired = [ip for ip, info in _connections.items() if now - info['last_seen'] > 3600]
        for ip in expired: del _connections[ip]
        return dict(_connections)


# ========== 并发控制（架构修订 R4：取消全局串行槽） ==========
# 缺陷历史：旧实现用“全局并发槽”（_max_concurrent=10、可排队等待）把 do_GET/do_POST
# 几乎全部请求串行化；上传/raw/zip 在流式正文读写阶段也全程持槽 → 一条慢速上传/慢读即可
# 让登录/列表等快速 API 排队 30s（503）甚至更久。
# 修订（R4）：HTTP 本就是每连接一线程（ThreadingMixIn），请求级全局槽整体取消：
#   * 新增整机“总连接/线程准入上限” _max_total_conns（默认 256，配置键 max_total_conns）：
#     try_acquire_thread() 是 O(1) 无等待准入 —— 超限立即拒绝（HTTPHandler 连接级 503/关闭），
#     绝不排队；流式正文与快速 API 在总上限内自由并发、互不阻塞。
#     （每 IP 活跃连接上限 max_conn_per_ip 仍旧保留，双保险均只拒绝不排队。）
#   * 慢客户端双控：读侧沿用 HTTPHandler 每连接 socket 读超时 60s；写侧“无进展超时”为
#     io_idle_timeout_secs（默认 120s），由 fs_api 各流式发送函数在写循环套用（中断清理
#     .part/临时资源，见 fs_core 落盘逻辑与 fs_api _stream_write）。
#   * 每 IP 活跃连接上限 max_conn_per_ip 仍旧保留，双保险均只拒绝不排队。
#   * UI“并发数”(max_concurrent) 即 _max_total_conns 的旋钮/回显（显示即真实）：
#     update_concurrent_limit / apply_deep_config('max_total_conns') / load_config 三者
#     同步 _max_concurrent = _max_total_conns；旧 acquire_concurrent / concurrent_guard
#     仅保留给历史调用方做兼容（立即成功、无限制），不再有任何强制/等待语义。
#   * 真正独占的共享写仍由各模块细粒度小锁保护（_config_lock、连接表锁、folder 聚合缓存锁、
#     缩略图索引锁、会话/配额锁等），临界区均为微秒级；慢路径（网络 I/O、磁盘扫描）绝不持锁。
_active_handlers = 0            # 正在处理 HTTP 连接的线程数（准入计数，仅 _thread_slot_lock 保护）
_thread_slot_lock = threading.Lock()

def get_max_concurrent(): return _max_concurrent      # 遗留回显（/api/config、/api/stats）
def get_max_total_conns(): return _max_total_conns    # R4：整机总连接/线程准入上限
def get_io_idle_timeout_secs(): return _io_idle_timeout_secs   # R4：写侧无进展超时（秒）

def try_acquire_thread():
    """无等待申请一个总并发准入位（O(1)）；已达 _max_total_conns 上限返回 False。

    调用方（HTTPHandler.handle 连接级）拿到 False 时直接拒绝该连接（回 503/关闭），
    绝不阻塞/排队 —— 这是本架构唯一的“整机并发上限”，只保护资源不串行化请求。
    """
    global _active_handlers
    with _thread_slot_lock:
        if _active_handlers >= _max_total_conns:
            return False
        _active_handlers += 1
        return True

def release_thread():
    """释放一个总并发准入位；与 try_acquire_thread 严格配对，必须在 finally 中调用。"""
    global _active_handlers
    with _thread_slot_lock:
        if _active_handlers > 0:
            _active_handlers -= 1

def active_thread_count():
    """当前活跃请求线程数（诊断/状态展示用）"""
    return _active_handlers


# ---- 遗留全局槽 API：保留旧签名，改为“无限制立即成功”（不串行、不排队）----
def acquire_concurrent(timeout=None):
    """[遗留兼容 R4] 旧全局并发槽已取消：一律立即成功，不再限制/等待任何请求。"""
    return True

def release_concurrent():
    """[遗留兼容 R4] 与 acquire_concurrent 配对；无操作。"""

class ConcurrentGuard:
    """[遗留兼容 R4] 旧全局并发槽已取消：with 块始终立即进入，无排队语义。"""
    __slots__ = ('_acquired', '_timeout')
    def __init__(self, timeout=None):
        self._acquired = True
        self._timeout = timeout
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        return False
    @property
    def acquired(self):
        return True

def concurrent_guard(timeout=None): return ConcurrentGuard(timeout)

def update_concurrent_limit(new_max):
    """R4：UI“并发数”(max_concurrent) 保存入口 —— 直接更新整机总并发准入上限。

    新架构没有“可排队的全局槽”，该旋钮现在等价于设置 max_total_conns（无等待、
    超限立即 503/拒绝）；回显 get_max_concurrent() 与上限恒一致（显示即真实）。
    """
    global _max_concurrent, _max_total_conns
    try:
        new_max = max(1, int(new_max))
        _max_total_conns = new_max
        _max_concurrent = new_max
        save_config()
        return True, ''
    except Exception as e: return False, str(e)


# 用户级共享限速器
class UserRateLimiter:
    def __init__(self):
        self._users = {}
        self._streams = {}
        self._lock = threading.Lock()

    def _get_acc(self, user_id):
        with self._lock:
            if user_id not in self._users:
                self._users[user_id] = {'bytes': 0, 'start': time.time(), 'last_active': time.time()}
            else:
                self._users[user_id]['last_active'] = time.time()
            return self._users[user_id]

    def record_and_wait(self, user_id, sent_bytes):
        rate = _speed_limit
        if rate <= 0: return
        acc = self._get_acc(user_id)
        with self._lock:
            acc['bytes'] += sent_bytes
            acc['last_active'] = time.time()
            elapsed = time.time() - acc['start']
            expect = acc['bytes'] / rate
            wait = max(0, expect - elapsed)
        if wait > 0.001: time.sleep(wait)

    def record_and_wait_with_rate(self, user_id, sent_bytes, rate):
        if rate <= 0: return
        acc = self._get_acc(user_id)
        with self._lock:
            acc['bytes'] += sent_bytes
            acc['last_active'] = time.time()
            elapsed = time.time() - acc['start']
            expect = acc['bytes'] / rate
            wait = max(0, expect - elapsed)
        if wait > 0.001: time.sleep(wait)

    def reset_user(self, user_id):
        with self._lock: self._users.pop(user_id, None)

    # ---- 每用户并发下载流计数（限速用户用） ----
    def acquire_stream(self, user_id, cap):
        """尝试为该用户占一个并发下载流名额；达到上限 cap 时返回 False"""
        with self._lock:
            cur = self._streams.get(user_id, 0)
            if cur >= cap:
                return False
            self._streams[user_id] = cur + 1
            return True

    def release_stream(self, user_id):
        with self._lock:
            cur = self._streams.get(user_id, 0)
            if cur > 1:
                self._streams[user_id] = cur - 1
            else:
                self._streams.pop(user_id, None)

    def cleanup_expired(self, max_idle=3600):
        now = time.time()
        with self._lock:
            expired = [uid for uid, info in self._users.items()
                       if now - info.get('last_active', info['start']) > max_idle]
            for uid in expired: del self._users[uid]
            return len(expired)

def _limiter_cleanup_loop():
    while True:
        time.sleep(300)
        cleaned = _user_limiter.cleanup_expired(3600)
        if cleaned:
            import logging
            logging.getLogger('wifi_convey').info(f'限速器清理: 移除 {cleaned} 个过期条目')

_user_limiter = UserRateLimiter()
def get_user_limiter(): return _user_limiter

_cleanup_thread = threading.Thread(target=_limiter_cleanup_loop, daemon=True)
_cleanup_thread.start()


# 端口配置
PORT = 8080
WS_PORT = 8081

def load_ports_from_config(cfg):
    global PORT, WS_PORT
    if 'http_port' in cfg: PORT = int(cfg['http_port'])
    if 'ws_port' in cfg: WS_PORT = int(cfg['ws_port'])

def save_ports_to_config(cfg):
    cfg['http_port'] = PORT
    cfg['ws_port'] = WS_PORT

def set_ports(http_port=None, ws_port=None):
    global PORT, WS_PORT
    new_p = PORT if http_port is None else int(http_port)
    new_w = WS_PORT if ws_port is None else int(ws_port)
    if http_port is not None and (new_p < 1024 or new_p > 65535):
        return False, 'HTTP 端口必须在 1024~65535 之间'
    if ws_port is not None and (new_w < 1024 or new_w > 65535):
        return False, 'WebSocket 端口必须在 1024~65535 之间'
    if new_p == new_w:
        return False, 'HTTP 与 WebSocket 端口不能相同'
    if new_p == _tls_trust_port:
        return False, f'HTTP 端口 {new_p} 与证书引导页端口重复'
    if new_w == _tls_trust_port:
        return False, f'WebSocket 端口 {new_w} 与证书引导页端口重复'
    PORT, WS_PORT = new_p, new_w
    save_config()
    return True, '端口已保存，重启后生效'

def set_tls_trust_port(port):
    """设置证书引导页端口（不能与 HTTP/WebSocket 端口重复）"""
    global _tls_trust_port
    port = int(port)
    if port < 1024 or port > 65535:
        return False, '引导页端口必须在 1024~65535 之间'
    if port == PORT or port == WS_PORT:
        return False, '引导页端口不能与 HTTP/WebSocket 端口重复'
    with _config_lock:
        _tls_trust_port = port
        save_config()
    return True, '引导页端口已保存，重启后生效'