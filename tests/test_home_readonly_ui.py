# -*- coding: utf-8 -*-
"""写入口按目录权限隐藏：两份首页都要接上（服务端给的 `can_write` / `can_delete`）。

**来由（2026-09-28）**：服务端把"往虚拟容器写"堵掉之后，网页上的入口照旧显示 ——
`public/mounts`、`public/shares` 与映射区里，「上传文件 / 上传文件夹 / 新建文件夹」和拖拽区
都在，点了才被拒。现在首页按 `/api/files`（以及 WS 列表）给的 `can_write` / `can_delete` 隐藏：
只读目录里藏掉上传区、换一句说明；`can_delete` 为假时禁用批量删除、容器条目（`f.local`）
不渲染行内删除按钮。

⚠️ **真实行为没法自动化测**（要浏览器 DOM + 真实请求）—— 本文件是**文本守卫**，盯的是结构
不被将来的改动破坏。最要紧的两条：
* **HTTP 与 WS 两条通道都必须赋值** —— 只接一条的话，用 WS 渲染列表时入口又会全回来；
* **两份页面都不能漏**（`home.en.html` 是独立文件）。

真实行为要人工点两下验（进 `public/mounts` 看上传区是否消失与那句说明）。
"""
import os

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME_DIR = os.path.join(PROJ_ROOT, 'app', 'opt', 'leaffs', 'web_page', 'home')
PAGES = ('home.html', 'home.en.html')


def _read(name):
    with open(os.path.join(HOME_DIR, name), encoding='utf-8') as f:
        return f.read()


def test_both_pages_hide_write_entries_by_dir_perms():
    """★ 两份页面、两条通道都要接上

    ⚠️ 赋值那四条**带前后换行**去匹配：只匹配子串的话，把整行注释掉
    （`// canWriteHere = msg.can_write !== false;`）照样算"接到了" —— 这种退化必须能红。
    """
    bad = []
    checks = (
        ('function applyDirPerms(', '缺少 applyDirPerms（入口不会跟着目录权限变）'),
        ('function delBtnHtml(', '缺少删除按钮的唯一写点 delBtnHtml'),
        ('id="readonlyTip"', '缺少"只读目录"那句说明的元素'),
        ('id="bulkDeleteBtn"', '批量删除按钮没有 id，禁不掉'),
        ('\n        canWriteHere = d.can_write !== false;\n', 'HTTP 列表没接 can_write'),
        ('\n            canWriteHere = msg.can_write !== false;\n',
         'WS 列表没接 can_write —— 走 WS 渲染时写入口又全回来了'),
        ('\n        canDeleteHere = d.can_delete !== false;\n', 'HTTP 列表没接 can_delete'),
        ('\n            canDeleteHere = msg.can_delete !== false;\n', 'WS 列表没接 can_delete'),
        ('delBtnHtml(sp, f.local)', '行模板没走 delBtnHtml（删除按钮又会各写一遍）'),
        ('if (!canWriteHere) {', '拖拽上传没拦（区藏了但拖进来照旧会传）'),
    )
    for name in PAGES:
        t = _read(name)
        for needle, why in checks:
            if needle not in t:
                bad.append('%s：%s' % (name, why))
    assert not bad, '写入口没按目录权限隐藏：\n  ' + '\n  '.join(bad)


def test_delete_button_has_a_single_writer():
    """行内删除按钮只经 `delBtnHtml` 生成

    folder 与 file 两个分支各写一遍的话，将来改一处必漏另一处（LF-28 的教训）。
    搜索结果那一行刻意**不**复用它：搜索结果跨目录，而 `can_delete` 是"当前浏览的目录"的属性，
    拿来判搜索结果会误伤（在只读目录里搜出来的、其实可删的文件会被一起藏掉）。
    """
    bad = []
    for name in PAGES:
        t = _read(name)
        if t.count('function delBtnHtml(') != 1:
            bad.append('%s：delBtnHtml 定义了 %d 次（应当只有一处）'
                       % (name, t.count('function delBtnHtml(')))
        n_call = t.count('delBtnHtml(sp, f.local)')
        if n_call < 2:
            bad.append('%s：行模板里 delBtnHtml 只出现 %d 次（folder 与 file 各一处，应当 ≥2）'
                       % (name, n_call))
        if '<a class="btn btn-sm btn-primary">打开</a><button class="btn btn-sm btn-danger"' in t \
                or '<a class="btn btn-sm btn-primary">Open</a><button class="btn btn-sm btn-danger"' in t:
            bad.append('%s：行模板里还直接拼着删除按钮（没走 delBtnHtml）' % name)
    assert not bad, '删除按钮的写点没收干净：\n  ' + '\n  '.join(bad)
