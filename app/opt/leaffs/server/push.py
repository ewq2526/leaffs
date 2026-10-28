# -*- coding: utf-8 -*-
"""WebSocket 订阅与推送 —— 管理页实时帧 / 下载器状态 / 二维码事件广播。

历史：原分散于 leaffs.py，重构抽成独立模块：
  * 订阅集合（admin / download / qr）与对应锁；
  * 广播函数（经 WS 事件循环 run_coroutine_threadsafe 投递发送，不阻塞调用线程）；
  * 管理页每秒快照帧循环。

WS 连接处理方（leaffs.py 的 ws_handler / 后续 server.ws）通过本模块 API 增删订阅。
"""
import asyncio
import json
import threading
import time

import leaffs.auth.core as _ac
import leaffs.config.core as _cfg
import leaffs.files.api as _fs_api
import leaffs.files.core as _fs
import leaffs.system.host as _sys_host
import leaffs.utils.log as _ut_log

# ========== 订阅集合 ==========

_download_subscribers = set()
_ds_lock = threading.Lock()

_admin_subscribers = set()
_admin_lock = threading.Lock()

_qr_waiters = {}          # sid -> set(websocket)
_qr_waiters_lock = threading.Lock()

_gallery_subscribers = set()   # 预览页订阅：文件树变化通知
_gallery_lock = threading.Lock()

_system_subscribers = set()    # 系统页订阅：本机状态增量
_system_lock = threading.Lock()

# 慢档（分区/服务/无线/温度/电池）不会一秒一变：兜底周期 30 秒，
# 事件（udev / systemd / wpa_supplicant）到来时用 `_slow_dirty` 提前这一次。
_SLOW_INTERVAL = 30.0
_slow_dirty = threading.Event()

_main_loop = None
_main_loop_lock = threading.Lock()


def set_main_loop(loop):
    """WS 事件循环就绪后注册（供 run_coroutine_threadsafe 投递发送）"""
    global _main_loop
    with _main_loop_lock:
        _main_loop = loop


def get_main_loop():
    with _main_loop_lock:
        return _main_loop


# ---------- 订阅增删（ws_handler 专用 API；全部幂等） ----------

def admin_sub_add(ws):
    with _admin_lock:
        _admin_subscribers.add(ws)


def admin_sub_remove(ws):
    with _admin_lock:
        _admin_subscribers.discard(ws)


def dl_sub_add(ws):
    with _ds_lock:
        _download_subscribers.add(ws)


def dl_sub_remove(ws):
    with _ds_lock:
        _download_subscribers.discard(ws)


def qr_sub_add(sid, ws):
    with _qr_waiters_lock:
        _qr_waiters.setdefault(sid, set()).add(ws)


def qr_sub_remove(sid, ws):
    with _qr_waiters_lock:
        st = _qr_waiters.get(sid)
        if st:
            st.discard(ws)
            if not st:
                del _qr_waiters[sid]


def qr_cleanup_ws(ws):
    """连接结束时摘除其全部二维码等待注册（幂等）"""
    with _qr_waiters_lock:
        for sid in list(_qr_waiters.keys()):
            st = _qr_waiters[sid]
            st.discard(ws)
            if not st:
                del _qr_waiters[sid]


# ---------- 文件树变化订阅（预览页；用 WS 推送代替前端轮询） ----------

def gallery_sub_add(ws):
    with _gallery_lock:
        _gallery_subscribers.add(ws)


def gallery_sub_remove(ws):
    with _gallery_lock:
        _gallery_subscribers.discard(ws)


# ---------- 本机状态订阅（系统页；每秒增量代替页面自己轮询 HTTP） ----------

def system_sub_add(ws):
    with _system_lock:
        was_empty = not _system_subscribers
        _system_subscribers.add(ws)
    if was_empty:
        # 没有订阅者时循环不采样（这是有意的），于是 cpu/io 的累计值基线会断档。
        # 重新有人订阅先重建基线，否则第一拍算出来的是"停了多久的平均值"。
        try:
            _sys_host.reseed()
        except Exception:
            pass


