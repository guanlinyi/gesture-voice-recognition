#!/bin/bash
# SG8A AGX Orin / JetPack 6.2 / L4T R36.4.3
# Back up all affected files before changing the system. Never reboot here.
set -Eeuo pipefail
umask 077
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PACKAGE_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
PYTHON=${PYTHON:-python3}
KVER=5.15.148-tegra
MODULES=/lib/modules/$KVER
BACKUP_ROOT=/var/backups/sg8a-camera
TARGETS=(
    /boot/Image
    "$MODULES/updates/drivers/media/platform/tegra/camera/tegra-camera.ko"
    "$MODULES/updates/drivers/video/tegra/host/nvcsi/nvhost-nvcsi-t194.ko"
    "$MODULES/updates/drivers/media/i2c/max9296.ko"
    "$MODULES/updates/drivers/media/i2c/max9295.ko"
    /boot/tegra234-camera-yuv-gmsl2x8-overlay.dtbo
)
SOURCES=(
    "$PACKAGE_DIR/input_bin/Image.bin"
    "$PACKAGE_DIR/input_ko/tegra-camera.ko"
    "$PACKAGE_DIR/input_ko/nvhost-nvcsi-t194.ko"
    "$PACKAGE_DIR/input_dtbo/tegra234-camera-yuv-gmsl2x8-overlay.dtbo"
)
die() { printf '错误：%s\n' "$*" >&2; exit 1; }
usage() {
    cat <<'EOF'
用法：
  ./install.sh --dry-run             仅检查、显示计划，不修改系统
  sudo ./install.sh                  备份后安装，不自动重启
  sudo ./install.sh --restore 备份目录  恢复受影响文件并重建驱动索引
备份位置：/var/backups/sg8a-camera/Image-内核版本__L4T-R36.4.3__UTC时间.随机字符
EOF
}
MODE=install
BACKUP=
KERNEL_BACKUP_NAME=Image
case "${1:-}" in
    --help|-h) usage; exit 0 ;;
    --dry-run) [[ $# == 1 ]] || die '参数错误'; MODE=dry ;;
    --restore) [[ $# == 2 ]] || die '--restore 需要备份目录'; MODE=restore; BACKUP=$2 ;;
    '') [[ $# == 0 ]] || die '参数错误' ;;
    *) usage; exit 1 ;;
esac
for tool in cp mv rm mkdir mktemp modinfo depmod flock sync sha256sum cmp "$PYTHON" grep find date cat uname dirname tr; do
    command -v "$tool" >/dev/null || die "缺少命令：$tool"
done
# Atomic replacement on the target filesystem, including restoration.
atomic_copy() {
    local src=$1 dst=$2 temp
    mkdir -p -- "$(dirname -- "$dst")" || return 1
    temp=$(mktemp "$(dirname -- "$dst")/.sg8a.XXXXXX") || return 1
    if ! cp -a -- "$src" "$temp" || ! mv -fT -- "$temp" "$dst"; then
        rm -f -- "$temp"
        return 1
    fi
}
# Read the version embedded in the actual Image, not merely uname of the running OS.
image_release() {
    "$PYTHON" - "$1" <<'PYIMAGE'
import re, sys
with open(sys.argv[1], 'rb') as f:
    match = re.search(rb'Linux version ([A-Za-z0-9._+\-]+)', f.read())
print(match.group(1).decode('ascii') if match else 'unknown')
PYIMAGE
}
backup_file() {
    if [[ "$1" == "${TARGETS[0]}" ]]; then
        printf '%s/files%s/%s' "$BACKUP" "$(dirname -- "$1")" "$KERNEL_BACKUP_NAME"
    else
        printf '%s/files%s' "$BACKUP" "$1"
    fi
}
validate_backup() {
    local i state
    [[ -f "$BACKUP/COMPLETE" ]] || die '备份未完成，不能恢复'
    KERNEL_BACKUP_NAME=Image
    if [[ -f "$BACKUP/kernel-filename" ]]; then
        KERNEL_BACKUP_NAME=$(cat "$BACKUP/kernel-filename")
        [[ "$KERNEL_BACKUP_NAME" =~ ^Image--[A-Za-z0-9._+-]+[.]bak$ ]] || die '无效的内核备份文件名'
    fi
    for i in "${!TARGETS[@]}"; do
        [[ -f "$BACKUP/state/$i" ]] || die "备份缺少状态：$i"
        state=$(cat "$BACKUP/state/$i")
        case "$state" in
            present) [[ -f "$(backup_file "${TARGETS[$i]}")" && ! -L "$(backup_file "${TARGETS[$i]}")" ]] || die "备份文件损坏：${TARGETS[$i]}" ;;
            absent) ;;
            *) die '无效备份状态' ;;
        esac
    done
    (cd -- "$BACKUP" && sha256sum -c SHA256SUMS) || die '备份校验失败'
}
restore_files() {
    local i state failed=0
    for i in "${!TARGETS[@]}"; do
        state=$(cat "$BACKUP/state/$i") || { failed=1; continue; }
        if [[ "$state" == present ]]; then
            atomic_copy "$(backup_file "${TARGETS[$i]}")" "${TARGETS[$i]}" || failed=1
        elif [[ "$state" == absent ]]; then
            rm -f -- "${TARGETS[$i]}" || failed=1
        else
            failed=1
        fi
    done
    depmod -a "$KVER" || failed=1
    sync || failed=1
    return "$failed"
}
if [[ "$MODE" != dry ]]; then
    [[ $EUID == 0 ]] || die '请使用 sudo 运行，或先用 --dry-run 检查'
    exec 9>/run/lock/sg8a-camera.lock
    flock -n 9 || die '另一个安装或恢复进程正在运行'
