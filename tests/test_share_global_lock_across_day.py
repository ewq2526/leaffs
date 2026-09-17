# -*- coding: utf-8 -*-
"""`issues.md` §二 第 7 条：跨自然日翻转**不能**把"分享页全锁"清掉。

**问题**：`_flush_day_locked()` 在跨日时重置按天计的计数与锁 —— 这本身是对的
（IP 当日锁、当日错误数、60 秒滑动窗口都该清），但它**顺带把 `global_lock_until` 也清了**。
那是一个**绝对时间戳**（`now + 30 分钟`），自带到期判定，根本不是"按天"的东西。

后果：**攻击者触发的全锁跨过零点就自动解除** —— 23:59 触发、实际只锁了 1 分钟
（`_P['global_lock_secs']` 默认 1800 秒）。而"全锁"正是用来对付
"多 IP 轮换猜码"（`record_failure` 里要求窗口内 ≥5 次且来自 ≥2 个 IP）的那一道闸。

**修法**：`_flush_day_locked` 里不再重置 `global_lock_until`；判定逻辑不变
（`access_blocked`: `global_lock_until > now`）。清全锁只有两条合法途径：
等它自己到期，或本人 `clear_attempts()`（主动重置）。
"""
import os
import time

import pytest

from leaffs.share import access as S


@pytest.fixture()
def acc(data_root_factory, monkeypatch):
    """数据指到临时目录（`_flush_day_locked` 末尾会落盘），并清掉模块级缓存与指纹"""
    root = data_root_factory('gday_')
    monkeypatch.setattr(S, '_ACCESS_FILE', os.path.join(root, 'share_access.json'))
    monkeypatch.setattr(S, '_CACHE', None)
    monkeypatch.setattr(S, '_CACHE_STAMP', None)
    return root


def _locked_user(until):
    """一个"正在被攻击、已触发全锁"的用户记录"""
    now = time.time()
    return {'code_hash': '', 'ip_errors': {'1.1.1.1': 3}, 'ip_locked': {'1.1.1.1': True},
            'window': [now], 'window_ips': {'1.1.1.1': now},
            'global_lock_until': until, 'attack_ips': ['1.1.1.1'], 'last_events': [{}]}


def test_day_flip_does_not_clear_global_lock(acc):
    """★ 跨日之后全锁**仍然在**（旧实现把它清零 ⇒ 攻击者的锁跨零点自动解除）"""
    now = time.time()
    S._CACHE = {'date': '1970-01-01', 'tickets': {},
                'users': {'alice': _locked_user(now + 1800)}}

    S._flush_day_locked()

    u = S._CACHE['users']['alice']
    assert S._CACHE['date'] == S._today(), '日期该翻到今天'
    assert u['global_lock_until'] > now, (
        '跨日把还在生效的全锁清掉了 —— 23:59 触发的 30 分钟锁跨零点就解了（§二 第 7 条）')


def test_day_flip_still_clears_per_day_state(acc):
    """对照：**该清的按天状态必须照旧清掉** —— 防"修过头"

    （只该去掉 `global_lock_until` 那一项，别把整段重置一起删。）
    """
    now = time.time()
    S._CACHE = {'date': '1970-01-01', 'tickets': {},
                'users': {'alice': _locked_user(now + 1800)}}

    S._flush_day_locked()

    u = S._CACHE['users']['alice']
    assert u['ip_errors'] == {}, '按天的 IP 错误数应当清零'
    assert u['ip_locked'] == {}, 'IP 当日锁应当清零（否则那个 IP 被永久锁死）'
    assert u['window'] == [] and u['window_ips'] == {}, '滑动窗口该收空'


def test_access_blocked_still_global_after_day_flip(acc):
    """端到端（走真实 `access_blocked`）：跨日之后仍判"全锁中"

    这条经过 `_load_locked` ＋ `_flush_day_locked` 两个真实入口，所以先把表落盘，
    让"盘上"与"内存"一致（也顺便验证了二者在跨日这一刻不会打架）。
    """
    now = time.time()
    S._CACHE = {'date': '1970-01-01', 'tickets': {},
                'users': {'alice': _locked_user(now + 1800)}}
    S._save_locked()          # 盘上也是这份（并已对齐指纹 ⇒ 下面不会白重载）

    blocked, reason = S.access_blocked('alice', '9.9.9.9')

    assert blocked is True and reason == 'global', (
        '跨日之后全锁失效了：blocked=%r reason=%r' % (blocked, reason))
