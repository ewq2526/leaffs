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
import tempfile
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
    "pct": 0,
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


def run_percent(cmd, lo, hi):
    """跑一条自己会报百分比的命令（unsquashfs -percentage），把它的 0–100
    换算到 [lo, hi] 这一段进度里。

    展开系统镜像占整个安装的大头。按"第几步"糊出来的进度条在它这里会几十分钟
    不动，装的人只会以为卡死了 —— 所以这一段读它自己吐出来的数字。
    """
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        raise RuntimeError("缺少命令 %s（镜像里没装？）" % cmd[0])
    buf = b""
    last = -1
    while True:
        chunk = proc.stdout.read1(256)
        if not chunk:
            break
        buf += chunk
        if b"\r" not in buf and b"\n" not in buf:
            continue
        parts = re.split(rb"[\r\n]+", buf)
        buf = parts.pop()
        for part in parts:
            part = part.strip()
            if not re.fullmatch(rb"\d{1,3}", part):
                continue
            v = int(part)
            if v == last:
                continue
            last = v
            set_state(pct=lo + (hi - lo) * min(v, 100) // 100)
    code = proc.wait()
    if code != 0:
        raise RuntimeError("%s 失败（退出码 %d）" % (" ".join(cmd), code))


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


USAGE_TMP = "/run/camphor-fsusage"
USAGE_LOCK = threading.Lock()
# 设备 → 只读挂载点。挂上就留着：statvfs 读的是内核里那份超级块，不碰盘，
# 每次挂/卸反而是白花时间。卸只在一个时候做 —— 要动分区表之前。
USAGE_MOUNTS = {}
USAGE_FAILED = set()
# 挂载失败的最后一行原因，界面上要能看见 —— 否则"读不到占用"和"这块是空的"分不清
USAGE_ERROR = {}
# 装系统的时候不需要那排百分比：装的过程中分区表一直在变，挂上去只会碍事
USAGE_ENABLED = True

# 有些文件系统报给 parted 的名字与 mount 认得的不一样
MOUNT_TYPE = {
    "fat16": "vfat", "fat32": "vfat", "fat": "vfat",
    "ntfs": "ntfs3",
}


def _mounted_at(device):
    """设备已经挂着就返回它的挂载点，别再挂第二次。"""
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == device:
                    return parts[1]
    except OSError:
        pass
    return None


def _used_pct(path):
    st = os.statvfs(path)
    if not st.f_blocks:
        return None
    used = st.f_blocks - st.f_bfree
    return int(round(used * 100.0 / st.f_blocks))


def _looks_dirty(text):
    """挂载被拒是不是因为这块卷"脏"（上次没正常关机留下的日志）。

    NTFS 脏卷、ext 需要 fsck 的卷都会被内核拒挂。这种情况和"没有文件系统"
    完全两回事，界面上得分开写 —— 否则装机的人只会以为工具坏了。
    """
    t = (text or "").lower()
    for word in ("dirty", "unclean", "needs recovery", "not clean", "run chkdsk", "chkdsk"):
        if word in t:
            return True
    return False


def _mount_usage(device, fstype):
    """把设备只读挂一次，挂点记下来复用；挂不上就记下来，别每次刷新都再试。"""
    with USAGE_LOCK:
        mp = USAGE_MOUNTS.get(device)
        if mp:
            return mp
        if device in USAGE_FAILED:
            return None
        os.makedirs(USAGE_TMP, exist_ok=True)
        mp = tempfile.mkdtemp(dir=USAGE_TMP, prefix="mnt-")
        # 只读挂载。选项必须跟着驱动这条路走：这里走的是内核里那个文件系统驱动，
        # 所以只能给它认得的选项（ro）。ntfs-3g 那套用户态选项（norecover、force、
        # remove_hiberfile）交给 ntfs3 只会得到 "unknown option"，一挂一个失败。
        opts = "ro"
        attempts = [["mount", "-o", opts, "-t", MOUNT_TYPE.get(fstype, fstype), device, mp],
                    # 名字对不上时（fat32 之类）退回让 mount 自己按超级块猜
                    ["mount", "-o", opts, device, mp]]
        errs = []
        for cmd in attempts:
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            except (OSError, subprocess.SubprocessError) as exc:
                errs.append(str(exc))
                continue
            if p.returncode == 0:
                USAGE_MOUNTS[device] = mp
                USAGE_ERROR.pop(device, None)
                return mp
            errs.append((p.stderr or p.stdout or "").strip())
        USAGE_FAILED.add(device)
        # 取有用的那几行：mount 的正文在第一行，dmesg 提示那种尾巴没有信息量
        lines = [p.strip() for p in (errs[-1] if errs else "").splitlines() if p.strip()]
        USAGE_ERROR[device] = (" ".join(lines) or "挂载失败")[:160]
        try:
            os.rmdir(mp)
        except OSError:
            pass
        return None


def release_usage_mounts(device=None):
    """卸掉读占用用的只读挂载。

    留给"马上要改分区表"的调用者：设备挂着的时候 parted 删/建分区会报 EBUSY。
    device 为空就把所有这类挂载都卸掉。
    """
    with USAGE_LOCK:
        for dev in list(USAGE_MOUNTS):
            if device is not None and dev != device:
                continue
            mp = USAGE_MOUNTS.pop(dev)
            subprocess.run(["umount", "-l", mp], capture_output=True, timeout=20)
            try:
                os.rmdir(mp)
            except OSError:
                pass
            # 卸过之后这块盘的分区可能会变，缓存过的"挂不上"也该重新试
            USAGE_FAILED.discard(dev)
            USAGE_ERROR.pop(dev, None)


def _cleanup_usage_mounts():
    """进程里没记着、文件系统上还挂着的，收干净（上次被打断留下的那种）。"""
    release_usage_mounts()
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            mounts = [line.split()[1] for line in f if len(line.split()) >= 2]
    except OSError:
        return
    for mp in mounts:
        if mp.startswith(USAGE_TMP + "/"):
            subprocess.run(["umount", "-l", mp], capture_output=True, timeout=15)
            try:
                os.rmdir(mp)
            except OSError:
                pass


def fs_usage(device, fstype):
    """分区的已用百分比。

    ext 系列看 dumpe2fs 的块计数，不碰设备；其它文件系统（vfat、exfat、ntfs、
    btrfs、xfs…）没有这种离线读法，就以只读方式挂一次，之后一直读它的 statvfs。
    已经挂着的设备直接用它的挂载点。读不到就返回 None —— 界面上少一个百分比，
    不影响装系统。
    """
    if not USAGE_ENABLED or not fstype or fstype == "swap":
        return None

    if fstype.startswith("ext"):
        try:
            p = subprocess.run(["dumpe2fs", "-h", device],
                               capture_output=True, text=True, timeout=5)
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

    mp = _mounted_at(device) or _mount_usage(device, fstype)
    if not mp:
        return None
    try:
        return _used_pct(mp)
    except OSError:
        return None


def _read_first(path, default=""):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return default


def _disk_bus(name, real):
    """这块盘接在哪条总线上。

    只看得到设备名不够：sda 既可能是 SATA，也可能是 USB 或 virtio。判据是
    /sys/block/<盘> 软链到的那串路径 —— USB 下有 /usb，SATA 走 libata 有 /ata1
    这类节点，virtio 有 /virtio*，SD 卡是 mmcblk。认不出就不写，不猜。
    """
    parts = [part for part in (real or "").split("/") if part]
    # 路径里那一段叫 usb1、usb2（控制器编号），不是光秃秃的 usb
    if any(part.startswith("usb") for part in parts):
        return "usb"
    if name.startswith("nvme"):
        return "nvme"
    if any(part.startswith("virtio") for part in parts):
        return "virtio"
    if name.startswith("mmcblk"):
        return "mmc"
    if any(part.startswith("ata") for part in parts):
        return "sata"
    return ""


def _disk_info(path):
    """这块盘是什么盘、哪一路：型号与总线，分区页标题上要写。

    `queue/rotational` 对 U 盘不可信（很多 U 盘报成 1，内核按"不是 SSD"一刀切），
    所以先按设备路径认 USB，再按 removable 认移动介质，最后才用它分机械/固态。
    """
    name = os.path.basename(path)
    base = os.path.join("/sys/block", name)
    try:
        real = os.path.realpath(base)
    except OSError:
        real = ""
    return {
        "model": _read_first(os.path.join(base, "device", "model")),
        "rotational": _read_first(os.path.join(base, "queue", "rotational")) == "1",
        "usb": _disk_bus(name, real) == "usb",
        "removable": _read_first(os.path.join(base, "removable")) == "1",
        "bus": _disk_bus(name, real),
    }


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
        #
        # 单位用扇区、不用 MiB：parted 打印 MiB 时只保留到小数点后一位，
        # 446467.1 MiB 会印成 "446467MiB"，拿这个值去建分区会压在前一个分区上，
        # parted 于是把它顶到最近的位置，报 "The closest location we can manage is …"。
        # 扇区是精确值，建分区直接用它的起止。
        p = subprocess.run(
            ["parted", "-s", "-j", path, "unit", "s", "print", "free"],
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

        def sectors(value):
            return int(str(value).rstrip("s") or 0)

        def mib(value):
            return round(sectors(value) / 2048.0, 2)

        if not disk_json:
            raise RuntimeError("parted 读不出 %s：%s" % (path, (p.stderr or p.stdout).strip()[:200]))

        # 没有分区表的盘：parted 会照常输出 disk（label 是 unknown），但没有 partitions，
        # 界面上就成了"一块没有行的设备"，点什么都没反应。这里按整块盘给一行空闲区，
        # 选中之后由 mkpart 去建 GPT 表。
        if not disk_json.get("partitions") and disk_json.get("label", "unknown") in ("", "unknown", None):
            total = sectors(disk_json.get("size", "0s"))
            if total <= 4096 * 3:
                raise RuntimeError("%s 太大或太小，读不出可用的容量（%d 扇区）" % (path, total))
            rows = [{
                "rowid": 1,
                "index": None,
                "start": 1.0,
                "end": round((total - 2048) / 2048.0, 2),
                "size": round((total - 4096) / 2048.0, 2),
                "start_s": 2048,
                "end_s": total - 2048,
                "fstype": "",
                "free": True,
                "name": "",
                "flags": "",
                "used_pct": None,
                "used_error": "",
                "used_dirty": False,
                "ptype": "",
                "protected": False,
            }]
            info = _disk_info(path)
            disks.append({"device": path, "size": "%d MiB" % (total // 2048),
                          "rows": rows, **info})
            continue
        size = "%d MiB" % int(sectors(disk_json.get("size", "0s")) / 2048)
        rowid = 0
        for part in disk_json.get("partitions", []):
            num = part.get("number", 0)
            is_free = part.get("type") == "free"
            fstype = "" if is_free else (part.get("filesystem") or "")
            tuuid = (part.get("type-uuid") or "").lower()
            ptype = PART_TYPES.get(tuuid, "")
            used = None
            used_err = ""
            used_dirty = False
            if not is_free and fstype and num:
                dev = partdev(path, num)
                used = fs_usage(dev, fstype)
                if used is None:
                    # 读不到就把原因带上：界面上"读不到"要能说清是为什么
                    raw = USAGE_ERROR.get(dev, "")
                    if _looks_dirty(raw):
                        used_dirty = True
                        used_err = ("这块分区上次没正常关机（脏卷），系统不认它；"
                                    "先在原来的系统里正常关机或检查一次，才能读占用")
                    else:
                        used_err = raw
            rowid += 1
            rows.append({
                "rowid": rowid,
                "index": num if num else None,
                "start": mib(part.get("start", "0s")),
                "end": mib(part.get("end", "0s")),
                "size": mib(part.get("size", "0s")),
                # 精确边界：建分区只认这两个
                "start_s": sectors(part.get("start", "0s")),
                "end_s": sectors(part.get("end", "0s")),
                "fstype": fstype,
                "free": is_free,
                "name": part.get("name", ""),
                "flags": ",".join(part.get("flags", [])),
                "used_pct": used,
                "used_error": used_err,
                "used_dirty": used_dirty,
                "ptype": ptype,
                "protected": ptype in PROTECTED,
            })
        disks.append({"device": path, "size": size, "rows": rows, **_disk_info(path)})
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


def create_partition(device, start_s, end_s, name, fs_type=""):
    """按扇区建一个分区。

    GPT 上 mkpart 的第一个参数是分区**名字**（不是 primary 那种分区类型），
    名字之后可跟文件系统类型，最后才是起止。名字写 "primary" 的记录里
    会真叫 primary，读起来像是没填。

    没有分区表的盘先建 GPT：不然 parted 直接报 "unrecognised disk label"，
    空盘上的第一次建分区（通常就是 ESP）会失败。
    """
    ensure_label(device)
    argv = ["parted", "-s", "-a", "none", device, "unit", "s", "mkpart", name]
    if fs_type:
        argv.append(fs_type)
    argv += ["%ds" % start_s, "%ds" % end_s]
    run(argv)


def mkpart(device, rowid, size_mib, name="camphor", fs_type="ext4"):
    """在一块空闲区里新建分区。rowid 是界面上那一行的编号，size_mib 是要的大小。

    起点用空闲区的扇区边界，不用 MiB 值：parted 打印 MiB 只保留有限位数，
    446467.1 MiB 会印成 "446467MiB"，照这个值建会压在前一个分区上，
    parted 就把它顶到最近位置、报 "The closest location we can manage is …"。

    唯一要抬的是盘的第一个空闲区：GPT 表头占着最前面那些扇区，从 0 起会被拒。

    也不加 `-a opt`：盘上已有分区的对齐偏移不一定是 1 MiB 的整数倍，
    带上它 parted 同样会拒绝精确取值。边界是自己算好的，用 `-a none` 照做。
    """
    # 读占用时挂上的只读挂载必须先卸：设备挂着，parted 改分区表会报 EBUSY
    release_usage_mounts(device)
    ensure_label(device)

    region = None
    for row in layout(device)["rows"]:
        if row["rowid"] == rowid and row.get("free"):
            region = row
            break
    if region is None:
        raise RuntimeError("第 %d 行不是空闲区，建不了分区" % rowid)

    start_s = region["start_s"]
    if start_s < 2048:            # GPT 表头之后第一块可用位置
        start_s = 2048
    end_s = region["end_s"]
    want_s = int(float(size_mib) * 2048)
    if start_s + want_s < end_s:
        end_s = start_s + want_s
    if end_s - start_s < 2048:
        raise RuntimeError("这块空闲区剩下的空间不够（%d MiB）"
                           % int((region["end_s"] - region["start_s"]) / 2048))

    try:
        create_partition(device, start_s, end_s, name, fs_type)
    except RuntimeError as exc:
        raise RuntimeError("建分区失败（%d–%d MiB）：%s"
                           % (start_s // 2048, end_s // 2048, exc))
    run(["partprobe", device], check=False)
    time.sleep(1)
    return layout(device)


def _free_span(device, lo, hi):
    """[lo, hi] 这段扇区范围里剩下的空闲区；没有返回 None。

    交换分区与 ESP 都是从前部往下切的，切完之后这里返回的就是根分区要用那一段。
    前后有相邻空闲区时 parted 会把它们并成一块，所以判断用「有重叠」而不是「完全在里」。
    """
    inside = [r for r in layout(device)["rows"]
              if r["free"] and r["end_s"] > lo and r["start_s"] < hi]
    if not inside:
        return None
    inside.sort(key=lambda r: r["start_s"])
    return inside[0]


def ensure_esp(device, lo=None, hi=None):
    """确保盘上有一块能用的 ESP，返回它的设备名（建不出来就抛错）。

    有现成的 ESP 就复用。没有的话从 [lo, hi] 这段空间的前部切 512 MiB —— 调用方
    保证交换分区已经先切走了，这里不会和它抢；那段空间放不下时再退到盘上其它
    空闲区（按扇区位置从低到高）。都不行就停下，不装作还能装。
    """
    rows = layout(device)["rows"]
    for r in rows:
        if not r["free"] and r["ptype"] == "esp":
            add_line("使用已有 EFI 系统分区：%s" % partdev(device, r["index"]))
            return partdev(device, r["index"])

    spots = []
    if lo is not None:
        here = _free_span(device, lo, hi)
        if here is not None:
            spots.append(here)
    for r in sorted((r for r in rows if r["free"]), key=lambda r: r["start_s"]):
        if not spots or r["rowid"] != spots[0]["rowid"]:
            spots.append(r)

    for spot in spots:
        start_s = max(2048, spot["start_s"])
        if spot["end_s"] - start_s < 512 * 2048:
            continue
        end_s = start_s + 512 * 2048
        set_state(step="新建 EFI 系统分区")
        try:
            create_partition(device, start_s, end_s, "EFI", "fat32")
        except RuntimeError as exc:
            raise RuntimeError("建 EFI 系统分区失败（%d–%d MiB）：%s"
                               % (start_s // 2048, end_s // 2048, exc))
        run(["partprobe", device], check=False)
        time.sleep(2)
        num = ""
        for r in layout(device)["rows"]:
            if not r["free"] and r["start_s"] == start_s:
                num = str(r["index"])
                break
        if not num:
            raise RuntimeError("EFI 系统分区建完却没有看到它")
        esp_dev = partdev(device, num)
        run(["parted", "-s", device, "set", num, "esp", "on"], check=False)
        unmount_device(esp_dev)
        run(["mkfs.vfat", "-F", "32", "-n", "CAMPHOR", esp_dev])
        add_line("创建 EFI 系统分区：%s（512 MiB，fat32，%d MiB 起）"
                 % (esp_dev, start_s // 2048))
        return esp_dev

    raise RuntimeError("这块盘上既没有 EFI 系统分区，也没有 512 MiB 的空闲空间放它")


def rmpart(device, index):
    """删一个分区。挂着的先卸，不然内核不认。"""
    # 这块盘上读占用用的只读挂载也要卸：设备挂着的时候改分区表，
    # 内核读不到新表，partprobe 等于白跑
    release_usage_mounts(device)
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
    # /run/udev 也要挂：os-prober 的 EFI 探测（os-probes/mounted/05efi）先问 udevadm
    # 分区类型（ID_PART_ENTRY_TYPE / ID_PART_ENTRY_SCHEME）来判断哪块是 ESP，
    # 这些属性只在 udev 的库里。不挂 /run/udev 时 udevadm 只给出 DEVNAME/DEVTYPE，
    # 那段判断就把所有分区当成"不是 ESP"直接退出 —— 同盘的 Windows 引导进不了菜单，
    # 而 ext4 上的另一个 Linux 照样认得出（那条探测不看分区类型），
    # 于是看起来就是"Windows 没了、只多出一份别的 Linux"。
    udev_db = "/run/udev"
    if os.path.isdir(udev_db):
        dst = os.path.join(TARGET, "run/udev")
        os.makedirs(dst, exist_ok=True)
        subprocess.run(["mount", "--bind", udev_db, dst], capture_output=True)
    # efivars 单独再挂一次：grub-install 要靠它写 NVRAM 启动项，
    # 光靠 /sys 的 bind 在有的环境里 chroot 内看不到。
    efivars = "/sys/firmware/efi/efivars"
    if os.path.isdir(efivars):
        dst = os.path.join(TARGET, "sys/firmware/efi/efivars")
        os.makedirs(dst, exist_ok=True)
        subprocess.run(["mount", "--bind", efivars, dst], capture_output=True)


def installed_live_packages():
    """目标系统里处于「已安装」状态的 live-* 包名。

    主镜像就是活体根文件系统：live-boot 与 live-config 是 lb config 自己写进
    live.list.chroot 的（不在仓库的包清单里），解压时一并落到盘上。
    """
    p = subprocess.run(["chroot", TARGET, "dpkg-query", "-W",
                        "-f=${Package}\t${db:Status-Abbrev}\n"],
                       capture_output=True, text=True)
    out = []
    for line in (p.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        name, status = parts
        if name.startswith("live-") and status.startswith("ii"):
            out.append(name)
    return out


def prune_dangling_wants():
    """清掉目标系统里指向已不存在单元的 wants/requires 链接。

    卸包只删软件包自己带的文件。`systemctl enable` 建的启用链接不在包里 ——
    live-config 卸载后 /etc/systemd/system/multi-user.target.wants/live-config.service
    会留成断链，每次开机 systemd 都为它报一条。
    """
    sysdir = os.path.join(TARGET, "etc/systemd/system")
    libdir = os.path.join(TARGET, "usr/lib/systemd/system")
    removed = []
    try:
        entries = os.listdir(sysdir)
    except OSError:
        return removed
    for name in entries:
        if not name.endswith((".wants", ".requires")):
            continue
        wantdir = os.path.join(sysdir, name)
        try:
            units = os.listdir(wantdir)
        except OSError:
            continue
        for unit in units:
            path = os.path.join(wantdir, unit)
            if not os.path.islink(path):
                continue
            # 屏蔽用的链接（指向 /dev/null）算存在，别当成断链删掉
            if os.path.lexists(os.path.join(sysdir, unit)):
                continue
            if os.path.lexists(os.path.join(libdir, unit)):
                continue
            try:
                os.unlink(path)
                removed.append(unit)
            except OSError:
                pass
    return removed


def purge_live_packages():
    """卸掉盘上系统里的 live 组件，并重打 initrd。

    live-config 的单元每次开机都被 boot=live 条件跳过，是纯空跑；带着分量的是
    live-boot 的 initramfs 钩子 —— 装好系统的 initrd 是构建期打的，钩子被烤在
    里面，每次启动都用不上。卸包不会自动把它从 initrd 里拿走，所以要再打一次。
    """
    pkgs = installed_live_packages()
    if not pkgs:
        add_line("系统里没有 live 组件")
        return
    add_line("卸掉 live 组件：%s" % " ".join(pkgs))
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    p = subprocess.run(["chroot", TARGET, "dpkg", "--purge"] + pkgs,
                       capture_output=True, text=True, env=env)
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()
        add_line("有包没卸干净：%s" % (tail[-1][:200] if tail else "原因不明"))
    dangling = prune_dangling_wants()
    if dangling:
        add_line("清掉断链：%s" % " ".join(dangling))
    add_line("重打 initrd（去掉 live-boot 的钩子）")
    p = subprocess.run(["chroot", TARGET, "update-initramfs", "-u", "-k", "all"],
                       capture_output=True, text=True, env=env)
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()
        add_line("initrd 没打成：%s" % (tail[-1][:200] if tail else "原因不明"))
    else:
        add_line("initrd 已重打")


SWAP_MIB = 128
# 根分区的最小要求，与界面上的限制同一个数（见"这块只有 N MiB"那条检查）
ROOT_MIN_MIB = 4096
# 盘上还没有 ESP 时要给它留的位置
ESP_MIB = 512


def swap_partition_mib(free_mib, esp_need_mib=ESP_MIB):
    """专用交换分区的大小：128 MiB。

    它不是拿来当交换空间的，是留给内核的退路：内存被写尽时内核要能把页换出去才
    能继续干活（回收、唤醒、给清理内存的那批帮手腾出运行空间）。128 MiB 足够
    周转，又不至于占用盘上本该给系统的空间。切完之后 ESP 与根分区还得放得下，
    放不下就不切（返回 0），不为了交换区把系统装不进去。
    """
    if free_mib < SWAP_MIB + esp_need_mib + ROOT_MIN_MIB:
        return 0
    return SWAP_MIB


def _ram_mb():
    try:
        with open("/proc/meminfo", encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def ensure_swap_partition(device, lo, hi):
    """在 [lo, hi] 这段空间的**前部**切一块专用交换分区，返回 {'dev','uuid','mib'}。

    这段空间就是用户选中的那块安装分区（已有分区会先被删掉，空闲区就是它自己），
    所以交换分区切在安装分区的最前头，根分区用后面剩下的部分。

    为什么是独立分区，而不是根分区里的文件：交换文件要走 ext4 的写路径 ——
    分配块、记日志、writeback —— 这些自身就要占内存；极端情况下那条路先卡住。
    裸分区中间没有文件系统，内核直接读写，是内存耗尽时最稳的一条退路。

    必须最先切：ESP 与根分区都从同一段空间的前部往下吃，先切根就没地方了。
    """
    rows = layout(device)["rows"]
    spot = _free_span(device, lo, hi)
    if spot is None:
        add_line("这块空间里没有空闲区，不切交换分区")
        return None
    has_esp = any(not r["free"] and r["ptype"] == "esp" for r in rows)
    size = swap_partition_mib(spot["size"], 0 if has_esp else ESP_MIB)
    if not size:
        add_line("这块空间只有 %d MiB，切了交换分区就放不下系统，不切"
                 % int(spot["size"]))
        return None

    before = {r["index"] for r in rows if not r["free"]}
    set_state(step="新建交换分区（%d MiB）" % size)
    try:
        after = mkpart(device, spot["rowid"], size, name="CAMPHORSWAP",
                       fs_type="linux-swap")
    except RuntimeError as exc:
        add_line("交换分区没建成：%s" % exc)
        return None
    new = [r for r in after["rows"] if not r["free"] and r["index"] not in before]
    if not new:
        add_line("交换分区建完却没有看到它，跳过")
        return None
    num = new[0]["index"]
    dev = partdev(device, num)
    run(["parted", "-s", device, "set", str(num), "swap", "on"], check=False)
    unmount_device(dev)
    p = subprocess.run(["mkswap", "-L", "camphor-swap", dev],
                       capture_output=True, text=True)
    if p.returncode != 0:
        add_line("mkswap 失败：%s" % (p.stderr or p.stdout or "").strip()[:200])
        return None
    uuid = run(["blkid", "-o", "value", "-s", "UUID", dev]).strip()
    if not uuid:
        add_line("读不到交换分区的 UUID，跳过写 fstab")
        return None
    add_line("交换分区就绪：%s（%d MiB，%d MiB 起，UUID %s）"
             % (dev, size, int(new[0]["start"]), uuid))
    return {"dev": dev, "uuid": uuid, "mib": size}


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


# 提示摆在启动菜单右上角，一个白框三行白字。字号就跟着菜单走（unicode.pf2 那份
# 16 像素），靠框把它跟底图分开，够看清。
WINDOWS_MENU_HINTS = (
    "由于安全限制，",
    "选择从该菜单启动 Windows 会导致 PIN 码重置一次。",
    "为了数据安全请在从此菜单启动前将 BitLocker 关掉。",
)
HINT_FONT = "Unifont Regular 16"
HINT_PAD = 16
HINT_PITCH = 22
HINT_CELL = 8         # 16 像素字体里一个半角字符格宽
HINT_ROW = 16         # 一整格高
HINT_WIDTH = 408      # 最长那行在 16 像素下的宽度
# 上下各留 12 像素，再凑成整格：竖列是整格的块字符，高度不是整格数就接不住横线。
HINT_HEIGHT = ((2 * 12 + HINT_PITCH * len(WINDOWS_MENU_HINTS) + HINT_ROW - 1)
               // HINT_ROW * HINT_ROW)


def _hint_label(left, top, text):
    return ("\n+ label {\n"
            "\tleft = %s\n"
            "\ttop = %s\n"
            '\ttext = "%s"\n'
            '\talign = "left"\n'
            '\tcolor = "#ffffff"\n'
            '\tfont = "%s"\n'
            "}\n" % (left, top, text, HINT_FONT))


def install_windows_hints(target):
    """ESP 上有 Windows 时，在装好系统的启动菜单右上角加一个提示框。返回是否写入。

    只写 `/boot/grub/themes/camphor/theme.txt`（装好的系统用这一份）：介质上的菜单里
    没有 Windows 项，提示放上去只会让人莫名其妙。主题文件不在就什么都不做。
    """
    bootmgfw = os.path.join(target, "boot/efi/EFI/Microsoft/Boot/bootmgfw.efi")
    theme = os.path.join(target, "boot/grub/themes/camphor/theme.txt")
    if not os.path.exists(bootmgfw) or not os.path.exists(theme):
        return False
    with open(theme, encoding="utf-8") as f:
        body = f.read()
    if WINDOWS_MENU_HINTS[0] in body:
        return False
    box = HINT_WIDTH + 2 * HINT_PAD
    left = "100%%-%d" % (box + HINT_PAD)
    right = "100%%-%d" % (HINT_PAD + HINT_CELL)
    # 框是块字符拼的：左右各一列全块（宽 8、高一格 16），上下各一行半块（高 8）。
    # 竖列从框顶铺到框底，跟两条横线重叠一格，角上不留缝；不贴图，也不铺底。
    blocks = []
    for k in range(HINT_HEIGHT // HINT_ROW):
        y = "5%%+%d" % (k * HINT_ROW)
        for x in (left, right):
            blocks.append(_hint_label(x, y, "█"))
    blocks.append(_hint_label(left, "5%", "▀" * (box // HINT_CELL)))
    blocks.append(_hint_label(left, "5%%+%d" % (HINT_HEIGHT - HINT_ROW),
                              "▄" * (box // HINT_CELL)))
    for i, text in enumerate(WINDOWS_MENU_HINTS):
        blocks.append(_hint_label("100%%-%d" % box, "5%%+%d" % (12 + i * HINT_PITCH), text))
    with open(theme, "a", encoding="utf-8") as f:
        f.write("".join(blocks))
    add_line("菜单右上角写明从菜单启动 Windows 的代价与前提")
    return True


def install_worker(device, rowid, username="", password=""):
    global USAGE_ENABLED
    try:
        set_state(running=True, done=False, error="", step="读取磁盘布局", lines=[], pct=2)
        # 从这里开始不再读分区占用：挂载留着只会让 parted 报 EBUSY，
        # 之前页面刷新留下的那几个也要收干净
        USAGE_ENABLED = False
        _cleanup_usage_mounts()
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
        esp_dev = ""
        swap_dev = None

        # 先确定"要装系统的那块空间"。选的是空闲区就是它自己；选的是一块已有分区，
        # 就把那块分区删掉重新分 —— 它反正是要被格式化的，只有删了才能在它的前部
        # 切出交换分区。交换分区、ESP、根分区全都从这一块空间里出，不挪到别处去。
        if row["free"]:
            span_lo = max(2048, row["start_s"])
            span_hi = row["end_s"]
        else:
            span_lo = row["start_s"]
            span_hi = row["end_s"]
            victim = partdev(device, row["index"])
            set_state(step="删除 %s（这块要重新分区）" % victim)
            add_line("删除已有分区 %s，这块空间重新分成：交换 + 根" % victim)
            rmpart(device, row["index"])
            # 删掉之后那块空间变成空闲区；前后紧邻空闲区时 parted 会把它们并成一块，
            # 所以按"有重叠"找，范围取合并后的完整范围。
            merged = None
            for r in layout(device)["rows"]:
                if not r["free"]:
                    continue
                if r["end_s"] <= span_lo or r["start_s"] >= span_hi:
                    continue
                if merged is None or r["size"] > merged["size"]:
                    merged = r
            if merged is None:
                raise RuntimeError("删掉分区之后找不到那块空闲区了")
            span_lo, span_hi = merged["start_s"], merged["end_s"]

        # 三块都从这块空间的**前部**往下吃，顺序不能反：交换分区最靠前（128 MiB），
        # 然后是 ESP（没有现成的才切），最后根分区把剩下的全部用掉。
        swap_dev = ensure_swap_partition(device, span_lo, span_hi)
        esp_dev = ensure_esp(device, span_lo, span_hi)

        region = _free_span(device, span_lo, span_hi)
        if region is None:
            raise RuntimeError("切完交换分区与 EFI 分区之后没有空间建根分区了")
        set_state(step="新建分区")
        add_line("新建根分区：%s（用掉这块空间剩下的 %d MiB）"
                 % (device, int(region["size"])))
        after = mkpart(device, region["rowid"], region["size"])
        nums = [r["index"] for r in after["rows"] if not r["free"] and r["index"]]
        if not nums:
            raise RuntimeError("分区建完却没有看到新分区")
        root_dev = partdev(device, max(nums))
        add_line("根分区：%s" % root_dev)

        if not os.path.exists(root_dev):
            raise RuntimeError("分区设备 %s 不存在" % root_dev)

        set_state(step="格式化 %s" % root_dev, pct=6)
        unmount_device(root_dev)
        add_line("创建文件系统：%s（ext4）" % root_dev)
        run(["mkfs.ext4", "-F", "-L", "camphor", root_dev])

        set_state(step="挂载到 /target", pct=9)
        os.makedirs(TARGET, exist_ok=True)
        run(["mount", "-t", "ext4", root_dev, TARGET])

        if esp_dev:
            unmount_device(esp_dev)
            os.makedirs(os.path.join(TARGET, "boot/efi"), exist_ok=True)
            run(["mount", "-t", "vfat", esp_dev, os.path.join(TARGET, "boot/efi")])
            add_line("挂载 EFI 系统分区：%s → /target/boot/efi" % esp_dev)


        squash = find_squashfs()
        if not squash:
            raise RuntimeError("找不到 live 的 squashfs 映像")
        set_state(step="复制系统文件（%s）" % os.path.basename(squash), pct=12)
        add_line("展开系统镜像：%s → %s" % (squash, TARGET))
        run_percent(["unsquashfs", "-f", "-percentage", "-d", TARGET, squash], 12, 85)

        # ESP 要在解压之后再挂：先挂的话 unsquashfs 会往 ESP 里写，
        # 把一块本来干净的引导分区弄脏。
        if esp_dev:
            os.makedirs(os.path.join(TARGET, "boot/efi"), exist_ok=True)
            run(["mount", "-t", "vfat", esp_dev, os.path.join(TARGET, "boot/efi")])

        set_state(step="写 fstab", pct=87)
        root_uuid = run(["blkid", "-o", "value", "-s", "UUID", root_dev]).strip()
        lines = [
            "# <file system> <mount point> <type> <options> <dump> <pass>",
            "UUID=%s / ext4 defaults,noatime 0 1" % root_uuid,
        ]
        if swap_dev:
            # 优先级 10，低于 zram 的 100：平时用压缩内存把页压小，
            # 压不动了内核才落到这块盘上 —— 那时候它还有地方落页，就不会卡死。
            lines.append("UUID=%s none swap defaults,pri=10 0 0" % swap_dev["uuid"])
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

        # 时区：默认北京时间。镜像里不带时区数据的话就沿用默认值，并在日志里写明。
        # 写法照 Debian：/etc/timezone 是名字，/etc/localtime 是指向 zoneinfo 的链接。
        zone = os.path.join(TARGET, "usr/share/zoneinfo/Asia/Shanghai")
        if os.path.exists(zone):
            os.makedirs(os.path.join(TARGET, "etc"), exist_ok=True)
            with open(os.path.join(TARGET, "etc/timezone"), "w", encoding="utf-8") as f:
                f.write("Asia/Shanghai\n")
            link = os.path.join(TARGET, "etc/localtime")
            try:
                os.unlink(link)
            except OSError:
                pass
            os.symlink("/usr/share/zoneinfo/Asia/Shanghai", link)
            add_line("时区：Asia/Shanghai（默认北京时间）")
        else:
            add_line("镜像里没有 Asia/Shanghai 时区数据，时区沿用镜像默认值")

        # 账号：镜像出厂状态里没有任何可登录的用户（root 是锁定的），
        # 不在这里建一个，装完就没人能进系统。用户名与口令由安装界面填。
        set_state(step="创建账号", pct=90)
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

        # 盘上系统不该带着介质那一层的组件。放在装引导之前：这一步要重打 initrd，
        # 慢十几秒，进度能先出现在页面上。
        set_state(step="移除 live 组件", pct=92)
        purge_live_packages()

        if esp_dev:
            set_state(step="安装引导", pct=95)
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
                        # 第二阶段要装 33 个包、铺文件，一两分钟内屏上必须有进度，
                        # 否则看起来就是死机。所以不带 quiet，loglevel 放到 6。
                        'GRUB_CMDLINE_LINUX_DEFAULT="loglevel=6 net.ifnames=0"\n'
                        "GRUB_DISABLE_OS_PROBER=false\n"
                        # 主题目录随母本落到 /boot/grub/themes/camphor；
                        # gfxterm 不显式打开，grub-mkconfig 就按文字终端生成，
                        # 主题那句话根本不会写进 grub.cfg。
                        "GRUB_TERMINAL_OUTPUT=gfxterm\n"
                        "GRUB_GFXMODE=1920x1080,1600x900,1366x768,auto\n"
                        "GRUB_THEME=/boot/grub/themes/camphor/theme.txt\n"
                    )
                add_line("生成 /etc/default/grub")
            # 主题要在 update-grub 之前改：grub.cfg 里引的是盘上那份 theme.txt。
            install_windows_hints(TARGET)
            run(["chroot", TARGET, "update-grub"], check=False)
            install_removable_fallback()
            add_line("引导程序安装完成")

        set_state(step="完成", running=False, done=True, pct=100)
        add_line("安装完成")
        # 第一阶段到此结束：删掉目标系统里的安装器入口。
        # 它会拦着第二阶段的脚本（那个单元的条件是「这个文件不存在」）。
        stage1 = os.path.join(TARGET, "usr/local/bin/camphor-installer-boot.sh")
        if os.path.exists(stage1):
            os.remove(stage1)
            add_line("移除安装入口：首次启动时安装应用层")
        # 入口删了，注册也要一起撤。单元还挂在 graphical.target.wants 里的话，装好的
        # 系统每次开机都会去启动那个已经不存在的脚本，白多一条 failed。第二阶段删自己
        # 时就是「先 disable 再删文件」；这里在 chroot 里没有 systemd，直接删链接和单元。
        for rel in ("etc/systemd/system/graphical.target.wants/camphor-installer.service",
                    "etc/systemd/system/multi-user.target.wants/camphor-installer.service",
                    "etc/systemd/system/camphor-installer.service"):
            p = os.path.join(TARGET, rel)
            if os.path.islink(p) or os.path.exists(p):
                os.remove(p)
        add_line("移除安装器单元与启用链接")
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
            rowid = int(data.get("rowid") or 0)
            size = data.get("size")
            if not re.match(r"^/dev/(sd|vd|nvme)", device) or rowid <= 0 or size is None:
                self._send(400, json.dumps({"error": "参数不对"}, ensure_ascii=False))
                return
            try:
                body = json.dumps(mkpart(device, rowid, size), ensure_ascii=False)
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
