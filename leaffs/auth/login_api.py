"""认证 API — 登录/登出/检查/游客/模式切换"""

import json
import os
import time
import logging
import threading

from leaffs.utils import log as _ut_log   # B-09/B-13：日志脱敏与安全事件

logger = logging.getLogger('wifi_convey')

# ========== 登录防爆破 ==========
_LOGIN_LOCK_MIN = 10      # 冷却窗口（分钟）
_LOGIN_MAX_FAIL = 6       # 窗口内允许的最大失败次数
_login_fail = {}
_login_fail_lock = threading.Lock()
_login_last_clean = 0.0   # 上次清理过期登录失败记录的时间

# B-09 账户级锁定：独立于 (IP,用户名) 的分布式爆破收敛（15 分钟 ≥ 12 次 → 该用户名全局锁定）
_ACCOUNT_LOCK_MIN = 15        # 账户级锁定窗口（分钟）
_ACCOUNT_LOCK_AFTER = 12      # 窗口内失败次数达到即锁定该用户名
_acct_fail = {}               # username -> {'n': 次数, 't': 最近失败时间}

# B-07 游客登录限速：单 IP 每分钟 ≤ _GUEST_LOGIN_MAX 次 guest_login
_GUEST_LOGIN_WINDOW = 60      # 滑动窗口（秒）
_GUEST_LOGIN_MAX = 10         # 窗口内最大允许次数
_guest_login_rate = {}        # ip -> [最近时间戳]
_guest_login_lock = threading.Lock()
_guest_login_last_clean = 0.0

# 登录接口按来源 IP 的总限速（含成功与失败）：单 IP 每分钟 ≤ LOGIN_IP_MAX_PER_MIN 次登录尝试。
# 独立于上方 (IP,用户名)/账户级计数（两者都可被轮换用户名绕开），把单次 ≈0.5s 的 PBKDF2
# 计费限制在可控速率内；30/分钟对真实使用足够宽，不会锁死合法用户。
_LOGIN_IP_WINDOW = 60           # 滑动窗口（秒）
LOGIN_IP_MAX_PER_MIN = 30       # 窗口内每 IP 允许的登录尝试总数（含成功与失败）
_login_ip_rate = {}             # ip -> [最近尝试时间戳]
_login_ip_lock = threading.Lock()
_login_ip_last_clean = 0.0      # 上次清理过期登录尝试记录的时间

# ---------------------------------------------------------------------------
# 锁定态“账号枚举 oracle”的残余风险说明与可选统一响应形态开关
# ---------------------------------------------------------------------------
# 现状（默认）语义：
#   * (IP,用户名) 6 次/10min、账户级 12 次/15min —— 命中即快速返回 429；
#   * 未锁定的错误口令（含未知用户名）→ 在 ac_core.verify_login 内做一次与真实校验
#     等代价的 PBKDF2 计费后返回 403「用户名或密码错误」。
# 可观察差异（残余风险，默认接受）：
#   * 状态码：锁定态 429 vs 普通失败 403 —— 攻击者可用 429 判定“该账号活跃且被锁定”；
#   * 时延：锁定态不经过 PBKDF2 计费、响应快，未知用户名每次 ≈PBKDF2 一次（慢），
#     同样可用于枚举“表内已计数/被锁”的账号。
# 已有一致性：未知用户名同样按同名入 _login_fail/_acct_fail（同一套计数与窗口过期节奏），
# 锁定窗口内被拒的请求不再累加计数、也不延长窗口（自愈）；即“锁定后计数节奏”与
# 未知用户名保持同表同节奏，差异仅体现在上述 429/时延两点。
# 开关（默认关，不改变 429 语义；开启后锁定态改为与「用户名或密码错误」完全一致的
# 403+同文案，消除状态码差异面，代价是合法用户被锁时看不到“尝试过多”的明确提示）：
_LOCK_RESPONSE_UNIFORM = False   # True=锁定态统一回 403 形态（默认 False）


def _normalize_username(username):
    """入表/入日志前的用户名归一：先过 sanitize_log_text（剔除 \r\n\x00 与控制字符），
    再截断到 64 字符（防超长用户名撑爆表/伪造日志行，B-09）。"""
    if not isinstance(username, str):
        username = ''
    return _ut_log.sanitize_log_text(username)[:64]


