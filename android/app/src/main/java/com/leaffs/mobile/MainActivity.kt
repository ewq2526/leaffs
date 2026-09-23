package com.leaffs.mobile

import android.app.Activity
import android.app.Dialog
import android.content.ClipData
import android.content.ClipboardManager
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.res.ColorStateList
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import com.chaquo.python.Python
import org.json.JSONObject
import org.mozilla.geckoview.AllowOrDeny
import org.mozilla.geckoview.BasicSelectionActionDelegate
import org.mozilla.geckoview.GeckoResult
import org.mozilla.geckoview.GeckoRuntime
import org.mozilla.geckoview.GeckoSession
import org.mozilla.geckoview.GeckoView
import org.mozilla.geckoview.WebExtension
import org.mozilla.geckoview.WebRequestError
import org.mozilla.geckoview.WebResponse
import java.io.File
import java.io.FileOutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.net.URL

/** LeafFS 手机版：打开应用 = 启动本地服务 + GeckoView 显示管理页面（与电脑端同一套网页）。 */
class MainActivity : ComponentActivity() {

    private lateinit var geckoView: GeckoView
    private lateinit var root: FrameLayout
    private lateinit var progress: ProgressBar       // 启动阶段：居中转圈
    private lateinit var pageProgress: ProgressBar   // 页面加载：顶部细进度条
    private lateinit var errorView: TextView         // 启动失败提示（点击重试）
    private lateinit var session: GeckoSession
    /** 主页 = 启动时 login 跳转到的那个页面（记一次）。返回键从其它页回这里，在这里则退出 */
    private var homeUrl: String? = null
    /** 最近的页面地址：选文件时用它推断"传到哪个目录" */
    @Volatile private var lastUrl = ""

    /** 证书错误放行：出错地址 + 已尝试次数（防止"放行失败→再报错"来回死循环） */
    private var certErrorUri = ""
    private var certErrorTries = 0

    /** onLoadError 已返回证书错误页，等它的 onPageStop 到来再放行（GeckoSession 没有 url 属性，
     *  只能靠这个标记判断"现在停在错误页上"） */
    private var certErrorPagePending = false

    /** 证书放行成功后的落点：TLS 下要依次给 8081（WS）和 8080（HTTP）各放行一次，最后停这儿 */
    private var certNextUrl = ""

    /** 正在预热 8081 的证书例外（WebSocket 是页面 JS 发起的连接，不会走 onLoadError） */
    private var certWarmupPending = false

    /** 服务端当前的 scheme 与 WS 端口（TLS 下 8080 是 https、8081 是 wss）——
     *  预热判断要在 onPageStop 里用，所以不能只做 bootAndOpen 的局部变量 */
    private var srvScheme = "http"
    private var wsPort = 8081

    /**
     * 启动遮罩：盖住 GeckoView 的一块纯色 View。
     *
     * 为什么需要它：TLS 下要先给 8081 预热证书例外，而 8081 是 WebSocket 端口，
     * 对普通 GET 回 `426 Upgrade Required` —— Gecko 会把这个响应当成一个页面画出来
     * （日志里是 `onPageStop(success=true) lastUrl=https://127.0.0.1:8081/`），
     * 于是每次启动都闪一下报错页。
     * 不能拿 `GeckoView.visibility` 去藏：不可见的 Gecko 内容会被节流，放行脚本可能不执行。
     * 所以只盖、不改可见性 —— 引擎照常渲染、脚本照常跑，用户看不见而已。
     */
    private lateinit var bootCover: View

