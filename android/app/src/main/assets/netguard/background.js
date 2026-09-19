/*
 * LeafFS Net Guard —— 只放行本机的网络请求。
 *
 * 为什么要有它：页面上的**子资源**（<img src>、外链脚本/样式、fetch/XHR、WebSocket）不走
 * GeckoView 的 NavigationDelegate（`onLoadRequest` 只管**导航**），所以"软件内不访问外部"
 * 在客户端这一侧一直缺一块。服务端已经补了页面 CSP（2026-09-19），但那依赖"页面是我们发的"；
 * 这一层是**深度防御**：页面万一被注入了外部资源，请求也出不去。
 *
 * 口径与 App 侧的 `isInternalUrl` 保持一致：只认 127.0.0.1 / localhost / [::1]（端口不限）。
 * 不一致的话会出现"导航允许、子资源被拦"或反过来的怪事。
 *
 * 解析不出 host 的一律按不可信处理（fail-closed）—— 宁可拦错，不可放过。
 */
var LOCAL_HOSTS = ['127.0.0.1', 'localhost', '[::1]', '::1'];

function isLocal(url) {
    try {
        var u = new URL(url);
        return LOCAL_HOSTS.indexOf(u.hostname) >= 0;
    } catch (e) {
        return false;
    }
}

/* 把拦截记录推给 App（只有 background script 能这样连；App 侧用 ext.setMessageDelegate 收）。 */
var port = null;
try {
    port = browser.runtime.connectNative('browser');
    port.postMessage({ type: 'ready' });
} catch (e) {
    port = null;
}

function report(msg) {
    if (!port) return;
    try {
        port.postMessage(msg);
    } catch (e) {
        /* 端口断了就算了：拦截本身不依赖上报 */
    }
}

browser.webRequest.onBeforeRequest.addListener(
    function (details) {
        if (isLocal(details.url)) {
            return {};
        }
        report({ type: 'blocked', url: details.url, kind: details.type });
        return { cancel: true };
    },
    { urls: ['<all_urls>'] },
    ['blocking']
);
