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

from leaffs.runtime_log import log_exception as _log_exc
# LF-22：上传临时目录对用户不可见 —— 判定收在 leaffs.paths 里（说明见那里），
# 列表/搜索/下载/缩略图/大小统计统一调它，免得各处各写一份条件而漏掉某一处。
from leaffs.paths import is_upload_tmp_entry, is_upload_tmp_relpath
# LF-23：删除失败原因的精确翻译（"被占用"与"权限不足"必须分开说，处置方式不同）
from leaffs.utils.core import delete_fail_reason, has_windows_device_name
# §二 第 4 条：上传读流期间"实时校验发现超额"的异常。定义在 `leaffs.utils.core` 的配额节
# —— 因为 `files/api.py` 与 `files/core.py` 是"注入解耦"关系（前者不 import 后者），
# 两边要用**同一个类型**，只能放在都能依赖的地方。
from leaffs.utils.core import UploadQuotaExceeded


# ========== C-01/R1：guest 写权限策略（HTTP 版 + WS 版，IC-WRITE） ==========
# guest 规则（R1 定稿）：guest_public_write=True 时 guest 仅可在 public[/子目录] 下
# “新建”上传；目标已存在视作 overwrite → 由 fs_core.handle_upload(auto_unique=True)
# 自动改名承接（绝不覆盖）；guest 的 delete/mkdir 与未登录一律拒写；空 path(共享根) 403。

def _guest_write_flag():
    """guest 写 public 开关（cfg_core.get_guest_public_write）。

    **不吞异常**：取不到开关时不能当成"允许 guest 写" —— 那是把"读配置失败"
    读成"用户放开了写权限"（旧写法 except → True）。真出错就让这次请求失败。
    """
    from leaffs.config import core as _cc
    return bool(_cc.get_guest_public_write())


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


def build_listing(rel_path, perm_check, list_files, share_filter=None):
    """构建目录列表 —— HTTP 与 WS 的**唯一**路径（只"算"，不"发"）。

    返回 (result, err_kind, detail)：
      result   —— 成功时的列表 dict；失败为 None
      err_kind —— None / 'bad_path' / 'denied' / 'io' / 'unknown'
      detail   —— 'io' 时的底层文案（HTTP 原来就是把它回给客户端的）

    顺序（两边必须一致，别再各写一份 —— LF-27 就是两份实现漂移出来的）：
      ① 规范化（路径穿越 / 绝对路径 / 盘符）→ 不合法 = 'bad_path'
      ② LF-22：`.uploads` **点名进入也不给看** → 'bad_path'
         （`list_files` 自己只是"不列出它"，挡不住直接走进去）
      ③ 权限判定 → 不过 = 'denied'
      ④ 扫描磁盘 → 读不了 = 'io'；返回 None 又没原因 = 'unknown'
      ⑤ 合并虚拟分享映射（分享码没解锁的 owner 由 share_filter 挡掉）

    调用方各自把 err_kind 翻成自己的响应形状（HTTP 状态码 / WS 消息），
    所以这里**只算不发** —— 两边的错误语义本来就不同，硬统一会互相牵制。
    """
    norm_p = _normalize_rel_path(rel_path)
    if norm_p is None:
        return None, 'bad_path', None
    p = norm_p
    if is_upload_tmp_relpath(p):
        return None, 'bad_path', None
    try:
        allowed = bool(perm_check(p))
    except Exception as e:
        # 判定不了 = 拒绝（绝不能读成"有权限"）；但要留痕，不能静默当成无权
        _log_exc('列表路径权限判定', e)
        allowed = False
    if not allowed:
        return None, 'denied', None
    result, err = list_files(p)
    if err:
        return None, 'io', err
    if result is None:
        return None, 'unknown', None
    # 分享虚拟映射：把 public 树下的映射条目合成进列表（磁盘零副本）。
    # 分享码没解锁的 owner 由 share_filter 挡掉，列表里不会出现它的任何信息。
    # **不吞异常**：合并失败说明映射读不出来，必须留痕 —— 旧写法 `except: pass`
    # 会静默少一个目录（与 TH1"失败不能静默"同一口径）。
    try:
        from leaffs.share import mappings as _mp
        _mp.merge_into_list(p, result, unlocked=share_filter)
    except Exception as e:
        _log_exc('合并分享虚拟条目', e)
    return result, None, None


