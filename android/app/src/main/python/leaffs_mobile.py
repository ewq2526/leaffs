# -*- coding: utf-8 -*-
"""LeafFS 安卓入口 —— 由 App(Kotlin) 经 Chaquopy 调用的服务器编排。

与桌面 leaffs.app.start_server 的区别（M1 版）：
  * 不拉起 webview/浏览器窗口、不阻塞主线程（Kotlin 管理生命周期）；
  * 不启动 aria2c 下载守护与守护监控（安卓无 aria2c：直链/上传/浏览不受影响，
    代码自身对缺失 exe 优雅降级，无需改动桌面源码）；
  * 证书提示页（8082）与 TLS 都与桌面**同一套**：TLS 开关/证书来源由
    config/server_config.json 与 leaffs.server.tls 决定（安卓没有随包的 openssl.exe，
    自签证书走 Python 生成那条分支，规格与桌面一致）；
  * 保留：HTTP + WebSocket + 管理页推送 + 冻结看门狗。

路径：
  * 资源（web_page、config/all.txt）= 随 APK 打包、由 Chaquopy 解包的 leaffs 包目录，
    leaffs.paths 的 BASE_DIR 自动解析到那里，无需改动；
  * 可写数据（config/账号/共享目录/缓存）= $HOME/leaffs_home。
    $HOME 由 Chaquopy 指向 App 私有 files 目录；本模块在任何 leaffs 模块导入前设置
    LEAFFS_PROJECT_ROOT，paths.py 即按环境变量定位数据根（现有机制，零改动）。
"""
import os
import json
import socket
import threading

_HOME = os.environ.get('HOME', '')
if _HOME:
    os.environ.setdefault('LEAFFS_PROJECT_ROOT', os.path.join(_HOME, 'leaffs_home'))

import leaffs.auth.core as _ac
import leaffs.config.core as _cfg
import leaffs.files.core as _fs
import leaffs.server.push as _push
import leaffs.server.ws as _ws
import leaffs.server.handler as _http
import leaffs.server.tls as _tls
import leaffs.server.cert_remind as _cert_remind
import leaffs.auth.local_token as _lt
import leaffs.share.mappings as _mapping
from leaffs.dl.dl_core import set_broadcast_fn
from leaffs.runtime_log import setup_logging, add_log
from leaffs.watchdog import _wd_loop

# ---------- 对外可访问地址（管理页「本机 IP」/二维码、分享页链接）----------
# 安卓上不能靠 socket.gethostbyname(socket.gethostname())：主机名是机型名
# （实测 iQOO-Z11-Turbo），解析必然失败 → 恒得 127.0.0.1。
# **更不能只看网卡名**：有的 ROM 开热点时 AP 网卡叫 wlan1（同样以 wlan 开头），
# 按名字"wlan 优先"会把热点地址当成 Wi-Fi 地址返回（实测踩过），所以先问系统。
_lan_log_state = ['']

# 不作为对外地址的网卡：回环、蜂窝、VPN 与隧道、虚拟与直连
_IFACE_SKIP = ('lo', 'rmnet', 'ccmni', 'pdp', 'seth', 'tun', 'ppp', 'dummy',
               'p2p', 'wifi-aware')


def _lan_log(msg, level='info'):
    """只在结果变化时写日志 —— /api/stats 每秒刷新都会走到探测这里，无条件记录会刷屏。"""
    if _lan_log_state[0] == msg:
        return
    _lan_log_state[0] = msg
    try:
        add_log(msg, level)
    except Exception:
        pass


def _system_lan_address():
    """问系统要 Wi-Fi / 以太网的 IPv4；返回 (ip, 网卡名)，没有则 ('', '')。

    用 ConnectivityManager 的 WIFI/ETHERNET transport 判断，**不看网卡名**，
    所以不会把热点网卡（某些 ROM 叫 wlan1）误判成 Wi-Fi。热点（Tethering）不注册成
    Network，这里拿不到 —— 这是有意的：能拿到才说明它是"连热点的设备也能访问"的那个。
    """
    from java import jclass
    from com.chaquo.python import Python as _CPy
    ctx = _CPy.getPlatform().getApplication()
    cm = ctx.getSystemService('connectivity')
    NC = jclass('android.net.NetworkCapabilities')
    for net in cm.getAllNetworks():
        caps = cm.getNetworkCapabilities(net)
        if caps is None:
            continue
        if not (caps.hasTransport(NC.TRANSPORT_WIFI)
                or caps.hasTransport(NC.TRANSPORT_ETHERNET)):
            continue
        lp = cm.getLinkProperties(net)
        if lp is None:
            continue
        addrs = lp.getLinkAddresses()
        for j in range(addrs.size()):
            s = addrs.get(j).getAddress().getHostAddress() or ''
            if ':' in s or s.startswith('127.'):   # 只要 IPv4，去回环
                continue
            return s, (lp.getInterfaceName() or '?')
    return '', ''


