/*
 * LeafFS 安全区注入（content script，document_start）
 *
 * GeckoView 是铺满整屏的 —— 页面背景因此连到状态栏/导航栏底下，视觉上是一整片。
 * 代价是正文也会顶到系统栏下面，所以这里只补内容侧的留白。
 *
 * 数值由 App 给出（只有它知道真实 insets）：runtime.sendNativeMessage 的返回值就是
 * App 侧 onMessage 返回的 GeckoResult（GeckoView javadoc：resolves with a response）。
 *
 * 时序上必须重试：App 的消息代理是异步挂上的（ensureBuiltIn 回调里），而本脚本在
 * document_start 就发问 —— 早到的那次会被 GeckoView 当挂起消息丢掉
 *（日志里那条 releasePendingMessages）。所以这里问到拿到值为止。
 */
(function () {
  'use strict';

  var TRIES = 0;
  var MAX_TRIES = 20;   // 300ms × 20 ≈ 6 秒，足够代理挂好
  var RETRY_MS = 300;

  function px(n) {
    return Math.max(0, Math.round(n || 0)) + 'px';
  }

  function apply(v) {
    if (!v || typeof v.top !== 'number') return false;
    var css = 'body {'
      + ' padding-top: max(10px, ' + px(v.top) + ') !important;'
      + ' padding-bottom: max(10px, ' + px(v.bottom) + ') !important;'
      + ' padding-left: max(10px, ' + px(v.left) + ') !important;'
      + ' padding-right: max(10px, ' + px(v.right) + ') !important;'
      + ' }';
    var s = document.createElement('style');
    s.id = '__leaffs_insets__';
    s.textContent = css;
    (document.head || document.documentElement).appendChild(s);
    return true;
  }

  function ask() {
    TRIES++;
    try {
      browser.runtime.sendNativeMessage('browser', { type: 'getInsets' })
        .then(function (v) {
          var o = v;
          if (typeof o === 'string') {
            try {
              o = JSON.parse(o);
            } catch (e) {
              o = null;
            }
          }
          if (apply(o)) return;
          if (TRIES < MAX_TRIES) setTimeout(ask, RETRY_MS);
          else console.log('[LeafFS insets] 放弃：拿到无效值 ' + JSON.stringify(v));
        })
        .catch(function (e) {
          if (TRIES < MAX_TRIES) setTimeout(ask, RETRY_MS);
          else console.log('[LeafFS insets] 放弃：调用一直失败 ' + String(e));
        });
    } catch (e) {
      console.log('[LeafFS insets] sendNativeMessage 不可用 ' + String(e));
    }
  }

  ask();

  /*
   * 主题变化时把网页的实际背景色报给 App：系统栏图标要跟着深浅色换，
   * 否则深色主题下状态栏/导航栏图标会看不见。
   * 网页切主题不发页面导航，原生收不到信号，所以由这里主动上报（初始也报一次）。
   */
  /*
   * 主题（亮暗 + 主色）直接读 <html data-theme / data-accent>：网页是主题的唯一真源
   * （存在客户端本地），页面这里是原生唯一能看到它的地方。
   * 不用等 0.25s 的背景过渡、也不用读背景色算亮度。
   */
  function reportTheme() {
    try {
      var el = document.documentElement;
      browser.runtime.sendNativeMessage('browser', {
        type: 'theme',
        dark: el.getAttribute('data-theme') === 'dark',
        accent: el.getAttribute('data-accent') || ''
      });
    } catch (e) {
      /* 忽略：上报失败不影响页面 */
    }
  }

  try {
    new MutationObserver(reportTheme).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme', 'data-accent']
    });
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', reportTheme);
    } else {
      reportTheme();
    }
  } catch (e) {
    /* 忽略 */
  }

  /* App 推来的导入进度 → 驱动网页自己的那条进度条（复用它的样式和位置，不另画一条） */
  try {
    browser.storage.onChanged.addListener(function (changes, area) {
      if (area !== 'local' || !changes.leaffsProgress) return;
      var pct = changes.leaffsProgress.newValue;
      var wrap = document.getElementById('progressWrap');
      var bar = document.getElementById('progressBar');
      if (!wrap || !bar) return;
      if (typeof pct !== 'number' || pct < 0 || pct >= 100) {
        wrap.classList.remove('active');
        bar.style.width = '0%';
      } else {
        wrap.classList.add('active');
        bar.style.width = pct + '%';
      }
    });
  } catch (e2) {
    /* 忽略 */
  }
})();