def send_files(handler, list_files, share_filter=None):
    """文件列表

    share_filter：可选的 (owner) -> bool，判断分享码是否已解锁该 owner。
    未解锁的 owner，其虚拟条目不会合并进列表 —— 连文件夹本身都不出现，
    文件名/大小/时间一个都不发（传 None 表示不过滤）。
    列表**怎么算**是 `build_listing` 的事（HTTP 与 WS 共用），这里只负责发 HTTP 响应。
    """
    try:
        q = urllib.parse.urlparse(handler.path).query
        p = urllib.parse.parse_qs(q).get('path', [''])[0]
        result, err_kind, detail = build_listing(
            p, handler._check_path_permission, list_files, share_filter)
        if err_kind is None:
            handler.send_json(result)
        elif err_kind == 'bad_path':
            handler.send_json({'error': '路径不合法'}, 403)
        elif err_kind == 'denied':
            handler.send_json({'error': '无权限访问此目录'}, 403)
        elif err_kind == 'io':
            handler.send_json({'error': detail}, 403)
        else:
            handler.send_json({'error': '未知错误'}, 500)
    except Exception as e:
        _log_exc('读取文件列表', e)
        handler.send_json({'error': '读取文件列表失败'}, 500)


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
        # LF-22：上传临时目录里的东西一律不对外（这条是预览/原始内容）
        if is_upload_tmp_relpath(p):
            handler.send_error(404); return
        if not handler._check_path_permission(p):
            handler.send_error(403); return
        full = os.path.join(UPLOAD_DIR, p)
        if not is_path_safe(UPLOAD_DIR, full): handler.send_error(403); return
        if not os.path.exists(full) or os.path.isdir(full): handler.send_error(404); return
        if os.path.getsize(full) > PREVIEW_MAX_SIZE:
            # HTTP/1.1 预备（2026-09-18）：正文必须有定界
            _oversize = '[文件过大]'.encode('utf-8')
            handler.send_response(413)
            handler.send_header('Content-Length', str(len(_oversize)))
            handler.end_headers()
            handler.wfile.write(_oversize); return
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
        # HTTP/1.1 预备（2026-09-18）：正文必须有定界 —— 文件大小是现成的
        # （上面刚做过存在性检查），发出去客户端才能知道正文到哪儿结束。
        handler.send_header('Content-Length', str(os.path.getsize(full)))
        # 同源内容隔离：sandbox 阻止脚本执行/表单提交等，且不信任任何同源资源
        # （外部图片等也仅允许 data: 内嵌）；配合 nosniff 防止 MIME 嗅探。
        # 公共头走统一入口（这处特有的 sandbox CSP 一并交给它）；手写会跟自动补的重复
        handler._common_security_headers(
            csp="sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:")
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
        from leaffs.config import core as _cc
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
        # LF-22：上传临时目录是服务端内部目录，任何角色（含 admin）都不该通过 URL 拿到它。
        # 回 404 而非 403：这里的语义是"没有这个资源"，与权限无关。
        if is_upload_tmp_relpath(name):
            handler.send_error(404); return
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
            # HTTP/1.1 预备（2026-09-18）：416 允许带正文，必须显式声明空正文
            handler.send_header('Content-Length', '0')
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
        _, sid = get_session(cookie, handler.client_address[0])
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
            # C-11 的 nosniff 由 handler.end_headers 统一补，不手写（会重复）
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


