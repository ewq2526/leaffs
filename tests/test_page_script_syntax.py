# -*- coding: utf-8 -*-
"""页面内联脚本必须是合法 JavaScript。

批量替换类名是按字符串做的，很容易把拼接吃坏 —— 典型形态是
`'<img class="thumb' + (_ok ? ' loaded' : '')" src=…'`，`? : ''` 后面少一个 `+`。
后果是**整段脚本语法错误、浏览器里一行都不执行**，页面只剩空骨架；而服务端一切正常、
金额/接口/HTML 结构全对，纯文本断言也发现不了。

这里直接用 node --check 逐块验语法（页面里没有 node 的机器自动跳过）。
"""
import io
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'app', 'opt', 'leaffs', 'web_page')
SCRIPT_RE = re.compile(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', re.S | re.I)


def _first_error(stderr):
    lines = [ln for ln in (stderr or '').strip().splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if ln.strip().startswith('^'):
            return lines[i - 1].strip() if i else ln.strip()
    return lines[-1].strip() if lines else ''


def test_inline_scripts_are_valid_js(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('没有 node 可执行文件，跳过内联脚本语法检查')

    bad = []
    checked = 0
    for dirpath, _dirs, names in os.walk(ROOT):
        for name in sorted(names):
            if not name.endswith('.html'):
                continue
            path = os.path.join(dirpath, name)
            text = io.open(path, encoding='utf-8').read()
            for i, body in enumerate(SCRIPT_RE.findall(text)):
                if not body.strip():
                    continue
                checked += 1
                f = tmp_path / ('%s.%d.js' % (name, i))
                with io.open(str(f), 'w', encoding='utf-8', newline='\n') as fp:
                    fp.write(body)
                r = subprocess.run([node, '--check', str(f)], capture_output=True,
                                   encoding='utf-8', errors='replace')
                if r.returncode != 0:
                    rel = os.path.relpath(path, ROOT).replace('\\', '/')
                    bad.append('%s 第 %d 个内联脚本：%s' % (rel, i + 1, _first_error(r.stderr)))
                os.unlink(str(f))

    assert checked > 30, '只抽到 %d 段内联脚本，抽取规则可能失效了' % checked
    assert not bad, '页面内联脚本有语法错误（浏览器里整段不执行）：\n  ' + '\n  '.join(bad)
