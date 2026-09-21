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

from leaffs.utils.core import (
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
# HTTP/1.1（2026-09-18）：keep-alive 下**等待下一个请求**的空闲超时为
# _keepalive_timeout_secs（默认 15s，配置键 keepalive_timeout）—— 比读超时短得多，
# 否则浏览器的空闲长连接会把线程和准入位占满（一个连接一个线程，上限 max_total_conns）。
_max_concurrent = 10   # 启动后由 load_config 镜像为 _max_total_conns
_max_total_conns = 256
_io_idle_timeout_secs = 120.0
_keepalive_timeout_secs = 15.0
# 游客登录限速上限（单 IP 每分钟次数，配置键 guest_login_max_per_min，默认 10）。
# 做成可配是为了让测试能放宽：测试套自己的 guest 登录次数本来就贴着 10 这条线，
# 跑得快时会挤进同一个 60 秒窗口、误报「游客登录过于频繁」。产品默认值不变。
_guest_login_max_per_min = 10
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
_guest_public_write = False           # D1/R1：guest 是否允许在 public[/子目录] 下新建文件（默认关）
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
    global _max_concurrent, _max_total_conns, _io_idle_timeout_secs, _keepalive_timeout_secs, _guest_login_max_per_min, _speed_limit, _guest_mode, _user_quota, _public_quota, _total_quota, PORT, WS_PORT
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
            _max_total_conns = _clamp_num(cfg.get('max_total_conns', 256), 8, 20000)
            _max_concurrent = _max_total_conns
            _io_idle_timeout_secs = _clamp_num(cfg.get('io_idle_timeout_secs', 120.0),
                                               1.0, 3600.0, float)
            _keepalive_timeout_secs = _clamp_num(cfg.get('keepalive_timeout', 15.0),
                                                 1.0, 300.0, float)
            _guest_login_max_per_min = _clamp_num(cfg.get('guest_login_max_per_min', 10),
                                                  1, 100000, int)
            _speed_limit = cfg.get('download_speed_limit', 0)
            if 'auth_enabled' in cfg:
                _guest_mode = not _to_bool(cfg['auth_enabled'])
            else:
                _guest_mode = _to_bool(cfg.get('guest_mode', False))
            _user_quota = cfg.get('user_quota', 5368709120)
            _public_quota = cfg.get('public_quota', 5368709120)
            _total_quota = cfg.get('total_quota', 53687091200)
            if 'http_port' in cfg:
                PORT = _clamp_num(cfg['http_port'], 1024, 65535)
            if 'ws_port' in cfg:
                WS_PORT = _clamp_num(cfg['ws_port'], 1024, 65535)
            # B-11 载入钳制：配置文件被手改到区间外也不生效（钳到边界，save_config 按钳制后值写回）
            _pbkdf2_iterations = _clamp_num(cfg.get('pbkdf2_iterations', 600000),
                                            PBKDF2_ITER_MIN, 10000000)
            _salt_length = _clamp_num(cfg.get('salt_length', 32), SALT_LEN_MIN, 64)
            _thumb_sample_ratio = _clamp_num(cfg.get('thumb_sample_ratio', 0.1), 0.001, 1.0, float)
            _thumb_miss_threshold = _clamp_num(cfg.get('thumb_miss_threshold', 0.05), 0.001, 1.0, float)
            _thumb_scan_batch = _clamp_num(cfg.get('thumb_scan_batch', 100), 1, 10000)
            _thumb_scan_interval = _clamp_num(cfg.get('thumb_scan_interval', 0.5), 0.01, 60.0, float)
            _upload_max_size = _clamp_num(cfg.get('upload_max_size', 10 * 1024 * 1024 * 1024), 0, 1 << 40)
            _copy_buffer_size = _clamp_num(cfg.get('copy_buffer_size', 16 * 1024 * 1024), 4096, 16 << 20)
            _cache_max_items = _clamp_num(cfg.get('cache_max_items', 50), 0, 100000)
            _cache_ttl = _clamp_num(cfg.get('cache_ttl', 5), 0, 86400)
            _folder_size_ttl = _clamp_num(cfg.get('folder_size_ttl', 5), 0, 86400)
            _debounce_delay = _clamp_num(cfg.get('debounce_delay', 2.0), 0.0, 60.0, float)
            _max_api_body_size = _clamp_num(cfg.get('max_api_body_size', 1 * 1024 * 1024), 4096, 64 << 20)
            _preview_max_size = _clamp_num(cfg.get('preview_max_size', 10 * 1024 * 1024), 0, 1 << 30)
            _session_expiry_days = _clamp_num(cfg.get('session_expiry_days', 30), 1, 3650)
            _upload_chunk = _clamp_num(cfg.get('upload_chunk', 1 * 1024 * 1024), 4096, 16 << 20)
            _zip_max_files = _clamp_num(cfg.get('zip_max_files', 500), 0, 100000)
            _zip_streaming = _to_bool(cfg.get('zip_streaming', True))
            _tls_enabled = _to_bool(cfg.get('tls_enabled', True))
            _tls_cert = str(cfg.get('tls_cert', '') or '')
            _tls_key = str(cfg.get('tls_key', '') or '')
            _tls_trust_port = _clamp_num(cfg.get('tls_trust_port', 8082), 1024, 65535)
            _guest_public_write = _to_bool(cfg.get('guest_public_write', False))
            _downloader_guest_allowed = _to_bool(cfg.get('downloader_guest_allowed', False))
            _auto_trust_ca = _to_bool(cfg.get('auto_trust_ca', False))
            _ca_trust_decision = cfg.get('ca_trust_decision', 'unset')
            if _ca_trust_decision not in ('unset', 'accepted', 'declined'):
                _ca_trust_decision = 'unset'
            _trust_bind_host = str(cfg.get('trust_bind_host', '0.0.0.0') or '0.0.0.0').strip() or '0.0.0.0'
            _ca_validity_days = _clamp_num(cfg.get('ca_validity_days', 730), 1, 36500)
            _max_conn_per_ip = _clamp_num(cfg.get('max_conn_per_ip', 20), 1, 1000)
            _ws_max_conn_per_ip = _clamp_num(cfg.get('ws_max_conn_per_ip', 8), 1, 256)
            _access_log = _to_bool(cfg.get('access_log', True))
            _harden_config_acls_enabled = _to_bool(cfg.get('harden_config_acls', False))
    except Exception:
        pass
    # 载入时被钳制/归一过的键：盘上的值跟我们实际用的不一致 —— 那是**我们改的**。
    # 真机上配置改错了救不回来，所以这里先备份原文件、再记一条明确的日志，
    # 绝不静默改用户配置（备份文件是 server_config.json.bak，只在下一次钳制时覆盖）。
    drift = []
    try:
        # 覆盖**我们管理的全部键**（与 save_config 写出的那份同源，12 + 30 个），
        # 且比较**盘上的原值**而不是归一后的值 —— 否则 `'false'` / `'8082'` 这种
        # "语义等价、类型不对"的值永远不会被判定成 drift，也就永远不会被修正。
        _managed = {
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
            'http_port': PORT,
            'ws_port': WS_PORT,
        }
        _managed.update(get_deep_config_dict())
        drift = [(k, cfg[k], v) for k, v in _managed.items() if k in cfg and cfg[k] != v]
    except Exception:
        drift = []
    if drift:
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'rb') as _src:
                    _raw = _src.read()
                with open(CONFIG_FILE + '.bak', 'wb') as _dst:
                    _dst.write(_raw)
        except Exception:
            pass
        _detail = '; '.join('%s: %r -> %r' % (k, o, n) for k, o, n in drift)
        try:
            import logging as _lg
            _lg.getLogger('wifi_convey').warning(
                '配置载入时被钳制/归一（原文件已备份为 %s.bak）：%s',
                os.path.basename(CONFIG_FILE), _detail)
        except Exception:
            pass
        # 再写进**运行日志**（leaffs.log）—— 那才是管理页日志页能看到的地方
        try:
            from leaffs.runtime_log import add_log as _ral
            _ral('配置载入时被钳制/归一（原文件已备份为 server_config.json.bak）：' + _detail,
                 'warn')
        except Exception:
            pass
    if drift:
        # **只把被钳制的那几个键写回**，绝不整份重写 —— 全量写回会连带抹掉我们不认识的键
        # （用户手加的、将来版本才有的），那等于替用户重写配置。
        # 原子替换（tmp + os.replace）保持不变；写失败要说话，不能静默。
        try:
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as _rf:
                    _disk = json.load(_rf)
                if not isinstance(_disk, dict):
                    _disk = {}
            except Exception:
                _disk = {}
            for _k, _old, _new in drift:
                _disk[_k] = _new
            _tmp = '%s.%d.tmp' % (CONFIG_FILE, threading.get_ident())
            with open(_tmp, 'w', encoding='utf-8') as _wf:
                json.dump(_disk, _wf, indent=2)
            os.replace(_tmp, CONFIG_FILE)
        except Exception:
            try:
                import logging as _lg3
                _lg3.getLogger('wifi_convey').warning(
                    '配置钳制写回失败（原值仍在 %s.bak 里）', os.path.basename(CONFIG_FILE))
            except Exception:
                pass
    elif not os.path.exists(CONFIG_FILE):
        # 首次启动：盘上还没有配置文件，生成一份完整的
        save_config()
    sync_all_constants()
    import logging
    logging.getLogger('wifi_convey').info('配置已加载（共 %d 项）', len(get_deep_config_dict()))


