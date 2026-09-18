# -*- coding: utf-8 -*-
"""深度配置（`/api/config/deep`）的输入校验。

背景（外部黑盒报告的 LF-08，已逐条核实）：`apply_deep_config` 原来是一串
`if 'k' in data` 分支 —— 未知键不进任何分支 → `changed` 一直 False → 函数却照样
`return True` → 接口回 `{"success": true, "配置已更新"}`；而且十几个键**完全没有区间校验**
（区间只写在前端），绕过前端直连 API 就能把 `upload_max_size` 设成 1、把
`max_total_conns` 设成 1 打成全站 503；类型错误还会抛 ValueError 逃到兜底 → 500。

本文件锁住五件事：未知键拒绝（且整体失败）、非对象体拒绝、类型错误 400、
区间越界拒绝（**不静默钳制**）、以及 `0` 这类哨兵值仍然可用。
"""
import json
import os

from conftest import login


def _deep(client):
    r = client.get('/api/config/deep')
    assert r.status_code == 200, r.text
    return r.json()


def _post(client, payload):
    return client.post('/api/config/deep', json=payload)


def _cfg(data_root):
    with open(os.path.join(data_root, 'config', 'server_config.json'),
              encoding='utf-8') as f:
        return json.load(f)


def test_unknown_key_rejected_and_nothing_applied(client, data_root):
    """未知键 → 400，且**同一请求里的合法键也不能落盘**（整体失败，不是部分生效）"""
    login(client)
    before = _cfg(data_root).get('upload_max_size')
    r = _post(client, {'upload_max_size': 12345678, 'zz_nonexistent': 1})
    assert r.status_code == 400, r.text
    err = r.json().get('error', '')
    assert 'zz_nonexistent' in err, err
    assert _cfg(data_root).get('upload_max_size') == before, '未知键不该让别的键落盘'


def test_non_dict_body_rejected(client):
    """请求体不是 JSON 对象 → 400（原来会走到 int(...) 抛异常变 500）"""
    login(client)
    for body in ([1, 2, 3], 'abc', 42):
        r = client.post('/api/config/deep', json=body)
        assert r.status_code == 400, '%r -> %s' % (body, r.text)


def test_type_errors_are_400_not_500(client):
    """数值键喂非数字 → 400 带键名（而不是 500「服务器内部错误」）"""
    login(client)
    for bad in ('abc', None, [1, 2], {'a': 1}, True):
        r = _post(client, {'session_expiry_days': bad})
        assert r.status_code == 400, '%r -> %s' % (bad, r.text)
        assert 'session_expiry_days' in r.json().get('error', ''), r.text


def test_ranges_rejected(client):
    """越界一律拒绝并点名（不静默钳制 —— 钳制等于悄悄改成别的值）"""
    login(client)
    cases = [
        ('upload_max_size', 512 * 1024),          # 非 0 却小于 1 MiB
        ('upload_max_size', (1 << 40) + 1),
        ('max_api_body_size', 4095),
        ('max_api_body_size', (64 << 20) + 1),
        ('session_expiry_days', 0),
        ('session_expiry_days', 3651),
        ('max_total_conns', 7),                   # 下限 8：1 会让服务自锁
        ('max_total_conns', 20001),
        ('max_conn_per_ip', 0),
        ('max_conn_per_ip', 1001),
        ('ca_validity_days', 0),
        ('ca_validity_days', 36501),
        ('copy_buffer_size', 4095),
        ('copy_buffer_size', (16 << 20) + 1),
        ('cache_max_items', 100001),
        ('cache_ttl', 86401),
        ('folder_size_ttl', 86401),
        ('debounce_delay', 61),
        ('preview_max_size', (1 << 30) + 1),
        ('zip_max_files', 100001),
        ('ws_max_conn_per_ip', 0),
        ('ws_max_conn_per_ip', 257),
        ('io_idle_timeout_secs', 0.5),
        ('io_idle_timeout_secs', 3601),
        # thumb_* 四个原来是"越界静默丢掉"，现在必须点名拒绝
        ('thumb_sample_ratio', 0.0001),
        ('thumb_sample_ratio', 1.5),
        ('thumb_miss_threshold', 0),
        ('thumb_scan_batch', 0),
        ('thumb_scan_batch', 10001),
        ('thumb_scan_interval', 0.001),
        ('thumb_scan_interval', 61),
        # 原来静默钳制 / 无上限的
        ('upload_chunk', 4095),
        ('upload_chunk', (16 << 20) + 1),
        ('pbkdf2_iterations', 10000001),
        ('salt_length', 65),
    ]
    for key, bad in cases:
        r = _post(client, {key: bad})
        assert r.status_code == 400, '%s=%r -> %s' % (key, bad, r.text)
        assert key in r.json().get('error', ''), '%s=%r -> %s' % (key, bad, r.text)


