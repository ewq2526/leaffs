# -*- coding: utf-8 -*-
"""分享访问保护：分享码 + 防爆破（IP 锁 + 时间密度全锁）。

规则：
  * **码的粒度是"每条分享"**：share 的每条映射、每个挂载点各有自己的码。
    索引键是**随机标签**（`share/mappings.py` 登记条目时生成），**不是名字** ——
    名字会变（改名 / 移动）、会撞（不同命名空间同名）、还能枚举，随机标签三样都不沾。
  * 码**存明文**：它本来就是"告诉别人"的东西（转发、抄纸上），不是秘密口令；
    而且**码要能被本人读回** —— 哈希存法读不回，等于设完就丢。
    只有本人与管理员能读（`read_code` 里判）。
  * IP 锁：同一 IP 对**那条分享**当日输错 ≥ 5 次 → 锁该 IP 访问该条（当日）。
  * 全局锁（时间密度）：任意 60 秒窗口内该条错误总量 ≥ 5（且来自 ≥2 个 IP）→
    该条全锁 30 分钟（持续被刷自动顺延），谁输都拒。
  * 锁与计数按自然日重置；本人可一键重置（清计数 + 解除两类锁）。
  * 已授权（1 小时 Cookie）的访客不受锁定影响 —— 锁定只拦"输码"环节。
  * guest 无权设置分享码。

⚠️ **旧数据**：老版本是"每个用户名一个码"（`users` 表）。它在加载时仍会被读入，
并**在启动时迁移**（`migrate_user_codes`）：把该用户名的码哈希复制到它每条分享的标签上，
所以**旧码继续有效**；但它只有哈希、回显不出明文，界面上显示"重设一次才能看到"。
迁移之后只有"按标签判"这一条路，不留双份逻辑。

票据：一张票据可以**累积多条分享**的解锁（`labels` 列表）—— 一条码一个 Cookie 会把
请求头撑爆，所以 Cookie 名是固定的 `leaf_sh_tk`，值是不透明随机票据，服务端记归属。
⚠️ 票据**不能**是"码的哈希"：那个值可以由码推算出来，攻击者离线枚举候选码、算出哈希后
自己写一个同名 Cookie 就能通过，全程不经过输码接口，IP 锁与全局锁一点都拦不到。

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
# `_CACHE` 只是进程内单例，不能当成唯一事实源。
_CACHE_STAMP = None

# 授权票据的 Cookie 名（固定一个）：票据里装"已解锁哪几条分享"
SHARE_COOKIE = 'leaf_sh_tk'


def _file_stamp():
    """`_ACCESS_FILE` 的指纹；文件不存在 → None。

    用 `(st_mtime_ns, st_size)`：mtime 在部分文件系统上只有秒级精度，同一秒内的两次写入
    会看不出来，叠上 size 再稳一层。
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
    """加载分享访问数据 —— **按文件指纹决定要不要重载**。

    原来是"进程内加载一次就用到死"。问题：`_CACHE` 是**进程内**单例，而这份 JSON 会被
    **本进程之外**的东西改 —— 同一数据根跑两个实例、外部备份还原、手工编辑。那种写法下
    本进程会一直用旧值（在 A 处设了码、B 处看不到），而且不会自愈，只有重启才更新。

    指纹没变时仍然只是一次 `os.stat`（微秒级）就返回 —— 相比码比对的开销可以忽略。
    """
    global _CACHE, _CACHE_STAMP
    stamp = _file_stamp()
    if _CACHE is not None and stamp == _CACHE_STAMP:
        return
    _CACHE = {'date': _today(), 'items': {}, 'tickets': {}, 'users': {}}
    try:
        with open(_ACCESS_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for key, field in (('items', 'items'), ('tickets', 'tickets'), ('users', 'users')):
                v = raw.get(key)
                if isinstance(v, dict):
                    _CACHE[field] = v
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


def _item(label, owner=''):
    """取（或建）某条分享的记录：码 + 计数 + 两类锁 + 事件。

    `owner` 只用于两件事：分享者本人免输码、以及删除用户时的级联清理；
    挂载点没有属主（它是服务器的东西），传空即可。
    """
    it = _CACHE['items'].setdefault(label, {})
    it.setdefault('code', '')            # 明文码；'' = 未设码
    it.setdefault('code_hash', '')       # 仅迁移来的旧码用（那种回显不出明文）
    if owner:
        it.setdefault('owner', owner)
    it.setdefault('ip_errors', {})
    it.setdefault('ip_locked', {})
    it.setdefault('window', [])
    it.setdefault('window_ips', {})
    it.setdefault('global_lock_until', 0.0)
    it.setdefault('attack_ips', [])
    it.setdefault('last_events', [])
    return it


def _flush_day_locked():
    """自然日翻转：重置**按天计**的防爆破计数与锁；跨日仍应有效的状态一个都不动。

    ⚠️ **`global_lock_until` 刻意不在这里清**：它是一个**绝对时间戳**（`now + 30 分钟`），
    自带到期判定，根本不是"按天"的东西。清它会让**攻击者触发的全锁跨过零点自动解除** ——
    23:59 触发、实际只锁 1 分钟。
    """
    if _CACHE['date'] != _today():
        _CACHE['date'] = _today()
        for it in _CACHE['items'].values():
            if not isinstance(it, dict):
                continue
            it['ip_errors'] = {}
            # ip_locked 必须一起清：access_blocked 判"该 IP 当日锁"只认它，
            # 漏清的话某个 IP 错满 5 次就永久锁死（过天、重启都不解，NAT 后的别人被连坐）
            it['ip_locked'] = {}
            it['window'] = []
            it['window_ips'] = {}
            it['attack_ips'] = []
            it['last_events'] = []
        # 跨日重置要**落盘**：加了"按指纹重载"之后，只改内存是不够的 ——
        # 别的进程一改文件就会触发本进程重载，把这次重置连同新的 date 一起冲掉
        _save_locked()


# ---------------- 分享码 ----------------

_LEGACY_CODE_SALT = 'leaffs-share:'      # 仅用于校验/迁移旧哈希，不再用于新码


def code_hash(code):
    """分享码哈希：`<iterations>$<salt_b64>$<hash_b64>`（与 `auth/core` 的口令同格式同参数）。

    只用于**迁移来的旧码**校验与升级 —— 新码一律存明文（明文才能被本人读回）。
    """
    salt = os.urandom(SALT_LENGTH)
    dk = hashlib.pbkdf2_hmac('sha256', code.encode('utf-8'), salt, PBKDF2_ITERATIONS)
    return '%d$%s$%s' % (PBKDF2_ITERATIONS,
                         base64.b64encode(salt).decode('ascii'),
                         base64.b64encode(dk).decode('ascii'))


def _legacy_code_hash(code):
    """更旧的算法（固定盐单轮）—— **只留给迁移**：已有分享码必须还能校验通过。"""
    return hashlib.sha256((_LEGACY_CODE_SALT + code).encode('utf-8')).hexdigest()


def _code_matches(code, stored):
    """按存储格式校验哈希，返回 (是否通过, 是否属于旧格式)。

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


def set_code(label, code, owner='', who=''):
    """给**某条分享**设/换码；code 为空串清除。返回 (ok, err)。

    码存明文 —— 本人要能读回自己的码。清除时连迁移来的旧哈希一起清掉，
    否则"清了码还要求输码"。
    """
    if not label:
        return False, '缺少条目'
    _load_params()
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        it = _item(label, owner)
        it['code'] = code or ''
        it['code_hash'] = ''            # 设了新码（或清了码）就不再认旧哈希
        if owner:
            it['owner'] = owner
        _save_locked()
    add_log('分享码已%s%s' % ('设置' if code else '清除',
                              ' [%s]' % who if who else ''), 'info')
    return True, None


def code_enabled(label):
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        it = _CACHE['items'].get(label)
        return bool(it and (it.get('code') or it.get('code_hash')))


def read_code(label, username='', owner='', is_admin=False):
    """把码**读回来**（明文）。

    只有**分享者本人与管理员**能读 —— 访客当然不行。迁移来的旧码只有哈希、
    回显不出明文，这里返回 `legacy=True` 让界面提示"重设一次才能看到"。
    返回 `{'code': 明文或 '', 'legacy': 是否旧码不可回显, 'enabled': 是否设了码}`。
    """
    if not label:
        return {'code': '', 'legacy': False, 'enabled': False}
    if not is_admin and not (username and owner and username == owner):
        return {'code': '', 'legacy': False, 'enabled': False}
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        it = _CACHE['items'].get(label) or {}
        plain = it.get('code') or ''
        hashed = it.get('code_hash') or ''
        return {'code': plain, 'legacy': bool(hashed and not plain),
                'enabled': bool(plain or hashed)}


def verify_code(label, code):
    """比对（不记账）。明文优先；迁移来的旧哈希按老规矩校验，**通过时顺手升级成明文**。

    锁的用法要注意：`_LOCK` 是普通 Lock（不可重入），而 PBKDF2 一次约 0.3 秒 ——
    **不能在持锁时计算**（会阻塞其它请求）。所以：锁内取值 → 出锁比对 → 需要升级时重新进锁。
    """
    if not label or not code:
        return False
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        it = _CACHE['items'].get(label) or {}
        plain = it.get('code') or ''
        want = it.get('code_hash') or ''
    if plain:
        return secrets.compare_digest(code, plain)
    if not want:
        return False
    ok, _is_legacy = _code_matches(code, want)
    if ok:
        # 校验通过才写：错误码绝不触发任何写入（否则就给了"用错误码探测"的写副作用）。
        # 写前复核并发下这条有没有被改过，别拿旧值覆盖新值。
        try:
            with _LOCK:
                _load_locked()
                cur = _CACHE['items'].get(label) or {}
                if (cur.get('code_hash') or '') == want and not (cur.get('code') or ''):
                    cur['code'] = code
                    cur['code_hash'] = ''
                    _save_locked()
                    add_log('分享码已升级为明文存储（旧的哈希码自动迁移）', 'info')
        except Exception as e:
            add_log('分享码升级失败（不影响本次校验结果）: %s' % e, 'warn')
    return ok


def migrate_user_codes(pairs):
    """把老的"按用户名一个码"迁移到新的"每条分享一个码"。

    `pairs` = `[(label, owner), ...]`（由 `share/mappings.py` 在启动时给出：它会遍历
    现有条目，把每条映射/挂载点的标签与属主报过来）。

    对每个 label：新表里还没有它、而老表里该 owner 有码 → 把**哈希照搬**过去
    （不还原明文，也还原不了），于是**旧码继续有效**，只是界面回显不出明文。
    幂等：跑多少次结果一样。返回迁移了几条。
    """
    if not pairs:
        return 0
    moved = 0
    with _LOCK:
        _load_locked()
        users = _CACHE.get('users') or {}
        if not users:
            return 0
        for label, owner in pairs:
            if not label or not owner:
                continue
            if label in _CACHE['items']:
                continue
            src = users.get(owner) or {}
            h = src.get('code_hash') or ''
            if not h:
                continue
            it = _item(label, owner)
            it['code'] = ''
            it['code_hash'] = h
            moved += 1
        if moved:
            _CACHE['users'] = {}          # 迁移过一次就够了，老表清掉，免得两处都判
            _save_locked()
    if moved:
        add_log('已把 %d 条"按用户名的分享码"迁移到按条目的码上' % moved, 'info')
    return moved


def forget(label):
    """条目被移除时，把它的码/计数/锁/票据一起清掉。

    不清的话就是一堆指向"已经不存在的分享"的残留：表越来越大，
    而且那条随机标签万一被复用（不会，但不必赌）就会继承旧码。
    返回是否真的删掉了东西。
    """
    if not label:
        return False
    with _LOCK:
        _load_locked()
        had = _CACHE['items'].pop(label, None) is not None
        tbl = _CACHE.get('tickets') or {}
        touched = False
        for k in list(tbl):
            v = tbl.get(k)
            if not isinstance(v, dict):
                continue
            labels = v.get('labels') or []
            if label in labels:
                labels = [x for x in labels if x != label]
                if labels:
                    v['labels'] = labels
                else:
                    tbl.pop(k, None)
                touched = True
        if had or touched:
            _save_locked()
    return had


def purge_owner(owner):
    """删除该属主的**全部**记录：码、错误计数、两类锁、授权票据。用于删除用户时的级联。

    不清理的话，重建同名账号会继承旧的码（→ 新用户的分享页仍要求输码，而没人知道那个码）
    与锁定状态；旧票据也会继续有效。返回是否真的删掉了东西。
    """
    if not owner:
        return False
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        mine = [k for k, v in _CACHE['items'].items()
                if isinstance(v, dict) and v.get('owner') == owner]
        for k in mine:
            _CACHE['items'].pop(k, None)
        had_user = (_CACHE.get('users') or {}).pop(owner, None) is not None
        # 票据里存的是**标签**（不存属主），所以要先拿到"这些标签"，再按标签回收票据 ——
        # 直接拿 owner 去比 labels 是永远匹配不上的（那等于一条票据都没清）。
        tbl = _CACHE.get('tickets') or {}
        mine_labels = set(mine)
        tk = [k for k, v in tbl.items()
              if isinstance(v, dict) and mine_labels & set(v.get('labels') or [])]
        for k in tk:
            tbl.pop(k, None)
        if mine or had_user or tk:
            _save_locked()
    if mine or had_user or tk:
        add_log('已清理 %s 的分享访问记录（%d 条码/计数/锁 + %d 张票据）'
                % (owner, len(mine), len(tk)), 'info')
    return bool(mine or had_user or tk)


def on_success(label, ip):
    """验码成功后调用：清该 IP 对**这条分享**的当日错误计数（放行；时间密度窗口不变）"""
    if not label:
        return
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        it = _CACHE['items'].get(label)
        if it is not None:
            it.get('ip_errors', {}).pop(ip, None)
            _save_locked()


def _prune_tickets_locked(now):
    tbl = _CACHE.setdefault('tickets', {})
    for k in [k for k, v in tbl.items()
              if not isinstance(v, dict) or v.get('exp', 0) <= now]:
        tbl.pop(k, None)
    if len(tbl) >= _MAX_TICKETS:
        for k in sorted(tbl, key=lambda x: tbl[x].get('exp', 0))[:len(tbl) - _MAX_TICKETS + 1]:
            tbl.pop(k, None)
    return tbl


def issue_ticket(label, cookie_header=''):
    """把**这条分享**的解锁加进票据并发回票据串（不透明随机值；服务端记归属与过期时刻）。

    一张票据累积多条分享的解锁（`labels`）：一条码一个 Cookie 会把请求头撑爆。
    已带着一张有效票据时**复用它**（追加 label），否则新签一张。

    过期由服务端判定（`exp`），浏览器那个 Max-Age 只是顺手清 Cookie 用的。
    """
    if not label:
        return ''
    ttl = max(60.0, float(_P.get('cookie_hours') or 1.0) * 3600.0)
    got = parse_cookies(cookie_header).get(SHARE_COOKIE, '') if cookie_header else ''
    with _LOCK:
        _load_locked()
        now = time.time()
        tbl = _prune_tickets_locked(now)
        cur = tbl.get(got) if got else None
        if isinstance(cur, dict) and cur.get('exp', 0) > now:
            labels = list(cur.get('labels') or [])
            if label not in labels:
                labels.append(label)
            cur['labels'] = labels
            cur['exp'] = now + ttl
            _save_locked()
            return got
        ticket = secrets.token_urlsafe(24)
        tbl[ticket] = {'labels': [label], 'exp': now + ttl}
        _save_locked()
    return ticket


def is_authorized(label, cookie_header, ip=''):
    """这条分享是否已解锁（没设码恒 True）。带票据的请求顺带清该 IP 的错误计数。"""
    if not label:
        return True
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        it = _CACHE['items'].get(label)
        if not it or not (it.get('code') or it.get('code_hash')):
            return True          # 没设码 → 无需授权
        got = parse_cookies(cookie_header).get(SHARE_COOKIE, '')
        tbl = _CACHE.setdefault('tickets', {})
        tk = tbl.get(got) if got else None
        ok = bool(isinstance(tk, dict) and label in (tk.get('labels') or [])
                  and tk.get('exp', 0) > time.time())
        # 只清**已过期**的那一张（这张表会落盘，别让它无限长）。
        # ⚠️ 绝不能因为"票据有效、只是没覆盖正在问的这一条标签"就清掉它：一个 cookie 会
        # 连续问多条标签（分享页逐条遍历、列表的解锁回调、 WS 列表都是这样），清掉的后果是
        # "解锁了 A，只要打开一个含另一条设码分享的列表页，A 的解锁就没了、得重新输码"。
        expired = isinstance(tk, dict) and tk.get('exp', 0) <= time.time()
        if got and expired and tbl.pop(got, None) is not None:
            _save_locked()
    # access_blocked / on_success 各自加锁，必须在 _LOCK 之外调用（否则自锁）
    if access_blocked(label, ip)[0]:
        return False
    if ok:
        on_success(label, ip)
        return True
    return False


def code_gate(label, owner, cookie_header, client_ip, username=''):
    """分享码判定：**"谁算已解锁"只有这一份实现**（HTTP 与 WS 都调它）。

    规则：没设码 = 恒解锁；分享者本人 = 解锁；其余要有效的 1h 授权票据 Cookie。
      · label     —— 这条分享的随机标签（`share/mappings.py` 登记时生成）；
                    空 = 这个路径不属于任何登记条目 → 恒放行
      · owner     —— 分享的属主（挂载点没有属主，传空）；用于"本人免输码"
      · username  —— **当前请求者是谁**（HTTP 从会话取、WS 从连接角色取）；空 = 匿名
      · client_ip —— 防爆破锁按来源 IP 记

    为什么要有这个"不带 handler"的版本：**WS 的列表也要按分享码过滤**
    （不过滤等于把设了码的分享白送给任何连得上 WS 的人），而 WS 没有 handler。
    与其在 `ws.py` 里照抄一遍这个判定，不如把判定收到这里 —— 两份实现迟早漂移。
    """
    if not label or not code_enabled(label):
        return True
    if username and owner and owner == username:
        return True
    return is_authorized(label, cookie_header, client_ip)


def access_blocked(label, ip):
    """输码前拦截判定：全局锁中 / 该 IP 当日锁。返回 (blocked, reason)"""
    if not label:
        return (False, '')
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        it = _CACHE['items'].get(label)
        if not it:
            return (False, '')
        now = time.time()
        if it.get('global_lock_until', 0) > now:
            return (True, 'global')
        if (it.get('ip_locked') or {}).get(ip):
            return (True, 'ip')
    return (False, '')


def record_failure(label, ip):
    """记录一次输错，返回事件字典：{blocked_new, reason, remaining, global_now, attack}"""
    if not label:
        return {'blocked_new': False, 'reason': '', 'remaining': 0,
                'global_now': False, 'attack': False}
    _load_params()
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        it = _item(label)
        now = time.time()
        ev = {'blocked_new': False, 'reason': '', 'remaining': 0,
              'global_now': False, 'attack': False}
        # 1) 滑动窗口时间密度：先收窗
        win = _P['global_window_secs']
        it['window'] = [t for t in it.get('window', []) if t > now - win]
        it['window'].append(now)
        # 窗口内出现过的来源 IP（跟时间窗一起过期）
        wips = {k: v for k, v in (it.get('window_ips') or {}).items() if v > now - win}
        wips[ip] = now
        it['window_ips'] = wips
        # 2) 全局锁判定：时间密度达阈值、**且来自 ≥2 个不同 IP** 才算"遭攻击"。
        #    单机反复试由 IP 锁兜住就够了（每 IP 每日 5 次）；不加这条 IP 限制的话，
        #    任何匿名者 5 次错码就能把任意分享对所有人锁死 30 分钟，每 30 分钟再来一次
        #    还能无限顺延 —— 拿来做 DoS 正好。
        if len(it['window']) >= _P['global_err_max_window'] and len(wips) >= 2:
            until = now + _P['global_lock_secs']
            if it.get('global_lock_until', 0) < until:
                it['global_lock_until'] = until
                ev['global_now'] = True
                ev['reason'] = 'global'
                ev['blocked_new'] = True
            it['window'] = []
        # 3) IP 锁（当日累计）
        ip_errors = it.setdefault('ip_errors', {})
        ip_locks = it.setdefault('ip_locked', {})
        ip_errors[ip] = ip_errors.get(ip, 0) + 1
        cnt = ip_errors[ip]
        ev['remaining'] = max(0, int(_P['ip_err_max_day']) - cnt)
        if not ev['blocked_new'] and cnt >= _P['ip_err_max_day']:
            ip_locks[ip] = True
            ev['blocked_new'] = True
            ev['reason'] = 'ip'
        # 攻击来源记录（供"我的"页提醒展示；只留最近 20 个不同 IP）
        srcs = it.setdefault('attack_ips', [])
        if ip not in srcs:
            srcs.insert(0, ip)
        it['attack_ips'] = srcs[:20]
        # 最近事件（reason → attack 级别）供提醒判断
        events = it.setdefault('last_events', [])
        events.append({'t': now, 'ip': ip, 'kind': ev['reason'] or 'fail'})
        it['last_events'] = events[-20:]
        ev['attack'] = bool(ev['global_now']) or ev['reason'] in ('global', 'ip')
        _save_locked()
    return ev


def clear_attempts(owner, who=''):
    """本人重置：清该属主名下**所有条目**的当日计数/IP 锁/全局锁/事件（码本身保留）。

    按属主（而不是按单条）重置，是因为界面上的"重置"按钮就在"我的分享"那一页上，
    用户的预期是"把我这边的计数清了"。
    """
    if not owner:
        return False
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        n = 0
        for it in _CACHE['items'].values():
            if not isinstance(it, dict) or it.get('owner') != owner:
                continue
            it['ip_errors'] = {}
            it['ip_locked'] = {}
            it['window'] = []
            it['window_ips'] = {}
            it['global_lock_until'] = 0
            it['last_events'] = []
            n += 1
        if n:
            _save_locked()
    add_log('分享防爆破计数已重置%s' % (' [%s]' % who if who else ''), 'info')
    return True


def status(owner):
    """该属主名下所有分享的汇总状态：有几条设了码、锁了多少、当日失败、攻击来源。

    逐条的锁定状态另有 `item_status(label)`，界面按条显示。
    """
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        mine = [it for it in _CACHE['items'].values()
                if isinstance(it, dict) and it.get('owner') == owner]
        now = time.time()
        failed = sum(sum((it.get('ip_errors') or {}).values()) for it in mine)
        ips, recent, locked_until = [], [], 0.0
        for it in mine:
            ips.extend(it.get('attack_ips') or [])
            recent.extend(e for e in (it.get('last_events') or [])
                          if e.get('kind') in ('ip', 'global'))
            locked_until = max(locked_until, float(it.get('global_lock_until') or 0))
        ip_locked = sum(len(it.get('ip_locked') or {}) for it in mine)
        return {
            'code_enabled': any(bool(it.get('code') or it.get('code_hash')) for it in mine),
            'coded_count': sum(1 for it in mine if it.get('code') or it.get('code_hash')),
            'ip_locked': ip_locked,
            'global_locked_until': locked_until,
            'global_active': locked_until > now,
            'failed_today': failed,
            'attack_ips': list(dict.fromkeys(ips))[:10],
            'recent': recent[-3:],
        }


def item_status(label):
    """单条分享的锁定状态（界面按条显示用）"""
    with _LOCK:
        _load_locked()
        _flush_day_locked()
        it = _CACHE['items'].get(label) or {}
        now = time.time()
        until = float(it.get('global_lock_until') or 0)
        return {'code_enabled': bool(it.get('code') or it.get('code_hash')),
                'ip_locked': len(it.get('ip_locked') or {}),
                'global_locked_until': until,
                'global_active': until > now}


def param(name):
    return _P.get(name)
