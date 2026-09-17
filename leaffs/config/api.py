"""配置 API — 基础配置/高级配置/深度配置"""

import json
import logging
import traceback

logger = logging.getLogger('wifi_convey')

# 深度配置里**只有超管**能改的字段（安全相关）：审计日志开关、会话寿命、口令哈希强度，
# 以及证书/绑定的信任面。普通 admin 能改这些 = 能抹掉自己的痕迹、能削弱口令。
_DEEP_SUPER_ONLY = {
    'access_log',            # 关掉全部访问日志
    'session_expiry_days',   # 会话寿命（无上下限）
    'pbkdf2_iterations',     # 口令哈希强度
    'salt_length',
    'auto_trust_ca',
    'trust_bind_host',
}


def get_config(handler, get_max_concurrent, get_speed_limit, get_guest_mode,
               get_default_user_quota, get_public_quota, get_total_quota):
    """GET 基础配置"""
    if handler._get_effective_role() not in ('admin', 'super_admin'):
        handler.send_json({'error': 'Forbidden'}, 403); return
    handler.send_json({
        'max_concurrent': get_max_concurrent(), 'download_speed_limit': get_speed_limit(),
        'guest_mode': get_guest_mode(),
        'user_quota': get_default_user_quota(), 'public_quota': get_public_quota(), 'total_quota': get_total_quota()
    })


def set_config(handler, add_log, update_concurrent_limit, set_speed_limit,
               set_guest_mode, set_quotas, get_speed_limit, get_guest_mode):
    """POST 基础配置"""
    try:
        length = int(handler.headers.get('Content-Length', 0))
        data = json.loads(handler.rfile.read(length).decode())
        caller_role = handler._get_current_caller_role()
        if caller_role not in ('super_admin', 'admin'):
            handler.send_json({'error': '无权限执行此操作'}, 403)
            return
        caller_user = handler._get_username_from_session()
        if handler.client_address[0] in ('127.0.0.1', '::1'):
            caller_user = '服务端(' + (caller_user or '本地') + ')'
        elif not caller_user:
            caller_user = '未登录'
        changed = []
        save_failed = []
        if 'max_concurrent' in data:
            ok, msg = update_concurrent_limit(int(data['max_concurrent']))
            if not ok:
                # "保存失败" 是服务器侧问题（500），其余是参数问题（400）
                handler.send_json({'success': False, 'error': msg},
                                  500 if msg.startswith('保存失败') else 400)
                return
            changed.append(f'并发数={data["max_concurrent"]}')
        if 'download_speed_limit' in data:
            if not set_speed_limit(int(data['download_speed_limit'])):
                save_failed.append('限速')
            changed.append(f'限速={data["download_speed_limit"]}KB/s')
        if 'guest_mode' in data:
            if not set_guest_mode(data['guest_mode']):
                save_failed.append('游客模式')
            changed.append('游客模式=' + ('开' if data['guest_mode'] else '关'))
        if 'user_quota' in data or 'public_quota' in data or 'total_quota' in data:
            uq = data.get('user_quota')
            pq = data.get('public_quota')
            tq = data.get('total_quota')
            if not set_quotas(user_q=uq, public_q=pq, total_q=tq):
                save_failed.append('配额')
            changed.append('配额更新')
        if save_failed:
            # 落盘失败必须说出来：改动只在内存里，重启就没了 —— 回 success 就是骗人
            add_log(f'配置保存失败 [{caller_user}({caller_role})]: {", ".join(save_failed)}', 'err')
            handler.send_json({'success': False,
                               'error': '保存失败（改动只在内存里，重启后会丢）：'
                                        + '、'.join(save_failed)}, 500)
            return
        if changed:
            add_log(f'配置更新 [{caller_user}({caller_role})]: {", ".join(changed)}', 'ok')
        handler.send_json({'success': True})
    except json.JSONDecodeError:
        # B-15：畸形/非 JSON 请求体不回显内部错误
        handler.send_json({'error': '请求体必须是合法 JSON'}, 400)
    except Exception:
        logger.error(f'set_config 失败: {traceback.format_exc()}')
        handler.send_json({'error': '服务器内部错误'}, 500)


