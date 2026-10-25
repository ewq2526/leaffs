# -*- coding: utf-8 -*-
"""`system.host._udev_fs_type`：分区文件系统类型要从 udev 的库里读。

内核不在 sysfs 的 uevent 里提供 FSTYPE（那里只有 DEVNAME/DEVTYPE/PARTN），
照 uevent 读的结果是"没挂载的分区一律没有文件系统"。
"""
from leaffs.system import host


def test_reads_type_from_udev_db(monkeypatch):
    seen = []

    def fake_read(path, default=''):
        seen.append(path)
        if path == '/run/udev/data/b8:3':
            return 'S:disk/by-id/x\nE:ID_FS_TYPE=ntfs\nE:ID_FS_UUID=06BC\n'
        return default

    monkeypatch.setattr(host, '_read', fake_read)
    assert host._udev_fs_type('8:3') == 'ntfs'
    assert seen == ['/run/udev/data/b8:3']


def test_missing_db_returns_empty(monkeypatch):
    monkeypatch.setattr(host, '_read', lambda path, default='': default)
    assert host._udev_fs_type('8:3') == ''


def test_no_major_minor(monkeypatch):
    monkeypatch.setattr(host, '_read', lambda path, default='': 'E:ID_FS_TYPE=ext4')
    assert host._udev_fs_type('') == ''