def send_thumbnail(handler, UPLOAD_DIR, get_thumbnail, is_path_safe, DISCONNECTED_EXCEPTIONS,
                   resolve_share=None):
    """缩略图

    resolve_share：可选的「分享虚拟路径 → 源文件相对路径」解析器。
    分享映射出来的 public/shares/<用户名>/<文件名> 磁盘上并不存在（源文件原位不动），
    所以取图前要先换成源文件路径，缩略图缓存也就与源文件共用同一份。
    解析全程在服务端内部：既不写进响应，也不写进日志（日志记的是原始 query）。
    """
    try:
        q = urllib.parse.urlparse(handler.path).query
        p = urllib.parse.parse_qs(q).get('path', [''])[0]
        if not p: handler.send_error(400); return
        # 规范化路径
        norm_p = _normalize_rel_path(p)
        if norm_p is None:
            handler.send_error(403); return
        p = norm_p
        # LF-22：不给上传临时文件生成缩略图（写到一半的数据，也不该被用户看见）
        if is_upload_tmp_relpath(p):
            handler.send_error(404); return
        if not handler._check_path_permission(p):
            handler.send_error(403); return
        if resolve_share is not None:
            try:
                real = resolve_share(p)
            except Exception:
                real = None
            # 约定：解析器返回源文件的**绝对路径**（见 share/mappings.resolve）
            if real:
                full = real
            else:
                full = os.path.join(UPLOAD_DIR, p)
        else:
            full = os.path.join(UPLOAD_DIR, p)
        if not is_path_safe(UPLOAD_DIR, full): handler.send_error(403); return
        if not os.path.exists(full) or os.path.isdir(full): handler.send_error(404); return
        thumb = get_thumbnail(full)
        if not thumb or not os.path.exists(thumb): handler.send_error(404); return
        handler.send_response(200)
        handler.send_header('Content-Type', 'image/jpeg')
        handler.send_header('Content-Length', str(os.path.getsize(thumb)))
        handler.send_header('Cache-Control', 'max-age=3600')
        # nosniff 由 handler.end_headers 统一补；这行的 Cache-Control 是缩略图自己的缓存策略
        handler.end_headers()
        with open(thumb, 'rb') as f: _stream_write(handler, f.read())
    except DISCONNECTED_EXCEPTIONS:
        try: handler.close_connection = True
        except Exception: pass
    except Exception as e:
        try: handler.send_error(500)
        except DISCONNECTED_EXCEPTIONS: pass


def build_stats_data(get_cached_stats, get_folder_size,
                     has_ffmpeg, thumbnail_backend, get_max_concurrent, COPY_BUFFER_SIZE,
                     get_speed_limit, get_connections, PORT,
                     get_guest_mode, get_default_user_quota, get_public_quota,
                     get_total_quota, UPLOAD_DIR):
    """构造服务器统计快照（供 HTTP /api/stats 与 WebSocket 管理推送共用）"""
    # 文件树聚合（大小/文件数/文件夹数/各路径占用）由 get_cached_stats
    # （= fs_core.get_server_stats，读取 folder 聚合缓存）一次取得，此处直接复用
    stats = get_cached_stats()
    # 对外展示地址（管理页「本机 IP」、访问二维码）统一由 hosts 算：
    # 桌面走 primary_lan_ip()，安卓由 leaffs_mobile 注册的提供者给（认 Wi-Fi/热点、
    # 排除蜂窝）。旧写法 gethostbyname(gethostname()) 在安卓上必然解析失败
    # （主机名是机型名）→ 恒得 127.0.0.1。
    try:
        from leaffs.server.hosts import lan_status as _lan_status
        sip, has_lan = _lan_status()
    except Exception:
        sip, has_lan = '127.0.0.1', False
    users_used = stats.get('users_used', 0)
    public_used = stats.get('public_used', 0)
    total_used = stats.get('total_used', 0)
    # 检查 aria2c 下载组件是否可用
    has_aria2c = False
    try:
        from leaffs.dl.dl_utils import has_aria2c as _has_aria2c
        has_aria2c = _has_aria2c()
    except Exception:
        pass
    # 服务器真实运行时长（秒）
    uptime = 0
    try:
        from leaffs.config.core import get_server_uptime as _get_uptime
        uptime = _get_uptime()
    except Exception:
        pass
    return {
        'file_count': stats.get('file_count', 0), 'folder_count': stats.get('folder_count', 0),
        'total_size': stats.get('total_size', 0),
        'thumb_count': stats.get('thumb_count', 0), 'thumb_size': stats.get('thumb_size', 0),
        'ffmpeg': has_ffmpeg(),
        'thumb_backend': thumbnail_backend(),
        'aria2c': has_aria2c,
        'uptime': uptime,
        'cache_max': 5, 'cache_ttl': 5,
        'concurrent_max': get_max_concurrent(), 'buffer_size': COPY_BUFFER_SIZE,
        'speed_limit': get_speed_limit(), 'blocked_count': 0,
        'active_connections': len(get_connections()),
        'server_ip': sip, 'server_port': PORT, 'guest_mode': get_guest_mode(),
        'server_has_lan': has_lan,
        'user_quota': get_default_user_quota(), 'public_quota': get_public_quota(),
        'total_quota': get_total_quota(),
        'users_used': users_used, 'public_used': public_used, 'total_used': total_used
    }


