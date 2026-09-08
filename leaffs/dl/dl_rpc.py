#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Aria2 RPC 客户端
封装 addUri、addTorrent、tellStatus、pause、unpause、remove 等操作
"""

import os
import json
import time
import atexit
import signal
import urllib.request
import urllib.error
import threading
import subprocess

from .dl_utils import _log, ARIA2C_PATH, load_trackers, refresh_trackers_async, _assign_to_job
from . import dl_config
from leaffs.utils.core import UPLOAD_DIR, CACHE_DIR


RPC_PORT = 6800


class Aria2RPC:
    """Aria2 JSON-RPC 客户端"""

    def __init__(self, host='127.0.0.1', port=6800, secret='', timeout=5.0):
        self.url = f'http://{host}:{port}/jsonrpc'
        self.secret = secret
        self.timeout = timeout
        self._req_id = 0

    def _call(self, method, params=None):
        """调用 RPC 方法，返回结果"""
        self._req_id += 1
        payload = {
            'jsonrpc': '2.0',
            'id': str(self._req_id),
            'method': method,
        }
        if self.secret:
            payload['params'] = [f'token:{self.secret}']
            if params:
                payload['params'].extend(params)
        else:
            payload['params'] = params or []

        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            self.url,
            data=data,
            headers={'Content-Type': 'application/json'},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
            body = resp.read().decode('utf-8')
            result = json.loads(body)
            if 'error' in result:
                err = result['error']
                raise RuntimeError(f'Aria2 RPC 错误 [{err.get("code",-1)}]: {err.get("message","")}')
            return result.get('result')
        except urllib.error.URLError as e:
            raise RuntimeError(f'Aria2 RPC 连接失败: {e.reason}')
        except json.JSONDecodeError as e:
            raise RuntimeError(f'Aria2 RPC 响应解析失败: {e}')

    def add_uri(self, uri, options=None, position=None):
        """添加 URI 下载，返回 gid"""
        params = [[uri]]
        if options:
            params.append(options)
        if position is not None:
            if len(params) < 3:
                params.extend([{} for _ in range(3 - len(params))])
            params[2] = position
        return self._call('aria2.addUri', params)

    def add_torrent(self, torrent_base64, uris=None, options=None, position=None):
        """添加种子文件（base64 编码），返回 gid"""
        params = [torrent_base64]
        if uris:
            params.append(uris)
        else:
            params.append([])
        if options:
            params.append(options)
        if position is not None:
            while len(params) < 4:
                params.append({})
            params.append(position)
        return self._call('aria2.addTorrent', params)

    def tell_status(self, gid, keys=None):
        """查询任务状态"""
        params = [gid]
        if keys:
            params.append(keys)
        return self._call('aria2.tellStatus', params)

    def tell_active(self, keys=None):
        """获取所有活跃任务"""
        params = []
        if keys:
            params.append(keys)
        return self._call('aria2.tellActive', params)

    def tell_waiting(self, offset=0, num=100, keys=None):
        """获取等待队列"""
        params = [offset, num]
        if keys:
            params.append(keys)
        return self._call('aria2.tellWaiting', params)

    def tell_stopped(self, offset=0, num=100, keys=None):
        """获取已停止的任务"""
        params = [offset, num]
        if keys:
            params.append(keys)
        return self._call('aria2.tellStopped', params)

    def pause(self, gid):
        """暂停任务"""
        return self._call('aria2.pause', [gid])

    def unpause(self, gid):
        """恢复任务"""
        return self._call('aria2.unpause', [gid])

    def remove(self, gid, remove_files=False):
        """删除任务"""
        if remove_files:
            return self._call('aria2.removeDownloadResult', [gid])
        return self._call('aria2.remove', [gid])

    def force_remove(self, gid):
        """强制删除任务"""
        return self._call('aria2.forceRemove', [gid])

    def get_global_stat(self):
        """获取全局统计"""
        return self._call('aria2.getGlobalStat')

    def get_version(self):
        """获取版本信息"""
        return self._call('aria2.getVersion')

    def get_peers(self, gid):
        """获取任务的连接对等节点信息"""
        return self._call('aria2.getPeers', [gid])

    def change_option(self, gid, options):
        """修改任务选项"""
        return self._call('aria2.changeOption', [gid, options])

    def change_global_option(self, options):
        """修改全局选项"""
        return self._call('aria2.changeGlobalOption', [options])

    def purge_download_result(self):
        """清理已完成/已停止的任务记录"""
        return self._call('aria2.purgeDownloadResult')

    @staticmethod
    def parse_progress(status_data):
        """解析 tellStatus 返回数据为统一 task_info 字典"""
        info = {}
        if not status_data:
            return info

        status = status_data.get('status', '')
        info['rpc_gid'] = status_data.get('gid', '')

        if status == 'active':
            info['status'] = 'downloading'
        elif status == 'waiting':
            info['status'] = 'waiting'
        elif status == 'paused':
            info['status'] = 'paused'
        elif status == 'complete':
            info['status'] = 'completed'
        elif status == 'error':
            info['status'] = 'error'
        elif status == 'removed':
            info['status'] = 'cancelled'
        else:
            info['status'] = status

        total = int(status_data.get('totalLength', '0'))
        completed = int(status_data.get('completedLength', '0'))

        info['total_size'] = total
        info['downloaded'] = completed

        if total > 0:
            pct = int(completed * 100 / total)
            info['progress'] = min(pct, 100)
        else:
            info['progress'] = 0

        dl_speed = int(status_data.get('downloadSpeed', '0'))
        ul_speed = int(status_data.get('uploadSpeed', '0'))
        info['speed'] = Aria2RPC._format_speed(dl_speed)
        info['upload_speed'] = Aria2RPC._format_speed(ul_speed)
        info['connections'] = int(status_data.get('connections', '0'))

        files = status_data.get('files', [])
        if files:
            first_path = files[0].get('path', '')
            if first_path:
                info['filename'] = os.path.basename(first_path)
            total_sum = sum(int(f.get('length', '0')) for f in files)
            if total_sum > info.get('total_size', 0):
                info['total_size'] = total_sum

        bittorrent = status_data.get('bittorrent', {})
        if bittorrent:
            info_name = bittorrent.get('info', {}).get('name', '')
            if info_name:
                info['filename'] = info_name

        error_msg = status_data.get('errorMessage', '')
        if error_msg:
            info['error_msg'] = error_msg

        return info

    @staticmethod
    def _format_speed(bps):
        if bps <= 0:
            return '0 B/s'
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        i = 0
        v = float(bps)
        while v >= 1024 and i < len(units) - 1:
            v /= 1024
            i += 1
        return f'{v:.1f} {units[i]}/s' if i > 0 else f'{int(v)} B/s'


# ========== 全局 RPC 客户端与守护进程管理 ==========

_RPC_CLIENT = None
_RPC_CLIENT_LOCK = threading.Lock()
_ARIA2C_PROC = None
_ARIA2C_PID = None
_daemon_starting = False
_daemon_ready = False
_restart_in_progress = False      # 防止多线程同时对已退出的 aria2c 触发强制重启
_daemon_lock = threading.Lock()  # 防止并发启动 aria2c 守护进程
_RPC_SECRET = ''                 # 每次启动随机生成，防本机其他进程连 RPC


def get_rpc_client():
    """获取全局 RPC 客户端实例"""
    global _RPC_CLIENT
    if _RPC_CLIENT is None or _RPC_CLIENT.secret != _RPC_SECRET:
        with _RPC_CLIENT_LOCK:
            if _RPC_CLIENT is None or _RPC_CLIENT.secret != _RPC_SECRET:
                _RPC_CLIENT = Aria2RPC(host='127.0.0.1', port=RPC_PORT, secret=_RPC_SECRET)
    return _RPC_CLIENT


def get_aria2c_status():
    """aria2c 守护进程状态：unavailable / starting / ready / error / stopped

    ready 表示进程存活且 RPC 握手已成功（由启动线程确认）。
    供下载页通过 WebSocket 推送展示“启动中/就绪/失败”提示。
    """
    if not ARIA2C_PATH:
        return 'unavailable'
    proc = _ARIA2C_PROC
    if proc is not None and proc.poll() is None:
        return 'ready' if _daemon_ready else 'starting'
    if proc is not None:  # 进程已退出
        return 'error'
    if _daemon_starting:
        return 'starting'
    return 'stopped'


def _kill_aria2c_force():
    """
    强制终止所有 aria2c 进程（最终手段）
    直接使用 taskkill 通过进程名杀死
    """
    global _ARIA2C_PROC, _ARIA2C_PID
    _log('强制终止 aria2c 进程...')

    # 方法1：通过保存的 PID 终止
    pid_to_kill = _ARIA2C_PID
    if pid_to_kill:
        try:
            subprocess.run(
                ['taskkill', '/f', '/t', '/pid', str(pid_to_kill)],
                capture_output=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            _log(f'taskkill PID {pid_to_kill}')
        except Exception:
            pass

    # 方法2：通过进程名终止（兜底）
    try:
        subprocess.run(
            ['taskkill', '/f', '/im', 'aria2c.exe'],
            capture_output=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        _log('taskkill /im aria2c.exe')
    except Exception:
        pass

    # 方法3：terminate 已保存的进程对象
    if _ARIA2C_PROC:
        try:
            if _ARIA2C_PROC.poll() is None:
                _ARIA2C_PROC.terminate()
                _ARIA2C_PROC.wait(timeout=3)
        except Exception:
            try:
                _ARIA2C_PROC.kill()
                _ARIA2C_PROC.wait(timeout=2)
            except Exception:
                pass

    _ARIA2C_PROC = None
    _ARIA2C_PID = None
    _log('aria2c 强制终止完成')


def start_aria2c_daemon(base_dir=None):
    """启动 aria2c RPC 守护进程，返回 True/False"""
    global _ARIA2C_PROC, _ARIA2C_PID, _daemon_starting, _daemon_ready, _RPC_SECRET
    if _ARIA2C_PROC is not None:
        return True
    if not ARIA2C_PATH:
        _log('aria2c 不可用，无法启动 RPC 守护进程')
        _daemon_ready = False
        return False
    # 双检：避免并发调用重复拉起 aria2c（如后台启动线程与首个下载任务同时到达）
    with _daemon_lock:
        if _daemon_starting or _ARIA2C_PROC is not None:
            return True
        _daemon_starting = True
    _daemon_ready = False

    try:
        if base_dir is None:
            # 默认下载根 = 数据层的共享目录（与 HTTP 上传根一致，打包后可写）
            base_dir = UPLOAD_DIR
        os.makedirs(base_dir, exist_ok=True)
        # 会话/节点缓存属于运行缓存，放在 .cache，避免在共享目录根生成隐藏文件
        os.makedirs(CACHE_DIR, exist_ok=True)
        session_file = os.path.join(CACHE_DIR, '.aria2_session')
        dht_file = os.path.join(CACHE_DIR, '.dht.dat')

        # 每次启动生成随机 RPC secret（本机其他进程无法猜出，也就无法调用 RPC）
        import secrets as _secrets_mod
        _RPC_SECRET = _secrets_mod.token_urlsafe(24)

        cmd = [
            ARIA2C_PATH,
            '--enable-rpc',
            f'--rpc-secret={_RPC_SECRET}',
            f'--rpc-listen-port={RPC_PORT}',
            '--rpc-allow-origin-all',
            '--rpc-listen-all=false',
            '--dir', base_dir,
            '--max-connection-per-server=16',
            '--split=16',
            '--continue=true',
            '--allow-overwrite=true',
            '--summary-interval=0',
            '--console-log-level=notice',
            '--enable-dht=true',
            '--dht-listen-port=6881-6889',
            '--dht-message-timeout=10',
            '--bt-save-metadata=true',
            '--follow-torrent=true',
            '--seed-time=600',
            '--max-upload-limit=0',
            '--bt-max-peers=100',
            '--bt-request-peer-speed-limit=100K',
            '--peer-id-prefix=-TR2940-',
            '--user-agent=Transmission/2.94',
            f'--save-session={session_file}',
            f'--dht-file-path={dht_file}',
            '--enable-dht6=false',
            '--bt-tracker-interval=60',
            '--bt-tracker-timeout=10',
            '--bt-tracker-connect-timeout=10',
        ]
        # tracker 用本地已有列表（缓存/内置，毫秒级）；联网刷新放后台，不阻塞 daemon 启动
        trackers = load_trackers(network=False)
        if trackers:
            cmd.extend(['--bt-tracker', ','.join(trackers)])
        refresh_trackers_async()

        # C-14：bt_dht_public=false（默认）→ DHT 仅本机监听 + 关闭 LPD（磁力出站不受影响，
        # 仅公网入站连接减少）；true → DHT/LPD 对外保持开放（运维权衡项）。
        # 注意：aria2 1.37.0 的选项名是 --bt-enable-lpd（--enable-lpd 不存在，会导致启动失败）。
        if dl_config.get_bt_dht_public():
            cmd.append('--bt-enable-lpd=true')
        else:
            cmd.append('--dht-listen-addr=127.0.0.1')
            cmd.append('--bt-enable-lpd=false')

        _log(f'启动 aria2c RPC 守护进程: port={RPC_PORT}')
        _ARIA2C_PROC = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        )
        _ARIA2C_PID = _ARIA2C_PROC.pid
        _log(f'aria2c RPC 守护进程已启动, PID={_ARIA2C_PID}')
        # 加入 Windows Job Object - Python 被强制终止时自动杀死 aria2c
        _assign_to_job(_ARIA2C_PROC)
        # 等待 RPC 就绪：轮询探测，就绪即返回（不再固定 sleep 拖慢服务器启动）
        ready = False
        deadline = time.time() + 8.0
        while time.time() < deadline:
            try:
                rpc = get_rpc_client()
                ver = rpc.get_version()
                _log(f'aria2c RPC 连接成功, 版本: {ver.get("version", "unknown")}')
                ready = True
                break
            except Exception:
                time.sleep(0.25)
        if not ready:
            _log('aria2c RPC 连接超时，稍后重试下载时可能失败')
            _daemon_ready = False
        else:
            _daemon_ready = True
        with _daemon_lock:
            _daemon_starting = False
        return True
    except Exception as e:
        _log(f'启动 aria2c 守护进程失败: {e}')
        _ARIA2C_PROC = None
        _ARIA2C_PID = None
        _daemon_ready = False
        with _daemon_lock:
            _daemon_starting = False
        return False


def wait_aria2_ready(timeout=12):
    """等待 aria2 RPC 就绪；必要时触发一次启动。返回 bool

    用于首个 magnet/torrent 任务入池前同步等待，避免 aria2c 尚未就绪导致首任务必挂。
    除进程状态外还做真实 RPC 握手探测（启动线程内的就绪标记可能滞后）。
    进程已退出（status=='error'）时自愈：强制清理旧进程对象后重启一次，仍失败才返回 False。
    """
    try:
        st = get_aria2c_status()
        if st == 'ready':
            return True
        if st == 'unavailable':
            return False  # 无 aria2c 可执行文件，无法就绪
        if st == 'error':
            # 进程已退出：强制重启一次自愈。用 _restart_in_progress 保证同一时刻
            # 只有一个线程执行 kill+start（避免并发双启 / 误杀刚拉起的进程），
            # 并在持锁期间复查状态，防止基于过期 error 状态误杀新进程。
            do_restart = False
            with _daemon_lock:
                st_now = get_aria2c_status()
                if st_now == 'ready':
                    return True
                if st_now == 'error' and not _restart_in_progress:
                    _restart_in_progress = True
                    do_restart = True
            if do_restart:
                try:
                    _log('wait_aria2_ready: aria2c 进程已退出，强制重启一次')
                    _kill_aria2c_force()   # 仅在 status 为 error/stopped 时调用，安全
                    start_aria2c_daemon()  # 幂等：已在启动中/已就绪时内部直接返回
                except Exception as e:
                    _log(f'wait_aria2_ready 重启 aria2c 失败: {e}')
                finally:
                    with _daemon_lock:
                        _restart_in_progress = False
            # 其它线程正在重启 / 已改为其它状态：直接进入下方轮询等待重启结果
        elif st in ('stopped', 'starting'):
            try:
                start_aria2c_daemon()  # 幂等：已在启动中/已就绪时内部直接返回
            except Exception as e:
                _log(f'wait_aria2_ready 启动失败: {e}')
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = get_aria2c_status()
            if st == 'ready':
                return True
            try:
                ver = get_rpc_client().get_version()
                if ver:
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False
    except Exception as e:
        _log(f'wait_aria2_ready 异常: {e}')
        return False


# 注册 atexit 和信号处理器
def _register_cleanup():
    atexit.register(_kill_aria2c_force)
    try:
        def _sig_handler(signum, frame):
            _kill_aria2c_force()
            signal.signal(signum, signal.SIG_DFL)
            os._exit(128 + signum)
        signal.signal(signal.SIGTERM, _sig_handler)
        signal.signal(signal.SIGINT, _sig_handler)
    except Exception:
        pass

_register_cleanup()


# ========== aria2c 守护监控（挂了自动重启 + 状态变化推送） ==========

_status_cb = None                # 状态变化回调（leaffs 注入 _broadcast_download_daemon_status）
_daemon_watch_started = False
_daemon_watch_lock = threading.Lock()


def set_daemon_status_cb(cb):
    """注册 aria2c 状态变化回调（无参；服务端注入页面推送）"""
    global _status_cb
    _status_cb = cb


def start_daemon_watchdog():
    """启动 aria2c 守护监控线程（幂等）。

    覆盖“RPC 挂了没处理”：周期探测 aria2c 进程/连接状态——
      * 进程已退出(error)        → 自动强制重启一次；
      * 进程活着但 RPC 连不上(starting 超 15s，如启动异常/端口被占) → 强制重启一次；
      * 进程活着且握手成功但标记滞后 → 真实握手后置 ready；
      * 从未启动(stopped)        → 幂等拉起。
    状态相对上次有变化时调用 set_daemon_status_cb 注册的回调，驱动页面提示刷新。
    """
    global _daemon_watch_started
    with _daemon_watch_lock:
        if _daemon_watch_started:
            return
        _daemon_watch_started = True
    threading.Thread(target=_daemon_watch_loop, daemon=True).start()


def _restart_aria2c_once(reason):
    """强制重启一次 aria2c（_restart_in_progress 防多线程并发重启/误杀新进程）"""
    global _restart_in_progress, _daemon_ready
    do_it = False
    with _daemon_lock:
        if not _restart_in_progress:
            _restart_in_progress = True
            do_it = True
    if not do_it:
        return  # 其它线程正在重启，交给它
    try:
        _log(f'aria2c 守护监控: {reason}，强制重启')
        _daemon_ready = False
        _kill_aria2c_force()
        start_aria2c_daemon()
    except Exception as e:
        _log(f'aria2c 守护监控重启失败: {e}')
    finally:
        with _daemon_lock:
            _restart_in_progress = False


def _daemon_watch_loop():
    global _daemon_ready
    last_pushed = None
    starting_since = None
    while True:
        time.sleep(2.0)
        try:
            st = get_aria2c_status()
            now = time.time()
            if st == 'ready':
                starting_since = None
            elif st == 'starting':
                # 真实 RPC 握手：_daemon_ready 标记可能滞后（进程已就绪但标记未置）
                try:
                    if get_rpc_client().get_version():
                        _daemon_ready = True
                        st = 'ready'
                        starting_since = None
                        _log('aria2c 守护监控: 真实握手成功，状态置为 ready')
                except Exception:
                    pass
                if st == 'starting':
                    if starting_since is None:
                        starting_since = now
                    elif now - starting_since > 15:
                        _restart_aria2c_once('进程存活但 RPC 超过 15s 无法连接（启动异常）')
                        starting_since = None
                        st = get_aria2c_status()
            elif st == 'error':
                # 进程已退出：RPC 挂掉，自动重启（用户诉求核心）
                _restart_aria2c_once('aria2c 进程已退出')
                starting_since = None
                st = get_aria2c_status()
            elif st == 'stopped':
                # 从未启动（正常启动流程被跳过等）：幂等拉起
                try:
                    start_aria2c_daemon()
                except Exception:
                    pass
                starting_since = None
                st = get_aria2c_status()
            if st != last_pushed:
                last_pushed = st
                cb = _status_cb
                if cb is not None:
                    try:
                        cb()
                    except Exception:
                        pass
        except Exception:
            pass