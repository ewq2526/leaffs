"""页面管理 API — 管理页面和静态资源渲染（HTML 注入）"""

import datetime
import email.utils
import os
import re

from leaffs.config import core as _cfg
from leaffs.utils.core import parse_cookies


def _inject_ws_port(html):
    """把实际 WS 端口注入页面占位 __WS_PORT_NUM__（改端口后前端仍能连上 WS）。

    占位符刻意不与 JS 属性名 window.__WS_PORT__ 相同，避免全局替换把属性赋值写坏。
    """
    return html.replace('__WS_PORT_NUM__', str(_cfg.WS_PORT))


def _inject_server_base(html):
    """注入「对外访问基址」与「是否有可用局域网」（占位 __SERVER_BASE_URL__ / __HAS_LAN__）。

    分享页要给别人的是局域网地址，而网页自己是从回环地址打开的（桌面窗口是
    localhost、安卓 WebView 是 127.0.0.1），用 location.origin 拼出来的链接别人
    打不开 —— 改成由服务端算好注入，两端同一份代码、同一个来源，显示才一致。
    占位符刻意不与 JS 属性名相同，避免全局替换把属性赋值写坏（同 _inject_ws_port）。
    """
    try:
        from leaffs.server.hosts import lan_status as _lan_status
        sip, has_lan = _lan_status()
    except Exception:
        sip, has_lan = '127.0.0.1', False
    try:
        scheme = 'https' if _cfg.get_tls_enabled() else 'http'
        port = _cfg.PORT
        tail = '' if (scheme == 'http' and port == 80) or \
                     (scheme == 'https' and port == 443) else ':%s' % port
    except Exception:
        scheme, tail = 'http', ''
    html = html.replace('__SERVER_BASE_URL__', '%s://%s%s' % (scheme, sip, tail))
    # 占位符用 __HAS_LAN_VAL__（不是 __HAS_LAN__）：后者同时是页面的 JS 属性名，
    # 同名会让 replace 把属性赋值也一起换掉（window.__HAS_LAN__= → window.false=）
    return html.replace('__HAS_LAN_VAL__', 'true' if has_lan else 'false')


# 主题切换按钮的文案（全站唯一一份；页面语言由调用方按是否英文版决定）
_THEME_LABELS = {
    'zh': {'light': '亮色', 'dark': '暗色'},
    'en': {'light': 'Light', 'dark': 'Dark'},
}


def _inject_theme_toggle(html, theme='', is_en=False):
    """把占位 <!--THEME_TOGGLE--> 换成主题切换按钮（连同它自己的脚本依赖）。

    按钮原先在 22 个静态页里各抄一份，连初始文案都各写各的（英文登录页就写成了 Dark），
    样式虽在 common/style.css、逻辑虽在 common/app.js，页面这一层始终没有统一的地方 ——
    改成只有一份：文案取自 _THEME_LABELS，切换时由 common/theme.js 从 data-label-* 读回。
    """
    labels = _THEME_LABELS['en' if is_en else 'zh']
    cur = labels['dark'] if theme == 'dark' else labels['light']
    # script 紧跟按钮之后：脚本执行时按钮已在 DOM 中，theme.js 初始化可直接改文案
    # （script 在 UA 样式里是 display:none，不会成为 flex/grid 的子项，不影响布局）
    frag = ('<button class="dark-toggle" id="darkToggle" onclick="toggleDark()"'
            ' style="width:auto;padding:0 10px"'
            ' data-label-light="%s" data-label-dark="%s">'
            '<span id="darkLabel" style="font-size:11px;font-weight:500">%s</span></button>'
            '<script src="/static/theme.js"></script>'
            ) % (labels['light'], labels['dark'], cur)
    return html.replace('<!--THEME_TOGGLE-->', frag)


