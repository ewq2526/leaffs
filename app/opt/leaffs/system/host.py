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


def _udev_fs_type(major_minor):
    """分区上的文件系统类型。

    内核不提供这个信息：/sys/class/block/<分区>/uevent 里只有 DEVNAME/DEVTYPE/
    PARTN 这些，没有 FSTYPE（以前就是照这个读的，于是没挂载的分区一律显示空）。
    真正的类型是 blkid 探出来的，udev 把结果记在 /run/udev/data/b<major>:<minor>
    里，读它既不用开设备、也不会把睡下去的盘叫醒。
    """
    if not major_minor:
        return ''
    for line in _read('/run/udev/data/b' + major_minor).splitlines():
        if line.startswith('E:ID_FS_TYPE='):
            return line.split('=', 1)[1]
    return ''


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
                fstype = _udev_fs_type(_read(os.path.join(pdir, 'dev')))
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


def _num_or_none(value):
    """systemd 的数值属性：没开账目时是 `[not set]`，未知时是 UINT64_MAX。

    这两种都不该显示成 1844 亿 GB，当"没有"处理。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 0 <= n < (1 << 62) else None


def _parse_show(text):
    """`systemctl show` 多单元输出 → {Id: {属性: 值}}。块之间以空行分隔。"""
    out, cur = {}, {}
    for line in text.splitlines():
        if not line.strip():
            if cur.get('Id'):
                out[cur['Id']] = cur
            cur = {}
            continue
        key, _, value = line.partition('=')
        cur[key.strip()] = value.strip()
    if cur.get('Id'):
        out[cur['Id']] = cur
    return out


def _service_row(name, props):
    props = props or {}
    return {
        'name': name,
        'state': props.get('ActiveState') or 'unknown',
        'pid': _num_or_none(props.get('MainPID')) or 0,
        'memory': _num_or_none(props.get('MemoryCurrent')),
        'cpu_ns': _num_or_none(props.get('CPUUsageNSec')),
        'desc': props.get('Description') or '',
    }


def services():
    """七个单元一次问完，顺带回占用。

    `systemctl is-active` 只给状态，内存与累计 CPU 时间还得再问一次；`systemctl show`
    一次就能接受多个单元、按单元分块输出，所以加上占用仍然只有一次子进程 ——
    与原来 is-active 那一趟同价。
    """
    exe = shutil.which('systemctl') or '/usr/bin/systemctl'
    props = 'Id,ActiveState,MainPID,MemoryCurrent,CPUUsageNSec,Description'
    try:
        r = subprocess.run([exe, 'show', '-p', props, *SERVICES],
                           capture_output=True, text=True, timeout=5)
        got = _parse_show(r.stdout or '')
    except (OSError, subprocess.SubprocessError):
        got = {}
    if len(got) == len(SERVICES):
        return [_service_row(n, got.get(n + '.service') or got.get(n)) for n in SERVICES]
    # 输出对不上（某个单元名被改过等）才退回逐个问，结果宁可慢也不能给半张表
    return [{'name': n, 'state': _service_state(n)} for n in SERVICES]


def processes(limit=8):
    """内存占用前 N 的进程。跟服务一起走慢档，30 秒一次。

    statm 的第二列是常驻页数，comm 是进程名 —— 都是小文件，不开 fork。
    CPU 时间不在这里给：逐进程差分要留着上一拍的累计值，而 30 秒一档的差分
    对"谁在占 CPU"几乎没有解释力，不如只按内存排。
    """
    try:
        page = os.sysconf('SC_PAGE_SIZE')
    except (ValueError, OSError):
        page = 4096
    try:
        entries = os.listdir('/proc')
    except OSError:
        return []
    out = []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open('/proc/' + entry + '/statm', 'rb') as f:
                fields = f.read().split()
            rss = int(fields[1]) * page
        except (OSError, IndexError, ValueError):
            continue
        if rss <= 0:
            continue
        try:
            with open('/proc/' + entry + '/comm', encoding='utf-8', errors='replace') as f:
                comm = f.read().strip()
        except OSError:
            continue
        out.append({'pid': int(entry), 'name': comm or entry, 'rss': rss})
    out.sort(key=lambda p: (-p['rss'], p['pid']))
    return out[:limit]


def timezone():
    """当前时区名（`Asia/Shanghai` 这种）。

    /etc/localtime 通常是指向 /usr/share/zoneinfo/<名字> 的符号链接，读链接目标
    比解析 TZif 简单；不是链接时退回 /etc/timezone（Debian 另存的一份）。
    """
    try:
        target = os.path.realpath('/etc/localtime')
        if 'zoneinfo/' in target:
            return target.split('zoneinfo/', 1)[1]
    except OSError:
        pass
    try:
        with open('/etc/timezone', encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


_zones = None


def timezones():
    """可选时区列表，取自 /usr/share/zoneinfo/zone.tab 的第三列。"""
    global _zones
    if _zones is not None:
        return _zones
    out = []
    try:
        with open('/usr/share/zoneinfo/zone.tab', encoding='utf-8') as f:
            for line in f:
                if line.startswith('#'):
                    continue
                cols = line.rstrip('\n').split('\t')
                if len(cols) >= 3 and cols[2]:
                    out.append(cols[2])
    except OSError:
        pass
    out.sort()
    _zones = out
    return out


# ---------- 利用率：/proc/stat 只有累计值，百分比必须两次采样相减 ----------

_cpu_lock = threading.Lock()
_last_cpu = {}
_net_lock = threading.Lock()
_last_net = {}


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


def io(now=None):
    """每块盘的读写速率与 IOPS。首次调用没有基线，返回空字典。

    `now` 由调用方给：一拍里所有速率都用同一个时刻，互相对得上；不给就自己取。
    """
    now = time.time() if now is None else now
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


# ---------- 三档：每秒档 / 状态档 / 静态档 ----------
# 分档的判据不是"界面多久刷一次"，而是**值会不会变**：
#   fast   —— /proc 里四个小文件，一秒一变，读一次几毫秒，零 fork；
#   slow   —— 分区、服务、无线、温度、电池：一人一秒内不可能变，由事件（或兜底周期）触发；
#   static —— 机器标识与时区：连接期间不变。
# 以前是 `collect()` 把三档捆在一起按最快的节奏跑，于是每秒 fork 一次 systemctl、
# 每秒遍历 /sys、每秒 statvfs 一遍，而其中没有一样是每秒会变的。

_boot_at = None


def statics():
    """连接期间不变的东西。`boot_at` 只在进程内算一次：重启会重开进程。"""
    global _boot_at
    if _boot_at is None:
        up = _read('/proc/uptime', '0').split()
        try:
            _boot_at = int(time.time()) - int(float(up[0]))
        except (IndexError, ValueError):
            _boot_at = 0
    return {
        'host': {
            'name': _read('/proc/sys/kernel/hostname'),
            'kernel': os.uname().release,
            'arch': os.uname().machine,
            'cores': os.cpu_count() or 0,
            'tz': timezone(),
        },
        'boot_at': _boot_at,
    }


def _net_dev():
    """/proc/net/dev 里每块网卡的累计收/发字节数。回环与容器网桥不列。"""
    rows = {}
    try:
        with open('/proc/net/dev', encoding='ascii', errors='replace') as f:
            for line in f:
                name, _, rest = line.partition(':')
                name = name.strip()
                if not name or name == 'lo' or name.startswith(('veth', 'docker', 'br-')):
                    continue
                cols = rest.split()
                if len(cols) < 9:
                    continue
                try:
                    rows[name] = (int(cols[0]), int(cols[8]))
                except ValueError:
                    continue
    except OSError:
        return {}
    return rows


def net(now=None):
    """每块网卡的收发速率（字节/秒）。与 io() 同一套：拿累计值差分，不 fork。

    `now` 同上：一拍里所有速率共用一个时刻。
    """
    now = time.time() if now is None else now
    rows = _net_dev()
    out = {}
    with _net_lock:
        for name, cur in rows.items():
            prev = _last_net.get(name)
            if prev is not None:
                dt = now - prev[0]
                if dt > 0.2:
                    prx, ptx = prev[1]
                    out[name] = {
                        'rx_bps': max(0, (cur[0] - prx) / dt),
                        'tx_bps': max(0, (cur[1] - ptx) / dt),
                    }
            _last_net[name] = (now, cur)
    return out


def fast():
    """每秒档：一次采齐，五个 /proc 文件各读一次。

    /proc 没有"一次读打包"这种东西，每个文件各自是一个视图，所以顺序读五个；
    但一拍之内同一个文件只读一次，而且**所有速率共用一个时刻**（`now`）——
    否则同一帧里磁盘和网卡的间隔起点差几毫秒，两条曲线对不齐。
    """
    now = time.time()
    load = _read('/proc/loadavg', '').split()
    cpu_data = cpu()
    mem_data = memory()
    io_data = io(now)
    net_data = net(now)
    return {
        'load': {
            'l1': load[0] if len(load) > 0 else '',
            'l5': load[1] if len(load) > 1 else '',
            'l15': load[2] if len(load) > 2 else '',
        },
        'cpu': cpu_data,
        'mem': mem_data,
        'io': io_data,
        'net': net_data,
    }


def slow():
    """状态档：分区与挂载、服务、无线、温度、电池、进程榜。"""
    import leaffs.system.wifi as _wifi
    return {
        'temp': temperatures(),
        'power': power(),
        'disks': disks(),
        'services': services(),
        'procs': processes(),
        'wifi': _wifi.status(),
    }


def reseed():
    """断档后重建基线。

    cpu、io、net 都是累计值相减，采样停了以后再算会把整段间隔平均进去；没订阅者时
    循环不采样（这是有意的），所以重新有人订阅时要先把基线清掉、取一个新样本，
    让接下来那一拍是真正的「一秒之差」。
    """
    with _cpu_lock:
        _last_cpu.clear()
    with _io_lock:
        _last_io.clear()
    with _net_lock:
        _last_net.clear()
    cpu()
    io()
    net()


def snapshot(scope='full'):
    """一整份。`scope='fast'` 只给每秒档 —— 断线兜底轮询用，慢档一个字段都不采。"""
    data = dict(statics())
    data.update(fast())
    if scope != 'fast':
        data.update(slow())
    return data
