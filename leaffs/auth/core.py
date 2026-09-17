#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auth_account.py - Account management (user/session/roles)
All inter-module communication via API functions in this file.
Password security: PBKDF2-HMAC-SHA256 with per-user salt (32 bytes)
"""

import os
import json
import threading
import secrets
import time
import hashlib
import hmac
import base64
from leaffs.utils.core import CONFIG_DIR

AUTH_COOKIE = 'wifi_session'
SESSION_EXPIRY_DAYS = 30
USERS_FILE = os.path.join(CONFIG_DIR, 'users.json')
PBKDF2_ITERATIONS = 600000
SALT_LENGTH = 32

# B-03 口令新策略：新建/改密口令长度下限/上限（存量口令不强改，见 FIX_SPEC §R1 D3）
PASSWORD_MIN_LEN = 8
PASSWORD_MAX_LEN = 128

# B-06/B-07 会话表上限（内存 dict，重启即清空，可接受；超限按 expiry 淘汰最旧）
MAX_SESSIONS_TOTAL = 20000      # 会话总量上限
MAX_SESSIONS_PER_USER = 30      # 每用户名上限（游客按 username=='游客' 合并计数）
MAX_GUEST_PER_IP = 5            # 游客每来源 IP 上限

# ========== Password Hashing ==========

def _hash_password(password):
    salt = os.urandom(SALT_LENGTH)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PBKDF2_ITERATIONS)
    salt_b64 = base64.b64encode(salt).decode('ascii')
    hash_b64 = base64.b64encode(dk).decode('ascii')
    return f'{PBKDF2_ITERATIONS}${salt_b64}${hash_b64}'

def _verify_password(password, stored):
    try:
        parts = stored.split('$', 2)
        if len(parts) == 3:
            iterations = int(parts[0])
            salt_b64, hash_b64 = parts[1], parts[2]
        else:
            salt_b64, hash_b64 = parts
            iterations = PBKDF2_ITERATIONS
        salt = base64.b64decode(salt_b64)
        stored_hash = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
        return hmac.compare_digest(dk, stored_hash)   # B-05 恒定时间比较
    except (ValueError, Exception):
        return False

def _is_legacy_plaintext(password):
    return isinstance(password, str) and '$' not in password

def _migrate_password(user_data):
    pwd = user_data.get('password', '')
    if not pwd:
        # 没有口令（首启的超管就是这样）：不是待迁移的明文。
        # 空串若被当明文去哈希，就得到 hash('')，而空串比空串是能通过的 —— 等于空密码可登录。
        return False
    if _is_legacy_plaintext(pwd):
        user_data['password'] = _hash_password(pwd)
        return True
    return False

def hash_password(password):
    return _hash_password(password)

def is_default_admin_password():
    """是否**还有超级管理员没设过自己的口令**（首启不写口令，只能靠本机一次性令牌登录）。

    原来这里是"密码是否等于 admin"，而建号时写死的就是 admin/admin —— 等于把超管
    敞在局域网里，而且那个口令还绕过了 8 位下限。现在首启不写口令，用户自己设过就有口令了。

    ⚠️ **按角色找、不按用户名**（`issues.md` §二 第 8 条）：原实现是 `_users.get('admin')`
    —— 硬编码用户名。超管**改名**、或**删掉 admin 另建一个超管**之后，这个判断就指向一个
    不存在的账号 ⇒ 恒为 False ⇒ 真正"还没设口令"的超管**再也提醒不到**
    （登录页与管理页那条"请尽快设置密码"的横幅不会出现）。
    口径与 `get_super_admin_name()` / `_ensure_super_admin()` 一致：超管 = `role == 'super_admin'`。
    只要**存在任一**没口令的超管就返回 True（也覆盖"有多个超管、其中一个没设"的情况）。
    """
    with _users_lock:
        for user in (_users or {}).values():
            if not isinstance(user, dict):
                continue
            if user.get('role') == 'super_admin' and not user.get('password'):
                return True
    return False


def has_password(username):
    """该账号是否已经设过口令（首启的超管没有口令，只能靠本机一次性令牌登录）"""
    with _users_lock:
        user = _users.get(username)
        return bool(user and user.get('password'))

_sessions = {}
_sessions_lock = threading.Lock()
_users = {}
_users_lock = threading.RLock()

# ===================================================
# Session API
# ===================================================

def _gen_id():
    return secrets.token_hex(24)

def _same_client(ip_a, ip_b):
    """判断会话来源 IP 与当前请求 IP 是否一致（IPv4-mapped IPv6 归一、环回等价）"""
    if not ip_a or not ip_b:
        return True  # 未知来源不做校验
    a, b = ip_a, ip_b
    if a.startswith('::ffff:'):
        a = a[7:]
    if b.startswith('::ffff:'):
        b = b[7:]
    loop = ('127.0.0.1', '::1')
    if a in loop and b in loop:
        return True
    return a == b

# ========== 会话持久化（2026-09-17）==========
# 会话表原来是**纯内存 dict**（上面那句注释自述"重启即清空，可接受"）。实测下来
# **不可接受**：每次重启服务、每次安卓 App 被系统杀掉后重启，用户都要重新登录一遍。
# 现在落盘到 config/sessions.json，重启后会话仍有效。
#
# ⚠️ 这个文件里装着 **sid**（等于会话凭据）：与同目录的 users.json（口令哈希）、
#    share_access.json（授权票据）同级敏感。`.gitignore` 已挡住 `config/`，不入库；
#    `harden_config_acls` 那个开关也顺带覆盖它。
#
# ⚠️ **只在"会话集合发生变化"时落盘**（新建 / 删除 / 踢人 / 容量淘汰 / 扫码兑换）——
#    `get_session` 只读、每次请求都会走，绝不能在那里写盘（那会把每个请求都变成一次 IO）。
#    盘上留着已过期的条目没关系：`load_sessions()` 启动时会清掉。
SESSIONS_FILE = os.path.join(CONFIG_DIR, 'sessions.json')


def _save_sessions_locked():
    """会话表写盘（**须持 `_sessions_lock`**）。

    原子写：先写 `.tmp` 再 `os.replace` —— 进程写到一半被杀，也不会留下半截 JSON
    把整张表废掉（那是"重启后所有人被登出"的另一种形式）。
    写失败**不抛**：内存里的会话仍然有效，只是退化成"重启后要重登"的旧行为，
    不该因为存不下盘而把用户这次请求打挂。
    """
    try:
        tmp = SESSIONS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_sessions, f, ensure_ascii=False)
        os.replace(tmp, SESSIONS_FILE)
    except Exception:
        pass


def load_sessions():
    """启动时把会话表读回来（`app.start_server` 调，紧跟 `load_users()`）。

    文件不存在 / 读不出来 / 结构损坏 → 空表（用户重登一次，好过带着半张坏表跑）。
    **顺手丢弃已过期的条目**，也丢弃单项结构不合法的（缺 expiry/username/role）——
    一个坏条目不该让整张表加载失败。
    """
    global _sessions
    now = time.time()
    loaded = {}
    try:
        with open(SESSIONS_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for sid, info in raw.items():
                if not isinstance(sid, str) or not isinstance(info, dict):
                    continue
                if not isinstance(info.get('expiry'), (int, float)) or info['expiry'] <= now:
                    continue
                if not isinstance(info.get('username'), str) or not isinstance(info.get('role'), str):
                    continue
                loaded[sid] = info
    except FileNotFoundError:
        pass
    except Exception:
        pass
    with _sessions_lock:
        _sessions = loaded


def create_session(username, role, client_ip=''):
    sid = _gen_id()
    with _sessions_lock:
        entry = {'expiry': time.time() + SESSION_EXPIRY_DAYS * 86400,
                 'username': username, 'role': role}
        if client_ip:
            entry['ip'] = client_ip
        _sessions[sid] = entry
        _clean_sessions()
        _enforce_session_caps(username, client_ip, keep_sid=sid)   # B-06/B-07
        _save_sessions_locked()      # 落盘：重启后这个会话仍然有效
    return sid

def get_session(cookie_header, client_ip=''):
    """从 Cookie 解出会话，返回 (role, sid)；无 Cookie / 会话无效 / IP 不符 → (None, '')

    ⚠️ 这里**故意没有**"关闭鉴权"的开关。早先签名是
    `get_session(cookie_header, auth_enabled, client_ip)`，函数第一行就是
    `if not auth_enabled: return 'super_admin', ''` —— 传 False 直接变超级管理员、
    连 Cookie 都不看。那是历史遗留的调试口子，已删除：这种开关只会在某天被人
    顺手传成 False，然后整站鉴权当场消失，而且看不出是故意的还是手滑。
    """
    if not cookie_header: return None, ''
    cookies = {}
    for part in cookie_header.split(';'):
        part = part.strip()
        if '=' in part:
            k, v = part.split('=', 1)
            cookies[k.strip()] = v.strip()
    sid = cookies.get(AUTH_COOKIE, '')
    if not sid: return None, ''
    with _sessions_lock:
        info = _sessions.get(sid)
        if not info:
            return None, ''
        if info['expiry'] <= time.time():
            _sessions.pop(sid, None)    # 过期：清掉
            return None, ''
        if not _same_client(info.get('ip', ''), client_ip):
            # 来源 IP 不符：**只拒绝这一次，不删会话**。
            # 旧写法在这里 pop —— 任何人只要拿到别人的 sid、从别的 IP 打一次请求，
            # 就能把对方踢下线（不需要会用它，只要毁掉它），是零成本的 DoS。
            # 而已绑 IP 的会话本来也认 IP：泄漏的 sid 换台机器本来就用不了。
            return None, ''
        return info['role'], sid

def _clean_sessions():
    now = time.time()
    expired = [k for k, v in _sessions.items() if v['expiry'] < now]
    for k in expired: del _sessions[k]

def _trim_sessions(sid_list, cap, keep_sid):
    """容量修剪：sid_list 含刚插入的 keep_sid；若总数超过 cap，按 expiry 升序
    淘汰最旧（keep_sid 永不淘汰）。cap <= 0 表示不限制。须持 _sessions_lock 调用。"""
    if cap <= 0 or len(sid_list) <= cap:
        return
    ordered = sorted((s for s in sid_list if s != keep_sid),
                     key=lambda s: (_sessions.get(s, {}).get('expiry', 0), s))
    for sid in ordered[:len(sid_list) - cap]:
        _sessions.pop(sid, None)

def _enforce_session_caps(username, client_ip='', keep_sid=''):
    """B-06/B-07 会话表上限（须持 _sessions_lock 调用）：
    1) 游客按来源 IP 限（MAX_GUEST_PER_IP）；2) 每用户名限（MAX_SESSIONS_PER_USER，
    游客按 '游客' 合并计数）；3) 总量限（MAX_SESSIONS_TOTAL）。均淘汰最旧。"""
    if not _sessions:
        return
    try:
        if username == '游客' and client_ip:
            _trim_sessions([s for s in _sessions
                            if _sessions[s].get('username') == '游客'
                            and _sessions[s].get('ip') == client_ip],
                           MAX_GUEST_PER_IP, keep_sid)
        _trim_sessions([s for s in _sessions if _sessions[s].get('username') == username],
                       MAX_SESSIONS_PER_USER, keep_sid)
        _trim_sessions(list(_sessions.keys()), MAX_SESSIONS_TOTAL, keep_sid)
    except Exception:
        pass

def _revoke_user_sessions(username, keep_sid=''):
    """撤销该用户名的全部会话（keep_sid 除外）。角色/口令/改名/删除即时生效（B-10/IC-SESS）。

    ⚠️ **必须落盘**：不落盘的话，"被踢掉的会话"只从内存消失 —— 重启之后它又回来了
    （删掉的用户、降权过的账号会带着旧会话复活）。这是本条修复附带的安全要求。
    """
    with _sessions_lock:
        for sid in list(_sessions.keys()):
            info = _sessions.get(sid)
            if info and info.get('username') == username and sid != keep_sid:
                del _sessions[sid]
        _save_sessions_locked()

def refresh_session_role(sid, ip=''):
    """按用户“当前”角色实时刷新会话角色（IC-SESS，供 A-07 WS 复查）。

    返回 (fresh_role, username)；会话失效/IP 不符/用户已删除 → None。
    游客会话（username=='游客'）不查 _users，按会话内角色返回。
    """
    if not sid:
        return None
    with _sessions_lock:
        info = _sessions.get(sid)
        if not info:
            return None
        if info.get('expiry', 0) <= time.time():
            _sessions.pop(sid, None)
            return None
        if not _same_client(info.get('ip', ''), ip):
            return None
        username = info.get('username', '')
        role = info.get('role', '')
    if username == '游客':
        return (role or 'guest'), username
    with _users_lock:
        user = _users.get(username)
    if not user:
        return None
    return user.get('role', 'guest'), username

def remove_session(sid):
    with _sessions_lock:
        _sessions.pop(sid, None)
        _save_sessions_locked()      # 登出要让盘上也消失，否则重启后"登出"白做

def remove_guest_sessions():
    """移除所有游客会话（关闭游客模式时调用，使其立即失效）"""
    with _sessions_lock:
        for sid in [s for s, i in _sessions.items() if i.get('username') == '游客']:
            del _sessions[sid]
        _save_sessions_locked()

def get_session_username(sid):
    with _sessions_lock:
        info = _sessions.get(sid)
        if info: return info['username']
    return ''

# ===================================================
# User Management API
# ===================================================

def load_users():
    with _users_lock:
        _load_users_locked()

def _load_users():
    _load_users_locked()

def _load_users_locked():
    global _users
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, 'r') as f:
                _users = json.load(f)
            migrated = False
            for user_info in _users.values():
                if _migrate_password(user_info): migrated = True
            if migrated: _save_users_locked()
    except Exception:
        _users = {}
    _ensure_super_admin()

def _ensure_super_admin():
    has_super = any(info.get('role') == 'super_admin' for info in _users.values())
    if not has_super:
        if _users:
            first = next(iter(_users))
            _users[first]['role'] = 'super_admin'
        else:
            # 首次启动：admin **不写口令** —— 初始只能靠本机一次性令牌登录（/login?leaf=）。
            # 没有口令就没有"默认弱口令"这回事；想用密码登录，用户自己去设一个。
            _users['admin'] = {'role': 'super_admin'}
        _save_users_locked()

def _save_users():
    with _users_lock:
        _save_users_locked()

def _save_users_locked():
    try:
        tmp = USERS_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(_users, f, indent=2)
        os.replace(tmp, USERS_FILE)
    except Exception:
        pass

def verify_login(username, password):
    with _users_lock:
        user = _users.get(username)
        if not user:
            # 抹平“用户不存在 / 密码错误”的时间侧信道：对给定密码执行一次
            # 与真实校验等代价的 PBKDF2（随机 16B salt，结果丢弃），
            # 保证两种失败路径都各恰一次 PBKDF2 消耗。
            try:
                hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                    secrets.token_bytes(16), PBKDF2_ITERATIONS)
            except Exception:
                pass
            return None
        stored = user.get('password', '')
        if not stored:
            # 该账号没有口令（首启的超管）：密码登录一律失败，只能用本机一次性令牌。
            # 不拦住的话，空串会被下面按"遗留明文"比较 —— compare_digest('', '') 是能过的。
            return None
        if _is_legacy_plaintext(stored):
            # 存量明文口令比较同样恒定时间（B-05），迁移窗口内防时序侧信道
            try:
                ok = hmac.compare_digest(stored, password)
            except (TypeError, ValueError):
                ok = False
            if ok:
                user['password'] = _hash_password(password)
                _save_users()
                return user['role']
            return None
        if _verify_password(password, stored):
            if stored.count('$') == 1:
                user['password'] = _hash_password(password)
                _save_users()
            return user['role']
    return None

def get_user_role(username):
    with _users_lock:
        user = _users.get(username)
        if user: return user.get('role')
    return None

def get_super_admin_name():
    """返回当前超级管理员用户名（IC-SESS，A-12 用于本机令牌登录建会话）；无则 None。"""
    with _users_lock:
        for name, info in _users.items():
            if info.get('role') == 'super_admin':
                return name
    return None

def issue_qr_authorize(caller_role, target_name, caller_name=''):
    """QR 代签授权判定（IC-QR，A-04 调用）。

    admin → 仅可代签“已存在且 role=='user'”的目标；
    super_admin → 可代签“已存在且 role in ('user','admin')”的目标，或目标 ==
                  caller_name（给自己签，返回该 super 目标角色，供唯一超管账号
                  在管理页“扫码登录”自己）；
    不存在目标 / guest / 他人的 super_admin 目标 → 一律拒绝（D6 禁 ghost/禁他人超管）。
    返回 (ok, target_role, err)；失败时 target_role 为空串。caller_name 缺省 '' 保持向后兼容。
    """
    if caller_role not in ('admin', 'super_admin'):
        return False, '', '无权限'
    if not isinstance(target_name, str) or not target_name:
        return False, '', '无权为该用户签发'
    with _users_lock:
        target = _users.get(target_name)
    if not target:
        return False, '', '无权为该用户签发'
    role = target.get('role', '')
    if caller_role == 'admin' and role == 'user':
        return True, role, ''
    if caller_role == 'admin' and role != 'user':
        return False, '', '无权为该用户签发'
    if caller_role == 'super_admin':
        if role in ('user', 'admin') or (caller_name and target_name == caller_name):
            return True, role, ''
        return False, '', '无权为该用户签发'
    return False, '', '无权限'

def list_users():
    with _users_lock:
        return {k: {'role': v['role'], 'speed_limit': v.get('speed_limit', 0), 'quota': v.get('quota', 0)} for k, v in _users.items()}

def get_user_quota(username):
    with _users_lock:
        user = _users.get(username)
        if user: return user.get('quota', 0)
    return 0


def get_user_ui_lang(username):
    """用户的服务端界面语言偏好（'en'/'zh'/'')——供无语言 cookie 的客户端
    （本机 webview 为无痕会话，cookie 存不住）恢复该用户上次的选择。"""
    with _users_lock:
        user = _users.get(username)
        if user: return str(user.get('ui_lang', '') or '')
    return ''

def set_user_ui_lang(username, lang):
    """持久化用户界面语言偏好到服务端账号（users.json）；非法值按 zh 落库。"""
    lang = 'en' if lang == 'en' else 'zh'
    with _users_lock:
        if username not in _users:
            return False
        _users[username]['ui_lang'] = lang
        _save_users_locked()
    return True

def set_user_quota(username, bytes_limit, caller_role='super_admin'):
    if caller_role not in ('super_admin', 'admin'): return False, 'No permission'
    with _users_lock:
        if username not in _users: return False, 'User not found'
        if caller_role == 'admin' and _users[username]['role'] in ('super_admin', 'admin'): return False, 'Cannot modify admin'
        _users[username]['quota'] = max(0, int(bytes_limit))
        _save_users()
    return True, ''

def get_user_speed_limit(username):
    with _users_lock:
        user = _users.get(username)
        if user: return user.get('speed_limit', 0)
    return 0

def set_user_speed_limit(username, bytes_per_sec, caller_role='super_admin'):
    if caller_role not in ('super_admin', 'admin'): return False, 'No permission'
    with _users_lock:
        if username not in _users: return False, 'User not found'
        if caller_role == 'admin' and _users[username]['role'] in ('super_admin', 'admin'): return False, 'Cannot modify admin'
        _users[username]['speed_limit'] = max(0, int(bytes_per_sec))
        _save_users()
    return True, ''

def _check_caller_can_manage(caller_role, target_username):
    if caller_role == 'super_admin': return True, ''
    if caller_role == 'admin':
        with _users_lock:
            target = _users.get(target_username)
            if not target: return False, 'User not found'
            if target['role'] in ('super_admin', 'admin'): return False, 'Cannot modify admin'
            return True, ''
    return False, 'No permission'

# B-02 角色枚举：合法可分配角色（super_admin 仅由 _ensure_super_admin/人工离线升级，防误建超管）
VALID_ROLES = ('user', 'admin', 'super_admin')

# B-04 Windows 保留设备名（与目录/文件语义冲突，禁止用作用户名）
_WIN_RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {f'COM{i}' for i in range(1, 10)} | {f'LPT{i}' for i in range(1, 10)}


def validate_password(pw):
    """口令新策略（B-03/IC：新建与改密 ≥ PASSWORD_MIN_LEN 且 ≤ PASSWORD_MAX_LEN）。

    存量口令不强改（验证路径兼容），仅“新建/修改时必须达标”。
    返回 (ok, err)。
    """
    if not isinstance(pw, str):
        return False, '密码格式无效'
    if not (PASSWORD_MIN_LEN <= len(pw) <= PASSWORD_MAX_LEN):
        return False, f'密码长度需在 {PASSWORD_MIN_LEN}~{PASSWORD_MAX_LEN} 字符之间'
    return True, ''

def validate_username(name):
    """用户名白名单：字母/数字/下划线/点/短横线/中文/日文假名/韩文，1~20 字符。

    显式排除引号、尖括号、反斜杠、/ 等会破坏 HTML 属性 / 内联 JS / 路径的字符；
    B-04 追加排除 '.'/'..' 与 Windows 保留设备名（CON/PRN/AUX/NUL/COM1-9/LPT1-9）及
    尾部点/空白（与 Windows 目录语义冲突）。
    """
    if not isinstance(name, str):
        return False
    n = len(name)
    if n < 1 or n > 20:
        return False
    basic = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
    for ch in name:
        if ch in basic:
            continue
        cp = ord(ch)
        if not (0x4e00 <= cp <= 0x9fff or   # CJK 统一表意
                0x3040 <= cp <= 0x30ff or   # 平假名/片假名
                0xac00 <= cp <= 0xd7af):    # 谚文
            return False
    if name in ('.', '..'):
        return False
    base_noext = name.split('.', 1)[0].upper()
    if base_noext in _WIN_RESERVED:
        return False
    if name != name.strip() or name.endswith('.'):
        return False
    return True

def add_user(username, password, role='user', caller_role='admin'):
    # B-02 服务端角色门槛与枚举收紧（纵深：B-01 在 ac_user 层已前置门槛）
    if caller_role not in ('admin', 'super_admin'):
        return False, '无权限'
    if role not in VALID_ROLES:
        return False, '非法角色'
    if role == 'super_admin':
        # 超管只能由 _ensure_super_admin / 人工离线升级产生，禁止经接口创建
        return False, '不允许直接创建超级管理员'
    if caller_role == 'admin' and role != 'user':
        return False, '管理员只能创建普通用户'
    if not validate_username(username):
        return False, '用户名只能包含字母/数字/下划线/点/短横线或中日韩文字（1~20 字符）'
    ok, err = validate_password(password)   # B-03：新建口令 ≥8 且 ≤128
    if not ok:
        return False, err
    with _users_lock:
        if username in _users: return False, 'Username exists'
        _users[username] = {'password': _hash_password(password), 'role': role}
        _save_users()
    return True, ''

def precheck_user_delete(username, caller_role='admin'):
    """删除用户的**前置校验**（不落盘）：角色门槛 / 存在性 / 最后一个超管。

    与 rename 的 `precheck_user_rename` 同模式：校验通过后调用方先去动目录
    （把家目录归档），目录动完再调 `delete_user` 提交 —— 免得"账号删了才发现目录搬不动"。
    """
    ok, err = _check_caller_can_manage(caller_role, username)
    if not ok: return False, err
    with _users_lock:
        if username not in _users: return False, 'User not found'
        if _users[username].get('role') == 'super_admin':
            super_count = sum(1 for u in _users.values() if u.get('role') == 'super_admin')
            if super_count <= 1: return False, 'Cannot delete last super admin'
    return True, ''


def delete_user(username, caller_role='admin'):
    ok, err = _check_caller_can_manage(caller_role, username)
    if not ok: return False, err
    with _users_lock:
        if username not in _users: return False, 'User not found'
        if _users[username].get('role') == 'super_admin':
            super_count = sum(1 for u in _users.values() if u.get('role') == 'super_admin')
            if super_count <= 1: return False, 'Cannot delete last super admin'
        del _users[username]
        _save_users()
        with _sessions_lock:
            for sid in list(_sessions.keys()):
                if _sessions[sid]['username'] == username: del _sessions[sid]
            # ⚠️ 必须落盘：否则被删用户的会话只从内存消失，**重启后会复活**
            _save_sessions_locked()
    return True, ''

def change_password(username, new_password, caller_role='admin', keep_sid=''):
    """修改口令。B-03：改密口令须过 validate_password（≥8 且 ≤128）。
    B-10：改密成功后即时注销该用户其它会话（keep_sid 为操作者自身 sid，保活自己）；
    同时清除其残留会话上的 need_change_password 提示标记。"""
    ok, err = _check_caller_can_manage(caller_role, username)
    if not ok: return False, err
    ok, err = validate_password(new_password)
    if not ok: return False, err
    with _users_lock:
        if username not in _users: return False, 'User not found'
        _users[username]['password'] = _hash_password(new_password)
        _save_users()
    _revoke_user_sessions(username, keep_sid=keep_sid)
    with _sessions_lock:
        for info in _sessions.values():
            if info.get('username') == username:
                info.pop('need_change_password', None)
    return True, ''


def self_change_password(username, new_password, keep_sid=''):
    """本人自助改密（“我的”页使用）：无 caller 管理门槛——HTTP 层已验旧密码与
    会话归属，这里只做口令长度校验并落盘；B-10 踢除该账号其它会话（保活 keep_sid），
    同时清除残留会话上的 need_change_password 提示。"""
    ok, err = validate_password(new_password)
    if not ok:
        return False, err
    with _users_lock:
        if username not in _users:
            return False, 'User not found'
        _users[username]['password'] = _hash_password(new_password)
        _save_users()
    _revoke_user_sessions(username, keep_sid=keep_sid)
    with _sessions_lock:
        for info in _sessions.values():
            if info.get('username') == username:
                info.pop('need_change_password', None)
    return True, ''

def update_user_role(username, new_role, caller_role='admin', keep_sid=''):
    # B-02：新角色只允许 user/admin（HTTP 层已限，核心层再限）；super_admin 目标不可改
    if new_role not in ('user', 'admin'):
        return False, '非法角色'
    if caller_role == 'admin' and new_role == 'admin':
        return False, 'Admin cannot create admin'
    ok, err = _check_caller_can_manage(caller_role, username)
    if not ok: return False, err
    with _users_lock:
        if username not in _users: return False, 'User not found'
        if _users[username]['role'] == 'super_admin': return False, 'Cannot modify super admin'
        _users[username]['role'] = new_role
        _save_users()
    _revoke_user_sessions(username, keep_sid=keep_sid)   # B-10：改角色即时注销
    return True, ''

def precheck_user_rename(old_name, new_name, caller_role='admin'):
    """改名前置校验（IC-USER：先校验后搬目录，不落盘）。返回 (ok, err)。"""
    if caller_role not in ('super_admin', 'admin'):
        return False, '无权限'
    if not validate_username(new_name):
        return False, '用户名只能包含字母/数字/下划线/点/短横线或中日韩文字（1~20 字符）'
    if caller_role == 'admin':
        ok, err = _check_caller_can_manage(caller_role, old_name)
        if not ok: return False, err
    with _users_lock:
        if old_name not in _users: return False, 'User not found'
        if new_name in _users: return False, 'New name already exists'
    return True, ''

def update_user_name(old_name, new_name, caller_role='admin', keep_sid=''):
    """改名提交（目录已由调用方先 os.rename 成功，IC-USER 事务顺序）。

    本函数只负责：再次校验 → 更新用户表（记录改名）→ 撤销旧用户名全部会话（B-10，
    不再把旧会话改名续用——旧名字已不存在，会话必须重登）。任一步失败由调用方回滚目录。
    """
    ok, err = precheck_user_rename(old_name, new_name, caller_role)
    if not ok: return False, err
    with _users_lock:
        user_data = _users.pop(old_name)
        _users[new_name] = user_data
        _save_users()
    _revoke_user_sessions(old_name, keep_sid=keep_sid)
    return True, ''


# ===================================================
# QR Code / Session API
# ===================================================

def create_qr_session(username, role, expiry_days=None, expiry_minutes=None):
    if expiry_minutes is not None: ttl = expiry_minutes * 60
    elif expiry_days is not None: ttl = expiry_days * 86400
    else: ttl = SESSION_EXPIRY_DAYS * 86400
    sid = secrets.token_hex(24)
    with _sessions_lock:
        _sessions[sid] = {'expiry': time.time() + ttl, 'username': username, 'role': role}
        _save_sessions_locked()      # 待扫码状态也要落盘：重启后二维码仍可扫
    return sid

def consume_qr_session(sid, client_ip=''):
    with _sessions_lock:
        info = _sessions.get(sid)
        if info and info['expiry'] > time.time():
            role, username = info['role'], info['username']
            del _sessions[sid]
            new_sid = secrets.token_hex(24)
            # 记录来源二维码 sid，用于区分“已扫码登录成功”与“过期/被撤销”
            entry = {'expiry': time.time() + SESSION_EXPIRY_DAYS * 86400,
                     'username': username, 'role': role, '_qr_sid': sid}
            if client_ip:
                entry['ip'] = client_ip
            _sessions[new_sid] = entry
            _save_sessions_locked()  # 扫码登录出来的会话同样要落盘
            return new_sid, username, role
    return None

def qr_login_status(sid):
    """查询二维码登录状态：pending(等待扫码) / consumed(已扫码登录) / expired(过期或无效)"""
    with _sessions_lock:
        if sid in _sessions:
            return 'pending'
        for info in _sessions.values():
            if info.get('_qr_sid') == sid:
                return 'consumed'
        return 'expired'

# 登录页 UI 已按架构独立迁移到 web_page/login/login.html（由 ac_auth.serve_login_page 读取渲染，不再内嵌于本模块）

# ========== Session Cleanup ==========
_cleanup_timer_started = False

def _session_cleanup_worker():
    while True:
        time.sleep(60)
        try: _clean_sessions()
        except: pass

def start_session_cleanup():
    global _cleanup_timer_started
    if not _cleanup_timer_started:
        _cleanup_timer_started = True
        t = threading.Thread(target=_session_cleanup_worker, daemon=True)
        t.start()