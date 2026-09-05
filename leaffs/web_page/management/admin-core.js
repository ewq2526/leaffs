// ========== 核心变量 ==========
var startTime = Date.now(); // 页面打开时间（仅后端未返回 uptime 时兜底）
// 服务端最近一次确认的游客模式状态（null = 未知；切换按钮以此取反，避免状态不同步）
var _guestModeKnown = null;

// ========== WS 诊断插桩（仅 console 输出，不改任何行为；URL 带 ?wsdebug 开启）==========
if (/\bwsdebug\b/.test(window.location.search)) {
    window.__WS_DEBUG__ = true;
    console.log('[WS] 诊断模式开启（?wsdebug）');
}
// 给当前 WebSocket 对象绑定诊断监听（重复绑定同一对象幂等；重连产生新对象时重新调用）
function _wsDebugBind(sock) {
    if (!sock || sock._wsDbgBound) return;
    sock._wsDbgBound = true;
    if (window.__WS_DEBUG__) console.log('[WS] url=', sock.url);
    sock.addEventListener('message', function(ev) {
        if (!window.__WS_DEBUG__) return;
        try {
            var m = JSON.parse(ev.data);
            console.log('[WS]', m.type || '(raw)', m);
        } catch (err) {
            console.log('[WS]', '(unparsed)', ev.data);
        }
    });
    sock.addEventListener('close', function(ev) {
        if (window.__WS_DEBUG__) console.warn('[WS] close code=', ev.code, 'reason=', ev.reason || '');
    });
    sock.addEventListener('error', function(ev) {
        if (window.__WS_DEBUG__) console.warn('[WS] error', (ev && ev.message) || '(无明细，请看 Network 面板)');
    });
}

// ========== WS 显式 sid 认证 + 管理订阅（共享助手见 common/app.js: wsAdminAuthAndSubscribe）==========
// 本页面对当前 WS 连接是否已成功发出 admin-sub。false 期间即使 wsConnected==true
// 也允许 HTTP 兜底轮询，避免“连接了但没订阅/收不到推送”时页面永久空转。
var _wsAdminReady = false;
// 当前连接已发出的 auth sid（诊断/匹配用）
var _wsAuthSid = '';
// WS 管理数据是否可用：连接打开 且 已对本连接成功发出 admin-sub
function _wsAdminUsable() {
    return !!(wsConnected && _wsAdminReady);
}
// 共享助手完成（或失败）订阅时回调本页：同步 HTTP 兜底门控
window._onWSAdminSubscribed = function (ok) {
    _wsAdminReady = !!ok;
};

function setAuthState(on) {
    var el = document.getElementById('authState');
    if (!el) return;
    _guestModeKnown = !!on;
    el.textContent = on ? '开启中' : '已关闭';
    el.className = 'auth-state ' + (on ? 'enabled' : 'disabled');
}

// 服务器运行时长格式化（秒 → 天/时/分/秒）
function fmtUptime(secs) {
    secs = Math.max(0, Math.floor(secs));
    var d = Math.floor(secs / 86400), h = Math.floor(secs % 86400 / 3600);
    var m = Math.floor(secs % 3600 / 60), s = secs % 60;
    if (d > 0) return '已运行 ' + d + ' 天 ' + h + ' 小时 ' + m + ' 分';
    if (h > 0) return '已运行 ' + h + ' 小时 ' + m + ' 分';
    return '已运行 ' + m + ' 分 ' + s + ' 秒';
}

// 相对时间（秒 → 天/小时/分钟/秒前）
function fmtAgo(secs) {
    secs = Math.max(0, Math.floor(secs));
    if (secs >= 86400) return Math.floor(secs / 86400) + ' 天前';
    if (secs >= 3600) return Math.floor(secs / 3600) + ' 小时前';
    if (secs >= 60) return Math.floor(secs / 60) + ' 分钟前';
    return secs + ' 秒前';
}
// myRole 由 admin.html 的内联脚本注入 __MY_ROLE__ 占位符（服务端替换），此处不再声明

