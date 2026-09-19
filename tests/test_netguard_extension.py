# -*- coding: utf-8 -*-
"""安卓内置扩展 `netguard`：只放行本机网络请求（2026-09-19，用户批「都要」的第二条）

**为什么要有它**：页面上的**子资源**（`<img src>`、外链脚本/样式、fetch/XHR、WebSocket）
不走 GeckoView 的 `NavigationDelegate` —— `onLoadRequest`（2026-09-18 做的）只管**导航**。
所以"软件内不访问外部"在客户端这一侧一直缺一块。服务端已经补了页面 CSP，但那依赖
"页面是我们发的"；这一层是**深度防御**。

⚠️ **安卓代码在本机不能编译**（构建是用户的活），所以这里全是**静态断言** ——
它钉的是"结构在位、口径一致"，**不能替代真机验证**。真机那一步的验证办法：
临时在一个页面里插一行外部引用（指向 PC 的局域网地址）、构建、看图片位置是否空白、
以及 App 日志里有没有 `netguard 拦截外部请求`。

⚠️ **`webRequestBlocking` 在内置扩展里到底给不给用，截至写这段时仍未经实测** ——
`manifest.json` 里先按这个写；真机若报缺权限，就改用 `declarativeNetRequest`。
"""
import json
import os

import leaffs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(leaffs.__file__)))
ASSETS = 'android/app/src/main/assets/netguard'
MANIFEST = ASSETS + '/manifest.json'
BACKGROUND = ASSETS + '/background.js'
MAIN_ACTIVITY = 'android/app/src/main/java/com/leaffs/mobile/MainActivity.kt'
APP_PY = 'leaffs/app.py'


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


# ---------- 扩展文件本身 ----------

def test_extension_files_exist():
    assert os.path.isfile(os.path.join(ROOT, MANIFEST)), '缺 manifest.json'
    assert os.path.isfile(os.path.join(ROOT, BACKGROUND)), '缺 background.js'


def test_manifest_declares_blocking_permissions():
    """拦截要 `webRequest` + `webRequestBlocking`，而且要能看所有 URL"""
    m = json.loads(_read(MANIFEST))
    perms = m.get('permissions', [])
    assert 'webRequest' in perms, '没有 webRequest 权限'
    assert 'webRequestBlocking' in perms, '没有 webRequestBlocking 权限（拦不住）'
    assert '<all_urls>' in perms, '没有 <all_urls>，看不到全部请求'
    assert 'geckoViewAddons' in perms, '少了 geckoViewAddons（GeckoView 内置扩展要它）'
    # 扩展要有稳定 id，否则每次安装都可能被当成新扩展
    assert m.get('browser_specific_settings', {}).get('gecko', {}).get('id'), '没有 gecko id'
    assert m.get('background', {}).get('scripts'), '没有 background script'


def test_background_blocks_by_default():
    """★ 拦的判据必须是"只放行本机"，其余一律 cancel（fail-closed）"""
    src = _read(BACKGROUND)
    assert 'onBeforeRequest' in src, '没挂 onBeforeRequest'
    assert 'cancel: true' in src, '没有 cancel —— 拦不住'
    assert "['blocking']" in src, '没有声明 blocking'
    # 解析不出 host 时必须拒绝，不能放行
    assert 'return false;' in src, '解析失败的分支没有按不可信处理'


# ---------- 口径必须与 App 侧一致 ----------

def test_guard_whitelist_matches_is_internal_url():
    """★ 扩展放行的 host 必须与 `app._is_internal_webview_url` 的口径**一致**

    不一致会出怪事：导航允许、子资源被拦（或反过来）—— 同一个地址两种待遇，
    查起来会很费劲。
    """
    src = _read(BACKGROUND)
    for host in ('127.0.0.1', 'localhost', '[::1]'):
        assert host in src, '扩展白名单漏了 %s' % host
    # App 那份的放行名单同样要有这三项（它另有 data:/about:/blob:，那些不走网络请求）
    app = _read(APP_PY)
    for host in ("'127.0.0.1'", "'localhost'", "'[::1]'"):
        assert host in app, 'isInternalUrl 白名单漏了 %s' % host


# ---------- Kotlin 侧的注册 ----------

def test_extension_registered_in_main_activity():
    src = _read(MAIN_ACTIVITY)
    assert 'private const val NETGUARD_EXTENSION' in src, '没有扩展路径常量'
    assert 'resource://android/assets/netguard/' in src, '扩展路径不对'
    assert 'private fun setupNetGuardExtension()' in src, '没有注册函数'
    # 必须真的被调用（定义在那儿没人调 = 扩展根本装不上）
    assert src.count('setupNetGuardExtension()') >= 2, '只有定义、没有调用'


def test_background_delegate_uses_extension_path():
    """★ background script 的消息要走 `ext.setMessageDelegate`

    ⚠️ insets 那边踩过：content script 的消息必须走 `session.webExtensionController`，
    而 `extension.setMessageDelegate` 只收 **background script** 的消息。挂错了不会报错，
    只是消息被静默丢弃（那边的注释写着 `releasePendingMessages: session=null`）。
    netguard 是 background script，所以用 `ext.` 那条；这里钉住别再挂错。
    """
    src = _read(MAIN_ACTIVITY)
    i = src.index('private fun setupNetGuardExtension()')
    body = src[i:i + 1200]
    assert 'ext.setMessageDelegate(' in body, '没有走 ext.setMessageDelegate'
    assert 'session.webExtensionController' not in body, \
        'background script 挂到了 session 那条路径上（消息会被静默丢弃）'


def test_delegate_implements_on_connect():
    """★ `onConnect` 必须实现 —— 扩展用 `runtime.connectNative` 建 Port，App 不接就建不起来，
    那样"我起来了 / 我拦了谁"两条上报全都没有，真机上就没法判断到底生效了没有。"""
    src = _read(MAIN_ACTIVITY)
    i = src.index('private fun netGuardMessageDelegate()')
    body = src[i:i + 1200]
    assert 'override fun onConnect(' in body, '没有实现 onConnect —— Port 建不起来'
    assert 'override fun onMessage(' in body, '没有实现 onMessage'
    assert 'netguard' in body.lower(), '没看到与被拦相关的日志文案'