def get_config_advanced(handler, COPY_BUFFER_SIZE, MAX_API_BODY_SIZE,
                        PREVIEW_MAX_SIZE, get_upload_max_size):
    """GET 高级配置（返回 cfg_core 中实际生效的值）"""
    if handler._get_effective_role() not in ('admin', 'super_admin'):
        handler.send_json({'error': 'Forbidden'}, 403); return
    from leaffs.config import core as _cc
    data = {
        'http_port': _cc.PORT,
        'ws_port': _cc.WS_PORT,
        'copy_buffer_size': _cc.get_copy_buffer_size(),
        'file_cache_ttl': _cc.get_cache_ttl(),
        'folder_cache_ttl': _cc.get_folder_size_ttl(),
        'debounce_delay': _cc.get_debounce_delay(),
        'api_body_size': _cc.get_max_api_body_size(),
        'cache_max_items': _cc.get_cache_max_items(),
        'preview_max_size': _cc.get_preview_max_size(),
        'upload_max_size': _cc.get_upload_max_size(),
        'upload_chunk': _cc.get_upload_chunk(),
        'zip_max_files': _cc.get_zip_max_files(),
        'zip_streaming': _cc.get_zip_streaming(),
        'tls_enabled': _cc.get_tls_enabled(),
        'tls_trust_port': _cc.get_tls_trust_port(),
    }
    handler.send_json(data)


def set_config_advanced(handler, add_log, set_ports, get_deep_config_dict, apply_deep_config):
    """POST 高级配置"""
    try:
        length = int(handler.headers.get('Content-Length', 0))
        data = json.loads(handler.rfile.read(length).decode())
        caller_role = handler._get_current_caller_role()
        if caller_role not in ('super_admin', 'admin'):
            handler.send_json({'error': '无权限执行此操作'}, 403)
            return
        caller_user = handler._get_username_from_session()
        if handler.client_address[0] in ('127.0.0.1', '::1'):
            caller_user = '服务端(' + (caller_user or '本地') + ')'
        elif not caller_user:
            caller_user = '未登录'
        # B-12 [D7]：请求体含 tls_enabled 时仅 super_admin 可提交（整体 403，admin 不得借道改其它字段）
        if 'tls_enabled' in data and caller_role != 'super_admin':
            handler.send_json({'success': False, 'error': '仅超级管理员可修改 TLS 设置'}, 403)
            return
        http_port = data.get('http_port')
        ws_port = data.get('ws_port')
        changed = []
        if http_port is not None:
            ok, msg = set_ports(http_port=http_port, ws_port=ws_port)
            if not ok:
                handler.send_json({'success': False, 'error': msg}, 400)
                return
            changed.append(f'HTTP端口={http_port}')
        if ws_port is not None:
            if http_port is None:
                set_ports(ws_port=ws_port)
            changed.append(f'WS端口={ws_port}')
        # TLS 开关（重启后生效；B-12 [D7]：仅 super_admin 可改——上方已整体拦截；关闭方向需二次确认）
        if 'tls_enabled' in data:
            on = bool(data['tls_enabled'])
            if not on and data.get('confirm_tls_off') is not True:
                handler.send_json({'success': False, 'error': '关闭 HTTPS 需确认（confirm_tls_off:true）'}, 400)
                return
            from leaffs.config import core as _cc
            if not _cc.set_tls_enabled(on):
                handler.send_json({'success': False,
                                   'error': '保存失败：TLS 开关只在内存里，重启后仍是原值'}, 500)
                return
            changed.append('HTTPS(TLS)=' + ('开启' if on else '关闭') + '（重启后生效）')
            if not on:
                # 高危事件（B-12）：关闭 TLS = 切换明文传输，单独记高危日志（含操作者/来源 IP）
                add_log(f'高危操作: HTTPS(TLS) 已被关闭（明文模式）[操作者: {caller_user}({caller_role})'
                        f'@IP {handler.client_address[0]}]', 'err')
        # 证书引导页端口（重启后生效，含端口重复校验）
        if 'tls_trust_port' in data:
            from leaffs.config import core as _cc
            ok, msg = _cc.set_tls_trust_port(int(data['tls_trust_port']))
            if not ok:
                handler.send_json({'success': False, 'error': msg}, 400)
                return
            changed.append(f'证书引导页端口={int(data["tls_trust_port"])}（重启后生效）')
        # 其余运行参数：把页面字段名映射到 cfg_core 的键后统一走 apply_deep_config
        # （持久化到 server_config.json 并同步模块运行时常量）
        key_map = {'file_cache_ttl': 'cache_ttl',
                   'folder_cache_ttl': 'folder_size_ttl',
                   'api_body_size': 'max_api_body_size'}
        supported = ('copy_buffer_size', 'cache_max_items', 'cache_ttl',
                     'folder_size_ttl', 'debounce_delay', 'max_api_body_size',
                     'preview_max_size', 'upload_max_size',
                     'upload_chunk', 'zip_max_files', 'zip_streaming')
        patch = {}
        for k, v in data.items():
            if k in ('http_port', 'ws_port'):
                continue
            bk = key_map.get(k, k)
            if bk in supported:
                try:
                    patch[bk] = v
                except Exception:
                    continue
        if patch:
            # apply_deep_config 返回 (ok, err, changed)；未知键 / 越界 / 下限违规 → 400
            ok, err, _changed = apply_deep_config(patch)
            if not ok:
                handler.send_json({'success': False, 'error': err}, 400)
                return
            changed.append('运行参数已保存（部分参数重启后完全生效）')
        if changed:
            suffix = '（重启后生效）' if (http_port is not None or ws_port is not None) else ''
            add_log(f'配置更新 [{caller_user}({caller_role})]: {", ".join(changed)}{suffix}', 'ok')
        handler.send_json({'success': True, 'msg': '配置已保存'})
    except json.JSONDecodeError:
        # B-15：畸形/非 JSON 请求体不回显内部错误
        handler.send_json({'success': False, 'error': '请求体必须是合法 JSON'}, 400)
    except Exception:
        logger.error(f'set_config_advanced 失败: {traceback.format_exc()}')
        handler.send_json({'success': False, 'error': '服务器内部错误'}, 500)