def server_stats(handler, get_cached_stats, get_folder_size,
                 has_ffmpeg, thumbnail_backend, get_max_concurrent, COPY_BUFFER_SIZE,
                 get_speed_limit, get_connections, PORT,
                 get_guest_mode, get_default_user_quota, get_public_quota,
                 get_total_quota, UPLOAD_DIR):
    """服务器统计"""
    if handler._get_effective_role() not in ('admin', 'super_admin'):
        handler.send_json({'error': 'Forbidden'}, 403); return
    try:
        handler.send_json(build_stats_data(
            get_cached_stats, get_folder_size, has_ffmpeg, thumbnail_backend,
            get_max_concurrent,
            COPY_BUFFER_SIZE, get_speed_limit, get_connections, PORT,
            get_guest_mode, get_default_user_quota, get_public_quota,
            get_total_quota, UPLOAD_DIR))
    except Exception as e:
        _log_exc('读取统计信息', e)
        handler.send_json({'error': '读取统计信息失败'}, 500)


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
        # cfg 值 0 表示不限）。**不吞异常**：0 是合法配置值（＝用户要求不限），
        # 所以取值失败绝不能退化成 0 —— 那等于把"读配置出错"读成"用户要求不限"。
        from leaffs.config import core as _cc
        umax = _cc.get_upload_max_size()
        if umax and cl > umax + 512 * 1024:
            handler.send_json({'error': '上传超过大小上限'}, 413); return
        role = handler._get_effective_role()
        if not role:
            # C-01：未登录一律拒写（A-02 已在路由层拦 401，这里兜底）
            handler.send_json({'error': '未登录或会话已失效'}, 401); return
        q = urllib.parse.urlparse(handler.path).query
        # 注意：`?path=`（空值）与"没有 path 参数"的区分在 **handler.upload()** 里做
        # （那里是 HTTP 契约的第一站，缺参数直接 400，见 LF-19）；本层只负责解析路径本身。
        sub_path = urllib.parse.parse_qs(q, keep_blank_values=True).get('path', [''])[0].strip()
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
        # ① 读 body **之前**的预检：用客户端声明的 cl 看一眼，省得白收 —— 但它**不记账**。
        #    声明的值只用来"提前拒绝"，所以虚报只会把攻击者自己拒掉，占不到别人的额度。
        ok, err = _check_quota(target_dir, cl)
        if not ok:
            handler.send_json({'success': False, 'saved': 0, 'errors': [err]}, 413); return
        # ② 记账按**实际读入**的字节（§二 第 4 条，2026-09-16）：
        #    原来这里是 `quota_reserve(b, cl)` —— 用客户端**声明的** Content-Length 预留，
        #    于是"声明一个大值、正文不发完"就能在请求存活期间把配额**虚拟占满**，
        #    期间别人的上传全部 413（实测复现：探针 `.cache/probe_quota_reserve.py`，
        #    一条只发 header 的连接即可瘫痪上传，断开后才恢复）。
        #    现在预留只反映**真实收到**的字节：不把数据发上来，就一个字节也占不到。
        auto_unique = (role == 'guest')     # guest 已过 write_allowed → fs_core 自动改名
        buckets = _upload_quota_buckets(target_dir, UPLOAD_DIR)
        try:
            from leaffs.utils import core as _utc
        except Exception:
            _utc = None
        _reserved = [0]          # 已记入"在途"的字节（finally 用它结算）
        _unbilled = [0]          # 还没到记账步长的字节
        _quota_hit = [False]     # 是否因超配额被中断（决定响应码 413）
        _BILL_STEP = 1024 * 1024     # 每 1 MiB 记一次账 + 校验一次（省锁，也省 get_folder_size）

        def _on_bytes(n):
            """上传解析层每从 socket 读入 n 字节回调一次（§二 第 4 条）。

            攒够 `_BILL_STEP` 才真正记账 + 实时校验：
              · 预留因此**永远等于实际读入量** → 虚占不可能；
              · **附加项**：并发把配额挤爆时，在**读到一半**就抛异常终止整个上传，
                而不是把剩下的 body 白收完再判定（那半截 .part 按 LF-24 语义丢弃、不落位）。
            """
            _unbilled[0] += n
            if _unbilled[0] < _BILL_STEP:
                return
            amount = _unbilled[0]
            _unbilled[0] = 0
            _reserved[0] += amount
            if _utc is not None:
                for b in buckets:
                    try: _utc.quota_reserve(b, amount)
                    except Exception: pass
            # new_size=0：这次上传**已经读进来**的量此刻就在在途账里了，
            # 所以「磁盘占用 + 在途 > 配额」只可能是（自己或并发的别人）把它撑爆了。
            ok2, err2 = _check_quota(target_dir, 0)
            if not ok2:
                _quota_hit[0] = True
                raise UploadQuotaExceeded(err2)

        saved = 0
        errors = []
        try:
            saved, errors = handle_upload_fn(handler.rfile, ct, cl, sub_path,
                                             auto_unique=auto_unique, on_bytes=_on_bytes)
        except UploadQuotaExceeded as e:
            # 兜底：回调在"单文件处理块之外"被触发时（找 boundary / 读 part 头期间），
            # 异常会直接落到这里（块内那条由 files/core 自己接住并带上文件名）。
            saved = 0
            errors = ['超过可用配额：%s' % (str(e) or '空间不足')]
        finally:
            # LF-21：先让磁盘统计反映刚落盘的文件，再释放预留 —— 顺序不能反，
            # 否则会出现"预留已归零、缓存还是旧值"的低估窗口。
            if saved > 0:
                try: invalidate_folder_cache_smart(target_dir)
                except Exception: pass
            # LF-21：预留只在**请求进行期间**有意义。它存在的理由是防 TOCTOU ——
            # 配额检查发生在读 body **之前**，那时磁盘上还没有这些字节，并发请求会
            # 各自以为额度够（磁盘统计只能看见"已经写下去的"）。
            # 请求一结束就该释放：成功时那些字节已经真的落盘、磁盘统计从此看得见；
            # 失败时它们根本没写。**原来成功时按 cl 全额保留**，于是 pending 从
            # "在途账"变成"历史累积账"，而 _check_quota 又把它与磁盘占用相加 ——
            # 同一批字节算两遍，攒过配额后每次上传都在预检被 413，且不会自愈
            # （实测：大文件成功后紧接着连续 413；删掉文件也不恢复，只有重启才清零）。
            # §二 第 4 条之后，这里结算的是**_reserved（真实记账过的量）**，不再是 cl。
            if _utc is not None and _reserved[0]:
                for b in buckets:
                    try: _utc.quota_settle(b, _reserved[0], 0)
                    except Exception: pass
        # 收口：saved==0 表示本请求没有任何文件落盘（目录不存在/空请求/全部被拒等），
        # 必须如实返回 success:false，避免前端只看 success 误报“上传成功”。
        if saved == 0 and not errors:
            errors.append('未收到任何可保存的文件（请求为空或格式错误）')
        # 超配额中断（附加项）如实回 413，与读前预检同一口径
        handler.send_json({'success': saved > 0, 'saved': saved, 'errors': errors},
                          413 if _quota_hit[0] else 200)
    except DISCONNECTED_EXCEPTIONS:
        # 客户端中途断连（含读超时/写侧无进展超时，均 ⊂ OSError）：
        # 请求未完成即断开 ≠ 服务器 500 —— 置 close、不补 500 响应、访问日志不记 500；
        # .part/配额清理已由 fs_core 与上方 finally（quota_settle）保证。
        try: handler.close_connection = True
        except Exception: pass
    except Exception as e:
        _log_exc('上传处理', e)
        try: handler.send_json({'error': '上传处理失败'}, 500)
        except DISCONNECTED_EXCEPTIONS: pass


