#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 ewq2526. 许可见 LICENSE.txt；联系 ewq2526@163.com
"""LeafFS - 主入口：路由分发 + 服务器启动"""

import os
import sys
# 确保项目根目录（leaffs 的父目录）在 sys.path 中
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
import json
import socket
import webbrowser
import urllib.parse
import asyncio
import websockets
import threading
import time
import logging
import re
import secrets
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

import leaffs.auth_account_core.ac_core as _ac
import leaffs.auth_account_core.ac_auth as _ac_auth
import leaffs.auth_account_core.ac_user as _ac_user
import leaffs.auth_account_core.ac_session as _ac_session
import leaffs.config_core.cfg_core as _cfg
import leaffs.config_core.cfg_api as _cfg_api
import leaffs.file_system_core.fs_core as _fs
import leaffs.file_system_core.fs_api as _fs_api
import leaffs.utils_core.ut_log as _ut_log
import leaffs.web_management.wm_page as _wm
import leaffs.utils_core.ut_core as _ut
from leaffs.downloader_core.dl_core import DownloadManager as _DLManager, set_broadcast_fn as _set_broadcast_fn
from leaffs.downloader_core import dl_api as _dl_api
from leaffs.downloader_core.dl_rpc import _kill_aria2c_force

# 下载管理器
_dl_manager = _DLManager()
_dl_api.set_dl_manager(_dl_manager)
def get_dl_manager(): return _dl_manager

# 日志
logger = logging.getLogger('leaffs')
LOG_FILE = os.path.join(_ut.PROJECT_DIR, 'leaffs.log')

def setup_logging():
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
    fh.setFormatter(fmt)
    logger.addHandler(fh)

MAX_API_BODY_SIZE = 1024 * 1024
PREVIEW_MAX_SIZE = 10 * 1024 * 1024   # 在线预览大小上限（可由 cfg_core 动态覆盖，见 sync_all_constants）

_server_logs = []
_server_logs_lock = threading.Lock()

def add_log(msg, level='info'):
    # 内存运行日志与文件/控制台日志统一带日期（A-16：%H:%M:%S → %Y-%m-%d %H:%M:%S）
    now = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    with _server_logs_lock:
        _server_logs.append({'time': now, 'msg': str(msg), 'level': level})
        if len(_server_logs) > 200: _server_logs[:] = _server_logs[-200:]
    if level == 'err': logger.error(msg)
    elif level == 'warn': logger.warning(msg)
    else: logger.info(msg)

def get_logs():
    with _server_logs_lock: return list(_server_logs)

def clear_runtime_logs():
    with _server_logs_lock: _server_logs[:] = []

# ---------- 本机一次性登录令牌（D4；启动时生成；登录用，与证书机制无关） ----------
_LOCAL_TOKEN = None            # 本机自动登录一次性令牌（/login?leaf= 与 WS auth token 共用）
LOCAL_TOKEN_FILE = os.path.join(_ut.CONFIG_DIR, 'local_token.txt')

def _write_local_token_file(token):
    """尽力写 config/local_token.txt（0600），失败仅 warn 不阻断启动"""
    try:
        os.makedirs(_ut.CONFIG_DIR, exist_ok=True)
        with open(LOCAL_TOKEN_FILE, 'w', encoding='utf-8') as f:
            f.write(token)
        try:
            os.chmod(LOCAL_TOKEN_FILE, 0o600)
        except Exception:
            pass
    except Exception as e:
        add_log(f'写入本机登录令牌文件失败: {e}', 'warn')

def _delete_local_token_file():
    try:
        if os.path.exists(LOCAL_TOKEN_FILE):
            os.remove(LOCAL_TOKEN_FILE)
    except Exception:
        pass

def _strip_host_port(host):
    """Host/Origin host → 裸主机名（小写、去端口/方括号；IPv6 字面量原样保留）

    IPv4「host:port」与「host」行为不变；IPv6 三种形态都正确归约：
    `[::1]:8080` → `::1`；`[::1]` → `::1`；裸 `::1`/`2001:db8::1`（含 ':'>1）不再被
    当成 host:port 截断（旧实现把 `::1` rsplit(':',1) 成 `:`，导致 Origin http://[::1]:8080
    白名单校验失败被误拒）。
    """
    h = (host or '').strip().lower()
    if not h:
        return ''
    if h.startswith('['):
        end = h.find(']')
        if end != -1:
            return h[1:end]
        return h
    if h.count(':') > 1:
        return h  # 裸 IPv6（无端口段）：不作 host:port 拆分
    if ':' in h:
        h = h.rsplit(':', 1)[0]
    return h

_LOCAL_HOST_NAMES = frozenset(('localhost', '127.0.0.1', '::1'))

# ---------- A-01：管理类 API 统一 admin/super_admin 门槛 ----------
_ADMIN_ONLY_POST = frozenset((
    '/api/users/add', '/api/users/delete', '/api/users/password', '/api/users/role',
    '/api/users/speed', '/api/users/quota', '/api/users/rename',
    '/api/config', '/api/config/advanced', '/api/config/deep',
    '/api/certs/reset', '/api/logs/clear', '/api/session/revoke',
))
_ADMIN_ONLY_GET = frozenset((
    '/api/users', '/api/config', '/api/config/advanced', '/api/config/deep',
    '/api/logs', '/api/connections', '/api/sessions',
))

# 实测收口：请求 Cookie 携带 wifi_session 但会话无效/过期时，/api 请求在路由前直接 401
# （不再静默降级为匿名/游客）。以下白名单保持原语义：公开探测/登录/登出/状态轮询/取 sid。
# /api/qrlogin 必须豁免：扫码设备往往带着一条早先的（已失效）Cookie 来打开二维码地址，
# 若被 401 拦截将永远无法扫码登录——该接口本就匿名可调、成功时直接换发全新会话。
_INVALID_SESSION_API_WHITELIST = frozenset((
    '/api/ping', '/api/auth/login', '/api/guest/login', '/api/auth/logout',
    '/api/auth/check', '/api/qrcode/status', '/api/qrlogin', '/api/session/sid',
))

# ---------- A-11：请求级时长 / 每 IP HTTP 连接配额 / 整机总并发准入 ----------
REQ_TOTAL_TIMEOUT = 180       # 单请求总时长预算（秒）；上传/下载/打包长流豁免
# 架构修订 R4：请求级“全局并发槽”已整体取消（不再有 CONC_SLOT_TIMEOUT 等待/503）；
# 资源保护改由整机“总连接/线程准入”（cfg_core.max_total_conns，默认 256）+ 每 IP 连接
# 上限承担，两者都是“超限立即拒绝、绝不排队”。长流路径：不套用“请求总时长”预算。
_TOTAL_TIMEOUT_EXEMPT = ('/download/', '/api/upload', '/api/zip')

# 总并发超限告警节流（日志不刷屏）
_overcap_warn_lock = threading.Lock()
_overcap_last_warn = 0.0
_OVERCAP_WARN_INTERVAL = 5.0

def _overcap_warn():
    """并发超限事件告警：每 _OVERCAP_WARN_INTERVAL 秒至多记一条（attack 日志洪峰抑制）"""
    global _overcap_last_warn
    now = time.monotonic()
    with _overcap_warn_lock:
        if now - _overcap_last_warn < _OVERCAP_WARN_INTERVAL:
            return
        _overcap_last_warn = now
    try:
        add_log(f'总并发已达上限（{_cfg.get_max_total_conns()}），已拒绝新连接（503/关闭，不排队）', 'warn')
    except Exception:
        pass

_http_ip_conns = {}
_http_ip_conns_lock = threading.Lock()

def _http_ip_conn_acquire(ip):
    """每 IP 活跃 HTTP 连接配额（cfg max_conn_per_ip，默认 20）；返回是否放行"""
    limit = 20
    try:
        limit = int(_cfg.get_max_conn_per_ip())
    except Exception:
        pass
    if limit <= 0:
        return True
    with _http_ip_conns_lock:
        n = _http_ip_conns.get(ip, 0)
        if n >= limit:
            return False
        _http_ip_conns[ip] = n + 1
        return True

def _http_ip_conn_release(ip):
    with _http_ip_conns_lock:
        n = _http_ip_conns.get(ip, 1) - 1
        if n > 0:
            _http_ip_conns[ip] = n
        else:
            _http_ip_conns.pop(ip, None)

def _clear_http_ip_conns():
    with _http_ip_conns_lock:
        _http_ip_conns.clear()

# ---------- A-16：访问日志 query 脱敏（sid/token/leaf/口令等值不入日志） ----------
_ACCESS_SENSITIVE_PARAM = re.compile(r'([?&](?:sid|token|leaf|password|pass|pw|code)=)[^&#]*', re.IGNORECASE)

def _sanitize_path_for_log(p):
    return _ACCESS_SENSITIVE_PARAM.sub(r'\1***', p or '')

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # R4：并发突刺容忍 —— listen backlog 从 http.server 默认 5 提到 256，
    # 避免“快速 API 自由并发”下（如数百并发探活）瞬时连接在 accept 前被内核 RST。
    # 实际处理并发仍由整机总准入（cfg_core.max_total_conns=256）在 handle() 收敛，此处只防背压丢连接。
    request_queue_size = 256

    def process_request(self, request, client_address):
        # 线程创建失败（系统线程上限等）不拖垮 serve_forever：记日志并关闭该请求
        try:
            super().process_request(request, client_address)
        except Exception as e:
            try:
                add_log(f'创建连接处理线程失败（继续监听）: {e}', 'err')
            except Exception:
                pass
            try:
                request.close()
            except Exception:
                pass

    def server_close(self):
        try:
            super().server_close()
        finally:
            _clear_http_ip_conns()


def _check_path_permission_core(role, username, path, guest_mode=True):
    if path:
        # 先检查原始路径中是否包含 ..（必须在 normpath 之前检查）
        raw_parts = path.replace('\\', '/').split('/')
        if '..' in raw_parts:
            return False
        if raw_parts and raw_parts[0] in ('..', '.'):
            return False
        # 规范化路径
        norm_path = os.path.normpath(path).replace('\\', '/')
        # 禁止绝对路径
        if norm_path.startswith('/'):
            return False
        # 标准化后再次检查
        if norm_path in ('..', '../') or norm_path.startswith('../'):
            return False
        path = norm_path
    if role in ('super_admin', 'admin'): return True
    # 游客/匿名：仅在游客模式开启时可访问 public；关闭后一律拒绝（防绕过 UI 直接调 API/WS）
    if role == 'guest' or not username:
        if not guest_mode:
            return False
        return path.startswith('public/') or path == 'public'
    return (path.startswith(f'users/{username}/') or path == f'users/{username}'
            or path.startswith('public/') or path == 'public')

