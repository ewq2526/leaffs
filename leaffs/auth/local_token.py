# -*- coding: utf-8 -*-
"""本机一次性登录令牌（D4）—— /login?leaf= 与 WS auth token 共用。

历史：原为 leaffs.py 模块级状态（_LOCAL_TOKEN/LOCAL_TOKEN_FILE/读写删函数），
重构抽出；HTTP 令牌登录、WS auth token 分支、启动生成统一经本模块。
每次服务启动生成新令牌并写入 config/local_token.txt；令牌一次性，
首个成功使用（恒定时间比对）后立即失效并删除文件。
"""
import os
import secrets

from leaffs.paths import CONFIG_DIR
from leaffs.runtime_log import add_log

LOCAL_TOKEN_FILE = os.path.join(CONFIG_DIR, 'local_token.txt')

_token = None


def get_current():
    """当前生效令牌（未生成/已消费返回 None）"""
    return _token


def reset_and_write():
    """生成新一次性令牌并落盘（服务启动时调用）；返回令牌"""
    global _token
    _token = secrets.token_urlsafe(24)
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(LOCAL_TOKEN_FILE, 'w', encoding='utf-8') as f:
            f.write(_token)
        try:
            os.chmod(LOCAL_TOKEN_FILE, 0o600)
        except Exception:
            pass
    except Exception as e:
        add_log(f'写入本机登录令牌文件失败: {e}', 'warn')
    return _token


def _delete_file():
    try:
        if os.path.exists(LOCAL_TOKEN_FILE):
            os.remove(LOCAL_TOKEN_FILE)
    except Exception:
        pass


def try_consume(candidate):
    """恒定时间比对候选令牌；成功则令牌立即失效并删除临时文件，返回 True"""
    global _token
    if not candidate or not _token:
        return False
    if not secrets.compare_digest(candidate, _token):
        return False
    _token = None
    _delete_file()
    return True