def system_sub_remove(ws):
    with _system_lock:
        _system_subscribers.discard(ws)


def system_nudge():
    """慢档可能有变化时叫醒推送循环（udev / systemd / wpa_supplicant 事件的入口）。

    事件源接上之前，慢档只靠 `_SLOW_INTERVAL` 那个兜底周期。
    """
    _slow_dirty.set()


# ========== 广播 ==========

async def safe_send(ws, msg):
    """安全发送 WebSocket 消息，异常不影响事件循环"""
    try:
        await ws.send(msg)
    except Exception:
        pass


def _schedule_send(ws, text):
    """向单连接投递发送任务；事件循环未就绪/投递失败返回 False"""
    loop = get_main_loop()
    if loop is None:
        return False
    try:
        asyncio.run_coroutine_threadsafe(safe_send(ws, text), loop)
        return True
    except Exception:
        return False


def broadcast_qr_consumed(sid):
    """通知等待该二维码的页面：已被扫码登录"""
    with _qr_waiters_lock:
        wss = _qr_waiters.pop(sid, set())
    if wss:
        text = json.dumps({'type': 'qr_consumed', 'sid': sid})
        for ws in wss:
            _schedule_send(ws, text)


def broadcast_gallery_changed():
    """文件树有变化（上传/删除/新建目录…）→ 通知预览页重新拉一次列表。

    只发信号不带列表：列表由页面按自身权限自己拉（游客只看公共目录），
    服务端不必给每个订阅者各算一遍全树，也不会把无权看的路径推出去。
    """
    with _gallery_lock:
        wss = list(_gallery_subscribers)
    if not wss:
        return
    text = json.dumps({'type': 'gallery'})
    for ws in wss:
        _schedule_send(ws, text)


def build_connections_payload():
    """构造连接用户列表快照（供 /api/connections 与 WebSocket 管理推送共用）"""
    conns = _cfg.get_connections()
    now = time.time()
    result = [{'ip': ip, 'first_seen': i['first_seen'], 'last_seen': i['last_seen'],
               'user_agent': i['user_agent'], 'request_count': i['request_count'],
               'username': i.get('username', ''), 'device': i.get('device', ''),
               'role': _ut_log.show_role_display(i.get('role', '')),
               'role_code': i.get('role', ''),
               'active_secs': int(now - i['last_seen'])} for ip, i in conns.items()]
    result.sort(key=lambda x: x['last_seen'], reverse=True)
    return result


def admin_snapshot_payload():
    """构造管理页一帧数据（等价 /api/stats + /api/connections）

    文件统计底层采用“变更即失效”的缓存（files.core.get_server_stats 挂靠
    invalidate_folder_cache 钩子）：无文件变更时每秒直接复用缓存结果，
    有变更时下一次读取即重算，因此逐帧构建开销很小。
    """
    try:
        stats = _fs_api.build_stats_data(
            _fs.get_server_stats, _fs.get_folder_size, _fs.has_ffmpeg,
            _fs.thumbnail_backend,
            _cfg.get_max_concurrent, _cfg.COPY_BUFFER_SIZE, _cfg.get_speed_limit,
            _cfg.get_connections, _cfg.PORT, _cfg.get_guest_mode,
            _cfg.get_default_user_quota, _cfg.get_public_quota,
            _cfg.get_total_quota, _fs.UPLOAD_DIR)
    except Exception:
        return None
    return {'type': 'admin_data', 'ts': time.time(),
            'stats': stats, 'connections': build_connections_payload(),
            'users': _ac.list_users()}


def admin_push_loop():
    """每秒向订阅管理推送的 WebSocket 发送一帧管理页快照"""
    while True:
        try:
            with _admin_lock:
                targets = list(_admin_subscribers)
            if targets and get_main_loop() is not None:
                payload = admin_snapshot_payload()
                if payload is not None:
                    text = json.dumps(payload)
                    for ws in targets:
                        if not _schedule_send(ws, text):
                            with _admin_lock:
                                _admin_subscribers.discard(ws)
        except Exception:
            pass
        time.sleep(1)


