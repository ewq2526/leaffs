# -*- coding: utf-8 -*-
"""内置依赖的跨平台查找：Windows 以 .exe 分发，其它平台同名无后缀。

调用方此前一律写死 "aria2c.exe" / "ffmpeg.exe"，Linux 上永远命中不了。
"""
import os

import leaffs.paths as paths
from leaffs.paths import find_bundled_exe


def _probe_names(monkeypatch, name):
    """拦掉真实文件系统与 PATH，只记录 find_bundled_exe 实际探过的名字"""
    seen = []

    def fake_isfile(p):
        seen.append(os.path.basename(p))
        return False

    def fake_which(n):
        seen.append(n)
        return None

    monkeypatch.setattr(paths.os.path, 'isfile', fake_isfile)
    monkeypatch.setattr(paths.shutil, 'which', fake_which)
    find_bundled_exe(name)
    return seen


def test_bare_name(monkeypatch):
    """传基名：Windows 补 .exe，其它平台原样"""
    seen = _probe_names(monkeypatch, 'aria2c')
    if os.name == 'nt':
        assert seen[0] == 'aria2c.exe'
    else:
        assert seen and all(n == 'aria2c' for n in seen)


def test_exe_name_strips_suffix_off_windows(monkeypatch):
    """传 .exe：非 Windows 上剥掉后缀再找"""
    seen = _probe_names(monkeypatch, 'aria2c.exe')
    if os.name != 'nt':
        assert 'aria2c' in seen


def test_missing_binary_returns_none(monkeypatch):
    monkeypatch.setattr(paths.os.path, 'isfile', lambda p: False)
    monkeypatch.setattr(paths.shutil, 'which', lambda n: None)
    assert find_bundled_exe('leaffs-no-such-binary') is None