class HTTPHandler(BaseHTTPRequestHandler):
    # 收尾：Server 响应头不再暴露 Python/组件版本，只保留产品名 LeafFS
    server_version = 'LeafFS'
    sys_version = ''
    def version_string(self):
        return self.server_version

    # 慢速请求上限（秒）：慢 POST/慢读最多占住连接 60 秒而非永久；
    # 对正常 LAN 上的下载足够，无需在 /download/ 等路径上设更短的超时
    READ_TIMEOUT = 60

    # 忽略客户端强制断连的错误（远程主机强迫关闭连接等）
    def handle(self):
        ip = ''
        try:
            ip = self.client_address[0]
        except Exception:
            pass
        try:
            self.connection.settimeout(self.READ_TIMEOUT)
        except Exception:
            pass
        # A-11：请求总时长预算起点（上传/下载/打包等长流豁免见 do_GET/do_POST）
        self._req_t0 = time.monotonic()
        self._exempt_total_timeout = False
        # 架构修订 R4：连接级“整机总连接/线程准入” —— O(1) 无等待；超限立即拒绝（503/关闭），
        # 绝不排队。流式正文与快速 API 都在该总上限（cfg_core.max_total_conns，默认 256）内
        # 自由并发，互不阻塞；这才是唯一的应用层并发上限（每 IP 上限见下）。
        if not _cfg.try_acquire_thread():
            self.close_connection = True
            try:
                self._reject_over_capacity()
            except Exception:
                pass
            _overcap_warn()
            return
        try:
            # A-11：每 IP 活跃连接配额（防单 IP 慢连接占满线程池）
            if ip and not _http_ip_conn_acquire(ip):
                self.close_connection = True
                try:
                    add_log(f'每 IP 并发连接超限，拒绝来自 {ip} 的连接', 'warn')
                except Exception:
                    pass
                return
            try:
                try:
                    super().handle()
                except TimeoutError:
                    pass
                except ConnectionResetError:
                    pass
                except BrokenPipeError:
                    pass
                except OSError as e:
                    if e.winerror != 10054:  # WSAECONNRESET
                        raise
            finally:
                if ip:
                    _http_ip_conn_release(ip)
        finally:
            _cfg.release_thread()

    def _reject_over_capacity(self):
        """总并发超限拒绝：尽力读走请求行后回 503 并关闭（不再为慢连接保留线程/资源）。

        仅在总准入满（>256 并发）时触发，正常负载不会走到这里；带 3s socket 超时上限，
        客户端无响应也不滞留线程。TLS 连接同样适用（首 I/O 即触发握手）。
        """
        try:
            old_to = None
            try:
                old_to = self.connection.gettimeout()
            except Exception:
                pass
            try:
                self.connection.settimeout(3.0)
                try:
                    self.rfile.readline(65537)   # 尽力消费请求行，使响应可被客户端正确对应
                except Exception:
                    pass
                body = b'{"error":"server busy (over capacity)"}'
                head = (b'HTTP/1.1 503 Service Unavailable\r\n'
                        b'Server: LeafFS\r\n'
                        b'Content-Type: application/json; charset=utf-8\r\n'
                        b'Content-Length: ' + str(len(body)).encode('ascii') + b'\r\n'
                        b'Connection: close\r\n\r\n')
                self.connection.sendall(head + body)
            except Exception:
                pass
            finally:
                try:
                    if old_to is not None:
                        self.connection.settimeout(old_to)
                except Exception:
                    pass
        except Exception:
            pass

    def _check_total_timeout(self):
        """读请求阶段的总时长检查：超过预算即断开（慢速客户端 DoS 加固）"""
        if getattr(self, '_exempt_total_timeout', False):
            return
        t0 = getattr(self, '_req_t0', None)
        if t0 is not None and time.monotonic() - t0 > REQ_TOTAL_TIMEOUT:
            self.close_connection = True
            raise TimeoutError('请求总时长超限')

    def _local_token_login(self):
        """一次性本机令牌登录（D4，替代原“本机自动登录超级管理员”）。

        仅当 来源 IP 为环回 + Host 为 localhost/环回 + query leaf 与 _LOCAL_TOKEN
        恒定时间匹配时才建立 super_admin 会话；令牌一次性，首个成功使用后立即失效。
        """
        ip = self.client_address[0]
        if ip not in ('127.0.0.1', '::1'):
            self.close_connection = True
            return False
        if _strip_host_port(self.headers.get('Host', '')) not in _LOCAL_HOST_NAMES:
            return False  # 防 rebinding/转发携带令牌重放
        global _LOCAL_TOKEN
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        tok = q.get('leaf', [''])[0]
        if not tok or not _LOCAL_TOKEN or not secrets.compare_digest(tok, _LOCAL_TOKEN):
            return False
        # 一次性：首个成功使用后立即失效并删除临时文件
        _LOCAL_TOKEN = None
        _delete_local_token_file()
        try:
            su = _ac.get_super_admin_name() or 'admin'
        except Exception:
            su = 'admin'
        sid = _ac.create_session(su, 'super_admin', client_ip=ip)
        add_log(f'本机一次性令牌登录成功（super_admin: {su}）', 'ok')
        self.send_response(302)
        self.send_header('Location', '/browse/')
        c = f'{_ac.AUTH_COOKIE}={sid}; Path=/; Max-Age={_ac.SESSION_EXPIRY_DAYS * 86400}; HttpOnly; SameSite=Lax'
        if self._is_secure(): c += '; Secure'
        self.send_header('Set-Cookie', c)
        self._common_security_headers()
        self.end_headers()
        return True

    def _get_effective_role(self):
        cookie = self.headers.get('Cookie', '')
        role, sid = _ac.get_session(cookie, True, self.client_address[0])
        if role == 'guest' and not _cfg.get_guest_mode():
            # 游客模式已关闭：即使持有旧游客会话也视为未登录（防地址构造/残留会话访问）
            return None
        if role: return role
        return None

    def _get_username_from_session(self):
        cookie = self.headers.get('Cookie', '')
        _, sid = _ac.get_session(cookie, True, self.client_address[0])
        return _ac.get_session_username(sid) if sid else ''

    def _has_invalid_session_cookie(self):
        """Cookie 携带 wifi_session 但服务端解析不到有效会话（无效/过期/非本机 IP）→ True。

        解析语义与 ac_core.get_session 一致（同名 cookie 取最后一个）；有效游客会话
        （role=guest）视为有效不在此列；未带 Cookie 的纯匿名不受影响。
        """
        cookie = self.headers.get('Cookie', '')
        if not cookie:
            return False
        sid = ''
        found = False
        for part in cookie.split(';'):
            part = part.strip()
            if part.startswith(_ac.AUTH_COOKIE + '='):
                found = True
                sid = part[len(_ac.AUTH_COOKIE) + 1:]
        if not found:
            return False
        if not sid:
            return True  # 显式携带空值 cookie（登出残留/伪造）按无效处理
        try:
            with _ac._sessions_lock:
                info = _ac._sessions.get(sid)
            if info and info.get('expiry', 0) > time.time() \
                    and _ac._same_client(info.get('ip', ''), self.client_address[0]):
                return False  # 会话仍有效（含有效游客会话）
        except Exception:
            return False
        return True

    def _reject_invalid_session_api(self, path):
        """/api/* 且携带无效会话 Cookie → 401（白名单放行）；返回 True 表示已响应"""
        if not path.startswith('/api/'):
            return False
        if path in _INVALID_SESSION_API_WHITELIST:
            return False
        if not self._has_invalid_session_cookie():
            return False
        self.send_json({'error': '会话无效或已过期，请重新登录'}, 401)
        return True

    def _check_path_permission(self, path):
        return _check_path_permission_core(self._get_effective_role(), self._get_username_from_session(), path, _cfg.get_guest_mode())

    def _require_roles(self, *roles):
        """A-01：统一鉴权助手 —— 当前有效角色是否属于 roles"""
        return self._get_effective_role() in roles

    # 架构修订 R4：请求级“全局并发槽”（_acquire_global_slot / CONC_SLOT_TIMEOUT）已整体移除。
    # 总并发资源保护改在连接级 handle() 完成（cfg_core.try_acquire_thread，无等待、超限即拒），
    # do_GET/do_POST 不再获取/释放任何全局槽 —— 快速 API 与慢速流式正文在总上限内自由并发。

    def _common_security_headers(self, csp=None):
        """A-10：统一安全响应头（JSON/重定向/手动 302 等响应共用）"""
        try:
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            if csp:
                self.send_header('Content-Security-Policy', csp)
            if self._is_secure():
                self.send_header('Strict-Transport-Security', 'max-age=15552000')
        except Exception:
            pass

    def _dl_allowed(self):
        """A-03：下载器门槛 —— user/admin/super_admin 可用；guest 依配置开关（默认禁）"""
        role = self._get_effective_role()
        if role in ('user', 'admin', 'super_admin'):
            return True
        if role == 'guest':
            try:
                return bool(_cfg.get_downloader_guest_allowed())
            except Exception:
                return False
        return False

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        # A-10：JSON 响应无子资源，CSP 收紧到 default-src 'none' + nosniff/XFO/Referrer
        self._common_security_headers(csp="default-src 'none'")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def redirect(self, path):
        self.send_response(302)
        self.send_header('Location', urllib.parse.quote(path, safe='/:?=&'))
        self._common_security_headers()
        self.end_headers()

    def send_error(self, code, message=None, explain=None):
        # A-10：错误页补安全头。BaseHTTPRequestHandler.send_error 内部先 send_response
        # 写状态行再写头，故不能先 send_header——这里自实现等价流程：状态行→基础头→
        # 安全头→正文，避免“头先于状态行”的协议错误（BadStatusLine）。
        try:
            short, long_msg = self.responses.get(code, ('Error', 'Error'))
            if message is None:
                message = short
            if explain is None:
                explain = long_msg
            _esc = lambda s: str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            body = (f'<!DOCTYPE HTML><html><head><meta charset="utf-8"><title>{code} '
                    f'{_esc(message)}</title></head><body><h1>{code} {_esc(message)}</h1>'
                    f'<p>{_esc(explain)}</p></body></html>').encode('utf-8', 'replace')
            # 状态行只允许 latin-1：非 ASCII 消息（如 send_error(403,'无权限')）会触发
            # BaseHTTPRequestHandler 内部 latin-1 strict 编码抛 UnicodeEncodeError，
            # 被本方法 except 吞掉后静默 close → 客户端看到“空应答/连接被重置”。
            # 这里仅对状态行回退为标准英文短描述（正文仍保留完整中文文案）。
            _msg_str = str(message)
            if all(ord(ch) < 128 for ch in _msg_str):
                _status_msg = _msg_str
            else:
                _status_msg = self.responses.get(code, ('Error', 'Error'))[0]
            self.send_response(code, _status_msg)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self._common_security_headers()
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)
        except Exception:
            try:
                self.close_connection = True
            except Exception:
                pass

    def log_message(self, fmt, *args):
        """A-16：请求级访问日志（配置键 access_log，默认 true）。

        读取 BaseHTTPRequestHandler 传参中的状态码（send_error 场景 args[0] 为 int，
        常规请求 args[0] 为 requestline），query 中的 sid/token/leaf 等值脱敏后入库。
        """
        try:
            if not _cfg.get_access_log():
                return
        except Exception:
            return
        try:
            ip = (self.client_address or ('', 0))[0]
            status = '?'
            reqline = self.requestline or ''
            if args and isinstance(args[0], int):
                status = str(args[0])
            elif len(args) > 1:
                status = str(args[1])
                reqline = str(args[0])
            elif args:
                reqline = str(args[0])
            parts = reqline.split()
            method = parts[0] if parts else (self.command or '')
            p = parts[1] if len(parts) > 1 else (self.path or '')
            p = _sanitize_path_for_log(p)
            t0 = getattr(self, '_req_t0', None)
            ms = int((time.monotonic() - t0) * 1000) if t0 else 0
            add_log(f'{ip} {method} {p} {status} {ms}ms', 'info')
        except Exception:
            pass

    def _read_json(self):
        # A-11/#3：Content-Length 非数字/负数 → 400，不再让 int() 异常冒泡成 500
        raw_cl = self.headers.get('Content-Length', 0)
        try:
            cl = int(raw_cl)
        except (TypeError, ValueError):
            self.send_json({'error': 'Invalid Content-Length'}, 400)
            return None
        if cl < 0:
            self.send_json({'error': 'Invalid Content-Length'}, 400)
            return None
        if not cl:
            return {}
        # A-11：慢速客户端逐段读 + 总时长检查（防 trickle 钉死线程）
        self._check_total_timeout()
        raw = bytearray()
        remaining = cl
        while remaining > 0:
            self._check_total_timeout()
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            raw.extend(chunk)
            remaining -= len(chunk)
        return json.loads(bytes(raw).decode()) if raw else {}

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        # 实测收口：携带无效/过期会话 Cookie 的 /api 请求路由前 401，不再静默降级匿名/游客
        if self._reject_invalid_session_api(path):
            return
        self._pending_session = None  # 重置
        role = self._get_effective_role()
        username = self._get_username_from_session()
        _cfg.track_connection(self.client_address[0], self.headers.get('User-Agent', ''),
                              username, role or '')
        if path.startswith(_TOTAL_TIMEOUT_EXEMPT):
            self._exempt_total_timeout = True
        # 架构修订 R4：不再获取任何全局并发槽（总并发资源保护在连接级 handle() 完成）。
        # /download/ 与其它 GET（含 /api/raw、/api/zip 流式输出）直接路由、自由并发；
        # 流式正文写循环的“无进展超时”由 fs_api 各发送函数自管（120s），互不阻塞。
        try:
            self._route_get(path, role)
        except TimeoutError:
            self.close_connection = True
            raise
        except _cfg.DISCONNECTED_EXCEPTIONS:
            pass
        except Exception as e:
            try:
                self.send_json({'error': str(e)}, 500)
            except _cfg.DISCONNECTED_EXCEPTIONS:
                pass
        else:
            if self._pending_session and not getattr(self, '_session_set', False):
                self._set_session_cookie(self._pending_session)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        # 实测收口：携带无效/过期会话 Cookie 的 /api 请求路由前 401，不再静默降级匿名/游客
        if self._reject_invalid_session_api(path):
            return
        role = self._get_effective_role()
        username = self._get_username_from_session()
        _cfg.track_connection(self.client_address[0], self.headers.get('User-Agent', ''),
                              username, role or '')
        if path.startswith(_TOTAL_TIMEOUT_EXEMPT):
            self._exempt_total_timeout = True
        # 架构修订 R4：不再获取/释放全局并发槽（连接级总准入 handle() 已兜底）。
        # /api/upload 等流式正文在总并发上限内自由并发；正文读超时沿用 handle() 的
        # READ_TIMEOUT=60s（每连接 socket 读超时），中断清理由 fs_core .part 逻辑负责。
        try:
            if path not in ('/api/upload', '/api/url-download/upload-torrent'):
                try:
                    cl = int(self.headers.get('Content-Length', 0))
                except (TypeError, ValueError):
                    self.send_json({'error': 'Invalid Content-Length'}, 400)
                    return
                if cl < 0:
                    self.send_json({'error': 'Invalid Content-Length'}, 400)
                    return
                if cl > MAX_API_BODY_SIZE:
                    self.send_json({'error': '请求体过大'}, 413)
                    return
            self._route_post(path)
        except TimeoutError:
            self.close_connection = True
            raise
        except _cfg.DISCONNECTED_EXCEPTIONS: pass
        except Exception as e:
            try:
                self.send_json({'error': str(e)}, 500)
            except _cfg.DISCONNECTED_EXCEPTIONS:
                pass

    def _route_get(self, path, role):
        self.role = role  # 供 wm_page 渲染使用
        # A-01：管理面 GET 统一 admin/super_admin（注销/降权后即时 403）
        if path in _ADMIN_ONLY_GET and not self._require_roles('admin', 'super_admin'):
            self.send_json({'error': 'Forbidden'}, 403)
            return
        # 页面路由
        if path == '/login':
            if role:
                self.redirect('/browse/')
                return
            # D4：本机访问不再无条件自动登录；带有效一次性令牌（?leaf=）才建会话
            if self._local_token_login():
                return
            _ac_auth.serve_login_page(self, _ut.BASE_DIR, _fs.read_file_cached, _cfg.get_guest_mode)
        elif path == '/admin':
            if role not in ('admin', 'super_admin'): self.send_error(403); return
            _wm.serve_admin_page(self, _ut.BASE_DIR, _fs.read_file_cached, _ac.is_default_admin_password)
        elif path == '/admin/users':
            if role not in ('admin', 'super_admin'): self.send_error(403); return
            _wm.serve_admin_users_page(self, _ac.get_session, _ac.get_session_username, _fs.read_file_cached, _ut.BASE_DIR)
        elif path in ('/admin/advanced', '/admin/deep', '/log'):
            if role not in ('admin', 'super_admin'): self.send_error(403); return
            pages = {'/admin/advanced': 'advanced.html', '/admin/deep': 'deep.html', '/log': 'log.html'}
            _wm.serve_file(self, os.path.join('web_page', 'management', pages[path]), 'text/html; charset=utf-8',
                          _ut.BASE_DIR, _ac.get_session, _ac.get_session_username)
        elif path == '/me':
            # 用户信息页（与 浏览/预览/下载器/管理 同级）：任意已登录角色（含游客）可看
            if not role:
                self.redirect('/login')
                return
            _wm.serve_file(self, os.path.join('web_page', 'account', 'account.html'), 'text/html; charset=utf-8',
                          _ut.BASE_DIR, _ac.get_session, _ac.get_session_username)
        elif path == '/' or path.startswith('/browse/') or path == '/gallery':
            self._route_page(path, role)
        elif path.startswith('/static/'):
            _wm.serve_static(self, path, _ut.BASE_DIR, _fs.safe_path, _fs.get_mime, _fs.read_file_cached)
        elif path == '/url-download/peers':
            _wm.serve_file(self, os.path.join('web_page', 'downloader', 'peers.html'), 'text/html; charset=utf-8',
                          _ut.BASE_DIR, _ac.get_session, _ac.get_session_username)
        elif path == '/url-download' or path.startswith('/url-download/'):
            _wm.serve_file(self, os.path.join('web_page', 'downloader', 'downloader.html'), 'text/html; charset=utf-8',
                          _ut.BASE_DIR, _ac.get_session, _ac.get_session_username)
        # API 路由
        elif path == '/api/admin/auto-login':
            # D4：与 /login 同规则 —— 仅带有效一次性令牌（?leaf=）的本机请求才建会话
            if not self._local_token_login():
                self.redirect('/login')
        elif path == '/api/auth/check': _ac_auth.auth_check(self, _ac.get_session, _ac.get_session_username, _ac.is_default_admin_password, _cfg.get_guest_mode, _ac._sessions, _ac._sessions_lock)
        elif path == '/api/auth/logout':
            # A-13：登出改为 POST（GET 命中 405），杜绝 Get 副作用/CSRF 登出面
            self.send_json({'error': 'Method Not Allowed：请使用 POST 调用 /api/auth/logout'}, 405)
        elif path == '/api/files': _fs_api.send_files(self, _fs.list_files)
        elif path == '/api/raw': _fs_api.send_raw(self, _fs.UPLOAD_DIR, self._preview_max_size(), _fs.safe_path, _fs.get_mime, _cfg.DISCONNECTED_EXCEPTIONS)
        elif path == '/api/thumb': _fs_api.send_thumbnail(self, _fs.UPLOAD_DIR, _fs.get_thumbnail, _fs.safe_path, _cfg.DISCONNECTED_EXCEPTIONS)
        elif path.startswith('/download/'): _fs_api.handle_download(self, _fs.UPLOAD_DIR, _cfg.COPY_BUFFER_SIZE, _fs.safe_path, _fs.get_mime, _ac.get_session, _ac.get_session_username, _ac.get_user_speed_limit, _cfg.get_speed_limit, _cfg.get_user_limiter, _cfg.DISCONNECTED_EXCEPTIONS)
        elif path == '/api/search': _fs_api.search_files(self, _fs.UPLOAD_DIR)
        elif path == '/api/stats': _fs_api.server_stats(self, _fs.get_server_stats, _fs.get_folder_size, _fs.has_ffmpeg, _cfg.get_max_concurrent, _cfg.COPY_BUFFER_SIZE, _cfg.get_speed_limit, _cfg.get_connections, _cfg.PORT, _cfg.get_guest_mode, _cfg.get_default_user_quota, _cfg.get_public_quota, _cfg.get_total_quota, _fs.UPLOAD_DIR)
        elif path == '/api/config': _cfg_api.get_config(self, _cfg.get_max_concurrent, _cfg.get_speed_limit, _cfg.get_guest_mode, _cfg.get_default_user_quota, _cfg.get_public_quota, _cfg.get_total_quota)
        elif path == '/api/config/advanced': _cfg_api.get_config_advanced(self, _cfg.COPY_BUFFER_SIZE, MAX_API_BODY_SIZE, self._preview_max_size(), _cfg.get_upload_max_size)
        elif path == '/api/config/deep': _cfg_api.get_config_deep(self, _cfg.get_deep_config_dict)
        elif path == '/api/connections': self.show_connections()
        elif path == '/api/qrcode': self.serve_qrcode()
        elif path == '/api/qrcode/status': self.qrcode_status()
        elif path == '/api/qrlogin': self.qr_login()
        elif path == '/api/ping':
            # 公开轻量探测（供页面/网络探活使用）
            self.send_json({'ok': True})
        elif path == '/api/session/sid':
            """返回当前 session id，供 WebSocket 认证使用"""
            cookie = self.headers.get('Cookie', '')
            _, sid = _ac.get_session(cookie, True, self.client_address[0])
            self.send_json({'sid': sid or ''})
        elif path == '/api/sessions':
            if role not in ('admin', 'super_admin'): self.send_json({'error': 'Forbidden'}, 403); return
            _ac_session.sessions_list(self, _ac.get_all_sessions)
        elif path == '/api/users': _ac_user.users_list(self)
        elif path == '/api/logs': _ut_log.serve_logs(self, get_logs)
        elif path == '/api/zip': _fs_api.zip_download(self, _fs.UPLOAD_DIR, _cfg.COPY_BUFFER_SIZE, _fs.safe_path, _cfg.DISCONNECTED_EXCEPTIONS)
        elif path.startswith('/api/url-download/'):
            # A-03：下载器门槛（user/admin/super_admin；guest 依 downloader_guest_allowed）
            if not self._dl_allowed():
                if not self._get_effective_role():
                    self.send_json({'error': 'Unauthorized'}, 401)
                else:
                    self.send_json({'error': '游客不可使用下载器'}, 403)
                return
            self._route_dl_get(path)
        else: self.send_error(404)

    def _route_page(self, path, role):
        if not role: self.redirect('/login'); return
        if path == '/' or path == '/browse/':
            username = self._get_username_from_session()
            start = _fs.get_user_start_path(username, role)
            if start: self.redirect('/browse/' + start); return
            _wm.serve_file(self, os.path.join('web_page', 'home', 'home.html'), 'text/html; charset=utf-8',
                          _ut.BASE_DIR, _ac.get_session, _ac.get_session_username)
            return
        if path.startswith('/browse/'):
            dp = path[len('/browse/'):]
            # 前端对完整相对路径做 encodeURIComponent 后拼 URL（如
            # /browse/public%2Ftransfer_test），斜杠被编码为 %2F；此处必须先解码，
            # 否则 public/ 前缀与 users/<name>/ 前缀比对全部失败 → 子文件夹一律 403
            try:
                dp = urllib.parse.unquote(dp)
            except Exception:
                dp = dp
            if not self._check_path_permission(dp): self.send_error(403, '无权限'); return
        if path == '/gallery':
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            if not params.get('dir'):
                username = self._get_username_from_session()
                start = _fs.get_user_start_path(username, role)
                self.redirect('/gallery?dir=' + start); return
            dp = params.get('dir', [''])[0]
            if not self._check_path_permission(dp): self.send_error(403, '无权限'); return
        fn = os.path.join('web_page', 'home', 'home.html') if path.startswith('/browse/') else os.path.join('web_page', 'preview', 'preview.html')
        _wm.serve_file(self, fn, 'text/html; charset=utf-8', _ut.BASE_DIR, _ac.get_session, _ac.get_session_username)

    def _route_post(self, path):
        # A-01：管理类 POST 统一 admin/super_admin（注销/降权后即时 403，单一入口防漏网）
        if path in _ADMIN_ONLY_POST and not self._require_roles('admin', 'super_admin'):
            self.send_json({'error': 'Forbidden'}, 403)
            return
        handlers = {
            '/api/auth/login': lambda: _ac_auth.auth_login(self, add_log, logger, _fs.UPLOAD_DIR, _ac.verify_login, _ac.create_session, _ac.is_default_admin_password, _ac._sessions, _ac._sessions_lock),
            '/api/guest/login': lambda: _ac_auth.guest_login(self, _ac.create_session, _cfg.get_guest_mode),
            '/api/auth/logout': lambda: _ac_auth.auth_logout(self, _ac.get_session, _ac.remove_session, _ac.AUTH_COOKIE),
            '/api/auth/toggle': lambda: _ac_auth.auth_toggle(self, add_log, _cfg.set_guest_mode, _cfg.get_guest_mode, _ac.remove_guest_sessions),
            '/api/upload': lambda: self.upload(),
            '/api/delete': lambda: self.delete(),
            '/api/mkdir': lambda: self.mkdir(),
            '/api/config': lambda: self.set_config(),
            '/api/config/advanced': lambda: self.set_config_advanced(),
            '/api/config/deep': lambda: self.set_config_deep(),
            '/api/users/add': lambda: _ac_user.users_add(self, _ac.add_user),
            '/api/users/delete': lambda: _ac_user.users_delete(self, _ac.delete_user),
            '/api/users/password': lambda: _ac_user.users_password(self, _ac.change_password),
            '/api/users/role': lambda: _ac_user.users_role(self, _ac.update_user_role),
            '/api/users/speed': lambda: _ac_user.users_speed(self, _ac.set_user_speed_limit),
            '/api/users/quota': lambda: _ac_user.users_quota(self, _ac.set_user_quota),
            '/api/users/rename': lambda: _ac_user.users_rename(self, _ac.update_user_name, add_log, _fs.UPLOAD_DIR),
            '/api/qrcode/refresh': lambda: self.qrcode_refresh(),
            '/api/certs/reset': lambda: self.reset_certs(),
            '/api/logs/clear': lambda: _ut_log.clear_logs(self, clear_runtime_logs),
            '/api/session/revoke': lambda: _ac_session.session_revoke(self, _ac.revoke_session_by_prefix),
        }
        h = handlers.get(path)
        if h: h()
        elif path.startswith('/api/url-download/'):
            # A-03：与 GET 分支同一下载器门槛（guest 依开关，匿名一律拒绝）
            if not self._dl_allowed():
                if not self._get_effective_role():
                    self.send_json({'error': 'Unauthorized'}, 401)
                else:
                    self.send_json({'error': '游客不可使用下载器'}, 403)
                return
            self._route_dl_post(path)
        else: self.send_error(404)

    def _route_dl_get(self, path):
        actions = {'start': self._dl_forward_start, 'cancel': self._dl_forward_cancel,
                   'list': self._dl_forward_list, 'config': self._dl_forward_config,
                   'parse-torrent': self._dl_forward_parse_torrent,
                   'probe': self._dl_forward_probe}
        action = path.split('/')[-1]
        if action in actions: actions[action]()
        else: self.send_error(404)

    def _route_dl_post(self, path):
        actions = {'config': self._dl_forward_config_post, 'start': self._dl_forward_start,
                   'cancel': self._dl_forward_cancel, 'pause': self._dl_forward_pause,
                   'resume': self._dl_forward_resume, 'delete': self._dl_forward_delete,
                   'upload-torrent': self._dl_forward_upload_torrent,
                   'merge': self._dl_forward_merge, 'retry': self._dl_forward_retry,
                   'peers': self._dl_forward_peers}
        action = path.split('/')[-1]
        if action in actions: actions[action]()
        else: self.send_error(404)

    def _preview_max_size(self): return PREVIEW_MAX_SIZE
    def _get_current_caller_role(self): return self._get_effective_role() or ''

    def set_config(self):
        _cfg_api.set_config(self, add_log, _cfg.update_concurrent_limit, _cfg.set_speed_limit,
                           _cfg.set_guest_mode, _cfg.set_quotas, _cfg.get_speed_limit, _cfg.get_guest_mode)

    def set_config_advanced(self):
        _cfg_api.set_config_advanced(self, add_log, _cfg.set_ports, _cfg.get_deep_config_dict, _cfg.apply_deep_config)

    def set_config_deep(self):
        _cfg_api.set_config_deep(self, add_log, _cfg.apply_deep_config)

    def upload(self):
        # A-02：上传强制有效会话（匿名一律 401）；空 path（共享根）仅 admin/super_admin
        role = self._get_effective_role()
        if not role:
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        sub_path = q.get('path', [''])[0].strip()
        if role not in ('admin', 'super_admin') and sub_path == '':
            # R2：仅“空 path（共享根）”属根级写，普通 user/guest 拒绝（提示带自己的目录）。
            # public/ 及其子目录对登录用户开放；guest 的 public 写入语义由 fs_api.write_allowed
            # （D1：可新建、禁覆盖/删除）与 fs_core 自动改名承接。
            self.send_json({'success': False, 'saved': 0,
                            'errors': ['游客/普通用户仅可上传到自己的目录（或使用 path=public）']}, 403)
            return
        _fs_api.handle_upload(self, _fs.handle_upload, _fs.invalidate_folder_cache_smart,
                             _fs.UPLOAD_DIR, _fs.safe_path, self._check_quota, _cfg.DISCONNECTED_EXCEPTIONS)

    def delete(self):
        _fs_api.handle_delete(self, _fs.UPLOAD_DIR, _fs.safe_path, _fs._delete_thumb,
                             _fs.invalidate_file_cache, _fs.invalidate_folder_cache, _cfg.DISCONNECTED_EXCEPTIONS)

    def mkdir(self):
        _fs_api.handle_mkdir(self, _fs.UPLOAD_DIR, _fs.safe_path, _fs.invalidate_folder_cache, _cfg.DISCONNECTED_EXCEPTIONS)

    @staticmethod
    def _quota_pending(bucket):
        """IC-QUOTA：读取 C 侧在途记账字节；契约函数尚未就绪时按 0 计（并行开发过渡）"""
        try:
            return int(_ut.quota_pending(bucket))
        except Exception:
            return 0

    def _check_quota(self, path, new_size, username=''):
        # IC-QUOTA：三层配额检查均并入“在途字节”预留，收窄并行上传超配 TOCTOU
        path = os.path.realpath(path)
        ud = os.path.realpath(_fs.UPLOAD_DIR)
        total = _fs.get_folder_size(_fs.UPLOAD_DIR) + self._quota_pending('total')
        if total + new_size > _cfg.get_total_quota(): return False, '服务器总空间不足'
        pub = os.path.join(ud, 'public')
        users = os.path.join(ud, 'users')
        pn = os.path.normcase(path)
        if pn.startswith(os.path.normcase(pub) + os.sep) or pn == os.path.normcase(pub):
            pu = _fs.get_folder_size(pub) + self._quota_pending('public')
            if pu + new_size > _cfg.get_public_quota(): return False, '公共文件夹空间不足'
        elif pn.startswith(os.path.normcase(users) + os.sep) or pn == os.path.normcase(users):
            rel = os.path.relpath(pn, os.path.normcase(users))
            tu = rel.split(os.sep)[0] if rel else ''
            if tu:
                uu = _fs.get_folder_size(os.path.join(users, tu)) if os.path.exists(os.path.join(users, tu)) else 0
                uu += self._quota_pending(f'users/{tu}')
                ul = _ac.get_user_quota(tu) or _cfg.get_default_user_quota()
                if uu + new_size > ul: return False, '用户文件夹空间不足'
        return True, ''

    def show_connections(self):
        role = self._get_effective_role()
        if role not in ('admin', 'super_admin'): self.send_json({'error': 'Forbidden'}, 403); return
        self.send_json({'connections': build_connections_payload()})

    def serve_qrcode(self):
        # 安全：仅已登录管理员可为目标用户签发二维码会话，杜绝匿名免密换取管理员会话
        if self._get_effective_role() not in ('admin', 'super_admin'):
            self.send_json({'error': 'Forbidden'}, 403)
            return
        base = self._public_base_url()
        q = urllib.parse.urlparse(self.path).query
        p = urllib.parse.parse_qs(q)
        dn = p.get('name', [''])[0].strip()
        is_embed = p.get('embed', [''])[0] == '1'
        sid = ''; qr_url = ''
        if dn:
            # A-04（IC-QR）：签发授权收敛 —— admin 仅可代签已存在的 user，禁 ghost/他人 super_admin；
            # super_admin 可给自己签（caller_name=当前会话用户名，唯一超管“扫码登录”自己）
            ok, ur, err = _ac.issue_qr_authorize(self._get_effective_role(), dn,
                                                 caller_name=self._get_username_from_session())
            if not ok:
                self.send_json({'error': err or '无权为该用户签发'}, 403)
                return
            sid = _ac.create_qr_session(dn, ur, expiry_minutes=2)
            qr_url = f'{base}/api/qrlogin?sid={sid}'
            add_log(f'QR 签发: {self._get_username_from_session()}({self._get_effective_role()}) '
                    f'-> {dn}({ur}) {self.client_address[0]}', 'ok')
        if is_embed: self.send_json({'sid': sid, 'qr_url': qr_url, 'name': dn}); return
        has_qr = bool(qr_url)
        dn_html = ('扫描后自动登录(' + _fs.esc_html(dn) + ')' if dn else '请选择用户')
        # A-14：qr_url 经 JSON 编码注入内联 JS，杜绝 Host/URL 内容字符串插值逃逸
        qr_obj = json.dumps({'text': qr_url, 'width': 200, 'height': 200}) if has_qr else ''
        qr_html = ('<div id=q></div>'
                   '<script src=/static/common/qrcode.min.js></script>'
                   f'<script>new QRCode(document.getElementById("q"), {qr_obj});</script>'
                   if has_qr else '<p>无二维码</p>')
        html = f'''<!DOCTYPE html><html><title>扫码连接</title><body>
<h2>扫码连接</h2><p>{dn_html}</p>
{qr_html}
</body></html>'''
        data = html.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self._common_security_headers()
        self.end_headers()
        self.wfile.write(data)

    def qrcode_refresh(self):
        data = self._read_json()
        if data is None: return  # 已回 400 Invalid Content-Length
        dn = data.get('name', '').strip()
        if not dn: self.send_json({'success': False, 'error': '缺少 name 参数'}, 400); return
        if self._get_effective_role() not in ('admin', 'super_admin'): self.send_json({'success': False, 'error': '无权限'}, 403); return
        base = self._public_base_url()
        # A-04（IC-QR）：与 serve_qrcode 同签发授权/审计；super_admin 给自己签需带 caller_name
        ok, ur, err = _ac.issue_qr_authorize(self._get_effective_role(), dn,
                                             caller_name=self._get_username_from_session())
        if not ok:
            self.send_json({'success': False, 'error': err or '无权为该用户签发'}, 403)
            return
        sid = _ac.create_qr_session(dn, ur, expiry_minutes=2)
        add_log(f'QR 签发: {self._get_username_from_session()}({self._get_effective_role()}) '
                f'-> {dn}({ur}) {self.client_address[0]}', 'ok')
        self.send_json({'success': True, 'sid': sid, 'qr_url': f'{base}/api/qrlogin?sid={sid}'})

    def qrcode_status(self):
        """二维码登录状态：pending / consumed / expired（供前端轮询区分成功与过期）"""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        sid = q.get('sid', [''])[0].strip()
        if not sid:
            self.send_json({'status': 'expired'}); return
        self.send_json({'status': _ac.qr_login_status(sid)})

    def _public_base_url(self):
        """对外 URL base（scheme://host[:port]）。

        A-14：host 只接受“本机网卡 IP / localhost”（端口剥离后比对），拒绝任意 Host 头
        注入（防反射 XSS/伪造二维码 base）。localhost/环回地址视为空 → 回退 socket 探测。
        无可用 Host 时回退 socket 探测主 LAN IP。
        """
        scheme = 'https' if self._is_secure() else 'http'
        host = ''
        try:
            raw = (self.headers.get('Host') or '').strip().lower()
            hh = _strip_host_port(raw)
            if hh in _LOCAL_HOST_NAMES:
                host = ''
            else:
                allowed = set(_collect_ips())
                if hh in allowed:
                    host = hh
                else:
                    host = ''  # 非本机主机名的 Host（伪造/rebinding）→ 回退探测
        except Exception:
            host = ''
        if not host:
            try:
                host = socket.gethostbyname(socket.gethostname())
            except Exception:
                host = '127.0.0.1'
        port = _cfg.PORT
        return f'{scheme}://{host}' + (f':{port}' if port != 80 else '')

    def reset_certs(self):
        """重置证书（仅 admin/super_admin）—— 证书已改为“服务器自动管理”，本接口不再重置。

        自签 CA / 证书自愈 / 信任引导机制已从 LeafFS 移除（分发安全：防止该机制被
        用于分发恶意 CA）。TLS 现在只涉及“服务器证书”本身：
          * 未配置（tls_cert/tls_key 均为空）→ 首次启动自动生成随机自签服务器证书
            config/selfsigned.crt + selfsigned.key（非 CA、不装信任库），此后直接复用；
          * 显式配置 tls_cert/tls_key → 以配置为准。
        本接口不备份/重置/删除任何证书文件（避免中断进行中的 HTTPS 连接）；
        需要更换证书请编辑 config/server_config.json 后重启服务。
        """
        if self._get_effective_role() not in ('admin', 'super_admin'):
            self.send_json({'success': False, 'error': '无权限'}, 403)
            return
        self.send_json({'success': False,
                        'error': '证书由服务器自动管理，无需也无法在此重置：未配置证书时首次启动'
                                 '会自动生成随机自签服务器证书（config/selfsigned.crt，非 CA、'
                                 '未安装任何信任库）；已配置 tls_cert/tls_key 时以配置为准。'
                                 '如需更换证书，请编辑 config/server_config.json 后重启服务。'
                                 '（自签证书的“不安全”提示属正常现象，可在本机打开 '
                                 'http://127.0.0.1:8082 查看大白话说明。）'})

    def qr_login(self):
        sid = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('sid', [''])[0]
        r = _ac.consume_qr_session(sid, client_ip=self.client_address[0])
        if r:
            # 通知等待该二维码的页面：已扫码登录（即时，无需前端轮询）
            _broadcast_qr_consumed(sid)
            ns, un, rl = r
            # A-04：QR 消费同样记审计（IC-QR：审计归属 A 侧 add_log）
            add_log(f'QR 登录消费: {un}({rl}) ip={self.client_address[0]}', 'ok')
            self.send_response(302)
            self._set_session_cookie(ns)
            self.send_header('Location', '/browse/')
            self._common_security_headers()
            self.end_headers()
        else:
            self.redirect('/login')

    def _is_secure(self):
        return bool(getattr(getattr(self, 'server', None), 'is_secure', False))

    def _set_session_cookie(self, sid, secure=False):
        secure = secure or self._is_secure()
        c = f'{_ac.AUTH_COOKIE}={sid}; Path=/; Max-Age={_ac.SESSION_EXPIRY_DAYS * 86400}; HttpOnly; SameSite=Lax'
        if secure: c += '; Secure'
        self.send_header('Set-Cookie', c)
        # 非 Secure 登录标记（见 ac_core.LOGIN_MARKER 说明）：供 8082 明文提示页识别
        # “本浏览器已登录”并跳回 https 主站；恒为 1，不含任何凭证。
        self.send_header('Set-Cookie',
                         f'{_ac.LOGIN_MARKER}=1; Path=/; Max-Age={_ac.SESSION_EXPIRY_DAYS * 86400}; HttpOnly; SameSite=Lax')

    def _parse_multipart_file(self, ct, cl):
        import email, io
        # A-11：大 body 读取前做请求总时长检查
        self._check_total_timeout()
        raw_body = self.rfile.read(cl)
        # 拼接 Content-Type header，让 email 解析器能识别 multipart 边界
        raw_with_header = f'Content-Type: {ct}\r\n\r\n'.encode() + raw_body
        msg = email.message_from_binary_file(io.BytesIO(raw_with_header))
        for part in msg.walk():
            if part.get_content_maintype() == 'multipart': continue
            fn = part.get_filename()
            if fn: return fn, part.get_payload(decode=True)
        raise ValueError('未找到文件')

    # 下载器转发（支持用户隔离）
    def _dl_get_user_and_role(self):
        """获取当前请求的用户名和角色

        A-03/A-05：已删除“本机访问自动授予 super_admin”兜底 —— 角色一律来自会话，
        本机免密场景改由 A-12 一次性令牌体系（/login?leaf= 或 WS auth token）接管。
        """
        cookie = self.headers.get('Cookie', '')
        role, sid = _ac.get_session(cookie, True, self.client_address[0])
        username = _ac.get_session_username(sid) if sid else ''
        # 游客下载任务按来源 IP 隔离：会话名保持“游客”，但任务归属键改为
        # “游客@<ip>”，列表/推送/管理接口天然按 IP 过滤，不同 IP 游客互不可见
        if role == 'guest':
            username = '游客@' + self.client_address[0]
        return username, role

    def _dl_quota_check(self, save_dir_abs, est_bytes):
        """IC-QUOTA-C：把 HTTP 层 _check_quota 包装成下载器的 quota_check 回调"""
        try:
            return self._check_quota(save_dir_abs, int(est_bytes or 0))
        except Exception:
            return True, ''

    def _dl_forward_start(self):
        data = self._read_json()
        if data is None: return  # 已回 400 Invalid Content-Length
        username, _ = self._dl_get_user_and_role()
        # IC-QUOTA-C：下载任务发起前复用上传配额口径（C 侧 fallback 见 dl_api.handle_start）
        r = _dl_api.handle_start(data, check_path_permission=self._check_path_permission,
                                 upload_dir=_fs.UPLOAD_DIR, user=username,
                                 quota_check=self._dl_quota_check)
        self.send_json(r[0] if isinstance(r, tuple) else r, r[1] if isinstance(r, tuple) else 200)

    def _dl_forward_list(self):
        username, role = self._dl_get_user_and_role()
        r = _dl_api.handle_list(user=username, role=role)
        self.send_json(r[0] if isinstance(r, tuple) else r, r[1] if isinstance(r, tuple) else 200)

    def _dl_forward_config(self):
        r = _dl_api.handle_get_config()
        self.send_json(r[0] if isinstance(r, tuple) else r, r[1] if isinstance(r, tuple) else 200)

    def _dl_forward_config_post(self):
        self._dl_forward_cmd(_dl_api.handle_set_config)

    def _dl_forward_cancel(self):
        self._dl_forward_cmd(_dl_api.handle_cancel)

    def _dl_forward_pause(self):
        self._dl_forward_cmd(_dl_api.handle_pause)

    def _dl_forward_resume(self):
        self._dl_forward_cmd(_dl_api.handle_resume)

    def _dl_forward_delete(self):
        self._dl_forward_cmd(_dl_api.handle_delete)

    def _dl_forward_merge(self):
        self._dl_forward_cmd(_dl_api.handle_merge)

    def _dl_forward_retry(self):
        self._dl_forward_cmd(_dl_api.handle_retry)

    def _dl_forward_cmd(self, fn):
        data = self._read_json()
        if data is None: return  # 已回 400 Invalid Content-Length
        user, role = self._dl_get_user_and_role()
        r = fn(data, user=user, role=role)
        self.send_json(r[0] if isinstance(r, tuple) else r, r[1] if isinstance(r, tuple) else 200)

    def _dl_forward_probe(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        r = _dl_api.handle_probe(params)
        self.send_json(r[0] if isinstance(r, tuple) else r, r[1] if isinstance(r, tuple) else 200)

    def _dl_forward_peers(self):
        self._dl_forward_cmd(_dl_api.handle_peers)

    def _dl_forward_parse_torrent(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        r = _dl_api.handle_parse_torrent_url(params)
        self.send_json(r[0] if isinstance(r, tuple) else r, r[1] if isinstance(r, tuple) else 200)

    def _dl_forward_upload_torrent(self):
        MAX = 10 * 1024 * 1024
        ct = self.headers.get('Content-Type', '')
        try:
            cl = int(self.headers.get('Content-Length', 0))
        except (TypeError, ValueError):
            self.send_json({'error': 'Invalid Content-Length'}, 400); return
        if cl < 0:
            self.send_json({'error': 'Invalid Content-Length'}, 400); return
        if cl > MAX: self.send_json({'success': False, 'error': '文件过大'}, 413); return
        if not ct.startswith('multipart/form-data'): self.send_json({'success': False, 'error': '需要 multipart'}, 400); return
        try:
            fn, payload = self._parse_multipart_file(ct, cl)
            if not payload: self.send_json({'success': False, 'error': '未找到文件'}, 400); return
            r = _dl_api.handle_upload_torrent(payload, fn)
            self.send_json(r[0] if isinstance(r, tuple) else r, r[1] if isinstance(r, tuple) else 200)
        except Exception as e:
            add_log(f'种子上传解析失败: {str(e)}', 'err')
            self.send_json({'success': False, 'error': str(e)}, 500)

    def _get_user_start_path(self, username, role):
        return _fs.get_user_start_path(username, role)

# ---------- WebSocket ----------
_download_subscribers = set()
_ds_lock = threading.Lock()
_main_loop = None

def set_main_loop(loop):
    global _main_loop
    _main_loop = loop

# ---------- 管理页实时推送（WebSocket，每秒一帧） ----------
_admin_subscribers = set()
_admin_lock = threading.Lock()

# ---------- 二维码登录事件（WebSocket 即时推送，替代轮询） ----------
_qr_waiters = {}          # sid -> set(websocket)
_qr_waiters_lock = threading.Lock()

def _broadcast_qr_consumed(sid):
    """通知等待该二维码的页面：已被扫码登录"""
    with _qr_waiters_lock:
        wss = _qr_waiters.pop(sid, set())
    if wss and _main_loop is not None:
        text = json.dumps({'type': 'qr_consumed', 'sid': sid})
        for ws in wss:
            try:
                asyncio.run_coroutine_threadsafe(_safe_send(ws, text), _main_loop)
            except Exception:
                pass

def build_connections_payload():
    """构造连接用户列表快照（供 /api/connections 与 WebSocket 管理推送共用）"""
    conns = _cfg.get_connections()
    now = time.time()
    result = [{'ip': ip, 'first_seen': i['first_seen'], 'last_seen': i['last_seen'],
                'user_agent': i['user_agent'], 'request_count': i['request_count'],
                'username': i.get('username', ''), 'device': i.get('device', ''),
                'role': _ut_log.show_role_display(i.get('role', '')),
                'active_secs': int(now - i['last_seen'])} for ip, i in conns.items()]
    result.sort(key=lambda x: x['last_seen'], reverse=True)
    return result

def _admin_snapshot_payload():
    """构造管理页一帧数据（等价 /api/stats + /api/connections）

    文件统计底层采用“变更即失效”的缓存（fs_core.get_server_stats 挂靠
    invalidate_folder_cache 钩子）：无文件变更时每秒直接复用缓存结果，
    有变更时下一次读取即重算，因此逐帧构建开销很小。
    """
    try:
        stats = _fs_api.build_stats_data(
            _fs.get_server_stats, _fs.get_folder_size, _fs.has_ffmpeg,
            _cfg.get_max_concurrent, _cfg.COPY_BUFFER_SIZE, _cfg.get_speed_limit,
            _cfg.get_connections, _cfg.PORT, _cfg.get_guest_mode,
            _cfg.get_default_user_quota, _cfg.get_public_quota,
            _cfg.get_total_quota, _fs.UPLOAD_DIR)
    except Exception:
        return None
    return {'type': 'admin_data', 'ts': time.time(),
            'stats': stats, 'connections': build_connections_payload(),
            'users': _ac.list_users()}

def _admin_push_loop():
    """每秒向订阅管理推送的 WebSocket 发送一帧管理页快照"""
    while True:
        try:
            with _admin_lock:
                targets = list(_admin_subscribers)
            if targets and _main_loop is not None:
                payload = _admin_snapshot_payload()
                if payload is not None:
                    text = json.dumps(payload)
                    for ws in targets:
                        try:
                            asyncio.run_coroutine_threadsafe(_safe_send(ws, text), _main_loop)
                        except Exception:
                            with _admin_lock:
                                _admin_subscribers.discard(ws)
        except Exception:
            pass
        time.sleep(1)

def _broadcast_download_daemon_status():
    """向所有下载器订阅者推送 aria2c 守护进程状态（页面据此显示启动中/就绪/失败）"""
    from leaffs.downloader_core import dl_rpc as _dlr
    status = _dlr.get_aria2c_status()
    with _ds_lock:
        if not _download_subscribers or _main_loop is None:
            return
        text = json.dumps({'type': 'daemon_status', 'status': status})
        for ws in list(_download_subscribers):
            try:
                asyncio.run_coroutine_threadsafe(_safe_send(ws, text), _main_loop)
            except Exception:
                _download_subscribers.discard(ws)

async def _safe_send(ws, msg):
    """安全发送 WebSocket 消息，异常不影响事件循环"""
    try:
        await ws.send(msg)
    except Exception:
        pass

def _broadcast_download_update(task_data):
    """广播下载进度更新到所有 WebSocket 客户端（按用户/角色隔离）

    管理员/超级管理员收全部；普通用户只收属于自己的任务更新，
    避免通过 WS 推送看到/渲染到其它用户的任务。
    """
    with _ds_lock:
        if not _download_subscribers or _main_loop is None:
            return
        msg = json.dumps({'type': 'download_update', 'task': task_data})
        task_user = task_data.get('user') or ''
        for ws in list(_download_subscribers):
            role = getattr(ws, 'cached_role', None)
            user = getattr(ws, 'cached_user', '') or ''
            try:
                if role not in ('admin', 'super_admin'):
                    # 普通用户只收自己发起(用户名一致)的任务更新
                    if not user or task_user != user:
                        continue
                asyncio.run_coroutine_threadsafe(_safe_send(ws, msg), _main_loop)
            except Exception:
                _download_subscribers.discard(ws)

import concurrent.futures
_thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=20)

async def _run_io(fn, *args):
    return await asyncio.wrap_future(_thread_pool.submit(fn, *args))

def _ws_get_session(websocket):
    """从 WebSocket 请求头中获取会话（cookie 会话认证，A-05）。

    websockets ≥14（本项目 16.0）握手头在 websocket.request.headers，
    不再有 request_headers 属性；legacy 版本用 websocket.request_headers。
    与 _ws_origin_ok 相同的双来源读取，避免新版库下 Cookie 解析恒为空、
    纯 cookie 会话的 WS 连接被当成匿名（admin-sub 等一律 Unauthorized）。
    """
    try:
        cookies_raw = ''
        req = getattr(websocket, 'request', None)
        headers = getattr(req, 'headers', None) if req is not None else None
        if headers is None:
            headers = getattr(websocket, 'request_headers', None)
        if headers is not None:
            try:
                if hasattr(headers, 'get_all'):
                    # websockets16 Headers.get_all(key) 不接受默认参数（签名仅一参），
                    # 传第二参抛 TypeError 被吞 → 恒匿名。这里只传 key 并容错 None/异常
                    _vals = headers.get_all('Cookie')
                    if _vals:
                        cookies_raw = '; '.join(_vals)
                elif hasattr(headers, 'get'):
                    cookies_raw = headers.get('Cookie') or ''
            except Exception:
                # 异常时回退 get('Cookie') 再失败则视为无 Cookie（不误伤会话判定）
                try:
                    if hasattr(headers, 'get'):
                        cookies_raw = headers.get('Cookie') or ''
                except Exception:
                    cookies_raw = ''
        ws_ip = getattr(websocket, 'remote_address', ('', 0))[0]
        role, sid = _ac.get_session(cookies_raw, True, ws_ip)
        username = _ac.get_session_username(sid) if sid else ''
        return role, username, sid
    except Exception:
        return None, '', ''


def _normalize_ws_rel_path(rel_path):
    """规范化 WebSocket 中的相对路径并禁止路径穿越"""
    if not rel_path:
        return ''
    # 先检查原始路径中是否包含 ..（必须在 normpath 之前检查）
    raw_parts = rel_path.replace('\\', '/').split('/')
    if '..' in raw_parts:
        return None
    if raw_parts and raw_parts[0] in ('..', '.'):
        return None
    norm = os.path.normpath(rel_path).replace('\\', '/')
    if norm.startswith('/'):
        return None
    if norm in ('..', '../') or norm.startswith('../'):
        return None
    return norm


# WS 敏感消息类型：会话被撤销/过期后这些操作必须逐条实时复查（见 ws_handler），
# 未显式 auth（纯 cookie 会话）的连接已在每条消息的“获取会话”段实时复查，无需重复。
_WS_REVALIDATE_TYPES = ('admin-sub', 'list', 'delete', 'mkdir', 'sub-download', 'upload')

# A-17：未认证（ws_role 为空）连接仅放行的最小消息集
_WS_ANON_ALLOWED = frozenset(('auth', 'ping', 'qr-sub', 'qr-unsub', 'admin-unsub', 'unsub-download'))

# A-06：WS 连接/消息限流参数（单 IP 活跃连接、滑动窗口消息速率）
# 单 IP 活跃连接上限改由配置键 ws_max_conn_per_ip 提供（默认 8），本常量仅作 cfg 不可用时兜底；
# 消息速率类参数仍为代码常量。
_WS_MAX_CONN_PER_IP = 8
_WS_MSG_WINDOW_SECS = 10.0
_WS_MSG_PER_CONN = 30
_WS_MSG_PER_IP = 120
# 自动断链参数：单 IP 配额已满时，优先自动关闭该 IP 中“空闲 ≥ 该秒数”的最旧连接
# （close(1013, 'server-evict-idle')）以腾出配额接纳新连接；全部连接都活跃才拒绝新连接。
_WS_IDLE_EVICT_SECS = 60.0
_WS_EVICT_CLOSE_CODE = 1013
_WS_EVICT_CLOSE_REASON = 'server-evict-idle'

# 每 IP WS 限流状态：ip -> {'conns': {websocket: last_seen(monotonic)}, 'msgs': deque[monotonic ts]}
# conns 以连接对象为键、记录最近活跃时刻（收到任意消息即刷新），配额满时据此淘汰最旧空闲连接；
# 每条登记过的连接，无论正常结束/异常/被限流 close/接受后立即被拒，都经 ws_handler 的 finally
# 按同一连接对象摘除（幂等），与进入登记一一对应，杜绝“只增不减”把配额永久占死。
_ws_conn_stats = {}
_ws_stats_lock = threading.Lock()


def _ws_conn_enter(ip, websocket):
    """单 IP 活跃 WS 连接登记（A-06）。

    配额未满 → 登记并返回 ('ok', [])。
    配额已满 → 若该 IP 存在空闲 ≥ _WS_IDLE_EVICT_SECS 秒的连接，先摘除其中最旧（LRU）
    的一条并登记本连接，返回 ('evict', [被淘汰连接])，由调用方负责 close 自动断链；
    若全部连接都在空闲阈值内（都活跃）→ 不登记并返回 ('reject', [])，由调用方拒绝。
    拒绝/淘汰路径都不会留下“被拒连接”的计数。
    """
    now = time.monotonic()
    # 每 IP WS 连接上限取配置键 ws_max_conn_per_ip（与 HTTP 每 IP 上限一起在管理页调整）；
    # cfg 不可用/非法时回退代码常量 _WS_MAX_CONN_PER_IP
    try:
        ws_limit = max(1, int(_cfg.get_ws_max_conn_per_ip()))
    except Exception:
        ws_limit = _WS_MAX_CONN_PER_IP
    with _ws_stats_lock:
        st = _ws_conn_stats.get(ip)
        if st is None:
            st = {'conns': {}, 'msgs': deque()}
            _ws_conn_stats[ip] = st
        conns = st['conns']
        if len(conns) < ws_limit:
            conns[websocket] = now
            return 'ok', []
        idle = [(c, ts) for c, ts in conns.items() if now - ts >= _WS_IDLE_EVICT_SECS]
        if not idle:
            return 'reject', []
        idle.sort(key=lambda x: x[1])        # last_seen 升序 → 最久未活跃者在最前
        victim = idle[0][0]
        del conns[victim]
        conns[websocket] = now
        return 'evict', [victim]


def _ws_conn_touch(ip, websocket):
    """收到任意消息时刷新该连接的最近活跃时刻（自动断链只淘汰真正空闲的最旧连接）"""
    with _ws_stats_lock:
        st = _ws_conn_stats.get(ip)
        if st is not None and websocket in st['conns']:
            st['conns'][websocket] = time.monotonic()


def _ws_conn_leave(ip, websocket):
    """连接结束清理（ws_handler 的 finally 统一调用）。

    按连接对象摘除而非计数递减：正常结束/异常/被限流 close/接受后立即被拒等一切路径，
    都以“进入登记时的同一对象”摘除，天然一一对应、只减一次；已被配额淘汰（enter 已摘除）
    的连接在此重复摘除为幂等空操作，不会把配额减成负数。
    """
    with _ws_stats_lock:
        st = _ws_conn_stats.get(ip)
        if not st:
            return
        st['conns'].pop(websocket, None)
        if not st['conns']:
            now = time.monotonic()
            q = st['msgs']
            while q and now - q[0] > _WS_MSG_WINDOW_SECS:
                q.popleft()
            if not q:
                _ws_conn_stats.pop(ip, None)


def _ws_conn_msg_ok(websocket):
    """单连接滑动窗口：≤30 msg/10s（计数全部消息，含低敏）"""
    now = time.monotonic()
    q = getattr(websocket, '_ws_msg_ts', None)
    if q is None:
        q = deque()
        websocket._ws_msg_ts = q
    while q and now - q[0] > _WS_MSG_WINDOW_SECS:
        q.popleft()
    q.append(now)
    return len(q) <= _WS_MSG_PER_CONN


def _ws_ip_msg_ok(ip):
    """单 IP 聚合滑动窗口：≤120 msg/10s（跨连接连续计时；ip 字典的生命周期由进入登记/结束清理维护）"""
    now = time.monotonic()
    with _ws_stats_lock:
        st = _ws_conn_stats.setdefault(ip, {'conns': {}, 'msgs': deque()})
        q = st['msgs']
        while q and now - q[0] > _WS_MSG_WINDOW_SECS:
            q.popleft()
        q.append(now)
        return len(q) <= _WS_MSG_PER_IP


def _ws_origin_ok(websocket):
    """WS Origin/Host 同源校验（A-06）。

    Host 必须为本机 host/IP/localhost（端口剥离后比对）；Origin（如存在）的
    scheme+host 必须同站。无 Origin 的原生客户端放行但照常计入限流。
    """
    try:
        allowed = set(_collect_ips())
        allowed.update(_LOCAL_HOST_NAMES)
        req = getattr(websocket, 'request', None)
        headers = getattr(req, 'headers', None) if req is not None else None
        if headers is None:
            headers = getattr(websocket, 'request_headers', None)
        if headers is not None and hasattr(headers, 'get'):
            host = headers.get('Host') or ''
        else:
            host = ''
        if host and _strip_host_port(host) not in allowed:
            return False
        origin = ''
        try:
            origin = (getattr(websocket, 'origin', None) or '').strip()
        except Exception:
            origin = ''
        if not origin and headers is not None and hasattr(headers, 'get'):
            origin = (headers.get('Origin') or '').strip()
        if not origin:
            return True  # 原生客户端无 Origin：放行，限流兜底
        if origin.lower() == 'null':
            return False
        u = urllib.parse.urlparse(origin)
        if u.scheme not in ('http', 'https'):
            return False
        return _strip_host_port(u.hostname or '') in allowed
    except Exception:
        return False


def _ws_dl_allowed(ws_role):
    """A-03（WS 侧）：下载器订阅门槛，与 HTTP _dl_allowed 同一判定"""
    if ws_role in ('user', 'admin', 'super_admin'):
        return True
    if ws_role == 'guest':
        try:
            return bool(_cfg.get_downloader_guest_allowed())
        except Exception:
            return False
    return False


async def ws_handler(websocket):
    global _LOCAL_TOKEN   # WS auth 一次性令牌分支需写回（D4）
    sub = False
    admin_sub = False
    ip = ''
    try:
        ip = getattr(websocket, 'remote_address', ('', 0))[0]
    except Exception:
        ip = ''
    # A-06：单 IP 活跃 WS 连接登记。配额满时优先自动淘汰该 IP 最旧的空闲连接
    # （last_seen 距今 ≥ _WS_IDLE_EVICT_SECS 即 close(1013,'server-evict-idle') 自动断链），
    # 腾出配额后接纳新连接；仅当全部连接都活跃（无空闲可淘汰）才拒绝新连接 close(1013)。
    _enter_ret, _evicted = _ws_conn_enter(ip, websocket)
    if _enter_ret == 'reject':
        try:
            add_log(f'WS 单 IP 连接数超限且无空闲连接可淘汰（全部活跃），拒绝来自 {ip or "?"} 的连接', 'warn')
        except Exception:
            pass
        try:
            await websocket.close(code=1013)
        except Exception:
            pass
        return
    if _enter_ret == 'evict':
        try:
            add_log(f'WS 单 IP 连接数超限，已自动淘汰来自 {ip or "?"} 的 {len(_evicted)} 条空闲旧连接'
                    f'（空闲≥{int(_WS_IDLE_EVICT_SECS)}s）并接纳新连接', 'warn')
        except Exception:
            pass
        for _old in _evicted:
            try:
                await _old.close(code=_WS_EVICT_CLOSE_CODE, reason=_WS_EVICT_CLOSE_REASON)
            except Exception:
                pass
    try:
        # A-06：Origin/Host 同源校验（跨站/rebinding 页拒绝）
        if not _ws_origin_ok(websocket):
            try:
                add_log(f'WS Origin/Host 校验失败，拒绝来自 {ip or "?"} 的连接', 'warn')
            except Exception:
                pass
            try:
                await websocket.close(code=1008, reason='origin/host check failed')
            except Exception:
                pass
            return
        # === 诊断插桩：连接建立记录（仅 add_log，不改行为；不打印 cookie 值）===
        try:
            _dbg_req = getattr(websocket, 'request', None)
            _dbg_hdrs = getattr(_dbg_req, 'headers', None) if _dbg_req is not None else None
            if _dbg_hdrs is None:
                _dbg_hdrs = getattr(websocket, 'request_headers', None)
            _dbg_host = _dbg_cookie = ''
            if _dbg_hdrs is not None:
                try:
                    if hasattr(_dbg_hdrs, 'get_all'):
                        _dbg_cv = _dbg_hdrs.get_all('Cookie')
                        _dbg_cookie = '; '.join(_dbg_cv) if _dbg_cv else ''
                        _dbg_host = _dbg_hdrs.get('Host', '') or ''
                    elif hasattr(_dbg_hdrs, 'get'):
                        _dbg_cookie = _dbg_hdrs.get('Cookie', '') or ''
                        _dbg_host = _dbg_hdrs.get('Host', '') or ''
                except Exception:
                    _dbg_host = _dbg_cookie = ''
            _dbg_origin = ''
            try:
                _dbg_origin = (getattr(websocket, 'origin', None) or '').strip()
            except Exception:
                _dbg_origin = ''
            if not _dbg_origin and _dbg_hdrs is not None:
                try:
                    _dbg_origin = (_dbg_hdrs.get('Origin') or '').strip()
                except Exception:
                    pass
            _dbg_cookie_has = bool(_dbg_cookie and (_ac.AUTH_COOKIE + '=') in _dbg_cookie)
            add_log(f'WS 已连接: ip={ip or "?"} host={_dbg_host or "(none)"} '
                    f'origin={_dbg_origin or "(none)"} cookie_has_wifi_session={_dbg_cookie_has}', 'info')
        except Exception:
            pass
        try:
            async for msg in websocket:
                # 收到任意消息即刷新最近活跃时刻（配额满时只淘汰真正空闲的最旧连接）
                _ws_conn_touch(ip, websocket)
                # A-06：滑动窗口消息限流（每连接 30/10s；每 IP 120/10s；全部消息计数）
                if not (_ws_conn_msg_ok(websocket) and _ws_ip_msg_ok(ip)):
                    try:
                        add_log(f'WS 消息频率超限，关闭来自 {ip or "?"} 的连接', 'warn')
                    except Exception:
                        pass
                    try:
                        await websocket.close(code=1008, reason='message rate limit')
                    except Exception:
                        pass
                    break
                try:
                    data = json.loads(msg)
                except (json.JSONDecodeError, TypeError):
                    continue
                t = data.get('type', '')
                # 获取 WebSocket 会话（优先使用认证缓存，其次握手 Cookie）
                ws_role = getattr(websocket, 'cached_role', None)
                ws_user = getattr(websocket, 'cached_user', '')
                if not ws_role:
                    ws_role, ws_user, _ = _ws_get_session(websocket)
                # 游客模式已关闭：游客会话一律视为无效（防残留会话/构造请求经 WS 访问公共目录）
                if ws_role == 'guest' and not _cfg.get_guest_mode():
                    ws_role = None
                    ws_user = ''

                # === 诊断插桩：首次角色解析结果（每条连接只记一次；仅记录，不改行为）===
                if not getattr(websocket, '_ws_dbg_role_logged', False):
                    websocket._ws_dbg_role_logged = True
                    try:
                        if ws_role:
                            add_log(f'WS 角色解析: ip={ip or "?"} first_msg={t} role={ws_role} '
                                    f'user={ws_user or ""}', 'info')
                        else:
                            add_log(f'WS 角色解析: ip={ip or "?"} first_msg={t} role=空（未认证/会话未解析）', 'warn')
                    except Exception:
                        pass

                # --- A-07：敏感消息按“用户当前角色”实时复查（非会话快照）---
                # 显式 auth 的连接把角色/sid 缓存在 websocket 对象上；此处经 B 的
                # refresh_session_role(sid, ip) 刷新为最新角色/用户名（降权/改名/删除
                # 即时生效），再以最新角色执行本消息。低敏消息不复查以省开销。
                if t in _WS_REVALIDATE_TYPES:
                    _ws_cached_sid = getattr(websocket, 'cached_sid', None)
                    if _ws_cached_sid:
                        _ws_chk_ip = getattr(websocket, 'remote_address', ('', 0))[0]
                        try:
                            _ws_fresh = _ac.refresh_session_role(_ws_cached_sid, _ws_chk_ip)
                        except Exception:
                            _ws_fresh = None
                        if _ws_fresh is None:
                            # 会话已失效/用户已删除：清角色并拒绝（保留 cached_sid 持续拦截）
                            websocket.cached_role = None
                            websocket.cached_user = ''
                            ws_role = None
                            ws_user = ''
                            try:
                                await websocket.send(json.dumps({'type': 'error', 'msg': '会话已失效，请重新登录'}))
                            except Exception:
                                pass
                            continue
                        fr, fu = _ws_fresh
                        if (fr, fu) != (getattr(websocket, 'cached_role', None),
                                        getattr(websocket, 'cached_user', '')):
                            websocket.cached_role = fr
                            websocket.cached_user = fu
                        ws_role = fr
                        ws_user = fu
                        if ws_role == 'guest' and not _cfg.get_guest_mode():
                            websocket.cached_role = None
                            websocket.cached_user = ''
                            ws_role = None
                            ws_user = ''
                            try:
                                await websocket.send(json.dumps({'type': 'error', 'msg': '游客模式已关闭'}))
                            except Exception:
                                pass
                            continue

                # --- A-17：未认证连接最小响应集（不再有“本机匿名=超管”面）---
                if not ws_role and t not in _WS_ANON_ALLOWED:
                    try:
                        await websocket.send(json.dumps({'type': 'error', 'msg': 'Unauthorized'}))
                    except Exception:
                        pass
                    continue

                if t == 'auth':
                    sid = (data.get('sid') or '').strip()
                    token = (data.get('token') or '').strip()
                    ws_role = None
                    ws_user = ''
                    if sid:
                        with _ac._sessions_lock:
                            info = _ac._sessions.get(sid)
                            if info and info.get('expiry', 0) > time.time():
                                ws_ip = getattr(websocket, 'remote_address', ('', 0))[0]
                                if _ac._same_client(info.get('ip', ''), ws_ip):
                                    ws_role = info.get('role')
                                    ws_user = info.get('username', '')
                    elif token:
                        # A-05/D4：本机一次性令牌 —— 仅环回来源 + 与 _LOCAL_TOKEN 恒定时间
                        # 比对；令牌只授权“建立”super_admin 会话，会话仍由 B 生成真实 sid
                        ws_ip = getattr(websocket, 'remote_address', ('', 0))[0]
                        if ws_ip in ('127.0.0.1', '::1') and _LOCAL_TOKEN \
                                and secrets.compare_digest(token, _LOCAL_TOKEN):
                            try:
                                su = _ac.get_super_admin_name() or 'admin'
                            except Exception:
                                su = 'admin'
                            sid = _ac.create_session(su, 'super_admin', client_ip=ws_ip)
                            ws_role = 'super_admin'
                            ws_user = su
                            # 一次性：首个成功使用后立即失效
                            _LOCAL_TOKEN = None
                            _delete_local_token_file()
                            add_log('WS 本机一次性令牌认证成功（super_admin）', 'ok')
                    if ws_role == 'guest' and not _cfg.get_guest_mode():
                        ws_role = None
                    if ws_role:
                        # 游客下载任务/推送按来源 IP 隔离：与 HTTP _dl_get_user_and_role
                        # 保持同一归属键 游客@<ip>，游客 WS 只收本 IP 的任务更新
                        if ws_role == 'guest' and ws_user == '游客':
                            ws_ip = getattr(websocket, 'remote_address', ('', 0))[0]
                            ws_user = '游客@' + ws_ip
                        # 缓存认证结果到 websocket 对象，后续消息直接使用
                        websocket.cached_role = ws_role
                        websocket.cached_user = ws_user
                        # 额外缓存 sid，供后续敏感消息实时复查（撤销/过期立即失效）
                        if sid:
                            websocket.cached_sid = sid
                        try:
                            await websocket.send(json.dumps({'type': 'auth', 'success': True, 'role': ws_role}))
                        except Exception:
                            pass
                    else:
                        try:
                            await websocket.send(json.dumps({'type': 'auth', 'success': False}))
                        except Exception:
                            pass
                    continue

                if t == 'admin-sub':
                    # 管理页订阅实时推送（仅限管理员/超级管理员）
                    if ws_role not in ('admin', 'super_admin'):
                        try:
                            add_log(f'WS admin-sub 拒绝: ip={ip or "?"} role={ws_role or "空"}', 'warn')
                        except Exception:
                            pass
                        await websocket.send(json.dumps({'type': 'error', 'msg': '无权限'})); continue
                    try:
                        add_log(f'WS admin-sub 命中: ip={ip or "?"} role={ws_role} user={ws_user or ""}', 'info')
                    except Exception:
                        pass
                    with _admin_lock: _admin_subscribers.add(websocket); admin_sub = True
                    # 订阅后立即推送一帧，避免等待下一个推送周期
                    payload = _admin_snapshot_payload()
                    if payload:
                        try:
                            await websocket.send(json.dumps(payload))
                        except Exception as _e:
                            # === 诊断插桩：管理首帧 send 异常记录后按原语义抛出（不改行为）===
                            try:
                                add_log(f'WS admin_data 发送异常: ip={ip or "?"} '
                                        f'{type(_e).__name__}: {_e}', 'warn')
                            except Exception:
                                pass
                            raise
                    continue
                if t == 'admin-unsub':
                    with _admin_lock: _admin_subscribers.discard(websocket); admin_sub = False
                    continue

                if t == 'qr-sub':
                    # 二维码弹窗订阅“已扫码”事件（sid 为随机密钥，无需额外鉴权）
                    sid = (data.get('sid') or '').strip()
                    if not sid: continue
                    websocket._qr_subscribed = True
                    with _qr_waiters_lock:
                        _qr_waiters.setdefault(sid, set()).add(websocket)
                    continue
                if t == 'qr-unsub':
                    sid = (data.get('sid') or '').strip()
                    with _qr_waiters_lock:
                        st = _qr_waiters.get(sid)
                        if st:
                            st.discard(websocket)
                            if not st: del _qr_waiters[sid]
                    continue

                if t == 'list':
                    p = data.get('path', '')
                    # 规范化路径
                    norm_p = _normalize_ws_rel_path(p)
                    if norm_p is None:
                        await websocket.send(json.dumps({'type': 'error', 'msg': '路径不合法'}))
                        continue
                    p = norm_p
                    if not _check_path_permission_core(ws_role, ws_user, p, _cfg.get_guest_mode()):
                        await websocket.send(json.dumps({'type': 'error', 'msg': '无权限'}))
                        continue
                    r = await _run_io(_fs.list_files, p)
                    result_data = r[0] if r[0] else {'files': []}
                    result_data['type'] = 'list'
                    result_data['current_path'] = p
                    if 'total_file_count' not in result_data:
                        result_data['total_file_count'] = 0
                    if 'total_size_sum' not in result_data:
                        result_data['total_size_sum'] = 0
                    await websocket.send(json.dumps(result_data))
                elif t == 'delete':
                    paths = data.get('paths', []) or []
                    n_deleted = 0
                    failed = []      # [(path, reason)] —— C-07：句柄占用等不再静默成功
                    for p in paths:
                        norm_p = _normalize_ws_rel_path(p)
                        if norm_p is None:
                            failed.append((str(p), '路径不合法'))
                            continue
                        # 路径权限（访问面）→ 写策略（guest 只读面，IC-WRITE）
                        if not _check_path_permission_core(ws_role, ws_user, norm_p, _cfg.get_guest_mode()):
                            failed.append((norm_p, '无权限'))
                            continue
                        try:
                            ws_write_ok = _fs_api.ws_write_allowed(ws_role, ws_user, norm_p, 'delete',
                                                                   _cfg.get_guest_mode())
                        except Exception:
                            ws_write_ok = True  # IC-WRITE 未就绪过渡期放行给 C-01/C-02 兜底
                        if not ws_write_ok:
                            failed.append((norm_p, '无权限'))
                            continue
                        try:
                            cnt, fails = _fs.delete_paths([norm_p])
                        except Exception as e:
                            cnt, fails = 0, [(norm_p, f'删除失败: {e}')]
                        n_deleted += int(cnt or 0)
                        for _pn, _why in (fails or []):
                            failed.append((_pn, _why))
                    resp = {'type': 'delete', 'success': n_deleted > 0, 'deleted': n_deleted}
                    if failed:
                        resp['failed'] = [{'path': str(a), 'error': str(b)} for a, b in failed]
                    await websocket.send(json.dumps(resp))
                    for _p, _why in failed:
                        try:
                            await websocket.send(json.dumps({'type': 'error', 'msg': f'{_why}: {_p}'}))
                        except Exception:
                            pass
                elif t == 'mkdir':
                    p, n = data.get('path', ''), data.get('name', '')
                    norm_p = _normalize_ws_rel_path(p + '/' if p else '')
                    if norm_p is None:
                        await websocket.send(json.dumps({'type': 'error', 'msg': '路径不合法'})); continue
                    p = norm_p.rstrip('/') if norm_p else ''
                    if not _check_path_permission_core(ws_role, ws_user, p + '/' if p else '', _cfg.get_guest_mode()):
                        await websocket.send(json.dumps({'type': 'error', 'msg': '无权限'})); continue
                    # IC-WRITE：guest 一律禁 mkdir（R1），非 guest 由路径权限兜底
                    try:
                        ws_write_ok = _fs_api.ws_write_allowed(ws_role, ws_user, p, 'mkdir',
                                                               _cfg.get_guest_mode())
                    except Exception:
                        ws_write_ok = True
                    if not ws_write_ok:
                        await websocket.send(json.dumps({'type': 'error', 'msg': '无权限'})); continue
                    ok, err = _fs.mkdir(p, n)
                    await websocket.send(json.dumps({'type': 'mkdir', 'success': ok, 'msg': err}))
                elif t == 'sub-download':
                    # A-03：WS 下载器订阅门槛（与 HTTP _dl_allowed 同一判定）
                    if not _ws_dl_allowed(ws_role):
                        if ws_role == 'guest':
                            await websocket.send(json.dumps({'type': 'error', 'msg': '游客不可使用下载器'}))
                        else:
                            await websocket.send(json.dumps({'type': 'error', 'msg': 'Unauthorized'}))
                        continue
                    # 未走 auth 消息、仅凭 cookie 会话的游客连接在此兜底对齐归属键
                    if ws_role == 'guest' and ws_user == '游客':
                        ws_ip = getattr(websocket, 'remote_address', ('', 0))[0]
                        ws_user = '游客@' + ws_ip
                        websocket.cached_user = ws_user
                    with _ds_lock: _download_subscribers.add(websocket); sub = True
                    try:
                        if ws_role in ('admin', 'super_admin'):
                            tasks = _dl_manager.get_all_tasks()
                        else:
                            tasks = _dl_manager.get_user_tasks(ws_user) if ws_user else []
                        await websocket.send(json.dumps({'type': 'download_list', 'tasks': tasks}))
                    except Exception:
                        await websocket.send(json.dumps({'type': 'download_list', 'tasks': []}))
                    # 立即回报当前 daemon 状态（页面据此显示“aria2c 启动中/就绪/失败”）
                    try:
                        from leaffs.downloader_core import dl_rpc as _dlr
                        await websocket.send(json.dumps({'type': 'daemon_status', 'status': _dlr.get_aria2c_status()}))
                    except Exception:
                        pass
                elif t == 'unsub-download':
                    with _ds_lock: _download_subscribers.discard(websocket); sub = False
                elif t == 'ping':
                    await websocket.send(json.dumps({'type': 'pong'}))
        except websockets.exceptions.ConnectionClosed:
            pass
    finally:
        # 结束清理：任何路径（正常结束/异常/被限流 close/接受后立即被拒）都按进入登记时的
        # 同一连接对象摘除，保证只减一次且不漏减；已被配额淘汰的连接此处为幂等空操作。
        _ws_conn_leave(ip, websocket)
        if sub:
            with _ds_lock: _download_subscribers.discard(websocket)
        if admin_sub:
            with _admin_lock: _admin_subscribers.discard(websocket)
        if getattr(websocket, '_qr_subscribed', False):
            with _qr_waiters_lock:
                for sid in list(_qr_waiters.keys()):
                    st = _qr_waiters[sid]
                    st.discard(websocket)
                    if not st: del _qr_waiters[sid]

# ---------- 启动 ----------
def _primary_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'

def _collect_ips():
    """收集本机全部 IPv4（供 Host/Origin 白名单、二维码 base 等使用）。

    保守实现（socket.getaddrinfo / gethostbyname_ex），不依赖 psutil/ipconfig。
    结果去重并剔除环回地址；探测全失败则退化为 [_primary_lan_ip(), '127.0.0.1']。
    """
    out = []

    def _add(ip):
        ip = (ip or '').strip()
        if ip and not ip.startswith('127.') and ip not in out:
            out.append(ip)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        _add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            _add(info[4][0])
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            _add(ip)
    except Exception:
        pass
    if not out:
        p = _primary_lan_ip() or '127.0.0.1'
        out = [p] if not p.startswith('127.') else ['127.0.0.1']
    return out


_tls_ctx_cache = None
_tls_ctx_lock = threading.Lock()

def _tls_context():
    """进程级单例：只构建一次 TLS 上下文并缓存。

    证书来源见 _build_tls_context：显式配置 tls_cert/tls_key，或既有/自动生成的
    config/selfsigned.crt + selfsigned.key（首次启动无证书时自动生成，非 CA、
    不装信任库）。缓存避免 run_http / run_ws 两个线程重复加载同一证书。
    """
    global _tls_ctx_cache
    with _tls_ctx_lock:
        if _tls_ctx_cache is not None:
            return _tls_ctx_cache
        _tls_ctx_cache = _build_tls_context()
        return _tls_ctx_cache

def _new_server_ctx():
    """A-09：集中构造服务端 TLS 上下文 —— 显式 TLS1.2 下限 + 密码套件白名单。

    R3/IC-TLS：服务端 SSLContext 统一走本函数（load_cert_chain 在调用方 _build_tls_context）。
    """
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.set_ciphers('ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:!aNULL:!eNULL:!MD5:!3DES:!RC4')
    except Exception as e:
        # 个别老 openssl 不接受该列表时回退系统默认，仅记录不阻断
        add_log(f'TLS 密码套件设置被拒（使用系统默认）: {e}', 'warn')
    return ctx

def _openssl_exe():
    """定位 openssl.exe：优先随包分发（资源根/主程序目录/源码包目录），其次 PATH。

    返回可执行文件路径字符串；找不到返回 None（调用方据此给出明确错误）。
    """
    try:
        return _ut.find_bundled_exe('openssl.exe')
    except Exception:
        return None


def _auto_gen_server_cert(cert_path, key_path):
    """首次启动无证书时自动生成一张随机自签【服务器证书】（非 CA、不装信任库）。

    仅当 TLS 已启用、且 server_config.json 未配置 tls_cert/tls_key、config 下也不存在
    既有证书时被调用（见 _build_tls_context）。生成结果写入 cert_path/key_path
    （config/selfsigned.crt + selfsigned.key），此后每次启动直接复用，不重复生成。

    证书为纯叶节点自签：SAN 覆盖 localhost、本机主机名与本机全部 IPv4，局域网内以
    IP 或主机名访问即可命中证书；绝不签发 CA、绝不写入任何系统信任库。
    返回 True=成功；False=失败（失败时清理半成品文件，由调用方维持“策略 A”拒绝明文）。
    """
    import subprocess
    openssl = _openssl_exe()
    if not openssl:
        add_log('自动生成服务器证书失败：找不到 openssl.exe（随包资源与 PATH 均无）', 'err')
        return False
    env = dict(os.environ)
    cnf = os.path.join(_ut.BASE_DIR, 'openssl.cnf')
    if os.path.isfile(cnf):
        env['OPENSSL_CONF'] = cnf
    san = ['DNS:localhost', 'IP:127.0.0.1']
    try:
        hn = socket.gethostname().strip()
        if hn:
            san.append('DNS:' + hn)
    except Exception:
        pass
    for ip in _collect_ips():
        if ip and not ip.startswith('127.'):
            san.append('IP:' + ip)
    args = [openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-days', '3650', '-keyout', key_path, '-out', cert_path,
            '-subj', '/CN=LeafFS Server',
            '-addext', 'subjectAltName=' + ','.join(san)]
    try:
        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        r = subprocess.run(args, capture_output=True, timeout=90,
                           cwd=_ut.CONFIG_DIR, env=env,
                           creationflags=creationflags)
    except Exception as e:
        add_log(f'自动生成服务器证书失败（无法执行 openssl）: {e}', 'err')
        return False
    if r.returncode != 0 or not os.path.isfile(cert_path) or not os.path.isfile(key_path):
        err = (r.stderr or r.stdout or b'').decode('utf-8', 'replace').strip()[-400:]
        add_log(f'自动生成服务器证书失败（openssl 退出码 {r.returncode}）: {err}', 'err')
        for p in (cert_path, key_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        return False
    return True


def _build_tls_context():
    """按配置构造 TLS 上下文；证书不可用返回 None（由 start_server 按“策略 A”拒绝明文启动）。

    TLS 证书来源按以下顺序（**全部只涉及“服务器证书”，绝不生成/安装 CA，绝不写入
    任何系统信任库**）：
      1a) server_config.json 显式配置 tls_cert/tls_key（相对 config 目录或绝对路径）；
      1b) 未配置（tls_cert/tls_key 均为空）但存在既有文件
          config/selfsigned.crt + config/selfsigned.key（直接加载，不重新生成）；
      1c) 前两者皆无 → 首次启动自动生成一张随机自签服务器证书
          （_auto_gen_server_cert，写入 config/selfsigned.crt + .key，此后走 1b 复用）。
    自动生成也失败 → 返回 None；显式配置的证书缺失/加载失败同样返回 None
    （不回退 legacy 文件），由调用方给出明确错误并拒绝明文启动。
    """
    import ssl
    if not _cfg.get_tls_enabled():
        return None
    cert, key = _cfg.get_tls_cert(), _cfg.get_tls_key()
    if cert and not os.path.isabs(cert):
        cert = os.path.join(_ut.CONFIG_DIR, cert)
    if key and not os.path.isabs(key):
        key = os.path.join(_ut.CONFIG_DIR, key)
    if (cert or key) and not (cert and key):
        add_log('TLS 证书配置不完整：tls_cert 与 tls_key 必须同时配置（当前只配了其一），'
                '请补全 config/server_config.json 或同时清空两项', 'err')
        return None
    if not cert and not key:
        #        服务器证书（非 CA、不装信任库；文件落 config，此后每次启动直接复用）
        legacy_cert = os.path.join(_ut.CONFIG_DIR, 'selfsigned.crt')
        legacy_key = os.path.join(_ut.CONFIG_DIR, 'selfsigned.key')
        if os.path.exists(legacy_cert) and os.path.exists(legacy_key):
            cert, key = legacy_cert, legacy_key
            add_log('使用既有自签服务器证书 config/selfsigned.crt（未安装任何信任库；'
                    '浏览器提示“不安全”属正常现象）', 'info')
        elif _auto_gen_server_cert(legacy_cert, legacy_key):
            cert, key = legacy_cert, legacy_key
            add_log('未配置证书：已自动生成随机自签服务器证书 config/selfsigned.crt + '
                    'selfsigned.key（仅本机使用、非 CA、未安装任何信任库；浏览器提示'
                    '“不安全 / 连接不是私密连接”是自签证书的正常现象，不是被劫持）', 'warn')
    if not cert or not key or not os.path.exists(cert) or not os.path.exists(key):
        add_log('TLS 已启用但无法取得可用服务器证书（未配置 tls_cert/tls_key，config 下'
                '无既有证书，且自动生成失败），拒绝明文启动（策略 A）', 'err')
        return None
    try:
        ctx = _new_server_ctx()
        ctx.load_cert_chain(cert, key)
        return ctx
    except Exception as e:
        add_log(f'TLS 证书加载失败: {e}（cert={cert}，key={key}），拒绝明文启动（策略 A）', 'err')
        return None

def run_http():
    tls_ctx = _tls_context()
    try:
        httpd = ThreadingHTTPServer(('0.0.0.0', _cfg.PORT), HTTPHandler)
    except Exception as e:
        add_log(f'HTTP 服务器启动失败 (端口 {_cfg.PORT}): {e}', 'err')
        raise
    httpd.is_secure = False
    if tls_ctx is not None:
        try:
            httpd.socket = tls_ctx.wrap_socket(httpd.socket, server_side=True)
            httpd.is_secure = True
        except Exception as e:
            add_log(f'HTTPS 包装失败: {e}', 'err')
            httpd.server_close()
            raise
    add_log(('HTTPS 服务器已启动' if httpd.is_secure else 'HTTP 服务器已启动') + f' (端口 {_cfg.PORT})', 'ok')
    httpd.serve_forever()

async def run_ws():
    set_main_loop(asyncio.get_running_loop())
    tls_ctx = _tls_context()
    try:
        ws_server = await websockets.serve(ws_handler, '0.0.0.0', _cfg.WS_PORT, ssl=tls_ctx)
    except Exception as e:
        add_log(f'WebSocket 服务器启动失败 (端口 {_cfg.WS_PORT}): {e}', 'err')
        raise
    async with ws_server:
        add_log(f'WebSocket 服务器已启动 (端口 {_cfg.WS_PORT})', 'ok')
        await asyncio.Future()

def run_ws_sync():
    """同步包装，供线程中运行 WebSocket"""
    asyncio.run(run_ws())


# ---------- 证书提示页（8082，纯 HTTP；仅 TLS 启用时提供，默认 0.0.0.0 全网监听） ----------
# 属正常现象；不提供任何证书安装/下载/接入码/技术配置内容。TLS 关闭时整页不启动。
# 下载引导，局域网暴露无 CA 投毒风险，最终用户可直接访问。
# 内联 <style>，不引外部资源），新增显著的“进入登录页”大按钮 → https 主站 /login
# （host 取请求 Host 去端口，端口取主站 http_port=_cfg.PORT）。请求携带有效 wifi_session
# （任意角色，get_session + client_address[0] IP 绑定校验）时 302 到 https 主站 /browse/，
# 无会话/无效会话照常渲染提示页。跳转目标恒为 https 主站，本页只处理 '/' 且不回跳自身 → 无循环。
_CERT_REMIND_TITLE = '文件服务器 HTTPS 证书提示'
_CERT_REMIND_ITEMS = (
    '浏览器提示“不安全 / 连接不是私密连接”是正常的——这台服务器用的是自签证书（没有找证书机构盖章）。',
    '只有当你确认这就是你自己的/信任的服务器时，才点浏览器的“高级 → 继续访问”。',
    '不要在陌生网站或来路不明的页面安装任何证书。',
    '想彻底消除提示：找这台服务器的管理员配置正式 HTTPS 证书后重启。',
)
# 英文摘要（AI 翻译，可能与中文存在差异），供英文使用者阅读
_CERT_REMIND_EN_SUMMARY = (
    'English (AI-translated): The "not secure" warning is expected because this server uses a '
    'self-signed certificate. Only continue when you are sure this is your own or a trusted server. '
    'Never install certificates from unknown pages. To remove the warning, ask the server '
    'administrator to configure an official HTTPS certificate.'
)

# 提示页内联样式（独立于静态目录，不引外部资源；观感对齐主站浅色卡片风，品牌主色 #0ea5e9）
_CERT_REMIND_CSS = '''
*{margin:0;padding:0;box-sizing:border-box}
html,body{min-height:100%}
body{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",Roboto,system-ui,sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center;
  padding:24px 16px;color:#22305c;line-height:1.7;-webkit-font-smoothing:antialiased;
  background:#e3ebf7;
  background-image:
    radial-gradient(760px 520px at 12% -10%,rgba(125,190,255,.5),transparent 62%),
    radial-gradient(700px 480px at 105% 8%,rgba(150,220,255,.42),transparent 60%),
    radial-gradient(620px 420px at 90% 108%,rgba(125,170,255,.35),transparent 62%);
  background-attachment:fixed;
}
.card{
  width:100%;max-width:620px;margin:auto;
  background:rgba(255,255,255,.9);
  border:1px solid rgba(130,155,220,.28);
  border-radius:22px;padding:40px 30px 34px;text-align:center;
  box-shadow:0 12px 34px rgba(96,120,210,.16),0 2px 8px rgba(130,150,220,.08);
}
.badge{
  width:66px;height:66px;margin:0 auto 18px;border-radius:50%;
  background:linear-gradient(135deg,#0ea5e9,#7dd3fc);
  display:flex;align-items:center;justify-content:center;font-size:30px;
  box-shadow:0 10px 24px rgba(14,165,233,.35);
}
h1{font-size:22px;font-weight:700;color:#1f2f63;margin-bottom:8px;letter-spacing:.3px}
.lead{font-size:14px;color:#5a6a99;margin:0 auto 20px;max-width:460px}
ol{list-style:none;counter-reset:item;margin:0 0 26px;padding:0;text-align:left}
ol li{
  counter-increment:item;position:relative;
  padding:12px 14px 12px 48px;margin:0 0 10px;
  background:rgba(14,165,233,.07);
  border:1px solid rgba(14,165,233,.16);
  border-radius:14px;font-size:14.5px;color:#2b3a63;
}
ol li::before{
  content:counter(item);position:absolute;left:12px;top:50%;transform:translateY(-50%);
  width:26px;height:26px;border-radius:50%;
  background:linear-gradient(135deg,#0ea5e9,#38bdf8);color:#fff;
  font-size:13px;font-weight:700;
  display:flex;align-items:center;justify-content:center;
}
.login-btn{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  text-decoration:none;color:#fff;font-size:16px;font-weight:600;
  background:linear-gradient(90deg,#0ea5e9,#38bdf8);
  border-radius:999px;padding:13px 38px;
  box-shadow:0 10px 26px rgba(14,165,233,.35);
  transition:transform .18s ease,box-shadow .18s ease,filter .18s ease;
}
.login-btn .arr{font-size:19px;line-height:1;transition:transform .18s ease}
.login-btn:hover{transform:translateY(-2px);box-shadow:0 14px 32px rgba(14,165,233,.45);filter:brightness(1.05)}
.login-btn:hover .arr{transform:translateX(3px)}
.login-btn:active{transform:scale(.97)}
@media(max-width:480px){
  .card{padding:28px 18px 24px}
  h1{font-size:19px}
  .login-btn{width:100%;max-width:320px}
}
'''

def _trust_http_bind_host():
    """证书提示页监听地址：默认返回 0.0.0.0（与主服务一致），使局域网/最终用户可达。

    配置键 trust_bind_host 可覆盖：显式给出具体地址（如 127.0.0.1 或某局域网 IP）时按其绑定；
    等价于“全网监听/未配置”的值（空 / 0.0.0.0 / :: / [::] / any）与明显非法的值
    一律安全回退 0.0.0.0。页面为纯信息提示（无证书安装/下载引导），全网监听无 CA 投毒风险。
    """
    import ipaddress
    host = ''
    try:
        host = str(_cfg.get_trust_bind_host() or '').strip().lower()
    except Exception:
        host = ''
    if host in ('', '0.0.0.0', '::', '[::]', 'any'):
        return '0.0.0.0'
    try:
        ipaddress.ip_address(host)
    except ValueError:
        # 明显非法值（非合法 IP 字面量）→ 安全回退全网监听
        return '0.0.0.0'
    return host


class _CertRemindHandler(BaseHTTPRequestHandler):
    """证书提示页处理器：仅响应根路径 '/'。

    无有效会话 → 渲染品牌化提示页（纯大白话 + 内联样式 + “进入登录页”大按钮，
    指向 https 主站 /login）；请求携带有效 wifi_session（任意角色，含来源 IP 绑定
    校验）→ 302 到 https 主站 /browse/（免重复登录）；其余路径一律 404。
    跳转目标恒为 https 主站（scheme= https、host= 请求 Host 去端口、port= 主站
    http_port），本页永不回跳自身 → 无重定向循环。无脚本、无外链、无安装/下载内容。
    """
    protocol_version = 'HTTP/1.0'
    # 收尾：与主服务一致，Server 响应头不暴露 Python/组件版本
    server_version = 'LeafFS'
    sys_version = ''
    def version_string(self):
        return self.server_version

    def log_message(self, fmt, *args):
        pass  # 提示页不写访问日志

    def _main_https_url(self, path, host_fallback=''):
        """拼主站 https URL：https://<host>:<http_port><path>。

        host 取请求 Host 头去端口后的主机（缺失时可用 host_fallback 兜底）；
        端口取主站 http_port（_cfg.PORT，load_config 后的运行值；8082 为明文 HTTP，
        本页仅在 tls_enabled=true 时提供，故目标恒为 https）。Host 缺失/端口非法 →
        返回 ''（由调用方决定：跳转分支跳过、按钮分支兜底 localhost）。
        """
        host = _strip_host_port(self.headers.get('Host', '')) or host_fallback
        if not host:
            return ''
        try:
            port = int(_cfg.PORT)
            if not 1 <= port <= 65535:
                return ''
        except Exception:
            return ''
        if ':' in host and not host.startswith('['):
            host = '[' + host + ']'  # 裸 IPv6 地址补方括号
        return f'https://{host}:{port}{path}'

    def _has_valid_session(self):
        """读取请求 Cookie 并判断“本浏览器已登录”。

        两级判定：
        1) 真会话：请求携带有效 wifi_session（ac_core.get_session，含来源 IP 绑定）。
        2) 登录标记：HTTPS 下发的会话 Cookie 带 Secure，浏览器不会经明文 http
           （8082）发回；故登录/登出会同步维护一个非 Secure 的 LOGIN_MARKER 标记，
           标记存在即视为“本浏览器已登录”（跳转后由 https 主站做真实验证，
           标记失效时主站会自行引导回登录页，不会造成死循环）。
        异常一律按未登录处理。
        """
        try:
            role, _sid = _ac.get_session(self.headers.get('Cookie', ''), True, self.client_address[0])
            if role:
                return True
        except Exception:
            pass
        try:
            raw = self.headers.get('Cookie', '') or ''
            for part in raw.split(';'):
                part = part.strip()
                if not part:
                    continue
                k, _, v = part.partition('=')
                if k.strip() == _ac.LOGIN_MARKER and v.strip() == '1':
                    return True
        except Exception:
            pass
        return False

    def _redirect_https(self, location):
        """302 跳转 https 主站（含与页面同款安全响应头）。"""
        self.send_response(302)
        self.send_header('Location', location)
        self.send_header('Content-Length', '0')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()

    def _page_bytes(self):
        # 按钮目标：https://<host>:<http_port>/login；Host 缺失等极端情形兜底 localhost
        login_url = self._main_https_url('/login', host_fallback='localhost')
        items = ''.join(f'<li>{t}</li>' for t in _CERT_REMIND_ITEMS)
        return (
            '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="robots" content="noindex">\n'
            f'<title>{_CERT_REMIND_TITLE}</title>\n'
            f'<style>\n{_CERT_REMIND_CSS}\n</style>\n'
            '</head>\n<body>\n'
            '<div class="card">\n'
            '<div class="badge" aria-hidden="true">🛡️</div>\n'
            '<h1>先看这里，再进入文件服务</h1>\n'
            '<p class="lead">浏览器出现“不安全”提示是正常的——读完下面这 4 点，你就知道该怎么做了。</p>\n'
            f'<ol>\n{items}\n</ol>\n'
            f'<p class="lead" style="font-size:12px;opacity:.8;margin-top:-6px">{_CERT_REMIND_EN_SUMMARY}</p>\n'
            f'<a class="login-btn" href="{login_url}">进入登录页<span class="arr" aria-hidden="true">→</span></a>\n'
            '</div>\n'
            '</body>\n</html>\n'
        ).encode('utf-8')

    def _respond(self, status, ctype, body):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        # A-10：与主服务同款安全响应头（nosniff / XFO DENY / Referrer）
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        if self.command != 'HEAD':
            try:
                self.wfile.write(body)
            except OSError:
                pass

    def do_GET(self):
        if urllib.parse.urlparse(self.path).path != '/':
            self._respond(404, 'text/plain; charset=utf-8', b'404 Not Found')
            return
        # 已登录用户（任意角色）→ 302 https 主站 /browse/。防循环：跳转目标恒为
        # https 主站；本页（8082）只服务 '/'，且无法拼出主站地址时（Host 缺失/端口
        # 非法，返回 ''）即使有会话也改为正常渲染，绝不回跳自身。
        browse_url = self._main_https_url('/browse/')
        if browse_url and self._has_valid_session():
            self._redirect_https(browse_url)
            return
        self._respond(200, 'text/html; charset=utf-8', self._page_bytes())

    do_HEAD = do_GET


def run_cert_remind_http():
    """证书提示页线程入口（纯 HTTP）。仅由 start_server 在 tls_enabled=true 时启动；
    绑定失败只告警不影响主服务。"""
    host = _trust_http_bind_host()
    try:
        port = int(_cfg.get_tls_trust_port())
    except Exception:
        port = 8082
    try:
        httpd = ThreadingHTTPServer((host, port), _CertRemindHandler)
    except Exception as e:
        add_log(f'证书提示页启动失败（http://{host}:{port}）: {e}（不影响主服务）', 'warn')
        return
    add_log(f'证书提示页已启动: http://{host}:{port}', 'ok')
    try:
        httpd.serve_forever()
    except Exception:
        pass
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass


def start_server():
    setup_logging()
    _cfg.load_config()
    _ac.load_users()
    _ac.start_session_cleanup()
    _fs.cleanup_orphan_thumbs()
    os.makedirs(_fs.UPLOAD_DIR, exist_ok=True)
    os.makedirs(os.path.join(_fs.UPLOAD_DIR, 'public'), exist_ok=True)
    add_log('服务器初始化完成', 'ok')
    add_log(f'共享目录: {_fs.UPLOAD_DIR}', 'info')
    add_log(f'ffmpeg: {"已安装" if _fs.has_ffmpeg() else "未安装"}', 'info')
    from leaffs.downloader_core.dl_utils import has_aria2c
    add_log(f'aria2c: {"已安装" if has_aria2c() else "未安装"}', 'info')
    _set_broadcast_fn(_broadcast_download_update)
    # A-12/D4：启动即生成“本机自动登录一次性令牌”（HTTP /login?leaf= 与 WS auth token 共用）。
    # 每次启动新令牌、旧文件覆盖（登录用；与证书机制无关）。
    global _LOCAL_TOKEN
    _LOCAL_TOKEN = secrets.token_urlsafe(24)
    _write_local_token_file(_LOCAL_TOKEN)
    # TLS 启用时：先单线程预热 TLS 上下文（见 _build_tls_context）。证书来源按序：
    #   a) server_config.json 显式配置 tls_cert/tls_key（相对 config 或绝对路径）；
    #   b) 既有 config/selfsigned.crt + selfsigned.key（直接加载）；
    #   c) 两者皆无 → 首次启动自动生成一张随机自签服务器证书（非 CA、不装信任库）。
    # 全部不可用则按安全策略 A 拒绝以明文方式提供服务（宁可不启动）。
    if _cfg.get_tls_enabled():
        try:
            ctx = _tls_context()
        except Exception:
            ctx = None
        if ctx is None:
            msg = ('自动生成服务器证书失败（或显式配置的证书不可用）：请在 server_config.json '
                   '配置 tls_cert/tls_key（可自行用 openssl 等签发），或将 tls_enabled 设为 false。\n'
                   '（配置文件位于 config/server_config.json）按安全策略拒绝以明文方式启动。')
            print(msg)
            add_log('无法取得可用服务器证书（未配置/自动生成失败）：请配置 tls_cert/tls_key '
                    '或将 tls_enabled 设为 false；按安全策略拒绝明文启动（策略 A）', 'err')
            raise SystemExit(1)
    # 证书提示页（纯 HTTP、默认 0.0.0.0 全网监听）：仅 TLS 启用时启动（TLS 关闭时整页不提供）
    if _cfg.get_tls_enabled():
        threading.Thread(target=run_cert_remind_http, daemon=True).start()
    # 启动 HTTP 服务器线程
    t = threading.Thread(target=run_http, daemon=True)
    t.start()
    # 启动 WebSocket 服务器线程
    ws_thread = threading.Thread(target=run_ws_sync, daemon=True)
    ws_thread.start()
    # 启动管理页实时推送线程（每秒一帧，仅在存在订阅者时构建数据）
    threading.Thread(target=_admin_push_loop, daemon=True).start()
    # 后台启动 aria2c RPC 守护进程（不再阻塞服务器启动），结束后记录日志并通知下载页
    def _start_daemon_and_notify():
        try:
            _dl_manager.start_daemon()
        except Exception:
            pass
        try:
            from leaffs.downloader_core import dl_rpc as _dlr
            st = _dlr.get_aria2c_status()
            if st == 'ready':
                add_log('aria2c 下载服务已就绪', 'ok')
            elif st == 'unavailable':
                add_log('aria2c 不可用，磁力/种子下载不可用（普通直链不受影响）', 'warn')
            elif st == 'error':
                add_log('aria2c 启动失败，请查看日志', 'err')
            else:
                add_log('aria2c 未就绪', 'warn')
            _broadcast_download_daemon_status()
        except Exception:
            pass
    threading.Thread(target=_start_daemon_and_notify, daemon=True).start()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]; s.close()
    except: ip = '127.0.0.1'
    ps = f':{_cfg.PORT}' if _cfg.PORT != 80 else ''
    h_scheme = 'https' if _cfg.get_tls_enabled() else 'http'
    w_scheme = 'wss' if _cfg.get_tls_enabled() else 'ws'
    print(f'  ───────────────────────────')
    print(f'\n  LeafFS 文件传输服务已启动')
    print(f'  {h_scheme.upper()}: {h_scheme}://{ip}{ps}')
    print(f'  WebSocket: {w_scheme}://{ip}:{_cfg.WS_PORT}')
    if _cfg.get_tls_enabled():
        # 横幅给用户可点击地址：默认全网监听（0.0.0.0）时展示主 LAN IP；显式绑定具体地址时按其展示
        remind_bind = _trust_http_bind_host()
        remind_url_host = ip if remind_bind == '0.0.0.0' else remind_bind
        print(f'  证书提示页: http://{remind_url_host}:{_cfg.get_tls_trust_port()}')
    if _LOCAL_TOKEN:
        print(f'  本机自动登录令牌（一次性）: {h_scheme}://localhost:{_cfg.PORT}/login?leaf={_LOCAL_TOKEN}')
    if not _cfg.get_tls_enabled():
        print('  [警告] 当前为明文模式（HTTP），仅限可信局域网内使用')
    print(f'  共享目录:  {_fs.UPLOAD_DIR}')
    print(f'  请使用管理员账户登录（仍为默认口令时，页面顶部会提示修改密码）\n')
    print(f'  ───────────────────────────')
    # 尝试启动桌面 WebView 窗口
    webview_ok = _start_webview_window()
    if webview_ok:
        print('  WebView 窗口已关闭，程序退出。')
    else:
        print('  已回退到浏览器，按 Ctrl+C 停止服务器')
        # 回退到浏览器后，需要保持主线程运行不让程序退出
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print('  服务器已停止。')

def _start_webview_window():
    """pywebview 原生窗口（D4：URL 带一次性令牌 ?leaf=，本机自动登录走令牌而非无条件免密）"""
    def _local_url():
        scheme = 'https' if _cfg.get_tls_enabled() else 'http'
        url = f'{scheme}://localhost:{_cfg.PORT}/login'
        if _LOCAL_TOKEN:
            url += '?leaf=' + _LOCAL_TOKEN
        return url
    try:
        # 仅供内嵌 WebView 放行本地自签证书；不装 CA；只影响本进程该控件。
        # 仅当用户未自定义时写入默认值（setdefault），不覆盖已有设置。
        os.environ.setdefault('WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS',
                              '--ignore-certificate-errors')
        import webview
        import time
        time.sleep(0.3)
        webview.create_window('LeafFS 文件传输', _local_url(),
                              width=1200, height=800, resizable=True)
        webview.start()
        return True
    except Exception as e:
        print(f'  窗口启动失败 ({e})，使用浏览器打开')
    try:
        webbrowser.open(_local_url())
    except Exception:
        pass
    return False

if __name__ == '__main__':
    start_server()