// ========== 日志 ==========
// 日志统一由服务端 add_log 记录（启动/操作），页面用 /api/logs 渲染预览，
// 不再维护前端自己的日志；完整日志见 /log 页面
var LOG_PREVIEW_MAX = 80; // 管理页预览条数

function renderServerLogs(logs) {
    var area = document.getElementById('logArea');
    if (!area) return;
    logs = logs || [];
    if (!logs.length) { area.innerHTML = '<div style="color:var(--text-light);font-size:11px">暂无日志</div>'; return; }
    var h = '';
    var start = Math.max(0, logs.length - LOG_PREVIEW_MAX);
    // 最新在前（logs 为时间正序，倒序输出后第一条即最新），并停在顶部让最新可见
    for (var i = logs.length - 1; i >= start; i--) {
        var l = logs[i];
        h += '<div class="lv"><span class="t">' + esc(l.time) + '</span><span class="m ' + esc(l.level || 'info') + '">' + esc(l.msg) + '</span></div>';
    }
    area.innerHTML = h;
    area.scrollTop = 0;
}

function loadServerLogs() {
    fetch('/api/logs').then(function(r) { return r.json(); }).then(function(d) { renderServerLogs(d.logs); }).catch(function() {});
}

// ========== 日志卡与连接配置卡等高 ==========
// 量“连接数上限（HTTP）”卡的实际高度，等值赋给“服务器日志”卡 → 日志卡高度由 HTTP 卡决定，
// 日志条数不再参与撑高；日志区在卡内滚动。窗口尺寸变化时自动重算。
var _alignLogCardBound = false;
function alignLogCard() {
    var httpCard = document.getElementById('connConfigCard');
    var logCard = document.getElementById('logCard');
    if (!httpCard || !logCard) return;
    logCard.style.height = httpCard.offsetHeight + 'px';
    if (!_alignLogCardBound) {
        _alignLogCardBound = true;
        var timer = null;
        window.addEventListener('resize', function() {
            if (timer) return;
            timer = setTimeout(function() { timer = null; alignLogCard(); }, 150);
        });
    }
}

// ========== 证书（自签 CA / 自动信任机制已移除；服务器证书改为自动管理）==========
// 自签 CA / 自动信任 / 证书接入引导已整体移除（分发安全策略）。服务器证书自动管理：
// 未配置 tls_cert/tls_key 时首次启动自动生成随机自签证书（config/selfsigned.crt，
// 非 CA、不装信任库），已配置则以配置为准；不提供手动“重置”。后端 /api/certs/reset
// 恒返回说明文案。本函数保留仅为兼容旧调用，直接透传服务端文案。
function resetCerts() {
    fetch('/api/certs/reset', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
    }).then(function(r) { return r.json(); }).then(function(d) {
        if (d && d.success) batchToast(d.message || '操作成功', 'success');
        else batchToast((d && d.error) || '重置失败', 'error');
    }).catch(function() { batchToast('请求失败', 'error'); });
}

// ========== 配置输入框专用刷新（从 /api/config 与 /api/config/deep 获取，编辑时跳过）=========
function updateConfigInputs() {
    fetch('/api/config')
        .then(function(r) { return r.json(); })
        .then(function(cfg) {
            _safeSetValue('speedInput', cfg.download_speed_limit > 0 ? Math.round(cfg.download_speed_limit / 1024) : 0);
            _safeSetValue('userQuotaInput', Math.round((cfg.user_quota || 5368709120) / 1073741824));
            _safeSetValue('publicQuotaInput', Math.round((cfg.public_quota || 5368709120) / 1073741824));
            _safeSetValue('totalQuotaInput', Math.round((cfg.total_quota || 53687091200) / 1073741824));
        });
    // 整机/单 IP 连接上限：以深度配置为准（max_total_conns / max_conn_per_ip）
    fetch('/api/config/deep')
        .then(function(r) { return r.json(); })
        .then(function(dd) {
            if (!dd || typeof dd !== 'object' || dd.error) return;
            if (typeof dd.max_total_conns === 'number') _safeSetValue('connLimitInput', dd.max_total_conns);
            if (typeof dd.max_conn_per_ip === 'number') _safeSetValue('connPerIpInput', dd.max_conn_per_ip);
            if (typeof dd.ws_max_conn_per_ip === 'number') _safeSetValue('wsConnPerIpInput', dd.ws_max_conn_per_ip);
        })
        .catch(function() {});
}

