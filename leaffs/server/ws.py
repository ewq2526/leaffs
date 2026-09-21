# -*- coding: utf-8 -*-
"""WebSocket 传输层 —— 连接注册/限流/消息分发（原 leaffs.py 内 ws 区间，重构抽出）。

包含：WS 连接登记与每 IP 配额（registry）、消息级/IP 级限流、Origin/Host 同源校验、
会话解析（_ws_get_session）、ws_handler 消息分发（admin-sub/list/delete/mkdir/
sub-download/qr/ping）、事件循环承载（run_ws/run_ws_sync）。

订阅集合与广播在 leaffs.server.push。
（本机一次性令牌只走 HTTP 的 `/login?leaf=`；**WS 的 `auth` 消息分支已于 2026-09-16 删除**
—— 全仓无发送方，是死代码，而它还是"环回但漏查 Host"的那条路。）
"""
import asyncio
import concurrent.futures
import json
import logging
import os
import threading
import time
import urllib.parse
from collections import deque
from http import HTTPStatus

import websockets
# 显式导入：下方 695 行的 `websockets.exceptions.ConnectionClosed` 原本靠
# `websockets/__init__.py` 里 `from .exceptions import ...` 顺带设的包属性。
# 而 except 的类型表达式只在异常真发生时求值 —— 那个属性一旦消失，坏的是
# "每次客户端断开"那一刻（1011 关连接 + 堆栈），而不是启动，最难查。
import websockets.exceptions

import leaffs.auth.core as _ac
import leaffs.config.core as _cfg
import leaffs.dl.manager as _dl_mgr
import leaffs.files.api as _fs_api
import leaffs.files.core as _fs
import leaffs.runtime_log
import leaffs.server.hosts as _hosts
import leaffs.server.push as _push
import leaffs.server.tls as _tls
# LF-27：分享码判定与 HTTP 侧**共用一份**（share/access.code_gate）——
# WS 的列表也要按分享码过滤，不能自己再写一套"谁算解锁"。
import leaffs.share.access as _sacc
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
    这里保留双来源读取，避免新版库下 Cookie 解析恒为空、
    纯 cookie 会话的 WS 连接被当成匿名（admin-sub 等一律 Unauthorized）。

    返回 **(role, username, sid, cookies_raw)** —— 第 4 项是给分享码判定用的：
    LF-27 之后 WS 的列表也要按分享码过滤，而判定必须看**握手时的 Cookie**
    （票据与 HTTP 侧同一套）；调用方负责缓存到 `websocket.cached_cookie`。
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
        role, sid = _ac.get_session(cookies_raw, ws_ip)
        username = _ac.get_session_username(sid) if sid else ''
        return role, username, sid, cookies_raw
    except Exception:
        return None, '', '', ''


def _normalize_ws_rel_path(rel_path):
    """规范化 WebSocket 中的相对路径并禁止路径穿越

    **实现只有一份**：转调 `files/api.py` 的 `_normalize_rel_path`（HTTP 侧一直用的那个）。
    这两个原本是**两份拷贝**，差异是 api 那份多拒一类 Windows 盘符（`C:/x`，
    `os.path.join(UPLOAD_DIR, 'C:/x')` 在 Windows 上会直接跳出共享根）——
    统一到这里等于**只变严不变松**。留着两份迟早再漂移（LF-27 就是这么来的）。
    """
    return _fs_api._normalize_rel_path(rel_path)


def _ws_str(data, key, default=''):
    """从 WS 消息里取**字符串**字段：非字符串一律当空值。

    为什么需要：消息循环外面只兜了 `ConnectionClosed`，其它异常会冒到 websockets
    那里、以 **1011** 关掉整条连接并刷一段堆栈日志。而 `json.loads` 之后各分支直接
    假定字段是 str —— 实测这些输入会把连接打崩：
    `"abc"`/`123`/`[1]`/`null`（非对象 JSON）、`{"token":123}`、`{"sid":123}`、
    `{"type":"list","path":123}`、`{"type":"delete","paths":123}`。

    当空之后走的是**既有的正常分支**（token 空 → 认证失败；path 空 → 路径不合法），
    客户端能拿到明确应答，比"静默忽略"好。
    """
    v = data.get(key, default)
    return v if isinstance(v, str) else default


def _ws_str_list(data, key):
    """从 WS 消息里取**字符串列表**字段：非列表当空，列表里的非字符串元素剔掉。

    原来直接 `for p in paths`：传数字 → TypeError 崩；传**字符串** → 逐字符当路径
    （不崩但语义错 —— 会拿 "a"/"b"/"c" 去逐个判权限/删除）。
    """
    v = data.get(key, [])
    if not isinstance(v, list):
        return []
    return [x for x in v if isinstance(x, str)]


