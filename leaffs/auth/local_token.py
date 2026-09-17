# -*- coding: utf-8 -*-
"""本机一次性登录令牌（D4）—— /login?leaf= 与 WS auth token 共用。

历史：原为 leaffs.py 模块级状态（_LOCAL_TOKEN/LOCAL_TOKEN_FILE/读写删函数），
重构抽出；HTTP 令牌登录、WS auth token 分支、启动生成统一经本模块。
每次服务启动生成新令牌并写入 config/local_token.txt；令牌一次性，
首个成功使用（恒定时间比对）后立即失效并删除文件。
"""
import os
import secrets
import threading

from leaffs.paths import CONFIG_DIR
from leaffs.runtime_log import add_log

LOCAL_TOKEN_FILE = os.path.join(CONFIG_DIR, 'local_token.txt')

_token = None
_lock = threading.Lock()


def get_current():
    """当前生效令牌（未生成 / 已消费返回 None）"""
    with _lock:
        return _token


def reset_and_write():
    """生成新一次性令牌并落盘（服务启动时调用）；返回令牌"""
    global _token
    with _lock:
        _token = secrets.token_urlsafe(24)
        tok = _token
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(LOCAL_TOKEN_FILE, 'w', encoding='utf-8') as f:
            f.write(tok)
        try:
            os.chmod(LOCAL_TOKEN_FILE, 0o600)
        except Exception:
            pass
    except Exception as e:
        add_log(f'写入本机登录令牌文件失败: {e}', 'warn')
    return tok


def _delete_file():
    try:
        if os.path.exists(LOCAL_TOKEN_FILE):
            os.remove(LOCAL_TOKEN_FILE)
    except Exception:
        pass


def try_consume(candidate):
    """恒定时间比对候选令牌；成功则令牌立即失效并删除临时文件，返回 True。

    比对和置空必须在**同一把锁**里：原来是两条独立语句、无锁，并发打同一令牌
    可能换出多个 super_admin 会话（"一次性"并不严格）。

    B1：候选先过形状校验。`secrets.compare_digest` 的契约是「两个 ASCII str 或两个
    bytes」—— 非 ASCII 的 str（`/login?leaf=中文`）或非 str（`{"token":123}`）都会
    **抛 TypeError**，实际后果是 HTTP 回 500、WS 连接被以 1011 关掉。畸形候选一律按
    「不匹配」处理：既不进比对，也不消耗令牌（真令牌仍可正常兑现）。
    """
    global _token
    if not candidate:
        return False
    if not isinstance(candidate, str) or not candidate.isascii():
        return False
    with _lock:
        if not _token:
            return False
        if not secrets.compare_digest(candidate, _token):
            return False
        _token = None
        _delete_file()
        return True
