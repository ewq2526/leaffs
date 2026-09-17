# -*- coding: utf-8 -*-
"""TLS 上下文 / 服务器证书服务 —— HTTP 与 WS 层共用。

历史：原为 leaffs.py 内 _tls_ctx_cache/_tls_context/_new_server_ctx/_openssl_exe/
_auto_gen_server_cert/_build_tls_context，重构抽出。职责：
  * 进程级单例 TLS 上下文（get_tls_context，HTTP/WS 两个线程共享）；
  * 证书来源按序：显式配置 tls_cert/tls_key → 既有 config/selfsigned.crt+.key →
    首次启动自动生成随机自签服务器证书（非 CA、不装信任库）；
  * 全部不可用返回 None，由启动方按“策略 A”拒绝明文启动。
"""
import os
import socket
import threading

import leaffs.config.core as _cfg
from leaffs.paths import BASE_DIR, CONFIG_DIR, find_bundled_exe
from leaffs.runtime_log import add_log
from leaffs.server.hosts import collect_ips

_tls_ctx_cache = None
_tls_ctx_lock = threading.Lock()


def get_tls_context():
    """进程级单例：只构建一次 TLS 上下文并缓存（HTTP/WS 线程共享）。"""
    global _tls_ctx_cache
    with _tls_ctx_lock:
        if _tls_ctx_cache is not None:
            return _tls_ctx_cache
        _tls_ctx_cache = _build_tls_context()
        return _tls_ctx_cache


def new_server_ctx():
    """A-09：集中构造服务端 TLS 上下文 —— 显式 TLS1.2 下限 + 密码套件白名单。

    R3/IC-TLS：服务端 SSLContext 统一走本函数（load_cert_chain 在调用方）。
    """
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.set_ciphers('ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:!aNULL:!eNULL:!MD5:!3DES:!RC4')
    except Exception as e:
        # 个别老 openssl 不接受该列表时回退系统默认，仅记录不阻断
        add_log(f'TLS 密码套件设置被拒（使用系统默认）: {e}', 'warn')
    return ctx


def _openssl_exe():
    """定位 openssl.exe：优先随包分发（资源根/主程序目录/源码包目录），其次 PATH。"""
    try:
        return find_bundled_exe('openssl.exe')
    except Exception:
        return None


