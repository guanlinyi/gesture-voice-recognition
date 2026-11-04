#!/bin/bash
# 可直接运行，也可 source 后调用 run_camera；不保存或读取密码。
run_camera() (
# SG8A 默认链路 0010、相机型号 5、设备 /dev/video4。
# 从本机已验证版本整理；公开版本仅通过 sudo 本身完成认证。
set -Eeuo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PACKAGE_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
KVER=5.15.148-tegra
CHECK=0
PREVIEW=1
DESKTOP=0
# 四组链路：CAM0/1、CAM2/3、CAM4/5、CAM6/7；0=6Gb/s，1=3Gb/s。
DEFAULT_RATES=(0 0 1 0)
CAM=5       # SG3S-ISX031C-GMSL2F
PORT=4      # /dev/video4
NEW_MODULES=()
FILES=(max9295.ko max9296.ko sgx-yuv-gmsl2.ko)
NAMES=()
CAMERAS=(
    SG2-IMX390C-5200-GMSL2 SG2-AR0233-5300-GMSL2 SG2-OX03CC-5200-GMSL2
    SG3S-ISX031C-GMSL2 SG2-OX03CC-5200-G2F SG3S-ISX031C-GMSL2F
    SG3S-OX03JC-G2F SG5-IMX490C-5300-GMSL2 SG8S-AR0820C-5300-G2A
    SG8-AR0820C-5300-GMSL2 SG8-OX08BC-5300-G2A SG8-OX08BC-5300-GMSL2
    OMSBDAAN-AA OMSBDAAN-AB DMSBBFAN
)
MODES=(0 0 0 1 0 1 1 2 3 3 3 3 5 4 6)
PINS=(0xffff0007 0xffff0007 0xffff0007 0xffff0007 0xffff0007
      0xffff0007 0xffff0007 0xffff0008 0xffff0007 0xffff0008
      0xffff0007 0xffff0008 0xffff0007 0xffff0007 0xffff0007)
usage() {
    cat <<'EOF'
用法：
  ./quick_bring_up_auto.sh               使用默认值 0010、相机 5、视频设备 4，自动加载驱动并预览
  ./quick_bring_up_auto.sh --check       只检查环境，不加载驱动、不设置相机
  ./quick_bring_up_auto.sh --no-preview  设置相机，不打开预览（可用于 SSH）
  ./quick_bring_up_auto.sh --desktop     桌面启动，结束时保留终端供查看日志
  ./quick_bring_up_auto.sh --check --no-preview
请在运行前接通相机和扩展板电源；本脚本不检测实际电压或 GMSL 链路锁定状态。
退出预览：Ctrl+C。驱动仍保留在内核中；本脚本不自动卸载或重启。
EOF
}
die() {
    printf '错误：%s\n' "$*" >&2
    if ((${#NEW_MODULES[@]})); then failure 1; fi
    exit 1
}
for arg in "$@"; do
    case "$arg" in
        --desktop) DESKTOP=1 ;;
        --check) CHECK=1 ;;
        --no-preview) PREVIEW=0 ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数：$arg" ;;
    esac
done
# 从桌面启动时，在结束后保留终端，便于读取出错原因。
desktop_exit() {
    local code=$?
    trap - EXIT
    if ((DESKTOP)); then
        printf '\n脚本已结束（退出码 %s）。\n' "$code"
        read -r -p '按回车关闭窗口…' _ || true
    fi
    exit "$code"
}
trap desktop_exit EXIT
failure() {
    local code=$1
    trap - ERR INT TERM
    printf '操作已停止（退出码 %s）。\n' "$code" >&2
    if ((${#NEW_MODULES[@]})); then
        printf '本次已加载的模块仍在内核中：%s\n' "${NEW_MODULES[*]}" >&2
    fi
    printf '已成功设置的相机参数可能仍然有效。不会自动卸载驱动；请先查看错误日志，避免反复加载。\n' >&2
    exit "$code"
}
trap 'failure $?' ERR
trap 'failure 130' INT
trap 'failure 143' TERM
loaded() { grep -q "^$1 " /proc/modules; }
printf '供电状态：未检测。本脚本不读取实际供电电压或 GMSL 链路锁定状态。\n'
printf '运行前请先接通相机和扩展板电源；驱动已加载、视频设备存在，都不代表相机已供电或可以出图。\n'
root_cmd() {
    if [[ $EUID == 0 ]]; then "$@"; else sudo -n -- "$@"; fi
}
for tool in uname grep modinfo v4l2-ctl insmod sleep flock; do
    command -v "$tool" >/dev/null || die "未找到 $tool；若缺少 v4l2-ctl，请先安装 v4l-utils"
done
if [[ $EUID != 0 ]]; then command -v sudo >/dev/null || die '未找到 sudo'; fi
[[ $(uname -m) == aarch64 && $(uname -r) == "$KVER" ]] || die "要求 aarch64 / $KVER"
[[ -r /etc/nv_tegra_release ]] || die '未找到 Jetson 版本信息'
grep -qE '^# R36 .*REVISION: 4[.]3,' /etc/nv_tegra_release || die '要求 L4T R36.4.3'
[[ -r /proc/device-tree/model ]] || die '无法读取板卡型号'
grep -aq 'AGX Orin' /proc/device-tree/model || die '要求 Jetson AGX Orin'
[[ -r /proc/modules ]] || die '无法读取已加载驱动列表'
for file in "${FILES[@]}"; do
    path="$PACKAGE_DIR/input_ko/$file"
    [[ -f "$path" && ! -L "$path" && -s "$path" ]] || die "驱动文件不存在、为空或为链接：$path"
    name=$(modinfo -F name "$path")
    [[ "$name" =~ ^[a-zA-Z0-9_]+$ ]] || die "驱动名称异常：$path"
    NAMES+=("$name")
    magic=$(modinfo -F vermagic "$path")
    [[ ${magic%% *} == "$KVER" ]] || die "驱动版本不匹配：$path"
    if loaded "$name"; then
        printf '已加载：%s，将沿用，不能据此确认其与包内文件相同。\n' "$name"
    else
        printf '待加载：%s\n' "$name"
    fi
done
if ((PREVIEW)); then
    for tool in gst-launch-1.0 gst-inspect-1.0; do
        command -v "$tool" >/dev/null || die "未找到 $tool"
    done
    gst-inspect-1.0 v4l2src >/dev/null || die '缺少 GStreamer v4l2src 插件'
    gst-inspect-1.0 xvimagesink >/dev/null || die '缺少 GStreamer xvimagesink 插件'
    if [[ -z ${DISPLAY:-} ]]; then
        if ((CHECK)); then
            printf '提示：DISPLAY 未设置；正式预览需在有显示权限的图形终端运行，或使用 --no-preview。\n'
        else
            die 'DISPLAY 未设置；请在图形终端运行，或使用 --no-preview。不自动强设 DISPLAY=:0'
        fi
    fi
fi
if ! compgen -G '/dev/video*' >/dev/null; then
    printf '提示：尚无 /dev/video*。驱动尚未加载时可能正常，不能据此判断相机是否供电。\n'
fi
if ((CHECK)); then
    printf '软件基础检查通过（工具、版本、驱动文件）。本次未执行驱动加载、相机设置或系统文件修改。\n'
    printf '尚未验证：相机实际供电、GMSL 链路、有效图像帧和显示权限；此结果不代表摄像头已就绪。\n'
    exit 0
fi
# Lock the script inode across accounts without creating a shared temporary file.
exec 9<"${BASH_SOURCE[0]}"
flock -n 9 || die '已有进程使用此脚本启动相机'
RATES=()
if ! loaded "${NAMES[2]}"; then
    RATES=("${DEFAULT_RATES[@]}")
    printf '使用默认链路速率 enable_3G=%s,%s,%s,%s（0=6Gb/s，1=3Gb/s）。\n' "${RATES[@]}"
else
    printf '相机驱动已加载，将沿用当前链路速率，本次不重新设置 enable_3G。\n'
    printf '若驱动是在相机未供电时加载的，补上电源后重跑本脚本不会自动重载驱动，初始化可能仍未完成。\n'
    printf '若因此绿屏或采集超时，请确认供电并保存工作后正常重启，再运行脚本；不要强制卸载驱动。\n'
    params="/sys/module/${NAMES[2]}/parameters/enable_3G"
    if [[ -r "$params" ]]; then
        IFS= read -r current_rates < "$params" || true
        printf '当前 enable_3G：%s\n' "${current_rates:-未知}"
    fi
fi
DEVICE="/dev/video$PORT"
CONTROLS="sensor_mode=${MODES[$CAM]},trig_pin=${PINS[$CAM]}"
case "$CAM" in
    7) CONTROLS+=,trig_mode=1 ;;
    12|13) CONTROLS+=,trig_mode=3 ;;
esac
printf '将配置 %s：%s；参数：%s\n' "$DEVICE" "${CAMERAS[$CAM]}" "$CONTROLS"
# Only loading new modules needs root. An initialized camera can run as a normal user.
needs_root=0
for name in "${NAMES[@]}"; do
    if ! loaded "$name"; then needs_root=1; fi
done
if [[ $EUID != 0 ]] && ((needs_root)); then
    if ! sudo -n -v 2>/dev/null; then
        if [[ -t 0 ]]; then
            sudo -v || die 'sudo 认证失败，尚未加载驱动'
        else
            die "需要先在终端运行：bash '$SCRIPT_DIR/quick_bring_up_auto.sh' --no-preview；完成后重新启动程序。不保存密码。"
        fi
    fi
fi
for i in "${!FILES[@]}"; do
    if loaded "${NAMES[$i]}"; then continue; fi
    args=()
    if ((i == 2)); then
        ((${#RATES[@]} == 4)) || die '驱动加载状态发生变化，请退出后重新运行'
        args+=("enable_3G=${RATES[0]},${RATES[1]},${RATES[2]},${RATES[3]}")
    fi
    root_cmd insmod "$PACKAGE_DIR/input_ko/${FILES[$i]}" "${args[@]}"
    NEW_MODULES+=("${NAMES[$i]}")
    loaded "${NAMES[$i]}" || die "模块未出现在内核列表中：${NAMES[$i]}"
done
for ((i=0; i<10; i++)); do
    [[ -c "$DEVICE" ]] && break
    sleep 0.5
done
[[ -c "$DEVICE" ]] || die "没有找到字符设备 $DEVICE，请检查设备树、接线及驱动日志"
[[ -r "$DEVICE" && -w "$DEVICE" ]] || die "没有 $DEVICE 的读写权限；请检查 video 用户组与设备权限"
printf '已找到 %s，但设备注册成功不代表收到有效图像；若绿屏或超时，请先核对供电和上电顺序，再检查接线及链路配置。\n' "$DEVICE"
# Query the device before setting any controls; failed queries stop the script.
v4l2-ctl -d "$DEVICE" --all
v4l2-ctl -d "$DEVICE" -c "$CONTROLS"
if ((PREVIEW)); then
    printf '开始预览，Ctrl+C 停止视频；驱动和已设置参数不会自动恢复。\n'
    gst-launch-1.0 v4l2src "device=$DEVICE" '!' xvimagesink -ev
else
    printf '相机参数已设置，未打开预览。\n'
fi

)

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    run_camera "$@"
fi
