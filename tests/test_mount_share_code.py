# -*- coding: utf-8 -*-
"""挂载点的分享码：列出挂载区时**两条路**都必须挡住没过码的人。

来由（2026-09-23）：分享区改成"进不去的人连目录名都不给"之后，原来那条拿
"游客在 public 列表里看不看得到 `shares`"当**分享码粒度**信号的守卫失效了 ——
游客再也看不到那个名字，`unlocked` 传什么都一样，等于观测不到。
挂载区不一样：它的访问权限与公共目录一个级别，**普通用户/游客都列得出来** ——
码在不在、有没有被过滤，在这一层看得见。

所以本文件盯的就是这个观测点，防的是"服务端不过滤"那个退化版本
（`merge_into_list(unlocked=None)`）：设了码的挂载点，游客在 HTTP 与 WS **两条路上**
都拿不到它（连名字都不给，只给一个随机标签的占位）；把码清掉之后它又该回来 ——
后一条是**对照**，证明前面"看不到"确实来自那一条码，而不是请求本身失败。

⚠️ 2026-09-27 补的两条（都在实测里被打出来过）：**只在"条目层"过滤是不够的** ——
容器列表不给名字了，但挂载点的名字就是源目录自己的名字，猜中
`?path=public/mounts/<名字>` 直取，就能把里面条目全列出来（`build_listing` 补了
"被列的路径本身也要过码闸"）；**下载器**当初没走 `resolve_rel` 那道只读闸，收下指向
挂载点的 `save_dir` 会在共享根里造出一个同名实体目录，把挂载点遮蔽掉。

⚠️ 与 `test_list_paths_agree.py` 同一个道理，用**独立数据根 + 独立端口**：
这里要挂载、要设码，都是留在服务进程里的共享状态，不能跟别的用例共用实例
（一条断言失败会连锁污染后面依赖 public 列表或游客的用例）。
"""
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTTP_PORT, WS_PORT = 8113, 8114
MOUNT_SRC = '电影'                      # 挂载的源目录名 —— 条目名取它
SHARE_CODE = 'lf28code'


@pytest.fixture(scope='module')
def inst(data_root_factory):
    """隔离实例：独立数据根 + 独立端口，备好一条挂载点（码由用例自己设/清）"""
    root = data_root_factory('lf28_')
    cfg = {'http_port': HTTP_PORT, 'ws_port': WS_PORT, 'tls_enabled': False,
           'guest_mode': True, 'guest_public_write': True, 'access_log': False}
    with open(os.path.join(root, 'config', 'server_config.json'), 'w', encoding='utf-8') as f:
        json.dump(cfg, f)
    with open(os.path.join(root, 'config', 'users.json'), 'w', encoding='utf-8') as f:
        json.dump({'admin': {'password': 'admin', 'role': 'super_admin'}}, f)

    # 挂载的源目录：共享根**之外**（本机路径），里面放个文件让它有大小
    outside = os.path.join(root, 'outside', MOUNT_SRC)
    os.makedirs(outside, exist_ok=True)
    with open(os.path.join(outside, 'a.txt'), 'w', encoding='utf-8') as f:
        f.write('lf28')

    env = dict(os.environ, LEAFFS_PROJECT_ROOT=root, LEAFFS_NO_WEBVIEW='1')
    logf = open(os.path.join(root, 'server.log'), 'wb', buffering=0)
    proc = subprocess.Popen([sys.executable, '-m', 'leaffs'], cwd=PROJ_ROOT, env=env,
                            stdout=logf, stderr=subprocess.STDOUT)
    base = 'http://127.0.0.1:%d' % HTTP_PORT
    ws_url = 'ws://127.0.0.1:%d' % WS_PORT
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError('隔离实例提前退出')
            try:
                if httpx.get(base + '/api/ping', timeout=1.0).status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError('隔离实例没起来：%s' % base)

        # 本机一次性令牌 → 本机会话：挂载、以及"给挂载点设码"都只认它
        # （`_share_item_actor`：挂载点没有属主，码只能在服务端本机设）
        with open(os.path.join(root, 'config', 'local_token.txt'), encoding='utf-8') as f:
            tok = f.read().strip()
        assert tok, '服务端启动时应写入本机一次性令牌'
        with httpx.Client(base_url=base, timeout=20) as c:
            r = c.get('/api/admin/auto-login', params={'leaf': tok}, follow_redirects=False)
            assert r.status_code == 302, r.status_code
            local_cookie = c.cookies.get('wifi_session')
            assert local_cookie

        with httpx.Client(base_url=base, timeout=20) as c:
            c.cookies.set('wifi_session', local_cookie)
            r = c.post('/api/share/mount', json={'path': outside})
            assert r.status_code == 200, r.text
            vp = (r.json() or {}).get('path') or ''
            assert vp == 'public/mounts/' + MOUNT_SRC, r.text

        yield SimpleNamespace(root=root, base=base, ws_url=ws_url, vp=vp,
                              local_cookie=local_cookie)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        logf.close()


