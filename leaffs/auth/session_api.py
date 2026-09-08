"""Session API — 会话列表/撤销"""

import json

# B-16：会话列表返回条数上限（按 expiry 倒序取最近 N 条，total 反映真实总数）
SESSION_LIST_LIMIT = 200


def sessions_list(handler, get_all_sessions):
    """获取所有活跃会话（最近 SESSION_LIST_LIMIT 条，按 expiry 倒序）"""
    if handler._get_effective_role() not in ('admin', 'super_admin'):
        handler.send_json({'error': 'Forbidden'}, 403); return
    sessions = get_all_sessions()
    items = sorted(sessions.items(),
                   key=lambda kv: (kv[1].get('expiry', 0) or 0), reverse=True)
    total = len(items)
    items = items[:SESSION_LIST_LIMIT]
    result = []
    for sid, info in items:
        result.append({
            'username': info['username'],
            'role': info['role'],
            'expiry': info['expiry'],
            'session': sid[:12] + '...'
        })
    handler.send_json({'sessions': result, 'total': total})


def session_revoke(handler, revoke_session_by_prefix):
    """撤销指定会话"""
    if handler._get_effective_role() not in ('admin', 'super_admin'):
        handler.send_json({'error': 'Forbidden'}, 403); return
    try:
        length = int(handler.headers.get('Content-Length', 0))
        data = json.loads(handler.rfile.read(length).decode())
        if not isinstance(data, dict):
            data = {}
        session_id_full = str(data.get('session', '')).strip()
        prefix = session_id_full.replace('...', '')
        if not prefix:
            handler.send_json({'success': False, 'error': '参数错误'}, 400); return
        revoked = revoke_session_by_prefix(prefix)
        if revoked:
            # B-16：撤销成功记安全审计（IC-LOG security_event；detail 内部会脱敏）
            from leaffs.utils import log as _ut_log
            try:
                operator = handler._get_username_from_session() or ''
            except Exception:
                operator = ''
            _ut_log.security_event('session_revoke',
                                   f'会话撤销: session={session_id_full} by={operator}',
                                   'warn')
        handler.send_json({'success': revoked})
    except json.JSONDecodeError:
        # B-15：畸形请求体不回显内部解析错误
        handler.send_json({'error': '请求体必须是合法 JSON'}, 400)
    except Exception:
        # B-15：内部错误不回显细节
        handler.send_json({'error': '服务器内部错误'}, 500)