# -*- coding: utf-8 -*-
"""LF-27：列表只有**一条**构建路径 —— HTTP 与 WS 必须给出同一个答案。

来由（2026-09-16，用户报告）：「在公共目录触发文件相关操作会使 shares 目录隐藏」。

根因：列表构建有**两份实现**，而"必须做的事"只挂在 HTTP 那份上：

    HTTP  /api/files       →  send_files  →  ...  →  merge_into_list()      →  有 shares
    WS    {"type":"list"}  →  只调 _fs.list_files()（纯磁盘扫描）            →  没有 shares

`public/shares` 是**虚拟区**（磁盘上从来不存在，源文件不搬动），它出现在列表里完全靠
运行时注入。前端 `loadFiles()` **优先走 WS** → 首次打开（WS 还没连上、回退 HTTP）有 shares，
之后任何文件操作触发的刷新都走 WS → shares 消失。

同一个根因还有**第二个面**（查方案时才发现、探针实测坐实）：`.uploads` 的拦截也只写在
`send_files` 里，WS 那条路能点名走进去 —— 实测 `{"type":"list","path":".uploads"}`
把 `probe123-0.part`（文件名/大小/时间）全列了出来。

修法：抽出 `files/api.build_listing()`（只"算"不发，HTTP/WS 共用）；
分享码判定收到 `share/access.code_gate()`（两边同一份）。

⚠️ 本文件用**独立数据根 + 独立端口**（不是会话级的 `server`/`client`）：
这里要**发布分享**和**设分享码**，都是留在服务进程里的共享状态 —— 一旦断言失败，
恢复语句就跑到 finally 之外去了，会连锁污染后面依赖 public 列表或游客的用例
（R1 那轮真踩过：一条红导致后面 9 条集体红）。

⚠️ 各用例的性质不一样，别把它们都当"复现测试"：
  · 用例 1、2 是**复现**：改之前必红（有探针实测留档）。
  · 用例 3 是**对照组**（两边都不显示 `.uploads`，改前改后都绿）。
  · 用例 4 是**防退化守卫**：改之前 WS 压根不含 shares，所以它在旧代码上也是绿的 ——
    它防的是修法的错误版本（WS 侧传 `unlocked=None` ＝ 不过滤 ＝ 绕过分享码）。
"""
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

HTTP_PORT, WS_PORT = 8111, 8112
PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARE_FILE = 'lf27-agree.txt'
TMP_DIRNAME = '.uploads'


def _ws_list(ws_url, cookie, path):
    """走 WS 发一条 list，返回收到的第一条 list / error 消息"""
    from websockets.sync.client import connect
    with connect(ws_url, additional_headers={'Cookie': 'wifi_session=%s' % cookie},
                 open_timeout=10) as ws:
        ws.send(json.dumps({'type': 'list', 'path': path}))
        for _ in range(6):
            msg = json.loads(ws.recv(timeout=5))
            if msg.get('type') in ('list', 'error'):
                return msg
    return None


def _names(d):
    return sorted(f.get('name') for f in ((d or {}).get('files') or []))


def _http_list(base, cookie, path):
    with httpx.Client(base_url=base, timeout=20) as c:
        c.cookies.set('wifi_session', cookie)
        r = c.get('/api/files', params={'path': path})
        return r


@pytest.fixture(scope='module')
def inst(data_root_factory):
    """隔离实例：独立数据根 + 独立端口，备好"一个已发布的分享" """
    root = data_root_factory('lf27_')
    cfg = {'http_port': HTTP_PORT, 'ws_port': WS_PORT, 'tls_enabled': False,
           'guest_mode': True, 'guest_public_write': True, 'access_log': False}
    with open(os.path.join(root, 'config', 'server_config.json'), 'w', encoding='utf-8') as f:
        json.dump(cfg, f)
    with open(os.path.join(root, 'config', 'users.json'), 'w', encoding='utf-8') as f:
        json.dump({'admin': {'password': 'admin', 'role': 'super_admin'}}, f)

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

        with httpx.Client(base_url=base, timeout=20) as c:
            r = c.post('/api/auth/login', json={'username': 'admin', 'password': 'admin'})
            assert r.status_code == 200, r.text
            admin_cookie = c.cookies.get('wifi_session')
            assert admin_cookie
            # 放一个文件并发布成分享：public/shares/<owner> 是虚拟区，
            # 没有映射时连 shares 这个目录本身都不存在，两条路都是空的、测不出差别。
            r = c.post('/api/upload?path=public',
                       files={'file': (SHARE_FILE, b'lf27', 'text/plain')})
            assert r.status_code == 200, r.text
            r = c.post('/api/share/publish', json={'paths': ['public/' + SHARE_FILE]})
            assert r.status_code == 200, r.text

        yield SimpleNamespace(root=root, base=base, ws_url=ws_url, admin_cookie=admin_cookie)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        logf.close()