def system_snapshot_payload():
    """订阅成功后立刻回的一帧全量（等价 `GET /api/system`）。

    `back` 是这一帧采集本身花的毫秒数：它要是两位数，说明读取又膨了。
    """
    t0 = time.perf_counter()
    data = _sys_host.snapshot('full')
    return {'type': 'system_snap', 'data': data, 't': time.time(),
            'back': int((time.perf_counter() - t0) * 1000)}


def system_push_loop():
    """每秒一整帧：把当前所有监控值原样发出去，不做"变没变"的过滤。

    以前这里只有变过的键才进帧，想省那几百字节。代价是页面必须自己判断
    "这个键没来是因为没变、还是因为没采"，而两条路都通向编数据：
    拿上一拍补一格，上一拍是 100 MB/s、这一拍盘没动，补上去就是假的 100 MB/s。
    监视器要的是每秒一条真实样本，省这点流量的做法本身就是错的。

    慢档（分区、服务、无线、温度、进程榜）仍然 30 秒才采一次 —— 那是**采集**的
    成本，不是发送的成本；没到采集点就在帧里带上一次采到的值（它确实还没变）。

    三条硬约束不变：
    * 没有订阅者时什么都不采，循环体只剩一次 sleep；
    * 帧里的值是整块（不做深合并），页面据此整块替换；
    * 帧自带 `t` 与 `back`（这一帧采集花的毫秒数）。
    """
    last_slow = 0.0
    slow_data = {}
    while True:
        try:
            with _system_lock:
                targets = list(_system_subscribers)
            if not targets:
                # 没人看就不采。
                _slow_dirty.clear()
                time.sleep(1.0)
                continue
            t0 = time.perf_counter()
            data = _sys_host.fast()
            now = time.monotonic()
            if now - last_slow >= _SLOW_INTERVAL or _slow_dirty.is_set():
                _slow_dirty.clear()
                last_slow = now
                slow_data = _sys_host.slow()
            data.update(slow_data)
            # 在线设备数：桌面任务栏要它。数一下连接字典的大小，没有额外开销。
            try:
                data['devices'] = len(_cfg.get_connections())
            except Exception:
                data['devices'] = 0
            back = int((time.perf_counter() - t0) * 1000)
            text = json.dumps({'type': 'system_delta', 'set': data,
                               't': time.time(), 'back': back})
            for ws in targets:
                if not _schedule_send(ws, text):
                    with _system_lock:
                        _system_subscribers.discard(ws)
        except Exception:
            pass
        time.sleep(1.0)


def broadcast_download_daemon_status():
    """向所有下载器订阅者推送 aria2c 守护进程状态（页面据此显示启动中/就绪/失败）"""
    from leaffs.dl import dl_rpc as _dlr
    status = _dlr.get_aria2c_status()
    with _ds_lock:
        if not _download_subscribers or get_main_loop() is None:
            return
        text = json.dumps({'type': 'daemon_status', 'status': status})
        for ws in list(_download_subscribers):
            if not _schedule_send(ws, text):
                _download_subscribers.discard(ws)


def broadcast_download_update(task_data):
    """广播下载进度更新到所有 WebSocket 客户端（按用户/角色隔离）

    管理员/超级管理员收全部；普通用户只收属于自己的任务更新，
    避免通过 WS 推送看到/渲染到其它用户的任务。
    """
    with _ds_lock:
        if not _download_subscribers or get_main_loop() is None:
            return
        msg = json.dumps({'type': 'download_update', 'task': task_data})
        task_user = task_data.get('user') or ''
        for ws in list(_download_subscribers):
            role = getattr(ws, 'cached_role', None)
            user = getattr(ws, 'cached_user', '') or ''
            try:
                if role not in ('admin', 'super_admin'):
                    # 普通用户只收自己发起(用户名一致)的任务更新
                    if not user or task_user != user:
                        continue
                _schedule_send(ws, msg)
            except Exception:
                _download_subscribers.discard(ws)