@pytest.fixture(scope='module')
def guest_cookie(inst):
    """游客会话（**只登录这一次**：游客登录按来源 IP 限次）"""
    with httpx.Client(base_url=inst.base, timeout=20) as c:
        r = c.post('/api/guest/login')
        assert r.status_code == 200, r.text
        ck = c.cookies.get('wifi_session')
    assert ck
    return ck


def _set_code(inst, code):
    """给那条挂载点设/清码（空串 = 清），返回服务端回的 enabled"""
    with httpx.Client(base_url=inst.base, timeout=20) as c:
        c.cookies.set('wifi_session', inst.local_cookie)
        r = c.post('/api/share/code', json={'path': inst.vp, 'code': code})
        assert r.status_code == 200, r.text
        return r.json().get('enabled')


def _http_list(inst, cookie, path):
    with httpx.Client(base_url=inst.base, timeout=20) as c:
        c.cookies.set('wifi_session', cookie)
        r = c.get('/api/files', params={'path': path})
        assert r.status_code == 200, r.text
        return r.json()


def _ws_msg(inst, cookie, path):
    from websockets.sync.client import connect
    with connect(inst.ws_url, additional_headers={'Cookie': 'wifi_session=%s' % cookie},
                 open_timeout=10) as ws:
        ws.send(json.dumps({'type': 'list', 'path': path}))
        for _ in range(6):
            msg = json.loads(ws.recv(timeout=5))
            if msg.get('type') in ('list', 'error'):
                return msg
    return None


def _ws_msg_raw(inst, cookie_header, path):
    """同 `_ws_msg`，但 Cookie 头由调用方整条给出（分享码票据在 `leaf_sh_tk` 里）"""
    from websockets.sync.client import connect
    with connect(inst.ws_url, additional_headers={'Cookie': cookie_header},
                 open_timeout=10) as ws:
        ws.send(json.dumps({'type': 'list', 'path': path}))
        for _ in range(6):
            msg = json.loads(ws.recv(timeout=5))
            if msg.get('type') in ('list', 'error'):
                return msg
    return None


def _list_raw(inst, cookie_header, path):
    """列出某路径，返回 (状态码, json)；Cookie 头整条给出（可为空 = 匿名）"""
    with httpx.Client(base_url=inst.base, timeout=20) as c:
        headers = {'Cookie': cookie_header} if cookie_header else {}
        r = c.get('/api/files', params={'path': path}, headers=headers)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {}


def _unlock(inst, label, code):
    """输码换票据（匿名即可），返回票据 Cookie 整条"""
    with httpx.Client(base_url=inst.base, timeout=20) as c:
        r = c.post('/api/share/auth', json={'label': label, 'code': code})
        assert r.status_code == 200, r.text
        sc = r.headers.get('set-cookie', '')
    assert sc, '输对码应当换到票据 Cookie'
    return sc.split(';')[0]


def _locked_label(inst, cookie):
    for f in (_http_list(inst, cookie, 'public/mounts').get('files') or []):
        if f.get('type') == 'locked':
            return f.get('label') or ''
    return ''


def _names(d):
    return sorted(f.get('name') for f in ((d or {}).get('files') or []))


def test_code_protected_mount_is_hidden_from_guests_on_both_paths(inst, guest_cookie):
    """★★ 防退化守卫：挂载点设了码，没过码的人在**两条路上**都拿不到它

    盯的是 `merge_into_list(unlocked=None)` 那个错误版本 —— "不过滤"就等于把设了码的
    挂载点白送给任何连得上 WS/HTTP 的人，分享码形同虚设（它正是靠"连名字都不给"来防的）。
    """
    assert _set_code(inst, SHARE_CODE) is True

    guest_http = _http_list(inst, guest_cookie, 'public/mounts')
    guest_ws = _ws_msg(inst, guest_cookie, 'public/mounts')

    # 对照：请求本身必须成功（这一层列得出来），否则下面"看不到"就是假绿
    assert guest_http.get('files') is not None, guest_http
    assert guest_ws and guest_ws.get('type') == 'list', guest_ws

    assert MOUNT_SRC not in _names(guest_http), \
        '没过码的游客在 HTTP 列表里看到了受保护的挂载点：%s' % _names(guest_http)
    assert MOUNT_SRC not in _names(guest_ws), (
        '没过码的游客在 **WS 列表**里看到了受保护的挂载点：%s\n'
        '（`unlocked=None` 的语义是"不过滤" —— 传它就等于绕过分享码）' % _names(guest_ws))
    assert any(f.get('type') == 'locked' for f in guest_http['files']), \
        '锁着的挂载点该给一个输码的占位（不给名字）：%s' % guest_http['files']


