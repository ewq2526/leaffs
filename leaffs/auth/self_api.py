"""账号自助 API（"我的"页）—— /api/account/me、/api/account/password、/api/account/revoke-sessions

仅限"当前登录会话本人"操作：
  * me              查看自己的角色/用量/当前设备/本账号会话；
  * password        自助改密（须验旧密码，游客不可改）；
  * revoke-sessions 踢除本账号除当前设备外的其它会话（游客无可管理会话）。
"""

import json
import os
import time


def _self_session(handler):
    """解析当前登录会话，返回 (role, username, sid)；未登录/会话无效返回 (None, '', '')"""
    try:
        import leaffs.auth.core as _ac
        cookie = handler.headers.get('Cookie', '')
        role, sid = _ac.get_session(cookie, True, handler.client_address[0])
        if not role or not sid:
            return None, '', ''
        return role, _ac.get_session_username(sid), sid
    except Exception:
        return None, '', ''


def account_me(handler):
    """GET /api/account/me —— 我的信息汇总"""
    role, username, sid = _self_session(handler)
    if not role:
        handler.send_json({'error': '未登录或会话已失效'}, 401)
        return
    import leaffs.auth.core as _ac
    import leaffs.config.core as _cc
    import leaffs.files.core as _fs
    ip = handler.client_address[0]
    ua = handler.headers.get('User-Agent', '')

    # 默认口令提示（与 /api/auth/check 同口径）
    need_change = False
    expiry_ts = 0
    try:
        with _ac._sessions_lock:
            info = _ac._sessions.get(sid)
            if info:
                need_change = bool(info.get('need_change_password', False))
                expiry_ts = info.get('expiry', 0) or 0
    except Exception:
        pass
    if not need_change and role in ('admin', 'super_admin'):
        try:
            need_change = bool(_ac.is_default_admin_password())
        except Exception:
            need_change = False

    # 用量：游客=公共目录；普通用户=自己的 users/<name> 目录；管理员=全站
    used = 0
    quota = 0
    try:
        if role == 'guest':
            stats = _fs.get_server_stats()
            used = int(stats.get('public_used', 0) or 0)
            quota = int(_cc.get_public_quota() or 0)
        elif role == 'user':
            p = os.path.join(_fs.UPLOAD_DIR, 'users', username)
            if os.path.isdir(p):
                try:
                    from leaffs.utils import core as _utc
                    used = int(_utc.get_folder_size(p) or 0)
                except Exception:
                    used = 0
            quota = int(_ac.get_user_quota(username) or 0) or int(_cc.get_default_user_quota() or 0)
        else:  # admin / super_admin：全站用量
            stats = _fs.get_server_stats()
            used = int(stats.get('total_used', 0) or 0)
            quota = int(_cc.get_total_quota() or 0)
    except Exception:
        pass

    # 本账号会话列表（游客：同来源 IP 的游客会话）
    sessions = []
    try:
        with _ac._sessions_lock:
            for sid2, i2 in list(_ac._sessions.items()):
                if (i2.get('expiry', 0) or 0) <= time.time():
                    continue
                if role == 'guest':
                    if not (i2.get('username') == '游客'
                            and _ac._same_client(i2.get('ip', ''), ip)):
                        continue
                elif i2.get('username') != username:
                    continue
                sessions.append({
                    'short': str(sid2)[:10] + '…',
                    'ip': i2.get('ip', ''),
                    'expiry': i2.get('expiry', 0) or 0,
                    'current': sid2 == sid,
                })
    except Exception:
        sessions = []
    sessions.sort(key=lambda s: (not s.get('current', False), -(s.get('expiry') or 0)))

    # 服务器地址（供展示/复制：局域网 IP 而非 localhost/127.0.0.1，手机可直接访问）
    server_url = ''
    try:
        import leaffs.server.hosts as _hosts
        scheme = 'https' if _cc.get_tls_enabled() else 'http'
        _sip = _hosts.primary_lan_ip()
        server_url = f'{scheme}://{_sip}:{_cc.PORT}' if _cc.PORT != 80 else f'{scheme}://{_sip}'
    except Exception:
        pass

    handler.send_json({
        'success': True,
        'username': username,
        'role': role,
        'guest_mode': bool(_cc.get_guest_mode()),
        'need_change_password': need_change,
        'server_url': server_url,
        'accent': _ac.get_user_accent(username),
        'current': {'ip': ip, 'ua': (ua or '')[:120], 'expiry': expiry_ts},
        'quota': {'used': used, 'quota': quota},
        'sessions': sessions,
    })


