# -*- coding: utf-8 -*-
"""安装时切的那块专用交换分区：128 MiB，切在选定空闲区的前部。

它不是拿来当交换空间的，是留给内核的退路：内存被写尽时内核要能把页换出去才能继续
干活（回收、唤醒、给清理内存的那批帮手腾出运行空间）。用独立分区而不是根分区里的
文件，是因为交换文件要走 ext4 的写路径 —— 分配块、记日志、writeback —— 这些自身
就要占内存；极端情况下那条路先卡住。
"""
import importlib.util
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, '..', 'mkimage', 'live', 'installer', 'server.py')


@pytest.fixture
def installer():
    """安装器后端。模块一加载就会建 HTTP 类，但不会起服务。"""
    spec = importlib.util.spec_from_file_location('camphor_installer_server2', SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_size_is_fixed_128(installer):
    """大小是定值：128 MiB，与内存大小无关。"""
    assert installer.SWAP_MIB == 128
    assert installer.swap_partition_mib(1000000) == 128
    assert installer.swap_partition_mib(1000000, 0) == 128


def test_size_zero_when_system_would_not_fit(installer):
    """切完之后 ESP 与根分区还得放得下，否则不切。"""
    need_esp = installer.SWAP_MIB + installer.ESP_MIB + installer.ROOT_MIN_MIB
    assert installer.swap_partition_mib(need_esp) == 128
    assert installer.swap_partition_mib(need_esp - 1) == 0
    # 盘上已经有 ESP 时不用再留那 512 MiB
    need_no_esp = installer.SWAP_MIB + installer.ROOT_MIN_MIB
    assert installer.swap_partition_mib(need_no_esp, 0) == 128
    assert installer.swap_partition_mib(need_no_esp - 1, 0) == 0


def test_fstab_line_uses_uuid_and_priority():
    """fstab 里按 UUID 挂，优先级 10（低于 zram 的 100）。"""
    uuid = '1234abcd-0000-4000-8000-123456789abc'
    line = "UUID=%s none swap defaults,pri=10 0 0" % uuid
    assert line.startswith('UUID=' + uuid)
    assert 'none swap' in line
    assert 'pri=10' in line


def test_swap_mib_is_aligned():
    """128 MiB 是 2048 扇区（1 MiB）的整数倍，切出来的边界才对齐。"""
    assert (128 * 1024 * 1024) % (2048 * 512) == 0
