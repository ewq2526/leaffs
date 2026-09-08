#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
下载器核心模块
支持：HTTP/HTTPS 直链、m3u8 流视频、种子文件、磁力链接
"""

from .dl_utils import (
    sanitize_filename, format_size, format_speed,
    decode_thunder, parse_content_disposition, get_extension_from_mime,
    bdecode, parse_torrent_data, parse_torrent_url,
    get_secure_client, has_aria2c, ARIA2C_PATH,
)
from .dl_http import HttpDownloader
from .dl_m3u8 import M3u8Downloader
from .dl_torrent import TorrentDownloader
from .dl_magnet import MagnetDownloader
from .dl_core import DownloadManager, get_downloader_for_url

__all__ = [
    'sanitize_filename', 'format_size', 'format_speed',
    'decode_thunder', 'parse_content_disposition', 'get_extension_from_mime',
    'bdecode', 'parse_torrent_data', 'parse_torrent_url',
    'get_secure_client', 'has_aria2c', 'ARIA2C_PATH',
    'HttpDownloader', 'M3u8Downloader', 'TorrentDownloader', 'MagnetDownloader',
    'DownloadManager', 'get_downloader_for_url',
]
