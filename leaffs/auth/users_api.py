"""用户管理 API — 增删改查/角色/密码/限速/配额/重命名"""

import json
import os
import time


def _archive_user_dir(username):
    """把被删用户的家目录**改名归档**到 `users/.deleted/<name>-<时间戳>[-序号]/`。

    返回 `(归档相对路径 or None, 错误文案 or None)`。没目录可归档不算错（用户从没登过录）
    → `(None, None)`。

    为什么归档而不是删除：删账号是管理员**一个操作**，但删目录会永久毁掉那个用户的
    全部文件 —— 真机上救不回来。归档之后就没事了：
      * 同名重建拿到的是**空目录**（原来会继承旧内容，那是个真缺陷）；
      * 文件还在，名字改回去就能恢复；
      * 不占额外空间（只是 rename）。
    """
    try:
        from leaffs.utils.core import UPLOAD_DIR, invalidate_folder_cache
    except Exception as e:
        from leaffs.runtime_log import log_exception
        log_exception('归档被删用户的家目录（取路径工具）', e)
        return None, '服务器内部错误'
    src = os.path.join(UPLOAD_DIR, 'users', username)
    if not os.path.isdir(src):
        return None, None
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime())
    dest_dir = os.path.join(UPLOAD_DIR, 'users', '.deleted')
    try:
        os.makedirs(dest_dir, exist_ok=True)
        # 时间戳只到秒，而**同一秒内删两次同名用户**是真会发生的：删掉 → 同名重建 → 再删，
        # 或者第一次失败后**立刻重试**。撞名时 os.rename 不是"覆盖"而是直接抛 ——
        # Windows 上 rename 到已存在目录必然 FileExistsError（WinError 183），
        # 于是整个删账号操作失败（账号没删、分享码与映射也没清，因为清理排在归档之后），
        # 而返回的文案还写着"可重试"—— 同秒重试**必然**再失败。
        # 所以唯一性不能指望时间戳：撞了就往后加序号。
        dest = os.path.join(dest_dir, '%s-%s' % (username, stamp))
        n = 1
        while os.path.exists(dest):
            n += 1
            dest = os.path.join(dest_dir, '%s-%s-%d' % (username, stamp, n))
        os.rename(src, dest)
        archived = 'users/.deleted/%s' % os.path.basename(dest)
    except Exception as e:
        from leaffs.runtime_log import log_exception
        log_exception('归档被删用户的家目录', e)
        return None, '用户目录归档失败（账号未删除，可重试）'
    try:
        invalidate_folder_cache(os.path.join(UPLOAD_DIR, 'users'), recursive=True)
    except Exception:
        pass
    return archived, None


def _caller_sid(handler):
    """取当前操作者会话 sid（改角色/改密后以 keep_sid 保活操作者自身，见 B-10）。"""
    try:
        import leaffs.auth.core as _ac
        cookie = handler.headers.get('Cookie', '')
        _, sid = _ac.get_session(cookie, handler.client_address[0])
        return sid or ''
    except Exception:
        return ''


def users_list(handler):
    """用户列表"""
    if handler._get_effective_role() not in ('admin', 'super_admin'):
        handler.send_json({'error': 'Forbidden'}, 403); return
    import leaffs.auth.core as _ac
    handler.send_json({'users': _ac.list_users()})


def users_add(handler, add_user):
    """添加用户"""
    # B-01：服务端角色门槛（不依赖调用方自报角色参数），匿名/guest/普通用户一律 403
    if handler._get_current_caller_role() not in ('admin', 'super_admin'):
        handler.send_json({'error': 'Forbidden'}, 403); return
    try:
        length = int(handler.headers.get('Content-Length', 0))
        data = json.loads(handler.rfile.read(length).decode())
        if not isinstance(data, dict):
            data = {}
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()
        role = data.get('role', 'user')
        if not username:
            handler.send_json({'success': False, 'error': '用户名不能为空'}, 400); return
        caller = handler._get_current_caller_role()
        # B-03：口令长度等校验统一由 ac_core.add_user（validate_password）返回错误文本
        ok, err = add_user(username, password, role, caller)
        if ok:
            handler.send_json({'success': True})
        else:
            handler.send_json({'success': False, 'error': err}, 400)
    except json.JSONDecodeError:
        # B-15：畸形请求体不回显内部解析错误
        handler.send_json({'error': '请求体必须是合法 JSON'}, 400)
    except Exception:
        # B-15：内部错误不回显细节
        handler.send_json({'error': '服务器内部错误'}, 500)