function _safeSetValue(id, val) {
    var el = document.getElementById(id);
    if (el && document.activeElement !== el) {
        el.value = val;
    }
}

// ========== 输入感知的定时刷新 ==========
var _refreshPaused = false;
var _refreshTimer = null;
var REFRESH_INTERVAL = 5000; // 5秒刷新一次，避免频繁干扰

function _shouldSkipRefresh() {
    var el = document.activeElement;
    if (!el) return false;
    var tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
    if (el.isContentEditable) return true;
    return false;
}

function applyStats(d) {
    if (!d) return;
    // 运行时间以后端返回的服务器运行时长为准（页面刷新/重开不会归零）
    var uptime = (typeof d.uptime === 'number' && d.uptime >= 0)
        ? d.uptime : Math.floor((Date.now() - startTime) / 1000);
    document.getElementById('startTime').textContent = fmtUptime(uptime);
    document.getElementById('localIp').textContent = d.server_ip || window.location.hostname;
    document.getElementById('fileTotal').textContent = d.file_count + ' 个文件, ' + d.folder_count + ' 个文件夹';
    document.getElementById('shareTotalSize').textContent = formatSize(d.total_size);
    document.getElementById('ffmpegStatus').textContent = d.ffmpeg ? '可用' : '未安装';
    document.getElementById('aria2cStatus').textContent = d.aria2c ? '可用' : '未安装';
    document.getElementById('connCount').textContent = d.active_connections + ' 个在线';
    document.getElementById('userQuotaInfo').textContent = formatSize(d.users_used || 0) + '  / ' + formatSize(d.user_quota || 5368709120);
    document.getElementById('publicQuotaInfo').textContent = formatSize(d.public_used || 0) + '  / ' + formatSize(d.public_quota || 5368709120);
    document.getElementById('totalQuotaInfo').textContent = formatSize(d.total_used || 0) + '  / ' + formatSize(d.total_quota || 53687091200);
    setAuthState(d.guest_mode);
}

// 定时刷新（HTTP 兜底）：WS 未连接、或已连接但尚未订阅成功（auth/admin-sub 未完成）
// 时由定时器调用；WS 订阅成功后数据由服务器每秒推送（见 handleWSMessage / onWSConnected）
function refreshAll() {
    // 用户正在输入时跳过本次刷新（仅刷新状态显示，不刷输入框）
    if (_refreshPaused || _shouldSkipRefresh()) return;
    fetch('/api/stats')
        .then(function(r) { return r.json(); })
        .then(applyStats);
    if (typeof refreshUsers === 'function') refreshUsers();
    refreshConnections();
}

function _onDemandRefresh() {
    // 交互触发的即时刷新：WS 推送可用（已连接且已订阅）时数据实时覆盖，否则走 HTTP
    if (_wsAdminUsable()) return;
    refreshAll();
}

function _onInputFocus() {
    _refreshPaused = true;
}

function _onInputBlur() {
    _refreshPaused = false;
    // 离开输入框后立即刷新一次（WS 已连接时推送会实时更新，无需额外 HTTP）
    _onDemandRefresh();
}

function _onVisibilityChange() {
    if (!document.hidden) {
        _refreshPaused = false;
        _onDemandRefresh();
    }
}

// 监听全局焦点事件：任何输入框获得焦点时暂停刷新
document.addEventListener('focusin', function(e) {
    var el = e.target;
    if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT')) {
        _onInputFocus();
    }
});
document.addEventListener('focusout', function(e) {
    var el = e.target;
    if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT')) {
        _onInputBlur();
    }
});

// 页面可见性变化时恢复刷新
document.addEventListener('visibilitychange', _onVisibilityChange);