def _is_login_locked(ip, username):
    username = _normalize_username(username)
    with _login_fail_lock:
        now = time.time()
        # B-09 账户级锁定：任意来源该用户名 ≥12 次失败/15min → 全局锁定。
        # 未知用户名同样按同名入 _acct_fail/_login_fail（同一套计数/过期节奏），
        # 锁表本身不构成存在性泄露；可观察差异只剩“锁定态 429/快速响应 vs 未锁定
        # 错误口令 403/经 PBKDF2 计费”（枚举 oracle，见 _LOCK_RESPONSE_UNIFORM 注释）。
        acc = _acct_fail.get(username)
        if acc and now - acc['t'] <= _ACCOUNT_LOCK_MIN * 60 and acc['n'] >= _ACCOUNT_LOCK_AFTER:
            return True
        # 既有 (IP,用户名) 语义：6 次 / 10 分钟（429 行为保留）
        rec = _login_fail.get((ip, username))
        if not rec:
            return False
        if now - rec['t'] > _LOGIN_LOCK_MIN * 60:
            _login_fail.pop((ip, username), None)
            return False
        return rec['n'] >= _LOGIN_MAX_FAIL


def _record_login_fail(ip, username):
    """记录 (IP,用户名) 失败与账户级失败计数。

    返回 True 表示本次触发账户级锁定（调用方应据此发告警），否则 False。
    """
    global _login_last_clean
    username = _normalize_username(username)
    try:
        now = time.time()
        if now - _login_last_clean > 300:
            with _login_fail_lock:
                if now - _login_last_clean > 300:
                    expired = [k for k, rec in _login_fail.items()
                               if now - rec['t'] > _LOGIN_LOCK_MIN * 60]
                    for k in expired:
                        _login_fail.pop(k, None)
                    expired_acct = [k for k, rec in _acct_fail.items()
                                    if now - rec['t'] > _ACCOUNT_LOCK_MIN * 60]
                    for k in expired_acct:
                        _acct_fail.pop(k, None)
                    _login_last_clean = now
    except Exception:
        pass
    triggered = False
    with _login_fail_lock:
        now = time.time()
        key = (ip, username)
        rec = _login_fail.get(key)
        if not rec or now - rec['t'] > _LOGIN_LOCK_MIN * 60:
            rec = {'n': 0, 't': now}
        rec['n'] += 1
        rec['t'] = now
        _login_fail[key] = rec
        # 账户级计数（分布式来源也收敛到同一用户名）
        acc = _acct_fail.get(username)
        if not acc or now - acc['t'] > _ACCOUNT_LOCK_MIN * 60:
            acc = {'n': 0, 't': now}
        acc['n'] += 1
        acc['t'] = now
        _acct_fail[username] = acc
        if acc['n'] == _ACCOUNT_LOCK_AFTER:
            triggered = True
    return triggered


def _reset_login_fail(ip, username):
    # 登录成功：同时清 (IP,用户名) 与该用户名账户级计数（B-09）
    username = _normalize_username(username)
    with _login_fail_lock:
        _login_fail.pop((ip, username), None)
        _acct_fail.pop(username, None)


def _warn_account_lock(safe_user, client_ip, add_log):
    """账户级锁定触发告警（B-09）：security_event（统一事件 logger）+ add_log（运行日志）。

    正常错误口令与畸形请求体触发锁定共用本函数，保证两种入口的告警节奏一致。
    """
    detail = (f'账户疑似被爆破: {safe_user} '
              f'({_ACCOUNT_LOCK_AFTER}次失败/{_ACCOUNT_LOCK_MIN}分钟) ip={client_ip}')
    try:
        _ut_log.security_event('account_lock', detail, 'warn')
    except Exception:
        pass
    try:
        if add_log is not None:
            add_log(detail, 'warn')
    except Exception:
        pass


def _guest_login_allowed(ip):
    """游客登录滑动窗口限速（B-07）：单 IP 每分钟 ≤ _GUEST_LOGIN_MAX 次，超限 429。"""
    global _guest_login_last_clean
    try:
        now = time.time()
        if now - _guest_login_last_clean > 300:
            with _guest_login_lock:
                if now - _guest_login_last_clean > 300:
                    for k in [k for k, v in _guest_login_rate.items() if v and now - v[-1] > 300]:
                        _guest_login_rate.pop(k, None)
                    _guest_login_last_clean = now
    except Exception:
        pass
    now = time.time()
    with _guest_login_lock:
        ts_list = _guest_login_rate.setdefault(ip, [])
        cutoff = now - _GUEST_LOGIN_WINDOW
        ts_list[:] = [t for t in ts_list if t > cutoff]
        if len(ts_list) >= _GUEST_LOGIN_MAX:
            return False
        ts_list.append(now)
        return True


