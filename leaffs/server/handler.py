# -*- coding: utf-8 -*-
"""HTTP 传输层 —— 请求处理（原 leaffs.py 内 HTTP 区间，重构抽出）。

包含：HTTP 守卫常量（管理面门槛/无效会话白名单/时长豁免）、连接准入与每 IP 配额、
ThreadingHTTPServer（线程模型）、HTTPHandler（请求解析/路由/通用能力）、run_http 承载。
"""
import os
import json
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

import leaffs.auth.core as _ac
import leaffs.auth.login_api as _ac_auth
import leaffs.auth.local_token as _lt
import leaffs.auth.self_api as _ac_self
import leaffs.auth.users_api as _ac_user
import leaffs.config.api as _cfg_api
import leaffs.config.core as _cfg
import leaffs.dl.manager as _dl_mgr
import leaffs.files.api as _fs_api
import leaffs.files.core as _fs
import leaffs.server.push as _push
import leaffs.server.tls as _tls
import leaffs.share.mappings as _mapping
import leaffs.share.access as _sacc
import leaffs.utils.core as _ut
import leaffs.utils.log as _ut_log
import leaffs.web.render as _wm
from leaffs.dl import dl_api as _dl_api
from leaffs.runtime_log import (
    logger, LOG_FILE, setup_logging, add_log, log_exception, get_logs, clear_runtime_logs,
)
from leaffs.watchdog import _wd_start, _wd_finish, _wd_ws_tick, _wd_loop
from leaffs.server.hosts import (  # 主机名/IP 工具（过渡期别名，随 handler 抽离正名）
    strip_host_port as _strip_host_port,
    primary_lan_ip as _primary_lan_ip,
    collect_ips as _collect_ips,
    _LOCAL_HOST_NAMES,
)

# 下载管理器（进程级单例；dl_api 已在 manager 模块装配）
_dl_manager = _dl_mgr.get_manager()

MAX_API_BODY_SIZE = 1024 * 1024
# LF-31：连接关闭前"补读完剩余请求体"的上限与时限。
# 这是给正常客户端擦屁股用的（不读完就关连接，对端会收到 RST，拿不到我们刚发出去的
# 错误 JSON），**不是**给慢速攻击留的钉子 —— 时限一到就不再等。
_DRAIN_LIMIT = 4 * 1024 * 1024
_DRAIN_SECONDS = 3.0
_DRAIN_POLL = 0.05


class _CountingReader:
    """只读包装：记录**已经从 rfile 交给调用方**的字节数。

    为什么需要它：请求被拒时服务端常常没读请求体就关连接，而对端从 RST 里拿不到
    我们发出的错误 JSON（LF-31）。要"把没读的补读完"，前提是知道**到底读了没有、
    读了多少** —— 这个信息在 handler 里原本根本不存在，所以第一版只能靠
    "看当下 socket 里有没有残留"猜，而正文还在路上时就会猜错。

    ⚠️ 为什么数"交给调用方的量"是安全方向：底层是 `BufferedReader`，它会**预读**，
    所以这个数**不会大于**真正从内核读走的量 ⇒ 我们算出来的"剩余"只会**偏多**、
    不会偏少 ⇒ 最坏是多等一小会儿，而不是漏读之后照样发 RST。

    只显式实现会被用到的读取方法，其余属性（close/closed/flush/fileno…）直接转发。
    """

    def __init__(self, raw):
        self._raw = raw
        self.bytes_read = 0

    def read(self, *a):
        b = self._raw.read(*a)
        self.bytes_read += len(b)
        return b

    def readline(self, *a):
        b = self._raw.readline(*a)
        self.bytes_read += len(b)
        return b

    def read1(self, *a):
        b = self._raw.read1(*a)
        self.bytes_read += len(b)
        return b

    def readinto(self, buf):
        n = self._raw.readinto(buf)
        self.bytes_read += n or 0
        return n

    def peek(self, *a):
        return self._raw.peek(*a)      # peek 不消耗字节，不计入

    def __getattr__(self, name):
        return getattr(self._raw, name)


PREVIEW_MAX_SIZE = 10 * 1024 * 1024   # 在线预览大小上限（可由 cfg_core 动态覆盖，见 sync_all_constants）

# ---------- A-01：管理类 API 统一 admin/super_admin 门槛 ----------
_ADMIN_ONLY_POST = frozenset((
    '/api/users/add', '/api/users/delete', '/api/users/password', '/api/users/role',
    '/api/users/speed', '/api/users/quota', '/api/users/rename',
    '/api/config', '/api/config/advanced', '/api/config/deep',
    '/api/certs/reset', '/api/logs/clear',
))
_ADMIN_ONLY_GET = frozenset((
    '/api/users', '/api/config', '/api/config/advanced', '/api/config/deep',
    '/api/logs', '/api/connections',
))

# ---------- 统一拒绝口径（2026-09-15，用户拍板）----------
# 身份 / 权限类拒绝一律回 **404**：不泄露"这个端点、这个资源、这个用户是存在的，
# 只是你没权限"。做在 `send_json` / `send_error` 两个统一出口上（不是逐处改那 94 个
# 返回点 —— 逐处改等于下次新写一处又漏）。
#
# **豁免**（`send_json(..., 403, exempt=True)`）：只有下面这几处，它们不是"拒绝访问"，
# 而是**业务失败**——前端要把原因显示给用户，且原因本身不含权限语义：
#   * 登录失败 / 被锁定（`auth/login_api.py`）
#   * 原密码不正确（`auth/self_api.py`）
#   * 游客模式已关闭（`auth/login_api.py` 的 guest_login）
#   * 分享码错误 / 被锁定（`server/handler.py` 的 share_auth）
#   * 分享页的"需要输码"引导（`/p/<用户>/api` 的 `code_required`）—— 前端
#     `web_page/share/public.html` 就靠这个 403 弹输码框，改了分享页就坏
#
# **不写日志**：这个映射是全局的、无例外的（除豁免清单），排障时看代码即可；
# 逐次记一条"本来是 401/403"反而会被攻击者刷屏。
_REJECT_AS_NOT_FOUND = frozenset((401, 403))

