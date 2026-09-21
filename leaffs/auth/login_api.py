"""认证 API — 登录/登出/检查/游客/模式切换"""

import json
import os
import time
import logging
import threading
import ipaddress

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
_GUEST_LOGIN_MAX = 10         # 窗口内最大允许次数（**默认值**；运行时由配置键覆盖）


def _guest_login_max():
    """当前生效的游客登录上限：优先取配置键 `guest_login_max_per_min`，
    配置层不可用时退回模块常量 `_GUEST_LOGIN_MAX`。

    做成可配是为了让测试放宽 —— 测试套的 guest 登录次数本来就贴着 10 这条线，
    跑得快时会挤进同一个 60 秒窗口，报出与本轮改动毫无关系的假失败。
    产品默认值仍是 10，行为不变。
    """
    try:
        from leaffs.config import core as _cc
        v = int(_cc.get_guest_login_max_per_min())
        return v if v > 0 else _GUEST_LOGIN_MAX
    except Exception:
        return _GUEST_LOGIN_MAX
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


def _begin_login_attempt(ip, username):
    """原子地「判锁定 + 记一次尝试」—— 必须在 PBKDF2 **之前**调用。

    原来这里是两步：`_is_login_locked()` 持锁**读**，然后 PBKDF2 校验，失败后再由
    `_record_login_fail()` 持锁**写**。中间隔着一次 PBKDF2（期间释放 GIL），于是并发的
    N 个请求会**同时**看到"失败次数还没到 6"而全部放行 —— 单次突发可试次数从 6 涨到
    `max_conn_per_ip`(20)。

    黑盒报告 C-1（2026-09-21）实测：

        串行对照         : WRONG×6, LOCK×2         → 正常第 7 次锁
        8 并发(不存在用户): WRONG=8, LOCK=0    ×2 次 → 8 次全部通过（上限是 6）
        8 并发(真实账号)  : WRONG=6, LOCK=2

    现在判定与占位在同一把锁内完成，并发放行数不会超过阈值。**成功登录仍走
    `_reset_login_fail()` 清零**，所以净效果依旧是"只有失败才计数"。

    返回 `(allowed, account_locked, spray)`：
      * `allowed=False` → 调用方回 429，且**不**累加计数（自愈，与旧语义一致）；
      * `account_locked` → 本次让账户级计数达到阈值。**只在最终失败时才发告警** ——
        成功登录虽然也占过位，但那笔计数马上会被清零，不该告警；
      * `spray` → 本次让"窗口内不同用户名数"达到喷洒阈值。
    """
    global _login_last_clean
    username = _normalize_username(username)

    # 懒清理（原来随计数一起住在 _record_login_fail 里，计数搬过来它就跟着搬）
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

    with _login_fail_lock:
        now = time.time()
        # B-09 账户级锁定：任意来源该用户名 ≥12 次失败/15min → 全局锁定。
        # 未知用户名同样按同名入 _acct_fail/_login_fail（同一套计数/过期节奏），
        # 锁表本身不构成存在性泄露；可观察差异只剩“锁定态 429/快速响应 vs 未锁定
        # 错误口令 403/经 PBKDF2 计费”（枚举 oracle，见 _LOCK_RESPONSE_UNIFORM 注释）。
        acc = _acct_fail.get(username)
        if acc and now - acc['t'] <= _ACCOUNT_LOCK_MIN * 60 and acc['n'] >= _ACCOUNT_LOCK_AFTER:
            return False, False, False
        # 既有 (IP,用户名) 语义：6 次 / 10 分钟（429 行为保留）
        key = (ip, username)
        rec = _login_fail.get(key)
        if rec and now - rec['t'] <= _LOGIN_LOCK_MIN * 60 and rec['n'] >= _LOGIN_MAX_FAIL:
            return False, False, False

        # 放行 → **立刻占位**：这次尝试现在就算数了，后来者马上看得见。
        # 判定与自增同处一把锁内，是这条修复的全部要害。
        if not rec or now - rec['t'] > _LOGIN_LOCK_MIN * 60:
            rec = {'n': 0, 't': now}
        rec['n'] += 1
        rec['t'] = now
        _login_fail[key] = rec

        if not acc or now - acc['t'] > _ACCOUNT_LOCK_MIN * 60:
            acc = {'n': 0, 't': now}
        acc['n'] += 1
        acc['t'] = now
        _acct_fail[username] = acc

        # 喷洒看的是"窗口内不同用户名数"，复用刚更新的 _acct_fail（同一把锁内，一致）
        return (True, acc['n'] == _ACCOUNT_LOCK_AFTER,
                _spray_user_count(now) >= _SPRAY_USERS)


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
        if len(ts_list) >= _guest_login_max():
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


