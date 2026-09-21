"""用户管理 API — 增删改查/角色/密码/限速/配额/重命名"""

import json
import os
import shutil
import time


ARCHIVE_DIRNAME = '.deleted'


def _archive_root():
    """被删用户家目录的归档根：`users/.deleted/`"""
    from leaffs.utils.core import UPLOAD_DIR
    return os.path.join(UPLOAD_DIR, 'users', ARCHIVE_DIRNAME)


def _archive_entry(name):
    """把归档名解析成归档根下的绝对路径；名字不合法返回 None。

    只接受**单层目录名**（删用户时生成的目录名就是 `用户名-时间戳[-序号]`，没有层级）。
    basename 比对是最省事也最严的判据：`../x`、`a/b`、`a\\b` 全在这里被挡掉，
    不可能借清理入口删到归档目录外面。
    """
    if not isinstance(name, str) or not name or name in ('.', '..'):
        return None
    if os.path.basename(name) != name:
        return None
    return os.path.join(_archive_root(), name)


def users_archive_list(handler):
    """列出已删除用户的归档目录（名字 / 大小 / 文件数 / 时间）。

    N-7（2026-09-21 黑盒报告）：删用户只归档不清理，数据留在 `users/.deleted/` 里
    只增不减。归档本身是**有意**的设计（删账号是管理员一个操作，删目录却会永久毁掉
    那个用户的全部文件），缺的是一个能看见、能清理的入口 —— 在这之前管理员只能自己
    摸到浏览页里输入 `users/.deleted` 去删。
    """
    if handler._get_effective_role() not in ('admin', 'super_admin'):
        handler.send_json({'error': 'Forbidden'}, 403); return
    from leaffs.utils.core import get_folder_stats
    root = _archive_root()
    items = []
    total = 0
    try:
        if os.path.isdir(root):
            for name in os.listdir(root):
                p = os.path.join(root, name)
                if not os.path.isdir(p):
                    continue
                st = get_folder_stats(p) or {}
                size = int(st.get('size', 0) or 0)
                items.append({'name': name, 'size': size,
                              'files': int(st.get('files', 0) or 0),
                              'mtime': os.path.getmtime(p)})
                total += size
    except Exception as e:
        from leaffs.runtime_log import log_exception
        log_exception('列出已删除用户的归档', e)
        handler.send_json({'error': '读取归档目录失败'}, 500); return
    items.sort(key=lambda x: x['mtime'], reverse=True)
    handler.send_json({'archives': items, 'total_size': total})


def users_archive_delete(handler):
    """删除归档目录 —— **真删，不可恢复**。

    body：`{"names": ["<归档名>", ...]}` 或 `{"all": true}`。
    "不可恢复"在界面层用两次 confirm 交代，服务端只管如实执行并逐项回报结果
    （与 HTTP 删除同一口径：有失败项就列出来，部分成功仍算成功）。
    """
    if handler._get_effective_role() not in ('admin', 'super_admin'):
        handler.send_json({'error': 'Forbidden'}, 403); return
    try:
        from leaffs.utils.core import invalidate_folder_cache, delete_fail_reason
    except Exception:
        handler.send_json({'error': '服务器内部错误'}, 500); return
    try:
        length = int(handler.headers.get('Content-Length', 0))
        data = json.loads(handler.rfile.read(length).decode())
        if not isinstance(data, dict):
            data = {}
        root = _archive_root()
        if data.get('all'):
            names = []
            if os.path.isdir(root):
                names = [n for n in os.listdir(root)
                         if os.path.isdir(os.path.join(root, n))]
        else:
            names = data.get('names')
            if not isinstance(names, list) or not names:
                handler.send_json({'success': False, 'error': '缺少参数 names'}, 400); return
        deleted = 0
        failed = []          # [(name, reason)]
        for name in names:
            p = _archive_entry(name)
            if p is None:
                failed.append((str(name), '名字不合法'))
                continue
            if not os.path.isdir(p):
                failed.append((str(name), '归档不存在'))
                continue
            try:
                shutil.rmtree(p)
                # 归档目录已消失：连同它的祖先聚合一起失效（挂在这里天然覆盖全部
                # 调用路径，逐个删除点打点必然会漏，见 invalidate_folder_cache 注释）
                invalidate_folder_cache(p, recursive=True)
                deleted += 1
            except Exception as e:
                from leaffs.runtime_log import log_exception
                log_exception('删除用户归档 %s' % name, e)
                failed.append((str(name), delete_fail_reason(e)))
        # 审计：这是**不可恢复**的操作，必须留下"谁在什么时候删掉了哪些归档"。
        # 原来只靠访问日志那行 `POST /api/users/archive/delete 200` —— 那里看不出
        # 删的是什么、也看不出是谁（访问日志 2026-09-21 才补上账户，此条同时受益）。
        if deleted:
            try:
                from leaffs.runtime_log import add_log
                _role, _uname = handler._session_identity()
                add_log('清理用户归档: 删除 %d 个（不可恢复）[%s(%s) ip=%s]%s'
                        % (deleted, _uname or '-', _role or '-',
                           handler.client_address[0],
                           '，其中 %d 个失败' % len(failed) if failed else ''), 'warn')
            except Exception:
                pass
        resp = {'success': not failed, 'deleted': deleted}
        if failed:
            resp['failed'] = [{'name': a, 'error': b} for a, b in failed]
        # 与 HTTP 删除同一口径：删到了就是 200（失败项在 failed 里说明），
        # 一个都没删且确有失败才是 400
        handler.send_json(resp, 200 if (deleted or not failed) else 400)
    except json.JSONDecodeError:
        handler.send_json({'error': '请求体必须是合法 JSON'}, 400)
    except Exception:
        handler.send_json({'error': '服务器内部错误'}, 500)


def _int_param(data, key):
    """取一个**必须存在**的整数参数；缺失或转不成整数时抛 `ValueError`（调用方回 400）。

    原来这两个接口写的是 `int(data.get('quota_mb', 0))` / `int(data.get('speed_kb', 0))`：
    字段名打错或者漏传时**静默变成 0**，而 0 在这两个接口里是**合法值**
    （= 不限额 / 不限速）—— 于是"参数根本没被用上"被当成"设置成功"回了 `success: true`。

    外部黑盒报告 N-10（2026-09-21）正是这么中招的：他们发的是 `{"quota": 100}`，
    接口读的是 `quota_mb`，于是配额被改成了**不限**，而响应是成功 ——
    报告里的结论一度是"这个端点改不动配额"，实际是它每次都把配额清成了 0。

    缺失与非法一律明确拒绝：宁可让调用方看到"参数不对"，也不能让它以为设置生效了。
    """
    if key not in data:
        raise ValueError('缺少参数 %s' % key)
    try:
        return int(data[key])
    except (TypeError, ValueError):
        raise ValueError('%s 必须是整数' % key)


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
        if not username:
            handler.send_json({'success': False, 'error': '参数错误'}, 400); return
        try:
            quota_mb = _int_param(data, 'quota_mb')
        except ValueError as e:
            handler.send_json({'success': False, 'error': str(e)}, 400); return
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
            _r, _u = handler._session_identity()
            add_log(f'用户改名: {old_name} -> {new_name}'
                    f' [{_u or "-"}({_r or "-"}) ip={handler.client_address[0]}]', 'warn')
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
        if not username:
            handler.send_json({'success': False, 'error': '参数错误'}, 400); return
        try:
            speed_kb = _int_param(data, 'speed_kb')
        except ValueError as e:
            handler.send_json({'success': False, 'error': str(e)}, 400); return
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