def save_config():
    """把运行时配置写回 `server_config.json`；返回是否写成功。

    **读-改-写**：先读盘上的 dict，只覆盖我们自己管理的那些键，其余**原样保留** ——
    从内存变量整份重建再覆盖，会抹掉我们不认识的键（用户手加的、将来版本才有的），
    等于替用户重写配置。

    写失败**不能静默**：磁盘满 / 权限不足 / 文件被占用都会让配置停在内存里、
    下次重启就没了。调用方拿到 False 必须如实回给用户，不能让接口回 success。
    临时文件 + `os.replace` 保持原子替换（多线程并发写同一临时文件的问题照旧规避）。
    """
    with _config_lock:
        # 1) 读盘上的现状（读不到就当空：首次启动，或文件坏了）
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as _rf:
                disk = json.load(_rf)
            if not isinstance(disk, dict):
                disk = {}
        except Exception:
            disk = {}
        # 2) 只覆盖我们管理的键，其它键原样留着
        disk['max_concurrent'] = _max_concurrent
        disk['download_speed_limit'] = _speed_limit
        disk['guest_mode'] = _guest_mode
        disk['user_quota'] = _user_quota
        disk['public_quota'] = _public_quota
        disk['total_quota'] = _total_quota
        disk['tls_enabled'] = _tls_enabled
        disk['tls_cert'] = _tls_cert
        disk['tls_key'] = _tls_key
        disk['tls_trust_port'] = _tls_trust_port
        save_ports_to_config(disk)
        disk.update(get_deep_config_dict())
        # 3) 原子写回
        tmp = '%s.%d.tmp' % (CONFIG_FILE, threading.get_ident())
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(disk, f, indent=2)
            os.replace(tmp, CONFIG_FILE)
            return True
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass
            try:
                import logging as _lg
                _lg.getLogger('wifi_convey').exception('配置保存失败（本次改动没落盘）')
            except Exception:
                pass
            try:
                from leaffs.runtime_log import add_log as _ral
                _ral('配置保存失败：本次改动只在内存里，重启后会丢', 'err')
            except Exception:
                pass
            return False


