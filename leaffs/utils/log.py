"""日志模块 — 标准日志/运行日志/日志配置"""

import logging

# LF-12：脱敏工具移到了 runtime_log（那样 add_log 自己就能清理，不必靠调用方记得），
# 这里 re-export 保持对外接口不变 —— login_api 等仍在用 _ut_log.sanitize_log_text。
from leaffs.runtime_log import sanitize_log_text  # noqa: F401


# ========== 标准日志模块配置 ==========
logger = logging.getLogger('wifi_convey')
LOG_FILE = None  # 由 setup_logging 初始化

# B-13 安全事件附加回调（如运行日志 add_log），由 register_add_log 注入；未注入仅写 logger
_add_log_hook = None


def setup_logging(LOG_FILE, add_log):
    """配置标准 logging 模块：输出到控制台和文件"""
    logger.setLevel(logging.INFO)
    # B-13：日期格式补日期（与 A-16 侧 leaffs logger 一致）
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s',
                                  datefmt='%Y-%m-%d %H:%M:%S')
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
    fh.setFormatter(formatter)
    logger.addHandler(fh)


def register_add_log(callback):
    """注册安全事件附加回调（IC-LOG 启动注入点）。回调签名 add_log(msg, level)。"""
    global _add_log_hook
    _add_log_hook = callback


# sanitize_log_text 已移到 leaffs.runtime_log（见文件顶部的 re-export）


def security_event(kind, detail, level='warn'):
    """安全事件统一出口（IC-LOG/B-13）：写 'wifi_convey' logger，
    若已通过 register_add_log 注册回调则同步写入运行日志。detail 先经 sanitize_log_text。

    kind：事件类别（account_lock / session_revoke / tls_off / qr_issue ...）。
    """
    lvl = str(level).lower()
    if lvl == 'warning':
        lvl = 'warn'
    if lvl not in ('debug', 'info', 'warn', 'error', 'critical'):
        lvl = 'warn'
    msg = sanitize_log_text(f'[{kind}] {detail}')
    log = logging.getLogger('wifi_convey')
    if lvl == 'debug':
        log.debug(msg)
    elif lvl == 'info':
        log.info(msg)
    elif lvl == 'error':
        log.error(msg)
    elif lvl == 'critical':
        log.critical(msg)
    else:
        log.warning(msg)
    cb = _add_log_hook
    if cb is not None:
        try:
            if lvl in ('warn', 'error', 'critical'):
                cb(msg, 'warn')
            elif lvl == 'info':
                cb(msg, 'info')
            else:
                cb(msg, 'ok')
        except Exception:
            pass


def serve_logs(handler, get_logs):
    """返回运行日志"""
    role = handler._get_effective_role()
    if role not in ('admin', 'super_admin'):
        handler.send_json({'error': 'Forbidden'}, 403)
        return
    handler.send_json({'logs': get_logs()})


def clear_logs(handler, clear_fn):
    """清空运行日志（仅管理员）"""
    role = handler._get_effective_role()
    if role not in ('admin', 'super_admin'):
        handler.send_json({'error': 'Forbidden'}, 403)
        return
    clear_fn()
    handler.send_json({'success': True})


def show_role_display(role_str):
    return {'super_admin': '超级管理员', 'admin': '管理员', 'user': '用户', 'guest': '游客'}.get(role_str, role_str)