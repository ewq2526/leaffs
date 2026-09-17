# -*- coding: utf-8 -*-
"""HTTPS 证书提示页（纯 HTTP，默认 8082）—— 桌面与安卓共用同一份。

主站开着自签证书时，访客直接访问 https 会被浏览器的「不安全」提示挡住；先在纯 HTTP
的这一页把话讲清楚，再引导进 https 主站。

页面本体就是站内普通页面 `web_page/notice/cert.html`（与其他页面同结构、同一套
style.css 的卡片与按钮），样式表全文在渲染时内联进去 —— 8082 不提供 /static/，
这与 ui_theme.error_page 是同一做法。因此它的观感、亮暗主题、主色与主站始终一致。

流程：服务端把这页渲染出来，按钮直接指向 https 主站（__CERT_MAIN_URL__），浏览器弹
一次「不安全」、点「高级 → 继续访问」即进入。**不做任何自动跳转，也不探测端口**：
  1) 曾经由页面脚本探测 8080/8081 两个端口的证书是否都已信任、据此决定要不要自动
     跳主站 —— 后来查清 8081 连不上与证书无关（是 WS 的 Host/Origin 白名单不认本机
     热点地址，见 server/ws.py 的 _ws_origin_host_ok），那段探测已删除；
  2) 服务端也**不按登录状态跳转** —— 曾经那道「已登录就 302 回主站」用的是弱标记
     （只是"这台浏览器登录过"，会话可能早已失效），会把人送进 /browse/ 再被主站
     弹回 /login，用户看到的是"点了一下又回来了"，同样已删除。

模板（`web_page/notice/cert.html`）里的两条约定：
  * `__CERT_STYLE__` 独占一行 —— 服务端把主站 style.css 全文替换进去（8082 不提供
    /static/，与 ui_theme.error_page 是同一做法）；
  * `__CERT_MAIN_URL__` 在按钮的 href 里 —— 替换前按 HTML 属性上下文转义。

⚠️ **模板里的注释会被原样下发给访客**（这里是运行时读盘渲染，没有构建剥离环节）：
别在 web_page 下的 HTML/JS/CSS 里写内部路径、文件名或服务端机制说明 ——
那些一律写进本文件的 docstring。有测试盯着（tests/test_no_internal_paths_in_web.py）。
"""
import html as _html
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler

import leaffs.auth.core as _ac
import leaffs.config.core as _cfg
import leaffs.utils.core as _utc
from leaffs.paths import BASE_DIR
from leaffs.runtime_log import add_log
from leaffs.server.handler import ThreadingHTTPServer
from leaffs.server.hosts import is_valid_host as _is_valid_host
from leaffs.server.hosts import strip_host_port as _strip_host_port

_PAGE_PATH = os.path.join(BASE_DIR, 'web_page', 'notice', 'cert.html')
_CSS_PATH = os.path.join(BASE_DIR, 'web_page', 'common', 'style.css')

# 模板读不到时的兜底（纯信息，不假装正常页面）：提示页读不到不该表现成白屏
_FALLBACK_PAGE = (
    '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<title>HTTPS 证书提示</title></head>'
    '<body style="font:14px/1.8 sans-serif;padding:28px;text-align:center">'
    '<p>浏览器提示「不安全」是正常的：这台服务器用的是自签证书。</p>'
    '<p>确认这是你自己的服务器后，点「高级 → 继续访问」即可（会弹一次）。</p>'
    '<p><a href="__CERT_MAIN_URL__">进入文件服务</a></p>'
    '</body></html>'
)


def _read_text(path, fallback=''):
    """按 mtime 缓存地读文本文件（改文件即时生效，不用重启）"""
    try:
        data = _utc.read_file_cached(path)
        if data:
            return data.decode('utf-8')
    except Exception as e:
        add_log(f'证书提示页读取失败 {path}: {e}', 'warn')
    return fallback


def _trust_http_bind_host():
    """提示页监听地址：默认 0.0.0.0（与主服务一致），使局域网/最终用户可达。

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


class CertRemindHandler(BaseHTTPRequestHandler):
    """证书提示页处理器：仅响应根路径 '/'（渲染说明页），其余路径一律 404。

    本页不做任何跳转（既不看登录状态，也不探测端口证书），
    无脚本、无外链、无安装/下载内容。
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
        端口取主站 http_port（_cfg.PORT）。8082 为明文 HTTP，本页仅在
        tls_enabled=true 时提供，故目标恒为 https。

        Host 是**不可信输入**，必须过 is_valid_host()：非法（带引号/尖括号、
        带下划线、空标签……）就不用它、退到 host_fallback；连兜底也不合法
        （或端口非法）才返回 ''，由调用方决定怎么渲染。
        """
        host = _strip_host_port(self.headers.get('Host', ''))
        if not _is_valid_host(host):
            host = host_fallback
        if not _is_valid_host(host):
            return ''
        try:
            p = int(_cfg.PORT)
            if not 1 <= p <= 65535:
                return ''
        except Exception:
            return ''
        if ':' in host and not host.startswith('['):
            host = '[' + host + ']'  # 裸 IPv6 地址补方括号
        return f'https://{host}:{p}{path}'

    def _page_bytes(self):
        """渲染提示页：模板 + 内联主站样式表 + 按请求拼出的主站地址"""
        html = _read_text(_PAGE_PATH, _FALLBACK_PAGE)
        html = html.replace('__CERT_STYLE__', _read_text(_CSS_PATH))
        # 按钮直接指向 https 主站（http_port）。早先这里换成过 WS 端口（ws_port），
        # 想让访客"按端口依次放行证书" —— 那个前提是错的：8081 连不上跟证书无关
        # （见 server/ws.py 的 _ws_origin_host_ok），所以按钮已改回主站。
        # 转义后才塞进 href="…"（HTML 属性上下文）：校验与编码是两件事 ——
        # 上面那道管"不接受非法 Host"，这里管"输出按上下文编码"，即使前者漏了，
        # 值里也不可能出现能闭合属性的字符
        html = html.replace('__CERT_MAIN_URL__', _html.escape(
            self._main_https_url('/', host_fallback='localhost'), quote=True))
        return html.encode('utf-8')

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
        # 只渲染说明页：既不按登录状态跳转，也不判断各端口的证书有没有被信任
        # （前者服务端判断不准，后者它根本看不见 —— 证书例外记在浏览器里），
        # 按钮直接指向 https 主站。
        self._respond(200, 'text/html; charset=utf-8', self._page_bytes())

    do_HEAD = do_GET


def run_cert_remind_http():
    """证书提示页线程入口（纯 HTTP）。仅由 TLS 启用时启动；
    绑定失败只告警不影响主服务。"""
    host = _trust_http_bind_host()
    try:
        port = int(_cfg.get_tls_trust_port())
    except Exception:
        port = 8082
    try:
        httpd = ThreadingHTTPServer((host, port), CertRemindHandler)
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
