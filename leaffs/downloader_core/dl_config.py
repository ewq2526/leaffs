#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
下载器独立配置管理
持久化到硬盘 JSON 文件，不依赖服务端配置
"""

import os
import json
import threading

from leaffs.utils_core.ut_core import CONFIG_DIR

# 配置文件目录（数据层 config/，跟随主程序所在位置，打包后可写）
CONFIG_FILE = os.path.join(CONFIG_DIR, 'downloader_config.json')

# IC-DLCFG（§R3 唯一签名事实源）：下载器安全/配额键，全部归 downloader_core 独立管理，
# 不依赖服务端 cfg_core（cfg_core 键不归本模块管）。
_default_config = {
    'max_concurrent': 3,
    'speed_limit': 0,  # bytes/s, 0=不限速
    # C-08a [D8]：下载目标默认必须公网（is_global）；true 时放行 RFC1918/ULA 私网，
    # 但回环/链路本地/云 metadata 等仍禁（见 dl_utils._ip_allowed）
    'allow_private_targets': False,
    # C-09/IC-DLCFG：任务与配额上限
    'max_user_tasks': 20,       # 每用户（含 游客@ip）任务总数上限
    'max_user_active': 5,       # 每用户同时 downloading/waiting 上限
    'max_total_tasks': 200,     # 全池任务总数上限
    'max_task_age_days': 7,     # completed 任务超龄自动清理（天）
    'max_download_size': 0,     # 单任务(HTTP 直链)大小上限字节，0=不限
    # C-14：DHT 对外暴露开关（false=仅本机监听 + 关 LPD）
    'bt_dht_public': False,
}

# 可重入锁：save_config 持锁期间调用 load_config（首次缓存未建立时需再取锁），
# 用 RLock 避免同一线程自锁死（线程内单次即可，无跨线程语义变化）。
_config_lock = threading.RLock()
_config_cache = None

def _ensure_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)

def load_config():
    """加载配置，返回 dict"""
    global _config_cache
    if _config_cache is not None:
        return dict(_config_cache)
    with _config_lock:
        if _config_cache is not None:
            return dict(_config_cache)
        _ensure_dir()
        cfg = dict(_default_config)
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        cfg.update(data)
            except Exception:
                pass
        _config_cache = cfg
        return dict(cfg)

def save_config(**kwargs):
    """保存配置项到文件"""
    global _config_cache
    with _config_lock:
        cfg = load_config()
        for k, v in kwargs.items():
            if v is not None:
                cfg[k] = v
        _config_cache = dict(cfg)
        _ensure_dir()
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return dict(cfg)

def get_max_concurrent():
    return load_config().get('max_concurrent', 3)

def get_speed_limit():
    return load_config().get('speed_limit', 0)

# ========== C-08a/C-09/C-14 配置键 getter（IC-DLCFG，§R3） ==========

def _as_bool(v):
    return bool(v) if isinstance(v, bool) else str(v).strip().lower() in ('1', 'true', 'yes', 'on')

def _as_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

def get_allow_private_targets():
    return _as_bool(load_config().get('allow_private_targets', False))

def get_max_user_tasks():
    return _as_int(load_config().get('max_user_tasks', 20), 20)

def get_max_user_active():
    return _as_int(load_config().get('max_user_active', 5), 5)

def get_max_total_tasks():
    return _as_int(load_config().get('max_total_tasks', 200), 200)

def get_max_task_age_days():
    return _as_int(load_config().get('max_task_age_days', 7), 7)

def get_max_download_size():
    return _as_int(load_config().get('max_download_size', 0), 0)

def get_bt_dht_public():
    return _as_bool(load_config().get('bt_dht_public', False))

def set_max_concurrent(val):
    return save_config(max_concurrent=max(1, min(10, int(val))))

def set_speed_limit(val):
    return save_config(speed_limit=max(0, int(val)))