def _lan_status():
    """返回 (对外地址, 是否有可用局域网)。

    优先级：Wi-Fi/以太网 → 热点网关 → 127.0.0.1。
    连着 Wi-Fi 时，连热点的设备也能访问这个 Wi-Fi 地址（包发给默认网关＝本机，
    而它就是本机地址，内核本地投递，连 IP 转发都不用），所以它是超集；热点网关
    地址则只有连热点的设备能用。只有蜂窝时给 127.0.0.1 并标记 has_lan=False，
    前端据此提示「无可用局域网」。
    """
    seen = []          # 枚举到的网卡（名=IP），排查用
    err = ''
    # 1) 先问系统：Wi-Fi / 以太网（不看网卡名，避免把热点的 wlan1 当成 Wi-Fi）
    try:
        ip, name = _system_lan_address()
        if ip:
            _lan_log('局域网地址: %s（%s，系统判定 Wi-Fi/以太网）' % (ip, name))
            return ip, True
    except Exception as e:
        err = repr(e)
    # 2) 系统里没有 Wi-Fi/以太网：枚举网卡 —— 此时唯一的非蜂窝地址就是热点网关
    try:
        from java.net import NetworkInterface
        from java.util import Collections
        # 不能直接遍历 getNetworkInterfaces()：它返回 java.util.Enumeration（接口），
        # 而 Chaquopy 调不了接口的抽象方法（实测报
        # NotImplementedError('java.util.Enumeration.nextElement is abstract ...')）。
        # Collections.list() 转成 ArrayList（具体类）后 size()/get() 才能正常调。
        ifaces = Collections.list(NetworkInterface.getNetworkInterfaces())
        for i in range(ifaces.size()):
            ni = ifaces.get(i)
            name = ni.getName() or ''
            if name.startswith(_IFACE_SKIP):
                continue
            try:
                if not ni.isUp():
                    continue
            except Exception:
                pass
            ip = ''
            addrs = Collections.list(ni.getInetAddresses())
            for j in range(addrs.size()):
                s = addrs.get(j).getHostAddress() or ''
                if ':' in s or s.startswith('127.'):   # 只要 IPv4，去回环
                    continue
                ip = s
                break
            seen.append('%s=%s' % (name, ip or '-'))
            if ip:
                _lan_log('局域网地址: %s（%s，没连 Wi-Fi 时的热点/其它网卡）' % (ip, name))
                return ip, True
    except Exception as e:
        err = (err + ' | ' if err else '') + repr(e)
    # 枚举不到网卡通常是缺局域网权限（安卓 16 起 NETWORK 被拦：NetworkInterface 只返回回环）；
    # 异常则多半是 Chaquopy 的 Java 桥没起来 —— 两种情况日志能直接分辨
    _lan_log('局域网地址: 无可用（只有回环）：网卡=[%s]%s'
             % (','.join(seen) or '未枚举到', (' 异常: ' + err) if err else ''), 'warn')
    return '127.0.0.1', False


