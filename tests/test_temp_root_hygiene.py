# -*- coding: utf-8 -*-
"""临时数据根的自清理（T1）。

来由：三个自建数据根的夹具（`clamp_` / `log_` / `savefail_`）原来只建不删，
全量跑一轮就多 3 个残留，累积到 69 个 0.2 MB；会话级 `data_root` 一直是清的。
现在"造 + 清"收在 `conftest.py` 的 `new_data_root` / `data_root_factory`，
外加一条按岁数扫"死运行"残留的 `sweep_stale_roots`。

这里钉死清扫的**边界**：只清**超过 24 小时**的，动的必须是**残留**、不是并发运行中的根。
"""
import os
import time

from conftest import TEST_RUNS_DIR, STALE_ROOT_AGE, new_data_root, sweep_stale_roots


def test_new_data_root_is_under_test_runs_with_config_dir():
    """造根的统一入口：落在 .cache/test_runs 下，且 config/ 已建好（三个调用点都靠它）"""
    root = new_data_root('tmp_')
    try:
        assert os.path.dirname(root) == TEST_RUNS_DIR, root
        assert os.path.basename(root).startswith('tmp_'), root
        assert os.path.isdir(os.path.join(root, 'config')), 'config/ 没建好'
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_sweep_removes_only_stale_roots():
    """清扫只按岁数动手：旧的残留清掉，新的（可能是并发运行中的根）必须留着"""
    stale = os.path.join(TEST_RUNS_DIR, 'stale_probe')
    fresh = os.path.join(TEST_RUNS_DIR, 'fresh_probe')
    os.makedirs(stale, exist_ok=True)
    os.makedirs(fresh, exist_ok=True)
    old = time.time() - STALE_ROOT_AGE - 3600          # 25 小时前
    os.utime(stale, (old, old))
    try:
        sweep_stale_roots()
        assert not os.path.exists(stale), '超过 24 小时的残留没被清掉'
        assert os.path.isdir(fresh), '新鲜的根被误删了（会打掉并发运行中的会话）'
    finally:
        import shutil
        shutil.rmtree(stale, ignore_errors=True)
        shutil.rmtree(fresh, ignore_errors=True)
