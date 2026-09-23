# -*- coding: utf-8 -*-
"""`issues.md` §二 第 12 条：`share_access.json` 被**别的进程/外部**改动后，本进程必须跟上。

**修复前的做法** —— `_load_locked()` 是：

```python
if _CACHE is not None:
    return
```

也就是"**进程内加载一次就用到死**"。而 `_CACHE` 是**进程内**单例，这份 JSON 却可能被
本进程之外的东西改（同一数据根跑两个实例、外部还原或手工编辑）⇒ 本进程一直用旧值：

- 在 A 处设了分享码，B 处**看不到**（列表/下载照旧不要求输码）；
- 在 A 处清了码，B 处**还要求输码**（而码已经不存在了，等于分享废掉）；
- 而且**不会自愈** —— 只有重启才更新。

**修法**：`_load_locked` 改成**按文件指纹重载**（`_file_stamp()` = `(st_mtime_ns, st_size)`）；
`_save_locked` 写完把指纹对齐（免得自己白重载）；**读失败故意不更新指纹**（瞬时读错下次会重试）；
`_flush_day_locked` 的跨日重置改成**落盘**（否则别的进程一改文件就会把它冲掉）。

⚠️ 索引键是**随机标签**（每条分享一个码，`share/mappings.py` 登记时生成），不再是用户名 ——
`set_code` / `code_enabled` 收的都是标签。本文件只碰 `share/access.py` 这一层（映射表不参与），
所以标签用固定字符串就够，值本身不影响语义。

⚠️ 本文件把 `_ACCESS_FILE` 指到临时目录、并重置模块级缓存 —— 全在 monkeypatch 作用域内，
用例结束自动还原，**不动真实数据根**。
"""
import json
import os
import time

import pytest

from leaffs.share import access as S

# 每条分享的标签（真实值由 `share/mappings.py` 登记时随机生成）；一个用例一个，互不干扰。
LABEL_A, LABEL_B, LABEL_C = 'lab-alice', 'lab-bob', 'lab-carol'
LABEL_D, LABEL_E = 'lab-dave', 'lab-erin'


@pytest.fixture()
def acc(data_root_factory, monkeypatch):
    """把分享访问数据指到一个临时 JSON，并清掉模块级缓存与指纹"""
    root = data_root_factory('shareacc_')
    path = os.path.join(root, 'share_access.json')
    monkeypatch.setattr(S, '_ACCESS_FILE', path)
    monkeypatch.setattr(S, '_CACHE', None)
    monkeypatch.setattr(S, '_CACHE_STAMP', None)
    return path


def _external_edit(path, mutate):
    """模拟"别的进程 / 外部编辑"：直接读盘改完写回，**不经过本模块的任何函数**

    写完显式把 mtime 推后 1 秒：`os.replace` 的纳秒级 mtime 一般够用，但不同文件系统
    精度不一，测试不该依赖它（真实场景里的外部改动也不会恰好落在同一纳秒）。
    """
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    mutate(data)
    tmp = path + '.ext'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_external_clear_is_picked_up(acc):
    """★ 另一处**清掉**某条分享的码后，本进程必须立刻不再要求输码

    （旧实现会一直用旧值：码已经没了，本进程还在拦人 —— 只有重启才更新。）
    """
    S.set_code(LABEL_A, 'abcdef', owner='alice')
    assert S.code_enabled(LABEL_A) is True

    _external_edit(acc, lambda d: d['items'][LABEL_A].update({'code': ''}))

    assert S.code_enabled(LABEL_A) is False, (
        '外部清掉分享码后本进程还认为设了码 —— `_CACHE` 被当成唯一事实源了（§二 第 12 条）')


def test_external_set_is_picked_up(acc):
    """★ 反向：另一处**设上**某条分享的码后，本进程必须立刻开始要求输码

    （旧实现里 A 处设码、B 处看不到 —— 列表与下载照旧放行，等于分享码不生效。）
    """
    assert S.code_enabled(LABEL_B) is False

    # 先让盘上确实有这个文件（真实场景里它是另一个进程写出来的）
    with open(acc, 'w', encoding='utf-8') as f:
        json.dump({'date': S._today(), 'items': {}, 'tickets': {}}, f)

    _external_edit(acc, lambda d: d.setdefault('items', {}).update(
        {LABEL_B: {'code': 'abcdef', 'owner': 'bob'}}))

    assert S.code_enabled(LABEL_B) is True, (
        '外部设上分享码后本进程仍然放行 —— 分享码形同虚设')


def test_no_reload_when_file_is_unchanged(acc):
    """对照：盘上文件没变时**不重载**（否则每次校验都重新解析 JSON，白花钱）

    判据：往内存缓存里塞一个哨兵键，走一次 `_load_locked` 之后它还在 ⇒ 没被盘上的内容覆盖。
    """
    S.set_code(LABEL_C, 'abcdef', owner='carol')
    S._CACHE['__probe__'] = 1
    assert S.code_enabled(LABEL_C) is True
    assert S._CACHE.get('__probe__') == 1, '文件没变却重载了 —— 指纹判断失效'


def test_save_aligns_stamp(acc):
    """自己写完要把指纹对齐，否则下一次 `_load_locked` 会以为过期、白重载一遍"""
    S.set_code(LABEL_D, 'abcdef', owner='dave')
    assert S._CACHE_STAMP == S._file_stamp(), '保存后指纹没对齐'


def test_read_failure_does_not_mark_as_loaded(acc):
    """读失败时**不更新指纹** —— 否则一次瞬时读错会被记成"已加载"，要等文件再变才重试

    （真实场景：正好撞上另一进程 `os.replace` 的瞬间，或盘上文件被写坏。）
    """
    S.set_code(LABEL_E, 'abcdef', owner='erin')
    with open(acc, 'w', encoding='utf-8') as f:
        f.write('{ not json at all')
    st = os.stat(acc)
    os.utime(acc, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    S.code_enabled(LABEL_E)          # 触发一次读失败

    assert S._CACHE_STAMP != S._file_stamp(), (
        '读失败却把指纹更新了 —— 之后不会重试，要等文件再变')

    # 文件修好后应当能重新加载（指纹不匹配 ⇒ 会再试）。这里**写成"设了码"**：
    # 读失败时内存里已经被清成空表，若指纹被错记成"已加载"，这条会一直读到空表 ⇒ 红。
    with open(acc, 'w', encoding='utf-8') as f:
        json.dump({'date': S._today(), 'items': {LABEL_E: {'code': 'abcdef'}},
                   'tickets': {}}, f)
    st = os.stat(acc)
    os.utime(acc, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert S.code_enabled(LABEL_E) is True, '文件修好后没能重新加载'