def _all_local_ips():
    """本机全部网卡 IPv4（含蜂窝/热点/Wi-Fi），供 Host/Origin 白名单与证书 SAN 用。

    为什么不能复用 _lan_status()：那个只返回**唯一对外地址**（给二维码/管理页显示用），
    而白名单要的是"访问者可能用到的**所有**本机地址" —— 漏掉一个（比如开热点时的
    wlan1）就会把合法连接直接拒掉，而浏览器对 WS 被关闭不给任何提示，极难排查（踩过）。

    与 _lan_status() 的两点不同：
      * 不跳过 rmnet/蜂窝：蜂窝地址同样可能被访问（同运营商内网、或开热点时）；
      * 不挑优先级、也不看网卡名 —— 只要 isUp，全部 IPv4 都收。
    拿不到就返回空列表，由 collect_ips() 的 socket 探测与 lan_status() 兜底。
    """
    out = []
    try:
        from java.net import NetworkInterface
        from java.util import Collections
        # 同 _lan_status：Chaquopy 遍历不了 java.util.Enumeration，先转成 ArrayList
        ifaces = Collections.list(NetworkInterface.getNetworkInterfaces())
        for i in range(ifaces.size()):
            ni = ifaces.get(i)
            try:
                if not ni.isUp():
                    continue
            except Exception:
                pass
            addrs = Collections.list(ni.getInetAddresses())
            for j in range(addrs.size()):
                s = addrs.get(j).getHostAddress() or ''
                if ':' in s or s.startswith('127.'):   # 只要 IPv4；回环由 localhost 覆盖
                    continue
                if s not in out:
                    out.append(s)
    except Exception as e:
        # 只在结果变化时记（_lan_log 自带去重）：WS 每次握手都会走到这里
        _lan_log('本机地址枚举失败: %r' % (e,), 'warn')
    return out


try:
    from leaffs.server import hosts as _hosts_mod
    _hosts_mod.set_ip_provider(_lan_status)
    # 白名单/证书 SAN 要的是"全部本机地址"，不是"唯一对外地址" —— 两者分开注册
    _hosts_mod.set_ip_collector(_all_local_ips)
except Exception:
    pass

_lock = threading.Lock()
_state = {'running': False, 'error': ''}


def _json(**kw):
    return json.dumps(kw, ensure_ascii=False)


