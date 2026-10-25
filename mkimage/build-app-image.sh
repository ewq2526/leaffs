#!/bin/sh
# 打应用层镜像：应用依赖（deb）+ 应用核心（leaffs、单元、脚本、配置、venv）。
#
# 这份镜像作为文件放进主镜像（/opt/camphor/app.squashfs），装系统时随主镜像
# 一起拷到盘上。系统重启后由第二阶段的脚本挂载它、把内容铺进系统、启服务。
# 所以主镜像开机时没有这些服务 —— 它们根本还没被装进来。
#
# 用法：sh build-app-image.sh <仓库> <输出文件>
#
# 环境变量：
#   CAMPHOR_DEPS        包源仓库（必填，一个带 pool/ 的本地 apt 仓库）
#   CAMPHOR_BASE_CHROOT 已装好主镜像的 chroot，用来算「应用层要增量哪些包」（必填）
#   CAMPHOR_PIP_INDEX   Python 包索引
set -eu

REPO=${1:?用法: build-app-image.sh <仓库> <输出>}
OUT=${2:?用法: build-app-image.sh <仓库> <输出>}
R=${CAMPHOR_DEPS:?必须给 CAMPHOR_DEPS（本地包仓库）}
BASE=${CAMPHOR_BASE_CHROOT:?必须给 CAMPHOR_BASE_CHROOT（主镜像的 chroot）}
PIP=${CAMPHOR_PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}

[ -d "$R/pool" ] || { echo "包源不存在：$R" >&2; exit 1; }
[ -f "$BASE/var/lib/dpkg/status" ] || { echo "没有主镜像的 dpkg 状态：$BASE" >&2; exit 1; }

S=$(mktemp -d)
W=$(mktemp -d)
trap 'rm -rf "$S" "$W"' EXIT

# 1 算增量：以主镜像的 dpkg 状态为基准，只要应用层多出来的包。
cat > "$W/sources.list" <<EOF
deb [trusted=yes] file://$R trixie main non-free-firmware
deb [trusted=yes] file://$R trixie-updates main non-free-firmware
deb [trusted=yes] file://$R trixie-security main non-free-firmware
EOF
mkdir -p "$W/lists/partial" "$W/cache/archives/partial"
APT="apt-get -o Dir::Etc::sourcelist=$W/sources.list -o Dir::Etc::sourceparts=- \
	-o Dir::State::lists=$W/lists -o Dir::State::status=$BASE/var/lib/dpkg/status \
	-o Dir::Cache=$W/cache -o APT::Architecture=amd64 \
	-o APT::Install-Recommends=false -o APT::Install-Suggests=false \
	-o Acquire::Languages=none"
$APT update >/dev/null 2>&1 || { echo "apt update 失败（包源：$R）" >&2; exit 1; }

PKGS=$(sed 's/#.*//' "$REPO/mkimage/packages.app.txt" | sed 's/[[:space:]]*$//' | grep -v '^$' | tr '\n' ' ')
$APT -s -y install $PKGS 2>/dev/null | sed -n 's/^Inst \([^ ]*\) .*/\1/p' | sort -u > "$W/list.txt"

mkdir -p "$S/pool" "$S/root"
IDX=$R/dists/trixie/main/binary-amd64/Packages
while read -r p; do
	line=$(awk -v p="$p" '/^Package: /{cur=substr($0,10)} cur==p && /^Filename: /{print substr($0,11); exit}' "$IDX")
	[ -n "$line" ] || continue
	cp -a "$R/$line" "$S/pool/" 2>/dev/null || true
done < "$W/list.txt"

# 2 应用核心
rsync -a "$REPO/app/etc/" "$S/root/etc/"
rsync -a "$REPO/app/opt/" "$S/root/opt/"

# 3 venv：--ignore-installed 是因为系统里也有 idna、certifi，
#    不强制装进 venv 的话，出厂系统缺它们时 leaffs 一 import 就崩。
V=$S/root/opt/leaffs/venv
python3 -m venv --system-site-packages "$V"
"$V/bin/pip" install --ignore-installed --no-cache-dir --retries 5 --timeout 60 \
	-i "$PIP" -r "$S/root/opt/leaffs/requirements.txt" >/dev/null 2>&1 \
	|| { echo "pip 安装失败（索引：$PIP）" >&2; exit 1; }

# venv 里的脚本 shebang 写死了临时路径，改成目标系统路径
grep -rl "$S/root" "$S/root" 2>/dev/null | while read -r f; do
	sed -i "s#$S/root##g" "$f"
done

find "$S/root/opt/leaffs" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$S/root" -name '*.pyc' -delete 2>/dev/null || true
chown -R root:root "$S"

mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"
mksquashfs "$S" "$OUT" -noappend -comp xz -b 1M -quiet >/dev/null
echo "    应用镜像：$(basename "$OUT")  $(stat -c %s "$OUT") 字节"
