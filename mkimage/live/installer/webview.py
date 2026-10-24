#!/usr/bin/env python3
"""安装页的窗口：一个全屏 WebKit 窗口，只加载本地安装页。

用 leaffs 的虚拟环境跑 —— 那里有 PyGObject 和 WebKit，系统 python 未必有。
"""
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2  # noqa: E402


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8090/"

    win = Gtk.Window()
    win.set_title("Camphor 安装")
    win.set_decorated(False)
    win.fullscreen()
    win.connect("destroy", Gtk.main_quit)

    view = WebKit2.WebView()
    view.load_uri(url)
    win.add(view)
    win.show_all()

    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
