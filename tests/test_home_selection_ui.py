# -*- coding: utf-8 -*-
"""LF-28：文件卡片的"选中态样子"必须只有一个写点（描边与左上角的勾同源）。

**现象**（用户报的）：「文件选中状态的描边与左上角的勾状态不一致」。

**根因**：两处视觉由**不同的东西**决定 ——
  · **描边** = 卡片上的 `.sel` class（`style.css` 的 `.file-card.sel{border-color…}`）；
  · **左上角的勾** = `<input type="checkbox" checked>` 的 `checked` **属性**（渲染时一次性生成）。
而两个点击入口当时各只更新一半：
  · 点卡片空白 → `toggleCardSelect` 只 `classList.toggle('sel')` ⇒ **描边变、勾不变**；
  · 只点勾 → `toggleSelect` 只改 `selected` 与计数 ⇒ **勾变、描边不变**。
（`render()` 会同时重绘两者，所以刷新/搜索/排序/全选之后看着又是对的 ——
  这正是"有时候不一致"的来源。）

**修法**：加唯一写点 `paintCardSel(card, on)`，两个入口都调它；checkbox 的 onclick 多传 `this`
（不然点勾时拿不到所属卡片）。改动落在 `home.html` 与 `home.en.html` **两份**，
且每份里 grid / list 两种视图各有一处 checkbox。

⚠️ **真实行为没法自动化测**（要浏览器 DOM 事件）—— 本文件只做**文本守卫**，
保证"唯一写点"这个结构不被将来某次改动破坏（**漏改一份就红**：`home.en.html` 是独立文件）。
行为本身要人工在浏览器里点两下验（见 `fix-log/2026-09-16.md` 的 LF-28）。
"""
import os

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME_DIR = os.path.join(PROJ_ROOT, 'leaffs', 'web_page', 'home')
PAGES = ('home.html', 'home.en.html')

# JS 里那两段字面量（注意反斜杠：源码里是 `\'` + sp + `\'`）
_CALL_TOGGLESELECT_OLD = "toggleSelect(\\'' + sp + '\\',this.checked)"
_CALL_TOGGLESELECT_NEW = "toggleSelect(\\'' + sp + '\\',this.checked,this)"


def _read(name):
    with open(os.path.join(HOME_DIR, name), encoding='utf-8') as f:
        return f.read()


def test_selected_state_is_painted_in_one_place():
    """★ 两份 home 页都必须是同一个结构：

    · 有 `paintCardSel(card, on)`；
    · `toggleCardSelect` 与 `toggleSelect` **都**调它（两个入口不能各更新一半）；
    · `classList.toggle('sel'` **只出现在 `paintCardSel` 里**（唯一写点）。
    """
    bad = []
    for name in PAGES:
        t = _read(name)
        if 'function paintCardSel(' not in t:
            bad.append('%s：缺少 paintCardSel —— 选中态又会退回"两个源"' % name)
            continue
        n_toggle = t.count("classList.toggle('sel'")
        if n_toggle != 1:
            bad.append("%s：`classList.toggle('sel'` 出现 %d 次（应当只有 paintCardSel 里那一次）"
                       % (name, n_toggle))
        if 'paintCardSel(el, selected.has(p));' not in t:
            bad.append('%s：toggleCardSelect 没调 paintCardSel（点卡片会变成"描边变、勾不变"）' % name)
        if "paintCardSel(el ? el.closest('.file-card') : null" not in t:
            bad.append('%s：toggleSelect 没调 paintCardSel（点勾会变成"勾变、描边不变"）' % name)
    assert not bad, '选中态写点没收干净：\n  ' + '\n  '.join(bad)


def test_checkbox_passes_itself_to_toggle_select():
    """点勾那条路要能拿到所属卡片：checkbox 的 onclick 必须把 `this` 传给 `toggleSelect`

    **grid 与 list 两种视图各有一处**，所以要求 ≥2 处 —— 只改一处的话 list 视图仍然是坏的。
    """
    bad = []
    for name in PAGES:
        t = _read(name)
        n_new = t.count(_CALL_TOGGLESELECT_NEW)
        n_all = t.count(_CALL_TOGGLESELECT_OLD)
        if n_all - n_new > 0:
            bad.append('%s：还有 %d 处 onclick 没传 this（点勾时找不到卡片）' % (name, n_all - n_new))
        if n_new < 2:
            bad.append('%s：只有 %d 处传了 this（grid 与 list 各一处，应当 ≥2）' % (name, n_new))
    assert not bad, '点勾那条路没接上：\n  ' + '\n  '.join(bad)
