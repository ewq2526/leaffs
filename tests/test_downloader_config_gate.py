# -*- coding: utf-8 -*-
"""B4：下载器全局配置只有管理员能读（与 set 对称），非管理员的页面也不显示设置区。

**为什么**：`GET /api/url-download/config` 原来只过 `_dl_allowed()` —— 普通用户（甚至
开了 `downloader_guest_allowed` 时的游客）能读到**整份** `downloader_config.json`：
`allow_private_targets`（能否下载私网目标）、`bt_dht_public`（DHT 是否对外暴露）
与各配额上限 —— 全是安全/策略信息（不只是页面上显示的并发/限速）。

**同一处代码还有第二个毛病**：下载器页把「并发数/限速」设置区对**所有角色**显示成可编辑框，
而保存接口只认管理员 —— 等于给了个按了必然失败的按钮。两个毛病一起修：
后端按角色拒绝 + 非管理员页面上不显示设置区、也不去拉配置。

⚠️ 页面的**实际隐藏效果**要真机看（前端在 APK 里）；这里能钉住的是
① 接口按角色拒绝、② 页面确实注入了角色（前端那半生效的前提）、③ 前端那半代码还在。
"""
import json
import os
import subprocess
import sys
import time

import httpx
import pytest

from conftest import PROJ_ROOT, login

PORT, WSPORT = 8101, 8102
BASE = 'http://127.0.0.1:%d' % PORT

# 只有管理员该看到的那些键
ADMIN_ONLY_KEYS = ('allow_private_targets', 'bt_dht_public', 'max_user_tasks',
                   'max_user_active', 'max_total_tasks', 'max_task_age_days',
                   'max_download_size')


@pytest.fixture(scope='module')
def dl_server(data_root_factory):
    """独立实例：要建普通用户、还要把游客下载器开关打开，不能动共享服务的那份配置"""
    root = data_root_factory('dlcfg_')
    cfg = {
        'http_port': PORT, 'ws_port': WSPORT, 'tls_enabled': False,
        'guest_mode': True, 'guest_public_write': True, 'access_log': False,
        'max_total_conns': 256,
        # 显式打开：这样游客才能过 _dl_allowed()，从而真正测到 B4 那道角色检查
        'downloader_guest_allowed': True,
    }
    with open(os.path.join(root, 'config', 'server_config.json'), 'w', encoding='utf-8') as f:
        json.dump(cfg, f)
    with open(os.path.join(root, 'config', 'users.json'), 'w', encoding='utf-8') as f:
        json.dump({'admin': {'password': 'admin', 'role': 'super_admin'}}, f)
    env = dict(os.environ)
    env['LEAFFS_PROJECT_ROOT'] = root
    env['LEAFFS_NO_WEBVIEW'] = '1'
    logf = open(os.path.join(root, 'server.log'), 'wb', buffering=0)
    proc = subprocess.Popen([sys.executable, '-m', 'leaffs'], cwd=PROJ_ROOT, env=env,
                            stdout=logf, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError('服务提前退出')
            try:
                if httpx.get(BASE + '/api/ping', timeout=1.0).status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError('服务未就绪')
        yield root
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        logf.close()


@pytest.fixture(scope='module')
def identities(dl_server):
    """三种身份各一个会话（普通用户只在建立时创建一次 —— 重复建会同名冲突）"""
    admin = httpx.Client(base_url=BASE, timeout=20)
    login(admin)
    r = admin.post('/api/users/add', json={'username': 'dlprobe',
                                           'password': 'pw-123456', 'role': 'user'})
    assert r.status_code == 200, r.text
    user = httpx.Client(base_url=BASE, timeout=20)
    r = user.post('/api/auth/login', json={'username': 'dlprobe', 'password': 'pw-123456'})
    assert r.status_code == 200 and r.json().get('success'), r.text
    guest = httpx.Client(base_url=BASE, timeout=20)
    r = guest.post('/api/guest/login')
    assert r.status_code == 200, r.text
    try:
        yield admin, user, guest
    finally:
        for c in (admin, user, guest):
            c.close()


def test_admin_can_read_and_value_matches_disk(dl_server, identities):
    """管理员：能读全量，且读到的是**真实**配置（与盘上 downloader_config.json 一致）"""
    admin, _, _ = identities
    r = admin.post('/api/url-download/config', json={'max_concurrent': 5, 'speed_limit': 1024})
    assert r.status_code == 200 and r.json().get('success'), r.text
    r = admin.get('/api/url-download/config')
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get('success') is True, d
    cfg = d.get('config') or {}
    for k in ('max_concurrent', 'speed_limit') + ADMIN_ONLY_KEYS:
        assert k in cfg, '管理员应当能看到 %s：%r' % (k, cfg)
    assert cfg['max_concurrent'] == 5 and cfg['speed_limit'] == 1024, cfg
    # 与盘上文件对得上 —— 证明不是回了个默认值/缓存值
    with open(os.path.join(dl_server, 'config', 'downloader_config.json'),
              encoding='utf-8') as f:
        disk = json.load(f)
    assert disk['max_concurrent'] == 5 and disk['speed_limit'] == 1024, disk


def test_non_admin_cannot_read_downloader_config(identities):
    """普通用户 / 游客：读不到 —— 统一拒绝口径下是 404，且响应体里不许漏策略键"""
    _, user, guest = identities
    for name, c in (('普通用户', user), ('游客', guest)):
        r = c.get('/api/url-download/config')
        assert r.status_code == 404, '%s 读下载器配置 -> %s %s' % (name, r.status_code, r.text)
        for k in ADMIN_ONLY_KEYS:
            assert k not in r.text, '%s 的响应体里漏了 %s：%s' % (name, k, r.text)
    # 但下载器本身仍能用（普通用户/游客没被一锅端）
    for name, c in (('普通用户', user), ('游客', guest)):
        assert c.get('/api/url-download/list').status_code == 200, name


def test_page_injects_role_for_every_identity(identities):
    """页面确实注入了角色 —— 这是前端那半（按 myRole 隐藏设置区）能生效的前提"""
    admin, user, guest = identities
    for name, c, want in (('管理员', admin, '"super_admin"'),
                          ('普通用户', user, '"user"'),
                          ('游客', guest, '"guest"')):
        r = c.get('/url-download')
        assert r.status_code == 200, (name, r.status_code)
        assert '__MY_ROLE__' not in r.text, '%s：角色占位符没被替换' % name
        assert 'var myRole = %s;' % want in r.text, \
            '%s：页面里的 myRole 不是 %s' % (name, want)


def test_frontend_still_hides_settings_for_non_admin():
    """白盒：两份下载器页面里"按角色隐藏设置区"的代码还在（删了这条就红）"""
    missing = []
    for rel in ('leaffs/web_page/downloader/downloader.html',
                'leaffs/web_page/downloader/downloader.en.html'):
        text = open(os.path.join(PROJ_ROOT, rel.replace('/', os.sep)), encoding='utf-8').read()
        for frag, why in (('var myRole = __MY_ROLE__;', '没注入角色'),
                          ("querySelector('.dl-settings')", '没找到设置区（隐藏无从谈起）'),
                          ("display = 'none'", '没有隐藏动作'),
                          ('if (IS_ADMIN)', '没有按角色分支')):
            if frag not in text:
                missing.append('%s：%s（缺 %r）' % (rel, why, frag))
    assert not missing, '下载器页的前端守卫被删了：\n  ' + '\n  '.join(missing)
