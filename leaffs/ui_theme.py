# -*- coding: utf-8 -*-
"""原生 UI 配色桥 —— 让安卓壳的原生控件与网页设计完全同源。

网页的全部配色都定义在 web_page/common/style.css（唯一真源）：
    :root                                            基础（浅色 + 默认蓝）
    [data-theme="dark"]                              暗色覆盖
    :root:not([data-theme="dark"])[data-accent="X"]  浅色 + 自选主色
    [data-theme="dark"][data-accent="X"]             暗色 + 自选主色

本模块按当前 (theme, accent) 做一次「层叠合并」，得到网页此刻真实生效的颜色，
再折算成原生控件可直接使用的不透明 ARGB（半透明的 card-bg / border 会与背景合成，
因为原生控件下面没有网页可透出）。
"""
import os
import re
import threading

from leaffs.paths import BASE_DIR
from leaffs.runtime_log import add_log

_CSS_PATH = os.path.join(BASE_DIR, 'web_page', 'common', 'style.css')

# 可换的主题主色（空字符串 = 默认蓝）；网页侧的白名单也用它（render._cookie_accent）
ACCENTS = ('green', 'purple', 'orange', 'red')

_BLOCK_RE = re.compile(r'([^{}]+)\{([^{}]*)\}', re.S)
_VAR_RE = re.compile(r'(--[\w-]+)\s*:\s*([^;]+);')
_ACCENT_SEL_RE = re.compile(r'\[data-accent="([^"]*)"\]')

_lock = threading.Lock()
_cache = {'mtime': None, 'text': ''}
# 解析结果缓存：键为 (是否暗色, 主色, css mtime)。原生每次弹窗前都会现读配色，
# 有这层缓存后重复读取只是字典查找，开销可忽略。
_palette_cache = {}


def _css_text():
    """读取 style.css（带 mtime 缓存；读不到返回空串）"""
    try:
        st = os.stat(_CSS_PATH)
    except OSError as e:
        add_log('读取网页样式失败: %s' % e, 'warn')
        return ''
    with _lock:
        if _cache['mtime'] != st.st_mtime:
            try:
                with open(_CSS_PATH, 'r', encoding='utf-8') as f:
                    _cache['text'] = f.read()
                _cache['mtime'] = st.st_mtime
            except OSError as e:
                add_log('读取网页样式失败: %s' % e, 'warn')
                return _cache['text']
        return _cache['text']


def _one_applies(sel, dark, accent):
    if not sel:
        return False
    s = sel
    # :not([data-theme="dark"]) —— 仅浅色生效（要先剥掉，否则会被下面的 dark 判定误伤）
    if ':not([data-theme="dark"])' in s:
        if dark:
            return False
        s = s.replace(':not([data-theme="dark"])', '')
    # 明示暗色块
    if '[data-theme="dark"]' in s and not dark:
        return False
    m = _ACCENT_SEL_RE.search(s)
    if m:
        return m.group(1) == accent
    return True


def _selector_applies(sel, dark, accent):
    return any(_one_applies(part.strip(), dark, accent) for part in sel.split(','))


def palette(theme='light', accent=''):
    """按当前主题/主色合并出 CSS 变量表（后出现的块覆盖先出现的，与 CSS 层叠一致）"""
    dark = (theme == 'dark')
    accent = accent if accent in ACCENTS else ''
    text = _css_text()
    if not text:
        return {}
    key = (dark, accent, _cache['mtime'])
    hit = _palette_cache.get(key)
    if hit is not None:
        return hit
    out = {}
    for sel, body in _BLOCK_RE.findall(text):
        if not _selector_applies(sel, dark, accent):
            continue
        for var_key, value in _VAR_RE.findall(body):
            out[var_key] = value.strip()
    # 只保留最近几组（主题×主色组合有限）
    if len(_palette_cache) > 16:
        _palette_cache.clear()
    _palette_cache[key] = out
    return out


def _parse_color(value):
    """#rgb / #rrggbb / #rrggbbaa / rgb() / rgba() → (r, g, b, a)"""
    if not value:
        return None
    v = value.strip()
    if v.startswith('#'):
        h = v[1:]
        if len(h) == 3:
            h = ''.join(c * 2 for c in h)
        if len(h) == 6:
            h += 'ff'
        if len(h) == 8:
            try:
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16),
                        int(h[6:8], 16) / 255.0)
            except ValueError:
                return None
        return None
    m = re.match(r'rgba?\(([^)]+)\)', v)
    if m:
        parts = [p.strip() for p in m.group(1).split(',')]
        if len(parts) >= 3:
            try:
                r, g, b = (int(round(float(x))) for x in parts[:3])
                a = float(parts[3]) if len(parts) > 3 else 1.0
                return (r, g, b, a)
            except ValueError:
                return None
    return None


