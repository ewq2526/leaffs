#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 ewq2526. 许可见 LICENSE.txt；联系 ewq2526@163.com
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
        # 仅供内嵌 WebView 放行本地自签证书；不装 CA；只影响本进程该控件。
        # 仅当用户未自定义时写入默认值（setdefault），不覆盖已有设置。
        os.environ.setdefault('WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS',
                              '--ignore-certificate-errors')
        import webview
        import time
        time.sleep(0.3)
        webview.create_window('LeafFS 文件传输', _local_url(),
                              width=1200, height=800, resizable=True)
        webview.start()
        return True
    except Exception as e:
        print(f'  窗口启动失败 ({e})，使用浏览器打开')
    try:
        webbrowser.open(_local_url())
    except Exception:
        pass
    return False
