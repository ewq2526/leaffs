// ========== 暗色模式切换 ==========
function toggleDark() {
    var html = document.documentElement;
    var isDark = html.getAttribute('data-theme') === 'dark';
    var btn = document.getElementById('darkToggle');
    if (isDark) {
        html.removeAttribute('data-theme');
        localStorage.setItem('theme', 'light');
        if (btn) { btn.querySelector('#darkIcon').textContent = '☀'; btn.querySelector('#darkLabel').textContent = '亮色'; }
    } else {
        html.setAttribute('data-theme', 'dark');
        localStorage.setItem('theme', 'dark');
        if (btn) { btn.querySelector('#darkIcon').textContent = '☾'; btn.querySelector('#darkLabel').textContent = '暗色'; }
    }
}
(function() {
    if (localStorage.getItem('theme') === 'dark') {
        document.documentElement.setAttribute('data-theme', 'dark');
        setTimeout(function() {
            var btn = document.getElementById('darkToggle');
            if (btn) { btn.querySelector('#darkIcon').textContent = '☾'; btn.querySelector('#darkLabel').textContent = '暗色'; }
        }, 0);
    }
})();

// 二维码配色跟随主题：暗色=白点黑底，亮色=黑点白底（保证扫码可辨）
function qrThemeColors() {
    var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    return isDark
        ? { colorDark: '#ffffff', colorLight: '#000000' }
        : { colorDark: '#000000', colorLight: '#ffffff' };
}

// ========== 全局工具函数 ==========

// 安全的 HTML 转义
function esc(str) {
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
}

