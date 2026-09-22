# -*- coding: utf-8 -*-
"""轻档：刷新后如实交代「上次这批没传完」（`issues.md` §九 ① 的"档 2"）。

它**不能**真的接着传（那是分片上传才做得到），只解决"刷新后一片空白、让人以为传完了"。
所以本文件钉两件事：

1. 两个页面都有这套记账，且**三个记账点**（入队 / 开始传 / 队列清空）一个不少 ——
   缺任何一个都会漏报；
2. ⚠️ **文案必须如实**：不许把"记了一笔账"写成"已恢复 / 已续传"这类做不到的承诺。

前端在测试环境里跑不起来，按项目既有做法用**静态守卫**。
"""
import io
import os
import re

import pytest

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'leaffs', 'web_page', 'home')
PAGES = ('home.html', 'home.en.html')


def _src(fn):
    return io.open(os.path.join(BASE, fn), encoding='utf-8').read()


@pytest.mark.parametrize('fn', PAGES)
def test_pending_batch_is_recorded_and_reported(fn):
    src = _src(fn)
    assert 'PENDING_KEY' in src, '%s 没有在途批次的记账键' % fn
    assert 'function savePendingUpload(' in src, '%s 没有记账函数' % fn
    assert 'function checkPendingUpload(' in src, '%s 没有回读函数' % fn
    assert 'checkPendingUpload();' in src, \
        '%s 页面加载时没有回读 —— 那样记了也白记' % fn
    # 三个记账点：入队、开始传下一个、队列清空
    n = src.count('savePendingUpload();')
    assert n >= 3, '%s 记账点只有 %d 处（入队/开始/清空各需一次），会漏报' % (fn, n)
    # 中断的那个文件必须放回队首，否则它不算进"没传完"（LF-25 的 onabort 路径）
    assert 'uploadQueue.unshift(item);' in src, \
        '%s 中断的文件没放回队首，会被漏报' % fn


@pytest.mark.parametrize('fn', PAGES)
def test_wording_does_not_promise_resuming(fn):
    """⚠️ 关键：不许把"记了一笔账"写成"能续传"。"""
    src = _src(fn)
    m = re.search(r'function checkPendingUpload\(\).*?\n\}', src, re.S)
    assert m, '%s 找不到 checkPendingUpload 的函数体' % fn
    body = m.group(0)
    for word in ('已恢复', '已续传', '继续上传', 'resumed', 'resume the upload'):
        assert word not in body, '%s 的提示文案承诺了做不到的事：%r' % (fn, word)
    assert ('重新选择' in body) or ('pick the files again' in body), \
        '%s 没有如实说明"要重新选择文件上传" —— 那是这条的全部意义' % fn
