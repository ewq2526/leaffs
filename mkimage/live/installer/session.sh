#!/bin/sh
# 安装会话：起窗口管理器 → 起本地服务 → 等端口 → 用 WebKit 窗口显示安装页。
set -u

# 依赖镜像挂在 /run/camphor-deps（由入口脚本挂好）；这里再声明一次，
# xinit 起客户端时不保证把环境全带过来。
DEPS=/run/camphor-deps
if [ -d "$DEPS/usr/sbin" ]; then
	export PATH="$DEPS/usr/sbin:$DEPS/usr/bin:$PATH"
	export LD_LIBRARY_PATH="$DEPS/usr/lib/x86_64-linux-gnu:$DEPS/lib/x86_64-linux-gnu"
fi

export GTK_THEME=Adwaita:dark
xset s off -dpms s noblank 2>/dev/null || true
xsetroot -solid '#101014' 2>/dev/null || true

# 没有窗口管理器时 fullscreen() 不生效，窗口会停在默认尺寸，
# 页面拿到的视口只有几百像素宽，中文就一个一个字竖着排。
openbox --sm-disable &
sleep 1

PORT=8090
/usr/bin/python3 /run/camphor-installer/server.py "$PORT" &
i=0
while [ $i -lt 100 ]; do
	ss -ltn 2>/dev/null | grep -qE ":$PORT" && break
	sleep 0.1
	i=$((i + 1))
done

# 显示安装页要 gi + WebKit。这两样在主镜像里（python3-gi、gir1.2-webkit2-4.1），
# 不在 leaffs 的 venv 里 —— 那个 venv 属于应用层，安装时还不存在。
# 用系统解释器起窗口；没有就退回控制台，别让整个 X 会话跟着退出。
if /usr/bin/python3 -c 'import gi; gi.require_version("WebKit2", "4.1")' 2>/dev/null; then
	exec /usr/bin/python3 /run/camphor-installer/webview.py "http://127.0.0.1:$PORT/"
fi
echo "installer: 没有可用的 WebKit 解释器，退回控制台" >/dev/tty1
exec /bin/sh