# ========== 第五层：全局登录限速 + 密码喷洒告警（2026-09-17） ==========
# 上面四层全是"按维度收敛"的：(IP,用户名)、用户名、游客 IP、每 IP 总数。
# 攻击者**换 IP ＋ 每个名字只试 1~2 次**就能同时躲开前两层，只剩"每 IP 30/分钟"挡着，
# 而那是可以靠换 IP 线性扩展的 ⇒ 总尝试速率与 PBKDF2 的 CPU 消耗都没有上限。
# 这一层看**总量**：60 秒窗口内非环回来源的登录尝试超过阈值 ⇒ 之后非环回登录一律 429。
#
# 口径（用户拍板）：「**就是自保**，打满了服务本来就会崩，还不如全局锁带提示」。
# 用**滑动窗口**而不是固定长锁：攻击进行中一直限，攻击停手后最多一个窗口自动恢复。
# 固定长锁有两个坑 —— 攻击停手后合法用户还要白等；若实现成"每次超限都续期"，
# 攻击者"隔几分钟碰一下"就能把锁无限延长，反而成了**他可控的拒绝服务**。
#
# ⚠️ **环回豁免**：127.0.0.1 / ::1 永不拦、也不计入窗口 —— 攻击者触发全局锁时，
# 管理员仍能从本机进去处理。来源 IP 伪造不了（要完成 TCP 握手），所以这条是安全的。
_GLOBAL_LOGIN_WINDOW = 60     # 滑动窗口（秒）
_GLOBAL_LOGIN_MAX = 100       # 窗口内允许的非环回登录尝试总数（含成功与失败）
_global_login_rate = []       # [时间戳]，只收非环回来源；长度天然 ≤ _GLOBAL_LOGIN_MAX
_global_login_lock = threading.Lock()
_global_login_warned_at = 0.0  # 上次"全局限速触发"告警时间（同窗口只报一次）

# 密码喷洒告警：60 秒窗口内**出现过登录失败的「不同用户名」个数**。
# 这是"喷洒"的指纹（大量不同名字各试少量）；单账号爆破去重后只有 1，
# 已由账户级锁定覆盖，不该在这里重复告警。
_SPRAY_WINDOW = 60
_SPRAY_USERS = 8
_spray_warned_at = 0.0        # 上次喷洒告警时间（同窗口只报一次）


def _is_loopback_ip(ip):
    """来源是否环回（用 ipaddress 判，顺带覆盖 ::ffff:127.0.0.1 这类 IPv4-mapped）。

    解析不了就按"不是环回"处理 —— 宁可多限一个看不懂的地址，
    也不能因为解析失败给全局限速开口子。
    """
    try:
        return ipaddress.ip_address((ip or '').split('%')[0]).is_loopback
    except Exception:
        return False


def _login_global_allowed(ip):
    """全局登录限速（第五层）：60 秒窗口内非环回尝试 ≤ _GLOBAL_LOGIN_MAX。

    环回来源直接放行，**不入表也不判表**（因此不占非环回的额度）。
    放行即记账（判定与 append 在同一把锁内完成），防并发穿透；
    表长天然不超过阈值，不需要额外清理。
    """
    if _is_loopback_ip(ip):
        return True
    now = time.time()
    with _global_login_lock:
        cutoff = now - _GLOBAL_LOGIN_WINDOW
        _global_login_rate[:] = [t for t in _global_login_rate if t > cutoff]
        if len(_global_login_rate) >= _GLOBAL_LOGIN_MAX:
            return False
        _global_login_rate.append(now)
        return True


