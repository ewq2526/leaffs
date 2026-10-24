#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HTTP/HTTPS 常规文件下载器（基于 urllib）
简单可靠：每 0.5 秒采样一次进度，不阻塞下载
"""

import os
import time
import shutil
import threading
import urllib.request
import urllib.error
import re
from urllib.parse import urlparse, unquote, urljoin

from .dl_utils import (
    _log, sanitize_filename, decode_thunder, validate_download_url,
)
from . import dl_config


# 重定向目标安全校验：所有重定向地址在跟随前都经过 validate_download_url，
# 拦截重定向到环回/链路本地/保留地址的 SSRF（对 literal 之外的 DNS 解析级地址同样生效）
class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        abs_url = urljoin(req.full_url, newurl)
        err = validate_download_url(abs_url)
        if err:
            raise urllib.error.URLError(err)
        return urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl)


# ========== 通用请求头 ==========

_COMMON_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': '*/*',
    'Accept-Encoding': 'identity',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}

def _build_headers(url=None):
    """构建请求头，自动根据 URL 设置 Referer"""
    headers = dict(_COMMON_HEADERS)
    if url:
        try:
            parsed = urlparse(url)
            # 使用 URL 自身的 scheme + host 作为 Referer（大多数 CDN 要求与请求同源）
            headers['Referer'] = f'{parsed.scheme}://{parsed.hostname}/'
        except Exception:
            pass
    return headers


# ========== 文件大小探测 ==========

def get_file_size(url):
    """获取 HTTP 资源文件大小"""
    url = decode_thunder(url)
    if validate_download_url(url):
        return None
    # 所有请求（含跟随的重定向）都走安全 opener，防止探测阶段被重定向到
    # 环回/保留地址等造成 SSRF
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    try:
        req = urllib.request.Request(url, method='HEAD', headers=_build_headers(url))
        with opener.open(req, timeout=10) as resp:
            cl = resp.headers.get('Content-Length')
            if cl:
                size = int(cl.strip())
                if size > 0:
                    return size
    except Exception:
        pass
    try:
        headers = _build_headers(url)
        headers['Range'] = 'bytes=0-0'
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=10) as resp:
            if resp.status in (200, 206):
                cr = resp.headers.get('Content-Range')
                if cr:
                    m = re.search(r'/(\d+)$', cr)
                    if m:
                        return int(m.group(1))
    except Exception:
        pass
    return None


# ========== 文件名获取 ==========

def _get_filename(url):
    """从 URL 提取文件名(仅从路径取，去掉查询参数)"""
    path = urlparse(url).path
    name = unquote(os.path.basename(path))
    # 去掉查询参数(如 ?v=1.17.9)
    if '?' in name:
        name = name.split('?')[0]
    if name and name != '/' and '.' in name:
        return sanitize_filename(name)
    return 'download'


# ========== 速度格式化 ==========

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


# ========== HttpDownloader 类 ==========


class HttpDownloader:
    """HTTP/HTTPS 常规文件下载器"""

    def __init__(self, chunk_size=65536, idle_timeout=15):
        self.chunk_size = chunk_size
        self.idle_timeout = idle_timeout

    def download(self, url, save_dir, filename=None,
                 speed_limit=0, on_progress=None, cancel_flag=None,
                 max_download_size=None):
        """
        下载文件
        每 0.5 秒采样一次进度并通知（使用增量速度计算）

        C-09：max_download_size>0 时单任务大小上限（bytes），超限立即中止并删除
        .tmp；不传则取 dl_config.get_max_download_size()（0=不限）。
        """
        # C-09：单任务大小上限
        if max_download_size is None:
            max_download_size = dl_config.get_max_download_size()
        max_size = int(max_download_size or 0)
        abs_dir = os.path.join(save_dir)
        os.makedirs(abs_dir, exist_ok=True)
        if cancel_flag is None:
            cancel_flag = threading.Event()

        task_info = {
            'url': url,
            'filename': filename or '',
            'status': 'downloading',
            'progress': 0,
            'downloaded': 0,
            'total_size': 0,
            'speed': '0 B/s',
            'error': '',
        }

        def _notify():
            if on_progress:
                try:
                    on_progress(dict(task_info))
                except Exception:
                    pass

        url = decode_thunder(url)

        # “最终要实际拉取”的 URL 统一校验（thunder 已解码；DNS 解析级拦截环回/保留地址）
        err = validate_download_url(url)
        if err:
            task_info['status'] = 'error'; task_info['error'] = err; _notify()
            return {'success': False, 'error': err, 'status': 'error'}

        # 获取文件大小
        total_size = 0
        try:
            detected = get_file_size(url)
            if detected is not None:
                total_size = detected
                task_info['total_size'] = total_size
        except Exception:
            pass

        # 文件名（从 URL 路径提取，去掉查询参数）
        if not task_info['filename']:
            task_info['filename'] = _get_filename(url)
        task_info['filename'] = sanitize_filename(task_info['filename'])
        if not task_info['filename']:
            task_info['filename'] = 'download'

        tmp_path = os.path.join(abs_dir, task_info['filename'] + '.tmp')
        final_path = os.path.join(abs_dir, task_info['filename'])

        # 断点续传
        downloaded = 0
        if os.path.exists(tmp_path):
            downloaded = os.path.getsize(tmp_path)
            if downloaded > 0:
                task_info['downloaded'] = downloaded

        # C-09：单任务大小上限——续传在途或已探测总大小超限 → 中止并删除 .tmp
        def _over_limit_err():
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            msg = f'下载超过大小上限（{format_size(max_size)}）'
            task_info['status'] = 'error'
            task_info['error'] = msg
            _notify()
            return {'success': False, 'error': msg, 'status': 'error'}

        if max_size > 0 and (downloaded > max_size or (total_size > 0 and total_size > max_size)):
            return _over_limit_err()

        headers = _build_headers(url)
        if downloaded > 0:
            headers['Range'] = f'bytes={downloaded}-'

        try:
            req = urllib.request.Request(url, headers=headers)
            opener = urllib.request.build_opener(_SafeRedirectHandler())
            resp = opener.open(req, timeout=30)

            status_code = resp.status
            if status_code not in (200, 206):
                err = f'HTTP {status_code}'
                resp.close()
                if not cancel_flag.is_set():
                    task_info['status'] = 'error'
                    task_info['error'] = err
                    _notify()
                return {'success': False, 'error': err, 'status': 'error'}

            if downloaded > 0 and status_code != 206:
                downloaded = 0
                task_info['downloaded'] = 0
                resp.close()
                req = urllib.request.Request(url, headers=_build_headers(url))
                resp = opener.open(req, timeout=30)
                if resp.status != 200:
                    err = f'HTTP {resp.status}'
                    resp.close()
                    if not cancel_flag.is_set():
                        task_info['status'] = 'error'
                        task_info['error'] = err
                        _notify()
                    return {'success': False, 'error': err, 'status': 'error'}

            if total_size == 0:
                cl = resp.headers.get('Content-Length')
                if cl is not None:
                    total_size = int(cl)
                    task_info['total_size'] = total_size

            # C-09：响应头确认超限（Content-Length 全量口径）→ 中止并删 .tmp
            if max_size > 0 and total_size > 0 and total_size > max_size:
                resp.close()
                return _over_limit_err()

            cs = self.chunk_size
            last_sample_time = time.time()
            last_sample_bytes = downloaded
            last_data = time.time()
            sample_interval = 0.5
            # 字节级限速基准：以本次下载会话已写入字节数匀速推进（speed_limit 单位
            # bytes/s，与下载字节口径一致）；睡眠分段 0.1s，期间可响应取消。
            throttle_start = time.time()
            throttle_sent = 0
            over_limit = False

            with open(tmp_path, 'ab' if downloaded > 0 else 'wb') as f:
                while True:
                    if cancel_flag.is_set():
                        resp.close()
                        task_info['status'] = 'cancelled'
                        _notify()
                        return {'success': False, 'error': '已取消', 'status': 'cancelled'}

                    try:
                        chunk = resp.read(cs)
                    except Exception:
                        chunk = b''

                    if not chunk:
                        if downloaded > 0 and downloaded < total_size:
                            _log('连接断开，尝试重连...')
                            resp.close()
                            retry_h = _build_headers(url)
                            retry_h['Range'] = f'bytes={downloaded}-'
                            try:
                                req = urllib.request.Request(url, headers=retry_h)
                                resp = opener.open(req, timeout=30)
                                if resp.status not in (200, 206):
                                    raise Exception(f'重连失败 HTTP {resp.status}')
                                last_data = time.time()
                                continue
                            except Exception as re:
                                raise Exception(f'重连失败: {re}')
                        break

                    last_data = time.time()
                    f.write(chunk)
                    downloaded += len(chunk)

                    # C-09：实读累计超限（服务器谎报/无 Content-Length）→ 中断，退出后删 .tmp
                    if max_size > 0 and downloaded > max_size:
                        over_limit = True
                        break

                    # 限速节流：按已发送总量对齐目标速率，不足则补睡（分段 0.1s）
                    throttle_sent += len(chunk)
                    if speed_limit > 0:
                        while True:
                            expected = throttle_sent / speed_limit
                            wait = expected - (time.time() - throttle_start)
                            if wait <= 0:
                                break
                            if cancel_flag.is_set():
                                resp.close()
                                task_info['status'] = 'cancelled'
                                _notify()
                                return {'success': False, 'error': '已取消', 'status': 'cancelled'}
                            time.sleep(min(0.1, wait))

                    now = time.time()
                    if now - last_sample_time >= sample_interval:
                        delta_bytes = downloaded - last_sample_bytes
                        delta_time = now - last_sample_time
                        if delta_time > 0 and delta_bytes > 0:
                            speed_bps = delta_bytes / delta_time
                            task_info['speed'] = _format_speed(speed_bps)
                        last_sample_time = now
                        last_sample_bytes = downloaded
                        task_info['downloaded'] = downloaded
                        if total_size > 0:
                            task_info['progress'] = min(99, int(downloaded * 100 / total_size))
                        _notify()

                    if now - last_data > self.idle_timeout:
                        raise Exception(f'下载超时（{self.idle_timeout}秒无数据）')

            resp.close()

            if over_limit:
                # C-09：流式累计超限 → 已退出 with 块（句柄已关），删 .tmp 后返回错误
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass
                msg = f'下载超过大小上限（{format_size(max_size)}）'
                task_info['status'] = 'error'
                task_info['error'] = msg
                _notify()
                return {'success': False, 'error': msg, 'status': 'error'}

            if not cancel_flag.is_set():
                if os.path.exists(tmp_path):
                    if os.path.exists(final_path):
                        base, ext = os.path.splitext(task_info['filename'])
                        final_path = os.path.join(abs_dir, base + '_1' + ext)
                    shutil.move(tmp_path, final_path)

                task_info['status'] = 'completed'
                task_info['progress'] = 100
                task_info['downloaded'] = os.path.getsize(final_path)
                _notify()
                return {
                    'success': True,
                    'file_path': final_path,
                    'filename': os.path.basename(final_path),
                    'total_size': task_info['downloaded'],
                    'status': 'completed',
                }

        except Exception as e:
            if not cancel_flag.is_set():
                from leaffs.runtime_log import log_exception
                log_exception('下载器：任务执行', e)
                task_info['status'] = 'error'
                task_info['error'] = '下载失败'
                _notify()
                return {'success': False, 'error': '下载失败', 'status': 'error'}

        return {'success': False, 'error': '已取消', 'status': 'cancelled'}