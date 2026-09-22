#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 ewq2526. 许可见 LICENSE；联系 ewq2526@163.com
"""LeafFS - 主入口：路由分发 + 服务器启动"""

import os
import sys
# 确保项目根目录（leaffs 的父目录）在 sys.path 中
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
import json
import socket
import webbrowser
import urllib.parse
import asyncio
import threading
import time
import logging
import re
import secrets
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

import leaffs.auth.core as _ac
import leaffs.auth.login_api as _ac_auth
import leaffs.auth.users_api as _ac_user
import leaffs.auth.self_api as _ac_self
import leaffs.config.core as _cfg
import leaffs.config.api as _cfg_api
import leaffs.files.core as _fs
import leaffs.files.api as _fs_api
import leaffs.utils.log as _ut_log
import leaffs.web.render as _wm
import leaffs.utils.core as _ut
from leaffs.dl.dl_core import set_broadcast_fn as _set_broadcast_fn
from leaffs.dl import dl_api as _dl_api
from leaffs.dl.dl_rpc import _kill_aria2c_force
from leaffs.dl import manager as _dl_mgr  # 下载管理器单例（装配在 dl.manager）
from leaffs.runtime_log import (  # 运行日志服务（原本文件顶部，重构抽出）
    logger, LOG_FILE, setup_logging, add_log, get_logs, clear_runtime_logs,
)
from leaffs.watchdog import _wd_start, _wd_finish, _wd_ws_tick, _wd_loop
import leaffs.server.push as _push  # WS 订阅集合与推送广播（管理帧/下载状态/二维码事件）
import leaffs.server.tls as _tls  # TLS 上下文/服务器证书服务（HTTP/WS 共用）
import leaffs.server.ws as _ws  # WebSocket 传输层（连接/限流/消息分发）
import leaffs.server.handler as _http  # HTTP 传输层（HTTPHandler/ThreadingHTTPServer/run_http）
import leaffs.server.cert_remind as _cert_remind  # HTTPS 证书提示页（8082，桌面/安卓共用同一份）
import leaffs.auth.local_token as _lt  # 本机一次性登录令牌服务
from leaffs.server.hosts import (  # 主机名/IP 工具（过渡期别名，随 handler 抽离正名）
    strip_host_port as _strip_host_port,
    primary_lan_ip as _primary_lan_ip,
    collect_ips as _collect_ips,
    _LOCAL_HOST_NAMES,
)

# 下载管理器（进程级单例；dl_api 已在 manager 模块装配）
_dl_manager = _dl_mgr.get_manager()
def get_dl_manager(): return _dl_manager