def get_speed_limit(): return _speed_limit
def get_guest_mode(): return _guest_mode
def get_default_user_quota(): return _user_quota
def get_public_quota(): return _public_quota
def get_total_quota(): return _total_quota


def set_speed_limit(bytes_per_sec):
    """设置限速；返回是否落盘成功（False = 只在内存里，重启后会丢）"""
    global _speed_limit
    _speed_limit = max(0, bytes_per_sec)
    _user_limiter._users.clear()
    return save_config()


def set_guest_mode(val):
    """切换游客模式；返回是否落盘成功"""
    global _guest_mode
    _guest_mode = bool(val)
    return save_config()


def set_quotas(user_q=None, public_q=None, total_q=None):
    """改配额；返回是否落盘成功"""
    global _user_quota, _public_quota, _total_quota
    if user_q is not None: _user_quota = int(user_q)
    if public_q is not None: _public_quota = int(public_q)
    if total_q is not None: _total_quota = int(total_q)
    return save_config()


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
    """保存 TLS 开关（重启后由 run_http/run_ws 生效）；返回是否落盘成功"""
    global _tls_enabled
    with _config_lock:
        _tls_enabled = bool(enabled)
        ok = save_config()
    return ok
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
        return save_config()
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
        # HTTP/1.1（2026-09-18）：keep-alive 等待下一个请求的空闲超时（秒）
        'keepalive_timeout': _keepalive_timeout_secs,
        'guest_login_max_per_min': _guest_login_max_per_min,
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


