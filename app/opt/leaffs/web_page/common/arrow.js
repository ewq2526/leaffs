// 方向键：左右切顶部导航，上下留给页面滚动。
//
// 这台机器常常只接一把键盘（或一个遥控器式的键盘），页面之间要有不靠鼠标的走法：
//   · 左右 → 在 <nav class="tabs"> 的链接之间走，走到就进那一页；
//   · 上下 → 不拦，交给浏览器滚页；
//   · 焦点在输入框 / textarea / select 里时，左右归光标与选项自己用，这里不碰；
//   · 带 Ctrl/Alt/Meta 的不拦，留给浏览器与页面自己的快捷键。
//
// 安装器页面另有自己的一份（选项卡片是竖排的，那里上下走焦点才对），不用这个文件。
//
// 由 /static/arrow.js 提供给每个页面，页面里只需一行
// <script src="/static/arrow.js"></script>。
(function () {
    function tabLinks() {
        var nav = document.querySelector('nav.tabs');
        if (!nav) return [];
        return Array.prototype.slice.call(nav.querySelectorAll('a[href]'));
    }

    function switchTab(dir) {
        var links = tabLinks();
        if (links.length < 2) return false;
        var i = -1;
        for (var k = 0; k < links.length; k++) {
            if (links[k].classList.contains('active') ||
                links[k].getAttribute('href') === location.pathname) { i = k; break; }
        }
        if (i < 0) return false;
        var j = i + dir;
        if (j < 0 || j >= links.length) return false;
        location.href = links[j].getAttribute('href');
        return true;
    }

    // 预览弹层开着时左右是翻上一张/下一张（common/preview.js 管），这里让路
    function overlayOpen() {
        var o = document.getElementById('modalOverlay');
        return !!(o && o.classList.contains('active'));
    }

    document.addEventListener('keydown', function (e) {
        if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
        if (e.ctrlKey || e.altKey || e.metaKey) return;
        if (overlayOpen()) return;
        var tag = e.target && e.target.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        if (switchTab(e.key === 'ArrowRight' ? 1 : -1)) e.preventDefault();
    });
})();