def _login_ip_allowed(ip):
    """登录接口按 IP 滑动窗口限速：单 IP 每分钟 ≤ LOGIN_IP_MAX_PER_MIN 次登录尝试
    （含成功与失败，风格与 _guest_login_allowed 一致），超限返回 False（调用方 429）。
    放行即记账（判定与 append 在同一把锁内完成），防并发穿透；窗口滑动后自动恢复；
    懒清理：距上次清理超过 300s 才扫一遍过期表项。"""
    global _login_ip_last_clean
    try:
        now = time.time()
        if now - _login_ip_last_clean > 300:
            with _login_ip_lock:
                if now - _login_ip_last_clean > 300:
                    for k in [k for k, v in _login_ip_rate.items() if v and now - v[-1] > 300]:
                        _login_ip_rate.pop(k, None)
                    _login_ip_last_clean = now
    except Exception:
        pass
    now = time.time()
    with _login_ip_lock:
        ts_list = _login_ip_rate.setdefault(ip, [])
        cutoff = now - _LOGIN_IP_WINDOW
        ts_list[:] = [t for t in ts_list if t > cutoff]
        if len(ts_list) >= LOGIN_IP_MAX_PER_MIN:
            return False
        ts_list.append(now)
        return True


def _login_ip_refund(ip):
    """登录成功后“回补”该 IP 的每 IP 限流窗口：弹出最近一条放行记账（pop 最右），
    使成功请求不再永久占用 30/min 窗口（失败仍照常计费，防并发穿透的“放行即记账”
    主目标不受影响）。空则忽略；若因此变空顺手清键（与 _login_ip_allowed 的懒清理
    清理语义一致：过期/空表项都不再占用）。并发下多个成功请求可能互相回补对方
    最新一条记录，属可接受近似（只会轻微放宽成功计数，不会放宽失败计费）。"""
    with _login_ip_lock:
        ts_list = _login_ip_rate.get(ip)
        if ts_list:
            ts_list.pop()
            if not ts_list:
                _login_ip_rate.pop(ip, None)


def serve_login_page(handler, base_dir, read_file_cached, get_guest_mode):
    """渲染登录页面

    登录页 UI 按架构独立存放在 web_page/login/login.html，
    与站内其他页面共用同一套静态资源（/static/style.css、/static/app.js）。
    本函数仅负责读取页面文件并注入游客模式开关（__GUEST_DISPLAY__）。
    """
    filepath = os.path.join(base_dir, 'web_page', 'login', 'login.html')
    # 语言 cookie 为 en 且存在英文版页面时，直接返回英文版
    try:
        ck = handler.headers.get('Cookie', '') or ''
        if 'leaf_lang=en' in ck:
            en_path = os.path.join(base_dir, 'web_page', 'login', 'login.en.html')
            if os.path.isfile(en_path):
                filepath = en_path
    except Exception:
        pass
    raw = read_file_cached(filepath)
    if raw is None:
        handler.send_error(404)
        return
    guest_display = '' if get_guest_mode() else 'none'
    html = raw.decode('utf-8').replace('__GUEST_DISPLAY__', guest_display)
    data = html.encode('utf-8')
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/html; charset=utf-8')
    handler.send_header('Content-Length', str(len(data)))
    # B-14：登录页安全响应头（登录页可含口令输入框 → no-store；wm_page 静态部分由 B2 负责）
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('X-Frame-Options', 'DENY')
    handler.send_header('Referrer-Policy', 'no-referrer')
    handler.end_headers()
    handler.wfile.write(data)


