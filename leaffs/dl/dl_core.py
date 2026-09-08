#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一下载管理器
管理任务池、自动识别下载类型、并发控制、进度回调
支持多用户隔离：每个用户只能管理自己的任务，管理员可看全部
"""

import os
import time
import threading

from .dl_utils import _log, decode_thunder, is_magnet, is_torrent_url, is_ed2k
from .dl_utils import prefetch_http_torrent
from . import dl_config
from .dl_http import HttpDownloader
from .dl_m3u8 import M3u8Downloader
from .dl_torrent import TorrentDownloader
from .dl_magnet import MagnetDownloader
from .dl_rpc import get_rpc_client, start_aria2c_daemon, wait_aria2_ready


class DownloadLimitError(Exception):
    """下载任务数/并发/总量超限（HTTP 429，见 C-09）"""


def get_downloader_for_url(url):
    """
    根据 URL 自动识别并返回对应的下载器实例
    返回:
        (downloader_instance, download_type)
        download_type: 'http' | 'm3u8' | 'torrent' | 'magnet'
    """
    decoded = decode_thunder(url)
    if is_magnet(decoded) or url.startswith('thunder://'):
        return MagnetDownloader(), 'magnet'

    if is_torrent_url(decoded):
        return TorrentDownloader(), 'torrent'

    if is_ed2k(decoded):
        raise ValueError('不支持 ed2k（电驴）协议下载')

    if '.m3u8' in decoded.lower():
        return M3u8Downloader(), 'm3u8'

    return HttpDownloader(), 'http'


class DownloadTask:
    """单个下载任务包装"""

    def __init__(self, task_id, url, save_dir, downloader, dl_type, user='', **kwargs):
        self.id = task_id
        self.url = url
        self.save_dir = save_dir
        self.downloader = downloader
        self.dl_type = dl_type
        self.user = user
        self.kwargs = kwargs
        # C-08c：远程 http(s).torrent 任务记录原始 URL，供 retry 在本地临时副本
        # 丢失时重新受控预拉取
        self.origin_url = kwargs.get('origin_url', '') or ''

        self.status = 'waiting'
        self.progress = 0
        self.downloaded = 0
        self.total_size = 0
        self.speed = '0 B/s'
        self.filename = ''
        self.error_msg = ''
        self.result = None
        self.connections = 0
        self.seeds = 0
        self.peers = 0
        self.dht_nodes = 0
        self.upload_speed = '0 B/s'
        self.rpc_port = 0
        self.phase = 'file'
        self.metadata_progress = 0
        self.metadata_total = 0
        self.metadata_done = False

        # RPC 相关
        self.rpc_gid = ''

        self._cancel_flag = threading.Event()
        self._thread = None
        self._start_time = 0
        self._last_update = 0
        self._end_time = 0  # C-09：终态时间戳（completed 超龄自动清理依据）

    def wait_thread(self, timeout=0.5):
        """等待下载线程完全退出"""
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def to_dict(self):
        return {
            'id': self.id,
            'url': self.url,
            'save_dir': self.save_dir,
            'type': self.dl_type,
            'user': self.user,
            'status': self.status,
            'progress': self.progress,
            'downloaded': self.downloaded,
            'total_size': self.total_size,
            'speed': self.speed,
            'filename': self.filename,
            'error_msg': self.error_msg,
            'connections': getattr(self, 'connections', 0),
            'seeds': getattr(self, 'seeds', 0),
            'peers': getattr(self, 'peers', 0),
            'dht_nodes': getattr(self, 'dht_nodes', 0),
            'upload_speed': getattr(self, 'upload_speed', '0 B/s'),
            'rpc_port': getattr(self, 'rpc_port', 0),
            'phase': getattr(self, 'phase', 'file'),
            'metadata_progress': getattr(self, 'metadata_progress', 0),
            'metadata_total': getattr(self, 'metadata_total', 0),
            'metadata_done': getattr(self, 'metadata_done', False),
            'rpc_gid': getattr(self, 'rpc_gid', ''),
        }


# 全局广播函数（由主服务器设置）
_broadcast_fn = None

def set_broadcast_fn(fn):
    """设置广播回调函数"""
    global _broadcast_fn
    _broadcast_fn = fn


class DownloadManager:
    """统一下载管理器（支持多用户隔离）"""

    def __init__(self):
        self._tasks = {}
        self._lock = threading.Lock()
        self._counter = 0
        # 注意：aria2c RPC 守护进程不在构造时同步启动 —— 那会阻塞模块导入与服务器启动；
        # 由服务端在 start_server 中用后台线程调用 start_daemon() 启动

    def start_daemon(self):
        """启动 aria2c RPC 守护进程（内部幂等，可安全重复调用）"""
        start_aria2c_daemon()

    def _notify_update(self, task):
        if _broadcast_fn and task:
            try:
                _broadcast_fn(task.to_dict())
            except Exception as e:
                import traceback
                traceback.print_exc()

    def _gen_id(self):
        self._counter += 1
        return 'dl_' + str(int(time.time())) + '_' + str(self._counter)

    def get_config(self):
        return dl_config.load_config()

    def set_config(self, max_concurrent=None, speed_limit=None):
        kwargs = {}
        if max_concurrent is not None:
            kwargs['max_concurrent'] = int(max_concurrent)
        if speed_limit is not None:
            kwargs['speed_limit'] = int(speed_limit)
        if kwargs:
            dl_config.save_config(**kwargs)

    def add_download(self, url, save_dir, user='', **kwargs):
        print(f'[DL-CORE] add_download: url={url[:60]}... save_dir={save_dir} user={user}', flush=True)
        task_id = self._gen_id()
        print(f'[DL-CORE] 生成 task_id={task_id}', flush=True)
        downloader, dl_type = get_downloader_for_url(url)
        print(f'[DL-CORE] 下载器类型: {dl_type}, 下载器: {type(downloader).__name__}', flush=True)

        # aria2 首个任务竞态：magnet/torrent 依赖 RPC，入池/开线程前先同步等待
        # aria2c 就绪（最多 12s，必要时触发一次启动），避免首任务必挂。
        if dl_type in ('magnet', 'torrent'):
            try:
                if not wait_aria2_ready(12):
                    _log(f'add_download: aria2 RPC 未就绪，{dl_type} 任务 {task_id} 仍将创建但下载可能在线程内失败')
            except Exception as e:
                _log(f'add_download: wait_aria2_ready 异常: {e}')

        # C-09 预检：先做超龄清理与限额判定，避免对超限用户做无谓的受控预拉取
        #（真正入池时会在持锁状态下二次判定，保证原子性）。
        with self._lock:
            self._cleanup_expired_tasks_locked()
            ok_limits, limit_err = self._limits_ok_locked(user)
            if not ok_limits:
                raise DownloadLimitError(limit_err)

        # C-08c 兜底：任何经 http(s) URL 直达本方法的 torrent 一律受控预拉取
        # （8MB 上限、解析+路径预检通过后落盘），杜绝 http URL 交给 aria2 addUri 自拉。
        if dl_type == 'torrent' and (url.startswith('http://') or url.startswith('https://')):
            kwargs.setdefault('origin_url', url)
            url = prefetch_http_torrent(url)

        task = DownloadTask(task_id, url, save_dir, downloader, dl_type, user=user, **kwargs)
        task.filename = kwargs.get('filename', '')
        print(f'[DL-CORE] 任务创建: id={task.id} type={task.dl_type}', flush=True)

        # C-09：入池前再次懒清理 + 限额判定（与插入同锁原子执行）
        with self._lock:
            self._cleanup_expired_tasks_locked()
            ok_limits, limit_err = self._limits_ok_locked(user)
            if not ok_limits:
                raise DownloadLimitError(limit_err)
            self._tasks[task_id] = task
            print(f'[DL-CORE] 任务加入池, 当前任务数={len(self._tasks)}', flush=True)

        self._begin_download(task)
        return task_id

    def _cleanup_expired_tasks_locked(self):
        """C-09 懒清理：删除 completed 且超过 max_task_age_days 的旧任务（须持锁调用）。

        completed 任务的 _end_time 在进入终态时写入；超龄任务在下一次 add 前移除，
        使其不再占用 per-user/总任务名额。
        """
        max_age_days = dl_config.get_max_task_age_days()
        if max_age_days <= 0:
            return
        cutoff = time.time() - max_age_days * 86400.0
        for tid in list(self._tasks.keys()):
            t = self._tasks[tid]
            if t.status == 'completed':
                end = getattr(t, '_end_time', 0) or 0
                if end and end < cutoff:
                    del self._tasks[tid]

    def _limits_ok_locked(self, user):
        """C-09 限额判定（须持锁调用）：max_total_tasks / max_user_tasks / max_user_active。

        返回 (ok, err)。0 或负值表示该维度不限制。active 口径 = waiting+downloading。
        """
        owner = user or '__anon__'
        max_total = dl_config.get_max_total_tasks()
        max_user_tasks = dl_config.get_max_user_tasks()
        max_user_active = dl_config.get_max_user_active()
        if max_total > 0 and len(self._tasks) >= max_total:
            return False, f'下载任务总数已达上限（{max_total}），请先清理旧任务'
        user_total = sum(1 for t in self._tasks.values() if t.user == owner)
        if max_user_tasks > 0 and user_total >= max_user_tasks:
            return False, f'任务数已达上限（每个用户最多 {max_user_tasks} 个任务）'
        user_active = sum(1 for t in self._tasks.values()
                          if t.user == owner and t.status in ('waiting', 'downloading'))
        if max_user_active > 0 and user_active >= max_user_active:
            return False, f'下载并发已满（最多 {max_user_active} 个同时下载）'
        return True, ''

    def _begin_download(self, task):
        print(f'[DL-CORE] _begin_download: task_id={task.id} dl_type={task.dl_type}', flush=True)

        def _run():
            print(f'[DL-CORE] _run 线程启动: task_id={task.id}', flush=True)
            task._start_time = time.time()
            task.status = 'downloading'
            print(f'[DL-CORE] 设置 status=downloading, 调用 _notify_update', flush=True)
            self._notify_update(task)

            try:
                print(f'[DL-CORE] 启动下载: type={task.dl_type} url={task.url[:60]}...', flush=True)
                result = task.downloader.download(
                    task.url, task.save_dir,
                    on_progress=self._make_progress_cb(task),
                    cancel_flag=task._cancel_flag,
                    **self._get_extra_kwargs(task),
                )

                print(f'[DL-CORE] 下载完成, result={result}', flush=True)
                task.result = result

                if not task._cancel_flag.is_set():
                    if result.get('success'):
                        task.status = 'completed'
                        task.progress = 100
                        task.filename = result.get('filename', '')
                        task.total_size = result.get('total_size', 0)
                        task.downloaded = task.total_size
                        print(f'[DL-CORE] 下载成功: filename={task.filename}', flush=True)
                    elif result.get('status') == 'partial':
                        task.status = 'partial'
                        task.error_msg = result.get('error', '')
                        print(f'[DL-CORE] 下载部分完成: {task.error_msg}', flush=True)
                    else:
                        task.status = 'error'
                        task.error_msg = result.get('error', '未知错误')
                        print(f'[DL-CORE] 下载失败: {task.error_msg}', flush=True)

                    task._end_time = time.time()  # C-09 终态时间戳（供超龄清理）
                    print(f'[DL-CORE] 最终通知, status={task.status}', flush=True)
                    self._notify_update(task)

            except Exception as e:
                print(f'[DL-CORE] 下载异常: {type(e).__name__}: {e}', flush=True)
                import traceback
                traceback.print_exc()
                if not task._cancel_flag.is_set():
                    task.status = 'error'
                    task.error_msg = str(e)
                    task._end_time = time.time()  # C-09 终态时间戳
                    print(f'[DL-CORE] 异常通知, status=error', flush=True)
                    self._notify_update(task)

        task._thread = threading.Thread(target=_run, daemon=True)
        task._thread.start()

    def _get_extra_kwargs(self, task):
        """根据下载类型返回额外参数"""
        kwargs = {}
        if task.dl_type == 'torrent':
            sel = task.kwargs.get('selected_files')
            if sel:
                kwargs['selected_files'] = sel
        elif task.dl_type == 'm3u8':
            fn = task.kwargs.get('filename')
            if fn:
                kwargs['filename'] = fn
        elif task.dl_type == 'http':
            fn = task.kwargs.get('filename')
            if fn:
                kwargs['filename'] = fn
            sp = dl_config.get_speed_limit()
            if sp:
                kwargs['speed_limit'] = sp
        elif task.dl_type == 'magnet':
            pass
        return kwargs

    def _make_progress_cb(self, task):
        def on_progress(info):
            task.progress = info.get('progress', task.progress)
            task.downloaded = info.get('downloaded', task.downloaded)
            task.total_size = info.get('total_size', task.total_size)
            task.speed = info.get('speed', task.speed)
            task.filename = info.get('filename', task.filename)
            task.connections = info.get('connections', task.connections)
            task.seeds = info.get('seeds', task.seeds)
            task.peers = info.get('peers', task.peers)
            task.dht_nodes = info.get('dht_nodes', task.dht_nodes)
            task.upload_speed = info.get('upload_speed', task.upload_speed)
            rpc_port = info.get('rpc_port', 0)
            if rpc_port:
                task.rpc_port = rpc_port
            # 磁力链接分阶段进度字段
            task.phase = info.get('phase', task.phase)
            task.metadata_progress = info.get('metadata_progress', task.metadata_progress)
            task.metadata_total = info.get('metadata_total', task.metadata_total)
            task.metadata_done = info.get('metadata_done', task.metadata_done)
            # RPC GID
            rpc_gid = info.get('rpc_gid', '')
            if rpc_gid:
                task.rpc_gid = rpc_gid
            self._notify_update(task)
        return on_progress

    @staticmethod
    def _can_manage(task, user, role):
        """任务归属判断：管理员可操作全部；普通用户只能操作自己的任务"""
        if role in ('admin', 'super_admin'):
            return True
        if not task or not user or not task.user:
            return False
        return user == task.user

    def can_manage_task(self, task_id, user='', role=''):
        with self._lock:
            task = self._tasks.get(task_id)
            return bool(task) and self._can_manage(task, user, role)

    def pause_task(self, task_id, user='', role=''):
        """暂停任务（校验用户归属），通过 RPC 暂停"""
        task = None
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or not self._can_manage(task, user, role):
                return False
            if task.status == 'downloading':
                rpc_gid = getattr(task, 'rpc_gid', '')
                if rpc_gid:
                    try:
                        rpc = get_rpc_client()
                        rpc.pause(rpc_gid)
                    except Exception as e:
                        _log(f'RPC pause 失败: {e}')
                task._cancel_flag.set()
                task.status = 'paused'
                self._notify_update(task)
        if task:
            task.wait_thread()
            return True
        return False

    def resume_task(self, task_id, user='', role=''):
        """恢复暂停的任务（校验用户归属），通过 RPC 恢复"""
        task = None
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or not self._can_manage(task, user, role):
                return False
            if task.status == 'paused':
                pass
            else:
                task = None
        if task:
            task.wait_thread()
            rpc_gid = getattr(task, 'rpc_gid', '')
            with self._lock:
                if rpc_gid:
                    try:
                        rpc = get_rpc_client()
                        rpc.unpause(rpc_gid)
                    except Exception as e:
                        _log(f'RPC unpause 失败: {e}')
                task._cancel_flag = threading.Event()
                task.status = 'waiting'
                task._end_time = 0
            self._begin_download(task)
            return True
        return False

    def retry_task(self, task_id, user='', role=''):
        """重跑已结束的下载任务（校验用户归属，同其它任务方法）。

        置回 waiting、复位取消标志后重建下载线程（参考 resume_task 的模式）。
        返回 {'success': True} 或 {'success': False, 'error': ...}；归属/存在失败带 'forbidden'。
        """
        task = None
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or not self._can_manage(task, user, role):
                return {'success': False, 'forbidden': True, 'error': '任务不存在或无权限操作'}
            if task.status not in ('completed', 'error', 'cancelled', 'partial'):
                return {'success': False, 'error': '任务未结束，无法重试'}
            # 复位状态与取消标志（旧线程已结束；wait_thread 兜底等待）
            task._cancel_flag = threading.Event()
            task.status = 'waiting'
            task.progress = 0
            task.downloaded = 0
            task.total_size = 0
            task.speed = '0 B/s'
            task.error_msg = ''
            task.result = None
            task.rpc_gid = ''
            task._end_time = 0
            # C-08c：远程 torrent 的本地预拉取副本可能已被清理——回退原始 URL 以便
            # 重试时重新受控预拉取；本地副本仍在则直接用本地文件
            if task.dl_type == 'torrent':
                src = task.url or ''
                origin = getattr(task, 'origin_url', '') or ''
                if not (os.path.isfile(src) or src.startswith('http://') or src.startswith('https://')):
                    if origin.startswith('http://') or origin.startswith('https://'):
                        task.url = origin
        if task:
            task.wait_thread()
            # magnet/torrent 重试同样需要 aria2 RPC 就绪（最多阻塞 12s）
            if task.dl_type in ('magnet', 'torrent'):
                try:
                    if not wait_aria2_ready(12):
                        _log(f'retry_task: aria2 RPC 未就绪，任务 {task_id} 已复位但下载可能失败')
                except Exception as e:
                    _log(f'retry_task: wait_aria2_ready 异常: {e}')
            self._begin_download(task)
            return {'success': True}
        return {'success': False, 'error': '任务不存在'}

    def _delete_task_files(self, task):
        """只删除该任务实际产出的本地文件，绝不做 save_dir 递归整目录删除"""
        if not task:
            return
        save_dir = task.save_dir or ''
        dl_type = task.dl_type or ''

        if dl_type == 'http':
            # 仅删除与 task.filename 相关的两个文件（filename 为空则跳过）
            filename = (task.filename or '').strip()
            if not filename or not save_dir:
                return
            base = os.path.basename(filename)  # 防御：只允许单文件名
            for name in (base, base + '.tmp'):
                try:
                    fp = os.path.join(save_dir, name)
                    if os.path.isfile(fp):
                        os.remove(fp)
                except Exception:
                    pass
            return

        if dl_type in ('magnet', 'torrent'):
            # 这些类型的文件由 aria2 管理：删除交给 RPC remove(remove_files=True)
            # 或用户自行处理，不在本地递归删除
            return

        if dl_type == 'm3u8':
            # m3u8 若保存了单文件结果则删除该结果文件（存在于 save_dir 下时只删该文件）
            if not save_dir:
                return
            result = task.result if isinstance(task.result, dict) else {}
            norm_save = os.path.normpath(save_dir)
            for key in ('file_path', 'filename'):
                val = (result.get(key) or '').strip()
                if not val:
                    continue
                fp = val if os.path.isabs(val) else os.path.join(save_dir, os.path.basename(val))
                try:
                    np = os.path.normpath(fp)
                    if np.startswith(norm_save + os.sep) and os.path.isfile(np):
                        os.remove(np)
                except Exception:
                    pass
            return

    def _cleanup_task_aria2(self, task):
        """C-12：取消后清理该任务的 .aria2 控制文件（按任务已知文件名精确匹配）。

        不做目录级通配删除，避免误删其它任务/用户的断点续传控制文件。
        """
        if not task:
            return
        save_dir = task.save_dir or ''
        if not save_dir:
            return
        candidates = []
        fn = (getattr(task, 'filename', '') or '').strip()
        if fn:
            candidates.append(os.path.basename(fn))
        res = task.result if isinstance(task.result, dict) else {}
        rfn = (res.get('filename') or '').strip()
        if rfn:
            rfn = os.path.basename(rfn)
            if rfn not in candidates:
                candidates.append(rfn)
        for base in candidates:
            if not base:
                continue
            try:
                fp = os.path.join(save_dir, base + '.aria2')
                if os.path.isfile(fp):
                    os.remove(fp)
                    _log(f'已清理 .aria2 控制文件: {fp}')
            except Exception as e:
                _log(f'清理 .aria2 失败: {e}')

    def cancel_task(self, task_id, user='', role=''):
        """取消任务（校验用户归属），通过 RPC 移除。

        取消只停止任务，不再删除任何本地文件（文件语义由删除接口/aria2 管理）；
        C-12：顺带清理该任务的 .aria2 控制文件残留。
        """
        task = None
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or not self._can_manage(task, user, role):
                return False
            rpc_gid = getattr(task, 'rpc_gid', '')
            if rpc_gid:
                try:
                    rpc = get_rpc_client()
                    rpc.force_remove(rpc_gid)
                except Exception as e:
                    _log(f'RPC remove 失败: {e}')
            task._cancel_flag.set()
            task.status = 'cancelled'
            task._end_time = time.time()
            self._notify_update(task)
        if task:
            task.wait_thread()
            self._cleanup_task_aria2(task)
            return True
        return False

    def delete_task(self, task_id, user='', role='', delete_files=False):
        task = None
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or not self._can_manage(task, user, role):
                return False
            rpc_gid = getattr(task, 'rpc_gid', '')
            if rpc_gid:
                try:
                    rpc = get_rpc_client()
                    rpc.remove(rpc_gid, remove_files=delete_files)
                except Exception as e:
                    _log(f'RPC remove 失败: {e}')
            task._cancel_flag.set()
        if task:
            task.wait_thread()
            if delete_files:
                self._delete_task_files(task)
            with self._lock:
                self._tasks.pop(task_id, None)
            return True
        return False

    def get_task(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            return task.to_dict() if task else None

    def get_user_tasks(self, user):
        with self._lock:
            tasks = []
            for tid in sorted(self._tasks.keys(), reverse=True):
                t = self._tasks[tid]
                if t.user == user:
                    tasks.append(t.to_dict())
            return tasks[:50]

    def get_all_tasks(self):
        with self._lock:
            tasks = []
            for tid in sorted(self._tasks.keys(), reverse=True):
                tasks.append(self._tasks[tid].to_dict())
            return tasks[:200]

    def get_all_tasks_grouped(self):
        with self._lock:
            grouped = {}
            for tid in sorted(self._tasks.keys(), reverse=True):
                t = self._tasks[tid]
                u = t.user or '__anon__'
                if u not in grouped:
                    grouped[u] = []
                grouped[u].append(t.to_dict())
            for u in grouped:
                grouped[u] = grouped[u][:50]
            return grouped

    def parse_torrent_url(self, url):
        from .dl_utils import parse_torrent_url as _parse
        try:
            files = _parse(url)
            return {'success': True, 'files': files, 'file_count': len(files)}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def parse_uploaded_torrent(self, file_data, filename='upload.torrent'):
        downloader = TorrentDownloader()
        return downloader.parse_upload(file_data, filename)

    def merge_m3u8_segments(self, ts_dir, output_path):
        from .dl_m3u8 import M3u8Downloader
        dl = M3u8Downloader()
        return dl.merge_segments(ts_dir, output_path)