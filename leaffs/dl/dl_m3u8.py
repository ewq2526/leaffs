#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
m3u8 流视频下载器
支持：多线程并发下载 TS 片段、失败重试、合并输出
"""

import os
import time
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .dl_utils import _log, get_secure_client, validate_download_url

# C-08d：m3u8 防护上限
MAX_SEGMENTS = 10000              # 片段总数上限（解析/下载双重计数，超限报错）
MAX_SEGMENT_BYTES = 256 * 1024 * 1024  # 单片段大小上限 256MB（实读累计超限即断）


class M3u8Downloader:
    """m3u8 流视频下载器"""

    def __init__(self, max_concurrent=5, max_retries=3, retry_delay=2):
        self.max_concurrent = max_concurrent
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    def parse_m3u8(self, url):
        """解析 m3u8 文件，返回 (base_dir, ts_urls)"""
        err = validate_download_url(url)
        if err: raise Exception(err)
        client = get_secure_client()
        resp = client.get(url, headers={'User-Agent': 'Mozilla/5.0'})
        resp.raise_for_status()
        content = resp.text
        final_url = str(resp.url)
        resp.close()

        # 重定向最终目标同样走统一校验（httpx 钩子只拦字面量，此处做 DNS 解析级拦截）
        e_redirect = validate_download_url(final_url)
        if e_redirect:
            raise Exception(f'm3u8 重定向目标被安全校验拒绝: {e_redirect}')

        # 解析 base_dir
        from urllib.parse import urlparse
        parsed = urlparse(final_url)
        base_dir = parsed.scheme + '://' + parsed.netloc + parsed.path.rsplit('/', 1)[0] + '/'

        ts_urls = []
        for line in content.split('\n'):
            l = line.strip()
            if not l or l.startswith('#'):
                continue
            if l.startswith('http://') or l.startswith('https://'):
                ts_urls.append(l)
            elif l.startswith('//'):
                ts_urls.append('https:' + l)
            elif l.startswith('/'):
                ts_urls.append(parsed.scheme + '://' + parsed.netloc + l)
            else:
                ts_urls.append(base_dir + l)

        if not ts_urls:
            raise Exception('m3u8 中未找到 ts 片段')

        # 每个拼接好的 ts 片段 URL 也走统一校验，命中即整体失败
        for u in ts_urls:
            e2 = validate_download_url(u)
            if e2:
                raise Exception(f'ts 片段地址被安全校验拒绝: {e2}')

        # C-08d：片段总数上限（解析阶段计数）
        if len(ts_urls) > MAX_SEGMENTS:
            raise Exception(f'm3u8 片段数超过上限（{MAX_SEGMENTS}）')

        return base_dir, ts_urls

    def download(self, url, save_dir, filename=None, output_ext='.mp4',
                 on_progress=None, cancel_flag=None):
        """
        下载 m3u8 流视频

        参数:
            url: m3u8 链接
            save_dir: 保存目录
            filename: 输出文件名（不含扩展名），None 则自动生成
            output_ext: 输出文件扩展名，默认 .mp4
            on_progress: 进度回调 fn(task_dict)
            cancel_flag: 取消标志 threading.Event()

        返回:
            dict: {success, file_path, filename, total_segments, downloaded_segments, error}
        """
        if cancel_flag is None:
            cancel_flag = threading.Event()

        client = get_secure_client()

        task_info = {
            'url': url,
            'status': 'downloading',
            'progress': 0,
            'downloaded': 0,
            'total_size': 0,
            'error': '',
        }

        def _notify():
            if on_progress:
                try:
                    on_progress(task_info)
                except Exception:
                    pass

        # 解析 m3u8
        try:
            _, ts_urls = self.parse_m3u8(url)
        except Exception as e:
            return {
                'success': False,
                'error': '解析 m3u8 失败',
                'status': 'error',
            }

        total_segments = len(ts_urls)
        task_info['total_size'] = total_segments

        # 创建临时目录
        ts_dir = os.path.join(tempfile.gettempdir(), 'm3u8_' + str(int(time.time() * 1000)))
        os.makedirs(ts_dir, exist_ok=True)

        failed_indices = []
        downloaded_count = 0

        def dl_one(idx_url):
            i, u = idx_url
            if cancel_flag.is_set():
                return i, False
            # 真实请求前对每个片段 URL 显式校验（httpx 钩子只拦字面量，DNS 解析级必须在此拦截）
            err = validate_download_url(u)
            if err:
                raise Exception(f'ts 片段地址被安全校验拒绝: {err}')
            tp = os.path.join(ts_dir, f'{i:05d}.ts')
            for retry in range(self.max_retries):
                if cancel_flag.is_set():
                    return i, False
                try:
                    # C-08d：流式读取，单片段实读累计超 MAX_SEGMENT_BYTES 即断（内存也有界）
                    with client.stream('GET', u, headers={'User-Agent': 'Mozilla/5.0'}) as r:
                        r.raise_for_status()
                        written = 0
                        with open(tp, 'wb') as f:
                            for chunk in r.iter_bytes(chunk_size=65536):
                                written += len(chunk)
                                if written > MAX_SEGMENT_BYTES:
                                    raise Exception('ts 片段超过大小上限（256MB）')
                                f.write(chunk)
                    return i, True
                except Exception:
                    if retry < self.max_retries - 1:
                        time.sleep(self.retry_delay)
            return i, False

        last_push = 0
        try:
            try:
                with ThreadPoolExecutor(max_workers=self.max_concurrent) as exec:
                    futs = {exec.submit(dl_one, (i, u)): i for i, u in enumerate(ts_urls)}
                    done = 0
                    for f in as_completed(futs):
                        if cancel_flag.is_set():
                            break
                        idx, ok = f.result()
                        done += 1
                        if not ok:
                            failed_indices.append(idx)
                        else:
                            downloaded_count += 1
                        task_info['downloaded'] = done
                        task_info['progress'] = int(done * 100 / total_segments)
                        now = time.time()
                        if now - last_push >= 1.0:
                            last_push = now
                            _notify()
            except Exception:
                # 片段 URL 安全校验失败等：清理临时片段后整体失败（不产出半成品并伪装成功）
                shutil.rmtree(ts_dir, ignore_errors=True)
                raise
        finally:
            if cancel_flag.is_set():
                shutil.rmtree(ts_dir, ignore_errors=True)
                return {
                    'success': False,
                    'error': '用户取消',
                    'status': 'cancelled',
                }

        if failed_indices:
            task_info['status'] = 'partial'
            task_info['error'] = f'下载失败({len(failed_indices)}个片段)'
            _notify()
            # 清理本次任务创建的临时片段目录：partial 为终态（当前无后续合并/续传流程），
            # 不应把片段残骸遗留在系统临时目录；只删本任务新建的 ts_dir
            shutil.rmtree(ts_dir, ignore_errors=True)
            return {
                'success': False,
                'error': f'下载失败({len(failed_indices)}/{total_segments}个片段)',
                'total_segments': total_segments,
                'downloaded_segments': downloaded_count,
                'failed_indices': failed_indices[:20],
                'status': 'partial',
            }

        # 合并 TS 文件
        if not filename:
            from urllib.parse import urlparse, unquote
            parsed = urlparse(url)
            path = parsed.path
            base_name = os.path.splitext(os.path.basename(path))[0] if path else 'video'
            filename = base_name or 'video'

        abs_dir = os.path.join(save_dir)
        try:
            # 合并阶段整体包在 try/finally：无论合并成功还是中途异常（目录/文件
            # 打不开、磁盘满等），都清理本次任务新建的临时片段目录，不留残骸
            os.makedirs(abs_dir, exist_ok=True)

            final_name = filename
            if not final_name.endswith(output_ext):
                final_name += output_ext
            final_path = os.path.join(abs_dir, final_name)

            if os.path.exists(final_path):
                base, ext = os.path.splitext(final_name)
                final_path = os.path.join(abs_dir, base + '_1' + ext)

            ts_files = sorted(os.listdir(ts_dir))
            written = 0
            with open(final_path, 'wb') as out:
                for ts in ts_files:
                    ts_path = os.path.join(ts_dir, ts)
                    if os.path.getsize(ts_path) > 0:
                        with open(ts_path, 'rb') as f:
                            shutil.copyfileobj(f, out)
                        written += 1
        finally:
            shutil.rmtree(ts_dir, ignore_errors=True)

        task_info['status'] = 'completed'
        task_info['progress'] = 100
        task_info['total_size'] = os.path.getsize(final_path)
        task_info['downloaded'] = task_info['total_size']
        _notify()

        return {
            'success': True,
            'file_path': final_path,
            'filename': os.path.basename(final_path),
            'total_segments': total_segments,
            'downloaded_segments': written,
            'status': 'completed',
        }

    def retry_failed(self, url, ts_dir, failed_indices, on_progress=None, cancel_flag=None):
        """重试下载失败的 TS 片段"""
        if cancel_flag is None:
            cancel_flag = threading.Event()

        client = get_secure_client()

        # 重新解析 m3u8 获取最新 URL
        try:
            _, ts_urls = self.parse_m3u8(url)
        except Exception as e:
            return {'success': False, 'error': '重新解析 m3u8 失败'}

        retried = 0
        still_failed = 0

        for idx in failed_indices:
            if cancel_flag.is_set():
                break
            if idx >= len(ts_urls):
                still_failed += 1
                continue

            ts_path = os.path.join(ts_dir, f'{idx:05d}.ts')
            success = False
            for retry in range(self.max_retries):
                if cancel_flag.is_set():
                    break
                # 真实请求前同样显式校验（httpx 钩子只拦字面量）
                if validate_download_url(ts_urls[idx]):
                    success = False
                    break
                try:
                    # C-08d：流式读取 + 单片段上限
                    with client.stream('GET', ts_urls[idx], headers={'User-Agent': 'Mozilla/5.0'}) as r:
                        r.raise_for_status()
                        written = 0
                        with open(ts_path, 'wb') as f:
                            for chunk in r.iter_bytes(chunk_size=65536):
                                written += len(chunk)
                                if written > MAX_SEGMENT_BYTES:
                                    raise Exception('ts 片段超过大小上限（256MB）')
                                f.write(chunk)
                    success = True
                    retried += 1
                    break
                except Exception:
                    if retry < self.max_retries - 1:
                        time.sleep(self.retry_delay)
            if not success:
                still_failed += 1

            if on_progress:
                try:
                    on_progress({
                        'retried': retried,
                        'still_failed': still_failed,
                        'total': len(failed_indices),
                    })
                except Exception:
                    pass

        return {
            'success': still_failed == 0,
            'retried': retried,
            'still_failed': still_failed,
            'total': len(failed_indices),
        }

    def merge_segments(self, ts_dir, output_path):
        """合并 TS 片段为单个文件"""
        if not os.path.exists(ts_dir):
            return {'success': False, 'error': '临时目录不存在'}

        ts_files = sorted(os.listdir(ts_dir))
        if not ts_files:
            return {'success': False, 'error': '没有可合并的片段'}

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        written = 0
        with open(output_path, 'wb') as out:
            for ts in ts_files:
                ts_path = os.path.join(ts_dir, ts)
                if os.path.getsize(ts_path) > 0:
                    with open(ts_path, 'rb') as f:
                        shutil.copyfileobj(f, out)
                    written += 1

        shutil.rmtree(ts_dir, ignore_errors=True)

        return {
            'success': True,
            'file_path': output_path,
            'written': written,
            'total': len(ts_files),
        }