# -*- coding: utf-8 -*-
"""下发给浏览器的文件：① 不许带内部路径 ② 模板占位符必须有服务端替换。

**① 内部路径**（外部黑盒报告的 LF-17，已核实）：`web_page/notice/cert.html` 里那段开发注释
（讲服务端 WS 的 Host/Origin 白名单、`ws.py` 里删过什么）**随模板整份下发**，而 8082 说明页
匿名可达 —— `cert_remind._page_bytes()` 只做两次 `str.replace`，**不剥离注释**。
同类还有 `common/theme.js`、`account/account.html` 各一处。

**② 占位符**：页面模板里散着 8 种占位符（`__WS_PORT_NUM__` / `__SERVER_BASE_URL__` /
`__HAS_LAN_VAL__` / `__MY_ROLE__` / `__MY_USERNAME__` / `__GUEST_DISPLAY__` /
`__CERT_STYLE__` / `__CERT_MAIN_URL__` / `<!--THEME_TOGGLE-->`）——
**漏替换一个，页面就会把 `__XXX__` 原样显示给用户**。光靠人眼看不过来，这里两道自动闸：
静态查"有没有替换代码"，动态查"渲染出来的页面里还有没有残留"。

这个项目是**运行时读盘下发、没有构建剥离环节**，所以唯一可靠的办法是
"把源文件写干净 + 有测试盯着"。
"""
import glob
import os
import re

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(PROJ_ROOT, 'leaffs', 'web_page')

# 出现在下发文件里就算泄露的字符串
_FORBIDDEN = ('leaffs/', 'ws.py', 'render.py', 'handler.py', 'access.py',
              'mappings.py', 'core.py', 'cert_remind.py', 'users_api.py')

_TEXT_EXT = ('.html', '.js', '.css', '.json', '.svg', '.txt')

# 模板占位符：`__XXX__`，但排除 `window.__XXX__` 那种 **JS 变量名**
# （`window.__WS_PORT__` / `window.__LANG__` 等是注入后供前端读的运行时变量，不是待替换占位符）
_PLACEHOLDER_RE = re.compile(r'(?<!window\.)(__[A-Z][A-Z0-9_]*__)')
# HTML 注释形式的占位符：<!--THEME_TOGGLE-->
_COMMENT_PLACEHOLDER_RE = re.compile(r'<!--([A-Z][A-Z0-9_]*)-->')

# 登录后应当能正常打开的页面（渲染路径覆盖到各模板）
PAGES = ('/', '/browse/', '/admin', '/admin/users', '/admin/advanced', '/admin/deep',
         '/log', '/gallery', '/account', '/share', '/url-download')


def _web_files():
    out = []
    for p in glob.glob(os.path.join(WEB_DIR, '**', '*'), recursive=True):
        if os.path.isfile(p) and p.lower().endswith(_TEXT_EXT):
            out.append(p)
    return out


def _template_placeholders():
    """收集所有 HTML 模板里出现过的占位符"""
    found = set()
    for p in glob.glob(os.path.join(WEB_DIR, '**', '*.html'), recursive=True):
        with open(p, encoding='utf-8', errors='replace') as f:
            text = f.read()
        found.update(_PLACEHOLDER_RE.findall(text))
        found.update('<!--%s-->' % m for m in _COMMENT_PLACEHOLDER_RE.findall(text))
    return found


def test_no_internal_paths_in_any_web_file():
    """web_page 下的任何文件都不该出现内部路径 / 服务端文件名"""
    bad = []
    for p in _web_files():
        with open(p, encoding='utf-8', errors='replace') as f:
            text = f.read()
        for token in _FORBIDDEN:
            if token in text:
                bad.append('%s 里出现了 %r' % (os.path.relpath(p, PROJ_ROOT), token))
    assert not bad, ('下发文件里带了内部信息（注释也会被下发）：\n  ' + '\n  '.join(bad))


def test_every_template_placeholder_has_server_side_replacement():
    """模板里的**每一个**占位符都必须有服务端替换代码 —— 漏一个就会把 __XXX__ 显示给用户"""
    py = ''
    for p in glob.glob(os.path.join(PROJ_ROOT, 'leaffs', '**', '*.py'), recursive=True):
        with open(p, encoding='utf-8', errors='replace') as f:
            py += f.read()
    placeholders = _template_placeholders()
    assert placeholders, '一个占位符都没扫到，正则或目录是不是写错了？'
    missing = [ph for ph in sorted(placeholders) if ph not in py]
    assert not missing, ('这些占位符在服务端找不到替换代码，会被原样显示给用户：%r\n'
                         '（扫到的占位符：%r）' % (missing, sorted(placeholders)))


def test_rendered_pages_have_no_leftover_placeholders(client):
    """真渲染一遍：登录后打开各页面，响应里不能残留任何占位符"""
    from conftest import login
    login(client)
    bad = []
    checked = 0
    for url in PAGES:
        r = client.get(url)
        if r.status_code != 200 or 'text/html' not in (r.headers.get('Content-Type') or ''):
            continue                      # 重定向/无权限/不是页面 —— 跳过（由别的用例覆盖）
        checked += 1
        left = set(_PLACEHOLDER_RE.findall(r.text))
        left.update('<!--%s-->' % m for m in _COMMENT_PLACEHOLDER_RE.findall(r.text))
        if left:
            bad.append('%s -> %r' % (url, sorted(left)))
    assert checked >= 5, '只渲染到 %d 个页面，测试没起到作用' % checked
    assert not bad, '渲染后的页面里还有占位符残留：\n  ' + '\n  '.join(bad)


def test_cert_page_renders_without_leftover_placeholders():
    """8082 说明页（不走主渲染路径）单独验一遍"""
    p = os.path.join(WEB_DIR, 'notice', 'cert.html')
    with open(p, encoding='utf-8') as f:
        html = f.read()
    assert '__CERT_STYLE__' in html and '__CERT_MAIN_URL__' in html, '占位符没了，渲染会坏'
    html = html.replace('__CERT_STYLE__', '/*css*/')
    html = html.replace('__CERT_MAIN_URL__', 'https://localhost:8080/')
    assert '__CERT_' not in html, '还有没被替换的占位符'
