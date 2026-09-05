#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
下载器 HTTP API 处理函数
所有下载相关 API 逻辑集中于此，主服务器仅做路由转发和 WebSocket 广播
"""

import os
import json
import socket
import ipaddress
import urllib.parse
import re
import asyncio
from urllib.parse import unquote, urlparse

import httpx

from .dl_core import DownloadManager, get_downloader_for_url, DownloadLimitError
from .dl_utils import is_ed2k, validate_download_url
from .dl_utils import save_uploaded_torrent, parse_torrent_data, parse_torrent_file, parse_torrent_url as _parse_torrent_url
from .dl_utils import prefetch_http_torrent, is_trusted_local_torrent
from .dl_http import get_file_size, _get_filename
from .dl_probe import probe_magnet
from .dl_rpc import get_rpc_client


def _url_blocked_error(url):
    """下载 URL 安全校验：统一委托 dl_utils.validate_download_url。

    thunder 链接先解码再校验；magnet 由 aria2 处理放行；http/https 目标做
    DNS 解析级环回/链路本地/保留/多播/未指定地址拦截。

    返回错误信息（不允许时）或 None（允许）。
    """
    return validate_download_url(url)


# ========== 全局管理器（与主服务器共享同一实例）=========
_dl_manager = None


def set_dl_manager(mgr):
    global _dl_manager
    _dl_manager = mgr


def _get_mgr():
    if _dl_manager is None:
        raise RuntimeError('下载管理器未初始化')
    return _dl_manager


# ========== 路径安全检查 ==========
def _check_path_safe(upload_dir, save_dir):
    abs_dir = os.path.join(upload_dir, save_dir)
    abs_dir = os.path.normpath(abs_dir)
    upload_dir = os.path.normpath(upload_dir)
    return abs_dir.startswith(upload_dir + os.sep) or abs_dir == upload_dir


# ========== 配置 ==========
def handle_get_config():
    try:
        mgr = _get_mgr()
        cfg = mgr.get_config()
        return {'success': True, 'config': cfg}
    except Exception as e:
        return {'error': str(e)}, 500


def handle_set_config(data, user='', role=''):
    # 下载器全局配置仅管理员可修改（leaffs._dl_forward_cmd 已透传 user/role）
    if role not in ('admin', 'super_admin'):
        return {'success': False, 'error': '无权限'}, 403
    try:
        mgr = _get_mgr()
        concurrent = data.get('max_concurrent')
        speed = data.get('speed_limit')
        mgr.set_config(max_concurrent=concurrent, speed_limit=speed)
        return {'success': True}
    except Exception as e:
        return {'error': str(e)}, 500


# ========== 任务列表 ==========
def handle_list(user='', role=''):
    try:
        mgr = _get_mgr()
        if role in ('admin', 'super_admin'):
            tasks = mgr.get_all_tasks()
        else:
            tasks = mgr.get_user_tasks(user) if user else []
        return {'success': True, 'tasks': tasks}
    except Exception as e:
        return {'error': str(e)}, 500


# ========== 开始下载 ==========
def handle_start(data, check_path_permission=None, upload_dir='', user='', quota_check=None):
    """开始下载。

    跨组契约 IC-QUOTA-C：quota_check(abs_save_dir, est_bytes) -> (ok, err)，
    由 A 侧把 handler._check_quota 包装传入；未传时 C 侧不做强一致配额判定
    （任务数/并发上限仍由 dl_core.add_download 强制执行）。
    """
    url = data.get('url', '').strip()
    save_dir = data.get('save_dir', '').strip()
    selected_files = data.get('selected_files', None)

    # 空字符串/空列表视为未选择
    if selected_files is not None:
        if isinstance(selected_files, str) and not selected_files.strip():
            selected_files = None
        elif isinstance(selected_files, (list, tuple)) and len(selected_files) == 0:
            selected_files = None

    if not url or not save_dir:
        return {'error': '缺少参数'}, 400

    if check_path_permission and not check_path_permission(save_dir):
        return {'error': '无权限写入此目录'}, 403

    if upload_dir:
        if not _check_path_safe(upload_dir, save_dir):
            return {'error': '目录不安全'}, 403
        save_dir = os.path.join(upload_dir, save_dir)

    # 明确拒绝 ed2k 协议
    if is_ed2k(url):
        return {'error': '不支持 ed2k（电驴）协议下载'}, 400

    # C-08c：仅“服务端自身落盘的临时 .torrent”（上传/远程预拉取产物）可作为本地源；
    # 其余一律先做 URL 协议/目标地址安全校验（禁 file:// 等本地读取、禁 SSRF 目标）
    local_torrent = is_trusted_local_torrent(url)
    if not local_torrent:
        url_err = _url_blocked_error(url)
        if url_err:
            return {'error': url_err}, 400

    try:
        mgr = _get_mgr()
        kwargs = {}
        if selected_files is not None:
            kwargs['selected_files'] = selected_files

        # 类型判定（ed2k 已在上方拒绝）
        _, dl_type = get_downloader_for_url(url)

        est_bytes = 0
        if dl_type == 'torrent' and not local_torrent and (url.startswith('http://') or url.startswith('https://')):
            # C-08c：远程 http(s).torrent 受控预拉取（8MB 上限、解析+路径预检通过后落盘），
            # 向 add_download 传本地路径，不再把 http URL 交给 aria2 addUri 自拉；
            # 同时保留原始 URL，供任务重试时在本地副本丢失情况下重新预拉取。
            kwargs['origin_url'] = url
            url = prefetch_http_torrent(url)
            local_torrent = True
        if local_torrent:
            # 已落盘的本地种子：读取文件清单大小之和作为配额估算
            try:
                files = parse_torrent_file(url)
                est_bytes = sum(int(f.get('size') or 0) for f in files)
            except Exception:
                est_bytes = 0
        elif dl_type == 'http':
            try:
                s = get_file_size(url)
                if s:
                    est_bytes = int(s)
            except Exception:
                est_bytes = 0

        # IC-QUOTA-C：可选配额回调（A 注入 handler._check_quota），不足则 413
        if quota_check is not None:
            try:
                ok_q, err_q = quota_check(os.path.abspath(save_dir), est_bytes)
            except Exception:
                ok_q, err_q = True, ''
            if not ok_q:
                return {'error': err_q or '磁盘配额不足'}, 413

        task_id = mgr.add_download(url, save_dir, user=user, **kwargs)
        return {'success': True, 'task_id': task_id}
    except DownloadLimitError as e:
        return {'error': str(e)}, 429
    except Exception as e:
        return {'error': str(e)}, 500


# ========== 暂停/恢复/取消/删除 ==========
def handle_pause(data, user='', role=''):
    tid = data.get('task_id', '')
    if not tid: return {'error': '缺少 task_id'}, 400
    try:
        mgr = _get_mgr()
        ok = mgr.pause_task(tid, user=user, role=role)
        if not ok:
            return {'success': False, 'error': '任务不存在或无权限操作'}, 403
        return {'success': True}
    except Exception as e:
        return {'error': str(e)}, 500

def handle_resume(data, user='', role=''):
    tid = data.get('task_id', '')
    if not tid: return {'error': '缺少 task_id'}, 400
    try:
        mgr = _get_mgr()
        ok = mgr.resume_task(tid, user=user, role=role)
        if not ok:
            return {'success': False, 'error': '任务不存在或无权限操作'}, 403
        return {'success': True}
    except Exception as e:
        return {'error': str(e)}, 500

def handle_cancel(data, user='', role=''):
    tid = data.get('task_id', '')
    if not tid: return {'error': '缺少 task_id'}, 400
    try:
        mgr = _get_mgr()
        ok = mgr.cancel_task(tid, user=user, role=role)
        if not ok:
            return {'success': False, 'error': '任务不存在或无权限操作'}, 403
        return {'success': True}
    except Exception as e:
        return {'error': str(e)}, 500

def handle_delete(data, user='', role=''):
    tid = data.get('task_id', '')
    if not tid: return {'error': '缺少 task_id'}, 400
    delete_files = data.get('delete_files', False)
    try:
        mgr = _get_mgr()
        ok = mgr.delete_task(tid, user=user, role=role, delete_files=delete_files)
        if not ok:
            return {'success': False, 'error': '任务不存在或无权限操作'}, 403
        return {'success': True}
    except Exception as e:
        return {'error': str(e)}, 500

def handle_peers(data, user='', role=''):
    """获取任务的 RPC 对等节点详情（校验任务归属）"""
    tid = data.get('task_id', '')
    if not tid: return {'error': '缺少 task_id'}, 400
    try:
        mgr = _get_mgr()
        if not mgr.can_manage_task(tid, user=user, role=role):
            return {'success': False, 'error': '任务不存在或无权限操作'}, 403
        task = mgr.get_task(tid)
        if not task:
            return {'error': '任务不存在'}, 404
        rpc_gid = task.get('rpc_gid', '')
        if not rpc_gid:
            return {'success': True, 'peers': [], 'status': {}, 'task': task, 'note': '没有关联的 RPC GID'}
        rpc = get_rpc_client()

        # 尝试 tellActive 和 tellWaiting 查找最新状态
        peers = []
        status = None

        try:
            status = rpc.tell_status(rpc_gid)
            if status:
                peers = rpc.get_peers(rpc_gid) or []
        except Exception:
            pass

        # 如果活跃中没找到，尝试 stopped 队列（已完成的也看有没有做种节点）
        if status is None:
            try:
                stopped = rpc.tell_stopped(0, 50)
                for s in stopped or []:
                    if s.get('gid') == rpc_gid:
                        status = s
                        try:
                            peers = rpc.get_peers(rpc_gid) or []
                        except Exception:
                            peers = []
                        break
            except Exception:
                pass

        # 还找不到，尝试通过 followedBy 查找
        if status is None or status.get('status') == 'complete':
            try:
                # 查找 active 中是否有相关 GID（followedBy 继承）
                active_list = rpc.tell_active() or []
                for active_gid_item in active_list:
                    agid = active_gid_item.get('gid', '')
                    if agid and agid != rpc_gid:
                        try:
                            p = rpc.get_peers(agid)
                            if p:
                                peers = p
                                status = rpc.tell_status(agid)
                                _log(f'从活跃 GID {agid} 获取到 peers')
                                break
                        except Exception:
                            continue
            except Exception:
                pass

        return {
            'success': True,
            'peers': peers or [],
            'status': status or {},
            'task': task,
        }
    except Exception as e:
        return {'error': str(e)}, 500

def handle_merge(data, user='', role=''):
    return {'success': False, 'msg': '新下载器不再支持独立合并操作，下载完成后自动合并'}

def handle_retry(data, user='', role=''):
    """重试已结束的下载任务（校验任务归属，沿用现有 user/role 机制）"""
    tid = data.get('task_id', '')
    if not tid:
        return {'error': '缺少 task_id'}, 400
    try:
        mgr = _get_mgr()
        r = mgr.retry_task(tid, user=user, role=role)
        if r.get('forbidden'):
            return {'success': False, 'error': '任务不存在或无权限操作'}, 403
        if not r.get('success'):
            return {'success': False, 'error': r.get('error', '重试失败')}, 400
        return {'success': True}
    except Exception as e:
        return {'error': str(e)}, 500


# ========== 文件名探测（异步，使用 httpx）=========

def _sanitize_filename(filename):
    if not filename:
        return "downloaded_file"
    filename = filename.strip().strip('"').strip("'")
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    filename = filename.rstrip('.')
    return filename if filename else "downloaded_file"

def _parse_content_disposition(cd_header):
    if not cd_header:
        return None
    match = re.search(r"filename\*=([^']+')?([^']+')?([^;]+)", cd_header, re.IGNORECASE)
    if match:
        return unquote(match.group(3))
    match = re.search(r'filename\s*=\s*["\']?([^"\';]+)["\']?', cd_header, re.IGNORECASE)
    if match:
        return unquote(match.group(1))
    return None

def _probe_ssrf_hook(request):
    """probe 网络请求（含重定向跳转）统一安全校验：httpx 请求行级 DNS 解析拦截"""
    err = validate_download_url(str(request.url))
    if err:
        raise RuntimeError(err)
    return request


async def _probe_filename_async(url, default_name="downloaded_file"):
    """异步探测文件名，零阻塞"""
    base_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.bilibili.com/',
        'Accept-Encoding': 'identity',
        'Accept': '*/*'
    }

    def fallback():
        path = url.split('?')[0]
        name = unquote(path.split('/')[-1])
        return _sanitize_filename(name) if name else default_name

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(5.0, connect=3.0),
                                     event_hooks={'request': [_probe_ssrf_hook]}) as client:
            # HEAD 请求
            resp = await client.request('HEAD', url, headers=base_headers)
            cd = resp.headers.get('content-disposition')
            if cd:
                fn = _parse_content_disposition(cd)
                if fn:
                    return _sanitize_filename(fn)

            # Range 请求（1 字节）
            range_h = dict(base_headers)
            range_h['Range'] = 'bytes=0-0'
            resp2 = await client.request('GET', url, headers=range_h)
            await resp2.aread()
            cd = resp2.headers.get('content-disposition')
            if cd:
                fn = _parse_content_disposition(cd)
                if fn:
                    return _sanitize_filename(fn)

            return fallback()
    except Exception:
        return fallback()


def _probe_filename_sync(url):
    """同步包装异步探测"""
    try:
        return asyncio.run(_probe_filename_async(url))
    except Exception:
        return _get_filename(url)


# ========== 探测文件信息 ==========

def handle_probe(params):
    """GET /api/url-download/probe?url=xxx 返回 {type, filename, size}"""
    url = params.get('url', [''])[0]
    if not url:
        return {'type': '', 'filename': '', 'size': ''}

    _, dl_type = get_downloader_for_url(url)

    # 磁力链接：仅从 dn= 参数提取文件名
    if dl_type == 'magnet':
        q = urllib.parse.urlparse(url).query
        qp = urllib.parse.parse_qs(q)
        dn = ''
        if qp.get('dn'):
            dn = _sanitize_filename(unquote(qp['dn'][0]))
        return {'type': '磁力链接', 'filename': dn, 'size': ''}

    if dl_type == 'torrent':
        return {'type': '种子文件', 'filename': '', 'size': ''}

    if dl_type == 'm3u8':
        fn = _get_filename(url)
        return {'type': 'm3u8 视频流', 'filename': fn, 'size': ''}

    if dl_type == 'ed2k':
        fn = _get_filename(url)
        return {'type': 'ed2k', 'filename': fn or '', 'size': '', 'unsupported': True}

    # HTTP 直链：异步探测文件名 + 同步探测大小（发请求前先做统一安全校验）
    err = validate_download_url(url)
    if err:
        return {'type': 'HTTP 直链', 'filename': '', 'size': '', 'error': err}
    filename = _probe_filename_sync(url)
    size = ''
    try:
        s = get_file_size(url)
        if s:
            size = str(s)
    except:
        pass
    return {'type': 'HTTP 直链', 'filename': filename, 'size': size}


# ========== 解析种子URL ==========
def handle_parse_torrent_url(params):
    url = params.get('url', [''])[0]
    if not url:
        return {'error': '缺少 url'}, 400
    # 发起网络请求前先做统一安全校验（dl_utils.parse_torrent_url 内也会二次校验）
    err = validate_download_url(url)
    if err:
        return {'error': err}, 400
    try:
        files = _parse_torrent_url(url)
        return {'success': True, 'files': files}
    except Exception as e:
        return {'error': str(e)}, 500


# ========== 上传种子 ==========
def handle_upload_torrent(raw_body, filename='upload.torrent'):
    try:
        if not raw_body:
            return {'error': '没有文件数据'}, 400
        # C-12：先解析并做路径预检（parse_torrent_data 内置 validate_torrent_paths），
        # 非法种子直接拒绝且不落盘残留
        files = parse_torrent_data(raw_body)
        torrent_path = save_uploaded_torrent(raw_body, filename)
        return {'success': True, 'files': files, 'torrent_path': torrent_path}
    except Exception as e:
        return {'error': str(e)}, 500