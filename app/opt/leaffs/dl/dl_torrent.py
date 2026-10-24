#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
种子文件下载器（RPC 版）
支持：本地种子上传解析、远程种子的URL解析、通过 aria2c RPC 下载
"""

import os
import re
import time
import base64
import json
import threading
from typing import List, Dict, Optional, Union, Callable, Any

from .dl_utils import (
    _log, get_secure_client,
    parse_torrent_data, parse_torrent_url, parse_torrent_file,
    save_uploaded_torrent, prefetch_http_torrent,
    is_magnet, format_size, format_speed,
)
from .dl_rpc import get_rpc_client, Aria2RPC


class TorrentDownloader:
    """种子文件下载器（RPC 版）"""

    POLL_INTERVAL = 0.5

    def __init__(self):
        self.keep_torrent_file = False   # 是否保留临时种子文件（用于调试）

    def parse(self, source: Union[str, bytes]) -> Dict[str, Any]:
        """
        解析种子文件
        参数:
            source: URL 字符串、本地文件路径或 bytes 数据
        返回:
            dict: {success, files, file_count, is_magnet, error}
        """
        _log(f'parse: type={type(source).__name__}')
        try:
            if isinstance(source, bytes):
                files = parse_torrent_data(source)
            elif os.path.isfile(source):
                files = parse_torrent_file(source)
            elif isinstance(source, str) and (source.startswith('http://') or source.startswith('https://')):
                files = parse_torrent_url(source)
            elif isinstance(source, str) and is_magnet(source):
                return {
                    'success': True, 'files': [], 'file_count': 0,
                    'is_magnet': True, 'error': None
                }
            else:
                return {'success': False, 'error': '不支持的种子来源类型'}
        except Exception as e:
            _log(f'parse 异常: {type(e).__name__}: {e}')
            import traceback
            traceback.print_exc()
            # A2：不回显异常文本（细节已在上面的 _log 里）
            return {'success': False, 'error': '种子解析失败'}

        return {
            'success': True, 'files': files,
            'file_count': len(files), 'is_magnet': False,
        }

    def parse_upload(self, file_data: bytes, filename: str = 'upload.torrent') -> Dict[str, Any]:
        """解析上传的种子文件"""
        _log(f'parse_upload: filename={filename} data_len={len(file_data) if file_data else 0}')
        try:
            files = parse_torrent_data(file_data)
        except Exception as e:
            _log(f'parse_upload 异常: {type(e).__name__}: {e}')
            return {'success': False, 'error': '种子文件解析失败'}
        torrent_path = save_uploaded_torrent(file_data, filename)
        return {'success': True, 'files': files, 'file_count': len(files), 'torrent_path': torrent_path}

    def _parse_selected_files(self, selected_files: Optional[Union[List[int], str]]) -> Union[List[int], Dict]:
        """解析 selected_files 为整数列表"""
        if selected_files is None:
            return []
        if isinstance(selected_files, str):
            raw = selected_files.strip()
            if raw.startswith('[') and raw.endswith(']'):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        return [int(x) for x in parsed]
                except:
                    pass
            elif ',' in raw:
                try:
                    return [int(x.strip()) for x in raw.split(',') if x.strip()]
                except:
                    pass
            else:
                try:
                    return [int(raw)]
                except:
                    pass
            return {'error': f'无法解析 selected_files: {selected_files}'}
        if isinstance(selected_files, (list, tuple)):
            try:
                return [int(x) for x in selected_files]
            except (ValueError, TypeError):
                return {'error': 'selected_files 列表包含非整数元素'}
        if isinstance(selected_files, int):
            return [selected_files]
        return {'error': f'selected_files 类型不支持: {type(selected_files)}'}

    def download(
        self,
        torrent_source: str,
        save_dir: str,
        selected_files: Optional[Union[List[int], str]] = None,
        on_progress: Optional[Callable[[Dict], None]] = None,
        cancel_flag: Optional[threading.Event] = None
    ) -> Dict[str, Any]:
        """
        通过 RPC 下载种子文件
        参数:
            torrent_source: 种子文件 URL、本地路径或磁力链接
            save_dir: 保存目录
            selected_files: 要下载的文件索引列表
            on_progress: 进度回调
            cancel_flag: 取消标志
        """
        if cancel_flag is None:
            cancel_flag = threading.Event()

        os.makedirs(save_dir, exist_ok=True)

        # 处理 selected_files
        selected_indices = self._parse_selected_files(selected_files)
        if isinstance(selected_indices, dict) and 'error' in selected_indices:
            return {'success': False, 'error': selected_indices['error'], 'status': 'error'}

        is_magnet_source = is_magnet(torrent_source)

        task_info = {
            'status': 'downloading', 'progress': 0, 'downloaded': 0,
            'total_size': 0, 'speed': '0 B/s', 'filename': '',
            'connections': 0, 'seeds': 0, 'peers': 0,
            'dht_nodes': 0, 'upload_speed': '0 B/s',
            'rpc_gid': '',
        }

        def _notify():
            if on_progress:
                try:
                    on_progress(dict(task_info))
                except Exception:
                    pass

        try:
            rpc = get_rpc_client()
        except Exception as e:
            return {'success': False, 'error': 'RPC 客户端获取失败', 'status': 'error'}

        # 构建选项
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

        if selected_indices:
            # aria2c --select-file 从 1 开始
            sel_str = ','.join(str(i + 1) for i in selected_indices)
            options['select-file'] = sel_str
            _log(f'select-file: {sel_str}')

        # 通过 RPC 添加任务
        try:
            if is_magnet_source:
                # 磁力链接（防御分支：正常情况下由 MagnetDownloader 处理）
                gid = rpc.add_uri(torrent_source, options=options)
            elif os.path.isfile(torrent_source):
                # 本地 .torrent（服务端落盘产物：上传 / C-08c 受控预拉取）：base64 addTorrent
                with open(torrent_source, 'rb') as f:
                    torrent_data = f.read()
                torrent_b64 = base64.b64encode(torrent_data).decode('ascii')
                gid = rpc.add_torrent(torrent_b64, options=options)
            elif torrent_source.startswith('http://') or torrent_source.startswith('https://'):
                # C-08c：远程 http(s) 种子 → 受控预拉取（8MB 上限、逐跳校验、路径预检）
                # 后落盘本地再 addTorrent；不再把 http URL 交给 aria2 addUri 自拉（SSRF 实证面）
                local_path = prefetch_http_torrent(torrent_source)
                with open(local_path, 'rb') as f:
                    torrent_data = f.read()
                torrent_b64 = base64.b64encode(torrent_data).decode('ascii')
                gid = rpc.add_torrent(torrent_b64, options=options)
            else:
                return {'success': False, 'error': f'不支持的种子来源: {torrent_source}', 'status': 'error'}

            _log(f'种子已通过 RPC 提交, GID={gid}')
            task_info['rpc_gid'] = gid
            _notify()
        except Exception as e:
            return {'success': False, 'error': 'RPC 添加种子失败', 'status': 'error'}

        # 轮询进度
        result = None
        while not cancel_flag.is_set():
            try:
                status_data = rpc.tell_status(gid)
            except Exception as e:
                _log(f'RPC tellStatus 失败: {e}')
                time.sleep(self.POLL_INTERVAL)
                continue

            if not status_data:
                time.sleep(self.POLL_INTERVAL)
                continue

            aria_status = status_data.get('status', '')
            total_len = int(status_data.get('totalLength', '0'))
            completed_len = int(status_data.get('completedLength', '0'))

            # 解析进度
            progress_info = Aria2RPC.parse_progress(status_data)
            task_info['downloaded'] = progress_info.get('downloaded', 0)
            task_info['total_size'] = progress_info.get('total_size', 0)
            task_info['speed'] = progress_info.get('speed', '0 B/s')
            task_info['upload_speed'] = progress_info.get('upload_speed', '0 B/s')
            task_info['connections'] = progress_info.get('connections', 0)
            if progress_info.get('filename'):
                task_info['filename'] = progress_info['filename']

            if total_len > 0:
                pct = int(completed_len * 100 / total_len)
                task_info['progress'] = min(pct, 99)
            else:
                task_info['progress'] = 0

            if aria_status == 'complete':
                _log(f'种子下载完成: GID={gid}')
                task_info['status'] = 'completed'
                task_info['progress'] = 100
                task_info['downloaded'] = max(total_len, completed_len)

                filename = task_info['filename']
                if not filename:
                    files = status_data.get('files', [])
                    if files:
                        first_path = files[0].get('path', '')
                        if first_path:
                            filename = os.path.basename(first_path)

                result = {
                    'success': True,
                    'save_dir': save_dir,
                    'total_size': task_info['total_size'] or total_len or completed_len,
                    'filename': filename,
                    'status': 'completed',
                }
                _notify()
                break

            elif aria_status == 'error':
                error_msg = status_data.get('errorMessage', 'aria2 RPC 错误')
                _log(f'种子下载错误: {error_msg}')
                result = {'success': False, 'error': error_msg, 'status': 'error'}
                _notify()
                break

            elif aria_status == 'removed':
                result = {'success': False, 'error': '下载已被移除', 'status': 'error'}
                break

            elif aria_status in ('waiting', 'active', 'paused'):
                _notify()

            time.sleep(self.POLL_INTERVAL)

        if cancel_flag.is_set():
            try:
                rpc.force_remove(gid)
            except Exception:
                pass
            if not result:
                result = {'success': False, 'error': '用户取消', 'status': 'cancelled'}
            return result

        if result is None:
            result = {'success': False, 'error': '未知错误', 'status': 'error'}

        return result