// ========== 连接用户列表 ==========
function renderConnections(list) {
    list = list || [];
    var html = '';
    if (!list.length) {
        html = '<div style="color:var(--text-light);font-size:11px;padding:10px 0">暂无连接</div>';
    } else {
        // 表头
        html += '<div style="display:flex;align-items:center;gap:12px;padding:5px 2px;font-size:11px;color:var(--text-light);border-bottom:1px solid var(--border);line-height:1.6">';
        html += '<span style="min-width:80px;font-weight:600">用户名</span>';
        html += '<span style="min-width:80px;text-align:center;font-weight:600">身份</span>';
        html += '<span style="flex:1;text-align:center;font-weight:600">IP</span>';
        html += '<span style="min-width:60px;text-align:right;font-weight:600">最近一次活动</span>';
        html += '</div>';
        for (var i = 0; i < Math.min(list.length, 50); i++) {
            var c = list[i];
            var timeStr = fmtAgo(c.active_secs || 0);
            var user = c.username || '游客';
            var role = c.role || '';
            var roleColor = '';
            if (role === '超级管理员') { roleColor = 'var(--danger)'; }
            else if (role === '管理员') { roleColor = 'var(--primary)'; }
            else if (role === '用户') { roleColor = 'var(--success)'; }
            else { role = '游客'; roleColor = 'var(--muted)'; }
            html += '<div style="display:flex;align-items:center;gap:12px;padding:5px 2px;font-size:12px;border-bottom:1px solid var(--border);line-height:1.6">';
            html += '<span style="font-weight:600;color:var(--text);white-space:nowrap;min-width:80px">' + esc(user) + '</span>';
            html += '<span style="color:' + roleColor + ';font-weight:600;font-size:11px;min-width:80px;text-align:center">[' + role + ']</span>';
            html += '<span style="color:var(--text-light);flex:1;text-align:center;white-space:nowrap">' + esc(c.ip) + '</span>';
            html += '<span style="color:var(--text-light);min-width:60px;text-align:right;white-space:nowrap">' + timeStr + '</span>';
            html += '</div>';
        }
    }
    document.getElementById('connTableWrap').innerHTML = html;
}

// HTTP 兜底刷新连接列表（WS 断开或尚未订阅成功时使用）
function refreshConnections() {
    fetch('/api/connections')
        .then(function(r) { return r.json(); })
        .then(function(d) { renderConnections(d.connections); });
}

// ========== WebSocket 实时推送（服务器每秒推送一帧管理数据） ==========
function onWSConnected() {
    // 诊断插桩：绑定新连接对象的监听（含自动重连后产生的对象）
    _wsDebugBind(ws);
    // 新连接：重置订阅就绪状态，先经共享助手做显式 sid 认证并订阅管理推送
    // （auth 成功才发 admin-sub；sid 失败由助手 console.warn 并置 ready=false → HTTP 兜底）
    _wsAdminReady = false;
    _wsAuthSid = '';
    if (window.wsAdminAuthAndSubscribe) {
        window.wsAdminAuthAndSubscribe(ws);
    } else {
        sendWS({ type: 'admin-sub' }); // 兼容：助手缺失时回退原直发逻辑
    }
    // WS 重连后补发所有等待中二维码弹窗的订阅（qr-sub 无需认证，sid 即知识证明）
    for (var sid in _qrWindows) sendWS({ type: 'qr-sub', sid: sid });
}

// 等待扫码的二维码弹窗集合：sid -> 收到 qr_consumed 后的完成回调
var _qrWindows = {};

function handleWSMessage(msg) {
    if (!msg) return;
    // auth 回执由共享助手 wsAdminAuthAndSubscribe 的一次性监听处理（发送 admin-sub），
    // 此处只处理 qr_consumed 与 admin_data
    if (msg.type === 'qr_consumed' && msg.sid && _qrWindows[msg.sid]) {
        var cb = _qrWindows[msg.sid];
        delete _qrWindows[msg.sid];
        cb();
        return;
    }
    if (msg.type !== 'admin_data') return;
    if (msg.stats) applyStats(msg.stats);
    if (msg.connections) renderConnections(msg.connections);
}

