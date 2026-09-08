// ========== 配置管理（zh/en 动态文案随页面注入的 window.__LANG__） ==========

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
    var val = parseInt(document.getElementById('speedInput').value) || 0;
    fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ download_speed_limit: val * 1024 })
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
    var uq = parseInt(document.getElementById('userQuotaInput').value) || 5;
    var pq = parseInt(document.getElementById('publicQuotaInput').value) || 5;
    var tq = parseInt(document.getElementById('totalQuotaInput').value) || 50;
    fetch('/api/config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            user_quota: uq * 1073741824,
            public_quota: pq * 1073741824,
            total_quota: tq * 1073741824
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
    if (isNaN(val) || val < 1 || val > 20000) {
        batchToast(_UI_EN ? 'Concurrent connections must be an integer 1~20000' : '同时服务连接数需为 1~20000 的整数', 'error');
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