def log_delete_audit(who, ip, deleted, targets, failed):
    """删除操作的审计日志：**谁**、删了几个、大概是哪些、失败几个。

    HTTP 与 WS 两条删除路径共用（口径必须一致，否则"从哪条路删的"就查不出差别）。

    ⚠️ 路径**限量**（前 3 个 + 总数）：一次删几千个文件时逐条写会把日志刷爆，
    而审计要的是"能定位到是谁干的、大概删了什么"，不是完整清单。
    完整清单在 `.deleted` 归档里也找得回来（如果那是删用户的话）。
    """
    try:
        from leaffs.runtime_log import add_log
        items = [str(t) for t in list(targets or ())]
        shown = ', '.join(items[:3])
        more = '' if len(items) <= 3 else ' 等共 %d 个' % len(items)
        tail = '' if not failed else '，失败 %d 个' % len(failed)
        add_log('删除: %d 个（%s%s）[%s ip=%s]%s'
                % (deleted, shown, more, who or '-', ip or '-', tail), 'warn')
    except Exception:
        pass          # 审计失败不该影响删除本身（与 runtime_log 同一处理）


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
            if not isinstance(path, str):
                # 畸形元素（数字/对象/数组）：与"路径不合法"同一口径拒绝。
                # 原来会一路传到 `_normalize_rel_path` 的 `.replace` 抛 AttributeError → 500。
                skipped_permission += 1
                continue
            path = path.strip()
            # 规范化路径
            norm_path = _normalize_rel_path(path)
            if norm_path is None:
                # LF-01：路径不合法（`./`、`../`、绝对路径、盘符…）原来直接 continue，
                # 把**整段判定**一起跳过了 —— 匿名发 {"files": ["./x"]} 拿到 200 success，
                # 而发 {"files": ["x"]} 是 403。计入 skipped_permission：与下面 is_path_safe
                # 失败同一口径（路径逃逸类拒绝），最终 deleted==0 时就会如实回 403。
                skipped_permission += 1
                continue
            if not norm_path:
                # LF-18：**空路径 = 共享根本身**（`os.path.join(UPLOAD_DIR, '')` 就是 UPLOAD_DIR）。
                # 删除的目标是"路径本身"（不像 upload/mkdir 那样是父目录），所以空串等于"删根" ——
                # 实测曾把整个 shared_files 递归删光、还回 `200 deleted:1`。
                # 按路径逃逸类拒绝计数：deleted==0 时如实回 404，绝不执行删除。
                skipped_permission += 1
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
            except PermissionError as e:
                # LF-23：精确区分"被占用"与"权限不足" —— 原来混成一句
                failed.append((path, delete_fail_reason(e)))
            except Exception as e:
                failed.append((path, delete_fail_reason(e)))
        # 实测收口：有路径因权限被拒且一条都没删成 → 403（而非 200 success:true）
        if deleted == 0 and skipped_permission > 0:
            handler.send_json({'error': '无权限删除'}, 403); return
        # LF-23：success 必须说真话 —— 有失败项就是失败。原来恒为 true，而前端只看这一个字段，
        # 于是"没删掉"和"删掉了"在界面上长得一模一样（真实原因塞在 failed 里没人读）。
        # **注意**：目标不存在（deleted=0 且 failed 为空）仍算成功 —— 那是幂等设计，别一起改掉。
        resp = {'success': not failed, 'deleted': deleted}
        if failed:
            resp['failed'] = [{'path': a, 'error': b} for a, b in failed]
        if skipped_permission:
            resp['skipped_permission'] = skipped_permission
        # 审计：只进访问日志的话，看得出"谁调了 /api/delete"，看不出**删了哪些**。
        if deleted:
            log_delete_audit(handler._actor(), handler.client_address[0], deleted, paths, failed)
        handler.send_json(resp)
    except DISCONNECTED_EXCEPTIONS:
        # 客户端中途断连：置 close、不补 500 响应、访问日志不记 500
        try: handler.close_connection = True
        except Exception: pass
    except Exception as e:
        _log_exc('删除', e)
        try: handler.send_json({'error': '删除失败'}, 500)
        except DISCONNECTED_EXCEPTIONS: pass


