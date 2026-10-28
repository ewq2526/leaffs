# 安装器

装系统只走这一条路：介质启动 → 网页选分区与账号 → 展开系统 → 重启装应用。
这里写它由哪些文件组成、每一步实际做什么、失败时去哪看。
构建流程见 `BUILD.md`，坑表见 `PITFALLS.md`。

## 一、文件分布

```
介质 /camphor/installer/          只在介质上，不进 squashfs
├─ server.py                      后端：列盘、分区、安装、进度
├─ start.sh                       起一个独立的 X 会话
├─ session.sh                     X 里的会话：窗口管理器 + 后端 + WebKit 窗口
├─ webview.py                     用 WebKit 全屏显示安装页
├─ web/index.html                 安装页（三步）
├─ web/backdrop.jpg               页面背景，和引导菜单同一张
└─ deps.squashfs                  分区与解开镜像要用的工具
```

系统里只有一个一次性入口：`/usr/local/bin/camphor-installer-boot.sh`，
以及启动它的 `camphor-installer.service`。装完第一阶段会把这个脚本从**目标系统**里删掉。

## 二、第一阶段的启动链

```
GRUB 菜单 → 内核参数 camphor.installer=1
  → camphor-installer.service（条件就是这个参数）
  → /usr/local/bin/camphor-installer-boot.sh
       等 /dev/dri/card0（无界面模式跳过）
       从介质找 /camphor/installer，拷到 /run/camphor-installer
       loop 挂 deps.squashfs 到 /run/camphor-deps，把它加进 PATH 与 LD_LIBRARY_PATH
       校验 parted / unsquashfs 在
  → start.sh：xinit session.sh :0 -nolisten tcp vt1
  → session.sh：openbox → server.py 8090 → 等端口 → WebKit 全屏打开安装页
```

X 退出后 `start.sh` 停在 shell 里，不让 systemd 重新拉起 —— 否则就是"注入→启动→失败→重启"的死循环。

无界面模式（内核参数 `camphor.headless=1`）不启动 X，只把后端起在 `0.0.0.0:8090`，
用来在没有可靠显示的机器上跑接口验证。正式安装的后端只听 `127.0.0.1`。

## 三、网页后端

| 接口 | 用途 |
|---|---|
| `GET /api/disks` | 列盘与每块盘的分区、空闲区 |
| `GET /api/progress` | 当前步骤、日志行、是否完成或失败 |
| `POST /api/mkpart` | 在空闲区建分区 |
| `POST /api/rmpart` | 删分区 |
| `POST /api/install` | 开始安装（设备、分区行号、账号、口令） |
| `POST /api/reboot` | 装完重启 |

### 分区数据

一律解析 `parted -s -j <盘> unit MiB print free` 的 JSON，不解析 `-m` 文本
（空闲行的段数在不同版本里不一样）。`number: 0` 就是空闲区，`type-uuid` 用来认分区类型：
EFI 系统分区、BIOS boot、MSR、Windows 恢复、Windows 数据、LDM、Linux、swap 各有固定 GUID。

列表里每行带一个 `rowid`，是渲染顺序而不是分区号 —— 空闲区没有分区号，界面上点的是 rowid。

### 建分区

- 空盘没有分区表时先 `mklabel gpt`
- 起点不足 1 MiB 抬到 1 MiB：GPT 表头占着盘最前面那些扇区，从 0 开始 parted 会拒绝
- 用 `-a opt` 让 parted 自己对齐
- 起始 512 MiB 的 fat32 并 `set esp on` 就是 EFI 系统分区

### 安装

```
选中的行如果是已有分区 = 这块要被格式化并重新分区，界面会描红提示
1  读布局，定位选中的那块（分区或空闲区）
2  选中已有分区：先把它删掉，那块空间重新分成「交换 + 根」；选空闲区则直接用
3  在这块空间的前部切 128 MiB 当专用交换分区（见下）—— 必须在 ESP 与根之前切
4  没有 ESP 就从剩下的前部再切 512 MiB，有现成的就复用
5  umount 目标分区（live 的自动挂载会挡住 mkfs）
6  剩下的空间全部给根分区，mkfs.ext4 -F -L camphor
7  mount 到 /target
8  unsquashfs 主镜像到 /target
9  挂 ESP 到 /target/boot/efi（要放在展开之后：先挂会被写进引导分区）
10 写 fstab：根与 ESP 都用 UUID，交换分区那一行按优先级 10 写进去
11 写 /etc/apt/sources.list：介质上那份，装好的系统才有 apt 源
12 chroot 建账号（useradd + chpasswd，加进 sudo 组）
13 chroot grub-install --target=x86_64-efi --bootloader-id=camphor
14 写 /etc/default/grub（发行版名、超时、内核参数、主题、gfxterm）
15 chroot update-grub
16 复制 shim 与 grub 到 ESP 的 EFI/BOOT/BOOTX64.EFI
17 删掉目标系统里的 camphor-installer-boot.sh
```

