#!/usr/bin/python3
"""LeafOS kiosk：全屏 GTK + WebKitGTK 窗口，显示本机 leaffs 界面。

这个窗口就是系统的操作界面，没有退出键。维护入口是 Ctrl+Alt+F2 切到 tty2。
页面 JS 出错时把信息发到 404 路径，落在 leaffs 日志里，便于定位。
"""
import ssl
import sys
import time
import urllib.request

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2

BASE = "https://localhost:8080"
TOKEN_FILE = "/srv/leaffs/config/local_token.txt"

REPORTER = r"""
window.addEventListener('error', function (e) {
  try {
    var s = String(e.message) + ' @' + String(e.filename) + ':' + String(e.lineno);
    var b = btoa(unescape(encodeURIComponent(s)))
              .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    var i = new Image();
    i.src = '/p/E' + b;
  } catch (x) {}
});
"""


def target_url():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            tok = f.read().strip()
    except OSError:
        tok = ""
    return f"{BASE}/login?leaf={tok}" if tok else BASE + "/login"


def wait_http(tries=90):
    ctx = ssl.create_default_context()
    for _ in range(tries):
        try:
            urllib.request.urlopen(BASE + "/login", timeout=3, context=ctx)
            return True
        except Exception:
            time.sleep(1)
    return False


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else target_url()
    if not wait_http():
        print("leaffs 没起来", file=sys.stderr)
        sys.exit(1)

    win = Gtk.Window()
    web = WebKit2.WebView()
    ucm = web.get_user_content_manager()
    ucm.add_script(WebKit2.UserScript.new(
        REPORTER,
        WebKit2.WebUserContentInjectedFrames.TOP_FRAME if False else WebKit2.UserContentInjectedFrames.TOP_FRAME,
        WebKit2.UserScriptInjectionTime.START, None, None))
    win.add(web)
    win.fullscreen()
    win.show_all()
    web.load_uri(url)
    Gtk.main()


if __name__ == "__main__":
    main()