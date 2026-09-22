# -*- coding: utf-8 -*-
"""孤儿路由守卫：后端声明了、但没有任何调用方的接口。

这类接口不会自己暴露出来 —— 权限门槛测试只验证"有权访问时能通"，
所以一个永远没人调的接口可以让测试一直全绿（本项目就躺过两个：
/api/sessions 和 /api/session/revoke，从 1.0.4 引入起前端就没接过）。

做法：
  1. 从 server/handler.py 用 AST 取**真正的路由声明** —— GET_ROUTES 表、
     _ADMIN_ONLY_GET/_ADMIN_ONLY_POST 两个清单、do_POST 里的 handlers 字典、
     以及 do_GET/do_POST 中硬编码的 path 字面量比较；
  2. 在全仓库（web_page 的 html/js/css、android 的源码、tests、其它 py、
     以及 README/CHANGELOG）里找这个路径有没有被引用（子串匹配，前缀路由
     如 /browse/ 会被 /browse/public 命中）；
  3. 只有 handler.py 自己提到过 → 孤儿。

判为孤儿时的处理：要么删掉那个接口，要么加进下面的白名单并写清为什么
（白名单只接受"服务端运行时生成、由浏览器/前端按值消费"这一类）。
"""
import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
HANDLER = ROOT / 'leaffs' / 'server' / 'handler.py'

# 扫描引用时要跳过的目录（构建副本、缓存、依赖、归档）
# ⚠️ `.work` 必须跳：探针脚本住在那里，它们含各种 URL 字面量，会污染"孤儿路由"判定。
SKIP_PARTS = {'build', 'generated', 'sources', 'env', '.git', '.cache', '.work',
              'node_modules', '__pycache__', '.venv', 'venv'}
SCAN_SUFFIXES = {'.py', '.js', '.html', '.css', '.kt', '.java', '.json', '.md', '.txt'}

# 服务端运行时生成、前端按"值"消费的 URL —— 前端代码里当然找不到这些字面量
GENERATED_URL_WHITELIST = {
    '/api/qrlogin': '二维码内容：后端拼 /api/qrlogin?sid=…，扫码方在浏览器地址栏访问它',
}


def _strings(node):
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
    return out


def route_paths():
    """handler.py 里声明的全部路由路径"""
    tree = ast.parse(HANDLER.read_text(encoding='utf-8'))
    paths = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(n in ('GET_ROUTES', '_ADMIN_ONLY_GET', '_ADMIN_ONLY_POST') for n in names):
                paths.update(_strings(node.value))
            if 'handlers' in names and isinstance(node.value, ast.Dict):
                for k in node.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        paths.add(k.value)
        # do_GET/do_POST 里硬编码的 path 字面量比较（如 path.startswith('/p/')）
        if isinstance(node, ast.Compare):
            for c in [node.left] + list(node.comparators):
                if isinstance(c, ast.Constant) and isinstance(c.value, str):
                    paths.add(c.value)
    return {p for p in paths
            if p.startswith('/') and len(p) > 1 and '?' not in p and ' ' not in p}


def referencing_files(paths):
    """每个路径 → 提到过它的文件集合"""
    hits = {p: set() for p in paths}
    for f in ROOT.rglob('*'):
        if not f.is_file() or f.suffix.lower() not in SCAN_SUFFIXES:
            continue
        if any(part in SKIP_PARTS for part in f.parts):
            continue
        try:
            text = f.read_text(encoding='utf-8', errors='replace')
        except Exception:
            continue
        rel = str(f.relative_to(ROOT))
        for p in paths:
            if p in text:
                hits[p].add(rel)
    return hits


def test_no_orphan_routes():
    paths = route_paths()
    assert paths, '没能从 handler.py 解出任何路由 —— 提取逻辑要跟着改'
    hits = referencing_files(paths)

    handler_rel = str(HANDLER.relative_to(ROOT))
    orphans = sorted(
        p for p, files in hits.items()
        if files <= {handler_rel} and p not in GENERATED_URL_WHITELIST
    )
    detail = '\n'.join('  %s  (只出现于 %s)' % (p, sorted(hits[p]) or '无')
                       for p in orphans)
    assert not orphans, (
        '发现没有任何调用方的接口：\n%s\n'
        '处理方式：删掉它，或加进 GENERATED_URL_WHITELIST 并说明为什么 '
        '（只接受"服务端运行时生成、前端按值消费"这一类）' % detail
    )