def _auto_gen_server_cert(cert_path, key_path):
    """首次启动无证书时自动生成一张随机自签【服务器证书】（非 CA、不装信任库）。

    证书为纯叶节点自签：SAN 覆盖 localhost、本机主机名与本机全部 IPv4。
    返回 True=成功；失败清理半成品文件，由调用方维持“策略 A”拒绝明文。
    """
    import subprocess
    openssl = _openssl_exe()
    if not openssl:
        # 安卓没有随包的 openssl.exe：改由 Python 生成（规格与下面 openssl 那条一致）
        return _gen_server_cert_python(cert_path, key_path)
    env = dict(os.environ)
    cnf = os.path.join(BASE_DIR, 'openssl.cnf')
    if os.path.isfile(cnf):
        env['OPENSSL_CONF'] = cnf
    san = ['DNS:localhost', 'IP:127.0.0.1']
    try:
        hn = socket.gethostname().strip()
        if hn:
            san.append('DNS:' + hn)
    except Exception:
        pass
    for ip in collect_ips():
        if ip and not ip.startswith('127.'):
            san.append('IP:' + ip)
    args = [openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-days', '3650', '-keyout', key_path, '-out', cert_path,
            '-subj', '/CN=LeafFS Server',
            '-addext', 'subjectAltName=' + ','.join(san)]
    try:
        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        r = subprocess.run(args, capture_output=True, timeout=90,
                           cwd=CONFIG_DIR, env=env,
                           creationflags=creationflags)
    except Exception as e:
        add_log(f'自动生成服务器证书失败（无法执行 openssl）: {e}', 'err')
        return False
    if r.returncode != 0 or not os.path.isfile(cert_path) or not os.path.isfile(key_path):
        err = (r.stderr or r.stdout or b'').decode('utf-8', 'replace').strip()[-400:]
        add_log(f'自动生成服务器证书失败（openssl 退出码 {r.returncode}）: {err}', 'err')
        for p in (cert_path, key_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        return False
    return True


def _gen_server_cert_python(cert_path, key_path):
    """用 Python（cryptography）生成自签服务器证书 —— 没有 openssl.exe 时走这条。

    安卓没有随包的 openssl，这条路是它唯一的证书来源。规格与 openssl 那条保持一致：
    RSA 2048、自签叶证书（非 CA）、SAN 覆盖 localhost/本机主机名/本机全部非回环 IPv4、
    有效期 10 年、PEM 写盘（证书 + 私钥各一个文件）。
    """
    try:
        import datetime
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except Exception as e:
        add_log(f'自动生成服务器证书失败：既没有 openssl.exe，也不可用 cryptography（{e}）', 'err')
        return False
    try:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'LeafFS Server')])
        sans = []
        for v in ('localhost', '127.0.0.1'):
            sans.append(x509.IPAddress(ipaddress.ip_address(v)) if _is_ip(v)
                        else x509.DNSName(v))
        try:
            hn = socket.gethostname().strip()
            if hn:
                sans.append(x509.DNSName(hn))
        except Exception:
            pass
        for ip in collect_ips():
            if ip and not ip.startswith('127.'):
                try:
                    sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
                except ValueError:
                    sans.append(x509.DNSName(ip))
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)                      # 自签：签发者即自身
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=5))
                .not_valid_after(now + datetime.timedelta(days=3650))
                .add_extension(x509.SubjectAlternativeName(sans), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        with open(key_path, 'wb') as f:
            f.write(key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption()))
        with open(cert_path, 'wb') as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        return True
    except Exception as e:
        add_log(f'自动生成服务器证书失败（cryptography）: {type(e).__name__}: {e}', 'err')
        for p in (cert_path, key_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        return False


def _is_ip(v):
    import ipaddress
    try:
        ipaddress.ip_address(v)
        return True
    except ValueError:
        return False


def _build_tls_context():
    """按配置构造 TLS 上下文；证书不可用返回 None（由启动方按“策略 A”拒绝明文启动）。

    证书来源按序（全部只涉及“服务器证书”，绝不生成/安装 CA）：
      1a) server_config.json 显式配置 tls_cert/tls_key；
      1b) 既有 config/selfsigned.crt + selfsigned.key（直接加载，不重新生成）；
      1c) 前两者皆无 → 自动生成随机自签服务器证书（此后走 1b 复用）。
    显式配置缺失/加载失败同样返回 None（不回退 legacy 文件）。
    """
    import ssl
    if not _cfg.get_tls_enabled():
        return None
    cert, key = _cfg.get_tls_cert(), _cfg.get_tls_key()
    if cert and not os.path.isabs(cert):
        cert = os.path.join(CONFIG_DIR, cert)
    if key and not os.path.isabs(key):
        key = os.path.join(CONFIG_DIR, key)
    if (cert or key) and not (cert and key):
        add_log('TLS 证书配置不完整：tls_cert 与 tls_key 必须同时配置（当前只配了其一），'
                '请补全 config/server_config.json 或同时清空两项', 'err')
        return None
    if not cert and not key:
        legacy_cert = os.path.join(CONFIG_DIR, 'selfsigned.crt')
        legacy_key = os.path.join(CONFIG_DIR, 'selfsigned.key')
        if os.path.exists(legacy_cert) and os.path.exists(legacy_key):
            cert, key = legacy_cert, legacy_key
            add_log('使用既有自签服务器证书 config/selfsigned.crt（未安装任何信任库；'
                    '浏览器提示“不安全”属正常现象）', 'info')
        elif _auto_gen_server_cert(legacy_cert, legacy_key):
            cert, key = legacy_cert, legacy_key
            add_log('未配置证书：已自动生成随机自签服务器证书 config/selfsigned.crt + '
                    'selfsigned.key（仅本机使用、非 CA、未安装任何信任库；浏览器提示'
                    '“不安全 / 连接不是私密连接”是自签证书的正常现象，不是被劫持）', 'warn')
    if not cert or not key or not os.path.exists(cert) or not os.path.exists(key):
        add_log('TLS 已启用但无法取得可用服务器证书（未配置 tls_cert/tls_key，config 下'
                '无既有证书，且自动生成失败），拒绝明文启动（策略 A）', 'err')
        return None
    try:
        ctx = new_server_ctx()
        ctx.load_cert_chain(cert, key)
        return ctx
    except Exception as e:
        add_log(f'TLS 证书加载失败: {e}（cert={cert}，key={key}），拒绝明文启动（策略 A）', 'err')
        return None
