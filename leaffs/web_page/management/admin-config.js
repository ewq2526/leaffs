// ========== 配置管理 ==========

// 游客模式切换
function toggleAuth() {
    var btn = document.getElementById('authToggleBtn');
    if (btn) btn.disabled = true;
    // 目标状态基于服务端已确认状态取反；未知时发空请求由服务端自行取反，
    // 不依赖界面文本猜测，避免游客模式“关了开不回来”
    var body = (_guestModeKnown === null) ? '{}' : JSON.stringify({ enabled: !_guestModeKnown });
    fetch('/api/auth/toggle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body
    }).then(function(r) { return r.json(); }).then(function(d) {
        if (d.success) {
            setAuthState(d.guest_mode);
            batchToast('游客模式已' + (d.guest_mode ? '开启' : '关闭'), 'success');
        } else {
            batchToast('切换失败: ' + (d.error || '未知错误'), 'error');
        }
    }).catch(function() {
        batchToast('网络错误，切换失败', 'error');
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
            batchToast('限速已更新: ' + val + ' KB/s', 'success');
            updateConfigInputs();
            refreshAll();
        } else {
            batchToast('限速更新失败', 'error');
        }
    }).catch(function() { batchToast('限速更新失败', 'error'); });
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
            batchToast('配额已更新', 'success');
            updateConfigInputs();
            refreshAll();
        } else {
            batchToast('配额更新失败', 'error');
        }
    }).catch(function() { batchToast('配额更新失败', 'error'); });
}

// 每台设备 WebSocket 连接上限（ws_max_conn_per_ip，默认 8）：与 HTTP 上限同卡保存、立即生效
function applyWsConnPerIp() {
    var el = document.getElementById('wsConnPerIpInput');
    var val = parseInt(el ? el.value : '', 10);
    if (isNaN(val) || val < 1 || val > 256) {
        batchToast('WebSocket 连接数需为 1~256 的整数', 'error');
        return;
    }
    fetch('/api/config/deep', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ws_max_conn_per_ip: val })
    }).then(function(r) { return r.json(); }).then(function(d) {
        if (d && d.success) {
            batchToast('每台设备 WebSocket 连接上限已保存为 ' + val + '（立即生效）', 'success');
            updateConfigInputs();
            refreshAll();
        } else {
            batchToast((d && d.error) || '保存失败', 'error');
        }
    }).catch(function() { batchToast('请求失败，请检查网络', 'error'); });
}

// 每台设备（同一来源 IP）连接上限（max_conn_per_ip，默认 20）：与整机上限同卡保存、立即生效
function applyConnPerIp() {
    var el = document.getElementById('connPerIpInput');
    var val = parseInt(el ? el.value : '', 10);
    if (isNaN(val) || val < 1 || val > 1000) {
        batchToast('每台设备连接数需为 1~1000 的整数', 'error');
        return;
    }
    fetch('/api/config/deep', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ max_conn_per_ip: val })
    }).then(function(r) { return r.json(); }).then(function(d) {
        if (d && d.success) {
            batchToast('每台设备连接上限已保存为 ' + val + '（立即生效）', 'success');
            updateConfigInputs();
            refreshAll();
        } else {
            batchToast((d && d.error) || '保存失败', 'error');
        }
    }).catch(function() { batchToast('请求失败，请检查网络', 'error'); });
}

// 整机“同时处理连接数上限”（max_total_conns，默认 256）：管理首页直接调整、立即生效。
// 空闲断开（io_idle_timeout_secs）仍在深度配置。
function applyConnLimit() {
    var el = document.getElementById('connLimitInput');
    var val = parseInt(el ? el.value : '', 10);
    if (isNaN(val) || val < 1 || val > 20000) {
        batchToast('同时服务连接数需为 1~20000 的整数', 'error');
        return;
    }
    fetch('/api/config/deep', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ max_total_conns: val })
    }).then(function(r) { return r.json(); }).then(function(d) {
        if (d && d.success) {
            batchToast('同时服务连接上限已保存为 ' + val + '（立即生效）', 'success');
            updateConfigInputs();
            refreshAll();
        } else {
            batchToast((d && d.error) || '保存失败', 'error');
        }
    }).catch(function() { batchToast('请求失败，请检查网络', 'error'); });
}
