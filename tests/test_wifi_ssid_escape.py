# -*- coding: utf-8 -*-
r"""`system.wifi._unescape`：wpa_supplicant 转义过的 SSID 要还原成正常文本。

wpa_cli 的输出走的是 wpa_supplicant 自己的 printf_encode：非 ASCII 字节写成 `\xNN`，
所以中文 SSID 原样打到页面上就是 `\xe4\xb8\xad…` 这样一段源码。
"""
from leaffs.system import wifi


def test_chinese_ssid():
    assert wifi._unescape(r'\xe4\xb8\xad\xe6\x96\x87') == '中文'


def test_ascii_untouched():
    assert wifi._unescape('MyHome-5G') == 'MyHome-5G'


def test_backslash_and_quote():
    assert wifi._unescape(r'a\\b\"c') == 'a\\b"c'


def test_mixed_text():
    assert wifi._unescape(r'\xe6\xb5\x8b\xe8\xaf\x95 WiFi') == '测试 WiFi'


def test_invalid_utf8_does_not_raise():
    # 隐藏网络或一串坏字节：只要不炸，替换字符就替换字符
    assert wifi._unescape(r'\xff\xfe') == '\ufffd\ufffd'