def _clamp_num(v, lo, hi, cast=int):
    """配置文件**载入**时的容错钳制：值不合法就退回边界。

    与 `apply_deep_config` 的拒绝式校验配套 —— 那边拒绝非法输入，这边保证"盘上被手改坏的
    值"不会让服务带着非法状态跑起来（`upload_max_size` 为负、`max_total_conns` 为 0 之类）。
    `load_config` 末尾的 `save_config()` 会把钳制后的值写回。
    """
    try:
        v = cast(v)
    except Exception:
        return lo
    if isinstance(v, float) and not math.isfinite(v):
        return lo
    return lo if v < lo else (hi if v > hi else v)


# 深度配置的**已知键白名单**：不在此列的键一律拒绝。
# 原来未知键不进任何 `if` 分支 → changed 一直是 False → 函数却照样 return True，
# 接口回 {"success": true, "配置已更新"} —— 用户以为改成功了，其实什么都没发生。
_DEEP_KNOWN_KEYS = frozenset((
    'pbkdf2_iterations', 'salt_length',
    'thumb_sample_ratio', 'thumb_miss_threshold', 'thumb_scan_batch', 'thumb_scan_interval',
    'upload_max_size', 'copy_buffer_size', 'cache_max_items', 'cache_ttl', 'folder_size_ttl',
    'debounce_delay', 'max_api_body_size', 'preview_max_size', 'upload_chunk', 'zip_max_files',
    'zip_streaming', 'max_total_conns', 'io_idle_timeout_secs', 'keepalive_timeout',
    'session_expiry_days',
    'guest_public_write', 'downloader_guest_allowed', 'auto_trust_ca', 'ca_trust_decision',
    'access_log', 'trust_bind_host', 'ca_validity_days', 'max_conn_per_ip',
    'ws_max_conn_per_ip', 'harden_config_acls',
))


def _deep_num(data, key, cast, lo, hi=None):
    """取一个数值键并校验区间，返回 (值, 错误文案)。

    越界一律**拒绝**，不做静默钳制 —— 钳制等于"用户提交 0、系统悄悄存成别的值"，
    和"未知键假成功"是同一类毛病。hi=None 表示只限下限。
    """
    raw = data[key]
    if isinstance(raw, bool):
        # JSON 的 true/false 落到数值键上没有意义（int(True)==1），直接拒
        return None, '%s 必须是数字' % key
    try:
        val = cast(raw)
    except Exception:
        return None, '%s 必须是数字' % key
    if isinstance(val, float) and not math.isfinite(val):
        return None, '%s 必须是有限数字' % key
    if val < lo or (hi is not None and val > hi):
        if hi is None:
            return None, '%s 不得低于 %s' % (key, lo)
        return None, '%s 必须在 %s~%s 之间' % (key, lo, hi)
    return val, None


