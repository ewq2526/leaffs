"""页面管理 API — 管理页面和静态资源渲染（HTML 注入）"""

import os

from leaffs.config import core as _cfg


def _inject_ws_port(html):
    """把实际 WS 端口注入页面占位 __WS_PORT_NUM__（改端口后前端仍能连上 WS）。

    占位符刻意不与 JS 属性名 window.__WS_PORT__ 相同，避免全局替换把属性赋值写坏。
    """
    return html.replace('__WS_PORT_NUM__', str(_cfg.WS_PORT))


def serve_admin_page(handler, BASE_DIR, read_file_cached, is_default_admin_password):
    """渲染管理页面（注入角色和密码提醒）；语言为 en（cookie 或账号偏好）时优先英文版。"""
    filepath = os.path.join(BASE_DIR, 'web_page', 'management', 'management.html')
    en_file = _en_variant_path(BASE_DIR, 'web_page/management/management.html',
                               handler.headers.get('Cookie', ''),
                               handler._get_username_from_session())
    if en_file:
        filepath = en_file
    data = read_file_cached(filepath)
    if data is None:
        handler.send_error(404)
        return
    role_code = 'super_admin' if handler.role == 'super_admin' else 'admin'
    _ck = handler.headers.get('Cookie', '')
    _u = handler._get_username_from_session()
    html = data.decode('utf-8').replace('__MY_ROLE__', role_code)
    html = _inject_theme(html, _effective_accent(_ck, _u))
    need_change = 'false'
    if handler.role in ('admin', 'super_admin'):
        if is_default_admin_password():
            need_change = 'true'
    html = html.replace('__NEED_CHANGE_PASSWORD__', need_change)
    html = _inject_ws_port(html)
    data = html.encode('utf-8')
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/html; charset=utf-8')
    handler.send_header('Content-Length', str(len(data)))
    # B-14：管理页（含会话内容）禁止缓存；补防嗅探/防点击劫持/防外泄引用头
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('X-Frame-Options', 'DENY')
    handler.send_header('Referrer-Policy', 'no-referrer')
    handler.end_headers()
    handler.wfile.write(data)


def serve_admin_users_page(handler, get_session, get_session_username, read_file_cached, BASE_DIR):
    """渲染用户管理页面（注入角色和用户名）；语言为 en（cookie 或账号偏好）时优先英文版。"""
    filepath = os.path.join(BASE_DIR, 'web_page', 'management', 'users.html')
    cookie = handler.headers.get('Cookie', '')
    _, sid = get_session(cookie, True, handler.client_address[0])
    username = get_session_username(sid) if sid else ''
    en_file = _en_variant_path(BASE_DIR, 'web_page/management/users.html', cookie, username)
    if en_file:
        filepath = en_file
    data = read_file_cached(filepath)
    if data is None:
        handler.send_error(404)
        return
    role_code = 'super_admin' if handler.role == 'super_admin' else 'admin'
    html = data.decode('utf-8')
    html = html.replace('__MY_ROLE__', role_code)
    html = html.replace('__MY_USERNAME__', username)
    html = _inject_theme(html, _effective_accent(cookie, username))
    html = _inject_ws_port(html)
    data = html.encode('utf-8')
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/html; charset=utf-8')
    handler.send_header('Content-Length', str(len(data)))
    # B-14：管理页（含会话内容）禁止缓存；补防嗅探/防点击劫持/防外泄引用头
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('X-Frame-Options', 'DENY')
    handler.send_header('Referrer-Policy', 'no-referrer')
    handler.end_headers()
    handler.wfile.write(data)


def _user_accent(username):
    """已登录用户的服务端主题主色（空=默认蓝色）；游客/未知返回 ''。"""
    if username and username != '游客':
        try:
            from leaffs.auth import core as _ac
            return _ac.get_user_accent(username)
        except Exception:
            pass
    return ''


def _cookie_accent(cookie):
    """请求 cookie 里的主题主色偏好（leaf_accent；供游客/未登录浏览器记住颜色）"""
    try:
        from leaffs.auth import core as _ac
        for part in (cookie or '').split(';'):
            k, _, v = part.strip().partition('=')
            if k == 'leaf_accent' and v and v in _ac._ACCENT_COLORS:
                return v
    except Exception:
        pass
    return ''


def _effective_accent(cookie, username=''):
    """页面主题主色：登录账号服务端偏好优先，其次浏览器 cookie，最后默认蓝。"""
    acc = _user_accent(username)
    if acc:
        return acc
    return _cookie_accent(cookie)


def _inject_theme(html, accent):
    """把主题主色写入 <html data-accent="...">（配色见 style.css data-accent 覆盖块）"""
    if accent:
        html = html.replace('<html', '<html data-accent="%s"' % accent, 1)
    return html


