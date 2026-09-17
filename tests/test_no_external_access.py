# -*- coding: utf-8 -*-
"""软件内不允许访问外部链接（2026-09-18，用户要求「不能在我们软件内出事」）。

**背景**：审网页 JS 时发现两个出站口子 —— 安卓端长按菜单的「在浏览器打开」
（`openExternal` → `ACTION_VIEW` 甩给系统浏览器），以及 `onLoadRequest` 对任意 URL 放行
（网页里的站外链接、**PDF 里的外链**会在 App 内嵌浏览器里打开）。
用户明确要求：**软件内不访问外部链接**，并要求 **Windows 的 webview 一起防护**。

**安卓侧（两处）**：
1. 删掉「在浏览器打开」整项 + `openExternal()`；
2. `onLoadRequest` 加白名单 —— 只放行 `127.0.0.1` / `localhost` / `[::1]`、
   `leaffs://`、`data:`、`about:`、`blob:`，其余 `DENY` 并提示。

**桌面侧（2026-09-18 晚，升级 pywebview 4.2.2 → 6.2.1 后重做）**：
三层，全在 `_install_webview_guard()` 里，都走 WebView2 官方接口：
导航层 `NavigationStarting` 取消外链、网络层 `WebResourceRequested` 给外部请求塞空响应、
新窗口 `NewWindowRequested` 自己接管。
⚠️ 每一层的行为都用**真窗口实测**过（`.cache/probe_pwv6_*.py`），不是照文档抄的 ——
本文件只做静态断言，行为验证看那些探针。

⚠️ **子资源那一层**：安卓侧**没做**（GeckoView 没有公开的拦截 API，要 WebExtension 的
webRequest），靠**服务端 CSP** 兜 —— `send_raw` 与 `_share_stream_file` 都发
`sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:`，且站内页面本身
没有任何外链。桌面侧这一层**做了**（`WebResourceRequested`，实测真的不发包）。
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


# ---------- 桌面：三层出站守卫 ----------

def test_desktop_guard_three_layers():
    """★ 桌面端三层守卫都在位：导航取消 + 网络阻断 + 接管新窗口

    这里断言的每一条都有对应的实测（.cache/probe_pwv6_*.py）：
      - 只挂 CoreWebView2 级 `NavigationStarting` **一级就够**（探针 F）；
      - `args.Cancel` 对**页面内**导航有效（探针 E 的 T1 页面内 JS、T2 链接点击），
        对 Python 侧 `win.load_url()` **无效**（T3）—— 但威胁来自页面内，且第 2 层会兜住内容；
      - `WebResourceRequested` 里设 `Response` 是**真的不发包**，不是发了包丢响应
        （探针 D：本机 8123 监听端一个请求都没收到）；
      - UI 线程里能把 pywebview 自己挂的 `on_new_window_request` 摘掉（探针 F）——
        不摘的话它会 `webbrowser.open()` 把外链甩给系统浏览器。
    """
    src = _read(APP_PY)
    assert 'def _install_webview_guard(' in src, '没有守卫装配函数'

    # (1) 三条 settings —— 少一条都漏
    assert "webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = False" in src, \
        '没关掉「target=_blank 甩给系统浏览器」'
    assert "webview.settings['ALLOW_DOWNLOADS'] = False" in src, '没关下载'
    assert "webview.settings['IGNORE_SSL_ERRORS'] = True" in src, \
        '没用官方开关放行本机自签证书'

    # (2) 导航层
    assert 'cwv2.NavigationStarting += _on_navigation_starting' in src, '没挂导航层'
    assert 'args.Cancel = True' in src, '导航层没有取消动作'

    # (3) 网络层
    assert 'cwv2.WebResourceRequested += _on_web_resource_requested' in src, '没挂网络层'
    assert 'CreateWebResourceResponse' in src, '网络层没有塞空响应'

    # (4) 新窗口：必须先摘掉 pywebview 自己的，否则它先执行就晚了
    assert 'cwv2.NewWindowRequested -= old_handler' in src, '没摘掉 pywebview 自己的新窗口处理'
    assert 'cwv2.NewWindowRequested += _on_new_window_requested' in src, '没接管新窗口请求'
    assert 'args.set_Handled(True)' in src, '接管后没有 Handled'

    # (5) 判定规则仍然复用同一个函数（安卓/桌面共用一套白名单语义）
    assert '_is_internal_webview_url' in src, '没有内网 URL 判定'


def test_desktop_guard_installed_on_ui_thread():
    """★ 装配必须发生在 UI 线程的回调里 —— CoreWebView2 只能从 UI 线程碰

    实测教训（探针 E）：在 `webview.start()` 起的 worker 线程里访问
    `wv.CoreWebView2` 会抛
    `InvalidOperationException: CoreWebView2 can only be accessed from the UI thread.`
    所以装配只能写在 `CoreWebView2InitializationCompleted` 的回调里。
    """
    src = _read(APP_PY)
    assert 'wv.CoreWebView2InitializationCompleted += _on_core_ready' in src, \
        '没有在初始化完成回调里装配'
    assert '_install_webview_guard(cwv2, win)' in src, '装配调用不在回调里'
    # ⚠️ 等的是 native.webview，不是 native —— native 比 .webview 早赋值（winforms.py 195 vs 281）
    assert "getattr(native, 'webview', None)" in src, \
        '等窗口时用了 native 而不是 native.webview（会拿到 None）'


def test_desktop_guard_no_polling_left():
    """★ 旧的「0.4 秒轮询 URL 再拉回」必须整个删掉，也不能再有命令行 hack

    ⚠️ 这几条断言是"整个文件里不出现"，所以注释里也不能留这些词 ——
    已经确认删干净了。
    """
    src = _read(APP_PY)
    assert '_guard_external_navigation' not in src, '旧的轮询守卫线程还在'
    assert 'time.sleep(0.4)' not in src, '还有 0.4 秒轮询'
    assert '_webview2_args' not in src, '旧的命令行参数函数还在'
    assert 'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS' not in src, '还在塞环境变量传浏览器参数'
    assert '--host-resolver-rules' not in src, \
        '又把 host-resolver-rules 加回来了 —— 它被 WebView2 过滤，实测无效'


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


# ---------- 服务端兜底：外来文档一律不许执行/连外网 ----------

def test_server_sandbox_csp_on_raw_and_share():
    """可内联文档强制纯文本 + sandbox CSP（安卓侧子资源那层靠它兜）"""
    need = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
    for rel in (RAW_API, HANDLER):
        src = _read(rel)
        assert need in src, '%s 缺少 sandbox CSP' % rel
    # force_plain 名单必须含 html 与 svg（最容易漏的就是 svg）
    for rel in (RAW_API, HANDLER):
        src = _read(rel)
        assert "'text/html'" in src and "'image/svg+xml'" in src, \
            '%s 的 force_plain 名单不全' % rel
