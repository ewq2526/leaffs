# 踩坑对照表

出过的错都在这里。一行一个坑：**症状**是现场看到的样子，**根因**是为什么，
**拦在哪**说明有没有脚本能在它进镜像之前拦住。

第三列最重要：写 `preflight` / `postbuild` 的，`mkimage/build.sh` 每次自动跑；
写 `人工` 的只能靠这一节。

```sh
sh mkimage/preflight.sh       <仓库> <构建目录>   # 起构建前
sh mkimage/postbuild-check.sh <ISO>              # 出 ISO 后
```

流程本身见 `BUILD.md`。

## 一、构建流程

| 症状 | 根因 | 拦在哪 | 修法 |
|---|---|---|---|
| 重出 ISO 后镜像里还是旧文件，退出码却是 0 | `config/includes.chroot` 只在 chroot 阶段铺；`lb binary_chroot` 复制的是外层 `chroot/` | 人工 | 同一份内容也铺进外层 `chroot/`（`build.sh` 第一步） |
| `mount: mount point does not exist` | 出厂清理删掉了 `/home`，`image` 模式不再跑 chroot 阶段，挂载点没人建 | 人工 | 挂之前 `mkdir -p` 挂载点 |
| 构建被中断后 chroot 只剩几十个包，grub 都不在 | `lb binary` 收尾会摘掉 binary 阶段装的包；中断时摘到一半 | 人工 | 从环境备份还原（见 `BUILD.md` 第八节），别在原地继续 |
| ISO 从 1.2 GB 涨到 2 GB 以上 | 装包用的 `mount --bind` 本地仓库一直挂着，`cp -a chroot` 把整仓 deb 复制进镜像 | preflight | 装包时挂、复制 chroot 前卸掉 |
| squashfs 只有几 KB，ISO 却有好几百 MB | `binary_rootfs` 收尾删了 `chroot/chroot`；只删它的阶段戳再压，压的是空目录 | postbuild（大小下限） | 去掉 `binary_rootfs` 戳就必须接着重跑 `binary_chroot` |
| `dpkg-divert: error: rename involves overwriting … not allowed` | 撤转移时替身文件还在，`--rename --remove` 会覆盖它 | preflight 报条数 | 先删替身再撤转移；判据是 `start-stop-daemon` 是 ELF、本地转移为 0 |
| 构建卡在 `security.debian.org` | `lb config` 不覆盖旧值，安全更新是另一个路径 | preflight | 带两个镜像变量重跑：先 `lb clean --config` 再 `lb config` |
| `lb config` 静默忽略整份配置 | 钩子或 `auto/config` 没有执行位 | preflight | `chmod +x` |
| `auto/config: bad interpreter`、包名带 `\r` | 仓库检出成 CRLF | preflight（LF 规则） | `.gitattributes` 覆盖构建目录与 `*.txt` |
| 提交后底图打不开（字节数少 2） | `.gitattributes` 的 `text eol=lf` 把 png/jpg 当文本转换 | preflight | 补 `*.png`/`*.jpg binary` 规则 |
| 改了菜单/主题，出的还是旧的 | `iso` 模式没删 `binary_hooks`、`binary_grub_cfg` 阶段戳，钩子没重跑 | 人工 | 删对应的 `.build/binary_*` 戳 |
| ISO 里一直躺着上一版的底图 | 安装器目录用 `rsync` 铺，不删多余文件 | 人工 | 先 `rm -rf` 再铺 |

## 二、镜像内容（出厂状态）

| 症状 | 根因 | 拦在哪 | 修法 |
|---|---|---|---|
| Live 项报 `file /live/vmlinuz-… not found` | `--linux-packages none` 让 `binary_linux-image` 直接退出，`/live` 里没有内核 | preflight + postbuild | binary 钩子从 squashfs 里取出内核与 initrd 放进 `binary/live` |
| Live 项掉进 busybox | 显式给 `--bootappend-live` 会整体替换默认串，`boot=live` 丢了 | preflight | 自己带上 `boot=live components` |
| 主机名是 `localhost.localdomain` | 镜像里的 `/etc/hostname` 来自构建环境 | postbuild | 仓库铺 `etc/hostname`，构建时显式写入 |
| 镜像里有构建路径、构建机的 `/etc/hosts` | 复制 chroot 时整棵带进去 | postbuild | 清理步骤删掉，只留 `lists/partial` |
| 出厂态带着身份：SSH 主机密钥、非空 `machine-id` | 构建环境的身份被压进镜像 | postbuild | 钩子删密钥、`machine-id` 清零 |
| 镜像里留着 pip 缓存、旧字节码、构建日志 | 构建过程留在 chroot 里 | postbuild | 钩子清理；apt 日志在子目录，只删顶层会漏 |
| `/usr/sbin/start-stop-daemon` 是 `/bin/true` 的替身 | 构建期 dpkg 转移没撤 | postbuild | 见第一节那条转移 |
| 装 grub 时弹 conffile 冲突 | 镜像里带了 `/etc/default/grub`（conffile） | preflight | 镜像不带；装完之后由安装器写一份 |
| 装好的系统菜单里没有同盘的其它系统 | 没装 `os-prober`，或没关掉新版 grub 的开关 | preflight + postbuild | 主镜像清单加 `os-prober`，写 `GRUB_DISABLE_OS_PROBER=false` |
| 装完卡在 `Running /scripts/local-block` 后掉 shell | `initramfs.conf` 写 `MODULES=list`，认不到 NVMe / eMMC / virtio 盘 | 人工 | 用 `MODULES=most` |
| 应用服务起不来（avahi 报用户不存在） | 只 `rsync` 了文件，没跑包的 postinst，系统用户没建 | 人工 | 应用层用 `dpkg -i` 装 |
| `leaffs` 反复重启 | venv 缺包：pip 见系统已有就跳过，出厂系统里没有 | 人工 | `pip install --ignore-installed` |