def _port_free(port):
    """端口是否可绑定（防 daemon 线程里启动失败无法回报）。

    必须带 SO_REUSEADDR：服务器本身允许地址复用（HTTPServer.allow_reuse_address），
    刚退出时残留的 TIME_WAIT 连接并不妨碍它绑定；探测时不带这个选项，就会把一堆
    TIME_WAIT 误判成"端口被占用"，于是刚退出再打开 App 必定启动失败。
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('0.0.0.0', port))
            return True
        finally:
            s.close()
    except Exception:
        return False


def _boot():
    """初始化 + 启动各服务线程（失败抛异常，由 start() 捕获回报）"""
    setup_logging()
    _cfg.load_config()
    if _cfg.get_tls_enabled():
        # TLS：与桌面同一套（证书来源见 leaffs.server.tls）。安卓没有随包的 openssl.exe，
        # 首次启动改由 Python（cryptography）签发自签服务器证书到 config/selfsigned.crt
        # + .key，之后复用。取不到证书就按安全策略 A 拒绝以明文启动 —— 与桌面一致。
        if _tls.get_tls_context() is None:
            raise RuntimeError('TLS 已启用但无法取得服务器证书（自签生成失败），'
                               '按安全策略拒绝以明文启动')
    _ac.load_users()
    _ac.start_session_cleanup()
    _fs.cleanup_orphan_thumbs()
    os.makedirs(_fs.UPLOAD_DIR, exist_ok=True)
    os.makedirs(os.path.join(_fs.UPLOAD_DIR, 'public'), exist_ok=True)
    set_broadcast_fn(_push.broadcast_download_update)
    _lt.reset_and_write()
    add_log('LeafFS 初始化完成（安卓）', 'ok')
    if not _port_free(_cfg.PORT):
        raise RuntimeError('端口 %s 被占用，无法启动 HTTP' % _cfg.PORT)
    if not _port_free(_cfg.WS_PORT):
        raise RuntimeError('端口 %s 被占用，无法启动 WebSocket' % _cfg.WS_PORT)
    threading.Thread(target=_http.run_http, daemon=True).start()
    threading.Thread(target=_ws.run_ws_sync, daemon=True).start()
    # 证书提示页（纯 HTTP，默认 8082）：与桌面同一模块、同一张页面；TLS 关着时整页不提供
    if _cfg.get_tls_enabled():
        threading.Thread(target=_cert_remind.run_cert_remind_http, daemon=True).start()
    threading.Thread(target=_push.admin_push_loop, daemon=True).start()
    threading.Thread(target=_wd_loop, daemon=True).start()
    add_log('HTTP %s / WS %s 已启动' % (_cfg.PORT, _cfg.WS_PORT), 'ok')


def start():
    """启动服务器（幂等）。返回 JSON 字符串：{ok, already, http_port, ws_port, error}"""
    with _lock:
        if _state['running']:
            return _json(ok=True, already=True,
                         http_port=_cfg.PORT, ws_port=_cfg.WS_PORT)
        try:
            _boot()
        except Exception as e:
            _state['error'] = '%s: %s' % (type(e).__name__, e)
            add_log('服务器启动失败: %s' % _state['error'], 'err')
            return _json(ok=False, error=_state['error'])
        _state['running'] = True
        _state['error'] = ''
    add_log('服务器运行中（http://0.0.0.0:%s）' % _cfg.PORT, 'ok')
    return _json(ok=True, already=False,
                 http_port=_cfg.PORT, ws_port=_cfg.WS_PORT)


def status():
    """返回 JSON 字符串：{ok, running, http_port, ws_port, error, upload_dir, local_token}"""
    with _lock:
        return _json(
            ok=True,
            running=_state['running'],
            http_port=(_cfg.PORT if _state['running'] else 0),
            ws_port=(_cfg.WS_PORT if _state['running'] else 0),
            # 服务端当前是 http 还是 https：App 侧据此拼 WebView 的 URL
            # （TLS 开启时 8080 就是 HTTPS，拼 http:// 会连不上，表现为"服务未就绪"）
            tls=_cfg.get_tls_enabled(),
            error=_state['error'],
            upload_dir=getattr(_fs, 'UPLOAD_DIR', ''),
            # 本机一次性登录令牌（用于 /login?leaf= 自动登录；未跑/已消费则为空）
            local_token=(_lt.get_current() if _state['running'] else ''),
        )


# ---------- 网页报来的主题（亮暗 + 主色）----------
# 主题的真源是网页的客户端本地存储（cookie），原生读不到 cookie —— 内置扩展在页面里读到
# <html data-theme / data-accent> 就上报 App，App 调 set_theme 转到这里。
# 这是原生侧唯一能看到主题的地方；网页还没加载时（权限页、错误页）用 App 上次记住的值。
_theme_state = {'dark': False, 'accent': ''}


def jlog(msg):
    """Java 侧（App）排查用的一行日志 —— 写进 leaffs.log。

    为什么不直接用 android.util.Log：那条路在真机上读不到（本项目没有 logcat 出口），
    而这条（`add_log`）本来就在用 —— 它还经 stderr 进 logcat 的 `python.stderr`。
    """
    try:
        add_log('java: ' + str(msg), 'info')
    except Exception:
        pass


def set_theme(dark, accent=''):
    """由 App 调用：网页当前的主题（亮暗 + 主色）。"""
    _theme_state['dark'] = bool(dark)
    _theme_state['accent'] = str(accent or '')
    return True


def _web_colors():
    """按网页此刻的主题取原生配色（来源见 _theme_state）。"""
    import leaffs.ui_theme as _ut
    return _ut.current('dark' if _theme_state['dark'] else 'light',
                       _theme_state['accent'])


def ui_theme():
    """原生 UI 配色 —— 与网页同一套样式真源 + 网页此刻的亮暗/主色。

    配色真源是网页的 web_page/common/style.css，叠加网页（经内置扩展）报来的主题，
    供安卓壳的原生控件（对话框、菜单等）使用，保证两端设计一致。
    返回 JSON：{ok, theme, accent, primary, bg, card, text, textLight, border, ...}
    """
    try:
        colors = _web_colors()
        if not colors:
            return _json(ok=False, error='网页样式文件不可用')
        return _json(**colors)
    except Exception as e:
        add_log('ui_theme 失败: %s: %s' % (type(e).__name__, e), 'warn')
        return _json(ok=False, error='%s: %s' % (type(e).__name__, e))


def error_page(retry_url='/'):
    """加载失败时展示的内嵌页面 —— 直接复用网页样式，与网页设计完全一致。

    以 data: URL 交给 GeckoView 显示，不依赖服务是否已就绪。
    """
    try:
        import leaffs.ui_theme as _ut
        return _ut.error_page(retry_url=retry_url or '/',
                              theme='dark' if _theme_state['dark'] else 'light',
                              accent=_theme_state['accent'])
    except Exception as e:
        add_log('error_page 失败: %s: %s' % (type(e).__name__, e), 'warn')
        return ('<!DOCTYPE html><html><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '</head><body style="font:14px sans-serif;padding:24px;text-align:center">'
                '<p>页面加载失败</p><p><a href="%s">重试</a></p></body></html>'
                % (retry_url or '/'))


def permission_page(label='附近的设备', since='Android 16', denied=False):
    """局域网权限说明页（data: URL 交给 GeckoView 显示，与 app 同一套 CSS）。

    与错误页同一套做法：内联网页 style.css、复用 .modal-overlay / .modal-content / .btn。
    按钮是 leaffs:// 链接 —— data: 页里调不到服务端，只能由 App 在 onLoadRequest
    里拦下来（在 data: 页点链接算用户手势，满足权限申请对时机的要求）。
    安全区由内置扩展注入（manifest 的 matches 已含 data:），App 侧另有兜底。

    denied=True：上一次申请没拿到 → 直接换成"去系统设置"的说明与按钮。
    """
    css = ''
    theme = 'light'
    accent = ''
    try:
        import leaffs.ui_theme as _ut
        css = _ut._css_text()
        colors = _web_colors() or {}
        theme = colors.get('theme', 'light')
        accent = colors.get('accent', '')
    except Exception as e:
        add_log('permission_page 取样式失败: %s: %s' % (type(e).__name__, e), 'warn')
    attrs = ''
    if theme == 'dark':
        attrs += ' data-theme="dark"'
    if accent:
        attrs += ' data-accent="%s"' % accent
    # 按钮都撑满宽度上下排（.btn 本身是 inline-flex，宽度会随文字长短不一）
    solid = 'display:flex;justify-content:center'
    ghost = ('display:flex;justify-content:center;margin-top:8px;background:transparent;'
             'border:1px solid var(--border,#dbe4f5);color:var(--text-light,#8a97bd)')
    if denied:
        # 被拒之后只给"去系统设置"这一条路：安卓只允许弹有限的几次，再放个"再试一次"
        # 要么弹不出来、要么跟"去设置"重复
        title = '没拿到「%s」权限' % label
        msg = ('需要到系统设置里手动打开。<br><br>'
               '路径：<b>设置 → 应用 → LeafFS → 权限 → %s</b><br><br>'
               '不打开的话，这台手机自己还能用，但局域网里别的设备连不上。' % label)
        buttons = ('<a class="btn" href="leaffs://settings" style="%s">去系统设置</a>'
                   '<a class="btn" href="leaffs://skip" style="%s">先不用</a>'
                   % (solid, ghost))
    else:
        title = '需要「%s」权限' % label
        msg = ('LeafFS 就是把这台手机上的文件共享给同一个 Wi-Fi 里的其他设备。<br><br>'
               '从 %s 起，这类访问需要「%s」权限。不给的话，这台手机自己还能打开，'
               '但局域网里别的设备连不上，管理页和分享页上的地址也没法发给别人用。<br><br>'
               '点「去授权」后系统会弹一个框，请选「允许」。' % (since, label))
        buttons = ('<a class="btn" href="leaffs://grant" style="%s">去授权</a>'
                   '<a class="btn" href="leaffs://skip" style="%s">先不用</a>'
                   % (solid, ghost))
    return (
        '<!DOCTYPE html><html lang="zh-CN"%s><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<style>%s</style></head><body>'
        '<div class="modal-overlay active">'
        '<div class="modal-content" style="padding:22px 20px;text-align:center;max-width:340px">'
        '<div style="font-size:15px;font-weight:700;color:var(--text);margin-bottom:8px">%s</div>'
        '<div style="font-size:13px;color:var(--text-light);line-height:1.7;margin-bottom:16px">%s</div>'
        '%s'
        '</div></div></body></html>'
    ) % (attrs, css, title, msg, buttons)


# ---------- 分享映射（与桌面共用：虚拟映射到 public/shares/<用户名>/） ----------
# 桌面用 HTTP /api/share/*；安卓 App 直接进程内调用同一映射模块。
# 传入的 paths 为共享根内相对路径。

def share_publish(paths, username):
    """paths: 共享根内相对路径数组 → JSON {ok, published:[{path,name,url}], failed}"""
    if not isinstance(paths, list) or not paths:
        return _json(ok=False, error='文件列表为空')
    username = str(username or '') or 'mobile'
    published = []
    failed = []
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            continue
        norm = p.strip().replace('\\', '/')
        vp, err = _mapping.publish(norm, username)
        if err:
            failed.append({'path': p, 'error': err})
        else:
            published.append({'path': vp, 'name': vp.rsplit('/', 1)[-1],
                              'url': '/download/' + vp})
    if not published:
        return _json(ok=False,
                     error=(failed[0].get('error') if failed else '没有可分享的文件'))
    return _json(ok=True, published=published, failed=failed)


def share_list(username, is_admin=False):
    try:
        items = _mapping.list_mappings(str(username or ''), is_admin=bool(is_admin))
        for it in items:
            it['url'] = '/download/' + it['path']
        return _json(ok=True, mappings=items)
    except Exception as e:
        return _json(ok=False, error='%s: %s' % (type(e).__name__, e))


def share_unpublish(path, username, is_admin=False):
    try:
        ok, err = _mapping.remove(str(path or ''), str(username or ''),
                                  is_admin=bool(is_admin))
        return _json(ok=ok, error=err or '')
    except Exception as e:
        return _json(ok=False, error='%s: %s' % (type(e).__name__, e))


# ---------- 本机路径挂载（与桌面共用：登记到 public/mounts/，只读引用，不复制文件） ----------
# 桌面走 HTTP /api/share/mount；安卓 App 直接进程内调用同一映射模块。
# 桌面那边靠"本机一次性令牌会话"判定"操作者人就坐在服务端这台机器前"；安卓上 App
# 本身就是那台机器，取路径也是 App 自己弹的系统选择器，没有第二条路能走到这里。

def mount_path(fs_path, username):
    """把手机上的绝对路径挂到 public/mounts/ 下。返回 JSON {ok, path, name, error}"""
    try:
        vp, err = _mapping.publish_fs(str(fs_path or ''), str(username or '') or 'mobile')
    except Exception as e:
        return _json(ok=False, error='%s: %s' % (type(e).__name__, e))
    if not vp:
        return _json(ok=False, error=err or '挂载失败')
    return _json(ok=True, path=vp, name=vp.rsplit('/', 1)[-1])


# ---------- 原生导入前的配额检查 ----------
# 与 HTTP 上传同一套三层规则（总空间 / public / 用户目录），只差"在途字节"那一项
# （那是 HTTP 并发上传的预留，本机导入是串行的，不需要）。

def check_quota(rel_dir, new_size):
    """返回 JSON {ok, error}。"""
    try:
        size = int(new_size or 0)
        ud = os.path.realpath(_fs.UPLOAD_DIR)
        path = os.path.realpath(os.path.join(ud, str(rel_dir or '')))
        if _fs.get_folder_size(_fs.UPLOAD_DIR) + size > _cfg.get_total_quota():
            return _json(ok=False, error='服务器总空间不足')
        pub = os.path.join(ud, 'public')
        users = os.path.join(ud, 'users')
        pn = os.path.normcase(path)
        if pn == os.path.normcase(pub) or pn.startswith(os.path.normcase(pub) + os.sep):
            if _fs.get_folder_size(pub) + size > _cfg.get_public_quota():
                return _json(ok=False, error='公共文件夹空间不足')
        elif pn == os.path.normcase(users) or pn.startswith(os.path.normcase(users) + os.sep):
            rel = os.path.relpath(pn, os.path.normcase(users))
            tu = rel.split(os.sep)[0] if rel and rel != '.' else ''
            if tu:
                uu = _fs.get_folder_size(os.path.join(users, tu))
                ul = _ac.get_user_quota(tu) or _cfg.get_default_user_quota()
                if uu + size > ul:
                    return _json(ok=False, error='用户文件夹空间不足')
        return _json(ok=True)
    except Exception as e:
        return _json(ok=False, error='%s: %s' % (type(e).__name__, e))