def _flatten(color, background):
    """把带透明度的颜色压到底色上（原生控件下方没有网页可透出）"""
    if color is None:
        return background[:3]
    r, g, b, a = color
    if a >= 1.0:
        return (r, g, b)
    br, bg_, bb = background[:3]
    return (int(round(r * a + br * (1 - a))),
            int(round(g * a + bg_ * (1 - a))),
            int(round(b * a + bb * (1 - a))))


def _argb(rgb, alpha=255):
    r, g, b = rgb
    return '#%02X%02X%02X%02X' % (alpha, r, g, b)


def _argb_keep_alpha(color, fallback_rgb, fallback_alpha):
    """保留 CSS 里的透明度。

    网页是玻璃拟态：卡片 background-color 用半透明 --card-bg，再叠 backdrop-filter 模糊，
    遮罩 --modal-overlay 也是半透明（所以能看到背景的星点）。压成不透明就全毁了，
    因此这类值必须原样保留 alpha。
    """
    if color is None:
        return _argb(fallback_rgb, int(round(fallback_alpha * 255)))
    r, g, b, a = color
    return _argb((r, g, b), int(round(a * 255)))


def _px_to_dp(css_value, fallback):
    """--radius: 18px → 18（原生按 dp 用）"""
    if not css_value:
        return fallback
    m = re.match(r'([\d.]+)px', css_value.strip())
    if m:
        try:
            return int(round(float(m.group(1))))
        except ValueError:
            return fallback
    return fallback


def _palette_colors(theme, accent):
    """按指定主题算出整套原生配色（ARGB 字符串）+ 圆角（dp）；样式不可用时 None。

    单独抽出来，是为了让 native_colors 能再算一遍"另一个主题"而不递归。
    """
    p = palette(theme, accent)
    if not p:
        return None

    bg = _parse_color(p.get('--bg')) or (255, 255, 255, 1.0)
    bg_rgb = bg[:3]

    def opaque(key, default_rgb):
        return _flatten(_parse_color(p.get(key)), bg)

    primary = _parse_color(p.get('--primary'))
    primary_hover = _parse_color(p.get('--primary-hover'))
    danger = _parse_color(p.get('--danger'))
    success = _parse_color(p.get('--success'))

    # 卡片星点用色：优先网页 body 的星点色；但浅色主题下 --star 是全透明的，
    # 直接用会让卡片完全没有点缀，因此浅色时退回一个淡色（--text-light 低透明度）。
    star = _parse_color(p.get('--star'))
    if star is None or star[3] <= 0.05:
        tl = _parse_color(p.get('--text-light')) or (124, 138, 176, 1.0)
        star = (tl[0], tl[1], tl[2], 0.38)

    return {
        'ok': True,
        'theme': 'dark' if theme == 'dark' else 'light',
        'accent': accent if accent in ACCENTS else '',
        'primary': _argb(primary[:3] if primary else (14, 165, 233)),
        'primaryHover': _argb(primary_hover[:3] if primary_hover else
                              (primary[:3] if primary else (2, 132, 199))),
        'bg': _argb(bg_rgb),
        'card': _argb(opaque('--card-bg', (255, 255, 255))),
        'text': _argb(opaque('--text', (34, 48, 92))),
        'textStrong': _argb(opaque('--text', (34, 48, 92))),
        'textSecondary': _argb(opaque('--text-secondary', (68, 82, 127))),
        'textLight': _argb(opaque('--text-light', (124, 138, 176))),
        'border': _argb(opaque('--border', (128, 148, 208))),
        'hover': _argb(opaque('--hover-bg', (110, 140, 255))),
        'danger': _argb(danger[:3] if danger else (242, 101, 139)),
        'success': _argb(success[:3] if success else (47, 191, 143)),
        'radius': _px_to_dp(p.get('--radius'), 18),
        'radiusSm': _px_to_dp(p.get('--radius-sm'), 10),

        # ---- 玻璃拟态：这些必须保留原始透明度（原生不能压成不透明色块）----
        'cardRaw': _argb_keep_alpha(_parse_color(p.get('--card-bg')),
                                    (255, 255, 255), 0.62),
        'glassRaw': _argb_keep_alpha(_parse_color(p.get('--glass-bg')),
                                     (255, 255, 255), 0.45),
        'hoverRaw': _argb_keep_alpha(_parse_color(p.get('--hover-bg')),
                                     (110, 140, 255), 0.09),
        # 卡片边缘高光（网页用 4 个径向渐变叠出玻璃反光）
        'edge1': _argb_keep_alpha(_parse_color(p.get('--card-edge1')),
                                  (3, 105, 161), 0.12),
        'edge2': _argb_keep_alpha(_parse_color(p.get('--card-edge2')),
                                  (70, 150, 220), 0.10),
        # 背景装饰：光晕球与星点（暗色主题下星星很明显）
        'star': _argb_keep_alpha(star, (225, 232, 255), 0.85),
        'orb1': _argb_keep_alpha(_parse_color(p.get('--orb1')),
                                 (150, 190, 255), 0.55),
        'orb2': _argb_keep_alpha(_parse_color(p.get('--orb2')),
                                 (150, 225, 255), 0.50),
        'orb3': _argb_keep_alpha(_parse_color(p.get('--orb3')),
                                 (150, 190, 255), 0.35),
        # 玻璃模糊半径（--glass-blur，原生在 API 31+ 可用于真实背景模糊）
        'blur': _px_to_dp(p.get('--glass-blur'), 18),
    }


