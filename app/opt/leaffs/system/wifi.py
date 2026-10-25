"""无线网络：当前状态、扫描、连接。

全部通过 `wpa_supplicant` 自己的控制接口（`wpa_cli`）完成，不自己解析或改写配置文件：

    status / scan / scan_results    读
    list_networks                   已保存的网络
    add_network / set_network / remove_network / enable_network / reassociate / save_config   写

`save_config` 负责持久化 —— 配置文件里本来就有 `update_config=1`，所以不用碰文件也不用管转义。
这台机器没有 NetworkManager，`camphor-wifi` 单元用固定配置文件拉起 wpa_supplicant，
控制套接字在 `/run/wpa_supplicant/`，只有 root 能连（服务本身以 root 运行）。
"""

import fcntl
import os
import re
import socket
import struct
import subprocess
import time

CTRL_DIR = '/run/wpa_supplicant'
CLI = '/sbin/wpa_cli'


def _unescape(text):
    """把 wpa_supplicant 转义过的字符串还原成正常文本。

    wpa_cli 输出的 SSID 走的是 wpa_supplicant 自己的 printf_encode：非 ASCII 的
    字节写成 `\\xNN`（中文 SSID 就是 `\\xe4\\xb8\\xad…`），反斜杠与双引号前面各加
    一个反斜杠。不还原的话，页面上的网络名就是一段转义源码。这里按同样的规则拼回
    字节，再按 UTF-8 解码。
    """
    if '\\' not in text:
        return text
    buf = bytearray()
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '\\' and i + 1 < n:
            nxt = text[i + 1]
            if nxt == 'x' and i + 3 < n:
                try:
                    buf.append(int(text[i + 2:i + 4], 16))
                    i += 4
                    continue
                except ValueError:
                    pass
            buf.extend(nxt.encode('utf-8'))
            i += 2
            continue
        buf.extend(ch.encode('utf-8'))
        i += 1
    return buf.decode('utf-8', 'replace')


def _iface():
    """控制目录下以接口名命名的套接字，取第一个无线接口。"""
    try:
        names = sorted(os.listdir(CTRL_DIR))
    except OSError:
        return None
    for n in names:
        if re.match(r'^wl', n):
            return n
    return names[0] if names else None


def _cli(*args, **kw):
    iface = _iface()
    if not iface:
        raise RuntimeError('没有可用的无线接口')
    try:
        r = subprocess.run([CLI, '-p', CTRL_DIR, '-i', iface] + list(args),
                           capture_output=True, text=True, timeout=kw.get('timeout', 12))
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError('wpa_cli 调用失败: %s' % e)
    out = (r.stdout or '').strip()
    if r.returncode != 0 or out.startswith('FAIL'):
        raise RuntimeError(out or (r.stderr or '').strip() or 'wpa_cli 返回非零')
    return [line for line in out.splitlines() if line]


def _kv(*args):
    fields = {}
    for line in _cli(*args):
        k, _, v = line.partition('=')
        if k:
            fields[k.strip()] = v.strip()
    return fields