**专用交换分区**：内存被写尽时，内核要能把页换出去才能继续干活（回收、唤醒、给清理内存的
那批帮手腾出运行空间）；没有地方落页就只能杀进程，界面那一支被杀掉就是整屏卡住。
所以安装时从**用户选中的那块安装分区的前部**切 128 MiB 出来做交换分区，写进 fstab，
优先级 10 —— 低于 zram 的 100，平时用压缩内存，压不动了内核才落到它上面。

为什么是独立分区而不是根分区里的交换文件：交换文件要走 ext4 的写路径 —— 分配块、记日志、
writeback —— 这些自身就要占内存，极端情况下那条路先卡住。裸分区中间没有文件系统，
内核直接读写。

切之前会算一遍：`128 MiB + （没 ESP 时）512 MiB + 根分区 4096 MiB` 必须放得下，
放不下就不切（安装日志里写明），不为了交换区把系统装不进去。

第 15 步是兜底：固件找不到 NVRAM 启动项时会走 removable media 路径（`\EFI\BOOT\BOOTX64.EFI`），
只写 `EFI/camphor/` 的话那块盘在有些机器上起不来。

第 16 步是两阶段的开关：目标系统里那个脚本还在，第二阶段的单元条件就不成立；
删掉之后，重启才会进第二阶段。

成功之后重启前不做别的收尾 —— `/api/reboot` 会 `umount -R /target`、`sync`，再 `reboot -f`。

## 四、第二阶段

`camphor-firstboot.service` 的条件是「入口脚本不存在」且「自己这个脚本存在」，
所以它只在装好之后的**第一次从盘启动**时跑一次：

```
1  挂 /opt/camphor/app.squashfs 到 /run/camphor-app
2  dpkg -i pool/*.deb        应用依赖；postinst 建 avahi、sshd 这些系统用户
3  rsync -aHAX root/ /       应用文件、单元、venv
4  账号回填                    ksmbd 的 force user/valid users 与 filemgr 的 User/Group
                              换成安装时建的账号（镜像里是 nobody 占位）
5  按 /etc/systemd/camphor-units.list 一次 enable + 一次 start（清单里只有常驻服务）
6  卸挂载、删应用镜像、disable 并删掉自己与单元
```

第 2 步必须走 dpkg：`avahi`、`sshd` 这些系统用户是包的 postinst 建的，只覆盖文件服务起不来。

失败时的表现：挂载或铺文件失败会保留脚本、下次开机再试；
dpkg 失败只记日志（`/tmp/firstboot-dpkg.log`）并继续，其余步骤照跑。

## 五、界面

三步：分区 → 账号 → 安装。

- 分区页面把布局画成一条色块带：已分区、EFI 系统分区、空闲区各一色，块里写类型与大小
- 点空闲块：填大小 → 建分区；点已有块：可以删，也可以直接选它当系统位置
- 选中带数据的分区时，选中框转红并把"会被格式化"写在旁边
- 识别出的特殊分区（EFI、Windows 恢复、MSR、LDM、swap）**都能选**，只是给警示
- 根分区小于 4096 MiB 不让继续
- 账号页：用户名不能以数字开头，口令必填、不限长度
- 安装页：步骤、日志、失败原因都在同一屏

## 六、排查

| 现象 | 去哪看 |
|---|---|
| 黑屏、停在控制台 | `/dev/tty1` 上的安装器输出；`start.sh` 退出时会写原因 |
| 装到一半失败 | 安装页的日志区，同时 `server.py` 的 stderr 在 `/dev/tty1` |
| 第二阶段没装成 | `journalctl -b -u camphor-firstboot`，dpkg 细节在 `/tmp/firstboot-dpkg.log` |
| 分区编辑不对 | 后端日志里是 parted 的原始 JSON，对照它看 rowid 与空闲区 |
