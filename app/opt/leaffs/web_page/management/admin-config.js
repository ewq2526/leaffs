// ========== 配置管理（zh/en 动态文案随页面注入的 window.__LANG__） ==========

/**
 * 读输入框里的数值（支持小数）；空 / 非数字 / 负数返回 null。
 *
 * 这里曾经用 `parseInt(x) || 默认值`：parseInt('0.001') 得 0，再被兜底成默认值 ——
 * 用户填 0.001（GB，约 1MB）想设个小配额，结果原样存回 5GB，界面还提示"已更新"。
 * 也刻意不再兜底：静默替用户填一个数比报错更糟，非法值交给调用方提示失败并恢复原值。
 */
function numValue(id) {
    var el = document.getElementById(id);
    var v = parseFloat(el ? el.value : '');
    return isNaN(v) || v < 0 ? null : v;
}

/** 非法输入时的统一处理：提示失败 + 把输入框恢复成当前生效值 */
function invalidNumber(id, msg) {
    batchToast(msg, 'error');
    updateConfigInputs();
    var el = document.getElementById(id);
    if (el) el.focus();
}

// 游客模式切换
function toggleAuth() {
    var btn = document.getElementById('authToggleBtn');
    if (btn) btn.disabled = true;
    // 目标状态基于服务端已确认状态取反；未知时发空请求由服务端自行取反
    var body = (_guestModeKnown === null) ? '{}' : JSON.stringify({ enabled: !_guestModeKnown });
    fetch('/api/auth/toggle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body
    }).then(function(r) { return r.json(); }).then(function(d) {
        if (d.success) {
            setAuthState(d.guest_mode);
            batchToast(_UI_EN
                ? ('Guest mode ' + (d.guest_mode ? 'enabled' : 'disabled'))
                : ('游客模式已' + (d.guest_mode ? '开启' : '关闭')), 'success');
        } else {
            batchToast(_UI_EN
                ? ('Toggle failed: ' + (d.error || 'unknown error'))
                : ('切换失败: ' + (d.error || '未知错误')), 'error');
        }
    }).catch(function() {
        batchToast(_UI_EN ? 'Network error, toggle failed' : '网络错误，切换失败', 'error');
    }).then(function() {
        if (btn) btn.disabled = false;
    });
}

// 限速设置
function applySpeed() {
    var val = numValue('speedInput');
    if (val === null) {
        invalidNumber('speedInput', _UI_EN
            ? 'Please enter a valid number (decimals such as 0.5 are fine)'
            : '请输入有效数字（可填小数，如 0.5）');
        return;
    }
    fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ download_speed_limit: Math.round(val * 1024) })
    }).then(function(r) { return r.json(); }).then(function(d) {
        if (d.success) {
            batchToast(_UI_EN ? ('Speed limit updated: ' + val + ' KB/s') : ('限速已更新: ' + val + ' KB/s'), 'success');
            updateConfigInputs();
            refreshAll();
        } else {
            batchToast(_UI_EN ? 'Speed limit update failed' : '限速更新失败', 'error');
        }
    }).catch(function() { batchToast(_UI_EN ? 'Speed limit update failed' : '限速更新失败', 'error'); });
}

// 配额设置
function applyQuotas() {
    var uq = numValue('userQuotaInput');
    var pq = numValue('publicQuotaInput');
    var tq = numValue('totalQuotaInput');
    if (uq === null || pq === null || tq === null) {
        invalidNumber('userQuotaInput', _UI_EN
            ? 'Please enter valid numbers (decimals such as 0.001 are fine)'
            : '请输入有效数字（可填小数，如 0.001 约等于 1MB）');
        return;
    }
    fetch('/api/config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            user_quota: Math.round(uq * 1073741824),
            public_quota: Math.round(pq * 1073741824),
            total_quota: Math.round(tq * 1073741824)
        })
    }).then(function(r) { return r.json(); }).then(function(d) {
        if (d.success) {
            batchToast(_UI_EN ? 'Quotas updated' : '配额已更新', 'success');
            updateConfigInputs();
            refreshAll();
        } else {
            batchToast(_UI_EN ? 'Quota update failed' : '配额更新失败', 'error');
        }
    }).catch(function() { batchToast(_UI_EN ? 'Quota update failed' : '配额更新失败', 'error'); });
}

// 每台设备 WebSocket 连接上限（ws_max_conn_per_ip）
function applyWsConnPerIp() {
    var el = document.getElementById('wsConnPerIpInput');
    var val = parseInt(el ? el.value : '', 10);
    if (isNaN(val) || val < 1 || val > 256) {
        batchToast(_UI_EN ? 'WebSocket connections per device must be an integer 1~256' : 'WebSocket 连接数需为 1~256 的整数', 'error');
        return;
    }
    fetch('/api/config/deep', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ws_max_conn_per_ip: val })
    }).then(function(r) { return r.json(); }).then(function(d) {
        if (d && d.success) {
            batchToast(_UI_EN
                ? ('WebSocket connections per device saved as ' + val + ' (applied immediately)')
                : ('每台设备 WebSocket 连接上限已保存为 ' + val + '（立即生效）'), 'success');
            updateConfigInputs();
            refreshAll();
        } else {
            batchToast((d && d.error) || (_UI_EN ? 'Save failed' : '保存失败'), 'error');
        }
    }).catch(function() { batchToast(_UI_EN ? 'Request failed, check your network' : '请求失败，请检查网络', 'error'); });
}

// 每台设备（同一来源 IP）连接上限（max_conn_per_ip）
function applyConnPerIp() {
    var el = document.getElementById('connPerIpInput');
    var val = parseInt(el ? el.value : '', 10);
    if (isNaN(val) || val < 1 || val > 1000) {
        batchToast(_UI_EN ? 'Connections per device must be an integer 1~1000' : '每台设备连接数需为 1~1000 的整数', 'error');
        return;
    }
    fetch('/api/config/deep', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ max_conn_per_ip: val })
    }).then(function(r) { return r.json(); }).then(function(d) {
        if (d && d.success) {
            batchToast(_UI_EN
                ? ('Connections per device saved as ' + val + ' (applied immediately)')
                : ('每台设备连接上限已保存为 ' + val + '（立即生效）'), 'success');
            updateConfigInputs();
            refreshAll();
        } else {
            batchToast((d && d.error) || (_UI_EN ? 'Save failed' : '保存失败'), 'error');
        }
    }).catch(function() { batchToast(_UI_EN ? 'Request failed, check your network' : '请求失败，请检查网络', 'error'); });
}

// 整机“同时处理连接数上限”（max_total_conns）
function applyConnLimit() {
    var el = document.getElementById('connLimitInput');
    var val = parseInt(el ? el.value : '', 10);
    if (isNaN(val) || val < 8 || val > 20000) {
        batchToast(_UI_EN ? 'Concurrent connections must be an integer 8~20000' : '同时服务连接数需为 8~20000 的整数', 'error');
        return;
    }
    fetch('/api/config/deep', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ max_total_conns: val })
    }).then(function(r) { return r.json(); }).then(function(d) {
        if (d && d.success) {
            batchToast(_UI_EN
                ? ('Concurrent connections limit saved as ' + val + ' (applied immediately)')
                : ('同时服务连接上限已保存为 ' + val + '（立即生效）'), 'success');
            updateConfigInputs();
            refreshAll();
        } else {
            batchToast((d && d.error) || (_UI_EN ? 'Save failed' : '保存失败'), 'error');
        }
    }).catch(function() { batchToast(_UI_EN ? 'Request failed, check your network' : '请求失败，请检查网络', 'error'); });
}
