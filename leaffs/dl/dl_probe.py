#!/usr/bin/env python3
"""磁力链接探测：通过 aria2c 快速获取元数据获取文件大小"""

import os
import re
import json
import time
import subprocess
import tempfile
import threading
from urllib.parse import unquote

from .dl_utils import ARIA2C_PATH, _log


def probe_magnet(url, timeout=8):
    """
    探测磁力链接，获取文件信息
    通过 aria2c 快速获取元数据
    返回: {filename, size} 或 None
    """
    if not ARIA2C_PATH:
        # 没有 aria2c，尝试从 URL 参数解析 xl=
        return _parse_magnet_params(url)

    tmp_dir = tempfile.mkdtemp(prefix='dl_probe_')
    result = {'filename': '', 'size': 0}

    try:
        cmd = [
            ARIA2C_PATH,
            '--dir', tmp_dir,
            '--max-connection-per-server=16',
            '--split=16',
            '--bt-metadata-only=true',
            '--bt-save-metadata=true',
            '--enable-dht=true',
            '--dht-listen-port=6881-6889',
            '--listen-port=6881-6889',
            '--bt-stop-timeout=' + str(timeout),
            '--console-log-level=error',
            '--summary-interval=0',
            url
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        )

        # 等待完成或超时
        try:
            proc.wait(timeout=timeout + 2)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()

        # 检查下载的元数据文件
        meta_file = None
        for f in os.listdir(tmp_dir):
            if f.endswith('.torrent') or f.endswith('.meta4'):
                meta_file = os.path.join(tmp_dir, f)
                break
            # aria2 有时保存为 .torrent 文件
            if f.endswith('.aria2'):
                continue

        if meta_file and os.path.exists(meta_file):
            try:
                from .dl_utils import parse_torrent_file
                files = parse_torrent_file(meta_file)
                if files:
                    total_size = sum(f.get('size', 0) for f in files)
                    name = files[0].get('path', '').split('/')[0]
                    result['filename'] = name
                    result['size'] = total_size
            except Exception as e:
                _log(f'解析种子元数据失败: {e}')

    except Exception as e:
        _log(f'探测磁力链接失败: {e}')
    finally:
        # 清理
        try:
            for f in os.listdir(tmp_dir):
                os.remove(os.path.join(tmp_dir, f))
            os.rmdir(tmp_dir)
        except:
            pass

    return result if result['size'] > 0 else _parse_magnet_params(url)


def _parse_magnet_params(url):
    """从磁力链接 URL 参数中提取 xl=(文件大小)"""
    result = {'filename': '', 'size': 0}
    # 解析 dn
    m = re.search(r'[?&]dn=([^&]+)', url)
    if m:
        result['filename'] = unquote(m.group(1))
    # 解析 xl (exact length)
    m = re.search(r'[?&]xl=(\d+)', url)
    if m:
        result['size'] = int(m.group(1))
    return result if result['size'] > 0 else None