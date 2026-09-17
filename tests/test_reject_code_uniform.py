# -*- coding: utf-8 -*-
"""统一拒绝口径（2026-09-15，用户拍板）：身份/权限类拒绝一律 **404**。

**为什么**：403/401 等于告诉对方"这个端点、这个资源、这个用户是存在的，只是你没权限"——
等于给攻击者画地图。统一成 404 之后，「不存在」与「无权」不可区分。

**做在哪**：`handler.py` 的 `send_json` / `send_error` 两个统一出口上（不是逐处改那 94 个
返回点 —— 逐处改等于下次新写一处又漏）。

**豁免**（那几处必须仍是 403）：它们不是"拒绝访问"，而是**业务失败**，前端要把原因显示给
用户，而且分享页的输码流程就靠那个 403。完整清单见 `handler._REJECT_AS_NOT_FOUND` 的注释。

本文件两道守卫：
1. **黑盒**：代表性端点在 匿名 / 游客 / 普通用户 三种身份下**都不许回 401/403**；
2. **白盒**：源码里所有 `send_json(..., 401|403)` 必须显式 `exempt=True`。

用**独立实例**（自建数据根 + 独立端口）：要建普通用户来验 user 身份，不能污染会话级
服务的 `users.json`。
"""
import json
import os
import re
import subprocess
import sys
import time

import httpx
import pytest

from conftest import PROJ_ROOT, login

PORT, WSPORT = 8099, 8100
BASE = 'http://127.0.0.1:%d' % PORT

# 代表性端点：覆盖闸、各 API 层复核、页面、下载器、分享、上传/删除/建目录
GET_APIS = (
    '/api/users', '/api/config', '/api/config/advanced', '/api/config/deep',
    '/api/logs', '/api/connections', '/api/stats', '/api/files',
    '/api/share', '/api/share/status', '/api/url-download/tasks',
    '/api/url-download/config', '/api/thumb?path=public', '/api/raw?path=public',
)
POST_APIS = (
    ('/api/delete', {'files': ['public/zz-nope.txt']}),
    ('/api/mkdir', {'path': 'public', 'name': 'zz-nope'}),
    ('/api/users/add', {'username': 'zz-nope', 'password': 'pw-123456'}),
    ('/api/users/delete', {'username': 'zz-nope'}),
    ('/api/config', {'guest_mode': True}),
    ('/api/config/deep', {'folder_size_ttl': 60}),
    ('/api/share/publish', {'paths': ['users/admin/zz-nope.txt']}),
    ('/api/share/code', {'code': 'zz-nope-123'}),
    ('/api/logs/clear', {}),
    ('/api/url-download/config', {'max_concurrent': 2}),
)
PAGES = ('/admin', '/admin/users', '/admin/deep', '/admin/advanced', '/log', '/share',
         '/url-download', '/url-download/peers', '/me', '/browse/public')


