# -*- coding: utf-8 -*-
"""WebSocket 传输层 —— 连接注册/限流/消息分发（原 leaffs.py 内 ws 区间，重构抽出）。

包含：WS 连接登记与每 IP 配额（registry）、消息级/IP 级限流、Origin/Host 同源校验、
会话解析（_ws_get_session）、ws_handler 消息分发（auth/admin-sub/list/delete/mkdir/
sub-download/qr/ping）、事件循环承载（run_ws/run_ws_sync）。

订阅集合与广播在 leaffs.server.push；本机令牌在 leaffs.auth.local_token。
"""
import asyncio
import concurrent.futures
import json
import os
import threading
import time
import urllib.parse
from collections import deque

import websockets

import leaffs.auth.core as _ac
import leaffs.auth.local_token as _lt
import leaffs.config.core as _cfg
import leaffs.dl.manager as _dl_mgr
import leaffs.files.api as _fs_api
import leaffs.files.core as _fs
import leaffs.runtime_log
import leaffs.server.hosts as _hosts
import leaffs.server.push as _push
import leaffs.server.tls as _tls
from leaffs.watchdog import _wd_ws_tick
from leaffs.runtime_log import add_log

_dl_manager = _dl_mgr.get_manager()
_strip_host_port = _hosts.strip_host_port
_LOCAL_HOST_NAMES = _hosts._LOCAL_HOST_NAMES
_collect_ips = _hosts.collect_ips

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
                _wd_ws_tick()
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
                        # A-05/D4：本机一次性令牌 —— 仅环回来源 + 与本地令牌恒定时间
                        # 比对；令牌只授权“建立”super_admin 会话，会话仍由 B 生成真实 sid
                        ws_ip = getattr(websocket, 'remote_address', ('', 0))[0]
                        if ws_ip in ('127.0.0.1', '::1') and _lt.try_consume(token):
                            try:
                                su = _ac.get_super_admin_name() or 'admin'
                            except Exception:
                                su = 'admin'
                            sid = _ac.create_session(su, 'super_admin', client_ip=ws_ip)
                            ws_role = 'super_admin'
                            ws_user = su
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
                    _push.admin_sub_add(websocket)
                    admin_sub = True
                    # 订阅后立即推送一帧，避免等待下一个推送周期
                    payload = _push.admin_snapshot_payload()
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
                    _push.admin_sub_remove(websocket)
                    admin_sub = False
                    continue

                if t == 'qr-sub':
                    # 二维码弹窗订阅“已扫码”事件（sid 为随机密钥，无需额外鉴权）
                    sid = (data.get('sid') or '').strip()
                    if not sid: continue
                    websocket._qr_subscribed = True
                    _push.qr_sub_add(sid, websocket)
                    continue
                if t == 'qr-unsub':
                    sid = (data.get('sid') or '').strip()
                    _push.qr_sub_remove(sid, websocket)
                    continue

                if t == 'list':
                    p = data.get('path', '')
                    # 规范化路径
                    norm_p = _normalize_ws_rel_path(p)
                    if norm_p is None:
                        await websocket.send(json.dumps({'type': 'error', 'msg': '路径不合法'}))
                        continue
                    p = norm_p
                    if not _fs.check_path_permission_core(ws_role, ws_user, p, _cfg.get_guest_mode()):
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
                        if not _fs.check_path_permission_core(ws_role, ws_user, norm_p, _cfg.get_guest_mode()):
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
                            # 文件删除含 os.walk/rmtree/缩略图清理，可能耗时数秒~数十秒，
                            # 必须在线程池执行，避免同步阻塞 WS 事件循环（卡死全服务页面）
                            cnt, fails = await _run_io(_fs.delete_paths, [norm_p])
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
                    if not _fs.check_path_permission_core(ws_role, ws_user, p + '/' if p else '', _cfg.get_guest_mode()):
                        await websocket.send(json.dumps({'type': 'error', 'msg': '无权限'})); continue
                    # IC-WRITE：guest 一律禁 mkdir（R1），非 guest 由路径权限兜底
                    try:
                        ws_write_ok = _fs_api.ws_write_allowed(ws_role, ws_user, p, 'mkdir',
                                                               _cfg.get_guest_mode())
                    except Exception:
                        ws_write_ok = True
                    if not ws_write_ok:
                        await websocket.send(json.dumps({'type': 'error', 'msg': '无权限'})); continue
                    ok, err = await _run_io(_fs.mkdir, p, n)
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
                    _push.dl_sub_add(websocket)
                    sub = True
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
                        from leaffs.dl import dl_rpc as _dlr
                        await websocket.send(json.dumps({'type': 'daemon_status', 'status': _dlr.get_aria2c_status()}))
                    except Exception:
                        pass
                elif t == 'unsub-download':
                    _push.dl_sub_remove(websocket)
                    sub = False
                elif t == 'ping':
                    await websocket.send(json.dumps({'type': 'pong'}))
        except websockets.exceptions.ConnectionClosed:
            pass
    finally:
        # 结束清理：任何路径（正常结束/异常/被限流 close/接受后立即被拒）都按进入登记时的
        # 同一连接对象摘除，保证只减一次且不漏减；已被配额淘汰的连接此处为幂等空操作。
        _ws_conn_leave(ip, websocket)
        if sub:
            _push.dl_sub_remove(websocket)
        if admin_sub:
            _push.admin_sub_remove(websocket)
        if getattr(websocket, '_qr_subscribed', False):
            _push.qr_cleanup_ws(websocket)

async def run_ws():
    _push.set_main_loop(asyncio.get_running_loop())
    tls_ctx = _tls.get_tls_context()
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