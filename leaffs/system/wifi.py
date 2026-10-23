"""无线网络：当前状态、扫描、连接。

全部通过 `wpa_supplicant` 自己的控制接口（`wpa_cli`）完成，不自己解析或改写配置文件：

    status / scan / scan_results    读
    list_networks                   已保存的网络
    add_network / set_network / remove_network / select_network / save_config   写

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
        out.append((nid, parts[1], flags))
    return out


def status():
    iface = _iface()
    data = {'interface': iface or '', 'ssid': '', 'state': 'unavailable',
            'ip': '', 'bssid': '', 'freq': '', 'saved': []}
    if not iface:
        return data
    fields = _kv('status')
    data.update({
        'ssid': fields.get('ssid', ''),
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
        bssid, freq, signal, flags, ssid = parts[0], parts[1], parts[2], parts[3], parts[4]
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


def _restore(ssid, psk):
    """把之前连着的网络重新装回去（失败回滚用）。"""
    quoted = '"' + ssid.replace('\\', '\\\\').replace('"', '\\"') + '"'
    for nid, name, _flags in _networks():
        if name == ssid:
            _cli('remove_network', str(nid))
    nid = _cli('add_network')[-1].strip()
    _cli('set_network', nid, 'ssid', quoted)
    if psk:
        _cli('set_network', nid, 'psk', '"%s"' % psk)
    else:
        _cli('set_network', nid, 'key_mgmt', 'NONE')
    _cli('select_network', nid)
    _cli('save_config')


def connect(ssid, psk):
    """连到一个网络。psk 为空表示开放网络；否则必须 8~63 位，或 64 位十六进制。

    同名网络先删掉再建，避免 `list_networks` 里越积越多同名块（旧密码会干扰选网）。
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
    # 先把当前连着的那个网络记下来：保存配置是持久化操作，新网络连不上时必须能退回去，
    # 否则这台设备会既没网也没了配置，只能从屏幕前重新输密码。
    keep = None
    for nid, name, flags in _networks():
        if 'CURRENT' in flags:
            keep = (name, _get(nid, 'psk'))
            break

    for nid, name, _flags in _networks():
        if name == ssid:
            _cli('remove_network', str(nid))
    nid = _cli('add_network')[-1].strip()
    _cli('set_network', nid, 'ssid', quoted)
    if psk:
        _cli('set_network', nid, 'psk', '"%s"' % psk)
    else:
        _cli('set_network', nid, 'key_mgmt', 'NONE')
    _cli('select_network', nid)
    _cli('save_config')                                   # 持久化交给 wpa_supplicant

    deadline = time.time() + 20
    state = ''
    while time.time() < deadline:
        fields = _kv('status')
        state = fields.get('wpa_state', '')
        if state == 'COMPLETED' and fields.get('ssid') == ssid:
            return {'ok': True, 'ssid': ssid, 'state': state, 'ip': _ip(_iface())}
        time.sleep(0.5)
    if keep and keep[0] != ssid:
        _restore(keep[0], keep[1])
        raise RuntimeError('连不上 %s（状态 %s），已回滚到 %s' % (ssid, state or '未知', keep[0]))
    raise RuntimeError('连不上 %s（当前状态 %s，通常是密码不对或信号太弱）' % (ssid, state or '未知'))
