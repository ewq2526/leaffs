/*
 * 与 App 建立长连接。
 *
 * 方向是 App -> 扩展（content script 那条 sendNativeMessage 只能扩展往 App 发），
 * 所以按 GeckoView 文档用 runtime.connectNative：App 在 onConnect 里拿到 Port，
 * 之后把原生的导入进度推过来，这里转存进 storage，insets.js 监听到就去驱动网页的进度条。
 */
let port = browser.runtime.connectNative('browser');

port.onMessage.addListener(function (msg) {
  if (msg && typeof msg.pct === 'number') {
    browser.storage.local.set({ leaffsProgress: msg.pct });
  }
});

port.postMessage({ type: 'ready' });