def _warn_login_flood(add_log):
    """全局限速触发告警：security_event + 运行日志，**同一窗口只报一次**。

    只报一次是必须的 —— 否则每条被拒的请求都写一行日志，"告警"本身就成了新的洪水面。
    """
    global _global_login_warned_at
    now = time.time()
    with _global_login_lock:
        if now - _global_login_warned_at < _GLOBAL_LOGIN_WINDOW:
            return
        _global_login_warned_at = now
    detail = ('全局登录限速触发: %d 秒内非环回登录尝试超过 %d 次，已临时限制'
              % (_GLOBAL_LOGIN_WINDOW, _GLOBAL_LOGIN_MAX))
    try:
        _ut_log.security_event('login_flood', detail, 'warn')
    except Exception:
        pass
    try:
        if add_log is not None:
            add_log(detail, 'warn')
    except Exception:
        pass


def _spray_user_count(now):
    """60 秒内出现过登录失败的**不同用户名**个数（调用方须持 _login_fail_lock）。

    直接复用 `_acct_fail` 的 `t`（最近失败时间），不需要新结构。
    """
    return sum(1 for rec in _acct_fail.values()
               if now - rec.get('t', 0) <= _SPRAY_WINDOW)


def _warn_login_spray(safe_user, add_log):
    """密码喷洒告警（同窗口只报一次，理由同 `_warn_login_flood`）"""
    global _spray_warned_at
    now = time.time()
    with _login_fail_lock:
        if now - _spray_warned_at < _SPRAY_WINDOW:
            return
        _spray_warned_at = now
    detail = ('疑似密码喷洒: %d 秒内有 %d 个不同用户名登录失败（阈值 %d），最近一个 %s'
              % (_SPRAY_WINDOW, _SPRAY_USERS, _SPRAY_USERS, safe_user))
    try:
        _ut_log.security_event('login_spray', detail, 'warn')
    except Exception:
        pass
    try:
        if add_log is not None:
            add_log(detail, 'warn')
    except Exception:
        pass


def serve_login_page(handler, base_dir, read_file_cached, get_guest_mode):
    """渲染登录页面

    登录页 UI 按架构独立存放在 web_page/login/login.html，
    与站内其他页面共用同一套静态资源（/static/style.css、/static/app.js）。
    本函数仅负责读取页面文件并注入游客模式开关（__GUEST_DISPLAY__）。
    """
    filepath = os.path.join(base_dir, 'web_page', 'login', 'login.html')
    used_en = False
    ck = ''
    # 语言 cookie 为 en 且存在英文版页面时，直接返回英文版
    try:
        ck = handler.headers.get('Cookie', '') or ''
        if 'leaf_lang=en' in ck:
            en_path = os.path.join(base_dir, 'web_page', 'login', 'login.en.html')
            if os.path.isfile(en_path):
                filepath = en_path
                used_en = True
    except Exception:
        pass
    raw = read_file_cached(filepath)
    if raw is None:
        handler.send_error(404)
        return
    guest_display = '' if get_guest_mode() else 'none'
    html = raw.decode('utf-8').replace('__GUEST_DISPLAY__', guest_display)
    # 亮暗与主题按钮：与站内其它页同一份（服务端只按 cookie 渲染色，不存主题）；
    # 登录页不注入主色，因此 accent 用默认空值
    from leaffs.web.render import inject_page_theme
    html = inject_page_theme(html, ck, used_en)
    data = html.encode('utf-8')
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/html; charset=utf-8')
    handler.send_header('Content-Length', str(len(data)))
    # B-14：登录页安全响应头（登录页可含口令输入框 → no-store；wm_page 静态部分由 B2 负责）
    handler.send_header('Cache-Control', 'no-store')
    # 公共头（nosniff / XFO / Referrer / HSTS）由 handler.end_headers 统一补，不手写（会重复）
    handler.end_headers()
    handler.wfile.write(data)


