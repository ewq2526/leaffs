package com.leaffs.mobile

import android.app.Activity
import android.app.Dialog
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.RectF
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.LayerDrawable
import android.os.Build
import android.view.Gravity
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.view.Window
import android.view.WindowManager
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import org.json.JSONObject

/**
 * LeafFS 风格对话框：圆角卡片 + 主色按钮，替代系统 AlertDialog。
 *
 * 内核（GeckoView）触发的原生 UI —— <select> 下拉、confirm/prompt/alert、长按菜单 ——
 * 全部走这里。
 *
 * 配色与网页**完全同源**：Python 侧解析网页 style.css 并按当前账号的
 * 「亮/暗 + 自选主色」算出网页此刻真实生效的颜色（leaffs/ui_theme.py），
 * 通过 [setWebColors] 传入；拿不到时回退到 res/values 的内置配色。
 */
object LfDialog {

    /** 网页当前配色（Python 解析 style.css 得到）；null = 用内置资源色 */
    private var web: JSONObject? = null

    /**
     * 卡片关闭后回调（MainActivity 注册）。
     *
     * 卡片窗口退出后，系统栏观感可能停在卡片那一版、不再跟网页主题 —— 关掉时让
     * 主窗口把它自己的值重设一遍。
     */
    var onCardClosed: (() -> Unit)? = null

    fun setWebColors(colors: JSONObject?) {
        web = colors
    }

    /** 取网页配色（#AARRGGBB，保留透明度）；缺失或异常时用回退值 */
    private fun colorOf(key: String, fallback: Int): Int {
        val v = web?.optString(key, "") ?: ""
        if (v.isNotEmpty()) {
            try {
                return Color.parseColor(v)
            } catch (_: IllegalArgumentException) {
                // 值异常则回退
            }
        }
        return fallback
    }

    private fun color(activity: Activity, key: String, fallbackRes: Int): Int =
        colorOf(key, activity.getColor(fallbackRes))

    /**
     * 卡片取色：**反主题** —— 亮主题用暗卡、暗主题用亮卡。
     *
     * 那是唯一把它从网页里拎出来的东西（遮罩是纯透明的，不压暗）；值由 Python 从同一份
     * style.css 里取另一个主题的变量（`cardPalette`），拿不到时退回当前主题的同名色。
     */
    private fun cardColorOf(key: String, fallback: Int): Int {
        val v = web?.optJSONObject("cardPalette")?.optString(key, "") ?: ""
        if (v.isNotEmpty()) {
            try {
                return Color.parseColor(v)
            } catch (_: IllegalArgumentException) {
                // 值异常则回退
            }
        }
        return colorOf(key, fallback)
    }

    private fun cardColor(activity: Activity, key: String, fallbackRes: Int): Int =
        cardColorOf(key, activity.getColor(fallbackRes))

    private fun tint(view: View?, color: Int) {
        view?.backgroundTintList = ColorStateList.valueOf(color)
    }

    /** 只改透明度、保留 RGB */
    private fun withAlpha(color: Int, alpha: Float): Int =
        Color.argb((alpha * 255).toInt().coerceIn(0, 255),
            Color.red(color), Color.green(color), Color.blue(color))

    /** 保证最低不透明度：低于下限时抬到下限（网页的半透明值在原生上会显得太透） */
    private fun ensureMinAlpha(color: Int, minAlpha: Float): Int =
        if (Color.alpha(color) / 255f >= minAlpha) color else withAlpha(color, minAlpha)

    /** 网页里的高光色 alpha 很低（0.1 左右），在原生卡片上几乎看不出；给个可见下限 */
    private fun visible(color: Int): Int {
        val a = Color.alpha(color)
        return if (a >= 56) color else withAlpha(color, 56f / 255f)
    }

