#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
磁力链接下载器（RPC 版）
通过 aria2c RPC 下载磁力链接 (magnet:?xt=...)
"""

import os
import re
import time
import threading
from urllib.parse import urlparse, parse_qs, unquote

from .dl_utils import _log, sanitize_filename
from .dl_rpc import get_rpc_client, Aria2RPC


class MagnetDownloader:
    """磁力链接下载器（基于 RPC）"""

    POLL_INTERVAL = 0.5  # 轮询间隔（秒）

    def __init__(self):
        pass

    def _extract_magnet_name(self, url):
        """从磁力链接提取文件名，返回净化后的文件名或 None"""
        try:
            qs = urlparse(url).query
            params = parse_qs(qs)
            dn_list = params.get('dn')
            if dn_list and dn_list[0]:
                raw = dn_list[0]
                decoded = unquote(raw)
                return sanitize_filename(decoded)
        except Exception:
            pass
        return None

    def download(self, url, save_dir,
                 on_progress=None, cancel_flag=None):
        if cancel_flag is None:
            cancel_flag = threading.Event()

        # 从磁力链接提取净化后的文件名
        magnet_name = self._extract_magnet_name(url)
        display_name = magnet_name or 'magnet_file'

        task_info = {
            'status': 'downloading', 'progress': 0, 'downloaded': 0,
            'total_size': 0, 'speed': '0 B/s', 'filename': display_name,
            'connections': 0, 'seeds': 0, 'peers': 0,
            'dht_nodes': 0, 'upload_speed': '0 B/s',
            'phase': 'metadata',
            'metadata_progress': 0,
            'metadata_total': 0,
            'metadata_done': False,
            'rpc_gid': '',
        }

        def _notify():
            if on_progress:
                try:
                    on_progress(dict(task_info))
                except Exception:
                    pass

        # 获取 RPC 客户端
        try:
            rpc = get_rpc_client()
        except Exception as e:
            return {'success': False, 'error': f'RPC 客户端获取失败: {e}', 'status': 'error'}

        # 通过 RPC 添加磁力链接
        options = {
            'dir': save_dir,
            'max-connection-per-server': '16',
            'split': '16',
            'continue': 'true',
            'allow-overwrite': 'false',  # C-12：关闭 per-task 覆盖，同名已存在 → aria2 报错不覆盖
            'bt-save-metadata': 'true',
            'follow-torrent': 'true',
            'enable-dht': 'true',
            'dht-listen-port': '6881-6889',
        }

        try:
            gid = rpc.add_uri(url, options=options)
            _log(f'磁力链接已通过 RPC 提交, GID={gid}')
            task_info['rpc_gid'] = gid
            _notify()
        except Exception as e:
            return {'success': False, 'error': f'RPC addUri 失败: {e}', 'status': 'error'}

        # 轮询进度 - 支持 GID 切换（followedBy）
        metadata_done = False
        active_gid = gid  # 当前活动的 GID（元数据→文件可能切换）
        result = None

        while not cancel_flag.is_set():
            try:
                status_data = rpc.tell_status(active_gid)
            except Exception as e:
                _log(f'RPC tellStatus 失败: {e}')
                time.sleep(self.POLL_INTERVAL)
                continue

            if not status_data:
                time.sleep(self.POLL_INTERVAL)
                continue

            aria_status = status_data.get('status', '')

            # 检查 followedBy：元数据下载完成后，aria2c 会创建新任务下载文件
            followed_by = status_data.get('followedBy', [])
            if followed_by and aria_status == 'complete':
                # 元数据下载完成，切换到文件下载 GID
                new_gid = followed_by[0]
                _log(f'元数据完成，切换到文件下载 GID: {new_gid}')
                active_gid = new_gid
                task_info['rpc_gid'] = new_gid
                task_info['phase'] = 'file'
                task_info['metadata_progress'] = 100
                task_info['metadata_done'] = True

                # 获取种子名称
                bittorrent = status_data.get('bittorrent', {})
                if bittorrent:
                    info_name = bittorrent.get('info', {}).get('name', '')
                    if info_name:
                        task_info['filename'] = info_name
                        _log(f'从种子信息获取文件名: {info_name}')

                _notify()
                continue

            # 解析进度
            progress_info = Aria2RPC.parse_progress(status_data)
            total_len = progress_info.get('total_size', 0)
            completed_len = progress_info.get('downloaded', 0)

            task_info['downloaded'] = completed_len
            task_info['total_size'] = total_len
            task_info['speed'] = progress_info.get('speed', '0 B/s')
            task_info['upload_speed'] = progress_info.get('upload_speed', '0 B/s')
            task_info['connections'] = progress_info.get('connections', 0)
            if progress_info.get('filename'):
                task_info['filename'] = progress_info['filename']

            # 元数据阶段（total_len==0 且无 followedBy）
            if not metadata_done and total_len == 0:
                if completed_len > 0:
                    est_pct = min(int(completed_len * 100 / 1048576), 99)
                    task_info['metadata_progress'] = max(task_info['metadata_progress'], est_pct)
                task_info['progress'] = 0

            # 文件阶段
            if total_len > 0 and not metadata_done:
                metadata_done = True
                task_info['phase'] = 'file'
                task_info['metadata_progress'] = 100
                task_info['metadata_done'] = True
                task_info['total_size'] = total_len
                _log(f'文件开始下载，总大小: {total_len} bytes')

            if total_len > 0:
                pct = int(completed_len * 100 / total_len)
                task_info['progress'] = min(pct, 99)

            # 检查完成状态
            if aria_status == 'complete':
                total_files = int(status_data.get('totalLength', '0'))
                completed_files = int(status_data.get('completedLength', '0'))
                _log(f'下载完成: GID={active_gid}')
                task_info['status'] = 'completed'
                task_info['progress'] = 100
                task_info['downloaded'] = max(total_files, completed_files)
                task_info['phase'] = 'file'
                task_info['metadata_progress'] = 100
                task_info['metadata_done'] = True

                if not task_info['filename'] or task_info['filename'] == display_name:
                    files = status_data.get('files', [])
                    if files:
                        first_path = files[0].get('path', '')
                        if first_path:
                            task_info['filename'] = os.path.basename(first_path)

                result = {
                    'success': True,
                    'save_dir': save_dir,
                    'total_size': task_info['total_size'] or total_files or completed_files,
                    'filename': task_info['filename'],
                    'status': 'completed',
                }
                _notify()
                break

            elif aria_status == 'error':
                error_msg = status_data.get('errorMessage', 'aria2 RPC 错误')
                _log(f'下载错误: {error_msg}')
                result = {'success': False, 'error': error_msg, 'status': 'error'}
                _notify()
                break

            elif aria_status == 'removed':
                _log(f'下载已被移除: GID={active_gid}')
                result = {'success': False, 'error': '下载已被移除', 'status': 'error'}
                break

            elif aria_status in ('waiting', 'active', 'paused'):
                _notify()

            time.sleep(self.POLL_INTERVAL)

        # 检查取消
        if cancel_flag.is_set():
            # 移除所有相关 GID
            for gid_to_kill in (gid, active_gid):
                if gid_to_kill:
                    try:
                        rpc.force_remove(gid_to_kill)
                    except Exception:
                        pass
            _log(f'磁力链接下载已取消: GID={gid}')
            if not result:
                result = {'success': False, 'error': '用户取消', 'status': 'cancelled'}
            return result

        # 如果未设定 result，说明循环因其他原因退出
        if result is None:
            result = {'success': False, 'error': '未知错误', 'status': 'error'}

        return result