# -*- coding: utf-8 -*-
"""下载管理器进程级单例与装配。

历史：实例化于 leaffs.py 顶部（_DLManager() + dl_api.set_dl_manager），
重构抽出；HTTP/WS 处理器与 app 启动统一经 get_manager() 获取同一实例。
"""
from leaffs.dl import dl_api as _dl_api
from leaffs.dl.dl_core import DownloadManager

_manager = DownloadManager()
_dl_api.set_dl_manager(_manager)


def get_manager():
    return _manager
