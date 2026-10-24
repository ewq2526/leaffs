#!/usr/bin/env python3
"""安装器的本地服务：只监听 127.0.0.1，给安装页提供数据、执行安装。

接口：
  GET  /api/disks     列盘（parted -m print free，每行一个 JSON 对象）
  POST /api/install   开始安装 {"device": "/dev/vda", "rowid": 3}
  GET  /api/progress  进度与日志
  POST /api/reboot    装完重启
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
TARGET = "/target"
# 应用层镜像挂在这里装：装的时候一次性铺进目标盘，不留给首次启动
APPMNT = "/run/camphor-app"

STATE = {
    "running": False,
    "done": False,
    "error": "",
    "step": "",
    "lines": [],
}
LOCK = threading.Lock()


def add_line(msg):
    with LOCK:
        STATE["lines"].append(msg)
    print("installer:", msg, flush=True)


def set_state(**kw):
    with LOCK:
        STATE.update(kw)


def run(cmd, check=True):
    """跑一条命令；工具缺失时明确报出来，别让页面只是空着。

    check=False 用于辅助步骤（partprobe、update-grub 之类）：
    那些命令缺了不该把整个安装打断。
    """
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        if check:
            raise RuntimeError("缺少命令 %s（镜像里没装？）" % cmd[0])
        return ""
    if check and p.returncode != 0:
        raise RuntimeError("%s 失败：%s" % (" ".join(cmd), (p.stderr or p.stdout).strip()[:400]))
    return p.stdout


# GPT 分区类型 GUID 到人类可读名字。用来识别「这块不是普通数据分区，别乱动」。
# 数值取自 GPT 规范与 Windows/Intel 的公开定义。
PART_TYPES = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "esp",          # EFI 系统分区
    "21686148-6449-6e6f-744e-656564454649": "bios-boot",    # BIOS 引导
    "e3c9e316-0b5c-4db8-817d-f92df00215ae": "msr",          # Microsoft 保留
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac": "win-recovery", # Windows 恢复
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": "win-data",     # Windows 基本数据
    "5808c8aa-7e8f-42e0-af85-e9c9ec22e798": "ldm",          # 动态磁盘元数据
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "linux",        # Linux 文件系统
    "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f": "swap",         # Linux swap
    "4f68bce3-e8cd-4db1-96e7-fba2a1b7d78": "linux-root",    # 旧式 Linux root
}

# 这些分区不能装系统，也不该随手删：动了别的系统就起不来。
PROTECTED = {"esp", "bios-boot", "msr", "win-recovery", "ldm", "swap"}


def fs_usage(device, fstype):
    """ext 系列的使用率（百分比）。其他文件系统没有可靠又不挂载的读法，返回 None。"""
    if fstype not in ("ext2", "ext3", "ext4"):
        return None
    try:
        p = subprocess.run(["dumpe2fs", "-h", device], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    total = free = None
    for line in p.stdout.splitlines():
        if line.startswith("Block count:"):
            total = int(line.split(":")[1])
        elif line.startswith("Free blocks:"):
            free = int(line.split(":")[1])
    if total and free is not None and total > 0:
        return int(round((total - free) * 100.0 / total))
    return None


def list_disks():
    """列盘：每块盘的每一行（分区与未分配空间）。"""
    disks = []
    seen = []
    boot_medium = find_source_device()
    for name in sorted(os.listdir("/dev")):
        if not re.fullmatch(r"(sd|vd|nvme\d+n)\w*", name):
            continue
        path = "/dev/" + name
        if not os.path.exists(path):
            continue
        seen.append(path)
        if os.path.exists("/sys/class/block/%s/partition" % name):
            continue
        # 正在跑的那块介质不进列表：装到它上面等于把安装源格式化掉。
        if boot_medium and path == boot_medium:
            continue

        rows = []
        size = ""
        # 用 JSON 输出解析：parted 的 -m 格式字段会随版本变，行尾分号、
        # 空闲行的段数都不一样（这里踩过）。JSON 里 number=0 就是空闲区。
        p = subprocess.run(
            ["parted", "-s", "-j", path, "unit", "MiB", "print", "free"],
            capture_output=True, text=True,
        )
        blob = p.stdout
        if "{" in blob:
            blob = blob[blob.find("{"):]
        disk_json = {}
        try:
            disk_json = json.loads(blob).get("disk", {}) if blob.strip() else {}
        except ValueError:
            disk_json = {}
        if not disk_json:
            # 真读不出来就别把这块盘列出来，让人看到的是"读不到"而不是错的布局
            raise RuntimeError("parted 读不出 %s：%s" % (path, (p.stderr or p.stdout).strip()[:200]))
        size = disk_json.get("size", "")
        rowid = 0
        for part in disk_json.get("partitions", []):
            num = part.get("number", 0)
            is_free = part.get("type") == "free"
            fstype = "" if is_free else (part.get("filesystem") or "")
            tuuid = (part.get("type-uuid") or "").lower()
            ptype = PART_TYPES.get(tuuid, "")
            used = None
            if not is_free and fstype and num:
                used = fs_usage(partdev(path, num), fstype)
            rowid += 1
            rows.append({
                "rowid": rowid,
                "index": num if num else None,
                "start": part.get("start", "").replace("MiB", ""),
                "end": part.get("end", "").replace("MiB", ""),
                "size": part.get("size", "").replace("MiB", ""),
                "fstype": fstype,
                "free": is_free,
                "name": part.get("name", ""),
                "flags": ",".join(part.get("flags", [])),
                "used_pct": used,
                "ptype": ptype,
                "protected": ptype in PROTECTED,
            })
        disks.append({"device": path, "size": size, "rows": rows})
    return disks, seen


def ensure_label(device):
    """空盘没有分区表时先建 GPT：mkpart 在无标签的盘上会直接失败。"""
    p = subprocess.run(["parted", "-s", "-j", device, "print"], capture_output=True, text=True)
    blob = p.stdout
    has_label = '"label": "gpt"' in blob or '"label": "msdos"' in blob
    if not has_label:
        run(["parted", "-s", device, "mklabel", "gpt"])


def layout(device):
    """当前布局：分区与空闲区，按 MiB 给起止。"""
    disks, _ = list_disks()
    for d in disks:
        if d["device"] == device:
            return d
    return {"device": device, "size": "", "rows": []}


def parse_size(text):
    """把 parted JSON 里的 "20480MiB" 之类换成数字（MiB）。"""
    m = re.match(r"([0-9.]+)\s*([KMGTP]i?B)?", (text or "").strip())
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = (m.group(2) or "MiB").upper()
    factor = {"B": 1 / 1048576.0, "KB": 1 / 1024.0, "KIB": 1 / 1024.0,
              "MB": 1.0, "MIB": 1.0, "GB": 1024.0, "GIB": 1024.0,
              "TB": 1048576.0, "TIB": 1048576.0}.get(unit, 1.0)
    return val * factor


def mkpart(device, start, end):
    """新建一个分区。start/end 是 MiB，end 可以是 100%。

    GPT 自己占着盘最前面的一点空间，分区不能从 0 MiB 起 —— parted 会拒绝，
    报 “closest location we can manage is 0.02MiB”。这里统一抬到 1 MiB，
    顺带按 1 MiB 对齐，分区起止都落在整齐的边界上。
    """
    ensure_label(device)
    s = int(float(start))
    if s < 1:
        s = 1
    if str(end) == "100%":
        e = "100%"
    else:
        e = int(float(end))
        if e <= s:
            raise RuntimeError("结束位置 %d MiB 不大于起始位置 %d MiB" % (e, s))
        e = "%dMiB" % e
    run(["parted", "-s", "-a", "opt", device, "unit", "MiB", "mkpart", "primary",
         "%dMiB" % s, e])
    run(["partprobe", device], check=False)
    time.sleep(1)
    return layout(device)


def rmpart(device, index):
    """删一个分区。挂着的先卸，不然内核不认。"""
    dev = partdev(device, index)
    unmount_device(dev)
    run(["parted", "-s", device, "rm", str(index)])
    run(["partprobe", device], check=False)
    time.sleep(1)
    return layout(device)


def partdev(device, num):
    """分区设备名：/dev/vda + 1 → /dev/vda1；/dev/nvme0n1 + 1 → /dev/nvme0n1p1"""
    if re.match(r"^/dev/nvme\d+n\d+$", device):
        return "%sp%s" % (device, num)
    return "%s%s" % (device, num)


def is_esp(device, num):
    try:
        out = run(["parted", "-s", "-m", device, "unit", "MiB", "print"], check=False)
    except RuntimeError:
        return False
    for line in out.splitlines():
        parts = [p.strip().rstrip(";") for p in line.split(":")]
        if len(parts) < 7 or parts[0] != str(num):
            continue
        fstype = parts[4].lower()
        flags = parts[6].lower()
        return ("esp" in flags or "boot" in flags) and fstype.startswith("fat")
    return False


def find_squashfs():
    """找 live 的 squashfs 映像，必须是一个普通文件。

    注意 /run/live/rootfs/filesystem.squashfs 是 live 的**挂载点目录**，
    不是映像本身；映像在介质上的 live/ 目录里。所以这里按 isfile 过滤。
    """
    pats = [
        "/run/live/medium/live/*.squashfs",
        "/lib/live/mount/medium/live/*.squashfs",
        "/run/live/medium/camphor/live/*.squashfs",
        "/cdrom/live/*.squashfs",
        "/run/live/rootfs/*.squashfs",
    ]
    for p in pats:
        for hit in sorted(glob.glob(p)):
            if os.path.isfile(hit):
                return hit
    return ""


def find_app():
    """找介质上的应用层镜像（/camphor/app.squashfs）。"""
    for p in ("/camphor/app.squashfs",
              "/run/live/medium/camphor/app.squashfs",
              "/lib/live/mount/medium/camphor/app.squashfs",
              "/cdrom/camphor/app.squashfs"):
        if os.path.isfile(p):
            return p
    return ""


def find_sources_list():
    """介质上那份装好系统要用的软件源（/camphor/sources.list）。"""
    for p in ("/camphor/sources.list",
              "/run/live/medium/camphor/sources.list",
              "/lib/live/mount/medium/camphor/sources.list",
              "/cdrom/camphor/sources.list"):
        if os.path.isfile(p):
            return p
    return ""


def _mount_source(mountpoint):
    """某个挂载点背后的设备（/proc/mounts 里的第一列）。"""
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == mountpoint and parts[0].startswith("/dev/"):
                    return parts[0]
    except OSError:
        pass
    return ""


def _parent_disk(dev):
    """分区名 → 整块盘的名字：去掉末尾的分区号，NVMe 这类先去掉 p 再取。"""
    name = os.path.basename(dev)
    if not os.path.exists("/sys/class/block/%s/partition" % name):
        return dev
    if re.match(r"nvme\d+n\d+p\d+$", name):
        return "/dev/" + name.rsplit("p", 1)[0]
    return "/dev/" + re.sub(r"\d+$", "", name)


def find_source_device():
    """启动介质所在的那块盘。

    列盘时要把它排除掉：正在跑的介质被格式化了，安装会在中途失去镜像来源，
    而且那块 U 盘也就废了。
    """
    for mp in ("/run/live/medium", "/lib/live/mount/medium", "/cdrom",
               "/run/live/rootfs"):
        dev = _mount_source(mp)
        if dev:
            return _parent_disk(dev)
    return ""


def unmount_device(dev):
    """把目标分区上已有的挂载卸掉。

    live 系统启动后有自己的自动挂载，分区一旦可识别就会被挂到 /media 下；
    不先卸，mkfs 会拒绝在已挂载的设备上建文件系统。
    """
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        src, mnt = parts[0], parts[1]
        if os.path.realpath(src) != os.path.realpath(dev):
            continue
        subprocess.run(["umount", "-l", mnt], capture_output=True)
        add_line("卸载 %s（原挂载于 %s）" % (dev, mnt))


def bind_mounts():
    for d in ("dev", "proc", "sys"):
        src = "/" + d
        dst = os.path.join(TARGET, d)
        os.makedirs(dst, exist_ok=True)
        subprocess.run(["mount", "--bind", src, dst], capture_output=True)
    # efivars 单独再挂一次：grub-install 要靠它写 NVRAM 启动项，
    # 光靠 /sys 的 bind 在有的环境里 chroot 内看不到。
    efivars = "/sys/firmware/efi/efivars"
    if os.path.isdir(efivars):
        dst = os.path.join(TARGET, "sys/firmware/efi/efivars")
        os.makedirs(dst, exist_ok=True)
        subprocess.run(["mount", "--bind", efivars, dst], capture_output=True)


def install_removable_fallback():
    """在 ESP 的 EFI/BOOT/ 下再放一份 shim + grub。

    固件找不到 NVRAM 启动项时会走 removable media 路径（\\EFI\\BOOT\\BOOTX64.EFI）；
    只写 EFI/camphor/ 的话，NVRAM 没写进去这块盘就起不来。
    """
    efi = os.path.join(TARGET, "boot/efi")
    src = os.path.join(efi, "EFI/camphor")
    dst = os.path.join(efi, "EFI/BOOT")
    if not os.path.isdir(src):
        return
    os.makedirs(dst, exist_ok=True)
    for name, target in (("shimx64.efi", "BOOTX64.EFI"),
                         ("grubx64.efi", "grubx64.efi"),
                         ("grub.cfg", "grub.cfg")):
        s = os.path.join(src, name)
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(dst, target))
    add_line("写入兜底引导：EFI/BOOT/BOOTX64.EFI")


def install_worker(device, rowid, username="", password=""):
    try:
        set_state(running=True, done=False, error="", step="读取磁盘布局", lines=[])
        disks, _ = list_disks()
        row = None
        for d in disks:
            if d["device"] != device:
                continue
            for r in d["rows"]:
                if r["rowid"] == rowid:
                    row = r
        if row is None:
            raise RuntimeError("选中的行不存在（磁盘布局变了？）")
        if not row["free"] and is_esp(device, row["index"]):
            raise RuntimeError("这一行是 EFI 系统分区，不能把系统装进去")

        # 大小检查：系统解开约 2.4 GB，应用层再占一点，塞不进去就别开始
        need = 4096
        got = int(float(row["size"] or 0))
        if got < need:
            raise RuntimeError("这块只有 %d MiB，系统至少要 %d MiB" % (got, need))

        root_dev = ""
        if row["free"]:
            set_state(step="新建分区")
            add_line("新建分区：%s %s – %s MiB" % (device, row["start"], row["end"]))
            # 空闲区可能一直延伸到盘尾，那种情况用 100% 让 parted 自己算；
            # 起点由 mkpart 抬到 1 MiB 并对齐，GPT 表头占着最前面那一点。
            disk = layout(device)
            at_end = abs(float(row["end"]) - parse_size(disk.get("size", "0"))) < 1.0
            after = mkpart(device, row["start"], "100%" if at_end else row["end"])
            nums = [r["index"] for r in after["rows"] if not r["free"] and r["index"]]
            if not nums:
                raise RuntimeError("分区建完却没有看到新分区")
            root_dev = partdev(device, max(nums))
            add_line("根分区：%s" % root_dev)
        else:
            root_dev = partdev(device, row["index"])

        if not os.path.exists(root_dev):
            raise RuntimeError("分区设备 %s 不存在" % root_dev)

        set_state(step="格式化 %s" % root_dev)
        unmount_device(root_dev)
        add_line("创建文件系统：%s（ext4）" % root_dev)
        run(["mkfs.ext4", "-F", "-L", "camphor", root_dev])

        set_state(step="挂载到 /target")
        os.makedirs(TARGET, exist_ok=True)
        run(["mount", "-t", "ext4", root_dev, TARGET])

        # ESP：同一块盘上已有的复用，没有就新建
        esp_dev = ""
        for line in run(["parted", "-s", "-m", device, "unit", "MiB", "print"]).splitlines():
            parts = [p.strip().rstrip(";") for p in line.split(":")]
            if len(parts) >= 7 and parts[0].isdigit() and is_esp(device, parts[0]):
                esp_dev = partdev(device, parts[0])
                add_line("使用已有 EFI 系统分区：%s" % esp_dev)
                break

        if not esp_dev:
            set_state(step="新建 EFI 系统分区")
            free = None
            for line in run(["parted", "-s", "-m", device, "unit", "MiB", "print", "free"]).splitlines():
                parts = [p.strip().rstrip(";") for p in line.split(":")]
                if len(parts) >= 5 and parts[4] == "free":
                    sz = float(parts[3].replace("MiB", ""))
                    if sz >= 600:
                        free = parts
                        break
            if free:
                start = float(free[1].replace("MiB", ""))
                run(["parted", "-s", device, "unit", "MiB", "mkpart", "primary", "fat32",
                     "%sMiB" % start, "%sMiB" % (start + 512)])
                run(["partprobe", device], check=False)
                time.sleep(2)
                out = run(["parted", "-s", "-m", device, "unit", "MiB", "print"])
                last = ""
                for line in out.splitlines():
                    parts = [p.strip().rstrip(";") for p in line.split(":")]
                    if len(parts) >= 5 and parts[0].isdigit():
                        last = parts[0]
                esp_dev = partdev(device, last)
                run(["parted", "-s", device, "set", last, "esp", "on"], check=False)
                unmount_device(esp_dev)
                run(["mkfs.vfat", "-F", "32", "-n", "CAMPHOR", esp_dev])
                add_line("创建 EFI 系统分区：%s（512 MiB，fat32）" % esp_dev)
            else:
                add_line("没有可用空间创建 EFI 系统分区，UEFI 引导将不可用")

        squash = find_squashfs()
        if not squash:
            raise RuntimeError("找不到 live 的 squashfs 映像")
        set_state(step="复制系统文件（%s）" % os.path.basename(squash))
        add_line("展开系统镜像：%s → %s" % (squash, TARGET))
        run(["unsquashfs", "-f", "-no-progress", "-d", TARGET, squash])

        # ESP 要在解压之后再挂：先挂的话 unsquashfs 会往 ESP 里写，
        # 把一块本来干净的引导分区弄脏。
        if esp_dev:
            os.makedirs(os.path.join(TARGET, "boot/efi"), exist_ok=True)
            run(["mount", "-t", "vfat", esp_dev, os.path.join(TARGET, "boot/efi")])

        set_state(step="写 fstab")
        root_uuid = run(["blkid", "-o", "value", "-s", "UUID", root_dev]).strip()
        lines = [
            "# <file system> <mount point> <type> <options> <dump> <pass>",
            "UUID=%s / ext4 defaults,noatime 0 1" % root_uuid,
        ]
        if esp_dev:
            esp_uuid = run(["blkid", "-o", "value", "-s", "UUID", esp_dev]).strip()
            lines.append("UUID=%s /boot/efi vfat umask=0077 0 1" % esp_uuid)
        os.makedirs(os.path.join(TARGET, "etc"), exist_ok=True)
        with open(os.path.join(TARGET, "etc/fstab"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        add_line("写入 /etc/fstab")

        # 软件源：介质上带着一份装好系统要用的源，装完要落到盘上。
        # 少了它，装好的机器能跑但没有 apt 源，装不了任何东西。
        src = find_sources_list()
        if src:
            os.makedirs(os.path.join(TARGET, "etc/apt"), exist_ok=True)
            shutil.copyfile(src, os.path.join(TARGET, "etc/apt/sources.list"))
            os.chmod(os.path.join(TARGET, "etc/apt/sources.list"), 0o644)
            add_line("写入 /etc/apt/sources.list")
        else:
            add_line("介质上没有 sources.list，装好的系统需要手动补软件源")

        # 账号：镜像出厂状态里没有任何可登录的用户（root 是锁定的），
        # 不在这里建一个，装完就没人能进系统。用户名与口令由安装界面填。
        set_state(step="创建账号")
        bind_mounts()
        if username:
            add_line("创建用户：%s" % username)
            p = subprocess.run(["chroot", TARGET, "useradd", "-m", "-s", "/bin/bash", "-G", "sudo", username],
                               capture_output=True, text=True)
            if p.returncode != 0:
                add_line("useradd 失败：%s" % (p.stderr or p.stdout).strip()[:200])
            else:
                p = subprocess.run(["chroot", TARGET, "chpasswd"],
                                   input="%s:%s\n" % (username, password),
                                   capture_output=True, text=True)
                if p.returncode != 0:
                    add_line("设口令失败：%s" % (p.stderr or p.stdout).strip()[:200])
                else:
                    add_line("用户创建完成：%s" % username)
        else:
            add_line("未提供用户名，系统中将没有可登录的账号")

        if esp_dev:
            set_state(step="安装引导")
            add_line("安装引导程序：grub-efi（bootloader-id=camphor）")
            env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
            p = subprocess.run(
                ["chroot", TARGET, "grub-install", "--target=x86_64-efi",
                 "--efi-directory=/boot/efi", "--bootloader-id=camphor", "--recheck"],
                capture_output=True, text=True, env=env,
            )
            if p.returncode != 0:
                raise RuntimeError("grub-install 失败：%s" % (p.stderr or p.stdout).strip()[:400])
            # 母本里刻意不带 /etc/default/grub（那是 conffile，装不进去），
            # 这里按装好的系统补一份：root 由 grub-mkconfig 从 fstab 推，
            # net.ifnames=0 保证无线网卡还叫 wlan0（wifi 服务按名字轮询）。
            grub_default = os.path.join(TARGET, "etc/default/grub")
            os.makedirs(os.path.dirname(grub_default), exist_ok=True)
            if not os.path.exists(grub_default):
                with open(grub_default, "w", encoding="utf-8") as f:
                    f.write(
                        'GRUB_DISTRIBUTOR="Camphor"\n'
                        "GRUB_DEFAULT=0\n"
                        "GRUB_TIMEOUT=3\n"
                        # rootwait 是给 USB 上的根盘用的，console=tty0 让内核日志
                        # 落在 kiosk 那块屏上，panic=10 让崩溃后自己重启。
                        'GRUB_CMDLINE_LINUX="console=tty0 panic=10 rootwait"\n'
                        'GRUB_CMDLINE_LINUX_DEFAULT="quiet loglevel=4 net.ifnames=0"\n'
                        "GRUB_DISABLE_OS_PROBER=false\n"
                        # 主题目录随母本落到 /boot/grub/themes/camphor；
                        # gfxterm 不显式打开，grub-mkconfig 就按文字终端生成，
                        # 主题那句话根本不会写进 grub.cfg。
                        "GRUB_TERMINAL_OUTPUT=gfxterm\n"
                        "GRUB_GFXMODE=1920x1080,1600x900,1366x768,auto\n"
                        "GRUB_THEME=/boot/grub/themes/camphor/theme.txt\n"
                    )
                add_line("生成 /etc/default/grub")
            run(["chroot", TARGET, "update-grub"], check=False)
            install_removable_fallback()
            add_line("引导程序安装完成")

        set_state(step="完成", running=False, done=True)
        add_line("安装完成")
        # 第一阶段到此结束：删掉目标系统里的安装器入口。
        # 它会拦着第二阶段的脚本（那个单元的条件是「这个文件不存在」）。
        stage1 = os.path.join(TARGET, "usr/local/bin/camphor-installer-boot.sh")
        if os.path.exists(stage1):
            os.remove(stage1)
            add_line("移除安装入口：首次启动时安装应用层")
    except Exception as e:  # noqa: BLE001 - 错误要显示在页面上
        add_line("失败：%s" % e)
        set_state(running=False, error=str(e), step="失败")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("installer: " + (fmt % args) + "\n")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/disks":
            try:
                disks, seen = list_disks()
                body = json.dumps(
                    {
                        "disks": disks,
                        "debug": {
                            "dev_candidates": seen,
                            "parted": shutil.which("parted") or "",
                            "uname": os.uname().release,
                        },
                    },
                    ensure_ascii=False,
                )
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
                return
            self._send(200, body)
            return
        if path == "/api/progress":
            with LOCK:
                body = json.dumps(dict(STATE), ensure_ascii=False)
            self._send(200, body)
            return
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(WEB, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "missing index.html", "text/plain")
            return
        if path == "/backdrop.jpg":
            try:
                with open(os.path.join(WEB, "backdrop.jpg"), "rb") as f:
                    self._send(200, f.read(), "image/jpeg")
            except OSError:
                self._send(404, "missing backdrop.jpg", "text/plain")
            return
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode() or "{}")
        except ValueError:
            self._send(400, json.dumps({"error": "请求格式不对"}, ensure_ascii=False))
            return

        if path == "/api/install":
            with LOCK:
                busy = STATE["running"]
            if busy:
                self._send(409, json.dumps({"error": "已经在装了"}, ensure_ascii=False))
                return
            device = data.get("device", "")
            rowid = int(data.get("rowid") or 0)
            username = (data.get("username") or "").strip()
            password = data.get("password") or ""
            if not re.match(r"^/dev/(sd|vd|nvme)", device):
                self._send(400, json.dumps({"error": "设备名不对"}, ensure_ascii=False))
                return
            if not username:
                self._send(400, json.dumps({"error": "必须给一个账号，否则装完没人能登录"}, ensure_ascii=False))
                return
            if not re.match(r"^[a-z_][a-z0-9_-]{0,31}$", username):
                self._send(400, json.dumps({"error": "用户名不能用数字开头，只能用字母、数字、下划线、减号"}, ensure_ascii=False))
                return
            if not password:
                self._send(400, json.dumps({"error": "口令不能为空"}, ensure_ascii=False))
                return
            threading.Thread(target=install_worker,
                             args=(device, rowid, username, password), daemon=True).start()
            self._send(200, json.dumps({"ok": True}))
            return

        if path == "/api/reboot":
            self._send(200, json.dumps({"ok": True}))
            # 目标系统还挂着（根与 ESP），先按挂载树卸掉再同步，能干净就干净；
            # 卸不掉也无所谓 —— 前面已经 sync 过，剩下的靠 ext4 自己的日志。
            subprocess.Popen(["sh", "-c", "umount -R /target 2>/dev/null; sync; sleep 1; reboot -f"])
            return

        if path == "/api/mkpart":
            device = data.get("device", "")
            start = data.get("start")
            end = data.get("end")
            if not re.match(r"^/dev/(sd|vd|nvme)", device) or start is None or end is None:
                self._send(400, json.dumps({"error": "参数不对"}, ensure_ascii=False))
                return
            try:
                body = json.dumps(mkpart(device, start, end), ensure_ascii=False)
            except RuntimeError as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
                return
            self._send(200, body)
            return

        if path == "/api/rmpart":
            device = data.get("device", "")
            index = int(data.get("index") or 0)
            if not re.match(r"^/dev/(sd|vd|nvme)", device) or index <= 0:
                self._send(400, json.dumps({"error": "参数不对"}, ensure_ascii=False))
                return
            try:
                body = json.dumps(rmpart(device, index), ensure_ascii=False)
            except RuntimeError as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
                return
            self._send(200, body)
            return

        self._send(404, json.dumps({"error": "not found"}))


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    # 只听本机：界面和它跑在同一台机器上，没有从外面访问这条路径。
    bind = os.environ.get("CAMPHOR_BIND", "127.0.0.1")
    srv = ThreadingHTTPServer((bind, port), Handler)
    print("installer: listening on %s:%d" % (bind, port), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