def handle_mkdir(handler, UPLOAD_DIR, is_path_safe, invalidate_folder_cache, DISCONNECTED_EXCEPTIONS):
    """创建目录"""
    try:
        length = int(handler.headers.get('Content-Length', 0))
        data = json.loads(handler.rfile.read(length).decode())
        path = data.get('path', ''); name = data.get('name', '')
        if not name: handler.send_json({'error': '需要名称'}, 400); return
        # 名称净化：拒绝 / \\ : . .. 控制字符与超长
        from leaffs.files.core import sanitize_entry_name
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
        _log_exc('创建文件夹', e)
        try: handler.send_json({'error': '创建文件夹失败'}, 500)
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
            # 不能按"用户名空不空"推基路径：游客会话名就是 `游客`（非空，见
            # auth/login_api 的 create_session('游客','guest')），推出来是
            # `users/游客` —— 而 check_path_permission 对 guest 只认 public，恒 403。
            if role in ('super_admin', 'admin'):
                base_path = ''                      # 管理员：默认全站
            elif role == 'guest':
                base_path = 'public'                # 游客：只有公共目录
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
        # 搜索根必须真的落在共享根内：base_path 只过了 check_path_permission，
        # 而盘符路径（C:/…）在 Windows 上会让 os.path.join 直接返回它本身、跳出共享根
        from leaffs.utils.core import safe_path as _safe_path
        if not _safe_path(UPLOAD_DIR, search_root):
            handler.send_json({'error': '无权限'}, 403); return
        if not os.path.exists(search_root):
            handler.send_json({'files': [], 'query': query}); return
        truncated = False
        for root, dirs, files in os.walk(search_root):
            # LF-22：剪枝，不进上传临时目录
            dirs[:] = [d for d in dirs if not is_upload_tmp_entry(d)]
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
        _log_exc('搜索', e)
        handler.send_json({'error': '搜索失败'}, 500)


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
    # 禁止 Windows 盘符（C:/x、C:x）：normpath 不去盘符，而
    # os.path.join(UPLOAD_DIR, 'C:/x') 在 Windows 上会直接返回 'C:/x' —— 等于跳出共享根
    if len(norm) >= 2 and norm[1] == ':' and norm[0].isalpha():
        return None
    # 标准化后再次检查，防止 normpath 改变相对结构
    if norm in ('..', '../') or norm.startswith('../'):
        return None
    # 禁止 Win32 保留设备名（NUL/CON/COM1…）：os.path.exists 对它们返回 True，
    # 于是能混过"文件是否存在"的检查、到下游才炸（N-3）
    if has_windows_device_name(norm):
        return None
    return norm


