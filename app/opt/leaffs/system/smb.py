"""SMB 账号桥：ksmbd 的用户库由后端生成。

`/etc/ksmbd/ksmbdpwd.db` 不是二进制库。ksmbdpwd.db(5) 写得很清楚：一行一个
`名字:口令`，而口令 = base64(MD4(UTF-16LE(明文)))。所以这件事不需要 ksmbd.adduser，
也不需要第二套账号 —— 后端在用户给出明文的那一刻把它算出来，加密后跟账号存在一起
（`users.json` 的 `smb_nt_enc`），再把整份库写到内存盘上：

    /run/ksmbd/ksmbdpwd.db               ← 后端每次启动/每次改动时重建，关机即消失
    /etc/ksmbd/ksmbdpwd.db -> 上面那个    ← 首次启动时建的符号链接

这样盘上常驻的只有密文（`users.json` 里那一串），明文形式的库只在运行期存在。
加密密钥是单独一个 0600 的文件，不放在账号目录里 —— 账号目录被整包备份出去时，
带走的只是密文。

写完通知 mountd 重读（`ksmbd.control --reload`）。删账号、改名都只是重写一次文件，
不需要明文；这条路上没有"第二份可编辑的账号系统"。

应用账号**不是** Linux 用户：落盘身份由 ksmbd.conf 的 `force user` 指定，账号一个都
不进 /etc/passwd。

MD4 走本文件里的实现：OpenSSL 3 默认不提供 md4，系统的 hashlib 靠不住。
"""

import base64
import os
import struct
import subprocess

from leaffs.paths import CONFIG_DIR

DB_PATH = '/run/ksmbd/ksmbdpwd.db'
KEY_PATH = '/etc/ksmbd/smb.key'
CONF_PATH = '/etc/ksmbd/ksmbd.conf'
USER_ROOT = '/srv/leaffs/shared_files/users'
PUBLIC_ROOT = '/srv/leaffs/shared_files/public'
CONTROL = '/usr/sbin/ksmbd.control'


# ---------- MD4（RFC 1320） ----------

def _lrot(v, n):
    v &= 0xffffffff
    return ((v << n) | (v >> (32 - n))) & 0xffffffff


def _md4(data):
    msg = bytearray(data)
    bit_len = (8 * len(msg)) & 0xffffffffffffffff
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0)
    msg += struct.pack('<Q', bit_len)

    a, b, c, d = 0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476
    for off in range(0, len(msg), 64):
        x = struct.unpack('<16I', bytes(msg[off:off + 64]))
        aa, bb, cc, dd = a, b, c, d

        for i in range(16):
            k, s = i, (3, 7, 11, 19)[i % 4]
            if i % 4 == 0:
                a = _lrot(a + ((b & c) | (~b & d)) + x[k], s)
            elif i % 4 == 1:
                d = _lrot(d + ((a & b) | (~a & c)) + x[k], s)
            elif i % 4 == 2:
                c = _lrot(c + ((d & a) | (~d & b)) + x[k], s)
            else:
                b = _lrot(b + ((c & d) | (~c & a)) + x[k], s)

        for i in range(16):
            k = (i % 4) * 4 + i // 4
            s = (3, 5, 9, 13)[i % 4]
            if i % 4 == 0:
                a = _lrot(a + ((b & c) | (b & d) | (c & d)) + x[k] + 0x5a827999, s)
            elif i % 4 == 1:
                d = _lrot(d + ((a & b) | (a & c) | (b & c)) + x[k] + 0x5a827999, s)
            elif i % 4 == 2:
                c = _lrot(c + ((d & a) | (d & b) | (a & b)) + x[k] + 0x5a827999, s)
            else:
                b = _lrot(b + ((c & d) | (c & a) | (d & a)) + x[k] + 0x5a827999, s)

        order = (0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15)
        s3 = (3, 9, 11, 15)
        for i in range(16):
            k = order[i]
            s = s3[i % 4]
            if i % 4 == 0:
                a = _lrot(a + (b ^ c ^ d) + x[k] + 0x6ed9eba1, s)
            elif i % 4 == 1:
                d = _lrot(d + (a ^ b ^ c) + x[k] + 0x6ed9eba1, s)
            elif i % 4 == 2:
                c = _lrot(c + (d ^ a ^ b) + x[k] + 0x6ed9eba1, s)
            else:
                b = _lrot(b + (c ^ d ^ a) + x[k] + 0x6ed9eba1, s)

        a = (a + aa) & 0xffffffff
        b = (b + bb) & 0xffffffff
        c = (c + cc) & 0xffffffff
        d = (d + dd) & 0xffffffff

    return struct.pack('<4I', a, b, c, d)


