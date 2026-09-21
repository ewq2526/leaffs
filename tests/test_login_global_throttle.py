# -*- coding: utf-8 -*-
"""全局登录限速（第五层）+ 密码喷洒告警（2026-09-17）。

**要解决的问题**：现有四层限速全是"按维度收敛"的 —— (IP,用户名) 6 次/10 分钟、
用户名 12 次/15 分钟、游客 IP 10 次/60 秒、每 IP 30 次/60 秒。
攻击者**换 IP + 每个名字只试 1~2 次**就能同时躲开前两层，只剩"每 IP 30/分钟"
挡着，而那是可以靠换 IP 线性扩展的 ⇒ 总尝试速率和 PBKDF2 的 CPU 消耗都没有上限。

**用户拍板的口径**：「就是自保，打满了服务本来就会崩，还不如全局锁带提示」
⇒ 做**全局滑动窗口限速**（策略 A）：攻击进行中一直限，攻击停手后最多 60 秒自动恢复
（不做固定长锁 —— 那种既让攻击停手后还要白等，又能被"隔几分钟碰一下"无限续期）。

**环回豁免**：`127.0.0.1`/`::1` 永不拦 ⇒ 攻击者触发全局锁时，管理员仍能从本机进去处理。

**为什么是单元级**：从 `127.0.0.1` 起的请求本身就是环回、会被豁免，端到端测不到全局锁；
Windows 上也没有 `127.0.0.2`（环回网段只有 .1）。所以直接喂来源 IP 调限速函数，
需要验证"429 + 文案"时用一个最小的假 handler。
"""
import logging
import time

import pytest


@pytest.fixture()
def L():
    """拿到 login_api，并把全局/账号级状态清干净（只动本进程的模块状态）"""
    from leaffs.auth import login_api as L

    def _clean():
        lock = getattr(L, '_global_login_lock', None)
        if lock is not None:
            with lock:
                L._global_login_rate.clear()
        fl = getattr(L, '_login_fail_lock', None)
        if fl is not None:
            with fl:
                L._acct_fail.clear()
                L._login_fail.clear()
        if hasattr(L, '_global_login_warned_at'):
            L._global_login_warned_at = 0.0

    _clean()
    yield L
    _clean()


def _fill(L, ip='10.1.1.1', n=None):
    """把全局窗口喂到刚好满（返回用了多少次）"""
    n = L._GLOBAL_LOGIN_MAX if n is None else n
    for _ in range(n):
        assert L._login_global_allowed(ip) is True
    return n


# ---------- 1. 超阈值就拒 ----------

def test_blocks_after_threshold(L):
    """★ 非环回来源：窗口喂满之后，下一次必须被拒"""
    _fill(L)
    assert L._login_global_allowed('10.2.2.2') is False, \
        '窗口内已达 %d 次，全局层没有拦' % L._GLOBAL_LOGIN_MAX


# ---------- 2. ★ 环回豁免（"不把自己锁在外面"） ----------

def test_loopback_is_exempt_and_does_not_consume_quota(L):
    """★ 环回永远放行，而且**不占**非环回的额度"""
    for _ in range(L._GLOBAL_LOGIN_MAX * 2):
        assert L._login_global_allowed('127.0.0.1') is True
    assert L._login_global_allowed('::1') is True
    assert L._login_global_allowed('::ffff:127.0.0.1') is True, \
        'IPv4-mapped IPv6 形式的环回也要认'

    # 环回打了这么多，非环回的额度应当一点没动
    assert len(L._global_login_rate) == 0, \
        '环回请求被计入了全局窗口：%r' % (L._global_login_rate,)

    # 全局锁生效期间，环回照样进得来（这是"攻击者锁不住管理员"的证明）
    _fill(L)
    assert L._login_global_allowed('10.9.9.9') is False
    assert L._login_global_allowed('127.0.0.1') is True


# ---------- 3. ★ 窗口滑过自动恢复（不是长锁） ----------

def test_recovers_after_window_slides(L):
    """★ 把窗口内的记录推到窗口之外，必须立刻恢复放行"""
    _fill(L)
    assert L._login_global_allowed('10.1.1.1') is False

    with L._global_login_lock:
        L._global_login_rate[:] = [t - L._GLOBAL_LOGIN_WINDOW - 1
                                   for t in L._global_login_rate]
    assert L._login_global_allowed('10.1.1.1') is True, \
        '窗口滑过后没有恢复 —— 说明实现成了固定长锁'


# ---------- 4. 喷洒告警按"去重用户名"计 ----------

def test_spray_counts_distinct_usernames(L):
    """★ 同一个用户名失败再多次也只算 1 个；不同用户名才累加

    这条是"喷洒告警"与"单账号爆破"的分界：后者已由账户级锁定覆盖，不该在这里重复告警。
    """
    now = time.time()
    for _ in range(L._SPRAY_USERS * 2):
        L._begin_login_attempt('10.0.0.1', 'same_user')
    assert L._spray_user_count(now) == 1, \
        '同一个用户名被算成了多个（去重没生效）'

    with L._login_fail_lock:
        L._acct_fail.clear()

    for i in range(L._SPRAY_USERS):
        L._begin_login_attempt('10.0.0.%d' % (i + 1), 'user%d' % i)
    assert L._spray_user_count(now) == L._SPRAY_USERS, \
        '不同用户名没有累加（喷洒检测失效）'


def test_spray_window_expires(L):
    """窗口外的用户名不再计入"""
    for i in range(L._SPRAY_USERS):
        L._begin_login_attempt('10.0.0.%d' % (i + 1), 'user%d' % i)
    assert L._spray_user_count(time.time()) == L._SPRAY_USERS
    assert L._spray_user_count(time.time() + L._SPRAY_WINDOW + 1) == 0, \
        '窗口外的记录仍被计入'


# ---------- 5. ★ 被拒时的响应：429 + 明确文案 ----------

class _FakeHandler:
    """只够走"被全局限速拦掉"那条路径：用到 client_address 与 send_json"""

    def __init__(self, ip):
        self.client_address = (ip, 41234)
        self.sent = []

    def send_json(self, data, status=200, **kw):
        self.sent.append((status, data))


def test_auth_login_returns_429_with_hint(L):
    """★ 被全局锁拦下时，返回 429 且文案说清原因（不是笼统的"请稍后再试"）"""
    _fill(L)
    h = _FakeHandler('10.5.5.5')
    L.auth_login(h, add_log=None, logger=logging.getLogger('t'),
                 UPLOAD_DIR=None, verify_login=None, create_session=None,
                 is_default_admin_password=None, _sessions=None, _sessions_lock=None)
    assert h.sent, 'auth_login 没有任何响应'
    status, data = h.sent[0]
    assert status == 429, (status, data)
    assert '大量登录尝试' in data.get('error', ''), data