// ========== 二维码生成 ==========
function genQR(name) {
    fetch('/api/qrcode?name=' + encodeURIComponent(name) + '&embed=1')
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (!d.qr_url) { batchToast('生成二维码失败', 'error'); return; }
            var sid = d.sid;
            var qrUrl = d.qr_url;

            var overlay = document.createElement('div');
            overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;z-index:99999;background:var(--modal-overlay);display:flex;align-items:center;justify-content:center';

            var card = document.createElement('div');
            card.style.cssText = 'background:var(--card-bg);border-radius:12px;padding:32px;max-width:360px;width:90%;box-shadow:var(--shadow-hover);text-align:center;position:relative';

            var closeBtn = document.createElement('button');
            closeBtn.innerHTML = '✕';
            closeBtn.style.cssText = 'position:absolute;top:8px;right:10px;width:28px;height:28px;border-radius:50%;border:none;background:var(--chip-bg);color:var(--muted);font-size:14px;cursor:pointer;display:flex;align-items:center;justify-content:center';
            closeBtn.onclick = function() { document.body.removeChild(overlay); };

            var title = document.createElement('div');
            title.style.cssText = 'font-size:16px;font-weight:700;color:var(--text);margin-bottom:4px';
            title.textContent = '扫码连接';

            var sub = document.createElement('div');
            sub.style.cssText = 'font-size:12px;color:var(--muted);margin-bottom:16px';
            sub.textContent = '扫描后自动登录（' + name + '）';

            var qrWrap = document.createElement('div');
            qrWrap.style.cssText = 'text-align:center;padding:12px 0';
            // 二维码外框（与游客卡片同款：描边+圆角+底色，暗色模式下配合白底黑格二维码）
            var qrFrame = document.createElement('div');
            qrFrame.id = 'embedQr';
            qrFrame.style.cssText = 'display:inline-block;border:2px solid var(--border);border-radius:10px;padding:8px;background:var(--card-bg);line-height:0';
            qrWrap.appendChild(qrFrame);

            var statusMsg = document.createElement('div');
            statusMsg.id = 'embedQrStatus';
            statusMsg.style.cssText = 'font-size:12px;font-weight:600;color:var(--success);margin-top:10px;display:none';
            statusMsg.textContent = '已扫码登录成功';

            var expiredMsg = document.createElement('div');
            expiredMsg.id = 'embedQrExpired';
            expiredMsg.style.cssText = 'font-size:12px;font-weight:600;color:var(--danger);margin-top:10px;display:none';
            expiredMsg.textContent = '二维码已失效';

            card.appendChild(closeBtn);
            card.appendChild(title);
            card.appendChild(sub);
            card.appendChild(qrWrap);
            card.appendChild(statusMsg);
            card.appendChild(expiredMsg);
            overlay.appendChild(card);
            document.body.appendChild(overlay);

            var qrContainer = document.getElementById('embedQr');
            qrContainer.innerHTML = '';
            var _qo = { text: qrUrl, width: 220, height: 220 };
            if (typeof qrThemeColors === 'function') { var _qc = qrThemeColors(); _qo.colorDark = _qc.colorDark; _qo.colorLight = _qc.colorLight; }
            var qrcode = new QRCode(qrContainer, _qo);
            var cvs = qrContainer.querySelector('canvas');
            var img = qrContainer.querySelector('img');
            if (cvs) cvs.style.display = 'none';
            if (img) { img.style.display = 'block'; img.style.margin = '0 auto'; }

            // 等待服务器 WS 推送 qr_consumed（扫码即时完成，无需轮询）
            var finished = false;
            function finishQRConsumed() {
                if (finished) return; finished = true;
                qrWrap.style.display = 'none';
                expiredMsg.style.display = 'none';
                statusMsg.style.display = 'block';
                setTimeout(function() {
                    if (document.body.contains(overlay)) document.body.removeChild(overlay);
                }, 2000);
            }
            _qrWindows[sid] = finishQRConsumed;
            if (ws && ws.readyState === WebSocket.OPEN) sendWS({ type: 'qr-sub', sid: sid });
            // 兜底：二维码有效期 2 分钟，超时未扫码则显示失效
            var qrExpireTimer = setTimeout(function() {
                if (finished) return; finished = true;
                delete _qrWindows[sid];
                qrWrap.style.display = 'none';
                statusMsg.style.display = 'none';
                expiredMsg.style.display = 'block';
            }, 120000);
            function qrClose() {
                if (!finished) {
                    finished = true;
                    clearTimeout(qrExpireTimer);
                    delete _qrWindows[sid];
                    sendWS({ type: 'qr-unsub', sid: sid });
                }
                if (document.body.contains(overlay)) document.body.removeChild(overlay);
            }
            closeBtn.onclick = qrClose;
            overlay.onclick = function(e) {
                if (e.target === overlay) qrClose();
            };
        });
}