def auth_login(handler, add_log, logger, UPLOAD_DIR, verify_login,
               create_session, is_default_admin_password, _sessions, _sessions_lock):
    """登录"""
    client_ip = handler.client_address[0]
    try:
        # 每 IP 总限速置于最前（JSON 解析 / 任何锁定检查 / PBKDF2 校验之前）：放行即记账防并发穿透，
        # 成功/失败/畸形请求体均计入同一窗口，轮换用户名无法绕开高频 PBKDF2 计费
        if not _login_ip_allowed(client_ip):
            handler.send_json({'success': False, 'error': '登录尝试过于频繁，请稍后再试'}, 429)
            return
        length = int(handler.headers.get('Content-Length', 0))
        data = json.loads(handler.rfile.read(length).decode())
        if not isinstance(data, dict):
            data = {}
        raw_user = data.get('username', '')
        raw_pass = data.get('password', '')
        safe_user = _normalize_username(raw_user)   # B-09：用户名入日志/入表前脱敏（容忍非字符串→''）
        # 锁定检查先于类型校验：锁定态（(IP,用户名)6次/10min 或账户级12次/15min）统一按
        # “当前形态”拒绝且不再累加计数/不延长窗口（自愈）——与正常尝试完全一致。
        # 残余风险（429 vs 403 可枚举活跃账号）说明与可选统一形态开关见模块常量
        # _LOCK_RESPONSE_UNIFORM；此处仅按开关选择响应形态，语义保持 429（默认）。
        if _is_login_locked(client_ip, raw_user):
            if _LOCK_RESPONSE_UNIFORM:
                # 开关开：与「用户名或密码错误」完全同形态（403+同文案），消除状态码差异面
                handler.send_json({'success': False, 'error': '用户名或密码错误'}, 403)
            else:
                handler.send_json({'success': False, 'error': '尝试次数过多，请稍后再试'}, 429)
            return
        # 1) 输入类型校验：username/password 非字符串（null/int/dict/list/数组…）→ 400，
        #    不进入 PBKDF2 计费；但按“一次失败尝试”计入 (IP,用户名) 与账户级窗口
        #    （_record_login_fail，与正常错误口令同节奏；触发账户锁同样告警）。其上
        #    _login_ip_allowed 已对放行请求先行记账 → 畸形包无法绕过任何一级限流做
        #    无限试探/日志洪水。
        if not isinstance(raw_user, str) or not isinstance(raw_pass, str):
            if _record_login_fail(client_ip, safe_user):
                _warn_account_lock(safe_user, client_ip, add_log)
            handler.send_json({'success': False, 'error': '用户名或密码格式错误'}, 400)
            return
        username = raw_user.strip()
        password = raw_pass.strip()
        role = verify_login(username, password)
        if role:
            # 登录成功：回补该 IP 的每 IP 限流窗口（成功请求不永久占用 30/min 窗口），
            # 使合法用户短时多次登录不会被自身成功记录累计撞 429；失败计费不受影响
            _login_ip_refund(client_ip)
            _reset_login_fail(client_ip, username)
            add_log(f'登录: {safe_user} ({client_ip})', 'ok')
            logger.info(f'登录: {safe_user} ({client_ip})')
            if username:
                user_dir = os.path.join(UPLOAD_DIR, 'users', username)
                os.makedirs(user_dir, exist_ok=True)
            sid = create_session(username, role, client_ip=client_ip)
            if username == 'admin':
                if is_default_admin_password():
                    with _sessions_lock:
                        if sid in _sessions:
                            _sessions[sid]['need_change_password'] = True
            handler.send_response(200)
            handler._set_session_cookie(sid)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Access-Control-Allow-Origin', '*')
            handler.end_headers()
            handler.wfile.write(json.dumps({'success': True, 'role': role}).encode('utf-8'))
        else:
            locked = _record_login_fail(client_ip, username)   # True=本次触发账户级锁定
            # 时间侧信道等化已收敛到 ac_core.verify_login：
            # “用户不存在”分支做一次与真实校验等代价的 PBKDF2，失败路径不再重复计费
            logger.warning(f'登录失败: {safe_user} ({client_ip})')
            if locked:
                _warn_account_lock(safe_user, client_ip, add_log)
            handler.send_json({'success': False, 'error': '用户名或密码错误'}, 403)
    except json.JSONDecodeError:
        # B-15：畸形请求体不回显内部解析错误
        handler.send_json({'error': '请求体必须是合法 JSON'}, 400)
    except Exception:
        logger.warning(f'登录异常: {client_ip}')
        handler.send_json({'error': '服务器内部错误'}, 500)


