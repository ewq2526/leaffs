# -*- coding: utf-8 -*-
"""运行日志服务 —— leaffs 进程的运行日志（内存环形缓冲 + 文件/控制台）。

历史：原定义于 leaffs.py 顶部，重构抽出为独立模块；调用方通过
from leaffs.runtime_log import add_log, get_logs, ... 引用，语义不变。
"""
import logging
import os
import re
import threading
import time

from leaffs.paths import PROJECT_DIR

# leaffs 进程主日志 logger（文件 + 控制台由 setup_logging 装配）
logger = logging.getLogger('leaffs')
LOG_FILE = os.path.join(PROJECT_DIR, 'leaffs.log')

# 控制字符（\r \n \x00 之外，保留 \t 兼容表格日志）
_CTRL_CHARS_RE = re.compile(r'[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]')


def sanitize_log_text(text, max_len=200):
    """日志文本脱敏：剔除 \\r \\n \\x00 与其余控制字符，超长截断。

    放在这一层（而不是 `utils/log`）是为了让 **add_log 自己**就能清理 ——
    用户可控文本入日志的入口有一堆（请求行、用户名、异常详情……），
    靠"每个调用方都记得先 sanitize"是防不住的，已经漏过一次（LF-12：访问日志的请求行）。

    `max_len=200` 是给"短字段"（路径、用户名）用的；`add_log` 内部**不截断**，
    因为 `log_exception` 要写完整 traceback，截了就把线索丢了。
    """
    if text is None:
        return ''
    s = str(text)
    s = s.replace('\r', ' ').replace('\n', ' ').replace('\x00', '')
    s = _CTRL_CHARS_RE.sub('', s)
    s = s.strip()
    if max_len and len(s) > max_len:
        s = s[:max_len] + '...'
    return s


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
    # LF-12：任何日志入口一律剔除控制字符 —— 原始字节直发的请求行能把 \x1b 这类控制字节
    # 带进日志，落到文件里会让终端显示错乱。这里**不截断**（见 sanitize_log_text 的说明）。
    msg = sanitize_log_text(msg, max_len=None)
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


def log_exception(where, exc=None):
    """把兜底异常的详情记进运行日志 —— **绝不回给客户端**。

    为什么不能把 `str(exc)` 放进 HTTP 响应：Windows 的 OSError 文本自带完整绝对路径
    （`[WinError 267] ... 'E:\\test\\shared_files\\public\\x'`），黑盒渗透测试正是靠它
    把服务器目录布局还原出来的。`where` 写清是哪个操作挂了，方便对着日志排障。
    """
    try:
        if exc is None:
            import traceback
            detail = traceback.format_exc()
        else:
            detail = '%s: %s' % (type(exc).__name__, exc)
        add_log('%s 失败: %s' % (where, detail), 'err')
    except Exception:
        pass


def get_logs():
    with _server_logs_lock:
        return list(_server_logs)


def clear_runtime_logs():
    with _server_logs_lock:
        _server_logs[:] = []
