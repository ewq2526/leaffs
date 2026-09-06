"""文件系统 API — 列表/下载/上传/缩略图/搜索/压缩等"""

import os
import json
import urllib.parse
import shutil
import zipfile
import tempfile
import time
import socket
import threading


# ========== C-01/R1：guest 写权限策略（HTTP 版 + WS 版，IC-WRITE） ==========
# guest 规则（R1 定稿）：guest_public_write=True 时 guest 仅可在 public[/子目录] 下
# “新建”上传；目标已存在视作 overwrite → 由 fs_core.handle_upload(auto_unique=True)
# 自动改名承接（绝不覆盖）；guest 的 delete/mkdir 与未登录一律拒写；空 path(共享根) 403。

def _guest_write_flag():
    """guest 写 public 开关（IC-CFG：cfg_core.get_guest_public_write，B2 组实现）。

    getter 尚未就绪时按 R1 定稿默认 True 兜底（guest 仅可新建、禁覆盖/删除/建目录）。
    """
    try:
        from leaffs.config_core import cfg_core as _cc
        return bool(_cc.get_guest_public_write())
    except Exception:
        return True


def write_allowed(handler, sub_path, op):
    """HTTP 写权限判定（C-01/R1）。op ∈ {'upload','overwrite','delete','mkdir'}。

    admin/super_admin/user 放行（自身目录越界由 _check_path_permission 另行把关）；
    guest 按 R1 规则收敛（见上）；未登录一律拒写。
    """
    try:
        role = handler._get_effective_role()
    except Exception:
        role = None
    if role in ('admin', 'super_admin', 'user'):
        return True
    if role != 'guest':
        return False            # 未登录/会话失效一律拒写（A-02 拦 401，这里兜底）
    if not _guest_write_flag():
        return False            # 开关关闭：guest 只读
    if op != 'upload':
        return False            # guest 禁 delete/mkdir/overwrite（覆盖由 fs_core 自动改名承接）
    if not sub_path:
        return False            # 空 path（共享根）guest 403（R2）
    sub = sub_path.strip('/')
    return sub == 'public' or sub.startswith('public/')


def ws_write_allowed(role, username, path, op, guest_mode=True):
    """WS 侧写权限判定（C-02，handler 无关版；供 leaffs.py A 组 ws_handler 调用）。

    与 write_allowed 同规则（R1）：guest 默认禁写；guest_public_write 开启时仅允许
    public[/子目录] 的 'upload'（新建，覆盖走 fs_core 自动改名）；guest 的
    delete/mkdir/overwrite、guest 模式关闭、未登录一律拒绝。
    """
    if role in ('admin', 'super_admin', 'user'):
        return True
    if role != 'guest' or not guest_mode:
        return False            # 未登录 / 游客模式已关闭（残留 guest 会话视为无效）
    if not _guest_write_flag():
        return False
    if op != 'upload':
        return False
    if not path:
        return False
    sub = path.strip('/')
    return sub == 'public' or sub.startswith('public/')


def _upload_quota_buckets(target_dir, UPLOAD_DIR):
    """把上传目标目录映射为配额 bucket 名（IC-QUOTA）。

    恒含 'total'；位于 public 下追加 'public'；位于 users/<name> 下追加 'users/<name>'，
    与 leaffs._check_quota 三层口径（total/public/user）保持一致。
    """
    buckets = ['total']
    try:
        ud = os.path.normcase(os.path.realpath(UPLOAD_DIR))
        tn = os.path.normcase(os.path.realpath(target_dir))
        pub = os.path.normcase(os.path.join(UPLOAD_DIR, 'public'))
        users = os.path.normcase(os.path.join(UPLOAD_DIR, 'users'))
        if tn == pub or tn.startswith(pub + os.sep):
            buckets.append('public')
        elif tn == users or tn.startswith(users + os.sep):
            rel = os.path.relpath(tn, users)
            first = rel.split(os.sep)[0] if rel else ''
            if first:
                buckets.append('users/' + first)
    except Exception:
        pass
    return buckets


# ========== C-04：搜索限流（每 IP 令牌桶：容量 6、补速 6/60s） ==========
_search_lock = threading.Lock()
_search_buckets = {}            # ip -> (tokens, last_refill_ts)
_SEARCH_CAPACITY = 6            # 桶容量：6 次
_SEARCH_RATE = 6.0 / 60.0       # 补速：6 次 / 60 秒
_SEARCH_MAX_Q = 64              # 查询串 q 最大长度
_SEARCH_MAX_RESULTS = 500       # 结果截断上限

def _search_take_token(ip):
    """令牌桶取牌：有令牌返回 True 并扣 1，否则返回 False（429）"""
    now = time.time()
    with _search_lock:
        tokens, ts = _search_buckets.get(ip, (_SEARCH_CAPACITY, now))
        tokens = min(_SEARCH_CAPACITY, tokens + (now - ts) * _SEARCH_RATE)
        if tokens >= 1.0:
            _search_buckets[ip] = (tokens - 1.0, now)
            return True
        _search_buckets[ip] = (tokens, now)
        return False


