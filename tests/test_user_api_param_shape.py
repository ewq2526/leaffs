# -*- coding: utf-8 -*-
"""用户管理端点：畸形参数必须是 400，不是 500。

`users_api.py` 里原来有 **9 处**都写 `data.get('username', '').strip()` 这种形式 ——
传数字/对象/数组时 `.strip()` 抛 `AttributeError`，一路落到各端点末尾的
`except Exception`，变成 **500「服务器内部错误」**。

500 的问题不只是"码不对"：它让调用方以为服务端故障（于是去重试），
也让这类输入在日志里和真故障混在一起 —— 而"参数形状不对"与"服务器坏了"
是完全不同的两件事，混掉之后排查得逐条翻堆栈才知道是哪种。

现在统一走 `_str_param()`，非字符串明确回 400。

⚠️ 注意 `None` 也归这一档：`data.get('username', '')` 在 JSON `null` 时拿到的是
`None`（不是缺失），原来同样会炸。
"""
import pytest

from conftest import login

PW = 'TestPass123!'

# (端点, 该端点还需要的其它字段)
ENDPOINTS = (
    ('/api/users/quota', {'quota_mb': 1}),
    ('/api/users/speed', {'speed_kb': 1}),
    ('/api/users/add', {'password': PW, 'role': 'user'}),
    ('/api/users/delete', {}),
    ('/api/users/password', {'password': PW}),
    ('/api/users/role', {'role': 'user'}),
)

BAD_VALUES = (123, 1.5, True, [], {}, None)


@pytest.mark.parametrize('endpoint, extra', ENDPOINTS)
@pytest.mark.parametrize('bad', BAD_VALUES)
def test_non_string_username_is_400_not_500(client, endpoint, extra, bad):
    login(client)
    body = dict(extra)
    body['username'] = bad
    r = client.post(endpoint, json=body)
    assert r.status_code == 400, \
        '%s 收到 username=%r 回了 %s（应当是 400）：%s' % (
            endpoint, bad, r.status_code, r.text[:200])


@pytest.mark.parametrize('bad', BAD_VALUES)
def test_non_string_role_is_400_not_500(client, bad):
    login(client)
    r = client.post('/api/users/add', json={'username': 'shape_probe_user',
                                            'password': PW, 'role': bad})
    assert r.status_code == 400, 'role=%r 回了 %s：%s' % (bad, r.status_code, r.text[:200])


@pytest.mark.parametrize('bad', BAD_VALUES)
def test_non_string_rename_params_are_400(client, bad):
    login(client)
    for body in ({'old_name': bad, 'new_name': 'x'},
                 {'old_name': 'x', 'new_name': bad},
                 {'old_name': bad, 'new_name': bad}):
        r = client.post('/api/users/rename', json=body)
        assert r.status_code == 400, \
            '%r 回了 %s（应当是 400）：%s' % (body, r.status_code, r.text[:200])


def test_valid_shapes_still_work(client):
    """对照组：正常形状不能被这次收紧误伤"""
    login(client)
    name = 'shape_ok_user'
    r = client.post('/api/users/add', json={'username': name, 'password': PW, 'role': 'user'})
    assert r.status_code == 200 and r.json().get('success') is True, r.text
    try:
        r = client.post('/api/users/quota', json={'username': name, 'quota_mb': 7})
        assert r.status_code == 200 and r.json().get('success') is True, r.text
        # 带首尾空格的名字：strip 后应当照样对得上
        r = client.post('/api/users/quota', json={'username': '  %s  ' % name, 'quota_mb': 8})
        assert r.status_code == 200 and r.json().get('success') is True, r.text
    finally:
        client.post('/api/users/delete', json={'username': name})
