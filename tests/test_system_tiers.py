# -*- coding: utf-8 -*-
"""分档采集与增量过滤：每秒那一趟只许读 /proc 里的四个小文件。

以前 `collect()` 把三档捆在一起按最快节奏跑：每秒 fork 一次 `systemctl`、
每秒遍历 `/sys`、每秒 `statvfs` 一遍已挂载分区，而其中没有一样是每秒会变的。
分档之后，"每秒档"的判据不是界面刷多快，而是**值会不会变**。

这几条测试盯的就是那个判据：多读一个文件、多 fork 一次，都得红。
"""
import leaffs.system.host as host


def test_fast_touches_only_proc(monkeypatch):
    """fast() 只读四个 /proc 文件，不碰 /sys、不碰 /run/udev。"""
    seen = []
    real_read = host._read

    def spy(path, default=''):
        seen.append(path)
        return real_read(path, default)

    monkeypatch.setattr(host, '_read', spy)
    data = host.fast()

    assert sorted(data) == ['cpu', 'io', 'load', 'mem']
    assert sorted(set(seen)) == ['/proc/diskstats', '/proc/loadavg',
                                 '/proc/meminfo', '/proc/stat']


def test_fast_forks_nothing(monkeypatch):
    """每秒档不许建进程：services() 那一次 systemctl 是 58 ms 里的大头。"""
    def boom(*a, **k):
        raise AssertionError('每秒档不该 fork: %r' % (a,))

    monkeypatch.setattr(host.subprocess, 'run', boom)
    monkeypatch.setattr(host.os, 'listdir', boom)
    host.fast()


def test_fast_scope_has_no_slow_keys():
    data = host.snapshot('fast')
    assert not (set(data) & {'disks', 'services', 'procs', 'wifi', 'temp', 'power'})
    assert {'host', 'boot_at', 'load', 'cpu', 'mem', 'io'} <= set(data)


def test_full_scope_has_every_slow_key(monkeypatch):
    calls = []

    def stub(name, ret):
        def _f(*a, **k):
            calls.append(name)
            return ret
        return _f

    monkeypatch.setattr(host, 'temperatures', stub('temp', []))
    monkeypatch.setattr(host, 'power', stub('power', {}))
    monkeypatch.setattr(host, 'disks', stub('disks', []))
    monkeypatch.setattr(host, 'services', stub('services', []))
    monkeypatch.setattr(host, 'processes', stub('procs', []))
    import leaffs.system.wifi as wifi
    monkeypatch.setattr(wifi, 'status', stub('wifi', {}))

    data = host.snapshot('full')

    assert {'disks', 'services', 'procs', 'wifi', 'temp', 'power'} <= set(data)
    assert sorted(calls) == ['disks', 'power', 'procs', 'services', 'temp', 'wifi']


def test_delta_only_carries_changed_keys():
    """值没变就不进帧：这是"空闲时每秒只有两百来字节"的来源。"""
    from leaffs.server.push import _delta

    sent = {}
    first = _delta(sent, {'cpu': {'total': 1}, 'mem': {'percent': 2}})
    assert set(first) == {'cpu', 'mem'}

    assert _delta(sent, {'cpu': {'total': 1}, 'mem': {'percent': 2}}) == {}

    one = _delta(sent, {'cpu': {'total': 1}, 'mem': {'percent': 3}})
    assert set(one) == {'mem'}


def test_delta_replaces_whole_key():
    """一级键整块替换，不做深合并：帧里出现哪个键，页面就整块换掉它。"""
    from leaffs.server.push import _delta

    sent = {'mem': {'percent': 2, 'used': 1}}
    out = _delta(sent, {'mem': {'percent': 2}})
    assert out == {'mem': {'percent': 2}}
    assert sent['mem'] == {'percent': 2}


def test_parse_show_splits_units():
    """`systemctl show` 多单元输出按空行分块，Id 用来对回单元名。"""
    text = (
        'Id=camphor-kiosk.service\n'
        'ActiveState=active\n'
        'MainPID=812\n'
        'MemoryCurrent=182452224\n'
        'CPUUsageNSec=42100000000\n'
        'Description=camphor kiosk\n'
        '\n'
        'Id=wsdd2.service\n'
        'ActiveState=inactive\n'
        'MainPID=0\n'
        'MemoryCurrent=[not set]\n'
        'CPUUsageNSec=[not set]\n'
        'Description=WSD/LLMNR discovery\n'
    )
    got = host._parse_show(text)
    assert sorted(got) == ['camphor-kiosk.service', 'wsdd2.service']
    assert got['camphor-kiosk.service']['ActiveState'] == 'active'
    assert got['wsdd2.service']['MainPID'] == '0'


def test_num_or_none_drops_sentinels():
    """systemd 的"没有值"有两种写法，都不该显示成 1844 亿 GB。"""
    assert host._num_or_none('[not set]') is None
    assert host._num_or_none('') is None
    assert host._num_or_none('18446744073709551615') is None
    assert host._num_or_none('-1') is None
    assert host._num_or_none('182452224') == 182452224
    assert host._num_or_none('0') == 0


def test_service_row_fills_usage_fields():
    """占用解析不出来时给 0 / None，而不是把 [not set] 原样给页面。"""
    row = host._service_row('ksmbd', {
        'ActiveState': 'active', 'MainPID': '701',
        'MemoryCurrent': '12582912', 'CPUUsageNSec': '[not set]',
        'Description': 'SMB3 kernel server',
    })
    assert row['state'] == 'active'
    assert row['pid'] == 701
    assert row['memory'] == 12582912
    assert row['cpu_ns'] is None
    assert row['desc'] == 'SMB3 kernel server'


def test_processes_sorted_and_limited():
    """进程榜：按 RSS 从大到小，最多 limit 条。"""
    rows = host.processes(3)
    assert 0 < len(rows) <= 3
    assert set(rows[0]) == {'pid', 'name', 'rss'}
    rss = [r['rss'] for r in rows]
    assert rss == sorted(rss, reverse=True)
    assert all(r['rss'] > 0 and r['pid'] > 0 and r['name'] for r in rows)