// ========== 游客卡片二维码 ==========
// HTTPS 开启时：二维码指向“证书提示页”（8082，纯 http、无证书警告）——访客扫码先看到
// 大白话说明，点按钮进 https 登录页；已登录的浏览器带 LOGIN_MARKER 会自动 302 回主站。
// HTTPS 关闭时：直接给出主站地址（无证书问题，无需提示页）。
function initQR() {
    var container = document.getElementById('qrContainer');
    var urlEl = document.getElementById('qrUrl');
    if (!container || !urlEl) return;
    var capEl = document.getElementById('qrCaption');
    function setCap(text) { if (capEl) capEl.textContent = text; }
    function draw(url, cap) {
        urlEl.textContent = url;
        container.innerHTML = '';
        var _qg = { text: url, width: 105, height: 105 };
        if (typeof qrThemeColors === 'function') { var _qgc = qrThemeColors(); _qg.colorDark = _qgc.colorDark; _qg.colorLight = _qgc.colorLight; }
        new QRCode(container, _qg);
        setCap(cap || '手机扫码访问文件服务');
    }
    Promise.all([
        fetch('/api/stats').then(function(r) { return r.json(); }).catch(function() { return {}; }),
        fetch('/api/config/advanced').then(function(r) { return r.json(); }).catch(function() { return {}; })
    ]).then(function(res) {
        var st = res[0] || {};
        var ad = res[1] || {};
        var serverIp = st.server_ip || window.location.hostname;
        var url, cap;
        if (ad.tls_enabled) {
            var tp = ad.tls_trust_port || 8082;
            url = 'http://' + serverIp + (String(tp) === '80' ? '' : ':' + tp);
            cap = 'HTTPS 已开启：扫码先到证书提示页（http:' + (String(tp) === '80' ? '' : ':' + tp) + '），已登录会自动跳转主站';
        } else {
            url = window.location.protocol + '//' + serverIp
                + (window.location.port ? ':' + window.location.port : '');
            cap = '手机扫码直接进入文件服务';
        }
        draw(url, cap);
    })
    .catch(function() {
        var url = window.location.protocol + '//' + window.location.host;
        draw(url, '手机扫码直接进入文件服务');
    });
}

// ========== 初始化 ==========
connectWS();
_wsDebugBind(ws); // 诊断插桩：首次连接对象（onWSConnected 覆盖重连后的对象）
setTimeout(function() {
    var httpPort = window.location.port || (window.location.protocol === 'https:' ? '443' : '80');
    var wsPort = window.__WS_PORT__ || 8081;
    var elHttp = document.getElementById('httpPort');
    if (elHttp) elHttp.textContent = httpPort;
    var elWs = document.getElementById('wsPortInfo');
    if (elWs) elWs.textContent = wsPort;
    loadServerLogs();
    initQR();
    fetch('/api/auth/check')
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (d && d.guest_mode !== undefined) setAuthState(d.guest_mode);
        });
    updateConfigInputs();
    alignLogCard();
    _onDemandRefresh();
}, 500);

// 管理页数据源：WebSocket 订阅成功后由服务器每秒推送（handleWSMessage）；
// 定时 HTTP 轮询作兜底：WS 断开、或连接了但尚未订阅成功（如 sid 认证失败）时刷新，
// 避免页面数据空白/永久转圈
setInterval(function() {
    if (!_wsAdminUsable()) refreshAll();
}, REFRESH_INTERVAL);

// 服务器日志预览：日志变化不频繁，独立低频刷新即可
setInterval(loadServerLogs, 5000);

