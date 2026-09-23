# -*- coding: utf-8 -*-
"""路径定位 —— 运行根/资源根/各数据目录的单一数据源。

历史：定位逻辑原散落在 utils_core/ut_core.py 顶部（v1.0.5 分层重构后为 leaffs/utils/core.py），
重构收敛到本模块。曾经的"经 utils/core.py 转口"已于 2026-09-22 收口 —— 需要路径常量
一律直连本模块，别再转手。

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
# 上传临时目录（LF-22）：上传先写临时文件、全部字节成功后 os.replace 原子落位。
# 临时文件**必须落在用户看不见的地方** —— 原先写成 `<目标>.part.<线程id>.<纳秒>` 直接躺在
# 共享目录里，于是它出现在文件列表、搜索、文件夹大小统计里，还能被直链下载
# （用户看到的就是"一个正在写入、大小还在涨的怪文件"，而删它会被 Windows 的写句柄挡住）。
# 放在 UPLOAD_DIR 之下而不是 CACHE_DIR：os.replace 要求同盘（CACHE_DIR 同盘但不保证），
# 且这样它对配额口径的影响与原来一致。
UPLOAD_TMP_DIRNAME = '.uploads'
UPLOAD_TMP_DIR = os.path.join(UPLOAD_DIR, UPLOAD_TMP_DIRNAME)
CACHE_DIR = os.path.join(PROJECT_DIR, '.cache')          # 缩略图/文件夹大小等缓存
THUMB_DIR = os.path.join(CACHE_DIR, 'thumbs')
CONFIG_DIR = os.path.join(PROJECT_DIR, 'config')         # 服务端/下载器/账号等运行配置

# 服务器挂载区：与 `public/`（公共目录）同级的一级目录，磁盘上是空壳 ——
# 里面的每个条目由分享映射表登记（本机路径映射，见 share/mappings.py）。
# 它是只读来源：内容在 LeafFS 之外，写操作一律拒（resolve_rel 的只读位）。
MOUNT_DIRNAME = 'mounts'
MOUNT_DIR = os.path.join(UPLOAD_DIR, MOUNT_DIRNAME)

# 数据根运行目录即时确保存在（原 ut_core import 期行为，语义不变）
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(UPLOAD_TMP_DIR, exist_ok=True)   # 上传临时目录（对用户不可见，见上）
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(MOUNT_DIR, exist_ok=True)


def is_upload_tmp_entry(name):
    """目录项名是否就是上传临时目录本身（列表/扫描时用它跳过）"""
    return name == UPLOAD_TMP_DIRNAME


def is_upload_tmp_relpath(rel):
    """相对共享根的路径是否落在上传临时目录里。

    判定收在**这一个地方**：所有面向用户的读取路径（列表 / 搜索 / 下载 / 缩略图 /
    文件夹大小统计）都调它。各处各写一份条件的写法必然漏掉某一处 —— `.part` 当初
    就是这么漏出来的（写的时候只想着"上传"，没想过列表和搜索也在读同一个目录）。
    """
    r = (rel or '').replace('\\', '/').strip('/')
    return r == UPLOAD_TMP_DIRNAME or r.startswith(UPLOAD_TMP_DIRNAME + '/')


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
