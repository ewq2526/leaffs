# -*- coding: utf-8 -*-
"""运行日志服务 —— leaffs 进程的运行日志（内存环形缓冲 + 文件/控制台）。

历史：原定义于 leaffs.py 顶部，重构抽出为独立模块；调用方通过
from leaffs.runtime_log import add_log, get_logs, ... 引用，语义不变。
"""
import logging
import os
import threading
import time

from leaffs.paths import PROJECT_DIR

# leaffs 进程主日志 logger（文件 + 控制台由 setup_logging 装配）
logger = logging.getLogger('leaffs')
LOG_FILE = os.path.join(PROJECT_DIR, 'leaffs.log')


def setup_logging():
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S')
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
    fh.setFormatter(fmt)
    logger.addHandler(fh)


_server_logs = []
_server_logs_lock = threading.Lock()


def add_log(msg, level='info'):
    # 内存运行日志与文件/控制台日志统一带日期（A-16：%H:%M:%S → %Y-%m-%d %H:%M:%S）
    now = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    with _server_logs_lock:
        _server_logs.append({'time': now, 'msg': str(msg), 'level': level})
        if len(_server_logs) > 200:
            _server_logs[:] = _server_logs[-200:]
    if level == 'err':
        logger.error(msg)
    elif level == 'warn':
        logger.warning(msg)
    else:
        logger.info(msg)


def get_logs():
    with _server_logs_lock:
        return list(_server_logs)


def clear_runtime_logs():
    with _server_logs_lock:
        _server_logs[:] = []
