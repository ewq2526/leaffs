"""本机状态采集：只读 /proc、/sys 与 systemctl，不写任何东西。

这个模块只回答问题"这台机器现在是什么样"，不做决策。权限、会话、是否允许操作
都不在这里 —— 那是 handler 的事。
"""

import os
import re
import shutil
import subprocess
import threading
import time

# 这台机器上真正会跑的服务。用固定清单而不是通配，避免把 systemd 的临时单元
# 也算进来：页面上要的是"这台机器承诺跑的东西有没有活着"。
SERVICES = (
    'camphor-kiosk',
    'camphor-ssh',
    'camphor-wifi',
    'camphor-trust-cert',
    'ksmbd',
    'wsdd2',
    'avahi-daemon',
)

# 伪文件系统不进磁盘列表
_SKIP_FSTYPES = {
    'proc', 'sysfs', 'devtmpfs', 'devpts', 'tmpfs', 'cgroup', 'cgroup2',
    'overlay', 'squashfs', 'ramfs', 'debugfs', 'tracefs', 'securityfs',
    'pstore', 'bpf', 'autofs', 'mqueue', 'hugetlbfs', 'fusectl', 'configfs',
    'efivarfs', 'nsfs', 'rpc_pipefs',
}


def _read(path, default=''):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read().strip()
    except OSError:
        return default


def _unquote(s):
    """fstab / mounts 里的空格与八进制转义"""
    out = []
    i = 0
    while i < len(s):
        if s[i] == '\\' and i + 3 < len(s) and s[i + 1:i + 2].isdigit():
            try:
                out.append(chr(int(s[i + 1:i + 4], 8)))
                i += 4
                continue
            except ValueError:
                pass
        out.append(s[i])
        i += 1
    return ''.join(out)


def _read_mounts():
    mounts = []
    for line in _read('/proc/mounts').splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        dev, mnt, fstype, opts = parts[0], _unquote(parts[1]), parts[2], parts[3]
        if fstype in _SKIP_FSTYPES:
            continue
        if not dev.startswith('/dev/'):
            continue
        mounts.append({'device': dev, 'mount': mnt, 'fstype': fstype, 'options': opts})
    return mounts


def _usage(path):
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - st.f_bfree * st.f_frsize
    percent = round(used * 100.0 / total) if total else 0
    return {'total': total, 'used': used, 'free': free, 'percent': percent}


def disks():
    """按物理盘枚举，而不是按挂载点。

    以前只列"已挂载的文件系统"，于是没挂的盘（启动盘的 ESP、内置盘的分区）完全不出现，
    也看不出"哪块盘在读写"。现在每块盘给出型号、容量、是否机械盘，下面挂它的分区与用量。
    """
    mounts = {}
    for m in _read_mounts():
        mounts[m['device']] = m

    out = []
    try:
        names = sorted(os.listdir('/sys/block'))
    except OSError:
        names = []
    for name in names:
        if not _DISK_RE.match(name):
            continue
        base = os.path.join('/sys/block', name)
        try:
            size = int(_read(os.path.join(base, 'size'), '0') or 0) * 512
        except ValueError:
            size = 0
        model = _read(os.path.join(base, 'device', 'model')).strip() or name
        rotational = _read(os.path.join(base, 'queue', 'rotational')) == '1'
        parts = []
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            entries = []
        for part in entries:
            pdir = os.path.join(base, part)
            if not os.path.exists(os.path.join(pdir, 'partition')):
                continue
            dev = '/dev/' + part
            mnt = mounts.get(dev)
            u = _usage(mnt['mount']) if mnt else None
            fstype = mnt['fstype'] if mnt else ''
            if not fstype:
                for line in _read(os.path.join(pdir, 'uevent')).splitlines():
                    if line.startswith('FSTYPE='):
                        fstype = line.split('=', 1)[1]
                        break
            parts.append({
                'name': part,
                'device': dev,
                'size': int(_read(os.path.join(pdir, 'size'), '0') or 0) * 512,
                'mount': mnt['mount'] if mnt else '',
                'fstype': fstype,
                'readonly': ('ro' in mnt['options'].split(',')) if mnt else False,
                'total': (u or {}).get('total', 0),
                'used': (u or {}).get('used', 0),
                'percent': (u or {}).get('percent', 0),
            })
        out.append({
            'name': name,
            'model': model,
            'size': size,
            'rotational': rotational,
            'partitions': parts,
        })
    out.sort(key=lambda d: d['name'])
    return out