// 安全的 JS 字符串
function safeStr(str) {
    return str.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

// 文件类型正则（集中定义，供 getFileIcon / fileType 共用）
var FILE_TYPES = {
    image: /\.(jpg|jpeg|png|gif|webp|bmp|svg)$/i,
    video: /\.(mp4|webm|mov|avi|mkv|flv|ts|mts|m4v|3gp|ogv|wmv|vob|mpeg|mpg)$/i,
    audio: /\.(mp3|wav|ogg|flac|aac|m4a)$/i,
    text: /\.(txt|md|log|ini|cfg|py|js|html|css|json|xml|yaml|yml|toml|sh|bat|conf)$/i,
    pdf: /\.pdf$/i,
    archive: /\.(zip|rar|7z|tar|gz)$/i,
    exe: /\.(exe|msi|dmg|apk)$/i
};

// 获取文件类型
function fileType(name) {
    for (var key in FILE_TYPES) {
        if (FILE_TYPES[key].test(name)) return key;
    }
    return 'other';
}

// 获取文件类型图标
var TYPE_ICONS = {
    image: '🖼️',
    video: '🎬',
    audio: '🎵',
    text: '📝',
    pdf: '📕',
    archive: '📦',
    exe: '⚙️'
};

function getFileIcon(name) {
    var ft = fileType(name);
    return TYPE_ICONS[ft] || '📄';
}

// Toast 通知（防刷屏）
var _toastTimer = null, _toastQueue = [];

function toast(msg, type) {
    var container = document.querySelector('.toast-container');
    if (!container) {
        container = document.createElement('div');
        container.className = 'toast-container';
        document.body.appendChild(container);
    }
    var el = document.createElement('div');
    el.className = 'toast ' + (type || 'info');
    el.textContent = msg;
    container.appendChild(el);
    setTimeout(function () { el.remove(); }, 2800);
}

function batchToast(msg, type) {
    _toastQueue.push({ msg: msg, type: type || 'info' });
    if (_toastTimer) return;
    _toastTimer = setTimeout(function () {
        var last = _toastQueue[_toastQueue.length - 1];
        var count = _toastQueue.length;
        var text = count > 1 ? last.msg + ' (+' + (count - 1) + ')' : last.msg;
        toast(text, last.type);
        _toastQueue = [];
        _toastTimer = null;
    }, 500);
}

// 格式化文件大小（支持 B ~ TB）
function formatSize(b) {
    if (!b || b === 0) return '0 B';
    var u = ['B', 'KB', 'MB', 'GB', 'TB'];
    var i = Math.floor(Math.log(b) / Math.log(1024));
    if (i >= u.length) i = u.length - 1;
    var val = b / Math.pow(1024, i);
    return (i === 0 ? val : val.toFixed(1)) + ' ' + u[i];
}

// 格式化时间戳（本地时间字符串）
function formatTime(ts) {
    if (!ts) return '';
    var d = new Date(ts * 1000);
    var pad = function (n) { return n < 10 ? '0' + n : '' + n; };
    var month = pad(d.getMonth() + 1);
    var day = pad(d.getDate());
    var hours = pad(d.getHours());
    var mins = pad(d.getMinutes());
    return month + '-' + day + ' ' + hours + ':' + mins;
}

// 缩略图加载成功：隐藏“加载中”提示
function thumbLoaded(img) {
    if (!img) return;
    var w = img.parentNode;
    var ld = w && w.querySelector('.thumb-loading');
    if (ld) ld.style.display = 'none';
}

// 缩略图重试仍失败：移除加载提示与图片（浏览页网格卡仍可点击预览）
function thumbGiveUpBlank(img) {
    if (!img) return;
    var w = img.parentNode;
    if (w) {
        var ld = w.querySelector('.thumb-loading');
        if (ld) ld.remove();
    }
    img.remove();
}

// 缩略图加载失败：5 秒后带缓存戳重试一次；仍失败则隐藏/交给占位回调
function retryThumbOnce(img, onGiveUp) {
    if (!img) return;
    var tries = parseInt(img.getAttribute('data-retry') || '0', 10);
    if (tries >= 1) {
        if (typeof onGiveUp === 'function') { onGiveUp(img); return; }
        img.style.display = 'none';
        return;
    }
    img.setAttribute('data-retry', '1');
    if (!img.getAttribute('data-src')) img.setAttribute('data-src', img.getAttribute('src') || '');
    setTimeout(function () {
        if (!img.isConnected) return; // 节点已被移除/页面已跳转则放弃
        var base = img.getAttribute('data-src') || img.getAttribute('src') || '';
        if (!base) { if (typeof onGiveUp === 'function') onGiveUp(img); else img.style.display = 'none'; return; }
        img.src = base + (base.indexOf('?') === -1 ? '?' : '&') + '_=' + Date.now();
    }, 5000);
}

// ========== 全局 Enter 键触发按钮 ==========
document.addEventListener('keydown', function(e) {
    if (e.key !== 'Enter') return;
    var el = e.target;
    // 只处理普通输入框，排除 textarea、checkbox、radio、button、file 等
    if (!el || el.tagName !== 'INPUT') return;
    if (el.type === 'button' || el.type === 'submit' || el.type === 'checkbox' || el.type === 'radio' || el.type === 'file') return;
    e.preventDefault();
    // 优先找所在容器中的第一个按钮
    var parent = el.closest('.config-row, .action-form, .dl-row, .dl-settings');
    var btn;
    if (parent) {
        btn = parent.querySelector('button');
    }
    // 如果所在容器没有按钮，退一级找卡片容器中的按钮（如配额三行共用同一个按钮）
    if (!btn) {
        var card = el.closest('.admin-card, .dl-card');
        if (card) btn = card.querySelector('button');
    }
    if (btn) btn.click();
});

// ========== WebSocket 连接（带心跳） ==========
var ws = null, wsConnected = false;
// 端口由服务端注入页面(window.__WS_PORT__)，不再硬编码，改端口后仍能连上
var WS_PORT = (typeof window !== 'undefined' && window.__WS_PORT__) ? window.__WS_PORT__ : 8081;
var _wsPingTimer = null;

function connectWS() {
    var protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    var host = window.location.hostname;
    var url = protocol + '//' + host + ':' + WS_PORT + '/ws';
    ws = new WebSocket(url);
    ws.onopen = function () {
        wsConnected = true;
        updateWSStatus(true);
        // 心跳保活：每 25 秒发一次 ping
        if (_wsPingTimer) clearInterval(_wsPingTimer);
        _wsPingTimer = setInterval(function () {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'ping' }));
            }
        }, 25000);
        if (typeof onWSConnected === 'function') onWSConnected();
    };
    ws.onmessage = function (e) {
        try {
            var msg = JSON.parse(e.data);
            // 忽略心跳响应
            if (msg.type === 'pong') return;
            if (typeof handleWSMessage === 'function') handleWSMessage(msg);
        } catch (err) {
            console.log('WS parse error:', err);
        }
    };
    ws.onclose = function () {
        wsConnected = false;
        updateWSStatus(false);
        if (_wsPingTimer) { clearInterval(_wsPingTimer); _wsPingTimer = null; }
        setTimeout(connectWS, 3000);
    };
    ws.onerror = function () {
        ws.close();
    };
}