def auth_check(handler, get_session, get_session_username,
               is_default_admin_password, get_guest_mode, _sessions, _sessions_lock):
    """检查登录状态"""
    cookie = handler.headers.get('Cookie', '')
    role, sid = get_session(cookie, True, handler.client_address[0])
    username = ''
    need_change_password = False
    if sid:
        username = get_session_username(sid)
        with _sessions_lock:
            info = _sessions.get(sid)
            if info:
                need_change_password = info.get('need_change_password', False)
    if not need_change_password and role in ('admin', 'super_admin'):
        need_change_password = is_default_admin_password()
    handler.send_json({
        'role': role,
        'guest_mode': get_guest_mode(),
        'username': username,
        'need_change_password': need_change_password
    })


def auth_logout(handler, get_session, remove_session, AUTH_COOKIE):
    """登出（仅 POST 语义；方法校验在路由层，本函数不关心方法）"""
    cookie = handler.headers.get('Cookie', '')
    _, sid = get_session(cookie, True, handler.client_address[0])   # 同 IP 校验
    if sid:
        remove_session(sid)
    # B-08：TLS 下登出 Set-Cookie 补 Secure（用 handler.server.is_secure，不猜测协议）
    secure = bool(getattr(getattr(handler, 'server', None), 'is_secure', False))
    sc = f'{AUTH_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax'
    if secure:
        sc += '; Secure'
    handler.send_response(302)
    handler.send_header('Location', '/login')
    handler.send_header('Set-Cookie', sc)
    # 同步清除“已登录”标记（ac_core.LOGIN_MARKER），避免登出后 8082 仍误判为已登录
    try:
        from leaffs.auth import core as _ac_mod
        handler.send_header('Set-Cookie',
                            f'{_ac_mod.LOGIN_MARKER}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax')
    except Exception:
        pass
    handler.end_headers()


def guest_login(handler, create_session, get_guest_mode=None):
    """游客登录（游客模式关闭时拒绝，防止直接构造请求绕过 UI 进入公共目录）"""
    try:
        if get_guest_mode is not None and not get_guest_mode():
            handler.send_json({'success': False, 'error': '游客模式已关闭'}, 403)
            return
        # B-07：单 IP 每分钟 guest_login 次数上限（滑动窗口），超限 429
        ip = handler.client_address[0]
        if not _guest_login_allowed(ip):
            handler.send_json({'success': False, 'error': '游客登录过于频繁'}, 429)
            return
        sid = create_session('游客', 'guest', client_ip=ip)
        handler.send_response(200)
        handler._set_session_cookie(sid)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Access-Control-Allow-Origin', '*')
        handler.end_headers()
        handler.wfile.write(json.dumps({'success': True, 'role': 'guest'}).encode('utf-8'))
    except Exception:
        # B-15：内部错误不回显细节
        handler.send_json({'error': '服务器内部错误'}, 500)


def auth_toggle(handler, add_log, set_guest_mode, get_guest_mode, revoke_guest_sessions=None):
    """切换游客模式

    body 可传 {"enabled": bool} 指定目标状态；未传或非布尔时服务端直接取反，
    避免客户端状态不同步导致游客模式“关了开不回来”。关闭时移除现有游客会话，
    使其立即失效。
    """
    try:
        # 安全：仅管理员可切换游客模式，防匿名远程开关
        if handler._get_effective_role() not in ('admin', 'super_admin'):
            handler.send_json({'error': 'Forbidden'}, 403)
            return
        length = int(handler.headers.get('Content-Length', 0))
        data = {}
        if length:
            data = json.loads(handler.rfile.read(length).decode())
            if not isinstance(data, dict):
                data = {}
        enabled = data.get('enabled')
        on = bool(enabled) if isinstance(enabled, bool) else not get_guest_mode()
        set_guest_mode(on)
        if not on and revoke_guest_sessions:
            try: revoke_guest_sessions()
            except Exception: pass
        add_log('游客模式已' + ('开启' if on else '关闭'), 'warn' if on else 'ok')
        handler.send_json({'success': True, 'guest_mode': get_guest_mode()})
    except json.JSONDecodeError:
        # B-15：畸形请求体不回显内部解析错误
        handler.send_json({'error': '请求体必须是合法 JSON'}, 400)
    except Exception:
        # B-15：内部错误不回显细节
        handler.send_json({'error': '服务器内部错误'}, 500)