def nt_hash(password):
    """ksmbd 记的那种口令：MD4(UTF-16LE(明文))，再 base64。"""
    return base64.b64encode(_md4(password.encode('utf-16-le'))).decode('ascii')


# ---------- 落盘只留密文 ----------

def _key():
    """32 字节密钥，单独一个 0600 的文件。没有就生成一个。

    不放在账号目录里：账号目录被整包备份出去时，带走的应该只有密文。
    密钥丢了等于 SMB 口令全部作废 —— 用户各自重设一次密码就又有了。
    """
    try:
        with open(KEY_PATH, 'rb') as f:
            key = f.read()
        if len(key) == 32:
            return key
    except OSError:
        pass
    key = os.urandom(32)
    d = os.path.dirname(KEY_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(key)
    return key


def encrypt_nt(nt):
    """把 ksmbd 形式的口令加密成一串（nonce 跟密文放在一起）。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    ct = AESGCM(_key()).encrypt(nonce, nt.encode('ascii'), b'')
    return base64.b64encode(nonce + ct).decode('ascii')


def decrypt_nt(blob):
    """解不回来（密钥换了、串被改过）就当没有：那个账号不会出现在库文件里。"""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        raw = base64.b64decode(blob.encode('ascii'))
        return AESGCM(_key()).decrypt(raw[:12], raw[12:], b'').decode('ascii')
    except (InvalidTag, ValueError, KeyError, OSError):
        return ''


# ---------- 生成 / 通知 ----------

def _render(accounts):
    """{名字: smb_nt} → 文件内容。没有口令形式的账号（旧版本升上来、没改过密码）不写。"""
    lines = []
    for name in sorted(accounts):
        nt = accounts[name]
        if not name or ':' in name or '\n' in name or not nt:
            continue
        lines.append('%s:%s' % (name, nt))
    return '\n'.join(lines) + ('\n' if lines else '')


def write_db(accounts):
    """整份重写 ksmbd 的用户库。返回写入的账号数。

    先写临时文件再 os.replace：mountd 随时可能来读，看到半份文件就是账号时有时无。
    """
    text = _render(accounts)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    tmp = DB_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.chmod(tmp, 0o640)
    os.replace(tmp, DB_PATH)
    return len([ln for ln in text.splitlines() if ln])


def _notify():
    """让 mountd 拿到新的库与共享表。

    库在 /run 下（tmpfs），开机时是空的：ksmbd 可能比这边先起，起来时没有库文件。
    所以先确保服务在跑（已经起来时 systemctl start 什么都不做），再让它重读。
    mountd(8) 写明 SIGHUP 会重读 ksmbd.conf 与 ksmbdpwd.db，ksmbd.control --reload
    发的正是它 —— 加共享、改共享都不需要重启。
    """
    ok = True
    try:
        subprocess.run(['systemctl', 'start', 'ksmbd.service'],
                       capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        ok = False
    if os.path.exists(CONTROL):
        try:
            p = subprocess.run([CONTROL, '--reload'], capture_output=True,
                               text=True, timeout=5)
            ok = ok and p.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
    return ok


# ---------- 共享：一个账号一个，靠 valid users 隔离 ----------
#
# ksmbd.conf(5) 的 `valid users`（S）说得很直白：「允许连到这个共享的用户列表；
# 留空则所有用户都允许」。之前那个装下所有账号目录的共享没有这一行，于是谁登录都能
# 翻别人的目录 —— 隔离必须在**共享这一层**做，因为 ksmbd 没有"每人一个目录"的写法。
#
# 所以后端按账号表追加一段共享：名字就是账号名，路径是这个账号自己的目录，
# `valid users` 只写它自己。mountd 收到 SIGHUP 就重读，不用重启。

SHARE_MARK = '# ==== 账号共享（由后端按账号表生成，手改会被覆盖）===='
RESERVED = {'global', 'public', 'users', 'ipc$', 'homes', 'printers'}

# SMB 会话的落盘身份。**不能**用安装账号：那个账号在 sudo 组里，共享路径一旦写错就
# 等于把整机交给任何能登录 SMB 的人。这个账号只用来落盘共享目录 —— 没有登录 shell、
# 不在 sudo 里，由首启脚本建。
SMB_USER = 'camphor-smb'


def _share_name(name):
    """账号名能不能直接当共享名：UTF-8、1~63 字节、不跟内置共享重名。"""
    name = (name or '').strip()
    if not name or len(name.encode('utf-8')) >= 64:
        return ''
    if name.lower() in RESERVED:
        return ''
    if ':' in name or '\n' in name or '/' in name or '\\' in name:
        return ''
    return name


def _conf_head(text):
    """ksmbd.conf 里我们自己生成的那一段之前的内容。"""
    i = text.find(SHARE_MARK)
    return text if i < 0 else text[:i]


def render_public(guest_mode, writable, owner=SMB_USER):
    """游客模式打开时才给 [public]，只读/可写跟 guest_public_write 走。

    ksmbd.conf(5)：`guest ok = yes` 允许不输口令连这个共享；`read only = yes` 就是
    只读。所以这块共享是否存在、能不能上传，都由后端那两个设置决定，不在这里写死。
    """
    if not guest_mode:
        return []
    out = ['', '[public]', '\tpath = %s' % PUBLIC_ROOT, '\tbrowseable = yes',
           '\tguest ok = yes']
    if writable:
        out.append('\tread only = no')
        out.append('\twritable = yes')
    else:
        out.append('\tread only = yes')
    if owner:
        out.append('\tforce user = %s' % owner)
    return out


def render_shares(names, owner=SMB_USER, guest_mode=False, public_writable=False):
    """账号共享 + （游客模式下的）公共共享 → 共享段文本。"""
    out = [SHARE_MARK]
    for name in sorted(names):
        out.append('')
        out.append('[%s]' % name)
        out.append('\tpath = %s' % os.path.join(USER_ROOT, name))
        out.append('\tread only = no')
        out.append('\twritable = yes')
        # 必须进浏览列表：Windows 打开 \\主机 时，只列 browseable = yes 的共享，
        # 全设成 no 的话连上之后一个文件夹都看不到（实测如此）。账号自己只被 valid
        # users 放行自己那一个，所以看不见也进不去别人的 —— 代价是登录后能在列表里
        # 读到别的账号名，这是 ksmbd 的取舍：它没有"只列自己"的开关。
        out.append('\tbrowseable = yes')
        out.append('\tvalid users = %s' % name)
        if owner:
            out.append('\tforce user = %s' % owner)
    if guest_mode:
        # 游客模式下客户端常常还是带着用户名来连的：把认证失败的映射成游客，
        # 否则 Windows 那边"不输口令进 public"会被拒。同名段是续写（见 ksmbd.conf(5)）。
        out.insert(1, '')
        out.insert(2, '[global]')
        out.insert(3, '\tmap to guest = Bad User')
    out.extend(render_public(guest_mode, public_writable, owner))
    return '\n'.join(out) + '\n'


def write_conf_shares(names, owner=SMB_USER, guest_mode=False, public_writable=False):
    """把共享写进 ksmbd.conf（保留前面的 global）。返回共享数。

    共享段的旧内容整段切掉再写：账号删了就少一段，改名就换一段，不会留下能连的残骸。
    """
    try:
        with open(CONF_PATH, encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return 0
    head = _conf_head(text)
    block = render_shares(names, owner, guest_mode, public_writable)
    tmp = CONF_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(head.rstrip('\n') + '\n\n' + block)
    try:
        os.chmod(tmp, os.stat(CONF_PATH).st_mode & 0o777)
    except OSError:
        os.chmod(tmp, 0o644)
    os.replace(tmp, CONF_PATH)
    return len(names)


def _ensure_dirs(names, owner):
    """每个账号一个目录，属主是这个共享的落盘身份。"""
    try:
        import pwd
        uid, gid = (pwd.getpwnam(owner).pw_uid, pwd.getpwnam(owner).pw_gid) if owner else (-1, -1)
    except (ImportError, KeyError):
        uid, gid = -1, -1
    for name in names:
        d = os.path.join(USER_ROOT, name)
        try:
            os.makedirs(d, exist_ok=True)
            os.chmod(d, 0o770)
            if uid >= 0:
                os.chown(d, uid, gid)
        except OSError:
            pass


def sync(accounts, guest_mode=False, public_writable=False):
    """写库 + 写共享表 + 让 mountd 重读。`accounts` = {名字: smb_nt}。

    只有拿到过口令（能登录 SMB）的账号才建共享，否则会出现一个谁也进不去的共享。
    `guest_mode` / `public_writable` 来自后端设置：游客模式关掉时 [public] 不出现。
    """
    n = write_db(accounts)
    names = [x for x in (_share_name(k) for k, v in accounts.items() if v) if x]
    _ensure_dirs(names, SMB_USER)
    write_conf_shares(names, SMB_USER, guest_mode, public_writable)
    _notify()
    return n