def _lang_wants_en(cookie, username=''):
    """语言判定：cookie 显式选择（leaf_lang）优先；否则已登录用户的服务端语言偏好
    ——本机 webview 为无痕会话、cookie 存不住，靠服务端账号偏好（users.json）恢复
    该用户上次的选择；游客/匿名无账号偏好时回退中文。"""
    try:
        if 'leaf_lang=en' in (cookie or ''):
            return True
        if 'leaf_lang=zh' in (cookie or ''):
            return False
        if username and username != '游客':
            from leaffs.auth import core as _ac
            return _ac.get_user_ui_lang(username) == 'en'
    except Exception:
        pass
    return False


def _en_variant_path(BASE_DIR, filename, cookie, username=''):
    """语言为 en 且存在同名 .en.html 时，返回英文版文件路径；否则 None。"""
    try:
        if filename.endswith('.html') and _lang_wants_en(cookie, username):
            cand = os.path.join(BASE_DIR, filename[:-5] + '.en.html')
            if os.path.isfile(cand):
                return cand
    except Exception:
        return None
    return None


def serve_file(handler, filename, content_type, BASE_DIR,
               get_session, get_session_username):
    """渲染常规 HTML 页面（注入用户名和角色）；语言 cookie 为 en 时优先渲染英文版页面。"""
    filepath = os.path.join(BASE_DIR, filename)
    try:
        with open(filepath, 'rb') as f:
            raw = f.read()
    except Exception:
        handler.send_error(404)
        return
    html = raw.decode('utf-8')
    cookie = handler.headers.get('Cookie', '')
    role, sid = get_session(cookie, True, handler.client_address[0])
    username = ''
    if sid:
        username = get_session_username(sid)
    # 英文页面文件覆盖（若存在）：英文版内容已直接书写为英文，不经过运行时词典；
    # 语言判定：cookie 显式选择优先，否则已登录用户的服务端语言偏好
    en_file = _en_variant_path(BASE_DIR, filename, cookie, username)
    if en_file:
        try:
            with open(en_file, 'rb') as f:
                html = f.read().decode('utf-8')
        except Exception:
            pass  # 读取失败回退中文版
    if username and username != '游客':
        html = html.replace('__MY_USERNAME__', username)
    else:
        html = html.replace('__MY_USERNAME__', '')
    # 注入角色信息（用于前端显隐导航项）
    role_json = f'"{role}"' if role else 'null'
    html = html.replace('__MY_ROLE__', role_json)
    html = _inject_theme(html, _effective_accent(cookie, username))
    html = _inject_ws_port(html)
    data = html.encode('utf-8')
    handler.send_response(200)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(len(data)))
    # B-14：serve_file 渲染的均为按会话注入的 HTML（含 /admin/* 管理页），禁止缓存；
    # 补防嗅探/防点击劫持/防外泄引用头
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('X-Frame-Options', 'DENY')
    handler.send_header('Referrer-Policy', 'no-referrer')
    handler.end_headers()
    handler.wfile.write(data)


def serve_static(handler, url_path, BASE_DIR, is_path_safe, get_mime, read_file_cached):
    """渲染静态资源（JS/CSS 等）"""
    filename = url_path.replace('/static/', '', 1)
    # ---- 名字净化：先统一分隔符再分段检查 ----
    # 反斜杠视为目录分隔符；任一段为 .. 、首段为空（绝对路径）或首段含冒号（盘符/C: 等）一律拒绝
    filename = filename.replace('\\', '/')
    parts = filename.split('/')
    if parts[0] == '' or '..' in parts or ':' in parts[0]:
        handler.send_error(403)
        return
    web_page_dir = os.path.join(BASE_DIR, 'web_page')
    filepath = os.path.join(web_page_dir, 'common', filename)
    if not os.path.exists(filepath) and '/' in filename:
        filepath = os.path.join(web_page_dir, filename)
    # 兜底：无论走 common 还是回退路径，最终文件都必须落在 BASE_DIR/web_page 内
    # （realpath 前缀 + os.sep 边界，统一由 is_path_safe 实现）
    if not is_path_safe(web_page_dir, filepath):
        handler.send_error(403)
        return
    mime = get_mime(filename)
    data = read_file_cached(filepath)
    if data is None:
        handler.send_error(404)
        return
    handler.send_response(200)
    handler.send_header('Content-Type', mime)
    handler.send_header('Content-Length', str(len(data)))
    # 每次请求都校验新鲜度，避免改版后浏览器长期使用旧静态资源
    handler.send_header('Cache-Control', 'no-cache')
    # B-14：补防嗅探/防点击劫持/防外泄引用头
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('X-Frame-Options', 'DENY')
    handler.send_header('Referrer-Policy', 'no-referrer')
    # B-14：按 mime 补 CSP —— JS/CSS 无内联内容，可用 default-src 'self'；
    # HTML 静态页因含内联脚本无法收紧到 default-src 'self'，折衷放行同源与内联脚本
    # （script-src 'unsafe-inline' 'self'），后续由前端配合点收紧。
    mime_l = (mime or '').lower()
    csp = None
    if 'javascript' in mime_l or 'css' in mime_l:
        csp = "default-src 'self'"
    elif 'html' in mime_l:
        csp = "script-src 'unsafe-inline' 'self'"
    if csp:
        handler.send_header('Content-Security-Policy', csp)
    handler.end_headers()
    handler.wfile.write(data)