def _service_state(name):
    """systemctl 一次只问一个单元：批量时某个单元不存在会拖垮整条命令。"""
    exe = shutil.which('systemctl') or '/usr/bin/systemctl'
    try:
        r = subprocess.run([exe, 'is-active', name], capture_output=True,
                           text=True, timeout=3)
        state = (r.stdout or '').strip() or 'unknown'
    except (OSError, subprocess.SubprocessError):
        state = 'unknown'
    return state


def services():
    """七个单元一次问完。

    `systemctl is-active` 接受多个单元、按同样顺序每行一个状态，所以一次子进程就够；
    逐个问是七个进程，实测单次 collect 的 58 ms 里 43 ms 都花在建进程上。
    """
    exe = shutil.which('systemctl') or '/usr/bin/systemctl'
    try:
        r = subprocess.run([exe, 'is-active', *SERVICES], capture_output=True,
                           text=True, timeout=5)
        states = (r.stdout or '').split()
        if len(states) == len(SERVICES):
            return [{'name': n, 'state': s} for n, s in zip(SERVICES, states)]
    except (OSError, subprocess.SubprocessError):
        pass
    # 输出对不上（某个单元名被改过等）才退回逐个问，结果宁可慢也不能给半张表
    return [{'name': n, 'state': _service_state(n)} for n in SERVICES]


def summary():
    up = _read('/proc/uptime', '0').split()
    try:
        uptime = int(float(up[0]))
    except (IndexError, ValueError):
        uptime = 0
    load = _read('/proc/loadavg', '').split()
    return {
        'hostname': _read('/proc/sys/kernel/hostname'),
        'kernel': os.uname().release,
        'arch': os.uname().machine,
        'cpu_count': os.cpu_count() or 0,
        'uptime': uptime,
        'boot_at': int(time.time()) - uptime,
        'load1': load[0] if load else '',
        'load5': load[1] if len(load) > 1 else '',
        'load15': load[2] if len(load) > 2 else '',
        'now': int(time.time()),
    }


# ---------- 利用率：/proc/stat 只有累计值，百分比必须两次采样相减 ----------

_cpu_lock = threading.Lock()
_last_cpu = {}


def _cpu_rows():
    """读 /proc/stat 的 cpu / cpuN 行，返回 [(名字, 空闲累计, 总累计)]"""
    rows = []
    for line in _read('/proc/stat').splitlines():
        if not line.startswith('cpu'):
            break
        parts = line.split()
        try:
            vals = [int(x) for x in parts[1:]]
        except ValueError:
            continue
        if len(vals) < 4:
            continue
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
        rows.append((parts[0], idle, sum(vals)))
    return rows


def cpu():
    """总占用与每核占用（0~100）。第一次调用没有基线，返回 0。"""
    result = {'total': 0.0, 'cores': []}
    with _cpu_lock:
        for name, idle, total in _cpu_rows():
            prev = _last_cpu.get(name)
            pct = 0.0
            if prev and total > prev[1]:
                d_total = total - prev[1]
                d_idle = idle - prev[0]
                pct = 100.0 * (d_total - d_idle) / d_total
                pct = max(0.0, min(100.0, pct))
            _last_cpu[name] = (idle, total)
            if name == 'cpu':
                result['total'] = round(pct, 1)
            else:
                result['cores'].append(round(pct, 1))
    return result


def memory():
    info = {}
    for line in _read('/proc/meminfo').splitlines():
        key, _, rest = line.partition(':')
        try:
            info[key] = int(rest.split()[0]) * 1024
        except (IndexError, ValueError):
            continue
    total = info.get('MemTotal', 0)
    available = info.get('MemAvailable', 0)
    used = max(0, total - available)

    # 拆成若干块，加起来等于总量。只看一个"已用"看不出结构 —— 里面近三分之一是内核 slab。
    #   进程 = AnonPages（私有匿名页）
    #   共享 = Shmem（tmpfs：/tmp、/run、/dev/shm；它也计在 Cached 里，必须单独摘出来）
    #   缓存 = Buffers + (Cached - Shmem) + SReclaimable（可回收，需要时会让出来）
    #   内核 = SUnreclaim + KernelStack + PageTables + Percpu（不可回收）
    #   其余 = 总量减掉以上与 MemFree，通常是驱动与保留页
    def g(key):
        return info.get(key, 0)

    free = g('MemFree')
    shmem = g('Shmem')
    cache = g('Buffers') + max(0, g('Cached') - shmem) + g('SReclaimable')
    anon = g('AnonPages')
    kernel = g('SUnreclaim') + g('KernelStack') + g('PageTables') + g('Percpu')
    other = max(0, total - free - cache - anon - kernel - shmem)
    swap_total = g('SwapTotal')
    swap_free = g('SwapFree')
    return {
        'total': total,
        'used': used,
        'available': available,
        'percent': round(used * 100.0 / total) if total else 0,
        'free': free,
        'cache': cache,
        'shmem': shmem,
        'anon': anon,
        'kernel': kernel,
        'other': other,
        'swap_total': swap_total,
        'swap_used': max(0, swap_total - swap_free),
    }


