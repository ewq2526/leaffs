# -*- coding: utf-8 -*-
"""上传临时文件（`.part`）不该出现在任何面向用户的路径里（LF-22）；
进程被杀留下的孤儿临时文件必须在启动时清掉（LF-26）。

背景：上传时先写临时文件，原来写成 `<目标>.part.<线程id>.<纳秒>` —— **就在共享目录里**。
于是它出现在文件列表（实测日志里手机端真的把它下载了：
`GET /download/public%2F...gguf.part.12272.1789513553046631300 200`）、搜索、文件夹大小统计，
还能通过直链下载。用户看到的就是"一个正在写入、大小还在涨的文件"。

改法：临时文件挪进 `UPLOAD_DIR/.uploads/`（同盘，落位仍是 `os.replace` 原子操作），
再让所有面向用户的读取路径跳过它 —— 临时文件从此不进入用户空间。

⚠️ 两处坑：
1. 缩略图那条**必须用真图片**：`_generate_thumbnail` 对解不了码的文件只会失败（TH1 踩过
   1×1 PNG 让 ffmpeg 报 `received no packets` 的坑），而失败也是 404 —— 现状与修好都是 404
   就测不出差别。这里自己生成 64×64 PNG，并轮询等异步生成。
2. 统计那条**必须先让聚合缓存失效**：目录大小是带缓存/防抖的，若读到旧缓存，
   放不放临时文件 total_size 都一样，会变成假绿。上传会联动失效祖先（含共享根），
   所以这里靠一次真实上传把缓存冲掉。
⚠️ 搜索有令牌桶限流（每 IP 6/60s），所以整个文件里只搜一次。
"""
import os
import shutil
import struct
import time
import zlib

import pytest

from conftest import BASE_URL, login

TMP_DIRNAME = '.uploads'
PROBE = 'lf22probe'
TXT_NAME = PROBE + '.txt'
PNG_NAME = PROBE + '.png'


def _png_64x64():
    """自己造一张 64×64 纯色 PNG（不依赖 PIL）"""
    w = h = 64
    raw = (b'\x00' + bytes([200, 30, 30] * w)) * h

    def chunk(tag, data):
        body = tag + data
        return (struct.pack('>I', len(data)) + body
                + struct.pack('>I', zlib.crc32(body) & 0xffffffff))

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw))
            + chunk(b'IEND', b''))


def _tmp_dir(data_root):
    return os.path.join(data_root, 'shared_files', TMP_DIRNAME)


def _place_tmp(data_root):
    """在临时目录里放两个"正在上传"的文件，返回 (目录, 总字节数)"""
    d = _tmp_dir(data_root)
    os.makedirs(d, exist_ok=True)
    txt = os.path.join(d, TXT_NAME)
    png = os.path.join(d, PNG_NAME)
    with open(txt, 'wb') as f:
        f.write(b'x' * 4096)
    with open(png, 'wb') as f:
        f.write(_png_64x64())
    return d, os.path.getsize(txt) + os.path.getsize(png)


