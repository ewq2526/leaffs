# -*- coding: utf-8 -*-
"""HTML 属性位置用"文本转义"→ 存储型 XSS（2026-09-18 修，同一根因的一组）。

**根因**：同一个转义函数被用在两个不同上下文上 ——

  * `esc()`     = **文本上下文**（`createTextNode` → `innerHTML`）：只处理 `&` `<` `>`，
                  **不转引号**（文本节点里本来不需要）；
  * `safeStr()` = **JS 字符串上下文**：只转 `\\` 与 `'`，**同样不转引号**。

把它们的结果放进 `title="..."` / `data-path="..."` / `onclick="f('...')"` 这类
**双引号属性**里，数据里一个 `"` 就能闭合属性、再注入 `onmouseover=` 之类。

**可达性**：`sanitize_entry_name` **允许文件名含 `"`**（只拒 `/` `\\` `:`、控制字符、
`..`、Windows 保留名、超长）；下载 URL 更松 —— `http://` 会被主机解析校验挡掉，
但 **magnet 不走主机解析**，`magnet:?xt=urn:btih:<40hex>&dn="onmouseover="alert(1)`
实测**原样穿过**并进任务列表（探针 `.cache/probe_dl_xss.py`），而 `_dl_allowed()` 对
user/admin/super_admin 都放行、任务池是进程级单例 ⇒ 普通用户投毒、管理员中招。

**修法**：两个**属性专用**转义 ——

  * `escAttr(s) = esc(s).replace(/"/g, '&quot;')`   —— 属性里的普通数据
  * `jsAttr(s)  = escAttr(safeStr(s))`              —— 属性里的 JS 字符串（onclick）

顺序有讲究：**先** `esc()` 把 `&` 转成 `&amp;`、**再**补引号，这样插入的 `&quot;`
才不会被二次转义。

⚠️ 前端代码没法在 pytest 里真跑 DOM，所以这里是两层：
① **文本守卫**锁住写法；② 用标准库 `html.parser` 验证**转义方案本身**的语义。
"""
import html.parser
import os
import re

import leaffs

# 引用 /static/app.js 的页面（helper 来自 common/app.js）
PAGES_VIA_APPJS = (
    'leaffs/web_page/home/home.html',
    'leaffs/web_page/home/home.en.html',
    'leaffs/web_page/preview/preview.html',
    'leaffs/web_page/preview/preview.en.html',
)
# 自带 helper 的页面（downloader 不引 app.js，esc/safeStr 都是自己的）
PAGES_SELF_CONTAINED = (
    'leaffs/web_page/downloader/downloader.html',
    'leaffs/web_page/downloader/downloader.en.html',
)
ALL_PAGES = PAGES_VIA_APPJS + PAGES_SELF_CONTAINED
APP_JS = 'leaffs/web_page/common/app.js'


def _read(rel):
    root = os.path.dirname(os.path.dirname(os.path.abspath(leaffs.__file__)))
    with open(os.path.join(root, rel), encoding='utf-8') as f:
        return f.read()


# ---------- ① 文本守卫 ----------

def test_no_text_escape_in_attribute_position():
    """★ 属性位置不许再用文本转义（`esc`）或纯 JS 字符串转义（`safeStr`）

    三种被修掉的写法都在这里锁住：
      * `title="' + esc(…)`      —— 文本转义进属性；
      * `data-path="' + esc(…)`  —— 同上；
      * `onclick="…safeStr(…)`   —— 只过了 JS 单引号，没过 HTML 双引号属性。
    """
    for rel in ALL_PAGES:
        src = _read(rel)
        for pat, why in (
            (r'title="\'\s*\+\s*esc\(', '标题属性用了文本转义 esc'),
            (r'data-path="\'\s*\+\s*esc\(', 'data-path 用了文本转义 esc'),
            (r'onclick="[^"]*safeStr\(', 'onclick 里只做了 safeStr（缺 HTML 属性转义）'),
        ):
            hit = re.findall(pat, src)
            assert not hit, '%s：%s ⇒ %r' % (rel, why, hit)