def temperatures():
    """温度：thermal_zone + hwmon。值为 0 的节点丢弃 —— acpitz 在不少机器上恒为 0，
    那不是"0 度"，是没读数。"""
    out = []
    seen = set()
    base = '/sys/class/thermal'
    try:
        zones = sorted(n for n in os.listdir(base) if n.startswith('thermal_zone'))
    except OSError:
        zones = []
    for z in zones:
        raw = _read(os.path.join(base, z, 'temp'))
        label = _read(os.path.join(base, z, 'type')) or z
        try:
            celsius = int(raw) / 1000.0
        except ValueError:
            continue
        if celsius <= 0:
            continue
        key = (label, round(celsius, 1))
        if key in seen:
            continue
        seen.add(key)
        out.append({'label': label, 'celsius': round(celsius, 1)})
    return out


def power():
    """电池与适配器。没有电池的台式机返回 present=False，页面自行隐藏。"""
    base = '/sys/class/power_supply'
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return {'battery': {'present': False}, 'ac_online': None}
    battery = {'present': False}
    ac_online = None
    for name in names:
        d = os.path.join(base, name)
        cap = _read(os.path.join(d, 'capacity'))
        if name.startswith('BAT') and cap.isdigit() and not battery['present']:
            def _num(field):
                v = _read(os.path.join(d, field))
                return int(v) if v.lstrip('-').isdigit() else None
            battery = {
                'present': True,
                'name': name,
                'capacity': int(cap),
                'status': _read(os.path.join(d, 'status')),
                'energy_now': _num('energy_now'),
                'energy_full': _num('energy_full'),
                'power_now': _num('power_now'),
            }
        elif name.startswith(('AC', 'ADP')):
            online = _read(os.path.join(d, 'online'))
            if online.isdigit():
                ac_online = online == '1'
    return {'battery': battery, 'ac_online': ac_online}


# ---------- 磁盘 I/O：/proc/diskstats 也是累计值，同样靠两次采样相减 ----------

_io_lock = threading.Lock()
_last_io = {}
_DISK_RE = re.compile(r'^(sd[a-z]+|nvme\d+n\d+|mmcblk\d+)$')


def _diskstats():
    """整盘计数（不含分区，否则同一次写入会被算两遍）。"""
    rows = {}
    for line in _read('/proc/diskstats').splitlines():
        parts = line.split()
        if len(parts) < 14:
            continue
        name = parts[2]
        if not _DISK_RE.match(name):
            continue
        try:
            # 5/9 是读/写扇区，3/7 是读/写次数，12 是 io_ticks（设备忙碌的毫秒数）
            rows[name] = (int(parts[5]), int(parts[9]), int(parts[3]), int(parts[7]), int(parts[12]))
        except ValueError:
            continue
    return rows


def io():
    """每块盘的读写速率与 IOPS。首次调用没有基线，返回空字典。"""
    now = time.time()
    rows = _diskstats()
    out = {}
    with _io_lock:
        for name, cur in rows.items():
            prev = _last_io.get(name)
            if prev is not None:
                dt = now - prev[0]
                if dt > 0.2:                    # 间隔太短算出来的百分比没意义
                    rd, wr, rdi, wri, ticks = cur
                    prd, pwr, prdi, pwri, pticks = prev[1]
                    # busy：io_ticks 是"设备有 I/O 在跑"的毫秒数，除以间隔就是占用率，
                    # 与任务管理器的"活动时间"同口径；单核排队再重也不会超过 100%
                    busy = min(100.0, max(0.0, (ticks - pticks) / 1000.0 / dt * 100.0))
                    out[name] = {
                        'read_bps': max(0, (rd - prd) * 512 / dt),
                        'write_bps': max(0, (wr - pwr) * 512 / dt),
                        'read_iops': max(0, (rdi - prdi) / dt),
                        'write_iops': max(0, (wri - pwri) / dt),
                        'busy': round(busy, 1),
                    }
            _last_io[name] = (now, cur)
    return out


def collect():
    return {
        'summary': summary(),
        'metrics': {
            'cpu': cpu(),
            'memory': memory(),
            'temperatures': temperatures(),
            'power': power(),
            'io': io(),
        },
        'disks': disks(),
        'services': services(),
    }