def auth_login(handler, add_log, logger, UPLOAD_DIR, verify_login,
               create_session, is_default_admin_password, _sessions, _sessions_lock):
    """登录"""
    client_ip = handler.client_address[0]
    try:
        # 第五层（全局，2026-09-17）置于最前：让**所有**尝试都进全局窗口 ——
        # 若放在每 IP 之后，被每 IP 拒掉的那部分就不计入，分布式尝试更容易漏网。
        # 环回不在此列（见 _login_global_allowed）：管理员始终能从本机登录。
        if not _login_global_allowed(client_ip):
            _warn_login_flood(add_log)
            handler.send_json({'success': False,
                               'error': '检测到大量登录尝试，已临时限制登录以保护服务，请稍后再试'},
                              429)
            return
        # 每 IP 总限速置于其后（JSON 解析 / 任何锁定检查 / PBKDF2 校验之前）：放行即记账防并发穿透，
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
        # 判定与占位**一步完成**（_begin_login_attempt），且必须在 PBKDF2 之前：
        # 锁定态（(IP,用户名)6次/10min 或账户级12次/15min）统一按“当前形态”拒绝且
        # 不再累加计数/不延长窗口（自愈）—— 与正常尝试完全一致。原来"检查在 PBKDF2 前、
        # 计数在 PBKDF2 后"分成两次加锁，并发请求会同时看到旧计数而全部放行（C-1）。
        # 残余风险（429 vs 403 可枚举活跃账号）说明与可选统一形态开关见模块常量
        # _LOCK_RESPONSE_UNIFORM；此处仅按开关选择响应形态，语义保持 429（默认）。
        allowed, acct_locked, spray = _begin_login_attempt(client_ip, safe_user)
        if not allowed:
            if _LOCK_RESPONSE_UNIFORM:
                # 开关开：与「用户名或密码错误」完全同形态（403+同文案），消除状态码差异面
                handler.send_json({'success': False, 'error': '用户名或密码错误'}, 403,
                                  exempt=True)
            else:
                handler.send_json({'success': False, 'error': '尝试次数过多，请稍后再试'}, 429)
            return
        # 1) 输入类型校验：username/password 非字符串（null/int/dict/list/数组…）→ 400，
        #    不进入 PBKDF2 计费；但按“一次失败尝试”计入 (IP,用户名) 与账户级窗口
        #    （_begin_login_attempt 已在上方占位，与正常错误口令同节奏；触发账户锁同样
        #    告警）。其上 _login_ip_allowed 已对放行请求先行记账 → 畸形包无法绕过任何
        #    一级限流做无限试探/日志洪水。
        if not isinstance(raw_user, str) or not isinstance(raw_pass, str):
            # 这次尝试的计数已在 _begin_login_attempt 里占位完成，这里只按结果告警
            if acct_locked:
                _warn_account_lock(safe_user, client_ip, add_log)
            if spray:
                _warn_login_spray(safe_user, add_log)
            handler.send_json({'success': False, 'error': '用户名或密码格式错误'}, 400)
            return
        username = raw_user.strip()
        password = raw_pass.strip()
        role = verify_login(username, password)
        if role:
            # 登录成功：回补该 IP 的每 IP 限流窗口（成功请求不永久占用 30/min 窗口），
            # 使合法用户短时多次登录不会被自身成功记录累计撞 429；失败计费不受影响
            _login_ip_refund(client_ip)
            # 清零的键必须与占位时用的键**完全同一个**（都取自 safe_user）——
            # 原来占位用 `_normalize_username(raw_user)`、清零用 `raw_user.strip()`，
            # 用户名带首尾空格时两者不相等，于是失败计数永远清不掉。
            _reset_login_fail(client_ip, safe_user)
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
            _ok = json.dumps({'success': True, 'role': role}).encode('utf-8')
            handler.send_response(200)
            handler._set_session_cookie(sid)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Access-Control-Allow-Origin', '*')
            # HTTP/1.1 预备（2026-09-18）：正文必须有定界（这条手写响应不走 send_json）
            handler.send_header('Content-Length', str(len(_ok)))
            # LF-10：登录响应含身份信息，绝不缓存（安全头由 handler.end_headers 统一补）
            handler.send_header('Cache-Control', 'no-store')
            handler.end_headers()
            handler.wfile.write(_ok)
        else:
            # 时间侧信道等化已收敛到 ac_core.verify_login：
            # “用户不存在”分支做一次与真实校验等代价的 PBKDF2，失败路径不再重复计费
            # （计数已在 _begin_login_attempt 里占位完成，这里只按结果告警）
            logger.warning(f'登录失败: {safe_user} ({client_ip})')
            if acct_locked:
                _warn_account_lock(safe_user, client_ip, add_log)
            if spray:
                _warn_login_spray(safe_user, add_log)
            handler.send_json({'success': False, 'error': '用户名或密码错误'}, 403,
                              exempt=True)
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
    role, sid = get_session(cookie, handler.client_address[0])
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
    _, sid = get_session(cookie, handler.client_address[0])   # 同 IP 校验
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
    # HTTP/1.1 预备（2026-09-18）：302 允许带正文，必须显式声明空正文
    handler.send_header('Content-Length', '0')
    handler.end_headers()


