// 上下键在可聚焦元素之间走焦点。
//
// 光有 Tab 时要按住 Shift 才能回头，只有键盘的场景下（装好的机器常常只接一把键盘）
// 上下走更顺手。三条边界：
//   · 单行输入框里上下键本来没有用途，直接征用；
//   · select 里上下是改值、textarea 里是翻行，这两种不碰；
//   · 左右键仍归输入框自己走光标，这里只管上下。
// 带 Ctrl/Alt/Meta/Shift 的不拦，留给浏览器与页面自己的快捷键。
//
// 由 /static/arrow.js 提供给每个页面，页面里只需一行
// <script src="/static/arrow.js"></script>。
(function () {
    var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), ' +
        'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

    function walk(dir) {
        var list = Array.prototype.filter.call(document.querySelectorAll(FOCUSABLE), function (n) {
            return n.tabIndex >= 0 && n.offsetParent !== null;
        });
        if (!list.length) return false;
        var i = list.indexOf(document.activeElement);
        if (i < 0) { list[0].focus(); return true; }
        var j = i + dir;
        if (j < 0 || j >= list.length) return false;
        list[j].focus();
        return true;
    }

    document.addEventListener('keydown', function (e) {
        if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
        if (e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return;
        var tag = e.target && e.target.tagName;
        if (tag === 'SELECT' || tag === 'TEXTAREA') return;
        if (walk(e.key === 'ArrowDown' ? 1 : -1)) e.preventDefault();
    });
})();