## 三、安装器

| 症状 | 根因 | 拦在哪 | 修法 |
|---|---|---|---|
| `parted mkpart 0MiB …` 报"最近的位置是 0.02MiB" | GPT 表头占着盘最前面几十个扇区，分区不能从 0 起 | 人工 | 起点抬到 1 MiB，并用 `-a opt` 对齐 |
| 点色块不出编辑框 | 点击后先异步重画整个列表，随后写入的工具区被这次重画清空 | 人工 | 先重画、再挂工具区 |
| 装完重启 `camphor-firstboot.service` 失败 | 脚本按介质的路径找 `app.squashfs`，装好的系统里没有介质 | 人工 | 应用镜像套进主镜像，从盘上取 |
| 安装器 X 退出、后端被带走 | `session.sh` 去应用层的 venv 找解释器 | 人工 | 用主镜像自带的 `python3` + gi/WebKit |
| 装完还进安装器 | 目标系统里的入口脚本没删，第二阶段条件不成立 | 人工 | 第一阶段末尾删掉它 |

## 四、引导外观

| 症状 | 根因 | 修法 |
|---|---|---|
| 标题一直是点阵小字，换多大的字体都一样 | 自定义 `pf2` 通过 `loadfont` 加载了、也不报错，但主题里的 `label` 仍退回内置字体 | 标题改画成带透明通道的图，由 `image` 组件摆放 |
| 顶部正中多出一行 "GRUB Boot Menu" | 主题没写 `title-text`，GRUB 画它自带的标题 | 显式写 `title-text: ""` |
| 菜单只剩一条，重启后掉进固件界面 | 重排菜单时按行流式插入，插入那一刻目标块还没读到，整条菜单项丢了 | 先整段摘出来再插；`set default` 指向的标题必须存在 |
| 装好的系统里主题文件属主是普通用户、权限 777 | `rsync -a` 把宿主的权限原样带进镜像 | `--chown=root:root --chmod=D755,F644` |
| 菜单文字压在亮处看不清 | 底图亮部正好在菜单区 | 版式挪到暗处，菜单宽度只够包住条目文字 |

## 五、验证手段本身的坑

| 症状 | 根因 | 修法 |
|---|---|---|
| 截图工具打不开 QEMU 抓的图 | `screendump` 写的是 PPM，扩展名写 `.png` 也不改格式 | 按 PPM 解，或先转换 |
| 截图里红蓝互换，夜空变土黄 | PPM 是 BGR 顺序 | 转换时换回来 |
| 手敲内核参数、截图比对，看半天还是错的 | 参数容易插错行，屏幕未必渲染得出日志 | 用 `-kernel/-initrd/-append` 直接起，串口一次拿到全部输出 |
| ISO 起得来就以为成了 | 三件事要分开验：起得来、装得进去、装完起得来 | 每项单独给判据 |

## 六、脚本拦不住的两类

**一、退出码 0 的假成功。** 上面好几条都属于这一类：几 KB 的 squashfs、
没生效的 `includes.chroot`、被覆盖的配置。共同点是构建全程没有报错，ISO 时间戳也是新的。
判据不能是"构建跑完了"，只能是 `postbuild-check.sh` 列的那几条事实，加上实机跑一遍。

**二、阶段戳与续跑。** 一个构建目录同时是"构建环境"和"live 系统母本"，
任何一步手工补丁都要先问：**这处改动会不会在下一个阶段被换掉、删掉或覆盖。**
`binary_rootfs` 会删 `chroot/chroot`，`binary_chroot` 会换掉外层 `chroot`，
`binary_grub_cfg` 会覆盖 `binary/boot/grub`。这条问句比记性可靠。