    /**
     * 卡片背景 = 半透明玻璃底 + 4 个边缘高光径向渐变。
     *
     * 四条渐变 1:1 对应网页 .modal-content 的四条 radial-gradient
     * （`130% 100% at 6% -12%` / `120% 95% at 100% -8%` / `110% 90% at 50% 118%` / `80% 70% at 100% 110%`）。
     * Android 只能画正圆渐变，所以把网页的椭圆按短半径折算，保证渐变落在卡片内看得见。
     * width/height 为 0 时用屏宽估算（布局完成前先垫一层，随后会用真实尺寸重算）。
     */
    private fun cardBackground(
        activity: Activity,
        corner: Float,
        fill: Int,
        width: Int,
        height: Int
    ): LayerDrawable {
        val metrics = activity.resources.displayMetrics
        val w = (if (width > 0) width else (metrics.widthPixels * 0.9f)).toFloat()
        val h = (if (height > 0) height else dp(activity, 200).toFloat()).toFloat()

        val base = GradientDrawable().apply {
            cornerRadius = corner
            setColor(fill)                                   // 半透明玻璃（保留 alpha，网页可见）
            setStroke(dp(activity, 1), cardColorOf("border", 0x4D8094D0))
        }
        val layers = arrayListOf<android.graphics.drawable.Drawable>(base)
        layers += radial(activity, visible(cardColorOf("edge1", 0x1F0369A1)), 0.06f, -0.12f, 1.30f, 1.00f, w, h, corner)
        layers += radial(activity, visible(cardColorOf("edge2", 0x1A4696DC)), 1.00f, -0.08f, 1.20f, 0.95f, w, h, corner)
        layers += radial(activity, visible(cardColorOf("edge3", 0x1F0369A1)), 0.50f, 1.18f, 1.10f, 0.90f, w, h, corner)
        layers += radial(activity, visible(cardColorOf("edge2", 0x1A4696DC)), 1.00f, 1.10f, 0.80f, 0.70f, w, h, corner)
        return LayerDrawable(layers.toTypedArray())
    }

    /**
     * 一个径向高光图层（对应网页卡片的一条 radial-gradient 边缘反光）。
     * cx/cy 为相对卡片坐标（可超出 0..1，即光源在卡片之外）；
     * rw/rh 是网页给的水平/垂直半径比例（相对卡片尺寸）。
     */
    private fun radial(
        activity: Activity,
        color: Int,
        cx: Float,
        cy: Float,
        rw: Float,
        rh: Float,
        width: Float,
        height: Float,
        corner: Float
    ): android.graphics.drawable.Drawable =
        GradientDrawable().apply {
            gradientType = GradientDrawable.RADIAL_GRADIENT
            setGradientCenter(cx, cy)
            // 网页是椭圆渐变，Android 只有正圆：取较短半径，避免渐变被摊平成一整片（那样看着就是纯色）
            gradientRadius = minOf(width * rw, height * rh).coerceAtLeast(1f)
            colors = intArrayOf(color, Color.TRANSPARENT)
            cornerRadius = corner
        }

    private class D(val dialog: Dialog, val view: View)

    private fun activityOf(view: View): Activity = view.context as Activity

    private fun dp(activity: Activity, v: Int): Int =
        (v * activity.resources.displayMetrics.density).toInt()

