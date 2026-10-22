"""本机状态接口：把 host.collect() 的结果交给网页。

只读为主，写操作只有无线网络连接这一处（`/api/wifi/connect`）。鉴权不在这里做 ——
分派层用 `_SUPER_ADMIN_ONLY_GET` / `_SUPER_ADMIN_ONLY_POST` 统一判，新增接口时
必须同时把路径加进那两个集合，否则就是开了个没门禁的入口。
"""

import leaffs.system.wifi as wifi


def send_system(handler, collect):
    try:
        data = collect()
    except Exception as e:                     # 采集失败要说清楚，不能给半张表
        handler.send_json({'error': '系统状态读取失败: %s' % e}, 500)
        return
    handler.send_json(data)


def send_wifi_status(handler):
    try:
        handler.send_json(wifi.status())
    except Exception as e:
        handler.send_json({'error': '无线状态读取失败: %s' % e}, 500)


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