def start_server():
    # 临时诊断：设置环境变量 LEAFFS_FAULTDUMP=1 后，卡死时按 Ctrl+Break 会把所有
    # 线程的调用栈写入项目目录 faulthandler_dump.txt（排查随机卡死用，默认关闭）
    if os.environ.get('LEAFFS_FAULTDUMP') == '1':
        try:
            import faulthandler
            import signal as _sig
            _dump_file = open(os.path.join(_ut.PROJECT_DIR, 'faulthandler_dump.txt'),
                              'w', encoding='utf-8', buffering=1)
            faulthandler.register(_sig.SIGBREAK, file=_dump_file, all_threads=True)
        except Exception:
            pass
    setup_logging()
    _cfg.load_config()
    _ac.load_users()
    # 会话表已落盘，启动时要读回来 —— 否则重启一次所有人被登出（2026-09-17）
    _ac.load_sessions()
    _ac.start_session_cleanup()
    _fs.cleanup_orphan_thumbs()
    # LF-26：进程被 kill / 崩溃时，正在写的上传临时文件会留在 UPLOAD_DIR/.uploads/ 里，
    # 而那个目录对用户不可见（LF-22）—— 不清就成了隐形垃圾。**刚启动时必然没有在途上传**，
    # 所以这是唯一安全的清理时机（运行期清会把正在上传的文件干掉）。
    try:
        _n = _fs.cleanup_upload_tmp()
        if _n:
            add_log('启动清理：删掉 %d 个上传临时残留' % _n, 'info')
    except Exception as _e:
        add_log('启动清理上传临时目录失败: %s' % (_e,), 'warn')
    os.makedirs(_fs.UPLOAD_DIR, exist_ok=True)
    os.makedirs(os.path.join(_fs.UPLOAD_DIR, 'public'), exist_ok=True)
    add_log('服务器初始化完成', 'ok')
    add_log(f'共享目录: {_fs.UPLOAD_DIR}', 'info')
    add_log(f'ffmpeg: {"已安装" if _fs.has_ffmpeg() else "未安装"}', 'info')
    from leaffs.dl.dl_utils import has_aria2c
    add_log(f'aria2c: {"已安装" if has_aria2c() else "未安装"}', 'info')
    _set_broadcast_fn(_push.broadcast_download_update)
    # A-12/D4：启动即生成“本机自动登录一次性令牌”（HTTP /login?leaf= 与 WS auth token 共用）。
    # 每次启动新令牌、旧文件覆盖（登录用；与证书机制无关）。
    _lt.reset_and_write()  # 每次启动新令牌、旧文件覆盖（登录用；与证书机制无关）
    # TLS 启用时：先单线程预热 TLS 上下文（见 _build_tls_context）。证书来源按序：
    #   a) server_config.json 显式配置 tls_cert/tls_key（相对 config 或绝对路径）；
    #   b) 既有 config/selfsigned.crt + selfsigned.key（直接加载）；
    #   c) 两者皆无 → 首次启动自动生成一张随机自签服务器证书（非 CA、不装信任库）。
    # 全部不可用则按安全策略 A 拒绝以明文方式提供服务（宁可不启动）。
    if _cfg.get_tls_enabled():
        try:
            ctx = _tls.get_tls_context()
        except Exception as _te:
            # 失败原因记日志（构建异常/证书异常都可见，不再静默吞掉）
            add_log(f'TLS 上下文构建异常: {type(_te).__name__}: {_te}', 'err')
            ctx = None
        if ctx is None:
            msg = ('自动生成服务器证书失败（或显式配置的证书不可用）：请在 server_config.json '
                   '配置 tls_cert/tls_key（可自行用 openssl 等签发），或将 tls_enabled 设为 false。\n'
                   '（配置文件位于 config/server_config.json）按安全策略拒绝以明文方式启动。')
            print(msg)
            add_log('无法取得可用服务器证书（未配置/自动生成失败）：请配置 tls_cert/tls_key '
                    '或将 tls_enabled 设为 false；按安全策略拒绝明文启动（策略 A）', 'err')
            raise SystemExit(1)
    # 证书提示页（纯 HTTP、默认 0.0.0.0 全网监听）：仅 TLS 启用时启动（TLS 关闭时整页不提供）
    if _cfg.get_tls_enabled():
        threading.Thread(target=_cert_remind.run_cert_remind_http, daemon=True).start()
    # 启动 HTTP 服务器线程
    t = threading.Thread(target=_http.run_http, daemon=True)
    t.start()
    # 启动 WebSocket 服务器线程
    ws_thread = threading.Thread(target=_ws.run_ws_sync, daemon=True)
    ws_thread.start()
    # 启动管理页实时推送线程（每秒一帧，仅在存在订阅者时构建数据）
    threading.Thread(target=_push.admin_push_loop, daemon=True).start()
    # 冻结看门狗（疑似卡死自动抓全线程栈，见 _wd_loop 说明）
    threading.Thread(target=_wd_loop, daemon=True).start()
    # 后台启动 aria2c RPC 守护进程（不再阻塞服务器启动），结束后记录日志并通知下载页
    def _start_daemon_and_notify():
        try:
            _dl_manager.start_daemon()
        except Exception:
            pass
        try:
            from leaffs.dl import dl_rpc as _dlr
            st = _dlr.get_aria2c_status()
            if st == 'ready':
                add_log('aria2c 下载服务已就绪', 'ok')
            elif st == 'unavailable':
                add_log('aria2c 不可用，磁力/种子下载不可用（普通直链不受影响）', 'warn')
            elif st == 'error':
                add_log('aria2c 启动失败，请查看日志', 'err')
            else:
                add_log('aria2c 未就绪', 'warn')
            _push.broadcast_download_daemon_status()
        except Exception:
            pass
    threading.Thread(target=_start_daemon_and_notify, daemon=True).start()
    # aria2c 守护监控：进程挂了/连不上自动重启，状态变化推送下载页（RPC 挂掉自愈）
    try:
        from leaffs.dl import dl_rpc as _dlr
        _dlr.set_daemon_status_cb(_push.broadcast_download_daemon_status)
        _dlr.start_daemon_watchdog()
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]; s.close()
    except: ip = '127.0.0.1'
    ps = f':{_cfg.PORT}' if _cfg.PORT != 80 else ''
    h_scheme = 'https' if _cfg.get_tls_enabled() else 'http'
    w_scheme = 'wss' if _cfg.get_tls_enabled() else 'ws'
    print(f'  ───────────────────────────')
    print(f'\n  LeafFS 文件传输服务已启动')
    print(f'  {h_scheme.upper()}: {h_scheme}://{ip}{ps}')
    print(f'  WebSocket: {w_scheme}://{ip}:{_cfg.WS_PORT}')
    if _cfg.get_tls_enabled():
        # 横幅给用户可点击地址：默认全网监听（0.0.0.0）时展示主 LAN IP；显式绑定具体地址时按其展示
        remind_bind = _cert_remind._trust_http_bind_host()
        remind_url_host = ip if remind_bind == '0.0.0.0' else remind_bind
        print(f'  证书提示页: http://{remind_url_host}:{_cfg.get_tls_trust_port()}')
    _tok_now = _lt.get_current()
    if _tok_now:
        print(f'  本机自动登录令牌（一次性）: {h_scheme}://localhost:{_cfg.PORT}/login?leaf={_tok_now}')
    if not _cfg.get_tls_enabled():
        print('  [警告] 当前为明文模式（HTTP），仅限可信局域网内使用')
    print(f'  共享目录:  {_fs.UPLOAD_DIR}')
    print(f'  请使用管理员账户登录（仍为默认口令时，页面顶部会提示修改密码）\n')
    print(f'  ───────────────────────────')
    # 尝试启动桌面 WebView 窗口
    webview_ok = _start_webview_window()
    if webview_ok:
        print('  WebView 窗口已关闭，程序退出。')
    else:
        print('  已回退到浏览器，按 Ctrl+C 停止服务器')
        # 回退到浏览器后，需要保持主线程运行不让程序退出
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print('  服务器已停止。')

