#!/bin/sh
# Camphor kiosk 会话：设置外观，然后以桌面形态运行 leaffs
# （窗口与服务同一进程，不涉及应用源码）
set -u

# 深色主题：GTK 原生控件与窗口底色跟随页面（页面默认深色底 #101014）
export GTK_THEME=Adwaita:dark

xset s off -dpms s noblank 2>/dev/null || true
# 根窗口铺成页面底色，避免启动瞬间露出白/灰底
xsetroot -solid '#101014' 2>/dev/null || true

# 面板 1920×1080 在 15.6 寸上约 141 PPI，X 因为 EDID 不给物理尺寸退回 96 DPI。
# GTK 侧（文件选择框、openbox 菜单）按 120 DPI 绘制；页面那 125% 由 app 里的
# WebKit zoom level 控制，两者作用于不同层，不叠加。
printf 'Xft.dpi: 120\n' | xrdb -merge 2>/dev/null || true

# 不调 xrandr：modeset 起来时已按 EDID 选好原生模式，屏幕就是 1920×1080。
# 之前那两次 xrandr --query 加一次 --auto 在冷显示器上要做数秒 EDID 探测，
# 那段时间正是屏幕黑的几秒。

openbox --sm-disable &
sleep 1

cd /opt/leaffs || exit 1
exec /opt/leaffs/venv/bin/python -m leaffs
