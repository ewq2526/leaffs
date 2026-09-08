# -*- coding: utf-8 -*-
"""兼容垫片 —— 直接运行 leaffs/leaffs.py 的入口。

实际实现已迁至 leaffs.app（装配与生命周期）；此处只注入项目根到 sys.path
（支持把本文件当脚本直接运行）并转发 start_server。
"""
import os
import sys

_proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

from leaffs.app import start_server  # noqa: E402,F401

if __name__ == '__main__':
    start_server()
