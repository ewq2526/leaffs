# -*- coding: utf-8 -*-
"""分享访问保护：分享码 + 防爆破（IP 锁 + 时间密度全锁）。

规则（用户确认）：
  * 分享码（可选）：登录用户为自己的分享设置访问码（码只存 SHA-256，不存明文）。
    设置后访客访问 /p/<用户名> 需输码，正确后下发 1 小时授权 Cookie
    （leaf_sh_<用户名> = **服务端签发的不透明随机票据**，见 issue_ticket）。
    ⚠️ 票据**不能**是"码的哈希"：那个值可以由码推算出来 —— 攻击者离线枚举候选码、
    算出哈希后自己写一个同名 Cookie 就能下载，全程不经过输码接口，
    IP 锁与全局锁一点都拦不到。随机票据没有这个入口。
  * IP 锁：同一 IP 对某分享页当天（自然日）输错 ≥ 5 次 → 锁该 IP 访问该页（当日），
    其他 IP 不受影响。
  * 全局锁（时间密度）：任意 60 秒窗口内该分享页错误总量 ≥ 5（不区分 IP）→
    该分享页全锁 30 分钟（持续被刷自动顺延），谁输都拒。
  * 锁与计数按自然日重置；本人可一键重置（清计数 + 解除两类锁）。
  * 已授权（1 小时 Cookie）的访客不受锁定影响 —— 锁定只拦截“输码”环节。
  * guest 无权设置分享码（游客没有自己的分享管理权）。

阈值/时长从 server_config.json 可选键读取（不存在用默认），便于调参：
  share_code_cookie_hours / share_ip_err_max_day / share_ip_lock_to_day_end /
  share_global_window_secs / share_global_err_max_window / share_global_lock_secs
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from leaffs.auth.core import PBKDF2_ITERATIONS, SALT_LENGTH   # 与登录口令同一套参数
from leaffs.paths import CONFIG_DIR
from leaffs.utils.core import parse_cookies
from leaffs.runtime_log import add_log

_ACCESS_FILE = os.path.join(CONFIG_DIR, 'share_access.json')
_LOCK = threading.Lock()
_CACHE = None          # 结构见 _load_locked
# 上次读到/写出时 `_ACCESS_FILE` 的指纹（见 `_file_stamp`）。用途：判断**别的进程**
# （同一数据根跑两个实例）或**外部编辑**有没有改过这份 JSON ——
# `_CACHE` 只是进程内单例，不能当成唯一事实源（`issues.md` §二 第 12 条）。
_CACHE_STAMP = None


def _file_stamp():
    """`_ACCESS_FILE` 的指纹；文件不存在 → None。

    用 `(st_mtime_ns, st_size)`：mtime 在部分文件系统上只有秒级精度，同一秒内的两次写入
    会看不出来，叠上 size 再稳一层；纳秒级 mtime 已足够区分。
    **刻意不用 inode**：本模块的写入是"写 `.tmp` → `os.replace`"，每次替换 inode 都变，
    拿它当指纹会退化成"每次都重载"。
    """
    try:
        st = os.stat(_ACCESS_FILE)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None

# 授权票据表上限（票据自带 TTL，默认 1 小时；超出时丢最早过期的那些）。
# 设上限是为了防"反复输对码"把这张会落盘的表撑大。
_MAX_TICKETS = 1000

# ---- 参数（可用 server_config.json 同名键覆盖）----
_P = {'cookie_hours': 1.0, 'ip_err_max_day': 5, 'global_window_secs': 60.0,
      'global_err_max_window': 5, 'global_lock_secs': 1800.0}


def _load_params():
    try:
        with open(os.path.join(CONFIG_DIR, 'server_config.json'), 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        if isinstance(cfg, dict):
            mapping = {
                'share_code_cookie_hours': 'cookie_hours',
                'share_ip_err_max_day': 'ip_err_max_day',
                'share_global_window_secs': 'global_window_secs',
                'share_global_err_max_window': 'global_err_max_window',
                'share_global_lock_secs': 'global_lock_secs',
            }
            for k, pk in mapping.items():
                v = cfg.get(k)
                if isinstance(v, (int, float)) and v > 0:
                    _P[pk] = float(v)
    except Exception:
        pass


def _today():
    return time.strftime('%Y-%m-%d')


def _load_locked():
    """加载分享访问数据 —— **按文件指纹决定要不要重载**（`issues.md` §二 第 12 条）。

    原来是 `if _CACHE is not None: return`，也就是"进程内加载一次就用到死"。
    问题：`_CACHE` 是**进程内**单例，而这份 JSON 会被**本进程之外**的东西改 ——
    同一数据根跑两个实例、外部备份还原、手工编辑。那种写法下本进程会**一直用旧值**
    （在 A 处设了码、B 处看不到；或在 A 处清了码、B 处还要求输码），
    而且**不会自愈，只有重启才更新**。

    指纹没变时仍然只是一次 `os.stat`（微秒级）就返回 —— 相比校验分享码本身要做的
    PBKDF2，这点开销可以忽略。
    """
    global _CACHE, _CACHE_STAMP
    stamp = _file_stamp()
    if _CACHE is not None and stamp == _CACHE_STAMP:
        return
    _CACHE = {'date': _today(), 'users': {}, 'tickets': {}}
    try:
        with open(_ACCESS_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            tk = raw.get('tickets')
            if isinstance(tk, dict):
                _CACHE['tickets'] = tk
            # 分享码是长期设置、不分自然日：跨天后也要加载回来，否则就是"过一天码就没了"。
            # 计数/锁的按天归零交给 _flush_day_locked —— 它靠这里记下的 date 判断翻了天没有。
            us = raw.get('users')
            if isinstance(us, dict):
                _CACHE['users'] = us
            d = raw.get('date')
            if isinstance(d, str) and d:
                _CACHE['date'] = d
    except FileNotFoundError:
        pass
    except Exception as e:
        # ⚠️ 读失败时**故意不更新指纹**：否则一次瞬时读错（正好撞上别人 os.replace 的空隙）
        # 就会被记成"已加载"，要等文件再变才重试。保持旧指纹 ⇒ 下次调用自然会再试一次。
        add_log('分享访问保护加载失败: %s' % e, 'warn')
        return
    _CACHE_STAMP = stamp


def _save_locked():
    global _CACHE_STAMP
    try:
        tmp = _ACCESS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_CACHE, f, ensure_ascii=False)
        os.replace(tmp, _ACCESS_FILE)      # 原子替换：写到一半崩了也不会留下半截 JSON
        # 自己写完就把指纹对齐 —— 否则下一次 `_load_locked` 会以为自己过期、白重载一遍
        _CACHE_STAMP = _file_stamp()
    except Exception as e:
        # 写失败**不动指纹**：保持旧值 ⇒ 下次 `_load_locked` 会去读盘（拿到别人的最新值）
        add_log('分享访问保护保存失败: %s' % e, 'err')


def _user(u):
    return _CACHE['users'].setdefault(u, {
        'code_hash': '', 'ip_errors': {}, 'ip_locked': {}, 'window': [],
        'window_ips': {}, 'global_lock_until': 0.0, 'attack_ips': [], 'last_events': [],
    })


def _flush_day_locked():
    """自然日翻转：重置**按天计**的防爆破计数与锁；跨日仍应有效的状态一个都不动。

    这里原来是 `_CACHE['users'] = {}` —— 把整张表清空，连分享码一起清掉了，
    表现为"过了一天，访问码就再也输不对"（实际是码已经没了）。

    ⚠️ **`global_lock_until` 刻意不在这里清**（原文是 `u['global_lock_until'] = 0.0`，
    `issues.md` §二 第 7 条）：它是一个**绝对时间戳**（`now + 30 分钟`），
    自带到期判定，根本不是"按天"的东西。清它会让**攻击者触发的全锁跨过零点自动解除** ——
    23:59 触发、实际只锁 1 分钟。判定在 `access_blocked`：`global_lock_until > now`。
    """
    if _CACHE['date'] != _today():
        _CACHE['date'] = _today()
        for u in _CACHE['users'].values():
            if not isinstance(u, dict):
                continue
            u['ip_errors'] = {}
            # ip_locked 必须一起清：access_blocked 判"该 IP 当日锁"只认它，
            # 漏清的话某个 IP 错满 5 次就永久锁死（过天、重启都不解，NAT 后的别人被连坐）
            u['ip_locked'] = {}
            u['window'] = []
            u['window_ips'] = {}
            u['attack_ips'] = []
            u['last_events'] = []
        # 跨日重置要**落盘**：加了"按指纹重载"之后，只改内存是不够的 ——
        # 别的进程一改文件就会触发本进程重载，把这次重置连同新的 date 一起冲掉
        # （重置本身幂等、不出错，但盘上会一直停在旧日期，语义不干净）。
        _save_locked()


# ---------------- 分享码 ----------------

_LEGACY_CODE_SALT = 'leaffs-share:'      # 仅用于校验/迁移旧哈希，不再用于新码


def code_hash(code):
    """分享码哈希：`<iterations>$<salt_b64>$<hash_b64>`（与 `auth/core` 的口令同格式同参数）。

    **为什么不是原来的 sha256('leaffs-share:' + code)**：那是**固定盐、单轮**——
    同一个码两次哈希完全相同，而分享码下限只有 6 位 → 一旦 `share_access.json` 泄露
    （备份、本地接触、任意文件读取），在线侧的 IP 锁与全局锁**全部归零**（离线秒破）。
    现在每用户每设一次码就重新生成 32 字节随机盐。
    """
    salt = os.urandom(SALT_LENGTH)
    dk = hashlib.pbkdf2_hmac('sha256', code.encode('utf-8'), salt, PBKDF2_ITERATIONS)
    return '%d$%s$%s' % (PBKDF2_ITERATIONS,
                         base64.b64encode(salt).decode('ascii'),
                         base64.b64encode(dk).decode('ascii'))


def _legacy_code_hash(code):
    """旧算法（固定盐单轮）—— **只留给迁移**：已有分享码必须还能校验通过。"""
    return hashlib.sha256((_LEGACY_CODE_SALT + code).encode('utf-8')).hexdigest()


def _code_matches(code, stored):
    """按存储格式校验，返回 (是否通过, 是否属于旧格式)。

    区分方式：新格式是 `iters$salt$hash`（含 `$`），旧格式是 64 位 hex。
    """
    if '$' in stored:
        try:
            iters_s, salt_b64, hash_b64 = stored.split('$', 2)
            dk = hashlib.pbkdf2_hmac('sha256', code.encode('utf-8'),
                                     base64.b64decode(salt_b64), int(iters_s))
            return hmac.compare_digest(dk, base64.b64decode(hash_b64)), False
        except Exception:
            return False, False
    return secrets.compare_digest(_legacy_code_hash(code), stored), True


def set_code(username, code, who=''):
    """设置/更换分享码；code 为空串清除。返回 (ok, err)。"""
    if not username:
        return False, '缺少用户名'
    _load_params()
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        u = _user(username)
        if code:
            u['code_hash'] = code_hash(code)
        else:
            u['code_hash'] = ''
        _save_locked()
    add_log('分享码已%s%s' % ('设置' if code else '清除',
                              ' [%s]' % who if who else ''), 'info')
    return True, None


def code_enabled(username):
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        u = _CACHE['users'].get(username)
        return bool(u and u.get('code_hash'))


def param(name):
    return _P.get(name)


def verify_code(username, code):
    """纯比对（不记账）；**发现旧格式且校验通过时顺手升级**为新格式。

    锁的用法要注意：`_LOCK` 是普通 Lock（不可重入），而 PBKDF2 一次约 0.3 秒 ——
    **不能在持锁时计算**（会阻塞其它请求）。所以：锁内取值 → 出锁比对 → 需要升级时重新进锁。
    升级只在**校验通过**时发生：错误码绝不触发任何写入（否则就给了"用错误码探测"的写副作用）。
    """
    if not username or not code:
        return False
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        u = _CACHE['users'].get(username)
        if not u or not u.get('code_hash'):
            return False
        want = u['code_hash']
    ok, is_legacy = _code_matches(code, want)
    if ok and is_legacy:
        try:
            with _LOCK:
                _load_locked()
                cur = (_CACHE['users'].get(username) or {}).get('code_hash')
                # 写前复核：并发下用户可能刚换过码，那就别拿旧值覆盖新值
                if cur == want:
                    _CACHE['users'][username]['code_hash'] = code_hash(code)
                    _save_locked()
                    add_log('分享码哈希已升级为 PBKDF2（旧格式自动迁移）', 'info')
        except Exception as e:
            add_log('分享码哈希升级失败（不影响本次校验结果）: %s' % e, 'warn')
    return ok


def purge_user(username):
    """删除该用户在 `share_access.json` 里的**全部**记录：分享码、错误计数、两类锁、授权票据。

    用于**删除用户时的级联清理**（T-1）：不清理的话，重建同名账号会继承旧的
    `code_hash`（→ 新用户的分享页仍要求输码，而没人知道那个码）与锁定状态；
    旧票据也会继续对该用户名有效。返回是否真的删掉了东西。
    """
    if not username:
        return False
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        had = _CACHE['users'].pop(username, None) is not None
        tbl = _CACHE.get('tickets') or {}
        mine = [k for k, v in tbl.items()
                if isinstance(v, dict) and v.get('user') == username]
        for k in mine:
            tbl.pop(k, None)
        if had or mine:
            _save_locked()
    if had or mine:
        add_log('已清理用户 %s 的分享访问记录（码/计数/锁/票据）' % username, 'info')
    return had or bool(mine)


def on_success(username, ip):
    """验码成功后调用：清该 IP 当日错误计数（放行；时间密度窗口不受单次成功影响）"""
    if not username:
        return
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        u = _CACHE['users'].get(username)
        if u is not None:
            u.get('ip_errors', {}).pop(ip, None)
            _save_locked()


def cookie_name(username):
    """Cookie 键：固定长度 ASCII（用户名可能是中文，不能直接进 header）"""
    return 'leaf_sh_' + hashlib.sha256(('cookie:' + username).encode('utf-8')).hexdigest()[:10]


def issue_ticket(username):
    """签发一张授权票据（不透明随机值；服务端记住归属与过期时刻）。

    过期由服务端判定（`exp`），浏览器那个 Max-Age 只是顺手清 Cookie 用的，
    不能当作唯一的有效期。返回票据串；username 为空返回 ''。
    """
    if not username:
        return ''
    ttl = max(60.0, float(_P.get('cookie_hours') or 1.0) * 3600.0)
    ticket = secrets.token_urlsafe(24)
    with _LOCK:
        _load_locked()
        now = time.time()
        tbl = _CACHE.setdefault('tickets', {})
        for k in [k for k, v in tbl.items()
                  if not isinstance(v, dict) or v.get('exp', 0) <= now]:
            tbl.pop(k, None)
        if len(tbl) >= _MAX_TICKETS:
            for k in sorted(tbl, key=lambda x: tbl[x].get('exp', 0))[:len(tbl) - _MAX_TICKETS + 1]:
                tbl.pop(k, None)
        tbl[ticket] = {'user': username, 'exp': now + ttl}
        _save_locked()
    return ticket


def _cookie_ok(username, cookie_header, ip=''):
    """请求 Cookie 里是否带着该用户的**有效授权票据**（见 issue_ticket）

    必须和 `/api/share/auth` 一样**先看锁**：被锁的 IP / 全局锁期间，带票据也不给过，
    否则锁形同虚设。这里**不记失败**：票据是 192 位随机值、猜不中；出现无效值几乎
    只意味着"过期了"，记账只会让带着过期 Cookie 的正常访客被锁死。
    """
    if not username:
        return False
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        u = _CACHE['users'].get(username)
        if not u or not u.get('code_hash'):
            return True          # 未设码 → 无需授权
        cname = cookie_name(username)
        # 解析口径全仓一份（utils/core.parse_cookies）：同名 Cookie 取最后一个
        got = parse_cookies(cookie_header).get(cname, '')
        tbl = _CACHE.setdefault('tickets', {})
        tk = tbl.get(got) if got else None
        ok = bool(isinstance(tk, dict) and tk.get('user') == username
                  and tk.get('exp', 0) > time.time())
        # 无效/过期票据顺手清掉（这张表会落盘，别让它无限长）
        if got and not ok and tbl.pop(got, None) is not None:
            _save_locked()
    # access_blocked / on_success 各自加锁，必须在 _LOCK 之外调用（否则自锁）
    if access_blocked(username, ip)[0]:
        return False
    if ok:
        on_success(username, ip)
        return True
    return False


def is_authorized(username, cookie_header, ip=''):
    """下载/预览/访客页数据访问入口：设码则校验 Cookie（无码恒 True）"""
    return _cookie_ok(username, cookie_header, ip)


def code_gate(owner, cookie_header, client_ip, username=''):
    """分享码判定：**"谁算已解锁"只有这一份实现**（HTTP 与 WS 都调它）。

    规则：没设码 = 恒解锁；分享者本人 = 解锁；其余要有效的 1h 授权票据 Cookie。
      · owner     —— 分享目录的属主（`public/shares/<owner>` 里那一段）
      · username  —— **当前请求者是谁**（HTTP 从会话取、WS 从连接角色取）；空 = 匿名
      · client_ip —— 防爆破锁按来源 IP 记（HTTP 用 client_address[0]，WS 用 remote_address[0]）

    为什么要有这个"不带 handler"的版本：**WS 的列表也要按分享码过滤**
    （不过滤等于把设了码的分享目录白送给任何连得上 WS 的人），而 WS 没有 handler。
    与其在 `ws.py` 里照抄一遍这个判定，不如把判定收到这里 —— 两份实现迟早漂移。
    """
    if not owner or not code_enabled(owner):
        return True
    if username and owner == username:
        return True
    return is_authorized(owner, cookie_header, client_ip)


def access_blocked(username, ip):
    """输码前拦截判定：全局锁中 / 该 IP 当日锁。返回 (blocked, reason)"""
    if not username:
        return (False, '')
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        u = _CACHE['users'].get(username)
        if not u:
            return (False, '')
        now = time.time()
        if u.get('global_lock_until', 0) > now:
            return (True, 'global')
        ip_locks = u.get('ip_locked', {})
        if ip_locks.get(ip):
            return (True, 'ip')
    return (False, '')


def record_failure(username, ip):
    """记录一次输错，返回事件字典：{blocked_new, reason, remaining, global_now, attack}"""
    _load_params()
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        u = _user(username)
        now = time.time()
        ev = {'blocked_new': False, 'reason': '', 'remaining': 0,
              'global_now': False, 'attack': False}
        # 1) 滑动窗口时间密度：先收窗
        win = _P['global_window_secs']
        u['window'] = [t for t in u.get('window', []) if t > now - win]
        u['window'].append(now)
        # 窗口内出现过的来源 IP（跟时间窗一起过期）
        wips = {k: v for k, v in (u.get('window_ips') or {}).items() if v > now - win}
        wips[ip] = now
        u['window_ips'] = wips
        # 2) 全局锁判定：时间密度达阈值、**且来自 ≥2 个不同 IP** 才算"遭攻击"。
        #    单机反复试由 IP 锁兜住就够了（每 IP 每日 5 次）；不加这条 IP 限制的话，
        #    任何匿名者 5 次错码就能把任意分享对所有人锁死 30 分钟，每 30 分钟再来一次
        #    还能无限顺延 —— 拿来做 DoS 正好。
        if len(u['window']) >= _P['global_err_max_window'] and len(wips) >= 2:
            until = now + _P['global_lock_secs']
            if u.get('global_lock_until', 0) < until:
                u['global_lock_until'] = until
                ev['global_now'] = True
                ev['reason'] = 'global'
                ev['blocked_new'] = True
            u['window'] = []
        # 3) IP 锁（当日累计）
        ip_errors = u.setdefault('ip_errors', {})
        ip_locks = u.setdefault('ip_locked', {})
        ip_errors[ip] = ip_errors.get(ip, 0) + 1
        cnt = ip_errors[ip]
        ev['remaining'] = max(0, int(_P['ip_err_max_day']) - cnt)
        if not ev['blocked_new'] and cnt >= _P['ip_err_max_day']:
            ip_locks[ip] = True
            ev['blocked_new'] = True
            ev['reason'] = 'ip'
        # 攻击来源记录（供“我的”页提醒展示；只留最近 20 个不同 IP）
        srcs = u.setdefault('attack_ips', [])
        if ip not in srcs:
            srcs.insert(0, ip)
        u['attack_ips'] = srcs[:20]
        # 最近事件（reason → attack 级别）供提醒判断
        events = u.setdefault('last_events', [])
        events.append({'t': now, 'ip': ip, 'kind': ev['reason'] or 'fail'})
        u['last_events'] = events[-20:]
        ev['attack'] = bool(ev['global_now']) or ev['reason'] in ('global', 'ip')
        _save_locked()
    return ev


def clear_attempts(username, who=''):
    """本人重置：清当日计数/IP 锁/全局锁/事件（分享码本身保留，可另行 set_code 更换）"""
    if not username:
        return False
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        u = _CACHE['users'].get(username)
        if u is None:
            return True
        u['ip_errors'] = {}
        u['ip_locked'] = {}
        u['window'] = []
        u['window_ips'] = {}
        u['global_lock_until'] = 0
        u['last_events'] = []
        _save_locked()
    add_log('分享防爆破计数已重置%s' % (' [%s]' % who if who else ''), 'info')
    return True


def status(username):
    """本人状态：码是否开启、IP 锁数、全锁到期、当日失败、攻击来源"""
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        u = _CACHE['users'].get(username)
        if u is None:
            return {'code_enabled': False, 'ip_locked': 0, 'global_locked_until': 0,
                    'failed_today': 0, 'attack_ips': [], 'recent': []}
        failed = sum(u.get('ip_errors', {}).values())
        now = time.time()
        recent = [e for e in u.get('last_events', []) if e.get('kind') in ('ip', 'global')]
        return {
            'code_enabled': bool(u.get('code_hash')),
            'ip_locked': len(u.get('ip_locked', {})),
            'global_locked_until': u.get('global_lock_until', 0),
            'global_active': u.get('global_lock_until', 0) > now,
            'failed_today': failed,
            'attack_ips': u.get('attack_ips', [])[:10],
            'recent': recent[-3:],
        }