def users_delete(handler, delete_user):
    """删除用户（家目录**归档**到 `users/.deleted/`，文件不删）"""
    try:
        length = int(handler.headers.get('Content-Length', 0))
        data = json.loads(handler.rfile.read(length).decode())
        if not isinstance(data, dict):
            data = {}
        username = data.get('username', '').strip()
        caller = handler._get_current_caller_role()
        import leaffs.auth.core as _ac
        # 1) 前置校验（不落盘）：角色门槛 / 存在性 / 最后一个超管
        ok, err = _ac.precheck_user_delete(username, caller)
        if not ok:
            handler.send_json({'success': False, 'error': err}, 400); return
        # 2) 先把家目录归档；动不了就整体失败，账号一个字节都不动
        archived, aerr = _archive_user_dir(username)
        if aerr:
            handler.send_json({'success': False, 'error': aerr}, 500); return
        # 2.5) T-1：级联清理该用户的**分享**记录 —— 分享码/错误计数/两类锁/授权票据
        #      （share_access.json）与全部分享映射（share_mappings.json）。
        # 顺序是有意的：这两步不可逆但**危害小**（用户重设码即可），而"账号删了、码还在"
        # 会让重建的同名账号继承旧码（没人知道它 → 分享直接废掉）。宁可留下
        # "账号还在、码被清"，也不要留下"账号没了、码还在"。
        try:
            import leaffs.share.access as _sacc
            import leaffs.share.mappings as _smap
            _sacc.purge_user(username)
            _smap.remove_by_owner(username)
        except Exception as e:
            from leaffs.runtime_log import log_exception
            log_exception('删除用户时清理分享记录', e)
        # 3) 提交：删账号 + 踢掉该用户全部会话
        ok, err = delete_user(username, caller)
        if ok:
            handler.send_json({'success': True, 'archived_dir': archived})
        else:
            handler.send_json({'success': False, 'error': err}, 400)
    except json.JSONDecodeError:
        # B-15：畸形请求体不回显内部解析错误
        handler.send_json({'error': '请求体必须是合法 JSON'}, 400)
    except Exception:
        # B-15：内部错误不回显细节
        handler.send_json({'error': '服务器内部错误'}, 500)


def users_role(handler, update_user_role):
    """修改用户角色"""
    try:
        length = int(handler.headers.get('Content-Length', 0))
        data = json.loads(handler.rfile.read(length).decode())
        if not isinstance(data, dict):
            data = {}
        username = data.get('username', '').strip()
        new_role = data.get('role', '').strip()
        if not username or new_role not in ('admin', 'user'):
            handler.send_json({'success': False, 'error': '参数错误'}, 400); return
        caller = handler._get_current_caller_role()
        # B-10：keep_sid 保活操作者自身会话；目标用户其它会话即时注销
        ok, err = update_user_role(username, new_role, caller, keep_sid=_caller_sid(handler))
        if ok:
            handler.send_json({'success': True})
        else:
            handler.send_json({'success': False, 'error': err}, 400)
    except json.JSONDecodeError:
        # B-15：畸形请求体不回显内部解析错误
        handler.send_json({'error': '请求体必须是合法 JSON'}, 400)
    except Exception:
        # B-15：内部错误不回显细节
        handler.send_json({'error': '服务器内部错误'}, 500)


def users_password(handler, change_password):
    """修改用户密码"""
    # ② 用户拍板：users_password 先做角色门槛（admin/super_admin）再做业务
    if handler._get_current_caller_role() not in ('admin', 'super_admin'):
        handler.send_json({'error': 'Forbidden'}, 403); return
    try:
        length = int(handler.headers.get('Content-Length', 0))
        data = json.loads(handler.rfile.read(length).decode())
        if not isinstance(data, dict):
            data = {}
        username = data.get('username', '').strip()
        new_password = data.get('password', '').strip()
        if not username:
            handler.send_json({'success': False, 'error': '参数错误'}, 400); return
        caller = handler._get_current_caller_role()
        # B-03：长度等校验统一由 ac_core.change_password（validate_password）返回错误文本；
        # B-10：keep_sid 保活操作者自身（如 admin/super_admin 自改默认口令后不掉线）
        ok, err = change_password(username, new_password, caller, keep_sid=_caller_sid(handler))
        if ok:
            handler.send_json({'success': True})
        else:
            handler.send_json({'success': False, 'error': err}, 400)
    except json.JSONDecodeError:
        # B-15：畸形请求体不回显内部解析错误
        handler.send_json({'error': '请求体必须是合法 JSON'}, 400)
    except Exception:
        # B-15：内部错误不回显细节
        handler.send_json({'error': '服务器内部错误'}, 500)