def test_sentinel_zero_still_allowed(client, data_root):
    """`0` 是哨兵值（不限制 / 禁用缓存 / 禁用预览），不能被区间打死 —— 改完恢复原值"""
    login(client)
    keys = ('upload_max_size', 'zip_max_files', 'cache_ttl', 'folder_size_ttl',
            'cache_max_items', 'preview_max_size', 'debounce_delay')
    current = _deep(client)
    originals = {}
    for k in keys:
        assert k in current, '取不到 %s 的当前值，无法安全恢复' % k
        originals[k] = current[k]
    try:
        for k in keys:
            r = _post(client, {k: 0})
            assert r.status_code == 200, '%s=0 应被接受：%s' % (k, r.text)
        assert _cfg(data_root).get('upload_max_size') == 0
    finally:
        for k, v in originals.items():
            r = _post(client, {k: v})
            assert r.status_code == 200, '恢复 %s=%r 失败：%s' % (k, v, r.text)


def test_frontend_keys_still_save(client):
    """前端管理页会发的键逐个回归：用**当前值**提交（合法 + 无变化）应回 200"""
    login(client)
    keys = ('pbkdf2_iterations', 'salt_length', 'thumb_sample_ratio', 'thumb_miss_threshold',
            'thumb_scan_batch', 'thumb_scan_interval', 'session_expiry_days',
            'guest_public_write', 'downloader_guest_allowed', 'access_log',
            'io_idle_timeout_secs', 'keepalive_timeout', 'trust_bind_host', 'ws_max_conn_per_ip',
            'max_conn_per_ip', 'max_total_conns')
    cur = _deep(client)
    for k in keys:
        assert k in cur, '前端会发 %s，但 GET 没回这个键' % k
        r = _post(client, {k: cur[k]})
        assert r.status_code == 200, '%s=%r -> %s' % (k, cur[k], r.text)
        assert r.json().get('success') is True, r.text


def test_max_total_conns_floor_is_8(client):
    """下限 8：7 拒绝、8 接受（接受后恢复 256）"""
    login(client)
    r = _post(client, {'max_total_conns': 7})
    assert r.status_code == 400, r.text
    r = _post(client, {'max_total_conns': 8})
    assert r.status_code == 200, r.text
    r = _post(client, {'max_total_conns': 256})
    assert r.status_code == 200, r.text


def test_enum_and_bool_inputs_are_strict(client):
    """枚举键取值不认识、布尔键给非 true/false → 400

    原来这两类都是**静默**的：枚举值不认识就丢掉，布尔键走宽松的 `_to_bool`
    （`"yes"` 变 True、`"garbage"` 变 False），接口还照样回 success。
    """
    login(client)
    r = _post(client, {'ca_trust_decision': 'whatever'})
    assert r.status_code == 400, r.text
    assert 'ca_trust_decision' in r.json().get('error', ''), r.text
    for key in ('access_log', 'zip_streaming', 'guest_public_write',
                'downloader_guest_allowed', 'auto_trust_ca', 'harden_config_acls'):
        r = _post(client, {key: 'yes'})
        assert r.status_code == 400, '%s="yes" -> %s' % (key, r.text)
        assert key in r.json().get('error', ''), r.text


def test_trust_bind_host_validated(client):
    """监听地址必须是真的 IP / 主机名（原来只查"非空"）"""
    login(client)
    for bad in ('not a host!', '<script>', 'a b c', 'x/y', 'exa mple.com'):
        r = _post(client, {'trust_bind_host': bad})
        assert r.status_code == 400, '%r -> %s' % (bad, r.text)
    cur = _deep(client)['trust_bind_host']
    r = _post(client, {'trust_bind_host': cur})
    assert r.status_code == 200, '合法值（当前值 %r）应仍可保存：%s' % (cur, r.text)
