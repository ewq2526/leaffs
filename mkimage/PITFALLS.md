# 踩坑对照表

出过的错都在这里。一行一个坑：**症状**是现场看到的样子，**根因**是为什么，
**拦在哪**说明有没有脚本能在它进镜像之前拦住，**修法**是当时的处置。

三列里最重要的是「拦在哪」这一列。写着 `preflight` / `postbuild` 的，
`mkimage/build.sh` 每次都会自动跑，不用靠记性；写 `人工` 的只能靠这一节。

```sh
sh mkimage/preflight.sh       <仓库> <构建目录>   # 起构建前
sh mkimage/postbuild-check.sh <ISO>              # 出 ISO 后
```

流程本身见 `BUILD.md`。

## 一、构建流程（chroot / binary）

| 症状 | 根因 | 拦在哪 | 修法 |
|---|---|---|---|
| 重出 ISO 后镜像里还是旧文件，退出码却是 0 | `config/includes.chroot` 只在 chroot 阶段铺；`lb binary_chroot` 复制的是外层 `chroot/` | 人工 | 仓库内容同时铺进外层 `chroot/`（`build.sh` 第一步） |
| 跑完一轮，构建目录里没有可用的构建环境了 | `cache/bootstrap` 存在时，`binary_chroot` 把外层 `chroot` 换成干净 bootstrap，再把真正的环境挪进 `chroot/chroot`；`binary_rootfs` 收尾会删掉后者 | preflight 提醒 | 先 `rm -rf cache/bootstrap`，或者认下这一轮之后从环境备份还原 |
| ISO 从 1.7 GB 涨到 2.6 GB | 装包用的 `mount --bind` 本地仓库一直挂着，`cp -a chroot` 把整仓 deb 复制进镜像 | preflight | 装包时挂、复制 chroot 前卸掉 |
| squashfs 只有 4 KB，ISO 657 MB，退出码 0 | `binary_rootfs` 收尾删了 `chroot/chroot`；去掉它的阶段戳再压，mksquashfs 压的是空目录 | postbuild（大小下限） | 去掉 `binary_rootfs` 戳就必须接着重跑 `binary_chroot`，不能只压片 |
| `dpkg-divert: error: rename involves overwriting … not allowed` | 撤转移时替身文件还在，`--rename --remove` 会覆盖它 | preflight 报条数，压片前再核对 | 先删替身再撤转移；判据是 `start-stop-daemon` 是 ELF、本地转移为 0 |
| `E: Unable to locate package squashfs-tools` | `mksquashfs` 在 chroot 里跑，宿主机装了不算；binary 阶段的依赖要装在 chroot | 人工 | 按 `Check_package` 清单把 binary / installer 阶段依赖装进 chroot，离线时进本地仓库 |
| 续跑 `binary` 报 `File not found - …/localrepo/pool/…` | 只有 `binary_rootfs` 里挂本地仓库；它有阶段戳时续跑直接跳过挂载 | 人工 | 手动 `mount --bind` 到 `chroot/home/<用户>/localrepo`，跑完卸 |
| `binary_loadlin` 报找不到 `loadlin` | `LB_LOADLIN` 默认 true，Debian 13 已无此包 | `auto/config` 已关 | `--loadlin false` |
| 续跑时报某个包删不掉 | `chroot.installed_tmp_pkgs` 里留着上一次失败轮次的包名 | 人工 | 删掉那一行 |
| `binary_bootloader_splash` 缺 `librsvg2-bin` | 画启动画面要 `rsvg-convert` | 人工 | 装进本地仓库 |
| 构建卡在 `security.debian.org` 上 | `lb config` 不覆盖旧值；安全更新是另一个路径，要单独换 | preflight | 带两个镜像变量重跑：`lb clean --config` 后再 `lb config` |
| `lb config` 静默忽略整份配置，选项全是默认 | 钩子或 `auto/config` 没有执行位 | preflight | `chmod +x` |
| 提交后引导底图损坏（31023 → 31021 字节） | `.gitattributes` 的 `text eol=lf` 把 png 当文本做了换行转换 | preflight | 补 `*.png binary` 一类规则 |
| `auto/config: bad interpreter`、包名带 `\r`、apt 报找不到包 | 仓库开了 autocrlf，脚本与清单被检出成 CRLF | preflight（LF 规则） | `.gitattributes` 覆盖构建目录与 `*.txt` |

## 二、镜像内容（出厂状态）