fi
if [[ "$MODE" == restore ]]; then
    validate_backup
    restore_files || die "恢复未完全成功，请勿重启；检查备份：$BACKUP"
    printf '文件已恢复。若内核/驱动已加载，需重启才能切换。备份保留在：%s\n' "$BACKUP"
    exit 0
fi
[[ $(uname -m) == aarch64 ]] || die '只支持 aarch64 Jetson'
[[ $(uname -r) == "$KVER" ]] || die "运行内核必须为 $KVER"
[[ -r /etc/nv_tegra_release ]] || die '未找到 Jetson 系统版本'
grep -qE '^# R36 .*REVISION: 4[.]3,' /etc/nv_tegra_release || die '要求 L4T R36.4.3'
[[ -r /proc/device-tree/model ]] || die '无法识别板卡'
grep -aq 'AGX Orin' /proc/device-tree/model || die '只支持 Jetson AGX Orin'
[[ -d "$MODULES" && -f /boot/Image ]] || die '内核或驱动目录不存在'
for src in "${SOURCES[@]}"; do
    [[ -f "$src" && ! -L "$src" && -s "$src" ]] || die "源文件不存在、为空或为链接：$src"
done
for src in "${SOURCES[1]}" "${SOURCES[2]}"; do
    vermagic=$(modinfo -F vermagic "$src")
    [[ ${vermagic%% *} == "$KVER" ]] || die "驱动内核版本不匹配：$src"
done
for dst in "${TARGETS[@]}"; do
    [[ ! -L "$dst" ]] || die "拒绝修改符号链接：$dst"
    [[ ! -e "$dst" || -f "$dst" ]] || die "目标不是普通文件：$dst"
done
DISK_KVER=$(image_release "${TARGETS[0]}")
NEW_KVER=$(image_release "${SOURCES[0]}")
[[ "$NEW_KVER" == "$KVER" ]] || die "待安装 Image 内核版本无法识别或不匹配：$NEW_KVER"
printf '当前运行内核：%s；磁盘 Image：%s；待安装 Image：%s\n' "$(uname -r)" "$DISK_KVER" "$NEW_KVER"
printf '计划：备份以下文件（同时记录原本不存在的文件），替换内核/两个驱动/DTBO，移除两个旧驱动，再运行 depmod。\n'
printf '  %s\n' "${TARGETS[@]}"
[[ "$MODE" != dry ]] || { printf '检查通过；尚未修改任何系统文件。\n'; exit 0; }
BACKUP_TIME=$(date -u +%Y%m%dT%H%M%SZ)
L4T_VERSION=R36.4.3
IMAGE_HASH=$(sha256sum "${TARGETS[0]}")
IMAGE_HASH=${IMAGE_HASH%% *}
KERNEL_BACKUP_NAME="Image--${DISK_KVER}--L4T-${L4T_VERSION}--${BACKUP_TIME}--sha256-${IMAGE_HASH:0:12}.bak"
mkdir -p -- "$BACKUP_ROOT"
BACKUP=$(mktemp -d "$BACKUP_ROOT/Image-${DISK_KVER}__L4T-${L4T_VERSION}__${BACKUP_TIME}.XXXXXX")
mkdir -- "$BACKUP/files" "$BACKUP/state"
for i in "${!TARGETS[@]}"; do
    dst=${TARGETS[$i]}
    if [[ -e "$dst" ]]; then
        mkdir -p -- "$BACKUP/files$(dirname -- "$dst")"
        cp -a -- "$dst" "$(backup_file "$dst")"
        cmp -- "$dst" "$(backup_file "$dst")"
        printf 'present\n' > "$BACKUP/state/$i"
    else
        printf 'absent\n' > "$BACKUP/state/$i"
    fi