    /**
     * 按网页 .modal-content 的规则搭建对话框：
     *   · **全屏透明遮罩**（根视图，铺满整屏含系统栏那条），点卡片外空白关闭
     *   · 卡片 = 半透明玻璃 + 圆角 + 1dp 边框 + 边缘高光，配色一律取**反主题**
     *     （亮主题暗卡 / 暗主题亮卡）—— 遮罩不压暗，辨识度全靠这套明暗反差
     */
    private fun build(activity: Activity): D {
        val dialog = Dialog(activity, R.style.Theme_LeafFS)
        dialog.setOnDismissListener { onCardClosed?.invoke() }
        dialog.requestWindowFeature(Window.FEATURE_NO_TITLE)
        val view = LayoutInflater.from(activity).inflate(R.layout.lf_dialog, null)
        dialog.setContentView(view)
        dialog.window?.setBackgroundDrawableResource(android.R.color.transparent)
        dialog.window?.setLayout(ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT)
        // 铺满整屏（含系统栏那条）：根视图就是那层透明遮罩，必须盖到边 ——
        // 窗口默认避让系统栏（上下各差一条），遮罩压在框里就会在状态栏/导航栏处各露一条缝。
        dialog.window?.addFlags(WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS)
        // 系统 dim 也关掉：对话框背后就是网页原样，不压暗（辨识度由卡片的反主题配色承担）
        dialog.window?.setDimAmount(0f)

        // 遮罩保持**全透明**（原来那层带颜色的压暗遮罩已删，删掉才不会露上面那两条缝），
        // 只负责接"点空白关闭"。
        // 点空白 = 取消，必须走 cancel() 而不是 dismiss()：dismiss() 不触发
        // OnCancelListener，而 confirm/alert/prompt 的返回值全靠那个回调送达 ——
        // 用 dismiss() 会让网页的 confirm() 永远等不到结果，整页就卡死了。
        view.setOnClickListener { dialog.cancel() }

        val radius = web?.optInt("radius", 0)?.takeIf { it > 0 } ?: 18
        val corner = dp(activity, radius).toFloat()

        // 卡片本身：取**反主题**的 --card-bg（亮主题暗卡 / 暗主题亮卡），
        // 并保证至少 85% 实心 —— 网页那层 60% 左右是叠在 backdrop-filter 模糊之上的，
        // 原生没有那层模糊，照抄会显得发飘，所以抬到 85% 下限。
        val fill = ensureMinAlpha(cardColorOf("cardRaw", 0x9EFFFFFF.toInt()), 0.85f)

        val cardView = view.findViewById<LinearLayout>(R.id.lfCard)
        cardView.elevation = dp(activity, 12).toFloat()
        // 吃掉卡片上的点击，避免穿透到根视图把对话框关掉
        cardView.setOnClickListener { }
        // 先铺一层（此时还不知道卡片尺寸），布局完成后再按真实尺寸重算渐变 ——
        // 否则半径只能拿屏宽估，半径大于卡片时整张卡被均匀染色，看着就是纯色。
        cardView.background = cardBackground(activity, corner, fill, 0, 0)
        cardView.post {
            cardView.background = cardBackground(activity, corner, fill,
                cardView.width, cardView.height)
        }
        // 键盘弹出时把卡片抬起来：窗口铺满后系统不再做 `adjust=pan` 平移，居中的卡片会被
        // 键盘压住（prompt 的输入框就在卡片里）。只在真会被挡时才动，并且**贴着键盘上沿**
        // 留一小道缝 —— 统一抬半个键盘高度的话，上面会空出一截（卡片越高越难看）。
        view.setOnApplyWindowInsetsListener { v, insets ->
            val ime = androidx.core.view.WindowInsetsCompat
                .toWindowInsetsCompat(insets, v)
                .getInsets(androidx.core.view.WindowInsetsCompat.Type.ime()).bottom
            val gapPx = dp(activity, 12).toFloat()
            val h = v.height
            val cardH = if (cardView.height > 0) cardView.height else cardView.measuredHeight
            if (ime > 0 && h > 0 && cardH > 0) {
                val cardBottom = (h + cardH) / 2f            // 居中时卡片的底边
                val limit = h - ime - gapPx                  // 键盘上沿（留一道缝）
                cardView.translationY = if (cardBottom > limit) limit - cardBottom else 0f
            } else {
                cardView.translationY = 0f
            }
            insets
        }

        return D(dialog, view)
    }

    private fun title(view: View, t: String?) {
        val tv = view.findViewById<TextView>(R.id.lfTitle)
        tv.setTextColor(cardColor(activityOf(view), "text", R.color.lf_text))
        if (t.isNullOrEmpty()) {
            tv.visibility = View.GONE
        } else {
            tv.text = t
            tv.visibility = View.VISIBLE
        }
    }

    private fun message(view: View, m: String?) {
        val tv = view.findViewById<TextView>(R.id.lfMessage)
        tv.setTextColor(cardColor(activityOf(view), "textSecondary", R.color.lf_text_secondary))
        if (m.isNullOrEmpty()) {
            tv.visibility = View.GONE
        } else {
            tv.text = m
            tv.visibility = View.VISIBLE
        }
    }

