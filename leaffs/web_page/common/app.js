// 主题（toggleDark / applyTheme）已独立到 common/theme.js：
// 按钮本体与脚本由服务端随 <!--THEME_TOGGLE--> 占位一起注入，不再随 app.js 分发。

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

// 已经成功加载过的缩略图路径。重建列表（点选/搜索/刷新/切视图）时据此直接给新节点
// 带上 loaded —— 否则新节点又要走一遍「透明 → 加载 → 可见」，中间那一帧看着就是
// 闪了一下占位图。这份信息必须活在 DOM 之外：挂在节点上，换个节点就丢了。
var _thumbOk = {};

// 缩略图加载成功：淡入盖住占位图标（占位图标一直在下面，不用管它）
function thumbLoaded(img) {
    if (!img) return;
    img.classList.add('loaded');
    var p = img.getAttribute('data-path');
    if (p) _thumbOk[p] = 1;
}

// 缩略图彻底失败：把图片摘掉即可 —— 占位图标本来就在下面
function thumbGiveUpBlank(img) {
    if (img && img.parentNode) img.parentNode.removeChild(img);
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
    var en = window.__LANG__ === 'en';
    if (label) {
        label.textContent = connected
            ? (en ? 'Connected' : '已连接')
            : (en ? 'Reconnecting…' : '断开重连...');
    }
}

function sendWS(data) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(data));
        return true;
    }
    return false;
}

// ========== WS 订阅管理推送（A-05）==========
// 管理概览页（management.html/admin-core.js）与用户管理页（users.html）共用：
// 连接建立后调用一次 —— 直接发 {type:'admin-sub'}，服务端按**握手 Cookie** 判定角色
// （同源 WS 必带 wifi_session），推来 admin_data 即订阅成功。
// ⚠️ 早先这里是"同源取 /api/session/sid → 发 {type:'auth',sid}"。那个接口返回的正是
// HttpOnly cookie 里的长期会话 id（会一直用到会话过期），等于让页面 JS 能读走凭据，
// 已连同接口与 WS 的 sid 认证分支一并移除 —— 名字里的 Auth 是历史遗留。
// 被拒（error）→ console.warn 并置 ready=false，调用方走 HTTP 兜底。
// 返回 true = 已发起订阅；false = sock 无效未发起。
// 订阅结果经 window._onWSAdminSubscribed(ok) 通知页面（可选，供 HTTP 兜底门控使用）。
window.wsAdminAuthAndSubscribe = function (sock) {
    if (!sock || sock.readyState !== WebSocket.OPEN) return false;
    // 一次性回执监听：只处理本连接的首条 admin_data / error，处理完即移除
    var onMsg = function (ev) {
        var m;
        try { m = JSON.parse(ev.data); } catch (e) { return; }
        if (!m) return;
        if (m.type === 'admin_data') {
            sock.removeEventListener('message', onMsg);
            if (typeof window._onWSAdminSubscribed === 'function') window._onWSAdminSubscribed(true);
        } else if (m.type === 'error') {
            sock.removeEventListener('message', onMsg);
            console.warn('[WS] admin-sub 被拒，走 HTTP 兜底', m);
            if (typeof window._onWSAdminSubscribed === 'function') window._onWSAdminSubscribed(false);
        }
    };
    sock.addEventListener('message', onMsg);
    sock.send(JSON.stringify({ type: 'admin-sub' }));
    return true;
};

// ========== HTTP 回退 ==========
function apiList(path, callback) {
    fetch('/api/files?path=' + encodeURIComponent(path || ''))
        .then(function (r) { return r.json(); })
        .then(callback)
        .catch(function () { batchToast((window.__LANG__ === 'en') ? 'Failed to load' : '加载失败', 'error'); });
}

// LF-23：删除结果的提示。刻意**不走 batchToast** —— 那个会把 500ms 内的多条合并成
// "xxx (+N)"，而删除结果每一条都重要（哪个失败了、为什么失败），合起来就丢了信息。
// 所以走单条 toast：失败项逐条说清楚（最多 3 条，其余汇总），精确优先、但不刷屏。
function toastDeleteResult(d) {
    var en = window.__LANG__ === 'en';
    var failed = (d && d.failed) || [];
    var deleted = (d && typeof d.deleted === 'number') ? d.deleted : 0;
    if (!failed.length) {
        // 成功（含"目标本来就不存在"的幂等成功）
        toast(deleted > 1
            ? ((en ? 'Deleted ' : '已删除 ') + deleted + (en ? ' item(s)' : ' 个'))
            : (en ? 'Deleted' : '删除成功'), 'success');
        return;
    }
    var head = deleted > 0
        ? ((en ? 'Deleted ' : '已删除 ') + deleted + (en ? ', then failed: ' : ' 个；失败：'))
        : '';
    var shown = Math.min(failed.length, 3);
    for (var i = 0; i < shown; i++) {
        var f = failed[i] || {};
        toast(head + (f.path || '') + (en ? ' — ' : ' —— ')
              + (f.error || (en ? 'unknown reason' : '未知原因')), 'error');
        head = '';        // 只有第一条带"已删除 N 个"的前缀
    }
    if (failed.length > shown) {
        toast(en ? ('…and ' + (failed.length - shown) + ' more failed')
                 : ('……另有 ' + (failed.length - shown) + ' 个同样失败'), 'error');
    }
}

function apiDelete(paths, callback) {
    fetch('/api/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ files: paths })
    }).then(function (r) { return r.json(); }).then(callback)
      .catch(function () {
          // LF-23：原来没有 catch —— 权限拒绝（R1 口径下走 404）或网络中断时 Promise 静默
          // reject，页面上什么都不显示，用户以为"点了没反应"
          toast(window.__LANG__ === 'en' ? 'Delete request failed, please retry'
                                         : '删除请求失败，请重试', 'error');
      });
}

function apiMkdir(path, name, callback) {
    fetch('/api/mkdir', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: path, name: name })
    }).then(function (r) { return r.json(); }).then(callback);
}