def test_upload_tmp_is_invisible_in_listing(client, data_root):
    """① 文件列表里不该出现临时目录本身"""
    login(client)
    d, _ = _place_tmp(data_root)
    try:
        r = client.get('/api/files', params={'path': ''})
        assert r.status_code == 200, r.text
        names = [f['name'] for f in r.json().get('files', [])]
        assert TMP_DIRNAME not in names, '上传临时目录出现在共享根列表里：%s' % names
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_upload_tmp_is_invisible_in_search(client, data_root):
    """② 搜索搜不到临时文件（整份文件只搜这一次 —— 有令牌桶限流）"""
    login(client)
    d, _ = _place_tmp(data_root)
    try:
        r = client.get('/api/search', params={'q': PROBE})
        assert r.status_code == 200, r.text
        hits = [f.get('path') for f in r.json().get('files', [])]
        assert not any(TMP_DIRNAME in (p or '') for p in hits), \
            '搜索能搜到上传临时文件：%s' % hits
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_upload_tmp_cannot_be_downloaded(client, data_root):
    """③ 直链也下载不到 —— 它是服务端内部目录，admin 同样不该摸到"""
    login(client)
    d, _ = _place_tmp(data_root)
    try:
        r = client.get('/download/%s/%s' % (TMP_DIRNAME, TXT_NAME))
        assert r.status_code != 200, \
            '上传临时文件能通过直链下载（%d）：%s' % (r.status_code, r.text[:200])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_upload_tmp_has_no_thumbnail(client, data_root):
    """④ 缩略图路径同样拒绝（用真图片；没有缩略图后端的环境跳过）"""
    from leaffs.utils.core import has_ffmpeg, thumbnail_backend
    if not has_ffmpeg() and not thumbnail_backend():
        pytest.skip('本机没有缩略图后端，这条测不出差别')
    login(client)
    d, _ = _place_tmp(data_root)
    try:
        path = '%s/%s' % (TMP_DIRNAME, PNG_NAME)
        deadline = time.time() + 5        # 缩略图是异步生成的，轮询等它出来
        while time.time() < deadline:
            r = client.get('/api/thumb', params={'path': path})
            assert r.status_code != 200, \
                '临时文件被生成并下发了缩略图 —— 说明它进了面向用户的读取路径'
            time.sleep(0.5)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_scan_folder_agg_skips_upload_tmp(data_root_factory):
    """⑤ 文件夹大小统计跳过上传临时目录。

    这一条**故意写成单元测试**、不走 `/api/stats`：站点统计与目录聚合各有一层缓存
    （内存 TTL + 磁盘分片，后者只有 invalidate 才会清），从外部很难可靠地把它冲到
    "刚重算过"的状态 —— 实测过一次，走 HTTP 的写法在**未修改的代码上也是绿的**，
    等于没测。直接调 `_scan_folder_agg` 没有缓存参与，把跳过改回去必红。
    """
    from leaffs.paths import UPLOAD_TMP_DIRNAME
    from leaffs.utils import core as uc

    root = data_root_factory('tmpagg_')
    shared = os.path.join(root, 'shared_files')
    tmpd = os.path.join(shared, UPLOAD_TMP_DIRNAME)
    os.makedirs(tmpd, exist_ok=True)
    with open(os.path.join(shared, 'real.txt'), 'wb') as f:
        f.write(b'y' * 100)                    # 真实文件：必须计入
    with open(os.path.join(tmpd, 'inflight.part'), 'wb') as f:
        f.write(b'x' * 5000)                   # 上传临时文件：必须跳过

    agg = uc._scan_folder_agg(shared)
    assert agg['files'] == 1, '临时文件被算进了文件数：%s' % agg
    assert agg['size'] == 100, \
        '临时文件被算进了目录大小：%s（应当只算 real.txt 的 100 字节）' % agg


# ---------- LF-26：启动时清孤儿（需要一个独立实例，因为清理发生在服务端启动阶段）----------

def test_orphan_upload_tmp_is_cleaned_on_startup(data_root_factory):
    """进程被杀留下的 `.part` 孤儿必须在启动时清掉。

    用**独立数据根 + 独立端口**起一个真实服务：先手造残留，再启动，然后断言目录是空的。
    只在测试进程里调清理函数是不够的 —— 那用的是测试进程的 `UPLOAD_DIR`，与服务端不是同一个根。
    """
    import json
    import subprocess
    import sys

    import httpx

    proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    http_port, ws_port = 8103, 8104
    root = data_root_factory('uptmp_')

    # 启动前造残留：模拟"上一次进程被 kill，正在写的临时文件留在了原地"
    d = os.path.join(root, 'shared_files', TMP_DIRNAME)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, '99999-123456789.part'), 'wb') as f:
        f.write(b'orphan' * 1000)

    with open(os.path.join(root, 'config', 'server_config.json'), 'w', encoding='utf-8') as f:
        json.dump({'http_port': http_port, 'ws_port': ws_port, 'tls_enabled': False,
                   'guest_mode': False, 'access_log': False}, f)
    with open(os.path.join(root, 'config', 'users.json'), 'w', encoding='utf-8') as f:
        json.dump({'admin': {'password': 'admin', 'role': 'super_admin'}}, f)

    env = dict(os.environ)
    env['LEAFFS_PROJECT_ROOT'] = root
    env['LEAFFS_NO_WEBVIEW'] = '1'
    log = open(os.path.join(root, 'server.log'), 'wb', buffering=0)
    proc = subprocess.Popen([sys.executable, '-m', 'leaffs'], cwd=proj_root, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        base = 'http://127.0.0.1:%d' % http_port
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError('server exited early')
            try:
                if httpx.get(base + '/api/ping', timeout=1.0).status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError('server not ready on %s' % base)

        left = os.listdir(d) if os.path.isdir(d) else []
        assert not left, '启动时没有清掉孤儿临时文件，目录里还剩：%s' % left
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log.close()
