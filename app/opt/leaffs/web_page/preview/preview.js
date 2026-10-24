// ========== 预览弹窗系统 ==========

var previewData = {
    currentIndex: -1,
    files: [],
    currentPath: ''
};

// 打开预览弹窗
function openPreview(path, files) {
    previewData.files = files || [];
    previewData.currentPath = path;

    var idx = -1;
    for (var i = 0; i < previewData.files.length; i++) {
        if (previewData.files[i].path === path) { idx = i; break; }
    }
    previewData.currentIndex = idx;

    var f = null;
    for (var i = 0; i < previewData.files.length; i++) {
        if (previewData.files[i].path === path) { f = previewData.files[i]; break; }
    }
    if (!f) return;

    var ft = fileType(f.name);
    var overlay = document.getElementById('modalOverlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'modalOverlay';
        overlay.className = 'modal-overlay';
        document.body.appendChild(overlay);
    }

    overlay.innerHTML = '';
    overlay.className = 'modal-overlay active';

    var content = document.createElement('div');
    content.className = 'modal-content';
    content.onclick = function (e) { e.stopPropagation(); };

    // 头部：文件名 + 操作按钮 + ✕
    var header = document.createElement('div');
    header.className = 'modal-header';
    header.innerHTML = '<span class="modal-title">' + esc(f.name) + '</span>' +
        '<div class="modal-actions">' +
        '<a href="/download/' + encodeURIComponent(f.path) + '" class="btn btn-xs btn-success" onclick="event.stopPropagation()">⬇ 下载</a>' +
        '<button class="btn btn-xs btn-danger" onclick="event.stopPropagation();deleteFromPreview()">🗑 删除</button>' +
        '<button class="modal-close-btn" onclick="closePreview()">✕</button>' +
        '</div>';
    content.appendChild(header);

    // 主体
    var body = document.createElement('div');
    body.className = 'modal-body';

    if (ft === 'image') {
        var img = document.createElement('img');
        img.className = 'preview-img';
        img.src = '/download/' + encodeURIComponent(f.path);
        // 不设 alt：图片旁边就有文件名，且加载中/失败时会把 alt 文本画出来
        // （长文件名没有断词机会，会直接穿出图片框）
        img.style.cursor = 'zoom-in';
        img.dataset.zoomed = 'false';
        img.onclick = function (e) {
            e.stopPropagation();
            if (this.dataset.zoomed === 'false') {
                this.style.maxWidth = 'none';
                this.style.maxHeight = 'none';
                this.style.width = 'auto';
                this.style.height = 'auto';
                this.style.cursor = 'zoom-out';
                this.dataset.zoomed = 'true';
            } else {
                this.style.maxWidth = '100%';
                this.style.maxHeight = '65vh';
                this.style.width = '';
                this.style.height = '';
                this.style.cursor = 'zoom-in';
                this.dataset.zoomed = 'false';
            }
        };
        body.appendChild(img);

        if (previewData.files.length > 1) {
            var prevBtn = document.createElement('button');
            prevBtn.className = 'modal-nav prev';
            prevBtn.innerHTML = '‹';
            prevBtn.onclick = function (e) { e.stopPropagation(); navPreview(-1); };
            content.appendChild(prevBtn);
            var nextBtn = document.createElement('button');
            nextBtn.className = 'modal-nav next';
            nextBtn.innerHTML = '›';
            nextBtn.onclick = function (e) { e.stopPropagation(); navPreview(1); };
            content.appendChild(nextBtn);
        }
    } else if (ft === 'video') {
        var video = document.createElement('video');
        video.className = 'preview-video';
        video.src = '/download/' + encodeURIComponent(f.path);
        video.controls = true;
        video.autoplay = true;
        body.appendChild(video);
    } else if (ft === 'audio') {
        var audio = document.createElement('audio');
        audio.className = 'preview-audio';
        audio.src = '/download/' + encodeURIComponent(f.path);
        audio.controls = true;
        audio.autoplay = true;
        body.appendChild(audio);
    } else if (ft === 'text') {
        var pre = document.createElement('pre');
        pre.className = 'preview-text';
        pre.textContent = '加载中...';
        body.appendChild(pre);
        fetch('/api/raw?path=' + encodeURIComponent(f.path))
            .then(function (r) {
                if (!r.ok) throw new Error('load failed');
                return r.text();
            })
            .then(function (text) { pre.textContent = text; })
            .catch(function () {
                var xhr = new XMLHttpRequest();
                xhr.open('GET', '/download/' + encodeURIComponent(f.path), true);
                xhr.onload = function () { pre.textContent = xhr.responseText; };
                xhr.onerror = function () { pre.textContent = '[加载失败]'; };
                xhr.send();
            });
    } else if (ft === 'pdf') {
        var iframe = document.createElement('iframe');
        iframe.className = 'preview-pdf';
        iframe.src = '/download/' + encodeURIComponent(f.path);
        body.appendChild(iframe);
    } else {
        body.innerHTML = '<div class="empty"><span class="icon">📄</span><p>此文件类型不支持直接预览，请下载查看</p></div>';
    }

    content.appendChild(body);
    overlay.appendChild(content);
    overlay.onclick = closePreview;

    document.onkeydown = function (e) {
        if (e.key === 'Escape') closePreview();
        if (e.key === 'ArrowLeft') navPreview(-1);
        if (e.key === 'ArrowRight') navPreview(1);
    };
}

function closePreview() {
    var overlay = document.getElementById('modalOverlay');
    if (overlay) {
        // 关闭前清理媒体元素，防止残留画面
        var videos = overlay.querySelectorAll('video');
        for (var i = 0; i < videos.length; i++) {
            var v = videos[i];
            v.pause();
            v.removeAttribute('src');
            v.load();
        }
        var audios = overlay.querySelectorAll('audio');
        for (var i = 0; i < audios.length; i++) {
            var a = audios[i];
            a.pause();
            a.removeAttribute('src');
            a.load();
        }
        var iframes = overlay.querySelectorAll('iframe');
        for (var i = 0; i < iframes.length; i++) {
            iframes[i].src = '';
        }
        overlay.className = 'modal-overlay';
        setTimeout(function () { overlay.innerHTML = ''; }, 200);
    }
    document.onkeydown = null;
}

function navPreview(direction) {
    if (previewData.currentIndex < 0) return;
    var newIdx = previewData.currentIndex + direction;
    if (newIdx < 0) newIdx = previewData.files.length - 1;
    if (newIdx >= previewData.files.length) newIdx = 0;
    var f = previewData.files[newIdx];
    if (!f) return;
    previewData.currentIndex = newIdx;
    openPreview(f.path, previewData.files);
}

function deleteFromPreview() {
    if (previewData.currentIndex < 0) return;
    var f = previewData.files[previewData.currentIndex];
    if (!f) return;
    if (!confirm('删除 "' + f.name + '" ？')) return;

    var oldHandler = window.handleWSMessage;
    window.handleWSMessage = function (msg) {
        if (msg.type === 'delete' && msg.success) {
            batchToast('✅ 删除成功', 'success');
            closePreview();
            if (typeof loadFiles === 'function') loadFiles();
        }
        if (oldHandler) oldHandler(msg);
    };

    if (sendWS({ type: 'delete', paths: [f.path] })) return;
    apiDelete([f.path], function (d) {
        batchToast(d.success ? '✅ 删除成功' : '❌ 删除失败', d.success ? 'success' : 'error');
        if (d.success) { closePreview(); if (typeof loadFiles === 'function') loadFiles(); }
    });
}