"""本机状态接口：把 host.collect() 的结果交给网页。

只读为主，写操作只有无线网络连接这一处（`/api/wifi/connect`）。鉴权不在这里做 ——
分派层用 `_SUPER_ADMIN_ONLY_GET` / `_SUPER_ADMIN_ONLY_POST` 统一判，新增接口时
必须同时把路径加进那两个集合，否则就是开了个没门禁的入口。
"""

import subprocess
import threading
import time

import leaffs.system.host as host
import leaffs.system.wifi as wifi

# 采集节拍固定 1 秒：页面每秒取一次并重绘，后端每秒采一次，两边同拍。
# 缓存只用来吸收同一秒内的多余请求（多开窗口、浏览器的并发），
# 不会让任何一份数据变旧超过 1 秒。
_TTL = 1.0
_lock = threading.Lock()
_cache = {'at': 0.0, 'data': None}


def _snapshot(collect):
    now = time.monotonic()
    with _lock:
        data = _cache['data']
        if data is not None and (now - _cache['at']) < _TTL:
            return data
    data = collect()
    with _lock:
        _cache['data'] = data
        _cache['at'] = time.monotonic()
    return data


def send_system(handler, collect):
    try:
        data = _snapshot(collect)
    except Exception as e:                     # 采集失败要说清楚，不能给半张表
        handler.send_json({'error': '系统状态读取失败: %s' % e}, 500)
        return
    handler.send_json(data)


def send_wifi_status(handler):
    try:
        handler.send_json(wifi.status())
    except Exception as e:
        handler.send_json({'error': '无线状态读取失败: %s' % e}, 500)


def send_timezones(handler):
    """当前时区 + 可选列表，给系统页那个下拉用。"""
    try:
        handler.send_json({'current': host.timezone(), 'timezones': host.timezones()})
    except Exception as e:
        handler.send_json({'error': '时区列表读取失败: %s' % e}, 500)


def set_timezone(handler):
    """POST /api/system/timezone {timezone} —— 只允许超级管理员。

    只接受 zone.tab 里存在的名字，不走 shell；写的是 /etc/localtime 与 /etc/timezone，
    timedatectl 负责这两处，也顺带通知其它进程时区变了。
    """
    body = handler._read_json()
    if body is None:
        return
    tz = (body.get('timezone') or '').strip()
    if tz not in host.timezones():
        handler.send_json({'error': '时区不在列表里'}, 400)
        return
    try:
        r = subprocess.run(['/usr/bin/timedatectl', 'set-timezone', tz],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        handler.send_json({'error': '设置时区失败: %s' % e}, 500)
        return
    if r.returncode != 0:
        handler.send_json({'error': (r.stderr or r.stdout or '设置时区失败').strip()}, 500)
        return
    handler.send_json({'ok': True, 'current': host.timezone()})


def send_wifi_scan(handler):
    try:
        handler.send_json({'networks': wifi.scan()})
    except Exception as e:
        handler.send_json({'error': '扫描失败: %s' % e}, 500)


def wifi_connect(handler):
    """POST /api/wifi/connect {ssid, psk} —— 只允许超级管理员。"""
    body = handler._read_json()
    if body is None:
        return
    try:
        result = wifi.connect(body.get('ssid', ''), body.get('psk', ''))
    except Exception as e:
        handler.send_json({'error': str(e)}, 400)
        return
    handler.send_json(result)