@pytest.fixture(scope='module')
def rej_server(data_root_factory):
    """独立实例：本文件要建普通用户，不能动共享服务的 users.json"""
    root = data_root_factory('rejcode_')
    cfg = {
        'http_port': PORT, 'ws_port': WSPORT, 'tls_enabled': False,
        'guest_mode': True, 'guest_public_write': True, 'access_log': False,
        'max_total_conns': 256,
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
        yield BASE
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        logf.close()


def _clients():
    """三种"不够格"的身份各一个独立会话"""
    admin = httpx.Client(base_url=BASE, timeout=20)
    login(admin)
    r = admin.post('/api/users/add', json={'username': 'rejprobe',
                                           'password': 'pw-123456', 'role': 'user'})
    assert r.status_code == 200, r.text
    user = httpx.Client(base_url=BASE, timeout=20)
    r = user.post('/api/auth/login', json={'username': 'rejprobe', 'password': 'pw-123456'})
    assert r.status_code == 200 and r.json().get('success'), r.text
    guest = httpx.Client(base_url=BASE, timeout=20)
    r = guest.post('/api/guest/login')
    assert r.status_code == 200, r.text
    anon = httpx.Client(base_url=BASE, timeout=20)
    return admin, (('匿名', anon), ('游客', guest), ('普通用户', user))


def test_no_401_403_for_any_weaker_identity(rej_server):
    """匿名 / 游客 / 普通用户打这些端点，一律不许出现 401/403"""
    admin, ident = _clients()
    try:
        bad = []
        for name, c in ident:
            for p in GET_APIS:
                code = c.get(p).status_code
                if code in (401, 403):
                    bad.append('%s GET %s -> %s' % (name, p, code))
            for p, body in POST_APIS:
                code = c.post(p, json=body).status_code
                if code in (401, 403):
                    bad.append('%s POST %s -> %s' % (name, p, code))
            for p in PAGES:
                code = c.get(p, follow_redirects=False).status_code
                if code in (401, 403):
                    bad.append('%s GET %s -> %s' % (name, p, code))
        assert not bad, '统一拒绝口径被破坏（不该再出现 401/403）：\n  ' + '\n  '.join(bad)
    finally:
        for _, c in ident:
            c.close()
        admin.close()


def test_exempt_business_failures_are_still_403(rej_server):
    """豁免清单必须**仍然是 403** —— 分享页输码流程就靠它，别被"统一"顺手改掉"""
    c = httpx.Client(base_url=BASE, timeout=20)
    anon = httpx.Client(base_url=BASE, timeout=20)
    try:
        # ① 登录失败（业务失败，不是"无权访问"）
        r = c.post('/api/auth/login', json={'username': 'admin', 'password': 'wrong-pass'})
        assert r.status_code == 403, r.status_code
        login(c)
        # ② 原密码不正确
        r = c.post('/api/account/password',
                   json={'old_password': 'nope', 'new_password': 'abcdefgh'})
        assert r.status_code == 403, r.status_code
        # ③ 分享页"需要输码"引导（前端 public.html 就认这个 403）
        r = c.post('/api/share/code', json={'code': 'abc123'})
        assert r.status_code == 200 and r.json().get('enabled') is True, r.text
        try:
            r = anon.get('/p/admin/api')
            assert r.status_code == 403 and r.json().get('error') == 'code_required', r.text
            # ④ 分享码错误（业务失败 + 前端要显示剩余次数）
            r = anon.post('/api/share/auth', json={'username': 'admin', 'code': 'wrong'})
            assert r.status_code == 403 and r.json().get('error') == 'incorrect', r.text
        finally:
            c.post('/api/share/code', json={'code': ''})
    finally:
        c.close()
        anon.close()


def _call_text(text, start):
    """从 `send_json(` 的 '(' 起按括号配平取出整段调用文本"""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:start + 400]


# 允许写 exempt=True 的地方（显式白名单）。新增豁免必须来这里登记 ——
# 漏登记就红，等于强制二次确认：豁免是"把 403 还回去"，多一处就多一分信息泄露。
_EXEMPT_ALLOWED = (
    ('leaffs/auth/login_api.py', '用户名或密码错误'),
    ('leaffs/auth/login_api.py', '游客模式已关闭'),
    ('leaffs/auth/self_api.py', '原密码不正确'),
    ('leaffs/server/handler.py', "'error': 'locked'"),   # share_auth 锁定态
    ('leaffs/server/handler.py', 'body'),                # share_auth 错码/锁定（同一个 body 变量）
    ('leaffs/server/handler.py', 'code_required'),       # 分享页"需要输码"引导
)


def test_only_whitelisted_calls_are_exempt():
    """白盒：`exempt=True` 只许出现在豁免白名单里

    注意**不是**禁止 `send_json(..., 403)` —— 那 80 多处"权限拒绝"正是要被统一出口
    映射成 404 的调用点，它们是这条路的设计。要守的是"没人为了省事随手加豁免"。
    """
    unmatched = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(PROJ_ROOT, 'leaffs')):
        dirnames[:] = [d for d in dirnames if d != '__pycache__']
        for fn in filenames:
            if not fn.endswith('.py'):
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, PROJ_ROOT).replace('\\', '/')
            text = open(p, encoding='utf-8').read()
            for m in re.finditer(r'send_json\s*\(', text):
                # 跳过注释里的示例（文档里会写 `send_json(..., 403, exempt=True)` 这种说明）
                ls = text.rfind('\n', 0, m.start()) + 1
                le = text.find('\n', m.start())
                if text[ls:le if le > 0 else len(text)].strip().startswith('#'):
                    continue
                call = _call_text(text, m.end() - 1)
                if 'exempt=True' not in call:
                    continue
                if not any(rel.endswith(af) and s in call for af, s in _EXEMPT_ALLOWED):
                    line = text[:m.start()].count('\n') + 1
                    unmatched.append('%s:%d  %s' % (rel, line, call.replace('\n', ' ')[:90]))
    assert not unmatched, (
        '这些地方写了 exempt=True 但不在豁免白名单里 —— 豁免等于把 403 还回去（泄露"存在但无权"）。'
        '若确实该豁免，请同时更新本文件的白名单并说明理由：\n  ' + '\n  '.join(unmatched))


def test_reject_mapping_is_installed():
    """白盒：统一出口的映射本身还在（有人把集合改成空集，行为会悄悄退回 401/403）"""
    import leaffs.server.handler as H
    assert H._REJECT_AS_NOT_FOUND == frozenset((401, 403)), H._REJECT_AS_NOT_FOUND