def _is_internal_webview_url(url):
    """内嵌窗口允许停留的地址：本机（127.0.0.1 / localhost / [::1]）+ about:/data:/blob:。

    端口**不限定** —— 服务自己会用到 8080/8081/8082 几个端口，卡死端口只会误伤自己。
    纯函数，便于单测（这条逻辑没法用窗口反复验证）。
    """
    u = (url or '').strip().lower()
    if not u:
        return True          # 还没拿到 URL（加载中）：别乱跳
    if u.startswith('about:') or u.startswith('data:') or u.startswith('blob:'):
        return True
    if not (u.startswith('http://') or u.startswith('https://')):
        return False
    rest = u.split('://', 1)[1]
    host = rest.split('/', 1)[0].split('?', 1)[0].split('#', 1)[0]
    host_only = (host.rsplit(']', 1)[0] + ']') if host.startswith('[') else host.split(':', 1)[0]
    return host_only in ('127.0.0.1', 'localhost', '[::1]')


def _install_webview_guard(cwv2, win):
    """装配内嵌窗口的出站守卫。**必须在 UI 线程调用** —— CoreWebView2 只能从 UI 线程访问
    （从别的线程碰它会抛 `InvalidOperationException: CoreWebView2 can only be accessed
    from the UI thread`，实测）。

    三层，全是 WebView2 官方接口，每一层的行为都用真窗口实测过（见 .work/probe_pwv6_*.py）：

      1. 导航层：外链导航 `args.Cancel = True` —— **导航根本不发生**。
         实测：页面内 `location.href = 外链`、页面内链接点击，都拦得住。
         ⚠️ 拦不住 Python 侧 `win.load_url()`（编程式导航不走这一层）—— 但威胁来自页面内，
         而且真有人这么调的话，第 2 层会把内容一起堵掉。
      2. 网络层：非本机请求塞一个 403 空响应。WebView2 收到 Response 就**跳过网络层**，
         实测：本机起 8123 监听，两个外部子资源都被拦下时，监听端**一个请求都没收到** ——
         是真的没发包，不是发了包再丢响应。
      3. 新窗口：先摘掉 pywebview 自己的 `on_new_window_request`，再换成 `Handled=True`。
         ⚠️ 不摘不行 —— 它默认 `webbrowser.open()`（把外链甩给**系统浏览器**），
         把 settings 的 OPEN_EXTERNAL_LINKS_IN_BROWSER 设成 False 之后它改走
         `load_url()`（在**窗口内**加载外链）。两条路都不行，所以只能自己接管。

    `WebResourceRequested` 的 URL 过滤器由 pywebview 注册（`AddWebResourceRequestedFilter('*',
    All)`，在它自己的 on_webview_ready 里）—— 那是同一个 CoreWebView2InitializationCompleted
    的**第一个**订阅者，先于本函数执行，所以这里不用再注册一遍。

    ⚠️ 为什么不用 pywebview 的 `before_load` 事件（5.4 才引入）：它对应 DOMContentLoaded，
    页面**已经在下载了**；而且 `util.py` 里 `window.events.before_load.set()` 的返回值被直接
    丢掉，根本没有取消通道 —— 它拦不住导航。
    ⚠️ 为什么不再用 0.4 秒轮询 URL：那是"跳走之后再拉回来"，站外页面至少有 0.4 秒的加载窗口；
       现在是**零窗口**。
    ⚠️ 为什么只挂 CoreWebView2 级、不挂控件级 `wv.NavigationStarting`：实测单独一级就够；
    两级都挂会让同一个导航被处理两遍、日志重复。
    ⚠️ 别再回头去试 WebView2 的 host-resolver-rules 那一类"改 DNS 解析"的启动参数：**实测被
    过滤**，加了照样能打开外部站点，是假保护。也别再用环境变量往 WebView2 塞浏览器参数。
    """
    def _on_navigation_starting(sender, args):
        uri = str(args.Uri)
        if _is_internal_webview_url(uri):
            return
        args.Cancel = True          # 先取消再打日志：日志出问题也不能影响拦截
        add_log(f'已阻止内嵌窗口访问外部地址: {uri[:120]}', 'warn')

    def _on_web_resource_requested(sender, args):
        uri = str(args.Request.Uri)
        if _is_internal_webview_url(uri):
            return
        args.Response = sender.Environment.CreateWebResourceResponse(
            None, 403, 'Blocked', 'Content-Type: text/plain')
        add_log(f'已阻止内嵌窗口的外部请求: {uri[:120]}', 'warn')

    def _on_new_window_requested(sender, args):
        args.set_Handled(True)      # 不开新窗口、不交给系统浏览器、也不 load_url
        add_log(f'已阻止内嵌窗口打开新窗口: {str(args.Uri)[:120]}', 'warn')

    cwv2.NavigationStarting += _on_navigation_starting
    cwv2.WebResourceRequested += _on_web_resource_requested

    old_handler = getattr(getattr(win.native, 'browser', None), 'on_new_window_request', None)
    if old_handler is not None:
        cwv2.NewWindowRequested -= old_handler
    cwv2.NewWindowRequested += _on_new_window_requested


