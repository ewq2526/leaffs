# -*- coding: utf-8 -*-
"""用户配额/限速端点：参数缺失必须报错，不能静默当成 0（N-10）。

黑盒报告 N-10（2026-09-21）：`POST /api/users/quota {"username":..., "quota":...}`
返回 `{"success": true}`，但 `/api/users` 里的配额纹丝不动。

真相是这个端点读的字段叫 `quota_mb`：发 `quota` 时 `data.get('quota_mb', 0)` 静默取 0，
而 0 是**合法值**（= 不限额），于是 `set_user_quota(username, 0)` 真的执行成功了 ——
报成功不是假的，它确实改了东西，只是改成了"不限"，而不是调用方要的 100 MB。
`/api/users` 里"纹丝不动"是因为那个用户本来就是 0。

所以这条的实质是**假成功**：请求表达的意图没有被执行，调用方却收到成功。
`users_speed`（`speed_kb`）是完全相同的写法。

本文件钉住：字段缺失或非法一律 400，且**不许改动任何状态**；显式传 0 仍然合法。
"""
import pytest

from conftest import login

PW = 'TestPass123!'


@pytest.fixture()
def probe_user(client):
    """建一个独立用户（不碰 admin 自己的配额 —— 那会影响别的测试），用完删掉"""
    login(client)
    name = 'n10_probe_user'
    r = client.post('/api/users/add', json={'username': name, 'password': PW, 'role': 'user'})
    assert r.status_code == 200, r.text
    yield name
    client.post('/api/users/delete', json={'username': name})


def _field(client, name, key):
    r = client.get('/api/users')
    assert r.status_code == 200, r.text
    return r.json()['users'][name][key]


def test_missing_quota_mb_is_rejected_and_changes_nothing(client, probe_user):
    name = probe_user
    r = client.post('/api/users/quota', json={'username': name, 'quota_mb': 100})
    assert r.status_code == 200 and r.json().get('success') is True, r.text
    before = _field(client, name, 'quota')
    assert before == 100 * 1048576, before

    # 黑盒测试发的就是这个形态：字段名是 quota，不是 quota_mb
    r = client.post('/api/users/quota', json={'username': name, 'quota': 999})
    assert r.status_code == 400, (
        'N-10：不认识的字段必须报错。原来它静默取 0 并回 success，'
        '把配额改成了"不限"，而调用方以为设置成功：%s' % r.text)
    assert _field(client, name, 'quota') == before, '配额被静默改动了'


def test_explicit_zero_quota_is_still_valid(client, probe_user):
    """显式传 0 是合法值（= 不限额），不能被新校验误伤"""
    r = client.post('/api/users/quota', json={'username': probe_user, 'quota_mb': 0})
    assert r.status_code == 200 and r.json().get('success') is True, r.text
    assert _field(client, probe_user, 'quota') == 0


@pytest.mark.parametrize('bad', ['abc', None, [], {}])
def test_non_integer_quota_mb_is_rejected(client, probe_user, bad):
    r = client.post('/api/users/quota', json={'username': probe_user, 'quota_mb': bad})
    assert r.status_code == 400, r.text


def test_missing_speed_kb_is_rejected_and_changes_nothing(client, probe_user):
    """users_speed 是完全相同的写法（speed_kb 缺失 → 0 = 不限速 → 报成功）"""
    name = probe_user
    r = client.post('/api/users/speed', json={'username': name, 'speed_kb': 512})
    assert r.status_code == 200 and r.json().get('success') is True, r.text
    before = _field(client, name, 'speed_limit')
    assert before == 512 * 1024, before

    r = client.post('/api/users/speed', json={'username': name, 'speed': 999})
    assert r.status_code == 400, r.text
    assert _field(client, name, 'speed_limit') == before, '限速被静默改动了'