def inject_page_theme(html, cookie='', is_en=False):
    """页面主题收尾：注入 <html data-theme / data-accent> + 主题切换按钮。

    主题是一体的（亮暗 + 主色）：只看客户端本地的 leaf_theme / leaf_accent cookie ——
    与语言的 leaf_lang 同一做法。每个客户端各存各的，服务端不存储，账号里也没有。
    都没有就是默认（亮色 + 默认蓝）—— 首次打开用默认主题是正常行为。
    服务端读 cookie 只是为了首屏就渲染成对的配色，避免先出来再被 JS 改。

    当前 4 个入口：serve_file / serve_admin_page / serve_admin_users_page /
    auth.login_api.serve_login_page。
    """
    theme = _cookie_theme(cookie)
    html = _inject_theme(html, _cookie_accent(cookie), theme)
    return _inject_theme_toggle(html, theme, is_en)


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
    html = data.decode('utf-8').replace('__MY_ROLE__', role_code)
    need_change = 'false'
    if handler.role in ('admin', 'super_admin'):
        if is_default_admin_password():
            need_change = 'true'
    html = html.replace('__NEED_CHANGE_PASSWORD__', need_change)
    html = _inject_ws_port(html)
    html = inject_page_theme(html, _ck, bool(en_file))
    data = html.encode('utf-8')
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/html; charset=utf-8')
    handler.send_header('Content-Length', str(len(data)))
    # B-14：管理页（含会话内容）禁止缓存。
    # nosniff / X-Frame-Options / Referrer-Policy **不在这里手写** —— 公共头只在
    # handler 的 _common_security_headers 里定义一处，end_headers 会自动补；
    # 手写会跟自动补的撞车，发出 `nosniff, nosniff` 这种重复头。
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()
    handler.wfile.write(data)


def serve_admin_users_page(handler, get_session, get_session_username, read_file_cached, BASE_DIR):
    """渲染用户管理页面（注入角色和用户名）；语言为 en（cookie 或账号偏好）时优先英文版。"""
    filepath = os.path.join(BASE_DIR, 'web_page', 'management', 'users.html')
    cookie = handler.headers.get('Cookie', '')
    _, sid = get_session(cookie, handler.client_address[0])
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
    html = _inject_ws_port(html)
    html = inject_page_theme(html, cookie, bool(en_file))
    data = html.encode('utf-8')
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/html; charset=utf-8')
    handler.send_header('Content-Length', str(len(data)))
    # B-14：管理页（含会话内容）禁止缓存。公共头由 end_headers 统一补（见上）
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()
    handler.wfile.write(data)


def _cookie_accent(cookie):
    """请求 cookie 里的主题主色偏好（leaf_accent）；缺失或非法值返回 ''（默认蓝）

    解析口径全仓一份（`utils/core.parse_cookies`，同名 Cookie 取**最后一个**）。
    ⚠️ 原来这里是"取第一个**合法**值" —— 两个同名值都合法时会挑前面那个，
    与"最后写下的生效"相反；非法值会被跳过这一点保持不变。
    """
    try:
        from leaffs.ui_theme import ACCENTS as _ACCENTS
        v = parse_cookies(cookie).get('leaf_accent', '')
        return v if v in _ACCENTS else ''
    except Exception:
        return ''


def _cookie_theme(cookie):
    """请求 cookie 里的亮/暗偏好（leaf_theme）—— 口径同 `_cookie_accent`"""
    try:
        v = parse_cookies(cookie).get('leaf_theme', '')
        return v if v in ('dark', 'light') else ''
    except Exception:
        return ''


def _inject_theme(html, accent, theme=''):
    """把主题主色与亮暗写入 <html>（配色见 style.css 的 data-theme / data-accent 覆盖块）

    服务端注入可保证首屏即正确主题（不闪），亮暗的来源是客户端本地的 cookie，
    见 inject_page_theme。
    """
    attrs = ''
    if theme == 'dark':
        attrs += ' data-theme="dark"'
    if accent:
        attrs += ' data-accent="%s"' % accent
    if attrs:
        html = html.replace('<html', '<html' + attrs, 1)
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


def _csp_host(handler):
    """从 `Host` 头取出可用于 CSP 的 host（**必须净化**）。

    CSP 头是拼字符串发出去的，中间混进换行就是**头注入**。所以这里只放行两种形状：
    普通域名 / IPv4（字母数字、点、连字符）与方括号形式的 IPv6；其余一律返回空串，
    调用方退化成只写 `'self'`（宁可靠同源兜，也不拼进去一段来路不明的东西）。
    端口一并丢掉 —— WS 端口我们自己知道（`_cfg.WS_PORT`），不用客户端告诉我们。
    """
    raw = (handler.headers.get('Host') or '').strip()
    if not raw:
        return ''
    if raw.startswith('['):                     # IPv6：[::1]:8080
        end = raw.find(']')
        if end < 0:
            return ''
        host = raw[:end + 1]
        return host if re.fullmatch(r'\[[0-9A-Fa-f:.]+\]', host) else ''
    host = raw.split(':', 1)[0]
    return host if re.fullmatch(r'[A-Za-z0-9.-]+', host) else ''