def users_quota(handler, set_user_quota):
    """设置用户配额"""
    try:
        length = int(handler.headers.get('Content-Length', 0))
        data = json.loads(handler.rfile.read(length).decode())
        if not isinstance(data, dict):
            data = {}
        username = data.get('username', '').strip()
        quota_mb = int(data.get('quota_mb', 0))
        if not username:
            handler.send_json({'success': False, 'error': '参数错误'}, 400); return
        caller = handler._get_current_caller_role()
        ok, err = set_user_quota(username, quota_mb * 1048576, caller)
        if ok:
            handler.send_json({'success': True})
        else:
            handler.send_json({'success': False, 'error': err}, 400)
    except json.JSONDecodeError:
        # B-15：畸形请求体不回显内部解析错误
        handler.send_json({'error': '请求体必须是合法 JSON'}, 400)
    except Exception:
        # B-15：内部错误不回显细节
        handler.send_json({'error': '服务器内部错误'}, 500)


def users_rename(handler, update_user_name, add_log, UPLOAD_DIR):
    """重命名用户（IC-USER 事务顺序：先校验 → 先搬目录 → 再更新用户表/撤销旧会话）"""
    # 与 users_add/users_password 一致：管理操作前置角色门槛
    if handler._get_current_caller_role() not in ('admin', 'super_admin'):
        handler.send_json({'error': 'Forbidden'}, 403); return
    try:
        import leaffs.auth.core as _ac
        length = int(handler.headers.get('Content-Length', 0))
        data = json.loads(handler.rfile.read(length).decode())
        if not isinstance(data, dict):
            data = {}
        old_name = data.get('old_name', '').strip()
        new_name = data.get('new_name', '').strip()
        if not old_name or not new_name:
            handler.send_json({'success': False, 'error': '参数错误'}, 400); return
        caller = handler._get_current_caller_role()
        # 1) 前置校验（不落盘）：角色/新名白名单/存在性/重名
        ok, err = _ac.precheck_user_rename(old_name, new_name, caller)
        if not ok:
            handler.send_json({'success': False, 'error': err}, 400); return
        # 2) 先搬目录（成功才允许提交记录；失败则用户表不变）
        old_dir = os.path.join(UPLOAD_DIR, 'users', old_name)
        new_dir = os.path.join(UPLOAD_DIR, 'users', new_name)
        moved = False
        if os.path.exists(old_dir):
            if os.path.exists(new_dir):
                handler.send_json({'success': False, 'error': '目标用户目录已存在'}, 400); return
            try:
                os.rename(old_dir, new_dir)
                moved = True
                # 目录树已变化：失效 folder 大小与服务器统计缓存（变更即刷新）
                from leaffs.utils.core import invalidate_folder_cache
                invalidate_folder_cache(os.path.join(UPLOAD_DIR, 'users'), recursive=True)
            except Exception as e:
                from leaffs.runtime_log import log_exception
                log_exception('用户目录改名', e)
                handler.send_json({'success': False, 'error': '用户目录改名失败'}, 500); return
        # 3) 提交记录 + 撤销旧用户名全部会话（B-10：改名后需重新登录）
        ok, err = update_user_name(old_name, new_name, caller)
        if not ok:
            if moved:
                try:
                    os.rename(new_dir, old_dir)   # 尽力回滚目录（用户表未变）
                except Exception:
                    pass
            # IC-USER：中途失败整体报 500 且用户表不变（目录已搬则回滚搬回）
            handler.send_json({'success': False, 'error': err or '改名失败'}, 500); return
        try:
            add_log(f'用户改名: {old_name} -> {new_name} ({handler.client_address[0]})', 'warn')
        except Exception:
            pass
        handler.send_json({'success': True})
    except json.JSONDecodeError:
        # B-15：畸形请求体不回显内部解析错误
        handler.send_json({'error': '请求体必须是合法 JSON'}, 400)
    except Exception:
        # B-15：内部错误不回显细节
        handler.send_json({'error': '服务器内部错误'}, 500)


def users_speed(handler, set_user_speed_limit):
    """设置用户限速"""
    try:
        length = int(handler.headers.get('Content-Length', 0))
        data = json.loads(handler.rfile.read(length).decode())
        if not isinstance(data, dict):
            data = {}
        username = data.get('username', '').strip()
        speed_kb = int(data.get('speed_kb', 0))
        if not username:
            handler.send_json({'success': False, 'error': '参数错误'}, 400); return
        caller = handler._get_current_caller_role()
        ok, err = set_user_speed_limit(username, speed_kb * 1024, caller)
        if ok:
            handler.send_json({'success': True})
        else:
            handler.send_json({'success': False, 'error': err}, 400)
    except json.JSONDecodeError:
        # B-15：畸形请求体不回显内部解析错误
        handler.send_json({'error': '请求体必须是合法 JSON'}, 400)
    except Exception:
        # B-15：内部错误不回显细节
        handler.send_json({'error': '服务器内部错误'}, 500)