@pytest.fixture(scope='module')
def guest_cookie(inst):
    """游客会话（**只登录这一次**：游客登录按来源 IP 限 10 次/分钟）"""
    with httpx.Client(base_url=inst.base, timeout=20) as c:
        r = c.post('/api/guest/login')
        assert r.status_code == 200, r.text
        ck = c.cookies.get('wifi_session')
    assert ck
    return ck


def test_ws_listing_has_shares_like_http(inst):
    """★ 用户报的那个现象：同一路径，WS 与 HTTP 的列表必须**完全一致**（含虚拟 shares）

    改之前：HTTP 有 shares、WS 没有 —— 前端优先走 WS，
    于是"页面刚打开 shares 在，做一次文件操作（上传/删除/新建文件夹）就没了"。
    """
    http_names = _names(_http_list(inst.base, inst.admin_cookie, 'public').json())
    ws_msg = _ws_list(inst.ws_url, inst.admin_cookie, 'public')
    assert ws_msg and ws_msg.get('type') == 'list', 'WS 没回列表：%r' % (ws_msg,)
    ws_names = _names(ws_msg)

    assert 'shares' in http_names, \
        'HTTP 侧就没有 shares（分享没发布成功？）：%s' % http_names
    assert ws_names == http_names, (
        'HTTP 与 WS 的列表不一致：\n  HTTP = %s\n  WS   = %s\n'
        '（改之前 WS 少一个 shares —— 前端优先走 WS，表现成"操作一次它就没了"）'
        % (http_names, ws_names))


def test_uploads_dir_is_rejected_on_both_paths(inst):
    """★ 第二个面：`.uploads` **点名进入**也在两边都被拒

    改之前：HTTP 被 `send_files` 里的 `is_upload_tmp_relpath` 拦下，
    而 WS 那条路**全文没有任何拦截** —— 能列出临时上传文件（文件名/大小/时间全给）。
    """
    d = os.path.join(inst.root, 'shared_files', TMP_DIRNAME)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'probe123-0.part'), 'wb') as f:
        f.write(b'partial upload data')

    r = _http_list(inst.base, inst.admin_cookie, TMP_DIRNAME)
    ws_msg = _ws_list(inst.ws_url, inst.admin_cookie, TMP_DIRNAME)

    assert r.status_code != 200, \
        'HTTP 能列上传临时目录：%d %s' % (r.status_code, r.text[:150])
    assert ws_msg and ws_msg.get('type') == 'error', (
        'WS 能点名走进 .uploads —— 临时上传文件的文件名/大小/时间全给出来了：%r' % (ws_msg,))


def test_uploads_dir_hidden_from_root_listing_on_both(inst):
    """对照组：共享根列表在两边**都**不显示 `.uploads` 这一项

    （`list_files` 自己就跳过它，所以这条改前改后都该绿 —— 放在这里是当参照物：
    它绿而上面那条红，说明差别只在"点名进入"那一层。）
    """
    http_names = _names(_http_list(inst.base, inst.admin_cookie, '').json())
    ws_names = _names(_ws_list(inst.ws_url, inst.admin_cookie, ''))

    assert TMP_DIRNAME not in http_names, 'HTTP 根列表里出现了临时目录：%s' % http_names
    assert TMP_DIRNAME not in ws_names, 'WS 根列表里出现了临时目录：%s' % ws_names
    assert ws_names == http_names, 'HTTP=%s WS=%s' % (http_names, ws_names)


