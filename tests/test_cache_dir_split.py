# -*- coding: utf-8 -*-
"""运行缓存目录（服务）与开发工作区（人）分家。

背景：源码开发时数据根就是项目根，服务的运行缓存 `.cache/` 与开发期的工作文件
（交接文档、探针脚本与输出、截图、素材）原本挤在同一个目录里 —— 清理服务缓存时
极容易误删工作文件，反过来"这个目录到底归谁"也变得含糊。

2026-09-22 分家，**方向是让工作文件搬走**，不是改服务的目录名：

  * `.cache/` —— 服务的运行缓存，就是 `paths.CACHE_DIR`，一个字没改；
  * `.work/`  —— 开发工作区，在 `.gitignore` 里，服务一行代码都不碰。

⚠️ 为什么不反过来：`.cache` 是**服务运行产生的**目录（README 把它列为运行产物、
ARCHITECTURE 的目录表里写着"运行缓存"）。给服务的目录改名，等于让所有已部署实例
跟着迁移一次，而真正的问题（工作文件占了服务的地盘）一点没解决。
"""
import os
import subprocess

from leaffs import paths

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK_DIR = os.path.join(paths.PROJECT_DIR, '.work')


def test_cache_dir_is_the_service_runtime_cache():
    """服务缓存是数据根下的 `.cache` —— 运行产物，不随开发习惯改名。"""
    assert paths.CACHE_DIR == os.path.join(paths.PROJECT_DIR, '.cache')
    assert paths.THUMB_DIR == os.path.join(paths.CACHE_DIR, 'thumbs')


def test_work_dir_is_not_the_cache_dir():
    """工作区不能落在服务缓存里，也不能只是换个名字。"""
    assert os.path.abspath(WORK_DIR) != os.path.abspath(paths.CACHE_DIR)
    assert os.path.basename(paths.CACHE_DIR) != '.work'


def test_work_dir_is_gitignored():
    """工作区不入库：里面有 logcat、APK、截图这些大件，也有过程产物。"""
    r = subprocess.run(['git', 'check-ignore', '-q', WORK_DIR], cwd=REPO,
                       capture_output=True)
    assert r.returncode == 0, '.work 没有被 .gitignore 覆盖'


def test_service_code_never_treats_the_work_dir_as_a_runtime_path():
    """服务代码里不许把 `.work` 当运行路径 —— 它只属于开发者。

    注释里指向探针脚本的引用是允许的（那是给人看的线索），所以这里只查
    带引号的字符串字面量：把 `.work` 拼进路径就会写成 `'.work'` 或 `".work"`。
    """
    pkg = os.path.join(REPO, 'leaffs')
    offenders = []
    for dp, dn, fn in os.walk(pkg):
        if '__pycache__' in dp:
            continue
        for f in fn:
            if not f.endswith('.py'):
                continue
            path = os.path.join(dp, f)
            text = open(path, encoding='utf-8').read()
            if "'.work'" in text or '".work"' in text:
                offenders.append(os.path.relpath(path, REPO))
    assert not offenders, '这些文件把 .work 当成了运行路径: %s' % offenders


def test_cache_dir_is_not_a_hidden_work_dumping_ground():
    """反向守卫：`.cache` 的顶层不该再长出工作文件来。

    服务的运行数据只有固定几项；出现别的名字说明又有人往这里堆东西了。
    """
    allowed = {'thumbs', 'folder_sizes', 'folder_sizes.json',
               '.aria2_session', '.dht.dat', 'trackers.txt'}
    if not os.path.isdir(paths.CACHE_DIR):
        return                      # 还没跑过服务，目录不存在也算通过
    extra = sorted(set(os.listdir(paths.CACHE_DIR)) - allowed)
    assert not extra, '.cache 里出现了非运行数据: %s（工作文件请放 .work/）' % extra