def _deep_bool(data, key):
    """取一个布尔键，只接受 JSON 的 true/false。

    配置文件那边仍用宽松的 `_to_bool`（历史落盘值可能是字符串），但 API 输入要严格：
    否则 `access_log: "garbage"` 会被静默当成 False —— 又是一种"假成功"。
    """
    v = data[key]
    if isinstance(v, bool):
        return v, None
    return None, '%s 必须是 true 或 false' % key


def apply_deep_config(data):
    """应用深度配置并返回 (ok: bool, err: str, changed: bool)。

    B-11：pbkdf2_iterations 低于 PBKDF2_ITER_MIN / salt_length 低于 SALT_LEN_MIN →
    返回 (False, 具体文案) 且本次整体失败（不落任何键、不落盘），由 cfg_api 转 400。
    全部通过才保存并同步运行时常量；成功返回 (True, '', changed)。
    未知键一律拒绝（不静默忽略）；数值键越界一律拒绝（不静默钳制）。
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
        global _max_total_conns, _max_concurrent, _io_idle_timeout_secs, _keepalive_timeout_secs
        if not isinstance(data, dict):
            return False, '请求体必须是 JSON 对象', False
        unknown = sorted(k for k in data if k not in _DEEP_KNOWN_KEYS)
        if unknown:
            return False, '未知配置项：' + '、'.join(unknown), False
        changed = False
        # B-11：pbkdf2_iterations / salt_length 只限下限 —— 下调口令哈希强度必须整体失败，
        # 不落任何键、不改运行时值。
        if 'pbkdf2_iterations' in data:
            # 下限是 B-11（不得下调哈希强度）；上限防"设成天文数字让每次登录卡死"
            val, err = _deep_num(data, 'pbkdf2_iterations', int, PBKDF2_ITER_MIN, 10000000)
            if err: return False, err, False
            _pbkdf2_iterations = val; changed = True
        if 'salt_length' in data:
            val, err = _deep_num(data, 'salt_length', int, SALT_LEN_MIN, 64)
            if err: return False, err, False
            _salt_length = val; changed = True
        # 这四个原来写成 `if 0.001 <= val <= 1.0: 赋值` —— 越界**静默丢掉**、连错都不报，
        # 而 float()/int() 没包 try 时类型错误又会 500。统一改成拒绝式。
        if 'thumb_sample_ratio' in data:
            val, err = _deep_num(data, 'thumb_sample_ratio', float, 0.001, 1.0)
            if err: return False, err, False
            _thumb_sample_ratio = val; changed = True
        if 'thumb_miss_threshold' in data:
            val, err = _deep_num(data, 'thumb_miss_threshold', float, 0.001, 1.0)
            if err: return False, err, False
            _thumb_miss_threshold = val; changed = True
        if 'thumb_scan_batch' in data:
            val, err = _deep_num(data, 'thumb_scan_batch', int, 1, 10000)
            if err: return False, err, False
            _thumb_scan_batch = val; changed = True
        if 'thumb_scan_interval' in data:
            val, err = _deep_num(data, 'thumb_scan_interval', float, 0.01, 60.0)
            if err: return False, err, False
            _thumb_scan_interval = val; changed = True
        if 'upload_max_size' in data:
            # 0 = 不限制（页面文案如此）；非 0 时必须够大，否则等于把上传功能关掉
            val, err = _deep_num(data, 'upload_max_size', int, 0, 1 << 40)
            if err: return False, err, False
            if 0 < val < (1 << 20):
                return False, 'upload_max_size 取 0（不限制）或不小于 1 MiB', False
            _upload_max_size = val; changed = True
        if 'copy_buffer_size' in data:
            val, err = _deep_num(data, 'copy_buffer_size', int, 4096, 16 << 20)
            if err: return False, err, False
            _copy_buffer_size = val; changed = True
        # cache_* / folder_size_ttl：页面文案是「0 = 禁用缓存」→ 下限必须是 0 不是 1
        if 'cache_max_items' in data:
            val, err = _deep_num(data, 'cache_max_items', int, 0, 100000)
            if err: return False, err, False
            _cache_max_items = val; changed = True
        if 'cache_ttl' in data:
            val, err = _deep_num(data, 'cache_ttl', int, 0, 86400)
            if err: return False, err, False
            _cache_ttl = val; changed = True
        if 'folder_size_ttl' in data:
            val, err = _deep_num(data, 'folder_size_ttl', int, 0, 86400)
            if err: return False, err, False
            _folder_size_ttl = val; changed = True
        if 'debounce_delay' in data:
            val, err = _deep_num(data, 'debounce_delay', float, 0.0, 60.0)
            if err: return False, err, False
            _debounce_delay = val; changed = True
        if 'max_api_body_size' in data:
            val, err = _deep_num(data, 'max_api_body_size', int, 4096, 64 << 20)
            if err: return False, err, False
            _max_api_body_size = val; changed = True
        if 'preview_max_size' in data:
            val, err = _deep_num(data, 'preview_max_size', int, 0, 1 << 30)
            if err: return False, err, False
            _preview_max_size = val; changed = True
        if 'upload_chunk' in data:
            # 原来是 max(4096, int(...)) 静默钳制，且 int() 未包 try（类型错误 → 500）
            val, err = _deep_num(data, 'upload_chunk', int, 4096, 16 << 20)
            if err: return False, err, False
            _upload_chunk = val; changed = True
        if 'zip_max_files' in data:
            # 0 = 不限（页面文案如此）
            val, err = _deep_num(data, 'zip_max_files', int, 0, 100000)
            if err: return False, err, False
            _zip_max_files = val; changed = True
        if 'zip_streaming' in data:
            val, err = _deep_bool(data, 'zip_streaming')
            if err: return False, err, False
            _zip_streaming = val; changed = True
        # R4：整机总连接/线程准入上限（无排队资源保护；改小可即时收紧、改大立即放量）。
        # UI“并发数”(max_concurrent) 回显随本值同步镜像，保证显示即真实。
        # 校验与 B-11（pbkdf2/salt）同模式：非法值整体失败(400)、不改运行时值、不落盘。
        if 'max_total_conns' in data:
            # 下限 8：改成 1 会让服务**自锁** —— 连"改回来"的请求都抢不到连接槽，
            # 全员 503 且改不回来（黑盒测试实测到过这个状态）。
            val, err = _deep_num(data, 'max_total_conns', int, 8, 20000)
            if err: return False, err, False
            _max_total_conns = val
            _max_concurrent = val
            changed = True
        # R4：慢客户端写侧无进展超时（秒，1~3600）；非法/非有限值整体失败(400)、不改值
        if 'io_idle_timeout_secs' in data:
            val, err = _deep_num(data, 'io_idle_timeout_secs', float, 1.0, 3600.0)
            if err: return False, err, False
            _io_idle_timeout_secs = val
            changed = True
        # HTTP/1.1（2026-09-18）：keep-alive 空闲超时（秒，1~300）。上限比读超时那档小得多
        # —— 它只是"等下一个请求"的耐心，设大了就回到"空闲连接占满线程"的老问题。
        if 'keepalive_timeout' in data:
            val, err = _deep_num(data, 'keepalive_timeout', float, 1.0, 300.0)
            if err: return False, err, False
            _keepalive_timeout_secs = val
            changed = True
        if 'session_expiry_days' in data:
            val, err = _deep_num(data, 'session_expiry_days', int, 1, 3650)
            if err: return False, err, False
            _session_expiry_days = val; changed = True
        # IC-CFG（R3）新增键
        if 'guest_public_write' in data:
            val, err = _deep_bool(data, 'guest_public_write')
            if err: return False, err, False
            _guest_public_write = val; changed = True
        if 'downloader_guest_allowed' in data:
            val, err = _deep_bool(data, 'downloader_guest_allowed')
            if err: return False, err, False
            _downloader_guest_allowed = val; changed = True
        if 'auto_trust_ca' in data:
            val, err = _deep_bool(data, 'auto_trust_ca')
            if err: return False, err, False
            _old_auto = _auto_trust_ca
            _auto_trust_ca = val; changed = True
            if _auto_trust_ca and not _old_auto:
                # 用户决定制：深度配置重新开启 = 重新同意 → 清除既往 declined，下次启动重新询问/安装
                _ca_trust_decision = 'unset'
        if 'ca_trust_decision' in data:
            # 原来是"取值不认识就静默丢掉"（还照样回 success）—— 改成明确拒绝
            _d = str(data['ca_trust_decision']).strip().lower()
            if _d not in ('unset', 'accepted', 'declined'):
                return False, 'ca_trust_decision 只能是 unset/accepted/declined', False
            _ca_trust_decision = _d; changed = True
        if 'access_log' in data:
            val, err = _deep_bool(data, 'access_log')
            if err: return False, err, False
            _access_log = val; changed = True
        if 'trust_bind_host' in data:
            # 只查"非空"不够：这个值会被拿去绑监听地址，必须是真正的 IP / 主机名
            from leaffs.server.hosts import is_valid_host
            host = str(data['trust_bind_host']).strip()
            if not host:
                return False, 'trust_bind_host 不能为空', False
            if not is_valid_host(host):
                return False, 'trust_bind_host 不是合法的主机名或 IP', False
            _trust_bind_host = host; changed = True
        if 'ca_validity_days' in data:
            val, err = _deep_num(data, 'ca_validity_days', int, 1, 36500)
            if err: return False, err, False
            _ca_validity_days = val; changed = True
        if 'max_conn_per_ip' in data:
            val, err = _deep_num(data, 'max_conn_per_ip', int, 1, 1000)
            if err: return False, err, False
            _max_conn_per_ip = val; changed = True
        if 'ws_max_conn_per_ip' in data:
            val, err = _deep_num(data, 'ws_max_conn_per_ip', int, 1, 256)
            if err: return False, err, False
            _ws_max_conn_per_ip = val
            changed = True
        if 'harden_config_acls' in data:
            val, err = _deep_bool(data, 'harden_config_acls')
            if err: return False, err, False
            _harden_config_acls_enabled = val; changed = True
        if changed:
            if not save_config():
                return False, '保存失败：改动只在内存里，重启后会丢', False
            sync_all_constants()
        return True, '', changed


def sync_all_constants():
    import leaffs.utils.core as _cu
    _cu.COPY_BUFFER_SIZE = _copy_buffer_size
    _cu.CACHE_MAX_ITEMS = _cache_max_items
    _cu.CACHE_TTL = _cache_ttl
    _cu.FOLDER_SIZE_TTL = _folder_size_ttl
    _cu.THUMB_SAMPLE_RATIO = _thumb_sample_ratio
    _cu.THUMB_MISS_THRESHOLD = _thumb_miss_threshold
    _cu.THUMB_SCAN_BATCH = _thumb_scan_batch
    _cu.THUMB_SCAN_INTERVAL = _thumb_scan_interval
    _cu.DEBOUNCE_DELAY = _debounce_delay

    import leaffs.auth.core as _aa
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
def get_keepalive_timeout_secs(): return _keepalive_timeout_secs  # HTTP/1.1：空闲超时（秒）


def get_guest_login_max_per_min(): return _guest_login_max_per_min  # 游客登录限速上限（次/分钟）

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
        new_max = int(new_max)
    except Exception:
        return False, '并发数必须是整数'
    # 下限与 apply_deep_config 对齐（8）：设成 1 会让服务**自锁**（连改回来的请求都抢不到槽）
    if new_max < 8 or new_max > 20000:
        return False, '并发数必须在 8~20000 之间'
    _max_total_conns = new_max
    _max_concurrent = new_max
    if not save_config():
        return False, '保存失败：改动只在内存里，重启后会丢'
    return True, ''


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
    if not save_config():
        return False, '保存失败：端口只在内存里，重启后仍是原端口'
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
        ok = save_config()
    if not ok:
        return False, '保存失败：引导页端口只在内存里，重启后仍是原端口'
    return True, '引导页端口已保存，重启后生效'