def test_code_protected_share_is_hidden_on_both_paths(inst, guest_cookie):
    """★★ 防退化守卫：分享码设上之后，没过码的人在**两条路上**都看不到 shares

    防的是修法的错误版本：`merge_into_list(unlocked=None)` 的语义是**不过滤**，
    WS 上若图省事传 `None`，等于把设了码的分享目录白送给任何连得上 WS 的人
    —— 分享码形同虚设（它正是靠"连文件名/大小/时间都不给看"来防的）。

    ⚠️ 这条在**改之前也是绿的**（那时 WS 压根不含 shares），所以它是守卫、不是复现。
    真正让它有意义的是下面那两条**对照断言**：游客必须能看到 public 里的普通文件，
    否则"看不到 shares"可能只是因为整个请求失败了 —— 那就是假绿。
    """
    with httpx.Client(base_url=inst.base, timeout=20) as c:
        c.cookies.set('wifi_session', inst.admin_cookie)
        r = c.post('/api/share/code', json={'code': 'lf27code'})
        assert r.status_code == 200, r.text

    # ① 分享者本人免输码：两条路都该看得到
    own_http = _names(_http_list(inst.base, inst.admin_cookie, 'public').json())
    own_ws = _names(_ws_list(inst.ws_url, inst.admin_cookie, 'public'))
    assert 'shares' in own_http and 'shares' in own_ws, \
        '分享者本人在自己的列表里该看得到 shares（免输码）：HTTP=%s WS=%s' % (own_http, own_ws)

    # ② 游客没过码：两条路都不该出现（连文件夹本身都不给）
    guest_http = _names(_http_list(inst.base, guest_cookie, 'public').json())
    guest_ws_msg = _ws_list(inst.ws_url, guest_cookie, 'public')
    guest_ws = _names(guest_ws_msg)

    # 对照：请求本身必须成功 —— 游客看得到 public 里的普通文件
    assert SHARE_FILE in guest_http, (
        '游客连 public 里的普通文件都看不到（%s）—— 说明这次请求本身失败了，'
        '下面那条"看不到 shares"就不算数' % guest_http)
    assert guest_ws_msg and guest_ws_msg.get('type') == 'list' and SHARE_FILE in guest_ws, (
        '游客的 WS 列表没成功拿到普通文件（%r）—— 同样会让下面那条变成假绿'
        % (guest_ws_msg,))

    assert 'shares' not in guest_http, \
        '没过码的游客在 HTTP 列表里看到了受保护的 shares：%s' % guest_http
    assert 'shares' not in guest_ws, (
        '没过码的游客在 **WS 列表**里看到了受保护的 shares：%s\n'
        '（`unlocked=None` 的语义是"不过滤" —— 传它就等于绕过分享码）' % guest_ws)


def test_home_pages_show_ws_errors():
    """LF-27 收尾：两份 home 页都要处理 WS 的 `error` 消息（`case 'error'`）

    服务端现在把"列表读失败"如实回成 `{"type":"error"}`（原来回的是**空列表**），
    前端若没有这个分支就会**静默忽略** —— 用户什么都看不到，而列表停在旧状态。
    守卫写法与 LF-25 那条同类：**漏改一份就红**（`home.en.html` 是独立文件，
    改中文版忘了英文版是这类改动最常见的漏法）。

    纯文本检查，不需要起服务。
    """
    base = os.path.join(PROJ_ROOT, 'leaffs', 'web_page', 'home')
    missing = []
    for name in ('home.html', 'home.en.html'):
        with open(os.path.join(base, name), encoding='utf-8') as f:
            text = f.read()
        if "case 'error'" not in text:
            missing.append("%s：缺 case 'error'（WS 报错会被静默忽略）" % name)
        if 'toast(' not in text:
            missing.append('%s：没有 toast 可用（提示发不出去）' % name)
    assert not missing, 'WS 错误提示没补齐：\n  ' + '\n  '.join(missing)
