"""本机状态接口：把 host 的分档采集结果交给网页。

只读为主，写操作只有无线网络连接这一处（`/api/wifi/connect`）。鉴权不在这里做 ——
分派层用 `_SUPER_ADMIN_ONLY_GET` / `_SUPER_ADMIN_ONLY_POST` 统一判，新增接口时
必须同时把路径加进那两个集合，否则就是开了个没门禁的入口。

数据通道见 `.work/PROTOCOL-2026-10-26-system-ws.md`：HTTP 只在打开页面时取一次全量，
之后由 WebSocket 推变化；HTTP 的 `scope=fast` 只给每秒档，供断线兜底轮询。
"""

import subprocess

import leaffs.system.host as host
import leaffs.system.wifi as wifi


def send_system(handler, scope='full'):
    """一次请求取一份。`scope='fast'` 时慢档字段不采集也不出现。

    这里不再挂缓存：采集已经分档，每秒档就是四个 /proc 文件，缓存省下的那点
    读取还不够做一次时间比较；而以前那两个「disks/services 各 5 秒」的槽从来没
    生效过 —— 1 秒那份 payload 已经把两者算进去了。
    """
    try:
        data = host.snapshot(scope)
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
