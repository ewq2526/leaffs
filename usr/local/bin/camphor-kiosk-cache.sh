#!/bin/sh
# kiosk 窗口启动前的一步：把 WebKit 的 HSTS 库放到内存里。
#
# WebKit 的存储目录整个放在盘上（Cookie 要跨重启），但里面只有 hsts/hsts-storage.sqlite
# 在页面每次轮询时都可能提交一次写。实测 20 秒里那个目录只有它在变，其它文件不动。
# 于是用 tmpfs 盖住这一个子目录：HSTS 走内存，Cookie 仍旧落盘。
set -u

BASE=${LEAFFS_PROJECT_ROOT:-/srv/leaffs}/.cache/webview/hsts

mkdir -p "$BASE" 2>/dev/null || exit 0
mountpoint -q "$BASE" && exit 0
mount -t tmpfs -o size=8M,mode=0700 tmpfs "$BASE" 2>/dev/null || true
exit 0