def get_config_deep(handler, get_deep_config_dict):
    """GET 深度配置"""
    if handler._get_effective_role() not in ('admin', 'super_admin'):
        handler.send_json({'error': 'Forbidden'}, 403); return
    from leaffs.config import core as _cc
    cfg = get_deep_config_dict()
    cfg['username_max_length'] = 20
    cfg['password_max_length'] = 50
    # B-11：向页面暴露安全下限提示字段（pbkdf2/salt 不得低于该值）
    cfg['pbkdf2_iterations_min'] = _cc.PBKDF2_ITER_MIN
    cfg['salt_length_min'] = _cc.SALT_LEN_MIN
    handler.send_json(cfg)


def set_config_deep(handler, add_log, apply_deep_config):
    """POST 深度配置"""
    try:
        length = int(handler.headers.get('Content-Length', 0))
        data = json.loads(handler.rfile.read(length).decode())
        caller_role = handler._get_current_caller_role()
        if caller_role not in ('super_admin', 'admin'):
            handler.send_json({'error': '无权限执行此操作'}, 403)
            return
        caller_user = handler._get_username_from_session()
        if handler.client_address[0] in ('127.0.0.1', '::1'):
            caller_user = '服务端(' + (caller_user or '本地') + ')'
        elif not caller_user:
            caller_user = '未登录'
        # 安全相关字段只有 super_admin 能改：关审计日志、改会话寿命、下调口令哈希强度 ——
        # 落到普通 admin 手里等于"能抹自己的痕迹、能把会话挂更久、能削弱口令"。
        # （tls_enabled 早就单独收到超管了，这里跟它对齐。）
        if caller_role != 'super_admin' and isinstance(data, dict):
            hit = sorted(k for k in data if k in _DEEP_SUPER_ONLY)
            if hit:
                handler.send_json({'success': False,
                                   'error': '这些字段只有超管能改：' + '、'.join(hit)}, 403)
                return
        # B-11：低于下限（pbkdf2_iterations < 100000 / salt_length < 16）整体失败 → 400 带具体文案
        ok, err, changed = apply_deep_config(data)
        if not ok:
            handler.send_json({'success': False, 'error': err}, 400)
            return
        if not changed:
            # 键都合法、但值跟当前一样：别谎报"配置已更新"
            add_log(f'配置更新 [{caller_user}({caller_role})]: 深度配置（无变化）', 'ok')
            handler.send_json({'success': True, 'msg': '没有变化'})
            return
        add_log(f'配置更新 [{caller_user}({caller_role})]: 深度配置', 'ok')
        handler.send_json({'success': True, 'msg': '配置已更新'})
    except json.JSONDecodeError:
        # B-15：畸形/非 JSON 请求体不回显内部错误
        handler.send_json({'error': '请求体必须是合法 JSON'}, 400)
    except Exception:
        logger.error(f'set_config_deep 失败: {traceback.format_exc()}')
        handler.send_json({'error': '服务器内部错误'}, 500)