class _ZipStreamWriter:
    """把 zipfile 的压缩输出直接转发到 HTTP 响应流（带写侧无进展超时）。

    该对象刻意不可 seek —— zipfile 检测到不可 seek 的输出会自动改用 data
    descriptor（local header 标志位 bit 3），并在 close() 时顺序追加中央目录，
    从而实现对网络流的“边压边发”，无需任何临时文件。

    HTTP/1.1 预备（2026-09-18）：流式打包事先不知道总长度，所以在 1.1 下改用
    **chunked** 定界（`chunked=True`）；1.0 下沿用旧行为（靠关闭连接定界），
    所以协议开关没切之前一切照旧。
    """

    def __init__(self, handler, chunked=False):
        self._handler = handler
        self._chunked = chunked

    def write(self, data):
        if not data:
            return 0
        n = len(data)
        if self._chunked:
            # HTTP chunked 分块：<十六进制长度>\r\n<数据>\r\n
            _stream_write(self._handler, b'%x\r\n' % n + data + b'\r\n')
        else:
            _stream_write(self._handler, data)
        return n                      # ⚠️ 必须是**原始**长度：zipfile 靠它记账

    def flush(self):
        try:
            self._handler.wfile.flush()
        except Exception:
            pass

    def finish(self):
        """chunked 的终止块（0 长度块）；非 chunked 时是空操作。

        ⚠️ 正文**正常写完**之后必须调用一次 —— 少了它客户端会一直等下一个块。
        连接已经断开时不用（也写不出去）。
        """
        if not self._chunked:
            return
        try:
            _stream_write(self._handler, b'0\r\n\r\n')
            # 终止块只有 5 字节，不 flush 就可能一直躺在 wfile 的写缓冲里，
            # 客户端于是收不到结束标记 —— chunked 下这是正确性问题，不是优化。
            self._handler.wfile.flush()
        except Exception:
            pass


