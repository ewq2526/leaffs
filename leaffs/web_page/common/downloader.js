// ========== 断点续传下载管理器 ==========
// 基于 localStorage 持久化任务状态，支持暂停/继续/取消

var DL = {
    STORAGE_KEY: 'dl_tasks',
    tasks: {},
    activeXHR: {},   // activeXHR[taskId] = xhr
    onUpdate: null,  // 回调，由 UI 绑定
    _loaded: false
};

// ========== 任务数据结构 ==========
// task = {
//   id: string,
//   name: string,
//   path: string,
//   totalSize: number,
//   downloaded: number,
//   status: 'waiting' | 'downloading' | 'paused' | 'completed' | 'error',
//   errorMsg: string,
//   chunks: [{ start, end }]
// }

// ========== 加载持久化任务 ==========
DL.load = function () {
    try {
        var raw = localStorage.getItem(DL.STORAGE_KEY);
        if (raw) {
            DL.tasks = JSON.parse(raw);
            for (var id in DL.tasks) {
                var t = DL.tasks[id];
                if (t.status === 'downloading' || t.status === 'waiting') {
                    t.status = 'paused';
                }
            }
        }
    } catch (e) {
        DL.tasks = {};
    }
    DL._loaded = true;
    DL._save();
};

DL._save = function () {
    try {
        localStorage.setItem(DL.STORAGE_KEY, JSON.stringify(DL.tasks));
    } catch (e) {}
    if (DL.onUpdate) DL.onUpdate();
};

DL.addTask = function (name, path, totalSize) {
    var id = 'dl_' + Date.now() + '_' + Math.random().toString(36).substr(2, 6);
    DL.tasks[id] = {
        id: id, name: name, path: path,
        totalSize: totalSize || 0, downloaded: 0,
        status: 'waiting', errorMsg: ''
    };
    DL._save();
    DL.startTask(id);
    return id;
};

DL.getFileSize = function (path, callback) {
    var xhr = new XMLHttpRequest();
    xhr.open('HEAD', '/download/' + encodeURIComponent(path), true);
    xhr.onreadystatechange = function () {
        if (xhr.readyState === 4) {
            if (xhr.status === 200 || xhr.status === 206) {
                var size = parseInt(xhr.getResponseHeader('Content-Length') || '0');
                var acceptRanges = xhr.getResponseHeader('Accept-Ranges');
                callback(size, acceptRanges === 'bytes');
            } else {
                callback(0, false);
            }
        }
    };
    xhr.send();
};

DL.startTask = function (id) {
    var task = DL.tasks[id];
    if (!task) return;
    if (task.status === 'completed') return;
    task.status = 'downloading';
    task.errorMsg = '';
    DL._save();
    DL._downloadChunk(id);
};

DL._downloadChunk = function (id) {
    var task = DL.tasks[id];
    if (!task || task.status !== 'downloading') return;

    var xhr = new XMLHttpRequest();
    DL.activeXHR[id] = xhr;

    var url = '/download/' + encodeURIComponent(task.path);
    xhr.open('GET', url, true);
    xhr.responseType = 'arraybuffer';

    if (task.downloaded > 0) {
        xhr.setRequestHeader('Range', 'bytes=' + task.downloaded + '-');
    }

    xhr.onload = function () {
        if (xhr.status === 200 || xhr.status === 206) {
            var data = xhr.response;
            if (!data) {
                DL._onError(id, '无数据返回');
                return;
            }
            var contentRange = xhr.getResponseHeader('Content-Range');
            if (contentRange) {
                var match = contentRange.match(/\/(\d+)$/);
                if (match) {
                    task.totalSize = parseInt(match[1]);
                }
            } else if (task.totalSize === 0) {
                task.totalSize = parseInt(xhr.getResponseHeader('Content-Length') || data.byteLength);
            }

            DL._writeChunk(task, data, function (success) {
                if (!success) {
                    DL._onError(id, '写入文件失败');
                    return;
                }
                task.downloaded += data.byteLength;
                if (task.totalSize > 0 && task.downloaded >= task.totalSize) {
                    task.status = 'completed';
                    task.downloaded = task.totalSize;
                    delete DL.activeXHR[id];
                    DL._save();
                    DL._finalize(task);
                    return;
                }
                DL._save();
                DL._downloadChunk(id);
            });
        } else if (xhr.status === 416) {
            if (task.totalSize > 0 && task.downloaded >= task.totalSize) {
                task.status = 'completed';
                delete DL.activeXHR[id];
                DL._save();
                DL._finalize(task);
            } else {
                DL._onError(id, 'Range 错误 (416)');
            }
        } else {
            DL._onError(id, 'HTTP ' + xhr.status);
        }
    };

    xhr.onerror = function () { DL._onError(id, '网络错误'); };
    xhr.onprogress = function (e) { if (e.total > 0) task.totalSize = e.total; DL._save(); };
    xhr.send();
};

DL._blobs = {};

DL._writeChunk = function (task, data, callback) {
    if (!DL._blobs[task.id]) { DL._blobs[task.id] = []; }
    DL._blobs[task.id].push(new Blob([data]));
    callback(true);
};

DL._finalize = function (task) {
    var parts = DL._blobs[task.id];
    if (!parts || !parts.length) { DL._onError(task.id, '没有数据'); return; }
    var blob = new Blob(parts, { type: 'application/octet-stream' });
    delete DL._blobs[task.id];
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = task.name;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 10000);
    DL._save();
};

DL.pauseTask = function (id) {
    var task = DL.tasks[id];
    if (!task || task.status !== 'downloading') return;
    task.status = 'paused';
    var xhr = DL.activeXHR[id];
    if (xhr) { xhr.abort(); delete DL.activeXHR[id]; }
    DL._save();
};

DL.resumeTask = function (id) {
    var task = DL.tasks[id];
    if (!task || task.status !== 'paused') return;
    DL.startTask(id);
};

DL.cancelTask = function (id) {
    var task = DL.tasks[id];
    if (!task) return;
    var xhr = DL.activeXHR[id];
    if (xhr) { xhr.abort(); delete DL.activeXHR[id]; }
    delete DL._blobs[id];
    delete DL.tasks[id];
    DL._save();
};

DL._onError = function (id, msg) {
    var task = DL.tasks[id];
    if (!task) return;
    task.status = 'error';
    task.errorMsg = msg;
    delete DL.activeXHR[id];
    delete DL._blobs[id];
    DL._save();
};

DL.getProgress = function (id) {
    var task = DL.tasks[id];
    if (!task || task.totalSize <= 0) return 0;
    return Math.min(100, Math.round(task.downloaded / task.totalSize * 100));
};

DL.getTasks = function () {
    var list = [];
    for (var id in DL.tasks) { list.push(DL.tasks[id]); }
    list.sort(function (a, b) { return a.id < b.id ? 1 : -1; });
    return list;
};

DL.init = function () { DL.load(); };

function startResumeDownload(name, path) {
    var tasks = DL.getTasks();
    for (var i = 0; i < tasks.length; i++) {
        if (tasks[i].path === path && tasks[i].status !== 'completed' && tasks[i].status !== 'error') {
            batchToast('⏳ 已在下载队列中', 'info');
            return;
        }
    }
    batchToast('📥 添加下载: ' + name, 'info');
    DL.addTask(name, path, 0);
}