function updateWSStatus(connected) {
    var dot = document.getElementById('wsDot');
    var label = document.getElementById('wsLabel');
    if (dot) dot.className = 'dot' + (connected ? ' connected' : '');
    if (label) label.textContent = connected ? '已连接' : '断开重连...';
}

function sendWS(data) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(data));
        return true;
    }
    return false;
}

// ========== WS 显式 sid 认证 + 管理订阅（A-05：auth 消息）==========
// 管理概览页（management.html/admin-core.js）与用户管理页（users.html）共用：
// 连接建立后调用一次 —— 同源取 sid → 发 {type:'auth',sid} → 服务端回
// {type:'auth',success:true} 后自动发 {type:'admin-sub'}（每连接一次即可，不重复 auth）。
// sid 为空/请求失败/连接已关闭 → console.warn 且不订阅（调用方应走各自 HTTP 兜底）。
// 返回 true = 已发起认证流程；false = sock 无效未发起。
// 订阅结果经 window._onWSAdminSubscribed(ok) 通知页面（可选，供 HTTP 兜底门控使用）。
window.wsAdminAuthAndSubscribe = function (sock) {
    if (!sock || sock.readyState !== WebSocket.OPEN) return false;
    fetch('/api/session/sid', { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
            var sid = (d && d.sid) || '';
            if (!sid) throw new Error('empty sid');
            if (sock.readyState !== WebSocket.OPEN) return; // 期间连接已重建/关闭
            // 一次性 auth 回执监听：只处理本连接的首条 auth 帧，处理完即移除
            var onAuthMsg = function (ev) {
                var m;
                try { m = JSON.parse(ev.data); } catch (e) { return; }
                if (!m || m.type !== 'auth') return;
                sock.removeEventListener('message', onAuthMsg);
                if (m.success && sock.readyState === WebSocket.OPEN) {
                    sock.send(JSON.stringify({ type: 'admin-sub' }));
                    if (typeof window._onWSAdminSubscribed === 'function') window._onWSAdminSubscribed(true);
                } else {
                    console.warn('[WS] auth 失败，走 HTTP 兜底', m);
                    if (typeof window._onWSAdminSubscribed === 'function') window._onWSAdminSubscribed(false);
                }
            };
            sock.addEventListener('message', onAuthMsg);
            sock.send(JSON.stringify({ type: 'auth', sid: sid }));
        })
        .catch(function (err) {
            console.warn('[WS] sid 获取失败，走 HTTP 兜底', (err && err.message) || err);
            if (typeof window._onWSAdminSubscribed === 'function') window._onWSAdminSubscribed(false);
        });
    return true;
};

// ========== HTTP 回退 ==========
function apiList(path, callback) {
    fetch('/api/files?path=' + encodeURIComponent(path || ''))
        .then(function (r) { return r.json(); })
        .then(callback)
        .catch(function () { batchToast('加载失败', 'error'); });
}

function apiDelete(paths, callback) {
    fetch('/api/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ files: paths })
    }).then(function (r) { return r.json(); }).then(callback);
}

function apiMkdir(path, name, callback) {
    fetch('/api/mkdir', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: path, name: name })
    }).then(function (r) { return r.json(); }).then(callback);
}