def native_colors(theme='light', accent=''):
    """给原生控件的配色（ARGB 字符串）+ 圆角（dp）。

    返回 None 表示样式文件不可用（调用方应回退到内置的 values/themes.xml 配色）。

    额外附带 `cardPalette` = **另一个主题**的整套配色：原生卡片没有遮罩，靠
    "亮主题配暗卡、暗主题配亮卡"的明暗反差把自己从页面里拎出来，所以卡片上的
    每个颜色都取反主题那一套（值仍来自同一份 style.css，不另造色）。
    """
    theme = 'dark' if theme == 'dark' else 'light'
    colors = _palette_colors(theme, accent)
    if not colors:
        return None
    other = _palette_colors('light' if theme == 'dark' else 'dark', accent)
    if other:
        colors['cardPalette'] = other
    return colors


def current(theme='light', accent=''):
    """当前应使用的原生配色 = 样式真源 + 网页此刻的主题（亮暗 + 主色）。

    主题的真源是网页的客户端本地存储（cookie）；安卓壳由内置扩展读到
    <html data-theme / data-accent> 后经 leaffs_mobile.set_theme 转进来，
    网页还没加载时用 App 上次记住的值。非法 accent 由 palette() 自动忽略。
    返回 None 表示样式文件不可用（调用方回退到内置配色）。
    """
    return native_colors('dark' if theme == 'dark' else 'light', accent)


def error_page(title='页面加载失败', message='共享服务可能还没就绪，或网络已断开。',
               retry_url='/', theme='', accent=''):
    """生成与网页视觉完全一致的内嵌错误页。

    直接内联网页自己的 style.css，并复用 .modal-overlay / .modal-content / .btn 结构，
    因此背景的星点光晕、玻璃卡片、按钮样式与网页同源，不会出现"另一个世界"的观感。
    由 App 以 data: URL 展示（不依赖服务是否已经起来）。
    """
    colors = current(theme, accent) or {}
    theme = theme or colors.get('theme', 'light')
    accent = accent or colors.get('accent', '')
    attrs = ''
    if theme == 'dark':
        attrs += ' data-theme="dark"'
    if accent:
        attrs += ' data-accent="%s"' % accent
    return (
        '<!DOCTYPE html><html lang="zh-CN"%s><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<style>%s</style></head><body>'
        '<div class="modal-overlay active">'
        '<div class="modal-content" style="padding:22px 20px;text-align:center;max-width:320px">'
        '<div style="font-size:15px;font-weight:700;color:var(--text);margin-bottom:8px">%s</div>'
        '<div style="font-size:13px;color:var(--text-light);line-height:1.7;margin-bottom:16px">%s</div>'
        '<a class="btn" href="%s" style="justify-content:center">重试</a>'
        '</div></div></body></html>'
    ) % (attrs, _css_text(), title, message, retry_url)