    /** 遮罩是否还盖着（真正进到应用页面才撤） */
    private var bootCovering = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        geckoView = GeckoView(this)
        progress = ProgressBar(this).apply {
            isIndeterminate = true
            visibility = View.VISIBLE
            indeterminateTintList = ColorStateList.valueOf(getColor(R.color.lf_primary))
        }
        // 页面加载进度：顶部 3dp 细条（网页内导航时才出现）
        pageProgress = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            max = 100
            visibility = View.GONE
            progressTintList = ColorStateList.valueOf(getColor(R.color.lf_primary))
        }
        // 启动失败提示：居中文字（主题色），点一下重试
        errorView = TextView(this).apply {
            textSize = 14f
            gravity = Gravity.CENTER
            setTextColor(getColor(R.color.lf_text))
            setPadding(dp(28), dp(28), dp(28), dp(28))
            visibility = View.GONE
            setOnClickListener { retryBoot() }
        }
        root = FrameLayout(this)
        root.setBackgroundColor(getColor(R.color.lf_bg))
        root.addView(geckoView, ViewGroup.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT))
        root.addView(pageProgress, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, dp(3), Gravity.TOP))
        // 启动遮罩：压在 GeckoView 之上（转圈和错误提示后加，仍在它上面，照常可见）
        bootCovering = true
        bootCover = View(this).apply {
            setBackgroundColor(getColor(R.color.lf_bg))
            visibility = View.VISIBLE
        }
        root.addView(bootCover, ViewGroup.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT))
        root.addView(progress, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.WRAP_CONTENT,
            FrameLayout.LayoutParams.WRAP_CONTENT,
            Gravity.CENTER))
        root.addView(errorView, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.WRAP_CONTENT,
            Gravity.CENTER))
        setContentView(root)
        applyEdgeToEdgeInsets()
        // 卡片关闭后系统栏观感可能停在卡片那一版、不再跟网页主题 → 重设一遍主窗口的值
        // （亮暗取自本地偏好：它由网页每次上报主题时写入，是原生侧唯一的常驻来源）
        LfDialog.onCardClosed = {
            applyBarsAppearanceDark(
                getSharedPreferences(PREFS, MODE_PRIVATE).getBoolean(KEY_THEME_DARK, false))
        }

        session = GeckoSession()
        session.setContentDelegate(object : GeckoSession.ContentDelegate {
            override fun onExternalResponse(session: GeckoSession, response: WebResponse) {
                // Gecko 无法内联显示的响应（文件下载）→ 交给系统下载管理器
                startDownload(response)
            }

            /** 长按链接/图片/媒体 → 原生菜单（内核不实现则长按没有任何反应） */
            override fun onContextMenu(
                session: GeckoSession,
                screenX: Int,
                screenY: Int,
                element: GeckoSession.ContentDelegate.ContextElement
            ) {
                showContextMenu(element, screenX, screenY)
            }

            /** 网页视频进入/退出全屏 → 同步隐藏或恢复系统栏 */
            @Suppress("DEPRECATION")
            override fun onFullScreen(session: GeckoSession, fullScreen: Boolean) {
                window.decorView.systemUiVisibility = if (fullScreen) {
                    View.SYSTEM_UI_FLAG_FULLSCREEN or
                        View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or
                        View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                } else {
                    View.SYSTEM_UI_FLAG_VISIBLE
                }
            }
        })
        // 文本选择菜单：内核默认弹系统的 ActionMode 浮条（样式无法改），
        // 这里继承官方实现但接管显示，换成 LeafFS 风格菜单。
        session.setSelectionActionDelegate(LeafSelectionActionDelegate(this))
        session.setProgressDelegate(object : GeckoSession.ProgressDelegate {
            override fun onPageStart(session: GeckoSession, url: String) {
                pageProgress.progress = 0
                pageProgress.visibility = View.VISIBLE
                // 证书流程期间记下每个开始加载的地址：用来确认错误页到底有没有真的加载
                if (certErrorPagePending || certWarmupPending) {
                    pyLog("证书流程: onPageStart $url")
                }
            }

            override fun onProgressChange(session: GeckoSession, progress: Int) {
                pageProgress.progress = progress
            }

            override fun onPageStop(session: GeckoSession, success: Boolean) {
                // 加载结束（含被下载中断的情况）都收起进度条
                pageProgress.visibility = View.GONE
                // 页面加载完成后刷新配色缓存（用户在网页里换过主题/主色时随之更新）
                // 页面加载完成后同步一次配色（外壳背景/进度条随网页更新）
                withWebColors { }
                // 证书流程期间把每一拍打出来：用来判断放行脚本是不是发早了
                if (certErrorPagePending || certWarmupPending) {
                    pyLog("证书流程: onPageStop(success=$success) lastUrl=$lastUrl")
                }
                // 证书放行不在这里做：onLoadError 会返回一个自带错误页（data: URL），
                // 那个页面一加载就自己调 document.addCertException 并用 leaffs:// 回执（见 certErrorPage）。
                // 放在这里曾经是错的 —— onLoadError 之后最先到的是"加载失败"那一拍，
                // 那时页面上根本没有 addCertException，脚本静默失效、回执永远等不到。
                //
                // 真正进到应用页面（不是 8081 预热、不是证书错误页）→ 撤掉启动遮罩。
                // 必须看 lastUrl：预热和错误页也会报 success，光看 success 会把遮罩提前撤掉。
                if (bootCovering && success && !certWarmupPending && !certErrorPagePending &&
                    lastUrl.startsWith("$srvScheme://127.0.0.1:") &&
                    !lastUrl.startsWith("$srvScheme://127.0.0.1:$wsPort")
                ) {
                    pyLog("启动遮罩: 已进入 $lastUrl，撤掉遮罩")
                    // 这一拍就是 login 跳转过去的那个页面 → 记成主页（返回键回它、在它上面则退出）
                    homeUrl = lastUrl
                    hideBootCover()
                    // 真正进到应用页面了，顺手引导后台运行权限（退后台被冻的根源）
                    askBackgroundRunPermission()
                }
                if (certWarmupPending && success &&
                    lastUrl.startsWith("$srvScheme://127.0.0.1:$wsPort")
                ) {
                    // 8081 那条没报证书错误（已被信任）→ 直接进真正的页面。
                    // 必须比对 lastUrl：否则上一个页面的 onPageStop 会把预热提前消耗掉，
                    // 8081 的例外从没加上，前端就连不上 wss（页面正常却一直"重连中"）。
                    certWarmupPending = false
                    pyLog("证书预热: 8081 未报证书错误，直接进入 $certNextUrl")
                    if (certNextUrl.isNotEmpty()) session.loadUri(certNextUrl)
                } else if (success) {
                    certErrorTries = 0   // 正常加载成功 → 放行计数归零
                }
            }
        })
        session.setPromptDelegate(promptDelegate())
        session.setNavigationDelegate(object : GeckoSession.NavigationDelegate {
            /** 记住当前地址：选文件时据此推断"传到哪个目录"（与网页上传的目标一致） */
            override fun onLocationChange(
                session: GeckoSession,
                url: String?,
                perms: MutableList<GeckoSession.PermissionDelegate.ContentPermission>,
                hasUserGesture: Boolean
            ) {
                if (!url.isNullOrEmpty()) lastUrl = url
            }

            override fun onLoadRequest(
                session: GeckoSession,
                request: GeckoSession.NavigationDelegate.LoadRequest
            ): GeckoResult<AllowOrDeny>? {
                // 权限说明页是 data: URL（不依赖服务先起来），页面里没法调服务端，
                // 两个按钮就用自定义 scheme 把用户的选择带回 App
                val uri = request.uri
                if (uri.startsWith("leaffs://")) {
                    val rest = uri.removePrefix("leaffs://")
                    // 证书放行回执：放行脚本执行完把结果带回来（见 onPageStop）
                    if (rest.startsWith("certok/")) {
                        certErrorPagePending = false
                        val next = android.net.Uri.decode(rest.removePrefix("certok/"))
                        pyLog("证书放行成功 → 前往 $next")
                        if (next.isNotEmpty()) session.loadUri(next)
                        return GeckoResult.fromValue(AllowOrDeny.DENY)
                    }
                    if (rest.startsWith("certfail/")) {
                        certErrorPagePending = false
                        pyLog("证书放行失败: " +
                            android.net.Uri.decode(rest.removePrefix("certfail/")))
                        return GeckoResult.fromValue(AllowOrDeny.DENY)
                    }
                    when (rest.trimEnd('/')) {
                        "grant" -> runOnUiThread { requestLocalNetworkPermissionIfNeeded() }
                        "skip" -> runOnUiThread { bootAndOpen() }
                        "settings" -> runOnUiThread { openAppSettings() }
                    }
                    return GeckoResult.fromValue(AllowOrDeny.DENY) // DENY = 已自行处理
                }
                // 2026-09-18：只允许访问**本机服务**，其余一律拒绝 —— 用户要求
                // 「软件内不允许访问外部链接」（网页里的站外链接、PDF 里的外链都不许在
                // App 内打开）。放行名单见 isInternalUrl()。
                if (!isInternalUrl(uri)) {
                    pyLog("已阻止外部链接: $uri")
                    runOnUiThread {
                        Toast.makeText(this@MainActivity, "已阻止访问外部链接",
                                       Toast.LENGTH_SHORT).show()
                    }
                    return GeckoResult.fromValue(AllowOrDeny.DENY)
                }
                // 单视图应用：网页要"新窗口"（target="_blank" / window.open）时不另开会话，
                // 改为当前视图加载 —— 下载类响应会走 onExternalResponse 落盘到下载目录。
                // 注意：不能在 onNewSession 里 load（javadoc 明令禁止），故在此拦截。
                if (request.target == GeckoSession.NavigationDelegate.TARGET_WINDOW_NEW) {
                    session.loadUri(request.uri)
                    return GeckoResult.fromValue(AllowOrDeny.DENY) // DENY = 已自行处理
                }
                return null // null 视为 ALLOW，照常加载
            }

            /** 加载失败（服务未就绪/断网）→ 展示与网页同一套样式的错误页，而不是白屏 */
            override fun onLoadError(
                session: GeckoSession,
                uri: String?,
                error: WebRequestError
            ): GeckoResult<String>? {
                val origin = try {
                    java.net.URI(uri).let { "${it.scheme}://${it.authority}/" }
                } catch (_: Exception) {
                    "/"
                }
                // 自签证书（安卓上服务端用的就是自签）：GeckoView 把证书错误当"加载失败"报过来。
                // 先让 Gecko 显示它自带的证书错误页，页加载完再由 onPageStop 自动放行 —— 放行入口
                // document.addCertException() 是引擎内的 WebIDL API，Java 侧没有对应接口。
                if (error.code == WebRequestError.ERROR_SECURITY_BAD_CERT && certErrorTries < 3) {
                    certErrorTries++
                    // uri 可能是空（实测 8081 这种非网页请求就没带上）→ 退回自己的目标地址
                    certErrorUri = if (!uri.isNullOrEmpty()) uri else certNextUrl
                    certErrorPagePending = true
                    certWarmupPending = false   // 预热就是为了加例外，现在正在加，别再走预热分支
                    val next = if (certNextUrl.isNotEmpty()) certNextUrl else certErrorUri
                    pyLog("证书错误(code=${error.code}) → 用自带错误页放行 $certErrorUri " +
                            "(第 $certErrorTries 次)，成功则前往 $next")
                    return GeckoResult.fromValue(certErrorPage(next))
                }
                // 预热期间遇到别的错误（连接被拒/超时等）→ 不再纠结 8081，直接进真正的页面
                if (certWarmupPending) {
                    certWarmupPending = false
                    pyLog("证书预热: 8081 加载失败(code=${error.code})，直接进入 $certNextUrl")
                    if (certNextUrl.isNotEmpty()) return GeckoResult.fromValue(certNextUrl)
                }
                // 错误页由 Python 侧生成：直接内联网页 style.css 并复用其 .modal 结构，
                // 因此与网页同一套设计（背景星点光晕、玻璃卡片、按钮全部一致）。
                val html = try {
                    val mod = Python.getInstance().getModule("leaffs_mobile")
                    mod.callAttr("error_page", origin).toJava(String::class.java)
                } catch (_: Throwable) {
                    ""
                }
                var page = if (html.isNullOrEmpty()) {
                    "<html><body style=\"font:14px sans-serif;padding:24px\">加载失败</body></html>"
                } else html
                // 错误页是 data: URL，扩展的 content script **注入不了 data: URL**，
                // 所以 insets.js 那套安全区 padding 在这里不生效 —— 直接拼进页面，
                // 免得内容贴到状态栏 / 导航栏下面
                val safe = safeAreaStyle()
                page = if (page.contains("</head>")) page.replace("</head>", safe + "</head>")
                       else safe + page
                val b64 = android.util.Base64.encodeToString(
                    page.toByteArray(Charsets.UTF_8), android.util.Base64.NO_WRAP)
                return GeckoResult.fromValue("data:text/html;base64,$b64")
            }
        })
        session.open(runtime())
        geckoView.setSession(session)

        // 扩展的注入脚本是 content script，消息代理必须等 session 打开之后再挂
        setupInsetsExtension()
        // netguard 是 background script（没有 content script），不依赖 session 是否打开；
        // 放在同一处注册只是为了一眼看到"这个 App 装了两个内置扩展"
        setupNetGuardExtension()

        // 启动前台服务：退到后台也继续共享（常驻通知）
        LeafService.start(this)
        requestNotifPermissionIfNeeded()
        requestStoragePermissionIfNeeded()

        // 返回键 / 返回手势：其它页面回主页，已经在主页则问"退后台还是彻底退出"。
        // 不用网页历史后退（session.goBack()）—— 那是"回上一网页"，不是回主页。
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                val home = homeUrl
                if (home == null || lastUrl == home) {
                    confirmExit()
                } else {
                    session.loadUri(home)
                }
            }
        })

        // 打开应用即启动本地服务，就绪后加载页面。
        // 但缺局域网权限时服务会起成"局域网不可达"（安卓 16 起默认拦），而授权框是异步的 ——
        // 所以要等用户点完再起，结果在 onRequestPermissionsResult 里接着走。
        if (needLocalNetworkPermission()) {
            // 先显示权限说明页（data: URL，不用等服务起来）讲清楚为什么要这个权限：
            // 系统框只会弹一次，直接弹容易被随手拒掉，之后就只能在系统设置里翻。
            // 之前申请过、系统又不再弹框的，直接给"去系统设置"那一版
            val perm = localNetworkPermission()
            showPermissionPage(denied = perm != null && localNetAskedBefore() &&
                !shouldShowRequestPermissionRationale(perm))
        } else {
            bootAndOpen()
        }
    }

    // ---------- 网页配色联动 ----------

    /** 上次成功读到的配色（读取失败时兜底，避免退化成内置色） */
    private var cachedColors: JSONObject? = null

    /**
     * 弹原生 UI 之前**现读**网页当前配色再渲染，保证卡片与网页此刻完全一致
     *（网页里刚换的主题/主色立刻生效，不需要等下一次）。
     *
     * Python 侧对 CSS 解析结果做了缓存，重复读取只是字典查找，开销可忽略。
     * 内核的选择态能稳定保持（实测 2 秒以上），同步读取不会导致菜单弹不出来。
     */
    private fun withWebColors(block: () -> Unit) {
        val colors = readWebColors() ?: cachedColors
        if (colors != null) cachedColors = colors
        LfDialog.setWebColors(colors)
        colors?.let { applyShellColors(it) }
        block()
    }

    /** 现读网页配色（Python 按 style.css + 网页报来的亮暗/主色计算） */
    private fun readWebColors(): JSONObject? = try {
        val mod = Python.getInstance().getModule("leaffs_mobile")
        val s: String = mod.callAttr("ui_theme").toJava(String::class.java)
        JSONObject(s).takeIf { it.optBoolean("ok") }
    } catch (_: Throwable) {
        null
    }

    /** 外壳（背景、进度条）也跟随网页配色 */
    private fun applyShellColors(colors: JSONObject) {
        try {
            if (::root.isInitialized) {
                // 网页铺满整屏（含系统栏区域），这个底色只在页面还没画出来时露一下
                root.setBackgroundColor(Color.parseColor(colors.getString("bg")))
            }
            val primary = ColorStateList.valueOf(
                Color.parseColor(colors.getString("primary")))
            progress.indeterminateTintList = primary
            pageProgress.progressTintList = primary
        } catch (_: Exception) {
            // 值异常时保留内置色
        }
    }

    /**
     * 沉浸式（edge-to-edge）：GeckoView 铺满整屏 —— 页面背景连状态栏/导航栏一起铺，
     * 这才是真正的"透明"，不再是"上面一条纯色色带、下面才是网页"。
     *
     * 正文避让交给注入的 CSS（assets/insets 里的内置扩展）：内容留在安全区、背景不受影响，
     * 这两件事可以同时成立。走过的弯路记在这里：
     *   · 给 root 加 insets padding —— 背景跟着一起缩，系统栏又露成一条纯色；
     *   · GeckoView 的 setDynamicToolbarMaxHeight / setVerticalClipping —— 实测不生效。
     *
     * 只在 API 35+ 做：那时窗口是强制的 edge-to-edge、系统栏透明；更低版本窗口本来就把内容
     * 放在安全区内、系统栏还是主题里的不透明色，铺满没有意义。
     */
    private fun applyEdgeToEdgeInsets() {
        // 初值跟系统深浅（网页还没上报之前）；之后完全由网页的 data-theme 决定
        val night = (resources.configuration.uiMode and
            android.content.res.Configuration.UI_MODE_NIGHT_MASK) ==
            android.content.res.Configuration.UI_MODE_NIGHT_YES
        applyBarsAppearanceDark(night)
        // 系统默认会给导航栏垫一层半透明灰，与网页的玻璃背景不搭，关掉
        if (Build.VERSION.SDK_INT >= 29) {
            window.isNavigationBarContrastEnforced = false
        }
        if (Build.VERSION.SDK_INT < 35) return

        ViewCompat.setOnApplyWindowInsetsListener(root) { _, insets ->
            val b = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            insetsTop = b.top
            insetsBottom = b.bottom
            insetsLeft = b.left
            insetsRight = b.right
            insets
        }
        ViewCompat.requestApplyInsets(root)
    }

    /** 当前系统栏安全区（px）：注入的 content script 会取走它给正文留白 */
    private var insetsTop = 0
    private var insetsBottom = 0
    private var insetsLeft = 0
    private var insetsRight = 0

    /**
     * 内嵌页（data: URL）自带的安全区样式。
     *
     * content script **不支持 data: URL**，所以 insets.js 的注入对这些页面无效，
     * 只能由页面自己带上。换算与 insets.js 一致：物理 px ÷ density，并保留同样的
     * 10px 保底（max()），免得小安全区时正文贴边。
     */
    private fun safeAreaStyle(): String {
        val d = resources.displayMetrics.density.takeIf { it > 0f } ?: 1f
        fun css(px: Int) = maxOf(10, (px / d).toInt())
        return "<style>body{" +
            "padding-top:${css(insetsTop)}px;padding-bottom:${css(insetsBottom)}px;" +
            "padding-left:${css(insetsLeft)}px;padding-right:${css(insetsRight)}px;" +
            "box-sizing:border-box}</style>"
    }

    /**
     * Java 侧（App）排查用的日志 —— 经 Chaquopy 转给 Python 的 `leaffs_mobile.jlog`，
     * 由它写进 leaffs.log / stderr。
     *
     * ⚠️ 别改回 `android.util.Log`：那条路在真机上**读不到** —— 本项目没有 logcat 出口，
     * netguard / 安全区扩展那些排查日志就是这么"打了却从来没见过"的。
     */
    private fun pyLog(msg: String) {
        try {
            Python.getInstance().getModule("leaffs_mobile").callAttr("jlog", msg)
        } catch (_: Throwable) {
        }
    }

    /**
     * 注册内置扩展（assets/insets）：它的 content script 在 document_start 向 App 要安全区，
     * 再往 body 注入 padding。一次注册，所有页面（含中英文各版）都生效。
     */
    private fun setupInsetsExtension() {
        try {
            runtime().webExtensionController
                // 用 installBuiltIn 而不是 ensureBuiltIn：后者只在"没装过"时安装，
                // 扩展升级（比如这次新加了 background 长连接）不会生效
                .installBuiltIn(INSETS_EXTENSION)
                .accept({ ext ->
                    if (ext == null) return@accept
                    // 注入脚本是 content script，消息必须走 SessionController 这条路径；
                    // extension.setMessageDelegate 那条只收 background script 的消息。
                    // （之前挂错了：结果是 releasePendingMessages: session=null，消息被丢弃）
                    session.webExtensionController.setMessageDelegate(
                        ext, insetsMessageDelegate(), "browser")
                }, { e ->
                    pyLog("安全区扩展注册失败: $e")
                })
        } catch (t: Throwable) {
            pyLog("安全区扩展不可用: $t")
        }
    }

    /**
     * 注册内置扩展（assets/netguard）：**只放行本机**（127.0.0.1 / localhost / [::1]）的网络
     * 请求，其余一律 cancel。补的是**子资源**那一侧 —— `onLoadRequest` 只管**导航**，
     * `<img src>`、外链脚本、fetch、WebSocket 都不走它。
     *
     * ⚠️ 这是深度防御的**第四层**，前面还有：属性位置 XSS 已修（2026-09-18）、用户文件里的
     * HTML/SVG 不被内联渲染（服务端 force_plain）、页面 CSP（服务端，2026-09-19）。
     * ⚠️ 消息走 `ext.setMessageDelegate` —— 那是 **background script** 那条路径；
     * content script 才需要走 session，见上面 setupInsetsExtension 里的说明（挂错会静默丢消息）。
     */
    private fun setupNetGuardExtension() {
        try {
            pyLog("netguard: 开始注册内置扩展")
            runtime().webExtensionController
                .installBuiltIn(NETGUARD_EXTENSION)
                .accept({ ext ->
                    if (ext == null) {
                        pyLog("netguard: installBuiltIn 回调拿到 null")
                        return@accept
                    }
                    pyLog("netguard: 扩展已安装，挂 message delegate")
                    ext.setMessageDelegate(netGuardMessageDelegate(), "browser")
                    pyLog("netguard: message delegate 已挂上")
                }, { e ->
                    // ⚠️ 若这里报的是"缺权限"，说明内置扩展拿不到 webRequestBlocking —— 那就
                    // 改用 declarativeNetRequest（声明式规则，不需要 blocking 权限）
                    pyLog("netguard 扩展注册失败: $e")
                })
        } catch (t: Throwable) {
            pyLog("netguard 扩展不可用: $t")
        }
    }

    /** netguard 只往 App 报两件事：「我起来了」「我拦了谁」
     *
     *  ⚠️ `onConnect` 必须实现 —— 扩展那边用 `runtime.connectNative` 建 Port，
     *  App 侧不接的话连接建不起来，上报（以及验证"到底有没有生效"）就全没了。
     *
     *  ⚠️ 这里的两条日志是**排查用**的，别删：以前成功路径一条日志都没有，于是
     *  "扩展明明装上了、GeckoView 日志里也有 WebExtension:Message，可就是看不到上报"
     *  这种情况根本没法定位（2026-09-19 真机上就是这么卡住的）。
     */
    private fun netGuardMessageDelegate() = object : WebExtension.MessageDelegate {

        override fun onConnect(port: WebExtension.Port) {
            // ⚠️ 两件都必须做：
            //   ① 把 Port **存住** —— 没人引用的话它会被回收，连接就断了；
            //   ② 调 `setDelegate` —— **少了这行就收不到 Port 上的消息**。
            // 2026-09-19 真机上就是卡在第 ②：扩展发了消息、GeckoView 日志里也有
            // `WebExtension:Message`，但 App 侧一条都收不到（`onMessage` 从未被调用）。
            // 官方文档的例子（web-extensions.html 的 port_messaging 一节）里就有这一步。
            netGuardPort = port
            port.setDelegate(netGuardPortDelegate)
            pyLog("netguard: 扩展连上来了（Port 已建立）")
        }

        override fun onMessage(
            nativeApp: String,
            message: Any,
            sender: WebExtension.MessageSender
        ): GeckoResult<Any>? {
            // background script 的消息**可能**走这条，也可能走 PortDelegate（取决于扩展用
            // connectNative 还是 sendNativeMessage）。两条是**不同的投递路径**、不会重复投递
            // 同一条消息，所以都接上不会打两遍。等实测确认哪条生效后，把没用的那条删掉。
            handleNetGuardMessage(message)
            return GeckoResult.fromValue("")
        }
    }

    /** netguard 连上来的 Port —— 必须持有引用，否则被回收、连接就断 */
    @Volatile private var netGuardPort: WebExtension.Port? = null

    private val netGuardPortDelegate = object : WebExtension.PortDelegate {
        override fun onPortMessage(message: Any, port: WebExtension.Port) {
            handleNetGuardMessage(message)
        }

        override fun onDisconnect(port: WebExtension.Port) {
            if (port == netGuardPort) netGuardPort = null
        }
    }

    /** netguard 上报的唯一处理入口（两条投递路径共用） */
    private fun handleNetGuardMessage(message: Any) {
        // 原样打出来：消息**可能不是** JSONObject（`as?` 会安静地变成 null，于是 when 全部
        // 跳过、什么都不打 —— 那就又回到"查不出原因"）。先看类型，再判断。
        pyLog("netguard: onMessage class="
            + message.javaClass.simpleName + " msg=" + message.toString())
        val json = message as? JSONObject
        when (json?.optString("type")) {
            "ready" -> pyLog("netguard 扩展已就绪（webRequest 可用）")
            "blocked" -> pyLog("netguard 拦截外部请求: " + json.optString("url"))
        }
    }

    /** 回答注入脚本的 insets 询问：返回 CSS px（物理像素要除以 density，否则留白会大一倍多） */
    /** 扩展连上来的长连接：导入进度靠它推给页面（App → 扩展方向） */
    @Volatile private var insetsPort: WebExtension.Port? = null

    private fun insetsMessageDelegate() = object : WebExtension.MessageDelegate {

        override fun onConnect(port: WebExtension.Port) {
            insetsPort = port
        }
        override fun onMessage(
            nativeApp: String,
            message: Any,
            sender: WebExtension.MessageSender
        ): GeckoResult<Any>? {
            val json = message as? JSONObject
            if (json?.optString("type") == "theme") {
                // 网页报来的主题（<html data-theme / data-accent>）：换系统栏图标，并存下来给
                // Python 侧取色（原生控件、权限页、错误页）；原生看不到 cookie，这是唯一来源
                val dark = json.optBoolean("dark")
                applyBarsAppearanceDark(dark)
                rememberTheme(dark, json.optString("accent"))
                return GeckoResult.fromValue("")
            }
            ViewCompat.getRootWindowInsets(root)?.let { ins ->
                val b = ins.getInsets(WindowInsetsCompat.Type.systemBars())
                insetsTop = b.top
                insetsBottom = b.bottom
                insetsLeft = b.left
                insetsRight = b.right
            }
            val density = resources.displayMetrics.density.takeIf { it > 0f } ?: 1f
            val r = JSONObject()
            r.put("top", (insetsTop / density).toInt())
            r.put("bottom", (insetsBottom / density).toInt())
            r.put("left", (insetsLeft / density).toInt())
            r.put("right", (insetsRight / density).toInt())
            // 返回 JSON 字符串而非 JSONObject：后者桥接回页面时报
            // "Invalid event data for callback"，字符串是 primitive、最稳
            return GeckoResult.fromValue(r.toString())
        }
    }

    /** 网页主题模式 → 系统栏图标：深色主题给浅色图标，浅色主题给深色图标 */
    private fun applyBarsAppearanceDark(dark: Boolean) {
        if (!::root.isInitialized) return
        val c = androidx.core.view.WindowInsetsControllerCompat(window, root)
        c.isAppearanceLightStatusBars = !dark
        c.isAppearanceLightNavigationBars = !dark
    }

    /**
     * 记住网页报来的主题（亮暗 + 主色）：写进本机偏好，并转给 Python 侧。
     *
     * 主题的真源是网页的客户端本地存储（cookie），原生读不到 cookie —— 内置扩展从
     * <html data-theme / data-accent> 读出来上报是原生唯一的信息来源。
     * 存本机是为了网页还没加载时（权限页、错误页）也能用上一次的值。
     */
    private fun rememberTheme(dark: Boolean, accent: String) {
        try {
            getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                .putBoolean(KEY_THEME_DARK, dark)
                .putString(KEY_THEME_ACCENT, accent)
                .apply()
        } catch (_: Throwable) {
        }
        pushSavedTheme()
    }

    /** 把本机记住的主题推给 Python（生成权限页/错误页取色时要用） */
    private fun pushSavedTheme() {
        try {
            val p = getSharedPreferences(PREFS, MODE_PRIVATE)
            Python.getInstance().getModule("leaffs_mobile").callAttr(
                "set_theme",
                p.getBoolean(KEY_THEME_DARK, false),
                p.getString(KEY_THEME_ACCENT, "") ?: ""
            )
        } catch (_: Throwable) {
        }
    }

    /** Android 13+ 通知权限：拒绝则常驻通知不显示（服务仍在前台运行） */
    private fun requestNotifPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
            == android.content.pm.PackageManager.PERMISSION_GRANTED) return
        requestPermissions(arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), REQ_NOTIF)
    }

    // ---------- GeckoView 桥接：文件选择 + JS 弹窗（内核不实现则网页这些功能静默失效） ----------

    private var pendingFile: GeckoSession.PromptDelegate.FilePrompt? = null
    private var pendingFileResult: GeckoResult<GeckoSession.PromptDelegate.PromptResponse>? = null

    private fun promptDelegate() = object : GeckoSession.PromptDelegate {

        /** <input type=file> / webkitdirectory → 转发系统文件选择器 */
        @Suppress("DEPRECATION")
        override fun onFilePrompt(
            session: GeckoSession,
            prompt: GeckoSession.PromptDelegate.FilePrompt
        ): GeckoResult<GeckoSession.PromptDelegate.PromptResponse> {
            val result = GeckoResult<GeckoSession.PromptDelegate.PromptResponse>()
            pendingFile = prompt
            pendingFileResult = result
            val isFolder = prompt.type == GeckoSession.PromptDelegate.FilePrompt.Type.FOLDER
            pyLog("文件选择请求 type=${prompt.type} folder=$isFolder " +
                "mime=${prompt.mimeTypes?.joinToString() ?: "null"}")
            val intent = if (isFolder) {
                // 官方示例：文件夹选择器加 CATEGORY_DEFAULT，content:// 可直接交给内核
                Intent(Intent.ACTION_OPEN_DOCUMENT_TREE).addCategory(Intent.CATEGORY_DEFAULT)
            } else {
                // 官方示例的写法：GET_CONTENT + CATEGORY_OPENABLE + EXTRA_LOCAL_ONLY
                Intent(Intent.ACTION_GET_CONTENT).apply {
                    addCategory(Intent.CATEGORY_OPENABLE)
                    putExtra(Intent.EXTRA_LOCAL_ONLY, true)
                    putExtra(Intent.EXTRA_ALLOW_MULTIPLE,
                        prompt.type == GeckoSession.PromptDelegate.FilePrompt.Type.MULTIPLE)
                    val mt = prompt.mimeTypes
                    if (mt != null && mt.size == 1) {
                        type = mt[0]
                    } else {
                        type = "*/*"
                        if (mt != null && mt.isNotEmpty()) putExtra(Intent.EXTRA_MIME_TYPES, mt)
                    }
                }
            }
            try {
                startActivityForResult(intent, if (isFolder) REQ_PICK_FOLDER else REQ_PICK_FILE)
            } catch (t: Throwable) {
                pendingFile = null
                pendingFileResult = null
                result.complete(prompt.dismiss())
            }
            return result
        }

        /** 网页 confirm() */
        override fun onButtonPrompt(
            session: GeckoSession,
            prompt: GeckoSession.PromptDelegate.ButtonPrompt
        ): GeckoResult<GeckoSession.PromptDelegate.PromptResponse> {
            val result = GeckoResult<GeckoSession.PromptDelegate.PromptResponse>()
            withWebColors {
                LfDialog.confirm(
                    this@MainActivity,
                    prompt.title ?: "确认",
                    prompt.message ?: "",
                    onOk = {
                        result.complete(prompt.confirm(
                            GeckoSession.PromptDelegate.ButtonPrompt.Type.POSITIVE))
                    },
                    onCancel = {
                        result.complete(prompt.confirm(
                            GeckoSession.PromptDelegate.ButtonPrompt.Type.NEGATIVE))
                    }
                )
            }
            return result
        }

        /** 网页 prompt()（如新建文件夹名称） */
        override fun onTextPrompt(
            session: GeckoSession,
            prompt: GeckoSession.PromptDelegate.TextPrompt
        ): GeckoResult<GeckoSession.PromptDelegate.PromptResponse> {
            val result = GeckoResult<GeckoSession.PromptDelegate.PromptResponse>()
            withWebColors {
                LfDialog.prompt(
                    this@MainActivity,
                    prompt.title,
                    prompt.message,
                    prompt.defaultValue,
                    onOk = { text -> result.complete(prompt.confirm(text)) },
                    onCancel = { result.complete(prompt.dismiss()) }
                )
            }
            return result
        }

        /** 网页 alert()（AlertPrompt 只能 dismiss，没有 confirm） */
        override fun onAlertPrompt(
            session: GeckoSession,
            prompt: GeckoSession.PromptDelegate.AlertPrompt
        ): GeckoResult<GeckoSession.PromptDelegate.PromptResponse> {
            val result = GeckoResult<GeckoSession.PromptDelegate.PromptResponse>()
            withWebColors {
                LfDialog.alert(
                    this@MainActivity,
                    prompt.title,
                    prompt.message,
                    onOk = { result.complete(prompt.dismiss()) }
                )
            }
            return result
        }

        /** 网页 <select> 下拉（首页排序、账号页语言切换）→ LeafFS 风格列表 */
        override fun onChoicePrompt(
            session: GeckoSession,
            prompt: GeckoSession.PromptDelegate.ChoicePrompt
        ): GeckoResult<GeckoSession.PromptDelegate.PromptResponse> {
            val result = GeckoResult<GeckoSession.PromptDelegate.PromptResponse>()
            val choices = prompt.choices
            if (choices == null || choices.isEmpty()) {
                result.complete(prompt.dismiss())
                return result
            }
            val labels = choices.map { it.label ?: "" }
            withWebColors {
                if (prompt.type == GeckoSession.PromptDelegate.ChoicePrompt.Type.SINGLE) {
                    LfDialog.singleList(
                        this@MainActivity,
                        prompt.message,
                        labels,
                        choices.indexOfFirst { it.selected },
                        onCancel = { result.complete(prompt.dismiss()) }
                    ) { which -> result.complete(prompt.confirm(choices[which])) }
                } else {
                    val pre = choices.indices.filter { choices[it].selected }
                    LfDialog.multiList(this@MainActivity, prompt.message, labels, pre,
                        onCancel = { result.complete(prompt.dismiss()) }) { picked ->
                        val sel = picked.map { choices[it] }.toTypedArray()
                        result.complete(
                            if (sel.isEmpty()) prompt.dismiss() else prompt.confirm(sel))
                    }
                }
            }
            return result
        }

        /** 上传文件夹前的确认提示 */
        override fun onFolderUploadPrompt(
            session: GeckoSession,
            prompt: GeckoSession.PromptDelegate.FolderUploadPrompt
        ): GeckoResult<GeckoSession.PromptDelegate.PromptResponse> {
            return GeckoResult.fromValue(prompt.confirm(AllowOrDeny.ALLOW))
        }
    }

    /** 退到网页起点后确认：退后台继续共享 / 彻底退出 / 后台权限 / 返回（什么都不做） */
    private fun confirmExit() {
        LfDialog.menu(this, "退出 LeafFS",
            listOf("退到后台（继续共享）", "彻底退出", "后台运行权限", "返回"), null) { which ->
            when (which) {
                0 -> moveTaskToBack(true)   // 退到后台，前台服务继续跑
                1 -> exitCompletely()       // 停服务、结束进程
                2 -> {
                    // 重新走一遍后台权限引导：先清掉"已问过"标记
                    getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                        .putBoolean(KEY_BG_ASKED, false).apply()
                    askBackgroundRunPermission()
                }
                // 3（返回）：卡片已关闭，留在当前页继续用
            }
        }
    }

    /**
     * 彻底退出：停前台服务后结束进程。
     *
     * 停服务后稍等再结束进程 —— 否则 system_server 还没处理完 stopService，会把服务当成
     * "被杀"，按 START_STICKY 又拉起来。端口不在这里关：进程结束后由系统回收，残留的
     * TIME_WAIT 不影响下次启动（_port_free 探测带 SO_REUSEADDR）。
     */
    private fun exitCompletely() {
        LeafService.stop(this)
        finishAndRemoveTask()
        android.os.Handler(android.os.Looper.getMainLooper()).postDelayed({
            android.os.Process.killProcess(android.os.Process.myPid())
        }, 500)
    }

    /** 刚跳去系统设置过，回来要问一句"开了没"（不自动标记） */
    private var bgReturnedFromSettings = false

    /**
     * 后台运行权限的引导：没进白名单时国产 ROM 会把带前台服务的进程一起冻住 ——
     * 现象是通知还在、服务却没响应。
     *
     * 只管厂商那套（后台高耗电 / 自启动）：它系统 API 查不到，只能引导用户去应用设置里开；
     * **只问一次**（偏好里记着，选过就不再问）。
     */
    private fun askBackgroundRunPermission() {
        val prefs = getSharedPreferences(PREFS, MODE_PRIVATE)
        if (prefs.getBoolean(KEY_BG_ASKED, false)) return
        withWebColors {
            LfDialog.menu(
                this, "后台运行",
                listOf("打开应用设置（开后台高耗电 / 自启动）", "以后再说"),
                "退到后台时系统会冻结 LeafFS，共享会中断"
            ) { which ->
                if (which == 0) {
                    // 去系统设置：不标记，回来还要问一句"开了没"（自己说开了才算数）
                    bgReturnedFromSettings = true
                    startActivity(Intent(
                        android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        Uri.parse("package:$packageName")))
                } else {
                    // 以后再说：明确表示不想弄了，记下不再问
                    prefs.edit().putBoolean(KEY_BG_ASKED, true).apply()
                }
            }
        }
    }

    /**
     * 把选中的文件直接导入共享目录的"当前目录" —— 不经过内核、也不经过上传接口。
     *
     * 为什么绕开内核：内核跑在独立进程，读不了 SAF 的 content://（NS_ERROR_FILE_UNRECOGNIZED_PATH），
     * 官方解法是先拷进缓存再交给它，那是两次写。这里直接读 content uri 写进目标目录，只写一次。
     * 目标目录由当前网页地址推断（/browse/users/admin → users/admin），和网页上传的目标一致。
     */
    private fun importPickedFiles(picked: List<Uri>) {
        Thread {
            val root = shareRoot()
            val rel = currentRelativeDir()
            if (root == null) {
                runOnUiThread {
                    Toast.makeText(this, "导入失败：拿不到共享目录", Toast.LENGTH_LONG).show()
                }
                return@Thread
            }
            val dir = File(root, rel)
            if (!dir.exists() && !dir.mkdirs()) {
                runOnUiThread {
                    Toast.makeText(this, "导入失败：无法创建目录", Toast.LENGTH_LONG).show()
                }
                return@Thread
            }
            // 网页上传会按服务端上限拒绝超大文件，这里照做
            val maxSize = uploadMaxSize()
            var total = 0L
            for (u in picked) total += fileSize(u)
            var done = 0L
            var ok = 0
            var tooBig = 0
            var quotaErr = ""
            for (u in picked) {
                val size = fileSize(u)
                if (maxSize > 0 && size > maxSize) {
                    tooBig++
                    done += size
                    reportProgress(done, total)
                    continue
                }
                // 配额也在写之前查（走安卓专属的 check_quota，规则与网页上传同一套）
                val qErr = checkQuota(rel, size)
                if (qErr != null) {
                    quotaErr = qErr
                    done += size
                    reportProgress(done, total)
                    continue
                }
                val name = queryDisplayName(u) ?: "upload_${System.currentTimeMillis()}"
                val target = uniqueFile(dir, name.replace(Regex("[\\\\/:*?\"<>|]"), "_"))
                try {
                    var written = 0L
                    contentResolver.openInputStream(u)?.use { input ->
                        FileOutputStream(target).use { out ->
                            val buf = ByteArray(64 * 1024)
                            while (true) {
                                val read = input.read(buf)
                                if (read < 0) break
                                written += read
                                // 边写边判：不依赖 OpenableColumns.SIZE，超限必拦
                                if (maxSize > 0 && written > maxSize) {
                                    throw IllegalStateException("超过大小上限")
                                }
                                out.write(buf, 0, read)
                            }
                        }
                    } ?: throw IllegalStateException("无法读取选中文件")
                    ok++
                } catch (t: Throwable) {
                    target.delete()   // 半截文件不留
                    pyLog("导入失败: $name: $t")
                }
                done += size
                reportProgress(done, total)
            }
            // 让 Python 的文件夹缓存作废，否则网页刷新也看不到新文件
            // （推送也挂在这次失效上，所以文件变化会自动通知开着的页面）
            try {
                Python.getInstance().getModule("leaffs.files.core")
                    .callAttr("invalidate_folder_cache_smart", rel)
            } catch (t: Throwable) {
                pyLog("失效目录缓存失败: $t")
            }
            val n = ok
            val skipped = tooBig
            pushProgress(-1)   // 让网页收起进度条
            runOnUiThread {
                val notes = ArrayList<String>()
                if (skipped > 0) notes.add("$skipped 个超过大小上限")
                if (quotaErr.isNotEmpty()) notes.add("配额不足：$quotaErr")
                val msg = if (notes.isEmpty()) {
                    "已导入 $n 个文件"
                } else {
                    "已导入 $n 个文件，${notes.joinToString("；")}"
                }
                Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
                if (n > 0) session.reload()
            }
        }.start()
    }

    /** 服务端配置的上传大小上限（0 = 不限），与网页上传同一口径 */
    private fun uploadMaxSize(): Long = try {
        Python.getInstance().getModule("leaffs.config.core")
            .callAttr("get_upload_max_size").toJava(Long::class.java) ?: 0L
    } catch (_: Throwable) {
        0L
    }

    /** 写之前的配额检查，返回错误文案（null = 通过）。检查本身失败不拦，避免误挡导入。 */
    private fun checkQuota(relDir: String, size: Long): String? = try {
        val mod = Python.getInstance().getModule("leaffs_mobile")
        val s: String = mod.callAttr("check_quota", relDir, size).toJava(String::class.java)
        val j = JSONObject(s)
        if (j.optBoolean("ok")) null else j.optString("error").ifBlank { "配额不足" }
    } catch (_: Throwable) {
        null
    }

    /**
     * 文件字节数 —— 写之前就要知道，所以不能靠读内容。
     * 先看 provider 报的 SIZE，拿不到就打开 fd 直接 seek 到末尾取长度（同样不读内容）。
     */
    private fun fileSize(uri: Uri): Long {
        try {
            contentResolver.query(uri, null, null, null, null)?.use { c ->
                if (c.moveToFirst()) {
                    val i = c.getColumnIndex(android.provider.OpenableColumns.SIZE)
                    if (i >= 0 && !c.isNull(i)) {
                        val s = c.getLong(i)
                        if (s > 0) return s
                    }
                }
            }
        } catch (_: Throwable) {
            // 落到 fd 兜底
        }
        return try {
            contentResolver.openFileDescriptor(uri, "r")?.use { pfd ->
                android.system.Os.lseek(
                    pfd.fileDescriptor, 0L, android.system.OsConstants.SEEK_END)
            } ?: 0L
        } catch (_: Throwable) {
            0L
        }
    }

    /** 导入进度推给页面，由网页自己那条进度条显示（走扩展的长连接） */
    private fun reportProgress(done: Long, total: Long) {
        val pct = if (total > 0) ((done * 100) / total).toInt().coerceIn(0, 100) else 0
        pushProgress(pct)
    }

    /** pct < 0 表示隐藏进度条 */
    private fun pushProgress(pct: Int) {
        try {
            val m = JSONObject()
            m.put("pct", pct)
            insetsPort?.postMessage(m)
        } catch (t: Throwable) {
            pyLog("推送导入进度失败: $t")
        }
    }

    /** 共享目录（取 Python 侧的 upload_dir） */
    private fun shareRoot(): File? = try {
        val mod = Python.getInstance().getModule("leaffs_mobile")
        val s: String = mod.callAttr("status").toJava(String::class.java)
        JSONObject(s).optString("upload_dir").takeIf { it.isNotBlank() }?.let { File(it) }
    } catch (_: Throwable) {
        null
    }

    /** 从当前网页地址推断共享根内的相对目录（与网页上传用的 path 一致） */
    private fun currentRelativeDir(): String = try {
        val u = URL(lastUrl)
        val p = u.path ?: ""
        when {
            p.startsWith("/browse/") ->
                java.net.URLDecoder.decode(p.removePrefix("/browse/"), "UTF-8")
            p.startsWith("/gallery") -> {
                val q = u.query ?: ""
                Regex("(?:^|&)dir=([^&]*)").find(q)?.groupValues?.get(1)
                    ?.let { java.net.URLDecoder.decode(it, "UTF-8") } ?: ""
            }
            else -> ""
        }
    } catch (_: Throwable) {
        ""
    }

    /** 取选中文件的显示名（拿不到就返回 null，由调用方用兜底名） */
    private fun queryDisplayName(uri: Uri): String? = try {
        contentResolver.query(uri, null, null, null, null)?.use { c ->
            if (c.moveToFirst()) {
                val i = c.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
                if (i >= 0) c.getString(i)?.takeIf { it.isNotBlank() } else null
            } else {
                null
            }
        }
    } catch (_: Exception) {
        null
    }

    /** 重名不覆盖：xxx (1).png */
    private fun uniqueFile(dir: File, name: String): File {
        var t = File(dir, name)
        if (!t.exists()) return t
        val dot = name.lastIndexOf('.')
        val base = if (dot > 0) name.substring(0, dot) else name
        val ext = if (dot > 0) name.substring(dot) else ""
        var i = 1
        while (t.exists() && i < 1000) {
            t = File(dir, "$base ($i)$ext")
            i++
        }
        return t
    }

    @Suppress("DEPRECATION", "OVERRIDE_DEPRECATION")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQ_PICK_FILE && requestCode != REQ_PICK_FOLDER) return
        val prompt = pendingFile
        val result = pendingFileResult
        pendingFile = null
        pendingFileResult = null
        if (prompt == null || result == null) return
        pyLog("文件选择返回 resultCode=$resultCode " +
            "data=${data?.data} clip=${data?.clipData?.itemCount ?: 0}")
        if (resultCode != RESULT_OK || data == null) {
            result.complete(prompt.dismiss())
            return
        }
        try {
            // javadoc 要求传 Application context 来解析 URI（不是 Activity context）
            val appCtx = applicationContext
            if (requestCode == REQ_PICK_FOLDER) {
                val uri = data.data
                result.complete(
                    if (uri == null) prompt.dismiss() else prompt.confirm(appCtx, uri))
                return
            }
            val clip = data.clipData
            val picked = when {
                clip != null -> (0 until clip.itemCount).map { clip.getItemAt(it).uri }
                data.data != null -> listOf(data.data!!)
                else -> emptyList()
            }
            if (picked.isEmpty()) {
                result.complete(prompt.dismiss())
                return
            }
            // 不走内核的上传流程：原生直接把文件写进共享目录，随即让网页以为"取消了"
            //（网页不必再 POST 一遍，也就没有 content:// 解析不了的问题）
            pyLog("原生导入 ${picked.size} 个文件 → ${currentRelativeDir()}")
            importPickedFiles(picked)
            result.complete(prompt.dismiss())
        } catch (t: Throwable) {
            pyLog("选文件处理异常: $t")
            result.complete(prompt.dismiss())
        }
    }

    /** 从"后台保护失效"通知回到应用：Activity 还活着时不会重跑 onCreate，这里补拉起前台服务 */
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        LeafService.start(this)
    }

    override fun onDestroy() {
        // 清掉卡片回调：它是挂在 LfDialog（单例 object）上的 lambda，捕获着本 Activity ——
        // 不清就会泄漏，而且 Activity 没了之后卡片再关闭会对着已销毁的 window 设系统栏
        LfDialog.onCardClosed = null
        session.close()
        super.onDestroy()
    }

    /**
     * 证书错误页（data: URL），页面一加载就自己放行。
     *
     * 为什么不用 `about:neterror`：GeckoView 不走桌面那套错误页机制 —— 实测把它交给 onLoadError
     * 之后页面根本没有开始加载（没有 onPageStart/onPageStop，lastUrl 一直停在 about:blank），
     * 放行脚本于是落在空白文档上：那里没有 document.addCertException，既不 resolve 也不 reject，
     * 回执永远等不到，界面直接白屏。返回 null 也一样没有错误页。
     * 而 GeckoView 自己的错误页就是 **data: URL** —— bug 1553265 / D56974 明确写着
     * "GeckoView error pages are data URLs"，并为此让这些文档上暴露 document.addCertException。
     * 所以这里自己给出这个 data: URL 错误页。
     *
     * addCertException 的参数是 isTemporary：**传 false = 永久例外**，会写进 profile 的
     * cert_override.txt，放行一次长期有效（Firefox 那个「不再提示」勾选框传的就是 !checked）。
     * 传 true 只记在内存里，冷启动后例外消失、每次都要重放一遍。
     *
     * 结果一律用 leaffs:// 回执带回 Java 侧（onLoadRequest 拦），成功/失败/没有 API 都要有回音，
     * 否则放行成没成完全看不见。
     */
    private fun certErrorPage(next: String): String {
        val jsNext = next.replace("\\", "\\\\").replace("'", "\\'")
        val html = "<!DOCTYPE html><html lang=\"zh\"><head><meta charset=\"utf-8\">" +
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">" +
            "<title>正在信任本机证书</title><style>" +
            "html,body{height:100%;margin:0}" +
            "body{display:flex;align-items:center;justify-content:center;" +
            "background:#0f1116;color:#e7e9ef;" +
            "font:15px/1.7 -apple-system,system-ui,\"Segoe UI\",sans-serif}" +
            ".tip{opacity:.85;text-align:center;padding:0 24px}" +
            "</style></head><body><div class=\"tip\">正在信任本机服务证书…</div><script>" +
            "(function(){" +
            "var f=document.addCertException;" +
            "if(typeof f!=='function'){location.replace('leaffs://certfail/no-api');return}" +
            "try{f.call(document,false).then(" +
            "function(){location.replace('leaffs://certok/$jsNext')}," +
            "function(e){location.replace('leaffs://certfail/'+encodeURIComponent(String(e)))})}" +
            "catch(e){location.replace('leaffs://certfail/sync-'+encodeURIComponent(String(e)))}" +
            "})();" +
            "</script></body></html>"
        val b64 = android.util.Base64.encodeToString(
            html.toByteArray(Charsets.UTF_8), android.util.Base64.NO_WRAP
        )
        return "data:text/html;base64,$b64"
    }

    private fun runtime(): GeckoRuntime {
        var r = sRuntime
        if (r == null) {
            // consoleOutput：让页面里的 console.log 也进 logcat（排查注入问题用）
            val settings = org.mozilla.geckoview.GeckoRuntimeSettings.Builder()
                .consoleOutput(true)
                .build()
            r = GeckoRuntime.create(this, settings)
            sRuntime = r
        }
        return r
    }

    /**
     * 权限页是否正在显示。从系统设置授完权回来时 Activity 不会重建、onCreate 不再跑，
     * 靠这个标志在 onResume 里补一次判断（否则用户明明开了权限，回来还停在权限页）。
     */
    private var permissionPageShowing = false

    /** 启动流程是否已经跑起来了（防止权限回调与 onResume 各触发一次） */
    private var booting = false

    override fun onResume() {
        super.onResume()
        // 权限页还开着、权限却已经拿到了 → 直接进 app，不用用户再点一次
        if (permissionPageShowing && !needLocalNetworkPermission()) {
            bootAndOpen()
        }
        // 刚从系统设置回来：问一句后台权限开了没（用户自己确认才算数，不自动标记）
        if (bgReturnedFromSettings) {
            bgReturnedFromSettings = false
            withWebColors {
                LfDialog.confirm(
                    this, "后台运行",
                    "在系统设置里把「后台高耗电 / 自启动」打开了吗？",
                    okText = "已开启", cancelText = "还没",
                    onOk = {
                        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                            .putBoolean(KEY_BG_ASKED, true).apply()
                    }
                    // onCancel：没开就不标记，下次进应用再问
                )
            }
        }
    }

    private fun bootAndOpen() {
        // 授权后 onRequestPermissionsResult 与 onResume 可能各触发一次；
        // 放第二次进去的话 Python start() 会因为端口已占而报"启动失败"
        if (booting) return
        booting = true
        permissionPageShowing = false
        // 从权限页过来时转圈是隐藏的，这里恢复：转圈专指"服务启动中"
        progress.visibility = View.VISIBLE
        errorView.visibility = View.GONE
        showBootCover()
        Thread {
            var port = 8080
            var token = ""
            var err = ""
            try {
                val py = Python.getInstance()
                val mod = py.getModule("leaffs_mobile")
                // 服务起来之前先把本机记住的主题交给 Python：外壳与首屏取色才对
                // （之后网页一加载，扩展会上报当前值覆盖它）
                pushSavedTheme()
                // 安卓没有 ffmpeg：缩略图改由系统 API 生成（Python 侧注册后调用）
                try {
                    Python.getInstance().getModule("leaffs.utils.core")
                        .callAttr("set_native_thumbnailer", Thumbnailer())
                } catch (t: Throwable) {
                    pyLog("注册缩略图生成器失败: $t")
                }
                val res: String = mod.callAttr("start").toJava(String::class.java)
                val obj = JSONObject(res)
                if (obj.optBoolean("ok")) {
                    val st = JSONObject(mod.callAttr("status").toJava(String::class.java))
                    port = st.optInt("http_port", 8080)
                    token = st.optString("local_token", "")
                    // 服务端开着 TLS 时 8080 就是 HTTPS —— 拿 http:// 去连会握手失败，
                    // 表现为"启动成功但页面连不上/服务未就绪"
                    srvScheme = if (st.optBoolean("tls", false)) "https" else "http"
                    wsPort = st.optInt("ws_port", 8081)
                } else {
                    err = obj.optString("error", "服务启动失败")
                }
            } catch (t: Throwable) {
                err = t.message ?: t.toString()
            }
            if (err.isNotEmpty()) {
                runOnUiThread { showBootError(err) }
                return@Thread
            }
            waitPort(port, 15000)
            val base = "$srvScheme://127.0.0.1:$port"
            // 本机自动登录：带一次性令牌走 /login?leaf=（桌面 pywebview 同款机制）
            val url = if (token.isNotEmpty()) "$base/login?leaf=$token" else "$base/"
            runOnUiThread {
                // 这里不撤遮罩也不收转圈：8081 预热会先把那个 426 报错页画出来，
                // 露出来就是"每次启动闪一下"。等真正进到应用页面再撤（见 onPageStop）。
                errorView.visibility = View.GONE
                if (srvScheme == "https") {
                    // 自签证书的例外是按「主机 + 端口」记的，而 WebSocket 是页面 JS 自己发起的
                    // 连接、不经过 onLoadError —— 所以先给 8081 预热一次例外，放行后自动落到
                    // 真正的登录页（那时再放行 8080）。否则页面能开、WS 却一直"重连中"。
                    certNextUrl = url
                    certWarmupPending = true
                    session.loadUri("$srvScheme://127.0.0.1:$wsPort/")
                } else {
                    session.loadUri(url)
                }
                // 外壳（背景/进度条）也跟随网页当前主题与主色
                withWebColors { }
            }
        }.start()
    }

    /** 盖上启动遮罩（连同转圈一起显示，"服务启动中"） */
    private fun showBootCover() {
        bootCovering = true
        bootCover.visibility = View.VISIBLE
        progress.visibility = View.VISIBLE
    }

    /** 撤掉启动遮罩与转圈，露出 WebView */
    private fun hideBootCover() {
        bootCovering = false
        bootCover.visibility = View.GONE
        progress.visibility = View.GONE
    }

    /** 启动失败：居中显示原因，点击重试（不再白屏无反馈） */
    private fun showBootError(msg: String) {
        booting = false          // 允许点一下重试
        hideBootCover()
        errorView.text = "服务启动失败\n\n$msg\n\n（点这里重试）"
        errorView.visibility = View.VISIBLE
    }

    private fun retryBoot() {
        errorView.visibility = View.GONE
        bootAndOpen()   // 它会重新盖上遮罩、恢复转圈
    }

    /** 轮询等待 HTTP 端口就绪（GeckoView 加载前先探测，避免首屏报连接失败） */
    private fun waitPort(port: Int, timeoutMs: Long) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            try {
                Socket().use { s ->
                    s.connect(InetSocketAddress("127.0.0.1", port), 300)
                    return
                }
            } catch (_: Exception) {
                Thread.sleep(200)
            }
        }
    }

    // ---------- 文本选择菜单（接管内核的系统浮条） ----------

    /**
     * 继承官方 [BasicSelectionActionDelegate] 以保留剪贴板行为，但**不调用 super**，
     * 即不使用系统 ActionMode 浮条，改为弹 LeafFS 风格菜单（与网页主题一致）。
     */
    private inner class LeafSelectionActionDelegate(activity: Activity) :
        BasicSelectionActionDelegate(activity) {

        private var menu: Dialog? = null
        private var selection: GeckoSession.SelectionActionDelegate.Selection? = null

        override fun onShowActionRequest(
            session: GeckoSession,
            selection: GeckoSession.SelectionActionDelegate.Selection
        ) {
            this.selection = selection
            // 以内核实际给出的动作集为准（不依赖常量匹配，避免过滤成空集导致菜单不弹）
            val available: List<String> = try {
                selection.availableActions?.toList() ?: emptyList()
            } catch (_: Throwable) {
                emptyList()
            }
            pyLog("选择动作: $available flags=${selection.flags}")
            // 只保留有中文标签的用户动作；内核的内部动作（HIDE/UNSELECT 等）不展示
            val ordered = ORDERED_ACTIONS.filter { available.contains(it) }
            if (ordered.isEmpty()) {
                pyLog("无可用用户动作 → 不弹菜单")
                selection.hide()
                return
            }
            val labels = ordered.map { actionLabel(it) }
            // 选中内容预览（浮层顶部显示，像网页里的选中摘要）
            val preview: String? = try {
                selection.text?.takeIf { it.isNotBlank() }?.let {
                    if (it.length > 60) it.take(60) + "…" else it
                }
            } catch (_: Throwable) {
                null
            }
            // 选区在屏幕上的位置：浮层据此贴到选区旁边（不盖住整页，也不吃外部点击）
            val anchor = try { selection.screenRect } catch (_: Throwable) { null }
            menu?.dismiss()
            // 同步弹出，保证长按立即有反馈；配色用缓存值（后台会刷新）
            withWebColors {
                menu = LfDialog.popupMenu(this@MainActivity, anchor, preview, labels) { which ->
                    menu = null
                    this.selection = null
                    selection.execute(ordered[which])
                }
            }
        }

        override fun onHideAction(session: GeckoSession, reason: Int) {
            menu?.dismiss()
            menu = null
            selection = null
        }
    }

    private fun actionLabel(action: String): String = when (action) {
        GeckoSession.SelectionActionDelegate.ACTION_COPY -> "复制"
        GeckoSession.SelectionActionDelegate.ACTION_CUT -> "剪切"
        GeckoSession.SelectionActionDelegate.ACTION_PASTE -> "粘贴"
        GeckoSession.SelectionActionDelegate.ACTION_PASTE_AS_PLAIN_TEXT -> "粘贴为纯文本"
        GeckoSession.SelectionActionDelegate.ACTION_SELECT_ALL -> "全选"
        GeckoSession.SelectionActionDelegate.ACTION_DELETE -> "删除"
        GeckoSession.SelectionActionDelegate.ACTION_UNSELECT -> "取消选择"
        else -> action
    }

    /** 长按菜单：按元素类型给出「复制链接 / 在浏览器打开 / 复制图片地址 / 复制文字」 */
    private fun showContextMenu(
        element: GeckoSession.ContentDelegate.ContextElement,
        screenX: Int,
        screenY: Int
    ) {
        val labels = ArrayList<String>()
        val actions = ArrayList<() -> Unit>()

        val link = element.linkUri
        val src = element.srcUri
        // GeckoView 146 的字段名是 textContent（157 才改叫 linkText）
        val text = element.textContent ?: element.title ?: element.altText

        if (!link.isNullOrEmpty()) {
            labels += "复制链接"
            actions += { copyText(link) }
            // 2026-09-18：原来这里还有一项「在浏览器打开」（openExternal → ACTION_VIEW 甩给
            // 系统浏览器）。用户要求「软件内不允许访问外部链接」⇒ 整项删除，只留复制。
        }
        if (!src.isNullOrEmpty() && src != link) {
            labels += if (element.type == GeckoSession.ContentDelegate.ContextElement.TYPE_IMAGE)
                "复制图片地址" else "复制媒体地址"
            actions += { copyText(src) }
        }
        if (!text.isNullOrEmpty()) {
            labels += "复制文字"
            actions += { copyText(text) }
        }
        if (labels.isEmpty()) return

        // 长按的那一点就是锚：浮层贴着手指旁边弹，而不是盖住整页。
        // 内核这两个数是 CSS 像素，必须乘 density 折成物理像素，否则浮层整体偏上、
        // 越往下错得越多（实测：日志里 y=303，按钮实际在 1060 物理像素处）。
        val density = resources.displayMetrics.density
        val anchor = android.graphics.RectF(
            screenX * density, screenY * density,
            screenX * density, screenY * density
        )
        withWebColors {
            LfDialog.popupMenu(this, anchor, null, labels) { which -> actions[which]() }
        }
    }

    private fun copyText(s: String) {
        val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        cm.setPrimaryClip(ClipData.newPlainText("LeafFS", s))
        Toast.makeText(this, "已复制", Toast.LENGTH_SHORT).show()
    }

    /**
     * 只放行**本机服务**与内部 scheme —— 用户要求「软件内不允许访问外部链接」。
     *
     * 放行：`http(s)://127.0.0.1|localhost|[::1]`（本机服务，任意端口）、`leaffs://`（证书流程
     * 用的自定义 scheme）、`data:`（证书错误页就是 data: URL）、`about:`、`blob:`。
     * 其余一律拒绝 —— 包括 `https://外部站点`、`javascript:`、`file:`、各种 App scheme。
     *
     * 说明：这里只管**导航**。子资源（`<img src="外部">` 之类）GeckoView 没有公开的拦截
     * API，靠**服务端**的 sandbox CSP 兜（`send_raw` / `_share_stream_file` 都带
     * `sandbox; default-src 'none'; img-src data:`），而站内页面本身没有任何外链。
     */
    private fun isInternalUrl(uri: String): Boolean {
        val u = uri.trim()
        if (u.isEmpty()) return false
        if (u.startsWith("leaffs://")) return true
        if (u.startsWith("data:") || u.startsWith("about:") || u.startsWith("blob:")) return true
        val lower = u.lowercase()
        if (!lower.startsWith("http://") && !lower.startsWith("https://")) return false
        val host = lower.substringAfter("://").substringBefore('/')
            .substringBefore('?').substringBefore('#')
        val hostOnly = if (host.startsWith("[")) host.substringBefore(']') + "]" else host.substringBefore(':')
        return hostOnly == "127.0.0.1" || hostOnly == "localhost" || hostOnly == "[::1]"
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    /** API 28 及以下把文件写进公共下载目录需要 WRITE_EXTERNAL_STORAGE（29 起免申请） */
    private fun needStoragePermission(): Boolean =
        Build.VERSION.SDK_INT <= Build.VERSION_CODES.P &&
            (checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE)
                != android.content.pm.PackageManager.PERMISSION_GRANTED)

    private fun requestStoragePermissionIfNeeded() {
        if (!needStoragePermission()) return
        requestPermissions(
            arrayOf(android.Manifest.permission.WRITE_EXTERNAL_STORAGE), REQ_STORAGE)
    }

    /**
     * 局域网权限点按 API 分版本（VERSION_CODES 常量只到 Android 16，按数字判断）：
     *   API 37+  Android 17 起 = ACCESS_LOCAL_NETWORK（"本地网络"）
     *   API 36   Android 16 = NEARBY_WIFI_DEVICES（"附近的设备"）—— 16 起局域网访问被
     *            RESTRICT_LOCAL_NETWORK 拦，但 36 上系统还不认识 ACCESS_LOCAL_NETWORK
     *            （dumpsys 里查无此权限），只声明那个等于没声明；实测漏了它就会出现
     *            "服务在跑、本机能打开、局域网里别的设备连不上"
     *   API 35-  没有局域网限制，不要申请（NEARBY_WIFI_DEVICES 在 13~15 上只管 Wi-Fi
     *            扫描，跟局域网访问无关，申请了就是凭空多一个授权框）
     */
    private fun localNetworkPermission(): String? = when {
        Build.VERSION.SDK_INT >= 37 -> "android.permission.ACCESS_LOCAL_NETWORK"
        Build.VERSION.SDK_INT >= 36 -> android.Manifest.permission.NEARBY_WIFI_DEVICES
        else -> null
    }

    /** 权限在系统设置里的中文名（提示文案用） */
    private fun localNetworkLabel(): String =
        if (Build.VERSION.SDK_INT >= 37) "本地网络" else "附近的设备"

    private fun needLocalNetworkPermission(): Boolean {
        val perm = localNetworkPermission() ?: return false
        return checkSelfPermission(perm) != android.content.pm.PackageManager.PERMISSION_GRANTED
    }

    /**
     * 本权限申请过没有。安卓在"禁止弹窗之后"和"从没申请过"两种情况下的
     * shouldShowRequestPermissionRationale() 都是 false，光靠它分不清，所以自己记一笔。
     */
    private fun localNetAskedBefore(): Boolean =
        getSharedPreferences(PREFS, MODE_PRIVATE).getBoolean(KEY_LOCAL_NET_ASKED, false)

    private fun markLocalNetAsked() {
        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
            .putBoolean(KEY_LOCAL_NET_ASKED, true).apply()
    }

    private fun requestLocalNetworkPermissionIfNeeded() {
        val perm = localNetworkPermission() ?: return
        if (checkSelfPermission(perm) == android.content.pm.PackageManager.PERMISSION_GRANTED) return
        // 申请过、系统又不再弹框（用户选了"不再询问"）→ requestPermissions 会静默失败，
        // 用户看到的就是"点了没反应"；这种情况直接跳系统设置，那才是唯一的出路
        if (localNetAskedBefore() && !shouldShowRequestPermissionRationale(perm)) {
            openAppSettings()
            return
        }
        markLocalNetAsked()
        requestPermissions(arrayOf(perm), REQ_LOCAL_NET)
    }

    /**
     * 局域网权限说明页：用 data: URL 展示（跟加载失败页同一套做法，不依赖服务先起来），
     * 页面里按钮是 leaffs:// 链接，由 onLoadRequest 拦下来 —— 这样就不用手写
     * 原生卡片，滚动/换行/长文案全归浏览器，样式也与网页同源。
     * 安全区由内置扩展注入（manifest 的 matches 已含 data:），这里再拼一层兜底。
     *
     * @param denied 上一次申请被拒（或系统已经不再弹框）时为 true：页面会多一段说明和
     *               「去设置」按钮 —— 安卓只允许弹有限的几次，之后只能去系统设置手动开。
     */
    private fun showPermissionPage(denied: Boolean = false) {
        permissionPageShowing = true
        // 转圈是"服务启动中"的指示：权限页本身是完整页面，让它一直转着像卡住了
        // 遮罩也要撤：权限页就画在 GeckoView 里，盖着就什么都看不见
        hideBootCover()
        errorView.visibility = View.GONE
        val label = localNetworkLabel()
        val since = if (Build.VERSION.SDK_INT >= 37) "Android 17" else "Android 16"
        val built = try {
            val mod = Python.getInstance().getModule("leaffs_mobile")
            // 权限页在网页加载之前就要显示：先把本机记住的主题推给 Python，页面配色才对
            pushSavedTheme()
            mod.callAttr("permission_page", label, since, denied).toJava(String::class.java)
        } catch (_: Throwable) {
            ""
        }
        var html = if (built.isNullOrEmpty()) {
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\">" +
                "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">" +
                "</head><body style=\"font:14px sans-serif;padding:24px;text-align:center\">" +
                "<p>LeafFS 需要「$label」权限，否则局域网里别的设备连不上这台手机。</p>" +
                "<p><a href=\"leaffs://grant\">去授权</a> · <a href=\"leaffs://skip\">先不用</a></p>" +
                "</body></html>"
        } else built
        val safe = safeAreaStyle()
        html = if (html.contains("</head>")) html.replace("</head>", safe + "</head>")
               else safe + html
        val b64 = android.util.Base64.encodeToString(
            html.toByteArray(Charsets.UTF_8), android.util.Base64.NO_WRAP)
        session.loadUri("data:text/html;base64,$b64")
    }

    /** 跳到本应用的系统设置页（权限被"不再询问"后，只能从那里手动开） */
    private fun openAppSettings() {
        try {
            startActivity(Intent(
                android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                Uri.fromParts("package", packageName, null)))
        } catch (e: Exception) {
            pyLog("打开应用设置页失败: $e")
        }
    }

    /** 等存储权限期间挂起的这次下载（授权后自动继续，不必再点一次） */
    private var pendingAction: (() -> Unit)? = null
    private var pendingBody: WebResponse? = null

    /** 老系统先拿存储权限：挂起本次下载，授权后自动接着存 */
    private fun withStoragePermission(action: () -> Unit, response: WebResponse) {
        if (!needStoragePermission()) {
            action()
            return
        }
        pendingAction = action
        pendingBody = response
        runOnUiThread { requestStoragePermissionIfNeeded() }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_LOCAL_NET) {
            // 拿到就照常启动；没拿到就回权限页把话说清楚 —— 安卓只允许弹有限几次，
            // 弹不动了只能去系统设置手动开，页面里有「去设置」和「先不用」两个出口
            if (grantResults.isNotEmpty() &&
                grantResults[0] == android.content.pm.PackageManager.PERMISSION_GRANTED) {
                bootAndOpen()
            } else {
                showPermissionPage(denied = true)
            }
            return
        }
        if (requestCode != REQ_STORAGE) return
        val action = pendingAction
        val body = pendingBody
        pendingAction = null
        pendingBody = null
        if (action == null) return
        if (grantResults.isNotEmpty() &&
            grantResults[0] == android.content.pm.PackageManager.PERMISSION_GRANTED) {
            action()
        } else {
            try { body?.body?.close() } catch (_: Exception) {}
            Toast.makeText(this, "未授予存储权限，无法保存到手机下载目录", Toast.LENGTH_LONG).show()
        }
    }

    /**
     * 网页下载：内核那次请求已经拿到完整响应（同一 URL、同一份 Cookie，服务端记的也是这一次），
     * 直接把响应流写进手机下载目录 —— **不再让系统下载器重发第二个请求**
     *（重发会丢 Cookie，老系统上还会卡在 DownloadProvider 的目标路径校验）。
     */
    private fun startDownload(response: WebResponse) {
        withStoragePermission({ saveResponse(response) }, response)
    }

    /** 后台线程落盘，完成后提示（大文件不阻塞界面） */
    private fun saveResponse(response: WebResponse) {
        val name = guessFilename(response)
        runOnUiThread { Toast.makeText(this, "开始下载: $name", Toast.LENGTH_SHORT).show() }
        Thread {
            val msg = try {
                writeToDownloads(response, name)
                "已保存到手机下载目录: $name"
            } catch (t: Throwable) {
                "下载失败: ${t.message}"
            } finally {
                try { response.body?.close() } catch (_: Exception) {}
            }
            runOnUiThread { Toast.makeText(this, msg, Toast.LENGTH_LONG).show() }
        }.start()
    }

    /** 写盘：API 29 起走 MediaStore（免权限），更早的系统直接写公共下载目录 */
    private fun writeToDownloads(response: WebResponse, name: String) {
        val input = response.body ?: throw IllegalStateException("响应内容为空")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, name)
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            val uri = contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: throw IllegalStateException("无法在下载目录创建文件")
            try {
                contentResolver.openOutputStream(uri)?.use { out -> input.copyTo(out) }
                    ?: throw IllegalStateException("无法写入下载目录")
                values.clear()
                values.put(MediaStore.Downloads.IS_PENDING, 0)
                contentResolver.update(uri, values, null, null)
            } catch (t: Throwable) {
                try { contentResolver.delete(uri, null, null) } catch (_: Exception) {}
                throw t
            }
            return
        }
        @Suppress("DEPRECATION")
        val dir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
        if (!dir.exists()) dir.mkdirs()
        var target = File(dir, name)
        if (target.exists()) {
            // 重名不覆盖：xxx (1).png
            val dot = name.lastIndexOf('.')
            val base = if (dot > 0) name.substring(0, dot) else name
            val ext = if (dot > 0) name.substring(dot) else ""
            var i = 1
            while (target.exists() && i < 1000) {
                target = File(dir, "$base ($i)$ext")
                i++
            }
        }
        FileOutputStream(target).use { out -> input.copyTo(out) }
    }

    /** 文件名清洗：解码 %XX + 去掉路径分隔符与非法字符（防越界、防写失败） */
    private fun sanitizeName(raw: String): String {
        val decoded = try { Uri.decode(raw) } catch (_: Exception) { raw }
        val cleaned = decoded
            .replace(Regex("[\\\\/:*?\"<>|]"), "_")
            .replace(Regex("[\\x00-\\x1F]"), "")
            .trim()
            .trim('.')
        return cleaned.ifEmpty { "leaffs_download" }
    }

    /** 从 Content-Disposition 或 URL 末尾提取文件名 */
    private fun guessFilename(response: WebResponse): String {
        // Content-Disposition: attachment; filename*=UTF-8''xxx 或 filename="xxx"
        val cd = response.headers?.entries?.firstOrNull {
            it.key.equals("Content-Disposition", ignoreCase = true)
        }?.value
        if (!cd.isNullOrEmpty()) {
            Regex("filename\\*?=(?:UTF-8'')?[\"']?([^\";]+)[\"']?", RegexOption.IGNORE_CASE)
                .find(cd)?.groupValues?.get(1)?.let {
                    if (it.isNotEmpty()) return sanitizeName(it.trim())
                }
        }
        // 回退 URL 路径最后一段
        val raw = try {
            val p = URL(response.uri).path ?: ""
            p.substringAfterLast('/')
        } catch (_: Exception) {
            ""
        }
        return sanitizeName(raw)
    }

    companion object {
        private var sRuntime: GeckoRuntime? = null
        private const val REQ_NOTIF = 1001
        private const val REQ_PICK_FILE = 1002
        private const val REQ_PICK_FOLDER = 1003
        private const val REQ_STORAGE = 1004
        private const val REQ_LOCAL_NET = 1005
        /** 自己记"局域网权限申请过没有"：安卓在"不再询问"和"从没申请过"时
         *  shouldShowRequestPermissionRationale() 都是 false，分不清 */
        private const val PREFS = "leaffs_prefs"
        private const val KEY_LOCAL_NET_ASKED = "local_net_asked"
        /** 网页上报的主题（亮暗 + 主色）：原生读不到网页的 cookie，靠扩展上报后记在这里，
         *  供权限页/错误页/原生控件在网页加载前取色 */
        private const val KEY_THEME_DARK = "theme_dark"
        private const val KEY_THEME_ACCENT = "theme_accent"

        /** 后台运行权限引导是否已经问过（问过就不再弹） */
        private const val KEY_BG_ASKED = "bg_run_asked"

        /** 内置扩展（assets/insets）：给页面注入系统栏安全区，让背景铺满而正文避开 */
        private const val INSETS_EXTENSION = "resource://android/assets/insets/"

        /** 内置扩展（assets/netguard）：只放行本机的网络请求，其余一律取消（补子资源那层） */
        private const val NETGUARD_EXTENSION = "resource://android/assets/netguard/"

        /** 文本选择菜单的显示顺序（只保留对用户有意义的动作；
         *  内核还会给出 HIDE / UNSELECT / COLLAPSE_* 等内部动作，一律不展示） */
        private val ORDERED_ACTIONS = listOf(
            GeckoSession.SelectionActionDelegate.ACTION_CUT,
            GeckoSession.SelectionActionDelegate.ACTION_COPY,
            GeckoSession.SelectionActionDelegate.ACTION_PASTE,
            GeckoSession.SelectionActionDelegate.ACTION_SELECT_ALL,
            GeckoSession.SelectionActionDelegate.ACTION_DELETE,
        )
    }
}