def zip_download(handler, UPLOAD_DIR, COPY_BUFFER_SIZE, is_path_safe, DISCONNECTED_EXCEPTIONS,
                 resolve_share=None):
    """ZIP 打包下载（双模式，配置键 zip_streaming 切换，默认全流式）。

    resolve_share：可选的「分享虚拟路径 → 源文件**绝对路径**」解析器（约定同
    `share/mappings.resolve`）。命中即按源文件打包；解析器抛异常视作无权（分享码没过）。
    与 `/download` 同口径：**先映射、后权限**。
    一个文件都没打成功时**统一回 404** —— 不区分"无权"与"不存在"，免得变成权限 oracle。

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
        # LF-22：上传临时目录既不作为打包基准、也不参与打包
        if is_upload_tmp_relpath(base_path):
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
            from leaffs.config import core as _cc
            zip_limit = int(_cc.get_zip_max_files())
        except Exception:
            zip_limit = 500
        entries = []
        skipped = 0
        for rel_path in file_list:
            # 规范化并校验每个文件的路径
            rel_path = _normalize_rel_path(rel_path)
            if rel_path is None:
                skipped += 1
                continue
            # LF-22：上传临时目录里的东西不参与打包
            if is_upload_tmp_relpath(rel_path):
                skipped += 1
                continue
            # 分享虚拟路径优先：与 /download 同口径（先映射、后权限）
            full = None
            arcname = None
            if resolve_share is not None:
                try:
                    full = resolve_share(rel_path)
                except Exception:
                    skipped += 1        # 解析器抛异常 = 分享码没解锁 → 按无权处理
                    continue
                if full:
                    arcname = rel_path.rsplit('/', 1)[-1]
            if not full:
                # 对每个文件单独进行权限检查
                if not handler._check_path_permission(rel_path):
                    skipped += 1
                    continue
                full = os.path.join(UPLOAD_DIR, rel_path)
                arcname = os.path.relpath(full, base_full)
            if not is_path_safe(UPLOAD_DIR, full):
                skipped += 1
                continue
            if not os.path.exists(full):
                skipped += 1
                continue
            if os.path.isfile(full):
                entries.append((full, arcname))
            elif os.path.isdir(full):
                for root, dirs, files in os.walk(full):
                    # LF-22：打包整个目录时也不进上传临时目录
                    dirs[:] = [d for d in dirs if not is_upload_tmp_entry(d)]
                    for file in files:
                        fp = os.path.join(root, file)
                        # 对子文件也校验路径安全性
                        if not is_path_safe(UPLOAD_DIR, fp): continue
                        entries.append((fp, os.path.relpath(fp, base_full)))
        # LF-05：一个都没打成功 → **统一回 404**，不再发 200 + 空 ZIP（那是假成功），
        # 也不区分"无权"与"不存在"—— 区分开就是个权限 oracle
        if not entries:
            handler.send_error(404, '文件不存在或无权访问')
            return
        if skipped:
            from leaffs.runtime_log import add_log as _al
            _al('ZIP 打包跳过 %d 项（无权 / 不存在 / 分享码未解锁）' % skipped, 'warn')
        if zip_limit and len(entries) > zip_limit:
            handler.send_json({'error': f'文件数量过多（上限{zip_limit}个，可在高级配置调整）'}, 400)
            return
        # 模式判定：zip_streaming 默认 True（全流式）；异常时按全流式兜底
        try:
            from leaffs.config import core as _cc
            streaming = bool(_cc.get_zip_streaming())
        except Exception:
            streaming = True
        disp = f'attachment; filename="files_{int(time.time())}.zip"'
        if streaming:
            # ===== 全流式：边压边发、零临时磁盘。定界方式随协议版本走：
            # 1.0 靠关闭连接（无 Content-Length），1.1 用 chunked =====
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/zip')
            handler.send_header('Content-Disposition', disp)
            # HTTP/1.1 预备（2026-09-18）：1.1 下必须自带定界，否则客户端会一直等正文。
            # 现在 protocol_version 还是 1.0 ⇒ 这段不启用，行为与改动前完全一致；
            # 等切换协议那一行落地，它自动生效。
            chunked = getattr(handler, 'protocol_version', 'HTTP/1.0') == 'HTTP/1.1'
            if chunked:
                handler.send_header('Transfer-Encoding', 'chunked')
            handler.end_headers()      # 公共头（含 nosniff）由 end_headers 统一补
            writer = _ZipStreamWriter(handler, chunked=chunked)
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
                writer.finish()      # 正文写完 ⇒ 发 chunked 终止块（非 chunked 时空操作）
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
            handler.end_headers()      # 公共头（含 nosniff）由 end_headers 统一补
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