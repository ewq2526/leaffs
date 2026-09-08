# -*- coding: utf-8 -*-
"""路径定位 —— 运行根/资源根/各数据目录的单一数据源。

历史：定位逻辑原散落在 utils_core/ut_core.py 顶部，重构收敛到本模块。
其它模块统一从这里取路径常量（ut_core 暂时保留名字转口，逐步直连本模块）。

约定（与历史语义完全一致）：
  * 源码运行（python -m leaffs）：可写数据根 = 项目根（leaffs 包的上层），
    资源根（web_page、内置 exe、默认数据）= leaffs 包目录。
  * 打包运行（PyInstaller/Nuitka 等）：可写数据根 = 主程序(exe)所在目录，
    资源根 = 解包/内置资源目录（PyInstaller 的 sys._MEIPASS，其它工具回退 exe 目录）。
    运行产生的文件夹(shared_files/.cache/config/日志)全部落在数据根。
  * 测试隔离：环境变量 LEAFFS_PROJECT_ROOT 重定向数据根（默认行为不变）。
"""
import os
import shutil
import sys


def _is_frozen():
    """是否处于打包后的运行环境（兼容 PyInstaller 与 Nuitka standalone/onefile）"""
    return (bool(getattr(sys, 'frozen', False))
            or bool(getattr(sys, '_MEIPASS', None))
            or bool(getattr(sys, '__compiled__', False)))


def _bundle_root():
    """打包后内置资源所在的只读根目录"""
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        return meipass
    return os.path.dirname(os.path.abspath(sys.executable))


# leaffs 源码包目录（本模块位于 leaffs/ 顶层）
_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
_FROZEN = _is_frozen()

# BASE_DIR —— 资源根（web_page 页面/静态资源、内置 exe 依赖所在；原则上只读）
BASE_DIR = _bundle_root() if _FROZEN else _CODE_DIR

# APP_DIR / PROJECT_DIR —— 数据根（运行产物所在，始终可写、始终跟随主程序/项目根）
_ENV_PROJECT_ROOT = os.environ.get('LEAFFS_PROJECT_ROOT', '').strip()
if _ENV_PROJECT_ROOT:
    APP_DIR = os.path.abspath(_ENV_PROJECT_ROOT)
elif _FROZEN:
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(_CODE_DIR)
PROJECT_DIR = APP_DIR  # 兼容旧名：项目/应用根目录

UPLOAD_DIR = os.path.join(PROJECT_DIR, 'shared_files')   # 共享上传数据
CACHE_DIR = os.path.join(PROJECT_DIR, '.cache')          # 缩略图/文件夹大小等缓存
THUMB_DIR = os.path.join(CACHE_DIR, 'thumbs')
CONFIG_DIR = os.path.join(PROJECT_DIR, 'config')         # 服务端/下载器/账号等运行配置

# 数据根运行目录即时确保存在（原 ut_core import 期行为，语义不变）
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)


def find_bundled_exe(name):
    """按通用顺序定位内置依赖可执行文件（如 aria2c.exe / ffmpeg.exe）

    查找顺序：打包解包根 → 主程序所在目录 → 源码资源目录(_CODE_DIR) → PATH。
    找不到返回 None。
    """
    candidates = []
    if _FROZEN:
        candidates.append(os.path.join(_bundle_root(), name))
        candidates.append(os.path.join(APP_DIR, name))
    candidates.append(os.path.join(_CODE_DIR, name))
    for p in candidates:
        try:
            if os.path.isfile(p):
                return p
        except Exception:
            continue
    try:
        found = shutil.which(name)
        if found:
            return found
    except Exception:
        pass
    return None


def _migrate_legacy_config():
    """源码开发期：把旧位置(代码包 config/)里已有的运行配置一次性搬到数据层 config/。

    打包运行时旧位置不存在，直接跳过；从零首次运行也无需搬运。
    """
    if _FROZEN:
        return
    legacy_dir = os.path.join(_CODE_DIR, 'config')
    try:
        if not os.path.isdir(legacy_dir):
            return
        for fn in os.listdir(legacy_dir):
            src = os.path.join(legacy_dir, fn)
            dst = os.path.join(CONFIG_DIR, fn)
            if fn.endswith('.json') and os.path.isfile(src) and not os.path.exists(dst):
                shutil.copy2(src, dst)
    except Exception:
        pass


_migrate_legacy_config()
