// ========== 主题切换（全站唯一实现）==========
// 按钮本体由服务端注入（页面里只有 <!--THEME_TOGGLE--> 占位），本文件随后一起下发。
// 按钮文案由服务端统一提供一份，这里从按钮的 data-label-light / data-label-dark 读回 ——
// JS 里不再出现「亮色/暗色」这类字面量。
//
// 主题是一体的（亮暗 + 主色），都按普通浏览器偏好处理（leaf_theme / leaf_accent，
// 与语言的 leaf_lang 同一做法）：只写 cookie，不上报账号 —— 每台设备各存各的。
// 服务端渲染页面时读它注入 <html data-theme / data-accent>，首屏即正确，不需要初始化脚本。

// 按亮暗设置按钮文案：两套文案随按钮由服务端下发
function setThemeLabel(isDark) {
    var btn = document.getElementById('darkToggle');
    if (!btn) return;
    var label = btn.querySelector('#darkLabel');
    if (label) label.textContent = btn.dataset[isDark ? 'labelDark' : 'labelLight'];
}

// 应用亮暗：写 <html> + 留存 cookie + 同步按钮文案
function applyTheme(isDark) {
    var html = document.documentElement;
    if (isDark) html.setAttribute('data-theme', 'dark');
    else html.removeAttribute('data-theme');
    try { document.cookie = 'leaf_theme=' + (isDark ? 'dark' : 'light') + '; path=/; max-age=31536000'; } catch (e) {}
    setThemeLabel(isDark);
}

function toggleDark() {
    applyTheme(document.documentElement.getAttribute('data-theme') !== 'dark');
}