def guest_login(handler, create_session, get_guest_mode=None):
    """游客登录（游客模式关闭时拒绝，防止直接构造请求绕过 UI 进入公共目录）"""
    try:
        if get_guest_mode is not None and not get_guest_mode():
            handler.send_json({'success': False, 'error': '游客模式已关闭'}, 403,
                              exempt=True)
            return
        # B-07：单 IP 每分钟 guest_login 次数上限（滑动窗口），超限 429
        ip = handler.client_address[0]
        if not _guest_login_allowed(ip):
            handler.send_json({'success': False, 'error': '游客登录过于频繁'}, 429)
            return
        sid = create_session('游客', 'guest', client_ip=ip)
        _ok = json.dumps({'success': True, 'role': 'guest'}).encode('utf-8')
        handler.send_response(200)
        handler._set_session_cookie(sid)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Access-Control-Allow-Origin', '*')
        # HTTP/1.1 预备（2026-09-18）：正文必须有定界（这条手写响应不走 send_json）
        handler.send_header('Content-Length', str(len(_ok)))
        # LF-10：游客登录响应同样含身份信息，绝不缓存
        handler.send_header('Cache-Control', 'no-store')
        handler.end_headers()
        handler.wfile.write(_ok)
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
        if not set_guest_mode(on):
            # 保存失败必须说：开关只在内存里，重启后就还原了
            handler.send_json({'success': False,
                               'error': '保存失败：游客模式开关只在内存里，重启后会还原'}, 500)
            return
        if not on and revoke_guest_sessions:
            try: revoke_guest_sessions()
            except Exception: pass
        # 审计：谁开的/关的（原来只有一句"游客模式已开启"，看不出操作者）
        try:
            _who = ' [%s ip=%s]' % (handler._actor(), handler.client_address[0])
        except Exception:
            _who = ''
        add_log('游客模式已' + ('开启' if on else '关闭') + _who, 'warn' if on else 'ok')
        handler.send_json({'success': True, 'guest_mode': get_guest_mode()})
    except json.JSONDecodeError:
        # B-15：畸形请求体不回显内部解析错误
        handler.send_json({'error': '请求体必须是合法 JSON'}, 400)
    except Exception:
        # B-15：内部错误不回显细节
        handler.send_json({'error': '服务器内部错误'}, 500)