async def _ws_proto_error(websocket, why):
    """帧级/信封级的格式问题 → 回一条明确错误（**不断连接**）。

    为什么不静默忽略：客户端无法区分"服务端不支持这条消息"与"服务端卡住/没理我"
    （外部测试者的反馈）。
    为什么不按 RFC 6455 回 1003/1007 关闭：那是**主动关闭连接**，等于把"连接被打断"
    换个规范的码还回来 —— 而这段代码的目标恰恰是"别因为一条错消息让客户端掉线、丢订阅"。
    应答不放大流量（1:1），且外面有每连接 30/10s、每 IP 120/10s 的滑动窗口限流兜底。
    """
    try:
        await websocket.send(json.dumps({'type': 'error', 'msg': '消息格式不合法：' + why}))
    except Exception:
        pass          # 发送失败说明连接已断，无需再管


# WS 敏感消息类型：会话被撤销/过期后这些操作必须逐条实时复查（见 ws_handler），
# 仅凭 cookie 会话的连接已在每条消息的“获取会话”段实时复查，无需重复。
# 2026-09-21：去掉 'upload' —— 与 'auth' 同样没有发送方（前端全部 WS 消息类型已逐一核对）。
_WS_REVALIDATE_TYPES = ('admin-sub', 'gallery-sub', 'list', 'delete', 'mkdir', 'sub-download')

# A-17：未认证（ws_role 为空）连接仅放行的最小消息集
# 2026-09-16：去掉 'auth' —— 那条消息分支是**死代码**（全仓无发送方），已整体删除；
# 认证只走握手 Cookie（`_ws_get_session`）。未认证连接发 auth 现在按"白名单外消息"拒。
_WS_ANON_ALLOWED = frozenset(('ping', 'qr-sub', 'qr-unsub', 'admin-unsub', 'unsub-download'))

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


