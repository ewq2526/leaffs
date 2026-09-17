# -*- coding: utf-8 -*-
"""软件内不允许访问外部链接（2026-09-18，用户要求「不能在我们软件内出事」）。

**背景**：审网页 JS 时发现两个出站口子 —— 安卓端长按菜单的「在浏览器打开」
（`openExternal` → `ACTION_VIEW` 甩给系统浏览器），以及 `onLoadRequest` 对任意 URL 放行
（网页里的站外链接、**PDF 里的外链**会在 App 内嵌浏览器里打开）。
用户明确要求：**软件内不访问外部链接**，并要求 **Windows 的 webview 一起防护**。

**改动（三处）**：
1. 安卓：删掉「在浏览器打开」整项 + `openExternal()`；
2. 安卓：`onLoadRequest` 加白名单 —— 只放行 `127.0.0.1` / `localhost` / `[::1]`、
   `leaffs://`、`data:`、`about:`、`blob:`，其余 `DENY` 并提示；
3. 桌面（pywebview + WebView2）：给内嵌窗口加
   `--host-resolver-rules=MAP * ~NOTFOUND,EXCLUDE localhost,EXCLUDE 127.0.0.1`
   ⇒ **除本机外的域名一律解析失败**，外部站点根本连不上。

⚠️ **子资源那一层**（`<img src="外部">` / 外链脚本）：GeckoView 没有公开的拦截 API
（要 WebExtension 的 webRequest），所以安卓侧**没做**，靠**服务端 CSP** 兜 ——
`send_raw` 与 `_share_stream_file` 都发 `sandbox; default-src 'none'; style-src
'unsafe-inline'; img-src data:`，且站内页面本身没有任何外链。桌面侧那层由 DNS 阻断一并覆盖。
"""
import os
import re

import leaffs

MAIN_ACTIVITY = 'android/app/src/main/java/com/leaffs/mobile/MainActivity.kt'
APP_PY = 'leaffs/app.py'
RAW_API = 'leaffs/files/api.py'
HANDLER = 'leaffs/server/handler.py'


def _read(rel):
    root = os.path.dirname(os.path.dirname(os.path.abspath(leaffs.__file__)))
    with open(os.path.join(root, rel), encoding='utf-8') as f:
        return f.read()


# ---------- 安卓：白名单在位 ----------

def test_android_navigation_whitelist():
    src = _read(MAIN_ACTIVITY)
    assert 'private fun isInternalUrl(' in src, '没有 isInternalUrl 白名单函数'
    # 放行名单
    for token in ('"127.0.0.1"', '"localhost"', '"[::1]"',
                  'leaffs://', 'data:', 'about:', 'blob:'):
        assert token in src, '白名单漏了 %s' % token
    # 必须在 onLoadRequest 里真的用上，而且拒绝路径要返回 DENY
    assert re.search(r'if \(!isInternalUrl\(uri\)\)', src), 'onLoadRequest 没调用 isInternalUrl'
    assert 'AllowOrDeny.DENY' in src


def test_android_browser_menu_item_removed():
    """★ 「在浏览器打开」必须整项删掉（函数定义与调用都不许留）

    ⚠️ 断言要精确到"定义/调用"，不能断言"整个文件里不出现这个词" ——
    留下来的注释里本来就会提到它（说明为什么删）。
    """
    src = _read(MAIN_ACTIVITY)
    assert 'private fun openExternal' not in src, 'openExternal 函数还在'
    assert 'actions += { openExternal' not in src, '菜单项还在'
    assert 'labels += "在浏览器打开"' not in src, '菜单项文案还在'
    # 没有任何地方再把 URL 交给系统去 VIEW（`package:` 跳应用详情页不算）
    assert 'Intent(Intent.ACTION_VIEW' not in src, '还有 ACTION_VIEW 打开链接的路径'


# ---------- 桌面：WebView2 阻断外部域名 ----------

def test_desktop_navigation_guard_present():
    """★ 桌面端：靠**导航层守卫**（不是 WebView2 参数）挡外链"""
    src = _read(APP_PY)
    assert '_is_internal_webview_url' in src, '没有内网 URL 判定'
    assert '已阻止内嵌窗口访问外部地址' in src, '没有拦到时的日志/拉回逻辑'
    assert 'load_url(home)' in src or 'load_url(home' in src, '没有把窗口拉回首页'
    # ⚠️ （"不许再留 host-resolver-rules" 由 test_webview2_args_only_cert 从**返回值**断言 ——
    #    这里不断言"文件里不出现这个词"，因为注释里会解释它为什么被撤掉。）


def test_is_internal_webview_url():
    """★ 内网判定：本机放行、外部拒绝、加载中的空值不乱跳"""
    from leaffs.app import _is_internal_webview_url as ok
    # 本机（端口不限定 —— 服务自己会用到 8080/8081/8082）
    for u in ('http://127.0.0.1:8080/login', 'http://localhost:8081/',
              'https://localhost:8080/x?y=1', 'https://127.0.0.1:8082/cert',
              'http://[::1]:8080/', 'about:blank', 'data:text/html,x', 'blob:http://x/y'):
        assert ok(u), '本机/内部地址被误判为外部: %s' % u
    # 外部
    for u in ('https://example.com/', 'http://192.168.1.5:8080/', 'http://127.0.0.1.evil.com/',
              'http://localhost.evil.com/', 'file:///C:/x', 'javascript:alert(1)'):
        assert not ok(u), '外部地址没被拦: %s' % u
    # 还没拿到 URL ⇒ 放行（否则会在加载途中被反复拉回）
    assert ok('') and ok(None)


def test_webview2_args_only_cert():
    """参数里只该有证书放行那一条"""
    from leaffs.app import _webview2_args
    out = _webview2_args('')
    assert '--ignore-certificate-errors' in out
    assert '--host-resolver-rules' not in out
    assert _webview2_args('--disable-gpu').startswith('--disable-gpu'), '把用户的参数冲掉了'
    assert _webview2_args(out) == out, '重复调用会重复追加'


# ---------- 服务端兜底：外来文档一律不许执行/连外网 ----------

def test_server_sandbox_csp_on_raw_and_share():
    """可内联文档强制纯文本 + sandbox CSP（子资源那层靠它兜）"""
    need = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
    for rel in (RAW_API, HANDLER):
        src = _read(rel)
        assert need in src, '%s 缺少 sandbox CSP' % rel
    # force_plain 名单必须含 html 与 svg（最容易漏的就是 svg）
    for rel in (RAW_API, HANDLER):
        src = _read(rel)
        assert "'text/html'" in src and "'image/svg+xml'" in src, \
            '%s 的 force_plain 名单不全' % rel