def test_unlocked_mount_comes_back_on_both_paths(inst, guest_cookie):
    """对照：码在时挡住、清掉之后两条路又都给 —— 证明挡住它的就是那条码

    自己先设一次码，不依赖上一条用例的执行顺序。
    """
    assert _set_code(inst, SHARE_CODE) is True
    assert MOUNT_SRC not in _names(_http_list(inst, guest_cookie, 'public/mounts'))

    assert _set_code(inst, '') is False

    http_names = _names(_http_list(inst, guest_cookie, 'public/mounts'))
    ws_names = _names(_ws_msg(inst, guest_cookie, 'public/mounts'))
    assert MOUNT_SRC in http_names, '码清了，游客在 HTTP 列表里还看不到：%s' % http_names
    assert MOUNT_SRC in ws_names, (
        '码清了，游客在 WS 列表里还看不到：%s\n'
        '（要么这条挂载点压根没进列表，要么两条路的口径不一致）' % ws_names)


def test_locked_mount_cannot_be_listed_by_path_either(inst, guest_cookie):
    """★★ 设了码的挂载点，**直取路径**也不给列（两条通道都要挡）

    只在条目层过滤挡不住它：挂载点的名字就是**源目录自己的名字**（`config`、`DCIM`、
    `电影`……），猜中 `?path=public/mounts/<名字>` 就能把里面的条目（名字/大小/时间）
    全列出来 —— 而"设了码就连名字都不给"正是这条闸要保的东西。
    最后一段是**对照**：输对码之后必须能列，否则这条用例只要"一律拒绝"就能骗过去。
    """
    assert _set_code(inst, SHARE_CODE) is True
    label = _locked_label(inst, guest_cookie)
    assert label, '锁着的挂载点该给一个可输码的占位：%s' % _http_list(inst, guest_cookie, 'public/mounts')

    st, body = _list_raw(inst, 'wifi_session=%s' % guest_cookie, inst.vp)
    assert st != 200 and 'a.txt' not in _names(body), \
        '没过码就直取挂载点路径，里面被列出来了：%s %s' % (st, body)

    ws = _ws_msg(inst, guest_cookie, inst.vp)
    assert ws and ws.get('type') == 'error', \
        'WS 那条路仍然把设码挂载点的内容发出来了：%s' % ws

    # 对照 1：拿码之后两条路都该给（WS 的 `list` 需要会话，所以票据叠在游客会话上）
    ticket = _unlock(inst, label, SHARE_CODE)
    st2, body2 = _list_raw(inst, ticket, inst.vp)
    assert st2 == 200 and 'a.txt' in _names(body2), \
        '输对码之后仍然列不出挂载点：%s %s' % (st2, body2)
    ws2 = _ws_msg_raw(inst, 'wifi_session=%s; %s' % (guest_cookie, ticket), inst.vp)
    assert ws2 and ws2.get('type') == 'list' and 'a.txt' in _names(ws2), \
        '输对码之后 WS 仍然列不出挂载点：%s' % ws2

    # 对照 2：与码无关的普通目录不受影响（别把闸做成"谁都别想列"）
    st3, body3 = _list_raw(inst, 'wifi_session=%s' % guest_cookie, 'public/mounts')
    assert st3 == 200 and body3.get('files') is not None, (st3, body3)
    assert _set_code(inst, '') is False


def test_url_downloader_refuses_a_mount_target(inst):
    """★★ 下载器也要过"挂载区只读"这道闸 —— 它原来是唯一漏掉的写入口

    它的落点是 `UPLOAD_DIR + save_dir`，收下指向挂载点的 save_dir 之后会在共享根里
    造出一个与挂载点同名的**实体目录**；挂载点反过来把它遮蔽掉（列表看不见、直链 404），
    磁盘上却真实存在。判据必须与 upload / mkdir / delete 同一处（`resolve_rel` 的映射标记）。
    """
    assert _set_code(inst, '') is False
    for sd in (inst.vp, inst.vp + '/sub'):
        with httpx.Client(base_url=inst.base, timeout=20) as c:
            c.cookies.set('wifi_session', inst.local_cookie)
            r = c.post('/api/url-download/start',
                       json={'url': 'https://example.com/x.txt', 'save_dir': sd})
            try:
                body = r.json()
            except Exception:
                body = {}
            assert not body.get('success'), \
                '下载器收下了指向挂载点的 save_dir（%s）：%s' % (sd, r.text)
            assert '映射区只读' in (body.get('error') or ''), r.text
    # 对照：普通目录不该被这道闸一起拒掉（否则"挂载点收不了"就可能是别的原因）
    with httpx.Client(base_url=inst.base, timeout=20) as c:
        c.cookies.set('wifi_session', inst.local_cookie)
        r = c.post('/api/url-download/start',
                   json={'url': 'https://example.com/x.txt', 'save_dir': 'public'})
        assert '映射区只读' not in r.text, '普通目录也被当成挂载区拒了：%s' % r.text
    # 磁盘：挂载点那个虚拟路径下不该多出实体目录
    shadow = os.path.join(inst.root, 'shared_files', 'public', 'mounts', MOUNT_SRC)
    assert not os.path.exists(shadow), '挂载点路径下被造出了实体目录：%s' % shadow