def test_attribute_helpers_exist():
    """★ helper 必须在位：app.js 两个，downloader 自带两个"""
    app = _read(APP_JS)
    assert re.search(r"function escAttr\(str\)\s*\{\s*return esc\(str\)\.replace\(/\"/g, '&quot;'\);", app), \
        'app.js 的 escAttr 实现不是"先 esc 再补引号"'
    assert re.search(r'function jsAttr\(str\)\s*\{\s*return escAttr\(safeStr\(str\)\);\s*\}', app), \
        'app.js 的 jsAttr 不是 escAttr(safeStr(…))'

    for rel in PAGES_SELF_CONTAINED:
        src = _read(rel)
        assert 'function escAttr' in src, '%s 没自带 escAttr' % rel
        assert 'var jsAttr = function' in src, '%s 没自带 jsAttr' % rel
        assert "escAttr(s.replace(" in src, '%s 的 jsAttr 没有叠上 escAttr' % rel


# ---------- ② 转义语义对照 ----------

class _Attrs(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.attrs = []

    def handle_starttag(self, tag, attrs):
        self.attrs.extend(attrs)


def _esc_js(s):
    """等价于 JS 的 esc()：只转 & < >"""
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _esc_attr_js(s):
    """等价于 escAttr()"""
    return _esc_js(s).replace('"', '&quot;')


def _safe_str_js(s):
    """等价于 safeStr()：只转反斜杠与单引号"""
    return s.replace('\\', '\\\\').replace("'", "\\'")


def test_esc_alone_is_injectable_but_esc_attr_is_not():
    """★ 对照：只 esc 会被注入、escAttr 不会，且属性值原样取回"""
    payload = '" onmouseover="alert(1)'

    g1 = _Attrs()
    g1.feed('<div title="%s">' % _esc_js(payload))
    assert 'onmouseover' in [k for k, _ in g1.attrs], \
        '对照失败：只做 esc 竟然没被注入？attrs=%r' % (g1.attrs,)

    g2 = _Attrs()
    g2.feed('<div title="%s">' % _esc_attr_js(payload))
    assert 'onmouseover' not in [k for k, _ in g2.attrs], 'escAttr 之后仍被注入：%r' % (g2.attrs,)
    assert dict(g2.attrs).get('title') == payload, '属性值被改变：%r' % (dict(g2.attrs),)


def test_safe_str_alone_is_injectable_but_js_attr_is_not():
    """★ 对照（onclick 那种位置）：只过 safeStr 仍会被注入，jsAttr 不会

    这条覆盖 `onclick="f('…')"` 这种"属性里的 JS 字符串"—— 必须两道都过。
    """
    payload = "x')\" onmouseover=\"alert(1)"

    g1 = _Attrs()
    g1.feed('<div onclick="f(\'%s\')">' % _safe_str_js(payload))
    assert 'onmouseover' in [k for k, _ in g1.attrs], \
        '对照失败：只做 safeStr 竟然没被注入？attrs=%r' % (g1.attrs,)

    g2 = _Attrs()
    g2.feed('<div onclick="f(\'%s\')">' % _esc_attr_js(_safe_str_js(payload)))
    assert 'onmouseover' not in [k for k, _ in g2.attrs], 'jsAttr 之后仍被注入：%r' % (g2.attrs,)


def test_esc_attr_keeps_ampersand_safe():
    """`&` 不能被二次转义：payload 含 `&quot;` 时应当原样还原"""
    payload = 'dn=a&quot;b'
    g = _Attrs()
    g.feed('<div title="%s">' % _esc_attr_js(payload))
    assert dict(g.attrs).get('title') == payload, '含 & 的载荷被改变了：%r' % (dict(g.attrs),)
