# -*- coding: utf-8 -*-
"""登录失败锁定的「判 + 记」必须原子（C-1）。

黑盒报告 C-1（2026-09-21）实测：

    串行对照         : WRONG×6, LOCK×2            → 正常第 7 次锁
    8 并发(不存在用户): WRONG=8, LOCK=0    ×2 次   → 8 次全部通过（上限是 6）
    8 并发(真实账号)  : WRONG=6, LOCK=2

根因：**锁定检查在 PBKDF2 之前、计数在 PBKDF2 之后**，两次独立加锁中间隔着一次
PBKDF2（期间释放 GIL），于是 N 个并发请求会**同时**看到"失败次数还没到 6"而全部放行。
单次突发可试次数从 6 涨到 `max_conn_per_ip`(20)，约 3.3 倍。

修法：两步并成 `_begin_login_attempt()` —— 同一把锁内「判锁定 → 未锁则占位 +1」；
成功登录仍走 `_reset_login_fail()` 清零，净效果依旧是"只有失败才计数"。

⚠️ 并发测试最容易变成"永远通过"的假测试（GIL 让线程根本没交错）。所以这里额外
**原样复现一遍旧的两步结构**作为对照：同样的 8 线程模型下，旧结构必须让全部线程
通过检查 —— 做不到就说明测试模型跑不出交错，主测试的"通过"也就没有意义。
"""
import threading
import time

import pytest

from conftest import login


@pytest.fixture()
def L():
    """拿到 login_api，并把账号级/每用户名锁定状态清干净（只动本进程的模块状态）"""
    from leaffs.auth import login_api as L

    def _clean():
        with L._login_fail_lock:
            L._acct_fail.clear()
            L._login_fail.clear()

    _clean()
    yield L
    _clean()


def _run_parallel(n, fn):
    """让 n 个线程尽量同时进入 fn(i)，按进入顺序收集返回值"""
    barrier = threading.Barrier(n)
    out = [None] * n

    def worker(i):
        barrier.wait()
        out[i] = fn(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def test_concurrent_attempts_are_capped_at_the_threshold(L):
    """★ C-1 本体：并发放行总数不许超过 6（同一 IP、同一用户名）"""
    n = 8
    results = _run_parallel(n, lambda i: L._begin_login_attempt('10.9.9.9', 'race_user')[0])
    allowed = sum(1 for r in results if r)
    assert allowed <= L._LOGIN_MAX_FAIL, \
        '并发下放行了 %d 次，上限是 %d（C-1）：%r' % (allowed, L._LOGIN_MAX_FAIL, results)


def test_concurrent_attempts_capped_for_account_level_too(L):
    """账户级那条（12 次 / 15 分钟，跨来源收敛）同样要在并发下守住"""
    n = 20
    results = _run_parallel(
        n, lambda i: L._begin_login_attempt('10.0.0.%d' % (i + 1), 'race_acct')[0])
    allowed = sum(1 for r in results if r)
    assert allowed <= L._ACCOUNT_LOCK_AFTER, \
        '并发下账户级放行了 %d 次，上限是 %d：%r' % (allowed, L._ACCOUNT_LOCK_AFTER, results)


def test_space_variants_share_one_account_bucket(L):
    """★ 用户名的空格变体算**同一个**账户（守卫，不是回归）

    ⚠️ **这里先纠正我自己一次误报**：我曾据代码判断「`' admin'` 与 `'admin'` 是两个键，
    每加一个空格就多一份 12 次/15 分钟的额度，账户级锁定被绕过」——**实测不成立**。
    计数键走 `_normalize_username()` → `sanitize_log_text()`，而后者内部**有
    `s.strip()`**（`runtime_log.py:38`），所以空格变体本来就会归一到同一个键。
    （发现过程：给这条写的"退化验证"跑出来是绿的 —— 测试抓不到差别。
    绿的退化验证不等于测试写得弱，也可能等于**被修的东西根本不存在**。）

    所以这条的定位是**守卫**：把"它们共用一个桶"钉住。哪天有人拿掉
    `sanitize_log_text` 里的 `strip()`、或者换掉归一化函数，这里会立刻红，
    而不是等攻击者先发现。

    每个变体换一个来源 IP，避开 (IP,用户名) 那层，只让**账户级**计数说话：
    4 个变体 × 5 次 = 20 次 —— 共用一个账户则止步于 12；各算一个账户则 20 次全放行。
    """
    names = ['admin', ' admin', 'admin ', '  admin']
    allowed = []
    for i, nm in enumerate(names):
        for _ in range(5):
            allowed.append(L._begin_login_attempt('10.7.%d.1' % (i + 1), nm)[0])
    got = sum(1 for a in allowed if a)
    assert got == L._ACCOUNT_LOCK_AFTER, \
        '空格变体被算成了不同账户（账户级锁定被摊薄）：放行 %d 次（应为 %d）' % (
            got, L._ACCOUNT_LOCK_AFTER)


def test_the_old_two_step_structure_really_races(L):
    """对照：原样复现旧结构，证明并发模型确实能抓到 C-1

    这条**不测生产代码**，它证明的是上面那条测试有意义：
    旧结构「检查（持锁读）→ PBKDF2 的等价位（锁是放开的）→ 计数（持锁写）」，
    在同样的 8 线程模型下会让**全部**线程通过检查 —— 这正是报告里的 WRONG=8。
    """
    n = 8
    key = ('10.8.8.8', 'legacy_user')

    def legacy(i):
        # ① 检查（持锁读）
        with L._login_fail_lock:
            rec = L._login_fail.get(key)
            locked = bool(rec and rec['n'] >= L._LOGIN_MAX_FAIL)
        if locked:
            return False
        time.sleep(0.05)        # ← PBKDF2 的等价位：这段时间没有锁保护
        # ② 计数（持锁写）
        with L._login_fail_lock:
            rec = L._login_fail.get(key) or {'n': 0, 't': time.time()}
            rec['n'] += 1
            rec['t'] = time.time()
            L._login_fail[key] = rec
        return True

    out = _run_parallel(n, legacy)
    passed = sum(1 for r in out if r)
    assert passed == n, (
        '对照失效：旧结构本应让 %d 个线程全部通过检查（这正是 C-1），实际只过了 %d 个 —— '
        '说明并发模型跑不出交错，主测试的"通过"并不代表修复有效' % (n, passed))


def test_eight_concurrent_wrong_logins_cannot_all_pass(client):
    """★ 端到端复现报告的测法：8 个并发错误登录，通过数不许超过 6

    报告（8 并发、不存在的用户名）实测 WRONG=8 LOCK=0。这里从 127.0.0.1 发起：
    环回豁免全局层，但 (IP,用户名) 与账户级两层照常生效 —— 正是被测的那两层。
    """
    login(client)
    n = 8
    barrier = threading.Barrier(n)
    codes = [None] * n

    def worker(i):
        barrier.wait()
        r = client.post('/api/auth/login',
                        json={'username': 'race_e2e_user', 'password': 'definitely-wrong'})
        codes[i] = r.status_code

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    wrong = sum(1 for c in codes if c != 429)
    assert wrong <= 6, \
        '8 个并发错误登录里放行了 %d 个，上限是 6（C-1）：%r' % (wrong, codes)
    # 一个都没放行说明它们被"每 IP 30 次/分钟"先挡掉了 —— 那这条就没测到东西
    assert wrong >= 1, '全部被更外层的每 IP 限速挡下，本轮没测到锁定层：%r' % (codes,)