def _ws_origin_host_ok(headers):
    """WS Host/Origin 同源校验（A-06）。**在握手前调用**，不过就回 HTTP 403。

    Host（如存在）剥离端口后必须落在本机地址白名单里；Origin（如存在）必须来自
    **本站主站** —— 主机名在白名单里，**且端口等于本站主站端口**。
    无 Origin 的原生客户端放行（限流兜底）。

    调用位置很重要（LF-14）：原先调用点在 `ws_handler` 里，而 `websockets` **调用 handler
    之前 101 已经发出去了** —— 不合法连接照样完成握手、进入连接计数，客户端拿到的是
    "连上又断"。现在由 `_ws_http_probe`（`process_request`，握手前）调用，拒得干净。
    """
    try:
        allowed = set(_collect_ips())
        allowed.update(_LOCAL_HOST_NAMES)
        host = ''
        origin = ''
        if headers is not None and hasattr(headers, 'get'):
            host = headers.get('Host') or ''
            origin = (headers.get('Origin') or '').strip()
        if host and _strip_host_port(host) not in allowed:
            return False
        if not origin:
            return True  # 原生客户端无 Origin：放行，限流兜底
        if origin.lower() == 'null':
            return False
        u = urllib.parse.urlparse(origin)
        if u.scheme not in ('http', 'https'):
            return False
        if _strip_host_port(u.hostname or '') not in allowed:
            return False
        # 端口也必须对得上：SameSite 只比主机名、**不比端口**，所以"本机任意其它端口"上
        # 的页面照样会带上会话 Cookie 连过来 —— 而 WebSocket 不受同源策略约束，服务端这
        # 道 Origin 校验就是唯一防线（漏了端口 = 本机任何端口的页面都能冒充当本站页面）。
        # 只认本站主站端口；_cfg.PORT 是动态读的，用户改端口也跟得上。
        try:
            want_port = int(_cfg.PORT)
        except Exception:
            return False
        # 不带端口时按 scheme 默认端口算；端口非法（如 :99999）会让 u.port 抛 ValueError，
        # 由外层 except 兜成拒绝
        got_port = u.port if u.port is not None else (443 if u.scheme == 'https' else 80)
        return got_port == want_port
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
        # A-06 的 Origin/Host 校验已经**上移到握手前**（见 _ws_http_probe / LF-14）：
        # 不合法的连接在那里就被 403 掉，根本走不到这里，所以此处不再重复判定
        # （那段重复判定在握手后必然成立，纯属白跑）。
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
                except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                    # 非 UTF-8 的**二进制帧**会让 `json.loads(bytes)` 抛 UnicodeDecodeError ——
                    # 原来没捕它，异常冒出消息循环、连接被以 **1011** 打掉（测试者报的
                    # `close 0x03f3` 就是它；这是上一轮形状契约修复漏掉的第 9 类输入）。
                    await _ws_proto_error(websocket, '帧内容不是合法 JSON')
                    continue
                if not isinstance(data, dict):
                    # 合法 JSON 但不是对象（"abc" / 123 / [1,2] / null）：
                    # 原来是直接 data.get(...) → AttributeError → 整条连接被以 1011 打掉。
                    await _ws_proto_error(websocket, '消息必须是 JSON 对象')
                    continue
                t = data.get('type', '')
                if not isinstance(t, str):
                    await _ws_proto_error(websocket, 'type 必须是字符串')
                    continue      # 非字符串 type 不可能匹配任何分支
                # 获取 WebSocket 会话（优先使用认证缓存，其次握手 Cookie）
                ws_role = getattr(websocket, 'cached_role', None)
                ws_user = getattr(websocket, 'cached_user', '')
                if not ws_role:
                    # 握手 Cookie 认证（同源 WS 必带 wifi_session）。顺手把 sid 缓存下来，
                    # 让 A-07 的"按用户当前角色实时复查"在纯 Cookie 路径下也生效
                    # （旧实现只在显式 auth 时缓存 sid，而显式 auth 已删）
                    ws_role, ws_user, _ws_sid, _ws_cookie = _ws_get_session(websocket)
                    if _ws_sid:
                        websocket.cached_sid = _ws_sid
                    # LF-27：分享码判定要**握手时的 Cookie**（与 HTTP 同一套票据）——
                    # 缓存到连接上供 list 分支用；取不到就是空串 = 未解锁（fail-closed）
                    websocket.cached_cookie = _ws_cookie
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
                # 会话解析那一拍把角色/sid 缓存在 websocket 对象上（`_ws_get_session` 那条路）；
                # 此处经 B 的 refresh_session_role(sid, ip) 刷新为最新角色/用户名（降权/改名/删除
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

                if t == 'gallery-sub':
                    # 预览页订阅「文件树变化」：只推一个信号、不带列表，列表由页面按自身
                    # 权限自己拉（游客只看公共目录），所以订阅本身没有数据泄漏面。
                    _push.gallery_sub_add(websocket)
                    continue
                if t == 'gallery-unsub':
                    _push.gallery_sub_remove(websocket)
                    continue

                if t == 'qr-sub':
                    # 二维码弹窗订阅“已扫码”事件（sid 为随机密钥，无需额外鉴权）
                    sid = _ws_str(data, 'sid').strip()
                    if not sid: continue
                    websocket._qr_subscribed = True
                    _push.qr_sub_add(sid, websocket)
                    continue
                if t == 'qr-unsub':
                    sid = _ws_str(data, 'sid').strip()
                    _push.qr_sub_remove(sid, websocket)
                    continue

                if t == 'list':
                    p = _ws_str(data, 'path')

                    def _ws_share_unlocked(owner):
                        """该 owner 的分享码是否已解锁 —— 与 HTTP 侧**同一套判定**。

                        **必须传真实回调**：`merge_into_list(unlocked=None)` 的语义是
                        "不过滤"，那等于把设了码的分享目录白送给任何连得上 WS 的人。
                        """
                        try:
                            return _sacc.code_gate(
                                owner,
                                getattr(websocket, 'cached_cookie', ''),
                                getattr(websocket, 'remote_address', ('', 0))[0],
                                ws_user or '')
                        except Exception:
                            return False

                    # 列表的构建只有一条路（`files/api.build_listing`）—— 与 HTTP 的
                    # `send_files` 共用：规范化 / `.uploads` 拦截 / 权限顺序 / 虚拟分享
                    # 条目注入，全在那一处，免得两边再漂移（LF-27 的两个面都是这么来的）。
                    r = await _run_io(
                        _fs_api.build_listing, p,
                        lambda q: _fs.check_path_permission_core(
                            ws_role, ws_user, q, _cfg.get_guest_mode()),
                        _fs.list_files, _ws_share_unlocked)
                    result_data, err_kind, detail = r
                    if err_kind == 'bad_path':
                        await websocket.send(json.dumps({'type': 'error', 'msg': '路径不合法'}))
                        continue
                    if err_kind == 'denied':
                        await websocket.send(json.dumps({'type': 'error', 'msg': '无权限'}))
                        continue
                    if err_kind:
                        # 'io' / 'unknown'：原来这里**回一个空列表** —— 前端会把界面清空，
                        # 看着像"这个目录里没东西了"。现在如实回错误（前端无 error 分支，
                        # 于是保持原列表不动），别用"空"冒充"读失败"。
                        await websocket.send(json.dumps(
                            {'type': 'error', 'msg': detail or '读取目录失败'}))
                        continue
                    result_data['type'] = 'list'
                    await websocket.send(json.dumps(result_data))
                elif t == 'delete':
                    paths = _ws_str_list(data, 'paths')
                    if not paths:
                        # 空路径列表 = 这个请求什么都没做，必须如实回"没成功"。
                        # 原来原样回 {'success': True, 'deleted': 0} —— 外部黑盒报告
                        # （WS-D，2026-09-21）据此把这个分支判成"死分支、报假成功"：
                        # 他们按 HTTP 那边的习惯发了 {"files": [...]}，而这里读的是
                        # paths，于是列表为空、循环不执行、回了一个漂亮的 success，
                        # 文件纹丝不动。分支本身是好的（下面权限校验、真删除都在），
                        # 错的只是"什么都没做却说成功"。
                        # 响应形态保持 'delete' 不变：字段级问题走业务分支，不叠格式错
                        # （与 _ws_str_list 的既有口径一致，见 test_ws_message_shape）。
                        await websocket.send(json.dumps(
                            {'type': 'delete', 'success': False, 'deleted': 0,
                             'msg': 'paths 为空：没有要删除的路径'}))
                        continue
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
                        # 写策略判定（files/api.py 的 ws_write_allowed，纯函数）。
                        # **不吞异常**：判定不了就等于拒绝，绝不能被读成"有权限" ——
                        # 旧写法 except → True，把内部错误直接变成放行。
                        if not _fs_api.ws_write_allowed(ws_role, ws_user, norm_p, 'delete',
                                                        _cfg.get_guest_mode()):
                            failed.append((norm_p, '无权限'))
                            continue
                        try:
                            # 文件删除含 os.walk/rmtree/缩略图清理，可能耗时数秒~数十秒，
                            # 必须在线程池执行，避免同步阻塞 WS 事件循环（卡死全服务页面）
                            cnt, fails = await _run_io(_fs.delete_paths, [norm_p])
                        except Exception as e:
                            from leaffs.runtime_log import log_exception
                            from leaffs.utils.core import delete_fail_reason
                            log_exception('WS 删除 %s' % norm_p, e)
                            cnt, fails = 0, [(norm_p, delete_fail_reason(e))]
                        n_deleted += int(cnt or 0)
                        for _pn, _why in (fails or []):
                            failed.append((_pn, _why))
                    # LF-23：与 HTTP 路径同一口径 —— 判据是"有没有失败项"，而不是"删掉几个"
                    # （删一个不存在的文件 n_deleted=0，但那是幂等成功，不是失败）
                    resp = {'type': 'delete', 'success': not failed, 'deleted': n_deleted}
                    if failed:
                        resp['failed'] = [{'path': str(a), 'error': str(b)} for a, b in failed]
                    await websocket.send(json.dumps(resp))
                    for _p, _why in failed:
                        try:
                            await websocket.send(json.dumps({'type': 'error', 'msg': f'{_why}: {_p}'}))
                        except Exception:
                            pass
                elif t == 'mkdir':
                    p, n = _ws_str(data, 'path'), _ws_str(data, 'name')
                    norm_p = _normalize_ws_rel_path(p + '/' if p else '')
                    if norm_p is None:
                        await websocket.send(json.dumps({'type': 'error', 'msg': '路径不合法'})); continue
                    p = norm_p.rstrip('/') if norm_p else ''
                    if not _fs.check_path_permission_core(ws_role, ws_user, p + '/' if p else '', _cfg.get_guest_mode()):
                        await websocket.send(json.dumps({'type': 'error', 'msg': '无权限'})); continue
                    # IC-WRITE：guest 一律禁 mkdir（R1），非 guest 由路径权限兜底
                    # （同 delete：不吞异常，"判定不了"就是拒绝）
                    if not _fs_api.ws_write_allowed(ws_role, ws_user, p, 'mkdir',
                                                    _cfg.get_guest_mode()):
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
                    # 仅凭 cookie 会话的游客连接在此兜底对齐归属键
                    # （原来这里写的是"未走 auth 消息"—— auth 分支已于 2026-09-16 删除）
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
                else:
                    # 认不出的 type 也必须回话，理由同 _ws_proto_error：客户端分不清
                    # 「服务端不支持这条消息」与「服务端卡住/没理我」（外部测试反馈）。
                    # 不回显 t —— 它是客户端输入，原样送回等于把不受控内容送进对方页面。
                    try:
                        add_log(f'WS 未知消息类型: {str(t)[:32]!r} role={ws_role or "空"}', 'warn')
                    except Exception:
                        pass
                    await websocket.send(json.dumps({'type': 'error', 'msg': '未知消息类型'}))
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
        # 文件树订阅的摘除是幂等的（没订阅过就是空操作），不必额外记标志位
        _push.gallery_sub_remove(websocket)

