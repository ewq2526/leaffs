# -*- coding: utf-8 -*-
"""缩略图生成失败必须留下记录（可诊断性）。

`_generate_thumbnail` 原来**只有"抛异常"那一条路有日志**，而真正常见的两种失败完全静默：

* **ffmpeg 返回码非 0** —— 实测 1×1 的图就让它是 69（stderr：`one of its streams
  received no packets` / `Conversion failed!`）
* **返回码 0 但没产出文件**

这两者的表现与"正在生成中"**一模一样**（都是返回 None、都不写日志），排查时是黑洞 ——
上一轮我就是被它卡了很久，绕去怀疑沙箱/进程树/路径。

本文件钉两件事：**坏图必须记一条失败日志**（含返回码）、**好图不许记**（防误报）。

⚠️ 素材与目录都放临时数据根：既避免污染仓库的 `.cache/thumbs`，
也**绝不碰真实的 `UPLOAD_DIR`**（那是用户的数据目录）；同盘还避开
`get_thumbnail` 内部 `os.path.relpath` 的跨盘 `ValueError`。
"""
import os
import struct
import zlib

import pytest


def _png(w=8, h=8):
    """真 PNG（8×8）—— 2×2 及以上 ffmpeg 都能处理，**1×1 不行**（见模块 docstring）"""
    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data +
                struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))

    raw = b''.join(b'\x00' + b'\x80\x40\x20' * w for _ in range(h))
    return (b'\x89PNG\r\n\x1a\n' +
            chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


@pytest.fixture()
def thumb_env(monkeypatch, data_root):
    """缩略图目录与上传根都指到临时数据根，并收集 `add_log` 的消息"""
    import leaffs.utils.core as UC
    if not UC.FFMPEG_PATH:
        pytest.skip('本机没有 ffmpeg，桌面端缩略图后端不可用（安卓走原生生成器，另说）')
    upload = os.path.join(data_root, 'thumb_probe_files')
    thumbs = os.path.join(data_root, 'thumb_probe_thumbs')
    os.makedirs(upload, exist_ok=True)
    os.makedirs(thumbs, exist_ok=True)
    monkeypatch.setattr(UC, 'UPLOAD_DIR', upload)
    monkeypatch.setattr(UC, 'THUMB_DIR', thumbs)
    logs = []
    import leaffs.runtime_log as RL
    monkeypatch.setattr(RL, 'add_log', lambda msg, level='info': logs.append((level, msg)))
    return upload, thumbs, logs


def test_bad_image_records_a_failure_log(thumb_env):
    """坏图（内容是文本的 .png）→ 必须留下失败记录，且带上返回码"""
    import leaffs.utils.core as UC
    upload, _thumbs, logs = thumb_env
    bad = os.path.join(upload, 'not_an_image.png')
    with open(bad, 'wb') as f:
        f.write(b'definitely not a png at all')

    assert UC._generate_thumbnail(bad) is None, '坏图不该被当成生成成功'
    assert logs, '坏图生成失败却一条日志都没有（修之前就是这样）'
    assert any('返回码' in msg for _lvl, msg in logs), \
        '失败记录里没有返回码 —— 那正是最能说明问题的信息：%s' % logs
    assert any(lvl == 'warn' for lvl, _msg in logs), '级别应当是 warn：%s' % logs
    assert any('not_an_image.png' in msg for _lvl, msg in logs), \
        '日志里看不出是哪个文件：%s' % logs


def test_good_image_records_no_failure(thumb_env):
    """好图：必须生成成功，且**一条失败日志都不许有**（防误报）"""
    import leaffs.utils.core as UC
    upload, _thumbs, logs = thumb_env
    good = os.path.join(upload, 'good.png')
    with open(good, 'wb') as f:
        f.write(_png())

    out = UC._generate_thumbnail(good)
    assert out and os.path.exists(out), '好图应当生成成功：%r' % out
    assert not logs, '好图不该记失败日志（误报）：%s' % logs
