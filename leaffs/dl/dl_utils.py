#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基础工具模块
"""

import os
import re
import time
import base64
import subprocess
import threading
import tempfile
from datetime import datetime
from urllib.parse import unquote

import httpx


def _log(msg):
    now = datetime.now().strftime('%H:%M:%S.%f')[:-3]
    print(f'[DL-CORE {now}] {msg}', flush=True)


def sanitize_filename(name):
    if not name: return 'download'
    name = name.replace('/', '').replace('\\', '')
    name = re.sub(r'\.{2,}', '', name)
    name = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff_.\- ]', '', name)
    return name.strip() or 'download'


def format_size(b):
    if b <= 0: return '0 B'
    u = ['B', 'KB', 'MB', 'GB', 'TB']
    i = 0; v = float(b)
    while v >= 1024 and i < len(u) - 1: v /= 1024; i += 1
    return f'{v:.1f} {u[i]}' if i > 0 else f'{int(v)} B'


def format_speed(bps): return format_size(bps) + '/s'


def parse_content_disposition(header):
    if not header: return None
    m = re.search(r"filename\*=UTF-8''([^;]+)", header)
    if m: return unquote(m.group(1))
    m = re.search(r'filename="([^"]+)"', header)
    if m: return m.group(1)
    m = re.search(r"filename=([^;]+)", header)
    if m: return m.group(1).strip('"\'')
    return None


MIME_EXT_MAP = {
    'video/mp4': '.mp4', 'video/x-m4v': '.mp4', 'video/quicktime': '.mov',
    'video/x-msvideo': '.avi', 'video/x-matroska': '.mkv', 'video/webm': '.webm',
    'audio/mpeg': '.mp3', 'audio/mp4': '.m4a', 'audio/x-m4a': '.m4a',
    'audio/ogg': '.ogg', 'audio/wav': '.wav', 'image/jpeg': '.jpg',
    'image/png': '.png', 'image/gif': '.gif', 'application/pdf': '.pdf',
    'application/zip': '.zip', 'application/x-rar-compressed': '.rar',
    'application/x-7z-compressed': '.7z', 'text/plain': '.txt',
}

def get_extension_from_mime(mime):
    if not mime: return ''
    return MIME_EXT_MAP.get(mime.split(';')[0].strip(), '')


def decode_thunder(url):
    if not url.startswith('thunder://'): return url
    b64 = url[10:]
    try:
        padding = 4 - len(b64) % 4
        if padding != 4: b64 += '=' * padding
        decoded = base64.b64decode(b64).decode('utf-8', errors='ignore')
        if decoded.startswith('AA') and decoded.endswith('ZZ'): decoded = decoded[2:-2]
        return decoded
    except Exception: return url


# ========== 下载目标 SSRF 校验（C-08a/C-08b [D8]） ==========
# 云 metadata / 运营商共享(CGNAT)地址段：即使 allow_private_targets=true 也永不放行。
#   - 100.64.0.0/10：阿里云 metadata 100.100.100.200 等“类 CGNAT 元数据”所在段；
#   - 169.254.0.0/16（含 169.254.169.254 整段）由 ip.is_link_local 统一拦截。
_CGNAT_METADATA_NETS = []
try:
    import ipaddress as _ipaddr_mod
    _CGNAT_METADATA_NETS = [_ipaddr_mod.ip_network('100.64.0.0/10')]
except Exception:
    pass

def _resolve_host(host, port):
    """解析一次 host:port 的 getaddrinfo（独立函数便于纯函数单测打桩，不触网）。"""
    import socket
    return socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)

def _allow_private_targets():
    """读取下载器配置键 allow_private_targets（懒加载避免 import 环）。"""
    from . import dl_config
    return bool(dl_config.get_allow_private_targets())

def _ip_allowed(ip, allow_private):
    """逐 IP 判定下载目标是否允许，返回 (ok:bool, err:str|None)。

    allow_private=False（默认）：必须 is_global（且非多播——Python 3.12 中多播
    地址 is_global 为 True，需显式排除）；
    allow_private=True：放行 RFC1918/ULA 等私网，回环/链路本地(含云 metadata
    169.254.0.0/16)/未指定/多播/保留/CGNAT 共享段(100.64.0.0/10) 仍禁。
    """
    # IPv4-mapped IPv6（如 [::ffff:127.0.0.1]）归一为 IPv4 后判定
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not allow_private:
        if ip.is_multicast or not ip.is_global:
            return False, f'禁止下载到非公网地址（{ip}）'
        return True, None
    if ip.is_loopback:
        return False, f'禁止下载到本机回环地址（{ip}）'
    if ip.is_link_local:
        return False, f'禁止下载到链路本地/云元数据地址（{ip}）'
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return False, f'禁止下载到未指定/多播/保留地址（{ip}）'
    if ip.version == 4:
        for net in _CGNAT_METADATA_NETS:
            if ip in net:
                return False, f'禁止下载到云元数据/运营商共享地址（{ip}）'
    return True, None

def validate_download_host_only(host, port, allow_private=None):
    """C-08b：解析一次 host:port 并逐 IP 校验（DNS 解析级）。

    返回 (ips, None) 允许或 (None, err) 拒绝。allow_private=None 时取配置键
    allow_private_targets（默认 False）。字面量 IP 不走 DNS 直接判定。
    """
    if allow_private is None:
        allow_private = _allow_private_targets()
    try:
        import ipaddress
        # 字面量 IP 快路径（含 IPv4-mapped），无需 DNS
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None:
            ok, err = _ip_allowed(ip, allow_private)
            if not ok:
                return None, err
            return [str(ip)], None
        try:
            infos = _resolve_host(host, port)
        except Exception:
            return None, '无法解析下载地址的主机'
        if not infos:
            return None, '无法解析下载地址的主机'
        ips = []
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except Exception:
                continue
            ok, err = _ip_allowed(ip, allow_private)
            if not ok:
                return None, err
            s = str(ip)
            if s not in ips:
                ips.append(s)
        if not ips:
            return None, '无法解析下载地址的主机'
        return ips, None
    except Exception as e:
        return None, f'下载地址校验失败: {e}'

def validate_download_url(url, allow_private=None):
    """校验“最终要实际拉取”的 URL。thunder 先解码；仅允许 http/https(magnet 交给 aria2)；
    对主机名做 DNS 解析级逐 IP 校验（默认必须公网 is_global；allow_private_targets=true
    时退化为禁回环/链路本地/云 metadata 等、放行 RFC1918/ULA），返回错误文案或 None。

    allow_private=None 时按配置键 allow_private_targets 动态取值。
    """
    try:
        from urllib.parse import urlparse

        if not url:
            return '下载地址为空'
        if url.startswith('thunder://'):
            url = decode_thunder(url)
        p = urlparse(url)
        scheme = (p.scheme or '').lower()
        if scheme == 'magnet':
            return None  # magnet 交给 aria2，不做 HTTP 拉取
        if scheme not in ('http', 'https'):
            return f'仅支持 http/https 下载（已拒绝 {scheme or "空"}://）'
        host = p.hostname or ''
        if not host:
            return '下载地址缺少主机名'
        port = p.port or (443 if scheme == 'https' else 80)
        _, err = validate_download_host_only(host, port, allow_private=allow_private)
        return err
    except Exception as e:
        return f'下载地址校验失败: {e}'


from leaffs.utils.core import BASE_DIR, CACHE_DIR, find_bundled_exe

# aria2c 内置依赖：统一走资源根查找（与 ffmpeg 同一套逻辑，兼容源码/打包）
ARIA2C_PATH = find_bundled_exe('aria2c.exe')
if ARIA2C_PATH is None:
    try:
        subprocess.run(['aria2c', '--version'], capture_output=True, check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        ARIA2C_PATH = 'aria2c'   # PATH 中的全局 aria2c 兜底
    except Exception:
        ARIA2C_PATH = None

def has_aria2c(): return ARIA2C_PATH is not None


# ========== aria2c 进程退出清理 ==========
_CTRL_HANDLER_REF = None  # 模块级引用防止 GC

def _register_ctrl_handler():
    """注册控制台事件处理器，关闭终端时立即杀死 aria2c"""
    global _CTRL_HANDLER_REF
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        
        CTRL_C_EVENT = 0
        CTRL_BREAK_EVENT = 1
        CTRL_CLOSE_EVENT = 2
        
        handler_cb = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
        
        def _console_handler(dwCtrlType):
            if dwCtrlType in (CTRL_C_EVENT, CTRL_BREAK_EVENT, CTRL_CLOSE_EVENT):
                _cleanup_all_aria2c()
            return False
        
        _CTRL_HANDLER_REF = handler_cb(_console_handler)
        ret = kernel32.SetConsoleCtrlHandler(_CTRL_HANDLER_REF, True)
        _log(f'[CTRL] SetConsoleCtrlHandler: {ret}')
    except Exception as e:
        _log(f'[CTRL] SetConsoleCtrlHandler 失败: {e}')

# ========== aria2c 进程管理 ==========
_ARIA2C_PROCESSES = []
_ARIA2C_LOCK = threading.Lock()

def _kill_aria2c_proc(proc):
    try:
        if proc.poll() is None:
            proc.terminate()
            try: proc.wait(timeout=2)
            except: pass
        if proc.poll() is None:
            import subprocess as _sp
            try:
                _sp.run(['taskkill', '/f', '/t', '/pid', str(proc.pid)], capture_output=True, timeout=5,
                        creationflags=subprocess.CREATE_NO_WINDOW)
            except Exception:
                try: proc.kill(); proc.wait(timeout=2)
                except: pass
    except: pass

def _cleanup_all_aria2c():
    with _ARIA2C_LOCK:
        procs = list(_ARIA2C_PROCESSES)
        _ARIA2C_PROCESSES.clear()
    for proc in procs: _kill_aria2c_proc(proc)
    try: subprocess.run(['taskkill', '/f', '/im', 'aria2c.exe'], capture_output=True, timeout=5,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    except: pass

def _assign_to_job(proc):
    """保持兼容性 - 实际清理由 SetConsoleCtrlHandler + taskkill 完成"""
    return False

def track_aria2c_process(proc):
    with _ARIA2C_LOCK: _ARIA2C_PROCESSES.append(proc)

def untrack_aria2c_process(proc):
    with _ARIA2C_LOCK:
        if proc in _ARIA2C_PROCESSES: _ARIA2C_PROCESSES.remove(proc)


# ========== 全局退出钩子 ==========
def _register_cleanup():
    import atexit
    atexit.register(_cleanup_all_aria2c)
    try:
        import signal
        def _sig_handler(signum, frame):
            _cleanup_all_aria2c()
            signal.signal(signum, signal.SIG_DFL)
            os._exit(128 + signum)
        signal.signal(signal.SIGTERM, _sig_handler)
        signal.signal(signal.SIGINT, _sig_handler)
    except: pass

_register_ctrl_handler()
_register_cleanup()


def is_magnet(url): return url.startswith('magnet:')
def is_torrent_url(url): return url.lower().endswith('.torrent')
def is_ed2k(url): return url.startswith('ed2k://')


_TRACKER_URL = 'https://trackerslist.com/all.txt'
# C-08e：tracker 源白名单（常量公网，防止配置被改成内网/本地后由本模块去拉取）
_TRACKER_SOURCE_HOSTS = ('trackerslist.com',)
# C-08e：响应行数上限，防超大响应撑爆内存
MAX_TRACKER_LINES = 5000
# 本地默认 tracker 列表属于只读内置资源，跟随资源根(BASE_DIR)，兼容源码/打包
_TRACKER_FALLBACK = os.path.join(BASE_DIR, 'config', 'all.txt')
# 最近一次联网获取的 tracker 缓存（数据层，写入后供后续启动秒回使用，不阻塞启动）
_TRACKER_CACHE = os.path.join(CACHE_DIR, 'trackers.txt')
_FALLBACK_TRACKERS = [
    'udp://tracker.opentrackr.org:1337/announce', 'udp://tracker.openbittorrent.com:6969/announce',
    'udp://opentracker.i2p.rocks:6969/announce', 'udp://tracker.torrent.eu.org:451/announce',
    'udp://exodus.desync.com:6969/announce', 'udp://open.demonii.com:1337/announce',
    'udp://tracker.moeking.me:6969/announce', 'https://tracker.lilithraws.cf:443/announce',
    'http://tracker.cpp.re:6969/announce', 'http://tracker.bittor.pw:1337/announce',
]

def _read_tracker_lines(path):
    try:
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                return [l.strip() for l in f.read().splitlines() if l.strip() and not l.strip().startswith('#')]
    except Exception:
        pass
    return []

def _normalize_trackers(lines, max_count):
    https = [l for l in lines if l.startswith('https://')]
    http = [l for l in lines if l.startswith('http://')]
    udp = [l for l in lines if l.startswith('udp://')]
    return (https + http + udp)[:max_count]

def _save_tracker_cache(lines):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = _TRACKER_CACHE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        os.replace(tmp, _TRACKER_CACHE)
    except Exception:
        pass

def _tracker_source_ok(url):
    """C-08e：联网拉取 tracker 前对常量源做白名单 + DNS 级校验（防源被改成私网）。"""
    from urllib.parse import urlparse
    try:
        p = urlparse(url)
        scheme = (p.scheme or '').lower()
        host = (p.hostname or '').lower()
    except Exception:
        return False
    if scheme != 'https' or host not in _TRACKER_SOURCE_HOSTS:
        return False
    return validate_download_url(url) is None

def load_trackers(max_count=150, network=True):
    """加载 BT tracker 列表。

    network=True 时联网拉取最新列表并写入数据层缓存（由后台线程调用）；
    network=False 时只读缓存/内置资源（毫秒级，供启动路径使用，不阻塞）。
    两路都失败则回退到内嵌常用列表。
    """
    lines = []
    if network:
        try:
            if not _tracker_source_ok(_TRACKER_URL):
                raise RuntimeError('tracker 源不在白名单或非公网地址')
            resp = httpx.get(_TRACKER_URL, timeout=5.0, follow_redirects=True)
            if resp.status_code == 200:
                lines = [l.strip() for l in resp.text.splitlines()
                         if l.strip() and not l.strip().startswith('#')][:MAX_TRACKER_LINES]
                if lines:
                    _save_tracker_cache(_normalize_trackers(lines, max_count))
        except Exception:
            pass
    if not lines:
        # 本地读取：数据层缓存(最近一次联网结果) → 内置资源列表
        lines = _read_tracker_lines(_TRACKER_CACHE) or _read_tracker_lines(_TRACKER_FALLBACK)
    if not lines:
        lines = list(_FALLBACK_TRACKERS)
    return _normalize_trackers(lines, max_count)

def refresh_trackers_async():
    """后台刷新 tracker 列表（不阻塞调用方），结果写入缓存供下次启动使用"""
    try:
        threading.Thread(target=load_trackers, kwargs={'network': True}, daemon=True).start()
    except Exception:
        pass


_secure_client = None
_client_lock = threading.Lock()

def _ssrf_hook(request):
    """C-08b：httpx 请求事件钩子（含重定向每一跳、每个最终请求）在发出前做
    DNS 解析级逐跳校验，取代旧的“字面量主机名黑名单”校验。"""
    err = validate_download_url(str(request.url))
    if err:
        raise RuntimeError(f'下载目标被安全策略拒绝: {err}')
    return request

def get_secure_client():
    global _secure_client
    if _secure_client is None:
        with _client_lock:
            if _secure_client is None:
                _secure_client = httpx.Client(timeout=60.0, follow_redirects=True,
                    headers={'User-Agent': 'Mozilla/5.0'}, event_hooks={'request': [_ssrf_hook]})
    return _secure_client


# ========== C-08c 远程 http(s) 内容受控拉取 ==========
MAX_TORRENT_PREFETCH_BYTES = 8 * 1024 * 1024   # 远程种子预拉取上限 8MB
_FETCH_CHUNK = 64 * 1024

def _fetch_http_bytes(url, max_bytes, what='远程资源'):
    """受控拉取 http(s) 内容：逐跳 SSRF 校验 + Content-Length 与实读双检大小上限。

    max_bytes<=0 表示不限制。超限/校验失败抛 Exception；成功返回 bytes。
    """
    err = validate_download_url(url)
    if err:
        raise Exception(err)
    client = get_secure_client()
    with client.stream('GET', url, headers={'User-Agent': 'Mozilla/5.0'}) as resp:
        resp.raise_for_status()
        cl = resp.headers.get('Content-Length')
        if cl is not None and max_bytes > 0:
            try:
                if int(cl) > max_bytes:
                    raise Exception(f'{what}超过大小上限（{format_size(max_bytes)}）')
            except ValueError:
                pass
        data = bytearray()
        for chunk in resp.iter_bytes(chunk_size=_FETCH_CHUNK):
            data += chunk
            if max_bytes > 0 and len(data) > max_bytes:
                raise Exception(f'{what}超过大小上限（{format_size(max_bytes)}）')
    return bytes(data)

def prefetch_http_torrent(url):
    """C-08c：远程 http(s).torrent 受控预拉取。

    校验 URL → 限 8MB 双检拉取 → bencode 解析（含 C-12 路径预检）通过后落盘
    到服务端临时目录，返回本地 .torrent 路径；随后应走 add_torrent 本地分支，
    不再把 http URL 直接交给 aria2 addUri 自拉。
    """
    if not (isinstance(url, str) and (url.startswith('http://') or url.startswith('https://'))):
        raise Exception('仅支持远程 http(s) 种子预拉取')
    data = _fetch_http_bytes(url, MAX_TORRENT_PREFETCH_BYTES, '远程种子')
    parse_torrent_data(data)   # 解析（含路径预检）不通过即拒绝
    return save_uploaded_torrent(data, filename='prefetch.torrent')

def is_trusted_local_torrent(path):
    """本地 .torrent 路径是否由服务端自身落盘（系统临时目录 dl_torrents 内）。

    用于 handle_start：上传种子/远程预拉取都会先落盘到该目录，随后以本地路径
    走 add_torrent 分支；其它任意本地路径一律不允许作为下载源。
    """
    try:
        if not path or not os.path.isfile(path):
            return False
        p = os.path.normpath(os.path.abspath(path))
        base = os.path.normpath(os.path.join(tempfile.gettempdir(), 'dl_torrents'))
        return (p == base or p.startswith(base + os.sep)) and p.lower().endswith('.torrent')
    except Exception:
        return False


class BencodeDecodeError(Exception): pass

def _bdecode_int(data, pos):
    end = data.find(b'e', pos)
    if end < 0: raise BencodeDecodeError('缺少 e 结束符')
    try: val = int(data[pos:end])
    except ValueError: raise BencodeDecodeError(f'整数格式错误: {data[pos:end]}')
    return val, end + 1

def _bdecode_str(data, pos):
    colon = data.find(b':', pos)
    if colon < 0: raise BencodeDecodeError('缺少 : 分隔符')
    try: length = int(data[pos:colon])
    except ValueError: raise BencodeDecodeError(f'字符串长度格式错误: {data[pos:colon]}')
    start = colon + 1; end = start + length
    if end > len(data): raise BencodeDecodeError('字符串长度超出数据范围')
    return data[start:end], end

def _bdecode_list(data, pos):
    pos += 1; items = []
    while pos < len(data) and data[pos:pos+1] != b'e':
        item, pos = _bdecode_one(data, pos); items.append(item)
    if pos >= len(data): raise BencodeDecodeError('列表缺少 e 结束符')
    return items, pos + 1

def _bdecode_dict(data, pos):
    pos += 1; d = {}
    while pos < len(data) and data[pos:pos+1] != b'e':
        key, pos = _bdecode_str(data, pos)
        val, pos = _bdecode_one(data, pos)
        try: d[key.decode('utf-8')] = val
        except UnicodeDecodeError: d[key.decode('latin-1', errors='replace')] = val
    if pos >= len(data): raise BencodeDecodeError('字典缺少 e 结束符')
    return d, pos + 1

def _bdecode_one(data, pos):
    if pos >= len(data): raise BencodeDecodeError('数据不足')
    ch = data[pos:pos+1]
    if ch == b'i': return _bdecode_int(data, pos + 1)
    elif ch == b'l': return _bdecode_list(data, pos)
    elif ch == b'd': return _bdecode_dict(data, pos)
    elif b'0' <= ch <= b'9': return _bdecode_str(data, pos)
    raise BencodeDecodeError(f'未知类型标记: {ch}')

def bdecode(data):
    if not isinstance(data, bytes): raise BencodeDecodeError('需要 bytes 类型数据')
    result, pos = _bdecode_one(data, 0)
    if pos != len(data) and data[pos:].strip(): raise BencodeDecodeError('解码后还有多余数据')
    return result

def validate_torrent_paths(files):
    """C-12：对 parse_torrent_data 产出的每个文件路径做逐段净化预检。

    按 / 与 \\ 拆分后：任一段为空、为 '..'、或以绝对分隔符开头（拆分后表现为
    空段）、或含 ':'（Windows 盘符/ADS）→ 视为非法路径，整体拒绝该种子。
    返回 True 或抛 Exception('种子包含非法路径')。
    """
    for f in files:
        raw = f.get('path') or ''
        for seg in re.split(r'[/\\]', raw):
            if (not seg) or seg == '..' or ':' in seg:
                raise Exception('种子包含非法路径（已拒绝）')
            if seg.startswith('/') or seg.startswith('\\'):
                raise Exception('种子包含非法路径（已拒绝）')
    return True

def parse_torrent_data(data):
    torrent = bdecode(data)
    if not isinstance(torrent, dict): raise Exception('种子文件格式错误')
    info = torrent.get('info')
    if not isinstance(info, dict): raise Exception('种子文件缺少 info 字典')
    files = []
    if 'files' in info:
        base_name = info.get('name', b'')
        if isinstance(base_name, bytes): base_name = base_name.decode('utf-8', errors='replace')
        for i, f in enumerate(info['files']):
            if not isinstance(f, dict): continue
            length = f.get('length', 0)
            path_parts = f.get('path', [])
            if isinstance(path_parts, list):
                fp = '/'.join(p.decode('utf-8', errors='replace') if isinstance(p, bytes) else str(p) for p in path_parts)
            else: fp = str(path_parts)
            files.append({'index': i, 'path': (base_name + '/' + fp) if base_name else fp, 'size': length, 'size_display': ''})
    else:
        name = info.get('name', b'')
        if isinstance(name, bytes): name = name.decode('utf-8', errors='replace')
        files.append({'index': 0, 'path': str(name) if name else 'unknown', 'size': info.get('length', 0), 'size_display': ''})
    if not files: raise Exception('种子文件内未找到文件列表')
    validate_torrent_paths(files)   # C-12：任一路径非法即整体拒绝
    return files

def parse_torrent_url(url):
    err = validate_download_url(url)
    if err: raise Exception(err)
    # C-08c：受控拉取（逐跳校验 + 8MB 上限双检），解析（含 C-12 路径预检）不通过即拒绝
    data = _fetch_http_bytes(url, MAX_TORRENT_PREFETCH_BYTES, '远程种子')
    return parse_torrent_data(data)

def parse_torrent_file(filepath):
    if not os.path.exists(filepath): raise Exception(f'种子文件不存在: {filepath}')
    if os.path.getsize(filepath) == 0: raise Exception('种子文件为空')
    with open(filepath, 'rb') as f: return parse_torrent_data(f.read())

def save_uploaded_torrent(data, filename='upload.torrent'):
    safe_name = os.path.basename(filename or 'upload.torrent')
    tmp_dir = os.path.join(tempfile.gettempdir(), 'dl_torrents')
    os.makedirs(tmp_dir, exist_ok=True)
    fp = os.path.join(tmp_dir, safe_name)
    with open(fp, 'wb') as f: f.write(data)
    return fp