done
printf '%s\n' "$KERNEL_BACKUP_NAME" > "$BACKUP/kernel-filename"
{
    printf 'backup_format=2\ncreated_utc=%s\n' "$BACKUP_TIME"
    printf 'running_kernel_release=%s\n' "$(uname -r)"
    printf 'running_kernel_details=%s\n' "$(uname -a)"
    printf 'disk_image_kernel_release=%s\n' "$DISK_KVER"
    printf 'disk_image_sha256=%s\n' "$IMAGE_HASH"
    printf 'kernel_backup_filename=%s\n' "$KERNEL_BACKUP_NAME"
    printf 'module_tree=%s\n' "$MODULES"
    printf 'installer_required_kernel=%s\nnew_image_kernel_release=%s\n' "$KVER" "$NEW_KVER"
    printf 'l4t=%s\nboard_model=' "$L4T_VERSION"
    tr -d '\000' < /proc/device-tree/model
    printf '\n\n注意：磁盘 Image 与运行中内核可能不同；相同版本号也不代表相同构建。以 SHA-256 区分文件。\n'
    printf '本备份保存受修改的模块，并非整个模块目录、initrd 或完整系统快照。\n'
    for i in "${!TARGETS[@]}"; do
        printf '\ntarget=%s\nstate=%s\n' "${TARGETS[$i]}" "$(cat "$BACKUP/state/$i")"
        if [[ "$(cat "$BACKUP/state/$i")" == present && "${TARGETS[$i]}" == *.ko ]]; then
            printf 'saved_module_vermagic='
            modinfo -F vermagic "$(backup_file "${TARGETS[$i]}")" || printf 'unknown\n'
            printf 'saved_module_dependencies='
            modinfo -F depends "$(backup_file "${TARGETS[$i]}")" || printf 'unknown\n'
        fi
    done
    printf '\nnew_image_sha256='
    sha256sum "${SOURCES[0]}"
} > "$BACKUP/backup-info.txt"
cp -- /etc/nv_tegra_release "$BACKUP/nv_tegra_release"
if [[ -r /etc/os-release ]]; then
    cp -L -- /etc/os-release "$BACKUP/os-release"
fi
for ref in /boot/initrd /boot/config-"$KVER"; do
    if [[ -f "$ref" ]]; then
        sha256sum "$ref" >> "$BACKUP/boot-reference-sha256.txt"
    fi
done
# Reference only: this installer never modifies the boot configuration.
if [[ -f /boot/extlinux/extlinux.conf ]]; then
    cp -a -- /boot/extlinux/extlinux.conf "$BACKUP/extlinux.conf.reference"
fi
(cd -- "$BACKUP" && find . -type f ! -name SHA256SUMS ! -name COMPLETE -exec sha256sum {} + > SHA256SUMS)
touch "$BACKUP/COMPLETE"
sync
printf '备份完成：%s\n' "$BACKUP"
validate_backup
rollback() {
    local status=$1
    trap - ERR INT TERM
    set +e
    printf '安装中断，正在恢复：%s\n' "$BACKUP" >&2
    if restore_files; then
        printf '已恢复安装前文件，请排查错误后再操作。\n' >&2
    else
        printf '自动恢复不完整，请勿重启。使用 --restore 重试，备份：%s\n' "$BACKUP" >&2
    fi
    exit "$status"
}
trap 'rollback $?' ERR
trap 'rollback 130' INT
trap 'rollback 143' TERM
atomic_copy "${SOURCES[1]}" "${TARGETS[1]}"
atomic_copy "${SOURCES[2]}" "${TARGETS[2]}"
rm -f -- "${TARGETS[3]}" "${TARGETS[4]}"
atomic_copy "${SOURCES[3]}" "${TARGETS[5]}"
# Install the kernel last, after drivers and overlay have been written.
atomic_copy "${SOURCES[0]}" "${TARGETS[0]}"
depmod -a "$KVER"
sync
trap - ERR INT TERM
printf '安装完成，未自动重启。备份：%s\n' "$BACKUP"
printf '恢复命令：sudo bash %q --restore %q\n' "$SCRIPT_DIR/install.sh" "$BACKUP"
printf '设备树启用和相机加载步骤见 camera_support/sensing_sg8a/README.md。\n'