def _ip(iface):
    """从内核直接问接口地址，不依赖 iproute2。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl.ioctl(s.fileno(), 0x8915,          # SIOCGIFADDR
                             struct.pack('256s', iface[:15].encode()))
        return socket.inet_ntoa(packed[20:24])
    except OSError:
        return ''
    finally:
        s.close()


def _networks():
    """[(id, ssid, flags)] —— 来自 `list_networks`，不是读配置文件。"""
    out = []
    lines = _cli('list_networks')
    for line in lines[1:]:                                # 第一行是表头
        parts = line.split('\t')
        if len(parts) < 2:
            continue
        try:
            nid = int(parts[0])
        except ValueError:
            continue
        flags = parts[3] if len(parts) > 3 else ''
        out.append((nid, _unescape(parts[1]), flags))
    return out


def status():
    iface = _iface()
    data = {'interface': iface or '', 'ssid': '', 'state': 'unavailable',
            'ip': '', 'bssid': '', 'freq': '', 'saved': []}
    if not iface:
        return data
    fields = _kv('status')
    data.update({
        'ssid': _unescape(fields.get('ssid', '')),
        'state': fields.get('wpa_state', ''),
        'bssid': fields.get('bssid', ''),
        'freq': fields.get('freq', ''),
        'ip': _ip(iface),
        'saved': [ssid for _, ssid, _ in _networks()],
    })
    return data


def _scan_raw():
    """scan_results → {bssid: 条目}。隐藏网络（空 SSID）跳过。"""
    nets = {}
    lines = _cli('scan_results')
    if not lines:
        return nets
    for line in lines[1:]:
        parts = line.split('\t')
        if len(parts) < 5:
            continue
        bssid, freq, signal, flags, ssid = parts[0], parts[1], parts[2], parts[3], _unescape(parts[4])
        if not ssid.strip():
            continue
        if 'WPA3' in flags:
            security = 'wpa3'
        elif 'WPA2' in flags:
            security = 'wpa2'
        elif 'WPA' in flags:
            security = 'wpa'
        elif 'WEP' in flags:
            security = 'wep'
        else:
            security = 'open'
        try:
            level = int(signal)
        except ValueError:
            level = -100
        nets[bssid] = {'ssid': ssid, 'bssid': bssid, 'freq': freq,
                       'signal': level, 'security': security}
    return nets


def scan():
    """触发一次扫描并等结果。已保存过的网络带 saved 标记。

    刚扫过时 `scan` 会返回 FAIL-BUSY，这时直接读上一次的结果 —— 那也是有效数据。
    """
    before = set(_scan_raw())
    try:
        _cli('scan')
    except RuntimeError:
        pass
    deadline = time.time() + 6
    nets = {}
    while time.time() < deadline:
        nets = _scan_raw()
        if set(nets) - before:
            break
        time.sleep(0.4)
    saved = {ssid for _, ssid, _ in _networks()}
    # 同一个 SSID 往往有多个 BSSID（2.4G/5G、多 AP），列表只按 SSID 留信号最好的那个，
    # 否则点一次扫描会出现一堆重名条目。
    best = {}
    priority = {'open': 0, 'wep': 1, 'wpa': 2, 'wpa2': 3, 'wpa3': 4}
    for net in nets.values():
        cur = best.get(net['ssid'])
        if cur is None or net['signal'] > cur['signal']:
            merged = dict(net)
            if cur and priority.get(cur['security'], 0) > priority.get(merged['security'], 0):
                merged['security'] = cur['security']
            best[net['ssid']] = merged
    out = []
    for net in best.values():
        net['saved'] = net['ssid'] in saved
        out.append(net)
    out.sort(key=lambda n: (-n['signal'], n['ssid']))
    return out


def _get(nid, field):
    """`get_network <id> <field>` 的返回值（已去掉两端引号）。"""
    try:
        lines = _cli('get_network', str(nid), field)
    except RuntimeError:
        return ''
    value = (lines[-1] if lines else '').strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    return value


def _priority(nid):
    try:
        return int(_get(nid, 'priority') or 0)
    except ValueError:
        return 0


def connect(ssid, psk):
    """连到一个网络。psk 为空表示开放网络；否则必须 8~63 位，或 64 位十六进制。

    用「抬优先级 + enable_network + disconnect/reassociate」，**不用 select_network**：
    后者的语义是 "select a network (disable others)"，会把其它网络全部标成 disabled，
    连一次失败就永久少一个候选，重启几次之后配置里只剩连不上的那个，表现就是越重启
    越连不上。抬优先级只影响这次选谁，失败时其它网络还在，能退回去。

    落盘同样只在 COMPLETED 之后：`save_config` 是持久的，先存再验会把失败的连接写进
    配置，下一轮开机读到的就是那个连不上的结果。

    同名网络沿用原来的块（不删）：删掉再建的话，这一轮失败就把已经存好的那个也弄没了。
    新密码只在连上之后才落盘，失败时改回配置文件里的旧值（reconfigure）。
    """
    ssid = (ssid or '').strip()
    psk = (psk or '').strip()
    if not ssid:
        raise RuntimeError('缺少 SSID')
    if len(ssid.encode()) > 32:
        raise RuntimeError('SSID 超过 32 字节')
    if psk and not (8 <= len(psk) <= 63 or (len(psk) == 64 and re.fullmatch(r'[0-9a-fA-F]{64}', psk))):
        raise RuntimeError('密码长度必须是 8~63 位（或 64 位十六进制）')

    quoted = '"' + ssid.replace('\\', '\\\\').replace('"', '\\"') + '"'
    nid = None
    for _id, name, _flags in _networks():
        if name == ssid:
            nid = _id
            break
    if nid is None:
        nid = int(_cli('add_network')[-1].strip())

    _cli('set_network', str(nid), 'ssid', quoted)
    if psk:
        _cli('set_network', str(nid), 'psk', '"%s"' % psk)
    else:
        _cli('set_network', str(nid), 'key_mgmt', 'NONE')

    # 抬优先级让 wpa_supplicant 挑它：priority 越大越先匹配（官方 wpa_supplicant.conf
    # 里 priority 一节的说明）。不用 select_network —— 那个命令的语义是
    # "select a network (disable others)"，会把其它网络全标成 disabled。
    top = max([_priority(i) for i, _, _ in _networks()] + [0]) + 10
    _cli('set_network', str(nid), 'priority', str(top))
    _cli('enable_network', str(nid))
    # 让它立刻按新的优先级重新选一次：disconnect 是"断开并等 reassociate"，
    # reassociate 是"强制重新关联"。两个都是官方列出的一等命令，且都不禁用别的网络。
    _cli('disconnect')
    _cli('reassociate')

    deadline = time.time() + 20
    state = ''
    while time.time() < deadline:
        fields = _kv('status')
        state = fields.get('wpa_state', '')
        if state == 'COMPLETED' and fields.get('ssid') == ssid:
            _cli('save_config')                           # 连上了才落盘
            return {'ok': True, 'ssid': ssid, 'state': state, 'ip': _ip(_iface())}
        time.sleep(0.5)

    # 没连上：从文件重新读配置（新建的块随之消失、改过的密码回到旧值，优先级也回退），
    # 再重新关联一次把原来那个网络接回来。配置文件一个字都不动。
    try:
        _cli('reconfigure')
        _cli('reassociate')
    except RuntimeError:
        pass
    raise RuntimeError('连不上 %s（当前状态 %s，通常是密码不对或信号太弱）' % (ssid, state or '未知'))