def account_password(handler):
    """POST /api/account/password —— 自助改密（验旧密码；游客不可改）"""
    role, username, sid = _self_session(handler)
    if not role:
        handler.send_json({'error': '未登录或会话已失效'}, 401)
        return
    if role == 'guest' or username == '游客':
        handler.send_json({'success': False, 'error': '游客账号无需修改密码'}, 403)
        return
    try:
        length = int(handler.headers.get('Content-Length', 0))
        data = json.loads(handler.rfile.read(length).decode())
        if not isinstance(data, dict):
            data = {}
        old_pw = data.get('old_password', '')
        new_pw = data.get('new_password', '')
        if not isinstance(old_pw, str) or not isinstance(new_pw, str) \
                or not old_pw or not new_pw:
            handler.send_json({'success': False, 'error': '参数错误'}, 400)
            return
        import leaffs.auth.core as _ac
        if _ac.verify_login(username, old_pw) is None:
            handler.send_json({'success': False, 'error': '原密码不正确'}, 403)
            return
        ok, err = _ac.self_change_password(username, new_pw, keep_sid=sid)
        if not ok:
            handler.send_json({'success': False, 'error': err}, 400)
            return
        handler.send_json({'success': True, 'msg': '密码已修改'})
    except json.JSONDecodeError:
        # B-15：畸形请求体不回显内部解析错误
        handler.send_json({'error': '请求体必须是合法 JSON'}, 400)
    except Exception:
        # B-15：内部错误不回显细节
        handler.send_json({'error': '服务器内部错误'}, 500)


def account_revoke_sessions(handler):
    """POST /api/account/revoke-sessions —— 踢除本账号其它设备会话（保留当前；游客不可用）"""
    role, username, sid = _self_session(handler)
    if not role:
        handler.send_json({'error': '未登录或会话已失效'}, 401)
        return
    if role == 'guest' or username == '游客':
        handler.send_json({'success': False, 'error': '游客账号无可管理的会话'}, 403)
        return
    import leaffs.auth.core as _ac
    n = 0
    try:
        with _ac._sessions_lock:
            for sid2 in list(_ac._sessions.keys()):
                i2 = _ac._sessions.get(sid2)
                if i2 and i2.get('username') == username and sid2 != sid:
                    del _ac._sessions[sid2]
                    n += 1
    except Exception:
        pass
    # B-16：踢会话属安全事件，记审计（detail 内部会脱敏）
    try:
        from leaffs.utils import log as _ut_log
        _ut_log.security_event('session_revoke',
                               f'账号自助踢除会话: user={username} revoked={n} '
                               f'ip={handler.client_address[0]}', 'warn')
    except Exception:
        pass
    handler.send_json({'success': True, 'revoked': n})


def account_set_lang(handler):
    """POST /api/account/lang —— 把当前账号的界面语言偏好持久化到服务端。

    本机 webview 是无痕会话、cookie/localStorage 存不住，语言选择在服务端账号
    （users.json）保存后，跨启动（重新登录本账号）仍能恢复；游客无账号不保存。
    """
    role, username, sid = _self_session(handler)
    if not role:
        handler.send_json({'error': '未登录或会话已失效'}, 401)
        return
    if role == 'guest' or username == '游客':
        handler.send_json({'success': False, 'error': '游客账号不保存语言偏好'}, 403)
        return
    try:
        length = int(handler.headers.get('Content-Length', 0))
        data = json.loads(handler.rfile.read(length).decode())
        if not isinstance(data, dict):
            data = {}
        lang = 'en' if data.get('lang') == 'en' else 'zh'
        import leaffs.auth.core as _ac
        _ac.set_user_ui_lang(username, lang)
        handler.send_json({'success': True, 'lang': lang})
    except json.JSONDecodeError:
        # B-15：畸形请求体不回显内部解析错误
        handler.send_json({'error': '请求体必须是合法 JSON'}, 400)
    except Exception:
        # B-15：内部错误不回显细节
        handler.send_json({'error': '服务器内部错误'}, 500)


def account_set_accent(handler):
    """POST /api/account/theme —— 持久化当前账号的主题主色到服务端。

    与语言偏好同机制：本机 webview 无痕会话存不住客户端存储，主题主色存服务端
    账号后跨启动（重新登录本账号）仍能恢复；游客不保存。
    """
    role, username, sid = _self_session(handler)
    if not role:
        handler.send_json({'error': '未登录或会话已失效'}, 401)
        return
    if role == 'guest' or username == '游客':
        handler.send_json({'success': False, 'error': '游客账号不保存主题设置'}, 403)
        return
    try:
        length = int(handler.headers.get('Content-Length', 0))
        data = json.loads(handler.rfile.read(length).decode())
        if not isinstance(data, dict):
            data = {}
        import leaffs.auth.core as _ac
        accent = str(data.get('accent', '') or '')
        _ac.set_user_accent(username, accent)
        handler.send_json({'success': True, 'accent': _ac.get_user_accent(username)})
    except json.JSONDecodeError:
        handler.send_json({'error': '请求体必须是合法 JSON'}, 400)
    except Exception:
        handler.send_json({'error': '服务器内部错误'}, 500)