def send_files(handler, list_files):
    """文件列表"""
    try:
        q = urllib.parse.urlparse(handler.path).query
        p = urllib.parse.parse_qs(q).get('path', [''])[0]
        # 规范化路径
        norm_p = _normalize_rel_path(p)
        if norm_p is None:
            handler.send_json({'error': '路径不合法'}, 403); return
        p = norm_p
        if not handler._check_path_permission(p):
            handler.send_json({'error': '无权限访问此目录'}, 403); return
        result, err = list_files(p)
        if err:
            handler.send_json({'error': err}, 403)
        elif result is None:
            handler.send_json({'error': '未知错误'}, 500)
        else:
            handler.send_json(result)
    except Exception as e:
        handler.send_json({'error': str(e)}, 500)


def send_raw(handler, UPLOAD_DIR, PREVIEW_MAX_SIZE, is_path_safe, get_mime, DISCONNECTED_EXCEPTIONS):
    """原始文件内容"""
    try:
        q = urllib.parse.urlparse(handler.path).query
        p = urllib.parse.parse_qs(q).get('path', [''])[0]
        if not p: handler.send_error(400); return
        # 规范化路径
        norm_p = _normalize_rel_path(p)
        if norm_p is None:
            handler.send_error(403); return
        p = norm_p
        if not handler._check_path_permission(p):
            handler.send_error(403); return
        full = os.path.join(UPLOAD_DIR, p)
        if not is_path_safe(UPLOAD_DIR, full): handler.send_error(403); return
        if not os.path.exists(full) or os.path.isdir(full): handler.send_error(404); return
        if os.path.getsize(full) > PREVIEW_MAX_SIZE:
            handler.send_response(413); handler.end_headers()
            handler.wfile.write('[文件过大]'.encode('utf-8')); return
        mime = get_mime(p)
        # 可执行/可内联文档类型不再同源渲染：HTML/SVG/XML/JS/JSON/RSS/Atom 强制以
        # 纯文本直出并附加下载头，浏览器不会内联解析执行，同时纯文本内容仍可直接预览。
        force_plain = False
        _mime_base = (mime or '').lower().split(';', 1)[0].strip()
        # C-11：force_plain 名单扩展（application/xml、text/xml、application/json、
        # application/javascript、text/javascript、rss/atom 与既有 html/svg 一致处理）
        if _mime_base in ('text/html', 'application/xhtml+xml', 'image/svg+xml',
                          'application/xml', 'text/xml',
                          'application/javascript', 'text/javascript',
                          'application/json',
                          'application/rss+xml', 'application/atom+xml'):
            mime = 'text/plain; charset=utf-8'
            force_plain = True
        handler.send_response(200)
        handler.send_header('Content-Type', mime or 'text/plain; charset=utf-8')
        # 同源内容隔离：sandbox 阻止脚本执行/表单提交等，且不信任任何同源资源
        # （外部图片等也仅允许 data: 内嵌）；配合 nosniff 防止 MIME 嗅探。
        handler.send_header('Content-Security-Policy',
                            "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:")
        handler.send_header('X-Content-Type-Options', 'nosniff')
        if force_plain:
            handler.send_header('Content-Disposition', 'attachment; filename="preview.txt"')
        handler.end_headers()
        # 正文流式写出（分块 + 120s 写侧无进展超时）；R4 后本端点不占任何全局并发槽
        with open(full, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                _stream_write(handler, chunk)
    except DISCONNECTED_EXCEPTIONS:
        # 断连/写侧无进展超时：按断连处理（部分响应已发出，不再补 500）
        try: handler.close_connection = True
        except Exception: pass
    except Exception as e:
        try: handler.send_error(500)
        except DISCONNECTED_EXCEPTIONS: pass


def _parse_range_header(range_header, file_size):
    """解析 Range 头，返回 (start, end, error_status)（RFC 7233）

    - 无 Range、覆盖全文件的开区间/超长 suffix（bytes=0-、bytes=-N 且 N>=size）→ 整文件；
    - bytes=start-end / bytes=start- 常规区间不变；
    - bytes=-N（suffix-range，'-' 前无数字）→ 返回文件**末 N 字节**
      (start=size-N, end=size-1)；suffix=0 或缺失 → 416；
    - 多 Range（逗号分隔）、start 越界、start>end、非数字/负数 → 416。
    """
    if not range_header:
        return 0, file_size - 1, None
    if not range_header.startswith('bytes='):
        return None, None, 416
    range_val = range_header[6:].strip()
    if not range_val:
        return None, None, 416
    # 空文件不存在任何可满足字节区间：显式 Range 一律 416（与既有行为一致）
    if file_size <= 0:
        return None, None, 416
    # 多 Range（逗号分隔的 byte-range-set）一律按 RFC7233 视为不可满足 → 416
    if ',' in range_val:
        return None, None, 416
    try:
        if '-' in range_val:
            parts = range_val.split('-', 1)
            start_str = parts[0].strip()
            end_str = parts[1].strip()
            if not start_str:
                # suffix-range：bytes=-N → 文件末 N 字节（RFC7233 §2.1）
                suffix = int(end_str)
                if suffix <= 0:
                    return None, None, 416
                if suffix >= file_size:
                    return 0, file_size - 1, None      # 覆盖全文件 → 整文件
                return file_size - suffix, file_size - 1, None
            start = int(start_str)
            end = int(end_str) if end_str else (file_size - 1)
            if start < 0 or end < 0: return None, None, 416
            if start >= file_size: return None, None, 416
            if end >= file_size: end = file_size - 1
            if start > end: return None, None, 416
            return start, end, None
        else:
            start = int(range_val)
            if start < 0 or start >= file_size: return None, None, 416
            return start, file_size - 1, None
    except (ValueError, TypeError):
        return None, None, 416


def _rate_stream_cap(rate_bytes):
    """按 10MB/s/线程 折算并发下载流上限：ceil(限速/10MB/s)，至少 1"""
    per = 10 * 1024 * 1024
    if rate_bytes <= 0:
        return 1
    return max(1, (rate_bytes + per - 1) // per)


# 架构修订 R4（慢客户端双控 · 写侧）：流式正文写循环的“无进展超时”（秒）兜底默认值。
# 实际生效值取配置键 io_idle_timeout_secs（默认 120，cfg_core 深配键），此处常量仅作
# cfg_core 不可用时的回退。读侧沿用 HTTPHandler 的每连接 socket 读超时（READ_TIMEOUT=60s）；
# 写侧在每次写出前把 socket 超时切到该阈值 —— 客户端停止读取导致任一次写阻塞超过阈值
# 即抛超时，调用方（各发送函数）把它按断连处理并清理资源，绝不无限滞留线程。
WRITE_STALL_TIMEOUT = 120.0


def _stream_write(handler, data):
    """带“无进展超时”的正文写出：写前把 socket 超时切到 io_idle_timeout_secs。

    超时（TimeoutError ⊂ OSError）由调用方的 DISCONNECTED_EXCEPTIONS 兜住，等同于断连。
    """
    timeout = WRITE_STALL_TIMEOUT
    try:
        from leaffs.config_core import cfg_core as _cc
        timeout = max(1.0, float(_cc.get_io_idle_timeout_secs() or WRITE_STALL_TIMEOUT))
    except Exception:
        pass
    try:
        handler.connection.settimeout(timeout)
    except Exception:
        pass
    handler.wfile.write(data)


def handle_download(handler, UPLOAD_DIR, COPY_BUFFER_SIZE, is_path_safe, get_mime,
                    get_session, get_session_username, get_user_speed_limit,
                    get_speed_limit, get_user_limiter, DISCONNECTED_EXCEPTIONS):
    """文件下载（支持断点续传和限速）

    并发模型（架构修订 R4：全局并发槽已取消）：
    - 不再占用任何应用层全局并发槽 —— 无限速与限速下载的正文传输都在“整机总连接/线程
      准入上限（cfg_core.max_total_conns，默认 256）+ 每 IP 连接上限”内自由并发，慢速
      下载不再拖慢登录/列表等快速 API；
    - 限速下载仍按“每用户并发流名额”限制（上限 = max(1, ceil(限速/10MB/s))），
      满额 429（无等待）；
    - 正文写循环经 _stream_write 带 io_idle_timeout_secs（默认 120s）无进展超时
      （写侧慢客户端双控）。
    """
    try:
        name = urllib.parse.unquote(handler.path.split('/download/')[-1])
        # 规范化路径，禁止路径穿越
        norm_name = _normalize_rel_path(name)
        if norm_name is None:
            handler.send_error(403); return
        name = norm_name
        path = os.path.join(UPLOAD_DIR, name)
        if not is_path_safe(UPLOAD_DIR, path): handler.send_error(403); return
        if not os.path.exists(path) or os.path.isdir(path): handler.send_error(404); return
        if not handler._check_path_permission(name):
            handler.send_error(403); return
        file_size = os.path.getsize(path)
        mime = get_mime(name)
        enc = urllib.parse.quote(os.path.basename(name))
        range_header = handler.headers.get('Range', '')
        start, end, range_err = _parse_range_header(range_header, file_size)
        if range_err is not None:
            handler.send_response(range_err)
            handler.send_header('Content-Range', f'bytes */{file_size}')
            handler.end_headers()
            return
        is_range = range_header and range_header.startswith('bytes=') and (start > 0 or end < file_size - 1)
        if is_range:
            cl = end - start + 1
            status, extra_headers = 206, [('Content-Range', f'bytes {start}-{end}/{file_size}')]
        else:
            cl, start = file_size, 0
            status, extra_headers = 200, []
        cookie = handler.headers.get('Cookie', '')
        _, sid = get_session(cookie, True, handler.client_address[0])
        username = get_session_username(sid) if sid else ''
        if username:
            user_id = 'user' + username
            user_speed = get_user_speed_limit(username)
            effective_rate = user_speed if user_speed > 0 else get_speed_limit()
        else:
            user_id = '__anon__' + handler.client_address[0]
            effective_rate = get_speed_limit()
        limiter = get_user_limiter()
        limited = effective_rate > 0
        if limited:
            CHUNK_SIZE = int(effective_rate)
            CHUNK_SIZE = (CHUNK_SIZE // 1024) * 1024
            if CHUNK_SIZE < 65536: CHUNK_SIZE = 65536
            if CHUNK_SIZE > COPY_BUFFER_SIZE: CHUNK_SIZE = COPY_BUFFER_SIZE
        else:
            CHUNK_SIZE = COPY_BUFFER_SIZE

        token_held = False

        # 限速用户：先占一个“每用户并发流名额”（响应头尚未发送，可安全拒绝）。
        # 不再获取全局并发槽（R4）：总并发由连接级准入兜底。
        if limited:
            cap = _rate_stream_cap(effective_rate)
            if not limiter.acquire_stream(user_id, cap):
                handler.send_json({'error': '下载并发已满（限速用户同时只允许 %d 个下载），请稍后再试' % cap}, 429)
                return
            token_held = True

        try:
            handler.send_response(status)
            handler.send_header('Content-Type', mime or 'application/octet-stream')
            handler.send_header('Content-Disposition', f"attachment; filename*=UTF-8''{enc}")
            handler.send_header('Content-Length', str(cl))
            handler.send_header('Accept-Ranges', 'bytes')
            handler.send_header('X-Content-Type-Options', 'nosniff')   # C-11
            for k, v in extra_headers:
                handler.send_header(k, v)
            handler.end_headers()

            # 正文传输（不限速/限速）均不占全局并发槽；写循环带 120s 无进展超时
            with open(path, 'rb') as f:
                f.seek(start); remaining = cl
                while remaining > 0:
                    chunk = f.read(min(remaining, CHUNK_SIZE))
                    if not chunk: break
                    _stream_write(handler, chunk)
                    remaining -= len(chunk)
                    if limited:
                        limiter.record_and_wait_with_rate(user_id, len(chunk), effective_rate)
        finally:
            if token_held:
                try: limiter.release_stream(user_id)
                except Exception: pass
                token_held = False
    except DISCONNECTED_EXCEPTIONS:
        # 断连/写侧无进展超时：标记关闭，资源（每用户流名额）已由 finally 释放
        try: handler.close_connection = True
        except Exception: pass
    except Exception as e:
        try: handler.send_error(500)
        except DISCONNECTED_EXCEPTIONS: pass


def send_thumbnail(handler, UPLOAD_DIR, get_thumbnail, is_path_safe, DISCONNECTED_EXCEPTIONS):
    """缩略图"""
    try:
        q = urllib.parse.urlparse(handler.path).query
        p = urllib.parse.parse_qs(q).get('path', [''])[0]
        if not p: handler.send_error(400); return
        # 规范化路径
        norm_p = _normalize_rel_path(p)
        if norm_p is None:
            handler.send_error(403); return
        p = norm_p
        if not handler._check_path_permission(p):
            handler.send_error(403); return
        full = os.path.join(UPLOAD_DIR, p)
        if not is_path_safe(UPLOAD_DIR, full): handler.send_error(403); return
        if not os.path.exists(full) or os.path.isdir(full): handler.send_error(404); return
        thumb = get_thumbnail(full)
        if not thumb or not os.path.exists(thumb): handler.send_error(404); return
        handler.send_response(200)
        handler.send_header('Content-Type', 'image/jpeg')
        handler.send_header('Content-Length', str(os.path.getsize(thumb)))
        handler.send_header('Cache-Control', 'max-age=3600')
        handler.send_header('X-Content-Type-Options', 'nosniff')   # C-11
        handler.end_headers()
        with open(thumb, 'rb') as f: _stream_write(handler, f.read())
    except DISCONNECTED_EXCEPTIONS:
        try: handler.close_connection = True
        except Exception: pass
    except Exception as e:
        try: handler.send_error(500)
        except DISCONNECTED_EXCEPTIONS: pass


def build_stats_data(get_cached_stats, get_folder_size,
                     has_ffmpeg, get_max_concurrent, COPY_BUFFER_SIZE,
                     get_speed_limit, get_connections, PORT,
                     get_guest_mode, get_default_user_quota, get_public_quota,
                     get_total_quota, UPLOAD_DIR):
    """构造服务器统计快照（供 HTTP /api/stats 与 WebSocket 管理推送共用）"""
    # 文件树聚合（大小/文件数/文件夹数/各路径占用）由 get_cached_stats
    # （= fs_core.get_server_stats，读取 folder 聚合缓存）一次取得，此处直接复用
    stats = get_cached_stats()
    try: sip = socket.gethostbyname(socket.gethostname())
    except Exception: sip = '127.0.0.1'
    users_used = stats.get('users_used', 0)
    public_used = stats.get('public_used', 0)
    total_used = stats.get('total_used', 0)
    # 检查 aria2c 下载组件是否可用
    has_aria2c = False
    try:
        from leaffs.downloader_core.dl_utils import has_aria2c as _has_aria2c
        has_aria2c = _has_aria2c()
    except Exception:
        pass
    # 服务器真实运行时长（秒）
    uptime = 0
    try:
        from leaffs.config_core.cfg_core import get_server_uptime as _get_uptime
        uptime = _get_uptime()
    except Exception:
        pass
    return {
        'file_count': stats.get('file_count', 0), 'folder_count': stats.get('folder_count', 0),
        'total_size': stats.get('total_size', 0),
        'thumb_count': stats.get('thumb_count', 0), 'thumb_size': stats.get('thumb_size', 0),
        'ffmpeg': has_ffmpeg(),
        'aria2c': has_aria2c,
        'uptime': uptime,
        'cache_max': 5, 'cache_ttl': 5,
        'concurrent_max': get_max_concurrent(), 'buffer_size': COPY_BUFFER_SIZE,
        'speed_limit': get_speed_limit(), 'blocked_count': 0,
        'active_connections': len(get_connections()),
        'server_ip': sip, 'server_port': PORT, 'guest_mode': get_guest_mode(),
        'user_quota': get_default_user_quota(), 'public_quota': get_public_quota(),
        'total_quota': get_total_quota(),
        'users_used': users_used, 'public_used': public_used, 'total_used': total_used
    }


def server_stats(handler, get_cached_stats, get_folder_size,
                 has_ffmpeg, get_max_concurrent, COPY_BUFFER_SIZE,
                 get_speed_limit, get_connections, PORT,
                 get_guest_mode, get_default_user_quota, get_public_quota,
                 get_total_quota, UPLOAD_DIR):
    """服务器统计"""
    if handler._get_effective_role() not in ('admin', 'super_admin'):
        handler.send_json({'error': 'Forbidden'}, 403); return
    try:
        handler.send_json(build_stats_data(
            get_cached_stats, get_folder_size, has_ffmpeg, get_max_concurrent,
            COPY_BUFFER_SIZE, get_speed_limit, get_connections, PORT,
            get_guest_mode, get_default_user_quota, get_public_quota,
            get_total_quota, UPLOAD_DIR))
    except Exception as e:
        handler.send_json({'error': str(e)}, 500)


def handle_upload(handler, handle_upload_fn, invalidate_folder_cache_smart, UPLOAD_DIR,
                  is_path_safe, _check_quota, DISCONNECTED_EXCEPTIONS):
    """文件上传

    C-01/R1：未登录一律拒写；guest 仅当 guest_public_write 开启且目标为 public[/子目录]
    时允许上传，且一律走 auto_unique（目标已存在自动改名、绝不覆盖），由 fs_core 承接。
    R2：空 path（共享根）上传仅 admin/super_admin。
    C-06/IC-QUOTA：_check_quota 通过后对在途字节 quota_reserve，请求结束/异常统一在
    finally 中 quota_settle，防止记账泄漏。
    """
    try:
        ct = handler.headers.get('Content-Type', '')
        cl = int(handler.headers.get('Content-Length', 0))
        if not ct.startswith('multipart/form-data'):
            handler.send_json({'error': '需要 multipart/form-data'}, 400); return
        # 服务端强制上传大小上限（读 body 前预检；multipart 头尾开销留 512KB 余量，
        # cfg 值 0 表示不限）
        try:
            from leaffs.config_core import cfg_core as _cc
            umax = _cc.get_upload_max_size()
        except Exception:
            umax = 0
        if umax and cl > umax + 512 * 1024:
            handler.send_json({'error': '上传超过大小上限'}, 413); return
        role = handler._get_effective_role()
        if not role:
            # C-01：未登录一律拒写（A-02 已在路由层拦 401，这里兜底）
            handler.send_json({'error': '未登录或会话已失效'}, 401); return
        q = urllib.parse.urlparse(handler.path).query
        sub_path = urllib.parse.parse_qs(q).get('path', [''])[0].strip()
        # 规范化上传路径
        norm_sub = _normalize_rel_path(sub_path)
        if sub_path and norm_sub is None:
            handler.send_json({'error': '路径不合法'}, 403); return
        if sub_path:
            sub_path = norm_sub
        # C-01/R1：guest 写收敛（guest 仅 public 下 upload；delete/overwrite/mkdir 与
        # 空 path/越界路径一律拒绝；覆盖由 fs_core auto_unique 自动改名承接）
        if not write_allowed(handler, sub_path, 'upload'):
            handler.send_json({'error': '无权限上传到此目录'}, 403); return
        if sub_path and not handler._check_path_permission(sub_path):
            handler.send_json({'error': '无权限上传到此目录'}, 403); return
        # R2：空 path（共享根）上传仅 admin/super_admin（user/guest → 403）
        if not sub_path and role not in ('admin', 'super_admin'):
            handler.send_json({'error': '无权限上传到根目录'}, 403); return
        target_dir = os.path.join(UPLOAD_DIR, sub_path) if sub_path else UPLOAD_DIR
        ok, err = _check_quota(target_dir, cl)
        if not ok:
            handler.send_json({'success': False, 'saved': 0, 'errors': [err]}, 413); return
        # C-06/IC-QUOTA：配额检查通过后预留“在途字节”；结束后（成功/异常均）结算。
        auto_unique = (role == 'guest')     # guest 已过 write_allowed → fs_core 自动改名
        buckets = _upload_quota_buckets(target_dir, UPLOAD_DIR)
        try:
            from leaffs.utils_core import ut_core as _utc
        except Exception:
            _utc = None
        if _utc is not None:
            for b in buckets:
                try: _utc.quota_reserve(b, cl)
                except Exception: pass
        saved = 0
        errors = []
        try:
            saved, errors = handle_upload_fn(handler.rfile, ct, cl, sub_path, auto_unique=auto_unique)
        finally:
            # 简化口径：成功(saved>0)按 cl 全额结算（实际字节累计由 fs_core 语义保证
            # 落盘一致），失败按 0 结算释放预留 —— 误差仅限被拒文件的虚占。
            if _utc is not None:
                _actual = cl if saved > 0 else 0
                for b in buckets:
                    try: _utc.quota_settle(b, cl, _actual)
                    except Exception: pass
        if saved > 0:
            try:
                invalidate_folder_cache_smart(target_dir)
            except Exception:
                pass
        # 收口：saved==0 表示本请求没有任何文件落盘（目录不存在/空请求/全部被拒等），
        # 必须如实返回 success:false，避免前端只看 success 误报“上传成功”。
        if saved == 0 and not errors:
            errors.append('未收到任何可保存的文件（请求为空或格式错误）')
        handler.send_json({'success': saved > 0, 'saved': saved, 'errors': errors})
    except DISCONNECTED_EXCEPTIONS:
        # 客户端中途断连（含读超时/写侧无进展超时，均 ⊂ OSError）：
        # 请求未完成即断开 ≠ 服务器 500 —— 置 close、不补 500 响应、访问日志不记 500；
        # .part/配额清理已由 fs_core 与上方 finally（quota_settle）保证。
        try: handler.close_connection = True
        except Exception: pass
    except Exception as e:
        try: handler.send_json({'error': str(e)}, 500)
        except DISCONNECTED_EXCEPTIONS: pass


def handle_delete(handler, UPLOAD_DIR, is_path_safe, _delete_thumb,
                  invalidate_file_cache, invalidate_folder_cache, DISCONNECTED_EXCEPTIONS):
    """删除文件/目录

    C-01/R1：guest/未登录一律禁删（403，含 guest_public_write 开启时）。
    C-07：逐项收集真实失败原因，响应新增 failed 字段（保留原 deleted 字段）。
    实测收口（删除语义）：区分“无权限被拒”与“目标不存在” —— 本次请求中存在
    因权限/路径逃逸被拒（skipped_permission>0）且最终 deleted==0 → 403
    {'error':'无权限删除'}；deleted>0 保持 200（响应附 skipped_permission 说明被拒数量）；
    文件本身不存在（可删但找不到）仍幂等 200 deleted:0。
    """
    try:
        length = int(handler.headers.get('Content-Length', 0))
        data = json.loads(handler.rfile.read(length).decode())
        paths = data.get('files', [])
        if isinstance(paths, str): paths = [paths]
        deleted = 0
        failed = []              # [(path, reason)] —— C-07 真实删除失败
        skipped_permission = 0   # 因路径权限/写策略/路径逃逸被拒（非目标不存在）的数量
        for path in paths:
            # 规范化路径
            norm_path = _normalize_rel_path(path)
            if norm_path is None:
                continue
            path = norm_path
            # C-01/R1：guest/未登录写收敛（403 终止整个请求；user/admin 由下权限判定把关）
            if not write_allowed(handler, path, 'delete'):
                handler.send_json({'error': '无权限删除'}, 403); return
            # 越权/无权限目录（登录用户删除无权目录内文件同样在此被拒并计数）
            if not handler._check_path_permission(path):
                skipped_permission += 1
                continue
            full = os.path.join(UPLOAD_DIR, path)
            if not is_path_safe(UPLOAD_DIR, full):
                skipped_permission += 1  # 路径逃逸/非法落点：按权限类拒绝计数
                continue
            # 目标不存在：可删但找不到 → 幂等（不计数、不报权限错误）
            if not os.path.exists(full): continue
            try:
                if os.path.isfile(full):
                    _delete_thumb(full)
                    os.remove(full)
                    invalidate_file_cache(full)
                    invalidate_folder_cache(os.path.dirname(full))
                else:
                    for root, _, files in os.walk(full):
                        for f in files:
                            _delete_thumb(os.path.join(root, f))
                    shutil.rmtree(full)
                    invalidate_folder_cache(full, recursive=True)
                deleted += 1
            except PermissionError:
                failed.append((path, '文件被占用或权限不足'))
            except Exception as e:
                failed.append((path, '删除失败: %s' % (e,)))
        # 实测收口：有路径因权限被拒且一条都没删成 → 403（而非 200 success:true）
        if deleted == 0 and skipped_permission > 0:
            handler.send_json({'error': '无权限删除'}, 403); return
        resp = {'success': True, 'deleted': deleted}
        if failed:
            resp['failed'] = [{'path': a, 'error': b} for a, b in failed]
        if skipped_permission:
            resp['skipped_permission'] = skipped_permission
        handler.send_json(resp)
    except DISCONNECTED_EXCEPTIONS:
        # 客户端中途断连：置 close、不补 500 响应、访问日志不记 500
        try: handler.close_connection = True
        except Exception: pass
    except Exception as e:
        try: handler.send_json({'error': str(e)}, 500)
        except DISCONNECTED_EXCEPTIONS: pass


def handle_mkdir(handler, UPLOAD_DIR, is_path_safe, invalidate_folder_cache, DISCONNECTED_EXCEPTIONS):
    """创建目录"""
    try:
        length = int(handler.headers.get('Content-Length', 0))
        data = json.loads(handler.rfile.read(length).decode())
        path = data.get('path', ''); name = data.get('name', '')
        if not name: handler.send_json({'error': '需要名称'}, 400); return
        # 名称净化：拒绝 / \\ : . .. 控制字符与超长
        from leaffs.file_system_core.fs_core import sanitize_entry_name
        if sanitize_entry_name(name) is None:
            handler.send_json({'error': '名称不合法'}, 400); return
        # 规范化路径
        norm_path = _normalize_rel_path(path + '/' if path else '')
        if norm_path is None:
            handler.send_json({'error': '路径不合法'}, 403); return
        path = norm_path.rstrip('/') if norm_path else ''
        # C-01/R1：guest 一律禁建目录（含 guest_public_write 开启时）；未登录一律拒写
        if not write_allowed(handler, path + '/' if path else '', 'mkdir'):
            handler.send_json({'error': '无权限在此目录创建文件夹'}, 403); return
        if not handler._check_path_permission(path + '/' if path else ''):
            handler.send_json({'error': '无权限在此目录创建文件夹'}, 403); return
        full = os.path.join(UPLOAD_DIR, path, name)
        if not is_path_safe(UPLOAD_DIR, full): handler.send_json({'error': '权限错误'}, 403); return
        os.makedirs(full, exist_ok=True)
        invalidate_folder_cache(os.path.dirname(full))
        handler.send_json({'success': True})
    except DISCONNECTED_EXCEPTIONS:
        # 客户端中途断连：置 close、不补 500 响应、访问日志不记 500
        try: handler.close_connection = True
        except Exception: pass
    except Exception as e:
        try: handler.send_json({'error': str(e)}, 500)
        except DISCONNECTED_EXCEPTIONS: pass


def search_files(handler, UPLOAD_DIR):
    """搜索文件（C-04：每 IP 令牌桶限流 6/60s，超限 429；q 超长 400；结果截断 500 条）"""
    try:
        # C-04：每 IP 令牌桶限流（容量 6、补速 6/60s）；无客户端地址（非常规环境/测试桩）不限
        ip = handler.client_address[0] if handler.client_address else ''
        if ip and not _search_take_token(ip):
            handler.send_json({'error': '搜索过于频繁，请稍后再试'}, 429); return
        q = urllib.parse.urlparse(handler.path).query
        params = urllib.parse.parse_qs(q)
        base_path = params.get('base', [''])[0]
        role = handler._get_effective_role()
        if not base_path:
            if role in ('super_admin', 'admin'):
                base_path = ''
            else:
                username = handler._get_username_from_session()
                base_path = f'users/{username}' if username else 'public'
        if not role:
            handler.send_json({'files': []}); return
        # 规范化搜索基路径
        norm_base = _normalize_rel_path(base_path)
        if norm_base is None:
            handler.send_json({'error': '路径不合法'}, 403); return
        base_path = norm_base
        if not handler._check_path_permission(base_path):
            handler.send_json({'error': '无权限'}, 403); return
        query = params.get('q', [''])[0].lower().strip()
        if not query: handler.send_json({'files': []}); return
        if len(query) > _SEARCH_MAX_Q:
            # C-04：查询串超长 → 400（不触发递归扫描）
            handler.send_json({'error': f'查询过长（最多 {_SEARCH_MAX_Q} 字符）'}, 400); return
        results = []
        search_root = os.path.join(UPLOAD_DIR, base_path) if base_path else UPLOAD_DIR
        if not os.path.exists(search_root):
            handler.send_json({'files': [], 'query': query}); return
        truncated = False
        for root, dirs, files in os.walk(search_root):
            for fname in files:
                if query in fname.lower():
                    full = os.path.join(root, fname)
                    rel = os.path.relpath(full, UPLOAD_DIR).replace('\\', '/')
                    try:
                        st = os.stat(full)
                        results.append({'name': fname, 'path': rel, 'type': 'file', 'size': st.st_size, 'mtime': st.st_mtime})
                    except Exception:
                        pass
                    # C-04：结果截断 500 条并标注 truncated
                    if len(results) >= _SEARCH_MAX_RESULTS:
                        truncated = True
                        break
            if truncated:
                break
        results.sort(key=lambda x: x['name'].lower())
        handler.send_json({'files': results, 'query': query, 'truncated': truncated})
    except Exception as e:
        handler.send_json({'error': str(e)}, 500)


def _normalize_rel_path(rel_path):
    """规范化相对路径并禁止路径穿越"""
    if not rel_path:
        return ''
    # 先检查原始路径中是否包含 ..（必须在 normpath 之前检查，否则 normpath 会先解析掉 ..）
    raw_parts = rel_path.replace('\\', '/').split('/')
    if '..' in raw_parts:
        return None
    if raw_parts and raw_parts[0] in ('..', '.'):
        return None
    # 规范化路径：移除多余的 . 和 /
    norm = os.path.normpath(rel_path).replace('\\', '/')
    # 禁止绝对路径
    if norm.startswith('/'):
        return None
    # 标准化后再次检查，防止 normpath 改变相对结构
    if norm in ('..', '../') or norm.startswith('../'):
        return None
    return norm


class _ZipStreamWriter:
    """把 zipfile 的压缩输出直接转发到 HTTP 响应流（带写侧无进展超时）。

    该对象刻意不可 seek —— zipfile 检测到不可 seek 的输出会自动改用 data
    descriptor（local header 标志位 bit 3），并在 close() 时顺序追加中央目录，
    从而实现对网络流的“边压边发”，无需任何临时文件。
    """

    def __init__(self, handler):
        self._handler = handler

    def write(self, data):
        if data:
            _stream_write(self._handler, data)
        return len(data)

    def flush(self):
        try:
            self._handler.wfile.flush()
        except Exception:
            pass


def zip_download(handler, UPLOAD_DIR, COPY_BUFFER_SIZE, is_path_safe, DISCONNECTED_EXCEPTIONS):
    """ZIP 打包下载（双模式，配置键 zip_streaming 切换，默认全流式）。

    全流式（默认）：边压边发、零临时磁盘，见 _ZipStreamWriter。代价：没有
    Content-Length（HTTP/1.0 以连接关闭定界），下载器不显示总大小/进度。
    非流式（zip_streaming=false）：先打包到 SpooledTemporaryFile（≤8MB 内存、
    更大自动转磁盘临时文件）再发送，有 Content-Length/进度条；但大包会占用临时
    磁盘（可能写满导致传输失败）且首字节更慢 —— 开关旁文案已就此显著警示。
    """
    try:
        q = urllib.parse.urlparse(handler.path).query
        if len(q) > 8192:
            handler.send_error(400, 'URL过长'); return
        params = urllib.parse.parse_qs(q)
        files_raw = params.get('files', ['[]'])[0]
        if len(files_raw) > 4096:
            handler.send_json({'error': '文件列表参数过长'}, 400); return
        file_list = json.loads(files_raw)
        base_path = params.get('base', [''])[0]
        if not file_list or not isinstance(file_list, list):
            handler.send_error(400); return

        # 规范化并校验 base_path
        base_path = _normalize_rel_path(base_path)
        if base_path is None:
            handler.send_json({'error': '路径不合法'}, 403); return
        if base_path and not handler._check_path_permission(base_path):
            handler.send_json({'error': '无权限'}, 403); return
        if base_path:
            base_full = os.path.join(UPLOAD_DIR, base_path)
            if not is_path_safe(UPLOAD_DIR, base_full):
                handler.send_json({'error': '路径不安全'}, 403); return
        else:
            base_full = UPLOAD_DIR

        # 先收集所有要打包的文件，再按可配置上限（0=不限）校验
        try:
            from leaffs.config_core import cfg_core as _cc
            zip_limit = int(_cc.get_zip_max_files())
        except Exception:
            zip_limit = 500
        entries = []
        for rel_path in file_list:
            # 规范化并校验每个文件的路径
            rel_path = _normalize_rel_path(rel_path)
            if rel_path is None:
                continue
            # 对每个文件单独进行权限检查
            if not handler._check_path_permission(rel_path):
                continue
            full = os.path.join(UPLOAD_DIR, rel_path)
            if not is_path_safe(UPLOAD_DIR, full): continue
            if not os.path.exists(full): continue
            arcname = os.path.relpath(full, base_full)
            if os.path.isfile(full):
                entries.append((full, arcname))
            elif os.path.isdir(full):
                for root, dirs, files in os.walk(full):
                    for file in files:
                        fp = os.path.join(root, file)
                        # 对子文件也校验路径安全性
                        if not is_path_safe(UPLOAD_DIR, fp): continue
                        entries.append((fp, os.path.relpath(fp, base_full)))
        if zip_limit and len(entries) > zip_limit:
            handler.send_json({'error': f'文件数量过多（上限{zip_limit}个，可在高级配置调整）'}, 400)
            return
        # 模式判定：zip_streaming 默认 True（全流式）；异常时按全流式兜底
        try:
            from leaffs.config_core import cfg_core as _cc
            streaming = bool(_cc.get_zip_streaming())
        except Exception:
            streaming = True
        disp = f'attachment; filename="files_{int(time.time())}.zip"'
        if streaming:
            # ===== 全流式：边压边发、零临时磁盘；HTTP/1.0 以关闭连接定界（无 Content-Length）=====
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/zip')
            handler.send_header('Content-Disposition', disp)
            handler.send_header('X-Content-Type-Options', 'nosniff')
            handler.end_headers()
            writer = _ZipStreamWriter(handler)
            try:
                zf = zipfile.ZipFile(writer, 'w', zipfile.ZIP_DEFLATED)
                try:
                    for full, arcname in entries:
                        zf.write(full, arcname)
                finally:
                    try:
                        zf.close()   # 顺序追加中央目录 + EOCD，产物为完整 zip
                    except DISCONNECTED_EXCEPTIONS:
                        raise
                    except Exception:
                        pass
            except DISCONNECTED_EXCEPTIONS:
                raise
            except Exception:
                # 响应头已发出、无法再回错误状态码：按断连收场（客户端表现为下载失败）
                try:
                    handler.close_connection = True
                except Exception:
                    pass
            return
        # ===== 非流式：先打包到 spool（≤8MB 常驻内存、更大自动转磁盘临时文件），
        # 拿到 Content-Length 再发送。稳定有进度，但大包占临时磁盘、首字节更慢。 =====
        spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
        try:
            with zipfile.ZipFile(spool, 'w', zipfile.ZIP_DEFLATED) as zf:
                for full, arcname in entries:
                    zf.write(full, arcname)
            zs = spool.tell()
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/zip')
            handler.send_header('Content-Disposition', disp)
            handler.send_header('Content-Length', str(zs))
            handler.send_header('X-Content-Type-Options', 'nosniff')
            handler.end_headers()
            spool.seek(0)
            while True:
                chunk = spool.read(COPY_BUFFER_SIZE)
                if not chunk:
                    break
                # 正文流式写出带 120s 无进展超时；R4 后本端点不占任何全局并发槽
                _stream_write(handler, chunk)
        finally:
            try:
                spool.close()
            except Exception:
                pass
    except DISCONNECTED_EXCEPTIONS:
        # 断连/写侧无进展超时：spool 已由 finally 关闭，无临时资源泄漏
        try: handler.close_connection = True
        except Exception: pass
    except Exception as e:
        try: handler.send_error(500)
        except DISCONNECTED_EXCEPTIONS: pass