class _HandshakeNoiseFilter(logging.Filter):
    """滤掉 websockets 的「握手失败」噪音日志。

    websockets 把握手阶段的**任何**异常都按
    `logger.error("opening handshake failed", exc_info=True)` 打一整段堆栈
    （websockets/server.py:185）。其中有一类对我们完全是常态：
    WebView 加载 8081 时会顺带留下一条「连上就断」的连接（Gecko 自己的连接竞速/预连接），
    服务端读请求行时直接 EOF → InvalidMessage。每次冷启动都会因此刷一段 Traceback，
    看着像服务故障，其实不是。

    只屏蔽这一条消息，websockets 的其它日志照常输出（真出问题时仍能看到）。
    """
    def filter(self, record):
        return 'opening handshake failed' not in record.getMessage()


logging.getLogger('websockets.server').addFilter(_HandshakeNoiseFilter())


async def _ws_http_probe(connection, request):
    """8081 上收到「不是 WebSocket 升级」的请求时的应答。

    这种请求是有的：App 启动时为了给 8081 预热证书例外，会让 WebView 直接打开
    https://127.0.0.1:8081/ —— Gecko 发的是普通 HTTPS GET（Connection: keep-alive）；
    浏览器或探测工具也可能直接访问这个端口。

    不在这里应答的话，websockets 会在握手阶段抛 InvalidUpgrade，然后按
    `logger.error("opening handshake failed", exc_info=True)` 刷一整段堆栈
    （asyncio/server.py:365）—— 每次冷启动都来一发，看着像服务出错。
    respond() 走的是「正常拒绝握手」那条路径（库把它当预期情况，不发错误日志），
    对端拿到的是一个普通 HTTP 响应而不是 426。

    这里**只**回一行纯文本，不再渲染任何引导页：曾经为了让访客"依次放行两个端口的
    证书"而返回过一张会自己跳主站的 HTML 落地页，后来查清 8081 连不上跟证书无关
    （是服务端 WS 的 Host/Origin 白名单不认本机热点地址，见 _ws_origin_host_ok），
    那张页面已删除。
    """
    if (request.headers.get('Upgrade') or '').lower() != 'websocket':
        return connection.respond(
            HTTPStatus.OK, 'LeafFS WebSocket 端口，仅供网页内部连接\n')
    # A-06 / LF-14：Origin/Host 同源校验**在握手前**做 —— 不过就回 403，连 101 都不发。
    # （原先这道校验在 ws_handler 里，而 websockets 调用 handler 之前握手已经完成：
    #   不合法连接照样进了连接计数，客户端拿到的是"连上又断"而不是明确被拒。）
    if not _ws_origin_host_ok(request.headers):
        try:
            _ra = getattr(connection, 'remote_address', None)
            _ip = _ra[0] if _ra else '?'
        except Exception:
            _ip = '?'
        try:
            add_log(f'WS Origin/Host 校验失败，拒绝来自 {_ip} 的连接', 'warn')
        except Exception:
            pass
        # 统一拒绝口径（2026-09-15）：与其他拒绝一样回 404，不用 403 ——
        # 403 等于告诉对方"这个 WS 端点存在，只是你的 Origin/Host 不被接受"。
        return connection.respond(HTTPStatus.NOT_FOUND, 'origin/host check failed\n')
    return None


async def run_ws():
    _push.set_main_loop(asyncio.get_running_loop())
    tls_ctx = _tls.get_tls_context()
    try:
        # LF-16：Server 头与另外两台服务一致（HTTP 主站 handler.server_version、
        # 8082 cert_remind 都是 'LeafFS'）。不传的话 websockets 用库默认值
        # `Python/3.12 websockets/16.0` —— 等于把 Python 与库版本一起报出去。
        ws_server = await websockets.serve(ws_handler, '0.0.0.0', _cfg.WS_PORT,
                                          ssl=tls_ctx, process_request=_ws_http_probe,
                                          server_header='LeafFS')
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