def page_csp(handler):
    """页面 CSP（2026-09-18）：把**子资源**锁在同源与自己的 WebSocket 上。

    为什么要有它：`serve_file` 渲染的页面以前**完全没有 CSP**，于是页面里一旦出现
    `<img src="外部">`、外链脚本/样式、或 `fetch('http://外部')`，浏览器就会真的去请求 ——
    那是「软件内不访问外部链接」的最后一块缺口。**导航**由客户端的白名单管
    （安卓 `onLoadRequest`、桌面 WebView2 的导航层），**子资源**归这里，两者互补。
    好处是它由浏览器执行：**安卓 / 桌面 / 任何浏览器一起生效**，不依赖客户端实现。

    写法依据（都查过页面实际用法，不是照抄模板）：
      · 所有 fetch/XHR 都是相对路径（含 `var API = '/api/url-download'`），没有一处打外部；
      · `__SERVER_BASE__` 只用来拼**下载链接**（导航，不受 CSP 管），没有 fetch 用它；
      · 页面里没有 <video>/<audio>/<iframe>/<object> ⇒ 不需要 media-src / frame-src；
      · 内联 `style="…"` 属性与内联 <script> 用得很多 ⇒ 必须留 'unsafe-inline'；
      · 缩略图走 /api/thumb（同源），二维码可能内联 ⇒ img-src 补 data: 与 blob:；
      · WebSocket 连的是 `ws(s)://<当前 hostname>:<WS_PORT>/ws`，而端口可配置
        ⇒ connect-src 必须动态带上它，只写 'self' 会把下载器的实时进度打断。
    """
    connect = "'self'"
    host = _csp_host(handler)
    if host:
        ws_port = _cfg.WS_PORT
        # ws 与 wss 都放行：这里连的是自己的端口，多给一个 scheme 不会扩大可达面，
        # 但能避免"明文/加密模式判断错了导致 WS 连不上"这种难以定位的故障。
        connect += f' ws://{host}:{ws_port} wss://{host}:{ws_port}'
    return (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        f"connect-src {connect}; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )


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
    role, sid = get_session(cookie, handler.client_address[0])
    username = ''
    if sid:
        username = get_session_username(sid)
    # 英文页面文件覆盖（若存在）：英文版内容已直接书写为英文，不经过运行时词典；
    # 语言判定：cookie 显式选择优先，否则已登录用户的服务端语言偏好
    en_file = _en_variant_path(BASE_DIR, filename, cookie, username)
    used_en = False
    if en_file:
        try:
            with open(en_file, 'rb') as f:
                html = f.read().decode('utf-8')
            used_en = True
        except Exception:
            pass  # 读取失败回退中文版
    if username and username != '游客':
        html = html.replace('__MY_USERNAME__', username)
    else:
        html = html.replace('__MY_USERNAME__', '')
    # 注入角色信息（用于前端显隐导航项）
    role_json = f'"{role}"' if role else 'null'
    html = html.replace('__MY_ROLE__', role_json)
    html = _inject_ws_port(html)
    html = _inject_server_base(html)
    html = inject_page_theme(html, cookie, used_en)
    data = html.encode('utf-8')
    handler.send_response(200)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(len(data)))
    # B-14：serve_file 渲染的均为按会话注入的 HTML（含 /admin/* 管理页），禁止缓存。
    # 2026-09-18：页面 CSP **不在这里手写** —— 由 handler.end_headers 按 Content-Type
    # 统一补（见 HTTPHandler._page_csp_for_response）。手工点会漏：今天一共数出 6 处
    # 发 HTML 的地方（本函数 / 两个管理页 / 登录页 / 公开分享页 / 扫码页）。
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()
    handler.wfile.write(data)


