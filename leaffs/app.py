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
import websockets
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
import leaffs.auth.session_api as _ac_session
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

_CERT_REMIND_TITLE = '文件服务器 HTTPS 证书提示'
_CERT_REMIND_ITEMS = (
    '浏览器提示“不安全 / 连接不是私密连接”是正常的——这台服务器用的是自签证书（没有找证书机构盖章）。',
    '只有当你确认这就是你自己的/信任的服务器时，才点浏览器的“高级 → 继续访问”。',
    '不要在陌生网站或来路不明的页面安装任何证书。',
    '想彻底消除提示：找这台服务器的管理员配置正式 HTTPS 证书后重启。',
)
# 英文摘要（AI 翻译，可能与中文存在差异），供英文使用者阅读
_CERT_REMIND_EN_SUMMARY = (
    'English (AI-translated): The "not secure" warning is expected because this server uses a '
    'self-signed certificate. Only continue when you are sure this is your own or a trusted server. '
    'Never install certificates from unknown pages. To remove the warning, ask the server '
    'administrator to configure an official HTTPS certificate.'
)

# 提示页内联样式（独立于静态目录，不引外部资源；观感对齐主站浅色卡片风，品牌主色 #0ea5e9）
_CERT_REMIND_CSS = '''
*{margin:0;padding:0;box-sizing:border-box}
html,body{min-height:100%}
body{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",Roboto,system-ui,sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center;
  padding:24px 16px;color:#22305c;line-height:1.7;-webkit-font-smoothing:antialiased;
  background:#e3ebf7;
  background-image:
    radial-gradient(760px 520px at 12% -10%,rgba(125,190,255,.5),transparent 62%),
    radial-gradient(700px 480px at 105% 8%,rgba(150,220,255,.42),transparent 60%),
    radial-gradient(620px 420px at 90% 108%,rgba(125,170,255,.35),transparent 62%);
  background-attachment:fixed;
}
.card{
  width:100%;max-width:620px;margin:auto;
  background:rgba(255,255,255,.9);
  border:1px solid rgba(130,155,220,.28);
  border-radius:22px;padding:40px 30px 34px;text-align:center;
  box-shadow:0 12px 34px rgba(96,120,210,.16),0 2px 8px rgba(130,150,220,.08);
}
.badge{
  width:66px;height:66px;margin:0 auto 18px;border-radius:50%;
  background:linear-gradient(135deg,#0ea5e9,#7dd3fc);
  display:flex;align-items:center;justify-content:center;font-size:30px;
  box-shadow:0 10px 24px rgba(14,165,233,.35);
}
h1{font-size:22px;font-weight:700;color:#1f2f63;margin-bottom:8px;letter-spacing:.3px}
.lead{font-size:14px;color:#5a6a99;margin:0 auto 20px;max-width:460px}
ol{list-style:none;counter-reset:item;margin:0 0 26px;padding:0;text-align:left}
ol li{
  counter-increment:item;position:relative;
  padding:12px 14px 12px 48px;margin:0 0 10px;
  background:rgba(14,165,233,.07);
  border:1px solid rgba(14,165,233,.16);
  border-radius:14px;font-size:14.5px;color:#2b3a63;
}
ol li::before{
  content:counter(item);position:absolute;left:12px;top:50%;transform:translateY(-50%);
  width:26px;height:26px;border-radius:50%;
  background:linear-gradient(135deg,#0ea5e9,#38bdf8);color:#fff;
  font-size:13px;font-weight:700;
  display:flex;align-items:center;justify-content:center;
}
.login-btn{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  text-decoration:none;color:#fff;font-size:16px;font-weight:600;
  background:linear-gradient(90deg,#0ea5e9,#38bdf8);
  border-radius:999px;padding:13px 38px;
  box-shadow:0 10px 26px rgba(14,165,233,.35);
  transition:transform .18s ease,box-shadow .18s ease,filter .18s ease;
}
.login-btn .arr{font-size:19px;line-height:1;transition:transform .18s ease}
.login-btn:hover{transform:translateY(-2px);box-shadow:0 14px 32px rgba(14,165,233,.45);filter:brightness(1.05)}
.login-btn:hover .arr{transform:translateX(3px)}
.login-btn:active{transform:scale(.97)}
@media(max-width:480px){
  .card{padding:28px 18px 24px}
  h1{font-size:19px}
  .login-btn{width:100%;max-width:320px}
}
'''