# 实测收口：请求 Cookie 携带 wifi_session 但会话无效/过期时，/api 请求在路由前直接 401
# （不再静默降级为匿名/游客）。以下白名单保持原语义：公开探测/登录/登出/状态轮询。
# /api/qrlogin 必须豁免：扫码设备往往带着一条早先的（已失效）Cookie 来打开二维码地址，
# 若被 401 拦截将永远无法扫码登录——该接口本就匿名可调、成功时直接换发全新会话。
# /api/share/auth 同理必须豁免：访客在分享页输码，来路设备常常带着一条早先登录过、
# 现已失效的 Cookie（本机手机上尤其如此），被拦在路由前就永远输不进码——而该接口本来
# 就匿名可调（share_auth 不读会话），码对不对由它自己校验并记账。
_INVALID_SESSION_API_WHITELIST = frozenset((
    '/api/ping', '/api/auth/login', '/api/guest/login', '/api/auth/logout',
    '/api/auth/check', '/api/qrcode/status', '/api/qrlogin',
    '/api/share/auth',
    # /api/admin/auto-login 与 /login 同规则：拿 ?leaf= 一次性令牌建超管会话。
    # 浏览器里只要还留着一条过期 wifi_session，它就会被路由前 401 挡掉、令牌白白用不上
    # （表现是启动时自动登录失败）。放行不等于放开：它自己仍要求环回来源 + Host 为
    # localhost/环回 + 令牌恒定时间比对且消费即废。
    '/api/admin/auto-login',
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
                    _wd_start()
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
                _wd_finish()
                if ip:
                    _http_ip_conn_release(ip)
        finally:
            _cfg.release_thread()

    def setup(self):
        """接上 `_CountingReader`：必须在任何读取之前（请求行、请求头都在它之后读）"""
        super().setup()
        self.rfile = _CountingReader(self.rfile)

    def parse_request(self):
        """请求头读完的那一刻记下基线 —— 之后 rfile 上读到的就都是请求体了（LF-31）。

        没有这个基线就没法把"请求行 + 请求头"的字节从正文计数里摘出去。
        """
        ok = super().parse_request()
        self._body_base = getattr(self.rfile, 'bytes_read', None)
        return ok

    def finish(self):
        """连接收尾：**先 flush 响应 → 再补读完剩余正文 → 最后才关 rfile**（LF-31）。

        为什么必须补完：请求被拒时服务端往往**根本没读请求体**就发了响应并准备关连接
        （同源 403、无效会话 401、`Content-Length` 超限 413，以及各 API 在
        `rfile.read` 之前的权限/参数拒绝），而内核在关闭一个**接收缓冲仍非空**的连接时
        会回 **RST** —— 对端收到 RST 时会丢掉尚未读走的响应，于是它看到的是"网络错误"
        （WinError 10053/10054），而不是我们刚发出去的那份错误 JSON。
        ⚠️ 只"清一下当下残留"不够：对端还在发，缓冲区就一直是满的，读一下就走等于没读
        （第一版实测大 body 仍 97% 失败）。

        ⚠️ 这里**不能**直接 `super().finish()` 之后再补读：`StreamRequestHandler.finish()`
        会**关掉 rfile**，而残留正文恰恰躺在它里面（它读请求头时已把正文预读进用户态
        缓冲）。第二版就是这么写的，拿到 `ValueError: read of closed file` 被兜底吞掉，
        表现是"一个字节都没读"、连接照样 RST —— 实测日志里 `left` 一点没减。
        所以按需要的顺序自己走完这三步，步骤与标准库一致。
        """
        if not self.wfile.closed:
            try:
                self.wfile.flush()
            except socket.error:
                pass
        self.wfile.close()
        self._drain_unread_input()
        self.rfile.close()

    def _drain_unread_input(self):
        """补读完本次请求剩余的正文字节，然后才允许连接真正关闭（LF-31）。

        剩余量 = `Content-Length` −（`rfile.bytes_read` − 请求头读完时的基线）。
        `<= 0` 说明正文早已读完 ⇒ 立刻返回，**正常请求在这里零开销**。

        ⚠️ 必须**从 `rfile` 读**，不能用 `conn.recv`：底层 `BufferedReader` 读请求头时
        会把正文一起预读进**用户态缓冲**，此时内核缓冲是空的 —— 用 `select`/`recv`
        看过去"没有数据"，于是干等到时限（第一版就栽在这里：每个被拒请求白等 3 秒）。
        `rfile.read1()` 会先取用户态缓冲、不够再碰 socket，两个来源一起覆盖。

        ⚠️ 也不能用 `rfile.read(n)`：它要**读满 n** 才返回，对端不再发时同样会挂住。
        `read1()` 只返回"当前已可用的数据"，配合临时压短的 socket 超时，
        没数据时抛 `socket.timeout`，我们下一轮再看 —— 全程不阻塞线程。
        """
        conn = getattr(self, 'connection', None)
        rfile = getattr(self, 'rfile', None)
        if conn is None or rfile is None:
            return
        base = getattr(self, '_body_base', None)
        if base is None:
            return                      # 没走过 parse_request：不猜，什么都不做
        try:
            cl = int(self.headers.get('Content-Length') or 0)
        except (TypeError, ValueError):
            return
        left = min(cl - (getattr(rfile, 'bytes_read', 0) - base), _DRAIN_LIMIT)
        if left <= 0:
            return                      # 正文读完了 ⇒ 关闭就是干净的 FIN

        prev_to = None
        try:
            prev_to = conn.gettimeout()
            conn.settimeout(_DRAIN_POLL)
        except Exception:
            pass
        try:
            deadline = time.monotonic() + _DRAIN_SECONDS
            while left > 0 and time.monotonic() < deadline:
                try:
                    chunk = rfile.read1(min(65536, left))
                except (socket.timeout, TimeoutError):
                    continue            # 这一轮没数据：接着等，由 deadline 兜底
                except Exception:
                    return
                if not chunk:
                    return              # 对端已关
                left -= len(chunk)
        finally:
            try:
                if prev_to is not None:
                    conn.settimeout(prev_to)
            except Exception:
                pass

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

        仅当 来源 IP 为环回 + Host 为 localhost/环回 + query leaf 与本地令牌
        恒定时间匹配时才建立 super_admin 会话；令牌一次性，首个成功使用后立即失效。
        """
        ip = self.client_address[0]
        if ip not in ('127.0.0.1', '::1'):
            self.close_connection = True
            return False
        if _strip_host_port(self.headers.get('Host', '')) not in _LOCAL_HOST_NAMES:
            return False  # 防 rebinding/转发携带令牌重放
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        tok = q.get('leaf', [''])[0]
        if not _lt.try_consume(tok):
            return False
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
        role, sid = _ac.get_session(cookie, self.client_address[0])
        if role == 'guest' and not _cfg.get_guest_mode():
            # 游客模式已关闭：即使持有旧游客会话也视为未登录（防地址构造/残留会话访问）
            return None
        if role: return role
        return None

    def _get_username_from_session(self):
        cookie = self.headers.get('Cookie', '')
        _, sid = _ac.get_session(cookie, self.client_address[0])
        return _ac.get_session_username(sid) if sid else ''

    def _has_invalid_session_cookie(self):
        """Cookie 携带 wifi_session 但服务端解析不到有效会话（无效/过期/非本机 IP）→ True。

        解析与 `ac_core.get_session` 用的是**同一份实现**（`utils/core.parse_cookies`，
        同名 Cookie 取最后一个）—— 两边口径一旦不同，就会出现"预检放行、会话解析成匿名"
        （或反过来）这种自相矛盾的请求。有效游客会话（role=guest）视为有效不在此列；
        未带 Cookie 的纯匿名不受影响。
        """
        cookie = self.headers.get('Cookie', '')
        if not cookie:
            return False
        # "带了这个名字"与"值是不是空"要分开判：显式带空值（登出残留/伪造）按无效处理
        cookies = _ut.parse_cookies(cookie)
        if _ac.AUTH_COOKIE not in cookies:
            return False
        sid = cookies[_ac.AUTH_COOKIE]
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
        return _fs.check_path_permission_core(self._get_effective_role(), self._get_username_from_session(), path, _cfg.get_guest_mode())

    def _require_roles(self, *roles):
        """A-01：统一鉴权助手 —— 当前有效角色是否属于 roles"""
        return self._get_effective_role() in roles

    # 架构修订 R4：请求级“全局并发槽”（_acquire_global_slot / CONC_SLOT_TIMEOUT）已整体移除。
    # 总并发资源保护改在连接级 handle() 完成（cfg_core.try_acquire_thread，无等待、超限即拒），
    # do_GET/do_POST 不再获取/释放任何全局槽 —— 快速 API 与慢速流式正文在总上限内自由并发。

    def _common_security_headers(self, csp=None):
        """A-10：统一安全响应头（JSON/重定向/手动 302 等响应共用）。

        幂等：一个响应里只发一次（重复发同名头对部分头是未定义行为）。
        真正保证"谁都不会漏"的是下面覆写的 `end_headers` —— 手写响应绕过统一出口
        正是这类遗漏的根因（登录成功、缩略图、zip、裸 413 都曾漏过）。
        """
        if getattr(self, '_sec_headers_sent', False):
            return
        self._sec_headers_sent = True
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

    def send_response(self, code, message=None):
        """每个响应开始时清掉"公共头已发"标记。

        主服务没设 `protocol_version`（默认 HTTP/1.0，一个请求一条连接），实例不会复用，
        所以这次清理**眼下是冗余的**。留着是因为它是正确性前提：哪天为了 keep-alive 把协议
        升到 HTTP/1.1，同一个实例就会处理多个请求 —— 不清标记的话，从第二个请求起
        `_common_security_headers` 会以为已经发过而直接返回，等于只有每条连接的第一个请求
        带头，**而且不会有任何报错**。
        """
        self._sec_headers_sent = False
        self._cache_control_sent = False
        super().send_response(code, message)

    def send_header(self, keyword, value):
        """记下"这次响应已经有人显式声明过缓存策略"（见 `end_headers` 的兜底）。

        只加一个标记，不改任何头的发送行为。
        """
        if not getattr(self, '_cache_control_sent', False) and keyword.lower() == 'cache-control':
            self._cache_control_sent = True
        super().send_header(keyword, value)

    def end_headers(self):
        """统一出口（A-10 / LF-10）：结束头之前，把公共安全头与**默认缓存策略**补上。

        安全头：项目的统一响应口只有 `send_json` / `redirect`，而手写 `send_response(...)`
        的地方全在它们之外 —— 全仓 22 处里有 11 处漏过公共头（黑盒报告只点出了其中两处）。
        逐个去补等于下次新写一处又漏，所以在这里兜住。

        缓存策略同理，但**它不能一刀切**（JSON `no-store`、静态资源 `no-cache`、
        缩略图 `max-age=3600`），所以规则是：**默认 `no-store`，要缓存必须显式声明**。
        漏掉的那些正是最不该被缓存的几处 —— 手写响应里装着授权 Cookie（`/api/share/auth`
        成功路径）、一次性登录二维码（`/api/qrcode`）、用户文件内容（`/api/raw`、`/api/zip`）、
        以及对外开放的分享页（`/p/<用户>`）；没有缓存头就等于允许浏览器/中间缓存
        按启发式规则留存，在共享设备或代理后面就是泄露面。
        """
        if not getattr(self, '_cache_control_sent', False):
            self.send_header('Cache-Control', 'no-store')
        self._common_security_headers()
        super().end_headers()

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

    def send_json(self, data, status=200, exempt=False):
        # 统一拒绝口径（2026-09-15）：身份/权限类拒绝一律回 404 —— 不泄露"这个端点、
        # 这个资源、这个用户是存在的，只是你没权限"。豁免清单见 _REJECT_AS_NOT_FOUND。
        if not exempt and status in _REJECT_AS_NOT_FOUND:
            status = 404
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        # LF-10：JSON 一律不许缓存 —— 这些响应装的是"当前是谁 / 文件列表 / 配置值"，
        # 被缓存下来就会出现"换了账号还看到旧身份""删掉的文件还在列表里"这类怪事。
        # （静态资源与缩略图是另一回事，它们的缓存策略在各自的地方，别在这里一刀切。）
        self.send_header('Cache-Control', 'no-store')
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
        # 统一拒绝口径：401/403 → 404，并且**把自定义 message/explain 一起丢掉** ——
        # 留一句"无权限"在正文里等于把权限信息从状态码挪到正文，等于白改。
        if code in _REJECT_AS_NOT_FOUND:
            code, message, explain = 404, None, None
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
            # A-16 脱敏（敏感参数值 → ***）+ LF-12 控制字符剔除与超长截断
            # （请求行未解码，原始字节直发能把 \x1b 这类控制字节带进来）
            p = _ut_log.sanitize_log_text(_sanitize_path_for_log(p), max_len=200)
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
            log_exception('GET 请求处理', e)
            try:
                self.send_json({'error': '请求处理失败'}, 500)
            except _cfg.DISCONNECTED_EXCEPTIONS:
                pass
        else:
            if self._pending_session and not getattr(self, '_session_set', False):
                self._set_session_cookie(self._pending_session)

    def _same_origin_ok(self):
        """改状态请求的同源校验（CSRF 防线）。

        浏览器发起的跨站请求**一定**带 `Origin`（连表单 POST 也带），所以"带了 Origin
        就必须同源"就能挡住跨站 CSRF。同一站点但**不同端口**的页面也要挡 —— SameSite
        只比较 scheme+host、不含端口，明文模式下别的端口上的页面照样会带上会话 Cookie。
        两个头都没有 → 当非浏览器客户端（curl / 原生壳），放行。
        """
        origin = self.headers.get('Origin') or self.headers.get('Referer') or ''
        if not origin:
            return True
        host = (self.headers.get('Host') or '').strip().lower()
        if not host:
            return False
        try:
            u = urllib.parse.urlparse(origin)
        except Exception:
            return False
        if not u.hostname:
            return False
        o_port = u.port or (443 if u.scheme == 'https' else 80)
        h_host, _, h_port_s = host.partition(':')
        # Host 不带端口（默认端口）时只比主机名
        h_port = int(h_port_s) if h_port_s.isdigit() else o_port
        return u.hostname.lower() == h_host and o_port == h_port

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        # CSRF：所有改状态接口统一先过同源校验（原先后端零防护，只靠 SameSite=Lax 兜）
        if not self._same_origin_ok():
            self.send_json({'error': '跨站请求被拒绝'}, 403)
            return
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
            log_exception('POST 请求处理', e)
            try:
                self.send_json({'error': '请求处理失败'}, 500)
            except _cfg.DISCONNECTED_EXCEPTIONS:
                pass

    def _route_get(self, path, role):
        self.role = role  # 供 wm_page 渲染使用
        # A-01：管理面 GET 统一 admin/super_admin（注销/降权后即时 403）
        if path in _ADMIN_ONLY_GET and not self._require_roles('admin', 'super_admin'):
            self.send_json({'error': 'Forbidden'}, 403)
            return
        # 声明式路由表 GET_ROUTES（见文件底部）：有序逐项匹配，命中即交给对应处理器
        for _spec, _fn in GET_ROUTES:
            if _match_route(_spec, path):
                _fn(self, path, role)
                return
        self.send_error(404)

    # ---------- GET 各处理器（模块级函数，见文件底部 GET_ROUTES 表） ----------

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
            '/api/share/publish': lambda: self.share_publish(),
            '/api/share/unpublish': lambda: self.share_unpublish(),
            '/api/share/code': lambda: self.share_code_set(),
            '/api/share/reset': lambda: self.share_reset(),
            '/api/share/auth': lambda: self.share_auth(),
            '/api/account/password': lambda: _ac_self.account_password(self),
            '/api/account/revoke-sessions': lambda: _ac_self.account_revoke_sessions(self),
            '/api/account/lang': lambda: _ac_self.account_set_lang(self),
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
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query,
                                  keep_blank_values=True)
        if 'path' not in q:
            # LF-19：上传目标**只能写在 query string**（`?path=`）。写成 multipart 表单字段
            # （`path`/`dir`/`target`…）会被静默忽略、落到共享根，却仍回 `saved: 1` ——
            # 调用方以为传对了、文件却在别处。所以"压根没给这个参数"直接报错并说明写法。
            # 注意 `?path=`（空值）仍是**合法**语义 = 上传到共享根（前端就是这么传的，
            # 见 `web_page/home/home.html` 的 `?path=' + encodeURIComponent(item.path || '')`），
            # 所以这里必须 keep_blank_values=True 才能把"没给"与"给了空值"区分开。
            self.send_json({'error': '缺少 path 参数：上传目标要写在 query string 上'
                                     '（如 /api/upload?path=public）；'
                                     '上传到共享根请显式写 ?path='}, 400)
            return
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

    # ---------- 分享映射（虚拟映射到公共目录 public/shares/<用户>/，源文件不搬动） ----------

    def share_publish(self):
        """POST /api/share/publish {paths:[共享根内相对路径]} → {published:[{path,url}...]}

        把权限内的文件登记为虚拟映射：guest/匿名禁止；普通 user 只能映射自己
        可访问的路径（_check_path_permission 按当前会话校验）；admin 全共享树。
        文件本体不动，公共目录仅出现虚拟条目，磁盘零副本。
        """
        role = self._get_effective_role()
        if not role or role == 'guest':
            self.send_json({'error': '游客不可创建分享'}, 403)
            return
        username = self._get_username_from_session() or ''
        data = self._read_json() or {}
        paths = data.get('paths')
        if not isinstance(paths, list) or not paths:
            self.send_json({'error': 'paths 必须是非空数组'}, 400)
            return
        published = []
        failed = []
        for p in paths:
            if not isinstance(p, str) or not p.strip():
                continue
            norm = p.strip().replace('\\', '/')
            if not self._check_path_permission(norm):
                failed.append({'path': p, 'error': '无权限访问该路径'})
                continue
            vp, err = _mapping.publish(norm, username)
            if err:
                failed.append({'path': p, 'error': err})
            else:
                published.append({'path': vp, 'name': vp.rsplit('/', 1)[-1],
                                  'url': '/download/' + vp})
        if not published:
            msgs = [f.get('error', '') for f in failed] or ['没有可分享的文件']
            self.send_json({'success': False, 'saved': 0, 'errors': msgs}, 400)
            return
        self.send_json({'success': True, 'published': published,
                        'failed': failed,
                        'share_root': '/browse/public/shares'})

    def share_unpublish(self):
        """POST /api/share/unpublish {path: 虚拟路径} —— 移除自己的映射（admin 可移除任意）

        身份在服务端写死：匿名 401、游客 403；归属校验见 _mapping.remove。
        """
        role = self._get_effective_role()
        if not role:
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if role == 'guest':
            self.send_json({'error': '游客不可管理分享'}, 403)
            return
        data = self._read_json() or {}
        p = str(data.get('path') or '')
        if not p:
            self.send_json({'error': '缺少 path'}, 400)
            return
        username = self._get_username_from_session() or ''
        ok, err = _mapping.remove(p, username, is_admin=(role in ('admin', 'super_admin')))
        if not ok:
            self.send_json({'error': err or '移除失败'}, 400 if err else 500)
            return
        self.send_json({'success': True})

    def share_list(self):
        """GET /api/share[?all=1] —— 我的映射（admin 带 all=1 看全量）"""
        role = self._get_effective_role()
        if not role:
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        username = self._get_username_from_session() or ''
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        want_all = q.get('all', ['0'])[0] == '1'
        is_admin = role in ('admin', 'super_admin')
        items = _mapping.list_mappings(username, is_admin=is_admin and want_all)
        for it in items:
            it['url'] = '/download/' + it['path']
            it['removable'] = is_admin or it.get('by') == username
        self.send_json({'mappings': items})

    # ---------- 分享码 + 防爆破（规则见 share/access.py）----------

    def _share_session_actor(self):
        role = self._get_effective_role()
        if not role:
            self.send_json({'error': 'Unauthorized'}, 401)
            return None, None
        if role == 'guest':
            self.send_json({'error': '游客无权设置分享码'}, 403)
            return None, None
        return role, self._get_username_from_session() or ''

    def share_code_set(self):
        """POST /api/share/code {code: 新码 | ''} —— 设/换/清自己的分享码"""
        role, username = self._share_session_actor()
        if not role:
            return
        data = self._read_json() or {}
        code = data.get('code')
        if code is None:
            self.send_json({'error': '缺少 code（空串=清除）'}, 400)
            return
        code = str(code).strip()
        if code and not (6 <= len(code) <= 32):
            self.send_json({'error': '分享码长度需 6~32 个字符'}, 400)
            return
        ok, err = _sacc.set_code(username, code)
        if not ok:
            self.send_json({'error': err or '设置失败'}, 400)
            return
        self.send_json({'success': True, 'enabled': bool(code)})

    def share_reset(self):
        """POST /api/share/reset {username?} —— 本人重置防爆破计数（admin 可对他人）"""
        role, username = self._share_session_actor()
        if not role:
            return
        data = self._read_json() or {}
        target = username
        if data.get('username'):
            if role not in ('admin', 'super_admin'):
                self.send_json({'error': '无权重置他人分享'}, 403)
                return
            target = str(data['username']).strip()
        _sacc.clear_attempts(target)
        self.send_json({'success': True})

    def share_auth(self):
        """POST /api/share/auth {username, code} —— 访客输入分享码
        正确 → 下发 1 小时授权 Cookie；错误 → 记失败并返回锁定状态/剩余次数。
        """
        data = self._read_json() or {}
        username = str(data.get('username') or '').strip()
        code = str(data.get('code') or '').strip()
        if not _mapping.valid_username(username) or not code:
            self.send_json({'ok': False, 'error': '参数无效'}, 400)
            return
        if not _sacc.code_enabled(username):
            self.send_json({'ok': False, 'error': '该分享未设置访问码'}, 400)
            return
        ip = self.client_address[0]
        # 锁定判定：全局锁中 / 该 IP 当日已锁 → 直接拒
        blocked, reason = _sacc.access_blocked(username, ip)
        if blocked:
            self.send_json({'ok': False, 'error': 'locked', 'reason': reason}, 403,
                           exempt=True)
            return
        cname = _sacc.cookie_name(username)
        if _sacc.verify_code(username, code):
            # 成功：签发 1 小时**授权票据**（该 IP 计数清零由 on_success 处理）。
            # 票据是不透明随机值 —— 早先这里直接把"码的哈希"当 Cookie 值，而哈希能由码
            # 推算出来：离线枚举出码后自己写一个同名 Cookie 就能下载，全程不经过本接口，
            # IP 锁与全局锁都拦不到。
            ticket = _sacc.issue_ticket(username)
            hours = max(1, int(round(_sacc.param('cookie_hours'))))
            sc = ('%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax'
                  % (cname, ticket, hours * 3600))
            if self._is_secure():
                # Cookie 不隔离端口：明文模式下不带 Secure 的话，访问一次 8082 那类明文页就可能把它带走
                sc += '; Secure'
            self.send_response(200)
            self.send_header('Set-Cookie', sc)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
            _sacc.on_success(username, ip)
            return
        ev = _sacc.record_failure(username, ip)
        body = {'ok': False, 'error': 'incorrect', 'remaining': ev['remaining']}
        if ev['global_now']:
            body['error'] = 'locked'
            body['reason'] = 'global'
        elif ev['reason'] == 'ip':
            body['error'] = 'locked'
            body['reason'] = 'ip'
        self.send_json(body, 403, exempt=True)

    def share_status(self):
        """GET /api/share/status[?username=] —— 本人防爆破状态/攻击提醒（admin 可查他人）"""
        role = self._get_effective_role()
        if not role:
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        username = self._get_username_from_session() or ''
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get('username'):
            if role not in ('admin', 'super_admin'):
                self.send_json({'error': 'Forbidden'}, 403)
                return
            username = q['username'][0].strip()
        if not username:
            self.send_json({'error': '缺少用户名'}, 400)
            return
        st = _sacc.status(username)
        st['username'] = username
        self.send_json(st)

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
        self.send_json({'connections': _push.build_connections_payload()})

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
            _push.broadcast_qr_consumed(sid)
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
        role, sid = _ac.get_session(cookie, self.client_address[0])
        username = _ac.get_session_username(sid) if sid else ''
        # 游客下载任务按来源 IP 隔离：会话名保持“游客”，但任务归属键改为
        # “游客@<ip>”，列表/推送/管理接口天然按 IP 过滤，不同 IP 游客互不可见
        if role == 'guest':
            username = '游客@' + self.client_address[0]
        return username, role

    def _dl_quota_check(self, save_dir_abs, est_bytes):
        """IC-QUOTA-C：把 HTTP 层 _check_quota 包装成下载器的 quota_check 回调

        **不吞异常**：配额检查自己出错时不能回"配额够"（旧写法 except →
        (True, '')），否则检查一坏就等于没有配额。真出错让这次请求失败。
        """
        return self._check_quota(save_dir_abs, int(est_bytes or 0))

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
        # B4：与 _dl_forward_list 同一形状 —— 全局配置只有管理员能读（含安全/配额策略）
        username, role = self._dl_get_user_and_role()
        r = _dl_api.handle_get_config(user=username, role=role)
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
            log_exception('种子上传解析', e)
            self.send_json({'success': False, 'error': '种子文件解析失败'}, 500)

    def _get_user_start_path(self, username, role):
        return _fs.get_user_start_path(username, role)

import concurrent.futures
def run_http():
    tls_ctx = _tls.get_tls_context()
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


# =====================================================
# GET 声明式路由（_route_get 查表分派）
# -----------------------------------------------------
# 匹配 spec 形态：
#   ('=', p)              —— 精确
#   ('in', (p1, p2...))   —— 精确成员
#   ('prefix', p)         —— p 前缀（含等于）
#   ('or', ((kind,t),...))—— 任一子 spec 命中
# 每项 (spec, 处理器)，处理器签名 handler(h, path, role)；命中即交由其处理并返回。
# 新增 GET 路由：加一行 spec 表项 + 一个处理器函数即可。
def _match_route(spec, path):
    kind, target = spec
    if kind == '=':
        return path == target
    if kind == 'in':
        return path in target
    if kind == 'prefix':
        return path.startswith(target)
    if kind == 'or':
        return any(_match_route((k, t), path) for k, t in target)
    return False


def _g_login(h, path, role):
    if role:
        h.redirect('/browse/')
        return
    # D4：本机访问不再无条件自动登录；带有效一次性令牌（?leaf=）才建会话
    if h._local_token_login():
        return
    _ac_auth.serve_login_page(h, _ut.BASE_DIR, _fs.read_file_cached, _cfg.get_guest_mode)


def _g_admin(h, path, role):
    # 未登录 → 登录页（与 /browse、下载器页同一口径）；已登录但非管理员 → 404
    # （统一拒绝口径：不告诉对方"这个管理页存在，只是你没权限"）
    if not role:
        h.redirect('/login')
        return
    if role not in ('admin', 'super_admin'):
        h.send_error(403)
        return
    _wm.serve_admin_page(h, _ut.BASE_DIR, _fs.read_file_cached, _ac.is_default_admin_password)


def _g_admin_users(h, path, role):
    if not role:
        h.redirect('/login')
        return
    if role not in ('admin', 'super_admin'):
        h.send_error(403)
        return
    _wm.serve_admin_users_page(h, _ac.get_session, _ac.get_session_username,
                               _fs.read_file_cached, _ut.BASE_DIR)


def _g_admin_pages(h, path, role):
    if not role:
        h.redirect('/login')
        return
    if role not in ('admin', 'super_admin'):
        h.send_error(403)
        return
    pages = {'/admin/advanced': 'advanced.html', '/admin/deep': 'deep.html', '/log': 'log.html'}
    _wm.serve_file(h, os.path.join('web_page', 'management', pages[path]), 'text/html; charset=utf-8',
                   _ut.BASE_DIR, _ac.get_session, _ac.get_session_username)


def _g_me(h, path, role):
    # 用户信息页（与 浏览/预览/下载器/管理 同级）：任意已登录角色（含游客）可看
    if not role:
        h.redirect('/login')
        return
    _wm.serve_file(h, os.path.join('web_page', 'account', 'account.html'), 'text/html; charset=utf-8',
                   _ut.BASE_DIR, _ac.get_session, _ac.get_session_username)


def _g_browse_page(h, path, role):
    h._route_page(path, role)


def _g_static(h, path, role):
    _wm.serve_static(h, path, _ut.BASE_DIR, _fs.safe_path, _fs.get_mime, _fs.read_file_cached)


def _g_dl_peers_page(h, path, role):
    """/url-download/peers —— 下载器 peers 页（与数据 API 同一门槛）"""
    if not role:
        h.redirect('/login')
        return
    if not h._dl_allowed():
        h.send_error(403)
        return
    _wm.serve_file(h, os.path.join('web_page', 'downloader', 'peers.html'), 'text/html; charset=utf-8',
                   _ut.BASE_DIR, _ac.get_session, _ac.get_session_username)


def _g_downloader_page(h, path, role):
    """/url-download[/…] —— 下载器页

    页面本身不含数据，但它是下载器的入口：门槛必须与数据 API（_g_api_url_download）
    同一口径 —— 否则未登录/游客能打开界面，点下去全是 401/403。
    未登录给登录页、已登录但无资格（guest 且开关关）给 403。
    """
    if not role:
        h.redirect('/login')
        return
    if not h._dl_allowed():
        h.send_error(403)
        return
    _wm.serve_file(h, os.path.join('web_page', 'downloader', 'downloader.html'), 'text/html; charset=utf-8',
                   _ut.BASE_DIR, _ac.get_session, _ac.get_session_username)


def _g_api_auto_login(h, path, role):
    # D4：与 /login 同规则 —— 仅带有效一次性令牌（?leaf=）的本机请求才建会话
    if not h._local_token_login():
        h.redirect('/login')


def _g_auth_check(h, path, role):
    _ac_auth.auth_check(h, _ac.get_session, _ac.get_session_username,
                        _ac.is_default_admin_password, _cfg.get_guest_mode,
                        _ac._sessions, _ac._sessions_lock)


def _g_account_me(h, path, role):
    _ac_self.account_me(h)


def _g_auth_logout_405(h, path, role):
    # A-13：登出改为 POST（GET 命中 405），杜绝 Get 副作用/CSRF 登出面
    h.send_json({'error': 'Method Not Allowed：请使用 POST 调用 /api/auth/logout'}, 405)


def _g_files(h, path, role):
    _fs_api.send_files(h, _fs.list_files, _share_unlocked_check(h))


def _share_unlocked_check(h):
    """给文件列表用的分享码判断，返回 (owner) -> bool。

    规则与 _share_code_gate 同一套：没设码 = 恒解锁，分享者本人 = 解锁。
    没解锁的 owner，其分享目录在列表里完全不出现（连文件夹本身都不给），
    免得没过码的人靠文件名/大小/时间白拿信息。
    """
    def check(owner):
        try:
            return _share_code_gate(h, owner, h.headers.get('Cookie', ''))
        except Exception:
            return False
    return check


def _g_raw(h, path, role):
    # 分享虚拟映射：命中 → 按源文件内联输出（预览）；授权独立于游客模式（同下载语义）
    try:
        q = urllib.parse.urlparse(h.path).query
        rp = urllib.parse.parse_qs(q).get('path', [''])[0]
        if rp:
            real = _mapping.resolve(rp)
            if real is not None:
                owner = _share_owner_of(rp)
                if not _share_code_gate(h, owner, h.headers.get('Cookie', '')):
                    h.send_error(403)
                    return
                _share_stream_file(h, real, inline=True)
                return
    except Exception:
        pass
    _fs_api.send_raw(h, _fs.UPLOAD_DIR, h._preview_max_size(), _fs.safe_path,
                     _fs.get_mime, _cfg.DISCONNECTED_EXCEPTIONS)


def _g_thumb(h, path, role):
    # 分享虚拟路径 public/shares/<用户名>/<文件名> 在磁盘上不存在（源文件原位不动），
    # 要由 send_thumbnail 解析成源文件再取图，否则访客页里这些文件一律 404。
    # 命中映射时先过分享码闸门：缩略图本身就是内容，不该绕过码拿到（口径与 _g_download 一致）。
    rel = ''
    try:
        rel = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query).get('path', [''])[0]
    except Exception:
        rel = ''
    if rel:
        try:
            owner = _share_owner_of(rel)
            if owner and _mapping.resolve(rel) and \
                    not _share_code_gate(h, owner, h.headers.get('Cookie', '')):
                h.send_json({'error': '需要分享码'}, 403)
                return
        except Exception:
            pass
    _fs_api.send_thumbnail(h, _fs.UPLOAD_DIR, _fs.get_thumbnail, _fs.safe_path,
                           _cfg.DISCONNECTED_EXCEPTIONS, _mapping.resolve)


def _g_download(h, path, role):
    # 分享虚拟映射：命中 → 按源文件附件下载。
    # 授权语义：映射 = 用户主动发布（白名单精确解析，无路径输入面）；该授权独立于
    # 游客模式开关 —— 游客模式只控制“匿名浏览 public 列表”，不回收已发布文件的直链。
    try:
        rel = path[len('/download/'):]
        rel = urllib.parse.unquote(rel)
        real = _mapping.resolve(rel) if rel else None
    except Exception:
        real = None
    if real is not None:
        owner = _share_owner_of(rel)
        if not _share_code_gate(h, owner, h.headers.get('Cookie', '')):
            # 开了分享码：直链不能直接 403（那只是一个错误页，访客没有输码的地方），
            # 把人带到分享页去 —— 输完码就能看到文件并下载（redirect 内部会做 URL 编码）
            h.redirect('/p/' + owner)
            return
        _share_stream_file(h, real, inline=False)
        return
    _fs_api.handle_download(h, _fs.UPLOAD_DIR, _cfg.COPY_BUFFER_SIZE, _fs.safe_path,
                            _fs.get_mime, _ac.get_session, _ac.get_session_username,
                            _ac.get_user_speed_limit, _cfg.get_speed_limit,
                            _cfg.get_user_limiter, _cfg.DISCONNECTED_EXCEPTIONS)


# ---------- 分享虚拟映射流输出（映射命中后的源文件下发；附件下载 / 内联预览） ----------

def _share_stream_file(h, abs_path, inline=False):
    """按映射登记解析出的源文件绝对路径流式输出。

    inline=False → 附件下载（访客点链接即下载）；inline=True → 内联预览（raw，
    受 PREVIEW_MAX_SIZE 限制）。可内联文档类型（HTML/SVG/XML/JS/JSON…）一律强制
    附件/纯文本，防同源脚本执行；响应带 sandbox CSP 双保险。
    """
    import mimetypes
    name = os.path.basename(abs_path)
    mime = (mimetypes.guess_type(name)[0] or 'application/octet-stream').lower()
    mime_base = mime.split(';', 1)[0].strip()
    force_plain = mime_base in (
        'text/html', 'application/xhtml+xml', 'image/svg+xml',
        'application/xml', 'text/xml',
        'application/javascript', 'text/javascript',
        'application/json',
        'application/rss+xml', 'application/atom+xml',
    )
    if force_plain:
        mime = 'text/plain; charset=utf-8'
    try:
        size = os.path.getsize(abs_path)
    except Exception:
        h.send_error(404)
        return
    if inline and size > PREVIEW_MAX_SIZE:
        h.send_response(413)
        h.end_headers()
        try:
            h.wfile.write('[文件过大]'.encode('utf-8'))
        except Exception:
            pass
        return
    if force_plain and inline:
        # 可内联文档在预览模式也强制附件下载（不渲染），与 send_raw 同语义
        inline = False
    h.send_response(200)
    h.send_header('Content-Type', mime)
    h.send_header('Content-Length', str(size))
    if inline:
        h.send_header('Content-Disposition', 'inline')
    else:
        h.send_header('Content-Disposition',
                      "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
    # 公共头走统一入口（这处特有的 sandbox CSP 一并交给它）——
    # 手写那几个头会跟 end_headers 的自动补撞车，发出 `nosniff, nosniff` 这种重复头
    h._common_security_headers(
        csp="sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:")
    h.end_headers()
    try:
        with open(abs_path, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                _fs_api._stream_write(h, chunk)
    except _cfg.DISCONNECTED_EXCEPTIONS:
        try:
            h.close_connection = True
        except Exception:
            pass
    except Exception:
        # 响应头已发出（部分正文可能已写）：按断连处理，不再补 500
        try:
            h.close_connection = True
        except Exception:
            pass


def _g_share_page(h, path, role):
    """/share —— 分享管理页（登录用户使用；游客/未登录不可进）"""
    if not role:
        h.redirect('/login')
        return
    if role == 'guest':
        h.send_error(403)
        return
    _wm.serve_file(h, os.path.join('web_page', 'share', 'manage.html'),
                   'text/html; charset=utf-8', _ut.BASE_DIR,
                   _ac.get_session, _ac.get_session_username)


def _g_p_share(h, path, role):
    """/p/<用户名>[/api] —— 访客分享展示页（公开，无需登录，无管理元素）

    展示该用户名下已发布（存在源文件）的映射；直链下载授权独立于游客模式。
    """
    rest = path[len('/p/'):]
    parts = rest.split('/')
    if not parts or not parts[0]:
        h.send_error(404)
        return
    try:
        uname = urllib.parse.unquote(parts[0])
    except Exception:
        uname = parts[0]
    if not _mapping.valid_username(uname):
        h.send_error(404)
        return
    if len(parts) == 2 and parts[1] == 'api':
        if not _share_code_gate(h, uname, h.headers.get('Cookie', '')):
            h.send_json({'ok': False, 'error': 'code_required', 'by': uname}, 403,
                        exempt=True)
            return
        h.send_json({'by': uname, 'files': _mapping.list_public(uname)})
        return
    if len(parts) in (1, 2) and (len(parts) == 1 or parts[1] == ''):
        page = os.path.join(_ut.BASE_DIR, 'web_page', 'share', 'public.html')
        try:
            with open(page, 'rb') as f:
                body = f.read()
        except Exception:
            h.send_error(500)
            return
        h.send_response(200)
        h.send_header('Content-Type', 'text/html; charset=utf-8')
        h.send_header('Content-Length', str(len(body)))
        # 公共头由 h.end_headers 统一补，不手写
        h.end_headers()
        try:
            h.wfile.write(body)
        except _cfg.DISCONNECTED_EXCEPTIONS:
            pass
        return
    h.send_error(404)


def _g_search(h, path, role):
    _fs_api.search_files(h, _fs.UPLOAD_DIR)


def _g_stats(h, path, role):
    _fs_api.server_stats(h, _fs.get_server_stats, _fs.get_folder_size, _fs.has_ffmpeg,
                         _fs.thumbnail_backend,
                         _cfg.get_max_concurrent, _cfg.COPY_BUFFER_SIZE, _cfg.get_speed_limit,
                         _cfg.get_connections, _cfg.PORT, _cfg.get_guest_mode,
                         _cfg.get_default_user_quota, _cfg.get_public_quota,
                         _cfg.get_total_quota, _fs.UPLOAD_DIR)


def _g_config(h, path, role):
    _cfg_api.get_config(h, _cfg.get_max_concurrent, _cfg.get_speed_limit, _cfg.get_guest_mode,
                        _cfg.get_default_user_quota, _cfg.get_public_quota, _cfg.get_total_quota)


def _g_config_advanced(h, path, role):
    _cfg_api.get_config_advanced(h, _cfg.COPY_BUFFER_SIZE, MAX_API_BODY_SIZE,
                                 h._preview_max_size(), _cfg.get_upload_max_size)


def _g_config_deep(h, path, role):
    _cfg_api.get_config_deep(h, _cfg.get_deep_config_dict)


def _g_connections(h, path, role):
    h.show_connections()


def _g_qrcode(h, path, role):
    h.serve_qrcode()


def _g_qrcode_status(h, path, role):
    h.qrcode_status()


def _g_qrlogin(h, path, role):
    h.qr_login()


def _g_ping(h, path, role):
    # 公开轻量探测（供页面/网络探活使用）
    h.send_json({'ok': True})


def _g_share_list(h, path, role):
    h.share_list()


def _g_share_status(h, path, role):
    h.share_status()


def _share_owner_of(rel):
    """虚拟路径 public/shares/<用户名>/<文件名> → 用户名；非映射路径 None

    反斜杠要先归一成斜杠：`mappings.resolve()` 是归一化之后再查表的，所以
    `public\\shares\\<用户>\\<文件>` 这种写法**能命中映射**；这里若不归一，切不出
    parts[0]=='public'，返回 None —— 而 `_share_code_gate` 首句就是"owner 为空即放行"，
    等于把设了分享码的文件直接敞开（无需任何 Cookie）。
    """
    parts = (rel or '').replace('\\', '/').strip('/').split('/')
    if len(parts) >= 3 and parts[0] == 'public' and parts[1] == 'shares':
        return parts[2]
    return None


def _share_code_gate(h, owner, cookie_header):
    """分享码校验（下载/预览/访客数据）：未设码放行；设码时需 1h 授权 Cookie 或本人会话

    判定逻辑**只有一份**（`share/access.py` 的 `code_gate`）—— WS 的列表也调它。
    这里只负责把 handler 上的两样东西取出来：当前会话用户名、来源 IP。
    """
    try:
        uname = h._get_username_from_session() or ''
    except Exception:
        uname = ''
    return _sacc.code_gate(owner, cookie_header, h.client_address[0], uname)


def _g_users(h, path, role):
    _ac_user.users_list(h)


def _g_logs(h, path, role):
    _ut_log.serve_logs(h, get_logs)


def _g_zip(h, path, role):
    def _resolve(rel):
        """zip 用的分享解析器：虚拟路径 → 源文件绝对路径；分享码没解锁就抛（按无权算）。

        与 `/download` 同口径（先映射后权限），但**带门禁**：zip 不像直链那样能把人
        带到分享页去输码，所以没解锁就直接算无权（最终统一回 404）。
        """
        real = _mapping.resolve(rel)
        if not real:
            return None
        owner = _share_owner_of(rel)
        if not _share_code_gate(h, owner, h.headers.get('Cookie', '')):
            raise PermissionError('need share code')
        return real
    _fs_api.zip_download(h, _fs.UPLOAD_DIR, _cfg.COPY_BUFFER_SIZE, _fs.safe_path,
                         _cfg.DISCONNECTED_EXCEPTIONS, _resolve)


def _g_api_url_download(h, path, role):
    # A-03：下载器门槛（user/admin/super_admin；guest 依 downloader_guest_allowed）
    if not h._dl_allowed():
        if not h._get_effective_role():
            h.send_json({'error': 'Unauthorized'}, 401)
        else:
            h.send_json({'error': '游客不可使用下载器'}, 403)
        return
    h._route_dl_get(path)


# 有序匹配：页面路由在前、API 在后；'or' 处理混合条件；命中即处理并返回
GET_ROUTES = (
    (('=', '/login'), _g_login),
    (('=', '/admin'), _g_admin),
    (('=', '/admin/users'), _g_admin_users),
    (('in', ('/admin/advanced', '/admin/deep', '/log')), _g_admin_pages),
    (('=', '/me'), _g_me),
    (('=', '/share'), _g_share_page),
    (('prefix', '/p/'), _g_p_share),
    (('or', (('=', '/'), ('prefix', '/browse/'), ('=', '/gallery'))), _g_browse_page),
    (('prefix', '/static/'), _g_static),
    (('=', '/url-download/peers'), _g_dl_peers_page),
    (('or', (('=', '/url-download'), ('prefix', '/url-download/'))), _g_downloader_page),
    (('=', '/api/admin/auto-login'), _g_api_auto_login),
    (('=', '/api/auth/check'), _g_auth_check),
    (('=', '/api/account/me'), _g_account_me),
    (('=', '/api/auth/logout'), _g_auth_logout_405),
    (('=', '/api/files'), _g_files),
    (('=', '/api/raw'), _g_raw),
    (('=', '/api/thumb'), _g_thumb),
    (('prefix', '/download/'), _g_download),
    (('=', '/api/search'), _g_search),
    (('=', '/api/stats'), _g_stats),
    (('=', '/api/config'), _g_config),
    (('=', '/api/config/advanced'), _g_config_advanced),
    (('=', '/api/config/deep'), _g_config_deep),
    (('=', '/api/connections'), _g_connections),
    (('=', '/api/qrcode'), _g_qrcode),
    (('=', '/api/qrcode/status'), _g_qrcode_status),
    (('=', '/api/qrlogin'), _g_qrlogin),
    (('=', '/api/ping'), _g_ping),
    (('=', '/api/share'), _g_share_list),
    (('=', '/api/share/status'), _g_share_status),
    (('=', '/api/users'), _g_users),
    (('=', '/api/logs'), _g_logs),
    (('=', '/api/zip'), _g_zip),
    (('prefix', '/api/url-download/'), _g_api_url_download),
)