def _static_not_modified(handler, etag, mtime):
    """条件请求判定（RFC 7232）：命中就 304，省掉整份静态资源的重传。

    ⚠️ 优先序是规范规定的：请求里**只要带了 `If-None-Match` 就不再理会 `If-Modified-Since`**
    —— 否则一个陈旧的 `If-Modified-Since` 会把 ETag 的判定推翻。
    `If-None-Match` 用**弱比较**（`W/"x"` 与 `"x"` 等价，见 RFC 7232 §3.2）。
    `If-Modified-Since` 只有秒精度，所以按秒取整比较（资源比它新才算变过）。
    """
    inm = handler.headers.get('If-None-Match')
    if inm is not None:
        for token in inm.split(','):
            token = token.strip()
            if token == '*':
                return True             # "只要资源还在就别回正文"
            if token.startswith('W/'):
                token = token[2:]
            if token == etag:
                return True
        return False
    ims = handler.headers.get('If-Modified-Since')
    if not ims:
        return False
    try:
        since = email.utils.parsedate_to_datetime(ims)
    except (TypeError, ValueError):
        return False
    if since is None:
        return False
    if since.tzinfo is None:
        # 规范要求 HTTP 日期是 GMT；真收到不带时区的就按 UTC 解释，
        # 不能落到系统本地时区（那会让判定整体偏移）
        since = since.replace(tzinfo=datetime.timezone.utc)
    try:
        return int(mtime) <= int(since.timestamp())
    except (OverflowError, OSError, ValueError):
        return False


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

    # ---- 条件请求（2026-09-18）：把 no-cache 从"每次全量重传"变成"回源验证后可以 304" ----
    # `no-cache` 的语义是"**可以**存、但每次用之前必须回源验证"，而验证需要验证器 ——
    # 以前一个都没发，于是每次都回完整的 200 ＋ 整个文件，`no-cache` 实际退化成了 `no-store`
    # （静态资源共 653.9 KB）。这里补上 ETag（大小 + mtime 纳秒）与 Last-Modified。
    #
    # ⚠️ 安全前提：`/static/*` 发的是 `web_page/` 下的**原文件、不做任何替换** ⇒ 同一个 URL
    # 对所有用户内容完全相同，304 不会串味。**按会话注入的页面是另一条路**（`serve_file`
    # 与各页面 handler，会替换 `__MY_ROLE__` / `__MY_USERNAME__`），它们因人而异、
    # 仍然是 `no-store`，**不要顺手也给它们加验证器**。
    # ⚠️ 拿不到 stat 时宁可退回"每次全量"，也不假装有一个 ETag。
    etag = last_modified = None
    mtime = 0.0
    try:
        st = os.stat(filepath)
        mtime = st.st_mtime
        etag = '"%x-%x"' % (st.st_size, st.st_mtime_ns)
        last_modified = email.utils.formatdate(mtime, usegmt=True)
    except OSError:
        pass

    if etag and _static_not_modified(handler, etag, mtime):
        handler.send_response(304)
        handler.send_header('ETag', etag)
        handler.send_header('Last-Modified', last_modified)
        # ⚠️ 这行不能省：`send_header` 会记下"缓存策略已声明"，`end_headers` 才不会再补一个
        # `no-store` —— 补上就把静态资源反而标成完全不可缓存了。有回归测试盯着这条。
        handler.send_header('Cache-Control', 'no-cache')
        handler.end_headers()
        return

    handler.send_response(200)
    handler.send_header('Content-Type', mime)
    handler.send_header('Content-Length', str(len(data)))
    if etag:
        handler.send_header('ETag', etag)
        handler.send_header('Last-Modified', last_modified)
    # 每次请求都校验新鲜度，避免改版后浏览器长期使用旧静态资源
    handler.send_header('Cache-Control', 'no-cache')
    # B-14：按 mime 补 CSP —— JS/CSS 无内联内容，可用 default-src 'self'；
    # HTML 静态页因含内联脚本无法收紧到 default-src 'self'，折衷放行同源与内联脚本
    # （script-src 'unsafe-inline' 'self'），后续由前端配合点收紧。
    # 其余公共头（nosniff/XFO/Referrer）走统一入口，不手写
    mime_l = (mime or '').lower()
    csp = None
    if 'javascript' in mime_l or 'css' in mime_l:
        csp = "default-src 'self'"
    elif 'html' in mime_l:
        csp = "script-src 'unsafe-inline' 'self'"
    handler._common_security_headers(csp=csp)
    handler.end_headers()
    handler.wfile.write(data)