def _start_webview_window():
    """pywebview 原生窗口（D4：URL 带一次性令牌 ?leaf=，本机自动登录走令牌而非无条件免密）

    环境变量 LEAFFS_NO_WEBVIEW=1 时跳过窗口与浏览器回退（测试/无桌面场景用）。
    """
    if os.environ.get('LEAFFS_NO_WEBVIEW') == '1':
        return False
    def _local_url():
        scheme = 'https' if _cfg.get_tls_enabled() else 'http'
        url = f'{scheme}://localhost:{_cfg.PORT}/login'
        _ltok = _lt.get_current()
        if _ltok:
            url += '?leaf=' + _ltok
        return url
    try:
        import webview

        # 出站防护（用户要求「不能在我们软件内出事」）。三条 settings 必须在 start() 之前设，
        # pywebview 只在启动时读一次：
        #   OPEN_EXTERNAL_LINKS_IN_BROWSER=False —— 否则它会把 target=_blank 的链接用
        #     webbrowser.open() 甩给**系统浏览器**（那等于我们主动把外链放出去）；设成 False
        #     之后它改走 load_url()，正好落进 _install_webview_guard 第 3 层的接管范围。
        #   ALLOW_DOWNLOADS=False —— 显式写上（本来也是 pywebview 的默认值）。
        #   IGNORE_SSL_ERRORS=True —— 放行本机自签证书。取代以前往环境变量里塞
        #     `--ignore-certificate-errors` 的写法：那个参数还依赖 WebView2 把环境变量与
        #     AdditionalBrowserArguments 合并的行为，不如这个官方开关直白。
        webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = False
        webview.settings['ALLOW_DOWNLOADS'] = False
        webview.settings['IGNORE_SSL_ERRORS'] = True
        webview.create_window('LeafFS 文件传输', _local_url(),
                              width=1200, height=800, resizable=True)

        installed = threading.Event()

        def _after_start():
            """等窗口和 WebView2 就绪，把三层守卫装上。

            ⚠️ 这里跑在 pywebview 起的**非 UI 线程**里（webview/__init__.py 里 func 是
            `Thread(target=func).start()`，而且**早于** `guilib.create_window()`），所以：
              - `webview.windows[0].native` 一开始是 None，必须等；
              - `native` 比 `native.webview` **早赋值**（winforms.py 第 195 行 vs 第 281 行，
                都在同一个构造函数里），只等 native 会拿到 None；
              - CoreWebView2 只能从 UI 线程访问，所以真正的装配放进
                CoreWebView2InitializationCompleted 回调（那个回调在 UI 线程）。
            """
            win = webview.windows[0]
            wv = None
            deadline = time.time() + 20
            while time.time() < deadline:
                native = win.native
                if native is not None:
                    wv = getattr(native, 'webview', None)
                    if wv is not None:
                        break
                time.sleep(0.05)
            if wv is None:
                add_log('内嵌窗口守卫未装配：拿不到 WebView2 控件（窗口可用，但没有出站防护）', 'warn')
                return

            def _on_core_ready(sender, args):
                cwv2 = sender.CoreWebView2
                if cwv2 is None:
                    add_log('内嵌窗口守卫未装配：CoreWebView2 初始化失败', 'warn')
                    return
                _install_webview_guard(cwv2, win)
                installed.set()

            wv.CoreWebView2InitializationCompleted += _on_core_ready
            if not installed.wait(20):
                # 装配不上就是安全缺口，必须看得见，不静默
                add_log('内嵌窗口守卫未装配：CoreWebView2 初始化回调没到', 'warn')

        webview.start(_after_start)
        return True
    except Exception as e:
        print(f'  窗口启动失败 ({e})，使用浏览器打开')
    try:
        webbrowser.open(_local_url())
    except Exception:
        pass
    return False