def _trust_http_bind_host():
    """证书提示页监听地址：默认返回 0.0.0.0（与主服务一致），使局域网/最终用户可达。

    配置键 trust_bind_host 可覆盖：显式给出具体地址（如 127.0.0.1 或某局域网 IP）时按其绑定；
    等价于“全网监听/未配置”的值（空 / 0.0.0.0 / :: / [::] / any）与明显非法的值
    一律安全回退 0.0.0.0。页面为纯信息提示（无证书安装/下载引导），全网监听无 CA 投毒风险。
    """
    import ipaddress
    host = ''
    try:
        host = str(_cfg.get_trust_bind_host() or '').strip().lower()
    except Exception:
        host = ''
    if host in ('', '0.0.0.0', '::', '[::]', 'any'):
        return '0.0.0.0'
    try:
        ipaddress.ip_address(host)
    except ValueError:
        # 明显非法值（非合法 IP 字面量）→ 安全回退全网监听
        return '0.0.0.0'
    return host


class _CertRemindHandler(BaseHTTPRequestHandler):
    """证书提示页处理器：仅响应根路径 '/'。

    无有效会话 → 渲染品牌化提示页（纯大白话 + 内联样式 + “进入登录页”大按钮，
    指向 https 主站 /login）；请求携带有效 wifi_session（任意角色，含来源 IP 绑定
    校验）→ 302 到 https 主站 /browse/（免重复登录）；其余路径一律 404。
    跳转目标恒为 https 主站（scheme= https、host= 请求 Host 去端口、port= 主站
    http_port），本页永不回跳自身 → 无重定向循环。无脚本、无外链、无安装/下载内容。
    """
    protocol_version = 'HTTP/1.0'
    # 收尾：与主服务一致，Server 响应头不暴露 Python/组件版本
    server_version = 'LeafFS'
    sys_version = ''
    def version_string(self):
        return self.server_version

    def log_message(self, fmt, *args):
        pass  # 提示页不写访问日志

    def _main_https_url(self, path, host_fallback=''):
        """拼主站 https URL：https://<host>:<http_port><path>。

        host 取请求 Host 头去端口后的主机（缺失时可用 host_fallback 兜底）；
        端口取主站 http_port（_cfg.PORT，load_config 后的运行值；8082 为明文 HTTP，
        本页仅在 tls_enabled=true 时提供，故目标恒为 https）。Host 缺失/端口非法 →
        返回 ''（由调用方决定：跳转分支跳过、按钮分支兜底 localhost）。
        """
        host = _strip_host_port(self.headers.get('Host', '')) or host_fallback
        if not host:
            return ''
        try:
            port = int(_cfg.PORT)
            if not 1 <= port <= 65535:
                return ''
        except Exception:
            return ''
        if ':' in host and not host.startswith('['):
            host = '[' + host + ']'  # 裸 IPv6 地址补方括号
        return f'https://{host}:{port}{path}'

    def _has_valid_session(self):
        """读取请求 Cookie 并判断“本浏览器已登录”。

        两级判定：
        1) 真会话：请求携带有效 wifi_session（ac_core.get_session，含来源 IP 绑定）。
        2) 登录标记：HTTPS 下发的会话 Cookie 带 Secure，浏览器不会经明文 http
           （8082）发回；故登录/登出会同步维护一个非 Secure 的 LOGIN_MARKER 标记，
           标记存在即视为“本浏览器已登录”（跳转后由 https 主站做真实验证，
           标记失效时主站会自行引导回登录页，不会造成死循环）。
        异常一律按未登录处理。
        """
        try:
            role, _sid = _ac.get_session(self.headers.get('Cookie', ''), True, self.client_address[0])
            if role:
                return True
        except Exception:
            pass
        try:
            raw = self.headers.get('Cookie', '') or ''
            for part in raw.split(';'):
                part = part.strip()
                if not part:
                    continue
                k, _, v = part.partition('=')
                if k.strip() == _ac.LOGIN_MARKER and v.strip() == '1':
                    return True
        except Exception:
            pass
        return False

    def _redirect_https(self, location):
        """302 跳转 https 主站（含与页面同款安全响应头）。"""
        self.send_response(302)
        self.send_header('Location', location)
        self.send_header('Content-Length', '0')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()

    def _page_bytes(self):
        # 按钮目标：https://<host>:<http_port>/login；Host 缺失等极端情形兜底 localhost
        login_url = self._main_https_url('/login', host_fallback='localhost')
        items = ''.join(f'<li>{t}</li>' for t in _CERT_REMIND_ITEMS)
        return (
            '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="robots" content="noindex">\n'
            f'<title>{_CERT_REMIND_TITLE}</title>\n'
            f'<style>\n{_CERT_REMIND_CSS}\n</style>\n'
            '</head>\n<body>\n'
            '<div class="card">\n'
            '<div class="badge" aria-hidden="true">🛡️</div>\n'
            '<h1>先看这里，再进入文件服务</h1>\n'
            '<p class="lead">浏览器出现“不安全”提示是正常的——读完下面这 4 点，你就知道该怎么做了。</p>\n'
            f'<ol>\n{items}\n</ol>\n'
            f'<p class="lead" style="font-size:12px;opacity:.8;margin-top:-6px">{_CERT_REMIND_EN_SUMMARY}</p>\n'
            f'<a class="login-btn" href="{login_url}">进入登录页<span class="arr" aria-hidden="true">→</span></a>\n'
            '</div>\n'
            '</body>\n</html>\n'
        ).encode('utf-8')

    def _respond(self, status, ctype, body):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        # A-10：与主服务同款安全响应头（nosniff / XFO DENY / Referrer）
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        if self.command != 'HEAD':
            try:
                self.wfile.write(body)
            except OSError:
                pass

    def do_GET(self):
        if urllib.parse.urlparse(self.path).path != '/':
            self._respond(404, 'text/plain; charset=utf-8', b'404 Not Found')
            return
        # 已登录用户（任意角色）→ 302 https 主站 /browse/。防循环：跳转目标恒为
        # https 主站；本页（8082）只服务 '/'，且无法拼出主站地址时（Host 缺失/端口
        # 非法，返回 ''）即使有会话也改为正常渲染，绝不回跳自身。
        browse_url = self._main_https_url('/browse/')
        if browse_url and self._has_valid_session():
            self._redirect_https(browse_url)
            return
        self._respond(200, 'text/html; charset=utf-8', self._page_bytes())

    do_HEAD = do_GET


def run_cert_remind_http():
    """证书提示页线程入口（纯 HTTP）。仅由 start_server 在 tls_enabled=true 时启动；
    绑定失败只告警不影响主服务。"""
    host = _trust_http_bind_host()
    try:
        port = int(_cfg.get_tls_trust_port())
    except Exception:
        port = 8082
    try:
        httpd = _http.ThreadingHTTPServer((host, port), _CertRemindHandler)
    except Exception as e:
        add_log(f'证书提示页启动失败（http://{host}:{port}）: {e}（不影响主服务）', 'warn')
        return
    add_log(f'证书提示页已启动: http://{host}:{port}', 'ok')
    try:
        httpd.serve_forever()
    except Exception:
        pass
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass


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
    _ac.start_session_cleanup()
    _fs.cleanup_orphan_thumbs()
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
        threading.Thread(target=run_cert_remind_http, daemon=True).start()
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
        remind_bind = _trust_http_bind_host()
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
