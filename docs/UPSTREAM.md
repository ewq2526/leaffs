# 与上游 leaffs 的关系

Camphor 的根目录就是 [`leaffs`](https://gitee.com/ewq2526/leaffs.git) 仓库的一条分支。应用是系统的原生组件，不是外部依赖：它带着系统需要的改动，也带着系统层。上游主干 `origin/main` 是纯应用，本分支 `camphor` 是上游加系统层与这些改动。

## 基准

上游提交 `cb1aacb`（2026-10-22）。基准之后上游的提交都还不在这里。上游的版本标签 `v1.0.4`、`v1.0.5`、`v1.0.6` 都在基准之前。

## 本分支相对上游的改动

### 应用（`leaffs/`）

| 位置 | 改动 |
|---|---|
| `system/` （新增 4 个文件） | 设备面板的采集与无线控制：`host.py` 读 `/proc`、`/sys` 与 `systemctl`，`wifi.py` 走 `wpa_cli` |
| `web_page/system/` （新增 2 个文件） | 面板页面，中英各一份 |
| `server/handler.py` | 注册 `/system`、`/api/system`、`/api/wifi/status`、`/api/wifi/scan`、`/api/wifi/connect`，并把这几个路径加进 `_SUPER_ADMIN_ONLY_GET` / `_SUPER_ADMIN_ONLY_POST` |
| `app.py` | kiosk 形态（`LEAFFS_KIOSK=1` 时整屏无边框）、首帧底色、整页缩放（`LEAFFS_ZOOM`，默认 1.25） |
| `web_page/common/app.js` | 非 `super_admin` 时隐藏「系统」入口 |
| `web_page/common/style.css` | `color-scheme` 声明、移除页面入场动画、「系统」入口贴到导航右侧 |
| 20 个页面 html | 导航栏各加一行 `<a href="/system">系统</a>` |

### 系统层

`etc/`、`usr/`、`boot/` 上游没有。`docs/` 上游只有应用的两份（`ARCHITECTURE`、`TRANSLATION_GLOSSARY`），这里多了应用说明 `LEAFFS` 与本文件。`for-distribution/` 上游放的是 Windows 包与安卓的许可材料，这里换成了系统的第三方许可登记。根目录 README 与 CHANGELOG 也改成写系统。

### 相对上游删掉的

`android/` 57 个文件（安卓客户端，系统用不上），以及上游 `for-distribution/` 的 24 个文件（Windows 与安卓的分发材料，换成本仓库这套）。这些文件在上游还在继续改，所以整树合并会比按路径打补丁麻烦得多。

## 拉取上游更新

```powershell
git fetch origin
git diff cb1aacb origin/main -- leaffs requirements.txt | git apply -3
```

只把上游对应用目录与依赖清单的改动打进来，`android/` 与系统层不参与。与本地改动撞上的行会留下冲突标记，逐个看。

打完以后：

1. `git apply` 无冲突就跑测试：`python -m pytest`
2. 把本文件里的基准 `cb1aacb` 换成 `origin/main` 当时的新提交
3. `requirements.txt` 跟着变过的话，重建 `/opt/leaffs/venv` 并重装依赖
4. 更新日志分两份：上游那份整份覆盖到 `docs/CHANGELOG-leaffs.md` 与 `.en.md`（`git show origin/main:CHANGELOG.md`），其中与应用有关的新条目摘进根目录 `CHANGELOG.md` 的「未发布」。根目录那份是系统自己的日志，不在自动打补丁的路径里，也不该跟着上游自动变。

两条不要做的事：

- **不要全量 `git merge origin/main`**：`android/` 那 57 个删除会变成每次合并都要处理的 modify/delete 冲突。
- **不要 `git checkout origin/main -- leaffs`**：那会用上游那版静默覆盖上面的改动，而且不报错。

上游新增页面时，导航里的 `/system` 入口不会自动带过去，要补一行。