| 症状 | 根因 | 拦在哪 | 修法 |
|---|---|---|---|
| Live 项报 `file /live/vmlinuz-… not found`，全屏只有一句 need to load the kernel | `--linux-packages none` 让 `binary_linux-image` 第一步就退出，`/live` 里没有内核与 initrd，而菜单已按内核版本名写好 | preflight + postbuild | binary 钩子从 `filesystem.squashfs` 里取出内核与 initrd 放进 `binary/live` |
| Live 项掉进 busybox，屏幕一片花 | 显式给 `--bootappend-live` 会整体替换默认串，`boot=live` 丢了；没有它 live-boot 不接管根挂载 | preflight | 自己带上 `boot=live components` |
| 主机名是 `localhost.localdomain` | 镜像里的 `/etc/hostname` 来自构建环境 | postbuild | 仓库铺 `etc/hostname`，构建时显式写入 |
| `/etc/hosts` 带着构建机的名字 | `chroot_hacks` 会删镜像里的 hosts，构建环境那份被压进去 | 人工 | 仓库铺与机器无关的 `etc/hosts` |
| 镜像里有构建路径（`/home/<用户>/localrepo`、`var/lib/apt/lists/_home_*`） | 复制 chroot 时整棵带进去 | postbuild | 清理步骤删掉，只留 `lists/partial` |
| 出厂态就带着身份：SSH 主机密钥、非空 `machine-id` | 构建环境的身份被压进镜像 | postbuild | 钩子里删密钥、`machine-id` 清零，首次启动由 systemd 生成 |
| 镜像里留着 pip 缓存、旧字节码、apt 与 dpkg 的构建日志 | 构建过程留在 chroot 里 | postbuild | 钩子清理；apt 日志在子目录，只删顶层会漏 |
| `/usr/sbin/start-stop-daemon` 是 `/bin/true` 的替身 | 构建期 dpkg 转移没撤 | postbuild | 见第一节那条转移 |
| 装 grub 时弹 conffile 冲突，无人值守停住 | 镜像里带了 `/etc/default/grub`，它是 conffile | preflight | 镜像不带；装完由收尾脚本按键合并 |
| 装好的系统没有软件源 | d-i 的 `apt-setup` 无条件用生成器产出的空 `sources.list.new` 覆盖 | 人工 | ISO 上放 `/camphor/sources.list`，收尾时回填 |
| 装好的系统菜单里没有同盘的其它系统 | 镜像里没装 `os-prober`；新版 grub 还要 `GRUB_DISABLE_OS_PROBER=false` 才会跑它 | preflight + postbuild | 包清单加 `os-prober`，收尾合并该键并重跑 `update-grub` |
| 装完起不来：内核起来了，卡在 `Running /scripts/local-block` 后掉 shell | `initramfs.conf` 写 `MODULES=list`，只收 SATA/USB 那套模块，认不到 NVMe / eMMC / virtio 盘 | 人工 | 改 `MODULES=most`（initrd 从 46 MB 涨到百 MB 级） |
| 装完服务全起不来，日志里一串 `in-target is already running` | 构建期的 dpkg 转移被压进镜像，d-i 再要转移时撞车，锁不释放 | 人工 | 重拷一份副本并按干净构建处理：撤转移、去掉 `00secure`/`00recommends`/`policy-rc.d`/`debian_chroot` |

## 三、安装器（preseed / late_command）

| 症状 | 根因 | 拦在哪 | 修法 |
|---|---|---|---|
| 语言、键盘根本不问 | `auto-install/enable=true` 让 localechooser 与 console-setup 整个组件不加载 | 人工 | 删掉 `auto-install/enable`、`debian-installer/locale`、`keyboard-configuration/xkb-keymap` |
| 分区概览一出现就被点掉，随即 `No root file system is defined` | `partman/choose_partition select finish` 预置的是概览屏的「完成」菜单项 | 人工 | 不预置任何 `partman` 键，分区交回安装者 |
| `late_command` 整段没跑，安装器退回主菜单，屏幕上完全看不出哪一步坏的 | preseed 里几十行 shell 被续行截断，`sh: syntax error: unterminated quoted string`，`07preseed` 返回 2 | preflight + postbuild | 收尾搬进 `/camphor/postinstall.sh`，`late_command` 只留一句 `sh /cdrom/camphor/postinstall.sh` |
| 装好的 `fstab` 只剩一行，ESP 与 swap 都不见了 | 自己 `printf > /target/etc/fstab` 覆盖了 partman 写好的那份 | 人工 | 不写 `fstab`；partman 那份本来就完整 |
| 安装器弹「缺固件」等确认 | 安装器环境里没有固件包 | 人工 | `hw-detect/load_firmware false`（治标：镜像里 `/firmware/*.deb` 是软链，为什么不被取走还没查） |
| 主机名输入框没有预填词 | `preseed.sh` 在加载每个文件**之前**读 `preseed/interactive`，写在后面的开关只对之后的 include 生效 | postbuild（查链路） | 拆三份：开 interactive → 给主机名默认值并关掉 → 其余静默答案 |

## 四、验证手段本身的坑

| 症状 | 根因 | 修法 |
|---|---|---|
| 逐字输入像卡死，一个口令要一分半 | `sendkey` 不产生输出，而截图封装每次都要读到超时才返回 | 按键只发不读（`read=False`） |
| 手敲 grub 参数、截图比对，看完还是错的 | 参数容易插错行，屏幕未必渲染得出日志 | 用 `-kernel/-initrd/-append` 直接起，串口一次拿到内核与 systemd 的全部输出 |
| ISO 起得来就以为成了 | 三件事要分开验：ISO 起得来、装得进去、装完起得来 | 每项单独给判据 |
| 安装停住，屏幕上只有主菜单 | d-i 的日志在第四个虚拟终端 | `ctrl-alt-f4` 看 `finish-install` / `preseed_command` / `log-output` |

## 五、脚本拦不住的两类

**一、退出码 0 的假成功。** 上面表里好几条都属于这一类：4 KB 的 squashfs、
没生效的 `includes.chroot`、被截断的 `late_command`。它们的共同点是构建全程没有报错，
ISO 时间戳也是新的。所以判据不能是「构建跑完了」，只能是 `postbuild-check.sh`
列的那几条事实，加上实机跑一遍。

**二、阶段戳与续跑。** 一个构建目录同时是「构建环境」和「live 系统母本」，
任何一步手工补丁都要问一句：**这处改动会不会在下一个阶段被换掉、删掉或覆盖。**
`binary_rootfs` 会删 `chroot/chroot`，`binary_chroot` 会换掉外层 `chroot`，
`binary_grub_cfg` 会覆盖 `binary/boot/grub`。手工按住其中一个阶段时，
这条问句比记性可靠。
