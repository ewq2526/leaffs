# -*- coding: utf-8 -*-
"""装好的系统里，菜单右上角写明从菜单启动 Windows 的代价与前提。

PIN 与 BitLocker 都封在 TPM 量到的启动链上：固件直接加载 bootmgfw 是一条链，
shim → grub → chainloader 是另一条。从菜单进 Windows 走的是后一条，所以要说清楚
"会让 PIN 重置一次"和"第一次进之前先关掉 BitLocker"。

提示只写进装好系统的启动菜单主题（/boot/grub/themes/camphor/theme.txt）：介质菜单里没有 Windows
项，写上去只会让人莫名其妙。
"""
import importlib.util
import os
import shutil

import pytest

from conftest import new_data_root

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, '..', 'mkimage', 'live', 'installer', 'server.py')

THEME = """title-text: ""

+ label {
\ttext = "@KEYMAP_SHORT@"
}
"""


@pytest.fixture
def installer():
    """安装器后端。模块一加载就会建 HTTP 类，但不会起服务。"""
    spec = importlib.util.spec_from_file_location('camphor_installer_hints', SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _target(with_windows=True, with_theme=True):
    root = new_data_root('hint_')
    if with_theme:
        d = os.path.join(root, 'boot', 'grub', 'themes', 'camphor')
        os.makedirs(d)
        with open(os.path.join(d, 'theme.txt'), 'w', encoding='utf-8') as f:
            f.write(THEME)
    if with_windows:
        d = os.path.join(root, 'boot', 'efi', 'EFI', 'Microsoft', 'Boot')
        os.makedirs(d)
        with open(os.path.join(d, 'bootmgfw.efi'), 'wb') as f:
            f.write(b'MZ')
    return root


def test_no_windows_no_hint(installer):
    """介质上没有 Windows，装好系统的启动菜单里也不该多出这几行。"""
    root = _target(with_windows=False)
    try:
        assert installer.install_windows_hints(root) is False
        body = open(os.path.join(root, 'boot', 'grub', 'themes', 'camphor', 'theme.txt'),
                    encoding='utf-8').read()
        assert body == THEME
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_hint_mentions_pin_and_bitlocker(installer):
    root = _target()
    try:
        path = os.path.join(root, 'boot', 'grub', 'themes', 'camphor', 'theme.txt')
        assert installer.install_windows_hints(root) is True
        body = open(path, encoding='utf-8').read()
        assert body.startswith(THEME), '原有主题内容不能被改掉'
        assert 'PIN 码重置一次' in body
        assert 'BitLocker' in body
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_hint_is_a_framed_box_in_the_corner(installer):
    """右上角一个白框：框是块字符拼的，不贴图；字跟菜单同一份。"""
    root = _target()
    try:
        path = os.path.join(root, 'boot', 'grub', 'themes', 'camphor', 'theme.txt')
        assert installer.install_windows_hints(root) is True
        body = open(path, encoding='utf-8').read()
        assert 'progress_bar' not in body, body
        # 左右各一列全块，上下各一行半块；横线要正好铺满框宽
        rows = installer.HINT_HEIGHT // installer.HINT_ROW
        box_width = installer.HINT_WIDTH + 2 * installer.HINT_PAD
        assert box_width % installer.HINT_CELL == 0
        assert body.count('█') == rows * 2
        assert body.count('▀') == body.count('▄') == box_width // installer.HINT_CELL
        assert body.count('+ label') == 1 + rows * 2 + 2 + len(installer.WINDOWS_MENU_HINTS)
        assert body.count('\tcolor = "#ffffff"') == rows * 2 + 2 + len(installer.WINDOWS_MENU_HINTS)
        assert 'font = "Unifont Regular 16"' in body
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_hint_written_once(installer):
    """重复跑不会叠第二份。"""
    root = _target()
    try:
        path = os.path.join(root, 'boot', 'grub', 'themes', 'camphor', 'theme.txt')
        assert installer.install_windows_hints(root) is True
        assert installer.install_windows_hints(root) is False
        body = open(path, encoding='utf-8').read()
        assert body.count(installer.WINDOWS_MENU_HINTS[0]) == 1
    finally:
        shutil.rmtree(root, ignore_errors=True)
