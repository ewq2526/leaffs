#!/bin/sh
# Camphor kiosk 会话：扫描输出，按显示器原生模式铺满，然后以桌面形态运行 leaffs
# （窗口与服务同一进程，不涉及应用源码）

set -u
log() { echo "kiosk-session: $*"; }

# 深色主题：GTK 原生控件与窗口底色跟随页面（页面默认深色底 #101014）
export GTK_THEME=Adwaita:dark

xset s off -dpms s noblank 2>/dev/null || true
# 根窗口铺成页面底色，避免启动瞬间露出白/灰底
xsetroot -solid '#101014' 2>/dev/null || true

# 面板 1920×1080 在 15.6 寸上约 141 PPI，X 因为 EDID 不给物理尺寸退回 96 DPI。
# GTK 侧（文件选择框、openbox 菜单）按 120 DPI 绘制；页面那 125% 由 app 里的
# WebKit zoom level 控制，两者作用于不同层，不叠加。
printf 'Xft.dpi: 120\n' | xrdb -merge 2>/dev/null || true

# 找第一个已连接的输出；多屏时全部按原生模式点亮
outputs=$(xrandr --query 2>/dev/null | awk '/ connected/ {print $1}')
[ -n "$outputs" ] || { log "没有检测到已连接的输出"; }

for out in $outputs; do
    xrandr --output "$out" --auto 2>/dev/null && log "输出 $out 使用原生模式"
done

geom=$(xrandr --query 2>/dev/null | sed -n 's/^Screen 0:.*current \([0-9]* x [0-9]*\).*/\1/p')
log "当前屏幕几何: ${geom:-未知}"

openbox &
sleep 1

cd /opt/leaffs || exit 1
exec /opt/leaffs/venv/bin/python -m leaffs