    private fun button(view: View, id: Int, label: String?, primary: Boolean, onClick: () -> Unit) {
        val tv = view.findViewById<TextView>(id)
        if (label == null) {
            tv.visibility = View.GONE
            return
        }
        val activity = activityOf(view)
        if (primary) {
            tv.setTextColor(Color.WHITE)
            tint(tv, color(activity, "primary", R.color.lf_primary))
        } else {
            tv.setTextColor(cardColor(activity, "textSecondary", R.color.lf_text_secondary))
            tv.backgroundTintList = ColorStateList.valueOf(
                cardColor(activity, "border", R.color.lf_border))
        }
        tv.visibility = View.VISIBLE
        tv.text = label
        tv.setOnClickListener { onClick() }
    }

    /** 列表内容（单选/多选共用）；超过 8 项时限高可滚动 */
    private fun listContent(
        activity: Activity,
        items: List<String>,
        checked: BooleanArray,
        multi: Boolean,
        onPick: (Int) -> Unit
    ): View {
        val textColor = cardColor(activity, "text", R.color.lf_text)
        val checkColor = color(activity, "primary", R.color.lf_primary)
        val column = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        items.forEachIndexed { i, label ->
            val row = LayoutInflater.from(activity)
                .inflate(R.layout.lf_list_item, column, false)
            row.findViewById<TextView>(R.id.lfItemText).apply {
                text = label
                setTextColor(textColor)
            }
            val mark = row.findViewById<TextView>(R.id.lfItemCheck)
            mark.setTextColor(checkColor)
            mark.visibility = if (checked[i]) View.VISIBLE else View.INVISIBLE
            row.setOnClickListener {
                if (multi) {
                    checked[i] = !checked[i]
                    mark.visibility = if (checked[i]) View.VISIBLE else View.INVISIBLE
                }
                onPick(i)
            }
            column.addView(row)
        }
        val scroll = ScrollView(activity)
        scroll.addView(column)
        val h = if (items.size > 8) dp(activity, 320)
        else ViewGroup.LayoutParams.WRAP_CONTENT
        scroll.layoutParams = FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, h)
        return scroll
    }

    private fun content(view: View, child: View) {
        view.findViewById<FrameLayout>(R.id.lfContent).addView(child)
    }

    /** 单选列表（网页 <select>）：点中一项即确定 */
    fun singleList(
        activity: Activity,
        titleText: String?,
        items: List<String>,
        selected: Int,
        onCancel: () -> Unit = {},
        onPick: (Int) -> Unit
    ) {
        val d = build(activity)
        title(d.view, titleText)
        val checked = BooleanArray(items.size) { it == selected }
        content(d.view, listContent(activity, items, checked, multi = false) { i ->
            d.dialog.dismiss()
            onPick(i)
        })
        button(d.view, R.id.lfCancel, "取消", primary = false) {
            d.dialog.dismiss()
            onCancel()
        }
        // 点空白/返回键也算取消：不回调的话网页那边的 PromptResult 永远等不到结果
        d.dialog.setOnCancelListener { onCancel() }
        d.dialog.show()
    }

    /** 多选列表：确定后回调所有选中下标 */
    fun multiList(
        activity: Activity,
        titleText: String?,
        items: List<String>,
        selected: List<Int>,
        onCancel: () -> Unit = {},
        onOk: (List<Int>) -> Unit
    ) {
        val d = build(activity)
        title(d.view, titleText)
        val checked = BooleanArray(items.size) { selected.contains(it) }
        content(d.view, listContent(activity, items, checked, multi = true) { })
        button(d.view, R.id.lfCancel, "取消", primary = false) {
            d.dialog.dismiss()
            onCancel()
        }
        button(d.view, R.id.lfOk, "确定", primary = true) {
            d.dialog.dismiss()
            onOk(items.indices.filter { checked[it] })
        }
        // 同上：点空白/返回键必须把"取消"送出去，否则网页卡死
        d.dialog.setOnCancelListener { onCancel() }
        d.dialog.show()
    }

    /** 菜单（长按元素 / 文本选择）：无按钮，点一项或点外部关闭；返回句柄便于外部关闭 */
    fun menu(
        activity: Activity,
        titleText: String?,
        items: List<String>,
        preview: String? = null,
        onPick: (Int) -> Unit
    ): Dialog {
        val d = build(activity)
        title(d.view, titleText)
        message(d.view, preview)
        val checked = BooleanArray(items.size)
        content(d.view, listContent(activity, items, checked, multi = false) { i ->
            d.dialog.dismiss()
            onPick(i)
        })
        d.dialog.show()
        return d.dialog
    }

    /**
     * 长按浮层菜单（内核长按选中文本）。
     *
     * 和 [menu] 的区别是卡片**贴着锚点摆**（见 [placePopup]），不是居中盖住整页。
     *
     * 窗口铺满整屏、里面是一层透明遮罩 + 浮在上面的卡片：卡片外的点击由遮罩接住并关掉卡片。
     * 那一下**不再穿透给网页** —— 早先用 `FLAG_NOT_TOUCH_MODAL` 让系统转发，实测不生效。
     *
     * 卡片外观（玻璃底、圆角、边框、边缘高光、主题色）沿用 [build] 那一套，风格不变。
     *
     * @param anchor 选区在**屏幕**坐标系里的位置（GeckoView 的 `Selection.screenRect`）；
     *               传 null 时退回屏幕底部居中
     */
    fun popupMenu(
        activity: Activity,
        anchor: RectF?,
        preview: String?,
        items: List<String>,
        onPick: (Int) -> Unit
    ): Dialog {
        val d = buildPopup(activity)
        message(d.view, preview)
        content(d.view, popupContent(activity, items) { i ->
            d.dialog.dismiss()
            onPick(i)
        })
        placePopup(activity, d, anchor)
        return d.dialog
    }

    /**
     * 浮层里的动作：**全部并排成一行**（不折行、不换行），每项紧贴自己的文字。
     *
     * 长按浮层要的是"指尖旁边一小条"，不是一块面板 —— 项少时（复制/全选）就是一行小横条；
     * 项多时靠外层给的最大宽度把它压住，而不是折行堆高。
     */
    private fun popupContent(activity: Activity, items: List<String>, onPick: (Int) -> Unit): View {
        val textColor = cardColor(activity, "text", R.color.lf_text)
        val row = LinearLayout(activity).apply { orientation = LinearLayout.HORIZONTAL }
        items.forEachIndexed { i, label ->
            row.addView(TextView(activity).apply {
                text = label
                textSize = 14f
                maxLines = 1
                ellipsize = android.text.TextUtils.TruncateAt.END
                setTextColor(textColor)
                background = activity.getDrawable(R.drawable.lf_item_bg)
                setPadding(dp(activity, 12), dp(activity, 8), dp(activity, 12), dp(activity, 8))
                layoutParams = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT
                ).apply { if (i > 0) marginStart = dp(activity, 4) }
                setOnClickListener { onPick(i) }
            })
        }
        return row
    }

    /**
     * 浮层的骨架：全屏透明遮罩 + 浮在上面的卡片。
     *
     * 不复用 [build]：那套卡片是居中的，浮层得贴着锚点摆（见 [placePopup]）。
     */
    private fun buildPopup(activity: Activity): D {
        val dialog = Dialog(activity, R.style.Theme_LeafFS)
        dialog.setOnDismissListener { onCardClosed?.invoke() }
        dialog.requestWindowFeature(Window.FEATURE_NO_TITLE)
        // 内容 = 全屏透明遮罩 + 浮在上面的卡片：遮罩把窗口撑满（到整屏可用区），
        // 并负责接住"卡片外"的点击 —— 点一下关掉卡片
        val card = LayoutInflater.from(activity).inflate(R.layout.lf_popup, null)
        val view = FrameLayout(activity).apply {
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
            // 必须显式给 wrap_content：FrameLayout 给子 View 的默认参数是 MATCH_PARENT，
            // 不写就会被撑成整屏（巨型卡片）
            addView(card, FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT))
            setOnClickListener { dialog.dismiss() }
        }
        dialog.setContentView(view)
        dialog.window?.setBackgroundDrawableResource(android.R.color.transparent)
        dialog.window?.setDimAmount(0f)
        // 卡片外的点击由外层的透明遮罩接住（见 placePopup），不靠系统转发。
        // NOT_FOCUSABLE 保留：不聚焦 → 不当"决定系统栏观感的那个窗口"。
        dialog.window?.addFlags(
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
                WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH
        )
        dialog.setCanceledOnTouchOutside(true)

        val radius = web?.optInt("radius", 0)?.takeIf { it > 0 } ?: 18
        val corner = dp(activity, radius).toFloat()
        val fill = ensureMinAlpha(cardColorOf("cardRaw", 0x9EFFFFFF.toInt()), 0.85f)
        val cardView = view.findViewById<LinearLayout>(R.id.lfCard)
        cardView.elevation = dp(activity, 12).toFloat()
        // 吃掉卡片自身的点击："卡片外"由外层遮罩负责关掉（见 buildPopup / placePopup），
        // 不隔开的话点卡片里的空白也会冒泡上去把卡片关掉
        cardView.setOnClickListener { }
        cardView.background = cardBackground(activity, corner, fill, 0, 0)
        cardView.post {
            cardView.background = cardBackground(activity, corner, fill,
                cardView.width, cardView.height)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            dialog.window?.addFlags(WindowManager.LayoutParams.FLAG_BLUR_BEHIND)
            dialog.window?.setBackgroundBlurRadius(dp(activity, web?.optInt("blur", 18) ?: 18))
        }
        return D(dialog, view)
    }

    /**
     * 把浮层一次摆到位（显示之前就算好位置）。
     *
     * 坐标系：给窗口加 `FLAG_LAYOUT_IN_SCREEN` 后，窗口坐标原点就是**物理屏幕左上角**，
     * 和 `Selection.screenRect` / `onContextMenu` 的 screenX,screenY 是同一套坐标 ——
     * 于是可以直接拿锚点坐标当 x/y。
     * （早先的写法是"先透明显示、再量基准点校正"，那会在到位之前先露一段错误位置，
     * 看着就是弹出来一小会儿才跳到位。）
     *
     * 卡片尺寸在 `show()` 之前用 `measure()` 量出来：既要据此决定贴锚点上方还是下方，
     * 也要用它把位置收进屏幕内。
     */
    private fun placePopup(activity: Activity, d: D, anchor: RectF?) {
        val win = d.dialog.window ?: return
        // 窗口必须铺满：遮罩要盖住整屏才能接住"卡片外"的点击（窗口小 → 遮罩也只有那么大）
        win.addFlags(WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or
            WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS)
        win.setLayout(
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.MATCH_PARENT
        )
        val attrs = win.attributes
        attrs.gravity = Gravity.TOP or Gravity.START
        win.attributes = attrs
        val metrics = activity.resources.displayMetrics
        val gap = dp(activity, 6)
        val edge = dp(activity, 6)
        // 量的是卡片，不是外层遮罩（遮罩是 match_parent，量出来是整屏，没法算位置）
        val card = d.view.findViewById<View>(R.id.lfCard) ?: d.view
        card.measure(
            View.MeasureSpec.makeMeasureSpec(
                (metrics.widthPixels - edge * 2).coerceAtLeast(1),
                View.MeasureSpec.AT_MOST
            ),
            View.MeasureSpec.makeMeasureSpec(0, View.MeasureSpec.UNSPECIFIED)
        )
        val w = card.measuredWidth
        val h = card.measuredHeight
        var tx: Int
        var ty: Int
        if (anchor != null && w > 0 && h > 0) {
            tx = anchor.left.toInt()
            // 一律贴锚点上方；上面放不下才改贴下方。
            // 上界不是 0 而是窗口真实可用区上沿（状态栏那条线，实测 y=140）——
            // 浮层进不到那条线以上，按 0 算会被系统压下来、紧贴锚点。
            val usable = android.graphics.Rect()
            activity.window?.decorView?.getWindowVisibleDisplayFrame(usable)
            val top = if (usable.top > edge) usable.top else edge
            val above = anchor.top.toInt() - gap - h
            ty = if (above >= top) above else anchor.bottom.toInt() + gap
        } else {
            tx = (metrics.widthPixels - w) / 2
            ty = metrics.heightPixels - h - dp(activity, 96)
        }
        tx = tx.coerceIn(edge, (metrics.widthPixels - edge - w).coerceAtLeast(edge))
        ty = ty.coerceIn(edge, (metrics.heightPixels - edge - h).coerceAtLeast(edge))
        // 位置写在卡片的 translation 上（遮罩不动，遮罩负责接卡片外的点击）
        card.translationX = tx.toFloat()
        card.translationY = ty.toFloat()
        d.dialog.show()
        // 卡片窗口刚加入时，系统栏会先按它的默认值算一遍（弹出瞬间闪一下）；
        // 立刻按网页值再设一遍，把这个窗口期抹掉（关闭时由 onCardClosed 恢复主窗口那版）
        val bars = androidx.core.view.WindowInsetsControllerCompat(win, win.decorView)
        val lightBars = web?.optString("theme") != "dark"
        bars.isAppearanceLightStatusBars = lightBars
        bars.isAppearanceLightNavigationBars = lightBars
    }

    /** 确认（网页 confirm） */
    fun confirm(
        activity: Activity,
        titleText: String?,
        messageText: String?,
        okText: String = "确定",
        cancelText: String = "取消",
        onOk: () -> Unit,
        onCancel: () -> Unit = {}
    ) {
        val d = build(activity)
        title(d.view, titleText)
        message(d.view, messageText)
        button(d.view, R.id.lfCancel, cancelText, primary = false) {
            d.dialog.dismiss(); onCancel()
        }
        button(d.view, R.id.lfOk, okText, primary = true) {
            d.dialog.dismiss(); onOk()
        }
        d.dialog.setOnCancelListener { onCancel() }
        d.dialog.show()
    }

    /** 提示（网页 alert）：只有确定 */
    fun alert(
        activity: Activity,
        titleText: String?,
        messageText: String?,
        onOk: () -> Unit = {}
    ) {
        val d = build(activity)
        title(d.view, titleText)
        message(d.view, messageText)
        button(d.view, R.id.lfOk, "确定", primary = true) {
            d.dialog.dismiss(); onOk()
        }
        d.dialog.setOnCancelListener { onOk() }
        d.dialog.show()
    }

    /** 输入（网页 prompt） */
    fun prompt(
        activity: Activity,
        titleText: String?,
        messageText: String?,
        defaultValue: String?,
        onOk: (String) -> Unit,
        onCancel: () -> Unit = {}
    ) {
        val d = build(activity)
        title(d.view, titleText)
        message(d.view, messageText)
        val input = EditText(activity).apply {
            setText(defaultValue ?: "")
            setSelection(text.length)
            setSingleLine()
            textSize = 14f
            setTextColor(color(activity, "text", R.color.lf_text))
            setHintTextColor(color(activity, "textLight", R.color.lf_text_light))
            background = activity.getDrawable(R.drawable.lf_input_bg)
            backgroundTintList = ColorStateList.valueOf(
                color(activity, "bg", R.color.lf_bg))
            setPadding(dp(activity, 12), dp(activity, 10), dp(activity, 12), dp(activity, 10))
        }
        content(d.view, input)
        button(d.view, R.id.lfCancel, "取消", primary = false) {
            d.dialog.dismiss(); onCancel()
        }
        button(d.view, R.id.lfOk, "确定", primary = true) {
            d.dialog.dismiss()
            onOk(input.text.toString())
        }
        d.dialog.setOnCancelListener { onCancel() }
        d.dialog.show()
    }
}
