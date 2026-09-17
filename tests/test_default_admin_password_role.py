# -*- coding: utf-8 -*-
"""`issues.md` §二 第 8 条：`is_default_admin_password()` 必须**按角色**判断。

**问题**：原实现是 `_users.get('admin')` —— 硬编码用户名。超管**改名**、或删掉 `admin`
另建一个超管之后，这个判断就指向一个不存在的账号 ⇒ 恒为 False ⇒
真正"还没设口令"的超管**再也提醒不到**（登录页/管理页那条"请尽快设置密码"的横幅不出现）。

**修法**：与 `get_super_admin_name()` / `_ensure_super_admin()` 同一口径 ——
超管 = `role == 'super_admin'`；只要**存在任一**没口令的超管就返回 True。
"""
import pytest

import leaffs.auth.core as C


@pytest.fixture()
def users(monkeypatch):
    """替换掉内存里的用户表（不动磁盘）"""
    def _set(m):
        monkeypatch.setattr(C, '_users', m)
    return _set


def test_renamed_super_admin_is_still_detected(users):
    """★ 超管改名后仍能识别出"还没设口令"（旧实现硬编码 'admin' ⇒ 恒 False）"""
    users({'zhangsan': {'role': 'super_admin', 'password': ''}})
    assert C.is_default_admin_password() is True, (
        '超管改名之后，"请尽快设置密码"的提醒就再也不会出现（§二 第 8 条）')


def test_super_admin_recreated_under_another_name_is_detected(users):
    """★ 删掉 admin、另建一个超管（同样没设口令）也要认出来"""
    users({'laowang': {'role': 'super_admin', 'password': ''},
           'someone': {'role': 'user', 'password': 'x' * 8}})
    assert C.is_default_admin_password() is True


def test_any_super_admin_without_password_counts(users):
    """有多个超管时：只要**其中一个**没设口令就该提醒"""
    users({'admin': {'role': 'super_admin', 'password': 'a' * 8},
           'second': {'role': 'super_admin', 'password': ''}})
    assert C.is_default_admin_password() is True


def test_all_super_admins_have_password(users):
    """对照：超管都设了口令 ⇒ False"""
    users({'admin': {'role': 'super_admin', 'password': 'a' * 8},
           'second': {'role': 'super_admin', 'password': 'b' * 8}})
    assert C.is_default_admin_password() is False


def test_ordinary_user_without_password_does_not_trigger(users):
    """对照：**普通用户**没口令不该触发超管提醒（防误报）"""
    users({'admin': {'role': 'super_admin', 'password': 'a' * 8},
           'normal': {'role': 'user', 'password': ''}})
    assert C.is_default_admin_password() is False


def test_no_super_admin_at_all(users):
    """边界：一个超管都没有 ⇒ False

    （正常运行时会由 `_ensure_super_admin()` 补一个，这里只钉判定本身。）
    """
    users({})
    assert C.is_default_admin_password() is False
